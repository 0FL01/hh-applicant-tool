import json

import pytest

from hh_llm_agent.config import TelegramBotConfig, TelegramCollectorConfig
from hh_llm_agent.tg_bot_client import TelegramBotError
from hh_llm_agent.tg_contact_collector import (
    CollectorHTTPError,
    EVENT_HEADER,
    EVENT_TYPE,
    GENERIC_PATH,
    IDEMPOTENCY_HEADER,
    TEMPLATE_HEADER,
    WEBHOOK_PATH,
    TelegramContactCollectorService,
)
from hh_llm_agent.tg_collector_store import TelegramCollectorStore


class FakeBotClient:
    def __init__(self):
        self.calls = []
        self.failures_remaining = 0

    def send_message(self, text: str) -> dict[str, object]:
        self.calls.append(text)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise TelegramBotError("temporary telegram outage")
        return {"ok": True, "result": {"message_id": 321}}


def make_config(tmp_path):
    return TelegramCollectorConfig(
        bot=TelegramBotConfig(token="bot-token", target_chat_id=123456),
        webhook_secret="supersecret",
        db_path=str(tmp_path / "collector.sqlite3"),
    )


def make_payload():
    return {
        "event_type": "recruiter_contact_offer",
        "idempotency_key": "recruiter_contact_offer:1:msg-1",
        "created_at": "2026-03-13T12:00:00+00:00",
        "run_id": "run-1",
        "classifier": {
            "category": "recruiter_contact_offer",
            "action": "skip",
            "reason": "direct_contacts_shared",
            "confidence": 0.97,
        },
        "candidate": {
            "first_name": "Ivan",
            "last_name": "Petrov",
            "resume_id": "resume-1",
            "resume_title": "Backend Engineer",
        },
        "negotiation": {
            "id": 1,
            "chat_id": 11,
            "state": "active",
            "last_message_id": "msg-1",
            "updated_at": "2026-03-13T12:00:00+00:00",
        },
        "vacancy": {
            "id": 101,
            "name": "Platform Engineer",
            "alternate_url": "https://hh.ru/vacancy/101",
            "area": {"id": "1", "name": "Moscow"},
            "salary": {},
        },
        "employer": {
            "id": 501,
            "name": "Acme",
            "site_url": "https://acme.example",
            "alternate_url": "https://hh.ru/employer/501",
        },
        "contacts": {
            "from_message": {
                "signature_name": "Виктория",
                "emails": ["hr@example.com"],
                "telegram_urls": ["https://t.me/ithr_victoria"],
                "telegram_handles": ["ithr_victoria"],
                "phones": ["+79991234567"],
                "urls": [],
                "contact_lines": ["telegram: t.me/ithr_victoria"],
            },
            "from_vacancy": {"contacts": [], "raw_contacts": {}},
            "from_employer_site": [],
        },
        "employer_tail": {
            "messages": [
                {
                    "id": "msg-1",
                    "created_at": "2026-03-13T12:00:00+00:00",
                    "text": "Напишите мне в Telegram, пожалуйста.",
                }
            ],
            "text": "Напишите мне в Telegram, пожалуйста.",
        },
    }


def make_headers(payload):
    return {
        EVENT_HEADER: EVENT_TYPE,
        IDEMPOTENCY_HEADER: payload["idempotency_key"],
        "X-Webhook-Secret": "supersecret",
    }


def test_collector_forwards_payload_and_persists_sent_status(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    store = TelegramCollectorStore(config.db_path)
    service = TelegramContactCollectorService(
        config,
        store=store,
        bot_client=bot_client,
    )
    payload = make_payload()

    result = service.handle_webhook(
        method="POST",
        path=WEBHOOK_PATH,
        headers=make_headers(payload),
        body=json.dumps(payload).encode("utf-8"),
    )

    assert result.status_code == 202
    assert result.payload["status"] == "forwarded"
    assert len(bot_client.calls) == 1
    assert "@ithr_victoria" in bot_client.calls[0]
    assert "Platform Engineer" in bot_client.calls[0]
    lead = store.get(payload["idempotency_key"])
    assert lead is not None
    assert lead.delivery_status == "sent"
    assert lead.telegram_message_id == 321
    assert lead.emails == ["hr@example.com"]
    assert lead.telegram_handles == ["ithr_victoria"]


def test_collector_returns_duplicate_for_sent_lead(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    service = TelegramContactCollectorService(config, bot_client=bot_client)
    payload = make_payload()

    first = service.handle_webhook(
        method="POST",
        path=WEBHOOK_PATH,
        headers=make_headers(payload),
        body=json.dumps(payload).encode("utf-8"),
    )
    second = service.handle_webhook(
        method="POST",
        path=WEBHOOK_PATH,
        headers=make_headers(payload),
        body=json.dumps(payload).encode("utf-8"),
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.payload["status"] == "duplicate"
    assert len(bot_client.calls) == 1


def test_collector_retries_failed_delivery(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    bot_client.failures_remaining = 1
    store = TelegramCollectorStore(config.db_path)
    service = TelegramContactCollectorService(
        config,
        store=store,
        bot_client=bot_client,
    )
    payload = make_payload()

    with pytest.raises(CollectorHTTPError) as exc:
        service.handle_webhook(
            method="POST",
            path=WEBHOOK_PATH,
            headers=make_headers(payload),
            body=json.dumps(payload).encode("utf-8"),
        )
    assert exc.value.status_code == 502

    failed = store.get(payload["idempotency_key"])
    assert failed is not None
    assert failed.delivery_status == "failed"
    assert failed.delivery_attempts == 1

    result = service.handle_webhook(
        method="POST",
        path=WEBHOOK_PATH,
        headers=make_headers(payload),
        body=json.dumps(payload).encode("utf-8"),
    )
    recovered = store.get(payload["idempotency_key"])

    assert result.status_code == 202
    assert recovered is not None
    assert recovered.delivery_status == "sent"
    assert recovered.delivery_attempts == 2
    assert len(bot_client.calls) == 2


def test_collector_rejects_invalid_secret(tmp_path):
    config = make_config(tmp_path)
    service = TelegramContactCollectorService(
        config, bot_client=FakeBotClient()
    )
    payload = make_payload()
    headers = make_headers(payload)
    headers["X-Webhook-Secret"] = "wrong"

    with pytest.raises(CollectorHTTPError) as exc:
        service.handle_webhook(
            method="POST",
            path=WEBHOOK_PATH,
            headers=headers,
            body=json.dumps(payload).encode("utf-8"),
        )

    assert exc.value.status_code == 401


# ==================== Generic webhook tests ====================


def test_generic_forwards_json_without_template(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    service = TelegramContactCollectorService(
        config,
        bot_client=bot_client,
    )
    payload = {"title": "Deploy", "status": "ok", "version": "1.2.3"}

    result = service.handle_generic_webhook(
        method="POST",
        path=GENERIC_PATH,
        headers={
            IDEMPOTENCY_HEADER: "generic-1",
            "X-Webhook-Secret": "supersecret",
        },
        body=json.dumps(payload).encode("utf-8"),
    )

    assert result.status_code == 202
    assert result.payload["status"] == "forwarded"
    assert len(bot_client.calls) == 1
    sent_text = bot_client.calls[0]
    assert "Deploy" in sent_text
    assert "1.2.3" in sent_text


def test_generic_renders_template_header(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    service = TelegramContactCollectorService(
        config,
        bot_client=bot_client,
    )
    payload = {
        "title": "Deploy Complete",
        "message": "Service api-v2 deployed to prod",
        "extra": {"env": "production"},
    }

    result = service.handle_generic_webhook(
        method="POST",
        path=GENERIC_PATH,
        headers={
            IDEMPOTENCY_HEADER: "generic-2",
            "X-Webhook-Secret": "supersecret",
            TEMPLATE_HEADER: "{{title}}\n\n{{message}}\nEnv: {{extra.env}}",
        },
        body=json.dumps(payload).encode("utf-8"),
    )

    assert result.status_code == 202
    sent_text = bot_client.calls[0]
    assert sent_text.startswith("Deploy Complete")
    assert "Service api-v2 deployed to prod" in sent_text
    assert "Env: production" in sent_text


def test_generic_deduplicates(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    service = TelegramContactCollectorService(
        config,
        bot_client=bot_client,
    )
    payload = {"alert": "test"}

    headers = {
        IDEMPOTENCY_HEADER: "generic-dup",
        "X-Webhook-Secret": "supersecret",
    }
    body = json.dumps(payload).encode("utf-8")

    first = service.handle_generic_webhook(
        method="POST",
        path=GENERIC_PATH,
        headers=headers,
        body=body,
    )
    second = service.handle_generic_webhook(
        method="POST",
        path=GENERIC_PATH,
        headers=headers,
        body=body,
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.payload["status"] == "duplicate"
    assert len(bot_client.calls) == 1


def test_generic_rejects_without_secret(tmp_path):
    config = make_config(tmp_path)
    service = TelegramContactCollectorService(
        config,
        bot_client=FakeBotClient(),
    )

    with pytest.raises(CollectorHTTPError) as exc:
        service.handle_generic_webhook(
            method="POST",
            path=GENERIC_PATH,
            headers={IDEMPOTENCY_HEADER: "k"},
            body=b"{}",
        )
    assert exc.value.status_code == 401


def test_generic_rejects_without_idempotency_key(tmp_path):
    config = make_config(tmp_path)
    service = TelegramContactCollectorService(
        config,
        bot_client=FakeBotClient(),
    )

    with pytest.raises(CollectorHTTPError) as exc:
        service.handle_generic_webhook(
            method="POST",
            path=GENERIC_PATH,
            headers={"X-Webhook-Secret": "supersecret"},
            body=b'{"a":1}',
        )
    assert exc.value.status_code == 400


def test_generic_rejects_get(tmp_path):
    config = make_config(tmp_path)
    service = TelegramContactCollectorService(
        config,
        bot_client=FakeBotClient(),
    )

    with pytest.raises(CollectorHTTPError) as exc:
        service.handle_generic_webhook(
            method="GET",
            path=GENERIC_PATH,
            headers={},
            body=b"",
        )
    assert exc.value.status_code == 405


def test_generic_retries_on_telegram_failure(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    bot_client.failures_remaining = 1
    service = TelegramContactCollectorService(
        config,
        bot_client=bot_client,
    )
    payload = {"title": "retry-test"}
    headers = {
        IDEMPOTENCY_HEADER: "generic-retry",
        "X-Webhook-Secret": "supersecret",
    }
    body = json.dumps(payload).encode("utf-8")

    with pytest.raises(CollectorHTTPError) as exc:
        service.handle_generic_webhook(
            method="POST",
            path=GENERIC_PATH,
            headers=headers,
            body=body,
        )
    assert exc.value.status_code == 502

    result = service.handle_generic_webhook(
        method="POST",
        path=GENERIC_PATH,
        headers=headers,
        body=body,
    )
    assert result.status_code == 202
    assert len(bot_client.calls) == 2


def test_generic_template_missing_keys_produce_empty(tmp_path):
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    service = TelegramContactCollectorService(
        config,
        bot_client=bot_client,
    )
    payload = {"title": "Hello"}

    result = service.handle_generic_webhook(
        method="POST",
        path=GENERIC_PATH,
        headers={
            IDEMPOTENCY_HEADER: "generic-missing",
            "X-Webhook-Secret": "supersecret",
            TEMPLATE_HEADER: "{{title}} / {{missing_key}}",
        },
        body=json.dumps(payload).encode("utf-8"),
    )

    assert result.status_code == 202
    assert bot_client.calls[0] == "Hello / "


def test_hh_webhook_accepts_string_negotiation_id(tmp_path):
    """HH API returns negotiation.id as string, e.g. '5145860360'."""
    config = make_config(tmp_path)
    bot_client = FakeBotClient()
    service = TelegramContactCollectorService(
        config,
        bot_client=bot_client,
    )
    payload = {
        "event_type": EVENT_TYPE,
        "idempotency_key": "recruiter_contact_offer:5145860360:msg-1",
        "created_at": "2026-04-07T12:00:00+00:00",
        "classifier": {
            "category": EVENT_TYPE,
            "action": "skip",
            "reason": "direct_contacts_shared",
            "confidence": 0.97,
        },
        "candidate": {
            "first_name": "Ivan",
            "last_name": "Petrov",
            "resume_id": "resume-1",
            "resume_title": "Backend Engineer",
        },
        "negotiation": {
            "id": "5145860360",
            "chat_id": 11,
            "state": "active",
            "last_message_id": "msg-1",
            "updated_at": "2026-04-07T12:00:00+00:00",
        },
        "vacancy": {
            "id": "101",
            "name": "Platform Engineer",
            "alternate_url": "https://hh.ru/vacancy/101",
            "area": {"id": "1", "name": "Moscow"},
            "salary": {
                "from": 200000,
                "to": 300000,
                "currency": "RUR",
                "gross": False,
            },
        },
        "employer": {
            "id": "201",
            "name": "TechCorp",
            "site_url": "https://techcorp.example.com",
            "alternate_url": "https://hh.ru/employer/201",
        },
        "contacts": {
            "from_message": {
                "telegram_handles": ["@recruiter"],
                "emails": [],
                "phones": [],
            },
        },
    }

    result = service.handle_webhook(
        method="POST",
        path=WEBHOOK_PATH,
        headers={
            IDEMPOTENCY_HEADER: "recruiter_contact_offer:5145860360:msg-1",
            EVENT_HEADER: EVENT_TYPE,
            "X-Webhook-Secret": "supersecret",
        },
        body=json.dumps(payload).encode("utf-8"),
    )

    assert result.status_code == 202
    assert result.payload["status"] == "forwarded"
    assert len(bot_client.calls) == 1
