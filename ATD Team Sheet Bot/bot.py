import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import asyncio
import time
import json
import os
from datetime import datetime
import requests.exceptions
from config import DISCORD_TOKEN, DISCORD_CHANNEL_ID, SPREADSHEET_ID, SERVICE_ACCOUNT_FILE, WORKSHEET_NAME, PRICE_REQUIRED, DRAFT_LIST_BOT_ID
from player_positions import PLAYER_POSITIONS
from emoji_map import EMOJI_TEAM_MAP, UNICODE_EMOJI_MAP

# ── Persistent storage ────────────────────────────────────────────────────────
_CONFIG_FILE  = "/data/sheet_config.json"
_UNDO_FILE    = "/data/undo_stack.json"
_AUDIT_FILE   = "/data/audit.json"
_OWNERS_FILE  = "/data/team_owners.json"


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict):
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(cfg, f)


def _load_undo() -> dict:
    try:
        with open(_UNDO_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _persist_undo(channel_id: int, stack: list):
    data = _load_undo()
    data[str(channel_id)] = stack
    os.makedirs(os.path.dirname(_UNDO_FILE), exist_ok=True)
    with open(_UNDO_FILE, "w") as f:
        json.dump(data, f)


def _load_audit() -> list:
    try:
        with open(_AUDIT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _log_audit(entry: dict):
    log = _load_audit()
    log.append(entry)
    os.makedirs(os.path.dirname(_AUDIT_FILE), exist_ok=True)
    with open(_AUDIT_FILE, "w") as f:
        json.dump(log, f)


def _load_owners() -> dict:
    """Load team ownership: {user_id_str: team_name}"""
    try:
        with open(_OWNERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_owners(owners: dict):
    os.makedirs(os.path.dirname(_OWNERS_FILE), exist_ok=True)
    with open(_OWNERS_FILE, "w") as f:
        json.dump(owners, f)


_team_owners: dict[str, str] = _load_owners()


_config = _load_config()

# channel_sheet_map: {channel_id (int): {"tab": str, "sheet_id": str}}
def _migrate_entry(v):
    """Migrate old string-value entries to the new dict format."""
    if isinstance(v, str):
        return {"tab": v, "sheet_id": SPREADSHEET_ID}
    return v

_channel_sheet_map: dict[int, dict] = {
    int(k): _migrate_entry(v) for k, v in _config.get("channel_sheet_map", {}).items()
}
# Backward compat: seed from legacy single-sheet config
if not _channel_sheet_map and DISCORD_CHANNEL_ID:
    _channel_sheet_map[DISCORD_CHANNEL_ID] = {
        "tab": _config.get("worksheet_name") or WORKSHEET_NAME,
        "sheet_id": SPREADSHEET_ID,
    }

# Per-channel SheetManager instances (lazy-initialised on first use)
_channel_managers: dict[int, "SheetManager"] = {}


def _persist_channel_map():
    cfg = _load_config()
    cfg["channel_sheet_map"] = {str(k): v for k, v in _channel_sheet_map.items()}
    _save_config(cfg)


def _set_channel_sheet(channel_id: int, tab: str, spreadsheet_id: str = None):
    _channel_sheet_map[channel_id] = {
        "tab": tab,
        "sheet_id": spreadsheet_id or SPREADSHEET_ID,
    }
    _channel_managers.pop(channel_id, None)
    _persist_channel_map()


def _remove_channel_sheet(channel_id: int):
    _channel_sheet_map.pop(channel_id, None)
    _channel_managers.pop(channel_id, None)
    _persist_channel_map()


def _get_manager(channel_id: int) -> "SheetManager | None":
    """Return the SheetManager for a channel, creating it lazily if needed."""
    if channel_id not in _channel_sheet_map:
        return None
    if channel_id not in _channel_managers:
        entry = _channel_sheet_map[channel_id]
        try:
            ws = connect_sheets(entry["tab"], entry["sheet_id"])
            _channel_managers[channel_id] = SheetManager(ws, entry["sheet_id"], channel_id)
        except Exception as e:
            print(f"[Setup] ❌ Cannot connect to '{entry['tab']}' for channel {channel_id}: {e}")
            return None
    return _channel_managers.get(channel_id)


async def _get_manager_async(channel_id: int) -> "SheetManager | None":
    """Async wrapper — runs the blocking _get_manager in a thread pool."""
    if channel_id not in _channel_sheet_map:
        return None
    if channel_id in _channel_managers:
        return _channel_managers[channel_id]
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_manager, channel_id)


def _extract_spreadsheet_id(args: str) -> tuple:
    """
    Parse an optional spreadsheet ID or Google Sheets URL from the start of args.
    Returns (spreadsheet_id_or_None, remaining_tab_name).
    """
    url_match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]{20,})', args)
    if url_match:
        sid = url_match.group(1)
        remaining = re.sub(r'https?://\S+', '', args).strip()
        return sid, remaining
    # Bare ID: first token is 30+ alphanumeric chars (won't match any tab name)
    first, _, rest = args.partition(' ')
    if re.fullmatch(r'[a-zA-Z0-9_-]{30,}', first):
        return first, rest.strip()
    return None, args


# =============================================================================
# GOOGLE SHEETS
# =============================================================================

def connect_sheets(sheet_name: str, spreadsheet_id: str = None):
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    sid = spreadsheet_id or SPREADSHEET_ID
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    client.session.timeout = 15
    ws = client.open_by_key(sid).worksheet(sheet_name)
    print(f"✅ Connected to worksheet: '{sheet_name}' (spreadsheet: {sid})")
    return ws


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
    def __init__(self, ws, spreadsheet_id: str = None, channel_id: int = None):
        self.ws = ws
        self._spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
        self._channel_id = channel_id
        undo_data = _load_undo()
        self._undo_stack = undo_data.get(str(channel_id), []) if channel_id else []

    def _call(self, method_name, *args, retries=3, **kwargs):
        """
        Call a gspread worksheet method by name, retrying up to `retries` times
        on transient errors. Reconnects the worksheet between attempts.
        """
        for attempt in range(1, retries + 1):
            try:
                return getattr(self.ws, method_name)(*args, **kwargs)
            except Exception as e:
                if attempt == retries:
                    raise
                print(f"[Sheets] Error (attempt {attempt}/{retries}): {type(e).__name__}: {e} — reconnecting…")
                time.sleep(2)
                try:
                    self.ws = connect_sheets(self.ws.title, self._spreadsheet_id)
                except Exception:
                    pass

    def _find_team_cell(self, team_name, data=None):
        """
        Scan every cell in the sheet for the team name.
        Returns (row, col) as 1-indexed integers, or (None, None).
        Supports any grid layout — teams can be anywhere.
        If data is provided, uses it instead of fetching from the API.
        """
        if data is None:
            print(f"[Sheet] Fetching sheet data to locate '{team_name}'…")
            data = self._call('get_all_values')
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

        # 1. Fetch sheet data once for team lookup, duplicate check, and slot scan
        all_data = self._call('get_all_values')

        team_row, team_col = self._find_team_cell(team_name, data=all_data)
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
            # Strip "$" and write as a number so SUM formulas work in Sheets
            try:
                price_num = int(float(re.sub(r'[^\d.]', '', price)))
            except (ValueError, TypeError):
                price_num = price
            updates.append({
                'range': gspread.utils.rowcol_to_a1(target_row, price_col),
                'values': [[price_num]],
            })

        print(f"[Pick] Writing to sheet: row={target_row} col={team_col} → '{player_name}' | year={year} price={price}")
        self._call('batch_update', updates)
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
        if self._channel_id:
            _persist_undo(self._channel_id, self._undo_stack)

        return True, (
            f"**{player_name}** ({used_position} — {slot_label}) added to "
            f"**{team_name}** (row {target_row})"
        )

    def get_roster(self, team_name):
        """
        Read the 10 roster slots (5 starters + 5 bench) for a team.
        Returns (team_name, roster_list) or (None, error_str).
        """
        all_data = self._call('get_all_values')
        team_row, team_col = self._find_team_cell(team_name, data=all_data)
        if not team_row:
            return None, f"Team **{team_name}** was not found in the sheet."
        col_idx = team_col - 1  # 0-indexed

        SLOTS = [
            ("PG", "Starter", 1),
            ("SG", "Starter", 2),
            ("SF", "Starter", 3),
            ("PF", "Starter", 4),
            ("C",  "Starter", 5),
            ("PG", "Bench",   6),
            ("SG", "Bench",   7),
            ("SF", "Bench",   8),
            ("PF", "Bench",   9),
            ("C",  "Bench",  10),
        ]

        roster = []
        for pos, slot_type, offset in SLOTS:
            r = (team_row - 1) + offset  # 0-indexed row
            player = year = price = ""
            if r < len(all_data):
                row_data = all_data[r]
                player = row_data[col_idx].strip() if col_idx < len(row_data) else ""
                year = row_data[col_idx + 1].strip() if (col_idx + 1) < len(row_data) else ""
                price = row_data[col_idx + 2].strip() if (col_idx + 2) < len(row_data) else ""
            roster.append({
                "position": pos,
                "slot": slot_type,
                "player": player,
                "year": year,
                "price": price,
            })

        return team_name, roster

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
        self._call('batch_clear', ranges)
        if self._channel_id:
            _persist_undo(self._channel_id, self._undo_stack)
        print(f"[Undo] ✅ Removed {player} ({slot}) from {team} row {row}")

        return True, f"**{player}** ({slot}) removed from **{team}** (row {row})"


# =============================================================================
# MESSAGE PARSER
# =============================================================================

_CUSTOM_EMOJI_RE = re.compile(r'<a?:([\w~]+):(\d+)>')
_YEAR_RE         = re.compile(r"'?(\d{2})-(\d{2})\b|(\d{4})-(\d{4})\b|(\d{4})-(\d{2})\b|\b(19\d{2}|20[0-4]\d)\b|'(\d{2})\b|\b(\d{2})'|\b(\d{2})\b")
_PRICE_RE        = re.compile(r'\(?(-?\$\d+(?:\.\d+)?)\)?|\((\d+(?:\.\d+)?)\)|\b(\d+(?:\.\d+)?)\$')
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
    # Trailing apostrophe "13'" → treat as end-year → "2012-13"
    m = re.match(r"^(\d{2})'$", raw)
    if m:
        end = int(m.group(1))
        century = 2000 if end <= 45 else 1900
        full_end = century + end
        return f"{full_end - 1}-{m.group(1)}"
    # Leading apostrophe "'23" → treat as end-year → "2022-23"
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
    # Bare 2-digit year "23" → "2022-23", "87" → "1986-87"
    m = re.match(r'^(\d{2})$', raw)
    if m:
        end = int(m.group(1))
        century = 2000 if end <= 45 else 1900
        full_end = century + end
        return f"{full_end - 1}-{m.group(1)}"
    return raw


def parse_message(content):
    """
    Parse a player-addition message.

    Supported formats (all optional parts shown in brackets):
      [61.] <:emoji:id> [year] Player Name [year] [$price|($ price)] [POS]

    Returns (data_dict, error_str) — one will always be None.
    """
    text = content.strip()

    # Normalize curly/smart apostrophes → straight apostrophe so year regex matches
    text = text.replace('‘', "'").replace('’', "'").replace('ʼ', "'")

    # Remove leading pick number e.g. "61. "
    text = _PICK_RE.sub('', text).strip()

    # --- Team emoji (required) ---
    emoji_match = _CUSTOM_EMOJI_RE.search(text)
    if emoji_match:
        emoji_name = emoji_match.group(1)
        text = _CUSTOM_EMOJI_RE.sub('', text, count=1).strip()
        team = EMOJI_TEAM_MAP.get(emoji_name)
        if not team:
            return None, (
                f"Unrecognised emoji **:{emoji_name}:**.\n"
                f"Add it to `emoji_map.py`: `\"{emoji_name}\": \"Team Name\"`"
            )
    else:
        # Fall back to built-in Unicode emojis (e.g. flag_fr 🇫🇷)
        team = None
        emoji_name = None
        for char, team_name in UNICODE_EMOJI_MAP.items():
            if char in text:
                team = team_name
                emoji_name = char
                text = text.replace(char, '', 1).strip()
                break
        if not team:
            return None, (
                "No team emoji found. "
                "Make sure to include your team's logo emoji in the message."
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

    # --- TBD year placeholder (strip silently, don't write to sheet) ---
    text = re.sub(r'\btbd\b', '', text, flags=re.IGNORECASE).strip()

    # --- Price (optional) ---
    price_match = _PRICE_RE.search(text)
    if price_match:
        # group(1) = "$42", group(2) = "(42)", group(3) = "42$"
        price = price_match.group(1) or f"${price_match.group(2) or price_match.group(3)}"
        text = (text[:price_match.start()] + text[price_match.end():]).strip()
    else:
        price = None

    # --- Year (optional) ---
    year_match = _YEAR_RE.search(text)
    year = _normalize_year(year_match.group(0)) if year_match else None
    if year_match:
        text = (text[:year_match.start()] + text[year_match.end():]).strip()

    # Clean up leftover punctuation / empty parens / extra spaces
    player = re.sub(r'\(\s*\)', '', text)                    # remove empty ()
    player = re.sub(r'(?i)^\s*select\s+', '', player)        # strip leading "select"
    player = re.sub(r'(?i)\s+for[\s.,;:]*$', '', player)      # strip trailing "for"
    player = re.sub(r'\s+-\s+', ' ', player)                 # collapse stray dashes (space-dash-space only, preserves hyphenated names)
    player = player.strip('.,;: ')                            # strip trailing punctuation
    player = re.sub(r'\s+', ' ', player).strip()
    player = player.title()

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


_PUBLIC_COMMANDS = {'roster', 'find', 'available', 'teams', 'sheethelp', 'sheetinfo', 'claimteam', 'swap', 'myteam', 'addyear'}


@bot.check
async def require_commissioner(ctx):
    """Global check — requires LeComissioner role OR server administrator permission.
    Read-only commands in _PUBLIC_COMMANDS are exempt."""
    if ctx.command and ctx.command.name in _PUBLIC_COMMANDS:
        return True
    if ctx.author.guild_permissions.administrator:
        return True
    if any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles):
        return True
    raise commands.CheckFailure("not_commissioner")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(
            f"❌ You don't have permission to use bot commands. "
            f"Contact **Soapz** to apply for **LeCommish**."
        )
    elif isinstance(error, commands.MissingRequiredArgument):
        if ctx.command and ctx.command.name == 'roster':
            await ctx.send("❌ Please specify a team. Usage: `!roster <team name or emoji>`")
        elif ctx.command and ctx.command.name == 'find':
            await ctx.send("❌ Please specify a player. Usage: `!find <player name>`")
        elif ctx.command and ctx.command.name == 'available':
            await ctx.send("❌ Please specify a position. Usage: `!available PG` (PG/SG/SF/PF/C)")
        else:
            await ctx.send(f"❌ Missing required argument: `{error.param.name}`")
    elif isinstance(error, commands.CommandNotFound):
        pass  # ignore unknown commands silently
    elif isinstance(error, commands.CommandInvokeError):
        print(f"[Error] Command '{ctx.command}' raised: {error.original}")
        await ctx.send(f"❌ Something went wrong: {error.original}")
    else:
        print(f"[Error] Unhandled: {error}")
        await ctx.send(f"❌ Unexpected error: {error}")


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    if not _channel_sheet_map:
        print("⚠️  No channel→sheet mappings configured. Use !addchannel or !setsheet.")
        return
    for ch_id, entry in _channel_sheet_map.items():
        ch = bot.get_channel(ch_id)
        ch_name = f"#{ch.name}" if ch else str(ch_id)
        print(f"📋 {ch_name} → '{entry['tab']}' (sheet: {entry['sheet_id']}) — will connect on first use")


@bot.event
async def on_message(message):
    # Let the Draft List Bot's picks through; ignore all other bot messages.
    _from_draft_list = bool(DRAFT_LIST_BOT_ID and message.author.id == DRAFT_LIST_BOT_ID)
    if message.author.bot and not _from_draft_list:
        return

    # Always process commands regardless of channel
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return

    # Only handle pick messages in channels that have a sheet mapping
    manager = await _get_manager_async(message.channel.id)
    if not manager:
        return

    print(f"\n[Msg] #{message.channel.name} | {message.author.display_name}: {message.content[:120]}")
    data, error = parse_message(message.content)

    if error:
        print(f"[Parse] ❌ {error}")
        # Respond if the message looks like a pick attempt:
        # has a custom emoji, OR starts with a pick number (e.g. "14.")
        looks_like_pick = bool(
            _CUSTOM_EMOJI_RE.search(message.content)
            or re.match(r'^\s*\d+\.', message.content)
        )
        if looks_like_pick:
            await message.add_reaction('❌')
            await message.channel.send(f"❌ {error}")
        return

    print(f"[Parse] ✅ emoji={data['emoji_name']} team={data['team']} player={data['player']} year={data['year']} price={data['price']} bench_only={data['bench_only']}")

    # Price is mandatory for this draft theme
    if PRICE_REQUIRED and not data.get('price'):
        await message.add_reaction('❌')
        await message.channel.send(
            "❌ **Price is required** for this draft. Include the price in your pick, e.g. `$26`.\n"
            "Format: `14. <:YourEmoji:> Player Name year $price`"
        )
        return

    try:
        loop = asyncio.get_event_loop()
        async with message.channel.typing():
            success, result = await loop.run_in_executor(None, lambda: manager.add_player(
                data['team'],
                data['player'],
                year=data['year'],
                price=data['price'],
                position_override=data['position_override'],
                bench_only=data['bench_only'],
            ))
    except Exception as exc:
        print(f"[Pick] ❌ Exception in add_player: {exc}")
        await message.add_reaction('❌')
        await message.channel.send(f"❌ Sheet error: {exc}")
        return

    if success:
        await message.add_reaction('✅')
        _log_audit({
            "timestamp": datetime.utcnow().isoformat(),
            "channel_id": message.channel.id,
            "user_id": message.author.id,
            "user_name": message.author.display_name,
            "player": data['player'],
            "team": data['team'],
            "year": data['year'],
            "price": data['price'],
            "slot": result,
        })

        embed = discord.Embed(color=discord.Color.green(), timestamp=datetime.utcnow())
        embed.add_field(name="Team",   value=data['team'],   inline=True)
        embed.add_field(name="Player", value=data['player'], inline=True)
        if data['year']:
            embed.add_field(name="Year",  value=data['year'],  inline=True)
        if data['price']:
            embed.add_field(name="Price", value=data['price'], inline=True)
        embed.set_footer(text=f"{result}  •  Added by {message.author.display_name}")
        bot_msg = await message.channel.send(embed=embed)
        await asyncio.sleep(60)
        await bot_msg.delete()
    else:
        await message.add_reaction('❌')
        await message.channel.send(f"❌ {result}")


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name='reload')
async def cmd_reload(ctx):
    """Reconnect to Google Sheets for this channel."""
    channel_id = ctx.channel.id
    if channel_id not in _channel_sheet_map:
        await ctx.send(
            "❌ This channel has no sheet mapping. "
            "Use `!setsheet <Tab Name>` to map it first."
        )
        return
    entry = _channel_sheet_map[channel_id]
    tab, sid = entry["tab"], entry["sheet_id"]
    try:
        loop = asyncio.get_event_loop()
        ws = await loop.run_in_executor(None, connect_sheets, tab, sid)
        _channel_managers[channel_id] = SheetManager(ws, sid, channel_id)
        await ctx.send(f"✅ Reconnected to worksheet **{tab}**.")
    except Exception as e:
        await ctx.send(f"❌ Reconnect failed: {e}")


@bot.command(name='setsheet')
async def cmd_setsheet(ctx, *, args: str):
    """Map this channel to a worksheet tab. Usage: !setsheet [SpreadsheetID_or_URL] Tab Name"""
    sid, tab = _extract_spreadsheet_id(args)
    sid = sid or SPREADSHEET_ID
    if not tab:
        await ctx.send("❌ Please provide a tab name. Usage: `!setsheet [SpreadsheetID] Tab Name`")
        return
    try:
        loop = asyncio.get_event_loop()
        ws = await loop.run_in_executor(None, connect_sheets, tab, sid)
        _set_channel_sheet(ctx.channel.id, tab, sid)
        _channel_managers[ctx.channel.id] = SheetManager(ws, sid, ctx.channel.id)
        await ctx.send(f"✅ This channel now writes to worksheet **{tab}**.")
    except gspread.exceptions.WorksheetNotFound:
        await ctx.send(f"❌ No worksheet tab named **{tab}** found. Check the spelling.")
    except Exception as e:
        await ctx.send(f"❌ Failed to switch sheet: {e}")


@bot.command(name='addchannel')
async def cmd_addchannel(ctx, channel: discord.TextChannel, *, args: str):
    """Map a channel to a worksheet tab. Usage: !addchannel #channel [SpreadsheetID_or_URL] Tab Name"""
    sid, tab = _extract_spreadsheet_id(args)
    sid = sid or SPREADSHEET_ID
    if not tab:
        await ctx.send("❌ Please provide a tab name. Usage: `!addchannel #channel [SpreadsheetID] Tab Name`")
        return
    try:
        loop = asyncio.get_event_loop()
        ws = await loop.run_in_executor(None, connect_sheets, tab, sid)
        _set_channel_sheet(channel.id, tab, sid)
        _channel_managers[channel.id] = SheetManager(ws, sid, channel.id)
        await ctx.send(f"✅ **#{channel.name}** → worksheet **{tab}**.")
    except gspread.exceptions.WorksheetNotFound:
        await ctx.send(f"❌ No worksheet tab named **{tab}** found. Check the spelling.")
    except Exception as e:
        await ctx.send(f"❌ Failed: {e}")


@bot.command(name='removechannel')
async def cmd_removechannel(ctx, channel: discord.TextChannel):
    """Remove a channel→sheet mapping. Usage: !removechannel #channel"""
    if channel.id not in _channel_sheet_map:
        await ctx.send(f"❌ **#{channel.name}** has no sheet mapping.")
        return
    _remove_channel_sheet(channel.id)
    await ctx.send(f"✅ Removed mapping for **#{channel.name}**. The bot will no longer process picks there.")


@bot.command(name='channels')
async def cmd_channels(ctx):
    """List all channel→sheet mappings."""
    if not _channel_sheet_map:
        await ctx.send("No channel→sheet mappings configured. Use `!addchannel` or `!setsheet` to add one.")
        return
    lines = []
    for ch_id, entry in _channel_sheet_map.items():
        ch_ref = f"<#{ch_id}>"
        tab = entry["tab"]
        sid = entry["sheet_id"]
        short_id = sid[:10] + "…" if len(sid) > 10 else sid
        lines.append(f"{ch_ref} → **{tab}** (`{short_id}`)")
    embed = discord.Embed(
        title="Channel → Sheet Mappings",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await ctx.send(embed=embed)


@bot.command(name='sheetundo')
async def cmd_sheetundo(ctx):
    """Undo the last player addition in this channel."""
    manager = await _get_manager_async(ctx.channel.id)
    if not manager:
        await ctx.send("❌ This channel has no sheet mapping.")
        return
    try:
        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(None, manager.undo_last)
    except Exception as e:
        await ctx.send(f"❌ Undo failed: {e}")
        return
    if success:
        await ctx.send(f"↩️ Undone: {result}")
    else:
        await ctx.send(f"❌ {result}")


# ── Team ownership & swap commands ────────────────────────────────────────────

class ClaimApprovalView(discord.ui.View):
    """Buttons for commissioner to approve/deny a team claim."""

    def __init__(self, user: discord.Member, team_name: str):
        super().__init__(timeout=300)
        self.user = user
        self.team_name = team_name
        self.message = None

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.administrator
                or any(r.name == COMMISSIONER_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message("❌ Only commissioners can approve.", ephemeral=True)
            return
        uid = str(self.user.id)
        # Check if team is already claimed by someone else
        for existing_uid, existing_team in _team_owners.items():
            if existing_team == self.team_name and existing_uid != uid:
                await interaction.response.send_message(
                    f"❌ **{self.team_name}** is already claimed by <@{existing_uid}>.", ephemeral=True)
                return
        _team_owners[uid] = self.team_name
        _save_owners(_team_owners)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"✅ **{self.user.display_name}** now owns **{self.team_name}**.")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.administrator
                or any(r.name == COMMISSIONER_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message("❌ Only commissioners can deny.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"❌ Claim denied for **{self.user.display_name}**.")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


@bot.command(name='claimteam')
async def cmd_claimteam(ctx, *, emoji_input: str = ""):
    """!claimteam <:emoji:> — Request ownership of a team. Commissioner must approve."""
    if not emoji_input.strip():
        await ctx.send("❌ Provide your team emoji. Usage: `!claimteam <:YourEmoji:>`")
        return

    emoji_match = _CUSTOM_EMOJI_RE.search(emoji_input)
    if emoji_match:
        emoji_name = emoji_match.group(1)
    else:
        await ctx.send("❌ Please use a custom server emoji, not text or Unicode.")
        return

    team_name = EMOJI_TEAM_MAP.get(emoji_name)
    if not team_name:
        await ctx.send(f"❌ Unrecognised emoji **:{emoji_name}:**. Add it to `emoji_map.py` first.")
        return

    uid = str(ctx.author.id)

    # Already owns this team?
    if _team_owners.get(uid) == team_name:
        await ctx.send(f"You already own **{team_name}**.")
        return

    # Team already claimed by someone else?
    for existing_uid, existing_team in _team_owners.items():
        if existing_team == team_name and existing_uid != uid:
            await ctx.send(f"❌ **{team_name}** is already claimed by <@{existing_uid}>.")
            return

    view = ClaimApprovalView(ctx.author, team_name)
    view.message = await ctx.send(
        f"🏀 **{ctx.author.display_name}** wants to claim **{team_name}**.\n"
        f"A commissioner must approve or deny.",
        view=view,
    )


@bot.command(name='myteam')
async def cmd_myteam(ctx):
    """!myteam — Show which team you own."""
    uid = str(ctx.author.id)
    team = _team_owners.get(uid)
    if team:
        await ctx.send(f"You own **{team}**.")
    else:
        await ctx.send("You haven't claimed a team yet. Use `!claimteam <:YourEmoji:>`.")


@bot.command(name='assignteam')
async def cmd_assignteam(ctx, *, args: str = ""):
    """Assign teams to users. Commissioner only.
    !assignteam @user <:emoji:>
    !assignteam @user1 <:emoji1:> @user2 <:emoji2:> ...
    Or multiline:
    !assignteam
    @user1 <:emoji1:>
    @user2 <:emoji2:>
    """
    if not args.strip():
        await ctx.send(
            "❌ Usage:\n"
            "```\n!assignteam <:emoji:> @user1 @user2\n!assignteam @user1 <:emoji1:> @user2 <:emoji2:>\n```"
        )
        return

    assigned = []
    errors = []

    # Format 1: <:emoji:> @user1 @user2 — one team, multiple GMs
    emoji_first = _CUSTOM_EMOJI_RE.match(args.strip())
    if emoji_first:
        emoji_name = emoji_first.group(1)
        team_name = EMOJI_TEAM_MAP.get(emoji_name)
        if not team_name:
            await ctx.send(f"❌ Unknown emoji **:{emoji_name}:**. Add it to `emoji_map.py` first.")
            return
        mentions = re.findall(r'<@!?(\d+)>', args)
        if not mentions:
            await ctx.send("❌ Mention at least one user after the emoji.")
            return
        for uid_str in mentions:
            _team_owners[uid_str] = team_name
            assigned.append(f"<@{uid_str}> → **{team_name}**")
    else:
        # Format 2: @user1 <:emoji1:> @user2 <:emoji2:> — different teams
        pairs = re.findall(r'<@!?(\d+)>\s*<a?:([\w~]+):\d+>', args)
        if not pairs:
            await ctx.send("❌ Could not parse. Use `!assignteam <:emoji:> @user1 @user2` or `!assignteam @user1 <:emoji1:> @user2 <:emoji2:>`")
            return
        for uid_str, emoji_name in pairs:
            team_name = EMOJI_TEAM_MAP.get(emoji_name)
            if not team_name:
                errors.append(f":{emoji_name}: — unknown emoji")
                continue
            _team_owners[uid_str] = team_name
            assigned.append(f"<@{uid_str}> → **{team_name}**")

    if assigned:
        _save_owners(_team_owners)

    msg = ""
    if assigned:
        msg += f"✅ Assigned {len(assigned)} teams:\n" + "\n".join(assigned)
    if errors:
        msg += f"\n\n❌ Errors:\n" + "\n".join(errors)
    await ctx.send(msg)


@bot.command(name='removeteam')
async def cmd_removeteam(ctx, *, target: str):
    """!removeteam @user — Remove a user's team ownership (commissioner only)."""
    member = ctx.message.mentions[0] if ctx.message.mentions else None
    if not member:
        try:
            member = await ctx.guild.fetch_member(int(target.strip("<@!>")))
        except (ValueError, discord.NotFound, discord.HTTPException):
            pass
    if not member:
        await ctx.send("❌ Could not find that user. Use an @mention or user ID.")
        return

    uid = str(member.id)
    team = _team_owners.pop(uid, None)
    if not team:
        await ctx.send(f"❌ **{member.display_name}** doesn't own any team.")
        return

    _save_owners(_team_owners)
    await ctx.send(f"✅ Removed **{member.display_name}**'s ownership of **{team}**.")


def _find_roster_slot(query: str, slots: dict):
    """Find a player in roster slots by exact match, then partial/substring.
    Returns (slot_dict, error_str). One will be None."""
    query_lower = query.lower().strip()
    # Exact match
    for s in slots.values():
        if s["name"].lower() == query_lower:
            return s, None
    # First or last name match
    matches = []
    for s in slots.values():
        if not s["name"]:
            continue
        name_lower = s["name"].lower()
        name_parts = name_lower.split()
        if query_lower in name_parts or query_lower == name_lower:
            matches.append(s)
    if len(matches) == 1:
        return matches[0], None
    # Substring match
    if not matches:
        for s in slots.values():
            if s["name"] and query_lower in s["name"].lower():
                matches.append(s)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        names = ", ".join(f"**{s['name']}**" for s in matches)
        return None, f"Multiple matches for **{query}**: {names}. Be more specific."
    return None, f"**{query}** is not on the roster."


@bot.command(name='swap')
async def cmd_swap(ctx, *, args: str):
    """!swap Player A / Player B  OR  !swap Player A / 9  (slot number 1-10)"""
    uid = str(ctx.author.id)
    team_name = _team_owners.get(uid)
    if not team_name:
        await ctx.send("❌ You haven't claimed a team. Use `!claimteam <:YourEmoji:>` first.")
        return

    if '/' not in args:
        await ctx.send(
            "❌ Usage:\n"
            "`!swap Player A / Player B` — swap two players\n"
            "`!swap Player A / 9` — move player to slot 9 (Bench PF)\n"
            "Slots: 1-PG 2-SG 3-SF 4-PF 5-C 6-BPG 7-BSG 8-BSF 9-BPF 10-BC"
        )
        return

    parts = args.split('/', 1)
    player_a = parts[0].strip().title()
    target_b = parts[1].strip()

    if not player_a or not target_b:
        await ctx.send("❌ Please provide both sides. Usage: `!swap Player A / Player B` or `!swap Player A / 9`")
        return

    # Check if right side is a slot number (1-10)
    slot_number = None
    if target_b.isdigit() and 1 <= int(target_b) <= 10:
        slot_number = int(target_b)
        player_b = None
    else:
        player_b = target_b.title()

    # Find the sheet manager
    manager = _get_manager(ctx.channel.id)
    if not manager:
        for ch_id in _channel_sheet_map:
            manager = _get_manager(ch_id)
            if manager:
                break
    if not manager:
        await ctx.send("❌ No sheet mappings configured.")
        return

    try:
        async with ctx.channel.typing():
            all_data = manager._call('get_all_values')

            # Find the team header
            team_row, team_col = manager._find_team_cell(team_name, all_data)
            if not team_row:
                await ctx.send(f"❌ Team **{team_name}** not found in the sheet.")
                return

            col_idx = team_col - 1  # 0-indexed

            # Read all 10 roster slots
            slots = {}
            for offset in range(1, 11):
                r = (team_row - 1) + offset
                if r < len(all_data):
                    row_data = all_data[r]
                    cell_name = row_data[col_idx].strip() if col_idx < len(row_data) else ""
                    cell_year = row_data[col_idx + 1].strip() if (col_idx + 1) < len(row_data) else ""
                    cell_price = row_data[col_idx + 2].strip() if (col_idx + 2) < len(row_data) else ""
                else:
                    cell_name = cell_year = cell_price = ""
                slots[offset] = {"row": team_row + offset, "name": cell_name, "year": cell_year, "price": cell_price, "offset": offset}

            # Find player A
            slot_a, err = _find_roster_slot(player_a, slots)
            if not slot_a:
                await ctx.send(f"❌ {err}")
                return

            # Find slot B — either by player name or slot number
            if slot_number:
                slot_b = slots[slot_number]
            else:
                slot_b, err = _find_roster_slot(player_b, slots)
                if not slot_b:
                    await ctx.send(f"❌ {err}")
                    return

            # Build batch update: write A's data to B's row and vice versa
            updates = [
                {"range": gspread.utils.rowcol_to_a1(slot_a["row"], team_col), "values": [[slot_b["name"]]]},
                {"range": gspread.utils.rowcol_to_a1(slot_a["row"], team_col + 1), "values": [[slot_b["year"]]]},
                {"range": gspread.utils.rowcol_to_a1(slot_a["row"], team_col + 2), "values": [[slot_b["price"]]]},
                {"range": gspread.utils.rowcol_to_a1(slot_b["row"], team_col), "values": [[slot_a["name"]]]},
                {"range": gspread.utils.rowcol_to_a1(slot_b["row"], team_col + 1), "values": [[slot_a["year"]]]},
                {"range": gspread.utils.rowcol_to_a1(slot_b["row"], team_col + 2), "values": [[slot_a["price"]]]},
            ]
            manager._call('batch_update', updates)

    except Exception as exc:
        await ctx.send(f"❌ Sheet error: {exc}")
        return

    # Position labels for display
    pos_labels = ["PG", "SG", "SF", "PF", "C"]
    def _slot_label(offset):
        if 1 <= offset <= 5:
            return f"Starter {pos_labels[offset - 1]}"
        elif 6 <= offset <= 10:
            return f"Bench {pos_labels[offset - 6]}"
        return f"Row +{offset}"

    label_a = _slot_label(slot_a["offset"])
    label_b = _slot_label(slot_b["offset"])

    embed = discord.Embed(
        title=f"🔄 Swap — {team_name}",
        description=(
            f"**{slot_a['name']}** ({label_a}) ↔ **{slot_b['name']}** ({label_b})"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"Swapped by {ctx.author.display_name}")
    await ctx.send(embed=embed)


@bot.command(name='addyear')
async def cmd_addyear(ctx, *, args: str):
    """!addyear Player Name '06 — Add or update a player's year on your team."""
    uid = str(ctx.author.id)
    team_name = _team_owners.get(uid)
    if not team_name:
        await ctx.send("❌ You haven't claimed a team. Use `!claimteam <:YourEmoji:>` first.")
        return

    # Split: everything except the last token is the player name, last token is the year
    parts = args.rsplit(None, 1)
    if len(parts) < 2:
        await ctx.send("❌ Usage: `!addyear Player Name '06` or `!addyear Player Name 2005-06`")
        return

    player_name = parts[0].strip().title()
    year = _normalize_year(parts[1].strip())

    manager = _get_manager(ctx.channel.id)
    if not manager:
        for ch_id in _channel_sheet_map:
            manager = _get_manager(ch_id)
            if manager:
                break
    if not manager:
        await ctx.send("❌ No sheet mappings configured.")
        return

    try:
        async with ctx.channel.typing():
            all_data = manager._call('get_all_values')
            team_row, team_col = manager._find_team_cell(team_name, all_data)
            if not team_row:
                await ctx.send(f"❌ Team **{team_name}** not found in the sheet.")
                return

            col_idx = team_col - 1
            slots = {}
            for offset in range(1, 11):
                r = (team_row - 1) + offset
                if r < len(all_data):
                    row_data = all_data[r]
                    cell_name = row_data[col_idx].strip() if col_idx < len(row_data) else ""
                else:
                    cell_name = ""
                slots[offset] = {"name": cell_name, "offset": offset}

            slot, err = _find_roster_slot(player_name, slots)
            if not slot:
                await ctx.send(f"❌ {err}")
                return

            player_name = slot["name"]
            year_col = team_col + 1
            manager._call('batch_update', [{
                'range': gspread.utils.rowcol_to_a1(team_row + slot["offset"], year_col),
                'values': [[year]],
            }])

    except Exception as exc:
        await ctx.send(f"❌ Sheet error: {exc}")
        return

    await ctx.send(f"✅ **{player_name}** — year set to **{year}** on **{team_name}**.")


_PLAYER_ADP = {
    "Michael Jordan": 1.20, "LeBron James": 1.80, "Shaquille O'Neal": 3.70,
    "Stephen Curry": 4.40, "Kevin Garnett": 4.40, "Larry Bird": 6.20,
    "Magic Johnson": 7.00, "Kareem Abdul-Jabbar": 8.30, "Hakeem Olajuwon": 9.30,
    "Kevin Durant": 9.70, "Kobe Bryant": 11.80, "Jerry West": 12.10,
    "Tim Duncan": 12.10, "Nikola Jokic": 15.30, "Kawhi Leonard": 15.70,
    "Shai Gilgeous-Alexander": 15.90, "Dwyane Wade": 16.10, "David Robinson": 19.00,
    "Steve Nash": 19.00, "Chris Paul": 19.00, "Tracy McGrady": 20.90,
    "Oscar Robertson": 21.70, "Bill Walton": 23.50, "Giannis Antetokounmpo": 24.60,
    "Wilt Chamberlain": 24.70, "Anthony Davis": 25.90, "Joel Embiid": 27.20,
    "Julius Erving": 27.70, "Bill Russell": 29.80, "James Harden": 31.40,
    "Karl Malone": 32.20, "Dirk Nowitzki": 32.70, "Grant Hill": 32.70,
    "Jayson Tatum": 33.80, "Luka Doncic": 33.80, "Scottie Pippen": 35.70,
    "Reggie Miller": 36.60, "Draymond Green": 39.00, "Penny Hardaway": 39.60,
    "Charles Barkley": 39.80, "Paul George": 40.00, "Manu Ginobili": 42.20,
    "Walt Frazier": 44.50, "Clyde Drexler": 44.60, "Bob McAdoo": 45.40,
    "Jimmy Butler": 45.70, "Victor Wembanyama": 47.10, "Ray Allen": 47.30,
    "Paul Pierce": 49.50, "Dwight Howard": 51.10, "Damian Lillard": 52.30,
    "Mark Price": 52.80, "John Havlicek": 54.00, "Rick Barry": 54.30,
    "Chris Mullin": 54.90, "Sidney Moncrief": 55.80, "Vince Carter": 56.70,
    "Moses Malone": 58.90, "Willis Reed": 61.00, "Chauncey Billups": 61.90,
    "Patrick Ewing": 61.90, "Jason Kidd": 61.90, "Anthony Edwards": 62.10,
    "Shawn Kemp": 62.80, "Russell Westbrook": 64.80, "Klay Thompson": 65.50,
    "Terry Porter": 69.20, "Marques Johnson": 70.20, "Rasheed Wallace": 71.10,
    "Brandon Roy": 71.10, "Devin Booker": 71.10, "Rashard Lewis": 71.70,
    "Eddie Jones": 73.40, "Tyrese Haliburton": 73.60, "Deron Williams": 73.90,
    "Kyle Lowry": 75.40, "Larry Nance Sr.": 76.30, "John Stockton": 79.90,
    "Evan Mobley": 80.30, "Gary Payton": 80.90, "Peja Stojakovic": 81.30,
    "George Gervin": 81.80, "Shawn Marion": 82.00, "Andrei Kirilenko": 84.30,
    "Bobby Jones": 85.60, "Marc Gasol": 86.00, "Kevin Johnson": 87.10,
    "Joe Dumars": 88.40, "Alonzo Mourning": 88.80, "Dave Cowens": 89.40,
    "Pau Gasol": 90.30, "Victor Oladipo": 91.10, "James Worthy": 91.90,
    "Isiah Thomas": 94.80, "Jalen Williams": 96.60, "Jaylen Brown": 97.00,
    "Kyrie Irving": 97.80, "Mitch Richmond": 98.10, "Andre Iguodala": 99.40,
    "Khris Middleton": 99.40, "Mike Conley": 101.00, "Bob Lanier": 101.30,
    "Dan Majerle": 102.30, "Al Horford": 106.20, "Chris Bosh": 106.90,
    "Pascal Siakam": 108.10, "Jrue Holiday": 108.20, "Alex English": 109.90,
    "Bam Adebayo": 110.80, "Chet Holmgren": 111.10, "Rudy Gobert": 113.30,
    "Derrick Rose": 113.50, "Jaren Jackson Jr.": 116.30, "Derek Harper": 117.00,
    "Bradley Beal": 117.90, "Paul Millsap": 120.30, "Michael Cooper": 120.40,
    "Donovan Mitchell": 121.20, "Allen Iverson": 121.30, "Karl-Anthony Towns": 121.40,
    "Jalen Brunson": 122.00, "Mikal Bridges": 122.10, "Elgin Baylor": 122.80,
    "Shane Battier": 125.20, "Doug Christie": 125.40, "Jeff Hornacek": 126.50,
    "Derrick White": 127.00, "Zion Williamson": 127.40, "Danny Granger": 131.30,
    "Horace Grant": 133.50, "Bob Dandridge": 134.30, "Danny Green": 134.90,
    "Jack Sikma": 134.90, "Paul Pressey": 135.30, "Gordon Hayward": 135.30,
    "Lauri Markkanen": 135.70, "Lamar Odom": 136.30, "Chris Webber": 136.70,
    "Hersey Hawkins": 142.80, "Sam Jones": 143.10, "Lou Hudson": 144.00,
    "Desmond Bane": 144.30, "Kevin Love": 145.10, "David Thompson": 145.30,
    "Artis Gilmore": 145.50, "Michael Finley": 147.50, "Blake Griffin": 147.80,
    "Dennis Rodman": 151.40, "Baron Davis": 151.50, "Gilbert Arenas": 152.10,
    "Kevin McHale": 152.70, "Chet Walker": 153.70, "Joe Johnson": 154.40,
    "Kiki VanDeWeghe": 154.90, "OG Anunoby": 156.50, "Tim Hardaway Sr.": 157.10,
    "Dominique Wilkins": 157.60, "Cade Cunningham": 157.80, "Ron Artest": 158.00,
    "Darius Garland": 158.10, "Richard Hamilton": 162.20, "Connie Hawkins": 163.00,
    "Walter Davis": 165.30, "Ja Morant": 166.30, "Hedo Turkoglu": 169.90,
    "Joakim Noah": 171.00, "Tayshaun Prince": 171.40, "Reggie Lewis": 172.10,
    "Bernard King": 172.20, "Latrell Sprewell": 172.40, "Ron Harper": 172.70,
    "Steve Smith": 174.40, "Robert Horry": 176.80, "David West": 178.00,
    "Kyle Korver": 178.30, "Dennis Johnson": 179.10, "Gus Johnson": 180.20,
    "Luol Deng": 180.50, "Nicolas Batum": 180.60, "Goran Dragic": 181.30,
    "Alvan Adams": 181.30, "Jamal Murray": 181.70, "Terrell Brandon": 183.10,
    "Michael Redd": 183.70, "Wesley Matthews": 183.80, "Billy Cunningham": 184.50,
    "Sam Perkins": 185.40, "Tyson Chandler": 185.90, "Elton Brand": 190.60,
    "Gus Williams": 190.70, "Paul Westphal": 191.20, "Antonio McDyess": 192.80,
    "George Hill": 193.40, "Carmelo Anthony": 195.50, "Amar'e Stoudemire": 196.30,
    "Ben Wallace": 197.00, "Ivica Zubac": 197.50, "Boris Diaw": 198.60,
    "John Wall": 199.00, "Dikembe Mutombo": 199.30, "Robert Covington": 199.80,
    "Dick Van Arsdale": 200.20, "Hal Greer": 201.70, "Bob Pettit": 201.90,
    "Glen Rice": 205.30, "Trae Young": 206.10, "Bobby Phills": 206.80,
    "Tony Parker": 206.90, "Jamaal Wilkes": 210.60, "Nate Thurmond": 211.80,
    "Tyrese Maxey": 213.10, "Josh Howard": 213.90, "Scott Wedman": 214.30,
    "Josh Smith": 215.20, "Andrew Bogut": 215.30, "Lonzo Ball": 215.80,
    "Andrew Wiggins": 217.40, "Robert Parish": 218.60, "Kemba Walker": 220.00,
    "Sam Cassell": 220.90, "Franz Wagner": 220.90, "Clifford Robinson": 221.40,
    "Sleepy Floyd": 223.00, "Arvydas Sabonis": 223.40, "Jermaine O'Neal": 224.40,
    "Kirk Hinrich": 224.80, "Kristaps Porzingis": 225.30, "Brad Daugherty": 225.50,
    "Gerald Wallace": 225.80, "Jarrett Allen": 228.30, "Aaron Gordon": 228.60,
    "Fat Lever": 228.90, "Maurice Cheeks": 231.20, "Brent Barry": 232.00,
    "Sam Lacey": 232.80, "Dale Ellis": 233.20, "Detlef Schrempf": 234.20,
    "Isaiah Thomas": 235.10, "Jason Terry": 236.90, "Jamal Mashburn": 237.80,
    "Tiny Archibald": 238.40, "Byron Scott": 238.70, "Fred VanVleet": 238.80,
    "De'Aaron Fox": 244.40, "Dave DeBusschere": 244.40, "Danny Ainge": 247.20,
    "Rolando Blackman": 247.50, "Brook Lopez": 248.90, "Vlade Divac": 250.40,
    "Zach LaVine": 250.60, "LaMelo Ball": 251.70, "Alex Caruso": 252.89,
    "Otto Porter Jr.": 253.00, "Brian Taylor": 255.90, "Toni Kukoc": 256.70,
    "Yao Ming": 259.70, "Bob Love": 261.30, "Isaiah Hartenstein": 261.90,
    "Trevor Ariza": 262.10, "Elvin Hayes": 263.10, "Amen Thompson": 266.50,
    "Brandon Ingram": 266.80, "Steve Francis": 268.10, "Roger Brown": 268.30,
    "Wes Unseld": 269.40, "Marcus Smart": 270.00, "Wesley Person": 270.80,
    "Brad Miller": 270.90, "Michael Porter Jr.": 273.20, "Nene Hilario": 273.60,
    "Caron Butler": 275.60, "Jon McGlocklin": 278.10, "Phil Smith": 278.70,
    "Fred Brown": 279.00, "Derrick Coleman": 280.10, "Anthony Mason": 280.40,
    "Dan Roundfield": 281.40, "Gary Harris": 282.70, "Robert Williams": 282.90,
    "Richard Jefferson": 283.20, "Malcolm Brogdon": 283.70, "PJ Brown": 284.80,
    "Mookie Blaylock": 285.80, "James Posey": 286.40, "Bill Laimbeer": 288.10,
    "Phil Chenier": 288.10, "Mike Miller": 288.20, "Mike Bibby": 290.40,
    "Marcus Camby": 291.00, "Allan Houston": 291.60, "Ralph Sampson": 291.60,
    "Rod Strickland": 292.00, "Kerry Kittles": 293.00, "Julius Randle": 295.60,
    "Jerome Kersey": 296.10, "Jason Richardson": 296.10, "Jaden McDaniels": 297.90,
    "Bo Outlaw": 298.60, "Scottie Barnes": 298.70, "Drazen Petrovic": 298.80,
    "Larry Johnson": 299.00, "Kenny Anderson": 299.20, "Larry Kenon": 299.20,
    "Herb Jones": 299.50, "Joe Ingles": 299.90, "Jalen Rose": 300.10,
    "Greg Ballard": 300.20, "Jerry Sloan": 300.40, "Domantas Sabonis": 301.20,
    "Tom Chambers": 301.60, "Buck Williams": 302.00, "Ben Simmons": 302.00,
    "LaMarcus Aldridge": 302.30, "Danilo Gallinari": 302.70, "Randy Smith": 303.00,
    "Theo Ratliff": 303.70, "Lu Dort": 304.40, "Wally Szczerbiak": 304.50,
    "DeAndre Jordan": 304.60, "Raja Bell": 304.70, "DeMar DeRozan": 304.70,
    "Doug Collins": 304.70, "JJ Redick": 304.80, "Lenny Wilkens": 304.80,
    "Willie Wise": 305.00, "Deandre Ayton": 305.00, "DeMarre Carroll": 305.00,
    "Carlos Boozer": 305.10, "Micheal Williams": 305.20, "Pete Maravich": 305.30,
    "CJ McCollum": 305.40, "Myles Turner": 305.40, "Zelmo Beaty": 305.50,
    "Micheal Ray Richardson": 305.60, "Tom Boerwinkle": 305.70,
    "Derrick McKey": 305.80, "JoJo White": 305.80, "Sean Elliott": 305.90,
    "Calvin Natt": 305.90, "Rodney McCray": 306.00, "Chuck Person": 306.00,
    "Dave Bing": 306.00, "Louie Dampier": 306.10, "Jerry Lucas": 306.10,
    "Paul Arizin": 306.10, "Nick Anderson": 306.20, "Norman Powell": 306.30,
    "Andrew Toney": 306.30, "Rudy Tomjanovich": 306.40, "Andrew Bynum": 306.50,
    "Mickey Johnson": 306.50, "Vin Baker": 306.50, "Mo Williams": 306.60,
    "Marvin Williams": 306.60, "Kentavious Caldwell-Pope": 306.70,
    "Maurice Lucas": 306.70, "Larry Hughes": 306.80, "Johnny Moore": 307.00,
    "Walt Bellamy": 307.00, "John Starks": 307.00, "Jack Twyman": 307.00,
    "Bobby Simmons": 307.10, "Bob Cousy": 307.10, "Doc Rivers": 307.20,
    "Kevin Martin": 307.20, "George McGinnis": 307.20, "Ben Gordon": 307.20,
    "John Collins": 307.30, "Billy Knight": 307.30, "Ty Lawson": 307.30,
    "Lucius Allen": 307.30, "Otis Birdsong": 307.40, "Glenn Robinson": 307.40,
    "Earl Monroe": 307.40, "Darrell Armstrong": 307.50, "Gail Goodrich": 307.50,
    "Jalen Suggs": 307.50, "Brian Winters": 307.50, "Thabo Sefolosha": 307.60,
    "DeMarcus Cousins": 307.60, "Clint Capela": 307.70, "Cedric Maxwell": 307.70,
    "Danny Manning": 307.70, "JR Smith": 307.80, "Archie Clark": 307.80,
    "Mehmet Okur": 307.80, "Tony Allen": 307.90, "Zydrunas Ilgauskas": 307.90,
    "Adrian Dantley": 307.90, "Rik Smits": 307.90, "Jameer Nelson": 308.00,
    "Raef LaFrentz": 308.00, "Serge Ibaka": 308.10, "Nikola Vucevic": 308.10,
    "Xavier McDaniel": 308.10, "Trey Murphy": 308.20, "Mark Aguirre": 308.20,
    "Dan Issel": 308.20, "Bill Bridges": 308.20, "Arron Afflalo": 308.30,
    "Mychal Thompson": 308.30, "World B. Free": 308.40, "Rajon Rondo": 308.50,
    "Kenyon Martin": 308.50, "Dorian Finney-Smith": 308.60,
    "Nicolas Claxton": 308.60, "Mark Eaton": 308.60, "Jae Crowder": 308.70,
    "Clifford Ray": 308.70, "Jim Paxson": 308.70, "Don Buse": 308.80,
    "Terry Cummings": 308.80, "Jay Vincent": 308.80, "Nate McMillan": 308.90,
    "Bruce Bowen": 308.90, "Darryl Dawkins": 308.90, "Tom Gola": 308.90,
    "Andre Miller": 309.00, "Norm Van Lier": 309.00, "Zach Randolph": 309.00,
    "Ricky Pierce": 309.00, "Channing Frye": 309.00, "Bryon Russell": 309.00,
    "Deni Avdija": 309.00, "Jalen Johnson": 309.00, "Austin Reaves": 309.00,
    "Charles Oakley": 309.00, "Duncan Robinson": 309.00, "Richie Guerin": 309.00,
    "Shareef Abdul-Rahim": 309.00, "Kendall Gill": 309.00,
    "Bill Cartwright": 309.00, "Mel Daniels": 309.00, "Paul Silas": 309.00,
    "James Silas": 309.00, "Spencer Haywood": 309.00, "Ricky Rubio": 309.00,
    "Calvin Murphy": 309.00, "Eddie Johnson": 309.00, "Stephon Marbury": 309.00,
    "Antawn Jamison": 309.00, "Paolo Banchero": 309.00, "Alvin Robertson": 309.00,
    "Carl Braun": 309.00, "Maurice Stokes": 309.00, "Quinn Buckner": 309.00,
    "Bill Sharman": 309.00, "Anthony Parker": 309.00,
}

_MATRIX_PLAYER_ORDER = [
    "Michael Jordan", "LeBron James", "Shaquille O'Neal", "Stephen Curry",
    "Kevin Garnett", "Larry Bird", "Magic Johnson", "Kareem Abdul-Jabbar",
    "Hakeem Olajuwon", "Kevin Durant", "Kobe Bryant", "Jerry West",
    "Tim Duncan", "Nikola Jokic", "Kawhi Leonard", "Shai Gilgeous-Alexander",
    "Dwyane Wade", "David Robinson", "Steve Nash", "Chris Paul",
    "Tracy McGrady", "Oscar Robertson", "Bill Walton", "Giannis Antetokounmpo",
    "Wilt Chamberlain", "Anthony Davis", "Joel Embiid", "Julius Erving",
    "Bill Russell", "James Harden", "Karl Malone", "Dirk Nowitzki",
    "Grant Hill", "Jayson Tatum", "Luka Doncic", "Scottie Pippen",
    "Reggie Miller", "Draymond Green", "Penny Hardaway", "Charles Barkley",
    "Paul George", "Manu Ginobili", "Walt Frazier", "Clyde Drexler",
    "Bob McAdoo", "Jimmy Butler", "Victor Wembanyama", "Ray Allen",
    "Paul Pierce", "Dwight Howard", "Damian Lillard", "Mark Price",
    "John Havlicek", "Rick Barry", "Chris Mullin", "Sidney Moncrief",
    "Vince Carter", "Moses Malone", "Willis Reed", "Chauncey Billups",
    "Patrick Ewing", "Jason Kidd", "Anthony Edwards", "Shawn Kemp",
    "Russell Westbrook", "Klay Thompson", "Terry Porter", "Marques Johnson",
    "Rasheed Wallace", "Brandon Roy", "Devin Booker", "Rashard Lewis",
    "Eddie Jones", "Tyrese Haliburton", "Deron Williams", "Kyle Lowry",
    "Larry Nance Sr.", "John Stockton", "Evan Mobley", "Gary Payton",
    "Peja Stojakovic", "George Gervin", "Shawn Marion", "Andrei Kirilenko",
    "Bobby Jones", "Marc Gasol", "Kevin Johnson", "Joe Dumars",
    "Alonzo Mourning", "Dave Cowens", "Pau Gasol", "Victor Oladipo",
    "James Worthy", "Isiah Thomas", "Jalen Williams", "Jaylen Brown",
    "Kyrie Irving", "Mitch Richmond", "Andre Iguodala", "Khris Middleton",
    "Mike Conley", "Bob Lanier", "Dan Majerle", "Al Horford",
    "Chris Bosh", "Pascal Siakam", "Jrue Holiday", "Alex English",
    "Bam Adebayo", "Chet Holmgren", "Rudy Gobert", "Derrick Rose",
    "Jaren Jackson Jr.", "Derek Harper", "Bradley Beal", "Paul Millsap",
    "Michael Cooper", "Donovan Mitchell", "Allen Iverson", "Karl-Anthony Towns",
    "Jalen Brunson", "Mikal Bridges", "Elgin Baylor", "Shane Battier",
    "Doug Christie", "Jeff Hornacek", "Derrick White", "Zion Williamson",
    "Danny Granger", "Horace Grant", "Bob Dandridge", "Danny Green",
    "Jack Sikma", "Paul Pressey", "Gordon Hayward", "Lauri Markkanen",
    "Lamar Odom", "Chris Webber", "Hersey Hawkins", "Sam Jones",
    "Lou Hudson", "Desmond Bane", "Kevin Love", "David Thompson",
    "Artis Gilmore", "Michael Finley", "Blake Griffin", "Dennis Rodman",
    "Baron Davis", "Gilbert Arenas", "Kevin McHale", "Chet Walker",
    "Joe Johnson", "Kiki VanDeWeghe", "OG Anunoby", "Tim Hardaway Sr.",
    "Dominique Wilkins", "Cade Cunningham", "Ron Artest", "Darius Garland",
    "Richard Hamilton", "Connie Hawkins", "Walter Davis", "Ja Morant",
    "Hedo Turkoglu", "Joakim Noah", "Tayshaun Prince", "Reggie Lewis",
    "Bernard King", "Latrell Sprewell", "Ron Harper", "Steve Smith",
    "Robert Horry", "David West", "Kyle Korver", "Dennis Johnson",
    "Gus Johnson", "Luol Deng", "Nicolas Batum", "Goran Dragic",
    "Alvan Adams", "Jamal Murray", "Terrell Brandon", "Michael Redd",
    "Wesley Matthews", "Billy Cunningham", "Sam Perkins", "Tyson Chandler",
    "Elton Brand", "Gus Williams", "Paul Westphal", "Antonio McDyess",
    "George Hill", "Carmelo Anthony", "Amar'e Stoudemire", "Ben Wallace",
    "Ivica Zubac", "Boris Diaw", "John Wall", "Dikembe Mutombo",
    "Robert Covington", "Dick Van Arsdale", "Hal Greer", "Bob Pettit",
    "Glen Rice", "Trae Young", "Bobby Phills", "Tony Parker",
    "Jamaal Wilkes", "Nate Thurmond", "Tyrese Maxey", "Josh Howard",
    "Scott Wedman", "Josh Smith", "Andrew Bogut", "Lonzo Ball",
    "Andrew Wiggins", "Robert Parish", "Kemba Walker", "Sam Cassell",
    "Franz Wagner", "Clifford Robinson", "Sleepy Floyd", "Arvydas Sabonis",
    "Jermaine O'Neal", "Kirk Hinrich", "Kristaps Porzingis", "Brad Daugherty",
    "Gerald Wallace", "Jarrett Allen", "Aaron Gordon", "Fat Lever",
    "Maurice Cheeks", "Brent Barry", "Sam Lacey", "Dale Ellis",
    "Detlef Schrempf", "Isaiah Thomas", "Jason Terry", "Jamal Mashburn",
    "Tiny Archibald", "Byron Scott", "Fred VanVleet", "De'Aaron Fox",
    "Dave DeBusschere", "Danny Ainge", "Rolando Blackman", "Brook Lopez",
    "Vlade Divac", "Zach LaVine", "LaMelo Ball", "Alex Caruso",
    "Otto Porter Jr.", "Brian Taylor", "Toni Kukoc", "Yao Ming",
    "Bob Love", "Isaiah Hartenstein", "Trevor Ariza", "Elvin Hayes",
    "Amen Thompson", "Brandon Ingram", "Steve Francis", "Roger Brown",
    "Wes Unseld", "Marcus Smart", "Wesley Person", "Brad Miller",
    "Michael Porter Jr.", "Nene Hilario", "Caron Butler", "Jon McGlocklin",
    "Phil Smith", "Fred Brown", "Derrick Coleman", "Anthony Mason",
    "Dan Roundfield", "Gary Harris", "Robert Williams", "Richard Jefferson",
    "Malcolm Brogdon", "PJ Brown", "Mookie Blaylock", "James Posey",
    "Bill Laimbeer", "Phil Chenier", "Mike Miller", "Mike Bibby",
    "Marcus Camby", "Allan Houston", "Ralph Sampson", "Rod Strickland",
    "Kerry Kittles", "Julius Randle", "Jerome Kersey", "Jason Richardson",
    "Jaden McDaniels", "Bo Outlaw", "Scottie Barnes", "Drazen Petrovic",
    "Larry Johnson", "Kenny Anderson", "Larry Kenon", "Herb Jones",
    "Joe Ingles", "Jalen Rose", "Greg Ballard", "Jerry Sloan",
    "Domantas Sabonis", "Tom Chambers", "Buck Williams", "Ben Simmons",
    "LaMarcus Aldridge", "Danilo Gallinari", "Randy Smith", "Theo Ratliff",
    "Lu Dort", "Wally Szczerbiak", "DeAndre Jordan", "Raja Bell",
    "DeMar DeRozan", "Doug Collins", "JJ Redick", "Lenny Wilkens",
    "Willie Wise", "Deandre Ayton", "DeMarre Carroll", "Carlos Boozer",
    "Micheal Williams", "Pete Maravich", "CJ McCollum", "Myles Turner",
    "Zelmo Beaty", "Micheal Ray Richardson", "Tom Boerwinkle", "Derrick McKey",
    "JoJo White", "Sean Elliott", "Calvin Natt", "Rodney McCray",
    "Chuck Person", "Dave Bing", "Louie Dampier", "Jerry Lucas",
    "Paul Arizin", "Nick Anderson", "Norman Powell", "Andrew Toney",
    "Rudy Tomjanovich", "Andrew Bynum", "Mickey Johnson", "Vin Baker",
    "Mo Williams", "Marvin Williams", "Kentavious Caldwell-Pope",
    "Maurice Lucas", "Larry Hughes", "Johnny Moore", "Walt Bellamy",
    "John Starks", "Jack Twyman", "Bobby Simmons", "Bob Cousy",
    "Doc Rivers", "Kevin Martin", "George McGinnis", "Ben Gordon",
    "John Collins", "Billy Knight", "Ty Lawson", "Lucius Allen",
    "Otis Birdsong", "Glenn Robinson", "Earl Monroe", "Darrell Armstrong",
    "Gail Goodrich", "Jalen Suggs", "Brian Winters", "Thabo Sefolosha",
    "DeMarcus Cousins", "Clint Capela", "Cedric Maxwell", "Danny Manning",
    "JR Smith", "Archie Clark", "Mehmet Okur", "Tony Allen",
    "Zydrunas Ilgauskas", "Adrian Dantley", "Rik Smits", "Jameer Nelson",
    "Raef LaFrentz", "Serge Ibaka", "Nikola Vucevic", "Xavier McDaniel",
    "Trey Murphy", "Mark Aguirre", "Dan Issel", "Bill Bridges",
    "Arron Afflalo", "Mychal Thompson", "World B. Free", "Rajon Rondo",
    "Kenyon Martin", "Dorian Finney-Smith", "Nicolas Claxton", "Mark Eaton",
    "Jae Crowder", "Clifford Ray", "Jim Paxson", "Don Buse",
    "Terry Cummings", "Jay Vincent", "Nate McMillan", "Bruce Bowen",
    "Darryl Dawkins", "Tom Gola", "Andre Miller", "Norm Van Lier",
    "Zach Randolph", "Ricky Pierce", "Channing Frye", "Bryon Russell",
    "Deni Avdija", "Jalen Johnson", "Austin Reaves", "Charles Oakley",
    "Duncan Robinson", "Richie Guerin", "Shareef Abdul-Rahim", "Kendall Gill",
    "Bill Cartwright", "Mel Daniels", "Paul Silas", "James Silas",
    "Spencer Haywood", "Ricky Rubio", "Calvin Murphy", "Eddie Johnson",
    "Stephon Marbury", "Antawn Jamison", "Paolo Banchero", "Alvin Robertson",
    "Carl Braun", "Maurice Stokes", "Quinn Buckner", "Bill Sharman",
    "Anthony Parker",
]


@bot.command(name='draftmatrix')
async def cmd_draftmatrix(ctx, *, args: str = None):
    """Build a GM × Player pick-count matrix in a Google Sheet.

    Reads GM names from column A (you fill those in).
    Writes all player names across row 1 and fills in pick counts from the audit log.

    Usage:
      !draftmatrix                       — writes to the ATD Draft Matrix sheet
      !draftmatrix <SpreadsheetID or URL> — writes to a specific spreadsheet
    """
    MATRIX_SHEET_ID = "1X6WD8Kp7DEuGQPcLUlOIZhgypnnDu9xOu_c3uqzDPzs"
    sid = MATRIX_SHEET_ID
    if args:
        parsed_sid, _ = _extract_spreadsheet_id(args)
        if parsed_sid:
            sid = parsed_sid

    # Custom ranking order, then any remaining players from PLAYER_POSITIONS alphabetically
    seen = set()
    all_players = []
    for p in _MATRIX_PLAYER_ORDER:
        if p not in seen:
            seen.add(p)
            all_players.append(p)
    for p in sorted(PLAYER_POSITIONS.keys()):
        if p not in seen:
            seen.add(p)
            all_players.append(p)
    audit = _load_audit()

    # Build frequency counts: {gm_name_lower: {player_lower: count}}
    from collections import defaultdict
    counts = defaultdict(lambda: defaultdict(int))
    for entry in audit:
        drafter = entry.get("user_name", "").strip()
        player = entry.get("player", "").strip()
        if drafter and player:
            counts[drafter.lower()][player.lower()] += 1

    tab_name = "Draft Matrix"
    await ctx.send(f"⏳ Updating draft matrix ({len(all_players)} players)…")

    try:
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sid)

        # Find or create the tab
        try:
            ws = spreadsheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=100, cols=len(all_players) + 1)

        # Read GM names from column A (skip row 1 header)
        col_a = ws.col_values(1)
        gm_names = [name.strip() for name in col_a[1:] if name.strip()]

        if not gm_names:
            await ctx.send(
                "❌ No GM names found in column A. "
                "Add GM/drafter names starting from **A2** (A1 is the header), then run this again."
            )
            return

        # Resize sheet if needed for all player columns
        needed_cols = len(all_players) + 1  # +1 for column A
        needed_rows = len(gm_names) + 1     # +1 for header row
        if ws.row_count < needed_rows or ws.col_count < needed_cols:
            ws.resize(rows=max(ws.row_count, needed_rows), cols=max(ws.col_count, needed_cols))

        # Write header: A1 + player names across row 1
        header = ["GM"] + all_players
        ws.update('A1', [header], value_input_option='RAW')

        # Build count rows (B2 onwards — don't overwrite column A)
        data_rows = []
        for gm in gm_names:
            gm_counts = counts.get(gm.lower(), {})
            row = [gm_counts.get(p.lower(), 0) for p in all_players]
            data_rows.append(row)

        ws.update('B2', data_rows, value_input_option='RAW')

    except Exception as exc:
        await ctx.send(f"❌ Failed to write matrix: {exc}")
        return

    picked_count = sum(1 for p in all_players if any(counts[g].get(p.lower(), 0) > 0 for g in counts))
    await ctx.send(
        f"✅ **Draft Matrix** updated — {len(gm_names)} GMs × {len(all_players)} players.\n"
        f"Players with picks so far: **{picked_count}**"
    )


@bot.command(name='teams')
async def cmd_teams(ctx):
    """List all configured emoji → team mappings."""
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


@bot.command(name='find')
async def cmd_find(ctx, *, player_name: str):
    """Search for a player across all configured sheets."""
    managers_to_try = []
    seen_ids = set()
    for ch_id in _channel_sheet_map:
        sheet_id = _channel_sheet_map[ch_id]["sheet_id"]
        tab = _channel_sheet_map[ch_id]["tab"]
        key = (sheet_id, tab)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        m = await _get_manager_async(ch_id)
        if m:
            managers_to_try.append((ch_id, m))

    if not managers_to_try:
        await ctx.send("❌ No sheet mappings configured.")
        return

    found_team = None
    found_tab = None
    loop = asyncio.get_event_loop()
    try:
        async with ctx.channel.typing():
            for ch_id, m in managers_to_try:
                def _search_player(mgr=m):
                    all_data = mgr._call('get_all_values')
                    return mgr._find_existing_player(player_name, all_data)
                team = await loop.run_in_executor(None, _search_player)
                if team:
                    found_team = team
                    found_tab = _channel_sheet_map[ch_id]["tab"]
                    break
    except Exception as exc:
        await ctx.send(f"❌ Sheet error: {exc}")
        return

    if found_team:
        embed = discord.Embed(
            description=f"**{player_name}** is on **{found_team}**",
            color=discord.Color.green(),
        )
        if found_tab:
            embed.set_footer(text=f"Sheet: {found_tab}")
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"**{player_name}** was not found on any team.")


@bot.command(name='available')
async def cmd_available(ctx, *, position: str):
    """Show undrafted players at a position. Usage: !available PG"""
    pos = position.upper().strip()
    if pos not in POSITION_OFFSETS:
        await ctx.send(f"❌ Invalid position. Use one of: PG, SG, SF, PF, C")
        return

    # Get all players that can play this position
    candidates = []
    for name, positions_str in PLAYER_POSITIONS.items():
        player_positions = [p.strip().upper() for p in positions_str.split('/')]
        if pos in player_positions:
            candidates.append(name)

    if not candidates:
        await ctx.send(f"No players found for position **{pos}**.")
        return

    # Use this channel's sheet, or fall back to the first available mapping
    manager = await _get_manager_async(ctx.channel.id)
    tab_name = _channel_sheet_map.get(ctx.channel.id, {}).get("tab", "")
    if not manager:
        for ch_id in _channel_sheet_map:
            manager = await _get_manager_async(ch_id)
            if manager:
                tab_name = _channel_sheet_map[ch_id]["tab"]
                break
    if not manager:
        await ctx.send("❌ No sheet mappings configured.")
        return

    # Find team headers and collect player names from their 10 roster rows
    drafted = set()
    known_teams_lower = {v.lower() for v in EMOJI_TEAM_MAP.values()}
    loop = asyncio.get_event_loop()
    try:
        async with ctx.channel.typing():
            def _fetch_drafted():
                all_data = manager._call('get_all_values')
                result = set()
                for row_idx, row in enumerate(all_data):
                    for col_idx, cell in enumerate(row):
                        if cell.strip().lower() in known_teams_lower:
                            for offset in range(1, 11):
                                r = row_idx + offset
                                if r < len(all_data):
                                    player_row = all_data[r]
                                    name = player_row[col_idx].strip() if col_idx < len(player_row) else ""
                                    if name:
                                        result.add(name)
                return result
            drafted = await loop.run_in_executor(None, _fetch_drafted)
    except Exception as exc:
        await ctx.send(f"❌ Sheet error: {exc}")
        return

    # Match candidates against drafted names (case-insensitive)
    drafted_lower = {n.lower() for n in drafted}
    candidates_lower = {name: name.lower() for name in candidates}

    undrafted = [name for name in candidates if candidates_lower[name] not in drafted_lower]

    if not undrafted:
        await ctx.send(f"All **{pos}** players have been drafted.")
        return

    # Sort by ADP (lower = better)
    undrafted.sort(key=lambda n: _PLAYER_ADP.get(n, 9999))

    # Paginate: 10 players per page
    per_page = 10
    embeds = []
    total_pages = (len(undrafted) + per_page - 1) // per_page
    for page in range(total_pages):
        start = page * per_page
        chunk = undrafted[start:start + per_page]
        lines = []
        for i, name in enumerate(chunk, start=start + 1):
            adp = _PLAYER_ADP.get(name)
            adp_str = f" — ADP {adp}" if adp else ""
            lines.append(f"**{i}.** {name}{adp_str}")
        embed = discord.Embed(
            title=f"Available {pos}s ({len(undrafted)})",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(drafted)} drafted, {len(undrafted)} available")
        embeds.append(embed)

    if len(embeds) == 1:
        await ctx.send(embed=embeds[0])
    else:
        view = HelpView(embeds)
        view.message = await ctx.send(embed=embeds[0], view=view)


@bot.command(name='roster')
async def cmd_roster(ctx, *, team_input: str):
    """View a team's current roster from the sheet. Accepts team name, emoji name, or custom emoji."""
    # Extract emoji name from custom emoji syntax (e.g. <:NW:123456> → "NW")
    emoji_match = _CUSTOM_EMOJI_RE.search(team_input)
    lookup = emoji_match.group(1) if emoji_match else team_input.strip()

    # Resolve: emoji name → exact team name → partial team name → raw
    team_name = EMOJI_TEAM_MAP.get(lookup)
    if not team_name:
        lookup_lower = lookup.lower()
        for ename, tname in EMOJI_TEAM_MAP.items():
            if ename.lower() == lookup_lower:
                team_name = tname
                break
    if not team_name:
        for tname in set(EMOJI_TEAM_MAP.values()):
            if tname.lower() == lookup.lower():
                team_name = tname
                break
    if not team_name:
        for tname in set(EMOJI_TEAM_MAP.values()):
            if lookup.lower() in tname.lower():
                team_name = tname
                break
    if not team_name:
        team_name = lookup

    # Use current channel's manager if available, otherwise search all mappings
    manager = await _get_manager_async(ctx.channel.id)
    managers_to_try = []
    if manager:
        managers_to_try.append(manager)
    for ch_id in _channel_sheet_map:
        if ch_id != ctx.channel.id:
            m = await _get_manager_async(ch_id)
            if m:
                managers_to_try.append(m)

    if not managers_to_try:
        await ctx.send("❌ No sheet mappings configured. Use `!addchannel` or `!setsheet` first.")
        return

    result_name = None
    roster = None
    last_error = None
    loop = asyncio.get_event_loop()
    try:
        async with ctx.channel.typing():
            for m in managers_to_try:
                result_name, roster = await loop.run_in_executor(None, m.get_roster, team_name)
                if result_name is not None:
                    break
                last_error = roster
    except Exception as exc:
        await ctx.send(f"❌ Sheet error: {exc}")
        return

    if result_name is None:
        await ctx.send(f"❌ {last_error}")
        return

    embed = discord.Embed(title=f"\U0001f3c0 {result_name}", color=discord.Color.blue())

    def _format_lines(entries):
        lines = []
        for e in entries:
            player = e["player"] or "—"
            line = f"`{e['position']}` {player}"
            if e["year"]:
                line += f" ({e['year']})"
            if e["price"]:
                p = e["price"]
                line += f" — {p}" if p.startswith("$") else f" — ${p}"
            lines.append(line)
        return "\n".join(lines)

    embed.add_field(name="Starters", value=_format_lines(roster[:5]), inline=False)
    embed.add_field(name="Bench", value=_format_lines(roster[5:]), inline=False)

    filled = sum(1 for e in roster if e["player"])
    embed.set_footer(text=f"{filled}/10 slots filled")
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
            "Undo is **per channel** — each channel has its own undo history.\n"
            "Undo history is saved to disk and survives bot restarts."
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
            "`!roster <team>` — View a team's current roster from the sheet\n"
            "`!find <player>` — Check if a player has been drafted and which team\n"
            "`!draftmatrix` — Build a Drafter × Player pick-count matrix in the sheet\n"
            "`!sheetundo` — Undo the last player addition in this channel\n"
            "`!reload` — Force-reconnect this channel to Google Sheets\n"
            "`!setsheet <Tab Name>` — Map this channel to a worksheet tab\n"
            "`!addchannel #channel <Tab Name>` — Map another channel to a worksheet tab\n"
            "`!removechannel #channel` — Remove a channel's sheet mapping\n"
            "`!channels` — List all channel → sheet mappings"
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
    embed = discord.Embed(
        title="ATD Team Sheet Bot",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Adding a player",
        value=(
            "Post in a configured channel (no command prefix needed):\n"
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
            "`!roster <team>` — View a team's current roster\n"
            "`!find <player>` — Check if a player has been drafted\n"
            "`!draftmatrix` — Build drafter × player matrix in the sheet\n"
            "`!sheetundo` — Undo the last player addition in this channel\n"
            "`!reload` — Reconnect this channel to Google Sheets\n"
            "`!setsheet <Tab Name>` — Map this channel to a worksheet tab\n"
            "`!addchannel #channel <Tab Name>` — Map another channel to a tab\n"
            "`!removechannel #channel` — Remove a channel mapping\n"
            "`!channels` — Show all channel → sheet mappings\n"
            "`!sheetinfo` — This summary\n"
            "`!sheethelp` — Full guide with all details"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not set in .env")
    elif not SPREADSHEET_ID:
        print("❌ SPREADSHEET_ID not set in .env")
    else:
        print("🚀 Starting ATD Team Sheet Bot...")
        bot.run(DISCORD_TOKEN)