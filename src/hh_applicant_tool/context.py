from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from http.cookiejar import MozillaCookieJar
from itertools import count
from os import getenv
from pathlib import Path
from typing import Any

import requests
import urllib3

from . import api, utils
from .storage import StorageFacade
from .utils.cookiejar import HHOnlyCookieJar

DEFAULT_CONFIG_DIR = utils.get_config_path() / (__package__ or "").replace(
    "_", "-"
)
DEFAULT_CONFIG_FILENAME = "config.json"
DEFAULT_LOG_FILENAME = "log.txt"
DEFAULT_DATABASE_FILENAME = "data"
DEFAULT_COOKIES_FILENAME = "cookies.txt"
DEFAULT_DESKTOP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

logger = logging.getLogger(__package__)


@dataclass
class HHProfileContext:
    """Programmatic runtime context for one hh-applicant-tool profile."""

    config_dir: Path | None = None
    profile_id: str | None = None
    api_delay: float | None = None
    user_agent: str | None = None
    proxy_url: str | None = None
    openai_proxy_url: str | None = None

    @classmethod
    def from_profile(
        cls,
        *,
        config_dir: str | Path | None = None,
        profile_id: str | None = None,
        api_delay: float | None = None,
        user_agent: str | None = None,
        proxy_url: str | None = None,
        openai_proxy_url: str | None = None,
    ) -> HHProfileContext:
        return cls(
            config_dir=Path(config_dir) if config_dir is not None else None,
            profile_id=profile_id,
            api_delay=api_delay,
            user_agent=user_agent,
            proxy_url=proxy_url,
            openai_proxy_url=openai_proxy_url,
        )

    def __post_init__(self) -> None:
        self.config_path.mkdir(parents=True, exist_ok=True)

    @cached_property
    def config_path(self) -> Path:
        config_dir = self.config_dir or Path(
            getenv("CONFIG_DIR", DEFAULT_CONFIG_DIR)
        )
        profile_id = self.profile_id or getenv("HH_PROFILE_ID", ".")
        return (config_dir / profile_id).resolve()

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

    def _get_proxies(self) -> dict[str, str]:
        proxy_url = self.proxy_url or self.config.get("proxy_url")
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
        openai_config = self.config.get("openai", {})
        proxy_url = self.openai_proxy_url or openai_config.get("proxy_url")
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
        return self._create_http_session(
            self._get_openai_proxies(),
            log_label="OpenAI requests",
        )

    @cached_property
    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    @cached_property
    def storage(self) -> StorageFacade:
        return StorageFacade(self.db)

    @cached_property
    def api_client(self) -> api.client.ApiClient:
        config = self.config
        token = config.get("token", {})
        user_agent = self.user_agent or config.get("user_agent")

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
            delay=self.api_delay or config.get("api_delay"),
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

    def save_token(self) -> bool:
        if self.api_client.access_token != self.config.get("token", {}).get(
            "access_token"
        ):
            self.config.save(token=self.api_client.get_access_token())
            return True
        return False

    def save_cookies(self) -> None:
        if isinstance(self.session.cookies, MozillaCookieJar):
            self.session.cookies.save(ignore_discard=True, ignore_expires=True)
            logger.debug("Cookies saved to %s", self.cookies_file)
        else:
            logger.warning(
                "Сессионные куки имеют неправильный тип: %s",
                type(self.session.cookies),
            )
