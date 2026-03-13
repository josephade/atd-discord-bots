"""
Bot configuration — loads .env and exposes shared constants/logger.
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()

# ── Discord ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")

# ── NBA ────────────────────────────────────────────────────────────────────────
DEFAULT_SEASON = "2024-25"
NBA_RATE_DELAY  = 0.6          # seconds between NBA API calls to avoid rate limits

# ── Logger ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nba-bot")