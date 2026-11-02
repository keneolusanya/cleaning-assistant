"""chorebot command line. `chorebot --help` for the list."""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from datetime import datetime

from . import admin, outbox
from .clock import TZ, Clock, iso
from .db import connect
from .engine import Engine
from .roster import load, seed

DEFAULT_DB = os.environ.get("CHOREBOT_DB", "chorebot.db")


def _engine(db_path: str, sender=None, writer=None) -> Engine:
    return Engine(connect(db_path), Clock(), sender, writer, rng=random.Random())


def cmd_init(a) -> None:
    eng = _engine(a.db)
    names = seed(eng, load(a.people))
    print(f"Database {a.db}: {len(names)} people loaded ({', '.join(names)}).")


def cmd_sim(a) -> None:
    from .simulator import Simulator

    if a.fresh and os.path.exists(a.db):
        os.remove(a.db)
    start = datetime.fromisoformat(a.start).replace(tzinfo=TZ) if a.start else datetime.now(TZ)
    Simulator(a.db, start, load(a.people), offline=a.offline, seed_n=a.seed).run()


def cmd_serve(a) -> None:
    from .interpreter import ClaudeInterpreter
    from .server import config_from_env, create_app
    from .sms import TwilioSender
    from .writer import ClaudeWriter

    sender = TwilioSender()
    eng = _engine(a.db, sender=sender, writer=ClaudeWriter())
    app = create_app(eng, ClaudeInterpreter(), media_fetcher=sender.fetch_media, **config_from_env())
    app.run(host=a.host, port=a.port)


def cmd_onboard(a) -> None:
    eng = _engine(a.db)
    with eng.tx():
        rows = eng.db.execute("SELECT id, name FROM people WHERE opted_out_at IS NULL").fetchall()
        for p in rows:
            outbox.enqueue(eng, p["id"], "intro", {})
    print(f"Queued intro texts for {len(rows)} people. The running server sends them.")


def cmd_admin(a) -> None:
    eng = _engine(a.db)
    if a.action == "tier":
        print(admin.set_tier(eng, a.name, a.weight))
    elif a.action == "reset-points":
        if input("Zero everyone's points? Type yes: ").strip().lower() == "yes":
            print(admin.reset_points(eng))
    elif a.action == "pause":
        print(admin.pause(eng, a.name, a.until))
    elif a.action == "unpause":
        print(admin.unpause(eng, a.name))
    elif a.action == "status":
        print(admin.status(eng))


def cmd_people(a) -> None:
    eng = _engine(a.db)
    if a.action == "list":
        for p in eng.db.execute("SELECT * FROM people ORDER BY id"):
            flags = " (admin)" if p["is_admin"] else ""
            flags += " opted out" if p["opted_out_at"] else ""
            print(f"{p['name']}{flags} {p['phone']}")
    elif a.action == "add":
        with eng.tx():
            pid = eng.db.execute("INSERT INTO people(name, phone, created_at) VALUES (?, ?, ?)",
                                 (a.name, a.phone, iso(eng.now))).lastrowid
            if a.kitchen is not None:
                eng.db.execute("INSERT INTO usage_weights(person_id, family, weight) VALUES (?, 'kitchen', ?)",
                               (pid, a.kitchen))
        print(f"Added {a.name}.")
    elif a.action == "set-phone":
        p = eng.person_by_name(a.name)
        eng.db.execute("UPDATE people SET phone = ? WHERE id = ?", (a.phone, p["id"]))
        print(f"{p['name']}'s phone updated.")
    elif a.action == "remove":
        # Keeps their history; they just stop being asked or texted.
        p = eng.person_by_name(a.name)
        eng.opt_out(p["id"])
        print(f"{p['name']} removed from the rotation (history kept).")


def cmd_eval(a) -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from evals.run import main as run_evals

    sys.exit(run_evals(a.only))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.environ.get("CHOREBOT_LOG", "WARNING"))
    ap = argparse.ArgumentParser(prog="chorebot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the database and load people.toml")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--people", default="people.toml")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("sim", help="terminal simulator with a fake clock")
    p.add_argument("--db", default="sim.db")
    p.add_argument("--people", default="people.example.toml")
    p.add_argument("--fresh", action="store_true", help="start from an empty database")
    p.add_argument("--start", help="local start time, e.g. '2026-11-07 09:00'")
    p.add_argument("--offline", action="store_true", help="no API calls: templates, and /tool instead of the model")
    p.add_argument("--seed", type=int, help="random seed for repeatable runs")
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("serve", help="run the webhook + scheduler (needs Twilio and Anthropic env vars)")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("onboard", help="queue the intro text to everyone")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(fn=cmd_onboard)

    p = sub.add_parser("admin", help="admin actions")
    p.add_argument("--db", default=DEFAULT_DB)
    asub = p.add_subparsers(dest="action", required=True)
    t = asub.add_parser("tier")
    t.add_argument("name")
    t.add_argument("weight", type=float)
    asub.add_parser("reset-points")
    t = asub.add_parser("pause")
    t.add_argument("name")
    t.add_argument("until", help="YYYY-MM-DD")
    t = asub.add_parser("unpause")
    t.add_argument("name")
    asub.add_parser("status")
    p.set_defaults(fn=cmd_admin)

    p = sub.add_parser("people", help="roster edits")
    p.add_argument("--db", default=DEFAULT_DB)
    psub = p.add_subparsers(dest="action", required=True)
    psub.add_parser("list")
    t = psub.add_parser("add")
    t.add_argument("name")
    t.add_argument("phone")
    t.add_argument("--kitchen", type=float)
    t = psub.add_parser("set-phone")
    t.add_argument("name")
    t.add_argument("phone")
    t = psub.add_parser("remove")
    t.add_argument("name")
    p.set_defaults(fn=cmd_people)

    p = sub.add_parser("eval", help="run the model eval set (calls the API)")
    p.add_argument("--only", help="run cases whose name contains this")
    p.set_defaults(fn=cmd_eval)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
