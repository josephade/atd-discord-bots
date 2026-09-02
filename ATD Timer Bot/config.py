import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
DRAFT_CHANNEL_ID   = int(os.getenv("DRAFT_CHANNEL_ID", 0))
LOTTO_CHANNEL_ID   = int(os.getenv("LOTTO_CHANNEL_ID", 934052115821764718))
ATD_CHAT_CHANNEL_ID = int(os.getenv("ATD_CHAT_CHANNEL_ID", 934052158532378634))

# Channel where the auto-generated draft recap posts when a draft finishes.
# Set: fly secrets set DRAFT_RECAP_CHANNEL_ID=<id> --app atd-timer-bot
DRAFT_RECAP_CHANNEL_ID = int(os.getenv("DRAFT_RECAP_CHANNEL_ID", 0)) or None

ROUNDS = 10

# Timer per round in seconds
ROUND_TIMERS = {
    **{r: 3600 for r in range(1, 3)},    # R1-2:  1 hour
    **{r: 2700 for r in range(3, 9)},    # R3-8:  45 minutes
    **{r: 1800 for r in range(9, 11)},   # R9-10: 30 minutes
}

SKIP_PENALTY = 300   # 5 minutes deducted per skip

ROUNDLESS_TIMER = 2700  # 45 minutes per pick in roundless (money-based) mode

# Active Skip: teams with this many skips or more are skipped immediately
# when it's their turn — no timer given.
AS_THRESHOLD = 3

# R3-8 and roundless mode (both 45 min base) use a fixed step-down timer per
# skip instead of the flat -5 min/skip formula: 1st skip -> 35 min, 2nd -> 20
# min, 3rd -> 10 min, 4th+ -> Active Skip (see STEPPED_AS_THRESHOLD below).
STEPPED_SKIP_SCHEDULE = {1: 2100, 2: 1200, 3: 600}  # skip_count -> seconds
STEPPED_AS_THRESHOLD = 4

# Players that trigger the "pick at the end of rounds 6-10" penalty
PENALTY_PLAYERS = {"lebron james", "michael jordan"}

# User ID of the ATD Draft List Bot — its picks are trusted (treated like a commissioner pick).
# Set this as a Fly.io secret: fly secrets set DRAFT_LIST_BOT_ID=<id> --app atd-timer-bot
DRAFT_LIST_BOT_ID = int(os.getenv("DRAFT_LIST_BOT_ID", 0)) or None

# Additional bot user IDs to trust the same way as DRAFT_LIST_BOT_ID (e.g. ATD
# Draft Theme Bot, posting anonymized picks on a hidden GM's behalf).
# Comma-separated. Set: fly secrets set EXTRA_TRUSTED_BOT_IDS=<id1>,<id2> --app atd-timer-bot
EXTRA_TRUSTED_BOT_IDS = {int(x) for x in os.getenv("EXTRA_TRUSTED_BOT_IDS", "").split(",") if x.strip()}

# Every bot user ID whose picks are trusted like a commissioner's — check
# membership in this set instead of comparing to DRAFT_LIST_BOT_ID alone.
TRUSTED_BOT_IDS = ({DRAFT_LIST_BOT_ID} if DRAFT_LIST_BOT_ID else set()) | EXTRA_TRUSTED_BOT_IDS

# ── Steal / Block / Lock (SBL) 
# Enabled via !timermode snake+sbl or !timermode roundless+sbl / !timerstart [mode]+sbl

SBL_STEALS_PER_TEAM = 2   # steals each GM gets for the whole draft
SBL_BLOCKS_PER_TEAM = 2   # blocks each GM gets for the whole draft
SBL_LOCKS_PER_TEAM  = 1   # locks each GM gets for the whole draft

# A pick can only be stolen/blocked while it's within the same "round" as the
# current pick, where a round = one pick per team (see DraftState.sbl_window
# in draft.py) — this applies even in roundless mode, independent of snake's
# own team-count-based rounds.
