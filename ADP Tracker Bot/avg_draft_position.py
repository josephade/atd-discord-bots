import os
import re
import json
import sys
import asyncio
from collections import defaultdict
from typing import Optional, Tuple

import logging
from logging import StreamHandler

import discord
from discord import Intents
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# ================== LOGGING (make Render show every line) ==================
# Force line-buffered stdout if available (Py3.7+)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Send logs to stdout, override any prior config, include level + message
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("adp-bot")

# Optional: quiet down very chatty libraries
logging.getLogger("discord").setLevel(logging.INFO)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("gspread").setLevel(logging.INFO)

# ================== ENV CONFIG ==================
load_dotenv()  # loads .env if present next to the script

# Read raw strings first (so "0" defaults don't hide missing values)
DISCORD_TOKEN_RAW = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID_RAW = os.getenv("DISCORD_CHANNEL_ID")
GOOGLE_SHEET_ID_RAW = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_WORKSHEET_GID_RAW = os.getenv("GOOGLE_WORKSHEET_GID")

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ADP_COL_LETTER  = os.getenv("ADP_COLUMN", "C").upper()

# Figure out credentials source (either JSON or a file path)
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

# Collect missing/invalid envs
missing = []
if not DISCORD_TOKEN_RAW:
    missing.append("DISCORD_TOKEN")
if not DISCORD_CHANNEL_ID_RAW:
    missing.append("DISCORD_CHANNEL_ID")
if not GOOGLE_SHEET_ID_RAW:
    missing.append("GOOGLE_SHEET_ID")
if not GOOGLE_WORKSHEET_GID_RAW:
    missing.append("GOOGLE_WORKSHEET_GID")

# Validate credentials presence
creds_source = None
if GOOGLE_CREDENTIALS_JSON:
    creds_source = "json"
elif GOOGLE_CREDENTIALS_PATH and os.path.exists(GOOGLE_CREDENTIALS_PATH):
    creds_source = "file"
else:
    missing.append("GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_PATH (file not found)")

# If anything missing -> exit with a clear list
if missing:
    msg = "Missing/invalid environment variables:\n  - " + "\n  - ".join(missing)
    log.error(msg)
    raise SystemExit(msg)

# Safe conversions now that presence is confirmed
DISCORD_TOKEN = DISCORD_TOKEN_RAW
CHANNEL_ID = int(DISCORD_CHANNEL_ID_RAW)
SHEET_ID = GOOGLE_SHEET_ID_RAW
WS_GID = int(GOOGLE_WORKSHEET_GID_RAW)

log.info("ENV OK | channel=%s sheet=%s gid=%s name_col=%s adp_col=%s",
         CHANNEL_ID, SHEET_ID, WS_GID, NAME_COL_LETTER, ADP_COL_LETTER)

# ================== GOOGLE SHEETS ==================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

try:
    if creds_source == "json":
        log.info("Using GOOGLE_CREDENTIALS_JSON for auth")
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    else:
        log.info("Using GOOGLE_CREDENTIALS_PATH=%s for auth", GOOGLE_CREDENTIALS_PATH)
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.get_worksheet_by_id(WS_GID)
except Exception:
    log.exception("Failed to initialize Google Sheets client")
    raise

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)
ADP_COL_INDEX  = col_to_index(ADP_COL_LETTER)

# ================== NORMALIZATION ==================
NONLETTER_RE  = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")

def normalize_key(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

# ================== LOAD PLAYERS ==================
def load_players():
    try:
        col_vals = ws.col_values(NAME_COL_INDEX)
    except Exception:
        log.exception("Failed to read player name column (index %s) from Google Sheet", NAME_COL_INDEX)
        raise

    names_orig, name_to_row = [], {}
    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if v:
            names_orig.append(v)
            name_to_row[v] = i
    keys_norm = [normalize_key(n) for n in names_orig]
    key_to_orig = {normalize_key(n): n for n in names_orig}
    log.info("[INIT] Loaded %d players", len(names_orig))
    return names_orig, name_to_row, keys_norm, key_to_orig

ALL_NAMES, NAME_TO_ROW, ALL_KEYS, KEY_TO_ORIG = load_players()

# ================== DISCORD BOT ==================
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

pick_history = defaultdict(list)

def find_best_match(text: str) -> Optional[Tuple[str, int, float]]:
    q = normalize_key(text)
    log.info("[MSG] Raw=%r | Normalized=%r", text, q)

    exact_orig = KEY_TO_ORIG.get(q)
    if exact_orig:
        log.info("[MATCH] Exact match -> %s (row=%s, score=100)", exact_orig, NAME_TO_ROW[exact_orig])
        return exact_orig, NAME_TO_ROW[exact_orig], 100.0

    hits = process.extract(q, ALL_KEYS, scorer=fuzz.token_sort_ratio, score_cutoff=85, limit=1)
    if hits:
        best_key, score, _ = hits[0]
        orig = KEY_TO_ORIG.get(best_key)
        if orig:
            log.info("[MATCH] Fuzzy match -> %s (row=%s, score=%.1f)", orig, NAME_TO_ROW[orig], score)
            return orig, NAME_TO_ROW[orig], score

    log.info("[MATCH] No match above threshold")
    return None

async def heartbeat():
    # keep-alive log every 5 minutes
    while True:
        log.info("heartbeat: bot alive; watching channel %s", CHANNEL_ID)
        await asyncio.sleep(300)

@client.event
async def on_ready():
    log.info("✅ ADP Tracker Logged in as %s (channel %s)", client.user, CHANNEL_ID)
    try:
        client.loop.create_task(heartbeat())
    except Exception:
        # older discord.py versions may differ; ignore if it fails
        pass

@client.event
async def on_message(message: discord.Message):
    try:
        if message.author.bot or message.channel.id != CHANNEL_ID:
            return

        text = (message.content or "").strip()
        if not text:
            return

        log.info("[DISCORD] %s#%s said: %r", message.author.name, message.author.discriminator, text)

        best = find_best_match(text)
        if not best:
            try:
                await message.add_reaction("❓")
            except Exception:
                log.warning("Failed to add ❓ reaction", exc_info=True)
            return

        name, row, _ = best

        # If your messages include "65. Player Name", you can parse the leading number:
        typed_pick = None
        m = re.match(r"^\s*(\d+)", text)
        if m:
            typed_pick = int(m.group(1))
        else:
            # fallback to simple running average index if you want
            pick_num = len(pick_history[name]) + 1
            pick_history[name].append(pick_num)
            typed_pick = round(sum(pick_history[name]) / len(pick_history[name]), 2)

        # Write to Sheets
        try:
            ws.update_cell(row, ADP_COL_INDEX, typed_pick)
            log.info("[UPDATE] Wrote %s to row=%s col=%s for %s",
                     typed_pick, row, ADP_COL_INDEX, name)
        except Exception:
            log.exception("Sheets update failed for row=%s col=%s value=%r", row, ADP_COL_INDEX, typed_pick)
            try:
                await message.add_reaction("‼️")
            except Exception:
                pass
            return

        # React success
        try:
            await message.add_reaction("✅")
        except Exception:
            log.warning("Failed to add ✅ reaction", exc_info=True)

    except Exception:
        log.exception("Unhandled exception in on_message")

if __name__ == "__main__":
    # extra safety: unbuffered mode hint
    if not os.getenv("PYTHONUNBUFFERED"):
        log.info("Tip: set PYTHONUNBUFFERED=1 or run with `python -u` for instant logs")
    client.run(DISCORD_TOKEN)
