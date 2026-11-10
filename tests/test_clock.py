from datetime import timedelta

import pytest

from chorebot import cadence, outbox, scheduler
from chorebot.clock import iso, parse, say
from chorebot.db import kv_set

from .conftest import Harness, local

NOW = local(2026, 11, 5, 14, 0)  # Thursday 2pm


@pytest.mark.parametrize(
    "when, expected",
    [
        ((2026, 11, 5, 23, 59), "tonight"),
        ((2026, 11, 5, 16, 0), "today at 4pm"),
        ((2026, 11, 5, 20, 30), "tonight at 8:30pm"),
        ((2026, 11, 6, 12, 0), "tomorrow at noon"),
        ((2026, 11, 6, 23, 59), "tomorrow night"),
        ((2026, 11, 8, 23, 59), "this Sunday night"),
        ((2026, 11, 8, 14, 0), "this Sunday at 2pm"),
        ((2026, 11, 9, 14, 0), "Monday at 2pm"),
        ((2026, 11, 15, 14, 0), "next Sunday at 2pm"),
        ((2026, 11, 30, 14, 0), "November 30"),
        ((2026, 11, 4, 14, 0), "yesterday at 2pm"),
        ((2026, 11, 3, 14, 0), "Tuesday at 2pm"),
    ],
)
def test_say(when, expected):
    assert say(local(*when), NOW) == expected


@pytest.mark.parametrize(
    "when, expected",
    [
        ((2026, 11, 5, 16, 0), "today"),
        ((2026, 11, 5, 23, 59), "tonight"),
        ((2026, 11, 6, 14, 0), "tomorrow"),
        ((2026, 11, 8, 14, 0), "this Sunday"),
        ((2026, 11, 4, 11, 0), "yesterday"),
    ],
)
def test_say_day_only(when, expected):
    assert say(local(*when), NOW, day_only=True) == expected


def test_deadlines_end_at_end_of_day():
    _, end = cadence.window_for("trash", "reported", local(2026, 11, 8, 14, 30))  # Sunday 2:30pm, 2-day window
    assert end == local(2026, 11, 10, 23, 59)
    assert say(end, local(2026, 11, 8, 14, 30)) == "Tuesday night"
    ask_after, end = cadence.window_for("fridge", "reported", local(2026, 11, 5, 14, 0))
    assert end == local(2026, 11, 9, 23, 59)


def test_sink_check_texts_are_spread_out():
    h = Harness(NOW)
    h.quiet_calendar()
    kv_set(h.db, "due:sink_check", iso(NOW))
    with h.eng.tx():
        scheduler._sink_checks(h.eng)
    times = [parse(r["send_after"]) for r in h.db.execute("SELECT send_after FROM outbox WHERE purpose = 'sink_check'")]
    assert len(times) == 7 and len(set(times)) == 7
    assert all(NOW <= t <= NOW + timedelta(minutes=60) for t in times)
    h.advance(timedelta(hours=1))
    assert len(h.db.execute("SELECT * FROM outbox WHERE purpose = 'sink_check' AND status = 'sent'").fetchall()) == 7


def test_held_message_says_the_time_as_of_delivery():
    h = Harness(local(2026, 11, 5, 23, 30))
    h.quiet_calendar()
    facts = {"chore": "trash", "deadline": iso(local(2026, 11, 6, 23, 59))}  # "tomorrow night" when queued
    with h.eng.tx():
        outbox.enqueue(h.eng, h.pid("Kene"), "ask", facts, send_after=local(2026, 11, 6, 1, 0))
    h.advance(timedelta(hours=13))
    body = h.db.execute("SELECT body FROM outbox WHERE status = 'sent'").fetchone()["body"]
    assert "by tonight" in body
