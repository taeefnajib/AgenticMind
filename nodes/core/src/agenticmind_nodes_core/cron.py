"""A small cron expression parser (5 fields: minute hour day-of-month month day-of-week).

Supports ``*``, numbers, lists (``1,15``), ranges (``1-5``), steps (``*/15``, ``10-50/10``), month and weekday
names (``jan``, ``mon``), ``7`` as Sunday, and the standard rule that when both day fields are restricted a
day matches if *either* does. Times are evaluated in a given time zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}  # fmt: skip


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    values: set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"bad step in {text!r}")
            step = int(step_text)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names), _value(b, names)
        else:
            start = end = _value(part, names)
            if step > 1:
                end = hi
        if start > end or start < lo or end > hi:
            raise ValueError(f"{text!r} is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _value(token: str, names: dict[str, int] | None) -> int:
    if names and token in names:
        return names[token]
    if not token.isdigit():
        raise ValueError(f"not a number: {token!r}")
    return int(token)


@dataclass(frozen=True)
class CronExpression:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, text: str) -> CronExpression:
        text = _ALIASES.get(text.strip().lower(), text.strip())
        parts = text.split()
        if len(parts) != 5:
            raise ValueError("a cron expression has 5 fields: minute hour day-of-month month day-of-week")
        weekdays = _field(parts[4], 0, 7, _DAYS)
        return cls(
            minutes=_field(parts[0], 0, 59),
            hours=_field(parts[1], 0, 23),
            days=_field(parts[2], 1, 31),
            months=_field(parts[3], 1, 12, _MONTHS),
            weekdays=frozenset(0 if d == 7 else d for d in weekdays),
            day_restricted=parts[2] not in ("*", "?"),
            weekday_restricted=parts[4] not in ("*", "?"),
        )

    def _day_matches(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.isoweekday() % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, after: datetime, tz: str = "UTC") -> datetime:
        """The first matching minute strictly after ``after`` (an aware datetime), returned in UTC-aware form."""
        zone = ZoneInfo(tz)
        t = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60 * 5):  # bounded search (5 years of minutes, never reached in practice)
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t.astimezone(ZoneInfo("UTC"))
        raise ValueError("the cron expression never matches")
