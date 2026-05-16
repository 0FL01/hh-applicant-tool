# Stage 3a — `skipped_vacancies` table: Model + Repository

## Why

Upstream AI vacancy filter needs to persist skipped vacancies (with reason) so they are never re-processed. This stage adds the database table, model, repository, and wires it into `StorageFacade`.

## Files to create/modify

| # | Action | File |
|---|--------|------|
| 1 | **Modify** | `src/hh_applicant_tool/storage/queries/schema.sql` |
| 2 | **Create** | `src/hh_applicant_tool/storage/models/skipped_vacancy.py` |
| 3 | **Create** | `src/hh_applicant_tool/storage/repositories/skipped_vacancies.py` |
| 4 | **Modify** | `src/hh_applicant_tool/storage/facade.py` |

**No changes needed** for `storage/models/__init__.py` or `storage/repositories/__init__.py` — both are empty files (0 lines).

---

## 1. Schema migration — `schema.sql`

### Pattern reference

- **`vacancy_response_dedup`** (lines 59–71) — same UUID primary key, `UNIQUE` constraint, `created_at`/`updated_at` defaults.
- **Trigger pattern** — `trg_vacancy_response_dedup_updated` (lines 224–230) — follow exact same `CREATE TRIGGER` form.
- **Index pattern** — `idx_vacancy_response_dedup_resume_key` (line 187) — add after all table definitions, before the `COMMIT`.

### Where to insert

1. **Table**: insert **after line 71** (after `vacancy_response_dedup` table block, before `/* ===================== negotiations ===================== */` on line 72).
2. **Index**: insert **after line 187** (after the `idx_vacancy_response_dedup_resume_key` index line).
3. **Trigger**: insert **after line 230** (after `trg_vacancy_response_dedup_updated` trigger block).

### Table DDL (insert after line 71)

```sql
/* ===================== skipped_vacancies ===================== */
CREATE TABLE IF NOT EXISTS skipped_vacancies (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))) NOT NULL,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    alternate_url TEXT,
    name TEXT,
    employer_name TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (resume_id, vacancy_id)
);
```

### Index (insert after line 187)

```sql
CREATE INDEX IF NOT EXISTS idx_skipped_vacancies_resume ON skipped_vacancies(resume_id, vacancy_id);
```

### Trigger (insert after line 230, before `/* ===================== employer_sites ===================== */`)

```sql
CREATE TRIGGER IF NOT EXISTS trg_skipped_vacancies_updated
AFTER
UPDATE ON skipped_vacancies BEGIN
UPDATE skipped_vacancies
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
```

---

## 2. Model — `storage/models/skipped_vacancy.py`

### Pattern reference

**File:** `src/hh_applicant_tool/storage/models/vacancy_response_dedup.py`

| Aspect | `vacancy_response_dedup.py` | New file should follow |
|--------|----------------------------|------------------------|
| Import `BaseModel` | `from .base import BaseModel` | Same |
| Import `datetime` | `from datetime import datetime` | Same |
| Class name | `VacancyResponseDedupModel(BaseModel)` | `SkippedVacancyModel(BaseModel)` |
| Fields | `id: str \| None = None`, `resume_id: str`, ... | See below |

### New file content

```python
from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class SkippedVacancyModel(BaseModel):
    id: str | None = None
    resume_id: str
    vacancy_id: str
    reason: str
    alternate_url: str | None = None
    name: str | None = None
    employer_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

---

## 3. Repository — `storage/repositories/skipped_vacancies.py`

### Pattern reference

**File:** `src/hh_applicant_tool/storage/repositories/vacancy_response_dedup.py`

| Aspect | `vacancy_response_dedup.py` | New file should follow |
|--------|-----------------------------|------------------------|
| Import model | `from ..models.vacancy_response_dedup import VacancyResponseDedupModel` | `from ..models.skipped_vacancy import SkippedVacancyModel` |
| Import base | `from .base import BaseRepository` | Same |
| `__table__` | `"vacancy_response_dedup"` | `"skipped_vacancies"` |
| `conflict_columns` | `("resume_id", "dedupe_key")` | `("resume_id", "vacancy_id")` |
| Custom `remember()` | Uses `self._insert(data, conflict_columns=...)` | Use `self.save()` / `self._insert()` |
| Custom queries | `self.conn.execute("SELECT ...")` | Same pattern for `is_skipped()` and `clear()` |

### New file content

```python
from __future__ import annotations

from typing import Any

from ..models.skipped_vacancy import SkippedVacancyModel
from .base import BaseRepository


class SkippedVacanciesRepository(BaseRepository):
    __table__ = "skipped_vacancies"
    model = SkippedVacancyModel
    conflict_columns = ("resume_id", "vacancy_id")

    def is_skipped(self, resume_id: str, vacancy_id: str) -> bool:
        """Return True if this resume+vacancy pair was already skipped."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM skipped_vacancies WHERE resume_id = ? AND vacancy_id = ?",
            (resume_id, vacancy_id),
        )
        return cur.fetchone()[0] > 0

    def save(self, data: SkippedVacancyModel | dict[str, Any], /, **kwargs: Any) -> None:
        """Insert a skipped vacancy record. Uses INSERT OR IGNORE logic via conflict_columns."""
        if isinstance(data, dict):
            data = self.model.from_api(data)
        self._insert(data.to_db(), conflict_columns=self.conflict_columns, **kwargs)

    def clear(self, reason: str | None = None, /, commit: bool | None = None) -> int:
        """Delete all skipped vacancies, or only those with a matching reason.
        Returns the number of deleted rows."""
        if reason is not None:
            cur = self.conn.execute(
                "DELETE FROM skipped_vacancies WHERE reason = ?",
                (reason,),
            )
        else:
            cur = self.conn.execute("DELETE FROM skipped_vacancies")
        self.maybe_commit(commit)
        return cur.rowcount if cur.rowcount != -1 else 0
```

**Rationale for `save()` override** (instead of using inherited `save`):

The base class `save()` calls `self._insert(data)` which checks `conflict_columns` and generates `ON CONFLICT ... DO UPDATE SET ...` for upsert behaviour. But for `skipped_vacancies` the upstream logic means "once skipped, permanently skipped" — we never want to update an existing row. The `conflict_columns` setting with `DO NOTHING` is already the default behaviour of `BaseRepository._insert()` when the conflict columns overlap with the insert columns and there are no update_excludes to set. However, the base `_insert` includes `DO UPDATE SET` logic when `update_set` is non-empty after subtracting conflict columns, pkey, and update_excludes. Since our model only has `resume_id, vacancy_id, reason, alternate_url, name, employer_name` as non-pkey/non-timestamp columns, and `reason` is always provided, the `DO UPDATE SET reason = excluded.reason` would overwrite the original reason. This is undesirable.

The explicit `save()` override above calls `self._insert(data.to_db(), conflict_columns=self.conflict_columns)` which triggers `ON CONFLICT(resume_id, vacancy_id) DO NOTHING` because after excluding conflict_columns and pkey from update_set, only `reason, alternate_url, name, employer_name` remain — but since `update_excludes` defaults to `("created_at", "updated_at")`, those are not excluded. So update_set would be `{'reason', 'alternate_url', 'name', 'employer_name'}`, which is non-empty, and the base class would do `DO UPDATE SET reason = excluded.reason, ...`.

**To guarantee true INSERT OR IGNORE** (never overwrite), we can pass `upsert=False`:

```python
    def save(self, data: SkippedVacancyModel | dict[str, Any], /, **kwargs: Any) -> None:
        if isinstance(data, dict):
            data = self.model.from_api(data)
        self._insert(data.to_db(), upsert=False, **kwargs)
```

But this would raise on duplicate. Better: use `OR IGNORE` manually:

```python
    def save(self, data: SkippedVacancyModel | dict[str, Any], /, **kwargs: Any) -> None:
        if isinstance(data, dict):
            data = self.model.from_api(data)
        row = data.to_db()
        columns = list(row.keys())
        self.conn.execute(
            f"INSERT OR IGNORE INTO {self.table_name} ({', '.join(columns)})"
            f" VALUES (:{', :'.join(columns)})",
            row,
        )
        self.maybe_commit(kwargs.get("commit"))
```

However, looking at the existing `vacancy_response_dedup` repo's `remember()` method, it does use the base `_insert()` with `conflict_columns` — and the `_insert` code shows:

```python
if update_set:
    sql += f" DO UPDATE SET {update_clause}"
else:
    sql += " DO NOTHING"
```

Since `conflict_columns = ("resume_id", "vacancy_id")` and the data dict includes `resume_id`, `vacancy_id`, `reason`, `alternate_url`, `name`, `employer_name` — subtracting conflict_set `{resume_id, vacancy_id}`, pkey `id`, and update_excludes `{created_at, updated_at}` — the update_set would be `{reason, alternate_url, name, employer_name}`, which is non-empty. So `_insert` would do `DO UPDATE SET reason = excluded.reason, ...`, which overwrites the original reason.

**Recommendation**: Use the explicit SQL approach shown above (the `INSERT OR IGNORE` version) to match the "once skipped, never updated" semantics. This is also what the upstream filter expects — once a vacancy is recorded as skipped, re-running the filter should not change the original reason.

Alternatively, for maximum simplicity, we could just call the inherited `save()`, accept the upsert behaviour, and let the `reason` be silently overwritten on re-processing. This matches the `vacancy_response_dedup.remember()` pattern exactly. Decide based on desired semantics:

- **If upsert (overwrite) is acceptable** — use inherited `save()` (no override needed).
- **If insert-or-ignore is required** — use the explicit `INSERT OR IGNORE` override above.

**Recommendation for this stage**: Use the `INSERT OR IGNORE` approach with a thin `save()` override, because the upstream AI filter explicitly said "prevent re-processing" — the reason should remain from the first pass.

---

## 4. Facade integration — `storage/facade.py`

### Pattern reference

Lines 1–38 of `src/hh_applicant_tool/storage/facade.py`.

### Changes

**Line 17** — add import after the `vacancy_response_dedup` import:

```python
from .repositories.skipped_vacancies import SkippedVacanciesRepository
```

**After line 37** (`self.vacancy_response_dedup = ...`) — add the new repository assignment:

```python
        self.skipped_vacancies = SkippedVacanciesRepository(conn)
```

---

## 5. `__init__.py` exports — no changes needed

Both `storage/models/__init__.py` and `storage/repositories/__init__.py` are empty files (0 lines each). The existing model/repository classes are not explicitly re-exported; they are imported by path in the facade and in tests. No changes required.

---

## Summary of all changes

| File | Action | Key lines |
|------|--------|-----------|
| `schema.sql` | Modify | Insert table after line 71, index after line 187, trigger after line 230 |
| `models/skipped_vacancy.py` | Create | New file — 14 lines, follows `vacancy_response_dedup.py` |
| `repositories/skipped_vacancies.py` | Create | New file — 37 lines, follows `vacancy_response_dedup.py` |
| `facade.py` | Modify | Add import on line 17, add `self.skipped_vacancies` after line 37 |
