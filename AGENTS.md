# Project: HH Applicant Tool

CLI-утилита для автоматизации действий соискателя на hh.ru: авторизация, массовые отклики, обновление резюме, ответы работодателям, работа с локальной SQLite-базой и вспомогательные операции через API и web-сессию.

**Tech Stack:**
- Language: Python 3.13
- Packaging: Poetry (`pyproject.toml`), entrypoint `hh-applicant-tool`
- Key libs: `requests`, `playwright` (optional), `prettytable`, `pillow` (optional)
- Storage: SQLite (`config/<profile>/data`) + SQL schema in `src/hh_applicant_tool/storage/queries/schema.sql`
- Infra: Docker + cron (`Dockerfile`, `docker-compose.yml`, `startup.sh`, `crontab`)

## Branch
The default branch is `main`.

## Project Structure

```
`hh-applicant-tool/`
- `src/hh_applicant_tool/main.py` - точка входа CLI, загрузка операций, общий runtime-контекст (`session`, `api_client`, `storage`, `config`)
- `src/hh_applicant_tool/operations/` - набор CLI-команд (плагинная модель: модули подхватываются автоматически)
- `src/hh_applicant_tool/api/` - HTTP-клиенты к hh API/OAuth, типы и обработка API-ошибок
- `src/hh_applicant_tool/storage/` - facade + repositories + dataclass-модели для SQLite
- `src/hh_applicant_tool/ai/` - интеграция с OpenAI-совместимым chat completion endpoint
- `src/hh_applicant_tool/utils/` - утилиты (конфиг, логирование, cookiejar, терминал, JSON и пр.)
- `tests/` - тесты (pytest)
- `docs/hhapi/openapi.yml` - архивная OpenAPI-спека hh API
- `config/` - runtime-данные профилей (tokens/cookies/log/db), локальные и не для коммита секретов
- `Dockerfile`, `docker-compose.yml`, `startup.sh`, `crontab` - контейнерный запуск и периодические задачи
```

### Key Modules
- **HHApplicantTool (`main.py`)**: формирует parser, динамически регистрирует команды из `operations`, предоставляет shared services для операций.
- **ApiClient/OAuthClient (`api/client.py`)**: обертка над `requests` c rate-delay, авторизационными заголовками и авто-refresh access token.
- **StorageFacade (`storage/facade.py`)**: единая точка доступа к репозиториям SQLite, инициализирует схему БД.
- **Operations**: каждая команда реализует `Operation` с `setup_parser()` и `run(tool)`.

## Architecture & Rules

### 1. Patterns
- CLI plugin architecture: новая команда = новый модуль в `operations/` c классом `Operation`.
- Shared application context: операции не создают клиентов вручную, а используют `tool.api_client`, `tool.storage`, `tool.config`, `tool.session`.
- Persistence via repositories: доступ к БД через `StorageFacade`/репозитории, а не raw SQL по всему коду (исключая команду `query`).
- Hybrid integration: часть действий выполняется через API, часть через web-сессию/cookies (например, XSRF и browser-auth сценарии).

### 2. Conventions
- **CLI aliases**: для пользовательских команд часто задаются короткие алиасы (`auth`, `apply`, `ls` и т.п.).
- **Config/profile model**: профиль выбирается через `--profile-id` или `HH_PROFILE_ID`; данные лежат в каталоге профиля.
- **Error handling**: в `HHApplicantTool.run()` централизованно обрабатываются API/SQLite/runtime исключения, лог пишется в профильный `log.txt`.
- **Typing/linting**: pyright включен в режиме `off`; основной линтинг через `ruff` и `pylint`.
- **Tests**: использовать `pytest` (основной smoke check перед изменениями в логике).

## Instructions for Agents
1. Сначала прочитай `README.md` и `src/hh_applicant_tool/main.py`, чтобы понять runtime и список доступных операций.
2. Перед добавлением новой команды проверь, нет ли близкой реализации в `src/hh_applicant_tool/operations/`.
3. Для изменений БД синхронизируй `storage/models`, `storage/repositories` и SQL-схему в `storage/queries/schema.sql`.
4. Не коммить секреты и runtime-артефакты из `config/` (tokens, cookies, локальные DB/log файлы).
5. Для проверки изменений запускай минимум `pytest`, а для CLI-поведения - целевую команду через `python -m hh_applicant_tool ...`.
