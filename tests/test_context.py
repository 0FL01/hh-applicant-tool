from __future__ import annotations

import json
from types import SimpleNamespace

from hh_applicant_tool import HHApplicantTool, HHProfileContext


def clear_proxy_env(monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_profile_context_resolves_profile_paths(tmp_path, monkeypatch):
    clear_proxy_env(monkeypatch)

    context = HHProfileContext.from_profile(
        config_dir=tmp_path,
        profile_id="profile-a",
    )

    assert context.config_path == (tmp_path / "profile-a").resolve()
    assert context.log_file == context.config_path / "log.txt"
    assert context.cookies_file == context.config_path / "cookies.txt"
    assert context.db_path == context.config_path / "data"
    assert context.config_path.exists()


def test_profile_context_generates_and_persists_user_agent(
    tmp_path,
    monkeypatch,
):
    clear_proxy_env(monkeypatch)
    monkeypatch.setattr(
        "hh_applicant_tool.context.utils.generate_android_useragent",
        lambda: "stable-user-agent",
    )

    context = HHProfileContext.from_profile(
        config_dir=tmp_path,
        profile_id="profile-a",
    )

    client = context.api_client

    assert client.user_agent == "stable-user-agent"
    assert context.config["user_agent"] == "stable-user-agent"

    config_data = json.loads((context.config_path / "config.json").read_text())
    assert config_data["user_agent"] == "stable-user-agent"


def test_profile_context_uses_separate_openai_proxy(
    tmp_path,
    monkeypatch,
):
    clear_proxy_env(monkeypatch)
    context = HHProfileContext.from_profile(
        config_dir=tmp_path,
        profile_id="profile-a",
    )
    context.config.save(
        proxy_url="http://general-proxy:3128",
        openai={"proxy_url": "http://ai-proxy:8080"},
    )

    assert context.session.proxies == {
        "http": "http://general-proxy:3128",
        "https": "http://general-proxy:3128",
    }
    assert context.openai_session.proxies == {
        "http": "http://ai-proxy:8080",
        "https": "http://ai-proxy:8080",
    }


def test_hh_applicant_tool_from_profile_returns_non_cli_context(
    tmp_path,
    monkeypatch,
):
    clear_proxy_env(monkeypatch)

    context = HHApplicantTool.from_profile(
        config_dir=tmp_path,
        profile_id="profile-a",
        api_delay=0.1,
        user_agent="provided-user-agent",
    )

    assert isinstance(context, HHProfileContext)
    assert context.config_path == (tmp_path / "profile-a").resolve()
    assert context.api_client.delay == 0.1
    assert context.api_client.user_agent == "provided-user-agent"


def test_get_openai_chat_uses_generic_openai_env(monkeypatch):
    captured = {}

    class FakeChatClient:
        def __init__(self, config):
            captured["config"] = config

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "auto/model")
    monkeypatch.setenv("OPENAI_REASONING", "max")
    monkeypatch.setattr(
        "hh_llm_agent.openrouter.OpenRouterChatClient",
        FakeChatClient,
    )
    tool = SimpleNamespace(config={})

    HHApplicantTool.get_openai_chat(tool, "system")

    config = captured["config"]
    assert config.api_key == "token"
    assert config.base_url == "https://proxy.example/v1"
    assert config.model == "auto/model"
    assert config.reasoning_effort == "max"
