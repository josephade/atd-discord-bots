import os
import re
import json
import time
import logging
import asyncio
from typing import Tuple, Optional, List

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
log = logging.getLogger("highlighter")

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
CHANNEL_ID = int(need("DISCORD_CHANNEL_ID"))
SHEET_ID = need("GOOGLE_SHEET_ID")
WS_GID = int(need("GOOGLE_WORKSHEET_GID"))

NAME_COL_LETTER = os.getenv("NAME_COLUMN", "B").upper()
ROW_START_COL = os.getenv("ROW_HILIGHT_START", "A").upper()
ROW_END_COL = os.getenv("ROW_HILIGHT_END", "D").upper()

FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", 75))

# ==========================================================
# COMMANDS
# ==========================================================

CMD_HELP = "!helpatd"
CMD_RESET = "!newatd"
CMD_STATUS = "!status"
CMD_UNDO = "!undo"
CMD_FORCE = "!force"
CMD_COLOR = "!changehexcolour"

ALLOWED_ROLE_NAMES = {"Admin", "Moderator", "LeComissioner"}

# ==========================================================
# GOOGLE SHEETS AUTH
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
sh = gc.open_by_key(SHEET_ID)
ws = sh.get_worksheet_by_id(WS_GID)

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

def normalize(s: str) -> str:
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s)
    return s.strip().lower()

def hex_to_rgb_frac(hex_color: str):
    hex_color = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_color):
        return None
    return {
        "red": int(hex_color[0:2], 16) / 255,
        "green": int(hex_color[2:4], 16) / 255,
        "blue": int(hex_color[4:6], 16) / 255,
    }

# ==========================================================
# LOAD PLAYERS
# ==========================================================

def load_players():
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

    log.info(f"[INIT] Loaded {len(names)} players")
    return names, row_map, keys, key_to_name

names_orig, name_to_row, keys_norm, key_to_orig = load_players()

# ==========================================================
# STATE
# ==========================================================

highlighted_forever: set[str] = set()
highlight_stack: List[Tuple[str, int]] = []

HIGHLIGHT_COLOR = {"red": 0.286, "green": 0.518, "blue": 0.910}

# ==========================================================
# RATE LIMITING (CRITICAL)
# ==========================================================

LAST_REACTION_TIME = 0
REACTION_COOLDOWN = 1.5  # seconds

async def safe_react(message: discord.Message, emoji: str):
    global LAST_REACTION_TIME

    wait = REACTION_COOLDOWN - (time.time() - LAST_REACTION_TIME)
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        await message.add_reaction(emoji)
        LAST_REACTION_TIME = time.time()
    except discord.HTTPException as e:
        if e.status == 429:
            log.warning("⚠️ Rate limited by Discord, skipping reaction")
        else:
            raise

# ==========================================================
# MATCHING
# ==========================================================

def find_best_match(text: str) -> Optional[Tuple[str, int]]:
    msg = normalize(text)

    for n in names_orig:
        if normalize(n) in msg:
            return n, name_to_row[n]

    hit = process.extractOne(msg, keys_norm, scorer=fuzz.token_sort_ratio)
    if not hit or hit[1] < FUZZY_THRESHOLD:
        return None

    name = key_to_orig[hit[0]]
    return name, name_to_row[name]

# ==========================================================
# SHEET OPS
# ==========================================================

async def apply_highlight(row: int, name: str):
    key = f"{ws.title}:{name.lower()}"

    if key in highlighted_forever:
        return "already"

    highlighted_forever.add(key)
    highlight_stack.append((name, row))

    start = col_to_index(ROW_START_COL) - 1
    end = col_to_index(ROW_END_COL)

    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": start,
                "endColumnIndex": end,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": HIGHLIGHT_COLOR}},
            "fields": "userEnteredFormat.backgroundColor"
        }
    }]

    await asyncio.to_thread(sh.batch_update, {"requests": requests})
    return True

async def clear_highlight(row: int):
    start = col_to_index(ROW_START_COL) - 1
    end = col_to_index(ROW_END_COL)

    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": start,
                "endColumnIndex": end,
            },
            "cell": {"userEnteredFormat": {}},
            "fields": "userEnteredFormat"
        }
    }]

    await asyncio.to_thread(sh.batch_update, {"requests": requests})

# ==========================================================
# DISCORD BOT
# ==========================================================

intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def has_permission(member: discord.Member) -> bool:
    if not ALLOWED_ROLE_NAMES:
        return True
    return bool({r.name for r in member.roles} & ALLOWED_ROLE_NAMES)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()

    # ---------------- COMMANDS ----------------
    if content == CMD_HELP:
        await message.reply("📘 ATD Highlight Bot – use `!newatd` before drafts", mention_author=False)
        return

    if content == CMD_RESET:
        highlighted_forever.clear()
        highlight_stack.clear()
        await message.reply("🧹 ATD memory reset.")
        return

    if content == CMD_STATUS:
        await message.reply(f"📊 Highlighted: {len(highlighted_forever)}", mention_author=False)
        return

    if content == CMD_UNDO:
        if not highlight_stack:
            await message.reply("⚠️ Nothing to undo.")
            return
        name, row = highlight_stack.pop()
        highlighted_forever.discard(f"{ws.title}:{name.lower()}")
        await clear_highlight(row)
        await message.reply(f"↩️ Undid highlight for **{name}**")
        return

    if content.lower().startswith(CMD_FORCE):
        forced = content[len(CMD_FORCE):].strip()
        match = find_best_match(forced)
        if not match:
            await message.reply("❌ Player not found.")
            return
        name, row = match
        await apply_highlight(row, name)
        await safe_react(message, "✅")
        return

    # ---------------- DRAFT FLOW ----------------
    if message.channel.id != CHANNEL_ID:
        return

    match = find_best_match(content)
    if not match:
        return

    name, row = match
    result = await apply_highlight(row, name)

    if result == "already":
        warn = await message.reply(
            f"❌ {message.author.mention} **{name}** has already been picked. Please pick again."
        )
        await asyncio.sleep(4)
        await warn.delete()
        return

    await safe_react(message, "✅")
    ok = await message.reply(f"✅ **{name}** has been logged successfully.")
    await asyncio.sleep(3)
    await ok.delete()

@client.event
async def on_ready():
    log.info(f"Connected as {client.user}")

# ==========================================================
# MAIN
# ==========================================================

if __name__ == "__main__":
    while True:
        try:
            client.run(DISCORD_TOKEN)
        except Exception as e:
            log.error(f"Discord error: {e}")
            time.sleep(5)
