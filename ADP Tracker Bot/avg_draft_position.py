import os
import re
import asyncio
from collections import defaultdict

import discord
from discord import Intents
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ================== ENV CONFIG ==================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WS_GID = int(os.getenv("GOOGLE_WORKSHEET_GID", "0"))

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ADP_COL_LETTER = os.getenv("ADP_COLUMN", "C").upper()

if not (DISCORD_TOKEN and CHANNEL_ID and SHEET_ID and WS_GID):
    raise SystemExit("Missing env vars. Check .env file!")

# ================== GOOGLE SHEETS ==================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")
creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
gc = gspread.authorize(creds)

sh = gc.open_by_key(SHEET_ID)
ws = sh.get_worksheet_by_id(WS_GID)

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)
ADP_COL_INDEX = col_to_index(ADP_COL_LETTER)

# ================== NORMALIZATION ==================
NONLETTER_RE = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")

def normalize_key(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

# ================== LOAD PLAYERS ==================
def load_players():
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if v:
            names_orig.append(v)
            name_to_row[v] = i
    keys_norm = [normalize_key(n) for n in names_orig]
    key_to_orig = {normalize_key(n): n for n in names_orig}
    print(f"[INIT] Loaded {len(names_orig)} players")
    return names_orig, name_to_row, keys_norm, key_to_orig

ALL_NAMES, NAME_TO_ROW, ALL_KEYS, KEY_TO_ORIG = load_players()

# ================== DISCORD BOT ==================
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

pick_history = defaultdict(list)

def find_best_match(text: str):
    q = normalize_key(text)
    exact_orig = KEY_TO_ORIG.get(q)
    if exact_orig:
        return exact_orig, NAME_TO_ROW[exact_orig], 100.0

    hits = process.extract(q, ALL_KEYS, scorer=fuzz.token_sort_ratio, score_cutoff=85, limit=1)
    if hits:
        best_key, score, _ = hits[0]
        orig = KEY_TO_ORIG.get(best_key)
        return (orig, NAME_TO_ROW[orig], score) if orig else None
    return None

@client.event
async def on_ready():
    print(f"✅ ADP Tracker Logged in as {client.user}")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return

    text = message.content.strip()
    if not text:
        return

    best = find_best_match(text)
    if not best:
        await message.add_reaction("❓")
        return

    name, row, _ = best
    pick_num = len(pick_history[name]) + 1
    pick_history[name].append(pick_num)

    avg_pick = sum(pick_history[name]) / len(pick_history[name])
    ws.update_cell(row, ADP_COL_INDEX, round(avg_pick, 2))
    print(f"[UPDATE] {name} -> avg pick {avg_pick:.2f}")
    await message.add_reaction("✅")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
