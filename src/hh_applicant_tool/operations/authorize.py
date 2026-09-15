from __future__ import annotations

import argparse
import asyncio
import logging
import re
import typing
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from http.cookiejar import Cookie
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:
    PlaywrightError = Exception
    PlaywrightTimeoutError = Exception
    async_playwright = None

from ..main import BaseOperation
from ..utils.terminal import print_kitty_image, print_sixel_mage

if TYPE_CHECKING:
    from ..main import HHApplicantTool


HH_ANDROID_SCHEME = "hhandroid"
DEFAULT_ANDROID_DEVICE = "Galaxy A55"
FALLBACK_ANDROID_DEVICES: tuple[str, ...] = (
    DEFAULT_ANDROID_DEVICE,
    "Pixel 7",
    "Pixel 5",
    "Pixel 4a (5G)",
    "Galaxy S9+",
    "Galaxy S8",
    "Nexus 5X",
)

logger = logging.getLogger(__name__)


class Operation(BaseOperation):
    """Авторизация через Playwright"""

    __aliases__: list = ["authenticate", "auth", "login"]

    # Селекторы. Старые data-qa оставлены как fallback: hh.ru в 2026
    # перешёл на magritte-форму (телефон/почта раздельно).
    SEL_LOGIN_FORM = '[data-qa="account-login-form"]'
    SEL_LOGIN_INPUT = 'input[data-qa="login-input-username"]'
    SEL_PHONE_INPUT = (
        'input[data-qa="magritte-phone-input-national-number-input"]'
    )
    SEL_EMAIL_TAB = '[data-qa="credential-type-email"]'
    SEL_EMAIL_INPUT = 'input[data-qa="applicant-login-input-email"]'
    SEL_EXPAND_PASSWORD = (
        '[data-qa="expand-login-by-password"], '
        'button[data-qa="account-login-submit-by-password"]'
    )
    SEL_PASSWORD_INPUT = (
        'input[data-qa="login-input-password"], input[type="password"]'
    )
    SEL_CODE_CONTAINER = 'div[data-qa="account-login-code-input"]'
    SEL_PIN_CODE_INPUT = 'input[data-qa="magritte-pincode-input-field"]'
    SEL_CAPTCHA_IMAGE = 'img[data-qa="account-captcha-picture"]'
    SEL_CAPTCHA_INPUT = 'input[data-qa="account-captcha-input"]'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tool: HHApplicantTool | None = None

    @property
    def is_headless(self) -> bool:
        return not self._tool.args.no_headless and self.is_automated

    @property
    def is_automated(self) -> bool:
        return not self._tool.args.manual

    @property
    def selector_timeout(self) -> int | None:
        if not self.is_automated:
            return 5000
        # Headless-автомат на медленной VDS: щедрый таймаут вместо
        # playwright-дефолта (None -> 30s), иначе тяжёлые формы hh.ru
        # не успевают отрендериться в software-рендеринге.
        return 120000

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("username", nargs="?", help="Email или телефон")
        parser.add_argument("--password", "-p", help="Пароль для входа")
        parser.add_argument(
            "--no-headless",
            "-n",
            action="store_true",
            help="Показать окно браузера",
        )
        parser.add_argument(
            "-m", "--manual", action="store_true", help="Ручной режим ввода"
        )
        parser.add_argument(
            "-k",
            "--use-kitty",
            "--kitty",
            action="store_true",
            help="Вывод капчи в kitty",
        )
        parser.add_argument(
            "-s",
            "--use-sixel",
            "--sixel",
            action="store_true",
            help="Вывод капчи в sixel",
        )

    def run(self, tool: HHApplicantTool) -> int | None:
        self._tool = tool
        try:
            asyncio.run(self._run())
        except (KeyboardInterrupt, asyncio.TimeoutError):
            logger.warning("Операция прервана пользователем или по таймауту")
            return 1
        return 0

    async def _run(self) -> None:
        if async_playwright is None:
            raise RuntimeError(
                "Не удалось импортировать Playwright. Установите extra "
                "`playwright` и Chromium (`pip install "
                "'hh-applicant-tool[playwright]'` и "
                "`hh-applicant-tool install`)."
            )

        args = self._tool.args
        api_client = self._tool.api_client
        storage = self._tool.storage

        if self.is_automated:
            username = (
                args.username
                or storage.settings.get_value("auth.username")
                or (
                    await asyncio.to_thread(
                        input, "👤 Введите email или телефон: "
                    )
                )
            ).strip()
            if not username:
                raise RuntimeError("Empty username")
            logger.debug(f"authenticate with: {username}")

        proxies = api_client.proxies
        proxy_url = proxies.get("https")
        chromium_args: list[str] = []
        if proxy_url:
            chromium_args.append(f"--proxy-server={proxy_url}")
            logger.debug(f"Используется прокси: {proxy_url}")

        if self.is_headless:
            logger.debug("Headless режим активен")

        async with async_playwright() as pw:
            logger.debug("Запуск браузера...")
            browser = await pw.chromium.launch(
                headless=self.is_headless, args=chromium_args
            )

            try:
                device_name, android_device = self._resolve_android_device(
                    pw.devices
                )
                if device_name != DEFAULT_ANDROID_DEVICE:
                    logger.debug(
                        "Playwright device '%s' not found, using '%s'",
                        DEFAULT_ANDROID_DEVICE,
                        device_name,
                    )
                context = await browser.new_context(**android_device)
                page = await context.new_page()

                code_future: asyncio.Future[str | None] = asyncio.Future()

                def handle_request(request):
                    url = request.url
                    if url.startswith(f"{HH_ANDROID_SCHEME}://"):
                        logger.info(f"Перехвачен OAuth redirect: {url}")
                        if not code_future.done():
                            sp = urlsplit(url)
                            code = parse_qs(sp.query).get("code", [None])[0]
                            code_future.set_result(code)

                page.on("request", handle_request)

                logger.debug(
                    f"Переход на страницу OAuth: {api_client.oauth_client.authorize_url}"
                )
                await page.goto(
                    api_client.oauth_client.authorize_url,
                    timeout=60000,
                    # OAuth-странице hh.ru не нужно ждать полной загрузки
                    # ресурсов (трекеры/реклама могут никогда не отдать load
                    # в headless): нам достаточно DOM с формой логина,
                    # её готовность проверяет wait_for_selector ниже.
                    wait_until="domcontentloaded",
                )

                if self.is_automated:
                    await self._fill_username(page, username)
                    logger.debug("Логин введен")

                    password = args.password or storage.settings.get_value(
                        "auth.password"
                    )
                    if password:
                        await self._direct_login(page, password)
                    else:
                        await self._onetime_code_login(page)

                logger.debug("Ожидание OAuth-кода...")
                auth_code = await asyncio.wait_for(
                    code_future, timeout=[None, 60.0][self.is_automated]
                )

                page.remove_listener("request", handle_request)

                logger.debug("Код получен, пробуем получить токен...")
                token = await asyncio.to_thread(
                    api_client.oauth_client.authenticate, auth_code
                )
                api_client.handle_access_token(token)

                print("🔓 Авторизация прошла успешно!")

                if self.is_automated:
                    storage.settings.set_value("auth.username", username)
                    if args.password:
                        storage.settings.set_value(
                            "auth.password", args.password
                        )

                storage.settings.set_value("auth.last_login", datetime.now())
                cookies = await context.cookies()
                self._set_session_cookies(cookies)

            finally:
                logger.debug("Закрытие браузера")
                with suppress(Exception):
                    await browser.close()

    @staticmethod
    def _national_phone(username: str) -> str:
        digits = re.sub(r"\D", "", username)
        if len(digits) == 11 and digits[0] in "78":
            return digits[1:]
        return digits

    async def _fill_username(self, page, username: str) -> None:
        """Заполняет логин в старой или новой (magritte) форме hh.ru."""
        await page.wait_for_selector(
            ", ".join(
                (
                    self.SEL_LOGIN_FORM,
                    self.SEL_LOGIN_INPUT,
                    self.SEL_PHONE_INPUT,
                    self.SEL_EMAIL_INPUT,
                )
            ),
            timeout=self.selector_timeout,
        )

        if "@" in username:
            email_tab = page.locator(self.SEL_EMAIL_TAB)
            if await email_tab.count():
                await email_tab.first.click(force=True)
            email_input = page.locator(self.SEL_EMAIL_INPUT)
            if await email_input.count():
                await email_input.first.fill(username)
                return

        phone_input = page.locator(self.SEL_PHONE_INPUT)
        if await phone_input.count() and "@" not in username:
            await phone_input.first.fill(self._national_phone(username))
            return

        await page.fill(self.SEL_LOGIN_INPUT, username)

    async def _direct_login(self, page, password: str) -> None:
        logger.info("Вход по паролю...")
        await page.locator(self.SEL_EXPAND_PASSWORD).first.click(force=True)
        await self._handle_captcha(page)
        await page.wait_for_selector(
            self.SEL_PASSWORD_INPUT, timeout=self.selector_timeout
        )
        password_input = page.locator(self.SEL_PASSWORD_INPUT).first
        await password_input.fill(password)
        await password_input.press("Enter")
        logger.debug("Форма с паролем отправлена")

    async def _onetime_code_login(self, page) -> None:
        logger.info("Вход по одноразовому коду...")
        login_field = page.locator(
            f"{self.SEL_EMAIL_INPUT}, {self.SEL_PHONE_INPUT}, "
            f"{self.SEL_LOGIN_INPUT}"
        ).first
        await login_field.press("Enter")
        # Капчу могут попросить повторно, если текст введён неверно:
        # тогда вместо формы кода снова появляется картинка капчи.
        # Крутим попытки в рамках одного запуска, чтобы не слать новый SMS.
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            # Первая проверка короткая: если капчи не будет вообще,
            # не маринуем пользователя полный таймаут.
            await self._handle_captcha(
                page, timeout=20000 if attempt == 1 else None
            )
            try:
                await page.wait_for_selector(
                    self.SEL_CODE_CONTAINER,
                    timeout=self.selector_timeout,
                    state="visible",
                )
                break
            except PlaywrightTimeoutError:
                if attempt >= max_attempts:
                    raise
                print(
                    "[...] Форма кода не появилась - возможно, текст капчи "
                    f"введён неверно. Пробуем ещё раз "
                    f"({attempt + 1}/{max_attempts})."
                )

        print("📨 Код был отправлен. Проверьте почту или SMS.")
        code = (
            await asyncio.to_thread(input, "📩 Введите полученный код: ")
        ).strip()
        if not code:
            raise RuntimeError("Код подтверждения не может быть пустым.")

        await page.fill(self.SEL_PIN_CODE_INPUT, code)
        await page.press(self.SEL_PIN_CODE_INPUT, "Enter")
        logger.debug("Форма с кодом отправлена")

    async def _handle_captcha(
        self, page, timeout: int | None = None
    ) -> bool:
        """Разбирает капчу, если она показана.

        Возвращает True, если капча была и её отправили, иначе False.
        """
        try:
            captcha_element = await page.wait_for_selector(
                self.SEL_CAPTCHA_IMAGE,
                timeout=self.selector_timeout if timeout is None else timeout,
                state="visible",
            )
        except PlaywrightTimeoutError:
            # Картинка капчи не появилась - значит, капчи нет:
            # продолжаем обычную авторизацию (например, ввод SMS-кода).
            logger.debug("Капчи нет, продолжаем.")
            return False
        except PlaywrightError as ex:
            if "has been closed" in str(ex):
                logger.debug("Браузер был закрыт до завершения ожидания капчи")
                return False
            raise
        except Exception:
            logger.debug("Капчи нет, продолжаем.")
            return False

        args = self._tool.args
        if not (args.use_kitty or args.use_sixel):
            raise RuntimeError(
                "Требуется ввод капчи! Используйте --kitty или --sixel."
            )

        img_bytes = await captcha_element.screenshot()
        try:
            captcha_path = Path(self._tool.config_path) / "captcha.png"
            captcha_path.write_bytes(img_bytes)
            print(f"[...] Капча также сохранена в файл: {captcha_path}")
        except Exception as ex:
            logger.debug("Не удалось сохранить капчу в файл: %s", ex)
        print("\n[!] Требуется ввод капчи.")
        if args.use_kitty:
            print_kitty_image(img_bytes)
        elif args.use_sixel:
            print_sixel_mage(img_bytes)

        captcha_text = (
            await asyncio.to_thread(input, "Введите текст с картинки: ")
        ).strip()
        await page.fill(self.SEL_CAPTCHA_INPUT, captcha_text)
        await page.press(self.SEL_CAPTCHA_INPUT, "Enter")
        logger.debug("Капча отправлена")
        return True

    @staticmethod
    def _resolve_android_device(
        devices: Mapping[str, Mapping[str, typing.Any]],
    ) -> tuple[str, dict[str, typing.Any]]:
        for device_name in FALLBACK_ANDROID_DEVICES:
            device = devices.get(device_name)
            if device is not None:
                return device_name, dict(device)

        for device_name, device in devices.items():
            user_agent = str(device.get("user_agent") or "")
            if "Android" in user_agent and "Mobile" in user_agent:
                return device_name, dict(device)

        raise RuntimeError(
            "Playwright does not provide any Android mobile device presets."
        )

    def _set_session_cookies(self, cookies: list[dict[str, typing.Any]]):
        for c in cookies:
            cookie = Cookie(
                version=0,
                name=c["name"],
                value=c["value"],
                port=None,
                port_specified=False,
                domain=c["domain"],
                domain_specified=True,
                domain_initial_dot=c["domain"].startswith("."),
                path=c["path"],
                path_specified=True,
                secure=c["secure"],
                expires=int(c.get("expires") or 0),
                discard=False,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": str(c.get("httpOnly", False))},
                rfc2109=False,
            )
            self._tool.session.cookies.set_cookie(cookie)
