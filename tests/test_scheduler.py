from datetime import timedelta

from chorebot.clock import fmt, iso, local, parse
from chorebot.db import kv_get, kv_set
from chorebot.scheduler import recap_facts



def test_nudge_ladder_then_no_response_moves_on(h):
    h.quiet_calendar()
    t = h.task("trash")
    with h.eng.tx():
        first = h.eng.ensure_asked(t["id"])
    name = h.eng.name(first["person_id"])
    h.tick()  # Thu 2pm: ask goes out
    assert len(h.sent_to(name, "ask")) == 1
    h.advance(timedelta(hours=4, minutes=5))  # 6pm: first nudge
    assert len(h.sent_to(name, "nudge")) == 1
    h.advance(timedelta(hours=17))  # Fri 11am: second nudge, next morning
    assert len(h.sent_to(name, "nudge")) == 2
    h.advance(timedelta(hours=6, minutes=30))
    ask = h.db.execute("SELECT * FROM asks WHERE id = ?", (first["id"],)).fetchone()
    assert ask["status"] == "no_response"
    assert h.p(name)["no_response_streak"] == 1
    holders = h.eng.active_asks(t["id"])
    assert len(holders) == 1 and holders[0]["person_id"] != first["person_id"]


def test_window_expiry_marks_unresolved_and_reasks(h):
    h.quiet_calendar()
    with h.eng.tx():
        t = h.eng.create_task("kitchen", "scheduled", ask_after=h.clock.now() + timedelta(hours=2),
                              window_end=h.clock.now() + timedelta(hours=1))
    h.advance(timedelta(hours=1, minutes=5))
    assert h.eng.task(t["id"])["status"] == "unresolved"
    assert kv_get(h.db, "reask:kitchen") is not None
    h.advance(timedelta(days=3), step=timedelta(minutes=30))
    reask = h.db.execute("SELECT * FROM tasks WHERE source = 'reask'").fetchone()
    assert reask and reask["chore"] == "kitchen"
    assert h.eng.active_asks(reask["id"])  # everyone eligible again


def test_no_reask_when_next_regular_instance_is_sooner(h):
    h.quiet_calendar()
    kv_set(h.db, "due:kitchen", iso(h.clock.now() + timedelta(days=2)))
    with h.eng.tx():
        h.eng.create_task("kitchen", "scheduled", ask_after=h.clock.now() + timedelta(hours=2),
                          window_end=h.clock.now() + timedelta(hours=1))
    h.advance(timedelta(hours=1, minutes=5))
    assert kv_get(h.db, "reask:kitchen") is None


def test_bathrooms_scheduled_saturday_through_sunday_night(h):
    h.advance(timedelta(days=2), step=timedelta(minutes=30))  # Thu 2pm -> Sat 2pm
    rows = h.db.execute("SELECT * FROM tasks WHERE chore LIKE 'bathroom%' ORDER BY chore").fetchall()
    assert [r["chore"] for r in rows] == ["bathroom_1", "bathroom_2"]
    for r in rows:
        assert local(parse(r["created_at"])).strftime("%a %H") == "Sat 11"
        assert fmt(parse(r["window_end"])) == "Sun 11/8 11:59pm"
    holders = {h.eng.active_asks(r["id"])[0]["person_id"] for r in rows}
    assert len(holders) == 2


def test_sink_check_goes_to_everyone_without_nudges(h):
    h.quiet_calendar()
    kv_set(h.db, "due:sink_check", iso(h.clock.now()))
    h.advance(timedelta(hours=1))  # texts are spread over up to an hour
    assert len([m for m in h.sender.sent if m["purpose"] == "sink_check"]) == 7
    task = h.db.execute("SELECT * FROM tasks WHERE source = 'sink_check'").fetchone()
    assert h.tool("Kene", "resolve_sink_check", task_id=task["id"], outcome="washed_mine")["points"] == 0.25
    assert h.tool("Salajan", "resolve_sink_check", task_id=task["id"], outcome="none_mine")["points"] == 0
    h.advance(timedelta(hours=13))
    assert h.eng.task(task["id"])["status"] == "done"
    assert [m for m in h.sender.sent if m["purpose"] == "nudge"] == []
    assert all(p["no_response_streak"] == 0 for p in h.db.execute("SELECT * FROM people"))
    nxt = parse(kv_get(h.db, "due:sink_check"))
    assert timedelta(days=3) - timedelta(hours=9) <= nxt - h.clock.now() + timedelta(hours=14) <= timedelta(days=4, hours=9)


def test_sink_check_skipped_if_someone_washed_everything_recently(h):
    h.quiet_calendar()
    h.tool("Kene", "claim_past_work", chore="dishes", dishes="others")
    kv_set(h.db, "due:sink_check", iso(h.clock.now()))
    h.tick()
    assert [m for m in h.sender.sent if m["purpose"] == "sink_check"] == []
    assert parse(kv_get(h.db, "due:sink_check")) > h.clock.now() + timedelta(days=2)


def test_inactivity_check_once_per_two_weeks(h):
    h.quiet_calendar()
    h.db.execute("UPDATE people SET no_response_streak = 3 WHERE name = 'Jones'")
    h.tick()
    assert len(h.sent_to("Jones", "inactivity_check")) == 1
    h.advance(timedelta(days=13), step=timedelta(hours=1))
    assert len(h.sent_to("Jones", "inactivity_check")) == 1
    h.advance(timedelta(days=2), step=timedelta(hours=1))
    assert len(h.sent_to("Jones", "inactivity_check")) == 2
    h.db.execute("UPDATE people SET no_response_streak = 0 WHERE name = 'Jones'")


def test_inactivity_check_skips_paused(h):
    h.quiet_calendar()
    h.tool("Jones", "pause", until=iso(h.clock.now() + timedelta(days=5)))
    h.db.execute("UPDATE people SET no_response_streak = 3 WHERE name = 'Jones'")
    h.tick()
    assert h.sent_to("Jones", "inactivity_check") == []


def test_weekly_recap_sunday_evening_once(h):
    h.quiet_calendar()
    h.tool("Kene", "claim_past_work", chore="trash")
    h.advance(timedelta(days=3, hours=5, minutes=10), step=timedelta(minutes=30))  # Sun 7:10pm
    assert len([m for m in h.sender.sent if m["purpose"] == "recap"]) == 7
    h.tick()
    h.advance(timedelta(hours=2))
    assert len([m for m in h.sender.sent if m["purpose"] == "recap"]) == 7


def test_recap_only_has_your_own_numbers(h):
    h.quiet_calendar()
    h.tool("Kene", "claim_past_work", chore="trash")
    h.tool("Alula", "claim_past_work", chore="kitchen")
    facts = recap_facts(h.eng, h.p("Kene"))
    assert facts["your_balance"] == 1.0
    assert facts["you_did"] == [{"chore": "trash", "points": 1.0, "claimed_after_the_fact": True}]
    assert facts["suite_done"] == {"kitchen reset": 1, "trash": 1}
    text = str(facts)
    assert "Alula" not in text and "2.0" not in text.replace("group_average", "")


def test_tick_is_idempotent(h):
    h.quiet_calendar()
    t = h.task("trash")
    with h.eng.tx():
        h.eng.ensure_asked(t["id"])
    h.tick()
    n = len(h.sender.sent)
    h.tick()
    h.tick()
    assert len(h.sender.sent) == n


def test_first_scheduled_dates_land_in_daytime(h):
    h.tick()
    for chore in ("trash", "common", "fridge", "kitchen", "bathroom_1"):
        due = local(parse(kv_get(h.db, f"due:{chore}")))
        assert 10 <= due.hour < 20, chore
