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

# Players that trigger the "pick at the end of rounds 6-10" penalty
# TEMPORARILY DISABLED for budget draft — re-enable when done
# PENALTY_PLAYERS = {"lebron james", "michael jordan"}
PENALTY_PLAYERS = set()

# User ID of the ATD Draft List Bot — its picks are trusted (treated like a commissioner pick).
# Set this as a Fly.io secret: fly secrets set DRAFT_LIST_BOT_ID=<id> --app atd-timer-bot
DRAFT_LIST_BOT_ID = int(os.getenv("DRAFT_LIST_BOT_ID", 0)) or None

# ── Steal / Block / Lock (SBL) 
# Enabled via !timermode snake+sbl or !timermode roundless+sbl / !timerstart [mode]+sbl

SBL_STEALS_PER_TEAM = 2   # steals each GM gets for the whole draft
SBL_BLOCKS_PER_TEAM = 2   # blocks each GM gets for the whole draft
SBL_LOCKS_PER_TEAM  = 1   # locks each GM gets for the whole draft

# A pick can only be stolen/blocked while it's within the same "round" as the
# current pick, where a round = one pick per team (see DraftState.sbl_window
# in draft.py) — this applies even in roundless mode, independent of snake's
# own team-count-based rounds.
