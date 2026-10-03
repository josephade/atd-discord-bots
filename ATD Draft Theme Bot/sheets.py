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


def ensure_division_tab(spreadsheet_id: str, pool_tab_name: str, division_tab_name: str) -> None:
    """A true duplicate of the master pool tab — values, header styling, the
    ADP heatmap coloring, column widths, everything — named division_tab_name,
    if that tab doesn't already exist. Gives each division its own
    independent blackout sheet instead of every division fighting over the
    master pool tab's coloring. Uses the Sheets API's real duplicate-sheet
    operation (Worksheet.duplicate) rather than copying values into a blank
    sheet, so the result actually looks like the source. No-ops if the tab
    is already there, so it's safe to call every time a division's packs
    are (re)generated without resetting an in-progress division's blackout
    state."""
    client = _client()
    ss = client.open_by_key(spreadsheet_id)
    try:
        ss.worksheet(division_tab_name)
        return
    except gspread.exceptions.WorksheetNotFound:
        pass
    master = ss.worksheet(pool_tab_name)
    master.duplicate(new_sheet_name=division_tab_name)


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


_ROUND_COL_RE = re.compile(r'^round\s+(\d+)$', re.IGNORECASE)


def load_flux_price_history(spreadsheet_id: str, worksheet_gid: int) -> tuple[dict[str, list[float]], int]:
    """Reads ATD Flux Bot's own live price-tracking sheet (a "Player" column,
    a "Value" column for the baseline price, then a new "Round N" column
    each time the commish runs !flux there) and returns every player's
    chronological price history — [Value, Round 2, Round 3, ...], oldest
    first — plus the total number of price columns found.

    That column count doubles as a lapse-detection fingerprint for Nation
    Flux: an activated power's stored count is compared against this live
    count each time it's checked, so the moment the commish runs !flux
    again (count goes up), every currently-active effect reads as expired
    with no separate cleanup step needed."""
    ws = _client().open_by_key(spreadsheet_id).get_worksheet_by_id(worksheet_gid)
    rows = ws.get_all_values()
    if not rows:
        return {}, 0

    header = rows[0]
    if 'Player' not in header:
        return {}, 0
    player_col = header.index('Player')

    price_cols: list[tuple[int, int]] = []  # (chronological order key, column index)
    if 'Value' in header:
        price_cols.append((1, header.index('Value')))
    for i, h in enumerate(header):
        m = _ROUND_COL_RE.match(h.strip())
        if m:
            price_cols.append((int(m.group(1)), i))
    price_cols.sort(key=lambda x: x[0])

    history: dict[str, list[float]] = {}
    for row in rows[1:]:
        if len(row) <= player_col or not row[player_col].strip():
            continue
        name = row[player_col].strip()
        series = []
        for _order, col in price_cols:
            if col >= len(row) or not row[col].strip():
                continue
            try:
                series.append(float(row[col].strip().replace('$', '')))
            except ValueError:
                continue
        if series:
            history[name] = series

    return history, len(price_cols)


# ── Draft results ─────────────────────────────────────────────────────────────
def write_draft_results(spreadsheet_id: str, tab_name: str, teams: dict[str, list],
                         teams_per_row: int = 5) -> str:
    """Write finished, validated rosters to a dedicated tab. Called only once
    every GM's roster has passed validate_roster()/validate_budget(), so this
    is also the reveal moment. Creates the tab (or clears + reuses it) and
    returns its name.

    Each team's value is either a flat list[str] of player names (one
    column, name only), or a list of (player, year, price) tuples — a
    Player/Year/Price 3-column block per team instead, data starting
    directly under the team name (no extra sub-header row). Themes choose
    per-team via Theme.final_roster_rows(); both shapes can appear in the
    same call.

    Teams are laid out as a grid: `teams_per_row` teams across, then
    wrapping into a new band of rows below — e.g. 40 teams at the default
    of 5 becomes an 8-band grid instead of one single 120-column-wide row."""
    client = _client()
    ss = client.open_by_key(spreadsheet_id)

    def _is_detailed(rows: list) -> bool:
        return bool(rows) and isinstance(rows[0], (tuple, list))

    def _total(rows: list) -> float:
        total = 0.0
        for _player, _year, price in rows:
            if isinstance(price, str) and price.startswith('$'):
                try:
                    total += float(price[1:])
                except ValueError:
                    pass
        return total

    team_items = list(teams.items())
    # Column width is per-team-shape (3 cols for a detailed Player/Year/Price
    # block, 1 for a flat name-only column). The grid math below assumes
    # every team in a single reveal uses the same shape, true for every
    # real caller today (Pack Draft always writes detailed rows for all
    # teams at once).
    col_width = 3 if any(_is_detailed(rows) for _, rows in team_items) else 1
    total_cols = teams_per_row * col_width or 1

    num_bands = -(-len(team_items) // teams_per_row) if team_items else 0
    # +1 for the header row — no gap row between bands: each band's last
    # player row is immediately followed by the next band's header row.
    band_height = max((len(rows) for _, rows in team_items), default=0) + 1

    total_rows = num_bands * band_height
    try:
        ws = ss.worksheet(tab_name)
        ws.clear()
        # A tab left over from an earlier reveal (or created some other way)
        # keeps whatever physical size it already had — clear() only wipes
        # cell contents, it doesn't grow the grid. Writing past that old
        # size 400s with "exceeds grid limits" instead of just... having
        # more room. Confirmed live: a 66-row tab from an earlier, smaller
        # reveal blocked this run's 96-row grid (40 teams, 5/row, 8 bands).
        needed_rows = max(total_rows, 50)
        needed_cols = max(total_cols, 1)
        if ws.row_count < needed_rows or ws.col_count < needed_cols:
            ws.resize(rows=max(ws.row_count, needed_rows), cols=max(ws.col_count, needed_cols))
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=max(total_rows, 50), cols=max(total_cols, 1))

    updates = []
    for i, (team_name, rows) in enumerate(team_items):
        band, col_group = divmod(i, teams_per_row)
        base_row = band * band_height + 1
        base_col = col_group * col_width + 1
        detailed = _is_detailed(rows)
        if detailed:
            updates.append({
                'range': f"{gspread.utils.rowcol_to_a1(base_row, base_col)}:{gspread.utils.rowcol_to_a1(base_row, base_col + 2)}",
                'values': [[team_name, 'Year', f"${_total(rows):.0f}"]],
            })
            for row_idx, (player, year, price) in enumerate(rows, start=base_row + 1):
                updates.append({
                    'range': f"{gspread.utils.rowcol_to_a1(row_idx, base_col)}:{gspread.utils.rowcol_to_a1(row_idx, base_col + 2)}",
                    'values': [[player, year, price]],
                })
        else:
            updates.append({
                'range': gspread.utils.rowcol_to_a1(base_row, base_col),
                'values': [[team_name]],
            })
            for row_idx, player in enumerate(rows, start=base_row + 1):
                updates.append({
                    'range': gspread.utils.rowcol_to_a1(row_idx, base_col),
                    'values': [[player]],
                })

    if updates:
        ws.batch_update(updates)
    return tab_name
