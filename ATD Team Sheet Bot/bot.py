import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import asyncio
import time
from datetime import datetime
import requests.exceptions
from config import DISCORD_TOKEN, DISCORD_CHANNEL_ID, SPREADSHEET_ID, SERVICE_ACCOUNT_FILE, WORKSHEET_NAME
from player_positions import PLAYER_POSITIONS
from emoji_map import EMOJI_TEAM_MAP


# =============================================================================
# GOOGLE SHEETS
# =============================================================================

def connect_sheets():
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
    print(f"✅ Connected to worksheet: '{WORKSHEET_NAME}'")
    return ws



try:
    worksheet = connect_sheets()
except Exception as e:
    print(f"❌ Google Sheets connection failed: {e}")
    worksheet = None


def _sheets_call(fn, *args, retries=3, **kwargs):
    """
    Call a gspread function, retrying up to `retries` times on transient
    network errors (dropped connections, timeouts, etc.).
    Sleeps 2s between attempts and reconnects the worksheet on failure.
    """
    global worksheet
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == retries:
                raise
            print(f"[Sheets] Error (attempt {attempt}/{retries}): {type(e).__name__}: {e} — reconnecting…")
            time.sleep(2)
            try:
                worksheet = connect_sheets()
            except Exception:
                pass  # will retry the original call anyway


# =============================================================================
# POSITION → ROW OFFSETS
# Row offsets relative to the team header row.
# e.g. if "Milwaukee Bucks" is in row 5, PG starter is row 6, PG bench is row 11.
#
# Layout:
#   +0  Team Name  (header — user fills this in)
#   +1  Starting PG
#   +2  Starting SG
#   +3  Starting SF
#   +4  Starting PF
#   +5  Starting C
#   +6  Bench PG
#   +7  Bench SG
#   +8  Bench SF
#   +9  Bench PF
#   +10 Bench C
# =============================================================================

POSITION_OFFSETS = {
    'PG': (1, 6),
    'SG': (2, 7),
    'SF': (3, 8),
    'PF': (4, 9),
    'C':  (5, 10),
}


# =============================================================================
# SHEET MANAGER
# =============================================================================

class SheetManager:
    def __init__(self, ws):
        self.ws = ws
        self._undo_stack = []  # list of dicts describing each successful write

    def _find_team_cell(self, team_name):
        """
        Scan every cell in the sheet for the team name.
        Returns (row, col) as 1-indexed integers, or (None, None).
        Supports any grid layout — teams can be anywhere.
        """
        print(f"[Sheet] Fetching sheet data to locate '{team_name}'…")
        data = _sheets_call(self.ws.get_all_values)
        name_lower = team_name.lower().strip()

        # Exact match first
        for row_idx, row in enumerate(data):
            for col_idx, cell in enumerate(row):
                if cell.strip().lower() == name_lower:
                    print(f"[Sheet] Team '{team_name}' found at row={row_idx+1} col={col_idx+1} (exact)")
                    return row_idx + 1, col_idx + 1

        # Partial match fallback
        for row_idx, row in enumerate(data):
            for col_idx, cell in enumerate(row):
                if name_lower in cell.strip().lower():
                    print(f"[Sheet] Team '{team_name}' found at row={row_idx+1} col={col_idx+1} (partial match on '{cell.strip()}')")
                    return row_idx + 1, col_idx + 1

        print(f"[Sheet] ❌ Team '{team_name}' not found in sheet")
        return None, None

    def _find_existing_player(self, player_name, all_data):
        """
        Scan all_data for an existing entry matching player_name.
        Returns the team name string if found, or None.

        Strategy: find the cell, then walk upward in the same column until we
        hit a cell whose value matches a known team name from EMOJI_TEAM_MAP.
        """
        known_teams_lower = {v.lower(): v for v in EMOJI_TEAM_MAP.values()}
        name_lower = player_name.lower().strip()

        for row_idx, row in enumerate(all_data):
            for col_idx, cell in enumerate(row):
                if cell.strip().lower() == name_lower:
                    # Walk upward in this column to find the team header
                    for r in range(row_idx - 1, -1, -1):
                        candidate = (
                            all_data[r][col_idx].strip()
                            if col_idx < len(all_data[r])
                            else ""
                        )
                        if candidate.lower() in known_teams_lower:
                            return known_teams_lower[candidate.lower()]
                    return "another team"  # found player but couldn't identify team

        return None

    def _get_positions(self, player_name, override=None):
        """
        Return an ordered list of positions for a player (e.g. ['PF', 'SF']).
        override (single position string) takes full precedence if provided.
        """
        if override:
            pos = override.upper().strip()
            if pos in POSITION_OFFSETS:
                return [pos]
            return []

        name_lower = player_name.lower().strip()
        pos_str = None

        # Exact match
        for stored, p in PLAYER_POSITIONS.items():
            if stored.lower() == name_lower:
                pos_str = p
                break

        # Partial match fallback
        if pos_str is None:
            for stored, p in PLAYER_POSITIONS.items():
                if name_lower in stored.lower() or stored.lower() in name_lower:
                    pos_str = p
                    break

        if pos_str is None:
            return []

        # Return all positions in order, filtering to valid ones only
        return [p.strip().upper() for p in pos_str.split('/') if p.strip().upper() in POSITION_OFFSETS]

    def add_player(self, team_name, player_name, year=None, price=None, position_override=None, bench_only=False):
        """
        Find the team in the sheet, determine the correct row for the player's
        position, and write the player data.

        Column layout for each team section (starting at the team's column):
          col+0: Player Name  (team name is in row 0 of this section)
          col+1: Year
          col+2: Price

        Position fallback: tries each of the player's positions in order
        (e.g. PF → SF) until an open slot is found.
        bench_only=True skips all starter slots and only tries bench rows.
        """
        print(f"\n{'─'*50}")
        print(f"[Pick] {player_name} → {team_name} | year={year} price={price} pos_override={position_override} bench_only={bench_only}")

        # 1. Find team header cell
        team_row, team_col = self._find_team_cell(team_name)
        if not team_row:
            print(f"[Pick] ❌ Team not found — aborting")
            return False, (
                f"Team **{team_name}** was not found in the sheet.\n"
                f"Make sure the name in `emoji_map.py` matches exactly what's in the sheet."
            )

        # 2. Get all positions for the player (in priority order)
        positions = self._get_positions(player_name, position_override)
        print(f"[Pick] Positions to try: {positions}")
        if not positions:
            print(f"[Pick] ❌ No position found for '{player_name}'")
            return False, (
                f"Position unknown for **{player_name}**.\n"
                f"Add their position at the end of your message (e.g. `PG`, `SG`, `SF`, `PF`, `C`), "
                f"or add them to `player_positions.py`."
            )

        # 3. Duplicate check — player must not already exist anywhere in the sheet
        print(f"[Pick] Checking for duplicates…")
        all_data = _sheets_call(self.ws.get_all_values)
        existing_team = self._find_existing_player(player_name, all_data)
        if existing_team:
            print(f"[Pick] ❌ Duplicate — '{player_name}' already on '{existing_team}'")
            return False, (
                f"**{player_name}** is already on **{existing_team}**. "
                f"Each player can only be on one team."
            )

        # 4. Try slots in this order: all starters first, then all bench.
        #    e.g. for PF/SF: PF Starter → SF Starter → PF Bench → SF Bench
        target_row = None
        slot_label = None
        used_position = None
        col_idx = team_col - 1  # 0-indexed

        start_slot = 1 if bench_only else 0
        print(f"[Pick] Scanning slots (team header at row={team_row} col={team_col})… bench_only={bench_only}")
        for slot_idx in range(start_slot, 2):  # 0 = starter, 1 = bench
            slot_name = "Starter" if slot_idx == 0 else "Bench"
            for position in positions:
                offset = POSITION_OFFSETS[position][slot_idx]
                r = (team_row - 1) + offset  # 0-indexed

                if r >= len(all_data):
                    print(f"[Pick]   {slot_name} {position} (row {team_row + offset}) → empty (row beyond data)")
                    target_row = team_row + offset
                    slot_label = slot_name
                    used_position = position
                    break

                row_data = all_data[r]
                current = row_data[col_idx] if col_idx < len(row_data) else ""

                if not current.strip():
                    print(f"[Pick]   {slot_name} {position} (row {team_row + offset}) → empty ✅")
                    target_row = team_row + offset
                    slot_label = slot_name
                    used_position = position
                    break
                else:
                    print(f"[Pick]   {slot_name} {position} (row {team_row + offset}) → taken by '{current.strip()}'")

            if target_row is not None:
                break

        if target_row is None:
            tried = " / ".join(positions)
            print(f"[Pick] ❌ All slots full for {tried}")
            return False, (
                f"No open slots found for **{player_name}** ({tried}) on **{team_name}**.\n"
                f"All matching position slots are filled."
            )

        # 5. Write to sheet (batch to minimise API calls)
        name_col  = team_col
        year_col  = team_col + 1
        price_col = team_col + 2

        updates = [
            {
                'range': gspread.utils.rowcol_to_a1(target_row, name_col),
                'values': [[player_name]],
            }
        ]
        if year:
            updates.append({
                'range': gspread.utils.rowcol_to_a1(target_row, year_col),
                'values': [[year]],
            })
        if price:
            updates.append({
                'range': gspread.utils.rowcol_to_a1(target_row, price_col),
                'values': [[price]],
            })

        print(f"[Pick] Writing to sheet: row={target_row} col={team_col} → '{player_name}' | year={year} price={price}")
        _sheets_call(self.ws.batch_update, updates)
        print(f"[Pick] ✅ Done — {player_name} ({used_position} {slot_label}) → {team_name} row {target_row}")

        # Record for undo
        self._undo_stack.append({
            'player':   player_name,
            'team':     team_name,
            'row':      target_row,
            'name_col': name_col,
            'year_col': year_col if year else None,
            'price_col': price_col if price else None,
            'slot':     f"{used_position} — {slot_label}",
        })

        return True, (
            f"**{player_name}** ({used_position} — {slot_label}) added to "
            f"**{team_name}** (row {target_row})"
        )

    def undo_last(self):
        """
        Clear the cells written by the most recent successful add_player call.
        Returns (success, message).
        """
        if not self._undo_stack:
            return False, "Nothing to undo."

        entry = self._undo_stack.pop()
        player   = entry['player']
        team     = entry['team']
        row      = entry['row']
        slot     = entry['slot']

        # Build list of A1 ranges to clear
        ranges = [gspread.utils.rowcol_to_a1(row, entry['name_col'])]
        if entry['year_col']:
            ranges.append(gspread.utils.rowcol_to_a1(row, entry['year_col']))
        if entry['price_col']:
            ranges.append(gspread.utils.rowcol_to_a1(row, entry['price_col']))

        print(f"[Undo] Clearing {ranges} for '{player}' on '{team}'")
        _sheets_call(self.ws.batch_clear, ranges)
        print(f"[Undo] ✅ Removed {player} ({slot}) from {team} row {row}")

        return True, f"**{player}** ({slot}) removed from **{team}** (row {row})"


sheet_manager = SheetManager(worksheet) if worksheet else None


# =============================================================================
# MESSAGE PARSER
# =============================================================================

_CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')
_YEAR_RE         = re.compile(r"'?(\d{2})-(\d{2})\b|(\d{4})-(\d{4})\b|(\d{4})-(\d{2})\b|\b(19\d{2}|20[0-4]\d)\b|'(\d{2})\b")
_PRICE_RE        = re.compile(r'\(?(-?\$\d+(?:\.\d+)?)\)?')
_PICK_RE         = re.compile(r'^\s*\d+\.\s*')
_BENCH_POS_RE    = re.compile(r'\bBench\s+(PG|SG|SF|PF|C)\b', re.IGNORECASE)
_BENCH_RE        = re.compile(r'\bBench\b', re.IGNORECASE)
_POSITION_RE     = re.compile(r'\b(PG|SG|SF|PF|C)\b', re.IGNORECASE)


def _normalize_year(raw):
    """
    Normalize year strings:
      '23     → 2022-23  (apostrophe + end-year only)
      1961    → 1960-61  (standalone 4-digit end-year)
      '91-92  → 1991-92
      91-92   → 1991-92
      2019-20 → 2019-20  (unchanged)
    Years 46–99 are treated as 1946–1999; 00–45 as 2000–2045.
    """
    raw = raw.strip()
    # Apostrophe short form "'23" → treat as end-year → "2022-23"
    m = re.match(r"^'(\d{2})$", raw)
    if m:
        end = int(m.group(1))
        century = 2000 if end <= 45 else 1900
        full_end = century + end
        return f"{full_end - 1}-{m.group(1)}"
    # Full 4-digit range "1986-1987" → "1986-87"
    m = re.match(r'^(\d{4})-(\d{4})$', raw)
    if m:
        return f"{m.group(1)}-{m.group(2)[-2:]}"
    # Standalone 4-digit year like "1961" → treat as end-year → "1960-61"
    m = re.match(r'^(19\d{2}|20[0-4]\d)$', raw)
    if m:
        year = int(m.group(1))
        return f"{year - 1}-{str(year)[-2:]}"
    # Short form "91-92" → "1991-92"
    m = re.match(r"'?(\d{2})-(\d{2})$", raw)
    if m:
        start = int(m.group(1))
        century = 1900 if start >= 46 else 2000
        return f"{century + start}-{m.group(2)}"
    return raw


def parse_message(content):
    """
    Parse a player-addition message.

    Supported formats (all optional parts shown in brackets):
      [61.] <:emoji:id> [year] Player Name [year] [$price|($ price)] [POS]

    Returns (data_dict, error_str) — one will always be None.
    """
    text = content.strip()

    # Remove leading pick number e.g. "61. "
    text = _PICK_RE.sub('', text).strip()

    # --- Team emoji (required) ---
    emoji_match = _CUSTOM_EMOJI_RE.search(text)
    if not emoji_match:
        return None, (
            "No team emoji found. "
            "Make sure to include your team's logo emoji in the message."
        )

    emoji_name = emoji_match.group(1)
    text = _CUSTOM_EMOJI_RE.sub('', text, count=1).strip()

    team = EMOJI_TEAM_MAP.get(emoji_name)
    if not team:
        return None, (
            f"Unrecognised emoji **:{emoji_name}:**.\n"
            f"Add it to `emoji_map.py`: `\"{emoji_name}\": \"Team Name\"`"
        )

    # --- Bench + position override (optional, e.g. "Bench PF" at end) ---
    bench_only = False
    bench_pos_match = _BENCH_POS_RE.search(text)
    if bench_pos_match:
        bench_only = True
        position_override = bench_pos_match.group(1).upper()
        text = (text[:bench_pos_match.start()] + text[bench_pos_match.end():]).strip()
    else:
        # Standalone "Bench" with no position — bench-only, position auto-detected
        bench_match = _BENCH_RE.search(text)
        if bench_match:
            bench_only = True
            text = (text[:bench_match.start()] + text[bench_match.end():]).strip()

        # Standalone position override (e.g. just "PF" at end)
        pos_match = _POSITION_RE.search(text)
        position_override = pos_match.group(1).upper() if pos_match else None
        if pos_match:
            text = (text[:pos_match.start()] + text[pos_match.end():]).strip()

    # --- Price (optional) ---
    price_match = _PRICE_RE.search(text)
    price = price_match.group(1) if price_match else None  # already includes $ and optional -
    if price_match:
        text = (text[:price_match.start()] + text[price_match.end():]).strip()

    # --- Year (optional) ---
    year_match = _YEAR_RE.search(text)
    year = _normalize_year(year_match.group(0)) if year_match else None
    if year_match:
        text = (text[:year_match.start()] + text[year_match.end():]).strip()

    # Clean up leftover punctuation / empty parens / extra spaces
    player = re.sub(r'\(\s*\)', '', text)                    # remove empty ()
    player = re.sub(r'(?i)^\s*select\s+', '', player)        # strip leading "select"
    player = re.sub(r'(?i)\s+for[\s.,;:]*$', '', player)      # strip trailing "for"
    player = re.sub(r'\s*-\s*', ' ', player)                 # collapse stray dashes
    player = player.strip('.,;: ')                            # strip trailing punctuation
    player = re.sub(r'\s+', ' ', player).strip()

    if not player:
        return None, "Could not find a player name in the message."

    return {
        'emoji_name':        emoji_name,
        'team':              team,
        'player':            player,
        'year':              year,
        'price':             price,
        'position_override': position_override,
        'bench_only':        bench_only,
    }, None


# =============================================================================
# BOT
# =============================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required to read ctx.author.roles for permission checks

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

COMMISSIONER_ROLE = "LeComissioner"


@bot.check
async def require_commissioner(ctx):
    """Global check — every command requires the LeComissioner role."""
    if any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles):
        return True
    raise commands.CheckFailure("not_commissioner")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        if ctx.channel.id == DISCORD_CHANNEL_ID:
            await ctx.send(
                f"❌ You don't have permission to use bot commands. "
                f"Contact **Soapz** to apply for **LeCommish**."
            )
    elif isinstance(error, commands.CommandNotFound):
        pass  # ignore unknown commands silently


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        print(f"✅ Monitoring #{channel.name} (ID: {DISCORD_CHANNEL_ID})")
    else:
        print(f"⚠️  Channel {DISCORD_CHANNEL_ID} not found — check DISCORD_CHANNEL_ID")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Always process commands regardless of channel
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return

    # Only handle non-command messages in the designated channel
    if message.channel.id != DISCORD_CHANNEL_ID:
        return

    if not sheet_manager:
        await message.channel.send("❌ Not connected to Google Sheets. Check the console.")
        return

    print(f"\n[Msg] #{message.channel.name} | {message.author.display_name}: {message.content[:120]}")
    data, error = parse_message(message.content)

    if error:
        print(f"[Parse] ❌ {error}")
        err_msg = await message.channel.send(f"❌ {error}")
        await asyncio.sleep(10)
        await err_msg.delete()
        return

    print(f"[Parse] ✅ emoji={data['emoji_name']} team={data['team']} player={data['player']} year={data['year']} price={data['price']} bench_only={data['bench_only']}")

    async with message.channel.typing():
        success, result = sheet_manager.add_player(
            data['team'],
            data['player'],
            year=data['year'],
            price=data['price'],
            position_override=data['position_override'],
            bench_only=data['bench_only'],
        )

    if success:
        await message.add_reaction('✅')

        embed = discord.Embed(color=discord.Color.green(), timestamp=datetime.utcnow())
        embed.add_field(name="Team",   value=data['team'],   inline=True)
        embed.add_field(name="Player", value=data['player'], inline=True)
        if data['year']:
            embed.add_field(name="Year",  value=data['year'],  inline=True)
        if data['price']:
            embed.add_field(name="Price", value=data['price'], inline=True)
        embed.set_footer(text=f"{result}  •  Added by {message.author.display_name}")
        await message.channel.send(embed=embed)
    else:
        await message.add_reaction('❌')
        await message.channel.send(f"❌ {result}")


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name='reload')
async def cmd_reload(ctx):
    """Reconnect to Google Sheets and refresh data."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    global worksheet, sheet_manager
    try:
        worksheet = connect_sheets()
        sheet_manager = SheetManager(worksheet)
        await ctx.send("✅ Reconnected to Google Sheets.")
    except Exception as e:
        await ctx.send(f"❌ Reconnect failed: {e}")


@bot.command(name='sheetundo')
async def cmd_sheetundo(ctx):
    """Undo the last player addition."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    if not sheet_manager:
        await ctx.send("❌ Not connected to Google Sheets.")
        return
    try:
        success, result = sheet_manager.undo_last()
    except Exception as e:
        await ctx.send(f"❌ Undo failed: {e}")
        return
    if success:
        await ctx.send(f"↩️ Undone: {result}")
    else:
        await ctx.send(f"❌ {result}")


@bot.command(name='teams')
async def cmd_teams(ctx):
    """List all configured emoji → team mappings."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    if not EMOJI_TEAM_MAP:
        await ctx.send("No teams configured in `emoji_map.py` yet.")
        return
    lines = [f"**:{k}:** → {v}" for k, v in sorted(EMOJI_TEAM_MAP.items())]
    embed = discord.Embed(
        title="Configured Team Emojis",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await ctx.send(embed=embed)


class HelpView(discord.ui.View):
    """Paginated view for !sheethelp."""

    def __init__(self, embeds: list):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.page = 0
        self.message = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page == len(self.embeds) - 1)
        # Update footer to show current page
        self.embeds[self.page].set_footer(text=f"Page {self.page + 1} of {len(self.embeds)}")

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


@bot.command(name='sheethelp')
async def cmd_sheethelp(ctx):
    """Detailed explanation of everything the bot does."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return

    # ── Embed 1: Message format ───────────────────────────────────────────────
    e1 = discord.Embed(
        title="ATD Team Sheet Bot — Full Guide (1/4)",
        description="How to post a pick in this channel.",
        color=discord.Color.blue(),
    )
    e1.add_field(
        name="Message format",
        value=(
            "```[pick#.] <team emoji> [year] Player Name [year] [$price] [POS | Bench POS]```\n"
            "Every part except the **team emoji** and **player name** is optional.\n"
            "The pick number at the start (e.g. `24.`) is ignored automatically."
        ),
        inline=False,
    )
    e1.add_field(
        name="Examples",
        value=(
            "`24. <:Warriors:123> Draymond Green 2015-16`\n"
            "`<:Spurs:456> 2002-03 Tim Duncan $0`\n"
            "`61. <:MIL:789> 91-92 Dennis Rodman ($3) PF`\n"
            "`<:GSW:123> Klay Thompson 2016-17 Bench SG`"
        ),
        inline=False,
    )
    e1.add_field(
        name="Team emoji",
        value=(
            "Use your server's **custom team emoji** — it tells the bot which team to update.\n"
            "To find an emoji's exact name, type `\\:YourEmoji:` in Discord.\n"
            "The bot maps emoji names → team names via `emoji_map.py`."
        ),
        inline=False,
    )
    e1.add_field(
        name="Year",
        value=(
            "Accepts `2015-16` or short form `91-92` (auto-expanded to `1991-92`).\n"
            "Years 46–99 → 1946–1999 | Years 00–45 → 2000–2045.\n"
            "Year can appear **before or after** the player name."
        ),
        inline=False,
    )
    e1.add_field(
        name="Price",
        value=(
            "Accepts `$26`, `($3)`, or `$ 3`. Written to the sheet alongside the player.\n"
            "Omit it entirely if your draft doesn't use prices."
        ),
        inline=False,
    )

    # ── Embed 2: Position logic ───────────────────────────────────────────────
    e2 = discord.Embed(
        title="ATD Team Sheet Bot — Full Guide (2/4)",
        description="How positions and sheet rows work.",
        color=discord.Color.blue(),
    )
    e2.add_field(
        name="Sheet layout (per team)",
        value=(
            "Each team occupies **11 rows** in the sheet:\n"
            "```"
            "Row +0  Team Name\n"
            "Row +1  Starting PG\n"
            "Row +2  Starting SG\n"
            "Row +3  Starting SF\n"
            "Row +4  Starting PF\n"
            "Row +5  Starting C\n"
            "Row +6  Bench PG\n"
            "Row +7  Bench SG\n"
            "Row +8  Bench SF\n"
            "Row +9  Bench PF\n"
            "Row +10 Bench C"
            "```"
            "Teams can be placed **anywhere** in the sheet — the bot scans every cell."
        ),
        inline=False,
    )
    e2.add_field(
        name="Auto-detected position",
        value=(
            "The bot looks up the player in `player_positions.py` (300+ players).\n"
            "Players can have multiple positions, e.g. LeBron is `PF/SF`."
        ),
        inline=False,
    )
    e2.add_field(
        name="Slot fill order (default)",
        value=(
            "For a player with positions `PF/SF`, the bot tries in this order:\n"
            "1. PF Starter → 2. SF Starter → 3. PF Bench → 4. SF Bench\n\n"
            "**All starters are tried before any bench slot.**"
        ),
        inline=False,
    )
    e2.add_field(
        name="Position overrides",
        value=(
            "`PF` at end → force PF slot (still tries starter first, then bench)\n"
            "`Bench PF` at end → force **bench PF slot only**, skip all starters\n"
            "`Bench` alone → force bench for auto-detected position(s)\n\n"
            "If a player's position is unknown, you **must** add a position override, "
            "or add them to `player_positions.py`."
        ),
        inline=False,
    )

    # ── Embed 3: Duplicate check + undo ──────────────────────────────────────
    e3 = discord.Embed(
        title="ATD Team Sheet Bot — Full Guide (3/4)",
        description="Duplicate detection, undo, and error messages.",
        color=discord.Color.blue(),
    )
    e3.add_field(
        name="Duplicate detection",
        value=(
            "Before writing, the bot scans the **entire sheet** for the player's name.\n"
            "If found, the pick is rejected with a message naming the team they're already on.\n"
            "Each player can only appear on one team."
        ),
        inline=False,
    )
    e3.add_field(
        name="Undo",
        value=(
            "`!sheetundo` — Removes the last successfully added player from the sheet.\n"
            "Can be used multiple times to step back through picks one by one.\n"
            "**Undo history resets when the bot restarts.**"
        ),
        inline=False,
    )
    e3.add_field(
        name="Common error messages",
        value=(
            "❌ *No team emoji found* — Your message didn't include a custom team emoji.\n"
            "❌ *Unrecognised emoji :X:* — Add it to `emoji_map.py`.\n"
            "❌ *Team not found in sheet* — Team name in `emoji_map.py` doesn't match the sheet.\n"
            "❌ *Position unknown* — Player not in `player_positions.py`; add a position override.\n"
            "❌ *Already on [team]* — Duplicate pick; player is already on another team.\n"
            "❌ *No open slots* — All matching position slots for this team are filled."
        ),
        inline=False,
    )
    e3.add_field(
        name="Connection drops",
        value=(
            "If the Google Sheets connection drops mid-pick, the bot automatically "
            "reconnects and retries up to 3 times. You'll see `[Sheets] Error — reconnecting…` "
            "in the terminal. The pick succeeds silently from Discord's perspective."
        ),
        inline=False,
    )

    # ── Embed 4: Commands reference ───────────────────────────────────────────
    e4 = discord.Embed(
        title="ATD Team Sheet Bot — Full Guide (4/4)",
        description="All available commands.",
        color=discord.Color.blue(),
    )
    e4.add_field(
        name="Commands",
        value=(
            "`!sheethelp` — This guide\n"
            "`!sheetinfo` — Quick-reference summary\n"
            "`!teams` — List all emoji → team mappings currently configured\n"
            "`!sheetundo` — Undo the last player addition\n"
            "`!reload` — Force-reconnect to Google Sheets (use if the bot seems stuck)"
        ),
        inline=False,
    )
    e4.add_field(
        name="Config files (for admins)",
        value=(
            "`emoji_map.py` — Maps custom emoji names to team names in the sheet.\n"
            "`player_positions.py` — Stores each player's position(s) in priority order.\n"
            "`.env` — Discord token, channel ID, spreadsheet ID, worksheet name."
        ),
        inline=False,
    )

    view = HelpView([e1, e2, e3, e4])
    view.message = await ctx.send(embed=e1, view=view)


@bot.command(name='sheetinfo')
async def cmd_help(ctx):
    """Show usage instructions."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return
    embed = discord.Embed(
        title="ATD Team Sheet Bot",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Adding a player",
        value=(
            "Post in this channel (no command prefix needed):\n"
            "```[pick#.] <team emoji> [year] Player Name [year] [$price] [POS]```\n"
            "**Examples:**\n"
            "`61. :MIL: 91-92 Dennis Rodman ($3)`\n"
            "`<:WAS:123> James Harden 2019-20 $26`\n"
            "`<:TOR:456> Kawhi Leonard 2018-19`\n\n"
            "**Year** can appear before or after the player name.\n"
            "**Price** is optional (`$26` or `($3)`).\n"
            "**Position** is auto-detected. Add `PG`/`SG`/`SF`/`PF`/`C` to override.\n"
            "Add `Bench PF` to force bench slot for that position. Add `Bench` alone to force bench (auto-detect pos)."
        ),
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=(
            "`!teams` — List all emoji → team mappings\n"
            "`!sheetundo` — Undo the last player addition\n"
            "`!reload` — Reconnect to Google Sheets\n"
            "`!sheetinfo` — This summary\n"
            "`!sheethelp` — Full guide with all details"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not set in .env")
    elif not DISCORD_CHANNEL_ID:
        print("❌ DISCORD_CHANNEL_ID not set in .env")
    elif not worksheet:
        print("❌ Could not connect to Google Sheets — check credentials and SPREADSHEET_ID")
    else:
        print("🚀 Starting ATD Team Sheet Bot...")
        bot.run(DISCORD_TOKEN)
