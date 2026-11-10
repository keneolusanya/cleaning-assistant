"""Outgoing message text (§11). Claude writes varied messages; templates are the fallback."""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("chorebot.writer")

WRITER_MODEL = os.environ.get("CHOREBOT_WRITER_MODEL", "claude-haiku-5-5")


def _deadline(f: dict) -> str:
    return f" by {f['deadline']}" if f.get("deadline") else ""


def _parts(f: dict) -> str:
    parts = f.get("parts_remaining")
    if not parts:
        return ""
    return " (" + ", ".join(p.replace("sink_mirror", "sink + mirror") for p in parts) + ")"


def _recap(name: str, f: dict) -> str:
    lines = [f"Weekly recap for {name}:"]
    lines.append(f"Your points: {f['your_balance']} (suite average {f['group_average']}).")
    diff = f["vs_your_share"]
    if abs(diff) < 0.05:
        lines.append("You're right at your fair share.")
    else:
        lines.append(f"You're {abs(diff)} points {'ahead of' if diff > 0 else 'behind'} your share.")
    if f["you_did"]:
        lines.append("You did: " + ", ".join(d["chore"] for d in f["you_did"]) + ".")
    else:
        lines.append("Nothing from you this week.")
    if f["suite_done"]:
        lines.append("Suite-wide: " + ", ".join(f"{c} x{n}" for c, n in f["suite_done"].items()) + ".")
    if f["still_undone"]:
        lines.append("Still undone: " + ", ".join(f["still_undone"]) + ".")
    if f["your_no_responses"]:
        lines.append(f"No reply from you on {f['your_no_responses']} ask(s).")
    if f.get("paused_until"):
        lines.append(f"You're paused until {f['paused_until']}.")
    return "\n".join(lines)


def template(purpose: str, f: dict, name: str) -> str:
    chore = f.get("chore", "")
    bonus = " Someone passed on it, so it's worth extra." if f.get("someone_passed_on_it") else ""
    if purpose == "ask":
        return (f"Hey {name}! {chore.capitalize()}{_parts(f)} needs doing{_deadline(f)}. "
                f"Can you take it?{bonus} Yes, no, or 'remind me later' all work.")
    if purpose == "snooze_reminder":
        return f"Reminder like you asked: {chore}{_parts(f)}{_deadline(f)}. Still good to do it?"
    if purpose == "nudge":
        return f"Quick nudge on {chore}{_deadline(f)}. Can you take it? A no is totally fine."
    if purpose == "checkin":
        if f.get("parts_remaining"):
            return f"How'd {chore} go? What did you get to?"
        return f"How's {chore} going? Text me when it's done."
    if purpose == "done_check":
        return f"{f.get('reported_by', 'Someone')} says {chore} got done. Was that you? If so, reply done."
    if purpose == "inactivity_check":
        return ("Haven't heard back on the last few, all good? "
                "If you're away, tell me until when and I'll pause you.")
    if purpose == "fridge_heads_up":
        return (f"Heads up: the fridge gets cleared out starting {f.get('clear_out_from')}. "
                "Label or claim anything you want to keep!")
    if purpose == "sink_check":
        return ("Got anything in the sink? If so, wash it, and if you've got a minute, help with the rest. "
                "'Nothing of mine' is a fine answer too.")
    if purpose == "recap":
        return _recap(name, f)
    if purpose == "intro":
        return (f"Hi {name}! I'm the suite chore bot. I'll text now and then about chores; just reply like normal. "
                "Saying no is always fine. Text STOP anytime to opt out.")
    if purpose == "stop_confirm":
        return "You're opted out, no more texts from the chore bot. Text START to undo."
    if purpose == "start_confirm":
        return "Welcome back! You're opted in again."
    if purpose == "stop_question":
        return "Want to stop all texts from the chore bot? Reply STOP to confirm."
    if purpose == "api_down":
        return "Got your message! I'm having a technical hiccup, I'll reply soon."
    if purpose == "fallback_reply":
        return "Sorry, I didn't catch that. Could you say it another way?"
    raise ValueError(f"no template for {purpose}")


class TemplateWriter:
    def write(self, purpose: str, facts: dict, name: str, recent: list[str]) -> str:
        return template(purpose, facts, name)


WRITER_SYSTEM = """You write text messages from a chore bot to one of seven college suitemates.

Style: short (1-3 sentences, recaps can be a few short lines), casual, warm. Plain text, no markdown, no emoji spam (one emoji max, optional). Never guilt-trip. Declining is always fine and you can say so.

Use only the facts you're given. Never invent chores, times, names or numbers. Copy numbers exactly. Don't mention task IDs. Everyone in the suite is male: refer to people by name or with he/him, never she/her. Use time phrases exactly as given ("tonight", "this Sunday") and never write numeric dates. Only mention point values in a recap.

Vary your wording from the recent messages you're shown so the bot doesn't sound repetitive.

Per purpose:
- ask: ask in plain words if they can do the chore, like "could you clean the common area?" or "could you do the kitchen reset by this Sunday night?" Only say when if you're given a deadline. If someone_passed_on_it, add a sentence that ends on the word extra, like "Someone passed on it, so it's worth extra." (don't say who passed). For bathrooms, list the parts and say doing part of it is fine.
- snooze_reminder: the reminder they asked for. Ask if they can do it now.
- nudge: a light follow-up on an ask they haven't answered.
- checkin: they agreed to do it. For bathrooms ask "what did you get to?" rather than "did you finish?". Otherwise ask how it's going / to text when done.
- done_check: someone said this chore got done. Ask if it was them.
- inactivity_check: a check on whether they're OK or away, not a reminder of what they missed. Offer to pause them if they say until when.
- fridge_heads_up: the fridge gets cleared from the given time; label or claim anything to keep.
- sink_check: "got anything in the sink? If so, wash it, and if you've got a minute, help with the rest." Easy and blame-free; "nothing of mine" is a fine answer.
- recap: weekly recap. Include: their points and the suite average, how far ahead/behind their share they are (positive vs_your_share = ahead), what they did, what got done suite-wide, what's still undone, and their no-response count if nonzero. No other person's numbers.

Output only the message text."""


class ClaudeWriter:
    """Haiku writes the message; any failure falls back to the template."""

    def __init__(self, client=None, model: str = WRITER_MODEL):
        import anthropic

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic(timeout=30.0, max_retries=1)
        self.model = model

    def write(self, purpose: str, facts: dict, name: str, recent: list[str]) -> str:
        given = {k: v for k, v in facts.items() if v is not None}
        payload = {"purpose": purpose, "to": name, "facts": given, "recent_messages_to_them": recent[-4:]}
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                output_config={"effort": "low"},
                system=WRITER_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            )
        except Exception:  # the template is always a safe fallback
            log.exception("writer API call failed; using template")
            return template(purpose, facts, name)
        if resp.stop_reason == "refusal":
            return template(purpose, facts, name)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or template(purpose, facts, name)
