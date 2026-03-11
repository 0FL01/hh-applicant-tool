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
- **HHApplicantTool (`main.py`)**: формирует parser, динамически регистрирует команды из `operations`, предоставляет shared services для операций.
- **ApiClient/OAuthClient (`api/client.py`)**: обертка над `requests` c rate-delay, авторизационными заголовками и авто-refresh access token.
- **StorageFacade (`storage/facade.py`)**: единая точка доступа к persistence-слою SQLite, включая репозитории агента.
- **Chat Agent Operation (`src/hh_applicant_tool/operations/chat_agent.py`)**: CLI-адаптер для `hh_llm_agent`, поддерживает one-shot и daemon-loop режимы.
- **ChatAgentService (`hh_llm_agent/service.py`)**: основной workflow автоответов: отбор переговоров, чтение сообщений, вызов LLM, дедупликация, отправка ответа, audit в SQLite.
- **OpenRouterChatClient (`hh_llm_agent/openrouter.py`)**: клиент OpenRouter через SDK `openai` с reasoning и JSON repair.

## Architecture & Rules

### 1. Patterns
- CLI plugin architecture: новая команда = новый модуль в `src/hh_applicant_tool/operations/` c классом `Operation`.
- Shared application context: операции не создают клиентов вручную, а используют `tool.api_client`, `tool.storage`, `tool.config`, `tool.session`.
- Separate root module for agent logic: сложная LLM-логика живет в `hh_llm_agent/`, а `operations/chat_agent.py` остается тонким адаптером.
- Persistence via repositories: доступ к БД через `StorageFacade`/репозитории, а не raw SQL по всему коду (исключая команду `query`).
- Hybrid integration: часть действий выполняется через API, часть через web-сессию/cookies (например, XSRF и browser-auth сценарии).
- Idempotent chat processing: агент не должен отвечать повторно на одно и то же последнее сообщение, если нет явного `--force`.

### 2. LLM Agent Workflow
- Источник чатов: агент читает переговоры через `tool.get_negotiations()` и историю сообщений через `/negotiations/{nid}/messages`.
- Фильтрация: агент пропускает неподходящие переговоры по `resume_id`, `period_days`, blacklist, `only_invitations`, состоянию `discard` и отсутствию текстовых сообщений.
- Триггер ответа: агент отвечает только если последнее текстовое сообщение пришло от работодателя.
- Дедупликация: если `(negotiation_id, last_message_id)` уже есть в `agent_decisions`, чат пропускается, если не передан `--force`.
- Подготовка prompt: в модель передаются системный prompt, контекст по кандидату/резюме/вакансии/работодателю, последние сообщения и инструкция вернуть JSON `action/reply_text/reason`.
- LLM decision: OpenRouter вызывается с reasoning; если модель вернула невалидный JSON, выполняется repair-запрос с сохранением `reasoning_details`.
- Выполнение решения:
  - `skip` -> сохраняется решение в `agent_decisions`
  - `reply` + `dry_run` -> печатается предполагаемый ответ без отправки
  - `reply` -> сообщение отправляется через API `/negotiations/{nid}/messages`
- Audit/persistence:
  - `negotiations` - sync переговоров
  - `chat_messages` - локальный кеш сообщений
  - `agent_runs` - статистика запуска агента
  - `agent_decisions` - решения модели, причины skip, reply text, raw_response, reasoning_details

### 3. Conventions
- **CLI aliases**: для пользовательских команд часто задаются короткие алиасы (`auth`, `apply`, `ls` и т.п.).
- **Config/profile model**: профиль выбирается через `--profile-id` или `HH_PROFILE_ID`; данные лежат в каталоге профиля.
- **Error handling**: в `HHApplicantTool.run()` централизованно обрабатываются API/SQLite/runtime исключения, лог пишется в профильный `log.txt`.
- **Typing/linting**: pyright включен в режиме `off`; основной линтинг через `ruff` и `pylint`.
- **Tests**: использовать `pytest` (основной smoke check перед изменениями в логике).
- **OpenRouter defaults**: базовая модель по умолчанию - `google/gemini-3.1-flash-lite-preview`, reasoning включен по умолчанию, max_completion_tokens=1200, max_history_messages=12, temperature=0.2.

## Runtime Notes for Agents

### Chat Agent Config Sources
- CLI flags in `chat-agent`
- `config.json` keys:
  - `openrouter.api_key` (обязателен)
  - `openrouter.model` (дефолт: `google/gemini-3.1-flash-lite-preview`)
  - `openrouter.base_url` (дефолт: `https://openrouter.ai/api/v1`)
  - `openrouter.temperature` (дефолт: 0.2)
  - `openrouter.max_completion_tokens` (дефолт: 1200)
  - `openrouter.reasoning_enabled` (дефолт: true)
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
- Environment variables from `.env.example`:
  - `OPENROUTER_API_KEY` (обязателен)
  - `OPENROUTER_MODEL`
  - `OPENROUTER_BASE_URL`
  - `CHAT_AGENT_TEMPERATURE`
  - `CHAT_AGENT_DRY_RUN` (альтернатива: `HH_AGENT_DRY_RUN`)
  - `CHAT_AGENT_POLL_INTERVAL` (дефолт: 60 секунд)
  - `CHAT_AGENT_MAX_COMPLETION_TOKENS` (дефолт: 1200)
  - `CHAT_AGENT_MAX_HISTORY_MESSAGES` (дефолт: 12)
  - `CHAT_AGENT_PERIOD_DAYS`
  - `CHAT_AGENT_SYSTEM_PROMPT`
  - `CHAT_AGENT_REPLY_INSTRUCTION`

### Docker Notes
- Основной app container и LLM agent container разные по назначению.
- `docker-compose.llm-agent.yml` запускает только daemon-сервис `llm_agent`.
- `Dockerfile.llm-agent` не включает Chromium/playwright/cron и должен оставаться легковесным.
- Контейнер LLM-агента по умолчанию запускает `chat-agent --daemon`.

## Instructions for Agents
1. Сначала прочитай `README.md` и `src/hh_applicant_tool/main.py`, чтобы понять runtime и список доступных операций.
2. Перед добавлением новой команды проверь, нет ли близкой реализации в `src/hh_applicant_tool/operations/`.
3. Если меняешь LLM-агент, смотри и CLI-адаптер `src/hh_applicant_tool/operations/chat_agent.py`, и root-level модуль `hh_llm_agent/`.
4. Для изменений БД синхронизируй `storage/models`, `storage/repositories` и SQL-схему в `storage/queries/schema.sql`.
5. Если меняешь audit/state логику агента, проверь согласованность таблиц `chat_messages`, `agent_runs`, `agent_decisions`.
6. Не коммить секреты и runtime-артефакты из `config/`, `.env`, токены, cookies и локальные DB/log файлы.
7. Для проверки изменений запускай минимум `pytest`, а для CLI-поведения - целевую команду через `python -m hh_applicant_tool ...`.
8. Для OpenRouter-логики отдельно полезно гонять `tests/test_openrouter_client.py`.
