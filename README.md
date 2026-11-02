# Suite Chore Bot

An SMS chore bot for the suite. [rules.md](rules.md) is the version for suitemates. [design.md](design.md) is the spec the code follows.

## Setup

```sh
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
```

## Try it in the simulator

The simulator runs the whole bot locally with a fake clock. Nothing is sent.

```sh
export ANTHROPIC_API_KEY=...        # optional; without it the bot runs offline
.venv/bin/chorebot sim --fresh --start "2026-11-07 09:00"
```

```
Kene: trash is full          # text the bot as Kene
/advance 4h                  # move time forward; scheduled texts and nudges print as they go out
/tasks                       # what's open and who's asked
/points                      # everyone's numbers (simulator only)
/photo Alula sink.jpg the sink is full
/help
```

With no API key, or with `--offline`, messages come from templates. Use `/tool` to act without the model, for example `/tool Kene complete_task {"task_id": 1}`.

It uses `people.example.toml` with placeholder phone numbers.

## Tests and evals

```sh
.venv/bin/pytest            # engine rules, offline, takes about a second
.venv/bin/chorebot eval     # the model on evals/cases.json; needs ANTHROPIC_API_KEY, costs a little
```

When the bot misreads a real text, add that text to `evals/cases.json`.

## Going live

1. `cp people.example.toml people.toml` and fill in real numbers (E.164). The file is gitignored.
2. `.venv/bin/chorebot init` creates `chorebot.db` and loads people.
3. Set these environment variables on the host:
   - `ANTHROPIC_API_KEY`
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
   - `CHOREBOT_PUBLIC_BASE_URL`: e.g. `https://your-app.fly.dev`
   - `CHOREBOT_WEBHOOK_SECRET`: 40+ random characters (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
4. `.venv/bin/chorebot serve` runs the webhook and the scheduler in one process. It needs a persistent disk for `chorebot.db`.
5. In Twilio, set the number's incoming-message webhook to `POST {CHOREBOT_PUBLIC_BASE_URL}/sms/{CHOREBOT_WEBHOOK_SECRET}`.
6. `.venv/bin/chorebot onboard` queues the intro text to everyone.

## Admin

From Kene's phone: `admin status`, `admin tier <name> <0-3>`, `admin reset points`, `admin pause <name> <YYYY-MM-DD>`, `admin unpause <name>`.

The same commands are on the CLI: `chorebot admin ...`. Roster edits are CLI only: `chorebot people list|add|set-phone|remove`.
