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

# ==========================================================
# LOGGING CONFIGURATION (IMPROVED)
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("highlighter")

# ==========================================================
# LOAD ENVIRONMENT
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
LOW_FUZZY_CUTOFF = int(os.getenv("LOW_FUZZY_CUTOFF", 65))

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

# ==========================================================
# GOOGLE SHEETS AUTH
# ==========================================================

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

# ==========================================================
# HELPERS
# ==========================================================

def col_to_index(col_letter: str) -> int:
    idx = 0
    for c in col_letter.strip().upper():
        idx = idx * 26 + (ord(c) - 64)
    return idx

NAME_COL_INDEX = col_to_index(NAME_COL_LETTER)

CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
NONLETTER_RE = re.compile(r"[^A-Za-z\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
SMART_QUOTE_RE = re.compile(r"[‘’´`“”]")

def normalize_key(s: str) -> str:
    s = SMART_QUOTE_RE.sub("'", s)
    s = NONLETTER_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s

def normalize_msg(t: str) -> str:
    t = SMART_QUOTE_RE.sub("'", t)
    t = CUSTOM_EMOJI_RE.sub(" ", t)
    t = re.sub(r"[\u200B-\u200D\uFEFF]", "", t)
    t = re.sub(r"\$?\d+(\.\d+)?", "", t)
    t = re.sub(r"\([^)]*\)", "", t)
    t = NONLETTER_RE.sub(" ", t)
    t = WHITESPACE_RE.sub(" ", t)
    return t.strip().lower()

# ==========================================================
# LOAD PLAYER NAMES
# ==========================================================

def load_player_names(ws):
    col_vals = ws.col_values(NAME_COL_INDEX)
    names_orig, name_to_row, keys_norm, surnames = [], {}, [], {}
    key_to_orig = {}

    for i, v in enumerate(col_vals, start=1):
        v = (v or "").strip()
        if not v:
            continue

        names_orig.append(v)
        name_to_row[v] = i

        norm = normalize_key(v)
        keys_norm.append(norm)
        key_to_orig[norm] = v

        last = v.split()[-1].lower()
        surnames.setdefault(last, []).append(v)

    log.info(
        f"[INIT] Loaded {len(names_orig)} players | Sheet='{ws.title}' | NameCol='{NAME_COL_LETTER}' (idx {NAME_COL_INDEX})"
    )

    return names_orig, name_to_row, keys_norm, key_to_orig, surnames

names_orig, name_to_row, keys_norm, key_to_orig, surnames = load_player_names(ws)
highlighted_forever: set[str] = set()

# ==========================================================
# FIND BEST MATCH (WITH DETAILED LOGGING)
# ==========================================================

def find_best_match(text: str) -> Optional[Tuple[str, int, float, str]]:
    msg_clean = normalize_msg(text)
    msg_words = msg_clean.split()

    if len(msg_words) < 2:
        return None

    skip_triggers = {
        "skipped", "skip", "pass", "waiting", "block", "blocked",
        "invalid", "bot", "register", "testing", "bro", "man", "lol",
        "pick", "why", "cant", "team", "round"
    }

    if any(w in msg_words for w in skip_triggers):
        log.info(f"[SKIP] Ignored message (general chatter): '{text}'")
        return None

    # Direct full-name match
    for orig in names_orig:
        if normalize_key(orig) in msg_clean:
            return orig, name_to_row[orig], 100.0, "Direct"

    # Unique surname
    for w in msg_words:
        if w in surnames and len(surnames[w]) == 1:
            orig = surnames[w][0]
            return orig, name_to_row[orig], 95.0, "Surname"

    # Fuzzy match
    hits = process.extract(msg_clean, keys_norm, scorer=fuzz.token_sort_ratio, limit=3)
    if not hits:
        return None

    best_key, best_score = hits[0][:2]

    if best_score < FUZZY_THRESHOLD:
        return None

    orig = key_to_orig.get(best_key)
    if orig:
        return orig, name_to_row[orig], best_score, "Fuzzy"

    return None

# ==========================================================
# APPLY HIGHLIGHT (FIXED + IMPROVED LOGGING)
# ==========================================================

async def highlight_row(row: int, reason: str, name: str):
    cache_key = f"{ws.title}:{name.lower()}"
    if cache_key in highlighted_forever:
        return "already"

    highlighted_forever.add(cache_key)

    start_col = col_to_index(ROW_START_COL) - 1
    end_col = col_to_index(ROW_END_COL)

    rng = f"{ROW_START_COL}{row}:{ROW_END_COL}{row}"

    bg = {"red": 0.29, "green": 0.52, "blue": 0.91}
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
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": bg, "textFormat": text_fmt}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }
    }]

    # FIX: Use sh.batch_update (correct spreadsheet handle)
    await asyncio.to_thread(sh.batch_update, {"requests": requests})

    log.info(
        f"[HIGHLIGHT] Sheet='{ws.title}' | Name='{name}' | Row={row} | Range={rng} | Reason={reason}"
    )

    return True

# ==========================================================
# DISCORD BOT
# ==========================================================

intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents, reconnect=True)

@client.event
async def on_ready():
    log.info(f"🔗 Connected as {client.user} | Watching Channel={CHANNEL_ID}")

@client.event
async def on_message(message: discord.Message):

    if message.author.bot or message.webhook_id:
        return
    if message.channel.id != CHANNEL_ID:
        return
    if message.type not in (MessageType.default, MessageType.reply):
        return

    content = (message.content or "").strip()
    original_content = content

    # Handle replies (merge parent content)
    if message.reference and message.reference.resolved:
        parent = message.reference.resolved
        if isinstance(parent, discord.Message) and parent.content:
            if len(normalize_msg(message.content)) > 1:
                content = f"{parent.content} {content}".strip()
                log.info(
                    f"[MERGE] Reply merged | Parent='{parent.content[:40]}' | Child='{message.content[:40]}'"
                )

    if not content:
        return

    best = find_best_match(content)
    if not best:
        log.info(
            f"[NO MATCH] msgID={message.id} | msg='{original_content}'"
        )
        return

    name, row, score, reason = best

    log.info(
        f"[MATCH] msgID={message.id} | Name='{name}' | Row={row} | Score={score:.1f} | Reason={reason} | Cleaned='{normalize_msg(content)}'"
    )

    result = await highlight_row(row, reason, name)

    if result == True:
        try:
            await message.add_reaction("✅")
            confirm = await message.reply(
                f"🟩 Highlighted **{name}** (row {row}) [{reason}]",
                mention_author=False
            )
            await asyncio.sleep(5)
            await confirm.delete()
        except Exception as e:
            log.warning(f"[CONFIRM ERROR] {e}")

    elif result == "already":
        try:
            await message.add_reaction("❌")
            note = await message.reply(
                f"⚠️ **{name}** already highlighted.",
                mention_author=False
            )
            await asyncio.sleep(5)
            await note.delete()
        except:
            pass

# ==========================================================
# MAIN LOOP (RECONNECT SAFE)
# ==========================================================

if __name__ == "__main__":
    while True:
        try:
            client.run(DISCORD_TOKEN)
        except Exception as e:
            log.error(f"[DISCORD ERROR] {e} | Reconnecting in 5s...")
            time.sleep(5)
