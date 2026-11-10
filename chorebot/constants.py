"""Every tunable number in one place. Section references are to design.md."""

from datetime import timedelta

H = timedelta(hours=1)
D = timedelta(days=1)

# --- Chores (§3) -------------------------------------------------------------

CHORES = ["trash", "dishes", "bathroom_1", "bathroom_2", "kitchen", "fridge", "common"]

LABELS = {
    "trash": "trash",
    "dishes": "dishes",
    "bathroom_1": "bathroom 1",
    "bathroom_2": "bathroom 2",
    "kitchen": "kitchen reset",
    "fridge": "fridge clear-out",
    "common": "common area",
}

POINTS = {
    "trash": 1.0,
    "bathroom_1": 3.0,
    "bathroom_2": 3.0,
    "kitchen": 2.0,
    "fridge": 1.0,
    "common": 1.5,
}
DISHES_OWN = 0.25
DISHES_OTHERS = 0.75

BATHROOMS = ("bathroom_1", "bathroom_2")
BATHROOM_PARTS = ["toilet", "sink_mirror", "shower", "floor"]
PART_POINTS = 0.75

COVER_BONUS = 1.25

# Weight family: which usage_weights row sets your fair share for a chore.
WEIGHT_FAMILY = {"kitchen": "kitchen", "fridge": "kitchen"}
# Recency family: "how long since you last did this" (ranking step 2).
RECENCY_FAMILY = {"bathroom_1": "bathroom", "bathroom_2": "bathroom"}


def weight_family(chore: str) -> str:
    return WEIGHT_FAMILY.get(chore, chore)


def recency_family(chore: str) -> str:
    return RECENCY_FAMILY.get(chore, chore)


def chores_in_recency_family(chore: str) -> list[str]:
    fam = recency_family(chore)
    return [c for c in CHORES if recency_family(c) == fam]


# Ledger statuses that count toward a balance (§3).
COUNTING = ("confirmed", "contested", "adjust")
# Statuses whose points are distributed into fair shares. `adjust` rows are personal.
SHARED = ("confirmed", "contested")

ACTIVE_ASK = ("pending", "snoozed", "accepted")

# --- Messaging limits (§5) ---------------------------------------------------

QUIET_END_HOUR = 10  # quiet hours are 00:00-10:00 local
MORNING_JITTER_SPAN = timedelta(minutes=45)  # first message lands 10:00-10:45
MORNING_GAP = (timedelta(minutes=10), timedelta(minutes=20))
MIN_GAP = timedelta(minutes=10)  # between bot-initiated texts to one person
DAILY_CAP = 3
SEND_ATTEMPTS = 3

PRIORITY = {
    "ask": 1,
    "snooze_reminder": 1,
    "dispute_question": 2,
    "photo_request": 2,
    "nudge": 3,
    "checkin": 3,
    "inactivity_check": 3,
    "done_check": 3,
    "fridge_heads_up": 4,
    "sink_check": 5,
    # Cap-exempt purposes still get a priority for ordering.
    "recap": 6,
    "intro": 0,
}
CAP_EXEMPT = {"recap", "intro"}

# --- Ask lifecycle (§4, §5) --------------------------------------------------

NUDGE_1_AFTER = 4 * H
NO_RESPONSE_AFTER_NUDGE_2 = 6 * H
CHECKIN_AFTER = 24 * H
CHECKIN_BEFORE_WINDOW_END = 3 * H
ACCEPTED_LAPSE_AFTER_CHECKIN = 24 * H  # windowless (reported) tasks only
REPORTED_ROUND_GAP = 24 * H

# --- Calendar (§8) -----------------------------------------------------------

SCHEDULED_CHORES = ["bathroom_1", "bathroom_2", "kitchen", "common", "trash", "fridge"]

BATHROOM_WEEKDAY = 5  # Saturday
BATHROOM_HOUR = 11
KITCHEN_WEEKDAY = 2  # Wednesday
KITCHEN_HOUR = 11
COMMON_INTERVAL_DAYS = (10, 11)
TRASH_INTERVAL = 3 * D  # after the last trash completion
FRIDGE_INTERVAL = 14 * D  # after the last fridge completion
DAYTIME_HOURS = (11, 19)  # random creation hour for chores without a fixed slot

WINDOW = {
    "bathroom": 3 * D,  # re-asks and spin-offs; scheduled bathrooms run to Sunday night
    "kitchen": 3 * D,
    "common": 3 * D,
    "trash": 2 * D,
    "fridge": 3 * D,  # counted from ask_after
}
REASK_DELAY = {
    "bathroom": 3 * D,
    "kitchen": 3 * D,
    "common": 5 * D,
    "trash": 1 * D,
    "fridge": 7 * D,
}
FRIDGE_HEADS_UP = 24 * H

SINK_INTERVAL_HOURS = (72, 96)
SINK_CHECK_WINDOW = 12 * H
SINK_SPREAD = (timedelta(minutes=30), timedelta(minutes=60))  # texts to each person land at random within this span
SINK_SKIP_IF_WASHED_WITHIN = 24 * H

INACTIVITY_STREAK = 3
INACTIVITY_COOLDOWN = 14 * D

RECAP_WEEKDAY = 6  # Sunday
RECAP_HOUR = 19

# --- Model context (§10) -----------------------------------------------------

HISTORY_WINDOW = 72 * H
HISTORY_MAX_MESSAGES = 60
LEDGER_LOOKBACK = 7 * D
