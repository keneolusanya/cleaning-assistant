# Suite Chore Bot — Design

**Status:** design doc, written before any code. Implementation should follow this; if it diverges, update this file.

**Scope:** 7 suitemates, 2 bathrooms, 1 kitchen, 1 common area. SMS-only interface.

---

## 0. Build decisions

- **Stack:** Python, SQLite. Times stored in UTC, all scheduling done in `America/New_York` (Boston).
- **SMS:** Twilio. Incoming texts arrive by webhook on a cheap always-on cloud host (§12 covers how the URL is protected).
- **Models:** Sonnet (`claude-sonnet-5-5`) interprets incoming texts into tool calls. Haiku (`claude-haiku-5-5`) writes outgoing messages.
- **Admin:** Kene. People, phones and kitchen tiers are seeded from a config file. Admin actions (§9) work by text from the admin's number and from a CLI. Roster changes are rare (expected at end of school year), so they're CLI/config only.
- **Onboarding:** on launch, everyone gets one intro text explaining the bot and how to STOP.
- **Points roll over** indefinitely. The admin can reset them.
- **v1 scope:** everything except disputes, photo-request mode (§7) and concentration tracking (§8). In v1, every completion and claim is trusted, with no way to challenge it.
- **Testing first:** a local simulator lets you text the bot as any suitemate from a terminal, with a fake clock, before anything goes live. Plus an eval set of tricky messages (§9).

---

## 1. Core principle

**The engine decides, the model talks.**

Ordinary code owns all state and all fairness math. The language model only does two things: interpret incoming texts into tool calls, and phrase outgoing messages.

The model never holds state, never computes points, and never decides who to ask. It can only act through a fixed list of tools, and only on IDs it was explicitly given in the current state snapshot.

Why: a model asked to remember who did what will drift, forget, and eventually invent facts. The ledger has to be auditable — when someone disputes a claim, there must be a row to point at.

---

## 2. Data model

Eight tables. Plain SQL, SQLite.

### `people`
| Field | Notes |
|---|---|
| id, name, phone | phone in E.164 |
| is_admin | true for Kene only |
| paused_from, paused_until | null when active |
| opted_out_at | set by STOP, cleared by START. Null when opted in |
| photo_mode_until | null unless in photo-request mode (§7) |
| no_response_streak | consecutive asks that ended `no_response`; reset by any incoming text |
| last_inactivity_check_at | when the last inactivity check-in went out (§6) |
| pending_stop_confirm_at | set when a lone CANCEL/END/QUIT is waiting on a confirmation (§6) |

### `usage_weights`
How much each person uses a shared space, for chore families where that differs. Sets their fair share (§3).

| Field | Notes |
|---|---|
| person_id, family | family: kitchen (covers kitchen reset and fridge). Others can be added later |
| weight | e.g. 3 / 2 / 1. 0 = takes no part in that chore. No row = weight 1 |

### `tasks`
One row per *instance* of a chore that needs doing.

| Field | Notes |
|---|---|
| id, chore | chore key: trash, dishes, bathroom_1, bathroom_2, kitchen, fridge, common |
| status | open · done · unresolved · cancelled |
| parts | for bathrooms: which parts still need doing |
| reporter_id | set when a suitemate reported it rather than the scheduler creating it |
| note | free text, e.g. "sink_check", "backup check" |
| created_at, ask_after, window_end, done_at | ask_after delays the first ask (used for the fridge heads-up); window_end is the deadline; past it, the task goes unresolved |

### `asks`
One row each time the bot asks a specific person about a specific task. A task can have several asks.

| Field | Notes |
|---|---|
| id, task_id, person_id | |
| status | pending · accepted · snoozed · declined · no_response · excluded · closed |
| sent_at, nudges, next_nudge_at | `excluded` = deliberately skipped (e.g. already did part of it) |

### `ledger`
The credit record. Append-only in spirit; rows change status, not points — except dispute outcomes and photo-mode half credit (§7).

| Field | Notes |
|---|---|
| id, person_id, task_id, chore, points | |
| status | confirmed · disputed · contested · pending_photo · void · adjust |
| reason | "done", "covered", "claimed: ...", "rejoin adjustment" |
| photo, retroactive | flags, used in the weekly recap |
| created_at | |

`contested` = a dispute ended in a 50/50 split. It counts toward the balance at the reduced amount, and toward photo-request mode.

### `disputes`
One row per dispute. Kept separate from the ledger so the disputer is recorded but never revealed to the claimant.

| Field | Notes |
|---|---|
| id, ledger_id, disputer_id | |
| reason | free text from the disputer |
| status | open · restored · partial · void · split · withdrawn |
| opened_at, expires_at | expires_at = opened_at + 48h |

### `messages`
Every text in and out. Needed for debugging misreads, for varying outgoing wording, and as the record when someone says "it never texted me."

### `outbox`
Outgoing messages waiting on quiet hours or the daily cap. Fields: person_id, body, purpose, priority, send_after, sent. Priority order is in §5.

Plus a small `kv` table for scheduler bookkeeping (next kitchen date, next suite-wide sink check, which Sundays the recap already went out).

---

## 3. The fairness rule

Each person has a **balance**: the sum of their confirmed, contested and adjust ledger points. Disputed and pending_photo rows don't count until resolved.

Each person also has a **fair share**: the points they'd be expected to have done, given what's been done in the suite while they were active and how much they use each space.

- Whenever points are credited for a chore, that amount is split across everyone active at that moment, in proportion to their weight for that chore family (`usage_weights`, default 1). Each person's slice is added to their fair share.
- With all weights equal, everyone's fair share is the same, and ranking reduces to plain balance.
- Paused time adds nothing to your fair share, so you come back level without needing a separate adjustment row.

**Surplus** = balance − fair share. Positive means you've done more than your share.

**Kitchen weights (proposed, group to confirm):**

| Tier | People | Weight |
|---|---|---|
| Heavy | Kene, Salajan, Alula | 3 |
| Medium | Yonas, Schneider, Jones | 2 |
| Light | Allahji | 1 |

Applies to kitchen reset and fridge clear-out. Everything else is weight 1 for everyone. With these weights, each heavy user's share of kitchen work is 3/16 (~19%), each medium user's is 2/16 (~13%), and Allahji's is 1/16 (~6%). Only the admin changes tiers, after the group agrees. Nobody sets their own.

Why fair share and not just "ask heavy users about the kitchen more": points are one pooled total. If heavy users simply got more kitchen asks, they'd build up points and get asked less for bathrooms, cancelling it out. Counting the extra kitchen work as what they owe keeps it from turning into credit elsewhere.

**Ranking for a task:** candidates are active (not paused, not opted out) people who haven't already been asked about this task, with a weight above 0 for its chore family, sorted by:

0. Fewest chores they're already being asked about — so the two bathrooms, created at the same moment, don't both go to the same person
1. Surplus, ascending — furthest behind their share first
2. How long ago they last did this chore family — longest ago first
3. Random

Bathrooms 1 and 2 count as **one family** for step 2. Having just cleaned bathroom 1 should make you less likely to get bathroom 2.

**Reporter rule:** reporting a chore doesn't exempt you from it. The reporter is ranked like everyone else, so if they're furthest behind, they're asked first.

**Point weights:**

| Chore | Points |
|---|---|
| Trash | 1 |
| Dishes — washed own | 0.25 |
| Dishes — washed others' (with or without own) | 0.75 |
| Bathroom | 3 (0.75 per part) |
| Kitchen reset | 2 |
| Fridge clear-out | 1 |
| Common area | 1.5 |

**Cover bonus:** 1.25× when the task already has a declined or no-response ask from someone else. An ask handed off because someone paused or opted out doesn't count. Doesn't apply to sink checks.

**Declining is free.** No deduction. The surplus simply stays low, which puts you near the front next time. This is the whole mechanism — no penalties anywhere in the system.

**Pausing:** since fair share doesn't grow while you're paused, a returning person is exactly as far ahead or behind as when they left. This holds whether they come back early or on schedule.

**Group average:** mean balance across active people only. Paused and opted-out people are excluded, since their frozen balances would drag it down. Used in the recap and the snapshot.

**Privacy:** balances and surpluses are private. A person can see their own balance, their own surplus and the group average, never anyone else's numbers. Who did which chore (person, chore, date) is not private — disputes depend on it. Usage tiers are agreed by the group, so they're not private either.

---

## 4. Ask lifecycle

```
                    ┌─────────┐
         asked ───► │ pending │
                    └────┬────┘
            ┌────────────┼────────────┬──────────────┐
            ▼            ▼            ▼              ▼
       ┌─────────┐ ┌──────────┐ ┌──────────┐  ┌──────────────┐
       │accepted │ │ snoozed  │ │ declined │  │ no_response  │
       └────┬────┘ └────┬─────┘ └────┬─────┘  └──────┬───────┘
            │           │            │               │
            │      (timer fires)     │               │
            │           └──► pending │               │
            │                        │               │
            ▼                        ▼               ▼
        completed              ask next person   ask next person
            │
            ▼
       ┌────────┐
       │ closed │  ledger row written
       └────────┘
```

**Transitions in detail:**

- **pending → accepted** — they said yes. A check-in is scheduled for ~24h later or just before the window closes, whichever is sooner.
- **pending → snoozed** — they named a time. Remind time is pushed out of quiet hours. If the time lands past the task's window, warn them that declining might be better.
- **pending → declined** — ask the next-ranked person immediately. If nobody is left, the task stays open with no one asked until its window ends.
- **pending → no_response** — the nudge ladder ran out. Mark it, increment the person's `no_response_streak`, move to the next person. No penalty, but it shows in the weekly recap and the person stays near the front of the line.
- **task window expires** — any remaining open asks close; the task becomes `unresolved` and appears in the recap. A re-ask is then scheduled (§8). Unless the next regular instance comes sooner, a fresh task is created after the re-ask delay and ranked from scratch. Everyone is eligible again, including people who declined last time.
- **Reported tasks** have no fixed window. While one is still open and nobody is left to ask, a new round of asks starts each day.

**Partial completion (bathrooms):** credit the parts done, then spin off a new task carrying the remaining parts, with the original person marked `excluded` so they don't get re-asked.

---

## 5. The nag ladder

Per ask:

1. First nudge ~4 hours after the ask
2. Second nudge the next morning
3. Then stop: mark `no_response`, move on

**Hard limits that override everything:**
- **Quiet hours, 12am–10am:** the bot never starts a conversation. Anything it would start overnight queues until morning. If a person texts during quiet hours, the reply goes out right away.
- **Morning jitter:** messages queued overnight don't all fire at 10:00. Each gets a random send time between 10:00 and ~11:30, keeping each person's messages in priority order with at least ~10 minutes between them. Any queued message fired by quiet hours gets the same treatment.
- **Cap of 3 bot-initiated messages per person per day.** The bot is conversational: replies to texts the person sent, including any back-and-forth that follows, are unlimited and never count. Over the cap, the lowest-priority messages roll to tomorrow.
- **The weekly recap is exempt from the cap**, so it never pushes out an ask and never slips to Monday.

**Priority when over the cap** (highest first):
1. Asks, and snooze reminders the person requested
2. Dispute questions and photo requests
3. Nudges and check-ins (accepted-but-not-done, inactivity)
4. Heads-ups (fridge)
5. Sink checks

A sink check that rolls over is dropped rather than queued. That person is simply included in the next suite-wide check.
- **Sink checks get no nudges at all.** They're honor-system; if someone doesn't answer within ~12 hours, the check quietly lapses. A lapsed sink check does not count toward `no_response_streak`.
- Nothing goes to an opted-out person, ever, except the single STOP confirmation.

Accepted-but-not-done gets one check-in, then stops. The window expiry handles it from there.

---

## 6. Pause, opt-out and inactivity

### Pause
- **Start:** "away until Sunday" → `pause(until)`. In-flight asks are handed to the next-ranked person; balance freezes.
- **Ends automatically** at `paused_until`.
- **Early return:** "I'm back" → `unpause`. No adjustment needed: fair share only grew while they were active (§3).

### STOP / START
Handled **in the engine, before the model sees the text.** Opt-out is a legal and carrier requirement, so it must not depend on the model reading it correctly.

Keywords only match when they are the **entire** text (case-insensitive, trimmed, trailing punctuation ignored). "Cancel my reminder" or "stop asking me about trash" go to the model like any other text. The goal is that nobody opts out by accident.

- **STOP:** a text that is exactly STOP, STOPALL or UNSUBSCRIBE sets `opted_out_at` immediately. The engine then:
  - hands off in-flight asks to the next-ranked person
  - deletes that person's unsent outbox rows
  - removes them from ranking, sink checks, fridge heads-ups and recaps
  - sends one confirmation that says to text START to undo it, and nothing after that

  Their balance and fair share freeze.
- **CANCEL, END, QUIT** on their own are ambiguous ("cancel" could mean a reminder). The engine sets `pending_stop_confirm_at` and replies asking whether they want to stop all texts (reply STOP to confirm). Any other reply clears it. *Depends on Twilio:* if its gateway still treats these words as an opt-out, they're handled like STOP, and the database is kept in sync. To check during Twilio setup.
- **START** (also UNSTOP): clears `opted_out_at`. **YES** counts as START only when the sender is currently opted out. For everyone else it's a normal reply that goes to the model. If their surplus is above the current lowest active surplus, an `adjust` row ("rejoin adjustment") brings it down to that lowest. Otherwise it's left alone. Unlike a pause, they come back at the front of the line rather than where they left off. History stays in the ledger.
- If the SMS provider (e.g. Twilio) also enforces STOP at the gateway, our handling keeps the database in sync with it. If a send fails because the provider says the number has opted out, set `opted_out_at` too.

### Inactivity check-in
Repeated silence is **not** assumed to mean someone is away. The bot asks once and changes nothing on its own.

- **Trigger:** `no_response_streak` reaches 3, and no inactivity check-in has gone out in the last 14 days. Not sent to paused or opted-out people.
- **Message:** one low-key text, along the lines of: haven't heard back on the last few, all good? If you're away, tell me until when and I'll pause you.
- **Any reply** resets the streak. "Away until X" goes through `pause` as usual; STOP goes through the engine as usual. Anything else needs no tool call.
- **No reply:** nothing changes. They stay active and in the rotation. They can get another check-in once the streak hits 3 again *and* 14 days have passed. Their no-response count keeps showing in their own recap.
- The check-in counts toward the daily message cap and respects quiet hours.

---

## 7. Photos, disputes and photo-request mode

### Photos (v1)
Photos are optional everywhere. When one comes in, the model looks at it and checks only two things: does it show the space the chore is about, and does it plausibly look done (or, for a report, look like it needs doing)? If not, the bot asks for another photo. A photo never changes points on its own. The engine records `photo` on the ledger row; the model's look is just a sanity check. The model also uses a photo to work out what a report is about ("this" + picture of the sink).

### Disputes (v2)
*Not in v1. Until this ships, completions and claims are trusted as-is.*


Anyone can dispute a ledger entry from the last 7 days (`dispute(ledger_id, reason)`). Disputing your own entry is rejected.

1. The ledger row goes to `disputed`. Its points stop counting toward the balance.
2. The bot asks the claimant what they did, or for a photo. It **does not say who raised the dispute.**
3. The claimant's reply resolves it, via `respond_dispute(ledger_id, outcome)`:

| Claimant outcome | Result | Dispute status |
|---|---|---|
| `photo` | Row back to `confirmed`, full points | restored |
| `parts_done` (with parts) | Points reduced to those parts; remaining parts spin off as a new task | partial |
| `concede` | Row `void` | void |
| `stand_by` (insists, no photo) | Points halved, row `contested` | split |
| no reply within 48h | Row `void` | void |

4. The disputer can `respond_dispute(ledger_id, "withdraw")` at any time before resolution, which restores the row to `confirmed`.

The engine checks the sender's role. Only the claimant can give claimant outcomes, and only the disputer can withdraw.

The bot never judges who is telling the truth. Every outcome comes from a fixed rule.

### Photo-request mode (v2)
- **Turns on** when a person has 2 entries end as `void` or `contested` through disputes within 30 days. Sets `photo_mode_until` = now + 30 days.
- **While it's on:** each completion or claim without a photo is written as `pending_photo`, and the bot asks for one. With a photo within 24h → `confirmed` at full points, and the entry can't be disputed. No photo within 24h → `confirmed` at **half** points.
- **Turns off** automatically when `photo_mode_until` passes. A further void or contested entry while it's on pushes it out another 30 days.
- **Private.** Never shown in anyone else's recap or messages.

---

## 8. Chore calendar

| Chore | Trigger | Re-ask if unresolved |
|---|---|---|
| Bathrooms | Scheduled: Saturday late morning, window through Sunday night. Both created at once. | ~3 days (midweek) |
| Kitchen | Scheduled weekly | ~3 days |
| Common area | Scheduled every 10–11 days (randomized so it doesn't always fall on the same weekday) | ~5 days |
| Trash | Scheduled 3 days after the last trash completion, plus reports at any time. A reported or claimed trash completion resets the clock. | Next day |
| Fridge clear-out | Scheduled 14 days after the last fridge completion, plus reports. Heads-up to everyone first (below) | ~7 days |
| Dishes | Suite-wide sink check to **every active person**, every **3–4 days** with random timing, each person's text sent at a random moment within a 30–60 minute spread. Skipped if someone washed everyone's dishes (a 0.75 dishes credit) in the last 24h. A "sink's full" report goes through `report_needed`, and whoever takes it is offered the 0.75 rate. | None. The next check is only 3–4 days away |
| Anything | Report-driven at any time | Daily while open |

The re-ask rule of thumb is about half the chore's interval. Reported tasks re-ask daily for as long as they're open (§4).

**Fridge heads-up:** whenever a fridge task is created, scheduled or reported, it gets `ask_after` = now + 24h and a window ending at the end of the day 3 days after that. Every active person immediately gets a message asking them to label or claim anything they want kept. These go through the outbox, so quiet hours and the daily cap apply. The first ask goes out at `ask_after`, using the normal ranking and reporter rule. If a fridge task is already open, a second report is just noted on it.

**Seven-person notes:**
- Chore supply (~12 points/week) across 7 people is under 2 points each. People will go weeks without being asked, so the weekly recap must show each person where they stand even if they weren't asked, or the rotation becomes invisible.
- These rhythms are estimates from a 4-person assumption. Expect to tighten them after two weeks of real data. Lean on reports early.

**Concentration tracking (add after ~1 month of history):** per person, per chore family, rolling 28-day share of completions. Compare against each person's weighted share for that family (~14% when weights are equal; see §3 for kitchen). Flag when one person exceeds ~2.5× their share, or two people together exceed ~60% beyond what their weights explain, with a minimum of ~5 completions in the window for the number to mean anything. Use the flag to nudge ranking. In the recap, a person only sees a line about their own share (e.g. "you've done 4 of the last 6 bathrooms").

---

## 9. Tool list

The complete set of actions the model may take. Everything else is read-only context.

| Tool | Arguments | What it does |
|---|---|---|
| `accept_task` | task_id | Marks agreement; schedules a check-in |
| `decline_task` | task_id, reason | Closes the ask, asks the next person |
| `snooze_task` | task_id, remind_at | Reminds later, quiet hours applied |
| `complete_task` | task_id, parts?, photo? | Writes the ledger row; spins off remaining parts |
| `claim_past_work` | chore, parts?, photo?, note, dishes? | Retroactive claim; trusted but flagged in the recap. Closes that chore's open task if there is one. `dishes`: own / others |
| `report_needed` | chore, note, photo? | Creates a task and picks who to ask (fridge: sends heads-up first) |
| `report_done_by_other` | chore, person_name? | Checks back with the named person, or whoever was assigned |
| `resolve_sink_check` | task_id, outcome | none_mine / washed_mine / washed_others |
| `pause` | until | Hands off in-flight asks, freezes balance |
| `unpause` | — | Ends a pause early |
| `dispute` | ledger_id, reason | Puts points on hold, asks the claimant for detail |
| `respond_dispute` | ledger_id, outcome | photo / parts_done (+parts) / concede / stand_by / withdraw |

STOP and START are deliberately **not** tools. The engine handles them before the model runs (§6).

`dispute` and `respond_dispute` ship in v2.

**Admin actions** aren't model tools either. The engine parses them directly, and only from the admin's number. Each is also a CLI command:
- `admin tier <name> <0-3>`: set someone's kitchen weight
- `admin reset points`: zero all balances and fair shares (with a confirmation step)
- `admin pause <name> <until>` / `admin unpause <name>`

Roster edits (adding or removing people, changing phones) are CLI-only.

**Design rules for this list:**
- Every tool is a verb the engine already implements. The model picks; it doesn't compute.
- Results come back as structured data (`{"ok": true, "next_asked": "Sam"}`) so the model can phrase a reply truthfully. Results never include other people's balances.
- The model may only pass IDs that appear in the state snapshot it was given. Anything else is rejected by the engine.
- When a message is ambiguous — "done" with two open tasks — the correct move is to ask, not to guess. Asking counts as correct behavior in evals.

---

## 10. State snapshot given to the model

On each incoming text, the model receives:

- Current date and time, and the sender's name
- The sender's own balance, surplus and usage tiers, plus the group average — **no one else's numbers**
- The sender's open asks, with task IDs and parts
- All currently open tasks
- Recent ledger entries from the last week, with IDs, person, chore and date (needed for disputes). No balances.
- Open disputes the sender is party to (as claimant or disputer), with ledger IDs. The disputer's name is withheld from the claimant's snapshot.
- Every message exchanged with this person in the last 72 hours
- Whether this person is paused, and whether they are in photo-request mode

That's the whole world the model can see. No conversation history beyond those 72 hours, and no memory between sessions.

---

## 11. Message generation

Outgoing texts are generated, not templated, so the bot doesn't repeat itself. The generator gets the purpose (ask, nudge, sink_check, fridge_heads_up, inactivity_check, dispute_question, photo_request, recap...), the relevant facts, and the last few messages sent to that person so it can vary phrasing.

Style rules: short, casual, warm. No guilt-tripping.
- **Bathrooms:** ask "what did you get to?" rather than "did you finish?" The first gets honest partial answers, the second gets rounded-up yeses.
- **Sink checks:** framed as "got anything in the sink? If so, wash it, and if you've got a minute, help with the rest." Easy and blame-free. "Nothing of mine" is a fine answer.
- **Disputes:** asked as "what did you get to?" too, never as an accusation. The disputer is never named.
- **Inactivity check-in:** a check on whether they're OK or away, not a reminder of what they missed.

**Weekly recap** (Sunday evening, one message per person):
- Your balance and the group average
- How far ahead of or behind your fair share you are (e.g. "1.5 points ahead of your share")
- What you did this week
- What got done suite-wide, and what's still unresolved
- Your own no-response count, if any
- Your own concentration line, once tracking is live

Never another person's balance, and never anyone's photo-request status.

Deterministic templates stay in the codebase as a fallback for when the API is unavailable.

---

## 12. Failure modes and how the design handles them

| Failure | Handling |
|---|---|
| Model misreads a text | Tools are narrow; errors are usually a wrong task_id, which the engine rejects. Every misread becomes an eval case. |
| Model invents a completion | Can't — points only exist as ledger rows written by engine calls. |
| Model misses a STOP | Can't — STOP/START are matched by the engine before the model runs. |
| Model leaks someone's balance | Can't — other balances are never in the snapshot or in tool results. |
| Someone games reports | A reporter who's furthest behind their share gets asked first. |
| Someone exaggerates a bathroom claim | Partial credit removes the incentive; the next person to use it notices; disputes hold the points. |
| Someone repeatedly overclaims | Photo-request mode (§7). |
| Someone goes silent | One inactivity check-in after 3 no-responses; nothing assumed (§6). |
| Someone's food gets tossed | Fridge tasks always give a 24h heads-up before anyone is asked. |
| Nobody responds to anything | Tasks expire as `unresolved` and appear in the recap. The system degrades to visible, not silently broken. |
| API is down | Outgoing falls back to templates; incoming texts queue in the message log. STOP/START still work. |
| Server restarts | All state is in SQLite. The scheduler is idempotent — a tick that runs twice doesn't double-send. |
| Someone finds the webhook URL | The webhook lives at a long random secret path (e.g. `/sms/<40 random chars>`), kept in an env var and never in the repo. Every other path returns 404. Behind that: every request must carry a valid Twilio signature, and texts from numbers not in `people` are ignored. Served over HTTPS only. |

---

## 13. Implementation notes

Decisions made while building v1 that this doc didn't pin down. All the numbers live in `chorebot/constants.py`.

- **Timing details:** kitchen is created Wednesdays 11am (3-day window). Common area and trash have 3- and 2-day windows. Every window ends at 11:59pm on its last day, so texts name a day ("by Tuesday night"), never a clock time. After the second nudge, an ask becomes `no_response` 6 hours later. The accepted check-in is 24h after accepting, or 3h before the window closes, whichever is sooner. The recap goes out Sundays at 7pm.
- **Accepted but not done, on reported tasks:** these have no window, so an accepted ask lets go 24h after its check-in and the next person is asked.
- **Early completions:** if a fixed-cadence chore (bathrooms, kitchen, common) gets done through a report or claim, and the next regular instance is less than half an interval away, that instance is skipped. Trash and fridge count their interval from the last completion anyway.
- **Volunteering:** `accept_task` on an open task you weren't asked about takes it over. Anyone else's pending ask on it is closed quietly.
- **Inline asks:** if a reporter is the one picked, the ask is part of the reply to their report rather than a separate text.
- **Fair-share storage:** a `shares` table holds each person's fraction of each ledger row. A row whose points change later carries its shares with it. `adjust` rows get no shares. An admin points reset is a cutoff timestamp; history is kept.
- **Schema additions:** `tasks.source` (scheduled / reask / reported / spinoff / sink_check / claim), `tasks.round` and `next_round_at` (daily re-ask rounds for reported tasks), `asks.round`, `remind_at`, `checkin_at`, `lapse_at`, `reason`. Also `outbox.held` (waiting for a morning slot), `outbox.status`, and `messages.processed` (texts waiting on the model).
- **STOP confirmation:** after a lone CANCEL/END/QUIT, a "yes" within an hour also confirms. Texts from opted-out people are ignored with no reply.
- **API down:** the person gets one "technical hiccup" reply per hour. Unprocessed texts are retried each tick for up to 6 hours.
- **Admin status:** `admin status` (text or CLI) lists open tasks and who's paused or opted out, with no points. Everyone's points are only visible in the simulator, never in production.

---

## 14. Open design questions

1. Whether the proposed kitchen tiers are right, and whether trash should also follow kitchen weights (a lot of it comes from cooking). Bathrooms could get weights too if use is uneven.
2. Whether to add a mid-week bathroom touch-up task (the ~3-day re-ask partly covers a missed weekend)
3. How much the concentration flag should shift ranking relative to surplus
4. Whether someone with a long no-response streak should get a shorter nudge ladder, so tasks aren't held up a day each time they're asked first
5. Whether the 50/50 split for `stand_by` is right, or whether contested claims should get less
