import os
import re
import json
import logging

import discord
from discord import Intents
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ----------------- logging -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("adp")

# ----------------- env -----------------
load_dotenv()

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env: {name}")
    return v

# Sheet + creds
SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID   = int(need("GOOGLE_WORKSHEET_GID"))

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

# Name column (where player names live)
NAME_COL_LETTER = os.getenv("NAME_COLUMN", "A").upper()

# ===================== ADP CHANNEL CONFIG =====================

# Channel 1
CHAN1 = int(need("DISCORD_CHANNEL_ID_1"))
COL1  = need("ADP_COLUMN_1").upper()

# Channel 2
CHAN2 = int(need("DISCORD_CHANNEL_ID_2"))
COL2  = need("ADP_COLUMN_2").upper()

# Channel 3 (NEW)
CHAN3 = int(need("DISCORD_CHANNEL_ID_3"))
COL3  = need("ADP_COLUMN_3").upper()

DISCORD_TOKEN = need("DISCORD_TOKEN")

# Map channels → target columns
channel_to_col_letter = {
    CHAN1: COL1,
    CHAN2: COL2,
    CHAN3: COL3,
}

# Convert column letters to numeric indexes
def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)

channel_to_col_index = {
    ch: col_to_index(letter)
    for ch, letter in channel_to_col_letter.items()
}

log.info(f"[CFG] Name column: {NAME_COL_LETTER} ({NAME_COL_INDEX})")
for ch, letter in channel_to_col_letter.items():
    log.info(f"[CFG] Channel {ch} -> column {letter} (index {channel_to_col_index[ch]})")

# ----------------- google sheets -----------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

if GOOGLE_CREDENTIALS_JSON:
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
else:
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        raise SystemExit("GOOGLE_CREDENTIALS_JSON is empty and GOOGLE_CREDENTIALS_PATH not found")
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)

gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)
ws = sh.get_worksheet_by_id(WS_GID)

# ----------------- normalization + player load -----------------
NONLETTER_RE  = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")

def clean(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

def load_players():
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if v:
            names_orig.append(v)
            name_to_row[v] = i
    key_to_orig = {clean(n): n for n in names_orig}
    log.info(f"[INIT] Loaded {len(names_orig)} players")
    return names_orig, name_to_row, key_to_orig

NAMES, NAME_TO_ROW, KEY_TO_ORIG = load_players()

# ----------------- parsing & matching -----------------
LEADING_PICK_RE = re.compile(r"^\s*(\d+)\s*[).:-]?\s*", re.ASCII)

def try_parse_picknum(text: str):
    m = LEADING_PICK_RE.match(text)
    return int(m.group(1)) if m else None

def find_best_match(text: str):
    text_no_pick = LEADING_PICK_RE.sub("", text)
    cleaned = clean(text_no_pick)
    if not cleaned:
        return None

    exact = KEY_TO_ORIG.get(cleaned)
    if exact:
        return exact, NAME_TO_ROW[exact], 100.0

    keys = list(KEY_TO_ORIG.keys())
    hits = process.extract(cleaned, keys, scorer=fuzz.token_set_ratio, limit=1)
    if hits:
        k, score, _ = hits[0]
        if score >= 85:
            orig = KEY_TO_ORIG[k]
            return orig, NAME_TO_ROW[orig], score
    return None

# ----------------- discord -----------------
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    log.info(
        f"ADP Tracker logged in as {client.user} — watching channels {list(channel_to_col_letter.keys())}"
    )

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    col_index = channel_to_col_index.get(message.channel.id)
    col_letter = channel_to_col_letter.get(message.channel.id)

    if not col_index:
        return  # ignore untracked channels

    raw = (message.content or "").strip()
    if not raw:
        return

    log.info(f"[DISCORD] {message.author} said: {raw!r}")

    pick_num = try_parse_picknum(raw)
    if pick_num is None:
        log.info("[PICK] No pick number detected")
        try: await message.add_reaction("❓")
        except: pass
        return

    log.info(f"[PICK] Parsed pick number: {pick_num}")

    best = find_best_match(raw)

    log.info(f"[MSG] Raw={raw!r} | Cleaned={clean(LEADING_PICK_RE.sub('', raw))!r}")

    if not best:
        log.info("[MATCH] No match found")
        try: await message.add_reaction("❓")
        except: pass
        return

    name, row, score = best
    match_type = "Exact" if score == 100 else "Fuzzy"
    log.info(f"[MATCH] {match_type} → {name} (row={row}, score={score})")

    ws.update_cell(row, col_index, int(pick_num))
    log.info(
        f"[UPDATE] {name}: wrote pick {pick_num} to row {row}, col {col_index} ({col_letter})"
    )

    try: await message.add_reaction("✅")
    except Exception:
        pass

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
