# Stage 2: Port `openai_session` — separate HTTP session for AI traffic

**Goal:** Give AI (OpenAI) requests their own dedicated HTTP session with an independently configurable proxy, instead of sharing the main `self.session`.

---

## File: `src/hh_applicant_tool/main.py` (425 lines)

### 1. `BaseNamespace` — add field (line ~60)

After line 59 (`proxy_url: str`), insert:

```python
    openai_proxy_url: str
```

**Edit target:** line 59 → 60 (insert after `proxy_url`).

---

### 2. `_create_parser()` — add CLI argument (line ~115)

After the `--proxy-url` block (current lines 112–115), insert:

```python
        parser.add_argument(
            "--openai-proxy",
            "--ai-proxy",
            dest="openai_proxy_url",
            help="Отдельный прокси, используемый только для OpenAI чата",
        )
```

**Edit target:** insert after line 115 (after `parser.add_argument("--proxy-url", ...)` block).

---

### 3. `_get_proxies()` — keep as-is (lines 144–162)

No changes. The existing method returns **general** proxy dict (CLI `--proxy-url` → config `proxy_url` → env vars).

---

### 4. `_get_openai_proxies()` — new method (insert after line 162)

Insert between `_get_proxies()` and the `session` cached_property. Place this new method:

```python
    def _get_openai_proxies(self) -> dict[str, str]:
        """Resolve proxy for AI (OpenAI) requests.

        Precedence:
          1. CLI --openai-proxy / --ai-proxy  (self.args.openai_proxy_url)
          2. config.json → openai.proxy_url
          3. Fall back to general proxies (_get_proxies)
        """
        openai_config = self.config.get("openai", {})
        proxy_url = self.args.openai_proxy_url or openai_config.get("proxy_url")
        if proxy_url:
            return {
                "http": proxy_url,
                "https": proxy_url,
            }
        return self._get_proxies()
```

**Edit target:** insert after line 162 (after `_get_proxies()` method ends, before line 164 `@cached_property`).

---

### 5. `_create_http_session()` — new factory method (insert between new `_get_openai_proxies` and `session`)

```python
    @staticmethod
    def _create_http_session(
        proxies: dict[str, str],
        *,
        log_label: str,
    ) -> requests.Session:
        """Build a requests.Session with proxies, insecure TLS, and desktop UA."""
        session = requests.Session()
        session.verify = False
        if proxies:
            logger.info("Use proxies for %s: %r", log_label, proxies)
            session.proxies = proxies
        session.headers.update({"User-Agent": DEFAULT_DESKTOP_USER_AGENT})
        return session
```

**Edit target:** insert after the `_get_openai_proxies()` method, before `session` cached_property.

---

### 6. Refactor `session` cached_property (lines 164–181) — use factory

Replace the current body with a call to the factory, keeping cookie-jar logic:

```python
    @cached_property
    def session(self) -> requests.Session:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        session = self._create_http_session(
            self._get_proxies(),
            log_label="requests",
        )

        session.cookies = HHOnlyCookieJar(str(self.cookies_file))
        if self.cookies_file.exists():
            session.cookies.load(ignore_discard=True, ignore_expires=True)

        return session
```

**Edit target:** replace lines 164–181.

**Diff summary:** remove the old `session.verify = False`, proxy-setting block, and `User-Agent` header (now in `_create_http_session`).

---

### 7. `openai_session` — new `@cached_property` (insert after `session`, before `config_path`)

Insert after the `session` cached_property (after line 181, before `config_path` at line 183):

```python
    @cached_property
    def openai_session(self) -> requests.Session:
        """Separate HTTP session for OpenAI/AI requests with its own proxy."""
        return self._create_http_session(
            self._get_openai_proxies(),
            log_label="OpenAI requests",
        )
```

**Edit target:** insert after line 181 (after the `session` cached_property block).

---

### 8. `get_openai_chat()` — use `self.openai_session` (line 314)

Change line 314:

```python
-            session=self.session,
+            session=self.openai_session,
```

**Edit target:** line 314 only.

---

## What NOT to port

| Upstream concept         | Reason |
|--------------------------|--------|
| `MegaTool` / `VersionChecker` | Local already has an empty stub; keep as-is. |
| `_assign_args()` pattern | Local uses `self.args` attribute set by `_parse_args`; keep existing pattern. |
| Multiple AI factories (`get_cover_letter_ai`, `get_vacancy_filter_ai`, `get_captcha_ai`) | Local keeps unified `get_openai_chat()`; port only when needed. |

---

## Edit summary table

| # | What | Location | Type |
|---|------|----------|------|
| 1 | `BaseNamespace.openai_proxy_url: str` | line ~60 | insert field |
| 2 | `--openai-proxy / --ai-proxy` argument | after line 115 | insert `add_argument` |
| 3 | `_get_openai_proxies()` method | after line 162 | insert new method |
| 4 | `_create_http_session()` static method | after `_get_openai_proxies` | insert new method |
| 5 | Refactor `session` cached_property | lines 164–181 | replace body |
| 6 | `openai_session` cached_property | after line 181 | insert new method |
| 7 | `get_openai_chat()` use `self.openai_session` | line 314 | one-line change |

---

## Verification

1. **Syntax & tests:**
   ```bash
   pytest
   ```

2. **Argument shows up in help:**
   ```bash
   python -m hh_applicant_tool config --help
   python -m hh_applicant_tool apply-vacancies --help
   ```
   Both should show `--openai-proxy / --ai-proxy` in the global options.

3. **Functional smoke test (dry-run):**
   ```bash
   python -m hh_applicant_tool apply-vacancies --openai-proxy http://127.0.0.1:9999 --use-ai --dry-run
   ```
   Logger should emit `Use proxies for OpenAI requests: {'http': 'http://127.0.0.1:9999', 'https': 'http://127.0.0.1:9999'}`.

4. **Config-only proxy:**
   ```bash
   python -m hh_applicant_tool config --openai-proxy http://example.com:8080
   ```
   Verify the argument is accepted without error.

5. **Fallback to general proxy:**
   - No `--openai-proxy`, no `openai.proxy_url` in config, but `--proxy-url http://general:3128` set → AI requests should use `http://general:3128`.
   - `pytest` + manual log inspection.
