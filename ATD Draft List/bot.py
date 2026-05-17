import asyncio
import json
import logging
import os
import re
import time

import discord
from discord.ext import commands
from googleapiclient.discovery import build
from oauth2client.service_account import ServiceAccountCredentials

from config import (
    DISCORD_TOKEN,
    DRAFT_CHANNEL_ID,
    TIMER_BOT_ID,
    OWNER_ID,
    SPREADSHEET_ID,
    PLAYERS_SHEET_NAME,
    SERVICE_ACCOUNT_FILE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Persistence ───────────────────────────────────────────────────────────────
# Schema: { "<user_id>": { "enabled": bool, "emoji": str, "picks": [ {player, year, price}, ... ] } }
DATA_FILE = "/data/picklists.json"


def _load_data() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_data(data: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


_data = _load_data()


def _ensure_user(uid: str):
    if uid not in _data:
        _data[uid] = {"enabled": True, "emoji": "", "picks": []}


# ── Google Sheets — background-color availability check ──────────────────────
_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _build_sheets_service():
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, _SCOPES)
    return build("sheets", "v4", credentials=creds)


def _is_taken(cell_data: dict) -> bool:
    """Return True if the cell has a non-white background (= player already drafted).
    White = available, any color fill (blue, etc.) = taken."""
    fmt = cell_data.get("effectiveFormat", {})
    bg  = fmt.get("backgroundColor", {})
    # Empty dict means no explicit fill → treat as white → available
    r = bg.get("red",   1.0)
    g = bg.get("green", 1.0)
    b = bg.get("blue",  1.0)
    return not (r > 0.9 and g > 0.9 and b > 0.9)


_availability_cache: dict | None = None
_availability_cache_ts: float = 0.0
_AVAILABILITY_TTL = 60  # seconds


def _fetch_availability(force: bool = False) -> dict:
    """
    Read the players sheet with grid data and return a mapping:
        player_name_lower -> True (available) | False (blacked out / taken)
    Results are cached for 60 seconds to avoid redundant API calls.
    """
    global _availability_cache, _availability_cache_ts
    now = time.monotonic()
    if not force and _availability_cache is not None and (now - _availability_cache_ts) < _AVAILABILITY_TTL:
        return _availability_cache

    service = _build_sheets_service()
    result  = service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        ranges=[f"{PLAYERS_SHEET_NAME}!A:D"],
        includeGridData=True,
    ).execute()

    availability: dict[str, bool] = {}
    sheets = result.get("sheets", [])
    if sheets:
        for row in sheets[0].get("data", [{}])[0].get("rowData", []):
            for cell in row.get("values", []):
                text = (cell.get("formattedValue") or "").strip()
                if text:
                    availability[text.lower()] = not _is_taken(cell)

    _availability_cache = availability
    _availability_cache_ts = now
    return availability


def _player_available(player_name: str, availability: dict) -> bool:
    """Check availability — exact match first, then partial."""
    name_lower = player_name.lower().strip()

    if name_lower in availability:
        return availability[name_lower]

    # Partial: pick-list name is substring of sheet cell, or vice versa
    for sheet_name, avail in availability.items():
        if name_lower in sheet_name or sheet_name in name_lower:
            return avail

    # Not found — log and assume available so we don't incorrectly skip
    log.warning("Player '%s' not found in sheet — assuming available", player_name)
    return True


# ── Pick text helpers ─────────────────────────────────────────────────────────
_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
_YEAR_RE  = re.compile(r"'(\d{2})\b|(\d{4})-(\d{2,4})\b|\b(\d{4})\b")


def _parse_pick_line(text: str) -> dict:
    """Parse 'LeBron James '23 $5' → {player, year, price}."""
    year  = None
    price = None

    # Strip leading "N." numbering
    text = re.sub(r"^\d+\.\s*", "", text.strip())

    pm = _PRICE_RE.search(text)
    if pm:
        price = int(float(pm.group(1)))
        text  = (text[: pm.start()] + text[pm.end() :]).strip()

    ym = _YEAR_RE.search(text)
    if ym:
        if ym.group(1):
            end     = int(ym.group(1))
            century = 2000 if end <= 45 else 1900
            year    = f"{century + end - 1}-{ym.group(1)}"
        elif ym.group(2) and ym.group(3):
            year = f"{ym.group(2)}-{ym.group(3)}"
        elif ym.group(4):
            year = ym.group(4)
        text = (text[: ym.start()] + text[ym.end() :]).strip()

    return {"player": text, "year": year, "price": price}


def _format_pick(pick: dict) -> str:
    parts = [pick["player"]]
    if pick.get("year"):
        parts.append(f"'{pick['year'][-2:]}")
    if pick.get("price") is not None:
        parts.append(f"${pick['price']}")
    return " ".join(parts)


def _is_valid_emoji(s: str) -> bool:
    """Return True if s looks like a real emoji (custom or text), not corrupted data."""
    if not s:
        return False
    s = s.strip()
    if re.match(r'^<a?:[^:]+:\d+>$', s):   # <:name:id> or <a:name:id>
        return True
    if re.match(r'^:[A-Za-z0-9_~]+:$', s):  # :text_emoji:
        return True
    return False


def _build_pick_message(pick_num: int, emoji: str, pick: dict) -> str:
    parts = [f"{pick_num}."]
    if emoji:
        parts.append(emoji)
    parts.append(pick["player"])
    if pick.get("year"):
        parts.append(f"'{pick['year'][-2:]}")
    if pick.get("price") is not None:
        parts.append(f"${pick['price']}")
    return " ".join(parts)


# ── Auto-pick logic ───────────────────────────────────────────────────────────
async def _do_auto_pick(channel: discord.TextChannel, user: discord.User, pick_num: int):
    uid       = str(user.id)
    user_data = _data.get(uid, {})

    if not user_data.get("enabled", True):
        log.info("AUTO-PICK | %s | paused — skipping", user.display_name)
        return

    picks = user_data.get("picks", [])
    if not picks:
        log.warning("AUTO-PICK | %s | pick list empty", user.display_name)
        try:
            await user.send(
                f"⚠️ It's your turn (Pick **#{pick_num}**) but your pick list is **empty**!\n"
                f"You need to respond manually in the draft channel."
            )
        except Exception:
            pass
        return

    emoji = user_data.get("emoji", "")

    # Fetch sheet availability (force-refresh — stale data could cause a bad auto-pick)
    try:
        availability = _fetch_availability(force=True)
        log.info("AUTO-PICK | fetched %d player entries from sheet", len(availability))
    except Exception as e:
        log.error("AUTO-PICK | sheet fetch failed: %s", e)
        availability = {}

    # Walk list — find first available player
    chosen_idx  = None
    chosen_pick = None
    skipped     = []

    for idx, pick in enumerate(picks):
        if _player_available(pick["player"], availability):
            chosen_idx  = idx
            chosen_pick = pick
            break
        else:
            skipped.append(pick["player"])
            log.info("AUTO-PICK | %s | '%s' already taken — skipping", user.display_name, pick["player"])

    if chosen_pick is None:
        log.warning("AUTO-PICK | %s | all picks taken", user.display_name)
        try:
            taken_list = "\n".join(f"• {n}" for n in skipped)
            await user.send(
                f"⚠️ It's your turn (Pick **#{pick_num}**) but **all your picks are already taken**!\n\n"
                f"Players checked:\n{taken_list}\n\n"
                f"You need to respond manually in the draft channel."
            )
        except Exception:
            pass
        return

    # Post the pick
    pick_msg = _build_pick_message(pick_num, emoji, chosen_pick)
    await channel.send(pick_msg)
    log.info("AUTO-PICK | %s | Pick #%d | %s", user.display_name, pick_num, pick_msg)

    # Remove used pick and save
    picks.pop(chosen_idx)
    _data[uid]["picks"] = picks
    _save_data(_data)

    # DM confirmation
    try:
        skipped_note = f"\n(Skipped {len(skipped)} already-taken: {', '.join(skipped)})" if skipped else ""
        remaining    = len(picks)
        await user.send(
            f"✅ Auto-picked for you: **{pick_msg}**{skipped_note}\n"
            f"You have **{remaining}** pick{'s' if remaining != 1 else ''} remaining."
        )
    except Exception:
        pass


# ── Discord bot ───────────────────────────────────────────────────────────────
intents                 = discord.Intents.default()
intents.message_content = True
intents.members         = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_PICK_NUM_RE = re.compile(r"Pick\s+(\d+)", re.IGNORECASE)


@bot.event
async def on_ready():
    log.info("ATD Draft List Bot ready — %s", bot.user)


@bot.event
async def on_message(message: discord.Message):
    # ── Timer bot ping detection (draft channel only) ─────────────────────────
    if (
        message.author.bot
        and message.channel.id == DRAFT_CHANNEL_ID
        and (not TIMER_BOT_ID or message.author.id == TIMER_BOT_ID)
        and message.embeds
    ):
        embed = message.embeds[0]
        desc  = embed.description or ""

        if "your turn" in desc.lower():
            m = _PICK_NUM_RE.search(embed.title or "")
            if m:
                pick_num = int(m.group(1))
                for mentioned_user in message.mentions:
                    uid = str(mentioned_user.id)
                    if uid in _data and _data[uid].get("picks") and _data[uid].get("enabled", True):
                        log.info(
                            "AUTO-PICK triggered | User: %s | Pick #%d",
                            mentioned_user.display_name,
                            pick_num,
                        )
                        asyncio.create_task(_do_auto_pick(message.channel, mentioned_user, pick_num))
                        break
        return  # don't process bot messages as commands

    if message.author.bot:
        return

    # ── First-time DM greeting ────────────────────────────────────────────────
    if isinstance(message.channel, discord.DMChannel):
        uid = str(message.author.id)
        if uid not in _data or not _data[uid].get("greeted"):
            _ensure_user(uid)
            _data[uid]["greeted"] = True
            _save_data(_data)
            greet = discord.Embed(
                title="👋 Welcome to ATD Draft List Bot",
                description=(
                    "I auto-pick for you during the draft when you're not online.\n\n"
                    "You give me a ranked list of players. When it's your turn, I check "
                    "the sheet to make sure each player is still available, then post your "
                    "pick automatically in the draft channel."
                ),
                color=discord.Color.blue(),
            )
            greet.add_field(
                name="Quick Start",
                value=(
                    "**1.** Add your ranked picks:\n"
                    "```\n!setlist\nLeBron James '23 $5\nMichael Jordan $10\nKobe Bryant\n```\n"
                    "**2.** That's it — I'll handle the rest when it's your turn.\n\n"
                    "_(Optional: set your team emoji by typing `!setup <:emoji:id>` in the server — no Nitro needed there.)_"
                ),
                inline=False,
            )
            greet.add_field(
                name="Other Useful Commands",
                value=(
                    "`!mylist` — view your current list\n"
                    "`!addpick Player Name` — add one pick to the end\n"
                    "`!removepick 2` — remove pick at position 2\n"
                    "`!insertpick 1 Player Name` — insert at a specific position\n"
                    "`!pause` / `!resume` — stop or restart auto-drafting\n"
                    "`!help` — full command reference"
                ),
                inline=False,
            )
            greet.set_footer(text="All commands should be sent here as a DM.")
            await message.channel.send(embed=greet)

    await bot.process_commands(message)


# ── Admin commands ────────────────────────────────────────────────────────────
@bot.command(name="setemoji")
@commands.has_permissions(administrator=True)
async def cmd_setemoji(ctx, member: discord.Member, *, emoji_str: str):
    """!setemoji @user <:emoji:id> — set a drafter's team emoji on their behalf (admin only)."""
    uid = str(member.id)
    _ensure_user(uid)
    _data[uid]["emoji"] = emoji_str.strip()
    _save_data(_data)
    await ctx.send(f"✅ Set **{member.display_name}**'s team emoji to {emoji_str.strip()}")


@bot.command(name="listsheets")
async def cmd_listsheets(ctx):
    """!listsheets — list all tab names in the spreadsheet."""
    try:
        service = _build_sheets_service()
        result  = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        names   = [s["properties"]["title"] for s in result.get("sheets", [])]
        lines   = [f"• `{n}`" for n in names]

        # Send in chunks under 1900 chars
        chunk = "**Sheet tabs found:**\n"
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await ctx.send(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await ctx.send(chunk)
    except Exception as e:
        await ctx.send(f"❌ Error: `{e}`")


@bot.command(name="checkplayer")
async def cmd_checkplayer(ctx, *, player_name: str):
    """!checkplayer Player Name — debug: show what the sheet says about a player."""
    await ctx.send(f"🔍 Checking sheet for **{player_name}**…")
    try:
        service = _build_sheets_service()
        result  = service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID,
            ranges=[f"{PLAYERS_SHEET_NAME}!A:D"],
            includeGridData=True,
        ).execute()

        sheets = result.get("sheets", [])
        if not sheets:
            await ctx.send("❌ No sheets returned — check `PLAYERS_SHEET_NAME` in config.")
            return

        name_lower = player_name.lower().strip()
        matches    = []

        for row_idx, row in enumerate(sheets[0].get("data", [{}])[0].get("rowData", [])):
            for col_idx, cell in enumerate(row.get("values", [])):
                text = (cell.get("formattedValue") or "").strip()
                if name_lower in text.lower() or text.lower() in name_lower:
                    taken = _is_taken(cell)
                    fmt   = cell.get("effectiveFormat", {})
                    bg    = fmt.get("backgroundColor", {})
                    r     = bg.get("red",   1.0)
                    g     = bg.get("green", 1.0)
                    b     = bg.get("blue",  1.0)
                    matches.append(
                        f"Row {row_idx+1} Col {col_idx+1}: `{text}` | "
                        f"RGB({r:.2f}, {g:.2f}, {b:.2f}) | "
                        f"{'❌ TAKEN' if taken else '✅ Available'}"
                    )

        if not matches:
            await ctx.send(f"⚠️ **{player_name}** not found in the `{PLAYERS_SHEET_NAME}` sheet.")
        else:
            await ctx.send("\n".join(matches))

    except Exception as e:
        await ctx.send(f"❌ Sheet error: `{e}`")


@bot.command(name="alllists")
async def cmd_alllists(ctx):
    """!alllists — show every registered drafter's pick list (owner only)."""
    in_dm = isinstance(ctx.channel, discord.DMChannel)
    is_owner = ctx.author.id == OWNER_ID

    if in_dm and not is_owner:
        return
    if not in_dm and not ctx.author.guild_permissions.administrator:
        return

    if not _data:
        await ctx.send("No drafters have registered a pick list yet.")
        return

    # Deduplicate: if multiple UIDs resolve to the same Discord user, keep the
    # best entry (active > paused, then more picks).
    seen: dict[int, str] = {}  # real_user_id → uid to keep
    for uid, user_data in _data.items():
        member  = ctx.guild.get_member(int(uid)) if ctx.guild else None
        fetched = bot.get_user(int(uid))
        real_id = member.id if member else (fetched.id if fetched else int(uid))

        if real_id in seen:
            prev = _data[seen[real_id]]
            curr_better = (
                user_data.get("enabled", True) and not prev.get("enabled", True)
            ) or len(user_data.get("picks", [])) > len(prev.get("picks", []))
            if curr_better:
                seen[real_id] = uid
        else:
            seen[real_id] = uid

    embeds = []
    for uid in seen.values():
        user_data = _data[uid]
        picks     = user_data.get("picks", [])
        raw_emoji = (user_data.get("emoji") or "").strip()
        enabled   = user_data.get("enabled", True)

        member  = ctx.guild.get_member(int(uid)) if ctx.guild else None
        fetched = bot.get_user(int(uid))
        name    = (member or fetched).display_name if (member or fetched) else f"User {uid}"

        emoji_display = raw_emoji if _is_valid_emoji(raw_emoji) else "(not set)"
        status_icon   = "▶️" if enabled else "⏸️"
        status_text   = "Active" if enabled else "Paused"
        pick_count    = len(picks)

        if not picks:
            pick_preview = "_No picks yet_"
        else:
            lines = [f"{i+1}. {_format_pick(p)}" for i, p in enumerate(picks[:15])]
            if pick_count > 15:
                lines.append(f"_…and {pick_count - 15} more_")
            pick_preview = "\n".join(lines)
            if len(pick_preview) > 1000:
                pick_preview = pick_preview[:997] + "…"

        embed = discord.Embed(
            title=f"{status_icon} {name}",
            color=discord.Color.green() if enabled else discord.Color.greyple(),
        )
        embed.add_field(name="Status", value=status_text, inline=True)
        embed.add_field(name="Emoji",  value=emoji_display, inline=True)
        embed.add_field(name="Picks",  value=str(pick_count), inline=True)
        if picks:
            embed.add_field(name="Pick List", value=pick_preview, inline=False)
        embeds.append(embed)

    for i in range(0, len(embeds), 10):
        await ctx.send(embeds=embeds[i:i+10])


# ── DM commands ───────────────────────────────────────────────────────────────
def _dm_only(ctx) -> bool:
    return isinstance(ctx.channel, discord.DMChannel)


@bot.command(name="setup")
async def cmd_setup(ctx, *, emoji_str: str = ""):
    """!setup <:emoji:id> — register your team emoji (works in server or DM)."""
    uid = str(ctx.author.id)
    _ensure_user(uid)
    _data[uid]["emoji"] = emoji_str.strip()
    _save_data(_data)
    display = emoji_str.strip() or "(none)"
    await ctx.send(
        f"✅ Team emoji set to `{display}`.\n"
        f"Now add your picks with `!setlist` or `!addpick`."
    )


@bot.command(name="setlist")
async def cmd_setlist(ctx, *, text: str = ""):
    """!setlist\\nPlayer 1\\nPlayer 2\\n... — replace your entire pick list."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    if not text.strip():
        await ctx.send(
            "Paste your ranked list right after the command:\n"
            "```\n!setlist\nLeBron James '23 $5\nMichael Jordan $10\nKobe Bryant\n```"
        )
        return

    uid = str(ctx.author.id)
    _ensure_user(uid)

    old_names = {p["player"].lower() for p in _data[uid].get("picks", [])}

    picks = []
    seen  = set()
    dupes = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        pick = _parse_pick_line(line)
        key  = pick["player"].lower()
        if key in seen:
            dupes.append(pick["player"])
        else:
            seen.add(key)
            picks.append(pick)

    already_in_list = [p["player"] for p in picks if p["player"].lower() in old_names]

    _data[uid]["picks"] = picks
    _save_data(_data)

    # Check all players against the sheet and flag any already taken
    try:
        availability = await asyncio.get_event_loop().run_in_executor(None, _fetch_availability)
        taken = [p["player"] for p in picks if not _player_available(p["player"], availability)]
    except Exception:
        taken = []

    preview = "\n".join(f"{i+1}. {_format_pick(p)}" for i, p in enumerate(picks))
    msg = f"✅ Pick list set ({len(picks)} picks):\n```\n{preview}\n```"
    if dupes:
        msg += f"\n⚠️ **Duplicates removed:** {', '.join(dupes)}"
    if already_in_list:
        msg += f"\n⚠️ **Already in your list:** {', '.join(already_in_list)} — already existed, list replaced."
    if taken:
        msg += f"\n⚠️ **Already taken in draft:** {', '.join(taken)} — they'll be skipped automatically when it's your turn. Consider replacing them."
    await ctx.send(msg)


@bot.command(name="addpick")
async def cmd_addpick(ctx, *, text: str):
    """!addpick Player Name ['year] [$price] — append one pick to your list."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid = str(ctx.author.id)
    _ensure_user(uid)

    pick = _parse_pick_line(text)

    # Reject duplicates
    existing = [p["player"].lower() for p in _data[uid]["picks"]]
    if pick["player"].lower() in existing:
        await ctx.send(f"⚠️ **{pick['player']}** is already in your list.")
        return

    # Check availability before adding
    try:
        availability = await asyncio.get_event_loop().run_in_executor(None, _fetch_availability)
        already_taken = not _player_available(pick["player"], availability)
    except Exception:
        already_taken = False

    _data[uid]["picks"].append(pick)
    _save_data(_data)

    pos = len(_data[uid]["picks"])
    if already_taken:
        await ctx.send(
            f"⚠️ **{pick['player']} is already taken** — added at position **#{pos}** anyway, "
            f"but they'll be skipped when it's your turn. You may want to replace them."
        )
    else:
        await ctx.send(f"✅ Added at position **#{pos}**: {_format_pick(pick)}")


@bot.command(name="insertpick")
async def cmd_insertpick(ctx, position: int, *, text: str):
    """!insertpick 1 Player Name — insert a pick at a specific position."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid = str(ctx.author.id)
    _ensure_user(uid)
    picks = _data[uid]["picks"]

    pos  = max(1, min(position, len(picks) + 1))
    pick = _parse_pick_line(text)

    if pick["player"].lower() in [p["player"].lower() for p in picks]:
        await ctx.send(f"⚠️ **{pick['player']}** is already in your list.")
        return

    picks.insert(pos - 1, pick)
    _data[uid]["picks"] = picks
    _save_data(_data)

    await ctx.send(f"✅ Inserted at position **#{pos}**: {_format_pick(pick)}")


@bot.command(name="removepick")
async def cmd_removepick(ctx, position: int):
    """!removepick 3 — remove the pick at position 3."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid  = str(ctx.author.id)
    picks = _data.get(uid, {}).get("picks", [])

    if not picks:
        await ctx.send("Your pick list is empty.")
        return
    if position < 1 or position > len(picks):
        await ctx.send(f"Invalid position. You have {len(picks)} picks (1–{len(picks)}).")
        return

    removed = picks.pop(position - 1)
    _data[uid]["picks"] = picks
    _save_data(_data)
    await ctx.send(f"✅ Removed **#{position}**: {_format_pick(removed)}")


@bot.command(name="mylist")
async def cmd_mylist(ctx):
    """!mylist — view your current pick list."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid       = str(ctx.author.id)
    user_data = _data.get(uid, {})
    picks     = user_data.get("picks", [])
    raw_emoji = (user_data.get("emoji") or "").strip()
    enabled   = user_data.get("enabled", True)

    emoji_display = raw_emoji if _is_valid_emoji(raw_emoji) else "(not set — use `!setup <:emoji:id>` in the server)"
    status_icon   = "▶️" if enabled else "⏸️"
    status_text   = "Active" if enabled else "Paused"

    embed = discord.Embed(
        title=f"{status_icon} Your Pick List",
        color=discord.Color.green() if enabled else discord.Color.greyple(),
    )
    embed.add_field(name="Status", value=status_text, inline=True)
    embed.add_field(name="Emoji",  value=emoji_display, inline=True)
    embed.add_field(name="Picks",  value=str(len(picks)), inline=True)

    if not picks:
        embed.add_field(
            name="Pick List",
            value="_Empty — use `!addpick` or `!setlist` to add picks._",
            inline=False,
        )
    else:
        preview = "\n".join(f"{i+1}. {_format_pick(p)}" for i, p in enumerate(picks))
        if len(preview) > 1000:
            preview = preview[:997] + "…"
        embed.add_field(name="Pick List", value=preview, inline=False)

    await ctx.send(embed=embed)


@bot.command(name="clearpicks")
async def cmd_clearpicks(ctx):
    """!clearpicks — clear your entire pick list (keeps emoji/settings)."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid = str(ctx.author.id)
    _ensure_user(uid)
    _data[uid]["picks"] = []
    _save_data(_data)
    await ctx.send("✅ Pick list cleared.")


@bot.command(name="pause")
async def cmd_pause(ctx):
    """!pause — disable auto-drafting (bot won't pick for you until you resume)."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid = str(ctx.author.id)
    _ensure_user(uid)
    _data[uid]["enabled"] = False
    _save_data(_data)
    await ctx.send("⏸️ Auto-drafting **paused**. Use `!resume` to turn it back on.")


@bot.command(name="resume")
async def cmd_resume(ctx):
    """!resume — re-enable auto-drafting."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid = str(ctx.author.id)
    _ensure_user(uid)
    _data[uid]["enabled"] = True
    _save_data(_data)
    await ctx.send("▶️ Auto-drafting **resumed**.")


@bot.command(name="help")
async def cmd_help(ctx):
    """!help — show all commands."""
    embed = discord.Embed(
        title="ATD Draft List Bot",
        description=(
            "I auto-pick for you when it's your turn. "
            "DM me these commands to manage your list."
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="1️⃣  First-time setup (optional)",
        value=(
            "Set your team emoji so your picks are correctly attributed:\n"
            "`!setup <:emoji:id>` — type this in the **server** (no Nitro needed there)\n\n"
            "If you skip this, the commissioner can set it for you."
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣  Build your list",
        value=(
            "`!setlist` — paste your full ranked list (one player per line)\n"
            "`!addpick Player Name ['year] [$price]` — append one pick\n"
            "`!insertpick 1 Player Name` — insert at a specific position\n"
            "`!removepick 3` — remove pick at position 3\n"
            "`!clearpicks` — clear everything"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣  View & control",
        value=(
            "`!mylist` — view your current list\n"
            "`!pause` — stop auto-drafting (if you come online)\n"
            "`!resume` — turn auto-drafting back on"
        ),
        inline=False,
    )
    embed.add_field(
        name="Pick format",
        value=(
            "Each pick can include:\n"
            "• **Player name** (required): `LeBron James`\n"
            "• **Year** (optional): `'23` or `2022-23`\n"
            "• **Price** (optional): `$5`\n\n"
            "Example: `LeBron James '23 $5`"
        ),
        inline=False,
    )
    embed.set_footer(text="All commands must be sent as a DM to this bot.")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound) and isinstance(ctx.channel, discord.DMChannel):
        await ctx.invoke(cmd_help)
    elif isinstance(error, commands.MissingRequiredArgument) and isinstance(ctx.channel, discord.DMChannel):
        await ctx.send(f"⚠️ Missing argument. Type `!help` for usage.")
    elif not isinstance(error, commands.CheckFailure):
        log.error("Command error in %s: %s", ctx.command, error)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)