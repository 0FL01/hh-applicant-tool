# Project: HH Applicant Tool

CLI-утилита для автоматизации действий соискателя на hh.ru: авторизация, массовые отклики, обновление резюме, ответы работодателям, работа с локальной SQLite-базой и вспомогательные операции через API и web-сессию.

LLM-модуль для автоответов в чатах работодателей через OpenRouter живет в `hh_llm_agent/` и запускается через CLI `hh-applicant-tool chat-agent`.

**Tech Stack:**
- Language: Python 3.11+
- Packaging: Poetry (`pyproject.toml`), entrypoint `hh-applicant-tool`
- Packages: `src/hh_applicant_tool/` + root-level `hh_llm_agent/`
- Key libs: `requests`, `openai`, `playwright` (optional), `prettytable`, `pillow` (optional)
- Storage: SQLite (`config/<profile>/data`) + schema in `src/hh_applicant_tool/storage/queries/schema.sql`
- Infra: Docker + cron (`Dockerfile`, `docker-compose.yml`) / LLM-agent: `Dockerfile.llm-agent`, `docker-compose.llm-agent.yml`

## Project Structure

```
`hh-applicant-tool/`
- `src/hh_applicant_tool/__main__.py` - entrypoint для `python -m hh_applicant_tool`
- `src/hh_applicant_tool/main.py` - точка входа CLI, загрузка операций, shared context
- `src/hh_applicant_tool/operations/` - 20 CLI-команд (плагинная модель)
- `src/hh_applicant_tool/api/` - HTTP-клиенты к hh API/OAuth
  - `client.py` - ApiClient, OAuthClient
  - `datatypes.py` - типы данных HH API
  - `errors.py` - обработка ошибок API
  - `user_agent.py` - генерация Android User-Agent
  - `client_keys.py` - Android OAuth ключи
- `src/hh_applicant_tool/storage/` - persistence слой
  - `facade.py` - точка доступа к БД
  - `utils.py` - init_db, apply_migrations
  - `models/` - 14 dataclass-моделей
  - `repositories/` - 15 репозиториев
  - `queries/schema.sql` - схема БД с индексами и триггерами
  - `queries/migrations/` - SQL-миграции (для развития схемы)
- `src/hh_applicant_tool/ai/` - старый OpenAI-compatible слой
- `src/hh_applicant_tool/utils/` - 14 утилит (config, log, cookiejar, terminal, JSON, date, string, misc, binpack, attrdict, mixins, datatypes)
- `hh_llm_agent/` - LLM-агент для автоответов
  - `config.py`, `gateway.py`, `service.py`, `openrouter.py`, `timing.py`
  - `contact_extract.py`, `webhook.py`, `tg_bot_client.py`
  - `tg_collector_store.py`, `tg_contact_collector.py`
- `tests/` - pytest тесты (10 файлов)
- `docs/hhapi/openapi.yml` - архивная OpenAPI-спека
- `config/` - runtime-данные профилей (tokens/cookies/log/db)
- `Dockerfile`, `docker-compose.yml`, `startup.sh`, `crontab` - основной контейнер
- `Dockerfile.llm-agent`, `docker-compose.llm-agent.yml` - LLM-агент контейнер
```

## CLI Operations (20 команд)

| Module | Command | Aliases |
|--------|---------|---------|
| `authorize.py` | `authorize` | `auth`, `authenticate`, `login` |
| `logout.py` | `logout` | `exit` |
| `whoami.py` | `whoami` | `id` |
| `list_resumes.py` | `list-resumes` | `ls-resumes`, `resumes` |
| `clone_resume.py` | `clone-resume` | — |
| `update_resumes.py` | `update-resumes` | `update` |
| `apply_vacancies.py` | `apply-vacancies` | `apply`, `apply-similar` |
| `reply_employers.py` | `reply-employers` | `reply-empls`, `reply-chats`, `reall` |
| `chat_agent.py` | `chat-agent` | `ai-agent`, `reply-agent` |
| `tg_contact_collector.py` | `tg-contact-collector` | `tg-collector` |
| `clear_negotiations.py` | `clear-negotiations` | `delete-negotiations` |
| `call_api.py` | `call-api` | `api` |
| `test_session.py` | `test-session` | — |
| `refresh_token.py` | `refresh-token` | `refresh` |
| `check_proxy.py` | `check-proxy` | — |
| `config.py` | `config` | — |
| `settings.py` | `settings` | `setting` |
| `query.py` | `query` | `sql` |
| `migrate_db.py` | `migrate-db` | `migrate` |
| `log.py` | `log` | — |
| `install.py` | `install` | — |
| `uninstall.py` | `uninstall` | — |

## Key Modules

- **HHApplicantTool (`main.py`)**: формирует parser, динамически регистрирует команды из `operations`, предоставляет shared services. Настраивает логгеры `hh_applicant_tool` и `hh_llm_agent`.
- **ApiClient/OAuthClient (`api/client.py`)**: обертка над `requests` c rate-delay, авторизационными заголовками и авто-refresh access token.
- **StorageFacade (`storage/facade.py`)**: единая точка доступа к SQLite, включает все репозитории.
- **HHGateway (`hh_llm_agent/gateway.py`)**: абстракционный слой над HH API для агента.
- **Chat Agent Operation (`operations/chat_agent.py`)**: CLI-адаптер для `hh_llm_agent`, one-shot и daemon-loop режимы.
- **ChatAgentService (`hh_llm_agent/service.py`)**: workflow автоответов: batch-run, quiet hours, debounce, outbox, audit в SQLite. INFO-логи на каждом этапе.
- **OpenRouterChatClient (`hh_llm_agent/openrouter.py`)**: OpenRouter через SDK `openai` со structured outputs, reasoning, rate limiting и retry.
- **TimingPolicy (`hh_llm_agent/timing.py`)**: политика сна, quiet hours по таймзоне, jitter.
- **TelegramContactCollectorService (`hh_llm_agent/tg_contact_collector.py`)**: HTTP-приемник webhook `recruiter_contact_offer`, сохраняет лид в SQLite и пересылает через Telegram Bot API. Idempotency, retry при сбоях Telegram. Также принимает generic-вебхуки на `POST /webhooks/generic` — любой JSON пересылается в Telegram с поддержкой шаблонов через заголовок `X-Telegram-Template` (синтаксис `{{key}}`, `{{nested.key}}`). Без шаблона отправляется JSON целиком.
- **Storage Models**: 14 dataclass-моделей — `Employer`, `Vacancy`, `VacancyContact`, `VacancyResponseDedup`, `Negotiation`, `ChatMessage`, `Resume`, `AgentRun`, `AgentDecision`, `AgentOutbox`, `AgentWebhook`, `EmployerSite`, `Setting`, `Base`.

## Architecture & Rules

### 1. Patterns
- CLI plugin architecture: новая команда = новый модуль в `operations/` c классом `Operation`.
- Shared application context: операции используют `tool.api_client`, `tool.storage`, `tool.config`, `tool.session`.
- Separate root module for agent logic: `hh_llm_agent/` — тонкий адаптер в `operations/chat_agent.py`.
- Persistence via repositories: доступ к БД через `StorageFacade`/репозитории (кроме команды `query`).
- Hybrid integration: API + web-сессия/cookies.
- Idempotent chat processing: без `--force` агент не отвечает повторно.
- Durable reply delivery: Q/A-серии живут в outbox и переживают рестарты/quiet hours.

### 2. LLM Agent Workflow (2-stage classifier + reply LLM)
- Источник чатов: `get_negotiations()` + `/negotiations/{nid}/messages`.
- Batch schedule: daemon-режим — проходы с паузой `sleep_min_minutes..sleep_max_minutes`; quiet hours `23:00-08:00` Europe/Moscow.
- Фильтрация: `resume_id`, `period_days`, blacklist, `only_invitations`, `discard`, отсутствие текстовых сообщений.
- Триггер: только неотвеченный хвост сообщений работодателя после последнего кандидата.
- Debounce: `incoming_collect_seconds` для склейки подряд идущих реплик.
- Дедупликация: `(negotiation_id, last_message_id)` в `agent_decisions`.
- **Stage 1 — Classifier gate** (если включен):
  - `human_actionable` / `bot_actionable` → pass to reply LLM
  - `passive_update` / `marketing_broadcast` / `system_event` / `irrelevant` → skip
  - `recruiter_contact_offer` → webhook delivery + skip
- **Stage 2 — Reply LLM**: для `human_actionable` / `bot_actionable` со structured JSON output.
- Выполнение:
  - `skip` → decision в `agent_decisions`
  - `reply` + `dry_run` → только печать, без записи в БД
  - `reply(single)` → один ответ через API
  - `reply(qa_series)` → 2-3 сообщения в `agent_outbox` с jitter

### 3. Database Tables & Audit
- `negotiations`, `chat_messages`, `agent_runs`, `agent_decisions`, `agent_outbox`, `agent_webhooks`
- `employers`, `vacancies`, `vacancy_contacts`, `vacancy_response_dedup`, `employer_sites`
- `resumes`, `settings`
- Индексы: `idx_vac_upd`, `idx_vacancy_response_dedup_resume_key`, `idx_emp_upd`, `idx_neg_upd`, `idx_chat_messages_neg`, `idx_agent_decisions_neg_msg`, `idx_agent_outbox_status_send_after`, `idx_agent_runs_created`, `idx_emp_site_upd`
- Триггеры: автоматическое `updated_at` при изменении записей

### 4. Logging (INFO level)
- Два логгера: `hh_applicant_tool` и `hh_llm_agent`
- Daemon-режим поднимает консольный уровень до INFO
- Лог-файл: `config/<profile>/log.txt` (DEBUG), контейнер: INFO+
- INFO-логи не содержат полных текстов сообщений
- Sensitive-данные редактируются через `RedactingFilter`

**Chat Agent Operation logs:**
- Startup banner, Quiet hours entry, Cycle start/summary/sleep

**ChatAgentService logs:**
- Run started/failed, Context loaded, Negotiation start/outcome, LLM decision, Reply outcome

### 5. Conventions
- **CLI aliases**: см. таблицу выше.
- **Config/profile**: `--profile-id` или `HH_PROFILE_ID`; данные в `config/<profile>/`.
- **Error handling**: централизованно в `HHApplicantTool.run()`, лог в `log.txt`.
- **Typing/linting**: pyright off, основной — `ruff` и `pylint`.
- **Tests**: `pytest`.

## Runtime Notes

### OpenRouter Defaults
- Model: `google/gemini-3.1-flash-lite-preview`, reasoning enabled, max_tokens=1200, temperature=0.2, request_interval=0.35s, max_retries=2, rate_limit_retry_base=2.0s

### Classifier Defaults
- Model: `google/gemma-3-27b-it`, enabled, temperature=0, reasoning=false, max_tokens=256, max_history=8

### Chat Timing Defaults
- quiet_hours: `23:00-08:00` Europe/Moscow
- debounce: 120s, batch_sleep: 20-30min, reply_delay: 15-60s, qa_series_delay: 30-120s, wake_jitter: 300s

### Config Sources (chat-agent)
**CLI flags** → **config.json** → **environment variables**

**config.json keys:**
- `openrouter.*`: api_key, base_url, model, temperature, max_completion_tokens, reasoning_enabled, request_interval_seconds, max_retries_on_rate_limit, rate_limit_retry_base_seconds
- `chat_agent.classifier.*`: enabled, model, temperature, max_completion_tokens, reasoning_enabled, max_history_messages, system_prompt, instruction
- `chat_agent.*`: system_prompt, reply_instruction, max_history_messages, period_days, only_invitations, dry_run, limit, resume_id, skip_blacklisted, force, sleep_min_minutes, sleep_max_minutes, timezone, quiet_hours_enabled, quiet_hours_start, quiet_hours_end, wake_jitter_seconds, incoming_collect_seconds, reply_delay_min_seconds, reply_delay_max_seconds, qa_series_delay_min_seconds, qa_series_delay_max_seconds
- `chat_agent.webhook.*`: url, enabled, timeout_seconds, secret, secret_header, max_attempts, retry_base_seconds, verify_ssl

**Environment variables:**
- `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`
- `CHAT_AGENT_TEMPERATURE`, `CHAT_AGENT_DRY_RUN` / `HH_AGENT_DRY_RUN`
- `CHAT_AGENT_POLL_INTERVAL`, `CHAT_AGENT_SLEEP_MIN_MINUTES`, `CHAT_AGENT_SLEEP_MAX_MINUTES`
- `CHAT_AGENT_TIMEZONE`, `CHAT_AGENT_QUIET_HOURS`, `CHAT_AGENT_QUIET_HOURS_START`, `CHAT_AGENT_QUIET_HOURS_END`, `CHAT_AGENT_WAKE_JITTER_SECONDS`
- `CHAT_AGENT_INCOMING_COLLECT_SECONDS`, `CHAT_AGENT_REPLY_DELAY_MIN_SECONDS`, `CHAT_AGENT_REPLY_DELAY_MAX_SECONDS`, `CHAT_AGENT_QA_SERIES_DELAY_MIN_SECONDS`, `CHAT_AGENT_QA_SERIES_DELAY_MAX_SECONDS`
- `CHAT_AGENT_MAX_COMPLETION_TOKENS`, `CHAT_AGENT_MAX_HISTORY_MESSAGES`, `CHAT_AGENT_PERIOD_DAYS`
- `CHAT_AGENT_SYSTEM_PROMPT`, `CHAT_AGENT_REPLY_INSTRUCTION`
- `CHAT_AGENT_CLASSIFIER_ENABLED`, `CHAT_AGENT_CLASSIFIER_MODEL`, `CHAT_AGENT_CLASSIFIER_TEMPERATURE`, `CHAT_AGENT_CLASSIFIER_MAX_COMPLETION_TOKENS`, `CHAT_AGENT_CLASSIFIER_REASONING`, `CHAT_AGENT_CLASSIFIER_MAX_HISTORY_MESSAGES`, `CHAT_AGENT_CLASSIFIER_SYSTEM_PROMPT`, `CHAT_AGENT_CLASSIFIER_INSTRUCTION`
- `CHAT_AGENT_OPENROUTER_REQUEST_INTERVAL_SECONDS`, `CHAT_AGENT_OPENROUTER_MAX_RETRIES_ON_RATE_LIMIT`, `CHAT_AGENT_OPENROUTER_RATE_LIMIT_RETRY_BASE_SECONDS`
- `CHAT_AGENT_WEBHOOK_ENABLED`, `CHAT_AGENT_WEBHOOK_URL`, `CHAT_AGENT_WEBHOOK_TIMEOUT_SECONDS`, `CHAT_AGENT_WEBHOOK_VERIFY_SSL`, `CHAT_AGENT_WEBHOOK_SECRET`, `CHAT_AGENT_WEBHOOK_SECRET_HEADER`, `CHAT_AGENT_WEBHOOK_MAX_ATTEMPTS`, `CHAT_AGENT_WEBHOOK_RETRY_BASE_SECONDS`

**Telegram Collector env vars:**
- `TELEGRAM_COLLECTOR_BOT_TOKEN`, `TELEGRAM_COLLECTOR_TARGET_CHAT_ID`
- `TELEGRAM_COLLECTOR_LISTEN_HOST`, `TELEGRAM_COLLECTOR_LISTEN_PORT`
- `TELEGRAM_COLLECTOR_WEBHOOK_SECRET`, `TELEGRAM_COLLECTOR_WEBHOOK_SECRET_HEADER`
- `TELEGRAM_COLLECTOR_DB_PATH`, `TELEGRAM_COLLECTOR_TIMEOUT_SECONDS`, `TELEGRAM_COLLECTOR_VERIFY_SSL`
- `TELEGRAM_COLLECTOR_BOT_API_BASE_URL`

### Docker Notes
- `docker-compose.llm-agent.yml` запускает `llm_agent` (daemon-режим) и `tg_contact_collector` (webhook-приемник).
- `Dockerfile.llm-agent` — легковесный, без playwright/Chromium/cron.
- Основной `Dockerfile` включает playwright/Chromium и cron.
- Контейнер LLM-агента запускает `chat-agent --daemon` по умолчанию.
- Контейнер коллектора слушает порт `8787`.
- Флаг `-v` в docker-compose для INFO-логов в контейнер.

## Instructions for Agents
1. Прочитай `README.md` и `src/hh_applicant_tool/main.py` для понимания runtime и списка операций.
2. Перед добавлением новой команды проверь существующие в `operations/`.
3. Для LLM-агента смотри и `operations/chat_agent.py`, и `hh_llm_agent/`.
4. Для изменений БД синхронизируй `storage/models`, `storage/repositories` и `storage/queries/schema.sql`.
5. При изменении audit/state логики агента проверь согласованность таблиц `chat_messages`, `agent_runs`, `agent_decisions`, `agent_outbox`, `agent_webhooks`.
6. При изменении логирования проверь тесты `tests/test_chat_agent_service.py` и `tests/test_chat_agent_operation.py`.
7. Не коммить секреты и runtime-артефакты из `config/`, `.env`, tokens, cookies, DB/log файлы.
8. Для проверки: `pytest` + `python -m hh_applicant_tool <command>`.
9. Для OpenRouter-логики: `tests/test_openrouter_client.py`.
10. **Tuning classifier prompts**: корректируй `chat_agent.classifier.instruction` или `chat_agent.classifier.system_prompt` — classifier дешевый.
11. **Webhook для recruiter_contact_offer**: при добавлении полей обнови `service.py::_build_contact_webhook_payload`, `webhook.py` и тесты.
