"""When scheduled chores come due and how long their windows run (§8)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from . import constants as C
from .clock import at_local, local, local_date, next_weekday_at


def daytime(dt: datetime, rng: random.Random) -> datetime:
    """Same local date as `dt`, at a random daytime hour, so chores don't all land at one time."""
    lo, hi = C.DAYTIME_HOURS
    return at_local(local_date(dt), rng.randint(lo, hi - 1), rng.randint(0, 59))


def first_due(chore: str, now: datetime, rng: random.Random) -> datetime:
    if chore in C.BATHROOMS:
        return next_weekday_at(now, C.BATHROOM_WEEKDAY, C.BATHROOM_HOUR)
    if chore == "kitchen":
        return next_weekday_at(now, C.KITCHEN_WEEKDAY, C.KITCHEN_HOUR)
    if chore == "common":
        return daytime(now + timedelta(days=4), rng)
    if chore == "trash":
        return daytime(now + timedelta(days=1), rng)
    if chore == "fridge":
        return daytime(now + timedelta(days=7), rng)
    raise ValueError(chore)


def next_due(chore: str, after: datetime, rng: random.Random) -> datetime:
    """The next regular instance after `after`. For trash and fridge, `after` is the last completion."""
    if chore in C.BATHROOMS:
        return next_weekday_at(after, C.BATHROOM_WEEKDAY, C.BATHROOM_HOUR)
    if chore == "kitchen":
        return next_weekday_at(after, C.KITCHEN_WEEKDAY, C.KITCHEN_HOUR)
    if chore == "common":
        return daytime(after + timedelta(days=rng.randint(*C.COMMON_INTERVAL_DAYS)), rng)
    if chore == "trash":
        return after + C.TRASH_INTERVAL
    if chore == "fridge":
        return after + C.FRIDGE_INTERVAL
    raise ValueError(chore)


def interval(chore: str) -> timedelta:
    if chore == "common":
        return timedelta(days=sum(C.COMMON_INTERVAL_DAYS) / 2)
    if chore == "trash":
        return C.TRASH_INTERVAL
    if chore == "fridge":
        return C.FRIDGE_INTERVAL
    return timedelta(days=7)


def end_of_day(dt: datetime) -> datetime:
    return at_local(local_date(dt), 23, 59)


def window_for(chore: str, source: str, created: datetime) -> tuple[datetime, datetime]:
    """(ask_after, window_end) for a scheduled, re-asked or spun-off task.

    Windows always end at 11:59pm local, so a deadline is a day ("by Tuesday night"), never a clock time.
    """
    if chore == "fridge":
        ask_after = created + C.FRIDGE_HEADS_UP
        return ask_after, end_of_day(ask_after + C.WINDOW["fridge"])
    if chore in C.BATHROOMS and source == "scheduled":
        d = local_date(created)
        while d.weekday() != 6:  # through Sunday night
            d += timedelta(days=1)
        return created, at_local(d, 23, 59)
    return created, end_of_day(created + C.WINDOW[C.recency_family(chore)])


def next_sink_check(after: datetime, rng: random.Random) -> datetime:
    t = after + timedelta(hours=rng.uniform(*C.SINK_INTERVAL_HOURS))
    lo, hi = C.DAYTIME_HOURS
    if not (lo <= local(t).hour < hi):
        t = daytime(t, rng)
    return t
