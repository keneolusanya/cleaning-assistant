"""Seeding people and kitchen tiers from people.toml (CLI-only roster edits, §0)."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from .clock import iso

if TYPE_CHECKING:
    from .engine import Engine


def load(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def seed(eng: "Engine", config: dict) -> list[str]:
    """Insert or update everyone in the config. Never deletes anyone."""
    admin = (config.get("admin") or "").lower()
    names = []
    with eng.tx():
        for entry in config["people"]:
            name, phone = entry["name"], entry["phone"]
            existing = eng.person_by_name(name)
            if existing:
                eng.db.execute("UPDATE people SET phone = ?, is_admin = ? WHERE id = ?",
                               (phone, int(name.lower() == admin), existing["id"]))
                pid = existing["id"]
            else:
                pid = eng.db.execute(
                    "INSERT INTO people(name, phone, is_admin, created_at) VALUES (?, ?, ?, ?)",
                    (name, phone, int(name.lower() == admin), iso(eng.now)),
                ).lastrowid
            if "kitchen" in entry:
                eng.db.execute(
                    "INSERT INTO usage_weights(person_id, family, weight) VALUES (?, 'kitchen', ?) "
                    "ON CONFLICT(person_id, family) DO UPDATE SET weight = excluded.weight",
                    (pid, float(entry["kitchen"])),
                )
            names.append(name)
    return names
