import os, json
import re
import logging
import asyncio
import time
from typing import Dict, List, Tuple

import discord
from discord import Intents, MessageType
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

load_dotenv()

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
CMD_TRACK        = "!track"
CMD_UNTRACK      = "!untrack"
CMD_TRACKS       = "!tracks"

# ==========================================================
# ROLE LOCK
# ==========================================================

COMMISH_ROLE_NAME = "LeComissioner"

def is_commish(member: discord.Member) -> bool:
    return any(role.name == COMMISH_ROLE_NAME for role in member.roles)

def is_command(text: str) -> bool:
    return text.startswith("!")

# ==========================================================
# CHANNEL/THREAD → SHEET CONFIG
# ==========================================================

CHANNEL_CONFIG: Dict[int, Dict] = {}

DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
TRACKS_FILE = os.path.join(DATA_DIR, "tracks.json")
STATE_FILE  = os.path.join(DATA_DIR, "state.json")

def load_tracks() -> Dict[int, Dict]:
    if os.path.exists(TRACKS_FILE):
        try:
            with open(TRACKS_FILE) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception as e:
            log.error(f"Failed to load tracks.json: {e}")
    return {}

def save_tracks():
    try:
        with open(TRACKS_FILE, "w") as f:
            json.dump({str(k): v for k, v in dynamic_tracks.items()}, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save tracks.json: {e}")

def get_all_configs() -> Dict[int, Dict]:
    """Merge hardcoded config with dynamic tracks (dynamic takes priority)."""
    return {**CHANNEL_CONFIG, **dynamic_tracks}

dynamic_tracks: Dict[int, Dict] = load_tracks()

def save_state():
    try:
        serializable = {}
        for ch_id, s in thread_state.items():
            serializable[str(ch_id)] = {
                "highlighted": list(s["highlighted"]),
                "stack": s["stack"],
                "redo_stack": s["redo_stack"],
                "pick_info": s["pick_info"],
            }
        with open(STATE_FILE, "w") as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for ch_id_str, s in data.items():
            ch_id = int(ch_id_str)
            thread_state[ch_id] = {
                "highlighted": set(s["highlighted"]),
                "stack": [tuple(x) for x in s["stack"]],
                "redo_stack": [tuple(x) for x in s["redo_stack"]],
                "pick_info": {k: tuple(v) for k, v in s["pick_info"].items()},
            }
        log.info(f"[STATE] Loaded state for {len(thread_state)} channels")
    except Exception as e:
        log.error(f"Failed to load state: {e}")

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

async def batch_update_with_retry(sh, body: dict, retries: int = 4):
    """Call sh.batch_update with exponential backoff on 429 rate-limit errors."""
    delay = 15
    for attempt in range(retries):
        try:
            await asyncio.to_thread(sh.batch_update, body)
            return
        except gspread.exceptions.APIError as e:
            if e.response.status_code == 429 and attempt < retries - 1:
                log.warning(f"Rate limited (429), retrying in {delay}s (attempt {attempt + 1}/{retries})")
                await asyncio.sleep(delay)
                delay *= 2
            else:
                raise

async def apply_highlight(sh, ws, row: int, color: dict):
    start = col_to_index(ROW_START_COL) - 1
    end = col_to_index(ROW_END_COL)
    await batch_update_with_retry(sh, {
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
    })

async def clear_highlight(sh, ws, row: int):
    start = col_to_index(ROW_START_COL) - 1
    end = col_to_index(ROW_END_COL)
    await batch_update_with_retry(sh, {
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
    })

# ==========================================================
# STATE
# ==========================================================

thread_state: Dict[int, Dict] = {}
sheet_cache: Dict[int, tuple] = {}
player_cache: Dict[int, tuple] = {}

load_state()

HIGHLIGHT_COLOR = {"red": 0.286, "green": 0.518, "blue": 0.910}

def get_state(channel_id: int):
    if channel_id not in thread_state:
        thread_state[channel_id] = {
            "highlighted": set(),
            "stack": [],
            "redo_stack": [],
            "pick_info": {},  # player_key -> (pick_number, picker_name)
        }
    return thread_state[channel_id]

def get_sheet(channel_id: int):
    if channel_id in sheet_cache:
        return sheet_cache[channel_id]
    cfg = get_all_configs().get(channel_id)
    if not cfg:
        return None
    sh = gc.open_by_key(cfg["spreadsheet_id"])
    ws = sh.worksheet(cfg["worksheet_name"]) if cfg.get("worksheet_name") else sh.get_worksheet(0)
    sheet_cache[channel_id] = (sh, ws)
    return sh, ws

def load_players(channel_id: int, ws):
    if channel_id in player_cache:
        return player_cache[channel_id]
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
    player_cache[channel_id] = (names, row_map, keys, key_to_name)
    log.info(f"[CHANNEL {channel_id}] Loaded {len(names)} players")
    return player_cache[channel_id]

def find_best_match(text: str, names, row_map, keys, key_to_name):
    msg = normalize(text)
    log.info(f"[MATCH] Normalized text: '{msg}'")
    
    # Check for direct substring match
    for n in names:
        normalized_n = normalize(n)
        if normalized_n in msg:
            log.info(f"[MATCH] Direct substring match: '{n}' -> '{normalized_n}' in '{msg}'")
            return n, row_map[n]
    
    # Try fuzzy matching
    hit = process.extractOne(msg, keys, scorer=fuzz.token_sort_ratio)
    if hit:
        log.info(f"[MATCH] Fuzzy match result: '{hit[0]}' with score {hit[1]}")
    
    if not hit or hit[1] < FUZZY_THRESHOLD:
        log.info(f"[MATCH] No fuzzy match above threshold {FUZZY_THRESHOLD}")
        return None
    
    name = key_to_name[hit[0]]
    return name, row_map[name]

# ==========================================================
# DISCORD BOT
# ==========================================================

intents = Intents.default()
intents.message_content = True
intents.messages = True
client = discord.Client(intents=intents)

@client.event
async def on_message(message: discord.Message):
    global ROW_END_COL

    if message.author.bot:
        return
    
    # Allow replies (remove the strict message type check)
    # Only filter out system messages, not replies
    if message.type not in [MessageType.default, MessageType.reply]:
        return
    
    # Check if it's a thread OR a text channel
    channel = message.channel
    channel_id = channel.id

    # ================= TRACK COMMANDS (work in any channel) =================
    if message.content.strip().startswith(CMD_TRACK + " ") or message.content.strip() == CMD_TRACK:
        if not isinstance(message.author, discord.Member) or not is_commish(message.author):
            await message.reply("❌ Only commissioners can use `!track`.")
            return
        parts = message.content.strip().split()
        if len(parts) < 3:
            await message.reply(
                "Usage: `!track <channel-id> <sheet-id> [worksheet-name]`\n"
                "Example: `!track 123456789 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms Sheet1`\n"
                "Omit worksheet name to use the first sheet."
            )
            return
        target_channel_id = int(parts[1]) if parts[1].isdigit() else None
        if not target_channel_id:
            await message.reply("❌ Invalid channel ID.")
            return
        sheet_id = parts[2]
        worksheet_name = parts[3] if len(parts) >= 4 else None
        # Validate by opening the sheet
        try:
            sh = gc.open_by_key(sheet_id)
            ws = sh.worksheet(worksheet_name) if worksheet_name else sh.get_worksheet(0)
        except Exception as e:
            await message.reply(f"❌ Could not open sheet: {e}")
            return
        dynamic_tracks[target_channel_id] = {
            "spreadsheet_id": sheet_id,
            "worksheet_name": ws.title,
        }
        # Invalidate caches for this channel
        sheet_cache.pop(target_channel_id, None)
        player_cache.pop(target_channel_id, None)
        save_tracks()
        log.info(f"[TRACK] channel={target_channel_id} sheet={sheet_id} ws={ws.title} by={message.author}")
        await message.reply(
            f"✅ Now tracking <#{target_channel_id}> → **{sh.title}** / **{ws.title}**"
        )
        return

    if message.content.strip().startswith(CMD_UNTRACK + " ") or message.content.strip() == CMD_UNTRACK:
        if not isinstance(message.author, discord.Member) or not is_commish(message.author):
            await message.reply("❌ Only commissioners can use `!untrack`.")
            return
        parts = message.content.strip().split()
        if len(parts) < 2:
            await message.reply("Usage: `!untrack <channel-id>`")
            return
        target_channel_id = int(parts[1]) if parts[1].isdigit() else None
        if not target_channel_id:
            await message.reply("❌ Invalid channel ID.")
            return
        if target_channel_id not in dynamic_tracks:
            await message.reply("⚠️ That channel isn't in the dynamic track list.")
            return
        dynamic_tracks.pop(target_channel_id)
        sheet_cache.pop(target_channel_id, None)
        player_cache.pop(target_channel_id, None)
        save_tracks()
        log.info(f"[UNTRACK] channel={target_channel_id} by={message.author}")
        await message.reply(f"✅ Removed tracking for <#{target_channel_id}>.")
        return

    if message.content.strip() == CMD_TRACKS:
        all_cfg = get_all_configs()
        if not all_cfg:
            await message.reply("No channels are currently tracked.")
            return
        embed = discord.Embed(title="📋 Tracked Channels", color=0x4A90E2)
        for ch_id, cfg in all_cfg.items():
            source = "hardcoded" if ch_id in CHANNEL_CONFIG and ch_id not in dynamic_tracks else "dynamic"
            embed.add_field(
                name=f"<#{ch_id}> ({ch_id})",
                value=f"Sheet: `{cfg['spreadsheet_id']}`\nWorksheet: `{cfg.get('worksheet_name', 'first sheet')}`\n_{source}_",
                inline=False,
            )
        await message.reply(embed=embed)
        return

    # Check if this channel/thread is configured
    sheet = get_sheet(channel_id)
    if not sheet:
        log.info(f"[DEBUG] No sheet config for channel {channel_id}")
        return

    # Log the message for debugging
    log.info(f"[DEBUG] Message received in {channel_id}: '{message.content}' type={message.type} author={message.author}")

    # ----------------------------------------------------------
    # IGNORE GIFS / IMAGES / VIDEOS / EMBEDS / LINK-ONLY
    # ----------------------------------------------------------
    # ONLY skip if message has NO TEXT CONTENT and has attachments/embeds
    # Messages with BOTH text and attachments should be processed
    
    has_text = bool(message.content.strip())
    has_only_url = re.fullmatch(r"https?://\S+", message.content.strip()) if message.content else False
    
    # Skip if it's ONLY a URL with no other text
    if has_only_url:
        log.info(f"[DEBUG] Skipping - is a URL only")
        return
    
    # Skip if it has embeds but no text (like link previews)
    if message.embeds and not has_text:
        log.info(f"[DEBUG] Skipping - has embeds but no text")
        return
    
    # Skip if it has attachments but no text (like image-only posts)
    if message.attachments and not has_text:
        log.info(f"[DEBUG] Skipping - has attachments but no text")
        return
    
    # If we get here, we have text content to process (even if there are also attachments)

    sh, ws = sheet
    names, row_map, keys, key_to_name = load_players(channel_id, ws)
    state = get_state(channel_id)

    # ============ KEY FIX: Check both message content AND referenced message ============
    content_to_check = MENTION_RE.sub("", message.content).strip()
    
    # Debug log original content
    log.info(f"[DEBUG] Original content: '{content_to_check}'")
    
    # If message is a reply, also check the referenced message for player names
    if message.reference:
        log.info(f"[DEBUG] Message is a reply, reference ID: {message.reference.message_id}")
        
        try:
            # Try to fetch the referenced message
            if message.reference.resolved:
                referenced_message = message.reference.resolved
                log.info(f"[DEBUG] Resolved referenced message: {type(referenced_message)}")
            else:
                # Fetch it from the channel
                referenced_message = await channel.fetch_message(message.reference.message_id)
                log.info(f"[DEBUG] Fetched referenced message")
            
            if isinstance(referenced_message, discord.Message):
                # Combine current message content with referenced message content
                referenced_content = MENTION_RE.sub("", referenced_message.content).strip()
                log.info(f"[DEBUG] Referenced content: '{referenced_content}'")
                content_to_check = f"{content_to_check} {referenced_content}"
        except Exception as e:
            log.error(f"[DEBUG] Failed to get referenced message: {e}")
    # ============ END FIX ============

    log.info(f"[DEBUG] Final content to check: '{content_to_check}'")
    
    content = content_to_check  # Use the combined content for processing

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
                f"channel={channel_id} command='{content}'"
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
            name="Draft Commands",
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
        embed.add_field(
            name="Setup Commands",
            value=(
                "`!track <channel-id> <sheet-id> [worksheet]` – Link a channel to a Google Sheet\n"
                "`!untrack <channel-id>` – Remove a channel's sheet link\n"
                "`!tracks` – List all tracked channels"
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

    # ================= ENDHIGHLIGHT =================
    if content.startswith(CMD_ENDHIGHLIGHT):
        if not is_commish(message.author):
            await message.reply("❌ Only commissioners can change the highlight range.")
            return
        
        parts = content.split()
        if len(parts) != 2:
            await message.reply(f"Usage: {CMD_ENDHIGHLIGHT} <column letter>\nExample: `{CMD_ENDHIGHLIGHT} L`")
            return
        
        new_end_col = parts[1].upper()
        
        # Validate it's a single letter A-Z
        if not re.match(r'^[A-Z]$', new_end_col):
            await message.reply("❌ Please provide a single column letter (A-Z).")
            return
        
        # Update the global variable
        global ROW_END_COL
        ROW_END_COL = new_end_col
        
        log.info(f"[ENDHIGHLIGHT] channel={channel_id} new_end_col={new_end_col} by={message.author}")
        await message.reply(f"✅ Highlight end column changed to **{new_end_col}**\nUse `!rehighlight` to apply this to all existing picks.")
        return

    # ================= RESET =================
    if content == CMD_RESET:
        state["highlighted"].clear()
        state["stack"].clear()
        state["redo_stack"].clear()
        save_state()
        log.info(f"[RESET] channel={channel_id} by={message.author}")
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
        save_state()
        log.info(f"[UNDO] channel={channel_id} player='{name}' by={message.author}")
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
        save_state()
        log.info(f"[REDO] channel={channel_id} player='{name}' by={message.author}")
        await message.reply(f"🔁 Redid highlight for **{name}**")
        return

    # ================= REHIGHLIGHT =================
    if content == CMD_REHIGHLIGHT:
        if not is_commish(message.author):
            await message.reply("❌ Only commissioners can use the rehighlight command.")
            return
        
        if not state["stack"]:
            await message.reply("⚠️ No picks have been made yet.")
            return
        
        await message.reply("🔄 Re-highlighting all picks...")
        
        # Re-apply highlight to all players in the stack
        for name, row in state["stack"]:
            await apply_highlight(sh, ws, row, HIGHLIGHT_COLOR)
        
        log.info(f"[REHIGHLIGHT] channel={channel_id} re-highlighted {len(state['stack'])} picks by={message.author}")
        await message.reply(f"✅ Re-highlighted **{len(state['stack'])}** picks.")
        return

    # ================= FORCE COMMAND =================
    if content.startswith(CMD_FORCE):
        if not is_commish(message.author):
            await message.reply("❌ Only commissioners can use the force command.")
            return
        
        parts = content.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(f"Usage: `{CMD_FORCE} <player name>`")
            return
        
        force_name = parts[1]
        match = find_best_match(force_name, names, row_map, keys, key_to_name)
        if not match:
            await message.reply(f"❌ Could not find player matching '{force_name}'")
            return
        
        name, row = match
        key = f"{ws.title}:{name.lower()}"
        
        if key in state["highlighted"]:
            pick_number, picker_name = state["pick_info"].get(key, ("?", "unknown"))
            await message.reply(
                f"❌ **{name}** has already been selected at pick **{pick_number}** "
                f"by **{picker_name}**."
            )
            return
        
        pick_number = len(state["stack"]) + 1
        picker_name = message.author.display_name
        
        state["highlighted"].add(key)
        state["stack"].append((name, row))
        state["redo_stack"].clear()
        state["pick_info"][key] = (pick_number, picker_name)
        
        await apply_highlight(sh, ws, row, HIGHLIGHT_COLOR)
        save_state()
        await message.reply(f"✅ Force highlighted **{name}** at pick **{pick_number}**")
        log.info(f"[FORCE] channel={channel_id} player='{name}' by={message.author}")
        return

    # ================= PICK FLOW =================
    match = find_best_match(content, names, row_map, keys, key_to_name)
    
    if not match:
        log.info(f"[DEBUG] No match found for content: '{content}'")
        if names:
            log.info(f"[DEBUG] Available names sample: {names[:10]}")
        return

    name, row = match
    log.info(f"[DEBUG] Match found: '{name}' at row {row}")
    
    key = f"{ws.title}:{name.lower()}"

    if key in state["highlighted"]:
        pick_number, picker_name = state["pick_info"].get(key, ("?", "unknown"))

        log.info(
            f"[DUPLICATE] channel={channel_id} player='{name}' "
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
        f"[HIGHLIGHT] channel={channel_id} sheet={ws.title} "
        f"player='{name}' row={row} by={message.author}"
    )

    await apply_highlight(sh, ws, row, HIGHLIGHT_COLOR)
    save_state()
    await message.add_reaction("✅")

@client.event
async def on_ready():
    log.info(f"Connected as {client.user}")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)