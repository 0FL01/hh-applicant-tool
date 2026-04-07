from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from re import compile as re_compile
from threading import Lock
from typing import Any

from .config import TelegramCollectorConfig
from .tg_bot_client import TelegramBotClient, TelegramBotError
from .tg_collector_store import TelegramCollectorStore

logger = logging.getLogger(__package__)

HEALTH_PATH = "/healthz"
WEBHOOK_PATH = "/webhooks/hh/recruiter-contact-offer"
GENERIC_PATH = "/webhooks/generic"
EVENT_TYPE = "recruiter_contact_offer"
IDEMPOTENCY_HEADER = "X-Idempotency-Key"
EVENT_HEADER = "X-HH-Applicant-Event"
TEMPLATE_HEADER = "X-Telegram-Template"
MAX_BODY_SIZE = 65536
_TEMPLATE_VAR_RE = re_compile(r"\{\{(\w[\w.]*\w|\w)\}\}")


class CollectorHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class CollectorResult:
    status_code: int
    payload: dict[str, object]


class TelegramContactCollectorService:
    def __init__(
        self,
        config: TelegramCollectorConfig,
        store: TelegramCollectorStore | None = None,
        bot_client: TelegramBotClient | None = None,
    ):
        self.config = config
        self.store = store or TelegramCollectorStore(config.db_path)
        self.bot_client = bot_client or TelegramBotClient(config.bot)
        self._lock = Lock()

    def handle_health(self) -> CollectorResult:
        return CollectorResult(status_code=200, payload={"status": "ok"})

    def handle_generic_webhook(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> CollectorResult:
        if method != "POST":
            raise CollectorHTTPError(405, "method not allowed")
        if path != GENERIC_PATH:
            raise CollectorHTTPError(404, "not found")

        if len(body) > MAX_BODY_SIZE:
            raise CollectorHTTPError(413, "payload too large")

        secret = self.config.webhook_secret
        if secret:
            header_value = headers.get(
                self.config.webhook_secret_header, ""
            ).strip()
            if header_value != secret:
                raise CollectorHTTPError(401, "unauthorized")

        idempotency_key = headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not idempotency_key:
            raise CollectorHTTPError(400, f"{IDEMPOTENCY_HEADER} is required")

        payload = self._load_payload(body)
        if not isinstance(payload, dict):
            raise CollectorHTTPError(400, "payload must be a JSON object")

        record = {
            "idempotency_key": idempotency_key,
            "event_type": "generic",
            "created_at": payload.get("created_at") or "",
            "payload_json": payload,
        }

        with self._lock:
            existing = self.store.get(idempotency_key)
            if existing is not None and existing.delivery_status == "sent":
                return CollectorResult(
                    status_code=200,
                    payload={
                        "status": "duplicate",
                        "idempotency_key": idempotency_key,
                        "telegram_message_id": existing.telegram_message_id,
                    },
                )

            self.store.save_event(record)
            template = headers.get(TEMPLATE_HEADER, "").strip()
            message_text = self._render_generic(payload, template)
            self.store.mark_attempt(idempotency_key)
            try:
                telegram_response = self.bot_client.send_message(message_text)
            except TelegramBotError as ex:
                self.store.mark_failed(idempotency_key, str(ex))
                raise CollectorHTTPError(502, str(ex)) from ex

            telegram_message_id = self._extract_telegram_message_id(
                telegram_response
            )
            self.store.mark_sent(
                idempotency_key,
                telegram_message_id=telegram_message_id,
            )
        logger.info(
            "Telegram collector forwarded generic %s telegram_msg_id=%s",
            idempotency_key,
            telegram_message_id,
        )
        return CollectorResult(
            status_code=202,
            payload={
                "status": "forwarded",
                "idempotency_key": idempotency_key,
                "telegram_message_id": telegram_message_id,
            },
        )

    def handle_webhook(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> CollectorResult:
        if method != "POST":
            raise CollectorHTTPError(405, "method not allowed")
        if path != WEBHOOK_PATH:
            raise CollectorHTTPError(404, "not found")

        secret = self.config.webhook_secret
        if secret:
            header_value = headers.get(
                self.config.webhook_secret_header, ""
            ).strip()
            if header_value != secret:
                raise CollectorHTTPError(401, "unauthorized")

        event_type = headers.get(EVENT_HEADER, "").strip()
        if event_type != EVENT_TYPE:
            raise CollectorHTTPError(
                400,
                f"{EVENT_HEADER} must be {EVENT_TYPE!r}",
            )

        idempotency_key = headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not idempotency_key:
            raise CollectorHTTPError(400, f"{IDEMPOTENCY_HEADER} is required")

        payload = self._load_payload(body)
        payload_key = self._required_string(payload, "idempotency_key")
        if payload_key != idempotency_key:
            raise CollectorHTTPError(
                400,
                "idempotency header does not match idempotency_key",
            )

        payload_event = self._required_string(payload, "event_type")
        if payload_event != EVENT_TYPE:
            raise CollectorHTTPError(
                400, "event_type must be recruiter_contact_offer"
            )

        record = self._build_record(payload)
        with self._lock:
            existing = self.store.get(idempotency_key)
            if existing is not None and existing.delivery_status == "sent":
                return CollectorResult(
                    status_code=200,
                    payload={
                        "status": "duplicate",
                        "idempotency_key": idempotency_key,
                        "telegram_message_id": existing.telegram_message_id,
                    },
                )

            self.store.save_event(record)
            message_text = self._render_message(payload)
            self.store.mark_attempt(idempotency_key)
            try:
                telegram_response = self.bot_client.send_message(message_text)
            except TelegramBotError as ex:
                self.store.mark_failed(idempotency_key, str(ex))
                raise CollectorHTTPError(502, str(ex)) from ex

            telegram_message_id = self._extract_telegram_message_id(
                telegram_response
            )
            self.store.mark_sent(
                idempotency_key,
                telegram_message_id=telegram_message_id,
            )
        logger.info(
            "Telegram collector forwarded %s negotiation=%s message_id=%s",
            idempotency_key,
            record.get("negotiation_id"),
            telegram_message_id,
        )
        return CollectorResult(
            status_code=202,
            payload={
                "status": "forwarded",
                "idempotency_key": idempotency_key,
                "telegram_message_id": telegram_message_id,
            },
        )

    def _load_payload(self, body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as ex:
            raise CollectorHTTPError(400, f"invalid JSON payload: {ex}") from ex
        if not isinstance(payload, dict):
            raise CollectorHTTPError(400, "payload must be a JSON object")
        return payload

    def _build_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        contacts = payload.get("contacts") or {}
        if not isinstance(contacts, dict):
            raise CollectorHTTPError(400, "contacts must be an object")
        from_message = contacts.get("from_message") or {}
        if not isinstance(from_message, dict):
            raise CollectorHTTPError(
                400, "contacts.from_message must be an object"
            )
        negotiation = payload.get("negotiation") or {}
        if not isinstance(negotiation, dict):
            raise CollectorHTTPError(400, "negotiation must be an object")
        vacancy = payload.get("vacancy") or {}
        employer = payload.get("employer") or {}

        negotiation_id = negotiation.get("id")
        if not isinstance(negotiation_id, int) or negotiation_id <= 0:
            raise CollectorHTTPError(
                400, "negotiation.id must be a positive integer"
            )

        recruiter_name = self._pick_recruiter_name(from_message, employer)
        return {
            "idempotency_key": self._required_string(
                payload, "idempotency_key"
            ),
            "event_type": self._required_string(payload, "event_type"),
            "created_at": self._required_string(payload, "created_at"),
            "negotiation_id": negotiation_id,
            "last_message_id": self._optional_string(
                negotiation, "last_message_id"
            ),
            "vacancy_name": self._optional_string(vacancy, "name"),
            "employer_name": self._optional_string(employer, "name"),
            "recruiter_name": recruiter_name,
            "telegram_handles": self._string_list(
                from_message.get("telegram_handles")
            ),
            "emails": self._string_list(from_message.get("emails")),
            "phones": self._string_list(from_message.get("phones")),
            "payload_json": payload,
        }

    def _render_message(self, payload: dict[str, Any]) -> str:
        contacts = payload.get("contacts") or {}
        from_message = contacts.get("from_message") or {}
        vacancy = payload.get("vacancy") or {}
        employer = payload.get("employer") or {}
        negotiation = payload.get("negotiation") or {}
        employer_tail = payload.get("employer_tail") or {}

        recruiter_name = self._pick_recruiter_name(from_message, employer)
        telegram_handles = self._string_list(
            from_message.get("telegram_handles")
        )
        telegram_urls = self._string_list(from_message.get("telegram_urls"))
        emails = self._string_list(from_message.get("emails"))
        phones = self._string_list(from_message.get("phones"))
        urls = self._string_list(from_message.get("urls"))
        vacancy_url = self._optional_string(vacancy, "alternate_url")
        tail_text = self._truncate(
            self._optional_string(employer_tail, "text") or "",
            1500,
        )

        parts = [
            "Новый контакт рекрутера из HH",
            f"Рекрутер: {recruiter_name}",
            f"Вакансия: {self._optional_string(vacancy, 'name') or '-'}",
            f"Компания: {self._optional_string(employer, 'name') or '-'}",
            f"Negotiation ID: {negotiation.get('id')}",
        ]
        if telegram_handles:
            parts.append(
                "Telegram: "
                + ", ".join(f"@{item.lstrip('@')}" for item in telegram_handles)
            )
        if telegram_urls:
            parts.append("Telegram URL: " + ", ".join(telegram_urls))
        if emails:
            parts.append("Email: " + ", ".join(emails))
        if phones:
            parts.append("Телефон: " + ", ".join(phones))
        if urls:
            parts.append("URL: " + ", ".join(urls))
        if vacancy_url:
            parts.append(f"Вакансия HH: {vacancy_url}")
        if tail_text:
            parts.append("Сообщение:\n" + tail_text)
        parts.append(
            f"Idempotency: {self._required_string(payload, 'idempotency_key')}"
        )
        return self._truncate("\n\n".join(parts), 4000)

    def _render_generic(
        self, payload: dict[str, Any], template: str | None
    ) -> str:
        if template:
            return self._truncate(
                self._render_template(template, payload), 4000
            )
        return self._truncate(
            json.dumps(payload, ensure_ascii=False, indent=2), 4000
        )

    def _render_template(self, template: str, payload: dict[str, Any]) -> str:
        """Simple {{key.path}} template rendering.

        Supports dot-notation for nested keys: ``{{vacancy.name}}`` resolves
        ``payload["vacancy"]["name"]``.  Missing keys produce empty string.
        """

        def _resolve(path: str) -> str:
            current: Any = payload
            for key in path.split("."):
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    return ""
                if current is None:
                    return ""
            return str(current) if current is not None else ""

        return _TEMPLATE_VAR_RE.sub(lambda m: _resolve(m.group(1)), template)

    def _extract_telegram_message_id(
        self,
        telegram_response: dict[str, object],
    ) -> int | None:
        result = telegram_response.get("result")
        if not isinstance(result, dict):
            return None
        message_id = result.get("message_id")
        return message_id if isinstance(message_id, int) else None

    def _pick_recruiter_name(
        self,
        from_message: dict[str, Any],
        employer: dict[str, Any],
    ) -> str:
        signature_name = self._optional_string(from_message, "signature_name")
        if signature_name:
            return signature_name
        handles = self._string_list(from_message.get("telegram_handles"))
        if handles:
            return f"@{handles[0].lstrip('@')}"
        employer_name = self._optional_string(employer, "name")
        if employer_name:
            return f"{employer_name} recruiter"
        return "Recruiter"

    def _required_string(self, data: dict[str, Any], key: str) -> str:
        value = self._optional_string(data, key)
        if not value:
            raise CollectorHTTPError(400, f"{key} is required")
        if any(char.isspace() for char in value) and key == "idempotency_key":
            raise CollectorHTTPError(
                400, "idempotency_key must not contain whitespace"
            )
        return value

    def _optional_string(self, data: dict[str, Any], key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        return str(value).strip() or None

    def _string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            text = str(item).strip()
            if text:
                result.append(text)
        return result

    def _truncate(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"


class TelegramCollectorHTTPServer(ThreadingHTTPServer):
    def __init__(
        self, server_address, service: TelegramContactCollectorService
    ):
        self.service = service
        super().__init__(server_address, TelegramCollectorRequestHandler)


class TelegramCollectorRequestHandler(BaseHTTPRequestHandler):
    server: TelegramCollectorHTTPServer

    def do_GET(self) -> None:
        if self.path != HEALTH_PATH:
            self._write_json(404, {"error": "not found"})
            return
        result = self.server.service.handle_health()
        self._write_json(result.status_code, result.payload)

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._write_json(400, {"error": "invalid Content-Length"})
            return
        body = self.rfile.read(content_length)
        headers = {key: value for key, value in self.headers.items()}
        try:
            if self.path == GENERIC_PATH:
                result = self.server.service.handle_generic_webhook(
                    method="POST",
                    path=self.path,
                    headers=headers,
                    body=body,
                )
            else:
                result = self.server.service.handle_webhook(
                    method="POST",
                    path=self.path,
                    headers=headers,
                    body=body,
                )
        except CollectorHTTPError as ex:
            self._write_json(ex.status_code, {"error": ex.message})
            return
        self._write_json(result.status_code, result.payload)

    def log_message(self, format: str, *args) -> None:
        logger.info(
            "Telegram collector HTTP %s - %s",
            self.address_string(),
            format % args,
        )

    def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
