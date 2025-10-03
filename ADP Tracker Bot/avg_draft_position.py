# avg_draft_position.py
import os
import re
import json
import logging
from collections import defaultdict
from typing import Optional, Tuple, List, Dict

import discord
from discord import Intents
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ------------------------------------------------------------
# Logging (works well on Render; make sure PYTHONUNBUFFERED=1)
# ------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()],
    force=True,  # override any previous basicConfig
)
log = logging.getLogger("adp")

# ------------------------------------------------------------
# Env & credentials
# ------------------------------------------------------------
load_dotenv()  # local convenience; ignored on Render if no .env

# raw values first so "0" defaults don't mask missing vars
DISCORD_TOKEN_RAW = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID_RAW = os.getenv("DISCORD_CHANNEL_ID")
GOOGLE_SHEET_ID_RAW = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_WORKSHEET_GID_RAW = os.getenv("GOOGLE_WORKSHEET_GID")

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "A").upper()
ADP_COL_LETTER  = os.getenv("ADP_COLUMN",  "B").upper()

# thresholds (tweak in env if needed)
FUZZY_THRESHOLD  = int(os.getenv("FUZZY_THRESHOLD", "85"))
LOW_FUZZY_CUTOFF = int(os.getenv("LOW_FUZZY_CUTOFF", "80"))

# Credentials: either JSON string or a file path
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

missing = []
if not DISCORD_TOKEN_RAW:          missing.append("DISCORD_TOKEN")
if not DISCORD_CHANNEL_ID_RAW:     missing.append("DISCORD_CHANNEL_ID")
if not GOOGLE_SHEET_ID_RAW:        missing.append("GOOGLE_SHEET_ID")
if not GOOGLE_WORKSHEET_GID_RAW:   missing.append("GOOGLE_WORKSHEET_GID")

creds_source = None
if GOOGLE_CREDENTIALS_JSON:
    creds_source = "json"
elif GOOGLE_CREDENTIALS_PATH and os.path.exists(GOOGLE_CREDENTIALS_PATH):
    creds_source = "file"
else:
    missing.append("GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_PATH (file not found)")

if missing:
    msg = "Missing/invalid environment variables:\n  - " + "\n  - ".join(missing)
    log.error(msg)
    raise SystemExit(msg)

# safe conversions
DISCORD_TOKEN = DISCORD_TOKEN_RAW
CHANNEL_ID    = int(DISCORD_CHANNEL_ID_RAW)
SHEET_ID      = GOOGLE_SHEET_ID_RAW
WS_GID        = int(GOOGLE_WORKSHEET_GID_RAW)

# ------------------------------------------------------------
# Google Sheets
# ------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

if creds_source == "json":
    log.info("[CREDS] Using GOOGLE_CREDENTIALS_JSON")
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
else:
    log.info("[CREDS] Using GOOGLE_CREDENTIALS_PATH=%s", GOOGLE_CREDENTIALS_PATH)
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)

gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)
ws = sh.get_worksheet_by_id(WS_GID)

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)
ADP_COL_INDEX  = col_to_index(ADP_COL_LETTER)

# ------------------------------------------------------------
# Normalization & cleaning
# ------------------------------------------------------------
NONLETTER_RE  = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")

# Discord artifacts
CUSTOM_EMOJI_RE        = re.compile(r"<a?:[^:\s>]+:\d+>")      # <:name:123> or <a:name:123>
MENTION_OR_CHANNEL_RE  = re.compile(r"<[@#][!&]?\d+>")         # <@123>, <@!123>, <#123>, <@&123>
LEADING_PICK_RE        = re.compile(r"^\s*\d+\s*[.)-]?\s*")    # "65." | "65 -" | "65)"
URL_RE                 = re.compile(r"https?://\S+")

def normalize_key(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

def clean_message(s: str) -> str:
    # remove leading pick number only for matching
    s = LEADING_PICK_RE.sub("", s)
    s = CUSTOM_EMOJI_RE.sub(" ", s)
    s = MENTION_OR_CHANNEL_RE.sub(" ", s)
    s = URL_RE.sub(" ", s)
    return normalize_key(s)

def extract_typed_pick(s: str) -> Optional[int]:
    """
    Returns the leading integer pick if present (e.g. '65.' -> 65), else None.
    (We read it from the raw text before cleaning.)
    """
    m = re.match(r"^\s*(\d+)\s*[.)-]?", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

# ------------------------------------------------------------
# Load players from the sheet
# ------------------------------------------------------------
def load_players() -> Tuple[List[str], Dict[str, int], List[str], Dict[str, str]]:
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if v:
            names_orig.append(v)
            name_to_row[v] = i
    keys_norm  = [normalize_key(n) for n in names_orig]
    key_to_orig = {normalize_key(n): n for n in names_orig}
    log.info("[INIT] Loaded %d players", len(names_orig))
    return names_orig, name_to_row, keys_norm, key_to_orig

ALL_NAMES, NAME_TO_ROW, ALL_KEYS, KEY_TO_ORIG = load_players()

# ------------------------------------------------------------
# Matching
# ------------------------------------------------------------
def find_best_match(text: str) -> Optional[Tuple[str, int, float]]:
    cleaned = clean_message(text)
    log.info("[MSG] Raw=%r | Cleaned=%r", text, cleaned)

    if not cleaned:
        return None

    # exact after normalization
    if cleaned in KEY_TO_ORIG:
        orig = KEY_TO_ORIG[cleaned]
        row  = NAME_TO_ROW[orig]
        log.info("[MATCH] Exact -> %s (row=%s, score=100)", orig, row)
        return (orig, row, 100.0)

    # strict then gentle
    hits = process.extract(cleaned, ALL_KEYS, scorer=fuzz.token_sort_ratio,
                           score_cutoff=FUZZY_THRESHOLD, limit=1)
    if not hits:
        hits = process.extract(cleaned, ALL_KEYS, scorer=fuzz.token_sort_ratio,
                               score_cutoff=LOW_FUZZY_CUTOFF, limit=1)

    if hits:
        best_key, score, _ = hits[0]
        orig = KEY_TO_ORIG.get(best_key)
        if orig:
            row = NAME_TO_ROW[orig]
            log.info("[MATCH] Fuzzy -> %s (row=%s, score=%.1f)", orig, row, score)
            return (orig, row, score)

    log.info("[MATCH] No match above threshold")
    return None

# ------------------------------------------------------------
# Discord bot
# ------------------------------------------------------------
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Keep history of typed picks to compute averages
pick_history: Dict[str, List[int]] = defaultdict(list)

@client.event
async def on_ready():
    log.info("✅ ADP Tracker Logged in as %s (channel %s)", client.user, CHANNEL_ID)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    raw = (message.content or "").strip()
    if not raw:
        return

    log.info("[DISCORD] %s said: %r", f"{message.author.name}#{message.author.discriminator}", raw)

    # try to parse a typed pick from the raw message
    typed_pick = extract_typed_pick(raw)
    if typed_pick is not None:
        log.info("[PICK] Detected typed pick: %s", typed_pick)

    match = find_best_match(raw)
    if not match:
        try:
            await message.add_reaction("❓")
        except Exception:
            pass
        return

    name, row, _score = match

    # value to record: typed number if present, else fall back to count index
    if typed_pick is None:
        typed_pick = len(pick_history[name]) + 1  # fallback
    pick_history[name].append(typed_pick)

    # compute average
    avg_pick = sum(pick_history[name]) / len(pick_history[name])

    # write to sheet
    try:
        ws.update_cell(row, ADP_COL_INDEX, round(avg_pick, 2))
        log.info("[UPDATE] %s -> wrote avg %.2f to row %d col %d",
                 name, avg_pick, row, ADP_COL_INDEX)
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
    except Exception as e:
        log.exception("[ERROR] Failed to update sheet: %s", e)
        try:
            await message.add_reaction("‼️")
        except Exception:
            pass

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
