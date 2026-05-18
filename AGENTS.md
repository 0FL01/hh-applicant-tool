# Project: HH Applicant Tool

CLI-утилита для автоматизации действий соискателя на hh.ru: авторизация, массовые отклики, обновление резюме, ответы работодателям, SQLite-база, LLM-автоответчик через OpenRouter.

**Tech Stack:** Python 3.11+, Poetry, `requests`, `openai`, `playwright` (opt), `prettytable`, SQLite.

## Where To Look

| Path | Purpose |
|------|---------|
| `src/hh_applicant_tool/main.py` | CLI entrypoint, shared context (`api_client`, `storage`, `config`, `session`) |
| `src/hh_applicant_tool/operations/` | 25 команд (plugin-архитектура, класс `Operation`) |
| `src/hh_applicant_tool/api/` | HTTP-клиенты: `ApiClient`, `OAuthClient`, User-Agent, ключи |
| `src/hh_applicant_tool/storage/` | SQLite: `StorageFacade`, 15 repositories, 14 model dataclasses |
| `src/hh_applicant_tool/storage/queries/schema.sql` | Полная схема БД с индексами и триггерами |
| `src/hh_applicant_tool/storage/queries/migrations/` | SQL-миграции |
| `hh_llm_agent/` | LLM-агент: `gateway.py`, `service.py`, `openrouter.py`, `timing.py`, `webhook.py` |
| `config/<profile>/` | Runtime-данные: tokens, cookies, логи, SQLite |
| `tests/` | pytest-тесты |
| `docs/hhapi/openapi.yml` | Архивная OpenAPI-спека |

## Architectural Invariants

- **Plugin model**: новая команда = новый модуль в `operations/` с классом `Operation`. Динамическая регистрация через `pkgutil.iter_modules`.
- **Shared context**: операции получают `tool.api_client`, `tool.storage`, `tool.config`, `tool.session`.
- **Persistence**: доступ к БД только через `StorageFacade`/репозитории (исключение: команда `query`).
- **Hybrid integration**: API + web-сессия/cookies для некоторых операций.
- **Idempotent LLM replies**: без `--force` агент не отвечает повторно на тот же чат. Дедупликация по `(negotiation_id, last_message_id)`.
- **Durable outbox**: Q/A-серии живут в `agent_outbox`, переживают рестарты и quiet hours.
- **Logging**: два логгера — `hh_applicant_tool` и `hh_llm_agent`. Файл DEBUG, контейнер INFO+. Sensitive-данные через `RedactingFilter`.
- **Config priority**: CLI flags > `config.json` > environment variables. Профиль через `--profile-id` или `HH_PROFILE_ID`.

## CLI Overview (25 команд)

**Авторизация и сессия:** `authorize`, `logout`, `whoami`, `refresh-token`, `test-session`

**Резюме:** `list-resumes`, `clone-resume`, `update-resumes`

**Отклики и чаты:** `apply-vacancies`, `reply-employers`, `clear-negotiations`

**LLM-агент:** `chat-agent` (daemon/one-shot), `tg-contact-collector` (webhook-приёмник)

**Профиль и БД:** `config`, `settings`, `query` (прямой SQL), `migrate-db`, `log`

**Утилиты:** `call-api`, `check-proxy`, `install`, `uninstall`, `debug-dump`

Полный список алиасов — в `operations/` модулях.

## LLM Agent Workflow (2-stage classifier + reply)

1. **Source**: `get_negotiations()` + `/negotiations/{nid}/messages`
2. **Schedule**: daemon-режим с паузой `sleep_min..max`; quiet hours `23:00-08:00` Europe/Moscow
3. **Filtering**: `resume_id`, `period_days`, blacklist, `only_invitations`, `discard`, non-empty employer tail
4. **Debounce**: `incoming_collect_seconds` для склейки реплик
5. **Bot loop detection**: `_is_bot_loop` — если employer_tail=1 и текст уже был в истории → skip (0 токенов)
6. **Stage 1 — Classifier** (если включён): `human_actionable`/`bot_actionable` → pass; `passive_update`/`marketing_broadcast`/`system_event`/`irrelevant` → skip; `recruiter_contact_offer` → webhook + skip
7. **Stage 2 — Reply LLM**: structured JSON output. Outcomes: `skip`, `reply(dry_run)`, `reply(single)`, `reply(qa_series)` (2-3 сообщения с jitter)

## Development Practices

- **Build**: `poetry build`
- **Lint**: `ruff check . && pylint src/hh_llm_agent/ src/hh_applicant_tool/`
- **Test**: `pytest` (chat-agent: `tests/test_chat_agent_service.py`, `tests/test_chat_agent_operation.py`, `tests/test_openrouter_client.py`)
- **Run**: `python -m hh_applicant_tool <command>` или `hh-applicant-tool <command>`

## Commit Style

- Коммитить только по явной просьбе пользователя.
- Перед коммитом: `git status`, `git diff`, `git log --oneline -5`.
- Запрещено: `--amend` (без явной просьбы или если хуки не изменили файлы), `--no-verify`, `--no-gpg-sign`, force push в main/master.
- Сообщение на русском: заголовок до 72 символов, пустая строка, тело с объяснением *почему* сделано изменение (не что).
- Stage только релевантные файлы. Не коммитить `config/`, `.env`, `__pycache__`, БД, токены, куки, логи.
- После коммита проверить `git status` — чистый working tree.
- PR через `gh`: изучить статус/diff/upstream/history/base-branch, запушить с `-u`, создать через `gh pr create` с HEREDOC.

## Docker

- **Основной контейнер**: `Dockerfile` + `docker-compose.yml` (playwright, cron). `startup.sh` + `crontab`.
- **LLM-агент**: `Dockerfile.llm-agent` + `docker-compose.llm-agent.yml` (лёгкий, без playwright/Chromium). По умолчанию `chat-agent --daemon`. Коллектор на порту 8787.
- Флаг `-v` в compose для INFO-логов.

## Config Notes

**OpenRouter defaults:** `google/gemini-3.1-flash-lite-preview`, reasoning on, max_tokens=1200, temperature=0.2

**Classifier defaults:** `google/gemma-3-27b-it`, enabled, temperature=0, max_tokens=256, max_history=8

**Timing defaults:** debounce 120s, batch_sleep 20-30min, reply_delay 15-60s, qa_series_delay 30-120s, wake_jitter 300s

**Full config schema** — ключи `openrouter.*`, `chat_agent.*`, `chat_agent.classifier.*`, `chat_agent.webhook.*` — см. `config.json` в профиле или `env`-переменные с префиксами `OPENROUTER_*`, `CHAT_AGENT_*`, `TELEGRAM_COLLECTOR_*`.

## Instructions for Agents

1. Перед новой командой проверь `operations/` — возможно, уже существует.
2. Для LLM-агента смотри и `operations/chat_agent.py`, и `hh_llm_agent/`.
3. Изменения БД: синхронизируй `storage/models/`, `storage/repositories/` и `storage/queries/schema.sql`.
4. При изменении audit/state логики агента проверь `chat_messages`, `agent_runs`, `agent_decisions`, `agent_outbox`, `agent_webhooks`.
5. Для тюнинга classifier: правь `chat_agent.classifier.instruction` или `.system_prompt`.
6. Webhook `recruiter_contact_offer`: обнови `service.py::_build_contact_webhook_payload`, `webhook.py` и тесты.
7. Проверка: `pytest` + `python -m hh_applicant_tool <command>`.
