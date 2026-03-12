from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class HHGateway:
    def __init__(self, tool: Any):
        self.tool = tool

    def get_user(self) -> dict[str, Any]:
        return self.tool.get_me()

    def get_blacklisted(self) -> set[str]:
        return set(self.tool.get_blacklisted())

    def get_resumes(self) -> list[dict[str, Any]]:
        return self.tool.get_resumes()

    def get_negotiations(self) -> Iterable[dict[str, Any]]:
        return self.tool.get_negotiations()

    def get_vacancy(self, vacancy_id: str | int) -> dict[str, Any]:
        return self.tool.api_client.get(f"/vacancies/{vacancy_id}")

    def get_employer(self, employer_id: str | int) -> dict[str, Any]:
        return self.tool.api_client.get(f"/employers/{employer_id}")

    def fetch_messages(self, negotiation_id: str | int) -> list[dict[str, Any]]:
        page = 0
        messages: list[dict[str, Any]] = []
        while True:
            response = self.tool.api_client.get(
                f"/negotiations/{negotiation_id}/messages",
                page=page,
                with_text_only=True,
            )
            items = response.get("items", [])
            if not items:
                break
            messages.extend(items)
            page += 1
            if page >= response.get("pages", 0):
                break
        return messages

    def send_message(
        self,
        negotiation_id: str | int,
        message: str,
        *,
        delay: float | None = None,
    ) -> None:
        self.tool.api_client.post(
            f"/negotiations/{negotiation_id}/messages",
            message=message,
            delay=delay,
        )
