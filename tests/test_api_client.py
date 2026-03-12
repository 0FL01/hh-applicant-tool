from __future__ import annotations

import json
import time
from types import SimpleNamespace

from requests import Request

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.main import HHApplicantTool


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}
        self.request = Request("GET", "https://api.hh.ru/me").prepare()

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls = []
        self.headers = {}
        self.proxies = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_api_client_uses_client_secret_from_config():
    tool = SimpleNamespace(
        args=SimpleNamespace(api_delay=None, user_agent=None),
        config={
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token": {},
        },
        session=FakeSession([]),
    )

    client = HHApplicantTool.api_client.func(tool)

    assert client.client_id == "client-id"
    assert client.client_secret == "client-secret"


def test_api_client_refreshes_after_forbidden_before_local_expiry(monkeypatch):
    session = FakeSession(
        responses=[
            FakeResponse(
                403,
                {
                    "description": "Forbidden",
                    "errors": [{"type": "forbidden"}],
                },
            ),
            FakeResponse(200, {"items": []}),
        ]
    )
    client = ApiClient(
        access_token="USER_old",
        refresh_token="refresh-token",
        access_expires_at=int(time.time()) + 3600,
        session=session,
        delay=0,
    )

    refreshed = {"called": 0}

    def fake_refresh_access_token() -> None:
        refreshed["called"] += 1
        client.access_token = "USER_new"

    monkeypatch.setattr(
        client, "refresh_access_token", fake_refresh_access_token
    )

    payload = client.get("/me")

    assert payload == {"items": []}
    assert refreshed["called"] == 1
    assert len(session.calls) == 2
    assert session.calls[0][2]["headers"]["authorization"] == "Bearer USER_old"
    assert session.calls[1][2]["headers"]["authorization"] == "Bearer USER_new"
