"""The engine owns all state and all fairness math (§1). The model acts only through `call_tool`."""

from __future__ import annotations

import inspect
import logging
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import cadence
from . import constants as C
from . import fairness, outbox
from .clock import Clock, iso, parse, say
from .db import dumps, kv_get, kv_set, loads


class ToolError(Exception):
    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code = code
        self.details = details


class Engine:
    def __init__(self, db: sqlite3.Connection, clock: Clock, sender, writer, rng: random.Random | None = None):
        self.db = db
        self.clock = clock
        self.sender = sender
        self.writer = writer
        self.rng = rng or random.Random()
        self.log = logging.getLogger("chorebot")

    @property
    def now(self) -> datetime:
        return self.clock.now()

    @contextmanager
    def tx(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # --- lookups -------------------------------------------------------------

    def person(self, pid: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()

    def person_by_phone(self, phone: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM people WHERE phone = ?", (phone,)).fetchone()

    def person_by_name(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM people WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()

    def name(self, pid: int | None) -> str | None:
        return self.person(pid)["name"] if pid else None

    def is_active(self, person: sqlite3.Row) -> bool:
        return fairness.is_active(person, self.now)

    def task(self, tid: int | None) -> sqlite3.Row | None:
        if tid is None:
            return None
        return self.db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()

    def open_task_for(self, chore: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM tasks WHERE chore = ? AND status = 'open' AND source != 'sink_check' ORDER BY id LIMIT 1",
            (chore,),
        ).fetchone()

    def active_ask(self, task_id: int, person_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            f"SELECT * FROM asks WHERE task_id = ? AND person_id = ? AND status IN {C.ACTIVE_ASK} ORDER BY id DESC LIMIT 1",
            (task_id, person_id),
        ).fetchone()

    def active_asks(self, task_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT * FROM asks WHERE task_id = ? AND status IN {C.ACTIVE_ASK}", (task_id,)
        ).fetchall()

    # --- messages ------------------------------------------------------------

    def log_message(self, pid: int | None, direction: str, body: str, purpose: str | None = None,
                    media: list | None = None, sid: str | None = None, processed: bool = True) -> int:
        cur = self.db.execute(
            "INSERT INTO messages(person_id, direction, body, media, purpose, created_at, processed, provider_sid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, direction, body, dumps(media) if media else None, purpose, iso(self.now), int(processed), sid),
        )
        return cur.lastrowid

    def recent_outgoing(self, pid: int, n: int = 6) -> list[str]:
        rows = self.db.execute(
            "SELECT body FROM messages WHERE person_id = ? AND direction = 'out' ORDER BY id DESC LIMIT ?", (pid, n)
        ).fetchall()
        return [r["body"] for r in reversed(rows)]

    def reply(self, person: sqlite3.Row, body: str, purpose: str = "reply") -> None:
        """A reply to a text the person sent. Not subject to quiet hours or the cap (§5)."""
        try:
            sid = self.sender.send(person["phone"], body, purpose=purpose, name=person["name"])
        except outbox.OptedOutError:
            self.mark_opted_out_by_provider(person["id"])
            return
        self.log_message(person["id"], "out", body, purpose=purpose, sid=sid)

    # --- tasks and asks ------------------------------------------------------

    def create_task(self, chore: str, source: str, *, ask_after: datetime | None = None,
                    window_end: datetime | None = None, reporter_id: int | None = None,
                    note: str | None = None, parts: list[str] | None = None) -> sqlite3.Row:
        if chore in C.BATHROOMS and parts is None:
            parts = list(C.BATHROOM_PARTS)
        cur = self.db.execute(
            "INSERT INTO tasks(chore, source, parts, reporter_id, note, created_at, ask_after, window_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chore, source, dumps(parts) if parts is not None else None, reporter_id, note,
             iso(self.now), iso(ask_after or self.now), iso(window_end)),
        )
        return self.task(cur.lastrowid)

    def task_facts(self, task: sqlite3.Row) -> dict:
        chore = task["chore"]
        covering = self._was_passed_on(task["id"], exclude_person=None)
        parts = (loads(task["parts"]) or []) if chore in C.BATHROOMS else None
        return {
            "task_id": task["id"],
            "chore": C.LABELS[chore],
            "parts_remaining": parts,
            "deadline": task["window_end"],
            "note": task["note"],
            "someone_passed_on_it": covering,
            "source": task["source"],
        }

    def _was_passed_on(self, task_id: int, exclude_person: int | None) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM asks WHERE task_id = ? AND status IN ('declined', 'no_response') AND person_id != ? LIMIT 1",
            (task_id, exclude_person or -1),
        ).fetchone()
        return row is not None

    def ask(self, task: sqlite3.Row, person_id: int, inline: bool = False) -> sqlite3.Row:
        """Ask one person. `inline` means the ask goes inside a reply that's already being written."""
        now = self.now
        cur = self.db.execute(
            "INSERT INTO asks(task_id, person_id, round, status, created_at, sent_at, next_nudge_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (task["id"], person_id, task["round"], iso(now),
             iso(now) if inline else None, iso(now + C.NUDGE_1_AFTER) if inline else None),
        )
        ask = self.db.execute("SELECT * FROM asks WHERE id = ?", (cur.lastrowid,)).fetchone()
        if not inline:
            outbox.enqueue(self, person_id, "ask", self.task_facts(task), ask_id=ask["id"], task_id=task["id"])
        return ask

    def ensure_asked(self, task_id: int, inline_for: int | None = None) -> sqlite3.Row | None:
        """If an open task has nobody currently on it, ask the best-ranked candidate."""
        task = self.task(task_id)
        if task["status"] != "open" or task["source"] == "sink_check":
            return None
        if parse(task["ask_after"]) > self.now or self.active_asks(task_id):
            return None
        cands = fairness.rank(self.db, task, self.now, self.rng)
        if cands:
            pid = cands[0]["id"]
            return self.ask(task, pid, inline=(pid == inline_for))
        if task["window_end"] is None:
            # Reported tasks have no window: start a fresh round daily while open (§4).
            nxt = parse(task["next_round_at"])
            if nxt is None:
                self.db.execute("UPDATE tasks SET next_round_at = ? WHERE id = ?",
                                (iso(self.now + C.REPORTED_ROUND_GAP), task_id))
            elif nxt <= self.now:
                self.db.execute("UPDATE tasks SET round = round + 1, next_round_at = NULL WHERE id = ?", (task_id,))
                task = self.task(task_id)
                cands = fairness.rank(self.db, task, self.now, self.rng)
                if cands:
                    return self.ask(task, cands[0]["id"], inline=(cands[0]["id"] == inline_for))
                self.db.execute("UPDATE tasks SET next_round_at = ? WHERE id = ?",
                                (iso(self.now + C.REPORTED_ROUND_GAP), task_id))
        return None

    def close_ask(self, ask_id: int, status: str, reason: str | None = None) -> None:
        self.db.execute(
            "UPDATE asks SET status = ?, reason = ?, closed_at = ?, next_nudge_at = NULL, remind_at = NULL, "
            "checkin_at = NULL, lapse_at = NULL WHERE id = ?",
            (status, reason, iso(self.now), ask_id),
        )
        outbox.cancel(self, "ask_id = ?", (ask_id,))

    def expire(self, task: sqlite3.Row) -> None:
        """The task's window ended (§4)."""
        if task["source"] == "sink_check":
            for a in self.active_asks(task["id"]):
                self.close_ask(a["id"], "closed", "lapsed")  # lapsed sink checks don't count toward streaks
            self.db.execute("UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?", (iso(self.now), task["id"]))
        else:
            for a in self.active_asks(task["id"]):
                self.close_ask(a["id"], "closed", "window_expired")
            self.db.execute("UPDATE tasks SET status = 'unresolved' WHERE id = ?", (task["id"],))
            self.schedule_reask(task["chore"])
        outbox.cancel(self, "task_id = ?", (task["id"],))

    def schedule_reask(self, chore: str) -> None:
        if chore not in C.SCHEDULED_CHORES:
            return
        reask_at = self.now + C.REASK_DELAY[C.recency_family(chore)]
        due = parse(kv_get(self.db, f"due:{chore}"))
        if due and due <= reask_at:
            return  # the next regular instance comes first
        kv_set(self.db, f"reask:{chore}", iso(reask_at))

    def after_completion(self, chore: str) -> None:
        if chore not in C.SCHEDULED_CHORES:
            return
        kv_set(self.db, f"reask:{chore}", None)
        if chore in ("trash", "fridge"):
            kv_set(self.db, f"due:{chore}", iso(cadence.next_due(chore, self.now, self.rng)))
            return
        # Fixed-cadence chore done early (e.g. reported): skip an instance that's now too close.
        due = parse(kv_get(self.db, f"due:{chore}"))
        if due and due - self.now < cadence.interval(chore) / 2:
            kv_set(self.db, f"due:{chore}", iso(cadence.next_due(chore, due, self.rng)))

    def _complete(self, person: sqlite3.Row, task: sqlite3.Row, parts: list[str] | None,
                  photo: bool, retroactive: bool) -> dict:
        chore = task["chore"]
        if task["source"] == "sink_check":
            raise ToolError("use_resolve_sink_check")
        remaining = loads(task["parts"]) or []
        if chore in C.BATHROOMS:
            if not parts:
                raise ToolError("parts_required", parts_remaining=remaining)
            parts = list(dict.fromkeys(parts))
            bad = [p for p in parts if p not in remaining]
            if bad:
                raise ToolError("parts_not_remaining", parts_remaining=remaining, invalid=bad)
            points = C.PART_POINTS * len(parts)
        elif chore == "dishes":
            points = C.DISHES_OTHERS
        else:
            points = C.POINTS[chore]
        covered = self._was_passed_on(task["id"], exclude_person=person["id"])
        if covered:
            points *= C.COVER_BONUS
        reason = ("claimed" if retroactive else "covered" if covered else "done")
        if chore in C.BATHROOMS:
            reason += ": " + ", ".join(parts)
        fairness.credit(self.db, self.now, person["id"], task["id"], chore, points, reason,
                        photo=photo, retroactive=retroactive)

        mine = self.active_ask(task["id"], person["id"])
        for a in self.active_asks(task["id"]):
            self.close_ask(a["id"], "closed", "completed" if mine and a["id"] == mine["id"] else "done_by_other")
        self.db.execute("UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?", (iso(self.now), task["id"]))
        outbox.cancel(self, "task_id = ?", (task["id"],))

        left = [p for p in remaining if p not in (parts or [])] if chore in C.BATHROOMS else []
        next_name = None
        if left:
            spin = self.create_task(chore, "spinoff", window_end=parse(task["window_end"]),
                                    parts=left, note=f"rest of task {task['id']}")
            self.db.execute(
                "INSERT INTO asks(task_id, person_id, round, status, created_at, reason) VALUES (?, ?, 1, 'excluded', ?, ?)",
                (spin["id"], person["id"], iso(self.now), "did the other parts"),
            )
            nxt = self.ensure_asked(spin["id"])
            next_name = self.name(nxt["person_id"]) if nxt else None
        self.after_completion(chore)
        return {
            "ok": True,
            "chore": C.LABELS[chore],
            "points": round(points, 2),
            "covered_bonus": covered,
            "parts_done": parts if chore in C.BATHROOMS else None,
            "parts_left": left or None,
            "rest_asked_to": next_name,
        }

    def _open_task(self, task_id) -> sqlite3.Row:
        try:
            task = self.task(int(task_id))
        except (TypeError, ValueError):
            task = None
        if task is None:
            raise ToolError("unknown_task")
        if task["status"] == "done":
            who = self.db.execute("SELECT person_id FROM ledger WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                                  (task["id"],)).fetchone()
            raise ToolError("already_done", done_by=self.name(who["person_id"]) if who else None)
        if task["status"] != "open":
            raise ToolError("not_open", status=task["status"])
        return task

    # --- tools (§9) ----------------------------------------------------------

    def call_tool(self, person: sqlite3.Row, name: str, args: dict) -> dict:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return {"ok": False, "error": "unknown_tool"}
        person = self.person(person["id"])
        try:
            inspect.signature(fn).bind(person, **args)
        except TypeError as e:
            return {"ok": False, "error": "bad_arguments", "detail": str(e)}
        try:
            with self.tx():
                return fn(person, **args)
        except ToolError as e:
            return {"ok": False, "error": e.code, **e.details}

    def tool_accept_task(self, person, task_id) -> dict:
        task = self._open_task(task_id)
        if task["source"] == "sink_check":
            raise ToolError("use_resolve_sink_check")
        mine = self.active_ask(task["id"], person["id"])
        if mine and mine["status"] == "accepted":
            return {"ok": True, "already_accepted": True}
        if not mine:
            # Volunteering for something they weren't asked about.
            for a in self.active_asks(task["id"]):
                if a["status"] == "accepted":
                    raise ToolError("already_taken", by=self.name(a["person_id"]))
            for a in self.active_asks(task["id"]):
                self.close_ask(a["id"], "closed", "taken_by_other")
            cur = self.db.execute(
                "INSERT INTO asks(task_id, person_id, round, status, created_at, sent_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                (task["id"], person["id"], task["round"], iso(self.now), iso(self.now)),
            )
            mine = self.db.execute("SELECT * FROM asks WHERE id = ?", (cur.lastrowid,)).fetchone()
        end = parse(task["window_end"])
        checkin = self.now + C.CHECKIN_AFTER
        if end:
            checkin = min(checkin, end - C.CHECKIN_BEFORE_WINDOW_END)
        if checkin <= self.now:
            checkin = None
        self.db.execute(
            "UPDATE asks SET status = 'accepted', checkin_at = ?, next_nudge_at = NULL, remind_at = NULL WHERE id = ?",
            (iso(checkin), mine["id"]),
        )
        outbox.cancel(self, "ask_id = ?", (mine["id"],))
        return {"ok": True, "chore": C.LABELS[task["chore"]], "deadline": say(end, self.now), "will_check_in": say(checkin, self.now, day_only=True)}

    def tool_decline_task(self, person, task_id, reason: str = "") -> dict:
        task = self._open_task(task_id)
        mine = self.active_ask(task["id"], person["id"])
        if not mine:
            raise ToolError("not_asked")
        self.close_ask(mine["id"], "declined", reason or None)
        nxt = self.ensure_asked(task["id"])
        return {"ok": True, "next_asked": self.name(nxt["person_id"]) if nxt else None}

    def tool_snooze_task(self, person, task_id, remind_at: str) -> dict:
        task = self._open_task(task_id)
        mine = self.active_ask(task["id"], person["id"])
        if not mine:
            raise ToolError("not_asked")
        try:
            t = parse(remind_at)
        except ValueError:
            raise ToolError("bad_time", hint="ISO 8601, e.g. 2026-11-07T15:00:00-05:00")
        if t <= self.now:
            raise ToolError("time_in_past")
        if outbox.in_quiet_hours(t):
            t = outbox.morning_of(t)
        self.db.execute(
            "UPDATE asks SET status = 'snoozed', remind_at = ?, next_nudge_at = NULL, checkin_at = NULL WHERE id = ?",
            (iso(t), mine["id"]),
        )
        outbox.cancel(self, "ask_id = ?", (mine["id"],))
        end = parse(task["window_end"])
        return {"ok": True, "remind_at": say(t, self.now), "past_deadline": bool(end and t > end), "deadline": say(end, self.now)}

    def tool_complete_task(self, person, task_id, parts: list[str] | None = None, photo: bool = False) -> dict:
        return self._complete(person, self._open_task(task_id), parts, photo, retroactive=False)

    def tool_claim_past_work(self, person, chore: str, parts: list[str] | None = None, photo: bool = False,
                             note: str = "", dishes: str | None = None) -> dict:
        if chore not in C.CHORES:
            raise ToolError("unknown_chore", chores=C.CHORES)
        if chore == "dishes":
            if dishes not in ("own", "others"):
                raise ToolError("dishes_scope_required", hint="own or others")
            points = C.DISHES_OWN if dishes == "own" else C.DISHES_OTHERS
            fairness.credit(self.db, self.now, person["id"], None, "dishes", points,
                            f"claimed: dishes ({dishes})", photo=photo, retroactive=True)
            return {"ok": True, "chore": "dishes", "points": points}
        task = self.open_task_for(chore)
        if task:
            res = self._complete(person, task, parts, photo, retroactive=True)
            res["closed_open_task"] = True
            return res
        if chore in C.BATHROOMS:
            if not parts or any(p not in C.BATHROOM_PARTS for p in parts):
                raise ToolError("parts_required", parts_remaining=C.BATHROOM_PARTS)
        task = self.create_task(chore, "claim", note=note or None, parts=list(parts) if parts else None)
        return self._complete(person, task, parts, photo, retroactive=True)

    def tool_report_needed(self, person, chore: str, note: str = "", photo: bool = False) -> dict:
        if chore not in C.CHORES:
            raise ToolError("unknown_chore", chores=C.CHORES)
        existing = self.open_task_for(chore)
        if existing:
            extra = f"reported again by {person['name']}" + (f": {note}" if note else "")
            self.db.execute("UPDATE tasks SET note = COALESCE(note || '; ', '') || ? WHERE id = ?", (extra, existing["id"]))
            holders = [self.name(a["person_id"]) for a in self.active_asks(existing["id"])]
            return {"ok": True, "already_open": True, "task_id": existing["id"], "currently_asked": holders or None}
        ask_after = window_end = None
        if chore == "fridge":
            ask_after, window_end = cadence.window_for("fridge", "reported", self.now)
        task = self.create_task(chore, "reported", ask_after=ask_after, window_end=window_end,
                                reporter_id=person["id"], note=note or None)
        if chore == "fridge":
            self._fridge_heads_up(task, skip=person["id"])
        asked = self.ensure_asked(task["id"], inline_for=person["id"])
        return {
            "ok": True,
            "task_id": task["id"],
            "asked_you": bool(asked and asked["person_id"] == person["id"]),
            "asked": self.name(asked["person_id"]) if asked else None,
            "fridge_heads_up_sent": chore == "fridge",
            "first_ask_at": say(ask_after, self.now, day_only=True),
        }

    def _fridge_heads_up(self, task: sqlite3.Row, skip: int | None = None) -> None:
        facts = {"task_id": task["id"], "clear_out_from": task["ask_after"]}
        for p in fairness.active_people(self.db, self.now):
            if p["id"] != skip:
                outbox.enqueue(self, p["id"], "fridge_heads_up", facts, task_id=task["id"])

    def tool_report_done_by_other(self, person, chore: str, person_name: str | None = None) -> dict:
        if chore not in C.CHORES:
            raise ToolError("unknown_chore", chores=C.CHORES)
        task = self.open_task_for(chore)
        if not task:
            raise ToolError("no_open_task")
        if person_name:
            target = self.person_by_name(person_name)
            if not target or not self.is_active(target):
                raise ToolError("unknown_person")
            targets = [target["id"]] if target["id"] != person["id"] else []
        else:
            targets = [a["person_id"] for a in self.active_asks(task["id"]) if a["person_id"] != person["id"]]
        if not targets:
            raise ToolError("nobody_to_check_with")
        facts = {"task_id": task["id"], "chore": C.LABELS[chore], "reported_by": person["name"]}
        for pid in targets:
            outbox.enqueue(self, pid, "done_check", facts, task_id=task["id"])
        return {"ok": True, "checking_with": [self.name(pid) for pid in targets]}

    def tool_resolve_sink_check(self, person, task_id, outcome: str) -> dict:
        task = self.task(int(task_id)) if str(task_id).isdigit() else None
        if task is None or task["source"] != "sink_check":
            raise ToolError("not_a_sink_check")
        if task["status"] != "open":
            raise ToolError("sink_check_lapsed", hint="use claim_past_work with chore=dishes")
        ask = self.db.execute(
            "SELECT * FROM asks WHERE task_id = ? AND person_id = ? AND status = 'pending'", (task["id"], person["id"])
        ).fetchone()
        if not ask:
            raise ToolError("not_asked_or_already_answered")
        points = {"none_mine": 0.0, "washed_mine": C.DISHES_OWN, "washed_others": C.DISHES_OTHERS}.get(outcome)
        if points is None:
            raise ToolError("bad_outcome", options=["none_mine", "washed_mine", "washed_others"])
        if points:
            fairness.credit(self.db, self.now, person["id"], task["id"], "dishes", points,
                            "sink: own" if outcome == "washed_mine" else "sink: others")
        self.close_ask(ask["id"], "closed", outcome)
        if outcome == "washed_others":
            # Sink's empty: don't text the people who haven't been reached yet.
            for row in self.db.execute(
                "SELECT ask_id FROM outbox WHERE task_id = ? AND status = 'queued' AND purpose = 'sink_check'",
                (task["id"],),
            ).fetchall():
                self.close_ask(row["ask_id"], "closed", "sink_cleared")
        return {"ok": True, "points": points}

    def tool_pause(self, person, until: str) -> dict:
        try:
            t = parse(until)
        except ValueError:
            raise ToolError("bad_time", hint="ISO 8601, e.g. 2026-11-08T23:59:00-05:00")
        if t <= self.now:
            raise ToolError("time_in_past")
        self.db.execute("UPDATE people SET paused_from = ?, paused_until = ? WHERE id = ?",
                        (iso(self.now), iso(t), person["id"]))
        handed = self._hand_off(person["id"], "handoff_pause")
        outbox.cancel(self, "person_id = ? AND purpose != 'recap'", (person["id"],))
        return {"ok": True, "paused_until": say(t, self.now), "handed_off": handed}

    def tool_unpause(self, person) -> dict:
        was = bool(person["paused_until"] and parse(person["paused_until"]) > self.now)
        self.db.execute("UPDATE people SET paused_from = NULL, paused_until = NULL WHERE id = ?", (person["id"],))
        return {"ok": True, "was_paused": was}

    def _hand_off(self, pid: int, reason: str) -> list[str]:
        """Close someone's in-flight asks and ask the next person. Not a decline: no cover bonus."""
        handed = []
        rows = self.db.execute(
            f"SELECT a.*, t.chore, t.source FROM asks a JOIN tasks t ON t.id = a.task_id "
            f"WHERE a.person_id = ? AND a.status IN {C.ACTIVE_ASK} AND t.status = 'open'",
            (pid,),
        ).fetchall()
        for a in rows:
            self.close_ask(a["id"], "closed", reason)
            if a["source"] != "sink_check":
                self.ensure_asked(a["task_id"])
                handed.append(C.LABELS[a["chore"]])
        return handed

    # --- STOP / START (§6), called by inbound before the model runs ----------

    def opt_out(self, pid: int) -> None:
        with self.tx():
            self.db.execute("UPDATE people SET opted_out_at = ?, pending_stop_confirm_at = NULL WHERE id = ?",
                            (iso(self.now), pid))
            self._hand_off(pid, "handoff_stop")
            outbox.cancel(self, "person_id = ?", (pid,))

    def mark_opted_out_by_provider(self, pid: int) -> None:
        if not self.person(pid)["opted_out_at"]:
            self.opt_out(pid)

    def opt_in(self, pid: int) -> dict:
        with self.tx():
            self.db.execute("UPDATE people SET opted_out_at = NULL, pending_stop_confirm_at = NULL WHERE id = ?", (pid,))
            others = [p["id"] for p in fairness.active_people(self.db, self.now) if p["id"] != pid]
            mine = fairness.surplus(self.db, pid)
            adjusted = 0.0
            if others:
                lowest = min(fairness.surplus(self.db, o) for o in others)
                if mine > lowest:
                    adjusted = lowest - mine
                    fairness.credit(self.db, self.now, pid, None, "adjust", adjusted, "rejoin adjustment",
                                    status="adjust")
            return {"adjusted_by": round(adjusted, 2)}
