from __future__ import annotations

from dataclasses import dataclass

import requests

from .config import TelegramBotConfig


class TelegramBotError(RuntimeError):
    pass


@dataclass
class TelegramBotClient:
    config: TelegramBotConfig
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    def send_message(self, text: str) -> dict[str, object]:
        url = f"{self.config.base_url.rstrip('/')}/bot{self.config.token}/sendMessage"
        try:
            response = self.session.post(
                url,
                json={
                    "chat_id": self.config.target_chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as ex:
            detail = ""
            if getattr(ex, "response", None) is not None:
                detail = (
                    f" status={ex.response.status_code}"
                    f" body={ex.response.text[:500]!r}"
                )
            raise TelegramBotError(
                f"telegram sendMessage failed:{detail or f' {ex}'}"
            ) from ex

        try:
            payload = response.json()
        except ValueError as ex:
            raise TelegramBotError("telegram returned invalid JSON") from ex
        if payload.get("ok") is False:
            raise TelegramBotError(
                f"telegram sendMessage rejected: {payload.get('description') or payload}"
            )
        return payload
