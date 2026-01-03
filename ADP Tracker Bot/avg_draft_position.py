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
from gspread.utils import rowcol_to_a1

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

SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID   = int(need("GOOGLE_WORKSHEET_GID"))

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "A").upper()

# ===================== CHANNEL CONFIG =====================
CHAN1 = int(need("DISCORD_CHANNEL_ID_1"))
COL1  = need("ADP_COLUMN_1").upper()

CHAN2 = int(need("DISCORD_CHANNEL_ID_2"))
COL2  = need("ADP_COLUMN_2").upper()

CHAN3 = int(need("DISCORD_CHANNEL_ID_3"))
COL3  = need("ADP_COLUMN_3").upper()

DISCORD_TOKEN = need("DISCORD_TOKEN")

channel_to_col_letter = {
    CHAN1: COL1,
    CHAN2: COL2,
    CHAN3: COL3,
}

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter:
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)
channel_to_col_index = {ch: col_to_index(col) for ch, col in channel_to_col_letter.items()}

# ----------------- google sheets -----------------
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

# ----------------- normalization -----------------
NONLETTER_RE = re.compile(r"[^A-Za-z\s]")
WHITESPACE_RE = re.compile(r"\s+")

def clean(s: str) -> str:
    return WHITESPACE_RE.sub(" ", NONLETTER_RE.sub(" ", s)).strip().lower()

def load_players():
    col_vals = ws.col_values(NAME_COL_INDEX)
    names, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        if v.strip():
            names.append(v)
            name_to_row[v] = i
    key_map = {clean(n): n for n in names}
    return names, name_to_row, key_map

NAMES, NAME_TO_ROW, KEY_TO_ORIG = load_players()

# ----------------- parsing -----------------
LEADING_PICK_RE = re.compile(r"^\s*(\d+)\s*[).:-]?\s*")

def try_parse_picknum(text):
    m = LEADING_PICK_RE.match(text)
    return int(m.group(1)) if m else None

def find_best_match(text):
    cleaned = clean(LEADING_PICK_RE.sub("", text))
    if not cleaned:
        return None

    if cleaned in KEY_TO_ORIG:
        name = KEY_TO_ORIG[cleaned]
        return name, NAME_TO_ROW[name], 100

    hit = process.extractOne(cleaned, KEY_TO_ORIG.keys(), scorer=fuzz.token_set_ratio)
    if hit and hit[1] >= 85:
        name = KEY_TO_ORIG[hit[0]]
        return name, NAME_TO_ROW[name], hit[1]

    return None

# ----------------- discord -----------------
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user}")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    col_index = channel_to_col_index.get(message.channel.id)
    if not col_index:
        return

    raw = message.content.strip()
    pick_num = try_parse_picknum(raw)

    if pick_num is None:
        await message.add_reaction("❓")
        return

    match = find_best_match(raw)
    if not match:
        await message.add_reaction("❓")
        return

    name, row, score = match
    cell = rowcol_to_a1(row, col_index)

    existing = ws.acell(cell).value

    if existing:
        await message.add_reaction("❌")
        await message.reply(
            f"**{name}** has already been picked and tracked at pick **{existing}**."
        )
        log.info(f"[DUPLICATE] {name} already picked at {existing}")
        return

    ws.update(cell, [[pick_num]])
    await message.add_reaction("✅")
    log.info(f"[UPDATE] {name} → pick {pick_num}")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
