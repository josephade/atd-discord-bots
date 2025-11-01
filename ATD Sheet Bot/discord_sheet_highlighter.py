import os
import re
import json
import time
import logging
import asyncio
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

# Multi-channel / multi-tab support
CHANNEL_ID_1 = int(need("DISCORD_CHANNEL_ID_1"))
CHANNEL_ID_2 = int(need("DISCORD_CHANNEL_ID_2"))
SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID_1 = int(need("GOOGLE_WORKSHEET_GID_1"))
WS_GID_2 = int(need("GOOGLE_WORKSHEET_GID_2"))

# Highlight formatting
NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ROW_START_COL = os.getenv("ROW_HILIGHT_START", "A").upper()
ROW_END_COL = os.getenv("ROW_HILIGHT_END", "D").upper()

FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", "88"))
LOW_FUZZY_CUTOFF = int(os.getenv("LOW_FUZZY_CUTOFF", "80"))

# Google auth
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

if not GOOGLE_CREDENTIALS_JSON and not os.path.exists(GOOGLE_CREDENTIALS_PATH):
    raise SystemExit("Missing credentials: set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_PATH")

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

ws_map = {
    CHANNEL_ID_1: sh.get_worksheet_by_id(WS_GID_1),
    CHANNEL_ID_2: sh.get_worksheet_by_id(WS_GID_2),
}

log.info("[GS] Connected to %s with worksheets %s and %s", SHEET_ID, WS_GID_1, WS_GID_2)

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)

# ================== NORMALIZATION ==================
NONLETTER_RE = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")

def normalize_key(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

def normalize_msg(t: str) -> str:
    t = CUSTOM_EMOJI_RE.sub(" ", t)
    return normalize_key(t)

# ================== LOAD NAMES ==================
def load_player_names(ws) -> Tuple[List[str], Dict[str, int], List[str], Dict[str, str]]:
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if v:
            names_orig.append(v)
            name_to_row[v] = i
    keys_norm = [normalize_key(n) for n in names_orig]
    key_to_orig = {normalize_key(n): n for n in names_orig}
    log.info("[INIT] Loaded %d names from '%s'", len(names_orig), ws.title)
    return names_orig, name_to_row, keys_norm, key_to_orig

data_maps = {}
for cid, ws in ws_map.items():
    data_maps[cid] = load_player_names(ws)

# ================== IMPROVED MATCHING ==================
def find_best_match(ws_id: int, text: str) -> Optional[Tuple[str, int, float]]:
    """Smart fuzzy matcher with token order priority and better disambiguation."""
    names_orig, name_to_row, keys_norm, key_to_orig = data_maps[ws_id]
    msg_clean = normalize_msg(text)

    # Strip prices and comments like ($1), (other pick ...)
    msg_clean = re.sub(r"\$?\d+(\.\d+)?", "", msg_clean)
    msg_clean = re.sub(r"\([^)]*\)", "", msg_clean)
    msg_clean = msg_clean.strip()

    log.info("[MSG] Raw=%r | Cleaned=%r", text, msg_clean)
    if not msg_clean:
        return None

    # Skip irrelevant phrases
    skip_triggers = {"skipped", "skip", "pass", "waiting", "round skipped"}
    if any(word in msg_clean for word in skip_triggers):
        log.info("[SKIP] Ignored system message")
        return None

    tokens = msg_clean.split()
    if len(tokens) == 1 and len(tokens[0]) < 4:
        return None  # ignore generic one-word messages

    # Exact direct match
    for orig in names_orig:
        if normalize_key(orig) in msg_clean:
            log.info(f"[DIRECT MATCH] '{msg_clean}' -> {orig}")
            return orig, name_to_row[orig], 100.0

    # Fuzzy search
    hits = process.extract(msg_clean, keys_norm, scorer=fuzz.token_sort_ratio, limit=3)
    if not hits:
        return None

    # Manual re-ranking
    best_key, best_score = None, 0
    for key, score, _ in hits:
        orig = key_to_orig.get(key)
        if not orig:
            continue

        # Boost if tokens appear in same order
        if normalize_key(orig) in msg_clean:
            score += 10
        elif normalize_key(orig).split()[0] == msg_clean.split()[0]:
            score += 5

        if score > best_score:
            best_score, best_key = score, key

    if best_key and best_score >= 88:
        orig = key_to_orig[best_key]
        log.info("[MATCH] '%s' → %s (score=%.1f)", text, orig, best_score)
        return (orig, name_to_row[orig], best_score)

    log.info("[MATCH] No strong enough match found for '%s'", text)
    return None

# ================== HIGHLIGHT ==================
async def highlight_row(ws, row: int):
    rng = f"{ROW_START_COL}{row}:{ROW_END_COL}{row}"
    log.info("[HIGHLIGHT] %s range=%s", ws.title, rng)

    # #4a86e8 background, black text, Roboto Condensed font, size 10
    bg = {"red": 0.29, "green": 0.52, "blue": 0.91}
    fmt = {
        "backgroundColor": bg,
        "textFormat": {
            "foregroundColor": {"red": 0, "green": 0, "blue": 0},
            "fontFamily": "Roboto Condensed",
            "fontSize": 10,
            "bold": False
        }
    }

    await asyncio.to_thread(ws.format, rng, fmt)

# ================== DISCORD BOT ==================
intents = Intents.default()
intents.message_content = True

client = discord.Client(intents=intents, reconnect=True)

@client.event
async def on_ready():
    log.info("✅ Logged in as %s | Watching channels %s and %s", client.user, CHANNEL_ID_1, CHANNEL_ID_2)

@client.event
async def on_resumed():
    log.info("🔄 Discord connection resumed.")

@client.event
async def on_disconnect():
    log.warning("⚠️ Lost connection to Discord gateway.")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    ws = ws_map.get(message.channel.id)
    if not ws:
        return

    content = (message.content or "").strip()
    if not content:
        return

    log.info("[DISCORD] %s (%s): %r", message.author, ws.title, content)

    best = find_best_match(message.channel.id, content)
    if not best:
        return  # no ? reaction spam

    name, row, score = best
    log.info("[MATCH] %s (%s) → row %s (score=%.1f)", name, ws.title, row, score)
    await highlight_row(ws, row)
    try:
        await message.add_reaction("✅")
    except Exception:
        pass

# ================== MAIN ==================
if __name__ == "__main__":
    while True:
        try:
            client.run(DISCORD_TOKEN)
        except Exception as e:
            log.error(f"[DISCORD ERROR] {e}. Reconnecting in 5s...")
            time.sleep(5)
