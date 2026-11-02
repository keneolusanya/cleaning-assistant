"""The periodic tick. Idempotent: running it twice in a row does nothing the second time (§12)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import cadence
from . import constants as C
from . import fairness, outbox
from .clock import fmt, iso, local, local_date, parse
from .db import kv_get, kv_set

if TYPE_CHECKING:
    from .engine import Engine


def tick(eng: "Engine") -> None:
    with eng.tx():
        _end_pauses(eng)
        _expire_windows(eng)
        _create_scheduled(eng)
        _start_due_tasks(eng)
        _snoozes(eng)
        _nudges(eng)
        _checkins(eng)
        _sink_checks(eng)
        _inactivity(eng)
        _recap(eng)
    with eng.tx():
        outbox.flush(eng)


def _end_pauses(eng: "Engine") -> None:
    eng.db.execute(
        "UPDATE people SET paused_from = NULL, paused_until = NULL WHERE paused_until IS NOT NULL AND paused_until <= ?",
        (iso(eng.now),),
    )


def _expire_windows(eng: "Engine") -> None:
    for task in eng.db.execute(
        "SELECT * FROM tasks WHERE status = 'open' AND window_end IS NOT NULL AND window_end <= ?", (iso(eng.now),)
    ).fetchall():
        eng.expire(task)


def _create_scheduled(eng: "Engine") -> None:
    now = eng.now
    for chore in C.SCHEDULED_CHORES:
        due = parse(kv_get(eng.db, f"due:{chore}"))
        if due is None:
            kv_set(eng.db, f"due:{chore}", iso(cadence.first_due(chore, now, eng.rng)))
            continue
        reask = parse(kv_get(eng.db, f"reask:{chore}"))
        if eng.open_task_for(chore):
            if due <= now:  # this instance is already covered by an open task
                kv_set(eng.db, f"due:{chore}", iso(cadence.next_due(chore, now, eng.rng)))
            continue
        if reask and reask <= now:
            source = "reask"
        elif due <= now:
            source = "scheduled"
            kv_set(eng.db, f"due:{chore}", iso(cadence.next_due(chore, now, eng.rng)))
        else:
            continue
        kv_set(eng.db, f"reask:{chore}", None)
        ask_after, window_end = cadence.window_for(chore, source, now)
        task = eng.create_task(chore, source, ask_after=ask_after, window_end=window_end)
        if chore == "fridge":
            eng._fridge_heads_up(task)
        eng.ensure_asked(task["id"])


def _start_due_tasks(eng: "Engine") -> None:
    """Asks for tasks that just passed ask_after, whose asker dropped off, or that are due a new round."""
    for task in eng.db.execute(
        "SELECT id FROM tasks WHERE status = 'open' AND source != 'sink_check' AND ask_after <= ?", (iso(eng.now),)
    ).fetchall():
        eng.ensure_asked(task["id"])


def _snoozes(eng: "Engine") -> None:
    for a in eng.db.execute(
        "SELECT a.* FROM asks a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.status = 'snoozed' AND a.remind_at <= ? AND t.status = 'open'",
        (iso(eng.now),),
    ).fetchall():
        eng.db.execute("UPDATE asks SET status = 'pending', remind_at = NULL WHERE id = ?", (a["id"],))
        task = eng.task(a["task_id"])
        outbox.enqueue(eng, a["person_id"], "snooze_reminder", eng.task_facts(task), ask_id=a["id"], task_id=task["id"])


def _nudges(eng: "Engine") -> None:
    for a in eng.db.execute(
        "SELECT a.* FROM asks a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.status = 'pending' AND a.next_nudge_at IS NOT NULL AND a.next_nudge_at <= ? AND t.status = 'open'",
        (iso(eng.now),),
    ).fetchall():
        if a["nudges"] < 2:
            eng.db.execute("UPDATE asks SET next_nudge_at = NULL WHERE id = ?", (a["id"],))
            task = eng.task(a["task_id"])
            facts = {**eng.task_facts(task), "nudge_number": a["nudges"] + 1, "asked_at": fmt(parse(a["sent_at"]))}
            outbox.enqueue(eng, a["person_id"], "nudge", facts, ask_id=a["id"], task_id=task["id"])
        else:
            eng.close_ask(a["id"], "no_response", "nudges ran out")
            eng.db.execute("UPDATE people SET no_response_streak = no_response_streak + 1 WHERE id = ?",
                           (a["person_id"],))
            eng.ensure_asked(a["task_id"])


def _checkins(eng: "Engine") -> None:
    now = eng.now
    for a in eng.db.execute(
        "SELECT a.* FROM asks a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.status = 'accepted' AND a.checkin_at IS NOT NULL AND a.checkin_at <= ? AND t.status = 'open'",
        (iso(now),),
    ).fetchall():
        task = eng.task(a["task_id"])
        lapse = iso(now + C.ACCEPTED_LAPSE_AFTER_CHECKIN) if task["window_end"] is None else None
        eng.db.execute("UPDATE asks SET checkin_at = NULL, lapse_at = ? WHERE id = ?", (lapse, a["id"]))
        outbox.enqueue(eng, a["person_id"], "checkin", eng.task_facts(task), ask_id=a["id"], task_id=task["id"])
    # Reported tasks have no window, so an accepted-but-not-done ask eventually lets go.
    for a in eng.db.execute(
        "SELECT * FROM asks WHERE status = 'accepted' AND lapse_at IS NOT NULL AND lapse_at <= ?", (iso(now),)
    ).fetchall():
        eng.close_ask(a["id"], "no_response", "accepted, not done")
        eng.ensure_asked(a["task_id"])


def _sink_checks(eng: "Engine") -> None:
    now = eng.now
    due = parse(kv_get(eng.db, "due:sink_check"))
    if due is None:
        kv_set(eng.db, "due:sink_check", iso(cadence.daytime(now + C.D, eng.rng)))
        return
    if due > now:
        return
    kv_set(eng.db, "due:sink_check", iso(cadence.next_sink_check(now, eng.rng)))
    washed = eng.db.execute(
        "SELECT 1 FROM ledger WHERE chore = 'dishes' AND points >= ? AND status = 'confirmed' AND created_at >= ? LIMIT 1",
        (C.DISHES_OTHERS, iso(now - C.SINK_SKIP_IF_WASHED_WITHIN)),
    ).fetchone()
    if washed:
        return
    task = eng.create_task("dishes", "sink_check", window_end=now + C.SINK_CHECK_WINDOW, note="sink_check")
    for p in fairness.active_people(eng.db, now):
        cur = eng.db.execute(
            "INSERT INTO asks(task_id, person_id, round, status, created_at) VALUES (?, ?, 1, 'pending', ?)",
            (task["id"], p["id"], iso(now)),
        )
        outbox.enqueue(eng, p["id"], "sink_check", {"task_id": task["id"]}, ask_id=cur.lastrowid, task_id=task["id"])


def _inactivity(eng: "Engine") -> None:
    now = eng.now
    for p in fairness.active_people(eng.db, now):
        if p["no_response_streak"] < C.INACTIVITY_STREAK:
            continue
        last = parse(p["last_inactivity_check_at"])
        if last and now - last < C.INACTIVITY_COOLDOWN:
            continue
        eng.db.execute("UPDATE people SET last_inactivity_check_at = ? WHERE id = ?", (iso(now), p["id"]))
        outbox.enqueue(eng, p["id"], "inactivity_check", {"missed_in_a_row": p["no_response_streak"]})


def _recap(eng: "Engine") -> None:
    now = eng.now
    l = local(now)
    if l.weekday() != C.RECAP_WEEKDAY or l.hour < C.RECAP_HOUR:
        return
    key = f"recap:{local_date(now).isoformat()}"
    if kv_get(eng.db, key):
        return
    kv_set(eng.db, key, iso(now))
    for p in eng.db.execute("SELECT * FROM people WHERE opted_out_at IS NULL ORDER BY id").fetchall():
        outbox.enqueue(eng, p["id"], "recap", recap_facts(eng, p))


def recap_facts(eng: "Engine", person) -> dict:
    """Weekly recap contents (§11). Only this person's numbers; never anyone's photo-mode status."""
    now = eng.now
    since = iso(now - C.D * 7)
    pid = person["id"]
    bal = fairness.balance(eng.db, pid)
    share = fairness.fair_share(eng.db, pid)
    mine = eng.db.execute(
        f"SELECT chore, points, retroactive, reason FROM ledger WHERE person_id = ? AND created_at >= ? "
        f"AND status IN {C.COUNTING} AND chore != 'adjust' ORDER BY id",
        (pid, since),
    ).fetchall()
    suite = eng.db.execute(
        f"SELECT chore, COUNT(*) AS n FROM ledger WHERE created_at >= ? AND status IN {C.COUNTING} "
        f"AND chore != 'adjust' AND points > 0 GROUP BY chore ORDER BY chore",
        (since,),
    ).fetchall()
    unresolved = eng.db.execute(
        "SELECT DISTINCT chore FROM tasks WHERE source != 'sink_check' AND "
        "((status = 'unresolved' AND created_at >= ?) OR status = 'open') ORDER BY chore",
        (since,),
    ).fetchall()
    no_resp = eng.db.execute(
        "SELECT COUNT(*) AS n FROM asks WHERE person_id = ? AND status = 'no_response' AND closed_at >= ?",
        (pid, since),
    ).fetchone()["n"]
    paused = parse(person["paused_until"])
    return {
        "your_balance": round(bal, 2),
        "group_average": round(fairness.group_average(eng.db, now), 2),
        "vs_your_share": round(bal - share, 2),  # positive = ahead
        "you_did": [
            {"chore": C.LABELS.get(r["chore"], r["chore"]), "points": round(r["points"], 2),
             "claimed_after_the_fact": bool(r["retroactive"])}
            for r in mine
        ],
        "suite_done": {C.LABELS.get(r["chore"], r["chore"]): r["n"] for r in suite},
        "still_undone": [C.LABELS[r["chore"]] for r in unresolved],
        "your_no_responses": no_resp,
        "paused_until": fmt(paused) if paused and paused > now else None,
    }
