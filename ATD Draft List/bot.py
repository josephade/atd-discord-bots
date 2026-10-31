import asyncio
import difflib
import json
import logging
import os
import re
import time
import threading

import discord
import gspread
from discord.ext import commands
from googleapiclient.discovery import build
from oauth2client.service_account import ServiceAccountCredentials
from player_years import get_default_year
from flux_rules import parse_prices, find_player, qualify as qualify_flux_pick, threshold as flux_threshold, failure_message, saved_message, failure_reports

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


def _build_gspread_client():
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, _SCOPES)
    return gspread.authorize(creds)


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
_availability_lock = threading.Lock()
# lowercase → original-case name as it appears in the sheet
_canonical_names: dict[str, str] = {}

# Tracks the last auto-pick so we can retry if the timer bot rejects it
_last_autopick: dict | None = None  # {"user": discord.User, "pick_num": int}
_flux_turn = None
_flux_in_progress = False
FLUX_BOT_ID = int(os.getenv('FLUX_BOT_ID', '1466803932310405181'))
_FLUX_SOURCE_FILE = os.path.join(os.getenv('STATE_DIR', '/data'), 'flux_source.json')


def _flux_source():
    try:
        with open(_FLUX_SOURCE_FILE) as source_file:
            return json.load(source_file)
    except (OSError, ValueError):
        return {'sheet': SPREADSHEET_ID, 'tab': PLAYERS_SHEET_NAME}


def _fetch_flux_book(source=None):
    source = source or _flux_source()
    service = _build_sheets_service()
    try:
        title = source['tab'].replace("'", "''")
        rows = service.spreadsheets().values().get(spreadsheetId=source['sheet'],
            range=f"'{title}'!A:AZ", valueRenderOption='FORMATTED_VALUE').execute().get('values', [])
        return parse_prices(rows)
    finally:
        service.close()


async def _flux_books(picks):
    books = {}
    if globals().get('_flux_in_progress', False):
        return books
    for pick in picks:
        if 'flux_drop' in pick:
            source = pick.get('flux_source') or _flux_source()
            token = json.dumps(source, sort_keys=True)
            if token not in books:
                try:
                    books[token] = await asyncio.to_thread(_fetch_flux_book, source)
                except Exception:
                    log.warning('Flux price read failed; conditional picks will wait.')
                    books[token] = {}
    return books


async def _maybe_flux_pick(uid):
    turn = _flux_turn
    if not turn or int(uid) not in turn['user_ids'] or _data.get(str(uid), {}).get('anon_draft_id'):
        return
    pending = _last_autopick
    if pending and pending['pick_num'] == turn['pick_num'] and pending['user'].id in turn['user_ids']:
        return
    entry = _data.get(str(uid), {})
    if not entry.get('enabled', True) or not any('flux_drop' in p for p in entry.get('picks', [])):
        return
    channel = bot.get_channel(turn['channel_id'])
    user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
    if channel:
        await _do_auto_pick(channel, user, turn['pick_num'])


async def _recover_flux_activity(channel):
    global _flux_in_progress
    _flux_in_progress = True
    try:
        async for message in channel.history(limit=100):
            if message.author.bot and message.author.id == FLUX_BOT_ID:
                text = message.content.replace('*', '').strip()
                if 'flux is in progress' in text.casefold():
                    return
                if re.fullmatch(r'✅\s*Round\s+\d+\s+Flux is done', text, re.I):
                    _flux_in_progress = False
                    return
        _flux_in_progress = False
    except Exception:
        log.warning('Could not recover Flux status; conditional picks will wait for the next Flux completion.')


async def _normal_flux_window(uid, source_round):
    turn = globals().get('_flux_turn')
    if turn and int(uid) in turn['user_ids']:
        return False
    emoji = await _ensure_team_emoji(str(uid))
    lotto = await _load_lotto_teams()
    teams = {value for value in lotto.values() if value}
    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    if not emoji or not teams or channel is None:
        return False
    current_round = source_round
    messages = [message async for message in channel.history(limit=1000)]
    first_pick = next((message.id for message in messages if re.match(r'^\s*1\s*\.', message.content)), None)
    if first_pick is None:
        return False
    _data[str(uid)]['flux_draft_start'] = first_pick
    for message in messages:
        if message.author.id == TIMER_BOT_ID and message.embeds:
            match = re.search(r'Round\s+(\d+)', message.embeds[0].title or '', re.I)
            if match:
                current_round = int(match[1]); break
    for message in messages:
        match = re.match(r'^\s*(\d+)\s*\.', message.content)
        if (match and (current_round-1)*len(teams) < int(match[1]) <= current_round*len(teams)
                and emoji in message.content and not message.content.strip().lower().endswith('skipped')
                and not any(str(reaction.emoji) == '❌' for reaction in getattr(message, 'reactions', []))):
            return True
    return False


async def _settle_normal_flux(completed_round):
    for uid, entry in list(_data.items()):
        if entry.get('anon_draft_id'):
            continue
        rules = [pick for pick in entry.get('picks', []) if 'flux_drop' in pick]
        if not rules:
            continue
        books = await _flux_books(rules)
        notices, remaining = [], []
        for pick in entry.get('picks', []):
            if 'flux_drop' not in pick or pick.get('flux_target_round', completed_round) > completed_round:
                remaining.append(pick); continue
            source = pick.get('flux_source') or _flux_source()
            book = books.get(json.dumps(source, sort_keys=True), {})
            if 'flux_target_round' in pick and qualify_flux_pick(pick, book) is not None:
                remaining.append(pick)
            else:
                notices.append(failure_message(pick, book) if qualify_flux_pick(pick, book) is None else
                               f"Your old Flux rule for {pick['player']} was cleared because it was not tied to an upcoming Flux. Finish your current pick before submitting a fresh rule.")
        if notices:
            entry['picks'] = remaining
            _save_data(_data)
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            for notice in failure_reports(notices):
                await user.send(notice)

def _iter_player_rows():
    """Read only names and fill colours, in bounded batches.

    Fetching all grid metadata for A:D can exhaust the bot's 512 MB VM,
    especially when formatting extends far beyond the populated players.
    The values endpoint gives the last populated row without that metadata.
    """
    service = _build_sheets_service()
    title = PLAYERS_SHEET_NAME.replace("'", "''")
    try:
        api = service.spreadsheets()
        values = api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{title}'!A:D",
            fields="values",
        ).execute()
        row_count = len(values.get("values", []))
        del values
        for start in range(1, row_count + 1, 250):
            result = api.get(
                spreadsheetId=SPREADSHEET_ID,
                ranges=[f"'{title}'!A{start}:D{min(start + 249, row_count)}"],
                includeGridData=True,
                fields="sheets(data(rowData(values(formattedValue,effectiveFormat(backgroundColor)))))",
            ).execute()
            for sheet in result.get("sheets", []):
                for block in sheet.get("data", []):
                    for offset, row in enumerate(block.get("rowData", [])):
                        yield start + offset, row
            del result
    finally:
        service.close()


def _fetch_availability(force: bool = False) -> dict:
    # Background list checks and a live auto-pick can run at the same time.
    # Keep only one grid fetch in flight, including cache updates.
    with _availability_lock:
        return _fetch_availability_inner(force)


def _fetch_availability_inner(force: bool = False) -> dict:
    """
    Read the players sheet with grid data and return a mapping:
        player_name_lower -> True (available) | False (blacked out / taken)
    Results are cached for 60 seconds to avoid redundant API calls.
    """
    global _availability_cache, _availability_cache_ts
    now = time.monotonic()
    if not force and _availability_cache is not None and (now - _availability_cache_ts) < _AVAILABILITY_TTL:
        return _availability_cache

    availability: dict[str, bool] = {}
    canonical_names = {}
    for _, row in _iter_player_rows():
        for cell in row.get("values", []):
            text = (cell.get("formattedValue") or "").strip()
            if text:
                availability[text.lower()] = not _is_taken(cell)
                canonical_names[text.lower()] = text

    _canonical_names.clear()
    _canonical_names.update(canonical_names)
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
    if not ROSTER_SHEET_NAME:
        return set()
    now = time.monotonic()
    if not force and _roster_cache is not None and (now - _roster_cache_ts) < _AVAILABILITY_TTL:
        return _roster_cache

    # Fetched via gspread's title lookup, not a raw A1 range string — the
    # Sheets values().get() API's range parser can't handle a "/" inside a
    # quoted sheet title (confirmed: it 400s on the bare quoted title alone,
    # before any range suffix is even added), which silently broke this for
    # any tab name containing one, e.g. "ATD 111 - Risk/Reward Random R2".
    client = _build_gspread_client()
    sh     = client.open_by_key(SPREADSHEET_ID)
    ws     = sh.worksheet(ROSTER_SHEET_NAME)
    values = ws.get_all_values()

    names: set[str] = set()
    for row in values:
        for cell in row:
            text = str(cell).strip()
            if text:
                names.add(text.lower())

    _roster_cache = names
    _roster_cache_ts = now
    log.info("ROSTER | fetched %d cell entries from '%s'", len(names), ROSTER_SHEET_NAME)
    return names


def _player_available(player_name: str, availability: dict, roster_names: set | None = None) -> bool:
    """Check availability — taken if EITHER source says so.

    The color sheet (PLAYERS_SHEET_NAME) is maintained by ATD Sheet Bot's
    fuzzy name-matching, which can silently miss a name it doesn't
    recognize (seen in practice — odd punctuation, nicknames, etc.), and
    would then leave that player looking "available" here forever. The
    roster sheet (ROSTER_SHEET_NAME) is written directly by Team Sheet Bot
    and is the more reliable ground truth, so it's always checked too — not
    just as a fallback when the color sheet is entirely missing."""
    name_lower = player_name.lower().strip()

    # A previous auto-pick may be seconds ahead of Sheets' colour update.
    # Honour the live channel tracker as well as the two sheet sources.
    if any(name_lower in drafted for drafted in _drafted_this_session):
        return False

    # Roster sheet — ground truth, written directly by Team Sheet Bot.
    if roster_names is not None:
        if name_lower in roster_names:
            return False
        for roster_name in roster_names:
            if name_lower in roster_name:
                return False

    # Color sheet — maintained by ATD Sheet Bot's fuzzy matching.
    if availability:
        if name_lower in availability:
            return availability[name_lower]
        for sheet_name, avail in availability.items():
            if name_lower in sheet_name:
                return avail

    log.warning("Player '%s' not found in either sheet — assuming available", player_name)
    return True


# ── Pick text helpers ─────────────────────────────────────────────────────────
_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
# Alternatives: 'YY (apostrophe) | YYYY-YY(YY) | bare YYYY | YY-YY (no apostrophe,
# e.g. "22-23" — same short-form GMs use without thinking to add the apostrophe).
_YEAR_RE  = re.compile(r"'(\d{2})\b|(\d{4})-(\d{2,4})\b|\b(\d{4})\b|\b(\d{2})-(\d{2})\b")


def _with_default_year(pick: dict) -> dict:
    """Fill only omitted years from the commissioner's recorded defaults."""
    if pick.get("year"):
        return pick
    year = get_default_year(pick["player"])
    return {**pick, "year": year} if year else pick


def _parse_pick_line(text: str) -> dict:
    """Parse 'LeBron James '23 $5' → {player, year, price}."""
    year  = None
    price = None

    # Normalize curly/smart apostrophes -> straight apostrophe so the year
    # regex matches (e.g. "'92" typed on a phone often autocorrects to "'92"
    # with a curly quote). Without this, the year never gets split out and
    # stays stuck as literal text inside the player name, which then can't
    # match the roster sheet's clean name when checking availability.
    text = text.replace('‘', "'").replace('’', "'").replace('ʼ', "'")

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
        elif ym.group(5) and ym.group(6):
            # Same century-cutoff convention Team Sheet Bot's _normalize_year
            # uses for this same short form: 46-99 -> 1900s, 00-45 -> 2000s.
            start   = int(ym.group(5))
            century = 1900 if start >= 46 else 2000
            year    = f"{century + start}-{ym.group(6)}"
        text = (text[: ym.start()] + text[ym.end() :]).strip()

    return _with_default_year({"player": text, "year": year, "price": price})


def _format_pick(pick: dict) -> str:
    pick = _with_default_year(pick)
    parts = [pick["player"]]
    if pick.get("year"):
        parts.append(f"'{pick['year'][-2:]}")
    if 'flux_drop' in pick:
        parts.append(f"[Flux Round {pick.get('flux_target_round', '?')}: drop >= ${pick['flux_drop']:g}]")
    elif pick.get("price") is not None:
        parts.append(f"${pick['price']}")
    if pick.get("lock"):
        parts.append("🔒")
    return " ".join(parts)


def _is_valid_emoji(s: str) -> bool:
    """Return True if s looks like a real emoji (custom or text), not corrupted data."""
    if not s:
        return False
    s = s.strip()
    custom = re.fullmatch(r'<a?:[^:]+:(\d+)>', s)
    if custom:
        return any(e.id == int(custom.group(1)) and getattr(e, 'available', True)
                   for e in bot.emojis)
    # :OKC: is plain text. Only a real Discord mention or Unicode emoji
    # can be used as the team's marker in a draft pick.
    return bool(re.fullmatch(
        r'(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF])'
        r'(?:[\uFE0F\u200D\U0001F3FB-\U0001F3FF]|[\U0001F300-\U0001FAFF\u2600-\u27BF])*', s))


def _build_pick_message(pick_num: int, emoji: str, pick: dict) -> str:
    if not _is_valid_emoji(emoji):
        raise ValueError("A resolved team emoji is required before posting a pick")
    # Also covers lists saved before default seasons were supported.
    pick = _with_default_year(pick)
    parts = [f"{pick_num}."]
    if emoji:
        parts.append(emoji)
    parts.append(pick["player"])
    if pick.get("year"):
        parts.append(f"'{pick['year'][-2:]}")
    if pick.get("price") is not None:
        parts.append(f"${pick['price']}")
    if pick.get("lock"):
        # Timer Bot's lock marker must be the trailing token in the message.
        parts.append("lock")
    return " ".join(parts)


# ── Auto-pick logic ───────────────────────────────────────────────────────────
_lotto_teams_cache: dict | None = None
_lotto_teams_cache_ts = 0.0
_lotto_lookup_lock = asyncio.Lock()


async def _load_lotto_teams() -> dict:
    """Read the newest lotto, including co-GMs. Never fill gaps from old drafts."""
    global _lotto_teams_cache, _lotto_teams_cache_ts
    async with _lotto_lookup_lock:
        now = time.monotonic()
        if _lotto_teams_cache is not None and now - _lotto_teams_cache_ts < 60:
            return _lotto_teams_cache
        teams = await _scan_lotto_teams()
        _lotto_teams_cache = teams
        _lotto_teams_cache_ts = now
        return teams


async def _scan_lotto_teams() -> dict:
    search_channels = []
    if LOTTO_CHANNEL_ID:
        ch = bot.get_channel(LOTTO_CHANNEL_ID)
        if ch:
            search_channels.append(ch)
    draft_ch = bot.get_channel(DRAFT_CHANNEL_ID)
    if draft_ch and draft_ch not in search_channels:
        search_channels.append(draft_ch)

    for ch in search_channels:
        try:
            async for msg in ch.history(limit=500):
                if re.search(r'^\s*1\.', msg.content, re.MULTILINE):
                    lines = list(_extract_lotto_lines(msg.content))
                    if len(lines) < 2:
                        continue
                    return {uid: _resolve_emoji_input(emoji) for emoji, uids in lines for uid in uids}
        except Exception as e:
            log.warning("LOTTO LOOKUP | channel %d failed: %s", ch.id, e)
    return {}


async def _lookup_emoji_from_lotto(user_id: int) -> str | None:
    return (await _load_lotto_teams()).get(user_id)


async def _ensure_team_emoji(uid: str) -> str:
    """Repair missing/shortcode markers before a list is shown or used."""
    _ensure_user(uid)
    original = _data[uid].get("emoji", "")
    resolved = _resolve_emoji_input(original)
    if not _is_valid_emoji(resolved):
        resolved = _resolve_emoji_input(await _lookup_emoji_from_lotto(int(uid)) or "")
    if _is_valid_emoji(resolved):
        if resolved != original:
            _data[uid]["emoji"] = resolved
            _save_data(_data)
            log.info("TEAM LINK | uid=%s | %r -> %s", uid, original, resolved)
        return resolved
    return ""


async def _repair_team_emojis():
    for uid in list(_data):
        try:
            await _ensure_team_emoji(uid)
        except Exception as e:
            log.warning("TEAM LOOKUP | uid=%s failed: %s", uid, e)


async def _confirm_team_setup(ctx, uid: str):
    try:
        emoji = await _ensure_team_emoji(uid)
    except Exception as e:
        log.warning("TEAM LOOKUP | uid=%s failed: %s", uid, e)
        emoji = ""
    if emoji:
        await ctx.send(f"Team linked: {emoji}. Your list is ready for auto-picking.")
    else:
        await ctx.send(
            "⚠️ List saved, but I couldn't identify the team from the current lotto. "
            "Auto-picking needs a team emoji. Use `!setemoji` in the server and "
            "select the actual team emoji from Discord's emoji picker."
        )


async def _do_auto_pick(channel: discord.TextChannel, user: discord.User, pick_num: int,
                         extra_excluded: set[str] | None = None, retry_picks: list | None = None):
    async with _autopick_lock:
        await _do_auto_pick_inner(channel, user, pick_num, extra_excluded or set(), retry_picks)


async def _do_auto_pick_inner(channel: discord.TextChannel, user: discord.User, pick_num: int,
                               extra_excluded: set[str], retry_picks: list | None = None):
    """extra_excluded: player names (lowercase) to skip for THIS call only —
    e.g. a player the user was just barred from repicking after being
    blocked/stolen from (Timer Bot-side rule, not a global availability
    fact, so it can't just go in _drafted_this_session — that would
    incorrectly hide the player from every OTHER team's auto-pick too)."""
    global _last_autopick
    uid       = str(user.id)
    user_data = _data.get(uid, {})

    if user_data.get('anon_draft_id'):
        return  # Theme Bot owns anonymous turns; never fall back to public picks.

    if not user_data.get("enabled", True):
        log.info("AUTO-PICK | %s | paused — skipping", user.display_name)
        return

    source_list = user_data.get("picks", [])
    source_lists = {team_uid: entry.get("picks") for team_uid, entry in _data.items()}
    picks = list(retry_picks if retry_picks is not None else source_list)
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
    resolved = _resolve_emoji_input(emoji)
    if resolved != emoji:
        # A `:name:` shorthand that failed to resolve when it was first set
        # (e.g. the bot wasn't yet in the emoji's server, or the cache wasn't
        # warm) gets one more chance here — cheap, and only ever an upgrade:
        # a plain string that already resolved, or already a real mention,
        # comes back unchanged.
        log.info("AUTO-PICK | %s | emoji re-resolved: %r -> %r", user.display_name, emoji, resolved)
        emoji = resolved
        _data[uid]["emoji"] = emoji
        _save_data(_data)

    if not _is_valid_emoji(emoji):
        log.info("AUTO-PICK | %s | no emoji set — searching lotto message", user.display_name)
        found = _resolve_emoji_input(await _lookup_emoji_from_lotto(user.id) or "")
        if found and _is_valid_emoji(found):
            log.info("AUTO-PICK | %s | emoji auto-found from lotto: %s", user.display_name, found)
            emoji = found
            _data[uid]["emoji"] = emoji
            _save_data(_data)
        else:
            log.warning("AUTO-PICK | %s | emoji not found in lotto — aborting", user.display_name)
            try:
                await user.send(
                    f"⚠️ It's your turn (Pick **#{pick_num}**) but I couldn't find your team emoji "
                    f"in the lotto message either.\n\n"
                    f"Ask the commissioner to run `!setemoji @{user.display_name} <:emoji:id>` "
                    f"in the server, then you'll be good to go.\n\n"
                    f"You need to respond manually in the draft channel for this pick."
                )
            except Exception:
                pass
            try:
                owner = await bot.fetch_user(OWNER_ID)
                if owner:
                    await owner.send(
                        f"⚠️ Auto-pick failed for **{user.display_name}** (Pick **#{pick_num}**) — "
                        f"no team emoji found in the lotto message.\n"
                        f"Run `!setemoji @{user.display_name} <:emoji:id>` to fix it."
                    )
            except Exception:
                pass
            return

    # Fetch BOTH sources every pick, independently — not one as a fallback for
    # the other. The color sheet can silently miss a name ATD Sheet Bot's
    # fuzzy matcher didn't recognize; the roster sheet (written directly by
    # Team Sheet Bot) is what catches that. See _player_available().
    availability = {}
    roster_names = set()
    try:
        availability = await asyncio.to_thread(_fetch_availability, force=True)
        log.info("AUTO-PICK | fetched %d player entries from PLAYERS sheet", len(availability))
    except Exception as e:
        log.error("AUTO-PICK | PLAYERS sheet fetch failed: %s", e)
    try:
        roster_names = await asyncio.to_thread(_fetch_roster_names, force=True)
        log.info("AUTO-PICK | fetched %d entries from ROSTER sheet", len(roster_names))
    except Exception as e:
        log.error("AUTO-PICK | ROSTER sheet fetch also failed: %s", e)

    # Walk list — find first available player
    flux_books = await _flux_books(picks) if any('flux_drop' in p for p in picks) else {}
    chosen_idx  = None
    chosen_pick = None
    skipped     = []

    for idx, pick in enumerate(picks):
        name_lower = pick["player"].lower()
        if name_lower in extra_excluded:
            skipped.append(pick["player"])
            log.info("AUTO-PICK | %s | '%s' barred for this repick — skipping", user.display_name, pick["player"])
        elif _player_available(pick["player"], availability, roster_names):
            if 'flux_drop' in pick:
                if 'flux_target_round' not in pick:
                    continue
                source = pick.get('flux_source') or _flux_source()
                eligible = qualify_flux_pick(pick, flux_books.get(json.dumps(source, sort_keys=True), {}))
                if eligible is None:
                    continue
                pick = eligible
            chosen_idx  = idx
            chosen_pick = pick
            break
        else:
            skipped.append(pick["player"])
            log.info("AUTO-PICK | %s | '%s' already taken — skipping", user.display_name, pick["player"])

    if chosen_pick is None:
        if any('flux_drop' in p for p in picks):
            await user.send('No available player currently meets your list conditions, or live Flux prices could not be verified. Your timer keeps running. I will recheck after Flux or a list update while it is your turn.')
            return
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
    if 'flux_drop' in chosen_pick:
        turn = globals().get('_flux_turn')
        if not turn or turn['pick_num'] != pick_num or user.id not in turn['user_ids']:
            return  # The authoritative Timer prompt changed during the price read.
    _drafted_this_session.add(chosen_pick["player"].lower())

    # Set BEFORE posting (not after) — the gateway can echo our own message back
    # into on_message before channel.send() even returns, and the real-time pick
    # tracker below relies on _last_autopick already pointing at this user to know
    # the announcement it just saw is its own auto-pick, not a manual pick to react to.
    previous_autopick = _last_autopick
    pending = {"user": user, "pick_num": pick_num,
               "retry_picks": picks[chosen_idx + 1:], "posted_at": time.monotonic()}
    _last_autopick = pending
    team_lists = {
        team_uid: source_lists.get(team_uid) for team_uid, entry in _data.items()
        if not entry.get('anon_draft_id') and (team_uid == uid or _resolve_emoji_input(entry.get("emoji", "")) == emoji)
    }

    # Post the pick
    pick_msg = _build_pick_message(pick_num, emoji, chosen_pick)
    try:
        await channel.send(pick_msg)
    except Exception:
        _drafted_this_session.discard(chosen_pick["player"].lower())
        if _last_autopick is pending:
            _last_autopick = previous_autopick
        raise
    log.info("AUTO-PICK | %s | Pick #%d | %s", user.display_name, pick_num, pick_msg)

    # The active list belongs to this turn only. Clear it immediately and
    # persist the empty list, so a restart or fast next turn cannot reuse it.
    # Retry options live only in the pending attempt for this pick number.
    # Preserve a fresh list submitted while channel.send was in flight.
    if retry_picks is None:
        changed = False
        for team_uid, saved_list in team_lists.items():
            if saved_list and _data[team_uid].get("picks") is saved_list:
                _data[team_uid]["picks"] = []
                changed = True
                log.info("ROUND COMPLETE | cleared remaining picks for uid=%s", team_uid)
        if changed:
            _save_data(_data)

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

from anon_bridge import AnonymousLists, BridgeError

def _anon_guild_id():
    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    return getattr(getattr(channel, 'guild', None), 'id', None) or int(os.getenv('DISCORD_GUILD_ID', '0'))

_anonymous_lists = AnonymousLists(bot, _data, _save_data, _parse_pick_line, _anon_guild_id, owner=OWNER_ID, price_source=_flux_source)

@bot.check
async def _anonymous_command_check(ctx):
    if ctx.command.name in ('wipelists', 'resetall', 'clearlist', 'setlistfor') and any(e.get('anon_draft_id') for e in _data.values()):
        await ctx.send('Anonymous lists are controlled privately by Theme Bot. Use its commissioner tools, or have the owner run !draftmode normal before a global reset.')
        return False
    return await _anonymous_lists.check(ctx)

@bot.command(name='draftmode')
async def cmd_draftmode(ctx, *, mode: str = ''):
    if ctx.guild is not None and ctx.guild.id != _anon_guild_id():
        await ctx.send('Use this command in the ATD server or DM Draft List Bot.')
        return
    try:
        await _anonymous_lists.mode(ctx, mode.strip().lower())
    except BridgeError as exc:
        await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())


@bot.command(name='fluxsource')
async def cmd_fluxsource(ctx, sheet_url: str = '', *, tab: str = ''):
    if ctx.author.id != OWNER_ID:
        await ctx.send('Only the bot owner can configure the Flux price sheet.')
        return
    if not sheet_url:
        source = _flux_source()
        await ctx.send(f"Flux price tab: **{source['tab']}**. Use `!fluxsource <Google Sheet URL> <tab name>` to change it, or `!fluxsource reset`.")
        return
    if sheet_url.lower() == 'reset':
        source = {'sheet': SPREADSHEET_ID, 'tab': PLAYERS_SHEET_NAME}
    else:
        match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', sheet_url)
        if not match or not tab.strip():
            await ctx.send('Use `!fluxsource <Google Sheet URL> <tab name>`. The tab must contain Player, Value and Round N columns.')
            return
        source = {'sheet': match[1], 'tab': tab.strip()}
    try:
        await asyncio.to_thread(_fetch_flux_book, source)
    except Exception:
        await ctx.send('Could not validate that Flux price tab. Check access and its Player / Value headers. The previous source was kept.')
        return
    os.makedirs(os.path.dirname(_FLUX_SOURCE_FILE), exist_ok=True)
    with open(_FLUX_SOURCE_FILE, 'w') as source_file:
        json.dump(source, source_file)
    try:
        await _anonymous_lists.sync_price_source(source)
    except BridgeError as exc:
        await ctx.send(f'The Draft List source was saved, but Theme Bot could not confirm it: {exc}. Rerun this command before continuing the anonymous draft.')
        return
    await ctx.send('Flux price source saved. New conditional picks will use this tab; existing rules retain their original source.')


@bot.command(name='fluxpick')
async def cmd_fluxpick(ctx, *, text: str = ''):
    if not _dm_only(ctx):
        await ctx.send('DM this command to Draft List Bot: `!fluxpick <player> [year] <drop amount>`.')
        return
    try:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not 1 <= len(lines) <= 100:
            raise ValueError('Use `!fluxpick` followed by one player and minimum drop per line. Example: `Alvan Adams 2`. Up to 100 entries.')
        source = _flux_source()
        book = await asyncio.to_thread(_fetch_flux_book, source)
        additions, seen = [], set()
        for number, line in enumerate(lines, 1):
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                raise ValueError(f'Line {number}: expected a player and minimum drop, for example Alvan Adams 2. No rules were saved.')
            drop = flux_threshold(parts[1])
            pick = _parse_pick_line(parts[0])
            record = find_player(book, pick['player'])
            if record['current'] is None:
                raise ValueError('The latest Flux price is not available yet. Wait until Flux finishes.')
            if record['player'].casefold() in seen:
                raise ValueError(f"{record['player']} appears twice. Each player can only be submitted once. No rules were saved.")
            seen.add(record['player'].casefold())
            pick.update(player=record['player'], price=float(record['current']), flux_drop=drop, flux_source=source,
                        flux_from_round=record['round'], flux_target_round=record['round']+1)
            additions.append(pick)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    except Exception:
        await ctx.send('Could not read the live Flux prices. No rule was saved. Ask the owner to check !fluxsource.')
        return
    uid = str(ctx.author.id)
    if _anonymous_lists.linked(uid):
        try:
            await _anonymous_lists.add_flux(ctx, additions)
        except BridgeError as exc:
            await ctx.send(str(exc))
        return
    _ensure_user(uid)
    if _flux_in_progress or not await _normal_flux_window(uid, record['round']):
        await ctx.send('Finish your team’s current-round pick before setting a Flux pick for the next round. Flux rules must be submitted before Flux starts.')
        return
    existing = _data[uid]['picks']
    tokens = []
    for pick in additions:
        token = json.dumps([_data[uid].get('flux_draft_start'), pick['player'].casefold(), source, pick['flux_target_round']], sort_keys=True)
        if any(p['player'].casefold() == pick['player'].casefold() for p in existing) or token in _data[uid].get('flux_rule_attempts', []):
            await ctx.send(f"You already submitted {pick['player']} for the upcoming Flux. No new rules were saved.")
            return
        tokens.append(token)
    if len(existing)+len(additions) > 100:
        await ctx.send('Your list can contain up to 100 players. No new rules were saved.')
        return
    try:
        available = await asyncio.to_thread(_fetch_availability, force=True)
        rosters = await asyncio.to_thread(_fetch_roster_names, force=True)
        for pick in additions:
            if not _player_available(pick['player'], available, rosters):
                await ctx.send(f"{pick['player']} has already been drafted. No new rules were saved.")
                return
    except Exception:
        await ctx.send('Could not verify player availability. No Flux rule was saved; please try again.')
        return
    _data[uid].setdefault('flux_rule_attempts', []).extend(tokens)
    existing.extend(additions)
    _save_data(_data)
    await ctx.send(saved_message(additions))


def _resolve_emoji_input(raw: str) -> str:
    """A real `<a?:name:id>` mention or native unicode emoji passes through unchanged.
    A plain `:name:` shorthand — what you get typing colons by hand in a DM, where
    Discord's autocomplete (which inserts the real mention) needs Nitro to reach
    another server's emoji — gets resolved by name against every custom emoji this
    bot can see and swapped for the real mention, since Discord never renders bare
    `:name:` text as an emoji on its own. Left as-is if no match is found."""
    s = (raw or "").strip()
    custom = re.fullmatch(r'<a?:([^:]+):(\d+)>', s)
    m = re.fullmatch(r':([A-Za-z0-9_~]+):', s)
    if custom:
        for emoji in bot.emojis:
            if emoji.id == int(custom.group(2)) and getattr(emoji, 'available', True):
                return str(emoji)
        name = custom.group(1).lower()
    elif m:
        name = m.group(1).lower()
    else:
        return s
    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    guild = getattr(channel, 'guild', None)
    # Prefer the draft server when multiple guilds use the same emoji name.
    for e in list(getattr(guild, 'emojis', [])) + list(bot.emojis):
        if e.name.lower() == name and getattr(e, 'available', True):
            return str(e)
    return s


_PICK_NUM_RE = re.compile(r"Pick\s+(\d+)", re.IGNORECASE)

# A steal/block declaration (e.g. "3. steal LeBron James $34") must not be
# treated as a literal pick announcement by the real-time tracker below —
# Timer Bot is the sole referee for these; this bot only reacts to its
# SBL_BLOCK/SBL_VETO confirmations (see the on_message handler).
_SBL_STEAL_INTENT_RE = re.compile(r'\b(steal|steals|stole|stolen|stealing)\b', re.IGNORECASE)
_SBL_BLOCK_INTENT_RE = re.compile(r'\b(block|blocks|blocked|blocking)\b', re.IGNORECASE)


def _has_sbl_intent(content: str) -> bool:
    is_steal = bool(_SBL_STEAL_INTENT_RE.search(content))
    is_block = bool(_SBL_BLOCK_INTENT_RE.search(content))
    return is_steal != is_block


def _reresolve_stored_emojis():
    """One-time self-heal: a `:name:` shorthand saved before this bot could resolve
    guild emojis (e.g. typed by hand in a Nitro-less DM) is stuck as literal text
    forever unless something re-checks it now that bot.emojis is populated."""
    fixed = 0
    for uid, user_data in _data.items():
        raw = (user_data.get("emoji") or "").strip()
        if not raw:
            continue
        resolved = _resolve_emoji_input(raw)
        if resolved != raw:
            user_data["emoji"] = resolved
            fixed += 1
            log.info("EMOJI SELF-HEAL | uid=%s | %r -> %r", uid, raw, resolved)
    if fixed:
        _save_data(_data)
    log.info("EMOJI SELF-HEAL | checked %d users, fixed %d", len(_data), fixed)


@bot.event
async def on_ready():
    log.info("ATD Draft List Bot ready — %s", bot.user)
    _anonymous_lists.start()
    _reresolve_stored_emojis()
    asyncio.create_task(_repair_team_emojis())
    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    if channel:
        await _recover_flux_activity(channel)
        asyncio.create_task(_backfill_drafted_session(channel))


def _current_draft_history(messages):
    """Accept newest-first history only when this draft's pick #1 is found."""
    current = []
    for message in messages:
        current.append(message)
        if re.match(r'^1\s*\.', message.content.strip()) and not _has_sbl_intent(message.content):
            return list(reversed(current))
    return []


async def _backfill_drafted_session(channel: discord.TextChannel):
    """Seed sheet-lag protection from this draft only, never older drafts.

    Sheet/roster availability is checked directly on each lookup. Copying it
    into the session set would make a corrected sheet entry stay taken forever.
    """
    try:
        messages = []
        async for msg in channel.history(limit=2000):
            messages.append(msg)
            if re.match(r'^1\s*\.', msg.content.strip()) and not _has_sbl_intent(msg.content):
                break
        current = _current_draft_history(messages)
        if not current:
            log.warning("BACKFILL | no current-draft pick #1 found; using sheets and live picks only")
            return
        all_pick_names = {
            p["player"].lower()
            for user_data in _data.values()
            for p in user_data.get("picks", [])
        }
        count = 0
        for msg in current:
            if msg.author.bot and (not TIMER_BOT_ID or msg.author.id == TIMER_BOT_ID):
                for line in msg.content.splitlines():
                    if line.strip().startswith(("SBL_BLOCK |", "SBL_VETO |")):
                        parts = line.split("|")
                        if len(parts) >= 2:
                            _drafted_this_session.discard(parts[1].strip().lower())
            if re.match(r'^\d+\s*\.', msg.content.strip()) and not _has_sbl_intent(msg.content):
                content_lower = msg.content.lower()
                for name in all_pick_names:
                    if name in content_lower and name not in _drafted_this_session:
                        _drafted_this_session.add(name)
                        count += 1
        log.info("BACKFILL | marked %d current-draft players taken; older drafts excluded", count)
    except Exception as e:
        log.error("BACKFILL | channel history failed: %s", e)


@bot.event
async def on_message(message: discord.Message):
    global _flux_turn, _flux_in_progress
    if message.author.bot and re.fullmatch(r'\s*\d+\.\s+.+?\s+skipped\s*', message.content, re.I):
        return
    if (message.author.bot and message.author.id == globals().get('FLUX_BOT_ID', 1466803932310405181)
            and message.channel.id == DRAFT_CHANNEL_ID):
        text = message.content.replace('*', '').strip()
        if 'flux is in progress' in text.casefold():
            _flux_in_progress = True
            return
        if re.fullmatch(r'✅\s*Round\s+\d+\s+Flux is done', text, re.I):
            _flux_in_progress = False
            await _settle_normal_flux(int(re.search(r'Round\s+(\d+)', text, re.I)[1]))
            if _flux_turn:
                for uid in _flux_turn['user_ids']:
                    await _maybe_flux_pick(str(uid))
            return
    global _last_autopick, _availability_cache, _roster_cache

    # ── Timer bot rejection — retry with next available pick ──────────────────
    # Fires on either rejection Timer Bot sends for an auto-posted pick:
    #   "**Player** has already been taken ... pick someone else." (real dupe —
    #     globally unavailable, safe to blacklist for everyone)
    #   "**Player** was just blocked from you ... pick someone else." (SBL veto —
    #     barred only for THIS specific team's repick, NOT a global fact, so it
    #     must not go in _drafted_this_session or it'd wrongly hide the player
    #     from every other team's auto-pick too)
    if (
        message.author.bot
        and message.channel.id == DRAFT_CHANNEL_ID
        and (not TIMER_BOT_ID or message.author.id == TIMER_BOT_ID)
        and bot.user in message.mentions
        and "pick someone else" in message.content.lower()
        and ("has already been taken" in message.content or "was just blocked from you" in message.content)
        and _last_autopick
        and time.monotonic() - _last_autopick["posted_at"] < 60
    ):
        retry_excluded: set[str] = set()
        m = re.search(r'[—–-]\s*\*{0,2}(.+?)\*{0,2}\s+has already been taken', message.content)
        if m:
            _drafted_this_session.add(m.group(1).strip().lower())
            log.info("REJECTION | '%s' confirmed taken — retrying", m.group(1).strip())
        else:
            m = re.search(r'\*\*(.+?)\*\*\s+was just blocked from you', message.content)
            if m:
                retry_excluded.add(m.group(1).strip().lower())
                log.info("REJECTION | '%s' barred for this repick — retrying (not globally excluded)", m.group(1).strip())
        retry_user     = _last_autopick["user"]
        retry_pick_num = _last_autopick["pick_num"]
        retry_picks    = _last_autopick["retry_picks"]
        _last_autopick = None
        asyncio.create_task(_do_auto_pick(message.channel, retry_user, retry_pick_num, retry_excluded, retry_picks))
        return

    # ── Timer bot SBL_BLOCK / SBL_VETO — player back in the pool ──────────────
    # A blocked (or veto'd, i.e. an invalid repick that got undone) player
    # becomes draftable again. _player_available() already re-checks the live
    # sheet on every auto-pick, so this isn't needed for auto-pick correctness
    # — it just keeps _drafted_this_session accurate for the real-time
    # tracker's own cross-off/DM logic, so a later re-pick of this player
    # isn't silently ignored as "already handled".
    if (
        message.author.bot
        and message.channel.id == DRAFT_CHANNEL_ID
        and (not TIMER_BOT_ID or message.author.id == TIMER_BOT_ID)
        and ("SBL_BLOCK |" in message.content or "SBL_VETO |" in message.content)
    ):
        for line in message.content.splitlines():
            line = line.strip()
            if line.startswith("SBL_BLOCK |") or line.startswith("SBL_VETO |"):
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2:
                    freed = parts[1].strip().lower()
                    _drafted_this_session.discard(freed)
                    log.info("SBL FREED | '%s' available again", parts[1].strip())
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
        _flux_turn = None

        if "your turn" in desc.lower():
            m = _PICK_NUM_RE.search(embed.title or "")
            if m:
                pick_num = int(m.group(1))
                _flux_turn = {'pick_num': pick_num, 'channel_id': message.channel.id,
                              'user_ids': [member.id for member in message.mentions]}
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
    # Any attempted pick invalidates the cached turn until Timer confirms it again.
    attempted = re.match(r'^\s*(\d+)\s*\.', message.content)
    if attempted and globals().get('_flux_turn') and int(attempted[1]) == _flux_turn['pick_num']:
        _flux_turn = None
    # Any message that starts with "N." or "N ." is a pick announcement.
    # Check it against every registered pick list and mark matched players as
    # drafted so auto-picks skip them immediately — even before the sheet updates.
    # Also notify users via DM and remove the player from their list.
    if (
        message.channel.id == DRAFT_CHANNEL_ID
        and re.match(r'^\d+\s*\.', message.content.strip())
        and not _has_sbl_intent(message.content)
    ):
        content_lower = message.content.lower()
        if re.match(r'^1\s*\.', message.content.strip()):
            _drafted_this_session.clear()
            _availability_cache = None
            _roster_cache = None
            log.info("NEW DRAFT | pick #1 cleared previous draft tracking and sheet caches")

        # An auto-posted pick is announced under THIS bot's own account, so
        # message.author is us, not the GM it was picked for — _do_auto_pick_inner
        # already updated that GM's own list and sent their confirmation DM, so
        # attribute the announcement to them (not to us) and skip re-processing
        # their list here, which would otherwise race _do_auto_pick_inner's own
        # pop of the same entry and/or DM them a confusing "drafted by <this bot>".
        auto_pick_uid = None
        if message.author.id == bot.user.id and _last_autopick:
            auto_pick_uid = str(_last_autopick["user"].id)
            picker_name = _last_autopick["user"].display_name
        else:
            picker_name = message.author.display_name

        log.info("PICK DETECTED | channel=%d author=%s content=%.80s",
                 message.channel.id, picker_name, message.content.strip())

        # Track against all registered users' pick lists
        for uid, user_data in _data.items():
            if user_data.get('anon_draft_id'):
                continue
            if uid == auto_pick_uid:
                continue
            picks = user_data.get("picks", [])
            if not picks:
                continue
            team_emoji = _resolve_emoji_input(user_data.get("emoji", ""))
            is_picker = (message.author.id == int(uid)
                         or (_is_valid_emoji(team_emoji) and team_emoji in message.content))
            # Any manual pick by the owner completes their turn, including
            # a player not on their list or already seen by the live tracker.
            if is_picker:
                _data[uid]["picks"] = []
                _save_data(_data)
                log.info("ROUND COMPLETE | manual pick cleared list for uid=%s", uid)
                continue
            for pick in list(picks):
                name_lower = pick["player"].lower()
                if name_lower in content_lower:
                    _drafted_this_session.add(name_lower)
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
    # Skipped for owner/admin commands run against the system itself (not a
    # genuine first pick-list interaction) -- !alllists in particular is run
    # by the commissioner checking everyone's lists, not a new drafter's
    # first message, so it shouldn't trigger (or consume) the greeting.
    _first_word = message.content.strip().split()[0].lower() if message.content.strip() else ''
    _is_admin_command = _first_word.lstrip('!') in ('alllists',)
    if isinstance(message.channel, discord.DMChannel) and not _is_admin_command:
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
    await _confirm_team_setup(ctx, uid)
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

    resolved = _resolve_emoji_input(emoji_str)
    if not _is_valid_emoji(resolved):
        await ctx.send("❌ That emoji hasn't resolved. Select the actual team emoji from Discord's emoji picker in the server, then run `!setemoji` again.")
        return
    warning = ""
    if resolved == emoji_str and re.match(r'^:[A-Za-z0-9_~]+:$', emoji_str):
        warning = (
            f"\n⚠️ I couldn't find a custom emoji named `{emoji_str.strip(':')}` to match that against, "
            f"so it'll show up as plain text `{emoji_str}` instead of an actual emoji. "
            f"Type `!setemoji` in the **server** and pick the emoji from Discord's autocomplete instead."
        )
    emoji_str = resolved

    uid = str(target_member.id)
    _ensure_user(uid)
    _data[uid]["emoji"] = emoji_str
    _save_data(_data)

    if target_member.id == ctx.author.id:
        await ctx.send(f"✅ Your team emoji is set to {emoji_str}{warning}")
    else:
        await ctx.send(f"✅ Set **{target_member.display_name}**'s team emoji to {emoji_str}{warning}")


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
        name_lower = player_name.lower().strip()
        matches    = []

        for row_num, row in _iter_player_rows():
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
                        f"Row {row_num} Col {col_idx+1}: `{text}` | "
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
        m = re.match(r'^\s*\d+\.\s*(.*?)\s*[-–—]\s*(.+)$', line.strip())
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
    _ALLLISTS_AUTHORIZED = {OWNER_ID, 1174419581675249807}
    is_owner = ctx.author.id in _ALLLISTS_AUTHORIZED

    if in_dm and not is_owner:
        return
    if not in_dm and not ctx.author.guild_permissions.administrator:
        return

    await _repair_team_emojis()
    if not _data:
        await ctx.send("No drafters have registered a pick list yet.")
        return

    # Deduplicate: if multiple UIDs resolve to the same Discord user, keep the
    # best entry (active > paused, then more picks).
    seen: dict[int, str] = {}  # real_user_id → uid to keep
    for uid, user_data in _data.items():
        if user_data.get('anon_draft_id') and not in_dm:
            continue
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

        raw_emoji = _resolve_emoji_input(raw_emoji)
        if raw_emoji != user_data.get("emoji", ""):
            _data[uid]["emoji"] = raw_emoji
            _save_data(_data)
        emoji_display = raw_emoji if _is_valid_emoji(raw_emoji) else "(not set)"
        status_icon   = "▶️" if enabled else "⏸️"
        status_text   = ("Active" if _is_valid_emoji(raw_emoji) else "Needs team emoji") if enabled else "Paused"
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
    raw = emoji_str.strip()
    resolved = _resolve_emoji_input(raw)
    if not _is_valid_emoji(resolved):
        resolved = await _ensure_team_emoji(str(ctx.author.id))
        if not resolved:
            await ctx.send("⚠️ I couldn't find your team in the current lotto. Run `!setup` in the server and select the actual team emoji from Discord's emoji picker.")
            return
    warning = ""
    if raw and resolved == raw and re.match(r'^:[A-Za-z0-9_~]+:$', raw):
        warning = (
            f"\n⚠️ I couldn't find a custom emoji named `{raw.strip(':')}` to match that against, "
            f"so it'll show up as plain text `{raw}` instead of an actual emoji. "
            f"Type `!setup` in the **server** and pick the emoji from Discord's autocomplete instead."
        )

    uid = str(ctx.author.id)
    _ensure_user(uid)
    _data[uid]["emoji"] = resolved
    _save_data(_data)
    display = resolved or "(none)"
    await ctx.send(
        f"✅ Team emoji set to {display}.{warning}\n"
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
    await _confirm_team_setup(ctx, uid)
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
    await _confirm_team_setup(ctx, uid)


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
    await _confirm_team_setup(ctx, uid)


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


@bot.command(name="lockpick")
async def cmd_lockpick(ctx, position: int):
    """!lockpick 3 — toggle the lock flag on pick #3. If that player is the one
    auto-picked for you, the pick is posted with Timer Bot's lock marker
    attached, spending your lock charge (each GM only gets 1 for the draft —
    Timer Bot rejects the lock, not the pick, if you're out)."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid   = str(ctx.author.id)
    picks = _data.get(uid, {}).get("picks", [])

    if not picks:
        await ctx.send("Your pick list is empty.")
        return
    if position < 1 or position > len(picks):
        await ctx.send(f"Invalid position. You have {len(picks)} picks (1–{len(picks)}).")
        return

    pick = picks[position - 1]
    pick["lock"] = not pick.get("lock", False)
    _data[uid]["picks"] = picks
    _save_data(_data)

    if pick["lock"]:
        await ctx.send(
            f"🔒 **#{position}: {pick['player']}** will be locked in if drafted for you. "
            f"Remember — you only get 1 lock for the whole draft."
        )
    else:
        await ctx.send(f"🔓 **#{position}: {pick['player']}** un-flagged — will draft normally.")


@bot.command(name="mylist")
async def cmd_mylist(ctx):
    """!mylist — view your current pick list."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    uid       = str(ctx.author.id)
    user_data = _data.get(uid, {})
    picks     = user_data.get("picks", [])
    raw_emoji = await _ensure_team_emoji(uid)
    raw_emoji = _resolve_emoji_input(raw_emoji)
    if raw_emoji != user_data.get("emoji", ""):
        _ensure_user(uid)
        _data[uid]["emoji"] = raw_emoji
        _save_data(_data)
    enabled   = user_data.get("enabled", True)

    emoji_display = raw_emoji if _is_valid_emoji(raw_emoji) else "(not set — use `!setup <:emoji:id>` in the server)"
    status_icon   = "▶️" if enabled else "⏸️"
    status_text   = ("Active" if _is_valid_emoji(raw_emoji) else "Needs team emoji") if enabled else "Paused"

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


@bot.command(name="steal")
async def cmd_steal(ctx, *, text: str):
    """!steal Player Name [lock] — relay a steal attempt into the draft
    channel on your behalf, as if you'd typed it there yourself. Timer Bot
    validates everything (your turn, charges remaining, eligibility) exactly
    the same way — this just saves you from needing to be in the channel.
    Add "lock" at the end to also lock the stolen player in."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ Couldn't find the draft channel — contact the commissioner.")
        return

    relayed = text.strip()
    await channel.send(f"{ctx.author.mention} steal {relayed}")
    await ctx.send(f"📨 Relayed **steal {relayed}** to the draft channel — check there for Timer Bot's response.")


@bot.command(name="block")
async def cmd_block(ctx, *, text: str):
    """!block Player Name — relay a block attempt into the draft channel on
    your behalf. Not turn-gated — works anytime a target is eligible, same
    as typing it live."""
    if not _dm_only(ctx):
        await ctx.send("Please DM this command to me directly.")
        return

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ Couldn't find the draft channel — contact the commissioner.")
        return

    relayed = text.strip()
    await channel.send(f"{ctx.author.mention} block {relayed}")
    await ctx.send(f"📨 Relayed **block {relayed}** to the draft channel — check there for Timer Bot's response.")


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
    _RESET_AUTHORIZED = {OWNER_ID, 1174419581675249807}
    if ctx.author.id not in _RESET_AUTHORIZED:
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
    await _maybe_flux_pick(uid)


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
            "`!lockpick 3` — flag/unflag pick #3 to be locked in if it's drafted for you\n"
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
        name="4️⃣  Steal / Block (only if the draft has SBL enabled)",
        value=(
            "`!steal Player Name` — relay a steal attempt on your turn\n"
            "`!block Player Name` — relay a block attempt, anytime\n"
            "Both post into the draft channel exactly as if you'd typed them there — "
            "Timer Bot validates and responds the same way either way."
        ),
        inline=False,
    )
    embed.add_field(
        name="Pick format",
        value=(
            "Each pick can include:\n"
            "• **Player name** (required): `LeBron James`\n"
            "• **Year** (optional): `'23` or `2022-23`\n"
            "  Omit it to use the player's recorded default year, when available.\n"
            "• **Price** (optional): `$5`\n\n"
            "Example: `LeBron James '23 $5`"
        ),
        inline=False,
    )
    embed.add_field(name='Anonymous Budget', value='The owner runs `!draftmode anon <Theme draft ID>` once for every registered GM. '
                    'Then DM `!setlist` with prices. No emoji setup needed; years optional. Owner: `!draftmode normal` withdraws everyone.', inline=False)
    embed.add_field(name='Flux price conditions', value="After finishing your current pick, DM `!fluxpick Devin Booker '23 3` for the NEXT Flux only. "
                    'Failed conditions expire with a DM; repeat players are blocked. Owner: `!fluxsource <sheet URL> <tab name>`.', inline=False)
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
