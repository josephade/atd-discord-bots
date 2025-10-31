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

# Two Discord channels → two Google Sheet tabs
CHANNEL_ID_1 = int(need("DISCORD_CHANNEL_ID_1"))
CHANNEL_ID_2 = int(need("DISCORD_CHANNEL_ID_2"))

SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID_1 = int(need("GOOGLE_WORKSHEET_GID_1"))
WS_GID_2 = int(need("GOOGLE_WORKSHEET_GID_2"))

# Highlight configuration
NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ROW_START_COL = os.getenv("ROW_HILIGHT_START", "A").upper()
ROW_END_COL = os.getenv("ROW_HILIGHT_END", "D").upper()

FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", "88"))
LOW_FUZZY_CUTOFF = int(os.getenv("LOW_FUZZY_CUTOFF", "80"))

# ================== GOOGLE SHEETS AUTH ==================
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

if not GOOGLE_CREDENTIALS_JSON and not os.path.exists(GOOGLE_CREDENTIALS_PATH):
    raise SystemExit("Missing credentials: set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_PATH")

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
log.info("[GS] Connected to %s with 2 worksheets: %s and %s", SHEET_ID, WS_GID_1, WS_GID_2)

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

def try_parse_picknum(text: str) -> Optional[int]:
    """Detect a leading number like '66.', '66 -', '66)' etc."""
    m = re.match(r"^\s*(\d+)\s*[).:-]?\s*", text)
    return int(m.group(1)) if m else None

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
    log.info("[INIT] Loaded %d names from %s", len(names_orig), ws.title)
    return names_orig, name_to_row, keys_norm, key_to_orig

# load player maps per worksheet
data_maps = {}
for cid, ws in ws_map.items():
    data_maps[cid] = load_player_names(ws)

# ================== MATCHING ==================
def find_best_match(ws_id: int, text: str) -> Optional[Tuple[str, int, float]]:
    """Find the best matching player name, even inside full sentences."""
    names_orig, name_to_row, keys_norm, key_to_orig = data_maps[ws_id]

    # 1️⃣ Clean and normalize the message
    msg_clean = normalize_msg(text)
    log.info("[MSG] Raw=%r | Normalized=%r", text, msg_clean)

    if not msg_clean:
        return None

    # 2️⃣ Tokenize message into possible name fragments
    # remove common filler/stop words
    stopwords = {
        "the", "from", "you", "u", "for", "and", "but", "with", "to", "of", "my",
        "your", "me", "lock", "locks", "steal", "stole", "draft", "pick", "round",
        "team", "up", "haha", "hahaha", "lmao", "lol", "yo", "bro", "him", "her"
    }
    tokens = [w for w in re.split(r"\s+", msg_clean) if w and w not in stopwords]

    if not tokens:
        return None

    # 3️⃣ Generate n-grams (1–3 words) to catch names like "Nikola Jokic"
    candidates = set()
    for i in range(len(tokens)):
        for j in range(i + 1, min(len(tokens), i + 3) + 1):
            frag = " ".join(tokens[i:j])
            if len(frag) >= 3:
                candidates.add(frag)

    # 4️⃣ Fuzzy match each candidate against player names
    best_match = None
    best_score = 0
    best_orig = None

    for cand in candidates:
        hits = process.extract(cand, keys_norm, scorer=fuzz.token_set_ratio, limit=1)
        if hits:
            k, score, _ = hits[0]
            if score > best_score:
                best_score = score
                best_orig = key_to_orig.get(k)

    # 5️⃣ Return match only if above threshold
    if best_orig and best_score >= LOW_FUZZY_CUTOFF:
        log.info("[SMART MATCH] '%s' → %s (score=%s)", text, best_orig, best_score)
        return (best_orig, name_to_row[best_orig], best_score)
    else:
        log.info("[SMART MATCH] No strong player name found in: %r", text)
        return None



# ================== HIGHLIGHT ==================
def highlight_row(ws, row: int):
    rng = f"{ROW_START_COL}{row}:{ROW_END_COL}{row}"
    log.info("[HILIGHT] %s Range=%s (bg=#4a86e8, text=black, font=Roboto Condensed, size=10px, not bold)", ws.title, rng)
    
    # Hex #4A86E8 → RGB (0.290, 0.525, 0.909)
    bg = {"red": 0.29, "green": 0.525, "blue": 0.909}
    text_color = {"red": 0, "green": 0, "blue": 0}

    ws.format(rng, {
        "backgroundColor": bg,
        "textFormat": {
            "foregroundColor": text_color,
            "bold": False,
            "fontSize": 10,
            "fontFamily": "Roboto Condensed"
        }
    })

# ================== DISCORD BOT ==================
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    log.info("✅ Logged in as %s | Watching channels: %s and %s", client.user, CHANNEL_ID_1, CHANNEL_ID_2)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    ws = ws_map.get(message.channel.id)
    if not ws:
        return  # ignore messages outside tracked channels

    content = (message.content or "").strip()
    if not content:
        return

    log.info("[DISCORD] %s (%s) said: %r", message.author, ws.title, content)

    best = find_best_match(message.channel.id, content)
    if not best:
        log.info("[MATCH] No match above threshold")
        try:
            await message.add_reaction("❓")
        except Exception:
            pass
        return

    name, row, score = best
    log.info("[MATCH] %s (%s) -> row %s (score=%s)", name, ws.title, row, score)

    highlight_row(ws, row)

    try:
        await message.add_reaction("✅")
    except Exception:
        pass

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
