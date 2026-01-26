import os
import re
import time
import logging
import asyncio
from typing import Tuple, Optional, List, Dict

import discord
from discord import Intents, MessageType
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("atd-bot")

# ==========================================================
# ENV
# ==========================================================

load_dotenv()

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env: {name}")
    return v

DISCORD_TOKEN = need("DISCORD_TOKEN")

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ROW_START_COL = os.getenv("ROW_HILIGHT_START", "A").upper()
ROW_END_COL = os.getenv("ROW_HILIGHT_END", "D").upper()
FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", 75))

# ==========================================================
# THREAD → SHEET CONFIG (EDIT THIS)
# ==========================================================

THREAD_CONFIG = {
    # thread_id: { spreadsheet_id, worksheet_name }
    # EXAMPLE:
    # 123456789012345678: {
    #     "spreadsheet_id": "1AbCdEf...",
    #     "worksheet_name": "Players"
    # },
    1465444677141528666: {
        "spreadsheet_id": "1CQyO93HKc5VlXsqS48dnPlkac153TDDoUsiqyhJwwOI",
        "worksheet_name": "East"
    },
    1465444571965034576: {
        "spreadsheet_id": "1CQyO93HKc5VlXsqS48dnPlkac153TDDoUsiqyhJwwOI",
        "worksheet_name": "West"
    },
}

# ==========================================================
# GOOGLE AUTH
# ==========================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(
    os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json"),
    scopes=SCOPES,
)
gc = gspread.authorize(creds)

# ==========================================================
# HELPERS
# ==========================================================

def col_to_index(col: str) -> int:
    idx = 0
    for c in col:
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)

NONLETTER_RE = re.compile(r"[^A-Za-z\s]")
WHITESPACE_RE = re.compile(r"\s+")
MENTION_RE = re.compile(r"<@!?\d+>")

def normalize(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s)
    return s.strip().lower()

# ==========================================================
# THREAD STATE
# ==========================================================

thread_state: Dict[int, Dict] = {}
sheet_cache: Dict[int, tuple] = {}
player_cache: Dict[int, tuple] = {}

def get_state(thread_id: int):
    if thread_id not in thread_state:
        thread_state[thread_id] = {
            "highlighted": set(),
            "stack": []
        }
    return thread_state[thread_id]

def get_sheet(thread_id: int):
    if thread_id not in THREAD_CONFIG:
        return None

    if thread_id in sheet_cache:
        return sheet_cache[thread_id]

    cfg = THREAD_CONFIG[thread_id]
    sh = gc.open_by_key(cfg["spreadsheet_id"])
    ws = sh.worksheet(cfg["worksheet_name"])
    sheet_cache[thread_id] = (sh, ws)
    return sh, ws

def load_players(thread_id: int, ws):
    if thread_id in player_cache:
        return player_cache[thread_id]

    col = ws.col_values(NAME_COL_INDEX)
    names, row_map, keys, key_to_name = [], {}, [], {}

    for i, v in enumerate(col, start=1):
        if not v.strip():
            continue
        names.append(v)
        row_map[v] = i
        k = normalize(v)
        keys.append(k)
        key_to_name[k] = v

    player_cache[thread_id] = (names, row_map, keys, key_to_name)
    log.info(f"[THREAD {thread_id}] Loaded {len(names)} players")
    return player_cache[thread_id]

# ==========================================================
# MATCHING
# ==========================================================

def find_best_match(text: str, names, row_map, keys, key_to_name):
    msg = normalize(text)

    for n in names:
        if normalize(n) in msg:
            return n, row_map[n]

    hit = process.extractOne(msg, keys, scorer=fuzz.token_sort_ratio)
    if not hit or hit[1] < FUZZY_THRESHOLD:
        return None

    name = key_to_name[hit[0]]
    return name, row_map[name]

# ==========================================================
# DISCORD BOT
# ==========================================================

intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.type not in (MessageType.default, MessageType.reply):
        return

    if not isinstance(message.channel, discord.Thread):
        return

    thread_id = message.channel.id
    sheet = get_sheet(thread_id)
    if not sheet:
        return

    sh, ws = sheet
    names, row_map, keys, key_to_name = load_players(thread_id, ws)
    state = get_state(thread_id)

    content = MENTION_RE.sub("", message.content).strip()

    if content.startswith("!"):
        if content == "!status":
            await message.reply(f"📊 Highlighted: {len(state['highlighted'])}")
        elif content == "!newatd":
            state["highlighted"].clear()
            state["stack"].clear()
            await message.reply("🧹 ATD memory reset.")
        return

    match = find_best_match(content, names, row_map, keys, key_to_name)
    if not match:
        return

    name, row = match
    key = f"{ws.title}:{name.lower()}"

    if key in state["highlighted"]:
        await message.reply(
            f"❌ {message.author.mention} **{name}** has already been picked."
        )
        return

    state["highlighted"].add(key)
    state["stack"].append((name, row))

    start = col_to_index(ROW_START_COL) - 1
    end = col_to_index(ROW_END_COL)

    await asyncio.to_thread(
        sh.batch_update,
        {
            "requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": start,
                        "endColumnIndex": end,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.286,
                                "green": 0.518,
                                "blue": 0.910
                            }
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            }]
        }
    )

    await message.add_reaction("✅")

@client.event
async def on_ready():
    log.info(f"Connected as {client.user}")

# ==========================================================
# MAIN
# ==========================================================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
