import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Discord Configuration
DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DEFAULT_PREFIX = os.getenv('BOT_PREFIX', '!')
ACTIVITY_TYPE = os.getenv('BOT_ACTIVITY_TYPE', 'watching')
ACTIVITY_NAME = os.getenv('BOT_ACTIVITY_NAME', 'NBA | {prefix}helpadvanced')

# Logging Configuration
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

# Cache Configuration
CACHE_ENABLED = os.getenv('CACHE_ENABLED', 'true').lower() == 'true'
CACHE_TIMEOUT = int(os.getenv('CACHE_TIMEOUT', '3600'))

# NBA API Configuration
NBA_API_TIMEOUT = int(os.getenv('NBA_API_TIMEOUT', '30'))
NBA_API_RETRIES = int(os.getenv('NBA_API_RETRIES', '3'))

# File paths
CONFIG_FILE = 'data/guild_configs.json'

# Validate required environment variables
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN not found in .env file!")

# Set up logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)