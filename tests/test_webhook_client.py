from hh_llm_agent.config import WebhookConfig
from hh_llm_agent.webhook import WebhookClient


class DummyResponse:
    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return DummyResponse()


def test_webhook_client_passes_ssl_verification_flag():
    session = FakeSession()
    client = WebhookClient(
        WebhookConfig(
            url="https://example.com/hook",
            timeout_seconds=3.0,
            verify_ssl=False,
        ),
        session=session,
    )

    client.send(
        event_type="recruiter_contact_offer",
        idempotency_key="neg-1:msg-1",
        payload={"hello": "world"},
    )

    assert len(session.calls) == 1
    assert session.calls[0]["verify"] is False
