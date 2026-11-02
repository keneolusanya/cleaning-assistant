"""The tool loop, against a fake API client (no network)."""

import json
from types import SimpleNamespace as NS

import pytest

from chorebot import inbound
from chorebot.interpreter import ClaudeInterpreter, ModelUnavailable


def text_block(t):
    return NS(type="text", text=t)


def tool_block(name, inp, id_="tu_1"):
    return NS(type="tool_use", name=name, input=inp, id=id_)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def fake_client(script):
    msgs = FakeMessages(script)
    return NS(beta=NS(messages=msgs)), msgs


def resp(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks))


@pytest.fixture
def trash_ask(h):
    h.quiet_calendar()
    t = h.task("trash")
    h.ask(t, "Kene")
    return t


def test_tool_call_then_reply(h, trash_ask):
    client, msgs = fake_client([
        resp("tool_use", tool_block("complete_task", {"task_id": trash_ask["id"]})),
        resp("end_turn", text_block("Nice, thanks for taking out the trash!")),
    ])
    interp = ClaudeInterpreter(client=client)
    result = inbound.handle(h.eng, interp, h.p("Kene")["phone"], "done")
    assert result.calls[0]["result"]["ok"] and result.calls[0]["result"]["points"] == 1.0
    assert h.sent_to("Kene")[-1]["body"] == "Nice, thanks for taking out the trash!"

    first = msgs.requests[0]
    assert first["model"] == "claude-sonnet-5-5"
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    user_text = first["messages"][0]["content"][-1]["text"]
    assert user_text == "New text from Kene: done"
    snap = json.loads(first["messages"][0]["content"][0]["text"].split("\n", 1)[1])
    assert snap["your_open_asks"][0]["task_id"] == trash_ask["id"]
    # The new text isn't duplicated into the history.
    assert all(m["text"] != "done" for m in snap["conversation_last_72h"])
    # Tool results go back in one user message.
    second = msgs.requests[1]
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"


def test_snapshot_never_has_other_balances(h, trash_ask):
    from chorebot import fairness, snapshot

    with h.eng.tx():
        fairness.credit(h.db, h.clock.now(), h.pid("Salajan"), None, "kitchen", 2.0, "done")
    snap = snapshot.build(h.eng, h.p("Kene"))
    text = json.dumps(snap)
    assert "your_points" in text
    assert "Salajan" in text  # who did what isn't private...
    assert "points" not in json.dumps(snap["recent_completions_last_7_days"])  # ...but their numbers are


def test_failed_tool_is_marked_error(h, trash_ask):
    client, msgs = fake_client([
        resp("tool_use", tool_block("complete_task", {"task_id": 999})),
        resp("end_turn", text_block("Which chore did you mean?")),
    ])
    result = ClaudeInterpreter(client=client).interpret(h.eng, h.p("Kene"), "done")
    assert result.calls[0]["result"]["error"] == "unknown_task"
    assert msgs.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_falls_back_to_template_reply(h, trash_ask):
    client, _ = fake_client([resp("refusal")])
    inbound.handle(h.eng, ClaudeInterpreter(client=client), h.p("Kene")["phone"], "hmm")
    assert "another way" in h.sent_to("Kene")[-1]["body"]


def test_connection_error_is_model_unavailable(h, trash_ask):
    import anthropic
    import httpx2

    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    client, _ = fake_client([err])
    with pytest.raises(ModelUnavailable):
        ClaudeInterpreter(client=client).interpret(h.eng, h.p("Kene"), "done")


def test_eval_cases_build():
    """Every eval case's setup is valid, so `chorebot eval` only fails on model behavior."""
    from evals.run import ROOT, build

    for case in json.loads((ROOT / "evals" / "cases.json").read_text()):
        eng, aliases = build(case)
        assert eng.person_by_name(case["from"]), case["name"]
        for v in json.dumps(case["expect"]).split('"'):
            if v.startswith("$"):
                assert v[1:] in aliases, case["name"]
