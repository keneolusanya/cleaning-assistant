"""Time handling. Everything is stored in UTC; all scheduling decisions use Boston local time."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
TZ = ZoneInfo("America/New_York")


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class FakeClock(Clock):
    """Used by the simulator and tests."""

    def __init__(self, start: datetime):
        self._now = start.astimezone(UTC).replace(microsecond=0)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, dt: datetime) -> None:
        self._now = dt.astimezone(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(s: str | None) -> datetime | None:
    """Parse an ISO string. Naive values are taken as Boston local time."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(UTC)


def local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def at_local(d: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time on a local date, as UTC. Handles DST."""
    return datetime.combine(d, time(hour, minute), tzinfo=TZ).astimezone(UTC)


def local_date(dt: datetime) -> date:
    return local(dt).date()


def fmt(dt: datetime | None) -> str | None:
    """Human-friendly local time, e.g. 'Sat 11/7 11:00am'."""
    if dt is None:
        return None
    l = local(dt)
    hour = l.strftime("%I").lstrip("0")
    return f"{l.strftime('%a')} {l.month}/{l.day} {hour}:{l.strftime('%M')}{l.strftime('%p').lower()}"


def _time_of_day(l: datetime) -> str:
    if (l.hour, l.minute) == (12, 0):
        return "noon"
    minutes = f":{l.minute:02d}" if l.minute else ""
    return f"{l.strftime('%I').lstrip('0')}{minutes}{l.strftime('%p').lower()}"


def say(dt: datetime | None, now: datetime, day_only: bool = False) -> str | None:
    """A time the way you'd text it, e.g. 'tonight', 'tomorrow at 2pm', 'this Sunday night'.

    11:59pm reads as the night of that day. `day_only` drops the clock time ('tomorrow').
    """
    if dt is None:
        return None
    l = local(dt)
    today = local_date(now)
    days = (l.date() - today).days
    end_of_day = (l.hour, l.minute) == (23, 59)
    if days >= 14 or days <= -7:
        return f"{l.strftime('%B')} {l.day}"
    if days == 0:
        day = "tonight" if end_of_day or l.hour >= 18 else "today"
        if end_of_day:
            return day
    elif days == 1:
        day = "tomorrow"
    elif days == -1:
        day = "yesterday"
    else:
        if days >= 7:
            prefix = "next "
        elif days > 0 and l.weekday() > today.weekday():
            prefix = "this "
        else:
            prefix = ""
        day = f"{prefix}{l.strftime('%A')}"
    if day_only:
        return day
    if end_of_day:
        return "last night" if days == -1 else f"{day} night"
    return f"{day} at {_time_of_day(l)}"


# Times stored in outbox facts. True = say only the day: these aren't cutoffs, so the hour is noise.
TIME_FACTS = {"deadline": False, "clear_out_from": True, "asked_at": True, "paused_until": False}


def speak_facts(facts: dict, now: datetime) -> dict:
    """Turn stored ISO times into phrases. Call when writing the message, since a queued text can go out days later."""
    return {k: say(parse(v), now, day_only=TIME_FACTS[k]) if k in TIME_FACTS else v for k, v in facts.items()}


def next_weekday_at(after: datetime, weekday: int, hour: int, minute: int = 0) -> datetime:
    """The first local `weekday` (Mon=0) at hour:minute strictly after `after`."""
    d = local_date(after)
    for i in range(15):
        day = d + timedelta(days=i)
        if day.weekday() == weekday:
            cand = at_local(day, hour, minute)
            if cand > after:
                return cand
    raise AssertionError("unreachable")
