from dotenv import load_dotenv
load_dotenv()

import os, json, base64
import logging
from typing import Dict, Tuple, Optional
import discord
from discord.ext import commands

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

creds_b64 = os.environ["GOOGLE_CREDENTIALS_B64"]
creds_json = base64.b64decode(creds_b64).decode("utf-8")
creds_info = json.loads(creds_json)

credentials = Credentials.from_service_account_info(creds_info)

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("atd-flux-bot")

# ==========================================================
# ENV
# ==========================================================

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v

DISCORD_TOKEN = need("DISCORD_TOKEN")
COMMISH_ROLE_NAME = "LeComissioner"

# Hardcoded fallback (optional — used if env vars are set)
_HC_CHANNEL_ID   = os.getenv("DISCORD_CHANNEL_ID")
_HC_SHEET_ID     = os.getenv("GOOGLE_SHEET_ID")
_HC_WORKSHEET_GID = os.getenv("GOOGLE_WORKSHEET_GID")
_HC_CREDS_PATH   = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

HARDCODED_CONFIG: Dict[int, Dict] = {}
if _HC_CHANNEL_ID and _HC_SHEET_ID:
    HARDCODED_CONFIG[int(_HC_CHANNEL_ID)] = {
        "spreadsheet_id": _HC_SHEET_ID,
        "worksheet_gid": int(_HC_WORKSHEET_GID) if _HC_WORKSHEET_GID else None,
    }

# ==========================================================
# GOOGLE AUTH
# ==========================================================

creds = Credentials.from_service_account_file(
    _HC_CREDS_PATH,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)

# ==========================================================
# DYNAMIC TRACKS (persisted to flux_tracks.json)
# ==========================================================

TRACKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flux_tracks.json")

def load_tracks() -> Dict[int, Dict]:
    if os.path.exists(TRACKS_FILE):
        try:
            with open(TRACKS_FILE) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception as e:
            log.error(f"Failed to load flux_tracks.json: {e}")
    return {}

def save_tracks():
    try:
        with open(TRACKS_FILE, "w") as f:
            json.dump({str(k): v for k, v in dynamic_tracks.items()}, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save flux_tracks.json: {e}")

def get_all_configs() -> Dict[int, Dict]:
    return {**HARDCODED_CONFIG, **dynamic_tracks}

dynamic_tracks: Dict[int, Dict] = load_tracks()

# ==========================================================
# SHEET CACHE + LOOKUP
# ==========================================================

sheet_cache: Dict[int, Tuple] = {}

def get_ws(channel_id: int) -> Optional[Tuple]:
    if channel_id in sheet_cache:
        return sheet_cache[channel_id]
    cfg = get_all_configs().get(channel_id)
    if not cfg:
        return None
    sh = gc.open_by_key(cfg["spreadsheet_id"])
    if cfg.get("worksheet_name"):
        ws = sh.worksheet(cfg["worksheet_name"])
    elif cfg.get("worksheet_gid") is not None:
        ws = sh.get_worksheet_by_id(cfg["worksheet_gid"])
    else:
        ws = sh.get_worksheet(0)
    sheet_cache[channel_id] = (sh, ws)
    return sh, ws

# ==========================================================
# HELPERS
# ==========================================================

def ensure_columns(ws, target_col: int):
    if ws.col_count < target_col:
        ws.add_cols(target_col - ws.col_count)

def get_existing_rounds(header):
    rounds = []
    for h in header:
        if h.lower().startswith("round "):
            rounds.append(int(h.split(" ")[1]))
    return sorted(rounds)

def col_letter_by_name(header, name):
    if name not in header:
        raise RuntimeError(f"Column '{name}' not found")
    idx = header.index(name) + 1
    return rowcol_to_a1(1, idx)[0]

def has_commish_role(member: discord.Member) -> bool:
    return any(r.name == COMMISH_ROLE_NAME for r in member.roles)

# ==========================================================
# FORMULAS
# ==========================================================

ROUND_2_TEMPLATE = """
=IF(INDIRECT("{VAL}"&ROW())>44.5,
 INDIRECT("{VAL}"&ROW())+RANDBETWEEN(-{VOL},0),
 IF(INDIRECT("{VAL}"&ROW())>39.5,
  INDIRECT("{VAL}"&ROW())+RANDBETWEEN(-{VOL},1),
 IF(INDIRECT("{VAL}"&ROW())<1.5,
  INDIRECT("{VAL}"&ROW())+RAND()-0.5,
 IF(INDIRECT("{VAL}"&ROW())<3.5,
  INDIRECT("{VAL}"&ROW())+RAND()*2-1.1,
 IF(INDIRECT("{VAL}"&ROW())<10.5,
  INDIRECT("{VAL}"&ROW())+RAND()*4-2.1,
  INDIRECT("{VAL}"&ROW())+RAND()*6-3.1)))))
""".strip()

ROUND_3_PLUS_TEMPLATE = """
=IF(INDIRECT("{VAL}"&ROW())<1.5,
 IF(INDIRECT("{PREV}"&ROW())>3.5,
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-{VOL},1),
 IF(INDIRECT("{PREV}"&ROW())<1.5,
  INDIRECT("{PREV}"&ROW())+RAND()-0.5,
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-1,1))),
 IF(INDIRECT("{PREV}"&ROW())>44.5,
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-{VOL},0),
 IF(INDIRECT("{PREV}"&ROW())>39.5,
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-{VOL},1),
 IF(INDIRECT("{PREV}"&ROW())<1.5,
  INDIRECT("{PREV}"&ROW())+RAND()-0.5,
 IF(INDIRECT("{PREV}"&ROW())<3.5,
  INDIRECT("{PREV}"&ROW())+RAND()*2-1.1,
 IF(INDIRECT("{PREV}"&ROW())-3>INDIRECT("{VAL}"&ROW()),
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-{VOL},1),
 IF(INDIRECT("{PREV}"&ROW())>INDIRECT("{VAL}"&ROW()),
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-{VOL},2),
 IF(INDIRECT("{PREV}"&ROW())+8<INDIRECT("{VAL}"&ROW()),
  INDIRECT("{PREV}"&ROW())+RANDBETWEEN(-2,3),
 IF(INDIRECT("{PREV}"&ROW())<10.5,
  INDIRECT("{PREV}"&ROW())+RAND()*4-2.1,
  INDIRECT("{PREV}"&ROW())+RAND()*6-3.1)))))))))
""".strip()

# ==========================================================
# DISCORD
# ==========================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # Allow commands from any tracked channel
    if message.channel.id not in get_all_configs():
        # Still allow track management commands from any channel
        content = message.content.strip()
        if not (content.startswith("!fluxtrack") or
                content.startswith("!fluxuntrack") or
                content == "!fluxtracks" or
                content == "!fluxhelp"):
            return
    await bot.process_commands(message)

# ==========================================================
# TRACK COMMANDS
# ==========================================================

@bot.command()
async def fluxtrack(ctx, channel_id_str: str = None, sheet_id: str = None, *worksheet_parts):
    if not isinstance(ctx.author, discord.Member) or not has_commish_role(ctx.author):
        await ctx.send("❌ Only commissioners can use `!fluxtrack`.")
        return

    if not channel_id_str or not sheet_id:
        await ctx.send(
            "Usage: `!fluxtrack <channel-id> <sheet-id> [worksheet-name]`\n"
            "Example: `!fluxtrack 123456789 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms Round1`\n"
            "Omit worksheet name to use the first sheet."
        )
        return

    if not channel_id_str.isdigit():
        await ctx.send("❌ Invalid channel ID.")
        return

    target_channel_id = int(channel_id_str)
    worksheet_name = " ".join(worksheet_parts) if worksheet_parts else None

    try:
        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet(worksheet_name) if worksheet_name else sh.get_worksheet(0)
    except Exception as e:
        await ctx.send(f"❌ Could not open sheet: {e}")
        return

    dynamic_tracks[target_channel_id] = {
        "spreadsheet_id": sheet_id,
        "worksheet_name": ws.title,
    }
    sheet_cache.pop(target_channel_id, None)
    save_tracks()
    log.info(f"[FLUXTRACK] channel={target_channel_id} sheet={sheet_id} ws={ws.title} by={ctx.author}")
    await ctx.send(f"✅ Now tracking <#{target_channel_id}> → **{sh.title}** / **{ws.title}**")


@bot.command()
async def fluxuntrack(ctx, channel_id_str: str = None):
    if not isinstance(ctx.author, discord.Member) or not has_commish_role(ctx.author):
        await ctx.send("❌ Only commissioners can use `!fluxuntrack`.")
        return

    if not channel_id_str:
        await ctx.send("Usage: `!fluxuntrack <channel-id>`")
        return

    if not channel_id_str.isdigit():
        await ctx.send("❌ Invalid channel ID.")
        return

    target_channel_id = int(channel_id_str)
    if target_channel_id not in dynamic_tracks:
        await ctx.send("⚠️ That channel isn't in the dynamic track list.")
        return

    dynamic_tracks.pop(target_channel_id)
    sheet_cache.pop(target_channel_id, None)
    save_tracks()
    log.info(f"[FLUXUNTRACK] channel={target_channel_id} by={ctx.author}")
    await ctx.send(f"✅ Removed flux tracking for <#{target_channel_id}>.")


@bot.command()
async def fluxtracks(ctx):
    all_cfg = get_all_configs()
    if not all_cfg:
        await ctx.send("No channels are currently tracked.")
        return
    embed = discord.Embed(title="📋 Flux Tracked Channels", color=0x4A90E2)
    for ch_id, cfg in all_cfg.items():
        source = "hardcoded" if ch_id in HARDCODED_CONFIG and ch_id not in dynamic_tracks else "dynamic"
        embed.add_field(
            name=f"<#{ch_id}> ({ch_id})",
            value=f"Sheet: `{cfg['spreadsheet_id']}`\nWorksheet: `{cfg.get('worksheet_name', cfg.get('worksheet_gid', 'first sheet'))}`\n_{source}_",
            inline=False,
        )
    await ctx.send(embed=embed)

# ==========================================================
# FLUX COMMANDS
# ==========================================================

@bot.command()
async def flux(ctx, vol: int):
    user = ctx.author
    log.info(f"Flux attempt | user={user} | vol={vol} | channel={ctx.channel.id}")

    if not isinstance(user, discord.Member) or not has_commish_role(user):
        log.warning(f"Flux denied (not commish) | user={user}")
        await ctx.send(
            "Unfortunately, you are not a commish. "
            "Ping a commish to assist you in fluxing"
        )
        return

    if vol not in (3, 4, 5):
        await ctx.send("❌ Flux must be 3, 4, or 5. If you would like a different flux, please contact the developer.")
        return

    result = get_ws(ctx.channel.id)
    if not result:
        await ctx.send("❌ This channel is not linked to a sheet. Use `!fluxtrack` to set one up.")
        return
    sh, ws = result

    await ctx.send("⏳ **Flux is in progress…**")

    rows = ws.get_all_values()
    header = rows[0]
    row_count = len(rows)

    value_col = col_letter_by_name(header, "Value")

    existing = get_existing_rounds(header)
    next_round = 2 if not existing else max(existing) + 1

    if next_round > 10:
        await ctx.send("❌ Maximum round is 10.")
        return

    log.info(f"Flux started | round={next_round} | vol={vol} | user={user}")

    new_col = ws.col_count + 1
    ensure_columns(ws, new_col)
    ws.update_cell(1, new_col, f"Round {next_round}")
    new_letter = rowcol_to_a1(1, new_col)[0]

    if next_round == 2:
        formula = ROUND_2_TEMPLATE.format(VOL=vol, VAL=value_col)
    else:
        prev_idx = header.index(f"Round {next_round - 1}") + 1
        prev_letter = rowcol_to_a1(1, prev_idx)[0]
        formula = ROUND_3_PLUS_TEMPLATE.format(VOL=vol, VAL=value_col, PREV=prev_letter)

    ws.update(
        f"{new_letter}2:{new_letter}{row_count}",
        [[formula]] * (row_count - 1),
        value_input_option="USER_ENTERED"
    )

    ws.spreadsheet.batch_update({
        "requests": [{
            "copyPaste": {
                "source": {
                    "sheetId": ws.id,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": new_col - 1,
                    "endColumnIndex": new_col
                },
                "destination": {
                    "sheetId": ws.id,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": new_col - 1,
                    "endColumnIndex": new_col
                },
                "pasteType": "PASTE_VALUES"
            }
        }]
    })

    log.info(f"Flux completed | round={next_round} | vol={vol} | user={user}")
    await ctx.send(f"✅ **Round {next_round} Flux is done**")


@bot.command()
async def undoflux(ctx):
    user = ctx.author
    log.info(f"UndoFlux attempt | user={user}")

    if not isinstance(user, discord.Member) or not has_commish_role(user):
        log.warning(f"UndoFlux denied (not commish) | user={user}")
        await ctx.send(
            "Unfortunately, you are not a commish. "
            "Ping a commish to assist you in undoing a flux."
        )
        return

    result = get_ws(ctx.channel.id)
    if not result:
        await ctx.send("❌ This channel is not linked to a sheet. Use `!fluxtrack` to set one up.")
        return
    sh, ws = result

    rows = ws.get_all_values()
    header = rows[0]

    round_cols = []
    for idx, h in enumerate(header):
        if h.lower().startswith("round "):
            try:
                round_num = int(h.split(" ")[1])
                round_cols.append((round_num, idx + 1))
            except ValueError:
                pass

    if not round_cols:
        await ctx.send("❌ There are no rounds to undo.")
        return

    round_cols.sort()
    last_round, col_index = round_cols[-1]

    await ctx.send(f"⏳ **Undoing Round {last_round}…**")
    log.info(f"UndoFlux started | round={last_round} | user={user}")

    ws.delete_columns(col_index)

    log.info(f"UndoFlux completed | round={last_round} | user={user}")
    await ctx.send(f"✅ **Round {last_round} has been undone successfully**")


@bot.command()
async def fluxhelp(ctx):
    embed = discord.Embed(
        title="📊 ATD Flux Bot Help",
        colour=discord.Colour.blue()
    )

    embed.add_field(
        name="Purpose",
        value=(
            "• Automatically generates draft prices for each round\n"
            "• Uses the **exact same formulas** as manual Google Sheets fluxing\n"
            "• Creates a new round column and freezes values"
        ),
        inline=False
    )

    embed.add_field(
        name="Who Can Use Flux?",
        value=(
            "• Only users with the **LeComissioner** role\n"
            "• Non-commishes will be blocked from running Flux"
        ),
        inline=False
    )

    embed.add_field(
        name="Flux Commands",
        value=(
            "`!flux 3` – Generate next round with ±3 volatility\n"
            "`!flux 4` – Generate next round with ±4 volatility\n"
            "`!flux 5` – Generate next round with ±5 volatility\n"
            "`!undoflux` – Undo the most recent round\n"
            "`!fluxhelp` – Show this help message"
        ),
        inline=False
    )

    embed.add_field(
        name="Setup Commands",
        value=(
            "`!fluxtrack <channel-id> <sheet-id> [worksheet]` – Link a channel to a Google Sheet\n"
            "`!fluxuntrack <channel-id>` – Remove a channel's sheet link\n"
            "`!fluxtracks` – List all tracked channels"
        ),
        inline=False
    )

    embed.add_field(
        name="How Flux Works",
        value=(
            "1️⃣ Bot detects the next available round automatically\n"
            "2️⃣ Creates a new column named **Round X**\n"
            "3️⃣ Applies the correct formula:\n"
            " • **Round 2** → Round 2 formula\n"
            " • **Rounds 3–10** → Every-other-round formula\n"
            "4️⃣ Google Sheets calculates prices\n"
            "5️⃣ Values are **pasted as values only** (numbers won't change)"
        ),
        inline=False
    )

    embed.add_field(
        name="Important Rules",
        value=(
            "• Maximum round is **Round 10**\n"
            "• Flux value must be **3, 4, or 5**\n"
            "• Pricing is based on the **Value** column\n"
            "• Behaviour matches manual drag-down exactly"
        ),
        inline=False
    )

    embed.set_footer(
        text="⚠️ Only commishes can run Flux • Contact a commish or developer for help"
    )

    await ctx.send(embed=embed)

# ==========================================================
# RUN
# ==========================================================

bot.run(DISCORD_TOKEN)
