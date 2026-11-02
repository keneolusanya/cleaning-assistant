"""Terminal simulator: text the bot as any suitemate, with a fake clock (§0 testing).

    Kene: done
    /advance 4h
    /photo Alula sink.jpg sink is full
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import shlex
from datetime import datetime, timedelta

from . import constants as C
from . import fairness, inbound, outbox, scheduler
from .clock import TZ, FakeClock, fmt, parse
from .db import connect, loads
from .engine import Engine
from .roster import seed
from .sms import ConsoleSender

HELP = """Commands:
  <Name>: <text>                  text the bot as that person (e.g. "Kene: done")
  /photo <Name> <file> [text]     text with a photo attached
  /advance <N>[m|h|d]             move the clock forward, running the scheduler (e.g. /advance 4h)
  /to YYYY-MM-DD HH:MM            jump the clock to a local time
  /now                            show the simulated time
  /tasks                          open tasks and who's asked
  /points                         everyone's points, share and surplus (simulator only)
  /ledger [n]                     last n ledger rows
  /outbox                         queued bot-initiated messages
  /people                         roster with pause / opt-out state
  /tool <Name> <tool> [json]      call an engine tool directly, skipping the model
  /onboard                        queue the intro text to everyone
  /help, /quit"""

TICK = timedelta(minutes=5)


class Simulator:
    def __init__(self, db_path: str, start: datetime, people: dict, offline: bool, seed_n: int | None):
        self.clock = FakeClock(start)
        self.sender = ConsoleSender(on_send=self._print_out)
        if not offline and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print("No ANTHROPIC_API_KEY set, so running offline (templates; use /tool instead of texting).")
            offline = True
        if offline:
            from .writer import TemplateWriter

            writer, self.interpreter = TemplateWriter(), None
        else:
            from .interpreter import ClaudeInterpreter
            from .writer import ClaudeWriter

            writer, self.interpreter = ClaudeWriter(), ClaudeInterpreter()
        self.db = connect(db_path)
        self.eng = Engine(self.db, self.clock, self.sender, writer, rng=random.Random(seed_n))
        seed(self.eng, people)

    def _print_out(self, name: str, phone: str, purpose: str, body: str) -> None:
        body = body.replace("\n", "\n      ")
        print(f"  [{fmt(self.clock.now())}] bot → {name} ({purpose}): {body}")

    def text(self, name: str, body: str, photo: str | None = None) -> None:
        p = self.eng.person_by_name(name)
        if not p:
            print(f"  no one named {name}")
            return
        media = []
        if photo:
            ctype = mimetypes.guess_type(photo)[0] or "image/jpeg"
            with open(photo, "rb") as f:
                media.append({"url": f"file://{photo}", "content_type": ctype, "data": f.read()})
        result = inbound.handle(self.eng, self.interpreter, p["phone"], body, media)
        for call in getattr(result, "calls", []) or []:
            print(f"    · {call['name']} {json.dumps(call['input'])} → {json.dumps(call['result'], default=str)}")
        # Anything the action queued (e.g. an ask to the next person) can go out right away.
        scheduler.tick(self.eng)

    def advance(self, delta: timedelta) -> None:
        end = self.clock.now() + delta
        while self.clock.now() < end:
            self.clock.advance(min(TICK, end - self.clock.now()))
            scheduler.tick(self.eng)
        print(f"  now {fmt(self.clock.now())}")

    def show_tasks(self) -> None:
        rows = self.db.execute("SELECT * FROM tasks WHERE status = 'open' ORDER BY id").fetchall()
        if not rows:
            print("  no open tasks")
        for t in rows:
            asks = ", ".join(f"{self.eng.name(a['person_id'])}:{a['status']}" for a in self.eng.active_asks(t["id"]))
            parts = loads(t["parts"])
            label = "sink check" if t["source"] == "sink_check" else C.LABELS[t["chore"]]
            extra = f" parts={parts}" if parts else ""
            print(f"  #{t['id']} {label} [{t['source']}]{extra} due {fmt(parse(t['window_end'])) or '-'}"
                  f" | asked: {asks or 'nobody'}")

    def show_points(self) -> None:
        for p in self.db.execute("SELECT * FROM people ORDER BY id").fetchall():
            b, s = fairness.balance(self.db, p["id"]), fairness.fair_share(self.db, p["id"])
            print(f"  {p['name']:<10} points {b:6.2f}  share {s:6.2f}  surplus {b - s:+6.2f}")
        print(f"  group average {fairness.group_average(self.db, self.clock.now()):.2f}")

    def show_ledger(self, n: int) -> None:
        for r in reversed(self.db.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (n,)).fetchall()):
            flags = (" retro" if r["retroactive"] else "") + (" photo" if r["photo"] else "")
            print(f"  #{r['id']} {fmt(parse(r['created_at']))} {self.eng.name(r['person_id'])} "
                  f"{r['chore']} {r['points']:+.2f} {r['status']} ({r['reason']}){flags}")

    def show_outbox(self) -> None:
        rows = self.db.execute("SELECT * FROM outbox WHERE status = 'queued' ORDER BY send_after").fetchall()
        if not rows:
            print("  outbox empty")
        for r in rows:
            held = " (held for morning)" if r["held"] else ""
            print(f"  {fmt(parse(r['send_after']))} → {self.eng.name(r['person_id'])} {r['purpose']}{held}")

    def show_people(self) -> None:
        for p in self.db.execute("SELECT * FROM people ORDER BY id").fetchall():
            state = []
            if p["opted_out_at"]:
                state.append("opted out")
            if p["paused_until"] and parse(p["paused_until"]) > self.clock.now():
                state.append(f"paused until {fmt(parse(p['paused_until']))}")
            if p["no_response_streak"]:
                state.append(f"no-response streak {p['no_response_streak']}")
            w = fairness.weight(self.db, p["id"], "kitchen")
            admin = " (admin)" if p["is_admin"] else ""
            print(f"  {p['name']}{admin} kitchen={w:g} {', '.join(state)}")

    def run(self) -> None:
        print(f"Simulator. Clock: {fmt(self.clock.now())}. "
              f"{'Offline (templates, no model).' if self.interpreter is None else 'Using Claude.'} /help for commands.")
        while True:
            try:
                line = input(f"[{fmt(self.clock.now())}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            try:
                if self.command(line) == "quit":
                    return
            except Exception as e:  # keep the REPL alive
                print(f"  error: {e!r}")

    def command(self, line: str):
        if not line.startswith("/"):
            m = re.match(r"^([A-Za-z]+)\s*:\s*(.*)$", line)
            if not m:
                print("  type <Name>: <text>, or /help")
                return
            return self.text(m.group(1), m.group(2))
        parts = shlex.split(line)
        cmd, args = parts[0], parts[1:]
        if cmd in ("/quit", "/exit"):
            return "quit"
        if cmd == "/help":
            print(HELP)
        elif cmd == "/photo" and len(args) >= 2:
            self.text(args[0], " ".join(args[2:]), photo=args[1])
        elif cmd == "/advance" and args:
            m = re.match(r"^(\d+(?:\.\d+)?)([mhd])$", args[0])
            if not m:
                print("  e.g. /advance 30m, /advance 4h, /advance 2d")
                return
            n, unit = float(m.group(1)), m.group(2)
            self.advance(timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n))
        elif cmd == "/to" and args:
            target = datetime.fromisoformat(" ".join(args)).replace(tzinfo=TZ)
            if target <= self.clock.now():
                print("  can only move forward")
                return
            self.advance(target - self.clock.now())
        elif cmd == "/now":
            print(f"  {fmt(self.clock.now())}")
        elif cmd == "/tasks":
            self.show_tasks()
        elif cmd == "/points":
            self.show_points()
        elif cmd == "/ledger":
            self.show_ledger(int(args[0]) if args else 10)
        elif cmd == "/outbox":
            self.show_outbox()
        elif cmd == "/people":
            self.show_people()
        elif cmd == "/tool" and len(args) >= 2:
            raw = line.split(None, 3)  # keep the JSON exactly as typed
            p = self.eng.person_by_name(raw[1])
            if not p:
                print(f"  no one named {raw[1]}")
                return
            tool_args = json.loads(raw[3]) if len(raw) > 3 else {}
            print(f"  {json.dumps(self.eng.call_tool(p, raw[2], tool_args), default=str)}")
            scheduler.tick(self.eng)
        elif cmd == "/onboard":
            with self.eng.tx():
                for p in self.db.execute("SELECT id FROM people WHERE opted_out_at IS NULL").fetchall():
                    outbox.enqueue(self.eng, p["id"], "intro", {})
            scheduler.tick(self.eng)
        else:
            print("  unknown command; /help")
