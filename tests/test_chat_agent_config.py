from types import SimpleNamespace

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
