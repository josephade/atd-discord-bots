import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from config import SPREADSHEET_ID, WORKSHEET_NAME, SERVICE_ACCOUNT_FILE

# Mirrors ATD Team Sheet Bot's roster layout: row offsets relative to the
# team header row (+0), 5 starters then 5 bench, one column per team.
ROSTER_SLOTS = [
    ("PG", "Starter", 1), ("SG", "Starter", 2), ("SF", "Starter", 3),
    ("PF", "Starter", 4), ("C",  "Starter", 5),
    ("PG", "Bench",   6), ("SG", "Bench",   7), ("SF", "Bench",   8),
    ("PF", "Bench",   9), ("C",  "Bench",  10),
]

_ws = None


def _worksheet():
    global _ws
    if _ws is None:
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
        client = gspread.authorize(creds)
        client.set_timeout(15)
        _ws = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
    return _ws


def _get_all_values(retries: int = 4):
    """Fetch the whole sheet, retrying transient connection errors with
    backoff and reconnecting between attempts (mirrors ATD Team Sheet Bot's
    SheetManager._call, which handles the same flaky-connection errors)."""
    global _ws
    for attempt in range(1, retries + 1):
        try:
            return _worksheet().get_all_values()
        except Exception as e:
            if attempt == retries:
                raise
            delay = 2 ** attempt  # 2s, 4s, 8s…
            print(f"[Sheets] Error (attempt {attempt}/{retries}): {type(e).__name__}: {e} — reconnecting in {delay}s…")
            time.sleep(delay)
            _ws = None


def _find_team_cell(team_name, data):
    name_lower = team_name.lower().strip()
    for row_idx, row in enumerate(data):
        for col_idx, cell in enumerate(row):
            if cell.strip().lower() == name_lower:
                return row_idx + 1, col_idx + 1
    for row_idx, row in enumerate(data):
        for col_idx, cell in enumerate(row):
            if name_lower in cell.strip().lower():
                return row_idx + 1, col_idx + 1
    return None, None


def get_roster(team_name: str):
    """Return (roster_list, error). roster_list is a list of player-name
    strings (empty slots omitted), in no particular order."""
    all_data = _get_all_values()
    team_row, team_col = _find_team_cell(team_name, all_data)
    if not team_row:
        return None, f"Team **{team_name}** was not found in the sheet."

    col_idx = team_col - 1
    roster = []
    for _pos, _slot, offset in ROSTER_SLOTS:
        r = (team_row - 1) + offset
        if r < len(all_data):
            row_data = all_data[r]
            player = row_data[col_idx].strip() if col_idx < len(row_data) else ""
            if player:
                roster.append(player)

    return roster, None
