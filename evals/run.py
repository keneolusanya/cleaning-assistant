"""Runs evals/cases.json against the real interpreter model. Every misread in real use should become a case here.

    chorebot eval            # all cases
    chorebot eval --only sink
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from chorebot import fairness
from chorebot.clock import TZ, FakeClock, iso
from chorebot.db import connect
from chorebot.engine import Engine
from chorebot.interpreter import ClaudeInterpreter
from chorebot.roster import load, seed
from chorebot.sms import ConsoleSender
from chorebot.writer import TemplateWriter, template

ROOT = Path(__file__).resolve().parent.parent
START = datetime(2026, 11, 7, 14, 0, tzinfo=TZ)  # a Saturday afternoon


def build(case: dict) -> tuple[Engine, dict]:
    eng = Engine(connect(":memory:"), FakeClock(START), ConsoleSender(), TemplateWriter(), rng=random.Random(0))
    seed(eng, load(str(ROOT / "people.example.toml")))
    aliases: dict[str, int] = {}
    with eng.tx():
        for step in case.get("setup", []):
            if "task" in step:
                task = eng.create_task(step["task"], step.get("source", "scheduled"),
                                       window_end=eng.now + timedelta(days=2))
                aliases[step["as"]] = task["id"]
                if "ask" in step:
                    p = eng.person_by_name(step["ask"])
                    ask = eng.ask(task, p["id"], inline=True)
                    if step.get("status", "pending") != "pending":
                        eng.db.execute("UPDATE asks SET status = ? WHERE id = ?", (step["status"], ask["id"]))
                    eng.log_message(p["id"], "out", template("ask", eng.task_facts(task), p["name"]), purpose="ask")
            elif "sink_check" in step:
                task = eng.create_task("dishes", "sink_check", window_end=eng.now + timedelta(hours=12))
                aliases[step["sink_check"]] = task["id"]
                for name in step["ask"]:
                    p = eng.person_by_name(name)
                    eng.db.execute("INSERT INTO asks(task_id, person_id, status, created_at, sent_at) "
                                   "VALUES (?, ?, 'pending', ?, ?)", (task["id"], p["id"], iso(eng.now), iso(eng.now)))
                    eng.log_message(p["id"], "out", template("sink_check", {}, name), purpose="sink_check")
            elif "pause" in step:
                p = eng.person_by_name(step["pause"])
                eng.db.execute("UPDATE people SET paused_from = ?, paused_until = ? WHERE id = ?",
                               (iso(eng.now), iso(eng.now + timedelta(days=step["days"])), p["id"]))
            elif "credit" in step:
                fairness.credit(eng.db, eng.now, eng.person_by_name(step["credit"])["id"], None,
                                step["chore"], step["points"], "done")
    return eng, aliases


def resolve(v, aliases):
    if isinstance(v, str) and v.startswith("$"):
        return aliases[v[1:]]
    if isinstance(v, list):
        return [resolve(x, aliases) for x in v]
    return v


def check(case: dict, calls: list[dict], reply: str, aliases: dict) -> list[str]:
    exp = case["expect"]
    names = [c["name"] for c in calls]
    problems = []
    if "tools" in exp and names != exp["tools"]:
        problems.append(f"tools {names} != expected {exp['tools']}")
    if "any_of" in exp and names not in exp["any_of"]:
        problems.append(f"tools {names} not in {exp['any_of']}")
    for tool, want in exp.get("args", {}).items():
        got = next((c["input"] for c in calls if c["name"] == tool), None)
        if got is None:
            continue
        for k, v in want.items():
            v = resolve(v, aliases)
            g = got.get(k)
            if isinstance(v, list):
                if sorted(g or []) != sorted(v):
                    problems.append(f"{tool}.{k} = {g!r}, expected {v!r}")
            elif g != v:
                problems.append(f"{tool}.{k} = {g!r}, expected {v!r}")
    for bad in exp.get("reply_excludes", []):
        if bad in reply:
            problems.append(f"reply contains {bad!r}")
    if not reply.strip():
        problems.append("empty reply")
    return problems


def main(only: str | None = None) -> int:
    cases = json.loads((ROOT / "evals" / "cases.json").read_text())
    if only:
        cases = [c for c in cases if only.lower() in c["name"].lower()]
    interp = ClaudeInterpreter()
    failed = 0
    for case in cases:
        eng, aliases = build(case)
        person = eng.person_by_name(case["from"])
        msg_id = eng.log_message(person["id"], "in", case["text"])
        result = interp.interpret(eng, person, case["text"], message_id=msg_id)
        problems = check(case, result.calls, result.reply, aliases)
        mark = "PASS" if not problems else "FAIL"
        failed += bool(problems)
        print(f"{mark}  {case['name']}")
        print(f"      {case['from']}: {case['text']}")
        for c in result.calls:
            print(f"      · {c['name']} {json.dumps(c['input'])} -> ok={c['result'].get('ok')}")
        print(f"      bot: {result.reply}")
        for p in problems:
            print(f"      ! {p}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0
