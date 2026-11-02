"""Admin actions (§9). Parsed by the engine, never by the model; texts only work from the admin's number."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C
from .clock import fmt, iso, parse
from .db import kv_set

if TYPE_CHECKING:
    from .engine import Engine

HELP = (
    "Admin commands:\n"
    "admin tier <name> <0-3>\n"
    "admin reset points\n"
    "admin pause <name> <YYYY-MM-DD>\n"
    "admin unpause <name>\n"
    "admin status"
)


def set_tier(eng: "Engine", name: str, weight: float) -> str:
    p = eng.person_by_name(name)
    if not p:
        return f"No one named {name}."
    if weight < 0 or weight > 3:
        return "Tier weight must be 0-3."
    with eng.tx():
        eng.db.execute(
            "INSERT INTO usage_weights(person_id, family, weight) VALUES (?, 'kitchen', ?) "
            "ON CONFLICT(person_id, family) DO UPDATE SET weight = excluded.weight",
            (p["id"], weight),
        )
    return f"{p['name']}'s kitchen weight is now {weight:g}."


def reset_points(eng: "Engine") -> str:
    with eng.tx():
        kv_set(eng.db, "points_reset_at", iso(eng.now))
    return "Points reset. Everyone starts from 0 now (history is kept)."


def pause(eng: "Engine", name: str, until: str) -> str:
    p = eng.person_by_name(name)
    if not p:
        return f"No one named {name}."
    try:
        t = parse(until if "T" in until else f"{until}T23:59:00")
    except ValueError:
        return "Date should look like 2026-12-01."
    res = eng.call_tool(p, "pause", {"until": t.isoformat()})
    if not res.get("ok"):
        return f"Couldn't pause: {res.get('error')}"
    return f"{p['name']} paused until {res['paused_until']}."


def unpause(eng: "Engine", name: str) -> str:
    p = eng.person_by_name(name)
    if not p:
        return f"No one named {name}."
    eng.call_tool(p, "unpause", {})
    return f"{p['name']} unpaused."


def status(eng: "Engine") -> str:
    lines = []
    for t in eng.db.execute("SELECT * FROM tasks WHERE status = 'open' AND source != 'sink_check' ORDER BY id"):
        holders = ", ".join(f"{eng.name(a['person_id'])} ({a['status']})" for a in eng.active_asks(t["id"]))
        lines.append(f"{C.LABELS[t['chore']]}: {holders or 'nobody yet'}")
    for p in eng.db.execute("SELECT * FROM people ORDER BY id"):
        if p["opted_out_at"]:
            lines.append(f"{p['name']}: opted out")
        elif p["paused_until"] and parse(p["paused_until"]) > eng.now:
            lines.append(f"{p['name']}: paused until {fmt(parse(p['paused_until']))}")
    return "\n".join(lines) or "Nothing open."


def handle_text(eng: "Engine", body: str) -> str:
    words = body.strip().split()
    cmd = [w.lower() for w in words[1:]]
    if cmd[:1] == ["tier"] and len(words) == 4:
        try:
            return set_tier(eng, words[2], float(words[3]))
        except ValueError:
            return "Usage: admin tier <name> <0-3>"
    if cmd[:2] == ["reset", "points"]:
        if cmd[2:3] == ["confirm"]:
            return reset_points(eng)
        return "This zeroes everyone's points. Text 'admin reset points confirm' to do it."
    if cmd[:1] == ["pause"] and len(words) == 4:
        return pause(eng, words[2], words[3])
    if cmd[:1] == ["unpause"] and len(words) == 3:
        return unpause(eng, words[2])
    if cmd[:1] == ["status"]:
        return status(eng)
    return HELP
