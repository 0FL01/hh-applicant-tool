from types import SimpleNamespace

import pytest

from hh_llm_agent.config import (
    DEFAULT_CLASSIFIER_MODEL,
    load_agent_config,
)


def make_tool(config=None):
    return SimpleNamespace(config=config or {})


def make_args(**overrides):
    defaults = {
        "openrouter_api_key": None,
        "temperature": None,
        "max_completion_tokens": None,
        "model": None,
        "openrouter_base_url": None,
        "period": None,
        "max_history_messages": None,
        "system_prompt": None,
        "reply_instruction": None,
        "reasoning": None,
        "classifier_enabled": None,
        "classifier_model": None,
        "classifier_temperature": None,
        "classifier_max_completion_tokens": None,
        "classifier_system_prompt": None,
        "classifier_instruction": None,
        "classifier_reasoning": None,
        "classifier_max_history_messages": None,
        "webhook_enabled": None,
        "webhook_url": None,
        "webhook_timeout_seconds": None,
        "webhook_verify_ssl": None,
        "webhook_secret": None,
        "webhook_secret_header": None,
        "webhook_max_attempts": None,
        "webhook_retry_base_seconds": None,
        "quiet_hours": None,
        "sleep_min_minutes": None,
        "sleep_max_minutes": None,
        "timezone": None,
        "quiet_hours_start": None,
        "quiet_hours_end": None,
        "wake_jitter_seconds": None,
        "incoming_collect_seconds": None,
        "reply_delay_min_seconds": None,
        "reply_delay_max_seconds": None,
        "qa_series_delay_min_seconds": None,
        "qa_series_delay_max_seconds": None,
        "only_invitations": None,
        "dry_run": None,
        "limit": None,
        "resume_id": None,
        "skip_blacklisted": None,
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_load_agent_config_uses_default_classifier_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")

    config = load_agent_config(make_tool(), make_args())

    assert config.classifier is not None
    assert config.classifier.enabled is True
    assert config.classifier.openrouter.model == DEFAULT_CLASSIFIER_MODEL


@pytest.mark.parametrize(
    "effort",
    ["none", "low", "medium", "high", "xhigh", "max"],
)
def test_load_agent_config_uses_generic_openai_env(monkeypatch, effort):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("CHAT_AGENT_CLASSIFIER_MODEL", raising=False)
    monkeypatch.delenv("CHAT_AGENT_CLASSIFIER_REASONING", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "auto/model")
    monkeypatch.setenv("OPENAI_REASONING", effort)

    config = load_agent_config(make_tool(), make_args())

    assert config.openrouter.api_key == "token"
    assert config.openrouter.base_url == "https://proxy.example/v1"
    assert config.openrouter.model == "auto/model"
    assert config.openrouter.reasoning_effort == effort
    assert config.classifier is not None
    assert config.classifier.openrouter.model == "auto/model"
    assert config.classifier.openrouter.reasoning_effort == effort


@pytest.mark.parametrize("value", ["true", "false", "extreme"])
def test_load_agent_config_rejects_invalid_openai_reasoning(monkeypatch, value):
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_REASONING", value)

    with pytest.raises(ValueError, match="OPENAI_REASONING must be one of"):
        load_agent_config(make_tool(), make_args())


def test_load_agent_config_allows_classifier_env_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")
    monkeypatch.setenv("CHAT_AGENT_CLASSIFIER_MODEL", "custom/classifier")

    config = load_agent_config(make_tool(), make_args())

    assert config.classifier is not None
    assert config.classifier.openrouter.model == "custom/classifier"


def test_load_agent_config_cli_disables_classifier_over_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")
    monkeypatch.setenv("CHAT_AGENT_CLASSIFIER_ENABLED", "true")

    config = load_agent_config(make_tool(), make_args(classifier_enabled=False))

    assert config.classifier is not None
    assert config.classifier.enabled is False


def test_load_agent_config_cli_classifier_model_overrides_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")
    monkeypatch.setenv("CHAT_AGENT_CLASSIFIER_MODEL", "custom/classifier")

    config = load_agent_config(
        make_tool(),
        make_args(classifier_model="cli/classifier"),
    )

    assert config.classifier is not None
    assert config.classifier.openrouter.model == "cli/classifier"


def test_load_agent_config_builds_webhook_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")
    monkeypatch.setenv("CHAT_AGENT_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("CHAT_AGENT_WEBHOOK_SECRET", "secret")

    config = load_agent_config(make_tool(), make_args())

    assert config.webhook is not None
    assert config.webhook.url == "https://example.com/hook"
    assert config.webhook.verify_ssl is True
    assert config.webhook.secret == "secret"


def test_load_agent_config_allows_disabling_webhook_ssl_verification(
    monkeypatch,
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")
    monkeypatch.setenv("CHAT_AGENT_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("CHAT_AGENT_WEBHOOK_VERIFY_SSL", "false")

    config = load_agent_config(make_tool(), make_args())

    assert config.webhook is not None
    assert config.webhook.verify_ssl is False


def test_load_agent_config_rejects_enabled_webhook_without_url(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "token")

    try:
        load_agent_config(make_tool(), make_args(webhook_enabled=True))
    except ValueError as ex:
        assert "CHAT_AGENT_WEBHOOK_URL" in str(ex)
    else:
        raise AssertionError(
            "expected ValueError for enabled webhook without url"
        )
