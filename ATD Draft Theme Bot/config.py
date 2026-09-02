import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN      = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD_ID   = int(os.getenv('DISCORD_GUILD_ID', '0')) or None

# Discord user ID allowed to run owner-only commands (e.g. !allteams, which
# bypasses reveal-gating to see every team's picks so far).
OWNER_ID = int(os.getenv('OWNER_ID', '681833601830354944'))

# Server channel where admin commands run and status/recap posts go.
# DM commands work regardless of this — it's only used for admin-facing output.
ADMIN_CHANNEL_ID   = int(os.getenv('ADMIN_CHANNEL_ID', '0')) or None

# Discord user ID of ATD Timer Bot — used by the Anonymous Draft themes to
# recognize its "your turn" pings in the selection channel. Leave unset to
# accept a matching-shaped ping from any bot (looser, but works before this
# is configured).
TIMER_BOT_ID = int(os.getenv('TIMER_BOT_ID', '0')) or None

# This bot's own Discord user ID — needed once it's running (bot.user.id) but
# also useful to have as a constant for generating the "fake lotto" message
# the commish posts into Timer Bot's channel (every team's owner mention
# needs to be THIS bot, never a real GM). Filled in from bot.user at runtime
# if left unset here.
SELF_BOT_ID = int(os.getenv('SELF_BOT_ID', '0')) or None

# ── Google Sheets: player pool / ADP ─────────────────────────────────────────
# Reuses the same spreadsheet ATD Draft Bot reads from (column B = player
# name, column C = ADP). Set POOL_SPREADSHEET_ID to point at it.
POOL_SPREADSHEET_ID = os.getenv('POOL_SPREADSHEET_ID')
POOL_TAB_NAME        = os.getenv('POOL_TAB_NAME', 'ADP')

# ── Google Sheets: team sheet / results ──────────────────────────────────────
# Reuses the same spreadsheet ATD Team Sheet Bot manages. Final rosters are
# only written here once a GM's submission passes validation, and (per-theme)
# the whole draft can be held back until every GM has submitted.
ROSTER_SPREADSHEET_ID = os.getenv('ROSTER_SPREADSHEET_ID')
ROSTER_TAB_NAME        = os.getenv('ROSTER_TAB_NAME', 'Draft Results')

# ── Persistence ───────────────────────────────────────────────────────────────
# Picks are kept secret in a local sqlite DB — never written to Sheets until
# a roster is validated and revealed. Point STATE_DIR at a Fly volume in
# production (matches ATD Timer Bot's convention); defaults to this folder
# for local runs.
STATE_DIR = os.getenv('STATE_DIR', os.path.dirname(__file__))
DB_PATH   = os.path.join(STATE_DIR, 'draft_theme.db')

# ── Google service account credentials ───────────────────────────────────────
_sa_json_env = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
if _sa_json_env:
    _tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    _tmp.write(_sa_json_env)
    _tmp.close()
    SERVICE_ACCOUNT_FILE = _tmp.name
else:
    SERVICE_ACCOUNT_FILE = 'service_account.json'
