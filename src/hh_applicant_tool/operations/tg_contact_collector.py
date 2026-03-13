from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)


def _missing_agent_dependency_error(ex: ModuleNotFoundError) -> RuntimeError:
    return RuntimeError(
        "Команда tg-contact-collector недоступна в этом рантайме: отсутствует "
        "пакет `hh_llm_agent`. Используйте обычный CLI-рантайм с установленным "
        "пакетом проекта или контейнер `docker compose -f docker-compose.llm-agent.yml ...`."
    )


def load_telegram_collector_config(*args, **kwargs):
    try:
        from hh_llm_agent.config import (
            load_telegram_collector_config as _load_telegram_collector_config,
        )
    except ModuleNotFoundError as ex:
        raise _missing_agent_dependency_error(ex) from ex
    return _load_telegram_collector_config(*args, **kwargs)


def TelegramCollectorHTTPServer(*args, **kwargs):
    try:
        from hh_llm_agent.tg_contact_collector import (
            TelegramCollectorHTTPServer as _TelegramCollectorHTTPServer,
        )
    except ModuleNotFoundError as ex:
        raise _missing_agent_dependency_error(ex) from ex
    return _TelegramCollectorHTTPServer(*args, **kwargs)


def TelegramContactCollectorService(*args, **kwargs):
    try:
        from hh_llm_agent.tg_contact_collector import (
            TelegramContactCollectorService as _TelegramContactCollectorService,
        )
    except ModuleNotFoundError as ex:
        raise _missing_agent_dependency_error(ex) from ex
    return _TelegramContactCollectorService(*args, **kwargs)


class Namespace(BaseNamespace):
    host: str | None
    port: int | None
    bot_token: str | None
    target_chat_id: int | None
    webhook_secret: str | None
    webhook_secret_header: str | None
    db_path: str | None
    timeout_seconds: float | None
    bot_api_base_url: str | None
    verify_ssl: bool | None


class Operation(BaseOperation):
    """Легкий HTTP-приемник recruiter_contact_offer и форвардер в Telegram."""

    __aliases__ = ["tg-collector"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--host", help="Адрес для bind, например 0.0.0.0")
        parser.add_argument("--port", type=int, help="Порт HTTP-сервера")
        parser.add_argument("--bot-token", help="Токен Telegram Bot API")
        parser.add_argument(
            "--target-chat-id",
            type=int,
            help="Куда пересылать лиды в Telegram",
        )
        parser.add_argument(
            "--webhook-secret",
            help="Секрет, который должен прийти в webhook заголовке",
        )
        parser.add_argument(
            "--webhook-secret-header",
            help="Имя заголовка с webhook-секретом",
        )
        parser.add_argument(
            "--db-path",
            help="Путь до SQLite-файла для накопления контактов",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            help="Timeout запросов к Telegram Bot API",
        )
        parser.add_argument(
            "--bot-api-base-url",
            help="Базовый URL Telegram Bot API",
        )
        parser.add_argument(
            "--verify-ssl",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Проверять SSL у Telegram Bot API",
        )

    def run(self, tool: HHApplicantTool) -> None:
        config = load_telegram_collector_config(tool, tool.args)
        service = TelegramContactCollectorService(config)
        server = TelegramCollectorHTTPServer(
            (config.listen_host, config.listen_port),
            service,
        )
        logger.info(
            "Telegram collector listening on %s:%s db=%s",
            config.listen_host,
            config.listen_port,
            config.db_path,
        )
        logger.info(
            "Telegram collector webhook path: /webhooks/hh/recruiter-contact-offer"
        )
        try:
            server.serve_forever()
        finally:
            server.server_close()
