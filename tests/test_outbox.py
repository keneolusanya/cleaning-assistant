from datetime import timedelta

import pytest

from chorebot import outbox
from chorebot.clock import local, parse

from .conftest import Harness, local as lt


@pytest.fixture
def night():
    h = Harness(lt(2026, 11, 6, 1, 0))  # Friday 1am
    h.quiet_calendar()
    return h


def sent_rows(h, name):
    return h.db.execute(
        "SELECT * FROM outbox WHERE person_id = ? AND status = 'sent' ORDER BY sent_at", (h.pid(name),)
    ).fetchall()


def enqueue(h, name, purpose, **kw):
    with h.eng.tx():
        return outbox.enqueue(h.eng, h.pid(name), purpose, {"chore": "trash"}, **kw)


def test_quiet_hours_hold_then_jittered_morning_in_priority_order(night):
    h = night
    enqueue(h, "Kene", "sink_check")
    enqueue(h, "Kene", "nudge")
    enqueue(h, "Kene", "ask")
    h.tick()
    assert h.sender.sent == []
    h.advance(timedelta(hours=10, minutes=45))  # to 11:45am
    rows = sent_rows(h, "Kene")
    assert [r["purpose"] for r in rows] == ["ask", "nudge", "sink_check"]
    times = [local(parse(r["sent_at"])) for r in rows]
    assert all(t.hour >= 10 for t in times)
    assert times[0] < times[0].replace(hour=10, minute=50)
    assert all(b - a >= timedelta(minutes=10) for a, b in zip(times, times[1:]))


def test_morning_messages_dont_all_fire_at_ten(night):
    h = night
    for name in ("Kene", "Salajan", "Alula", "Yonas", "Schneider"):
        enqueue(h, name, "nudge")
    h.advance(timedelta(hours=10, minutes=50))
    minutes = {local(parse(sent_rows(h, n)[0]["sent_at"])).minute for n in ("Kene", "Salajan", "Alula", "Yonas", "Schneider")}
    assert len(minutes) > 1


def test_cap_rolls_lower_priority_to_tomorrow_and_drops_sink_checks(h):
    h.quiet_calendar()
    for _ in range(3):
        enqueue(h, "Kene", "nudge")
    enqueue(h, "Kene", "fridge_heads_up")
    enqueue(h, "Kene", "sink_check")
    h.advance(timedelta(hours=1))
    assert [r["purpose"] for r in sent_rows(h, "Kene")] == ["nudge", "nudge", "nudge"]
    rows = {r["purpose"]: r for r in h.db.execute("SELECT * FROM outbox WHERE person_id = ?", (h.pid("Kene"),))}
    assert rows["sink_check"]["status"] == "dropped"
    assert rows["fridge_heads_up"]["status"] == "queued" and rows["fridge_heads_up"]["held"] == 1
    h.advance(timedelta(hours=22))  # next day, 1pm
    assert [r["purpose"] for r in sent_rows(h, "Kene")][-1] == "fridge_heads_up"


def test_replies_dont_count_toward_cap(h):
    h.quiet_calendar()
    for _ in range(5):
        h.eng.reply(h.p("Kene"), "sure thing")
    enqueue(h, "Kene", "nudge")
    h.tick()
    assert [r["purpose"] for r in sent_rows(h, "Kene")] == ["nudge"]


def test_recap_is_exempt_from_cap(h):
    h.quiet_calendar()
    for _ in range(3):
        enqueue(h, "Kene", "nudge")
    h.advance(timedelta(hours=1))
    with h.eng.tx():
        outbox.enqueue(h.eng, h.pid("Kene"), "recap", {
            "your_balance": 0, "group_average": 0, "vs_your_share": 0, "you_did": [], "suite_done": {},
            "still_undone": [], "your_no_responses": 0})
    h.advance(timedelta(minutes=15))
    assert sent_rows(h, "Kene")[-1]["purpose"] == "recap"


def test_nothing_goes_to_opted_out_people(h):
    h.quiet_calendar()
    h.eng.opt_out(h.pid("Kene"))
    enqueue(h, "Kene", "nudge")
    h.tick()
    assert h.sent_to("Kene") == []


def test_provider_opt_out_error_syncs_database(h):
    h.quiet_calendar()

    def refuse(phone, body, purpose="", name=""):
        raise outbox.OptedOutError(phone)

    h.sender.send = refuse
    enqueue(h, "Kene", "nudge")
    h.tick()
    assert h.p("Kene")["opted_out_at"] is not None
