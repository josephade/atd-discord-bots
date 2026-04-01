# draft_manager.py
# Manages all state for a single ATD draft session.
# Handles snake order, pick recording, and writing results to Google Sheets.

import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import gspread
import gspread.utils
from oauth2client.service_account import ServiceAccountCredentials

from config import (
    POOL_SPREADSHEET_ID, POOL_TAB_NAME,
    OUTPUT_SPREADSHEET_ID, SERVICE_ACCOUNT_FILE, ROUNDS,
)

# ── Google Sheets scope ──────────────────────────────────────────────────────
_SCOPE = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

# ── Sheet layout (mirrors ATD Team Sheet Bot) ────────────────────────────────
# Each team section occupies 11 rows:
#   +0  Team Name header
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
#
# Teams are arranged side by side; each team group is 4 columns wide:
#   col+0  player names (team name in row 0)
#   col+1  year
#   col+2  price
#   col+3  (gap / separator)

POSITION_ORDER = ['PG', 'SG', 'SF', 'PF', 'C']   # starter then bench


NBA_TEAMS = [
    ("Seattle SuperSonics",     "⚡🟢"),
    ("Vancouver Grizzlies",       "🐻🟢"),
    ("New Orleans Hornets",       "🐝🟢"),
    ("Cincinnati Royals",          "👑🟢"),
    ("Providence Steamrollers",      "🚂🟢"),
    ("New Jersey Nets",              "🏀🟢"),
    ("Atlanta Hawks",           "🦅"),
    ("Boston Celtics",          "🍀"),
    ("Brooklyn Nets",           "🕷️"),
    ("Charlotte Hornets",       "🐝"),
    ("Chicago Bulls",           "🐂"),
    ("Cleveland Cavaliers",     "⚔️"),
    ("Dallas Mavericks",        "🐴"),
    ("Denver Nuggets",          "⛏️"),
    ("Detroit Pistons",         "🔧"),
    ("Golden State Warriors",   "🌉"),
    ("Houston Rockets",         "🚀"),
    ("Indiana Pacers",          "🏎️"),
    ("Los Angeles Clippers",    "⛵"),
    ("Los Angeles Lakers",      "💛"),
    ("Memphis Grizzlies",       "🐻"),
    ("Miami Heat",              "🔥"),
    ("Milwaukee Bucks",         "🦌"),
    ("Minnesota Timberwolves",  "🐺"),
    ("New Orleans Pelicans",    "🦢"),
    ("New York Knicks",         "🗽"),
    ("Oklahoma City Thunder",   "⚡"),
    ("Orlando Magic",           "✨"),
    ("Philadelphia 76ers",      "🔔"),
    ("Phoenix Suns",            "☀️"),
    ("Portland Trail Blazers",  "🌲"),
    ("Sacramento Kings",        "👑"),
    ("San Antonio Spurs",       "⭐"),
    ("Toronto Raptors",         "🦕"),
    ("Utah Jazz",               "🎷"),
    ("Washington Wizards",      "🧙"),
]


class DraftState(Enum):
    IDLE          = "idle"
    SETUP_TEAMS   = "setup_teams"
    SETUP_HUMANS  = "setup_humans"
    SETUP_PLAYERS = "setup_players"
    ACTIVE        = "active"
    COMPLETE      = "complete"


@dataclass
class TeamSlot:
    name:     str
    emoji:    str                    # unicode fallback emoji
    owner_id: Optional[int] = None   # Discord user ID; None = AI
    picks:    list[str] = field(default_factory=list)

    @property
    def is_ai(self) -> bool:
        return self.owner_id is None

    def display(self) -> str:
        tag = "🤖 AI" if self.is_ai else f"<@{self.owner_id}>"
        return f"{self.emoji} **{self.name}** — {tag}"


class DraftManager:
    def __init__(self):
        self.state        = DraftState.IDLE
        self.teams:  list[TeamSlot] = []
        self.total_teams  = 0
        self.human_count  = 0
        self.started_by:  str | None = None   # Discord username of who ran !draft
        self.pick_order:  list[int] = []   # team indices (snake)
        self.current_pick = 0              # index into pick_order
        self.player_pool: list[str]       = []   # all available players
        self.player_adp:  dict[str, float] = {}   # player → ADP (lower = better)
        self.drafted:     set[str]         = set()

    # ── Properties ──────────────────────────────────────────────────────────
    @property
    def total_picks(self) -> int:
        return self.total_teams * ROUNDS

    @property
    def current_team(self) -> TeamSlot:
        return self.teams[self.pick_order[self.current_pick]]

    @property
    def pick_number(self) -> int:
        return self.current_pick + 1

    @property
    def round_number(self) -> int:
        return self.current_pick // self.total_teams + 1

    @property
    def available_players(self) -> list[str]:
        return [p for p in self.player_pool if p not in self.drafted]

    # ── Setup helpers ────────────────────────────────────────────────────────
    def setup(self, total_teams: int, human_ids: list[int],
              human_positions: list[int] | None = None):
        """
        Assign teams, logos, and owners.  Call after collecting all inputs.
        human_ids: Discord user IDs for human players (in draft order).
        human_positions: optional 1-based slot indices for each human ID.
                         If None, positions are randomly shuffled.
        """
        self.total_teams = total_teams
        self.human_count = len(human_ids)

        pool = random.sample(NBA_TEAMS, min(total_teams, len(NBA_TEAMS)))
        # If more teams than NBA teams (unlikely), pad with numbered entries
        while len(pool) < total_teams:
            pool.append((f"Team {len(pool)+1}", "🏀"))

        if human_positions:
            # Place each human at their chosen slot; AI fills the rest
            owners: list = [None] * total_teams
            for uid, pos in zip(human_ids, human_positions):
                owners[pos - 1] = uid  # 1-based → 0-based
        else:
            # Random lottery — shuffle all owners
            owners = human_ids + [None] * (total_teams - len(human_ids))
            random.shuffle(owners)

        self.teams = [
            TeamSlot(name=pool[i][0], emoji=pool[i][1], owner_id=owners[i])
            for i in range(total_teams)
        ]

        # Build snake draft order (rounds 1-10)
        #
        # Standard snake alternates F/B every round (R1→, R2←, R3→ …).
        # The "flip" at R3 and R6 means those rounds go ← again instead of →,
        # shifting the back-to-back to the top pick on R4 and R7.
        #
        # Direction by round (1-indexed):
        #   R1 →  R2 ←  R3 ← (flip)  R4 →  R5 ←  R6 ← (flip)  R7 →  R8 ←  R9 →  R10 ←
        #
        # As a bool list (True = reversed 30→1, False = forward 1→30):
        _REVERSED = [False, True, True, False, True, True, False, True, False, True]

        self.pick_order = []
        for r in range(ROUNDS):
            seq = list(range(total_teams))
            # Extend the last direction if ROUNDS > 10
            rev = _REVERSED[r] if r < len(_REVERSED) else (r % 2 == 1)
            if rev:
                seq.reverse()
            self.pick_order.extend(seq)

        self.current_pick = 0
        self.state = DraftState.ACTIVE

    def load_player_pool(self) -> int:
        """
        Read player names (col B) and ADP (col C) from the pool tab.
        Returns count of players loaded.
        """
        creds  = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, _SCOPE)
        client = gspread.authorize(creds)
        ws     = client.open_by_key(POOL_SPREADSHEET_ID).worksheet(POOL_TAB_NAME)
        rows   = ws.get_all_values()   # full grid — small enough to fetch at once

        names: list[str]        = []
        adp:   dict[str, float] = {}

        _SKIP = {'player', 'player name', 'name', 'players', 'adp', ''}

        for row in rows:
            # col B = index 1 (player name), col C = index 2 (ADP)
            player_val = row[1].strip() if len(row) > 1 else ''
            adp_val    = row[2].strip() if len(row) > 2 else ''

            if not player_val or player_val.lower() in _SKIP:
                continue
            if re.match(r'^\d+$', player_val):
                continue   # skip pure numbers

            names.append(player_val)

            # Parse ADP if present and numeric
            try:
                adp[player_val] = float(adp_val)
            except ValueError:
                pass   # no ADP for this row — fallback handled in ai_drafter

        print(f"[Pool] Loaded {len(names)} players | {len(adp)} with ADP from '{POOL_TAB_NAME}'")
        self.player_pool = names
        self.player_adp  = adp
        return len(names)

    # ── Pick recording ───────────────────────────────────────────────────────
    def record_pick(self, player: str) -> None:
        self.current_team.picks.append(player)
        self.drafted.add(player)
        self.current_pick += 1

    def is_complete(self) -> bool:
        return self.current_pick >= self.total_picks

    # ── Google Sheets output ─────────────────────────────────────────────────
    def write_results(self, tab_label: str = "") -> str:
        """
        Write completed draft to the output spreadsheet.
        Creates a new worksheet tab.  Returns the tab name.
        """
        from datetime import datetime
        tab_name = tab_label or f"Draft {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        creds  = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, _SCOPE)
        client = gspread.authorize(creds)
        ss     = client.open_by_key(OUTPUT_SPREADSHEET_ID)

        # Create (or clear) the worksheet
        try:
            ws = ss.worksheet(tab_name)
            ws.clear()
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=tab_name, rows=50, cols=self.total_teams * 4 + 5)

        # Build cell updates
        updates = []

        # Optional row 1: position labels in column A
        labels = ["", "Starting PG", "Starting SG", "Starting SF", "Starting PF", "Starting C",
                  "Bench PG", "Bench SG", "Bench SF", "Bench PF", "Bench C"]
        for row_offset, label in enumerate(labels):
            updates.append({
                'range': gspread.utils.rowcol_to_a1(row_offset + 1, 1),
                'values': [[label]],
            })

        for team_idx, team in enumerate(self.teams):
            col = team_idx * 4 + 2   # column B, F, J, ... (1-indexed; col A = labels)

            # Header row: team name
            updates.append({
                'range': gspread.utils.rowcol_to_a1(1, col),
                'values': [[team.name]],
            })

            # Build the 10 player slots (5 starters + 5 bench, by position)
            from player_data import get_positions
            slots: dict[tuple[str, str], str] = {}  # (starter/bench, pos) -> player
            unplaced: list[str] = []

            for player in team.picks:
                positions = get_positions(player)
                if not positions:
                    positions = POSITION_ORDER   # fallback: try all positions
                placed = False
                # Prefer an open starter slot across all positions before any bench slot.
                # e.g. Havlicek (SG/SF) with SG starter taken → goes to SF starter, not SG bench.
                for pos in positions:
                    key = ('starter', pos)
                    if key not in slots:
                        slots[key] = player
                        placed = True
                        break
                if not placed:
                    for pos in positions:
                        key = ('bench', pos)
                        if key not in slots:
                            slots[key] = player
                            placed = True
                            break
                if not placed:
                    unplaced.append(player)

            # Spillover: place any remaining players in the first open slot
            for player in unplaced:
                for slot_type in ('starter', 'bench'):
                    for pos in POSITION_ORDER:
                        key = (slot_type, pos)
                        if key not in slots:
                            slots[key] = player
                            break
                    else:
                        continue
                    break

            # Write players in the correct row offsets
            row_map = {
                ('starter', 'PG'): 2,  ('bench', 'PG'): 7,
                ('starter', 'SG'): 3,  ('bench', 'SG'): 8,
                ('starter', 'SF'): 4,  ('bench', 'SF'): 9,
                ('starter', 'PF'): 5,  ('bench', 'PF'): 10,
                ('starter', 'C'):  6,  ('bench', 'C'):  11,
            }
            for key, player in slots.items():
                row = row_map.get(key)
                if row:
                    updates.append({
                        'range': gspread.utils.rowcol_to_a1(row, col),
                        'values': [[player]],
                    })

        # Write all updates in one batch
        ws.batch_update(updates)
        return tab_name
