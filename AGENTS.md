# Project: HH Applicant Tool

CLI-утилита для автоматизации действий соискателя на hh.ru: авторизация, массовые отклики, обновление резюме, ответы работодателям, работа с локальной SQLite-базой и вспомогательные операции через API и web-сессию.

Сейчас в проекте есть отдельный LLM-модуль для автоответов в чатах работодателей через OpenRouter. Он живет отдельно от основного пакета, но запускается через обычный CLI `hh-applicant-tool chat-agent`.

**Tech Stack:**
- Language: Python 3.11+
- Packaging: Poetry (`pyproject.toml`), entrypoint `hh-applicant-tool`
- Packages: основной пакет `src/hh_applicant_tool/` + отдельный root-level пакет `hh_llm_agent/`
- Key libs: `requests`, `openai`, `playwright` (optional), `prettytable`, `pillow` (optional)
- Storage: SQLite (`config/<profile>/data`) + SQL schema in `src/hh_applicant_tool/storage/queries/schema.sql`
- Infra:
  - основной runtime: Docker + cron (`Dockerfile`, `docker-compose.yml`, `startup.sh`, `crontab`)
  - LLM agent runtime: `Dockerfile.llm-agent`, `docker-compose.llm-agent.yml`

## Branch
The default branch is `main`.

## Project Structure

```
`hh-applicant-tool/`
- `src/hh_applicant_tool/main.py` - точка входа CLI, загрузка операций, общий runtime-контекст (`session`, `api_client`, `storage`, `config`)
- `src/hh_applicant_tool/operations/` - набор CLI-команд (плагинная модель: модули подхватываются автоматически)
- `src/hh_applicant_tool/api/` - HTTP-клиенты к hh API/OAuth, типы и обработка API-ошибок
- `src/hh_applicant_tool/storage/` - facade + repositories + dataclass-модели для SQLite
- `src/hh_applicant_tool/ai/` - старый OpenAI-compatible слой для простых single-shot сценариев
- `src/hh_applicant_tool/utils/` - утилиты (конфиг, логирование, cookiejar, терминал, JSON и пр.)
- `hh_llm_agent/` - отдельный root-level модуль LLM-агента для автоответов в чатах работодателей
- `tests/` - тесты (pytest)
- `docs/hhapi/openapi.yml` - архивная OpenAPI-спека hh API
- `config/` - runtime-данные профилей (tokens/cookies/log/db), локальные и не для коммита секретов
- `Dockerfile`, `docker-compose.yml`, `startup.sh`, `crontab` - контейнерный запуск основного инструмента
- `Dockerfile.llm-agent`, `docker-compose.llm-agent.yml` - контейнерный запуск daemon-режима LLM-агента
```

### Key Modules
- **HHApplicantTool (`main.py`)**: формирует parser, динамически регистрирует команды из `operations`, предоставляет shared services для операций. Настраивает логгеры `hh_applicant_tool` и `hh_llm_agent`.
- **ApiClient/OAuthClient (`api/client.py`)**: обертка над `requests` c rate-delay, авторизационными заголовками и авто-refresh access token.
- **StorageFacade (`storage/facade.py`)**: единая точка доступа к persistence-слою SQLite, включая репозитории агента.
- **Chat Agent Operation (`src/hh_applicant_tool/operations/chat_agent.py`)**: CLI-адаптер для `hh_llm_agent`, поддерживает one-shot и daemon-loop режимы. Отвечает за daemon циклы, quiet hours логирование и cycle summary.
- **ChatAgentService (`hh_llm_agent/service.py`)**: основной workflow автоответов: batch-run, quiet hours, debounce свежих сообщений работодателя, single-reply/QA-series планирование, outbox и audit в SQLite. Логирует run lifecycle, negotiation start/outcome и LLM решения на INFO.
- **OpenRouterChatClient (`hh_llm_agent/openrouter.py`)**: клиент OpenRouter через SDK `openai` со structured outputs (`response_format=json_schema`), `require_parameters=true`, reasoning и `response-healing` plugin.
- **TimingPolicy (`hh_llm_agent/timing.py`)**: политика сна агента, quiet hours по таймзоне, jitter между циклами и сообщениями.

## Architecture & Rules

### 1. Patterns
- CLI plugin architecture: новая команда = новый модуль в `src/hh_applicant_tool/operations/` c классом `Operation`.
- Shared application context: операции не создают клиентов вручную, а используют `tool.api_client`, `tool.storage`, `tool.config`, `tool.session`.
- Separate root module for agent logic: сложная LLM-логика живет в `hh_llm_agent/`, а `operations/chat_agent.py` остается тонким адаптером.
- Persistence via repositories: доступ к БД через `StorageFacade`/репозитории, а не raw SQL по всему коду (исключая команду `query`).
- Hybrid integration: часть действий выполняется через API, часть через web-сессию/cookies (например, XSRF и browser-auth сценарии).
- Idempotent chat processing: агент не должен отвечать повторно на одно и то же последнее сообщение, если нет явного `--force`.
- Durable reply delivery: если агент планирует серию ответов, они должны жить в outbox и переживать рестарты/quiet hours без дублей.

### 2. LLM Agent Workflow (2-stage classifier + reply LLM)
- Источник чатов: агент читает переговоры через `tool.get_negotiations()` и историю сообщений через `/negotiations/{nid}/messages`.
- Batch schedule: в daemon-режиме агент работает проходами, затем спит случайный интервал `sleep_min_minutes..sleep_max_minutes`; ночью по умолчанию не отвечает (`23:00-08:00`, `Europe/Moscow`).
- Фильтрация: агент пропускает неподходящие переговоры по `resume_id`, `period_days`, blacklist, `only_invitations`, состоянию `discard` и отсутствию текстовых сообщений.
- Триггер ответа: агент отвечает только на неотвеченный хвост сообщений работодателя после последнего сообщения кандидата.
- Debounce: если последнее сообщение работодателя слишком свежее, агент выдерживает `incoming_collect_seconds`, перечитывает чат и пытается склеить подряд идущие реплики в один пакет.
- Дедупликация: если `(negotiation_id, last_message_id)` уже есть в `agent_decisions`, чат пропускается, если не передан `--force`.
- **Stage 1 — Classifier gate** (если включен): отдельная дешевая модель классифицирует входящий пакет employer-tail по категориям:
  - `human_actionable` — есть прямой вопрос, screening, просьба данных, подтверждающий интерес → pass to reply LLM
  - `bot_actionable` — hh bot / AI assistant / robot recruiter задает вопросы → pass to reply LLM
  - `passive_update` — автоуведомления, thank-you без действия → skip
  - `marketing_broadcast` — брендовые портянки, соцсети, "узнайте нас лучше" → skip
  - `system_event` — join/leave ботов, системные события → skip
  - `irrelevant` — всё остальное → skip
  - Если classifier сказал `skip`, решение сразу сохраняется в `agent_decisions`, reply LLM не вызывается.
- **Stage 2 — Reply LLM**: только для `human_actionable` / `bot_actionable`:
  - В модель передаются системный prompt, контекст по кандидату/резюме/вакансии/работодателю, последние сообщения и неотвеченный employer-tail.
  - LLM decision: OpenRouter вызывается со structured JSON schema output (`action/reply_mode/reply_text/reply_messages/reason`), reasoning и `response-healing`; локальный repair round-trip не используется.
- Выполнение решения:
  - `skip` -> сохраняется решение в `agent_decisions` (classifier или reply-модель)
  - `reply` + `dry_run` -> classifier/reply LLM все равно выполняются, но ответ только печатается без отправки и без записи decision в БД
  - `reply(single)` -> один ответ отправляется через API `/negotiations/{nid}/messages`
  - `reply(qa_series)` -> 2-3 коротких сообщения кладутся в `agent_outbox` и отправляются с jitter между частями
- Audit/persistence:
  - `negotiations` - sync переговоров
  - `chat_messages` - локальный кеш сообщений
  - `agent_runs` - статистика запуска агента
  - `agent_decisions` - решения модели, причины skip, reply text, raw_response, reasoning_details, plus classifier metadata (`classifier_category`, `classifier_reason`, `classifier_confidence`, `classifier_model`, `classifier_raw_response`)
  - `agent_outbox` - отложенные части Q/A-серий, их статус, время отправки и ошибки

### 3. Logging (INFO level)
Chat agent пишет подробные INFO-логи на каждом этапе работы:

**Namespace & handlers:**
- Настроены два логгера: `hh_applicant_tool` и `hh_llm_agent`
- Daemon-режим автоматически поднимает консольный уровень до INFO (чтобы видно было в `docker logs`)
- `setup_logger()` очищает дублирующиеся handlers перед настройкой
- В лог-файл (`config/<profile>/log.txt`) пишется всё (DEBUG)
- В контейнер выводятся INFO и выше

**Chat Agent Operation logs (`operations/chat_agent.py`):**
- Startup banner: профиль, модель, `dry_run`, timezone, quiet hours, текущее локальное время, sleep window
- Quiet hours: явный лог при входе в ночной режим с `sleeping_until` (timestamp пробуждения)
- Cycle start: номер цикла
- Cycle summary: `total/replied/skipped/errors` по результатам цикла
- Cycle sleep: сколько секунд до следующего цикла

**ChatAgentService logs (`hh_llm_agent/service.py`):**
- Run started: `run_id`, `dry_run`, `limit`, `resume_id`, `only_invitations`, `skip_blacklisted`, `force`
- Context loaded: имя кандидата, количество резюме и работодателей в blacklist
- Negotiation start: `negotiation_id`, вакансия, работодатель, статус, резюме
- Negotiation outcome (skip): причина (например, `blacklisted`, `no_messages`, `outside_period`, `already_processed`)
- LLM decision: `action` (reply/skip), `reply_mode` (single/qa_series), количество сообщений, `reason`
- Negotiation outcome (reply): тип ответа, режим, количество сообщений
- Run completed/failed: `run_id`, финальная статистика `total/replied/skipped/errors`

**Для отладки (DEBUG):**
- Полные запросы/ответы API
- Детали LLM-промптов (контекст, история сообщений)
- Технические детали OpenRouter-вызовов
- Полное состояние internal-объектов

**Важно:**
- INFO-логи не содержат полных текстов сообщений, чтобы не светить данные
- Все sensitive-данные (токены, длинные ID) редактируются в лог-файле через `RedactingFilter`
- При одновременной работе двух сервисов с одним профилем они пишут в один `log.txt` — в будущем можно разделить

### 4. Conventions
- **CLI aliases**: для пользовательских команд часто задаются короткие алиасы (`auth`, `apply`, `ls` и т.п.).
- **Config/profile model**: профиль выбирается через `--profile-id` или `HH_PROFILE_ID`; данные лежат в каталоге профиля.
- **Error handling**: в `HHApplicantTool.run()` централизованно обрабатываются API/SQLite/runtime исключения, лог пишется в профильный `log.txt`.
- **Typing/linting**: pyright включен в режиме `off`; основной линтинг через `ruff` и `pylint`.
- **Tests**: использовать `pytest` (основной smoke check перед изменениями в логике).
- **OpenRouter defaults**: базовая модель по умолчанию - `google/gemini-3.1-flash-lite-preview`, reasoning включен по умолчанию, max_completion_tokens=1200, max_history_messages=12, temperature=0.2.
- **Classifier defaults**: классификатор включен по умолчанию, дефолтная модель — `google/gemma-3-27b-it`, deterministic режим (temperature=0), reasoning выключен, max_completion_tokens=256, max_history_messages=8.
- **Chat timing defaults**: quiet hours включены по умолчанию (`23:00-08:00`, `Europe/Moscow`), debounce новых employer-сообщений 120 секунд, batch-sleep 20-30 минут.

## Runtime Notes for Agents

### Chat Agent Config Sources
- CLI flags in `chat-agent`
- `config.json` keys:
  - `openrouter.api_key` (обязателен)
  - `openrouter.model` (дефолт: `google/gemini-3.1-flash-lite-preview`)
  - `openrouter.temperature` (дефолт: 0.2)
  - `openrouter.max_completion_tokens` (дефолт: 1200)
  - `openrouter.reasoning_enabled` (дефолт: true)
  - `chat_agent.classifier.enabled` (дефолт: true) — включить/выключить classifier gate
  - `chat_agent.classifier.model` (дефолт: `google/gemma-3-27b-it`) — модель классификатора
  - `chat_agent.classifier.temperature` (дефолт: 0.0) — deterministic для классификатора
  - `chat_agent.classifier.max_completion_tokens` (дефолт: 256) — маленький лимит токенов
  - `chat_agent.classifier.reasoning_enabled` (дефолт: false) — reasoning выключен для экономии
  - `chat_agent.classifier.max_history_messages` (дефолт: 8) — сколько истории давать классификатору
  - `chat_agent.classifier.system_prompt` (есть дефолт на русском)
  - `chat_agent.classifier.instruction` (есть дефолт с категориями)
  - `chat_agent.system_prompt` (есть дефолт на русском)
  - `chat_agent.reply_instruction` (есть дефолт)
  - `chat_agent.max_history_messages` (дефолт: 12)
  - `chat_agent.period_days` (дефолт: None - без фильтра)
  - `chat_agent.only_invitations` (дефолт: false)
  - `chat_agent.dry_run` (дефолт: false)
  - `chat_agent.limit` (дефолт: None - без лимита)
  - `chat_agent.resume_id` (дефолт: None - все резюме)
  - `chat_agent.skip_blacklisted` (дефолт: true)
  - `chat_agent.force` (дефолт: false)
  - `chat_agent.sleep_min_minutes` / `chat_agent.sleep_max_minutes` (дефолт: 20/30)
  - `chat_agent.timezone` (дефолт: `Europe/Moscow`)
  - `chat_agent.quiet_hours_enabled` / `quiet_hours_start` / `quiet_hours_end`
  - `chat_agent.incoming_collect_seconds` (дефолт: 120)
  - `chat_agent.reply_delay_min_seconds` / `reply_delay_max_seconds`
  - `chat_agent.qa_series_delay_min_seconds` / `qa_series_delay_max_seconds`
- Environment variables from `.env.example`:
  - `OPENROUTER_API_KEY` (обязателен)
  - `OPENROUTER_MODEL`
  - `CHAT_AGENT_TEMPERATURE`
  - `CHAT_AGENT_DRY_RUN` (альтернатива: `HH_AGENT_DRY_RUN`)
  - `CHAT_AGENT_POLL_INTERVAL` (дефолт: 60 секунд)
  - `CHAT_AGENT_SLEEP_MIN_MINUTES`, `CHAT_AGENT_SLEEP_MAX_MINUTES`
  - `CHAT_AGENT_TIMEZONE`
  - `CHAT_AGENT_QUIET_HOURS`, `CHAT_AGENT_QUIET_HOURS_START`, `CHAT_AGENT_QUIET_HOURS_END`, `CHAT_AGENT_WAKE_JITTER_SECONDS`
  - `CHAT_AGENT_INCOMING_COLLECT_SECONDS`
  - `CHAT_AGENT_REPLY_DELAY_MIN_SECONDS`, `CHAT_AGENT_REPLY_DELAY_MAX_SECONDS`
  - `CHAT_AGENT_QA_SERIES_DELAY_MIN_SECONDS`, `CHAT_AGENT_QA_SERIES_DELAY_MAX_SECONDS`
  - `CHAT_AGENT_MAX_COMPLETION_TOKENS` (дефолт: 1200)
  - `CHAT_AGENT_MAX_HISTORY_MESSAGES` (дефолт: 12)
  - `CHAT_AGENT_PERIOD_DAYS`
  - `CHAT_AGENT_SYSTEM_PROMPT`
  - `CHAT_AGENT_REPLY_INSTRUCTION`
  - `CHAT_AGENT_CLASSIFIER_ENABLED` (дефолт: true)
  - `CHAT_AGENT_CLASSIFIER_MODEL` (дефолт: `google/gemma-3-27b-it`)
  - `CHAT_AGENT_CLASSIFIER_TEMPERATURE` (дефолт: 0.0)
  - `CHAT_AGENT_CLASSIFIER_MAX_COMPLETION_TOKENS` (дефолт: 256)
  - `CHAT_AGENT_CLASSIFIER_REASONING` (дефолт: false)
  - `CHAT_AGENT_CLASSIFIER_MAX_HISTORY_MESSAGES` (дефолт: 8)
  - `CHAT_AGENT_CLASSIFIER_SYSTEM_PROMPT`
  - `CHAT_AGENT_CLASSIFIER_INSTRUCTION`

### Docker Notes
- Основной app container и LLM agent container разные по назначению.
- `docker-compose.llm-agent.yml` запускает только daemon-сервис `llm_agent`.
- `Dockerfile.llm-agent` не включает Chromium/playwright/cron и должен оставаться легковесным.
- Контейнер LLM-агента по умолчанию запускает `chat-agent --daemon`.
- В `docker-compose.llm-agent.yml` используется флаг `-v` для вывода INFO-логов в контейнер.

## Instructions for Agents
1. Сначала прочитай `README.md` и `src/hh_applicant_tool/main.py`, чтобы понять runtime и список доступных операций.
2. Перед добавлением новой команды проверь, нет ли близкой реализации в `src/hh_applicant_tool/operations/`.
3. Если меняешь LLM-агент, смотри и CLI-адаптер `src/hh_applicant_tool/operations/chat_agent.py`, и root-level модуль `hh_llm_agent/`.
4. Для изменений БД синхронизируй `storage/models`, `storage/repositories` и SQL-схему в `storage/queries/schema.sql`.
5. Если меняешь audit/state логику агента, проверь согласованность таблиц `chat_messages`, `agent_runs`, `agent_decisions`, `agent_outbox`.
6. При изменении логирования проверь тесты `tests/test_chat_agent_service.py` и `tests/test_chat_agent_operation.py` для подтверждения формата INFO-логов.
7. Не коммить секреты и runtime-артефакты из `config/`, `.env`, токены, cookies и локальные DB/log файлы.
8. Для проверки изменений запускай минимум `pytest`, а для CLI-поведения - целевую команду через `python -m hh_applicant_tool ...`.
9. Для OpenRouter-логики отдельно полезно гонять `tests/test_openrouter_client.py`.
10. **Tuning classifier prompts**: если агент пропускает шум (автоответы, брендовые рассылки, thank-you без действия), скорректируй `chat_agent.classifier.instruction` или `chat_agent.classifier.system_prompt`; classifier дешевый, поэтому можно агрессивно тюнить под реальные кейсы шума, не тратя токены на reply-модель.
