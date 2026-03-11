from datetime import datetime

from hh_llm_agent.timing import AgentTimingConfig, TimingPolicy


def test_quiet_hours_cover_night_window():
    config = AgentTimingConfig(
        timezone="Europe/Moscow",
        quiet_hours_enabled=True,
        quiet_hours_start="23:00",
        quiet_hours_end="08:00",
        wake_jitter_seconds=0,
    )
    policy = TimingPolicy(
        config,
        now_fn=lambda tz: datetime.fromisoformat(
            "2026-03-11T23:30:00+03:00"
        ).astimezone(tz),
        uniform_fn=lambda a, b: a,
    )

    assert policy.in_quiet_hours() is True
    assert policy.quiet_sleep_seconds() == 30600.0


def test_cycle_sleep_uses_configured_window():
    config = AgentTimingConfig(
        sleep_min_minutes=20,
        sleep_max_minutes=30,
    )
    policy = TimingPolicy(config, uniform_fn=lambda a, b: b)

    assert policy.cycle_sleep_seconds() == 1800
