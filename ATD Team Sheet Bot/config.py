# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# Discord Bot Token
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Discord Channel ID (single channel to monitor)
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID', '0'))  # Convert to integer

# Google Sheets Configuration
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', 'your-spreadsheet-id-here')
SERVICE_ACCOUNT_FILE = 'service_account.json'

# Bot Configuration
PREFIX = '!'
ALLOWED_ROLES = ['Admin', 'Manager']  # Optional: restrict by roles

# Team Name Mappings (for abbreviations/logos)
TEAM_MAPPINGS = {
    # Full names
    'washington wizards': 'Washington Wizards',
    'washington': 'Washington Wizards',
    'wizards': 'Washington Wizards',
    'was': 'Washington Wizards',
    
    'new orleans hornets': 'New Orleans Hornets',
    'hornets': 'New Orleans Hornets',
    'noh': 'New Orleans Hornets',
    
    'toronto raptors': 'Toronto Raptors',
    'raptors': 'Toronto Raptors',
    'tor': 'Toronto Raptors',
    
    'miami heat': 'Miami Heat',
    'heat': 'Miami Heat',
    'mia': 'Miami Heat',
    
    'orlando magic': 'Orlando Magic',
    'magic': 'Orlando Magic',
    'orl': 'Orlando Magic',
    
    # Add more teams as needed
}

# Position Priority (which position to use when multiple are listed)
POSITION_PRIORITY = ['PG', 'SG', 'SF', 'PF', 'C']

# Sheet Structure
ROW_MAPPINGS = {
    'team_name': 1,
    'starting_pg': 2,
    'starting_sg': 3,
    'starting_sf': 4,
    'starting_pf': 5,
    'starting_c': 6,
    'bench_pg': 7,
    'bench_sg': 8,
    'bench_sf': 9,
    'bench_pf': 10,
    'bench_c': 11
}