from datetime import timedelta

import pytest

from chorebot import fairness
from chorebot.clock import iso
from chorebot.db import kv_get, kv_set, loads


@pytest.fixture(autouse=True)
def _quiet(h):
    h.quiet_calendar()


def ledger(h, name=None):
    q = "SELECT * FROM ledger" + (" WHERE person_id = ?" if name else "") + " ORDER BY id"
    return h.db.execute(q, (h.pid(name),) if name else ()).fetchall()


def active_holder(h, task_id):
    asks = h.eng.active_asks(task_id)
    assert len(asks) == 1
    return h.eng.name(asks[0]["person_id"])


def test_complete_credits_and_closes(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    res = h.tool("Kene", "complete_task", task_id=t["id"])
    assert res["ok"] and res["points"] == 1.0 and not res["covered_bonus"]
    assert h.eng.task(t["id"])["status"] == "done"
    assert h.asks(t["id"])[0]["status"] == "closed"
    assert ledger(h, "Kene")[0]["reason"] == "done"


def test_unknown_or_done_task_is_rejected(h):
    assert h.tool("Kene", "complete_task", task_id=999)["error"] == "unknown_task"
    t = h.task("trash")
    h.tool("Kene", "complete_task", task_id=t["id"])
    res = h.tool("Salajan", "complete_task", task_id=t["id"])
    assert res["error"] == "already_done" and res["done_by"] == "Kene"


def test_bad_arguments_are_rejected_not_raised(h):
    t = h.task("trash")
    assert h.tool("Kene", "complete_task", task_id=t["id"], bogus=1)["error"] == "bad_arguments"


def test_cover_bonus_after_decline(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    res = h.tool("Kene", "decline_task", task_id=t["id"], reason="busy")
    nxt = res["next_asked"]
    assert nxt and nxt != "Kene"
    res = h.tool(nxt, "complete_task", task_id=t["id"])
    assert res["points"] == 1.25 and res["covered_bonus"]
    assert ledger(h, nxt)[0]["reason"] == "covered"


def test_no_cover_bonus_after_pause_handoff(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    res = h.tool("Kene", "pause", until=iso(h.clock.now() + timedelta(days=3)))
    assert res["handed_off"] == ["trash"]
    nxt = active_holder(h, t["id"])
    assert nxt != "Kene"
    assert h.tool(nxt, "complete_task", task_id=t["id"])["points"] == 1.0


def test_declining_is_free(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    h.tool("Kene", "decline_task", task_id=t["id"])
    assert fairness.balance(h.db, h.pid("Kene")) == 0
    assert ledger(h, "Kene") == []


def test_bathroom_needs_parts(h):
    t = h.task("bathroom_1")
    h.ask(t, "Kene")
    res = h.tool("Kene", "complete_task", task_id=t["id"])
    assert res["error"] == "parts_required"
    assert res["parts_remaining"] == ["toilet", "sink_mirror", "shower", "floor"]


def test_bathroom_partial_spins_off_the_rest(h):
    t = h.task("bathroom_1")
    h.ask(t, "Kene")
    res = h.tool("Kene", "complete_task", task_id=t["id"], parts=["toilet", "sink_mirror"])
    assert res["points"] == 1.5 and res["parts_left"] == ["shower", "floor"]
    spin = h.db.execute("SELECT * FROM tasks WHERE source = 'spinoff'").fetchone()
    assert loads(spin["parts"]) == ["shower", "floor"]
    statuses = {h.eng.name(a["person_id"]): a["status"] for a in h.asks(spin["id"])}
    assert statuses["Kene"] == "excluded"
    assert res["rest_asked_to"] and res["rest_asked_to"] != "Kene"
    # Kene can't be re-asked about the rest, even in a new round.
    assert "Kene" not in [p["name"] for p in fairness.rank(h.db, spin, h.clock.now(), h.eng.rng)]


def test_bathroom_parts_must_still_be_open(h):
    t = h.task("bathroom_1")
    h.ask(t, "Kene")
    h.tool("Kene", "complete_task", task_id=t["id"], parts=["toilet"])
    spin = h.db.execute("SELECT * FROM tasks WHERE source = 'spinoff'").fetchone()
    other = active_holder(h, spin["id"])
    assert h.tool(other, "complete_task", task_id=spin["id"], parts=["toilet"])["error"] == "parts_not_remaining"


def test_snooze_out_of_quiet_hours_and_reminds(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    two_am = h.clock.now().replace(hour=7, minute=0) + timedelta(days=1)  # 07:00 UTC = 2am Boston
    res = h.tool("Kene", "snooze_task", task_id=t["id"], remind_at=iso(two_am))
    assert res["ok"] and res["remind_at"] == "tomorrow at 10am"
    h.advance(timedelta(hours=21))
    assert len(h.sent_to("Kene", "snooze_reminder")) == 1


def test_snooze_past_deadline_warns(h):
    with h.eng.tx():
        t = h.eng.create_task("trash", "scheduled", window_end=h.clock.now() + timedelta(hours=5))
    h.ask(t, "Kene")
    res = h.tool("Kene", "snooze_task", task_id=t["id"], remind_at=iso(h.clock.now() + timedelta(days=1)))
    assert res["past_deadline"]


def test_accept_gets_one_checkin(h):
    with h.eng.tx():
        t = h.eng.create_task("kitchen", "scheduled", window_end=h.clock.now() + timedelta(days=3))
    h.ask(t, "Kene")
    assert h.tool("Kene", "accept_task", task_id=t["id"])["ok"]
    h.advance(timedelta(days=2), step=timedelta(minutes=30))
    assert len(h.sent_to("Kene", "checkin")) == 1
    assert h.sent_to("Kene", "nudge") == []


def test_volunteering_takes_the_task(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    assert h.tool("Salajan", "accept_task", task_id=t["id"])["ok"]
    assert active_holder(h, t["id"]) == "Salajan"
    assert h.tool("Yonas", "accept_task", task_id=t["id"])["error"] == "already_taken"


def test_claim_past_work_closes_open_task_and_flags_it(h):
    t = h.task("kitchen")
    h.ask(t, "Kene")
    res = h.tool("Yonas", "claim_past_work", chore="kitchen", note="did it yesterday")
    assert res["ok"] and res["closed_open_task"]
    row = ledger(h, "Yonas")[0]
    assert row["retroactive"] == 1 and row["points"] == 2.0
    assert h.eng.task(t["id"])["status"] == "done"


def test_claim_dishes_needs_scope(h):
    assert h.tool("Kene", "claim_past_work", chore="dishes")["error"] == "dishes_scope_required"
    assert h.tool("Kene", "claim_past_work", chore="dishes", dishes="others")["points"] == 0.75


def test_claim_bathroom_without_open_task_needs_parts(h):
    assert h.tool("Kene", "claim_past_work", chore="bathroom_2")["error"] == "parts_required"
    res = h.tool("Kene", "claim_past_work", chore="bathroom_2", parts=["shower"])
    assert res["points"] == 0.75


def test_reporter_furthest_behind_is_asked_inline(h):
    for name in ("Kene", "Salajan", "Yonas", "Schneider", "Jones", "Allahji"):
        with h.eng.tx():
            fairness.credit(h.db, h.clock.now(), h.pid(name), None, "trash", 1.0, "done")
    res = h.tool("Alula", "report_needed", chore="trash", note="overflowing")
    assert res["asked_you"]
    queued = h.db.execute("SELECT * FROM outbox WHERE purpose = 'ask'").fetchall()
    assert queued == []  # the ask is in the reply, not a separate text


def test_reporter_not_behind_doesnt_get_it(h):
    with h.eng.tx():
        fairness.credit(h.db, h.clock.now(), h.pid("Alula"), None, "trash", 5.0, "done")
    res = h.tool("Alula", "report_needed", chore="trash")
    assert not res["asked_you"] and res["asked"] != "Alula"


def test_duplicate_report_is_noted(h):
    first = h.tool("Kene", "report_needed", chore="trash")
    second = h.tool("Jones", "report_needed", chore="trash", note="still full")
    assert second["already_open"] and second["task_id"] == first["task_id"]
    assert "Jones" in h.eng.task(first["task_id"])["note"]


def test_fridge_report_heads_up_then_ask_a_day_later(h):
    res = h.tool("Kene", "report_needed", chore="fridge")
    assert res["fridge_heads_up_sent"] and res["asked"] is None
    h.tick()
    assert len([m for m in h.sender.sent if m["purpose"] == "fridge_heads_up"]) == 6
    h.advance(timedelta(hours=23))
    assert [m for m in h.sender.sent if m["purpose"] == "ask"] == []
    h.advance(timedelta(hours=2))
    assert len([m for m in h.sender.sent if m["purpose"] == "ask"]) == 1


def test_report_done_by_other_checks_with_assignee(h):
    t = h.task("trash")
    h.ask(t, "Kene")
    res = h.tool("Jones", "report_done_by_other", chore="trash")
    assert res["checking_with"] == ["Kene"]
    res = h.tool("Jones", "report_done_by_other", chore="trash", person_name="Alula")
    assert res["checking_with"] == ["Alula"]
    assert h.tool("Jones", "report_done_by_other", chore="kitchen")["error"] == "no_open_task"


def test_reported_task_starts_new_round_daily_when_everyone_declines(h):
    t = h.task("trash")
    with h.eng.tx():
        h.eng.ensure_asked(t["id"])
    for _ in range(7):
        holder = active_holder(h, t["id"])
        h.tool(holder, "decline_task", task_id=t["id"])
    assert h.eng.active_asks(t["id"]) == []
    h.advance(timedelta(hours=23))
    assert h.eng.active_asks(t["id"]) == []
    h.advance(timedelta(hours=2))
    assert len(h.eng.active_asks(t["id"])) == 1
    assert h.eng.task(t["id"])["round"] == 2


def test_pause_ends_automatically_and_unpause_works(h):
    h.tool("Kene", "pause", until=iso(h.clock.now() + timedelta(days=1)))
    assert not h.eng.is_active(h.p("Kene"))
    h.advance(timedelta(days=1, minutes=10), step=timedelta(hours=1))
    assert h.eng.is_active(h.p("Kene"))
    h.tool("Kene", "pause", until=iso(h.clock.now() + timedelta(days=5)))
    assert h.tool("Kene", "unpause")["was_paused"]
    assert h.eng.is_active(h.p("Kene"))


def test_trash_comes_due_three_days_after_completion(h):
    t = h.task("trash")
    h.tool("Kene", "complete_task", task_id=t["id"])
    due = kv_get(h.db, "due:trash")
    assert due == iso(h.clock.now() + timedelta(days=3))


def test_early_completion_skips_a_too_close_instance(h):
    from chorebot.clock import local, parse

    kv_set(h.db, "due:kitchen", iso(h.clock.now() + timedelta(days=1)))
    t = h.task("kitchen")
    h.tool("Kene", "complete_task", task_id=t["id"])
    due = parse(kv_get(h.db, "due:kitchen"))
    assert due > h.clock.now() + timedelta(days=4)
    assert local(due).strftime("%a %H:%M") == "Wed 11:00"
