import random
from datetime import datetime, timedelta

import pytest

from chorebot import scheduler
from chorebot.clock import TZ, FakeClock
from chorebot.db import connect
from chorebot.engine import Engine
from chorebot.roster import seed
from chorebot.sms import ConsoleSender
from chorebot.writer import TemplateWriter

PEOPLE = {
    "admin": "Kene",
    "people": [
        {"name": "Kene", "phone": "+15550000001", "kitchen": 3},
        {"name": "Salajan", "phone": "+15550000002", "kitchen": 3},
        {"name": "Alula", "phone": "+15550000003", "kitchen": 3},
        {"name": "Yonas", "phone": "+15550000004", "kitchen": 2},
        {"name": "Schneider", "phone": "+15550000005", "kitchen": 2},
        {"name": "Jones", "phone": "+15550000006", "kitchen": 2},
        {"name": "Allahji", "phone": "+15550000007", "kitchen": 1},
    ],
}


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


class Harness:
    def __init__(self, start: datetime):
        self.clock = FakeClock(start)
        self.sender = ConsoleSender()
        self.eng = Engine(connect(":memory:"), self.clock, self.sender, TemplateWriter(), rng=random.Random(7))
        seed(self.eng, PEOPLE)
        self.db = self.eng.db

    def quiet_calendar(self):
        """Push every scheduled chore and sink check far out, so a test sees only what it sets up."""
        from chorebot import constants as C
        from chorebot.db import kv_set

        for chore in C.SCHEDULED_CHORES + ["sink_check"]:
            kv_set(self.db, f"due:{chore}", "2030-01-01T00:00:00+00:00")

    def p(self, name):
        return self.eng.person_by_name(name)

    def pid(self, name):
        return self.p(name)["id"]

    def tool(self, name, tool, **args):
        return self.eng.call_tool(self.p(name), tool, args)

    def tick(self):
        scheduler.tick(self.eng)

    def advance(self, delta: timedelta, step=timedelta(minutes=5)):
        end = self.clock.now() + delta
        while self.clock.now() < end:
            self.clock.advance(min(step, end - self.clock.now()))
            self.tick()

    def sent_to(self, name, purpose=None):
        phone = self.p(name)["phone"]
        return [m for m in self.sender.sent if m["phone"] == phone and (purpose is None or m["purpose"] == purpose)]

    def task(self, chore, source="reported"):
        """An open task with nobody asked yet."""
        with self.eng.tx():
            return self.eng.create_task(chore, source)

    def ask(self, task, name, sent=True):
        with self.eng.tx():
            a = self.eng.ask(task, self.pid(name), inline=sent)
        return a

    def asks(self, task_id):
        return self.db.execute("SELECT * FROM asks WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()


@pytest.fixture
def h():
    # Thursday 2pm, outside quiet hours, well before the first scheduled chores.
    return Harness(local(2026, 11, 5, 14, 0))
