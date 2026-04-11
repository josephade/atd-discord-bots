import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
DRAFT_CHANNEL_ID = int(os.getenv("DRAFT_CHANNEL_ID", 0))
LOTTO_CHANNEL_ID = int(os.getenv("LOTTO_CHANNEL_ID", 934052115821764718))

ROUNDS = 10

# Timer per round in seconds
ROUND_TIMERS = {
    **{r: 3600 for r in range(1, 3)},    # R1-2:  1 hour
    **{r: 2700 for r in range(3, 9)},    # R3-8:  45 minutes
    **{r: 1800 for r in range(9, 11)},   # R9-10: 30 minutes
}

SKIP_PENALTY = 600   # 10 minutes deducted per skip

# Active Skip: teams with this many skips or more are skipped immediately
# when it's their turn — no timer given.
AS_THRESHOLD = 3

# Players that trigger the "pick at the end of rounds 6-10" penalty
PENALTY_PLAYERS = {"lebron james", "michael jordan"}
