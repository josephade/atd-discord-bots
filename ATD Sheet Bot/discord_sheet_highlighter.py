import os, json, base64
import re
import logging
import asyncio
from typing import Dict, List, Tuple

import discord
from discord import Intents, MessageType
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process


creds_b64 = os.environ["GOOGLE_CREDENTIALS_B64"]
creds_json = base64.b64decode(creds_b64).decode("utf-8")
creds_info = json.loads(creds_json)

credentials = Credentials.from_service_account_info(creds_info)

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
# COMMANDS
# ==========================================================

CMD_HELP         = "!helpatd"
CMD_RESET        = "!newatd"
CMD_STATUS       = "!status"
CMD_UNDO         = "!undo"
CMD_REDO         = "!redo"
CMD_FORCE        = "!force"
CMD_COLOR        = "!changehexcolour"
CMD_ENDHIGHLIGHT = "!endhighlight"
CMD_REHIGHLIGHT  = "!rehighlight"

# ==========================================================
# ROLE LOCK
# ==========================================================

COMMISH_ROLE_NAME = "LeComissioner"

def is_commish(member: discord.Member) -> bool:
    return any(role.name == COMMISH_ROLE_NAME for role in member.roles)

def is_command(text: str) -> bool:
    return text.startswith("!")

# ==========================================================
# THREAD → SHEET CONFIG
# ==========================================================

THREAD_CONFIG = {
    1465444677141528666: {
        "spreadsheet_id": "1CQyO93HKc5VlXsqS48dnPlkac153TDDoUsiqyhJwwOI",
        "worksheet_name": "ADP",
    },
}

# ==========================================================
# GOOGLE AUTH
# ==========================================================

creds = Credentials.from_service_account_file(
    os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json"),
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
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

async def apply_highlight(sh, ws, row: int, color: dict):
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
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }]
        }
    )

async def clear_highlight(sh, ws, row: int):
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
                    "cell": {"userEnteredFormat": {}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }]
        }
    )

# ==========================================================
# STATE
# ==========================================================

thread_state: Dict[int, Dict] = {}
sheet_cache: Dict[int, tuple] = {}
player_cache: Dict[int, tuple] = {}

HIGHLIGHT_COLOR = {"red": 0.286, "green": 0.518, "blue": 0.910}

def get_state(thread_id: int):
    if thread_id not in thread_state:
        thread_state[thread_id] = {
            "highlighted": set(),
            "stack": [],
            "redo_stack": [],
            "pick_info": {},  # player_key -> (pick_number, picker_name)
        }
    return thread_state[thread_id]

def get_sheet(thread_id: int):
    if thread_id in sheet_cache:
        return sheet_cache[thread_id]
    cfg = THREAD_CONFIG.get(thread_id)
    if not cfg:
        return None
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
    global ROW_END_COL

    if message.author.bot:
        return
    if message.type != MessageType.default:
        return
    if not isinstance(message.channel, discord.Thread):
        return

    # ----------------------------------------------------------
    # IGNORE GIFS / IMAGES / VIDEOS / EMBEDS / LINK-ONLY
    # ----------------------------------------------------------
    if message.attachments:
        return
    if message.embeds:
        return
    if re.fullmatch(r"https?://\S+", message.content.strip()):
        return

    thread_id = message.channel.id
    sheet = get_sheet(thread_id)
    if not sheet:
        return

    sh, ws = sheet
    names, row_map, keys, key_to_name = load_players(thread_id, ws)
    state = get_state(thread_id)

    content = MENTION_RE.sub("", message.content).strip()

    # ----------------------------------------------------------
    # ROLE LOCK — COMMANDS ONLY
    # ----------------------------------------------------------
    if is_command(content):
        if not isinstance(message.author, discord.Member) or not is_commish(message.author):
            await message.reply(
                "Unfortunately, you are not a commish. "
                "Apply to be a commish with Soapz and then try again"
            )
            log.info(
                f"[ROLE_BLOCK] user={message.author} ({message.author.id}) "
                f"thread={thread_id} command='{content}'"
            )
            return

    # ================= HELP =================
    if content == CMD_HELP:
        embed = discord.Embed(title="📘 ATD Highlight Bot Help", color=0x4A90E2)
        embed.add_field(
            name="Purpose",
            value="• Detects draft picks in chat\n• Highlights the corresponding player row in Google Sheets",
            inline=False,
        )
        embed.add_field(
            name="Matching Priority",
            value="1️⃣ Full name match\n2️⃣ Fuzzy match (handles typos)",
            inline=False,
        )
        embed.add_field(
            name="Commands",
            value=(
                "`!newatd` – Reset bot memory for a new draft\n"
                "`!status` – Show draft progress\n"
                "`!undo` – Undo last highlighted pick\n"
                "`!redo` – Redo last undone pick\n"
                "`!endhighlight <column>` – Change highlight end column\n"
                "`!rehighlight` – Re-apply highlight to all picked players\n"
                "`!force <name>` – Force highlight a player\n"
                "`!changehexcolour <hex>` – Change highlight colour\n"
                "`!helpatd` – Show this help"
            ),
            inline=False,
        )
        embed.set_footer(text="⚠️ Always run !newatd before starting a new ATD")
        await message.reply(embed=embed)
        return

    # ================= STATUS =================
    if content == CMD_STATUS:
        await message.reply(f"📊 Highlighted: {len(state['stack'])}")
        return

    # ================= RESET =================
    if content == CMD_RESET:
        state["highlighted"].clear()
        state["stack"].clear()
        state["redo_stack"].clear()
        log.info(f"[RESET] thread={thread_id} by={message.author}")
        await message.reply("🧹 ATD memory reset.")
        return

    # ================= UNDO =================
    if content == CMD_UNDO:
        if not state["stack"]:
            await message.reply("⚠️ Nothing to undo.")
            return
        name, row = state["stack"].pop()
        state["highlighted"].discard(f"{ws.title}:{name.lower()}")
        state["pick_info"].pop(name.lower(), None)
        state["redo_stack"].append((name, row))
        await clear_highlight(sh, ws, row)
        log.info(f"[UNDO] thread={thread_id} player='{name}' by={message.author}")
        await message.reply(f"↩️ Undid highlight for **{name}**")
        return

    # ================= REDO =================
    if content == CMD_REDO:
        if not state["redo_stack"]:
            await message.reply("⚠️ Nothing to redo.")
            return
        name, row = state["redo_stack"].pop()
        state["highlighted"].add(f"{ws.title}:{name.lower()}")
        state["stack"].append((name, row))
        pick_number = len(state["stack"])
        state["pick_info"][name.lower()] = (pick_number, message.author.display_name)
        await apply_highlight(sh, ws, row, HIGHLIGHT_COLOR)
        log.info(f"[REDO] thread={thread_id} player='{name}' by={message.author}")
        await message.reply(f"🔁 Redid highlight for **{name}**")
        return

    # ================= PICK FLOW =================
    match = find_best_match(content, names, row_map, keys, key_to_name)
    if not match:
        return

    name, row = match
    key = f"{ws.title}:{name.lower()}"

    if key in state["highlighted"]:
        pick_number, picker_name = state["pick_info"].get(key, ("?", "unknown"))

        log.info(
            f"[DUPLICATE] thread={thread_id} player='{name}' "
            f"pick={pick_number} by={picker_name}"
        )

        await message.reply(
            f"❌ **{name}** has already been selected at pick **{pick_number}** "
            f"by **{picker_name}**. Please check #atd-sheet"
        )
        return


    pick_number = len(state["stack"]) + 1
    picker_name = message.author.display_name

    state["highlighted"].add(key)
    state["stack"].append((name, row))
    state["redo_stack"].clear()

    state["pick_info"][key] = (pick_number, picker_name)

    log.info(
        f"[HIGHLIGHT] thread={thread_id} sheet={ws.title} "
        f"player='{name}' row={row} by={message.author}"
    )

    await apply_highlight(sh, ws, row, HIGHLIGHT_COLOR)
    await message.add_reaction("✅")

@client.event
async def on_ready():
    log.info(f"Connected as {client.user}")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
