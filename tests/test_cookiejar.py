from __future__ import annotations

from http.cookiejar import Cookie

from hh_applicant_tool.utils.cookiejar import HHOnlyCookieJar


def make_cookie(domain: str) -> Cookie:
    return Cookie(
        version=0,
        name="session",
        value="x",
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
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


def collect_domains(jar: HHOnlyCookieJar, domains: list[str]) -> set[str]:
    for domain in domains:
        jar.set_cookie(make_cookie(domain))
    return {cookie.domain for cookie in jar}


def test_hh_domains_are_kept(tmp_path):
    jar = HHOnlyCookieJar(str(tmp_path / "cookies.txt"))

    kept = collect_domains(jar, ["hh.ru", ".hh.ru", "hh.kz", "api.hh.ru"])

    assert kept == {"hh.ru", ".hh.ru", "hh.kz", "api.hh.ru"}


def test_foreign_domains_are_dropped(tmp_path):
    jar = HHOnlyCookieJar(str(tmp_path / "cookies.txt"))

    kept = collect_domains(jar, ["example.com", "hh.com.ru", "yandex.ru"])

    assert kept == set()


def test_israel_hh_com_is_dropped(tmp_path):
    jar = HHOnlyCookieJar(str(tmp_path / "cookies.txt"))

    kept = collect_domains(jar, ["israel.hh.com"])

    assert kept == set()
