from datetime import timedelta

import pytest

from chorebot import fairness
from chorebot.clock import iso


def credit(h, name, chore, points):
    with h.eng.tx():
        return fairness.credit(h.db, h.clock.now(), h.pid(name), None, chore, points, "done")


def test_kitchen_points_split_by_usage_weight(h):
    credit(h, "Kene", "kitchen", 2.0)
    # Weights 3+3+3+2+2+2+1 = 16
    assert fairness.fair_share(h.db, h.pid("Kene")) == pytest.approx(2 * 3 / 16)
    assert fairness.fair_share(h.db, h.pid("Yonas")) == pytest.approx(2 * 2 / 16)
    assert fairness.fair_share(h.db, h.pid("Allahji")) == pytest.approx(2 * 1 / 16)
    assert fairness.balance(h.db, h.pid("Kene")) == 2.0
    assert fairness.surplus(h.db, h.pid("Kene")) == pytest.approx(2 - 6 / 16)


def test_other_chores_split_equally(h):
    credit(h, "Kene", "trash", 1.0)
    for name in ("Kene", "Allahji", "Jones"):
        assert fairness.fair_share(h.db, h.pid(name)) == pytest.approx(1 / 7)


def test_paused_people_accrue_no_share(h):
    h.tool("Jones", "pause", until=iso(h.clock.now().replace(year=2027)))
    credit(h, "Kene", "trash", 1.0)
    assert fairness.fair_share(h.db, h.pid("Jones")) == 0
    assert fairness.fair_share(h.db, h.pid("Kene")) == pytest.approx(1 / 6)


def test_group_average_excludes_paused_and_opted_out(h):
    credit(h, "Kene", "trash", 1.0)
    h.eng.opt_out(h.pid("Allahji"))
    h.tool("Jones", "pause", until=iso(h.clock.now().replace(year=2027)))
    assert fairness.group_average(h.db, h.clock.now()) == pytest.approx(1 / 5)


def test_ranking_puts_furthest_behind_first(h):
    for name in ("Kene", "Salajan", "Yonas", "Schneider", "Jones", "Allahji"):
        credit(h, name, "trash", 1.0)
    task = h.task("trash")
    ranked = fairness.rank(h.db, task, h.clock.now(), h.eng.rng)
    assert ranked[0]["name"] == "Alula"
    assert len(ranked) == 7


def test_bathrooms_share_one_recency_family(h):
    credit(h, "Kene", "bathroom_1", 3.0)
    assert fairness.last_done(h.db, h.pid("Kene"), "bathroom_2") != ""
    assert fairness.last_done(h.db, h.pid("Kene"), "kitchen") == ""


def test_weight_zero_takes_no_part(h):
    h.db.execute("UPDATE usage_weights SET weight = 0 WHERE person_id = ?", (h.pid("Allahji"),))
    task = h.task("kitchen")
    names = [p["name"] for p in fairness.rank(h.db, task, h.clock.now(), h.eng.rng)]
    assert "Allahji" not in names
    credit(h, "Kene", "kitchen", 2.0)
    assert fairness.fair_share(h.db, h.pid("Allahji")) == 0


def test_two_bathrooms_go_to_different_people(h):
    h.quiet_calendar()
    with h.eng.tx():
        a1 = h.eng.ensure_asked(h.eng.create_task("bathroom_1", "scheduled")["id"])
        a2 = h.eng.ensure_asked(h.eng.create_task("bathroom_2", "scheduled")["id"])
    assert a1["person_id"] != a2["person_id"]


def test_points_reset_zeroes_balances_and_shares(h):
    from chorebot import admin

    credit(h, "Kene", "trash", 1.0)
    h.clock.advance(timedelta(seconds=1))
    admin.reset_points(h.eng)
    assert fairness.balance(h.db, h.pid("Kene")) == 0
    assert fairness.fair_share(h.db, h.pid("Salajan")) == 0
