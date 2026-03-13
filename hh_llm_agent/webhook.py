from __future__ import annotations

from dataclasses import dataclass

import requests

from .config import WebhookConfig


class WebhookError(RuntimeError):
    pass


@dataclass
class WebhookClient:
    config: WebhookConfig
    session: requests.Session | None = None
    proxies: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        if self.proxies:
            self.session.proxies.update(self.proxies)

    def send(
        self,
        *,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "hh-applicant-tool/webhook",
            "X-HH-Applicant-Event": event_type,
            "X-Idempotency-Key": idempotency_key,
        }
        if self.config.secret:
            headers[self.config.secret_header] = self.config.secret
        try:
            response = self.session.post(
                self.config.url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as ex:
            detail = ""
            response = getattr(ex, "response", None)
            if response is not None:
                detail = f" status={response.status_code} body={response.text[:500]!r}"
            raise WebhookError(
                f"webhook delivery failed:{detail or f' {ex}'}"
            ) from ex
