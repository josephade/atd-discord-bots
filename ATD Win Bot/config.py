import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', '0'))
SPREADSHEET_ID   = os.getenv('SPREADSHEET_ID')
ROSTER_SHEET_ID  = os.getenv('ROSTER_SHEET_ID')
STATE_DIR        = os.getenv('STATE_DIR', '/data')

_sa_json_env = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
if _sa_json_env:
    _tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    _tmp.write(_sa_json_env)
    _tmp.close()
    SERVICE_ACCOUNT_FILE = _tmp.name
else:
    SERVICE_ACCOUNT_FILE = 'service_account.json'
