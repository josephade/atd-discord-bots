import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
AUDIT_LOG_CHANNEL_ID  = int(os.getenv("AUDIT_LOG_CHANNEL_ID", 0)) or None
