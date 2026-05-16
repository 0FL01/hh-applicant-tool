# Stage 0+4: Quick fixes from upstream

## Stage 0: JSONDecodeError fix

**File:** `src/hh_applicant_tool/api/client.py`

**What:** On line 114, `except as_json.decoder.JSONDecodeError` references `as_json` which is a `bool` method parameter (line 76), not the `json` module. This raises `AttributeError: 'bool' object has no attribute 'decoder'` before any JSON decode error can be caught. The fix is to use the standard library exception `json.JSONDecodeError`.

**Changes:**
1. Add `import json` among existing imports at the top of the file (lines 1–10).
2. Line 114: Change `except as_json.decoder.JSONDecodeError as ex:` → `except json.JSONDecodeError as ex:`

**Verification:**
- `pytest` passes
- `python -c "from hh_applicant_tool.api.client import BaseClient; print('ok')"` succeeds
- No `AttributeError` when the API returns malformed JSON

---

## Stage 4: `check_same_thread=False`

**File:** `src/hh_applicant_tool/main.py`, line 211

**What:** `sqlite3.connect(self.db_path)` is called without `check_same_thread=False`. The daemon mode of `chat-agent` runs in threads and shares the same `sqlite3.Connection` across threads, causing `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`. The fix adds `check_same_thread=False`.

**Change:**
- Line 211: `conn = sqlite3.connect(self.db_path)` → `conn = sqlite3.connect(self.db_path, check_same_thread=False)`

**Why this is the only place needing the fix:** Only one `sqlite3.connect()` call exists in the main application (`src/`). The other call in `hh_llm_agent/tg_collector_store.py:38` uses a short-lived connection per call (context manager), so it is not affected by the same threading issue.

**Verification:**
- `chat-agent --daemon` does not crash with `ProgrammingError` when processing chats concurrently
- `pytest` passes
