"""Turns an incoming text into engine tool calls plus a reply (§9, §10).

The model picks tools; the engine validates and executes them. The model never
sees anyone else's balance and can only act on IDs from the snapshot.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import constants as C
from . import snapshot

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("chorebot.interpreter")

INTERPRETER_MODEL = os.environ.get("CHOREBOT_INTERPRETER_MODEL", "claude-sonnet-5-5")
MAX_TURNS = 6

_task_id = {"type": "integer", "description": "A task_id from the snapshot."}
_parts = {
    "type": "array",
    "items": {"type": "string", "enum": C.BATHROOM_PARTS},
    "description": "Bathroom parts actually done. Only for bathrooms.",
}
_photo = {"type": "boolean", "description": "True if they attached a usable photo of this chore."}
_chore = {"type": "string", "enum": C.CHORES}

TOOLS = [
    {
        "name": "accept_task",
        "description": "They agree to do a task (or volunteer for an open one). Schedules one check-in.",
        "input_schema": {"type": "object", "properties": {"task_id": _task_id}, "required": ["task_id"],
                         "additionalProperties": False},
    },
    {
        "name": "decline_task",
        "description": "They can't or won't do a task they were asked about. Free, no penalty. The engine asks the next person.",
        "input_schema": {"type": "object",
                         "properties": {"task_id": _task_id, "reason": {"type": "string"}},
                         "required": ["task_id"], "additionalProperties": False},
    },
    {
        "name": "snooze_task",
        "description": "Remind them later about a task they were asked about. remind_at is ISO 8601 with offset, Boston time.",
        "input_schema": {"type": "object",
                         "properties": {"task_id": _task_id, "remind_at": {"type": "string"}},
                         "required": ["task_id", "remind_at"], "additionalProperties": False},
    },
    {
        "name": "complete_task",
        "description": "They did an open task (just now, in answer to an ask or a check-in). Writes their credit. "
                       "For bathrooms pass exactly the parts they did; the rest is spun off to someone else.",
        "input_schema": {"type": "object",
                         "properties": {"task_id": _task_id, "parts": _parts, "photo": _photo},
                         "required": ["task_id"], "additionalProperties": False},
    },
    {
        "name": "claim_past_work",
        "description": "They did a chore earlier without being asked, e.g. 'I already did the kitchen yesterday'. "
                       "Trusted, flagged in the recap. If that chore has an open task it gets closed. "
                       "For dishes, set dishes to 'own' or 'others' (others = washed other people's too).",
        "input_schema": {"type": "object",
                         "properties": {"chore": _chore, "parts": _parts, "photo": _photo,
                                        "note": {"type": "string"},
                                        "dishes": {"type": "string", "enum": ["own", "others"]}},
                         "required": ["chore"], "additionalProperties": False},
    },
    {
        "name": "report_needed",
        "description": "A chore needs doing ('trash is full', 'sink is full', 'fridge is gross'). Creates a task and "
                       "asks whoever's furthest behind, which may be the reporter. Fridge reports send everyone a 24h heads-up first.",
        "input_schema": {"type": "object",
                         "properties": {"chore": _chore, "note": {"type": "string"}, "photo": _photo},
                         "required": ["chore"], "additionalProperties": False},
    },
    {
        "name": "report_done_by_other",
        "description": "They say someone else did a chore ('Alula took out the trash'). Checks with that person "
                       "(or whoever was asked) so they can claim it. Gives the reporter no credit.",
        "input_schema": {"type": "object",
                         "properties": {"chore": _chore, "person_name": {"type": "string"}},
                         "required": ["chore"], "additionalProperties": False},
    },
    {
        "name": "resolve_sink_check",
        "description": "Answer to a sink check. none_mine = nothing of theirs; washed_mine = washed their own; "
                       "washed_others = also washed other people's.",
        "input_schema": {"type": "object",
                         "properties": {"task_id": _task_id,
                                        "outcome": {"type": "string",
                                                    "enum": ["none_mine", "washed_mine", "washed_others"]}},
                         "required": ["task_id", "outcome"], "additionalProperties": False},
    },
    {
        "name": "pause",
        "description": "They're away. No asks until `until` (ISO 8601 with offset, Boston time). "
                       "Their in-flight asks go to someone else; their standing freezes.",
        "input_schema": {"type": "object", "properties": {"until": {"type": "string"}}, "required": ["until"],
                         "additionalProperties": False},
    },
    {
        "name": "unpause",
        "description": "They're back early.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

SYSTEM = """You are the text-message interface of a chore bot for a suite of seven college students (2 bathrooms, a kitchen, a common area). You read one incoming text, act on it through tools, and write the reply text.

How the system works (so you can answer questions):
- A separate engine owns all state and math. You never compute points or decide who gets asked; tools do that and tell you the result.
- Points: trash 1, dishes 0.25 for washing your own / 0.75 if you also help with others', each bathroom 3 (four parts: toilet, sink_mirror, shower, floor, 0.75 each; partial credit is normal), kitchen reset 2, fridge clear-out 1, common area 1.5. Taking something someone else passed on is worth 1.25x.
- Whoever is furthest behind their fair share gets asked first. Declining is always free. Kitchen and fridge shares are weighted by kitchen use (tiers set by Kene, the admin).
- The bot never starts a text 12am-10am and starts at most 3 texts per person per day. Replies don't count.
- "away until X" pauses someone. STOP (texted alone) opts out; START opts back in.

Rules:
1. Only use task_ids that appear in the snapshot. Never guess one.
2. If a message could mean more than one task, ask which one instead of guessing. Asking is a correct outcome.
3. Bathrooms: credit only the parts they say they did. "Done"/"finished" for a bathroom without details: ask what they got to (toilet, sink + mirror, shower, floor) before calling complete_task. "All of it"/"the whole thing" means all four parts.
4. "Done" in reply to an ask or check-in: complete_task. Something done earlier that nobody asked them about: claim_past_work. Answers to a sink check: resolve_sink_check.
5. Times: convert relative times ("tomorrow", "after my 3pm class", "Sunday") using the current time in the snapshot. Pass ISO 8601 with the America/New_York offset. "After my 3pm class" with no end time: assume about an hour later. "Away until Sunday" means through the end of Sunday (23:59).
6. Photos: you can see attached photos. Check only that the photo shows the space the chore is about and plausibly looks done (or, for a report, looks like it needs doing). If not, don't call the tool yet; ask for another photo. Pass photo=true only for a usable photo. A photo never changes points.
7. Privacy: you only know the sender's own points and the group average. Never state or guess anyone else's points or standing. Who did which chore is not private.
8. You can't change kitchen tiers, reset points or edit the roster. Say Kene (the admin) handles that.
9. If they want texts to stop in other words ("stop texting me"), tell them to text the word STOP by itself. Don't treat it as a pause.
10. Questions ("what are my points?", "how does this work?", "what's open?") are answered from the snapshot with no tool call.
11. Never say something happened unless a tool result says so. If a tool returns ok:false, explain briefly or ask a follow-up; don't pretend it worked.
12. If report_needed returns asked_you=true, the sender is now being asked to do it: your reply must ask them (yes / no / remind me later).
13. Small talk or thanks: reply briefly, no tool.

Reply style: 1-3 short sentences, casual, warm, no guilt-tripping, plain text (no markdown). Don't mention task IDs, tools, or the engine."""


@dataclass
class Result:
    reply: str
    calls: list[dict] = field(default_factory=list)  # [{name, input, result}]


class ModelUnavailable(Exception):
    """The API couldn't be reached. The incoming text stays unprocessed and gets retried."""


class ClaudeInterpreter:
    def __init__(self, client=None, model: str = INTERPRETER_MODEL):
        import anthropic

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic(timeout=60.0, max_retries=2)
        self.model = model

    def _create(self, messages):
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "medium"},
        )
        if self.model == "claude-sonnet-5-5":
            # Server-side refusal fallback (Claude API only).
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        a = self.anthropic
        try:
            return self.client.beta.messages.create(**kwargs)
        except (a.APIConnectionError, a.RateLimitError, a.AuthenticationError, a.InternalServerError) as e:
            raise ModelUnavailable(str(e)) from e
        except a.APIStatusError as e:
            if e.status_code >= 500:
                raise ModelUnavailable(str(e)) from e
            raise

    def interpret(self, eng: "Engine", person, text: str, images: list[dict] | None = None,
                  message_id: int | None = None) -> Result:
        snap = snapshot.build(eng, person, exclude_message_id=message_id)
        content: list[dict] = [{"type": "text", "text": "STATE SNAPSHOT\n" + json.dumps(snap, indent=1, default=str)}]
        for img in images or []:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": img["content_type"],
                "data": base64.standard_b64encode(img["data"]).decode()}})
        label = " (with photo)" if images else ""
        content.append({"type": "text", "text": f"New text from {person['name']}{label}: {text}"})
        messages = [{"role": "user", "content": content}]

        calls: list[dict] = []
        resp = None
        for _ in range(MAX_TURNS):
            resp = self._create(messages)
            if resp.stop_reason == "refusal":
                return Result(reply="", calls=calls)
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in uses:
                out = eng.call_tool(person, b.name, dict(b.input or {}))
                calls.append({"name": b.name, "input": dict(b.input or {}), "result": out})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str),
                                **({"is_error": True} if not out.get("ok") else {})})
            messages.append({"role": "user", "content": results})
        reply = "".join(b.text for b in resp.content if b.type == "text").strip() if resp else ""
        return Result(reply=reply, calls=calls)
