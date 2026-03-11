from __future__ import annotations

import argparse
import logging
import time
from os import getenv
from typing import TYPE_CHECKING

from hh_llm_agent.config import load_agent_config
from hh_llm_agent.service import ChatAgentService
from hh_llm_agent.timing import TimingPolicy

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
    sleep_min_minutes: int | None
    sleep_max_minutes: int | None
    timezone: str | None
    quiet_hours: bool | None
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    wake_jitter_seconds: int | None
    incoming_collect_seconds: int | None
    reply_delay_min_seconds: int | None
    reply_delay_max_seconds: int | None
    qa_series_delay_min_seconds: int | None
    qa_series_delay_max_seconds: int | None


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
        parser.add_argument(
            "--sleep-min-minutes",
            type=int,
            help="Минимальная пауза между batch-запусками агента",
        )
        parser.add_argument(
            "--sleep-max-minutes",
            type=int,
            help="Максимальная пауза между batch-запусками агента",
        )
        parser.add_argument(
            "--timezone",
            help="Таймзона для quiet hours и jitter policy",
        )
        parser.add_argument(
            "--quiet-hours",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Включить ночной quiet window для агента",
        )
        parser.add_argument(
            "--quiet-hours-start",
            help="Время начала quiet hours в формате HH:MM",
        )
        parser.add_argument(
            "--quiet-hours-end",
            help="Время конца quiet hours в формате HH:MM",
        )
        parser.add_argument(
            "--wake-jitter-seconds",
            type=int,
            help="Случайный jitter после выхода из quiet hours",
        )
        parser.add_argument(
            "--incoming-collect-seconds",
            type=int,
            help="Сколько ждать, чтобы склеить подряд идущие сообщения работодателя",
        )
        parser.add_argument(
            "--reply-delay-min-seconds",
            type=int,
            help="Минимальная задержка перед первым ответом",
        )
        parser.add_argument(
            "--reply-delay-max-seconds",
            type=int,
            help="Максимальная задержка перед первым ответом",
        )
        parser.add_argument(
            "--qa-series-delay-min-seconds",
            type=int,
            help="Минимальная пауза между сообщениями в Q/A серии",
        )
        parser.add_argument(
            "--qa-series-delay-max-seconds",
            type=int,
            help="Максимальная пауза между сообщениями в Q/A серии",
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
        config = load_agent_config(tool, tool.args)
        policy = TimingPolicy(config.timing)
        logger.info(
            "Starting chat agent daemon with sleep window %s-%s minutes",
            config.timing.sleep_min_minutes,
            config.timing.sleep_max_minutes,
        )
        print(
            "🤖 Chat agent daemon started, "
            f"sleep window: {config.timing.sleep_min_minutes}-{config.timing.sleep_max_minutes}m"
        )
        cycle = 0
        while True:
            if policy.in_quiet_hours():
                sleep_seconds = policy.quiet_sleep_seconds()
                logger.info(
                    "Chat agent daemon is in quiet hours, sleeping %.1f seconds",
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
                continue
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
                "Chat agent daemon cycle %s finished, sleeping %.1f seconds",
                cycle,
                sleep_seconds := policy.cycle_sleep_seconds(),
            )
            time.sleep(sleep_seconds)
