# Stage 1 — Port upstream rate limiting + retry into `ai/openai.py`

## Goal

Add rate limiting (RPM-based, threaded lock) and exponential retry (with `Retry-After` parsing) to `ChatOpenAI` without breaking any caller that uses `send_message()`.

---

## 1. File to Modify

### Primary: `src/hh_applicant_tool/ai/openai.py` (77 lines)

| Lines | What changes |
|-------|-------------|
| 1 | May need `import threading.Lock` |
| 2 | No change — `dataclass` / `KW_ONLY` stays |
| 3 | No change |
| 11 | No change — `DEFAULT_COMPLETION_ENDPOINT` kept but renamed to `DEFAULT_BASE_URL` for clarity (or keep as-is and alias via property) |
| 18–28 | **Extend `ChatOpenAI` dataclass** — add fields, rename existing fields for backward compat |
| 35–38 | `_default_headers()` — change `token` → `self.api_key` |
| 40–77 | **Rewrite `send_message()`** — retry loop delegating to new `_request()` |

### Secondary: `src/hh_applicant_tool/main.py`

| Lines | What changes |
|-------|-------------|
| 303–315 | `get_openai_chat()` — pass renamed fields (`api_key`, `base_url`) |

### Callers (no changes needed — they use `send_message()` only)

| File | Lines | Usage |
|------|-------|-------|
| `operations/apply_vacancies.py` | 458, 713, 1069, 1093 | `tool.get_openai_chat(...).send_message(...)` |
| `operations/reply_employers.py` | 116, 263 | `tool.get_openai_chat(...).send_message(...)` |

---

## 2. Backward Compatibility Strategy

**Rename local fields to match upstream**, update `main.py` in the same commit. This is safe because `ChatOpenAI` is only constructed in one place (`main.py:get_openai_chat()`).

| Old name | New name | Reason |
|----------|----------|--------|
| `token` | `api_key` | Both upstream and local call `Bearer {key}` |
| `completion_endpoint` | `base_url` | Upstream uses this; config key stays `completion_endpoint` for now (mapped in `main.py`) |

**Keep `send_message(message) -> str` unchanged** — all callers use it. Internally it will use the new retry logic.

**`__post_init__`**: change `self.completion_endpoint` assignment to `self.base_url`.

---

## 3. New Fields to Add (lines 18–28)

```python
from threading import Lock

@dataclass
class ChatOpenAI:
    api_key: str                        # was: token
    _: KW_ONLY
    system_prompt: str | None = None
    timeout: float = 15.0
    temperature: float = 0.7
    max_completion_tokens: int = 1000
    model: str | None = None
    base_url: str = None                # was: completion_endpoint
    session: requests.Session = field(default_factory=requests.Session)
    # --- NEW ---
    max_retries: int = 5                # retry attempts before raising
    rate_limit: int = 40                # requests per minute
    _previous_request_time: float = field(default=0.0, init=False)
    _lock: Lock = field(init=False, repr=False)
```

`DEFAULT_COMPLETION_ENDPOINT` → keep the same value (`"https://api.openai.com/v1/chat/completions"`), rename to `DEFAULT_BASE_URL` for clarity, or leave as-is.

---

## 4. New Property and Methods

### 4a. `_min_request_interval` property

Insert after `__post_init__`. Calculates minimum seconds between requests.

```python
@property
def _min_request_interval(self) -> float:
    """Minimum interval between requests based on rate limit (RPM)."""
    return 60.0 / self.rate_limit if self.rate_limit > 0 else 0.0
```

### 4b. `_request(payload) -> requests.Response` (rate-limited POST)

```python
def _request(self, payload: dict) -> requests.Response:
    """POST with rate limiting lock."""
    with self._lock:
        if self._previous_request_time > 0:
            delay = self._min_request_interval - time.monotonic() + self._previous_request_time
            if delay > 0:
                time.sleep(delay)
        try:
            return self.session.post(
                self.base_url,
                json=payload,
                headers=self._default_headers(),
                timeout=self.timeout,
            )
        finally:
            self._previous_request_time = time.monotonic()
```

Needs `import time` at top of file.

### 4c. `_get_retry_delay(response, attempt) -> float`

```python
def _get_retry_delay(self, response: requests.Response, attempt: int) -> float:
    """Extract retry delay from Retry-After header, fall back to exponential backoff."""
    min_interval = self._min_request_interval or 1.0
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), min_interval)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                return max(parsedate_to_datetime(retry_after).timestamp() - time.time(), min_interval)
            except Exception:
                pass
    return max(min_interval * (attempt + 1), 1.0)
```

### 4d. Update `_default_headers()` — line 35–38

```python
def _default_headers(self) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {self.api_key}",  # was self.token
    }
```

---

## 5. Rewrite `send_message()` — lines 40–77

Replace the current single-shot POST with a retry loop:

```python
def send_message(self, message: str) -> str:
    messages = []
    if self.system_prompt:
        messages.append({"role": "system", "content": self.system_prompt})
    messages.append({"role": "user", "content": message})

    payload = {
        "messages": messages,
        "temperature": self.temperature,
        "max_completion_tokens": self.max_completion_tokens,
    }
    if self.model:
        payload["model"] = self.model

    for attempt in range(self.max_retries + 1):
        try:
            response = self._request(payload)
        except requests.exceptions.RequestException as ex:
            raise OpenAIError(f"Network error: {ex}") from ex

        if response.status_code == 429:
            if attempt >= self.max_retries:
                raise OpenAIError("OpenAI rate limit exceeded")
            delay = self._get_retry_delay(response, attempt)
            logger.warning(
                "Rate limited (attempt %d/%d), sleeping %.1fs",
                attempt + 1, self.max_retries, delay,
            )
            time.sleep(delay)
            continue

        response.raise_for_status()

        data = response.json()
        if "error" in data:
            raise OpenAIError(data["error"]["message"])

        return data["choices"][0]["message"]["content"]
```

Key differences from current code:
- Uses `self._request(payload)` instead of direct `self.session.post(...)`
- Catches 429 for retry, not just `raise_for_status()`
- Logs warning on retry
- `response.raise_for_status()` still runs for non-429, non-200 codes
- `RequestException` is caught outside the retry loop (fatal — no retry on network errors)

---

## 6. Update `main.py:get_openai_chat()` — line 303–315

Change the ChatOpenAI construction to use renamed fields:

```python
def get_openai_chat(self, system_prompt: str) -> ai.ChatOpenAI:
    c = self.config.get("openai", {})
    if not (token := c.get("token")):
        raise ValueError("Токен для OpenAI не задан")
    return ai.ChatOpenAI(
        api_key=token,                          # was: token=token
        model=c.get("model"),
        temperature=c.get("temperature", 0.7),
        max_completion_tokens=c.get("max_completion_tokens", 1000),
        system_prompt=system_prompt,
        base_url=c.get("completion_endpoint"),  # was: completion_endpoint=...
        session=self.session,
    )
```

Config key remains `"completion_endpoint"` — no config file migration needed.

---

## 7. Imports (top of file)

Add to existing imports:

```python
import time
from threading import Lock
```

`parsedate_to_datetime` is imported inside `_get_retry_delay` (to avoid top-level `email` import if not needed).

---

## 8. Verification

```bash
# 1. Unit tests pass
pytest -x -q

# 2. ChatOpenAI instantiates correctly (upstream rename)
python -c "
from hh_applicant_tool.ai import ChatOpenAI
c = ChatOpenAI(api_key='test', base_url='https://example.com')
assert c.api_key == 'test'
assert c.base_url == 'https://example.com'
assert c._lock is not None
assert c._min_request_interval == 1.5  # 60/40
print('OK')
"

# 3. Dry-run apply-vacancies (no real API call)
python -m hh_applicant_tool apply-vacancies --help

# 4. If a valid config/token exists, smoke-test:
# python -m hh_applicant_tool apply-vacancies --use-ai --dry-run ...
```

### Unit-test coverage to add (separate test file or extend existing):

| Test | What it checks |
|------|---------------|
| `test_chat_openai_defaults` | `max_retries=5`, `rate_limit=40`, `_min_request_interval == 1.5` |
| `test_chat_openai_send_message_success` | Mock `_request()` returns 200 + valid JSON → returns content |
| `test_chat_openai_retry_then_success` | Mock returns 429×2, then 200 → exactly 3 attempts |
| `test_chat_openai_retry_then_fail` | Mock returns 429×6 → raises `OpenAIError("rate limit exceeded")` |
| `test_chat_openai_network_error` | Mock raises `RequestException` → raises `OpenAIError` |
| `test_chat_openai_non_429_error` | Mock returns 500 → raises `HTTPError` |
| `test_get_retry_delay_with_header` | Header `Retry-After: 5` → delay=5.0 |
| `test_get_retry_delay_fallback` | No header → `min_interval * (attempt+1)` |
| `test_rate_limit_enforced` | Two rapid calls → second waits for `_min_request_interval` |
| `test_backward_compatible_send_message` | Same signature and return type as before |

---

## 9. Edge Cases / Notes

- **`rate_limit=0`**: `_min_request_interval` returns `0.0` → no rate limiting. `_get_retry_delay` fallback defaults to `1.0`.
- **`max_retries=0`**: Single attempt, 429 immediately raises `OpenAIError`.
- **Shared session**: `main.py` passes `self.session` (same requests Session used for HH API). The lock in `_request()` protects concurrent access to `_previous_request_time`, but the session object itself is shared — safe because `requests.Session` is thread-safe for independent requests.
- **`time.monotonic()` vs `time.time()`**: Used for interval measurement (immune to system clock jumps); `time.time()` only used for `Retry-After` HTTP-date parsing.
- **No breaking change for callers**: `send_message(message)` signature unchanged, all callers unaffected.
