"""Four simulated weeks with random responders. Checks the system-wide invariants hold together."""

import random
from collections import Counter
from datetime import timedelta

from chorebot import constants as C
from chorebot import fairness
from chorebot.clock import local, parse
from chorebot.db import loads

from .conftest import Harness


def respond(h: Harness, rng: random.Random, handled: set) -> None:
    for a in h.db.execute(
        "SELECT a.*, t.chore, t.source, t.parts FROM asks a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.status = 'pending' AND a.sent_at IS NOT NULL AND t.status = 'open'"
    ).fetchall():
        if a["id"] in handled:
            continue
        handled.add(a["id"])
        name = h.eng.name(a["person_id"])
        if a["source"] == "sink_check":
            if rng.random() < 0.7:
                h.tool(name, "resolve_sink_check", task_id=a["task_id"],
                       outcome=rng.choice(["none_mine", "washed_mine", "washed_others"]))
            continue
        r = rng.random()
        if r < 0.5:
            parts = loads(a["parts"])
            if parts:
                parts = parts[: rng.randint(1, len(parts))]
            h.tool(name, "complete_task", task_id=a["task_id"], parts=parts)
        elif r < 0.75:
            h.tool(name, "decline_task", task_id=a["task_id"], reason="busy")
        # else: ignore it and let the nudge ladder run


def test_four_weeks(h):
    rng = random.Random(3)
    handled: set = set()
    end = h.clock.now() + timedelta(days=28)
    while h.clock.now() < end:
        h.clock.advance(timedelta(minutes=15))
        h.tick()
        respond(h, rng, handled)
        if rng.random() < 0.01:
            h.tool(rng.choice(["Kene", "Alula", "Jones"]), "report_needed", chore=rng.choice(["trash", "dishes", "common"]))

    sent = h.db.execute("SELECT * FROM outbox WHERE status = 'sent'").fetchall()
    assert len(sent) > 100

    # Never starts a text in quiet hours.
    assert all(local(parse(r["sent_at"])).hour >= C.QUIET_END_HOUR for r in sent)

    # Cap of 3 bot-initiated texts per person per local day (recaps exempt).
    per_day = Counter((r["person_id"], local(parse(r["sent_at"])).date()) for r in sent
                      if r["purpose"] not in C.CAP_EXEMPT)
    assert max(per_day.values()) <= C.DAILY_CAP

    # Recap every Sunday to everyone.
    assert len([r for r in sent if r["purpose"] == "recap"]) == 7 * 4

    # Work got done, and nobody ran away from their share.
    assert h.db.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()["n"] > 30
    surpluses = [fairness.surplus(h.db, p["id"]) for p in h.db.execute("SELECT id FROM people")]
    assert abs(sum(surpluses)) < 1e-6  # fair shares sum to total credited
    assert max(surpluses) - min(surpluses) < 12

    # Every task ends up somewhere sensible.
    statuses = {r["status"] for r in h.db.execute("SELECT status FROM tasks")}
    assert statuses <= {"open", "done", "unresolved"}
