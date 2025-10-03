import os
import re
import json
import logging
from typing import Dict, List, Tuple, Optional

import discord
from discord import Intents
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("highlighter")

# ================== ENV CONFIG ==================
load_dotenv()

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env: {name}")
    return v

DISCORD_TOKEN = need("DISCORD_TOKEN")
CHANNEL_ID = int(need("DISCORD_CHANNEL_ID"))

SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID = int(need("GOOGLE_WORKSHEET_GID"))

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ROW_START_COL = os.getenv("ROW_HILIGHT_START", "A").upper()
ROW_END_COL   = os.getenv("ROW_HILIGHT_END", "D").upper()

FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", "88"))
LOW_FUZZY_CUTOFF = int(os.getenv("LOW_FUZZY_CUTOFF", "80"))

# Optional: write to a column (e.g., "E")
WRITE_COLUMN = os.getenv("WRITE_COLUMN", "").strip().upper()

# Credentials: JSON or PATH
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

if not GOOGLE_CREDENTIALS_JSON and not os.path.exists(GOOGLE_CREDENTIALS_PATH):
    raise SystemExit("Missing credentials: set GOOGLE_CREDENTIALS_JSON or a valid GOOGLE_CREDENTIALS_PATH")

log.info("[CFG] Channel=%s | Sheet=%s gid=%s | Names=%s | Highlight=%s:%s | WriteColumn=%s",
         CHANNEL_ID, SHEET_ID, WS_GID, NAME_COL_LETTER, ROW_START_COL, ROW_END_COL, WRITE_COLUMN or "(disabled)")

# ================== GOOGLE SHEETS ==================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

if GOOGLE_CREDENTIALS_JSON:
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)

gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)
ws = sh.get_worksheet_by_id(WS_GID)
log.info("[GS] Connected to worksheet id=%s", WS_GID)

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)
ROW_START_IDX = col_to_index(ROW_START_COL)
ROW_END_IDX   = col_to_index(ROW_END_COL)
WRITE_COL_INDEX = col_to_index(WRITE_COLUMN) if WRITE_COLUMN else None

# ================== NORMALIZATION ==================
NONLETTER_RE = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")

def normalize_key(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

def normalize_msg(t: str) -> str:
    t = CUSTOM_EMOJI_RE.sub(" ", t)  # strip <:emoji:123> from message
    return normalize_key(t)

def try_parse_picknum(text: str) -> Optional[int]:
    """Detect a leading number like '66.', '66 -', '66)' etc."""
    m = re.match(r"^\s*(\d+)\s*[).:-]?\s*", text)
    return int(m.group(1)) if m else None

# ================== LOAD NAMES ==================
def load_player_names() -> Tuple[List[str], Dict[str, int], List[str], Dict[str, str]]:
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if v:
            names_orig.append(v)
            name_to_row[v] = i
    keys_norm = [normalize_key(n) for n in names_orig]
    key_to_orig = {normalize_key(n): n for n in names_orig}
    log.info("[INIT] Loaded %d names", len(names_orig))
    return names_orig, name_to_row, keys_norm, key_to_orig

ALL_NAMES, NAME_TO_ROW, ALL_KEYS, KEY_TO_ORIG = load_player_names()

# ================== MATCHING ==================
def find_best_match(text: str) -> Optional[Tuple[str, int, float]]:
    # remove a leading pick number when matching names
    name_part = re.sub(r"^\s*\d+\s*[).:-]?\s*", "", text)
    q = normalize_msg(name_part)
    log.info("[MSG] Raw=%r | Cleaned=%r", text, q)
    if not q:
        return None

    exact_orig = KEY_TO_ORIG.get(q)
    if exact_orig:
        return (exact_orig, NAME_TO_ROW[exact_orig], 100.0)

    hits = process.extract(q, ALL_KEYS, scorer=fuzz.token_set_ratio, score_cutoff=FUZZY_THRESHOLD, limit=3)
    if not hits:
        hits = process.extract(q, ALL_KEYS, scorer=fuzz.token_set_ratio, score_cutoff=LOW_FUZZY_CUTOFF, limit=3)
        if not hits:
            return None

    best_key, score, _ = hits[0]
    orig = KEY_TO_ORIG.get(best_key)
    return (orig, NAME_TO_ROW[orig], score) if orig else None

# ================== HIGHLIGHT ==================
def highlight_row(row: int):
    rng = f"{ROW_START_COL}{row}:{ROW_END_COL}{row}"
    log.info("[HILIGHT] Range=%s (bg=#2659ea, text=white)", rng)

    # Hex #2659ea -> RGB floats 0–1
    bg = {"red": 38/255, "green": 89/255, "blue": 234/255}

    ws.format(rng, {
        "backgroundColor": bg,
        "textFormat": {
            "foregroundColor": {"red": 1, "green": 1, "blue": 1}
        }
    })

def maybe_write_value(row: int, value: Optional[int]):
    if WRITE_COL_INDEX is None:
        return
    out = value if value is not None else 1  # if no pick typed, write 1 by default
    ws.update_cell(row, WRITE_COL_INDEX, out)
    log.info("[WRITE] Row=%s Col=%s (%s) -> %s", row, WRITE_COL_INDEX, WRITE_COLUMN, out)

# ================== DISCORD BOT ==================
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    log.info("✅ Logged in as %s | Watching channel %s", client.user, CHANNEL_ID)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return

    content = (message.content or "").strip()
    if not content:
        return

    log.info("[DISCORD] %s said: %r", message.author, content)
    typed_pick = try_parse_picknum(content)
    if typed_pick is not None:
        log.info("[PICK] Detected typed pick: %s", typed_pick)

    best = find_best_match(content)
    if not best:
        log.info("[MATCH] No match above threshold")
        try: await message.add_reaction("❓")
        except Exception: pass
        return

    name, row, score = best
    log.info("[MATCH] %s -> row %s (score=%s)", name, row, score)

    # Highlight & optional write
    highlight_row(row)
    maybe_write_value(row, typed_pick)

    try: await message.add_reaction("✅")
    except Exception: pass

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
