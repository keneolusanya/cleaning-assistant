"""Balances, fair shares and ranking (§3). Pure reads plus the one write that credits points."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime

from . import constants as C
from .clock import iso, parse
from .db import kv_get


def _reset_at(db: sqlite3.Connection) -> str:
    return kv_get(db, "points_reset_at") or ""


def is_active(person: sqlite3.Row, now: datetime) -> bool:
    if person["opted_out_at"]:
        return False
    until = parse(person["paused_until"])
    return not (until and until > now)


def active_people(db: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    rows = db.execute("SELECT * FROM people ORDER BY id").fetchall()
    return [p for p in rows if is_active(p, now)]


def weight(db: sqlite3.Connection, person_id: int, chore: str) -> float:
    row = db.execute(
        "SELECT weight FROM usage_weights WHERE person_id = ? AND family = ?",
        (person_id, C.weight_family(chore)),
    ).fetchone()
    return 1.0 if row is None else float(row["weight"])


def balance(db: sqlite3.Connection, person_id: int) -> float:
    q = f"""SELECT COALESCE(SUM(points), 0) AS s FROM ledger
            WHERE person_id = ? AND status IN {C.COUNTING} AND created_at >= ?"""
    return float(db.execute(q, (person_id, _reset_at(db))).fetchone()["s"])


def fair_share(db: sqlite3.Connection, person_id: int) -> float:
    q = f"""SELECT COALESCE(SUM(s.fraction * l.points), 0) AS s
            FROM shares s JOIN ledger l ON l.id = s.ledger_id
            WHERE s.person_id = ? AND l.status IN {C.SHARED} AND l.created_at >= ?"""
    return float(db.execute(q, (person_id, _reset_at(db))).fetchone()["s"])


def surplus(db: sqlite3.Connection, person_id: int) -> float:
    return balance(db, person_id) - fair_share(db, person_id)


def group_average(db: sqlite3.Connection, now: datetime) -> float:
    people = active_people(db, now)
    if not people:
        return 0.0
    return sum(balance(db, p["id"]) for p in people) / len(people)


def credit(
    db: sqlite3.Connection,
    now: datetime,
    person_id: int,
    task_id: int | None,
    chore: str,
    points: float,
    reason: str,
    photo: bool = False,
    retroactive: bool = False,
    status: str = "confirmed",
) -> int:
    """Write a ledger row and split its points into everyone's fair share."""
    cur = db.execute(
        """INSERT INTO ledger(person_id, task_id, chore, points, status, reason, photo, retroactive, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (person_id, task_id, chore, points, status, reason, int(photo), int(retroactive), iso(now)),
    )
    ledger_id = cur.lastrowid
    if status != "adjust":
        weights = {p["id"]: weight(db, p["id"], chore) for p in active_people(db, now)}
        weights = {pid: w for pid, w in weights.items() if w > 0}
        total = sum(weights.values())
        for pid, w in weights.items():
            db.execute(
                "INSERT INTO shares(ledger_id, person_id, fraction) VALUES (?, ?, ?)",
                (ledger_id, pid, w / total),
            )
    return ledger_id


def last_done(db: sqlite3.Connection, person_id: int, chore: str) -> str:
    """When this person last got credit for this chore family. '' if never."""
    fam = C.chores_in_recency_family(chore)
    marks = ",".join("?" * len(fam))
    row = db.execute(
        f"""SELECT MAX(created_at) AS t FROM ledger
            WHERE person_id = ? AND chore IN ({marks}) AND points > 0 AND status IN {C.COUNTING}""",
        (person_id, *fam),
    ).fetchone()
    return row["t"] or ""


def rank(
    db: sqlite3.Connection, task: sqlite3.Row, now: datetime, rng: random.Random
) -> list[sqlite3.Row]:
    """Candidates for a task, best first (§3 ranking).

    Excludes: inactive people, weight 0, anyone already asked this round, anyone
    marked `excluded` on the task. Order: fewest active asks elsewhere (so both
    bathrooms don't land on one person), then surplus ascending, then longest
    since they did this family, then random.
    """
    asked = {
        r["person_id"]
        for r in db.execute(
            "SELECT person_id FROM asks WHERE task_id = ? AND (round = ? OR status = 'excluded')",
            (task["id"], task["round"]),
        )
    }
    out = []
    for p in active_people(db, now):
        if p["id"] in asked or weight(db, p["id"], task["chore"]) <= 0:
            continue
        holding = db.execute(
            f"SELECT COUNT(*) AS n FROM asks a JOIN tasks t ON t.id = a.task_id "
            f"WHERE a.person_id = ? AND a.status IN {C.ACTIVE_ASK} AND t.status = 'open' "
            f"AND t.source != 'sink_check'",
            (p["id"],),
        ).fetchone()["n"]
        key = (holding, round(surplus(db, p["id"]), 6), last_done(db, p["id"], task["chore"]), rng.random())
        out.append((key, p))
    out.sort(key=lambda kp: kp[0])
    return [p for _, p in out]
