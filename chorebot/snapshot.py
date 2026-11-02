"""The whole world the model sees for one incoming text (§10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C
from . import fairness
from .clock import fmt, iso, local, parse
from .db import loads

if TYPE_CHECKING:
    from .engine import Engine


def build(eng: "Engine", person, exclude_message_id: int | None = None) -> dict:
    now = eng.now
    pid = person["id"]
    bal = fairness.balance(eng.db, pid)
    share = fairness.fair_share(eng.db, pid)
    paused = parse(person["paused_until"])
    photo_mode = parse(person["photo_mode_until"])

    my_asks = []
    for a in eng.db.execute(
        f"SELECT a.*, t.chore, t.parts, t.window_end, t.source FROM asks a JOIN tasks t ON t.id = a.task_id "
        f"WHERE a.person_id = ? AND a.status IN {C.ACTIVE_ASK} AND t.status = 'open' ORDER BY a.id",
        (pid,),
    ):
        my_asks.append({
            "task_id": a["task_id"],
            "kind": "sink_check" if a["source"] == "sink_check" else "chore",
            "chore": C.LABELS[a["chore"]] if a["source"] != "sink_check" else "sink check (dishes)",
            "status": a["status"],
            "parts_remaining": loads(a["parts"]),
            "deadline": fmt(parse(a["window_end"])),
            "asked_at": fmt(parse(a["sent_at"] or a["created_at"])),
            "remind_at": fmt(parse(a["remind_at"])),
        })

    open_tasks = []
    for t in eng.db.execute(
        "SELECT * FROM tasks WHERE status = 'open' AND source != 'sink_check' ORDER BY id"
    ).fetchall():
        holders = [
            {"name": eng.name(a["person_id"]), "status": a["status"]} for a in eng.active_asks(t["id"])
        ]
        open_tasks.append({
            "task_id": t["id"],
            "chore_key": t["chore"],
            "chore": C.LABELS[t["chore"]],
            "parts_remaining": loads(t["parts"]),
            "deadline": fmt(parse(t["window_end"])),
            "starts_asking_at": fmt(parse(t["ask_after"])) if parse(t["ask_after"]) > now else None,
            "currently_asked": holders,
            "source": t["source"],
        })

    ledger = [
        {"ledger_id": r["id"], "person": eng.name(r["person_id"]), "chore": C.LABELS.get(r["chore"], r["chore"]),
         "when": fmt(parse(r["created_at"])), "detail": r["reason"]}
        for r in eng.db.execute(
            "SELECT * FROM ledger WHERE created_at >= ? AND chore != 'adjust' AND points > 0 ORDER BY id",
            (iso(now - C.LEDGER_LOOKBACK),),
        )
    ]

    history = [
        {"when": fmt(parse(m["created_at"])), "from": "them" if m["direction"] == "in" else "bot",
         "text": m["body"] + (" [photo attached]" if m["media"] else "")}
        for m in reversed(eng.db.execute(
            "SELECT * FROM messages WHERE person_id = ? AND created_at >= ? AND id != ? ORDER BY id DESC LIMIT ?",
            (pid, iso(now - C.HISTORY_WINDOW), exclude_message_id or -1, C.HISTORY_MAX_MESSAGES),
        ).fetchall())
    ]

    weights = {r["family"]: r["weight"] for r in eng.db.execute(
        "SELECT family, weight FROM usage_weights WHERE person_id = ?", (pid,))}

    return {
        "now": local(now).strftime("%A %Y-%m-%d %H:%M %Z") + f" (ISO {local(now).isoformat(timespec='minutes')})",
        "sender": {
            "name": person["name"],
            "your_points": round(bal, 2),
            "your_fair_share": round(share, 2),
            "ahead_of_share_by": round(bal - share, 2),
            "kitchen_tier_weight": weights.get("kitchen", 1),
            "paused_until": fmt(paused) if paused and paused > now else None,
            "photo_request_mode": bool(photo_mode and photo_mode > now),
        },
        "group_average_points": round(fairness.group_average(eng.db, now), 2),
        "your_open_asks": my_asks,
        "open_tasks": open_tasks,
        "recent_completions_last_7_days": ledger,
        "conversation_last_72h": history,
    }
