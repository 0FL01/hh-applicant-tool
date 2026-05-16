# Stage 3b: AI Vacancy Filter for `apply-vacancies`

Add an AI-based pre-filter to the apply pipeline. Uses a dedicated OpenAI-compatible `ChatOpenAI` client to classify whether a vacancy is suitable for the candidate before spending time/API calls on employer profile fetch, cover letter generation, and submission.

---

## 1. CLI Additions in `setup_parser()` (lines ~184-188)

Insert two new arguments after the `--skip-tests` argument (after line 188):

```python
        parser.add_argument(
            "--ai-filter",
            help="Использовать AI для фильтрации вакансий. Режимы: heavy/light",
            choices=["heavy", "light"],
            default=None,
        )
        parser.add_argument(
            "--ai-rate-limit",
            help="Лимит запросов к AI в минуту для фильтрации",
            type=int,
            default=40,
        )
```

---

## 2. Namespace Field (after line 91)

Add at end of `Namespace` class (after `work_format: list[str] | None` on line 91):

```python
    ai_filter: str | None
    ai_rate_limit: int
```

---

## 3. Config Support

### 3.1 Config section shape (`config.json`)
```json
{
  "openai_vacancy_filter": {
    "api_key": "...",
    "base_url": "...",
    "model": "gpt-4o-mini",
    "temperature": 0.0,
    "max_completion_tokens": 1000,
    "rate_limit": 40
  }
}
```
- `base_url` defaults to `https://api.openai.com/v1` (OpenAI-compatible)
- Falls back to the main `openai` config section keys if `openai_vacancy_filter` is absent

### 3.2 Dedicated `ChatOpenAI` instance

Create a separate instance in `run()` alongside the existing cover-letter `self.openai_chat` (after line ~458):

```python
        # AI Vacancy Filter instance
        vf_config = tool.config.get("openai_vacancy_filter", {})
        vf_token = vf_config.get("api_key") or tool.config.get("openai", {}).get("token")
        if self.ai_filter and not vf_token:
            raise ValueError(
                "Токен для AI-фильтрации не задан. "
                "Укажите openai_vacancy_filter.api_key в config.json "
                "или используйте openai.token"
            )
        self._vacancy_filter_ai: ai.ChatOpenAI | None = (
            ai.ChatOpenAI(
                token=vf_token,
                model=vf_config.get("model", "gpt-4o-mini"),
                temperature=vf_config.get("temperature", 0.0),
                max_completion_tokens=vf_config.get("max_completion_tokens", 1000),
                completion_endpoint=(
                    (vf_config.get("base_url") or "").rstrip("/")
                    + "/chat/completions"
                ) if vf_config.get("base_url") else None,
                session=tool.session,
            )
            if self.ai_filter
            else None
        )
```

Add accessor helper:

```python
    @property
    def vacancy_filter_ai(self) -> ai.ChatOpenAI:
        assert self._vacancy_filter_ai is not None
        return self._vacancy_filter_ai
```

---

## 4. New DB Table + Repository

### 4.1 Schema addition (`storage/queries/schema.sql`)

Append after the `employer_sites` table section (before `COMMIT`):

```sql
/* ===================== skipped_vacancies (AI filter) ===================== */
CREATE TABLE IF NOT EXISTS skipped_vacancies (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))) NOT NULL,
    resume_id TEXT NOT NULL,
    vacancy_id INTEGER NOT NULL,
    employer_id INTEGER,
    vacancy_name TEXT NOT NULL,
    alternate_url TEXT,
    reason TEXT NOT NULL DEFAULT 'ai_rejected',
    resume_analysis_mode TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (resume_id, vacancy_id)
);
CREATE INDEX IF NOT EXISTS idx_skipped_vacancies_resume ON skipped_vacancies(resume_id, vacancy_id);
CREATE TRIGGER IF NOT EXISTS trg_skipped_vacancies_updated
AFTER UPDATE ON skipped_vacancies
BEGIN
    UPDATE skipped_vacancies
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = OLD.id;
END;
```

### 4.2 Model (`storage/models/skipped_vacancy.py`)

```python
from __future__ import annotations
from datetime import datetime
from .base import BaseModel

class SkippedVacancyModel(BaseModel):
    id: str | None = None
    resume_id: str
    vacancy_id: int
    employer_id: int | None = None
    vacancy_name: str
    alternate_url: str | None = None
    reason: str = "ai_rejected"
    resume_analysis_mode: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

Register in `storage/models/__init__.py` (if it exists) or simply import where used.

### 4.3 Repository (`storage/repositories/skipped_vacancies.py`)

```python
from __future__ import annotations
from ..models.skipped_vacancy import SkippedVacancyModel
from .base import BaseRepository

class SkippedVacancyRepository(BaseRepository):
    __table__ = "skipped_vacancies"
    model = SkippedVacancyModel
    conflict_columns = ("resume_id", "vacancy_id")

    def is_skipped(self, resume_id: str, vacancy_id: str | int) -> bool:
        cur = self.conn.execute(
            f"SELECT 1 FROM {self.table_name} WHERE resume_id = ? AND vacancy_id = ?",
            (resume_id, int(vacancy_id)),
        )
        return cur.fetchone() is not None

    def remember(
        self,
        *,
        resume_id: str,
        vacancy_id: int | str,
        vacancy_name: str,
        employer_id: int | str | None = None,
        alternate_url: str | None = None,
        reason: str = "ai_rejected",
        resume_analysis_mode: str | None = None,
        commit: bool | None = None,
    ) -> None:
        import typing
        data: dict[str, typing.Any] = {
            "resume_id": resume_id,
            "vacancy_id": vacancy_id,
            "vacancy_name": vacancy_name,
            "alternate_url": alternate_url,
            "reason": reason,
            "resume_analysis_mode": resume_analysis_mode,
        }
        if employer_id is not None:
            data["employer_id"] = employer_id
        self._insert(data, conflict_columns=self.conflict_columns, commit=commit)

    def clear_for_resume(self, resume_id: str, commit: bool | None = None) -> None:
        self.conn.execute(
            f"DELETE FROM {self.table_name} WHERE resume_id = ?", (resume_id,)
        )
        self.maybe_commit(commit)
```

### 4.4 Register in `StorageFacade` (`storage/facade.py`)

Add import and field:

```python
from .repositories.skipped_vacancies import SkippedVacancyRepository
# ...
class StorageFacade:
    def __init__(self, conn: sqlite3.Connection):
        # ... existing repos ...
        self.skipped_vacancies = SkippedVacancyRepository(conn)
```

### 4.5 Add migration in `storage/queries/migrations/` (optional, or just add to schema.sql which runs on `init_db`)

Since `init_db` runs `schema.sql` with `CREATE TABLE IF NOT EXISTS`, adding the new table there is sufficient — existing databases with the old schema will create it on next startup.

---

## 5. New Methods on `Operation` Class

### 5.1 `_save_skipped_vacancy(vacancy: SearchVacancy, resume_id: str) -> None`

Writes to `skipped_vacancies` table with `reason='ai_rejected'`.

```python
def _save_skipped_vacancy(self, vacancy: SearchVacancy, resume_id: str) -> None:
    employer = vacancy.get("employer", {})
    self.tool.storage.skipped_vacancies.remember(
        resume_id=resume_id,
        vacancy_id=vacancy["id"],
        vacancy_name=vacancy.get("name", ""),
        employer_id=employer.get("id") if employer else None,
        alternate_url=vacancy.get("alternate_url"),
        reason="ai_rejected",
        resume_analysis_mode=self.ai_filter,  # "heavy" or "light"
    )
```

### 5.2 `_is_vacancy_already_skipped(resume_id: str, vacancy_id: str) -> bool`

Fast DB dedup check to avoid re-asking AI for already-rejected vacancies.

```python
def _is_vacancy_already_skipped(self, resume_id: str, vacancy_id: str) -> bool:
    return self.tool.storage.skipped_vacancies.is_skipped(resume_id, vacancy_id)
```

### 5.3 `_analyze_resume_heavy(resume_id: str) -> str`

Fetches full resume via `GET /resumes/{resume_id}`, extracts structured text for the LLM prompt. Cache per `(resume_id, "heavy")` in an instance dict `self._resume_analysis_cache`.

```python
def _analyze_resume_heavy(self, resume_id: str) -> str:
    """Full resume analysis: title, skills, full experience history."""
    cache_key = (resume_id, "heavy")
    if cache_key in self._resume_analysis_cache:
        return self._resume_analysis_cache[cache_key]
    resume = self.api_client.get(f"/resumes/{resume_id}")
    parts = [f"Название резюме: {resume.get('title', '')}"]
    # Skills text
    if skills := resume.get("skills", ""):
        parts.append(f"Навыки: {skills}")
    # skill_set array
    if skill_set := resume.get("skill_set", []):
        parts.append(f"Технологии: {', '.join(s.strip() for s in skill_set if s.strip())}")
    # Experience history
    experience = resume.get("experience", []) or []
    for exp in experience:
        company = exp.get("company", "")
        position = exp.get("position", "")
        period = f"{exp.get('start') or '?'} — {exp.get('end') or '?'}"
        desc = (exp.get("description") or "").strip()
        parts.append(f"• {company} | {position} ({period})")
        if desc:
            parts.append(f"  {desc[:500]}")  # truncate long descriptions
    text = "\n".join(parts)
    self._resume_analysis_cache[cache_key] = text
    return text
```

### 5.4 `_analyze_resume_light(resume_id: str) -> str`

Lightweight: only resume title and skill_set from the already-loaded resume object (available in `_apply_resume` as `resume` parameter — no extra API call needed).

```python
@staticmethod
def _analyze_resume_light(resume_obj: datatypes.Resume) -> str:
    """Lightweight resume summary from search-result data."""
    title = resume_obj.get("title", "")
    # skill_set is not available from search results, use what we have
    return f"Название резюме: {title}"
```

> **Note**: light mode has restricted data because search-result resumes lack `skills`/`skill_set`. The LLM will only see the resume title and vacancy info.

### 5.5 `_build_vacancy_context(vacancy: SearchVacancy, mode: str) -> str`

Returns a text block describing the vacancy for the LLM. Heavy mode fetches full vacancy with description. Light mode uses only `name` and (optionally) `key_skills` from the search result.

```python
def _build_vacancy_context(self, vacancy: SearchVacancy, mode: str) -> str:
    name = vacancy.get("name", "")
    employer = vacancy.get("employer", {}).get("name", "")
    if mode == "heavy":
        details = self.api_client.get(f"/vacancies/{vacancy['id']}")
        desc = strip_tags(details.get("description") or "")
        # truncate to avoid token blowup
        desc = desc[:2000] if len(desc) > 2000 else desc
        return f"Название: {name}\nКомпания: {employer}\nОписание: {desc}"
    else:
        return f"Название: {name}\nКомпания: {employer}"
```

> **Cache**: use `self._vacancy_description_cache` (already exists) to cache fetched vacancy descriptions and avoid duplicate API calls between dedupe and the AI filter.

### 5.6 `_build_filter_system_prompt(mode: str, resume_analysis: str) -> str`

Returns the system prompt for the filter LLM.

```python
@staticmethod
def _build_filter_system_prompt(mode: str, resume_analysis: str) -> str:
    return (
        "Определи, подходит ли вакансия кандидату.\n"
        "Смотри в первую очередь на тип работы (роль), а не на технологии.\n"
        "Правила:\n"
        "1. Если работа по сути другая -> suitable = false\n"
        "2. Если роль совпадает или очень близкая:\n"
        "   - есть пересечения по задачам или навыкам -> suitable = true\n"
        "3. Общие технологии сами по себе ничего не значат.\n"
        "4. Если данных мало -> ориентируйся на название роли\n"
        "Не пиши объяснения.\n"
        "Ответ строго JSON:\n"
        '{"suitable": true} или {"suitable": false}\n'
        "\n"
        f"Кандидат:\n{resume_analysis}"
    )
```

### 5.7 `_ask_ai_suitability(vacancy_context: str, resume_analysis: str, mode: str) -> bool`

Sends the query to the filter LLM. Returns `True` if suitable (or on network/parse error — don't block pipeline). Retries up to 3 times on invalid JSON.

```python
def _ask_ai_suitability(self, vacancy_context: str, resume_analysis: str, mode: str) -> bool:
    system_prompt = self._build_filter_system_prompt(mode, resume_analysis)
    # The ChatOpenAI class doesn't support per-call system prompt override,
    # so we inject it into the user message or create a temporary instance.
    # Workaround: build a single combined message.
    # Option A: create a temporary ChatOpenAI with the filter system prompt
    vf_ai = self.vacancy_filter_ai
    # Save original system_prompt, swap, send, restore
    original_sp = vf_ai.system_prompt
    vf_ai.system_prompt = system_prompt
    try:
        response = vf_ai.send_message(f"Вакансия:\n{vacancy_context}")
    except ai.base.AIError:
        logger.warning("AI filter call failed, passing vacancy through")
        return True
    finally:
        vf_ai.system_prompt = original_sp
    return self._parse_ai_json_response(response)
```

### 5.8 `_parse_ai_json_response(response_text: str) -> bool`

```python
@staticmethod
def _parse_ai_json_response(response: str) -> bool | None:
    response = response.strip().lower()
    # Direct matches
    if response in ("да", "yes", "true"):
        return True
    if response in ("нет", "no", "false"):
        return False
    # JSON extraction
    m = re.search(
        r'\{\s*"suitable"\s*:\s*(true|false)\s*\}',
        response,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).lower() == "true"
    # Fallback: try to find true/false in the general vicinity
    if re.search(r'\bsuitable["\s]*:[^"]*\btrue\b', response, re.IGNORECASE):
        return True
    if re.search(r'\bsuitable["\s]*:[^"]*\bfalse\b', response, re.IGNORECASE):
        return False
    logger.warning("Could not parse AI filter response: %.200s", response)
    return None  # caller should treat as "pass through"
```

### 5.9 Retry wrapper (in `_ask_ai_suitability`)

Add a retry loop (up to 3 attempts) for non-JSON responses:

```python
def _ask_ai_suitability(self, vacancy_context: str, resume_analysis: str, mode: str) -> bool:
    system_prompt = self._build_filter_system_prompt(mode, resume_analysis)
    vf_ai = self.vacancy_filter_ai
    original_sp = vf_ai.system_prompt
    vf_ai.system_prompt = system_prompt
    try:
        for attempt in range(3):
            try:
                response = vf_ai.send_message(f"Вакансия:\n{vacancy_context}")
            except ai.base.AIError:
                logger.warning("AI filter call failed, passing vacancy through")
                return True
            result = self._parse_ai_json_response(response)
            if result is not None:
                return result
            if attempt < 2:
                logger.debug("Retrying AI filter parse, attempt %d", attempt + 2)
                continue
        logger.warning("AI filter: could not parse response after 3 attempts, passing through")
        return True
    finally:
        vf_ai.system_prompt = original_sp
```

---

## 6. Integration Hook in `_apply_resume()`

Insert the AI filter block **after line 645** (`continue` after the excluded-filter blacklist) and **before line 648** (employer profile fetch).

**Exact insertion point**:

```python
# Line 645:                     continue
# Line 646:
# Line 647:                 # Перед откликом выгружаем профиль компании
#                      👆 INSERT AI FILTER HERE 👆
# Line 648:                 if employer_id and employer_id not in seen_employers:
```

**Code to insert** (after line 645, before the `# Перед откликом выгружаем профиль компании` comment):

```python
                # ── AI vacancy filter ─────────────────────────────────────
                if self.ai_filter:
                    resume_id = resume["id"]
                    vacancy_id = vacancy["id"]

                    # Dedup: skip if already rejected by AI
                    if self._is_vacancy_already_skipped(resume_id, vacancy_id):
                        logger.info(
                            "Пропускаем ранее отклонённую AI вакансию: %s",
                            vacancy["alternate_url"],
                        )
                        continue

                    # Build resume analysis (cached per resume in this loop)
                    if not hasattr(self, "_resume_analysis_cache"):
                        self._resume_analysis_cache: dict[tuple[str, str], str] = {}

                    cache_key = (resume_id, self.ai_filter)
                    if cache_key not in self._resume_analysis_cache:
                        if self.ai_filter == "heavy":
                            analysis = self._analyze_resume_heavy(resume_id)
                        else:
                            analysis = self._analyze_resume_light(resume)
                        self._resume_analysis_cache[cache_key] = analysis
                    resume_analysis = self._resume_analysis_cache[cache_key]

                    vacancy_context = self._build_vacancy_context(vacancy, self.ai_filter)
                    suitable = self._ask_ai_suitability(vacancy_context, resume_analysis, self.ai_filter)

                    if not suitable:
                        logger.info(
                            "AI-фильтр отклонил вакансию: %s",
                            vacancy["alternate_url"],
                        )
                        if not self.dry_run:
                            self._save_skipped_vacancy(vacancy, resume_id)
                        continue

                    logger.debug(
                        "AI-фильтр пропустил вакансию: %s",
                        vacancy["alternate_url"],
                    )
```

---

## 7. Data Flow Summary

```
_apply_resume loop:
  for vacancy in vacancies:
    ...
    # existing skips:
    if relations:                    continue  (line 593)
    if dedupe_key in known:          continue  (line 602)
    if archived:                     continue  (line 609)
    if response_url:                 continue  (line 617)
    if work_format filtered:         continue  (line 626)
    if excluded_filter hits:         continue  (line 645)  ← also blacklists
    ──────────────────────────────────────────────────────────
    [NEW] if ai_filter:
        if already_skipped in DB:   continue            ← fast path
        analyze_resume (once per resume_id+mode)
        build_vacancy_context
        ask_ai_suitability()
        if not suitable:
            save_skipped_vacancy()   continue            ← ai_rejected
    ──────────────────────────────────────────────────────────
    # original pipeline resumes:
    fetch employer profile           (line 648)
    build cover letter               (line 698-728)
    solve test / POST negotiation    (line 760-820)
```

### `_init_args` additions (in `run()`)

Add the following assignments after line ~459:

```python
        # AI filter
        self.ai_filter = args.ai_filter
        self.ai_rate_limit = args.ai_rate_limit
```

### Cache init

Add in `run()` (alongside `self._vacancy_description_cache` on line 456):

```python
        self._resume_analysis_cache: dict[tuple[str, str], str] = {}
```

---

## 8. Rate Limiting

The filter AI calls share the same `ChatOpenAI` rate limiting as the cover letter AI (none built-in). Two strategies:

1. **Config-controlled**: the `--ai-rate-limit` flag + `openai_vacancy_filter.rate_limit` controls how many filter calls we make per minute via a simple `time.sleep(60 / rate_limit)` delay before each AI call.
2. **No explicit rate limiter**: rely on the existing API delay + the per-vacancy iteration speed; bulk batches of 20-100 vacancies may hit OpenAI rate limits.

**Simple rate limiter** (add before `vf_ai.send_message(...)` in `_ask_ai_suitability`):

```python
        if self.ai_rate_limit > 0:
            delay = 60.0 / self.ai_rate_limit
            time.sleep(delay)
```

---

## 9. Verification

### Automated
```bash
pytest
# Add new tests in tests/test_apply_vacancies_ai_filter.py:
#   - test_parse_ai_json_response_true
#   - test_parse_ai_json_response_false
#   - test_parse_ai_json_response_json
#   - test_parse_ai_json_response_garbage_returns_none
#   - test_is_vacancy_already_skipped
#   - test_save_skipped_vacancy
#   - test_build_filter_system_prompt
#   - test_analyze_resume_light
```

### Manual
```bash
# Light mode (uses only vacancy name from search results)
python -m hh_applicant_tool apply-vacancies --ai-filter light --dry-run

# Heavy mode (fetches full vacancy descriptions for richer analysis)
python -m hh_applicant_tool apply-vacancies --ai-filter heavy --dry-run

# With custom rate limit
python -m hh_applicant_tool apply-vacancies --ai-filter light --ai-rate-limit 20 --dry-run
```

### DB check
```bash
sqlite3 config/<profile>/data "SELECT * FROM skipped_vacancies;"
```

### Edge cases
- `--dry-run` with `--ai-filter`: filter runs (prints decisions) but skips are **NOT** persisted to DB
- No `openai_vacancy_filter` config + no `openai.token`: raises `ValueError` at startup
- AI call fails (network error): vacancy passes through (returns `True`) — pipeline continues
- Already-rejected vacancy in DB: fast skip without AI call (dedup check first)
- Same vacancy ID, different resume: separate entries (unique key is `(resume_id, vacancy_id)`)

---

## 10. File Change Checklist

| File | Change |
|------|--------|
| `operations/apply_vacancies.py` | CLI args, Namespace fields, `run()` init, 7 new methods, integration hook, imports (`re`, `time` already imported) |
| `storage/queries/schema.sql` | Add `skipped_vacancies` table + index + trigger |
| `storage/models/skipped_vacancy.py` | NEW — model dataclass |
| `storage/repositories/skipped_vacancies.py` | NEW — repository class |
| `storage/facade.py` | Register `SkippedVacancyRepository` |
| `utils/json.py` (already imported) | No change needed |
| `api/datatypes.py` (already imported) | No change needed |
