# PRD

## 1. Executive Summary

**TL;DR:** Для `hh-applicant-tool` нужен не CLI-wrapper, а отдельный programmatic service-layer и отдельный MCP entrypoint. MVP должен запускаться по `stdio`, работать в одном profile-context, переиспользовать текущие `ApiClient`, `StorageFacade` и `OpenRouterChatClient`, а destructive-операции обязаны идти через `dry_run=true` по умолчанию, `confirm_apply=true`, лимиты, audit и dedupe.

Текущий репозиторий уже содержит почти все доменные кирпичи для MCP: HH API client, SQLite persistence, dedupe для вакансий, OpenRouter-compatible LLM client и удачный пример service-layer в `chat-agent`, где CLI только адаптер над `ChatAgentService` (`src/hh_applicant_tool/api/client.py:71-132`, `src/hh_applicant_tool/storage/queries/schema.sql:60-85`, `hh_llm_agent/openrouter.py:22-270`, `src/hh_applicant_tool/operations/chat_agent.py:322-344`, `hh_llm_agent/service.py:118-223`). Но вакансионный flow сейчас устроен иначе: `apply-vacancies` держит бизнес-логику внутри `Operation.run()` и `_apply_resume()`, зависит от `argparse`, мутирует `self`, печатает в stdout и делает дополнительные side effects, не связанные напрямую с целью MCP (`src/hh_applicant_tool/operations/apply_vacancies.py:390-521`, `src/hh_applicant_tool/operations/apply_vacancies.py:561-973`).

Целевая архитектура для MVP:

* `proposed HHProfileContext` как non-CLI runtime context;
* `proposed HHApplicantTool.from_profile(...)` как compatibility shim, но не как основная абстракция;
* `proposed VacancyResearchService` как единый service-layer для поиска, анализа, генерации письма и отклика;
* `proposed MCP server` c tool handlers, которые вызывают service-layer и никогда не запускают CLI operations напрямую;
* новые audit-таблицы для vacancy-analysis и application-attempts, при сохранении текущего `vacancy_response_dedup` как authoritative dedupe store.

## 2. Current System Recon

### CLI entrypoint

Пакет публикует один console script: `hh-applicant-tool = "hh_applicant_tool.main:main"`. Отдельный MCP entrypoint not currently implemented (`pyproject.toml:47-48`). В package manifest включены два пакета: `hh_applicant_tool` и `hh_llm_agent`; dependency на MCP library not currently implemented (`pyproject.toml:10-28`).

`HHApplicantTool._create_parser()` строит `argparse` parser, сканирует `src/hh_applicant_tool/operations`, импортирует каждый модуль и привязывает `op.run` через `set_defaults(run=op.run)`. Это значит, что команда в текущей архитектуре — это модуль operation, а не сервис с отдельным API (`src/hh_applicant_tool/main.py:77-140`, `README.md:713-731`).

`HHApplicantTool.__init__()` сразу парсит `argv` и создает каталог профиля. Отдельный non-CLI constructor not currently implemented (`src/hh_applicant_tool/main.py:142-149`). README в разделе “Использование в скриптах” тоже показывает только argv-based путь: создать `HHApplicantTool([...])`, затем вручную вызвать `save_token()`. Там же пример использует `--config-path`, тогда как текущий parser объявляет `--config-dir` / `--config`; это означает, что programmatic usage documented, but still CLI-bound and slightly stale (`README.md:1054-1077`, `src/hh_applicant_tool/main.py:89-96`, `src/hh_applicant_tool/main.py:328-340`).

`HHApplicantTool.run()` — CLI lifecycle wrapper: настраивает логи, вызывает `self.args.run(self)`, ловит исключения и в `finally` сохраняет token/cookies. Это важно: token refresh persistence сегодня привязана к CLI-run lifecycle, а не к reusable service API (`src/hh_applicant_tool/main.py:434-483`).

Даже read-only команды сейчас не являются “чистыми”:

* `whoami` делает `api_client.get("me")`, пишет имя/email/phone в `settings`, затем печатает human-readable summary в stdout (`src/hh_applicant_tool/operations/whoami.py:36-61`);
* `list-resumes` вызывает `tool.get_resumes()`, сохраняет batch в SQLite и печатает `PrettyTable` (`src/hh_applicant_tool/operations/list_resumes.py:33-51`);
* `call-api` разрешает произвольный method/endpoint вызов и печатает JSON в stdout, что делает его слишком широким и небезопасным кандидатом для MCP MVP (`src/hh_applicant_tool/operations/call_api.py:46-80`).

### API client

HTTP слой — synchronous `requests` client. `BaseClient.request()` берет mutex, вставляет delay через `time.sleep`, вызывает `session.request(..., allow_redirects=False)` и не задает явный timeout. Это blocking path без cancellation и без отдельной concurrency strategy (`src/hh_applicant_tool/api/client.py:71-105`).

`HHApplicantTool` создает две HTTP session:

* `session` для HH API;
* `openai_session` для AI/OpenRouter requests.
  Обе строятся из profile config и proxy settings (`src/hh_applicant_tool/main.py:203-224`).

`HHApplicantTool.api_client` lazily создает `ApiClient`, читает profile token из `config.json`, а если `user_agent` отсутствует, генерирует и сохраняет его в config. Это behavior уже покрыт тестами и должен быть сохранен для MCP/programmatic path (`src/hh_applicant_tool/main.py:261-282`, `tests/test_api_client.py:71-88`).

`ApiClient.request()` поверх базового клиента умеет refresh token только в одном случае: если запрос вернул `Forbidden` и локальный `access_expires_at` уже истек. Если HH вернул 403 до локального expiry, auto-refresh не происходит; это подтверждено unit tests (`src/hh_applicant_tool/api/client.py:241-265`, `tests/test_api_client.py:91-158`).

Маппинг HH API ошибок уже есть:

* 3xx -> `Redirect`;
* 400 + `limit_exceeded` -> `LimitExceeded`;
* 403 + `captcha_required` -> `CaptchaRequired`;
* 404 -> `ResourceNotFound`;
* 502 -> `BadGateway`;
* 5xx -> `InternalServerError` (`src/hh_applicant_tool/api/errors.py:76-148`).

### Storage

`StorageFacade` автоматически инициализирует SQLite schema при конструировании и открывает репозитории для резюме, вакансий, переговоров, chat-agent audit и vacancy dedupe/skip (`src/hh_applicant_tool/storage/facade.py:22-40`, `src/hh_applicant_tool/storage/utils.py:53-61`).

Для vacancy flow уже есть полезные persistent primitives:

* `vacancies` cache (`src/hh_applicant_tool/storage/queries/schema.sql:42-58`);
* `vacancy_response_dedup` с `UNIQUE (resume_id, dedupe_key)` (`src/hh_applicant_tool/storage/queries/schema.sql:59-71`);
* `skipped_vacancies` с `UNIQUE (resume_id, vacancy_id)` (`src/hh_applicant_tool/storage/queries/schema.sql:72-85`).

Для chat-agent уже существует зрелый audit/persistence слой:

* `agent_runs` (`src/hh_applicant_tool/storage/queries/schema.sql:109-120`);
* `agent_decisions` (`src/hh_applicant_tool/storage/queries/schema.sql:121-144`);
* `agent_outbox` (`src/hh_applicant_tool/storage/queries/schema.sql:146-160`);
* `agent_webhooks` (`src/hh_applicant_tool/storage/queries/schema.sql:161-178`).

Есть индексы и update triggers, в том числе для dedupe и skipped-vacancies (`src/hh_applicant_tool/storage/queries/schema.sql:198-252`).

Model layer already supports JSON columns through `mapped(store_json=True)`, а `AgentDecisionModel` уже хранит `raw_response` и `reasoning_details`. Это прямой precedent для future `vacancy_analysis` model (`src/hh_applicant_tool/storage/models/base.py:15-59`, `src/hh_applicant_tool/storage/models/base.py:121-131`, `src/hh_applicant_tool/storage/models/agent_decision.py:8-30`).

### apply-vacancies workflow

`apply-vacancies` — главный источник доменной логики для MCP design. Parser объявляет большое число search/apply flags, включая `--dry-run`, `--dedupe-vacancies`, `--skip-tests`, `--ai-filter light|heavy`, `--ai-rate-limit`, `--max-responses` и HH search filters (`src/hh_applicant_tool/operations/apply_vacancies.py:105-205`).

`Operation.run()` делает почти все orchestration work:

* копирует CLI/env/config values в mutable `self.*`;
* применяет скрытый default `work_format=["REMOTE"]`, если ничего не задано;
* создает LLM client для cover letters;
* создает отдельный LLM client для vacancy filter;
* затем вызывает `_apply_vacancies()` (`src/hh_applicant_tool/operations/apply_vacancies.py:390-521`).

Search source today выбирается exactly as stated in the task:

* если есть `self.search`, используется `/vacancies`;
* иначе используется `/resumes/{resume_id}/similar_vacancies` (`README.md:694`, `src/hh_applicant_tool/operations/apply_vacancies.py:1476-1502`).

Внутри `_apply_resume()` уже реализованы важные domain rules:

* сохранить summary vacancy и contacts в SQLite (`src/hh_applicant_tool/operations/apply_vacancies.py:610-625`);
* seed local dedupe from existing `relations` and `vacancy_response_dedup` (`src/hh_applicant_tool/operations/apply_vacancies.py:587-597`, `src/hh_applicant_tool/operations/apply_vacancies.py:1107-1163`);
* skip `relations`, `archived`, `response_url`, unsuitable `work_format` (`src/hh_applicant_tool/operations/apply_vacancies.py:630-687`);
* blacklist filtered vacancies remotely through `PUT /vacancies/blacklisted/{id}` (`src/hh_applicant_tool/operations/apply_vacancies.py:689-705`);
* run AI filter and persist AI rejects in `skipped_vacancies` only when not dry-run (`src/hh_applicant_tool/operations/apply_vacancies.py:708-739`, `src/hh_applicant_tool/operations/apply_vacancies.py:980-990`);
* fetch employer profile and parse employer site (`src/hh_applicant_tool/operations/apply_vacancies.py:746-795`);
* generate cover letter through LLM or template (`src/hh_applicant_tool/operations/apply_vacancies.py:797-829`);
* optionally solve tests by scraping HH HTML (`src/hh_applicant_tool/operations/apply_vacancies.py:838-893`, `src/hh_applicant_tool/operations/apply_vacancies.py:1218-1278`);
* send `POST /negotiations` with small random delay (`src/hh_applicant_tool/operations/apply_vacancies.py:894-919`);
* persist dedupe only when not dry-run (`src/hh_applicant_tool/operations/apply_vacancies.py:921-928`, `tests/test_apply_vacancies.py:194-209`);
* optionally send SMTP email (`src/hh_applicant_tool/operations/apply_vacancies.py:930-959`).

Dedupe algorithm today is meaningful and should be preserved: `sha256(normalized(employer_id + title + description))`, где description берется из `/vacancies/{id}` с fallback на `snippet.requirement` + `snippet.responsibility` (`src/hh_applicant_tool/operations/apply_vacancies.py:1165-1207`). Existing tests already verify this dedupe behavior and dry-run non-persistence (`tests/test_apply_vacancies.py:153-209`).

`max_responses` is not currently implemented. В `_is_filtered()` branch для response count есть TODO comments, а функция возвращает `False` (`src/hh_applicant_tool/operations/apply_vacancies.py:1513-1557`).

### AI filter

Current AI filter supports two resume modes:

* `heavy`: full resume fetch via `/resumes/{id}` with skills and experience history (`src/hh_applicant_tool/operations/apply_vacancies.py:995-1017`);
* `light`: only resume title (`src/hh_applicant_tool/operations/apply_vacancies.py:1019-1023`).

Prompt/output contract today слишком слабый для MCP:

* system prompt expects only `{"suitable": true|false}` and no explanation (`src/hh_applicant_tool/operations/apply_vacancies.py:1035-1051`);
* parser is regex-based (`src/hh_applicant_tool/operations/apply_vacancies.py:1053-1072`);
* on `OpenRouterError` or repeated parse failure, vacancy passes through (`src/hh_applicant_tool/operations/apply_vacancies.py:1074-1096`).

Это дает только boolean suitability, без score/reason/policy awareness, и с fail-open fallback, что неприемлемо для safe auto-apply.

### chat-agent

`chat-agent` уже демонстрирует правильное направление архитектуры. CLI adapter тонкий:

* `Operation.run()` либо заходит в daemon loop, либо вызывает `ChatAgentService(tool, tool.args).run()` и печатает summary (`src/hh_applicant_tool/operations/chat_agent.py:322-344`);
* daemon mode явно сохраняет token/cookies после каждой итерации (`src/hh_applicant_tool/operations/chat_agent.py:356-418`).

`ChatAgentService` строит structured-output flow:

* classifier JSON schema;
* reply JSON schema;
* `complete_json()` against OpenRouter;
* run/decision/outbox persistence (`hh_llm_agent/service.py:37-107`, `hh_llm_agent/service.py:186-223`, `hh_llm_agent/service.py:430-560`, `hh_llm_agent/service.py:1225-1522`).

`HHGateway` — уже существующий narrow abstraction над `tool` и `api_client`, хороший precedent для non-CLI service boundary (`hh_llm_agent/gateway.py:7-58`).

Caveat: chat-agent intentionally does not persist dry-run decisions, and prior dry-run decisions do not block future processing (`hh_llm_agent/service.py:515-526`, `hh_llm_agent/service.py:1193-1211`, `hh_llm_agent/service.py:1460-1461`). Для vacancy MCP правильной будет только вторая часть этого поведения: dry-run не должен блокировать future real apply, но audit для vacancy dry-run все равно нужен.

### OpenRouter client

`OpenRouterChatClient` уже предоставляет нужный foundation:

* plain-text `send_message()` for free-text generation (`hh_llm_agent/openrouter.py:236-249`);
* structured `complete_json()` for JSON schema outputs (`hh_llm_agent/openrouter.py:251-270`);
* `response_format=json_schema`, response-healing plugin, provider-routing fallback and 429 retries (`hh_llm_agent/openrouter.py:95-130`, `hh_llm_agent/openrouter.py:146-227`).

`hh_llm_agent/config.py` already implements a mature precedence model for args/env/config/defaults and OpenRouter settings. MCP should reuse that style, not invent a third incompatible config path (`hh_llm_agent/config.py:50-103`, `hh_llm_agent/config.py:147-314`).

## 3. Problem Statement

MCP нельзя просто “прикрутить” поверх существующей CLI-команды. В `stdio` transport сервер обязан писать в `stdout` только валидные MCP messages, а текущие operations печатают human-readable output в `stdout` (`apply-vacancies`, `whoami`, `list-resumes`, `chat-agent`). Это несовместимо с прямым reuse CLI subcommands as-is (`src/hh_applicant_tool/operations/apply_vacancies.py:572`, `src/hh_applicant_tool/operations/apply_vacancies.py:645`, `src/hh_applicant_tool/operations/apply_vacancies.py:853-855`, `src/hh_applicant_tool/operations/apply_vacancies.py:908-910`, `src/hh_applicant_tool/operations/list_resumes.py:38-51`, `src/hh_applicant_tool/operations/whoami.py:56-60`, `src/hh_applicant_tool/operations/chat_agent.py:337-343`). ([Model Context Protocol][1])

### CLI-bound problems

**`argparse`-coupling.**
Current entrypoint builds behavior by parsing argv and binding subcommands dynamically through `argparse`. There is no stable typed runtime API for “search vacancies”, “analyze vacancy”, “apply vacancy” outside of CLI command execution (`src/hh_applicant_tool/main.py:77-149`, `src/hh_applicant_tool/main.py:449-452`).

**`HHApplicantTool.__init__` is CLI-first.**
Constructor immediately parses `argv` and creates profile directories. `HHApplicantTool.from_profile(...)` not currently implemented. Current script usage in README still goes through fake CLI args and manual token persistence (`src/hh_applicant_tool/main.py:142-149`, `README.md:1054-1077`).

**`Operation.run` is the business boundary.**
Base contract is `BaseOperation.run(tool) -> None | int`, which is CLI-like and exit-code oriented, not service-oriented (`src/hh_applicant_tool/main.py:40-47`). For `apply-vacancies`, almost весь domain workflow живет внутри `Operation.run()` + `_apply_resume()`, а не в reusable service (`src/hh_applicant_tool/operations/apply_vacancies.py:390-521`, `src/hh_applicant_tool/operations/apply_vacancies.py:561-973`).

**`print` and side effects are mixed with domain logic.**
Current operations print to stdout, update SQLite caches/settings, blacklist vacancies remotely, parse employer sites, solve tests via HTML scraping and optionally send SMTP email. That makes them unsuitable as MCP tool handlers (`src/hh_applicant_tool/operations/whoami.py:51-60`, `src/hh_applicant_tool/operations/list_resumes.py:36-51`, `src/hh_applicant_tool/operations/apply_vacancies.py:689-705`, `src/hh_applicant_tool/operations/apply_vacancies.py:746-795`, `src/hh_applicant_tool/operations/apply_vacancies.py:930-959`).

**Sync/blocking API path.**
Current API client is synchronous, uses `time.sleep`, has no explicit timeout and shares one `requests.Session`. Это приемлемо для CLI и для single-client stdio MVP, но не для multi-client server without additional isolation (`src/hh_applicant_tool/api/client.py:71-105`, `src/hh_applicant_tool/main.py:203-224`, `src/hh_applicant_tool/main.py:253-259`).

**Stateful `self` in `apply_vacancies`.**
`Operation.run()` writes many request-specific values into mutable instance fields (`self.dry_run`, `self.work_format`, `self.openai_chat`, `self.ai_filter`, caches, etc.), а `_apply_resume()` ожидает, что они уже заполнены. Это затрудняет reentrant/programmatic reuse and safe testing at service granularity (`src/hh_applicant_tool/operations/apply_vacancies.py:382-521`).

**HTML scraping in core path.**
Current `excluded_filter` path loads `https://hh.ru/vacancy/{id}` and scrapes description from HTML, а test-solving flow scrapes page markers like `vacancyTests`. Это brittle and unsuitable for MCP MVP (`src/hh_applicant_tool/operations/apply_vacancies.py:1539-1581`, `src/hh_applicant_tool/operations/apply_vacancies.py:1218-1278`).

**Read-only commands are not actually read-only.**
`whoami` mutates `settings`; `list-resumes` mutates `resumes`; `call-api` is an unrestricted raw API escape hatch. MCP MVP needs explicit safe tool boundaries, not a generic wrapper over operations (`src/hh_applicant_tool/operations/whoami.py:51-54`, `src/hh_applicant_tool/operations/list_resumes.py:36`, `src/hh_applicant_tool/operations/call_api.py:46-80`).

Итог: нужен отдельный service-layer и отдельный MCP server path, а не subprocess wrapper over existing CLI command.

## 4. Goals

Минимальные цели MVP:

* Поднять MCP server для внешнего AI-agent.
* Дать агенту read-only tools для discovery:

  * кто авторизован;
  * какие резюме доступны;
  * какие вакансии найдены;
  * что внутри конкретной вакансии.
* Дать agent-safe tools для:

  * анализа релевантности vacancy относительно resume и policy;
  * dry-run planning;
  * реального apply только через explicit confirmation.
* Переиспользовать существующие:

  * `ApiClient`;
  * `StorageFacade`;
  * `OpenRouterChatClient`;
  * существующий dedupe algorithm.
* Вынести vacancy logic в programmatic service-layer, независимый от `argparse`.
* Сохранять audit решений и application attempts в SQLite.
* Исключить повторные отклики через `relations`, dedupe и persisted attempts.
* Ограничивать число apply за run и за day.
* Поддержать configurable user policy для personalization.
* Не сломать существующий CLI behavior.

## 5. Non-Goals

В MVP не входят:

* Авторизация в HH через MCP tools. Authorization flow already exists in CLI; MCP auth tool not currently implemented and не нужен для первой итерации.
* CAPTCHA bypass.
* Решение HH tests через MCP. Current HTML scraping path brittle and intentionally out of scope (`src/hh_applicant_tool/operations/apply_vacancies.py:1218-1278`).
* Generic raw `call-api` MCP tool.
* UI или hosted SaaS.
* Автоматическое remote blacklisting вакансий как часть MCP research/apply. Current CLI делает это, но для MCP MVP это лишний hidden side effect (`src/hh_applicant_tool/operations/apply_vacancies.py:689-705`).
* SMTP email notifications as part of MCP apply (`src/hh_applicant_tool/operations/apply_vacancies.py:930-959`).
* Изменение семантики существующего CLI в рамках MVP.
* Streamable HTTP as primary transport in MVP.
* Dockerization as required deliverable in MVP.

## 6. User Stories

* Как пользователь, я хочу дать агенту profile-specific доступ к моим резюме и вакансиям без запуска CLI subcommands вручную.
* Как пользователь, я хочу сначала сделать `dry_run`, увидеть кандидатов на apply и причины решения, а потом отдельно подтвердить real apply.
* Как пользователь, я хочу передать policy с must-have, avoid и dealbreakers, чтобы агент принимал персонализированные решения, а не только keyword-match.
* Как пользователь, я хочу получать объяснение, почему вакансия прошла или не прошла: score, reason, red_flags, missing.
* Как пользователь, я хочу ограничить количество apply за run и за day.
* Как пользователь, я хочу гарантировать, что агент не отправит повторный отклик на ту же или дублирующую вакансию.
* Как агент, я хочу сначала получить список резюме и выбрать `resume_id`.
* Как агент, я хочу искать вакансии either by explicit search query or by similar vacancies for a resume.
* Как агент, я хочу отдельно запросить detailed vacancy and analysis before making any destructive step.
* Как агент, я хочу безопасно откликнуться на одну выбранную вакансию с заданным или generated cover letter.
* Как пользователь, я хочу, чтобы все решения и попытки были сохранены в audit и могли быть воспроизведены post factum.

## 7. Proposed Architecture

### Целевой sketch

```text
External AI Agent / MCP Client
  -> hh-applicant-mcp (stdio)
    -> MCP tool handlers
      -> MCPRequestContext (run_id, limits, dry_run, counters, caches)
      -> VacancyResearchService
        -> HHProfileContext
        -> ApiClient
        -> StorageFacade
        -> OpenRouterChatClient
```

### Ключевое решение по non-CLI construction

Нужна новая core abstraction: `proposed src/hh_applicant_tool/context.py` with `HHProfileContext`.

`HHProfileContext` должен отвечать за:

* resolution of `config_path`, `config.json`, cookies file and DB path using current profile rules (`src/hh_applicant_tool/main.py:227-250`);
* creation of `session` and `openai_session` with current proxy logic (`src/hh_applicant_tool/main.py:151-224`);
* lazy creation of `StorageFacade` and `ApiClient`, включая current user-agent generation/persistence (`src/hh_applicant_tool/main.py:257-282`, `tests/test_api_client.py:71-88`);
* helper methods `get_me()`, `get_resumes()`, `get_blacklisted()`, `get_negotiations()` where relevant (`src/hh_applicant_tool/main.py:284-324`);
* persistence methods `save_token()` and `save_cookies()` (`src/hh_applicant_tool/main.py:328-340`).

`HHApplicantTool.from_profile(...)` should be added, but only as a compatibility shim. Recommendation:

* **Yes, add `HHApplicantTool.from_profile(...)`.**
* **No, do not make MCP/service code depend on `HHApplicantTool.__init__(argv)`.**
* Service-layer should depend on `HHProfileContext` or a minimal protocol with the same properties.
* `HHApplicantTool.from_profile(...)` can internally delegate to `HHProfileContext.from_profile(...)` and exist for programmatic users who already import `HHApplicantTool` from `src/hh_applicant_tool/__init__.py:1`.

### Ключевое решение по separation of business logic

Бизнес-логику нужно вынести из `apply_vacancies.Operation.run()` в `proposed VacancyResearchService`. `Operation.run()` должен стать thin adapter only:

* прочитать CLI args;
* собрать request DTO;
* вызвать service method;
* преобразовать result в CLI output.

Для MCP path `Operation.run()` вообще не используется.

### Proposed modules

```text
proposed src/hh_applicant_tool/context.py
proposed src/hh_applicant_tool/services/__init__.py
proposed src/hh_applicant_tool/services/types.py
proposed src/hh_applicant_tool/services/policy.py
proposed src/hh_applicant_tool/services/cover_letter.py
proposed src/hh_applicant_tool/services/vacancy_research.py

proposed src/hh_applicant_tool/mcp/__init__.py
proposed src/hh_applicant_tool/mcp/server.py
proposed src/hh_applicant_tool/mcp/context.py
proposed src/hh_applicant_tool/mcp/schemas.py
proposed src/hh_applicant_tool/mcp/tools.py
```

Storage additions:

```text
proposed src/hh_applicant_tool/storage/models/mcp_run.py
proposed src/hh_applicant_tool/storage/models/vacancy_analysis.py
proposed src/hh_applicant_tool/storage/models/application_attempt.py
proposed src/hh_applicant_tool/storage/repositories/mcp_runs.py
proposed src/hh_applicant_tool/storage/repositories/vacancy_analysis.py
proposed src/hh_applicant_tool/storage/repositories/application_attempts.py
```

Existing files to extend:

```text
src/hh_applicant_tool/main.py
src/hh_applicant_tool/storage/facade.py
src/hh_applicant_tool/storage/queries/schema.sql
pyproject.toml
README.md
```

### Runtime model

Для MVP server process должен быть **single-profile per process**. Это естественно следует из current profile-scoped config/db layout (`src/hh_applicant_tool/main.py:227-250`) и уменьшает риск accidental cross-profile state leakage.

Каждый MCP request создает `proposed MCPRequestContext`, который хранит:

* `run_id`;
* `tool_name`;
* `dry_run`;
* `confirm_apply`;
* resolved limits;
* request-local counters;
* in-run caches:

  * `seen_employer_ids`;
  * `vacancy_description_cache`;
  * `resume_analysis_cache`.

Это решает current stateful-`self` problem in `apply_vacancies`: service object остается stateless relative to request, а mutable run state переносится в explicit request context.

### Что reuse, а что нет

Reuse:

* `ApiClient` and error types (`src/hh_applicant_tool/api/client.py:71-265`, `src/hh_applicant_tool/api/errors.py:76-148`);
* `StorageFacade`;
* `vacancy_response_dedup` table and current dedupe hash algorithm (`src/hh_applicant_tool/operations/apply_vacancies.py:1165-1175`);
* `OpenRouterChatClient.complete_json()` and `send_message()` (`hh_llm_agent/openrouter.py:236-270`);
* config precedence style from `hh_llm_agent/config.py:147-314`.

Do not reuse directly:

* CLI operations as MCP handlers;
* HTML scraping paths (`_get_vacancy_tests`, `_solve_vacancy_test`, `_is_filtered` HTML branch);
* remote vacancy blacklisting in MCP MVP;
* SMTP email path;
* ad hoc vacancy AI filter parser that fail-opens (`src/hh_applicant_tool/operations/apply_vacancies.py:1035-1096`).

### Search filters vs policy

В новой архитектуре нужно строго разделить:

* **search filters** — objective HH query parameters, mapped into API requests;
* **policy** — subjective user preferences and safety/personalization rules, applied after retrieval and preserved in audit.

Это избавит от смешения current CLI flags, где часть filters уходит в HH API, а часть обрабатывается локально и даже через HTML scraping (`src/hh_applicant_tool/operations/apply_vacancies.py:1408-1583`).

## 8. MCP Transport Decision

Для MVP рекомендуется **stdio**.

По текущему MCP draft стандартными transport являются `stdio` и `Streamable HTTP`, причем clients SHOULD support stdio whenever possible. В `stdio` transport client запускает server как subprocess; server читает JSON-RPC из `stdin`, пишет JSON-RPC в `stdout`, и `stdout` не должен содержать ничего, кроме валидных MCP messages. Логи допускаются в `stderr`. ([Model Context Protocol][1])

`Streamable HTTP` — современный remote transport, который заменил старый `HTTP+SSE` transport из версии 2024-11-05. Он ориентирован на independent process и typically multi-client use, использует HTTP POST и может отдавать либо JSON, либо SSE stream; для него также обязательны origin validation, localhost binding for local mode и proper authentication. Standalone legacy `HTTP with SSE` не должен быть целевым transport в MVP. ([Model Context Protocol][1])

Рекомендация для этого репозитория:

* **MVP:** `stdio`.
* **Later:** `Streamable HTTP`.
* **Do not target in MVP:** legacy standalone SSE.

Причины выбора `stdio` для MVP:

* current codebase local/profile-scoped and CLI-first (`src/hh_applicant_tool/main.py:227-250`);
* one shared SQLite connection with `check_same_thread=False` fits single-client local process better than multi-client remote service (`src/hh_applicant_tool/main.py:253-259`);
* API client is synchronous and blocking (`src/hh_applicant_tool/api/client.py:71-105`);
* direct CLI wrapping impossible because existing commands print to stdout, while stdio transport forbids any non-MCP stdout (`src/hh_applicant_tool/operations/whoami.py:56-60`, `src/hh_applicant_tool/operations/list_resumes.py:51`, `src/hh_applicant_tool/operations/apply_vacancies.py:572`, `src/hh_applicant_tool/operations/chat_agent.py:337-343`). ([Model Context Protocol][1])

## 9. MCP Tools Specification

### Общие правила

Все MCP tools возвращают JSON objects, а не human text. Errors surface as MCP tool errors with normalized shape:

```json
{
  "code": "safety_blocked",
  "message": "Real apply requires confirm_apply=true.",
  "retryable": false,
  "details": {}
}
```

Классификация tools:

* **Read-only (no HH remote mutation):**

  * `hh_whoami`
  * `hh_list_resumes`
  * `hh_search_vacancies`
  * `hh_get_vacancy`
  * `hh_analyze_vacancy`
  * `hh_research_vacancies`
* **Destructive (HH remote mutation possible):**

  * `hh_apply_vacancy`
  * `hh_research_and_apply`

Локальные cache/audit writes не делают tool destructive в терминах HH remote state.

Для destructive tools обязательны:

* `dry_run=true` by default;
* `confirm_apply=false` by default;
* server-side gate `allow_apply=false` by default;
* run/day limits;
* dedupe and hard safety prechecks.

### `hh_whoami`

**Purpose**
Вернуть текущего авторизованного пользователя и profile context.

**Input schema**
Optional:

```json
{
  "refresh": false
}
```

**Output schema**

```json
{
  "run_id": "string",
  "profile_id": "string",
  "authenticated": true,
  "user": {
    "id": "string",
    "first_name": "string",
    "last_name": "string",
    "middle_name": "string|null",
    "email": "string|null",
    "phone": "string|null",
    "counters": {}
  }
}
```

**Side effects**
Recommended MCP behavior: no writes to `settings`. Current CLI `whoami` writes to `settings`; MCP version should not reuse that side effect (`src/hh_applicant_tool/operations/whoami.py:51-54`).

**Safety constraints**
Read-only.

**Errors**
`auth_expired`, `forbidden`, `captcha_required`, `hh_api_error`, `network_error`, `storage_error`.

### `hh_list_resumes`

**Purpose**
Вернуть resumes, доступные текущему профилю.

**Input schema**
Optional:

```json
{
  "only_selected": false,
  "only_published": false
}
```

**Output schema**

```json
{
  "run_id": "string",
  "resumes": [
    {
      "id": "string",
      "title": "string",
      "status": "string",
      "published": true,
      "selected": true,
      "alternate_url": "string|null"
    }
  ]
}
```

**Side effects**
May refresh local `resumes` cache in SQLite; no HH remote mutation.

**Safety constraints**
Read-only.

**Errors**
`auth_expired`, `forbidden`, `captcha_required`, `hh_api_error`, `network_error`, `storage_error`.

### `hh_search_vacancies`

**Purpose**
Вернуть raw vacancy candidates without LLM analysis.

**Input schema**

```json
{
  "source": "auto|search|similar",
  "resume_id": "string|null",
  "search": "string|null",
  "filters": {
    "area": [],
    "employment": [],
    "experience": [],
    "industry": [],
    "professional_role": [],
    "schedule": [],
    "salary": 0,
    "currency": "RUR",
    "period": 7,
    "order_by": "publication_time",
    "work_format": [],
    "employer_id": [],
    "excluded_employer_id": [],
    "label": [],
    "only_with_salary": false,
    "premium": false,
    "no_magic": false,
    "per_page": 20,
    "page_limit": 1
  }
}
```

**Output schema**

```json
{
  "run_id": "string",
  "source": "search|similar",
  "request_params": {},
  "total_found": 0,
  "items": [
    {
      "id": "string",
      "name": "string",
      "alternate_url": "string|null",
      "employer": {
        "id": "string|null",
        "name": "string|null"
      },
      "archived": false,
      "response_url": "string|null",
      "response_letter_required": false,
      "has_test": false,
      "relations": [],
      "work_format": [],
      "snippet": {},
      "published_at": "string|null"
    }
  ]
}
```

**Side effects**
May refresh `vacancies` cache and `vacancy_contacts` cache where available, but no HH remote mutation.

**Safety constraints**
Default response size should be capped server-side to prevent oversized MCP payloads.

**Errors**
`invalid_request` if `source=similar` without `resume_id`, or `source=search` without `search`; `auth_expired`, `forbidden`, `captcha_required`, `hh_api_error`, `network_error`, `storage_error`.

### `hh_get_vacancy`

**Purpose**
Вернуть detailed vacancy snapshot for one vacancy.

**Input schema**

```json
{
  "vacancy_id": "string"
}
```

**Output schema**

```json
{
  "run_id": "string",
  "vacancy": {
    "id": "string",
    "name": "string",
    "description": "string|null",
    "alternate_url": "string|null",
    "archived": false,
    "response_url": "string|null",
    "response_letter_required": false,
    "has_test": false,
    "relations": [],
    "employer": {},
    "salary": {},
    "work_format": [],
    "contacts": {}
  }
}
```

**Side effects**
May refresh local vacancy/contact cache.

**Safety constraints**
Read-only.

**Errors**
`vacancy_not_found`, `auth_expired`, `forbidden`, `captcha_required`, `hh_api_error`, `network_error`, `storage_error`.

### `hh_analyze_vacancy`

**Purpose**
Вернуть structured vacancy suitability analysis against one resume and one resolved policy.

**Input schema**

```json
{
  "resume_id": "string",
  "vacancy_id": "string",
  "analysis_mode": "light|heavy",
  "policy": {},
  "force_refresh": false
}
```

**Output schema**

```json
{
  "run_id": "string",
  "analysis_id": "string",
  "analysis_status": "ok|blocked|degraded",
  "resume_id": "string",
  "vacancy_id": "string",
  "policy_hash": "string",
  "model": "string|null",
  "suitable": true,
  "score": 0.84,
  "reason": "string",
  "red_flags": [],
  "missing": [],
  "recommended_action": "apply|skip|review",
  "precheck_reasons": []
}
```

**Side effects**
Persists one `vacancy_analysis` audit row and one `mcp_runs` row.

**Safety constraints**
Read-only remote.
If LLM is unavailable or returns malformed JSON, tool should return `analysis_status="degraded"` and `recommended_action="review"` instead of silently fail-open.

**Errors**
`invalid_request`, `resume_not_found`, `vacancy_not_found`, `auth_expired`, `forbidden`, `captcha_required`, `hh_api_error`, `storage_error`. LLM failure should normally degrade into a result, not crash the tool.

### `hh_apply_vacancy`

**Purpose**
Safely plan or perform a single vacancy apply.

**Input schema**

```json
{
  "resume_id": "string",
  "vacancy_id": "string",
  "analysis_id": "string|null",
  "analysis_mode": "light|heavy",
  "policy": {},
  "cover_letter": {
    "mode": "auto|none|text|template|llm",
    "text": "string|null",
    "template_text": "string|null"
  },
  "dry_run": true,
  "confirm_apply": false,
  "max_applications_per_run": 5,
  "max_applications_per_day": 20
}
```

**Output schema**

```json
{
  "run_id": "string",
  "analysis_id": "string",
  "attempt_id": "string",
  "dry_run": true,
  "status": "planned|applied|blocked|failed|unknown",
  "reason": "string",
  "cover_letter_source": "provided|template|llm|none",
  "cover_letter_preview": "string|null",
  "policy_hash": "string",
  "dedupe_key": "string|null",
  "safety_blocks": [],
  "vacancy": {
    "id": "string",
    "alternate_url": "string|null"
  }
}
```

**Side effects**

* `dry_run=true`: persists audit only (`mcp_runs`, `vacancy_analysis`, `application_attempts` with `status="planned"`); no remote apply; no `vacancy_response_dedup` write.
* `dry_run=false`: on success performs `POST /negotiations`, persists `application_attempts`, and writes to `vacancy_response_dedup`.

**Safety constraints**

* real apply requires all of:

  * server `allow_apply=true`;
  * request `dry_run=false`;
  * request `confirm_apply=true`.
* blind apply forbidden: if `analysis_id` absent, service must analyze vacancy first.
* block on:

  * archived vacancy;
  * `response_url` / manual external form;
  * `has_test`;
  * existing `relations`;
  * local dedupe hit;
  * existing successful or unknown attempt;
  * run/day limit hit;
  * `recommended_action != "apply"`.

**Errors**
`invalid_request`, `safety_blocked`, `manual_form_required`, `rate_limited`, `auth_expired`, `forbidden`, `captcha_required`, `hh_api_error`, `network_error`, `storage_error`, `llm_unavailable`, `llm_bad_response`.

### `hh_research_vacancies`

**Purpose**
End-to-end search + precheck + analysis without any remote apply.

**Input schema**

```json
{
  "resume_id": "string",
  "source": "auto|search|similar",
  "search": "string|null",
  "filters": {},
  "analysis_mode": "light|heavy",
  "policy": {},
  "max_candidates": 20
}
```

**Output schema**

```json
{
  "run_id": "string",
  "resume_id": "string",
  "source": "search|similar",
  "summary": {
    "fetched": 0,
    "analyzed": 0,
    "apply_recommended": 0,
    "review_recommended": 0,
    "skipped": 0,
    "blocked": 0,
    "errors": 0
  },
  "results": [
    {
      "vacancy": {},
      "analysis_id": "string",
      "analysis_status": "ok|blocked|degraded",
      "suitable": true,
      "score": 0.84,
      "reason": "string",
      "red_flags": [],
      "missing": [],
      "recommended_action": "apply|skip|review",
      "precheck_reasons": []
    }
  ]
}
```

**Side effects**
Persists `mcp_runs` and `vacancy_analysis`; no remote HH mutation.

**Safety constraints**
Read-only remote.
Should dedupe within run before expensive analysis using current dedupe key algorithm.

**Errors**
Same as search/analyze. Partial failures should be reflected in summary instead of aborting the whole run whenever possible.

### `hh_research_and_apply`

**Purpose**
End-to-end search + analysis + safe apply loop.

**Input schema**

```json
{
  "resume_id": "string",
  "source": "auto|search|similar",
  "search": "string|null",
  "filters": {},
  "analysis_mode": "light|heavy",
  "policy": {},
  "cover_letter": {
    "mode": "auto|none|text|template|llm",
    "text": "string|null",
    "template_text": "string|null"
  },
  "dry_run": true,
  "confirm_apply": false,
  "max_candidates": 20,
  "max_applications_per_run": 5,
  "max_applications_per_day": 20
}
```

**Output schema**

```json
{
  "run_id": "string",
  "dry_run": true,
  "summary": {
    "fetched": 0,
    "analyzed": 0,
    "apply_recommended": 0,
    "planned": 0,
    "applied": 0,
    "blocked": 0,
    "failed": 0,
    "unknown": 0
  },
  "results": [
    {
      "vacancy_id": "string",
      "analysis_id": "string",
      "attempt_id": "string",
      "status": "planned|applied|blocked|failed|unknown",
      "reason": "string",
      "score": 0.84,
      "recommended_action": "apply|skip|review"
    }
  ]
}
```

**Side effects**

* dry-run: audit only;
* real apply: remote `/negotiations` calls for selected candidates, plus dedupe/application audit.

**Safety constraints**
Same as `hh_apply_vacancy`, plus:

* stop applying when run/day caps reached;
* never auto-apply `review` candidates;
* no remote vacancy blacklisting;
* no SMTP email;
* no test solving.

**Errors**
Same as `hh_apply_vacancy`, but result should remain partially useful for already analyzed candidates.

## 10. Service Layer Specification

Нужен `proposed VacancyResearchService` в `src/hh_applicant_tool/services/vacancy_research.py`.

Service должен быть:

* programmatic;
* side-effect explicit;
* request-scoped via `MCPRequestContext`;
* independent of `argparse`;
* testable with fake API and fake LLM;
* free of `print()` and CLI exit codes.

Recommended constructor dependencies:

* `HHProfileContext`;
* optional `OpenRouterChatClient` factory / resolver;
* `clock`;
* `uuid generator`;
* optional `sleep` function for controlled retry/jitter;
* logger.

### `search_vacancies(...)`

**Inputs**

* `search: str | None`
* `filters: SearchFilters`
* `per_page: int`
* `page_limit: int`

**Outputs**
`VacancySearchResult` with:

* `source="search"`
* resolved request params
* raw vacancy summaries
* `total_found`

**Dependencies**

* `ApiClient.get("/vacancies")`
* `StorageFacade.vacancies`
* optional `StorageFacade.vacancy_contacts`

**Errors**

* `invalid_request`
* HH auth/API/network/storage errors

**Test strategy**

* fake API client verifies endpoint `/vacancies`
* verifies request params mapping from filters
* verifies vacancy cache save
* verifies oversized page_limit is clamped

### `get_similar_vacancies(...)`

**Inputs**

* `resume_id: str`
* `filters: SearchFilters`
* `per_page: int`
* `page_limit: int`

**Outputs**
`VacancySearchResult` with `source="similar"`.

**Dependencies**

* `ApiClient.get(f"/resumes/{resume_id}/similar_vacancies")`
* `StorageFacade.vacancies`

**Errors**

* `resume_not_found`
* HH auth/API/network/storage errors

**Test strategy**

* fake API verifies endpoint `/resumes/{id}/similar_vacancies`
* verifies empty pages stop iteration
* verifies cache writes

### `get_vacancy_details(...)`

**Inputs**

* `vacancy_id: str`

**Outputs**
`VacancyDetailsResult` with normalized detailed vacancy snapshot.

**Dependencies**

* `ApiClient.get(f"/vacancies/{vacancy_id}")`
* `StorageFacade.vacancies`
* optional `StorageFacade.vacancy_contacts`

**Errors**

* `vacancy_not_found`
* HH auth/API/network/storage errors

**Test strategy**

* fake API returns details
* verifies description is preserved
* verifies cache save
* verifies not-found mapping

### `analyze_vacancy(...)`

**Inputs**

* `resume_id: str`
* `vacancy_id: str`
* `policy: ResolvedPolicy`
* `analysis_mode: "light" | "heavy"`
* `request_context: MCPRequestContext`

**Outputs**
`VacancyAnalysisResult` with:

* `analysis_id`
* `analysis_status`
* `suitable`
* `score`
* `reason`
* `red_flags`
* `missing`
* `recommended_action`
* `precheck_reasons`
* `policy_hash`
* `model`

**Dependencies**

* `get_vacancy_details(...)`
* `HHProfileContext.get_resumes()` / `/resumes/{id}` for heavy mode
* `OpenRouterChatClient.complete_json()`
* `StorageFacade.vacancy_analysis`
* `StorageFacade.mcp_runs`
* dedupe helper
* blacklist helper via `get_blacklisted()`

**Behavior**

1. Run cheap hard prechecks before LLM:

   * archived;
   * `response_url`;
   * `has_test`;
   * `relations`;
   * blacklisted employer if enabled;
   * policy-based excluded employer/keywords;
   * local dedupe hit.
2. If blocked, return `analysis_status="blocked"` without LLM call.
3. Otherwise build resume summary:

   * light -> title only, mirroring current behavior (`src/hh_applicant_tool/operations/apply_vacancies.py:1019-1023`);
   * heavy -> `/resumes/{id}` summary, mirroring current behavior (`src/hh_applicant_tool/operations/apply_vacancies.py:995-1017`).
4. Call LLM with structured schema.
5. Normalize `recommended_action`:

   * `apply` if score >= `min_score` and no blocking flags;
   * `review` if degraded or ambiguous;
   * `skip` otherwise.
6. Persist audit row.

**Errors**

* `resume_not_found`
* `vacancy_not_found`
* HH auth/API/network/storage errors
* LLM errors usually degrade result; only configuration errors should hard-fail

**Test strategy**

* fake LLM returns valid JSON
* LLM unavailable -> degraded result
* blocked vacancy -> no LLM call
* policy threshold -> `recommended_action="skip"`
* audit row persists with `policy_hash` and raw response

### `generate_cover_letter(...)`

**Inputs**

* `resume_id: str`
* `vacancy: VacancyDetails`
* `policy: ResolvedPolicy`
* `cover_letter_request`
* `request_context`

**Outputs**
`CoverLetterResult`:

* `source="provided|template|llm|none"`
* `text`
* `preview`
* `sha256`

**Dependencies**

* optional `OpenRouterChatClient.send_message()`
* config/policy template resolver
* maybe `get_me()` and resume summary for template variables

**Behavior**
Precedence must be explicit:

1. provided text;
2. provided template text;
3. policy/config template;
4. LLM generation if requested or required;
5. `none`.

If vacancy requires cover letter and no viable source exists, return hard block `cover_letter_required_but_unavailable`.

**Errors**

* `llm_unavailable`
* `invalid_request`
* `storage_error` only if preview/hash audit cannot be saved

**Test strategy**

* provided text wins
* template rendering works
* LLM path works
* required letter without source blocks apply

### `apply_vacancy(...)`

**Inputs**

* `resume_id: str`
* `vacancy_id: str`
* `policy: ResolvedPolicy`
* optional `analysis_id`
* optional `cover_letter_request`
* `dry_run: bool`
* `confirm_apply: bool`
* `request_context`

**Outputs**
`ApplicationAttemptResult`:

* `attempt_id`
* `status`
* `reason`
* `analysis_id`
* `cover_letter_*`
* `dedupe_key`
* `safety_blocks`

**Dependencies**

* `analyze_vacancy(...)`
* `generate_cover_letter(...)`
* `ApiClient.post("/negotiations")`
* `StorageFacade.application_attempts`
* `StorageFacade.vacancy_response_dedup`

**Behavior**

1. Resolve/reuse analysis.
2. Enforce safety gates.
3. If dry-run: persist `planned`, return preview.
4. If real apply:

   * execute `POST /negotiations`;
   * on success persist `applied` and dedupe row;
   * on `Redirect` mark `manual_form_required`;
   * on timeout/unknown outcome mark `unknown` and block auto-retry.

Real apply should not use `assert res == {}`. Current CLI asserts empty response (`src/hh_applicant_tool/operations/apply_vacancies.py:901-907`); service should treat successful 2xx response more defensively and persist raw response summary.

**Errors**

* validation/safety errors
* HH auth/API/network/storage errors
* `manual_form_required`
* `rate_limited`
* `llm_unavailable` only if letter generation or analysis cannot be degraded safely

**Test strategy**

* dry-run: no POST, no dedupe write
* real apply: POST + dedupe write
* redirect -> blocked/manual form
* archived/test/external/relations/dedupe -> blocked
* unknown network error -> `status="unknown"`

### `research_vacancies(...)`

**Inputs**

* `resume_id`
* `search/source/filters`
* `policy`
* `analysis_mode`
* `max_candidates`
* `request_context`

**Outputs**
`ResearchResult` with summary counts and analyzed results sorted by:

1. `recommended_action` (`apply` first),
2. score descending,
3. publication date descending.

**Dependencies**

* `search_vacancies(...)` or `get_similar_vacancies(...)`
* `analyze_vacancy(...)`

**Errors**

* fatal HH auth/captcha can abort run
* per-vacancy API/LLM issues should be best-effort and reflected in summary

**Test strategy**

* verifies source auto-resolution
* verifies dedupe-within-run
* verifies summary counts
* verifies sorted output
* verifies max_candidates cap

### `research_and_apply(...)`

**Inputs**

* same as `research_vacancies(...)`
* plus `cover_letter_request`
* plus destructive flags/limits

**Outputs**
`ResearchAndApplyResult`:

* research summary
* apply summary
* list of attempts/results

**Dependencies**

* `research_vacancies(...)`
* `apply_vacancy(...)`

**Behavior**

* analyze first;
* only candidates with `recommended_action="apply"` enter apply stage;
* stop at run/day limit or fatal auth/captcha/rate-limit;
* do not auto-apply `review` or degraded candidates.

**Errors**
Same as research + apply. Partial results should remain inspectable.

**Test strategy**

* dry-run research-and-plan
* confirm required for real apply
* per-run/day limits enforced
* repeated runs do not reapply duplicates
* degraded analysis does not auto-apply

## 11. LLM Personalization Design

### Policy model

Нужна explicit policy model, separate from HH search filters.

Recommended MVP shape:

```json
{
  "must_have": [],
  "nice_to_have": [],
  "avoid": [],
  "dealbreakers": [],
  "excluded_employers": [],
  "excluded_keywords": [],
  "min_score": 0.7,
  "cover_letter_style": "short",
  "cover_letter_language": "ru",
  "force_message": false,
  "notes": ""
}
```

Recommended semantics:

* `must_have`: желательно явно учитывать в reasoning; отсутствие может снижать score, но не всегда hard-block.
* `nice_to_have`: boosts score.
* `avoid`: soft negatives.
* `dealbreakers`: hard negatives for model reasoning; если rule-based can detect them structurally, они должны блокировать до LLM.
* `excluded_employers`, `excluded_keywords`: deterministic prechecks.
* `min_score`: threshold between `review` and `apply`.
* `cover_letter_style`, `cover_letter_language`, `force_message`: cover-letter generation policy.
* `notes`: свободный user context.

Hard safety constraints MVP не должны отключаться policy:

* archived vacancy -> skip;
* `response_url` / manual form -> skip;
* `has_test` -> skip;
* repeated/deduped vacancy -> skip.

### Policy resolution

Recommended precedence for policy:

1. per-tool `policy` payload;
2. server startup `policy_file` or `vacancy_policy` section;
3. built-in defaults.

Merge rule:

* if field present in request policy, it replaces corresponding default-policy field;
* if field absent, default value inherited;
* resolved policy is canonicalized and hashed into `policy_hash`.

Это важно для audit и для того, чтобы old skip-decision не считался universally valid after policy changes.

### Structured vacancy analysis output

Current boolean-only vacancy filter must be replaced.

Recommended structured output:

```json
{
  "analysis_status": "ok",
  "suitable": true,
  "score": 0.84,
  "reason": "Role and responsibilities match backend profile; stack is adjacent and remote format fits.",
  "red_flags": [],
  "missing": [],
  "recommended_action": "apply"
}
```

Field semantics:

* `analysis_status`: `ok | blocked | degraded`
* `suitable`: normalized boolean summary
* `score`: `0.0..1.0`
* `reason`: one concise primary explanation
* `red_flags`: machine-readable negatives
* `missing`: missing information needed for stronger confidence
* `recommended_action`: `apply | skip | review`

### Prompting and model integration

Vacancy analysis should use `OpenRouterChatClient.complete_json()` with a strict `StructuredOutputSchema`, not the current free-text `send_message()` + regex parse path (`hh_llm_agent/openrouter.py:95-130`, `hh_llm_agent/openrouter.py:251-270`, `src/hh_applicant_tool/operations/apply_vacancies.py:1053-1096`).

Input to LLM should include:

* resume summary:

  * light or heavy mode;
* vacancy summary/details;
* resolved policy;
* explicit instruction to return only the schema.

Recommended reuse:

* `OpenRouterChatClient.complete_json()` for vacancy analysis;
* `OpenRouterChatClient.send_message()` for cover-letter generation.

Current `HHApplicantTool.get_openai_chat()` is a legacy compatibility helper for old flows and uses old config chain (`src/hh_applicant_tool/main.py:346-383`). MCP service should prefer direct `OpenRouterConfig` resolution modeled after `hh_llm_agent/config.py:180-314`.

### Fallback behavior

**If LLM is unavailable**

* `hh_analyze_vacancy`: return `analysis_status="degraded"`, `suitable=false`, `score=0.0`, `recommended_action="review"`, with explicit reason like “LLM unavailable; only prechecks executed”.
* `hh_apply_vacancy` / `hh_research_and_apply`: do not auto-apply degraded results.

**If JSON is malformed**
`OpenRouterChatClient.complete_json()` already raises `OpenRouterError` on invalid JSON or non-object JSON (`hh_llm_agent/openrouter.py:257-270`). Service should convert that into the same degraded behavior as above, not fail-open.

**If score is below threshold**
Return `recommended_action="skip"` if clearly below threshold, or `review` if data is insufficient and the result is close to threshold.

**If hard precheck blocks vacancy**
Do not call LLM at all. Return `analysis_status="blocked"`, `suitable=false`, `score=0.0`, explicit `precheck_reasons`, and `recommended_action="skip"`.

## 12. Data Model / Audit

### Current schema analysis

Current schema already has:

* `vacancy_response_dedup` as the right authoritative store for “already applied to this semantic vacancy” (`src/hh_applicant_tool/storage/queries/schema.sql:60-71`);
* `skipped_vacancies` as a coarse skip-memory keyed only by `(resume_id, vacancy_id)` (`src/hh_applicant_tool/storage/queries/schema.sql:73-85`);
* `agent_runs`, `agent_decisions`, `agent_outbox`, `agent_webhooks` for chat-agent audit (`src/hh_applicant_tool/storage/queries/schema.sql:109-178`).

`skipped_vacancies` is **not sufficient** as an MCP vacancy-analysis cache:

* unique key is only `(resume_id, vacancy_id)`;
* no `policy_hash`;
* no `score`;
* no `reason`;
* no structured red flags/missing;
* repository API is only `is_skipped()` / `remember()` with coarse semantics (`src/hh_applicant_tool/storage/repositories/skipped_vacancies.py:10-77`).

Recommendation:

* keep `vacancy_response_dedup` as-is and reuse it;
* keep `skipped_vacancies` for legacy CLI compatibility only;
* add new MCP-specific audit tables.

### Proposed new tables

#### `proposed mcp_runs`

Purpose: one row per MCP tool invocation.

Recommended fields:

* `id TEXT PRIMARY KEY`
* `tool_name TEXT NOT NULL`
* `status TEXT NOT NULL` (`running|completed|failed`)
* `transport TEXT NOT NULL` (`stdio` in MVP)
* `profile_id TEXT NOT NULL`
* `resume_id TEXT NULL`
* `dry_run BOOLEAN NOT NULL`
* `confirm_apply BOOLEAN NOT NULL DEFAULT 0`
* `policy_hash TEXT NOT NULL`
* `policy_json TEXT NOT NULL DEFAULT '{}'`
* `search_params_json TEXT NOT NULL DEFAULT '{}'`
* `model TEXT NULL`
* `total_candidates INTEGER NOT NULL DEFAULT 0`
* `analyzed_count INTEGER NOT NULL DEFAULT 0`
* `planned_apply_count INTEGER NOT NULL DEFAULT 0`
* `applied_count INTEGER NOT NULL DEFAULT 0`
* `skipped_count INTEGER NOT NULL DEFAULT 0`
* `blocked_count INTEGER NOT NULL DEFAULT 0`
* `error_count INTEGER NOT NULL DEFAULT 0`
* `created_at DATETIME DEFAULT CURRENT_TIMESTAMP`
* `finished_at DATETIME NULL`

Recommended indexes:

* `created_at`
* `(status, created_at)`
* `(tool_name, created_at)`

#### `proposed vacancy_analysis`

Purpose: one row per analyzed vacancy.

Recommended fields:

* `id TEXT PRIMARY KEY`
* `run_id TEXT NULL`
* `resume_id TEXT NOT NULL`
* `vacancy_id INTEGER NOT NULL`
* `employer_id INTEGER NULL`
* `dedupe_key TEXT NULL`
* `source TEXT NOT NULL` (`search|similar|manual`)
* `source_query TEXT NULL`
* `analysis_status TEXT NOT NULL` (`ok|blocked|degraded`)
* `analysis_mode TEXT NOT NULL` (`light|heavy`)
* `policy_hash TEXT NOT NULL`
* `policy_json TEXT NOT NULL DEFAULT '{}'`
* `suitable BOOLEAN NOT NULL`
* `score REAL NOT NULL`
* `reason TEXT NOT NULL`
* `red_flags_json TEXT NOT NULL DEFAULT '[]'`
* `missing_json TEXT NOT NULL DEFAULT '[]'`
* `recommended_action TEXT NOT NULL` (`apply|skip|review`)
* `precheck_reasons_json TEXT NOT NULL DEFAULT '[]'`
* `model TEXT NULL`
* `raw_response TEXT NULL`
* `reasoning_details TEXT NOT NULL DEFAULT '[]'`
* `prompt_hash TEXT NULL`
* `vacancy_snapshot_json TEXT NOT NULL DEFAULT '{}'`
* `created_at DATETIME DEFAULT CURRENT_TIMESTAMP`

Recommended indexes:

* `(resume_id, vacancy_id, created_at DESC)`
* `(run_id)`
* `(policy_hash)`
* `(dedupe_key)`

Notes:

* `raw_response` and `reasoning_details` intentionally mirror `agent_decisions` precedent (`src/hh_applicant_tool/storage/models/agent_decision.py:21-29`).
* JSON fields should use the existing `mapped(store_json=True)` pattern (`src/hh_applicant_tool/storage/models/base.py:15-59`, `src/hh_applicant_tool/storage/models/base.py:121-131`).

#### `proposed application_attempts`

Purpose: one row per dry-run plan or real apply attempt.

Recommended fields:

* `id TEXT PRIMARY KEY`
* `run_id TEXT NULL`
* `analysis_id TEXT NULL`
* `resume_id TEXT NOT NULL`
* `vacancy_id INTEGER NOT NULL`
* `employer_id INTEGER NULL`
* `dedupe_key TEXT NULL`
* `day_bucket TEXT NOT NULL`
* `dry_run BOOLEAN NOT NULL`
* `confirm_apply BOOLEAN NOT NULL DEFAULT 0`
* `status TEXT NOT NULL` (`planned|applied|blocked|failed|unknown`)
* `reason TEXT NOT NULL`
* `cover_letter_source TEXT NOT NULL` (`provided|template|llm|none`)
* `cover_letter_preview TEXT NULL`
* `cover_letter_sha256 TEXT NULL`
* `hh_request_id TEXT NULL`
* `error_code TEXT NULL`
* `error_message TEXT NULL`
* `created_at DATETIME DEFAULT CURRENT_TIMESTAMP`
* `sent_at DATETIME NULL`

Recommended indexes:

* `(run_id)`
* `(resume_id, vacancy_id, created_at DESC)`
* `(resume_id, dedupe_key, created_at DESC)`
* `(day_bucket, status, dry_run)`
* `(status, created_at)`

`status="unknown"` нужен для safe handling of ambiguous network failures on real apply: system must not blindly retry and risk duplicate application.

### Reuse of existing tables

**Reuse unchanged**

* `vacancy_response_dedup` for authoritative apply dedupe.
* existing `vacancies`, `employers`, `vacancy_contacts`, `resumes` caches.

**Do not reuse as source of truth**

* `skipped_vacancies` for policy-aware analysis dedupe.

Reason: if vacancy was skipped once under one policy, that should not globally block future analysis under a different policy. Current schema cannot represent that because key is only `(resume_id, vacancy_id)` (`src/hh_applicant_tool/storage/queries/schema.sql:73-85`, `src/hh_applicant_tool/storage/repositories/skipped_vacancies.py:15-21`).

### Migration strategy

Current `init_db()` always executes `schema.sql` and only has additive-column helpers for `agent_decisions` and `skipped_vacancies` (`src/hh_applicant_tool/storage/utils.py:19-61`). Therefore:

* New additive tables can be introduced directly in `schema.sql`; existing DBs will create them on next `StorageFacade` init.
* If any existing tables must be altered, add either:

  * a new explicit migration under `src/hh_applicant_tool/storage/queries/migrations/`, or
  * a new `_ensure_*` helper in `storage/utils.py`.
* To minimize migration risk, vacancy MCP audit should prefer **new tables over altering old ones**.

## 13. Safety Model

### Default safety posture

MVP must be **fail-closed for destructive actions**.

Required defaults:

* `dry_run=true` for destructive tools;
* `confirm_apply=false` by default;
* `allow_apply=false` as server startup default;
* no blind apply without analysis.

Real apply requires:

1. server `allow_apply=true`;
2. request `dry_run=false`;
3. request `confirm_apply=true`.

### Hard blocks

The following should be hard blocks in MVP:

* vacancy already archived (`src/hh_applicant_tool/operations/apply_vacancies.py:665-670`);
* vacancy has external `response_url` / manual form (`src/hh_applicant_tool/operations/apply_vacancies.py:672-678`);
* vacancy has `has_test=true` (`src/hh_applicant_tool/operations/apply_vacancies.py:838-857`);
* vacancy already has `relations` indicating prior response (`src/hh_applicant_tool/operations/apply_vacancies.py:631-654`);
* vacancy already matches local dedupe key (`src/hh_applicant_tool/operations/apply_vacancies.py:656-663`, `src/hh_applicant_tool/operations/apply_vacancies.py:1165-1175`);
* run/day limits exceeded.

### Dedupe / no repeated responses

No repeated apply must be enforced in this order:

1. HH `relations` from vacancy search result.
2. `vacancy_response_dedup` using the current dedupe hash algorithm (`src/hh_applicant_tool/operations/apply_vacancies.py:1107-1175`).
3. latest `application_attempts` row with `status in ('applied', 'unknown')`.
4. dry-run `planned` attempts do **not** block future real apply.

This preserves current CLI dedupe behavior while adding audit for ambiguous outcomes.

### Limits

Recommended conservative defaults:

* `max_applications_per_run = 5`
* `max_applications_per_day = 20`

Rationale:

* current README mentions HH daily cap `200`, but safe defaults for an AI-driven agent should be much lower (`README.md:694`).

Counting rules:

* only successful real applies (`status='applied'` and `dry_run=false`) count toward daily cap;
* `unknown` does not increase applied_count, but blocks automatic retry for the same vacancy until reviewed;
* dry-run attempts do not count toward daily cap.

### Blacklists and exclusions

MVP should support:

* `skip_blacklisted_employers=true` by default, using existing `get_blacklisted()` helper and chat-agent precedent (`src/hh_applicant_tool/main.py:294-303`, `hh_llm_agent/gateway.py:14-15`, `hh_llm_agent/config.py:100`, `hh_llm_agent/service.py:225-228`);
* policy-based `excluded_employers`;
* policy-based `excluded_keywords`;
* explicit `work_format` post-filter.

MCP MVP should **not** auto-blacklist vacancies remotely. Current CLI does `PUT /vacancies/blacklisted/{id}` for filtered vacancies (`src/hh_applicant_tool/operations/apply_vacancies.py:689-705`), but that is an extra destructive side effect beyond the MCP goal.

### Audit

Every analyze/apply decision must be persisted, including:

* blocked by safety;
* skipped by policy;
* degraded due to LLM failure;
* dry-run planned apply;
* real apply success/failure/unknown.

This deliberately differs from current chat-agent dry-run audit behavior (`hh_llm_agent/service.py:1460-1461`).

### Logging safety

No secrets in logs:

* no API keys;
* no full prompts;
* no full generated cover letters;
* no webhook secrets.

Current logging is insufficient for MCP safety:

* current redactor only masks long token-like patterns and long hex strings (`src/hh_applicant_tool/utils/log.py:111-118`);
* current apply flow logs prompt text and generated letter (`src/hh_applicant_tool/operations/apply_vacancies.py:811-829`).

Recommendation:

* log hashes, IDs, counts and statuses only;
* store raw LLM response in SQLite audit, not in logs;
* store cover letter as preview + SHA256, not as full log message.

## 14. Error Handling

### Error envelope

All MCP tool failures should normalize to:

```json
{
  "code": "string",
  "message": "string",
  "retryable": false,
  "details": {}
}
```

### Error classes and mapping

**Expired token / missing auth**

* Map to `auth_expired`.
* If current `ApiClient` successfully refreshes token, tool continues.
* If refresh unavailable or forbidden persists, stop destructive flow and return structured auth error.
* Important current limitation: refresh only happens when local expiry says token expired (`src/hh_applicant_tool/api/client.py:241-265`, `tests/test_api_client.py:91-158`).

**Forbidden**

* Map generic 403 to `forbidden`.
* Fatal for current request.

**Captcha required**

* Map `CaptchaRequired` to `captcha_required` and include `captcha_url` from exception (`src/hh_applicant_tool/api/errors.py:121-134`).
* Fatal for current request.
* No CAPTCHA solving in MVP.

**Rate limit**

* HH `LimitExceeded` -> `rate_limited`.
* OpenRouter 429 -> `llm_rate_limited` or normalized `rate_limited` with `provider='openrouter'`.
* `research_and_apply` should stop additional real applies after HH rate limit and return partial results.

**HH API errors**

* `ResourceNotFound` -> `resume_not_found` / `vacancy_not_found` depending on context.
* `BadGateway` / 5xx -> `hh_api_error`, retryable for GET/search/detail operations, not auto-retry for POST apply.
* `Redirect` on apply -> `manual_form_required`.

**OpenRouter errors**

* `OpenRouterError` during analysis -> degrade read-only analysis to `review`.
* `OpenRouterError` during required cover-letter generation -> block real apply with `llm_unavailable` unless alternate letter source exists.

**Malformed LLM response**

* Same handling as OpenRouter structured failure:

  * read-only analysis => degraded;
  * destructive apply => blocked.

**SQLite errors**

* Map to `storage_error`.
* Fatal for current tool call.

**Network timeout**

* Map to `network_error`.
* Retry policy:

  * safe GET/read-only calls: bounded retry allowed;
  * `POST /negotiations`: no blind retry. Persist `status="unknown"` if request outcome is ambiguous.

### Retry policy

Recommended:

* Retry at most 2 times for:

  * read-only GETs;
  * OpenRouter 429;
  * transient 502/5xx.
* No automatic retry for real apply POST if request may have reached HH.

### Timeout requirement

Current HH API client does not set explicit timeouts (`src/hh_applicant_tool/api/client.py:99-105`). MCP/service path should add configurable timeouts. This is not currently implemented.

## 15. Configuration

### Current config sources

Current repo already uses:

* CLI args and profile-scoped config dir (`src/hh_applicant_tool/main.py:89-100`, `src/hh_applicant_tool/main.py:227-234`);
* top-level `api_key` and `openai_base_url` (`README.md:786-793`, `README.md:813-820`);
* legacy `openai.*` section for old AI flows (`README.md:795-807`, `src/hh_applicant_tool/main.py:355-377`);
* `openrouter.*` and `chat_agent.*` with args/env/config/default precedence (`README.md:822-859`, `hh_llm_agent/config.py:147-314`).

### Proposed MCP startup config

Recommended new profile config section:

```json
{
  "mcp": {
    "transport": "stdio",
    "allow_apply": false,
    "max_applications_per_run": 5,
    "max_applications_per_day": 20,
    "policy_path": "vacancy_policy.json",
    "request_timeout_seconds": 20
  }
}
```

Recommended separate policy file or inline section:

```json
{
  "vacancy_policy": {
    "must_have": [],
    "nice_to_have": [],
    "avoid": [],
    "dealbreakers": [],
    "excluded_employers": [],
    "excluded_keywords": [],
    "min_score": 0.7,
    "cover_letter_style": "short"
  }
}
```

### MCP server startup parameters

Recommended startup params for `proposed hh-applicant-mcp`:

* `--config-dir`
* `--profile-id`
* `--transport` (default `stdio`)
* `--allow-apply`
* `--policy-file`
* `--max-applications-per-run`
* `--max-applications-per-day`
* `--request-timeout-seconds`
* `--log-level`

### LLM config

Recommendation:

* primary LLM config source for MCP: `openrouter.*`;
* fallback to top-level `api_key` + `openai_base_url`, consistent with current repo (`README.md:813-833`, `hh_llm_agent/config.py:188-234`);
* support legacy `openai.*` only as migration alias for backward compatibility, not as primary MCP contract.

### Per-tool params

Allowed per-tool operational params:

* search filters;
* policy override;
* analysis mode;
* dry-run / confirm flags;
* cover letter request;
* run/day limit overrides within server-side caps.

Per-tool secrets should **not** be accepted.

### Precedence

Recommended precedence:

1. per-tool params for request-scoped non-secret behavior;
2. MCP server startup params;
3. environment variables;
4. profile `config.json`;
5. built-in defaults.

For secrets:

1. startup params;
2. environment variables;
3. profile `config.json`;
4. built-in defaults.

For policy:

1. per-tool inline policy;
2. server `policy_file`;
3. profile `vacancy_policy` section;
4. built-in defaults.

## 16. Backward Compatibility

Backward compatibility must be preserved explicitly.

### What must not change in MVP

* Existing console script `hh-applicant-tool` remains unchanged (`pyproject.toml:47-48`).
* Existing `apply-vacancies` behavior must not change in MVP.
* Existing tests for CLI behaviors must remain green.

This is critical because current CLI has semantics that MCP should **not** silently inherit:

* hidden default `work_format=["REMOTE"]` (`src/hh_applicant_tool/operations/apply_vacancies.py:428-445`);
* remote vacancy blacklisting (`src/hh_applicant_tool/operations/apply_vacancies.py:689-705`);
* SMTP email path (`src/hh_applicant_tool/operations/apply_vacancies.py:930-959`);
* HTML test-solving path (`src/hh_applicant_tool/operations/apply_vacancies.py:838-893`, `src/hh_applicant_tool/operations/apply_vacancies.py:1218-1278`).

### Recommended compatibility strategy

Phase-in strategy:

1. add new context/service/MCP modules additively;
2. do not replace CLI operations in the same iteration;
3. once service-layer stabilizes, optionally migrate selected CLI commands to call it internally;
4. for `apply-vacancies`, migrate only after parity tests prove semantics are preserved.

`chat-agent` is the in-repo precedent for this migration direction: thin CLI adapter over reusable service (`src/hh_applicant_tool/operations/chat_agent.py:322-344`, `hh_llm_agent/service.py:118-223`).

### `HHApplicantTool.from_profile(...)`

Adding `HHApplicantTool.from_profile(...)` is backward-compatible because:

* it is additive;
* it does not alter existing `__init__(argv)` semantics;
* it gives programmatic users a proper path without fake CLI argv.

## 17. Testing Strategy

### Unit tests for service-layer

Primary target: `VacancyResearchService`.

Use the same style already present in repo:

* fake API client and in-memory storage from `tests/test_apply_vacancies.py:126-209`;
* fake LLM and monkeypatch patterns from `tests/test_chat_agent_service.py:148-178`, `tests/test_chat_agent_service.py:259-326`;
* structured-output OpenRouter tests as precedent from `tests/test_openrouter_client.py:74-216`.

Recommended proposed tests:

* `proposed tests/test_vacancy_research_service.py`
* `proposed tests/test_mcp_tools.py`
* `proposed tests/test_mcp_stdio.py`
* `proposed tests/test_vacancy_audit_storage.py`

### Fake HH API

Use fake API clients, not real HH API:

* current repo already tests `ApiClient` with fake session (`tests/test_api_client.py:14-158`);
* current apply tests already instantiate `StorageFacade(sqlite3.connect(":memory:"))`, a fake tool namespace and call `_apply_resume()` directly (`tests/test_apply_vacancies.py:126-209`).

MCP/service tests should follow that pattern:

* no network;
* no real tokens;
* no real HH side effects.

### Fake LLM

Use fake `OpenRouterChatClient`:

* same pattern as `FakeLLMClient.complete_json()` in chat-agent tests (`tests/test_chat_agent_service.py:148-178`).

Test cases:

* valid structured JSON;
* invalid structured JSON;
* LLM unavailable;
* degraded analysis;
* cover-letter generation fallback.

### SQLite

Use in-memory SQLite via `StorageFacade(sqlite3.connect(":memory:"))`, as already done in apply tests (`tests/test_apply_vacancies.py:127`).

Test:

* schema init;
* audit writes;
* dedupe persistence;
* day-bucket counting;
* dry-run not writing `vacancy_response_dedup`.

### MCP schema tests

Need contract tests for each tool:

* required fields;
* forbidden combinations;
* default values (`dry_run=true`);
* error mapping.

### Dry-run tests

Required:

* `hh_apply_vacancy` and `hh_research_and_apply` with default input do not call `/negotiations`;
* audit persists;
* `vacancy_response_dedup` does not persist;
* future real apply is still allowed.

This mirrors current dry-run dedupe expectation from `tests/test_apply_vacancies.py:194-209` but extends it with audit.

### Dedupe tests

Required:

* relations-based dedupe;
* local dedupe by semantic hash;
* existing real apply blocks repeated apply;
* dry-run planned attempts do not block later real apply;
* unknown attempts block automatic retry.

Current repo already has relations + dedupe unit coverage to reuse conceptually (`tests/test_apply_vacancies.py:153-192`).

### Safety limit tests

Required:

* per-run cap;
* per-day cap;
* `confirm_apply` required;
* `allow_apply=false` blocks real apply;
* archived/response_url/has_test blocked;
* blacklisted employer blocked when enabled.

### Regression tests for existing CLI

Must keep existing test suites green:

* `tests/test_apply_vacancies.py`
* `tests/test_chat_agent_service.py`
* `tests/test_openrouter_client.py`
* `tests/test_api_client.py`

This is the acceptance gate for backward compatibility.

### stdio transport tests

Need subprocess-level tests to verify:

* server starts over stdio;
* stdout contains only MCP messages;
* logs go to stderr;
* tool handlers never call CLI operations that print.

Это критично because current operations print to stdout and would break stdio transport.

## 18. Implementation Plan

### Phase 0 — Recon and design

**Scope**

* analyze current repo;
* define target architecture;
* agree on MVP boundaries.

**Files touched**

* `PRD.md` only.

**Acceptance criteria**

* approved design for context/service/MCP/audit/safety.

**Risks**

* underestimating hidden CLI semantics in `apply-vacancies`.

### Phase 1 — Programmatic context factory

**Scope**

* introduce `proposed HHProfileContext`;
* add `HHApplicantTool.from_profile(...)`;
* centralize profile/session/storage/api creation outside `argparse`.

**Files touched**

* `proposed src/hh_applicant_tool/context.py`
* `src/hh_applicant_tool/main.py`
* `src/hh_applicant_tool/__init__.py`
* `proposed tests/test_context.py`

**Acceptance criteria**

* profile context can be created without argv;
* current user-agent generation/persistence preserved;
* `save_token()` / `save_cookies()` callable outside CLI run loop.

**Risks**

* duplicating profile/config resolution logic;
* accidentally breaking current CLI constructor behavior.

### Phase 2 — VacancyResearchService core

**Scope**

* implement programmatic search/similar/detail/dedupe/precheck logic;
* extract safe logic from `apply_vacancies`.

**Files touched**

* `proposed src/hh_applicant_tool/services/types.py`
* `proposed src/hh_applicant_tool/services/policy.py`
* `proposed src/hh_applicant_tool/services/vacancy_research.py`
* `proposed tests/test_vacancy_research_service.py`

**Acceptance criteria**

* service methods callable without CLI;
* endpoint selection `/vacancies` vs `/similar_vacancies` preserved;
* dedupe hash matches current algorithm;
* no `print()` in service path.

**Risks**

* semantic drift from current CLI hidden defaults;
* accidentally carrying over HTML scraping into new service.

### Phase 3 — Structured LLM vacancy analysis and cover letters

**Scope**

* implement structured vacancy analysis schema;
* integrate policy and cover-letter generation;
* replace fail-open boolean filter with structured output.

**Files touched**

* `proposed src/hh_applicant_tool/services/vacancy_research.py`
* `proposed src/hh_applicant_tool/services/cover_letter.py`
* optionally `hh_llm_agent/openrouter.py` only if helper changes are required
* `proposed tests/test_vacancy_analysis_llm.py`

**Acceptance criteria**

* structured analysis output exists;
* degraded fallback works;
* cover-letter source precedence works;
* destructive flows do not auto-apply on degraded analysis.

**Risks**

* prompt brittleness;
* model/provider variability.

### Phase 4 — Audit persistence

**Scope**

* add `mcp_runs`, `vacancy_analysis`, `application_attempts`;
* wire repositories and storage facade.

**Files touched**

* `src/hh_applicant_tool/storage/queries/schema.sql`
* `src/hh_applicant_tool/storage/facade.py`
* `proposed src/hh_applicant_tool/storage/models/mcp_run.py`
* `proposed src/hh_applicant_tool/storage/models/vacancy_analysis.py`
* `proposed src/hh_applicant_tool/storage/models/application_attempt.py`
* `proposed src/hh_applicant_tool/storage/repositories/mcp_runs.py`
* `proposed src/hh_applicant_tool/storage/repositories/vacancy_analysis.py`
* `proposed src/hh_applicant_tool/storage/repositories/application_attempts.py`
* optional migration file under `src/hh_applicant_tool/storage/queries/migrations/`
* `proposed tests/test_vacancy_audit_storage.py`

**Acceptance criteria**

* every analysis/apply path persists audit;
* dry-run persists audit but not dedupe;
* real apply persists dedupe and attempt;
* day-bucket queries support daily cap.

**Risks**

* schema migration issues on existing DBs;
* accidental coupling with legacy `skipped_vacancies`.

### Phase 5 — MCP server skeleton

**Scope**

* add MCP entrypoint and stdio server bootstrap;
* define tool registration and request lifecycle wrapper;
* ensure token/cookies are saved after tool invocation.

**Files touched**

* `proposed src/hh_applicant_tool/mcp/server.py`
* `proposed src/hh_applicant_tool/mcp/context.py`
* `proposed src/hh_applicant_tool/mcp/schemas.py`
* `proposed src/hh_applicant_tool/mcp/tools.py`
* `pyproject.toml`
* `proposed tests/test_mcp_stdio.py`

**Acceptance criteria**

* server starts over stdio;
* one read-only tool works end-to-end;
* stdout contains only MCP frames;
* token/cookies saved in request lifecycle wrapper.

**Risks**

* stdout contamination;
* MCP library choice;
* request lifecycle not flushing auth state.

### Phase 6 — MCP tools MVP

**Scope**

* implement all MVP tools on top of `VacancyResearchService`.

**Files touched**

* `proposed src/hh_applicant_tool/mcp/tools.py`
* `proposed src/hh_applicant_tool/mcp/schemas.py`
* `proposed tests/test_mcp_tools.py`

**Acceptance criteria**

* all eight MVP tools registered and callable;
* destructive tools default to dry-run;
* real apply requires explicit confirmation and server allow flag.

**Risks**

* schema drift between tools and service;
* hidden side effects sneaking in from copied CLI code.

### Phase 7 — Tests and regression hardening

**Scope**

* expand unit/integration tests;
* keep CLI tests green.

**Files touched**

* new service/MCP tests;
* possibly small fixes in service/storage code.

**Acceptance criteria**

* new tests pass;
* existing CLI regression suites pass unchanged.

**Risks**

* incomplete fake coverage for HH edge cases;
* multi-layer flakiness around time/UUIDs if not injected.

### Phase 8 — Docs and examples

**Scope**

* document MCP startup, tool contracts, config and policy files.

**Files touched**

* `README.md`
* possibly `examples/` if later added

**Acceptance criteria**

* README contains MCP section;
* sample config and policy documented;
* no ambiguity around dry-run/confirm behavior.

**Risks**

* doc drift vs implemented schemas.

### Phase 9 — Optional Streamable HTTP / Docker

**Scope**

* optional second transport and packaging.

**Files touched**

* `proposed src/hh_applicant_tool/mcp/http.py` or extension in `server.py`
* `pyproject.toml`
* optional `Dockerfile`

**Acceptance criteria**

* optional HTTP transport available behind explicit flag;
* security controls documented.

**Risks**

* multi-client concurrency over shared SQLite connection;
* origin/auth requirements;
* transport complexity beyond MVP.

## 19. Open Questions

* Какой MCP Python SDK/library выбрать для implementation?
* Нужен ли strict one-profile-per-process only, или позже нужен multi-profile server?
* Должен ли `allow_apply` быть обязательным server startup gate для real apply? Recommendation: yes.
* Какие exact default limits выбрать для `max_applications_per_run` и `max_applications_per_day`?
* Какой model использовать по умолчанию для vacancy analysis?
* Нужен ли отдельный model/config path для cover-letter generation или достаточно reuse analysis model?
* Где хранить default policy: inline `config.json`, external file, или оба варианта?
* Хранить ли полный cover letter в SQLite audit, или только preview + hash? Recommendation: preview + hash.
* Должен ли `skip_blacklisted_employers` быть non-disableable safety default, или configurable preference?
* Что делать с `status="unknown"` attempt: блокировать auto-retry бессрочно или только until next confirmed relations refresh?
* Нужен ли в будущем отдельный MCP tool для authorization/token refresh?
* Нужен ли Dockerfile в том же milestone, или после stabilization of stdio MVP?
* Нужен ли future tool for batch multi-resume research/apply, или explicit `resume_id` mandatory enough for MVP? Recommendation: explicit single `resume_id` only in MVP.
* Должен ли MCP reuse legacy `openai.*` config path, или only `openrouter.*` + top-level `api_key`?

## 20. Acceptance Criteria

* `proposed hh-applicant-mcp` server стартует по `stdio`.
* Server работает в одном profile context без `argparse` dependency in request path.
* `HHApplicantTool.from_profile(...)` exists.
* Agent может вызвать `hh_whoami`.
* Agent может вызвать `hh_list_resumes`.
* Agent может вызвать `hh_search_vacancies`.
* Agent может вызвать `hh_get_vacancy`.
* Agent может вызвать `hh_analyze_vacancy` и получить structured fields:

  * `suitable`
  * `score`
  * `reason`
  * `red_flags`
  * `missing`
  * `recommended_action`
* `hh_apply_vacancy` по умолчанию выполняется в `dry_run=true`.
* `hh_apply_vacancy` при `dry_run=true` не вызывает `POST /negotiations`.
* Real apply requires:

  * server `allow_apply=true`;
  * request `dry_run=false`;
  * request `confirm_apply=true`.
* Dry-run analysis/apply decisions сохраняются в audit.
* Dry-run не записывает `vacancy_response_dedup`.
* Repeated apply blocked by:

  * HH `relations`;
  * `vacancy_response_dedup`;
  * prior `application_attempts` with `applied` or `unknown`.
* Archived vacancies are skipped.
* Vacancies with external `response_url` are skipped.
* Vacancies with tests are skipped by default.
* Per-run and per-day limits are enforced.
* LLM failure does not silently pass a vacancy into auto-apply; destructive flows degrade to blocked/review.
* MCP MVP does not auto-blacklist vacancies remotely.
* MCP MVP does not send SMTP emails.
* MCP MVP does not solve HH tests.
* Token/cookies are saved after MCP tool invocation.
* Existing CLI tests remain green.
* New service/MCP test suite passes.

[1]: https://modelcontextprotocol.io/specification/draft/basic/transports "Transports - Model Context Protocol"
