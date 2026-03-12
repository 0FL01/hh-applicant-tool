from __future__ import annotations

import argparse
import logging
import time
from datetime import timedelta
from os import getenv
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from hh_llm_agent.service import RunStats
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)


def _missing_agent_dependency_error(ex: ModuleNotFoundError) -> RuntimeError:
    return RuntimeError(
        "Команда chat-agent недоступна в этом рантайме: отсутствует пакет "
        "`hh_llm_agent`. Для `auth` и других основных команд используйте "
        "обычный CLI-контейнер, а для LLM-агента - `docker compose -f "
        "docker-compose.llm-agent.yml ...`."
    )


def load_agent_config(*args, **kwargs):
    try:
        from hh_llm_agent.config import load_agent_config as _load_agent_config
    except ModuleNotFoundError as ex:
        raise _missing_agent_dependency_error(ex) from ex
    return _load_agent_config(*args, **kwargs)


def ChatAgentService(*args, **kwargs):
    try:
        from hh_llm_agent.service import ChatAgentService as _ChatAgentService
    except ModuleNotFoundError as ex:
        raise _missing_agent_dependency_error(ex) from ex
    return _ChatAgentService(*args, **kwargs)


def TimingPolicy(*args, **kwargs):
    try:
        from hh_llm_agent.timing import TimingPolicy as _TimingPolicy
    except ModuleNotFoundError as ex:
        raise _missing_agent_dependency_error(ex) from ex
    return _TimingPolicy(*args, **kwargs)


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
    classifier_enabled: bool | None
    classifier_model: str | None
    classifier_temperature: float | None
    classifier_max_completion_tokens: int | None
    classifier_system_prompt: str | None
    classifier_instruction: str | None
    classifier_reasoning: bool | None
    classifier_max_history_messages: int | None
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
            "--classifier-enabled",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Включить отдельную модель-классификатор перед генерацией ответа",
        )
        parser.add_argument(
            "--classifier-model",
            help="Модель OpenRouter для классификатора",
        )
        parser.add_argument(
            "--classifier-temperature",
            type=float,
            help="Температура классификатора",
        )
        parser.add_argument(
            "--classifier-max-completion-tokens",
            type=int,
            help="Лимит токенов на ответ классификатора",
        )
        parser.add_argument(
            "--classifier-system-prompt",
            help="Системный промпт для классификатора",
        )
        parser.add_argument(
            "--classifier-instruction",
            help="Инструкция для классификатора",
        )
        parser.add_argument(
            "--classifier-reasoning",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Включить reasoning для классификатора",
        )
        parser.add_argument(
            "--classifier-max-history-messages",
            type=int,
            help="Сколько последних сообщений отдавать классификатору",
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
        stats = self._run_once(tool)
        logger.info(
            "Chat agent finished: total=%s replied=%s skipped=%s errors=%s",
            stats.total,
            stats.replied,
            stats.skipped,
            stats.errors,
        )
        return None

    def _run_once(self, tool: HHApplicantTool) -> RunStats:
        stats = ChatAgentService(tool, tool.args).run()
        print(
            "🤖 Агент завершил работу:",
            f"обработано={stats.total}",
            f"ответов={stats.replied}",
            f"пропусков={stats.skipped}",
            f"ошибок={stats.errors}",
        )
        return stats

    def _quiet_hours_window(self, config) -> str:
        if not config.timing.quiet_hours_enabled:
            return "disabled"
        return (
            f"{config.timing.quiet_hours_start}-{config.timing.quiet_hours_end}"
        )

    def _format_ts(self, value) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S %Z")

    def _run_daemon(self, tool: HHApplicantTool) -> None:
        config = load_agent_config(tool, tool.args)
        policy = TimingPolicy(config.timing)
        now = policy.now()
        logger.info(
            "Starting chat agent daemon: profile=%s model=%s classifier=%s dry_run=%s timezone=%s quiet_hours=%s now=%s sleep_window=%s-%s minutes",
            tool.args.profile_id or ".",
            config.openrouter.model,
            config.classifier.openrouter.model
            if config.classifier and config.classifier.enabled
            else "disabled",
            config.dry_run,
            config.timing.timezone,
            self._quiet_hours_window(config),
            self._format_ts(now),
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
                now = policy.now()
                sleep_seconds = policy.quiet_sleep_seconds()
                wake_at = now + timedelta(seconds=sleep_seconds)
                logger.info(
                    "Quiet hours active: now=%s window=%s sleeping_until=%s sleep_seconds=%.1f",
                    self._format_ts(now),
                    self._quiet_hours_window(config),
                    self._format_ts(wake_at),
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
                continue
            cycle += 1
            logger.info("Chat agent daemon cycle %s started", cycle)
            try:
                stats = self._run_once(tool)
                tool.save_token()
                tool.save_cookies()
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("Chat agent daemon cycle %s failed", cycle)
                stats = None
            if stats is not None:
                logger.info(
                    "Chat agent daemon cycle %s summary: total=%s replied=%s skipped=%s errors=%s",
                    cycle,
                    stats.total,
                    stats.replied,
                    stats.skipped,
                    stats.errors,
                )
            logger.info(
                "Chat agent daemon cycle %s finished, sleeping %.1f seconds",
                cycle,
                sleep_seconds := policy.cycle_sleep_seconds(),
            )
            time.sleep(sleep_seconds)
