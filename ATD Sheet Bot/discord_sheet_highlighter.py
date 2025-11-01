import os
import re
import json
import time
import logging
import asyncio
from typing import Dict, List, Tuple, Optional

import discord
from discord import Intents, MessageType
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("highlighter")

# ================== ENV CONFIG ==================
load_dotenv()

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env: {name}")
    return v

DISCORD_TOKEN = need("DISCORD_TOKEN")
CHANNEL_ID_1 = int(need("DISCORD_CHANNEL_ID_1"))
CHANNEL_ID_2 = int(need("DISCORD_CHANNEL_ID_2"))
SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID_1 = int(need("GOOGLE_WORKSHEET_GID_1"))
WS_GID_2 = int(need("GOOGLE_WORKSHEET_GID_2"))

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ROW_START_COL = os.getenv("ROW_HILIGHT_START", "A").upper()
ROW_END_COL = os.getenv("ROW_HILIGHT_END", "D").upper()

FUZZY_THRESHOLD = 75   # relaxed
LOW_FUZZY_CUTOFF = 65  # extra fallback

# ================== GOOGLE AUTH ==================
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

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

# ================== HELPERS ==================
def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
NONLETTER_RE = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
SMART_QUOTE_RE = re.compile(r"[‘’´`“”]")  # Unicode quotes/apostrophes

def normalize_key(s: str) -> str:
    s = SMART_QUOTE_RE.sub("'", s)
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

def normalize_msg(t: str) -> str:
    t = SMART_QUOTE_RE.sub("'", t)
    t = CUSTOM_EMOJI_RE.sub(" ", t)
    t = re.sub(r"[\u200B-\u200D\uFEFF]", "", t)  # remove zero-width spaces
    t = re.sub(r"\$?\d+(\.\d+)?", "", t)         # remove money/numbers
    t = re.sub(r"\([^)]*\)", "", t)              # remove (...)
    t = NONLETTER_RE.sub(" ", t)
    t = WHITESPACE_RE.sub(" ", t)
    return t.strip().lower()

# ================== LOAD NAMES ==================
def load_player_names(ws):
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row, surnames = [], {}, {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if not v:
            continue
        names_orig.append(v)
        name_to_row[v] = i
        last = v.split()[-1].lower()
        surnames.setdefault(last, []).append(v)
    keys_norm = [normalize_key(n) for n in names_orig]
    key_to_orig = {normalize_key(n): n for n in names_orig}
    log.info("[INIT] Loaded %d names from '%s'", len(names_orig), ws.title)
    return names_orig, name_to_row, keys_norm, key_to_orig, surnames

data_maps = {cid: load_player_names(ws) for cid, ws in ws_map.items()}

# ================== MATCHING ==================
def find_best_match(ws_id: int, text: str) -> Optional[Tuple[str, int, float, str]]:
    names_orig, name_to_row, keys_norm, key_to_orig, surnames = data_maps[ws_id]
    msg_clean = normalize_msg(text)

    skip_triggers = {"skipped", "skip", "pass", "waiting"}
    if any(word in msg_clean for word in skip_triggers):
        log.info("[SKIP] Ignored system message")
        return None
    if not msg_clean:
        return None

    # 1️⃣ Direct substring match
    for orig in names_orig:
        if normalize_key(orig) in msg_clean:
            log.info(f"[DIRECT MATCH] '{msg_clean}' → {orig}")
            return orig, name_to_row[orig], 100.0, "Direct"

    # 2️⃣ Unique surname fallback
    words = msg_clean.split()
    for w in words:
        if w in surnames and len(surnames[w]) == 1:
            orig = surnames[w][0]
            log.info(f"[SURNAME MATCH] '{w}' uniquely → {orig}")
            return orig, name_to_row[orig], 95.0, "Surname"

    # 3️⃣ Fuzzy fallback
    hits = process.extract(msg_clean, keys_norm, scorer=fuzz.token_sort_ratio, limit=3)
    if not hits:
        log.info(f"[MATCH] No hits for '{msg_clean}'")
        return None

    best_key, best_score = max(hits, key=lambda x: x[1])[:2]
    if best_score < FUZZY_THRESHOLD:
        # Check if surname is present even if fuzzy low
        last_name = best_key.split()[-1]
        if last_name in msg_clean:
            orig = key_to_orig.get(best_key)
            if orig:
                log.info(f"[SURNAME+FUZZY MATCH] '{msg_clean}' → {orig} (score={best_score})")
                return orig, name_to_row[orig], best_score, "Surname+Fuzzy"
        log.info(f"[MATCH] Weak fuzzy {best_score} for '{msg_clean}' (best={best_key})")
        return None

    orig = key_to_orig.get(best_key)
    if orig:
        log.info(f"[FUZZY MATCH] '{msg_clean}' → {orig} (score={best_score})")
        return orig, name_to_row[orig], best_score, "Fuzzy"
    return None

# ================== HIGHLIGHT ==================
async def highlight_row(ws, row: int, reason: str):
    rng = f"{ROW_START_COL}{row}:{ROW_END_COL}{row}"
    bg = {"red": 0.29, "green": 0.52, "blue": 0.91}  # #4a86e8
    text_fmt = {
        "foregroundColor": {"red": 0, "green": 0, "blue": 0},
        "fontFamily": "Roboto Condensed",
        "fontSize": 10,
        "bold": False,
    }
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": col_to_index(ROW_START_COL) - 1,
                "endColumnIndex": col_to_index(ROW_END_COL),
            },
            "cell": {"userEnteredFormat": {"backgroundColor": bg, "textFormat": text_fmt}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    }]
    await asyncio.to_thread(ws.spreadsheet.batch_update, {"requests": requests})
    log.info(f"[HIGHLIGHT] {ws.title} range={rng} ({reason})")

# ================== DISCORD BOT ==================
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents, reconnect=True)

@client.event
async def on_ready():
    log.info("✅ Logged in as %s | Watching channels %s and %s", client.user, CHANNEL_ID_1, CHANNEL_ID_2)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id is not None:
        return
    if message.type != MessageType.default:
        return
    if message.attachments or message.embeds or message.stickers:
        return

    ws = ws_map.get(message.channel.id)
    if not ws:
        return

    content = (message.content or "").strip()
    if not content:
        return

    best = find_best_match(message.channel.id, content)
    if not best:
        return

    name, row, score, reason = best
    await highlight_row(ws, row, reason)
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
