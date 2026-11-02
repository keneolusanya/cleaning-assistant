"""SQLite schema and connection (§2)."""

from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT NOT NULL UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    paused_from TEXT,
    paused_until TEXT,
    opted_out_at TEXT,
    photo_mode_until TEXT,
    no_response_streak INTEGER NOT NULL DEFAULT 0,
    last_inactivity_check_at TEXT,
    pending_stop_confirm_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_weights (
    person_id INTEGER NOT NULL REFERENCES people(id),
    family TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (person_id, family)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    chore TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open, done, unresolved, cancelled
    source TEXT NOT NULL,                  -- scheduled, reask, reported, spinoff, sink_check, claim
    parts TEXT,                            -- JSON list of bathroom parts still to do
    reporter_id INTEGER REFERENCES people(id),
    note TEXT,
    round INTEGER NOT NULL DEFAULT 1,      -- reported tasks start a fresh round of asks daily
    next_round_at TEXT,
    created_at TEXT NOT NULL,
    ask_after TEXT NOT NULL,
    window_end TEXT,                       -- NULL for reported tasks
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, chore);

CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    person_id INTEGER NOT NULL REFERENCES people(id),
    round INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending', -- pending, accepted, snoozed, declined, no_response, excluded, closed
    created_at TEXT NOT NULL,
    sent_at TEXT,
    nudges INTEGER NOT NULL DEFAULT 0,
    next_nudge_at TEXT,
    remind_at TEXT,
    checkin_at TEXT,
    lapse_at TEXT,
    closed_at TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS asks_task ON asks(task_id, status);
CREATE INDEX IF NOT EXISTS asks_person ON asks(person_id, status);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    task_id INTEGER REFERENCES tasks(id),
    chore TEXT NOT NULL,
    points REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    reason TEXT NOT NULL,
    photo INTEGER NOT NULL DEFAULT 0,
    retroactive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_person ON ledger(person_id, created_at);

-- Fair-share slices (§3): when a ledger row's points count, each person active at
-- that moment owes `fraction` of them. Kept as fractions so a row whose points
-- change later (disputes, photo mode) carries its shares with it.
CREATE TABLE IF NOT EXISTS shares (
    ledger_id INTEGER NOT NULL REFERENCES ledger(id),
    person_id INTEGER NOT NULL REFERENCES people(id),
    fraction REAL NOT NULL,
    PRIMARY KEY (ledger_id, person_id)
);

-- v2 (§7). Created now so the schema doesn't need a migration later.
CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledger(id),
    disputer_id INTEGER NOT NULL REFERENCES people(id),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    person_id INTEGER REFERENCES people(id),
    direction TEXT NOT NULL,          -- in, out
    body TEXT NOT NULL,
    media TEXT,                       -- JSON list of {url, content_type}
    purpose TEXT,                     -- for outgoing: reply, ask, nudge, ...
    created_at TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 1,  -- incoming texts waiting on the model are 0
    provider_sid TEXT
);
CREATE INDEX IF NOT EXISTS messages_person ON messages(person_id, created_at);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    purpose TEXT NOT NULL,
    priority INTEGER NOT NULL,
    facts TEXT NOT NULL,              -- JSON handed to the message writer at send time
    ask_id INTEGER REFERENCES asks(id),
    task_id INTEGER REFERENCES tasks(id),
    send_after TEXT NOT NULL,
    held INTEGER NOT NULL DEFAULT 0,  -- waiting for a morning slot (§5 jitter)
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, sent, dropped, cancelled, failed
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    body TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, send_after);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def loads(s: str | None):
    return json.loads(s) if s else None
