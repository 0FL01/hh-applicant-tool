from __future__ import annotations

import argparse
import html
import logging
import os
import re
import signal
import smtplib
import sqlite3
import sys
import threading
from collections.abc import Sequence
from functools import cached_property
from http.cookiejar import MozillaCookieJar
from importlib import import_module
from itertools import count
from os import getenv
from pathlib import Path
from pkgutil import iter_modules
from typing import Any, Iterable

import requests
import urllib3

from . import api, utils
from .context import HHProfileContext
from .storage import StorageFacade
from .utils.cookiejar import HHOnlyCookieJar
from .utils.find import find_key
from .utils.log import setup_logger

DEFAULT_CONFIG_DIR = utils.get_config_path() / (__package__ or "").replace(
    "_", "-"
)
DEFAULT_CONFIG_FILENAME = "config.json"
DEFAULT_LOG_FILENAME = "log.txt"
DEFAULT_DATABASE_FILENAME = "data"
DEFAULT_COOKIES_FILENAME = "cookies.txt"
DEFAULT_DESKTOP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

logger = logging.getLogger(__package__)
agent_logger = logging.getLogger("hh_llm_agent")


class BaseOperation:
    def setup_parser(self, parser: argparse.ArgumentParser) -> None: ...

    def run(
        self,
        tool: HHApplicantTool,
    ) -> None | int:
        raise NotImplementedError()


OPERATIONS = "operations"


class BaseNamespace(argparse.Namespace):
    profile_id: str
    config_dir: Path
    verbosity: int
    delay: float
    user_agent: str
    proxy_url: str
    openai_proxy_url: str


class HHApplicantTool:
    """Утилита для автоматизации действий соискателя на сайте hh.ru.

    Исходники и предложения: <https://github.com/s3rgeym/hh-applicant-tool>

    Группа поддержки: <https://t.me/hh_applicant_tool>
    """

    class ArgumentFormatter(
        argparse.ArgumentDefaultsHelpFormatter,
        argparse.RawDescriptionHelpFormatter,
    ):
        pass

    def _create_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=self.__doc__,
            formatter_class=self.ArgumentFormatter,
        )
        parser.add_argument(
            "-v",
            "--verbosity",
            help="При использовании от одного и более раз увеличивает количество отладочной информации в выводе",  # noqa: E501
            action="count",
            default=0,
        )
        parser.add_argument(
            "-c",
            "--config-dir",
            "--config",
            help="Путь до директории с конфигом",
            type=Path,
            default=None,
        )
        parser.add_argument(
            "--profile-id",
            "--profile",
            help="Используемый профиль — подкаталог в --config-dir. Так же можно передать через переменную окружения HH_PROFILE_ID.",
        )
        parser.add_argument(
            "-d",
            "--api-delay",
            "--delay",
            type=float,
            help="Задержка между запросами к API HH по умолчанию",
        )
        parser.add_argument(
            "--user-agent",
            help="User-Agent для каждого запроса",
        )
        parser.add_argument(
            "--proxy-url",
            help="Прокси, используемый для запросов и авторизации",
        )
        parser.add_argument(
            "--openai-proxy",
            "--ai-proxy",
            dest="openai_proxy_url",
            help="Отдельный прокси, используемый только для OpenAI чата",
        )
        subparsers = parser.add_subparsers(help="commands")
        package_dir = Path(__file__).resolve().parent / OPERATIONS
        for _, module_name, _ in iter_modules([str(package_dir)]):
            if module_name.startswith("_"):
                continue
            mod = import_module(f"{__package__}.{OPERATIONS}.{module_name}")
            op: BaseOperation = mod.Operation()
            kebab_name = module_name.replace("_", "-")
            op_parser = subparsers.add_parser(
                kebab_name,
                aliases=getattr(op, "__aliases__", []),
                description=op.__doc__,
                formatter_class=self.ArgumentFormatter,
            )
            op_parser.set_defaults(run=op.run)
            op.setup_parser(op_parser)
        parser.set_defaults(run=None)
        return parser

    def __init__(self, argv: Sequence[str] | None):
        self._parse_args(argv)

        # Создаем путь до конфига
        self.config_path.mkdir(
            parents=True,
            exist_ok=True,
        )

    @staticmethod
    def from_profile(
        *,
        config_dir: str | Path | None = None,
        profile_id: str | None = None,
        api_delay: float | None = None,
        user_agent: str | None = None,
        proxy_url: str | None = None,
        openai_proxy_url: str | None = None,
    ) -> HHProfileContext:
        """Create a non-CLI profile context for services and integrations."""
        return HHProfileContext.from_profile(
            config_dir=config_dir,
            profile_id=profile_id,
            api_delay=api_delay,
            user_agent=user_agent,
            proxy_url=proxy_url,
            openai_proxy_url=openai_proxy_url,
        )

    def _get_proxies(self) -> dict[str, str]:
        proxy_url = self.args.proxy_url or self.config.get("proxy_url")

        if proxy_url:
            return {
                "http": proxy_url,
                "https": proxy_url,
            }

        proxies = {}
        http_env = getenv("HTTP_PROXY") or getenv("http_proxy")
        https_env = getenv("HTTPS_PROXY") or getenv("https_proxy") or http_env

        if http_env:
            proxies["http"] = http_env
        if https_env:
            proxies["https"] = https_env

        return proxies

    def _get_openai_proxies(self) -> dict[str, str]:
        """Resolve proxy for AI (OpenAI) requests.

        Precedence:
          1. CLI --openai-proxy / --ai-proxy  (self.args.openai_proxy_url)
          2. config.json → openai.proxy_url
          3. Fall back to general proxies (_get_proxies)
        """
        openai_config = self.config.get("openai", {})
        proxy_url = self.args.openai_proxy_url or openai_config.get("proxy_url")
        if proxy_url:
            return {
                "http": proxy_url,
                "https": proxy_url,
            }
        return self._get_proxies()

    @staticmethod
    def _create_http_session(
        proxies: dict[str, str],
        *,
        log_label: str,
    ) -> requests.Session:
        """Build a requests.Session with proxies, insecure TLS, and desktop UA."""
        session = requests.Session()
        session.verify = False
        if proxies:
            logger.info("Use proxies for %s: %r", log_label, proxies)
            session.proxies = proxies
        session.headers.update({"User-Agent": DEFAULT_DESKTOP_USER_AGENT})
        return session

    @cached_property
    def session(self) -> requests.Session:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        session = self._create_http_session(
            self._get_proxies(),
            log_label="requests",
        )

        session.cookies = HHOnlyCookieJar(str(self.cookies_file))
        if self.cookies_file.exists():
            session.cookies.load(ignore_discard=True, ignore_expires=True)

        return session

    @cached_property
    def openai_session(self) -> requests.Session:
        """Separate HTTP session for OpenAI/AI requests with its own proxy."""
        return self._create_http_session(
            self._get_openai_proxies(),
            log_label="OpenAI requests",
        )

    @cached_property
    def config_path(self) -> Path:
        return (
            (
                self.args.config_dir
                or Path(getenv("CONFIG_DIR", DEFAULT_CONFIG_DIR))
            )
            / (self.args.profile_id or getenv("HH_PROFILE_ID", "."))
        ).resolve()

    @cached_property
    def config(self) -> utils.Config:
        return utils.Config(self.config_path / DEFAULT_CONFIG_FILENAME)

    @cached_property
    def log_file(self) -> Path:
        return self.config_path / DEFAULT_LOG_FILENAME

    @cached_property
    def cookies_file(self) -> Path:
        return self.config_path / DEFAULT_COOKIES_FILENAME

    @cached_property
    def db_path(self) -> Path:
        return self.config_path / DEFAULT_DATABASE_FILENAME

    @cached_property
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return conn

    @cached_property
    def storage(self) -> StorageFacade:
        return StorageFacade(self.db)

    @cached_property
    def api_client(self) -> api.client.ApiClient:
        args = self.args
        config = self.config
        token = config.get("token", {})
        user_agent = args.user_agent or config.get("user_agent")

        if not user_agent:
            user_agent = utils.generate_android_useragent()
            config.save(user_agent=user_agent)
            logger.debug("Generated and saved profile user-agent")

        return api.client.ApiClient(
            client_id=config.get("client_id"),
            client_secret=config.get("client_secret"),
            access_token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            access_expires_at=token.get("access_expires_at"),
            delay=args.api_delay or config.get("api_delay"),
            user_agent=user_agent,
            session=self.session,
        )

    def get_me(self) -> api.datatypes.User:
        return self.api_client.get("/me")

    def get_resumes(self) -> list[api.datatypes.Resume]:
        return self.api_client.get("/resumes/mine").get("items", [])

    def first_resume_id(self) -> str:
        resume = self.get_resumes()[0]
        return resume["id"]

    def get_blacklisted(self) -> list[str]:
        rv = []
        for page in count():
            r: api.datatypes.PaginatedItems[api.datatypes.EmployerShort] = (
                self.api_client.get("/employers/blacklisted", page=page)
            )
            rv += [item["id"] for item in r["items"]]
            if page + 1 >= r["pages"]:
                break
        return rv

    def get_negotiations(
        self, status: str = "active"
    ) -> Iterable[api.datatypes.Negotiation]:
        for page in count():
            r: dict[str, Any] = self.api_client.get(
                "/negotiations",
                page=page,
                per_page=100,
                status=status,
            )

            items = r.get("items", [])

            if not items:
                break

            yield from items

            if page + 1 >= r.get("pages", 0):
                break

    # TODO: добавить еще методов или те удалить?

    def save_token(self) -> bool:
        if self.api_client.access_token != self.config.get("token", {}).get(
            "access_token"
        ):
            self.config.save(token=self.api_client.get_access_token())
            return True
        return False

    def save_cookies(self) -> None:
        """Сохраняет текущие куки сессии в файл."""
        if isinstance(self.session.cookies, MozillaCookieJar):
            self.session.cookies.save(ignore_discard=True, ignore_expires=True)
            logger.debug("Cookies saved to %s", self.cookies_file)
        else:
            logger.warning(
                f"Сессионные куки имеют неправильный тип: {type(self.session.cookies)}"
            )

    def get_openai_chat(self, system_prompt: str) -> Any:
        """Get an AI chat client configured for the old path (cover letters, replies).

        Returns OpenRouterChatClient for unified AI interface.
        The system_prompt argument is passed through to send_message() call sites.
        """
        from hh_llm_agent.config import (
            OpenRouterConfig,
            load_openai_env,
            parse_reasoning_effort,
        )
        from hh_llm_agent.openrouter import OpenRouterChatClient

        c = self.config.get("openai", {})
        openai_env = load_openai_env()
        # Fallback chain: openai.token → universal api_key → env
        token = c.get("token") or self.config.get("api_key") or openai_env.api_key
        if not token:
            raise ValueError(
                "Токен для OpenAI не задан. Укажите api_key в config.json "
                "или установите OPENAI_API_KEY"
            )
        # Fallback chain: openai.completion_endpoint → universal openai_base_url → env
        base_url = (
            c.get("completion_endpoint")
            or self.config.get("openai_base_url")
            or openai_env.base_url
        )
        reasoning_effort = (
            parse_reasoning_effort(
                c.get("reasoning_effort"),
                "openai.reasoning_effort",
            )
            if c.get("reasoning_effort") is not None
            else openai_env.reasoning_effort
        )

        config = OpenRouterConfig(
            api_key=token,
            base_url=base_url,
            model=c.get("model") or openai_env.model,
            temperature=c.get("temperature", 0.7),
            max_completion_tokens=c.get("max_completion_tokens", 1000),
            reasoning_enabled=bool(c.get("reasoning_enabled", False)),
            reasoning_effort=reasoning_effort,
        )
        if hasattr(self, "openai_session") and self.openai_session.proxies:
            prox = self.openai_session.proxies
            if prox.get("http") or prox.get("https"):
                config.proxies = dict(prox)

        return OpenRouterChatClient(config)

    # TODO: вынести в миксин какой
    def _cookie_value(self, name: str) -> str | None:
        for cookie in self.session.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def _extract_xsrf_token(self, content: str) -> str:
        # hh.ru отдает этот блок с HTML-заэкранированными кавычками
        # (внутри HTML-атрибута), поэтому сначала разэкранируем всю страницу
        content = html.unescape(content)
        tokens = re.findall(r',"xsrfToken":"([^"]+)"', content)
        if not tokens:
            raise ValueError("xsrf token not found")

        # На странице hh.ru может быть несколько xsrfToken. Первый из них —
        # случайное значение, которое ротируется при каждой загрузке и НЕ
        # соответствует cookie `_xsrf`, из-за чего POST на
        # /applicant/vacancy_response/popup возвращал 403 (CSRF mismatch).
        # Сервер сверяет токен именно с cookie `_xsrf`, поэтому отдаем
        # совпадающее значение, а не первое вхождение.
        cookie_xsrf = self._cookie_value("_xsrf")
        if cookie_xsrf and cookie_xsrf in tokens:
            return cookie_xsrf
        return tokens[0]

    def _get_xsrf_token(self, url: str | None = None) -> str:
        """Возвращает XSRF-токен, который выдается на сессию"""
        r = self.session.get(url or "https://hh.ru/")
        return self._extract_xsrf_token(r.text)

    @cached_property
    def xsrf_token(self) -> str:
        return self._get_xsrf_token()

    @staticmethod
    def _is_authenticated(config: dict[str, Any]) -> bool:
        account = config.get("account") or {}
        if not account:
            return False
        # Если пользователь неавторизован, содержит поля типа firstName,
        # lastName и т.д. со значением None (все поля)
        return any(v is not None for v in account.values())

    def parse_redirect_config(
        self,
        response: requests.Response,
        check_auth: bool = True,
    ) -> dict[str, Any]:
        """Разбирает HH-Lux-InitialState со страницы отклика hh.ru."""
        if response.status_code != 200:
            raise api.BadResponse(
                f"Неожиданный код ответа: {response.status_code} {response.url}"
            )

        try:
            raw_config = response.text.split('id="HH-Lux-InitialState">')[1]
            raw_config = raw_config.split("</template>")[0]
        except IndexError as ex:
            raise api.BadResponse(
                f"Template with config not found on {response.url}"
            ) from ex

        # hh.ru экранирует кавычки сущностями внутри атрибута
        if raw_config.startswith('{&#34;'):
            raw_config = html.unescape(raw_config)

        config = utils.json.loads(raw_config)
        assert type(config) is dict
        assert "redirectConfig" in config
        if check_auth and not self._is_authenticated(config):
            raise api.BadResponse("Авторизация истекла, требуется новая!")
        return config

    def get_redirect_config(
        self, url: str, check_auth: bool = True
    ) -> dict[str, Any]:
        return self.parse_redirect_config(
            self.session.get(url), check_auth
        )

    @property
    def is_logged_in(self) -> bool:
        """Проверяет авторизован ли пользователь через сайт."""
        return self.session.get("https://hh.ru/settings").status_code == 200

    @cached_property
    def smtp(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        conf = self.config.get("smtp", {})
        host = conf.get("host")
        port = conf.get("port")
        user = conf.get("user")
        password = conf.get("password")
        use_ssl = conf.get("ssl", False)

        if not host or not port:
            raise ValueError("SMTP host or port not configured")

        client_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        server = client_cls(host, port)

        if not use_ssl and conf.get("starttls", True):
            server.starttls()

        if user and password:
            server.login(user, password)

        return server

    def run(self) -> None | int:
        verbosity_level = max(
            logging.DEBUG,
            logging.WARNING - self.args.verbosity * 10,
        )
        if getattr(self.args, "daemon", False):
            verbosity_level = min(verbosity_level, logging.INFO)

        setup_logger(logger, verbosity_level, self.log_file)
        setup_logger(agent_logger, verbosity_level, self.log_file)

        logger.debug("Путь до профиля: %s", self.config_path)

        utils.setup_terminal()

        if self.args.run:
            # Мягкое прерывание по Ctrl+C (SIGINT). Первый ^C просит
            # операцию остановиться после текущего шага (через
            # _cancel_event), второй - немедленно прерывает (upstream
            # 9f0e0d0). Это спасает массовую рассылку откликов от
            # мгновенного обрыва посреди итерации.
            cancel_event = threading.Event()
            operation = getattr(self.args.run, "__self__", None)
            if operation is not None:
                operation._cancel_event = cancel_event

            def _handle_sigint(signum, frame):  # noqa: ARG001
                if cancel_event.is_set():
                    logger.warning(
                        "Повторное прерывание - принудительный выход"
                    )
                    raise KeyboardInterrupt
                logger.warning(
                    "Получен SIGINT: останавливаюсь после текущего шага "
                    "(еще один Ctrl+C для немедленного выхода)"
                )
                cancel_event.set()

            previous_handler = signal.signal(signal.SIGINT, _handle_sigint)
            try:
                try:
                    return self.args.run(self)
                except KeyboardInterrupt:
                    logger.warning("Выполнение прервано пользователем!")
                finally:
                    signal.signal(signal.SIGINT, previous_handler)
            except api.errors.CaptchaRequired as ex:
                logger.error(f"Требуется ввод капчи: {ex.captcha_url}")
            except api.errors.InternalServerError:
                logger.error(
                    "Сервер HH.RU не смог обработать запрос из-за высокой"
                    " нагрузки или по иной причине"
                )
            except api.errors.Forbidden:
                logger.error("Требуется авторизация")
            except sqlite3.Error as ex:
                logger.exception(ex)

                script_name = sys.argv[0].split(os.sep)[-1]

                logger.warning(
                    f"Возможно база данных повреждена, попробуйте выполнить команду:\n\n"  # noqa: E501
                    f"  {script_name} migrate-db"
                )
            except Exception as e:
                logger.exception(e)
            finally:
                # Токен мог автоматически обновиться
                if self.save_token():
                    logger.info("Токен был сохранен после обновления.")

                try:
                    self.save_cookies()
                except Exception as ex:
                    logger.error(f"Не удалось сохранить cookies: {ex}")
            return 1
        self._parser.print_help(file=sys.stderr)
        return 2

    def _parse_args(self, argv) -> None:
        self._parser = self._create_parser()
        self.args = self._parser.parse_args(argv, namespace=BaseNamespace())


def main(argv: Sequence[str] | None = None) -> None | int:
    return HHApplicantTool(argv).run()
