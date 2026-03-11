from __future__ import annotations

import argparse
import logging
import time
from os import getenv
from typing import TYPE_CHECKING

from hh_llm_agent.service import ChatAgentService

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    resume_id: str | None
    period: int | None
    only_invitations: bool | None
    dry_run: bool
    limit: int | None
    max_history_messages: int | None
    model: str | None
    temperature: float | None
    max_completion_tokens: int | None
    system_prompt: str | None
    reply_instruction: str | None
    reasoning: bool | None
    skip_blacklisted: bool | None
    openrouter_api_key: str | None
    openrouter_base_url: str | None
    force: bool
    daemon: bool
    poll_interval: int


class Operation(BaseOperation):
    """LLM-агент для автоответов в чатах работодателей."""

    __aliases__ = ["ai-agent", "reply-agent"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--resume-id", help="Фильтр по резюме")
        parser.add_argument(
            "--period",
            type=int,
            help="Игнорировать чаты, не обновлявшиеся больше N дней",
        )
        parser.add_argument(
            "--only-invitations",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Обрабатывать только приглашения",
        )
        parser.add_argument(
            "--dry-run",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="Не отправлять ответы в HH, только показывать их",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Максимум чатов за один запуск",
        )
        parser.add_argument(
            "--max-history-messages",
            type=int,
            help="Сколько последних сообщений отдавать модели",
        )
        parser.add_argument("--model", help="Модель OpenRouter")
        parser.add_argument(
            "--temperature",
            type=float,
            help="Температура генерации",
        )
        parser.add_argument(
            "--max-completion-tokens",
            type=int,
            help="Лимит токенов на ответ модели",
        )
        parser.add_argument(
            "--system-prompt",
            help="Системный промпт для агента",
        )
        parser.add_argument(
            "--reply-instruction",
            help="Инструкция для генерации ответа",
        )
        parser.add_argument(
            "--reasoning",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Включить reasoning в OpenRouter",
        )
        parser.add_argument(
            "--skip-blacklisted",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Пропускать работодателей из черного списка",
        )
        parser.add_argument(
            "--openrouter-api-key",
            help="API key OpenRouter",
        )
        parser.add_argument(
            "--openrouter-base-url",
            help="Base URL OpenRouter-compatible API",
        )
        parser.add_argument(
            "--force",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="Повторно обработать чат даже если последнее сообщение уже разбиралось",
        )
        parser.add_argument(
            "--daemon",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="Запускать агент в бесконечном цикле, пока процесс не будет остановлен",
        )
        parser.add_argument(
            "--poll-interval",
            type=int,
            default=int(getenv("CHAT_AGENT_POLL_INTERVAL", "60")),
            help="Пауза между циклами опроса чатов в секундах",
        )

    def run(self, tool: HHApplicantTool) -> None:
        if tool.args.daemon:
            return self._run_daemon(tool)
        return self._run_once(tool)

    def _run_once(self, tool: HHApplicantTool) -> None:
        stats = ChatAgentService(tool, tool.args).run()
        logger.info(
            "Chat agent finished: total=%s replied=%s skipped=%s errors=%s",
            stats.total,
            stats.replied,
            stats.skipped,
            stats.errors,
        )
        print(
            "🤖 Агент завершил работу:",
            f"обработано={stats.total}",
            f"ответов={stats.replied}",
            f"пропусков={stats.skipped}",
            f"ошибок={stats.errors}",
        )

    def _run_daemon(self, tool: HHApplicantTool) -> None:
        interval = max(1, tool.args.poll_interval)
        logger.info(
            "Starting chat agent daemon with poll interval %s seconds",
            interval,
        )
        print(f"🤖 Chat agent daemon started, poll interval: {interval}s")
        cycle = 0
        while True:
            cycle += 1
            logger.info("Chat agent daemon cycle %s started", cycle)
            try:
                self._run_once(tool)
                tool.save_token()
                tool.save_cookies()
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("Chat agent daemon cycle %s failed", cycle)
            logger.info(
                "Chat agent daemon cycle %s finished, sleeping %s seconds",
                cycle,
                interval,
            )
            time.sleep(interval)
