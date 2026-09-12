"""Тесты выбора XSRF-токена и разбора HH-Lux-InitialState.

Причина бага: на странице hh.ru бывает несколько `xsrfToken`. Первое
вхождение — случайное значение, оно ротируется при каждой загрузке и не
соответствует cookie `_xsrf`. Из-за этого POST на
`/applicant/vacancy_response/popup` возвращал 403 (CSRF mismatch).

Сервер валидирует именно cookie `_xsrf`, поэтому инструмент должен слать
значение из этой cookie, а не первое вхождение из HTML.
"""

from __future__ import annotations

from http.cookiejar import Cookie, CookieJar

import pytest

from hh_applicant_tool.main import HHApplicantTool


def cookie_jar(data: dict[str, str]) -> CookieJar:
    """Настоящий CookieJar из данных {name: value} (домен .hh.ru)."""
    jar = CookieJar()
    for name, value in data.items():
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=".hh.ru",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=False,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return jar


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = ""):
        self.text = text
        self.status_code = status_code
        self.url = url or "https://hh.ru/fake"


class FakeSession:
    def __init__(
        self,
        cookies: dict[str, str] | None = None,
    ):
        self.cookies = cookie_jar(cookies or {})


def make_tool(cookies: dict[str, str] | None = None) -> HHApplicantTool:
    # Создаем экземпляр, не запуская __init__ (тот строит argparse и читает диск).
    tool = HHApplicantTool.__new__(HHApplicantTool)
    tool.session = FakeSession(cookies)
    return tool


def page_html(*tokens: str, escaped: bool = False) -> str:
    """Собирает HTML с несколькими полями `,"xsrfToken":"..."`."""
    q = "&quot;" if escaped else '"'
    return "".join(f',{q}xsrfToken{q}:{q}{t}{q}' for t in tokens)


def test_first_token_used_when_no_xsrf_cookie():
    tool = make_tool()

    assert (
        tool._extract_xsrf_token(page_html("rotating-junk", "real-token"))
        == "rotating-junk"
    )


def test_cookie_value_preferred_over_first_occurrence():
    tool = make_tool({"_xsrf": "real-token"})

    assert (
        tool._extract_xsrf_token(page_html("rotating-junk", "real-token"))
        == "real-token"
    )


def test_first_token_used_when_cookie_value_not_in_page():
    tool = make_tool({"_xsrf": "stale-cookie-value"})

    assert (
        tool._extract_xsrf_token(page_html("rotating-junk", "real-token"))
        == "rotating-junk"
    )


def test_escaped_quotes_are_unescaped_before_extraction():
    tool = make_tool()

    result = tool._extract_xsrf_token(
        page_html("token-one", escaped=True),  # type: ignore[arg-type]
    )

    assert result == "token-one"


def test_missing_token_raises_value_error():
    tool = make_tool()

    with pytest.raises(ValueError, match="xsrf token not found"):
        tool._extract_xsrf_token("<html>no tokens here</html>")


def test_cookie_value_helper_reads_named_cookie():
    tool = make_tool({"_xsrf": "abc", "other": "def"})

    assert tool._cookie_value("_xsrf") == "abc"
    assert tool._cookie_value("missing") is None


def lux_page(config: str, escaped: bool = True) -> FakeResponse:
    body = (
        '<template id="HH-Lux-InitialState">'
        + (config.replace('"', "&#34;") if escaped else config)
        + "</template>"
    )
    return FakeResponse(f"<html>{body}</html>")


AUTHENTICATED_CONFIG = (
    '{"redirectConfig":{"account":{"firstName":"Ivan","lastName":null}}}'
)
ANONYMOUS_CONFIG = (
    '{"redirectConfig":{"account":{"firstName":null,"lastName":null}}}'
)


def test_parse_redirect_config_unescapes_and_returns_config():
    tool = make_tool()

    config = tool.parse_redirect_config(
        lux_page(AUTHENTICATED_CONFIG), check_auth=False
    )

    assert config == {
        "redirectConfig": {"account": {"firstName": "Ivan", "lastName": None}}
    }


def test_parse_redirect_config_checks_auth():
    tool = make_tool()

    with pytest.raises(Exception, match="Авторизация истекла"):
        tool.parse_redirect_config(lux_page(ANONYMOUS_CONFIG))


def test_parse_redirect_config_rejects_unexpected_status():
    tool = make_tool()

    with pytest.raises(Exception, match="Неожиданный код ответа"):
        tool.parse_redirect_config(FakeResponse("x", status_code=404))


def test_is_authenticated_detects_anonymous_account():
    assert HHApplicantTool._is_authenticated(
        {"account": {"firstName": "Ivan", "lastName": None}}
    )
    assert not HHApplicantTool._is_authenticated(
        {"account": {"firstName": None, "lastName": None}}
    )
    assert not HHApplicantTool._is_authenticated(
        {"redirectConfig": {"account": None}}
    )
    assert not HHApplicantTool._is_authenticated({})
