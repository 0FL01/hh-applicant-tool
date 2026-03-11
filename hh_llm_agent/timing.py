from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":", 1)
    return time(hour=int(hours), minute=int(minutes))


@dataclass(frozen=True)
class AgentTimingConfig:
    sleep_min_minutes: int = 20
    sleep_max_minutes: int = 30
    timezone: str = "Europe/Moscow"
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    wake_jitter_seconds: int = 300
    incoming_collect_seconds: int = 120
    reply_delay_min_seconds: int = 15
    reply_delay_max_seconds: int = 60
    qa_series_delay_min_seconds: int = 30
    qa_series_delay_max_seconds: int = 120


class TimingPolicy:
    def __init__(
        self,
        config: AgentTimingConfig,
        *,
        now_fn=None,
        uniform_fn=None,
    ):
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.now_fn = now_fn or (lambda tz: datetime.now(tz))
        self.uniform_fn = uniform_fn or random.uniform

    def now(self) -> datetime:
        current = self.now_fn(self.tz)
        if current.tzinfo is None:
            return current.replace(tzinfo=self.tz)
        return current.astimezone(self.tz)

    def in_quiet_hours(self, current: datetime | None = None) -> bool:
        if not self.config.quiet_hours_enabled:
            return False
        current = current or self.now()
        start = parse_hhmm(self.config.quiet_hours_start)
        end = parse_hhmm(self.config.quiet_hours_end)
        now_time = current.timetz().replace(tzinfo=None)
        if start <= end:
            return start <= now_time < end
        return now_time >= start or now_time < end

    def next_quiet_hours_end(self, current: datetime | None = None) -> datetime:
        current = current or self.now()
        end = parse_hhmm(self.config.quiet_hours_end)
        wake_at = current.replace(
            hour=end.hour,
            minute=end.minute,
            second=0,
            microsecond=0,
        )
        if wake_at <= current:
            wake_at += timedelta(days=1)
        return wake_at

    def quiet_sleep_seconds(self, current: datetime | None = None) -> float:
        current = current or self.now()
        wake_at = self.next_quiet_hours_end(current)
        jitter = max(0.0, self.uniform_fn(0, self.config.wake_jitter_seconds))
        return max(0.0, (wake_at - current).total_seconds() + jitter)

    def cycle_sleep_seconds(self) -> float:
        lower = min(
            self.config.sleep_min_minutes,
            self.config.sleep_max_minutes,
        )
        upper = max(
            self.config.sleep_min_minutes,
            self.config.sleep_max_minutes,
        )
        return max(1.0, self.uniform_fn(lower * 60, upper * 60))

    def incoming_collect_delay(self, message_time: datetime) -> float:
        age = (self.now() - message_time.astimezone(self.tz)).total_seconds()
        return max(0.0, self.config.incoming_collect_seconds - age)

    def message_schedule_delays(self, count: int) -> list[float]:
        if count <= 0:
            return []
        delays = [
            self.uniform_fn(
                self.config.reply_delay_min_seconds,
                self.config.reply_delay_max_seconds,
            )
        ]
        for _ in range(count - 1):
            delays.append(
                self.uniform_fn(
                    self.config.qa_series_delay_min_seconds,
                    self.config.qa_series_delay_max_seconds,
                )
            )
        return delays
