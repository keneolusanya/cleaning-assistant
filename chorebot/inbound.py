"""Handling one incoming text. STOP/START and admin commands never reach the model (§6, §9)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from . import admin
from .clock import iso, parse
from .interpreter import ModelUnavailable
from .writer import template

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("chorebot.inbound")

STOP_WORDS = {"stop", "stopall", "unsubscribe"}
AMBIGUOUS_STOP = {"cancel", "end", "quit"}
START_WORDS = {"start", "unstop"}
STOP_CONFIRM_WINDOW = timedelta(hours=1)


def keyword(body: str) -> str:
    """The whole text, normalized, so 'Stop.' matches but 'stop asking me' doesn't."""
    return body.strip().lower().rstrip(".!? ").strip()


def handle(eng: "Engine", interpreter, phone: str, body: str, media: list[dict] | None = None):
    """media: [{url, content_type, data?}] — data (bytes) is passed to the model, url is logged."""
    person = eng.person_by_phone(phone)
    if person is None:
        log.info("ignoring text from unknown number")
        return
    media = media or []
    logged_media = [{"url": m.get("url"), "content_type": m.get("content_type")} for m in media] or None
    with eng.tx():
        msg_id = eng.log_message(person["id"], "in", body, media=logged_media, processed=False)
        # Any reply resets the silence streak (§6).
        eng.db.execute("UPDATE people SET no_response_streak = 0 WHERE id = ?", (person["id"],))
    person = eng.person(person["id"])
    word = keyword(body)

    pending_stop = parse(person["pending_stop_confirm_at"])
    pending_stop = bool(pending_stop and eng.now - pending_stop < STOP_CONFIRM_WINDOW)

    if word in STOP_WORDS or (pending_stop and word == "yes"):
        if not person["opted_out_at"]:
            eng.opt_out(person["id"])
            eng.reply(person, template("stop_confirm", {}, person["name"]), purpose="stop_confirm")
        return _done(eng, msg_id)
    if word in START_WORDS or (word == "yes" and person["opted_out_at"]):
        if person["opted_out_at"]:
            eng.opt_in(person["id"])
            eng.reply(eng.person(person["id"]), template("start_confirm", {}, person["name"]), purpose="start_confirm")
        else:
            eng.reply(person, "You're already opted in!")
        return _done(eng, msg_id)
    if person["opted_out_at"]:
        return _done(eng, msg_id)  # nothing goes to an opted-out person
    if word in AMBIGUOUS_STOP:
        eng.db.execute("UPDATE people SET pending_stop_confirm_at = ? WHERE id = ?", (iso(eng.now), person["id"]))
        eng.reply(person, template("stop_question", {}, person["name"]), purpose="stop_question")
        return _done(eng, msg_id)
    if person["pending_stop_confirm_at"]:
        eng.db.execute("UPDATE people SET pending_stop_confirm_at = NULL WHERE id = ?", (person["id"],))

    if person["is_admin"] and word.startswith("admin"):
        eng.reply(person, admin.handle_text(eng, body), purpose="admin")
        return _done(eng, msg_id)

    return _interpret(eng, interpreter, person, body, media, msg_id)


def _interpret(eng, interpreter, person, body, media, msg_id) -> None:
    if interpreter is None:
        eng.reply(person, "(offline mode: no model. Use /tool in the simulator.)", purpose="reply")
        return _done(eng, msg_id)
    images = [m for m in media if m.get("data") and m.get("content_type", "").startswith("image/")]
    try:
        result = interpreter.interpret(eng, person, body, images=images, message_id=msg_id)
    except ModelUnavailable:
        log.warning("model unavailable; text %s queued for retry", msg_id)
        already = eng.db.execute(
            "SELECT 1 FROM messages WHERE person_id = ? AND purpose = 'api_down' AND created_at >= ?",
            (person["id"], iso(eng.now - timedelta(hours=1))),
        ).fetchone()
        if not already:
            eng.reply(person, template("api_down", {}, person["name"]), purpose="api_down")
        return
    eng.reply(eng.person(person["id"]), result.reply or template("fallback_reply", {}, person["name"]))
    _done(eng, msg_id)
    return result


def _done(eng: "Engine", msg_id: int) -> None:
    eng.db.execute("UPDATE messages SET processed = 1 WHERE id = ?", (msg_id,))


def retry_unprocessed(eng: "Engine", interpreter, max_age: timedelta = timedelta(hours=6)) -> None:
    """Texts that arrived while the API was down (§12)."""
    if interpreter is None:
        return
    for m in eng.db.execute(
        "SELECT * FROM messages WHERE direction = 'in' AND processed = 0 AND created_at >= ? ORDER BY id",
        (iso(eng.now - max_age),),
    ).fetchall():
        person = eng.person(m["person_id"])
        if person["opted_out_at"]:
            _done(eng, m["id"])
            continue
        try:
            result = interpreter.interpret(eng, person, m["body"], images=[], message_id=m["id"])
        except ModelUnavailable:
            return
        eng.reply(eng.person(person["id"]), result.reply or template("fallback_reply", {}, person["name"]))
        _done(eng, m["id"])
