from __future__ import annotations

import json
import time
from types import SimpleNamespace

from requests import Request

from hh_applicant_tool.api import errors
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


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = []

    def save(self, **kwargs):
        self.saved.append(kwargs)
        self.update(kwargs)


def test_api_client_uses_client_secret_from_config():
    config = FakeConfig(
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token": {},
            "user_agent": "saved-user-agent",
        }
    )
    tool = SimpleNamespace(
        args=SimpleNamespace(api_delay=None, user_agent=None),
        config=config,
        session=FakeSession([]),
    )

    client = HHApplicantTool.api_client.func(tool)

    assert client.client_id == "client-id"
    assert client.client_secret == "client-secret"
    assert client.user_agent == "saved-user-agent"
    assert config.saved == []


def test_api_client_generates_and_persists_profile_user_agent(monkeypatch):
    config = FakeConfig({"token": {}})
    tool = SimpleNamespace(
        args=SimpleNamespace(api_delay=None, user_agent=None),
        config=config,
        session=FakeSession([]),
    )

    monkeypatch.setattr(
        "hh_applicant_tool.main.utils.generate_android_useragent",
        lambda: "stable-user-agent",
    )

    client = HHApplicantTool.api_client.func(tool)

    assert client.user_agent == "stable-user-agent"
    assert config["user_agent"] == "stable-user-agent"
    assert config.saved == [{"user_agent": "stable-user-agent"}]


def test_api_client_refreshes_when_token_is_expired(monkeypatch):
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
        access_expires_at=int(time.time()) - 1,
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


def test_api_client_does_not_refresh_before_local_expiry():
    session = FakeSession(
        responses=[
            FakeResponse(
                403,
                {
                    "description": "Forbidden",
                    "errors": [{"type": "forbidden"}],
                },
            )
        ]
    )
    client = ApiClient(
        access_token="USER_old",
        refresh_token="refresh-token",
        access_expires_at=int(time.time()) + 3600,
        session=session,
        delay=0,
    )

    try:
        client.get("/me")
    except errors.Forbidden:
        pass
    else:
        raise AssertionError("Expected Forbidden to be raised")

    assert len(session.calls) == 1


def test_api_client_solves_captcha_and_retries_request_once():
    captcha_url = "https://hh.ru/account/captcha?state=private-state"
    session = FakeSession(
        [
            FakeResponse(
                403,
                {
                    "errors": [
                        {
                            "type": "captcha_required",
                            "value": "captcha_required",
                            "captcha_url": captcha_url,
                        }
                    ]
                },
            ),
            FakeResponse(200, {"items": []}),
        ]
    )
    client = ApiClient(session=session, delay=0)
    handled = []
    handler = lambda ex: handled.append(ex.captcha_url)

    with client.handle_captcha(handler):
        assert client.get("/me") == {"items": []}
    assert handled == [captcha_url]
    assert len(session.calls) == 2
    assert session.calls[0][:2] == session.calls[1][:2]


def test_api_client_does_not_retry_a_repeated_captcha():
    session = FakeSession(
        [
            FakeResponse(
                403,
                {
                    "errors": [
                        {
                            "type": "captcha_required",
                            "value": "captcha_required",
                            "captcha_url": "https://hh.ru/account/captcha?state=one",
                        }
                    ]
                },
            ),
            FakeResponse(
                403,
                {
                    "errors": [
                        {
                            "type": "captcha_required",
                            "value": "captcha_required",
                            "captcha_url": "https://hh.ru/account/captcha?state=two",
                        }
                    ]
                },
            ),
        ]
    )
    client = ApiClient(session=session, delay=0)
    handled = []
    handler = lambda ex: handled.append(ex.captcha_url)

    try:
        with client.handle_captcha(handler):
            client.get("/me")
    except errors.CaptchaRequired as ex:
        assert ex.captcha_url.endswith("state=two")
    else:
        raise AssertionError("Expected repeated CAPTCHA to abort the request")

    assert len(handled) == 1
    assert len(session.calls) == 2


def test_api_client_does_not_retry_when_captcha_handler_fails():
    session = FakeSession(
        [
            FakeResponse(
                403,
                {
                    "errors": [
                        {
                            "type": "captcha_required",
                            "value": "captcha_required",
                            "captcha_url": "https://hh.ru/account/captcha?state=one",
                        }
                    ]
                },
            ),
            FakeResponse(200, {"items": []}),
        ]
    )
    client = ApiClient(session=session, delay=0)

    def abort(_challenge):
        raise RuntimeError("CAPTCHA was not solved")

    try:
        with client.handle_captcha(abort):
            client.get("/me")
    except RuntimeError as ex:
        assert str(ex) == "CAPTCHA was not solved"
    else:
        raise AssertionError("Expected solver failure to abort the request")

    assert len(session.calls) == 1


def test_api_client_passes_timeout_to_requests_session():
    session = FakeSession([FakeResponse(200, {"items": []})])
    client = ApiClient(
        session=session,
        delay=0,
        timeout=12.5,
    )

    client.get("/me")

    assert session.calls[0][2]["timeout"] == 12.5
    assert client.oauth_client.timeout == 12.5
