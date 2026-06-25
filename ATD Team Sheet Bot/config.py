import json
import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN      = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
SPREADSHEET_ID     = os.getenv('SPREADSHEET_ID')

# The exact name of the worksheet tab in your Google Sheet
WORKSHEET_NAME = os.getenv('WORKSHEET_NAME', 'Sheet1')

# Set PRICE_REQUIRED=true in .env / Fly.io secrets to require a price on every pick.
# Leave unset (or false) for drafts where price is optional.
PRICE_REQUIRED = os.getenv('PRICE_REQUIRED', '').lower() in ('1', 'true', 'yes')

# User ID of the ATD Draft List Bot — its picks are processed like regular user picks.
# Set: fly secrets set DRAFT_LIST_BOT_ID=<id> --app atd-team-sheet-bot
DRAFT_LIST_BOT_ID = int(os.getenv('DRAFT_LIST_BOT_ID', 0)) or None

# If the service account JSON is provided as an env var (e.g. on Fly.io),
# write it to a temp file so oauth2client can read it normally.
_sa_json_env = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
if _sa_json_env:
    _tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    _tmp.write(_sa_json_env)
    _tmp.close()
    SERVICE_ACCOUNT_FILE = _tmp.name
else:
    SERVICE_ACCOUNT_FILE = 'service_account.json'
