import asyncio
import difflib
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
    LOTTO_CHANNEL_ID,
    TIMER_BOT_ID,
    OWNER_ID,
    SPREADSHEET_ID,
    PLAYERS_SHEET_NAME,
    ROSTER_SHEET_NAME,
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

# In-memory set of player names (lowercase) auto-picked this draft session.
# Catches duplicates instantly — before the sheet has time to update.
_drafted_this_session: set[str] = set()

# Serializes concurrent auto-picks so two simultaneous Timer Bot pings can't
# both hit the sheet at once and cause a freeze or double-pick.
_autopick_lock = asyncio.Lock()


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
    """Return True if the cell has a near-black background (= player already drafted).
    Only black / very dark fills count — blue, tan, or other colored rows are NOT taken."""
    fmt = cell_data.get("effectiveFormat", {})
    bg  = fmt.get("backgroundColor", {})
    r = bg.get("red",   1.0)
    g = bg.get("green", 1.0)
    b = bg.get("blue",  1.0)
    return r < 0.2 and g < 0.2 and b < 0.2


_availability_cache: dict | None = None
_availability_cache_ts: float = 0.0
_roster_cache: set | None = None
_roster_cache_ts: float = 0.0
_AVAILABILITY_TTL = 60  # seconds
# lowercase → original-case name as it appears in the sheet
_canonical_names: dict[str, str] = {}

# Tracks the last auto-pick so we can retry if the timer bot rejects it
_last_autopick: dict | None = None  # {"user": discord.User, "pick_num": int}

# Per-user pending tasks that clear the pick list after a confirmed pick.
# Cancelled and rescheduled on retry so the list only clears once the
# timer bot stops rejecting.
_clear_tasks: dict[str, asyncio.Task] = {}


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
        ranges=[f"'{PLAYERS_SHEET_NAME}'!A:D"],
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
                    _canonical_names[text.lower()] = text

    _availability_cache = availability
    _availability_cache_ts = now
    return availability


def _resolve_canonical_name(player_name: str) -> str:
    """Expand a partial/nickname to the full name as it appears in the sheet."""
    name_lower = player_name.lower().strip()
    if name_lower in _canonical_names:
        return _canonical_names[name_lower]
    matches = [canon for key, canon in _canonical_names.items() if name_lower in key]
    if len(matches) == 1:
        return matches[0]
    return player_name


def _fetch_roster_names(force: bool = False) -> set:
    """
    Read the roster sheet (the one the Team Sheet Bot writes picks into) and
    return a set of all non-empty cell values in lowercase.
    This catches players that are drafted but whose cells haven't been colored.
    """
    global _roster_cache, _roster_cache_ts
    now = time.monotonic()
    if not force and _roster_cache is not None and (now - _roster_cache_ts) < _AVAILABILITY_TTL:
        return _roster_cache

    service = _build_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{ROSTER_SHEET_NAME}'!A:Z",
    ).execute()

    names: set[str] = set()
    for row in result.get("values", []):
        for cell in row:
            text = str(cell).strip()
            if text:
                names.add(text.lower())

    _roster_cache = names
    _roster_cache_ts = now
    log.info("ROSTER | fetched %d cell entries from '%s'", len(names), ROSTER_SHEET_NAME)
    return names


def _player_available(player_name: str, availability: dict, roster_names: set | None = None) -> bool:
    """Check availability — exact match first, then partial."""
    name_lower = player_name.lower().strip()

    # Text-based check against the roster sheet (most reliable — Team Sheet Bot writes text, not colors)
    if roster_names is not None:
        if name_lower in roster_names:
            return False
        for roster_name in roster_names:
            # Only check if player name is a substring of a roster cell (e.g. "LeBron James" in "LeBron James '13").
            # NOT the reverse — short cell values (headers, years, team names) would falsely match inside player names.
            if name_lower in roster_name:
                return False

    # Color-based check against the players pool sheet (black background = drafted)
    if name_lower in availability:
        return availability[name_lower]

    for sheet_name, avail in availability.items():
        if name_lower in sheet_name:
            return avail

    # Not found in either sheet — log and assume available
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
    async with _autopick_lock:
        await _do_auto_pick_inner(channel, user, pick_num)


async def _do_auto_pick_inner(channel: discord.TextChannel, user: discord.User, pick_num: int):
    global _last_autopick
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
    if not _is_valid_emoji(emoji):
        log.warning("AUTO-PICK | %s | no valid emoji set — aborting", user.display_name)
        try:
            await user.send(
                f"⚠️ It's your turn (Pick **#{pick_num}**) but I can't auto-pick for you because "
                f"you don't have a team emoji set.\n\n"
                f"Ask the commissioner to run `!setemoji @{user.display_name} <:emoji:id>` "
                f"in the server, then you'll be good to go.\n\n"
                f"You need to respond manually in the draft channel for this pick."
            )
        except Exception:
            pass
        return

    # Fetch sheet availability (force-refresh — stale data could cause a bad auto-pick)
    try:
        availability = _fetch_availability(force=True)
        log.info("AUTO-PICK | fetched %d player entries from sheet", len(availability))
    except Exception as e:
        log.error("AUTO-PICK | sheet fetch failed: %s", e)
        availability = {}

    try:
        roster_names = _fetch_roster_names(force=True)
    except Exception as e:
        log.error("AUTO-PICK | roster fetch failed: %s", e)
        roster_names = None

    # Scan recent draft channel messages to catch picks not yet reflected in the sheet.
    # A pick message starts with a number + period (e.g. "42. <:emoji:id> LeBron James '23 $5").
    recently_picked: set[str] = set()
    try:
        pick_names_lower = {p["player"].lower() for p in picks}
        async for msg in channel.history(limit=50):
            if re.match(r'^\d+\s*\.', msg.content.strip()):
                content_lower = msg.content.lower()
                for name in pick_names_lower:
                    if name in content_lower:
                        recently_picked.add(name)
    except Exception as e:
        log.warning("AUTO-PICK | channel history scan failed: %s", e)

    # Walk list — find first available player
    chosen_idx  = None
    chosen_pick = None
    skipped     = []

    for idx, pick in enumerate(picks):
        name_lower = pick["player"].lower()
        if name_lower in _drafted_this_session:
            skipped.append(pick["player"])
            log.info("AUTO-PICK | %s | '%s' already drafted this session — skipping", user.display_name, pick["player"])
        elif name_lower in recently_picked:
            skipped.append(pick["player"])
            log.info("AUTO-PICK | %s | '%s' found in recent channel messages — skipping", user.display_name, pick["player"])
        elif _player_available(pick["player"], availability, roster_names):
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

    # Resolve partial/nickname to full canonical name from the sheet
    canonical = _resolve_canonical_name(chosen_pick["player"])
    if canonical != chosen_pick["player"]:
        log.info("AUTO-PICK | resolved '%s' → '%s'", chosen_pick["player"], canonical)
        chosen_pick = {**chosen_pick, "player": canonical}

    # Mark as drafted BEFORE posting so no concurrent path can pick the same player
    _drafted_this_session.add(chosen_pick["player"].lower())

    # Post the pick
    pick_msg = _build_pick_message(pick_num, emoji, chosen_pick)
    await channel.send(pick_msg)
    log.info("AUTO-PICK | %s | Pick #%d | %s", user.display_name, pick_num, pick_msg)

    # Store so the rejection handler can retry if the timer bot rejects this pick
    _last_autopick = {"user": user, "pick_num": pick_num}

    # Remove the chosen pick so retries can find the next available player.
    # The rest of the list is cleared after 30 s — long enough for the timer bot
    # to reject a bad pick and trigger a retry, but quick enough to feel instant.
    picks.pop(chosen_idx)
    _data[uid]["picks"] = picks
    _save_data(_data)

    # Cancel any existing clear task then schedule a fresh one.
    existing = _clear_tasks.get(uid)
    if existing and not existing.done():
        existing.cancel()

    async def _clear_round_picks(uid_: str):
        await asyncio.sleep(30)
        if uid_ in _data:
            _data[uid_]["picks"] = []
            _save_data(_data)
            log.info("ROUND COMPLETE | cleared remaining picks for uid=%s", uid_)

    _clear_tasks[uid] = asyncio.create_task(_clear_round_picks(uid))

    # DM confirmation
    try:
        skipped_note = f"\n(Skipped {len(skipped)} already-taken: {', '.join(skipped)})" if skipped else ""
        await user.send(
            f"✅ Auto-picked for you: **{pick_msg}**{skipped_note}\n"
            f"Your pick list for this round has been cleared. Set a new list before your next turn."
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
    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    if channel:
        asyncio.create_task(_backfill_drafted_session(channel))


async def _backfill_drafted_session(channel: discord.TextChannel):
    """On startup, mark already-taken players so auto-picks skip them immediately."""
    count = 0

    # 1. Fetch the roster sheet — this is the ground truth for what's been picked.
    try:
        roster_names = await asyncio.get_event_loop().run_in_executor(None, _fetch_roster_names)
        all_pick_names = {
            p["player"].lower()
            for user_data in _data.values()
            for p in user_data.get("picks", [])
        }
        for name in all_pick_names:
            if name not in _drafted_this_session:
                # player name is substring of a roster entry → already taken
                for rname in roster_names:
                    if name in rname:
                        _drafted_this_session.add(name)
                        count += 1
                        break
    except Exception as e:
        log.error("BACKFILL | roster fetch failed: %s", e)

    # 2. Also scan recent channel history for pick messages (catches sheet lag).
    try:
        all_pick_names = {
            p["player"].lower()
            for user_data in _data.values()
            for p in user_data.get("picks", [])
        }
        async for msg in channel.history(limit=500):
            if re.match(r'^\d+\s*\.', msg.content.strip()):
                content_lower = msg.content.lower()
                for name in all_pick_names:
                    if name in content_lower and name not in _drafted_this_session:
                        _drafted_this_session.add(name)
                        count += 1
    except Exception as e:
        log.error("BACKFILL | channel history failed: %s", e)

    log.info("BACKFILL | marked %d players as taken on startup", count)


@bot.event
async def on_message(message: discord.Message):
    global _last_autopick

    # ── Timer bot rejection — retry with next available pick ──────────────────
    # Fires when the timer bot replies "already been taken — pick someone else."
    if (
        message.author.bot
        and message.channel.id == DRAFT_CHANNEL_ID
        and bot.user in message.mentions
        and "has already been taken" in message.content
        and "pick someone else" in message.content.lower()
        and _last_autopick
    ):
        m = re.search(r'[—–-]\s*\*{0,2}(.+?)\*{0,2}\s+has already been taken', message.content)
        if m:
            _drafted_this_session.add(m.group(1).strip().lower())
            log.info("REJECTION | '%s' confirmed taken — retrying", m.group(1).strip())
        retry_user     = _last_autopick["user"]
        retry_pick_num = _last_autopick["pick_num"]
        _last_autopick = None
        # Cancel the clear task so remaining picks survive for the retry
        retry_uid = str(retry_user.id)
        existing = _clear_tasks.get(retry_uid)
        if existing and not existing.done():
            existing.cancel()
        asyncio.create_task(_do_auto_pick(message.channel, retry_user, retry_pick_num))
        return

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

    # ── Real-time pick tracking (draft channel) ───────────────────────────────
    # Any message that starts with "N." or "N ." is a pick announcement.
    # Check it against every registered pick list and mark matched players as
    # drafted so auto-picks skip them immediately — even before the sheet updates.
    # Also notify users via DM and remove the player from their list.
    if (
        message.channel.id == DRAFT_CHANNEL_ID
        and re.match(r'^\d+\s*\.', message.content.strip())
    ):
        content_lower = message.content.lower()
        picker_name = message.author.display_name
        log.info("PICK DETECTED | channel=%d author=%s content=%.80s",
                 message.channel.id, picker_name, message.content.strip())

        # Track against all registered users' pick lists
        for uid, user_data in _data.items():
            picks = user_data.get("picks", [])
            if not picks:
                continue
            is_picker = message.author.id == int(uid)
            for pick in list(picks):
                name_lower = pick["player"].lower()
                if name_lower not in _drafted_this_session and name_lower in content_lower:
                    _drafted_this_session.add(name_lower)
                    if is_picker:
                        log.info("PICK TRACKED | '%s' picked by owner — session-tracked only", pick["player"])
                        continue
                    pos = picks.index(pick) + 1
                    picks.remove(pick)
                    _save_data(_data)
                    log.info("PICK TRACKED | '%s' drafted — removed from %s's list (was #%d)",
                             pick["player"], uid, pos)
                    try:
                        user = bot.get_user(int(uid))
                        if not user:
                            user = await bot.fetch_user(int(uid))
                        if user:
                            await user.send(
                                f"⚠️ **{pick['player']}** was just drafted by **{picker_name}**. "
                                f"He was **#{pos}** on your list and has been removed.\n"
                                f"You have **{len(picks)}** picks remaining."
                            )
                    except Exception as e:
                        log.error("PICK NOTIFY DM failed for uid=%s: %s", uid, e)

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

async def _resolve_member(ctx, arg: str) -> "discord.Member | None":
    """Resolve an @mention or raw user ID to a guild Member."""
    if ctx.message.mentions:
        return ctx.message.mentions[0]
    try:
        return await ctx.guild.fetch_member(int(arg.strip("<@!>")))
    except (ValueError, discord.NotFound, discord.HTTPException):
        return None


@bot.command(name="clearlist")
@commands.has_permissions(administrator=True)
async def cmd_clearlist(ctx, *, target: str):
    """!clearlist @user — wipe a drafter's pick list (admin only)."""
    member = await _resolve_member(ctx, target.split()[0])
    if not member:
        await ctx.send("❌ Could not find that user. Use an @mention or user ID.")
        return
    uid = str(member.id)
    _ensure_user(uid)
    old_count = len(_data[uid].get("picks", []))
    _data[uid]["picks"] = []
    _save_data(_data)
    await ctx.send(f"✅ Cleared **{member.display_name}**'s pick list ({old_count} picks removed).")


@bot.command(name="setlistfor")
@commands.has_permissions(administrator=True)
async def cmd_setlistfor(ctx, target: str, *, text: str = ""):
    """!setlistfor @user\\nPlayer 1\\nPlayer 2\\n... — set a pick list for someone (admin only)."""
    member = await _resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Could not find that user. Use an @mention or user ID.")
        return
    if not text.strip():
        await ctx.send(
            "Paste the pick list after the mention:\n"
            "```\n!setlistfor @User\nLeBron James '23\nKobe Bryant\n```"
        )
        return

    uid = str(member.id)
    _ensure_user(uid)

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

    _data[uid]["picks"] = picks
    _save_data(_data)

    preview = "\n".join(f"{i+1}. {_format_pick(p)}" for i, p in enumerate(picks))
    msg = f"✅ Set **{member.display_name}**'s pick list ({len(picks)} picks):\n```\n{preview}\n```"
    if dupes:
        msg += f"\n⚠️ Duplicates removed: {', '.join(dupes)}"
    await ctx.send(msg)

    # Check availability and warn about taken players
    async def _check_and_warn():
        try:
            availability = await asyncio.get_event_loop().run_in_executor(None, _fetch_availability)
            roster_names = await asyncio.get_event_loop().run_in_executor(None, _fetch_roster_names)
            taken = []
            for p in picks:
                name = p["player"]
                if name.lower() in _drafted_this_session:
                    taken.append(name)
                elif not _player_available(name, availability, roster_names):
                    taken.append(name)
            if taken:
                await ctx.send(
                    f"⚠️ **Already taken:** {', '.join(taken)} — they'll be skipped automatically. Consider replacing them."
                )
        except Exception as e:
            log.error("setlistfor availability check failed: %s", e)

    asyncio.create_task(_check_and_warn())

    # Notify the user via DM
    try:
        await member.send(
            f"📋 Your ATD Draft List pick list has been set by the commissioner "
            f"({len(picks)} picks):\n```\n{preview}\n```"
        )
    except Exception:
        pass


@bot.command(name="setemoji")
async def cmd_setemoji(ctx, member_or_emoji: str = None, *, rest: str = ""):
    """!setemoji <:emoji:id> — set your own emoji. Admins: !setemoji @user <:emoji:id>"""
    is_admin = ctx.author.guild_permissions.administrator if ctx.guild else False

    # Resolve member + emoji from the arguments
    target_member = None
    emoji_str = None

    if member_or_emoji and ctx.message.mentions:
        # Admin setting someone else's emoji: !setemoji @user <:emoji:id>
        if not is_admin:
            await ctx.send("❌ You need administrator permission to set someone else's emoji.")
            return
        target_member = ctx.message.mentions[0]
        emoji_str = rest.strip() or None
    elif member_or_emoji and ctx.guild and is_admin and member_or_emoji.isdigit():
        # Admin using raw user ID: !setemoji 123456789 <:emoji:id>
        target_member = await _resolve_member(ctx, member_or_emoji)
        if not target_member:
            await ctx.send("❌ Could not find that user ID.")
            return
        emoji_str = rest.strip() or None
    elif member_or_emoji:
        # User setting their own emoji: !setemoji <:emoji:id>
        emoji_str = (member_or_emoji + (" " + rest if rest else "")).strip()
        target_member = ctx.author

    if not emoji_str:
        await ctx.send("❌ Provide an emoji. Example: `!setemoji <:YourEmoji:123456>`")
        return

    uid = str(target_member.id)
    _ensure_user(uid)
    _data[uid]["emoji"] = emoji_str
    _save_data(_data)

    if target_member.id == ctx.author.id:
        await ctx.send(f"✅ Your team emoji is set to {emoji_str}")
    else:
        await ctx.send(f"✅ Set **{target_member.display_name}**'s team emoji to {emoji_str}")


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
            ranges=[f"'{PLAYERS_SHEET_NAME}'!A:D"],
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


_CUSTOM_EMOJI_RE2 = re.compile(r'<a?:[^:]+:\d+>')
# Broad unicode emoji range covering most standard + extended emoji blocks
_UNICODE_EMOJI_RE = re.compile(
    r'[\U0001F300-\U0001FAFF]'   # misc symbols, emoticons, transport, etc.
    r'|[\U00002600-\U000027BF]'  # misc symbols
    r'|[\U0001F900-\U0001F9FF]'  # supplemental symbols
    r'|[\U00002300-\U000023FF]'  # misc technical
)


def _extract_lotto_lines(text: str):
    """
    Yield (emoji_str, [user_id, ...]) for each valid lotto line.
    Handles both custom <:name:id> emojis and unicode emojis.
    """
    for line in text.splitlines():
        # Must start with a pick number
        m = re.match(r'^\s*\d+\.\s*(.*?)\s*-\s*((?:<@!?\d+>\s*)+)$', line.strip())
        if not m:
            continue
        before_dash = m.group(1)
        user_ids = [int(uid) for uid in re.findall(r'<@!?(\d+)>', m.group(2))]
        if not user_ids:
            continue

        # Prefer custom emoji, fall back to unicode emoji
        ce = _CUSTOM_EMOJI_RE2.search(before_dash)
        if ce:
            emoji_str = ce.group(0)
        else:
            ue = _UNICODE_EMOJI_RE.search(before_dash)
            emoji_str = ue.group(0) if ue else None

        if emoji_str:
            yield emoji_str, user_ids


@bot.command(name="synclotto")
async def cmd_synclotto(ctx):
    """!synclotto — read the lotto message and auto-set everyone's team emoji (owner only)."""
    if ctx.author.id != OWNER_ID:
        return

    # Resolve which message to parse: reply > current channel > lotto channel
    lotto_text = None
    if ctx.message.reference:
        try:
            ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            lotto_text = ref_msg.content
        except Exception:
            await ctx.send("❌ Could not fetch the replied message.")
            return
    else:
        # Search current channel first, then dedicated lotto channel
        search_channels = [ctx.channel]
        if LOTTO_CHANNEL_ID:
            lotto_ch = bot.get_channel(LOTTO_CHANNEL_ID)
            if lotto_ch and lotto_ch.id != ctx.channel.id:
                search_channels.append(lotto_ch)

        for ch in search_channels:
            async for msg in ch.history(limit=50):
                if re.search(r'^\s*1\.', msg.content, re.MULTILINE):
                    lotto_text = msg.content
                    break
            if lotto_text:
                break

        if not lotto_text:
            await ctx.send("❌ No lotto message found. Reply directly to the lotto message and try again.")
            return

    updated = []
    for emoji_str, user_ids in _extract_lotto_lines(lotto_text):
        for uid in user_ids:
            _ensure_user(str(uid))
            _data[str(uid)]["emoji"] = emoji_str
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name   = member.display_name if member else str(uid)
            updated.append(f"{emoji_str} → **{name}**")

    if not updated:
        await ctx.send("⚠️ No valid lotto lines found. Try replying directly to the lotto message.")
        return

    _save_data(_data)
    await ctx.send("✅ Emojis synced from lotto:\n" + "\n".join(updated))


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

    _data[uid]["picks"] = picks
    _save_data(_data)

    # Confirm immediately so the user isn't waiting
    preview = "\n".join(f"{i+1}. {_format_pick(p)}" for i, p in enumerate(picks))
    msg = f"✅ Pick list set ({len(picks)} picks):\n```\n{preview}\n```"
    if dupes:
        msg += f"\n⚠️ **Duplicates removed:** {', '.join(dupes)}"
    await ctx.send(msg)

    # Check availability in the background and send warnings as a follow-up
    async def _check_and_warn():
        try:
            availability = await asyncio.get_event_loop().run_in_executor(None, _fetch_availability)
            roster_names = await asyncio.get_event_loop().run_in_executor(None, _fetch_roster_names)
            taken = [p["player"] for p in picks if not _player_available(p["player"], availability, roster_names)]
            if taken:
                await ctx.send(
                    f"⚠️ **Already taken in draft:** {', '.join(taken)} — they'll be skipped automatically when it's your turn. Consider replacing them."
                )
        except Exception:
            pass

    asyncio.create_task(_check_and_warn())


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
        roster_names = await asyncio.get_event_loop().run_in_executor(None, _fetch_roster_names)
        already_taken = not _player_available(pick["player"], availability, roster_names)
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


@bot.command(name="wipelists")
async def cmd_wipelists(ctx):
    """!wipelists — clear ALL drafters' pick lists (owner DM only)."""
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ You don't have permission to use this command.")
        return

    _drafted_this_session.clear()
    wiped = 0
    for user_data in _data.values():
        if user_data.get("picks"):
            user_data["picks"] = []
            wiped += 1
    _save_data(_data)
    await ctx.send(f"✅ Cleared pick lists for **{wiped}** drafter{'s' if wiped != 1 else ''}.")


@bot.command(name="resetall")
async def cmd_resetall(ctx):
    """!resetall — wipe ALL drafter data (picks, emoji, settings) for a fresh ATD (owner DM only)."""
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ You don't have permission to use this command.")
        return

    count = len(_data)
    _data.clear()
    _drafted_this_session.clear()
    _save_data(_data)
    await ctx.send(f"✅ Full reset complete — removed **{count}** drafter record{'s' if count != 1 else ''} (picks, emojis, and settings).")


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
        # Extract the attempted command name from the error string
        attempted = str(error).removeprefix("Command '").split("'")[0]
        all_commands = [c.name for c in bot.commands]
        suggestions = difflib.get_close_matches(attempted, all_commands, n=3, cutoff=0.5)
        msg = f"❌ `!{attempted}` isn't a command."
        if suggestions:
            msg += f" Did you mean: {', '.join(f'`!{s}`' for s in suggestions)}?"
        await ctx.send(msg)
    elif isinstance(error, commands.MissingRequiredArgument) and isinstance(ctx.channel, discord.DMChannel):
        await ctx.send(f"⚠️ Missing argument. Type `!help` for usage.")
    elif not isinstance(error, commands.CheckFailure):
        log.error("Command error in %s: %s", ctx.command, error)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)