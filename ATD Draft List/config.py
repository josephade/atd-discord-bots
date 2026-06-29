import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
DRAFT_CHANNEL_ID   = int(os.getenv("DRAFT_CHANNEL_ID", 0))

# Discord user ID of the ATD Timer Bot (used to filter whose pings to watch for)
# Set to 0 to accept pings from any bot.
_timer_bot_id = int(os.getenv("TIMER_BOT_ID", 0))
TIMER_BOT_ID = _timer_bot_id or None

OWNER_ID        = int(os.getenv("OWNER_ID", 0))
LOTTO_CHANNEL_ID = int(os.getenv("LOTTO_CHANNEL_ID", 0))

SPREADSHEET_ID     = os.getenv("SPREADSHEET_ID")

# The worksheet tab that contains the player list with background-color availability markers.
# Black / near-black cell background = player already drafted.
PLAYERS_SHEET_NAME = os.getenv("PLAYERS_SHEET_NAME", "Sheet1")

# The worksheet tab the Team Sheet Bot writes drafted players into.
# Used for text-based duplicate detection (more reliable than color checks).
# Defaults to PLAYERS_SHEET_NAME if not set separately.
ROSTER_SHEET_NAME = os.getenv("ROSTER_SHEET_NAME") or PLAYERS_SHEET_NAME

_sa_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if _sa_json_env:
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp.write(_sa_json_env)
    _tmp.close()
    SERVICE_ACCOUNT_FILE = _tmp.name
else:
    SERVICE_ACCOUNT_FILE = "service_account.json"