from datetime import timedelta

import pytest

from chorebot import fairness, inbound
from chorebot.interpreter import ModelUnavailable, Result


class FakeInterpreter:
    def __init__(self, fail=False):
        self.seen = []
        self.fail = fail

    def interpret(self, eng, person, text, images=None, message_id=None):
        if self.fail:
            raise ModelUnavailable("down")
        self.seen.append((person["name"], text))
        return Result(reply=f"got: {text}")


@pytest.fixture
def fi():
    return FakeInterpreter()


def text(h, fi, name, body):
    return inbound.handle(h.eng, fi, h.p(name)["phone"], body)


def test_stop_must_be_the_whole_text(h, fi):
    text(h, fi, "Salajan", "stop asking me about trash")
    assert fi.seen == [("Salajan", "stop asking me about trash")]
    assert h.p("Salajan")["opted_out_at"] is None
    text(h, fi, "Kene", " Stop. ")
    assert h.p("Kene")["opted_out_at"] is not None
    assert h.sent_to("Kene")[-1]["purpose"] == "stop_confirm"
    assert "START" in h.sent_to("Kene")[-1]["body"]


def test_stop_hands_off_in_flight_asks(h, fi):
    h.quiet_calendar()
    t = h.task("trash")
    h.ask(t, "Kene")
    text(h, fi, "Kene", "STOP")
    holders = h.eng.active_asks(t["id"])
    assert len(holders) == 1 and holders[0]["person_id"] != h.pid("Kene")


def test_opted_out_person_gets_nothing(h, fi):
    text(h, fi, "Kene", "STOP")
    n = len(h.sender.sent)
    text(h, fi, "Kene", "done")
    assert fi.seen == [] and len(h.sender.sent) == n


def test_yes_restarts_only_when_opted_out(h, fi):
    text(h, fi, "Kene", "yes")
    assert fi.seen == [("Kene", "yes")]  # a normal reply for an opted-in person
    text(h, fi, "Salajan", "STOP")
    text(h, fi, "Salajan", "Yes")
    assert h.p("Salajan")["opted_out_at"] is None
    assert h.sent_to("Salajan")[-1]["purpose"] == "start_confirm"


def test_cancel_alone_asks_to_confirm(h, fi):
    text(h, fi, "Kene", "cancel")
    assert h.p("Kene")["opted_out_at"] is None
    assert h.sent_to("Kene")[-1]["purpose"] == "stop_question"
    text(h, fi, "Kene", "STOP")
    assert h.p("Kene")["opted_out_at"] is not None


def test_cancel_then_other_reply_clears_it(h, fi):
    text(h, fi, "Kene", "Cancel")
    text(h, fi, "Kene", "nah I meant cancel my reminder")
    assert h.p("Kene")["pending_stop_confirm_at"] is None
    assert fi.seen == [("Kene", "nah I meant cancel my reminder")]
    text(h, fi, "Kene", "yes")
    assert h.p("Kene")["opted_out_at"] is None


def test_cancel_then_yes_confirms(h, fi):
    text(h, fi, "Kene", "quit")
    text(h, fi, "Kene", "yes")
    assert h.p("Kene")["opted_out_at"] is not None


def test_start_brings_you_back_at_the_front(h, fi):
    with h.eng.tx():
        fairness.credit(h.db, h.clock.now(), h.pid("Kene"), None, "trash", 5.0, "done")
    text(h, fi, "Kene", "STOP")
    text(h, fi, "Kene", "START")
    others = [fairness.surplus(h.db, p["id"]) for p in fairness.active_people(h.db, h.clock.now())
              if p["name"] != "Kene"]
    assert fairness.surplus(h.db, h.pid("Kene")) == pytest.approx(min(others))
    adj = h.db.execute("SELECT * FROM ledger WHERE status = 'adjust'").fetchone()
    assert adj["reason"] == "rejoin adjustment"


def test_admin_commands_only_from_admin(h, fi):
    text(h, fi, "Kene", "admin tier Allahji 2")
    assert fairness.weight(h.db, h.pid("Allahji"), "kitchen") == 2
    assert "2" in h.sent_to("Kene")[-1]["body"]
    text(h, fi, "Salajan", "admin tier Salajan 0")
    assert fairness.weight(h.db, h.pid("Salajan"), "kitchen") == 3
    assert fi.seen == [("Salajan", "admin tier Salajan 0")]


def test_admin_reset_needs_confirm(h, fi):
    text(h, fi, "Kene", "admin reset points")
    assert "confirm" in h.sent_to("Kene")[-1]["body"]
    text(h, fi, "Kene", "admin reset points confirm")
    assert "reset" in h.sent_to("Kene")[-1]["body"].lower()


def test_unknown_number_is_ignored(h, fi):
    inbound.handle(h.eng, fi, "+19999999999", "hi")
    assert fi.seen == [] and h.sender.sent == []
    assert h.db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_any_text_resets_no_response_streak(h, fi):
    h.db.execute("UPDATE people SET no_response_streak = 2 WHERE name = 'Jones'")
    text(h, fi, "Jones", "hey")
    assert h.p("Jones")["no_response_streak"] == 0


def test_replies_go_out_during_quiet_hours(h, fi):
    h.clock.advance(timedelta(hours=11))  # 1am
    text(h, fi, "Kene", "what are my points")
    assert h.sent_to("Kene")[-1]["body"] == "got: what are my points"


def test_api_down_queues_text_and_retries(h):
    down = FakeInterpreter(fail=True)
    text(h, down, "Kene", "done")
    text(h, down, "Kene", "hello?")
    assert [m["purpose"] for m in h.sent_to("Kene")] == ["api_down"]  # only once
    assert h.db.execute("SELECT COUNT(*) AS n FROM messages WHERE processed = 0").fetchone()["n"] == 2
    up = FakeInterpreter()
    inbound.retry_unprocessed(h.eng, up)
    assert up.seen == [("Kene", "done"), ("Kene", "hello?")]
    assert h.db.execute("SELECT COUNT(*) AS n FROM messages WHERE processed = 0").fetchone()["n"] == 0
