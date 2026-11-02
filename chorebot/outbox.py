"""Bot-initiated messages: quiet hours, morning jitter, the daily cap, priority (§5).

Replies to a person's own text never pass through here; they go out immediately
via Engine.reply.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from . import constants as C
from .clock import at_local, iso, local, local_date, parse
from .db import dumps, loads

if TYPE_CHECKING:
    from .engine import Engine


class OptedOutError(Exception):
    """The SMS provider says this number has opted out."""


def in_quiet_hours(dt: datetime) -> bool:
    return local(dt).hour < C.QUIET_END_HOUR


def morning_of(dt: datetime) -> datetime:
    return at_local(local_date(dt), C.QUIET_END_HOUR)


def next_morning(dt: datetime) -> datetime:
    return at_local(local_date(dt) + timedelta(days=1), C.QUIET_END_HOUR)


def enqueue(
    eng: "Engine",
    person_id: int,
    purpose: str,
    facts: dict,
    ask_id: int | None = None,
    task_id: int | None = None,
    send_after: datetime | None = None,
) -> int:
    send_after = send_after or eng.now
    held = in_quiet_hours(send_after)
    if held:
        send_after = morning_of(send_after)
    cur = eng.db.execute(
        """INSERT INTO outbox(person_id, purpose, priority, facts, ask_id, task_id, send_after, held, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (person_id, purpose, C.PRIORITY[purpose], dumps(facts), ask_id, task_id,
         iso(send_after), int(held), iso(eng.now)),
    )
    return cur.lastrowid


def cancel(eng: "Engine", where: str, params: tuple) -> None:
    eng.db.execute(f"UPDATE outbox SET status = 'cancelled' WHERE status = 'queued' AND {where}", params)


def _assign_morning_slots(eng: "Engine") -> None:
    """Spread held messages across the late morning, in priority order per person."""
    now = eng.now
    rows = eng.db.execute(
        "SELECT * FROM outbox WHERE status = 'queued' AND held = 1 AND send_after <= ? "
        "ORDER BY person_id, priority, id",
        (iso(now),),
    ).fetchall()
    base = max(now, morning_of(now))
    t_by_person: dict[int, datetime] = {}
    for row in rows:
        pid = row["person_id"]
        if pid not in t_by_person:
            t = base + timedelta(seconds=eng.rng.uniform(0, C.MORNING_JITTER_SPAN.total_seconds()))
        else:
            lo, hi = C.MORNING_GAP
            t = t_by_person[pid] + timedelta(seconds=eng.rng.uniform(lo.total_seconds(), hi.total_seconds()))
        t_by_person[pid] = t
        eng.db.execute("UPDATE outbox SET held = 0, send_after = ? WHERE id = ?", (iso(t), row["id"]))


def sent_today(eng: "Engine", person_id: int) -> int:
    """Bot-initiated, cap-counted messages sent to this person today (local date)."""
    start = at_local(local_date(eng.now), 0)
    marks = ",".join("?" * len(C.CAP_EXEMPT))
    return eng.db.execute(
        f"SELECT COUNT(*) AS n FROM outbox WHERE person_id = ? AND status = 'sent' AND sent_at >= ? "
        f"AND purpose NOT IN ({marks})",
        (person_id, iso(start), *C.CAP_EXEMPT),
    ).fetchone()["n"]


def _last_initiated(eng: "Engine", person_id: int) -> datetime | None:
    row = eng.db.execute(
        "SELECT MAX(sent_at) AS t FROM outbox WHERE person_id = ? AND status = 'sent'", (person_id,)
    ).fetchone()
    return parse(row["t"])


def _still_relevant(eng: "Engine", row) -> bool:
    person = eng.person(row["person_id"])
    if person["opted_out_at"]:
        return False
    purpose = row["purpose"]
    task = eng.task(row["task_id"]) if row["task_id"] else None
    if task is not None and task["status"] != "open":
        return False
    if row["ask_id"]:
        ask = eng.db.execute("SELECT * FROM asks WHERE id = ?", (row["ask_id"],)).fetchone()
        want = "accepted" if purpose == "checkin" else "pending"
        if ask is None or ask["status"] != want:
            return False
    if purpose == "inactivity_check" and not eng.is_active(person):
        return False
    return True


def _drop_sink_check(eng: "Engine", row, status: str) -> None:
    eng.db.execute("UPDATE outbox SET status = ? WHERE id = ?", (status, row["id"]))
    if row["ask_id"]:
        eng.db.execute(
            "UPDATE asks SET status = 'closed', reason = 'dropped', closed_at = ? WHERE id = ? AND status = 'pending'",
            (iso(eng.now), row["ask_id"]),
        )


def flush(eng: "Engine") -> int:
    """Send whatever is due. Returns how many went out."""
    now = eng.now
    if in_quiet_hours(now):
        return 0
    _assign_morning_slots(eng)
    rows = eng.db.execute(
        "SELECT * FROM outbox WHERE status = 'queued' AND held = 0 AND send_after <= ? "
        "ORDER BY priority, send_after, id",
        (iso(now),),
    ).fetchall()
    sent = 0
    for row in rows:
        if not _still_relevant(eng, row):
            if row["purpose"] == "sink_check":
                _drop_sink_check(eng, row, "cancelled")
            else:
                eng.db.execute("UPDATE outbox SET status = 'cancelled' WHERE id = ?", (row["id"],))
            continue
        pid = row["person_id"]
        if row["purpose"] not in C.CAP_EXEMPT:
            if sent_today(eng, pid) >= C.DAILY_CAP:
                if row["purpose"] == "sink_check":
                    # Dropped, not queued (§5): they're in the next suite-wide check anyway.
                    _drop_sink_check(eng, row, "dropped")
                else:
                    eng.db.execute(
                        "UPDATE outbox SET send_after = ?, held = 1 WHERE id = ?",
                        (iso(next_morning(now)), row["id"]),
                    )
                continue
            last = _last_initiated(eng, pid)
            if last and now - last < C.MIN_GAP:
                eng.db.execute(
                    "UPDATE outbox SET send_after = ? WHERE id = ?", (iso(last + C.MIN_GAP), row["id"])
                )
                continue
        if _deliver(eng, row):
            sent += 1
    return sent


def _deliver(eng: "Engine", row) -> bool:
    person = eng.person(row["person_id"])
    facts = loads(row["facts"]) or {}
    body = eng.writer.write(row["purpose"], facts, person["name"], eng.recent_outgoing(person["id"]))
    try:
        sid = eng.sender.send(person["phone"], body, purpose=row["purpose"], name=person["name"])
    except OptedOutError:
        eng.mark_opted_out_by_provider(person["id"])
        eng.db.execute("UPDATE outbox SET status = 'cancelled' WHERE id = ?", (row["id"],))
        return False
    except Exception:
        eng.log.exception("send failed (outbox %s)", row["id"])
        attempts = row["attempts"] + 1
        status = "failed" if attempts >= C.SEND_ATTEMPTS else "queued"
        eng.db.execute(
            "UPDATE outbox SET attempts = ?, status = ?, send_after = ? WHERE id = ?",
            (attempts, status, iso(eng.now + timedelta(minutes=5)), row["id"]),
        )
        return False
    now = eng.now
    eng.db.execute(
        "UPDATE outbox SET status = 'sent', sent_at = ?, body = ? WHERE id = ?", (iso(now), body, row["id"])
    )
    eng.log_message(person["id"], "out", body, purpose=row["purpose"], sid=sid)
    _on_sent(eng, row)
    return True


def _on_sent(eng: "Engine", row) -> None:
    """Advance the nudge ladder (§5) based on what just went out."""
    if not row["ask_id"]:
        return
    now = eng.now
    purpose = row["purpose"]
    if purpose in ("ask", "snooze_reminder"):
        eng.db.execute(
            "UPDATE asks SET sent_at = ?, nudges = 0, next_nudge_at = ? WHERE id = ?",
            (iso(now), iso(now + C.NUDGE_1_AFTER), row["ask_id"]),
        )
    elif purpose == "nudge":
        ask = eng.db.execute("SELECT nudges FROM asks WHERE id = ?", (row["ask_id"],)).fetchone()
        n = ask["nudges"] + 1
        nxt = next_morning(now) if n == 1 else now + C.NO_RESPONSE_AFTER_NUDGE_2
        eng.db.execute("UPDATE asks SET nudges = ?, next_nudge_at = ? WHERE id = ?", (n, iso(nxt), row["ask_id"]))
    elif purpose == "sink_check":
        eng.db.execute("UPDATE asks SET sent_at = ? WHERE id = ?", (iso(now), row["ask_id"]))
