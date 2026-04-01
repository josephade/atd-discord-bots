import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN      = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
SPREADSHEET_ID     = os.getenv('SPREADSHEET_ID')
SERVICE_ACCOUNT_FILE = 'service_account.json'

# The exact name of the worksheet tab in your Google Sheet
WORKSHEET_NAME = os.getenv('WORKSHEET_NAME', 'Sheet1')
