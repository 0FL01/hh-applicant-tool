import re
from http.cookiejar import Cookie, MozillaCookieJar


class HHOnlyCookieJar(MozillaCookieJar):
    """Хранилище, которое сохраняет куки только с хх"""

    def set_cookie(self, cookie: Cookie):
        # Домены hh.ru, hh.kz и т.д. Поддомены разрешены, но israel.hh.com
        # исключен: это отдельный сайт с чужой авторизацией (upstream 5d94bdd).
        pattern = r"^(?!israel\.)(?:.*?\.)?hh\.(ru|kz|uz|by|net|com)\.?$"

        if re.search(pattern, cookie.domain):
            super().set_cookie(cookie)

    # def save(
    #     self,
    #     filename: str | None = None,
    #     ignore_discard: bool = False,
    #     ignore_expires: bool = False,
    # ) -> None:
    #     return super(MozillaCookieJar).save(
    #         filename, ignore_discard, ignore_expires
    #     )
