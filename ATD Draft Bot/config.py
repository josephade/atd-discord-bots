import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN          = os.getenv('DISCORD_TOKEN')
DRAFT_CHANNEL_ID       = int(os.getenv('DRAFT_CHANNEL_ID', '0'))

# Spreadsheet containing the player pool (tab name set by POOL_TAB_NAME)
POOL_SPREADSHEET_ID    = os.getenv('POOL_SPREADSHEET_ID')
POOL_TAB_NAME          = os.getenv('POOL_TAB_NAME', 'East')

# Separate spreadsheet where completed draft results are written
OUTPUT_SPREADSHEET_ID  = os.getenv('OUTPUT_SPREADSHEET_ID')

SERVICE_ACCOUNT_FILE   = 'service_account.json'

ROUNDS = 10
PICK_TIMEOUT_SECONDS = 10    # 30 seconds
