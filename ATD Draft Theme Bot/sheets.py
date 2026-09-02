# sheets.py
# Google Sheets I/O — player pool / ADP in, final draft results out.
# Synchronous (gspread); callers run these via run_in_executor so the
# Discord event loop never blocks on a network call.

import re
import time

import gspread
import gspread.utils
from oauth2client.service_account import ServiceAccountCredentials

from config import SERVICE_ACCOUNT_FILE

_SCOPE = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

_POOL_SKIP = {'player', 'player name', 'name', 'players', 'adp', ''}


def _client() -> gspread.Client:
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, _SCOPE)
    return gspread.authorize(creds)


# ── Player pool / ADP ─────────────────────────────────────────────────────────
def load_player_pool(spreadsheet_id: str, tab_name: str) -> tuple[list[str], dict[str, float]]:
    """Read player names (col B) and ADP (col C) from the pool tab, same layout
    ATD Draft Bot's draft_manager.py uses. Returns (names, {name: adp})."""
    ws = _client().open_by_key(spreadsheet_id).worksheet(tab_name)
    rows = ws.get_all_values()

    names: list[str] = []
    adp: dict[str, float] = {}

    for row in rows:
        player_val = row[1].strip() if len(row) > 1 else ''
        adp_val = row[2].strip() if len(row) > 2 else ''

        if not player_val or player_val.lower() in _POOL_SKIP:
            continue
        if re.match(r'^\d+$', player_val):
            continue

        names.append(player_val)
        try:
            adp[player_val] = float(adp_val)
        except ValueError:
            pass

    return names, adp


def _col_to_index(col: str) -> int:
    idx = 0
    for c in col.upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx


def _batch_update_with_retry(spreadsheet: gspread.Spreadsheet, body: dict, retries: int = 4):
    """Mirrors ATD Sheet Bot's backoff — Sheets' write quota is easy to hit
    once a bot is coloring a cell on every single pick."""
    delay = 15
    for attempt in range(retries):
        try:
            spreadsheet.batch_update(body)
            return
        except gspread.exceptions.APIError as e:
            if e.response.status_code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
            else:
                raise


# #4281e8 — note this is a different convention from ATD Draft List Bot's
# own color-check (its _is_taken() only treats near-black, r/g/b all < 0.2,
# as taken), so that bot won't recognize this blue as "taken" if it ever
# reads this same sheet. Not an issue for this engine's own correctness —
# the sqlite DB is always the real source of truth — just worth knowing.
_TAKEN_COLOR = {'red': 0x42 / 255, 'green': 0x81 / 255, 'blue': 0xe8 / 255}


def mark_player_taken(spreadsheet_id: str, tab_name: str, player_name: str,
                       highlight_cols: str = 'A:D') -> bool:
    """Colors a player's row on the pool/ADP sheet to show they're drafted —
    cosmetic only. The sqlite DB stays the actual source of truth regardless
    of whether this succeeds, so callers should treat failures as non-fatal.
    Returns False if the player isn't found in column B."""
    ws = _client().open_by_key(spreadsheet_id).worksheet(tab_name)
    col_b = ws.col_values(2)
    target = player_name.strip().lower()
    row = next((i for i, v in enumerate(col_b, start=1) if v.strip().lower() == target), None)
    if row is None:
        return False

    start_col, end_col = highlight_cols.split(':')
    start_idx = _col_to_index(start_col) - 1
    end_idx = _col_to_index(end_col)

    _batch_update_with_retry(ws.spreadsheet, {
        'requests': [{
            'repeatCell': {
                'range': {
                    'sheetId': ws.id,
                    'startRowIndex': row - 1,
                    'endRowIndex': row,
                    'startColumnIndex': start_idx,
                    'endColumnIndex': end_idx,
                },
                'cell': {'userEnteredFormat': {'backgroundColor': _TAKEN_COLOR}},
                'fields': 'userEnteredFormat.backgroundColor',
            }
        }],
    })
    return True


def load_prices(spreadsheet_id: str, tab_name: str) -> dict[str, float]:
    """Best-effort: read player names (col B) and a $ price (col D) from the
    pool tab, if that column holds numeric data. Returns {} if col D isn't
    priced — callers should treat that as "no budget check available" rather
    than an error, since not every pool sheet has a price column."""
    ws = _client().open_by_key(spreadsheet_id).worksheet(tab_name)
    rows = ws.get_all_values()

    prices: dict[str, float] = {}
    for row in rows:
        player_val = row[1].strip() if len(row) > 1 else ''
        price_val = row[3].strip() if len(row) > 3 else ''
        if not player_val or player_val.lower() in _POOL_SKIP:
            continue
        try:
            prices[player_val] = float(price_val.replace('$', ''))
        except ValueError:
            continue
    return prices


# ── Draft results ─────────────────────────────────────────────────────────────
def write_draft_results(spreadsheet_id: str, tab_name: str, teams: dict[str, list]) -> str:
    """Write finished, validated rosters to a dedicated tab. Called only once
    every GM's roster has passed validate_roster()/validate_budget(), so this
    is also the reveal moment. Creates the tab (or clears + reuses it) and
    returns its name.

    Each team's value is either a flat list[str] of player names (one
    column, name only), or a list of (player, year, price) tuples — a
    Player/Year/Price 3-column block per team instead, data starting
    directly under the team name (no extra sub-header row). Themes choose
    per-team via Theme.final_roster_rows(); both shapes can appear in the
    same call."""
    client = _client()
    ss = client.open_by_key(spreadsheet_id)

    def _is_detailed(rows: list) -> bool:
        return bool(rows) and isinstance(rows[0], (tuple, list))

    total_cols = sum(3 if _is_detailed(rows) else 1 for rows in teams.values()) or 1

    try:
        ws = ss.worksheet(tab_name)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        max_rows = max((len(rows) for rows in teams.values()), default=0) + 1
        ws = ss.add_worksheet(title=tab_name, rows=max(max_rows, 50), cols=max(total_cols, 1))

    def _total(rows: list) -> float:
        total = 0.0
        for _player, _year, price in rows:
            if isinstance(price, str) and price.startswith('$'):
                try:
                    total += float(price[1:])
                except ValueError:
                    pass
        return total

    updates = []
    col_idx = 1
    for team_name, rows in teams.items():
        detailed = _is_detailed(rows)
        if detailed:
            updates.append({
                'range': f"{gspread.utils.rowcol_to_a1(1, col_idx)}:{gspread.utils.rowcol_to_a1(1, col_idx + 2)}",
                'values': [[team_name, 'Year', f"${_total(rows):.0f}"]],
            })
            for row_idx, (player, year, price) in enumerate(rows, start=2):
                updates.append({
                    'range': f"{gspread.utils.rowcol_to_a1(row_idx, col_idx)}:{gspread.utils.rowcol_to_a1(row_idx, col_idx + 2)}",
                    'values': [[player, year, price]],
                })
            col_idx += 3
        else:
            updates.append({
                'range': gspread.utils.rowcol_to_a1(1, col_idx),
                'values': [[team_name]],
            })
            for row_idx, player in enumerate(rows, start=2):
                updates.append({
                    'range': gspread.utils.rowcol_to_a1(row_idx, col_idx),
                    'values': [[player]],
                })
            col_idx += 1

    if updates:
        ws.batch_update(updates)
    return tab_name
