from dotenv import load_dotenv
load_dotenv()

import os, json, base64
import logging
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
DISCORD_CHANNEL_ID = int(need("DISCORD_CHANNEL_ID"))
SHEET_ID = need("GOOGLE_SHEET_ID")
WORKSHEET_GID = int(need("GOOGLE_WORKSHEET_GID"))
CREDS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service_account.json")

COMMISH_ROLE_NAME = "LeComissioner"

# ==========================================================
# GOOGLE SHEETS
# ==========================================================

creds = Credentials.from_service_account_file(
    CREDS_PATH,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID)
ws = sheet.get_worksheet_by_id(WORKSHEET_GID)

# ==========================================================
# HELPERS
# ==========================================================

def ensure_columns(target_col: int):
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
# FORMULAS (ROW-SAFE, LOGIC UNCHANGED)
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
    if message.channel.id != DISCORD_CHANNEL_ID:
        return
    await bot.process_commands(message)

# ==========================================================
# COMMAND
# ==========================================================

@bot.command()
async def flux(ctx, vol: int):
    user = ctx.author

    log.info(f"Flux attempt | user={user} | vol={vol}")

    # Role check
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

    log.info(
        f"Flux started | round={next_round} | vol={vol} | user={user}"
    )

    # ------------------------------------------------------
    # Create column
    # ------------------------------------------------------
    new_col = ws.col_count + 1
    ensure_columns(new_col)
    ws.update_cell(1, new_col, f"Round {next_round}")
    new_letter = rowcol_to_a1(1, new_col)[0]

    # ------------------------------------------------------
    # Build formula
    # ------------------------------------------------------
    if next_round == 2:
        formula = ROUND_2_TEMPLATE.format(
            VOL=vol,
            VAL=value_col
        )
    else:
        prev_idx = header.index(f"Round {next_round - 1}") + 1
        prev_letter = rowcol_to_a1(1, prev_idx)[0]
        formula = ROUND_3_PLUS_TEMPLATE.format(
            VOL=vol,
            VAL=value_col,
            PREV=prev_letter
        )

    # ------------------------------------------------------
    # Apply formula
    # ------------------------------------------------------
    ws.update(
        f"{new_letter}2:{new_letter}{row_count}",
        [[formula]] * (row_count - 1),
        value_input_option="USER_ENTERED"
    )

    # ------------------------------------------------------
    # Freeze values
    # ------------------------------------------------------
    ws.spreadsheet.batch_update({
        "requests": [{
            "copyPaste": {
                "source": {
                    "sheetId": WORKSHEET_GID,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": new_col - 1,
                    "endColumnIndex": new_col
                },
                "destination": {
                    "sheetId": WORKSHEET_GID,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": new_col - 1,
                    "endColumnIndex": new_col
                },
                "pasteType": "PASTE_VALUES"
            }
        }]
    })

    log.info(
        f"Flux completed | round={next_round} | vol={vol} | user={user}"
    )

    await ctx.send(f"✅ **Round {next_round} Flux is done**")

@bot.command()
async def undoflux(ctx):
    user = ctx.author

    log.info(f"UndoFlux attempt | user={user}")

    # Role check
    if not isinstance(user, discord.Member) or not has_commish_role(user):
        log.warning(f"UndoFlux denied (not commish) | user={user}")
        await ctx.send(
            "Unfortunately, you are not a commish. "
            "Ping a commish to assist you in undoing a flux."
        )
        return

    rows = ws.get_all_values()
    header = rows[0]

    # Find all Round columns
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

    # Get latest round
    round_cols.sort()
    last_round, col_index = round_cols[-1]

    await ctx.send(f"⏳ **Undoing Round {last_round}…**")

    log.info(
        f"UndoFlux started | round={last_round} | user={user}"
    )

    # Delete column
    ws.delete_columns(col_index)

    log.info(
        f"UndoFlux completed | round={last_round} | user={user}"
    )

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
        name="Commands",
        value=(
            "`!flux 3` – Generate next round with ±3 volatility\n"
            "`!flux 4` – Generate next round with ±4 volatility\n"
            "`!flux 5` – Generate next round with ±5 volatility\n"
            "`!fluxhelp` – Show this help message"
        ),
        inline=False
    )

    embed.add_field(
        name="How Flux Works",
        value=(
            "1️⃣ Bot detects the next available round automatically\n"
            "2️⃣ Creates a new column named **Round X**\n"
            "3️⃣ Applies the correct formula:\n"
            " • **Round 2** → Round 2 formula\n"
            " • **Rounds 3–10** → Every-other-round formula\n"
            "4️⃣ Google Sheets calculates prices\n"
            "5️⃣ Values are **pasted as values only** (numbers won’t change)"
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

    embed.add_field(
        name="Status Messages",
        value=(
            "⏳ *Flux is in progress…* – Bot is working\n"
            "✅ *Round X Flux is done* – Flux completed successfully"
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
