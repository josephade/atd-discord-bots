# bot.py - FIXED for your actual team format (numbered list)
import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import asyncio
from datetime import datetime
from config import *
from player_positions import PLAYER_POSITIONS

# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Google Sheets setup
def setup_google_sheets():
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive.file',
        'https://www.googleapis.com/auth/drive'
    ]
    
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    
    try:
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        print(f"✅ Successfully connected to Google Sheets")
        
        # Get the specific worksheet "ATD Test"
        try:
            worksheet = spreadsheet.worksheet("ATD Test")
            print(f"✅ Found worksheet: ATD Test")
            return spreadsheet, worksheet
        except gspread.WorksheetNotFound:
            print(f"❌ Worksheet 'ATD Test' not found!")
            print(f"   Available worksheets: {[ws.title for ws in spreadsheet.worksheets()]}")
            return spreadsheet, None
            
    except Exception as e:
        print(f"❌ Error connecting to Google Sheets: {e}")
        return None, None

spreadsheet, worksheet = setup_google_sheets()

class TeamManager:
    def __init__(self, worksheet):
        self.worksheet = worksheet
        self.team_rows = {}  # Cache for team locations
        self.load_teams()
    
    def load_teams(self):
        """Load all teams from the ATD Test worksheet"""
        try:
            # Get all values from column A
            col_a = self.worksheet.col_values(1)
            
            print("\n📋 Scanning ATD Test for teams...")
            
            # YOUR ACTUAL FORMAT: Team names are in column A, sometimes with blank rows between
            self.team_rows = {}  # Clear existing
            
            for i, cell_value in enumerate(col_a, start=1):
                if cell_value and cell_value.strip():
                    # Check if this is a team entry (not a player, not empty)
                    # Simple approach: any non-empty cell in column A that doesn't look like a player name
                    # and isn't already identified as part of another team's roster
                    team_name = cell_value.strip()
                    
                    # Skip if this looks like a player name (has first/last name format)
                    words = team_name.split()
                    is_team = True
                    
                    # Basic heuristics to identify if this is a team vs player:
                    # 1. Team names are usually multiple words including "City" (San Antonio, Washington)
                    # 2. Team names don't have first/last name pattern
                    # 3. Common NBA city names
                    nba_cities = ['san antonio', 'washington', 'los angeles', 'chicago', 'boston', 
                                'new york', 'miami', 'dallas', 'houston', 'phoenix', 'philadelphia',
                                'brooklyn', 'atlanta', 'denver', 'portland', 'utah', 'milwaukee',
                                'orlando', 'indiana', 'memphis', 'new orleans', 'sacremento',
                                'detroit', 'oklahoma', 'minnesota', 'charlotte', 'cleveland',
                                'golden state', 'toronto']
                    
                    # Check if it contains a known NBA city or ends with commonly used terms
                    team_name_lower = team_name.lower()
                    if not any(city in team_name_lower for city in nba_cities):
                        if not ('spurs' in team_name_lower or 'wizards' in team_name_lower or 
                            'lakers' in team_name_lower or 'warriors' in team_name_lower or
                            'bulls' in team_name_lower or 'celtics' in team_name_lower):
                            is_team = False
                    
                    # Also check if the next row has a number or position indicator (for team headers)
                    if i < len(col_a):
                        next_row_val = col_a[i] if i < len(col_a) else ""
                        if next_row_val and not next_row_val.strip():
                            # Empty row after suggests this is a header
                            pass
                    
                    if is_team:
                        # Check if this row is likely a team header (not already in team_rows)
                        self.team_rows[team_name.lower()] = i
                        print(f"   ✅ Found team: '{team_name}' at row {i}")
            
            print(f"✅ Loaded {len(self.team_rows)} teams from ATD Test")
            
            if len(self.team_rows) == 0:
                print("\n⚠️  NO TEAMS FOUND! Debug info:")
                print(f"   First 20 rows of column A:")
                for i in range(1, min(21, len(col_a) + 1)):
                    val = col_a[i-1] if i <= len(col_a) else "EMPTY"
                    print(f"   Row {i}: '{val}'")
                
        except Exception as e:
            print(f"❌ Error loading teams: {e}")
    
    def find_team_row(self, team_name):
        """Find the row where this team starts"""
        team_name_lower = team_name.lower()
        
        # Check cache first
        if team_name_lower in self.team_rows:
            return self.team_rows[team_name_lower]
        
        # Try partial match
        for stored_team, row_num in self.team_rows.items():
            if team_name_lower in stored_team or stored_team in team_name_lower:
                print(f"✅ Found team '{stored_team}' at row {row_num}")
                return row_num
        
        print(f"❌ Team '{team_name}' not found in ATD Test")
        print(f"   Available teams: {list(self.team_rows.keys())}")
        return None
    
    def get_player_position(self, player_name):
        """Get player's primary position"""
        player_lower = player_name.lower().strip()
        
        # Try exact match first
        for stored_name, position in PLAYER_POSITIONS.items():
            if stored_name.lower() == player_lower:
                positions = position.split('/')
                return positions[0].strip().upper()
        
        # Try partial match
        for stored_name, position in PLAYER_POSITIONS.items():
            if player_lower in stored_name.lower() or stored_name.lower() in player_lower:
                positions = position.split('/')
                return positions[0].strip().upper()
        
        return None
    
    def find_position_row(self, team_row, position):
        """Find the row for a specific position under this team"""
        try:
            # Team header is at team_row
            # Players start at team_row + 2 (skip the number row)
            start_row = team_row + 2
            
            # Get all values to search
            all_data = self.worksheet.get_all_values()
            
            # Search for the position in column B
            for row_num in range(start_row, len(all_data) + 1):
                if row_num > len(all_data):
                    break
                    
                row_data = all_data[row_num - 1]
                
                # Check if we hit the next team (column B has a number)
                if len(row_data) > 1 and row_data[1] and row_data[1].strip().isdigit():
                    print(f"   Reached next team at row {row_num}")
                    break
                
                # Check column B for position
                if len(row_data) > 1:
                    position_cell = row_data[1] if row_data[1] else ""
                    player_cell = row_data[2] if len(row_data) > 2 and row_data[2] else ""
                    
                    # If this row has the position we want AND no player
                    if position.upper() in position_cell.upper() and not player_cell:
                        print(f"   ✅ Found empty {position} spot at row {row_num}")
                        return row_num, position_cell
            
            return None, None
            
        except Exception as e:
            print(f"❌ Error finding position row: {e}")
            return None, None
    
    def update_player(self, team_name, player_name, year=None, price=None):
        """Update ATD Test worksheet with player information"""
        try:
            if not self.worksheet:
                return False, "❌ No worksheet connected"
            
            # Find the team's starting row
            team_row = self.find_team_row(team_name)
            
            if not team_row:
                return False, f"❌ Team '{team_name}' not found in ATD Test"
            
            # Get player position
            position = self.get_player_position(player_name)
            if not position:
                return False, f"❌ Could not find position for player: {player_name}"
            
            # Find empty row for this position under this team
            row_num, position_label = self.find_position_row(team_row, position)
            
            if not row_num:
                return False, f"❌ No available {position} spots for {team_name}"
            
            # Prepare the update
            # Column B: Position (already has "PG", "SG", etc.)
            # Column C: Player Name
            # Column D: Year
            # Column E: Price
            
            updates = [[player_name, year if year else '', price if price else '']]
            
            # Update columns C through E (Player, Year, Price)
            self.worksheet.update(
                values=updates,
                range_name=f'C{row_num}:E{row_num}'
            )
            
            return True, f"✅ Added {player_name} to {team_name} as {position_label} (Row {row_num})"
            
        except Exception as e:
            print(f"❌ Error updating sheet: {e}")
            return False, f"❌ Error updating sheet: {str(e)}"
    
    def get_team_roster(self, team_name):
        """Get all players for a specific team"""
        team_row = self.find_team_row(team_name)
        if not team_row:
            return None
        
        try:
            all_data = self.worksheet.get_all_values()
            start_row = team_row + 2
            roster = []
            
            for row_num in range(start_row, len(all_data) + 1):
                if row_num > len(all_data):
                    break
                    
                row_data = all_data[row_num - 1]
                
                # Check if we hit next team (column B has a number)
                if len(row_data) > 1 and row_data[1] and row_data[1].strip().isdigit():
                    break
                
                # Get player data
                if len(row_data) > 2 and row_data[2]:  # Has player name
                    position = row_data[1] if len(row_data) > 1 else ""
                    player = row_data[2]
                    year = row_data[3] if len(row_data) > 3 else ""
                    price = row_data[4] if len(row_data) > 4 else ""
                    
                    roster.append({
                        'row': row_num,
                        'position': position,
                        'player': player,
                        'year': year,
                        'price': price
                    })
            
            return roster
            
        except Exception as e:
            print(f"❌ Error getting roster: {e}")
            return None

# Initialize TeamManager with the ATD Test worksheet
team_manager = TeamManager(worksheet) if worksheet else None

class MessageParser:
    @staticmethod
    def parse_message(message):
        """Parse message in various formats"""
        text = message.strip()
        
        # Extract price (starts with $)
        price_match = re.search(r'\$(\d+(?:\.\d+)?)', text)
        price = price_match.group(0) if price_match else None
        text = re.sub(r'\$\d+(?:\.\d+)?', '', text).strip()
        
        # Extract year (format: 2019-20, 2019, '19-20)
        year_match = re.search(r'(\d{4}-\d{2}|\d{4}|\'\d{2}-\d{2})', text)
        year = year_match.group(0) if year_match else None
        if year_match:
            text = text.replace(year_match.group(0), '').strip()
        
        words = text.split()
        
        if len(words) < 2:
            return None, "Message must include at least team and player name"
        
        # Identify team
        team_name = None
        player_start_idx = 0
        
        # Try multi-word team names
        for i in range(min(3, len(words)), 0, -1):
            potential_team = ' '.join(words[:i]).lower()
            if potential_team in TEAM_MAPPINGS:
                team_name = TEAM_MAPPINGS[potential_team]
                player_start_idx = i
                break
        
        if not team_name:
            for word in words:
                if word.lower() in TEAM_MAPPINGS:
                    team_name = TEAM_MAPPINGS[word.lower()]
                    player_start_idx = words.index(word) + 1
                    break
        
        if not team_name:
            team_name = words[0].upper()
            player_start_idx = 1
        
        player_name = ' '.join(words[player_start_idx:]).strip()
        
        if not player_name:
            return None, "Could not extract player name"
        
        return {
            'team': team_name,
            'player': player_name,
            'year': year,
            'price': price
        }, None

# Discord Events
@bot.event
async def on_ready():
    print(f'✅ Bot is ready. Logged in as {bot.user.name}')
    
    if not team_manager:
        print("❌ No worksheet connected! Bot cannot function.")
        return
    
    # Get and verify channel
    if DISCORD_CHANNEL_ID:
        channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if channel:
            print(f'✅ Monitoring channel: #{channel.name}')
            print(f'📊 Working with worksheet: ATD Test')
            print(f'🏀 Teams loaded: {len(team_manager.team_rows)}')
            await bot.change_presence(
                activity=discord.Game(name=f"ATD Test | {len(team_manager.team_rows)} teams"),
                status=discord.Status.online
            )
        else:
            print(f'❌ Could not find channel with ID: {DISCORD_CHANNEL_ID}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    # ONLY process messages from the specific channel
    if message.channel.id != DISCORD_CHANNEL_ID:
        return
    
    if not team_manager:
        await message.channel.send("❌ Bot not connected to Google Sheets. Check console for errors.")
        return
    
    await process_player_addition(message)

async def process_player_addition(message):
    """Process any message in the designated channel as a player addition"""
    parser = MessageParser()
    data, error = parser.parse_message(message.content)
    
    if error:
        error_msg = await message.channel.send(f"❌ Error: {error}")
        await asyncio.sleep(5)
        await error_msg.delete()
        return
    
    async with message.channel.typing():
        success, result = team_manager.update_player(
            data['team'],
            data['player'],
            data['year'],
            data['price']
        )
        
        if success:
            await message.add_reaction('✅')
            
            embed = discord.Embed(
                title="✅ Player Added Successfully",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Team", value=data['team'], inline=True)
            embed.add_field(name="Player", value=data['player'], inline=True)
            if data['year']:
                embed.add_field(name="Year", value=data['year'], inline=True)
            if data['price']:
                embed.add_field(name="Price", value=data['price'], inline=True)
            embed.add_field(name="Result", value=result, inline=False)
            embed.set_footer(text=f"Added by {message.author.display_name}")
            
            await message.channel.send(embed=embed)
        else:
            await message.add_reaction('❌')
            
            embed = discord.Embed(
                title="❌ Error Adding Player",
                color=discord.Color.red(),
                description=result
            )
            await message.channel.send(embed=embed)

@bot.command(name='roster')
async def show_roster(ctx, *, team_name=None):
    """Show a team's roster from ATD Test"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    
    if not team_manager:
        await ctx.send("❌ Bot not connected to Google Sheets")
        return
    
    if not team_name:
        await ctx.send("Please specify a team name. Usage: `!roster [Team Name]`")
        return
    
    roster = team_manager.get_team_roster(team_name)
    
    if roster is None:
        await ctx.send(f"❌ Could not find team: {team_name}")
        return
    
    embed = discord.Embed(
        title=f"🏀 {team_name} Roster - ATD Test",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    
    if roster:
        roster_text = ""
        for player in roster:
            roster_text += f"**Row {player['row']} - {player['position']}:** {player['player']}"
            if player['year']:
                roster_text += f" ({player['year']})"
            if player['price']:
                roster_text += f" - {player['price']}"
            roster_text += "\n"
        embed.description = roster_text
    else:
        embed.description = "No players added yet."
    
    await ctx.send(embed=embed)

@bot.command(name='teams')
async def list_teams(ctx):
    """List all teams in ATD Test"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    
    if not team_manager:
        await ctx.send("❌ Bot not connected to Google Sheets")
        return
    
    teams = sorted([team.title() for team in team_manager.team_rows.keys()])
    
    embed = discord.Embed(
        title="🏀 ATD Test - Available Teams",
        description="\n".join(f"• {team}" for team in teams),
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Total: {len(teams)} teams")
    
    await ctx.send(embed=embed)

@bot.command(name='debug')
async def debug_sheet(ctx):
    """Debug: Show ATD Test structure"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    
    if not team_manager or not team_manager.worksheet:
        await ctx.send("❌ Bot not connected to Google Sheets")
        return
    
    # Get sample data
    all_data = team_manager.worksheet.get_all_values()
    
    embed = discord.Embed(
        title="🔍 ATD Test - Debug Info",
        color=discord.Color.gold()
    )
    
    # Show first 20 rows
    sample_rows = []
    for i in range(1, min(21, len(all_data) + 1)):
        row = all_data[i-1] if i <= len(all_data) else []
        col_a = row[0] if len(row) > 0 else "EMPTY"
        col_b = row[1] if len(row) > 1 else "EMPTY"
        sample_rows.append(f"Row {i}: A='{col_a[:20]}', B='{col_b[:20]}'")
    
    embed.add_field(name="First 20 Rows", value="\n".join(sample_rows), inline=False)
    embed.add_field(name="Teams Found", value=str(len(team_manager.team_rows)), inline=True)
    embed.add_field(name="Total Rows", value=str(len(all_data)), inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='help')
async def help_command(ctx):
    """Show help information"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    
    embed = discord.Embed(
        title="🏀 ATD Team Sheet Bot - Help",
        description="This bot manages the **ATD Test** worksheet only.",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="📝 Adding Players",
        value="Simply type in this channel:\n`[Team] [Player Name] [Year] [Price]`\n\n**Examples:**\n`Washington Wizards Michael Jordan 1990-91 $45`\n`WAS James Harden 2019-20 $26`",
        inline=False
    )
    
    embed.add_field(
        name="📋 Commands",
        value="`!roster [Team]` - View team roster\n`!teams` - List all teams in ATD Test\n`!debug` - Show sheet debug info",
        inline=False
    )
    
    embed.set_footer(text="Working exclusively with ATD Test worksheet")
    
    await ctx.send(embed=embed)

# Run the bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ Error: DISCORD_TOKEN not found in .env file")
    elif not DISCORD_CHANNEL_ID:
        print("❌ Error: DISCORD_CHANNEL_ID not found in .env file")
    elif not worksheet:
        print("❌ Error: Could not find 'ATD Test' worksheet in your spreadsheet")
        print("   Please check that:")
        print("   1. Your spreadsheet has a tab named exactly 'ATD Test'")
        print("   2. The service account has access to this spreadsheet")
        print("   3. The spreadsheet ID in .env is correct")
    else:
        print("🚀 Starting ATD Team Sheet Bot...")
        print(f"📋 Will monitor channel ID: {DISCORD_CHANNEL_ID}")
        print(f"📊 Working EXCLUSIVELY with worksheet: ATD Test")
        print(f"🏀 Teams loaded: {len(team_manager.team_rows)}")
        bot.run(DISCORD_TOKEN)