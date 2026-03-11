from datetime import datetime
import sys
import types
from types import SimpleNamespace

import pytest

openai_module = types.ModuleType("openai")
openai_module.OpenAI = object
sys.modules.setdefault("openai", openai_module)

from hh_applicant_tool.operations.chat_agent import Operation
from hh_llm_agent.config import AgentConfig, OpenRouterConfig
from hh_llm_agent.timing import AgentTimingConfig


class FakeTimingPolicy:
    def __init__(self, now, sleep_seconds):
        self._now = now
        self._sleep_seconds = sleep_seconds

    def now(self):
        return self._now

    def in_quiet_hours(self):
        return True

    def quiet_sleep_seconds(self):
        return self._sleep_seconds


def test_daemon_logs_quiet_hours_window(monkeypatch, caplog):
    config = AgentConfig(
        openrouter=OpenRouterConfig(api_key="token"),
        timing=AgentTimingConfig(
            timezone="Europe/Moscow",
            quiet_hours_enabled=True,
            quiet_hours_start="00:00",
            quiet_hours_end="08:00",
            wake_jitter_seconds=300,
        ),
    )
    tool = SimpleNamespace(
        args=SimpleNamespace(profile_id="night", daemon=True),
        save_token=lambda: None,
        save_cookies=lambda: None,
    )
    now = datetime.fromisoformat("2026-03-12T00:28:32+03:00")
    policy = FakeTimingPolicy(now=now, sleep_seconds=60.0)

    monkeypatch.setattr(
        "hh_applicant_tool.operations.chat_agent.load_agent_config",
        lambda tool, args: config,
    )
    monkeypatch.setattr(
        "hh_applicant_tool.operations.chat_agent.TimingPolicy",
        lambda timing: policy,
    )
    monkeypatch.setattr(
        "hh_applicant_tool.operations.chat_agent.time.sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    caplog.set_level("INFO", logger="hh_applicant_tool.operations")

    with pytest.raises(KeyboardInterrupt):
        Operation()._run_daemon(tool)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Starting chat agent daemon:" in message for message in messages)
    assert any("quiet_hours=00:00-08:00" in message for message in messages)
    assert any("Quiet hours active:" in message for message in messages)
    assert any(
        "sleeping_until=2026-03-12 00:29:32 UTC+03:00" in message
        for message in messages
    )
