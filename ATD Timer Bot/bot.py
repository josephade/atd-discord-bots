"""
ATD Timer Bot — Discord bot for managing timed ATD draft picks.

Picks are detected automatically from messages in the draft channel.
Expected format:  14. :Pacers: Marc Gasol 2012-13

Commands (prefix: !)
────────────────────
Setup (admin only):
  !timerloadlotto        Reply to the lotto message to load it (preferred)
  !timerlotto            Generate a random lotto from registered participants
  !timersetup @u1 @u2   Manually register participants (use before !timerlotto)
  !timerorder 2 1 3 …   Set draft order manually by participant number
  !timerstart            Begin the draft

During draft:
  !timerskip             Skip your turn (-10 min on future picks)
  !timerstatus           Current pick, round, and time remaining
  !timerboard            Show all picks so far
  !timerhelp             Full command reference

Admin:
  !timereset             Cancel and wipe the draft
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from config import AS_THRESHOLD, ATD_CHAT_CHANNEL_ID, DISCORD_TOKEN, DRAFT_CHANNEL_ID, DRAFT_LIST_BOT_ID, LOTTO_CHANNEL_ID, PENALTY_PLAYERS, ROUNDS
from draft import DraftState, HISTORY_FILE, build_snake_order

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("atd-timer")

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── Global state ──────────────────────────────────────────────────────────────

draft: DraftState = DraftState.load()
_timer_task:   asyncio.Task | None = None
_window_task:  asyncio.Task | None = None
_active_ping:    discord.Message | None = None  # the current pick prompt — deleted on pick/skip
_active_warning: discord.Message | None = None  # the 5-min warning — deleted on pick/skip

# ── Challenge state ───────────────────────────────────────────────────────────
_ping_time:          datetime | None = None  # when the current GM was pinged (challenge window opens)
_challenge_count:    int       = 0           # challenges received this pick turn
_challenged_msg_ids: set       = set()       # IDs of messages already challenged this turn (1 per msg)

# _last_skip is now persisted in draft.last_skip (saved to disk in draft_state.json)

# ── Pick idempotency guard ────────────────────────────────────────────────────
# Tracks pick numbers currently being processed to prevent double-advance
# from concurrent on_message + scanner calls for the same pick.
_processing_picks: set[int] = set()

# ── Draft window (Eastern Time) ───────────────────────────────────────────────
# Picks are only pinged/timed between 10am ET and midnight ET.
# Outside that window the timer auto-pauses and resumes at 10am.

_ET            = ZoneInfo("America/New_York")
_WINDOW_START  = 10   # 10:00 AM ET (inclusive)
_WINDOW_END    = 0    # midnight ET (exclusive — i.e. hours 0-9 are outside)


# ── Skip history persistence ──────────────────────────────────────────────────

def _load_skip_history() -> list[dict]:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_skip_history(entry: dict):
    history = _load_skip_history()
    history.append(entry)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def _in_window() -> bool:
    return datetime.now(_ET).hour >= _WINDOW_START


def _secs_until_close() -> float:
    """Seconds until midnight ET tonight."""
    now   = datetime.now(_ET)
    close = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (close - now).total_seconds())


def _secs_until_open() -> float:
    """Seconds until the next 10am ET."""
    now  = datetime.now(_ET)
    open_ = now.replace(hour=_WINDOW_START, minute=0, second=0, microsecond=0)
    if now.hour >= _WINDOW_START:
        open_ += timedelta(days=1)
    return max(0.0, (open_ - now).total_seconds())

COMMISSIONER_ROLE = "LeComissioner"

def is_commissioner():
    """Custom check: passes if the user has the LeComissioner role or is a server admin."""
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True
        if any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles):
            return True
        raise commands.CheckFailure(
            f"❌ You need the **{COMMISSIONER_ROLE}** role or administrator permissions."
        )
    return commands.check(predicate)

# Matches:  14. <:Pacers:123> Marc Gasol 2012-13
#           14. :Pacers: Marc Gasol 2012-13
#           14. Marc Gasol 2012-13
_PICK_RE = re.compile(
    r'^(\d+)\.\s+'
    r'(?:<:[^:]+:\d+>\s*|:[^:\s]+:\s*)?'
    r'(.+)$',
    re.IGNORECASE,
)

# Matches a single lotto line:
#   1. <:emoji:id> - <@userid> <@userid2>
#   2. 🦢 - <@userid>
_LOTTO_LINE_RE = re.compile(
    r'^\s*(\d+)\.'          # position number
    r'.*?-\s*'              # anything up to the first dash
    r'((?:<@!?\d+>\s*)+)$', # one or more Discord mentions
)

# Matches prices in roundless pick messages: $42, ($42), (42), 42$
_PRICE_RE = re.compile(
    r'\(?(-?\$\d+(?:\.\d+)?)\)?'
    r'|\((\d+(?:\.\d+)?)\)'
    r'|\b(\d+(?:\.\d+)?)\$'
)


def _extract_player_name(raw: str) -> str:
    text = re.sub(r'<:[^:]+:\d+>', '', raw).strip()
    text = re.sub(r'^:[^:\s]+:\s*', '', text).strip()
    # Strip all common year formats: 2012, 2012-13, 2012-2013, '12, '12-13, 12'
    text = re.sub(r"\s+'?\d{2}'-?\d{0,2}$", '', text).strip()   # 12', '12, '12-13
    text = re.sub(r'\s+\d{4}(-\d{2,4})?$', '', text).strip()    # 2012, 2012-13, 2012-2013
    text = re.sub(r"\s+'?\d{2}'?$", '', text).strip()            # catch any remaining short year
    return text


def _pick_name_key(raw: str) -> str:
    """Normalize a raw pick string to a comparable player-name key.
    Strips emoji, year, and price so 'LeBron James 1996-97 $0' == 'LeBron James'.
    """
    name = _extract_player_name(raw)
    name = _PRICE_RE.sub('', name).strip()   # remove price
    return name.lower()


def _team_mentions(team: dict) -> str:
    """Return mention string for all co-owners of a team slot."""
    return " ".join(f"<@{uid}>" for uid in team["user_ids"])


def _is_team_owner(user_id: int, team: dict) -> bool:
    return user_id in team["user_ids"]


def _pick_title() -> str:
    if draft.mode == "roundless":
        return f"Pick {draft.overall_pick}"
    return f"Round {draft.round_number} of {ROUNDS}  -  Pick {draft.overall_pick}"


def _pick_format(pick_num: int) -> str:
    if draft.mode == "roundless":
        return f"`{pick_num}. :YourEmoji: Player Name $Price Year`"
    return f"`{pick_num}. :YourEmoji: Player Name Year`"


def _parse_lotto_message(content: str, guild: discord.Guild) -> list[dict] | None:
    """
    Parse a lotto message into a list of team dicts ordered by draft position.
    Returns None if the message doesn't look like a valid lotto.

    Expected line format:
        1. <:emoji:id> - <@userid>
        14. <:emoji:id> - <@userid1> <@userid2>   (co-owners)
    """
    teams_by_pos: dict[int, dict] = {}

    for line in content.splitlines():
        m = _LOTTO_LINE_RE.match(line)
        if not m:
            continue

        pos      = int(m.group(1))
        mentions = re.findall(r'<@!?(\d+)>', m.group(2))
        user_ids = [int(uid) for uid in mentions]

        if not user_ids:
            continue

        # Build a display name from guild members if possible
        names = []
        for uid in user_ids:
            member = guild.get_member(uid)
            names.append(member.display_name if member else str(uid))

        teams_by_pos[pos] = {
            "user_ids":  user_ids,
            "name":      " / ".join(names),
            "picks":     [],
            "skip_count": 0,
        }

    if not teams_by_pos:
        return None

    # Return teams sorted by draft position
    return [teams_by_pos[p] for p in sorted(teams_by_pos)]


# ── Timer helpers ─────────────────────────────────────────────────────────────

async def _ping_current(channel: discord.TextChannel, remaining: int = None):
    global _active_ping, _ping_time, _challenge_count, _challenged_msg_ids
    team     = draft.current_team
    duration = remaining if remaining is not None else draft.effective_timer(draft.round_number, draft.current_team_idx)

    log.info(
        "PING | Round %d Pick %d (overall #%d) | Team: %s | Timer: %d min",
        draft.round_number, draft.pick_in_round, draft.overall_pick,
        team["name"], duration // 60,
    )

    deadline_ts = int(datetime.now(timezone.utc).timestamp()) + duration

    embed = discord.Embed(
        title=_pick_title(),
        description=(
            f"{_team_mentions(team)} it's your turn!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in this channel:\n"
            f"{_pick_format(draft.overall_pick)}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use !timerskip to pass (costs 10 min on future picks).")
    # Mention must be in message content (not just the embed) to trigger Discord push notifications
    _ping_time          = datetime.now(timezone.utc)
    _challenge_count    = 0
    _challenged_msg_ids = set()
    _active_ping = await channel.send(content=_team_mentions(team), embed=embed)


async def _delete_active_ping():
    global _active_ping, _active_warning
    for msg in (_active_ping, _active_warning):
        if msg:
            try:
                await msg.delete()
            except discord.NotFound:
                pass
    _active_ping = None
    _active_warning = None
    global _ping_time, _challenge_count, _challenged_msg_ids
    _ping_time          = None
    _challenge_count    = 0
    _challenged_msg_ids = set()


async def _auto_pause_for_window(remaining: float, next_up: bool = False):
    """Pause the timer because the draft window is closed.
    next_up=True: a pick just advanced the draft — ping the next person that it's their turn.
    next_up=False: the timer was running and midnight arrived mid-countdown.
    """
    global _window_task
    team = draft.current_team
    if not team:
        return

    remaining = max(0, int(remaining))
    draft.paused_remaining = remaining
    draft.timer_start      = None
    draft.state            = "window_paused"
    draft.save()

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    mins, s = remaining // 60, remaining % 60
    log.info("WINDOW PAUSE | next_up=%s | Team: %s | Remaining: %dm %ds", next_up, team["name"], mins, s)

    if next_up:
        embed = discord.Embed(
            title=_pick_title(),
            description=(
                f"{_team_mentions(team)} it's your turn!\n\n"
                f"🌙 Draft window is closed — your **{mins}m {s}s** timer starts at **10:00 AM ET**.\n\n"
                f"Type your pick in this channel:\n"
                f"{_pick_format(draft.overall_pick)}"
            ),
            color=discord.Color.dark_gray(),
        )
        embed.set_footer(text="Use !timerskip to pass (costs 10 min on future picks).")
        await channel.send(content=_team_mentions(team), embed=embed)
    else:
        await channel.send(
            f"🌙 **Draft window closed** (midnight ET). Timer paused.\n"
            f"{_team_mentions(team)} has **{mins}m {s}s** remaining — resumes at **10:00 AM ET**."
        )

    _window_task = asyncio.create_task(_window_resume_task(_secs_until_open()))


async def _window_resume_task(sleep_secs: float):
    """Sleeps until 10am ET then auto-resumes the draft."""
    await asyncio.sleep(sleep_secs)

    if draft.state != "window_paused":
        return

    team = draft.current_team
    if not team:
        return

    remaining = draft.paused_remaining or draft.effective_timer(draft.round_number, draft.current_team_idx)

    draft.state            = "active"
    draft.timer_start      = datetime.now(timezone.utc).isoformat()
    draft.paused_remaining = None
    draft.save()

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    mins, s = remaining // 60, remaining % 60
    log.info("WINDOW RESUME | Team: %s | Remaining: %dm %ds", team["name"], mins, s)

    deadline_ts = int(datetime.now(timezone.utc).timestamp()) + remaining

    embed = discord.Embed(
        title=_pick_title(),
        description=(
            f"{_team_mentions(team)} it's your turn!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in this channel:\n"
            f"{_pick_format(draft.overall_pick)}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use !timerskip to pass (costs 10 min on future picks).")
    global _active_ping
    _active_ping = await channel.send(content=f"☀️ **Draft window open!** {_team_mentions(team)}", embed=embed)

    global _timer_task
    _timer_task = asyncio.create_task(_timer_loop(remaining, team["user_ids"]))


async def _timer_loop(duration: int, user_ids: list[int]):
    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    loop    = asyncio.get_event_loop()
    start   = loop.time()

    def _elapsed() -> float:
        return loop.time() - start

    def _remaining() -> float:
        return max(0.0, duration - _elapsed())

    def _still_their_turn() -> bool:
        return (
            draft.state == "active"
            and draft.current_team is not None
            and any(uid in draft.current_team["user_ids"] for uid in user_ids)
        )

    async def _checked_sleep(target_elapsed: float) -> bool:
        """Sleep until target_elapsed seconds from loop start.
        Returns True if completed normally, False if window closed mid-sleep."""
        while True:
            rem_sleep = target_elapsed - _elapsed()
            if rem_sleep <= 0:
                return True
            # Never sleep past window close; check every 60s at most
            sleep = min(rem_sleep, _secs_until_close(), 60.0)
            await asyncio.sleep(max(sleep, 0))
            if not _in_window():
                return False
            if _elapsed() >= target_elapsed - 0.5:
                return True

    mentions = " ".join(f"<@{uid}>" for uid in user_ids)

    # ── 5-min warning ─────────────────────────────────────────────────────────
    if duration > 300:
        ok = await _checked_sleep(duration - 300)
        if not ok:
            if _still_their_turn():
                await _auto_pause_for_window(_remaining())
            return
        if _still_their_turn():
            global _active_warning
            log.info("WARNING | 5 min remaining | Team: %s", draft.current_team["name"] if draft.current_team else "?")
            _active_warning = await channel.send(f"⚠️ {mentions} - **5 minutes remaining**!")

    # ── Final countdown ───────────────────────────────────────────────────────
    ok = await _checked_sleep(duration)
    if not ok:
        if _still_their_turn():
            await _auto_pause_for_window(_remaining())
        return

    if _still_their_turn():
        log.info("TIMEOUT | Auto-skip triggered | Team: %s", draft.current_team["name"] if draft.current_team else "?")
        await _do_skip(auto=True)


async def _process_challenge(challenger_mention: str, challenger_name: str):
    """Cuts the current GM's timer to 10 min; 3 challenges = instant skip."""
    global _challenge_count, _timer_task, _active_ping, _active_warning

    _challenge_count += 1
    team    = draft.current_team
    channel = bot.get_channel(DRAFT_CHANNEL_ID)

    log.info("CHALLENGE #%d | Challenger: %s | Team: %s",
             _challenge_count, challenger_name, team["name"])

    if _challenge_count >= 3:
        await channel.send(
            f"⚡ **Challenge #{_challenge_count}!** {challenger_mention} challenged "
            f"{_team_mentions(team)} — **3 challenges reached, skipping immediately!**"
        )
        _challenge_count = 0
        await _do_skip(auto=True)
        return

    # Cancel the current timer
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()

    # Delete the stale ping embed (shows old deadline) and the warning if any
    for msg in (_active_ping, _active_warning):
        if msg:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
    _active_warning = None

    # Send the challenge announcement + new 10-min ping
    new_duration = 600   # 10 minutes
    deadline_ts  = int(datetime.now(timezone.utc).timestamp()) + new_duration

    embed = discord.Embed(
        title=_pick_title(),
        description=(
            f"⚡ **Challenge #{_challenge_count}!** {challenger_mention} challenged "
            f"{_team_mentions(team)}!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in this channel:\n"
            f"{_pick_format(draft.overall_pick)}"
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="Use !timerskip to pass (costs 10 min on future picks).")

    _active_ping = await channel.send(content=_team_mentions(team), embed=embed)

    draft.timer_start = datetime.now(timezone.utc).isoformat()
    draft.save()

    _timer_task = asyncio.create_task(_timer_loop(new_duration, team["user_ids"]))


async def _start_timer():
    global _timer_task
    # Don't cancel the timer task if we're being called from within it —
    # that would raise CancelledError here and prevent the next ping from sending.
    current = asyncio.current_task()
    if _timer_task and not _timer_task.done() and _timer_task is not current:
        _timer_task.cancel()

    team = draft.current_team
    if not team or draft.state != "active":
        log.warning("_start_timer: early return — team=%s state=%s", team["name"] if team else None, draft.state)
        return

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    if not channel:
        log.error("_start_timer: draft channel %d not found in cache", DRAFT_CHANNEL_ID)
        return

    # Outside draft window — notify next picker and pause until 10am
    if not _in_window():
        duration = draft.effective_timer(draft.round_number, draft.current_team_idx)
        await _auto_pause_for_window(duration, next_up=True)
        return

    # Pending makeup — only for snake drafts; roundless uses queue reordering instead
    if draft.mode != "roundless" and team.get("pending_makeup"):
        log.info("PENDING MAKEUP SKIP | Round %d | Pick %d | Team: %s",
                 draft.round_number, draft.overall_pick, team["name"])
        await channel.send(
            f"⏩ **{_team_mentions(team)} ({team['name']})** has a pending makeup pick from a previous round — skipping immediately."
        )
        await _do_skip(auto=True)
        return

    # Active Skip — team has hit the AS threshold, skip immediately with no timer
    if draft.is_active_skip(draft.current_team_idx):
        log.info("ACTIVE SKIP | Round %d | Pick %d | Team: %s | Skips: %d",
                 draft.round_number, draft.overall_pick, team["name"],
                 team.get("skip_count", 0))
        await channel.send(
            f"⏩ **{_team_mentions(team)} ({team['name']})** is on "
            f"**Active Skip (AS)** — {AS_THRESHOLD}+ skips recorded. Skipping immediately."
        )
        await _do_skip(auto=True)
        return

    duration = draft.effective_timer(draft.round_number, draft.current_team_idx)

    # If accumulated penalties have eroded the timer to 0, skip immediately
    if duration <= 0:
        log.info("TIMER ZERO | Round %d | Pick %d | Team: %s | Auto-skipping",
                 draft.round_number, draft.overall_pick, team["name"])
        await channel.send(
            f"⏩ {_team_mentions(team)} — timer has been fully consumed by skip penalties. Auto-skipping."
        )
        await _do_skip(auto=True)
        return

    log.info(
        "TIMER START | Round %d | Pick %d | Team: %s | Duration: %d sec (%d min)",
        draft.round_number, draft.overall_pick, team["name"], duration, duration // 60,
    )
    draft.timer_start = datetime.now(timezone.utc).isoformat()
    draft.save()

    await _ping_current(channel)
    _timer_task = asyncio.create_task(_timer_loop(duration, team["user_ids"]))


async def _do_skip(auto: bool = False):
    global _timer_task
    current = asyncio.current_task()
    if _timer_task and not _timer_task.done() and _timer_task is not current:
        _timer_task.cancel()

    team = draft.current_team
    if not team:
        return

    pick_num   = draft.overall_pick
    team_idx   = draft.current_team_idx          # capture before any mutations
    mentions   = _team_mentions(team)
    prev_skip  = team.get("skip_count", 0)
    skip_count = prev_skip + 1
    prev_last_pick = team.get("last_pick_number", 0)

    team["skip_count"] = skip_count
    team["pending_makeup"] = True
    if draft.mode == "roundless":
        # Also record the pick number so their position within the pending group is ordered
        team["last_pick_number"] = pick_num

    # Save undo state before we advance
    draft.last_skip = {
        "round":                 draft.current_round,
        "in_round":              draft.current_in_round,
        "team_idx":              team_idx,
        "prev_skip_count":       prev_skip,
        "prev_last_pick_number": prev_last_pick,
    }

    # Calculate remaining timer after this skip
    from config import SKIP_PENALTY
    if draft.mode == "roundless":
        from config import ROUNDLESS_TIMER
        next_timer_min = max((ROUNDLESS_TIMER - skip_count * SKIP_PENALTY) // 60, 0)
    else:
        from config import ROUND_TIMERS
        next_timer_min = max((ROUND_TIMERS.get(draft.round_number, 1800) - skip_count * SKIP_PENALTY) // 60, 0)

    log.info(
        "SKIP | %s | Team: %s | Total skips: %d",
        "auto (timeout)" if auto else "manual",
        team["name"], skip_count,
    )

    # ── Persist skip to cross-draft history ───────────────────────────────────
    _append_skip_history({
        "draft_label":   draft.draft_label or draft.draft_started or "Unknown ATD",
        "draft_started": draft.draft_started,
        "user_ids":      list(team["user_ids"]),
        "team_name":     team["name"],
        "pick_num":      pick_num,
        "round_num":     draft.round_number,
        "auto":          auto,
        "mode":          draft.mode,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

    await _delete_active_ping()

    draft.advance()
    draft.timer_start = None
    draft.save()

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    await channel.send(
        f"**{pick_num}.** {mentions} skipped "
        f"({skip_count} skip{'s' if skip_count != 1 else ''} - {next_timer_min}m left on future picks)"
    )

    if draft.state == "complete":
        await channel.send("🏆 **Draft complete!**")
        return

    await _start_timer()


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Bot online — logged in as %s (id: %s)", bot.user, bot.user.id)
    log.info("Draft state on startup: %s | Round: %d | Pick: %d",
             draft.state, draft.current_round + 1, draft.overall_pick if draft.state == "active" else 0)

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    global _timer_task, _window_task

    if draft.state == "window_paused" and draft.current_team:
        team = draft.current_team
        remaining = draft.paused_remaining or draft.effective_timer(draft.round_number, draft.current_team_idx)
        mins, s = remaining // 60, remaining % 60

        if _in_window():
            # Bot restarted AFTER 10 AM — window is already open, resume immediately
            log.info("RESTART | Window-paused but window is OPEN | Team: %s | Resuming now", team["name"])
            draft.state            = "active"
            draft.timer_start      = datetime.now(timezone.utc).isoformat()
            draft.paused_remaining = None
            draft.save()
            await channel.send(
                f"🔄 Bot restarted - draft window is open. Resuming {_team_mentions(team)}'s turn "
                f"(**{mins}m {s}s** remaining)."
            )
            _timer_task = asyncio.create_task(_timer_loop(remaining, team["user_ids"]))
            await _ping_current(channel, remaining=remaining)
        else:
            # Bot restarted while window is closed — wait for 10 AM
            log.info("RESTART | Window-paused | Team: %s | Remaining: %dm %ds", team["name"], mins, s)
            await channel.send(
                f"🔄 Bot restarted - draft window is closed. {_team_mentions(team)} has **{mins}m {s}s** remaining.\n"
                f"Timer will resume at **10:00 AM ET**."
            )
            _window_task = asyncio.create_task(_window_resume_task(_secs_until_open()))

    elif draft.state == "active" and draft.timer_start and draft.current_team:
        elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(draft.timer_start)).total_seconds()
        duration  = draft.effective_timer(draft.round_number, draft.current_team_idx)
        remaining = duration - elapsed

        team     = draft.current_team
        mentions = _team_mentions(team)

        if remaining <= 0:
            await channel.send(f"🔄 Bot restarted - {mentions}'s time had already expired. Auto-skipping…")
            await _do_skip(auto=True)
        else:
            _timer_task = asyncio.create_task(_timer_loop(int(remaining), team["user_ids"]))
            await _ping_current(channel, remaining=int(remaining))

    # Start the missed-pick scanner
    asyncio.create_task(_missed_pick_scanner())


async def _missed_pick_scanner():
    """
    Background task: every 30 seconds, scan recent draft channel messages for a
    pick matching the current pick number that the bot never acknowledged (no ✅).
    Catches messages Discord dropped before delivering to on_message.
    """
    await asyncio.sleep(30)   # initial delay — let on_ready fully settle
    while True:
        await asyncio.sleep(30)
        try:
            if draft.state not in ("active", "paused", "window_paused"):
                continue

            channel = bot.get_channel(DRAFT_CHANNEL_ID)
            if not channel:
                continue

            expected_pick = draft.overall_pick

            async for msg in channel.history(limit=30):
                if msg.author.bot:
                    continue

                match = _PICK_RE.match(msg.content.strip())
                if not match:
                    continue

                if int(match.group(1)) != expected_pick:
                    continue

                # Check if bot already reacted ✅ — if so, already processed
                already_done = any(
                    r.emoji == "✅" and r.me
                    for r in msg.reactions
                )
                if already_done:
                    break  # found it, already handled — stop scanning

                # Found an unprocessed pick — process it now
                log.info(
                    "MISSED PICK RECOVERED | Overall #%d | Author: %s | Content: %s",
                    expected_pick, msg.author.display_name, msg.content[:80],
                )
                await _try_process_pick(msg)
                break

            # ── Watchdog: active draft but no ping showing ─────────────────
            # Catches two failure modes:
            #   (a) timer task is dead (crashed or never started)
            #   (b) timer task is running but _ping_current() silently failed
            #       so nobody got a visible pick prompt
            if (draft.state == "active"
                    and _in_window()
                    and draft.current_team
                    and (_timer_task is None or _timer_task.done() or _active_ping is None)):
                log.warning(
                    "WATCHDOG | No active ping / dead timer | Pick #%d | Team: %s | "
                    "timer_done=%s active_ping=%s — restarting",
                    draft.overall_pick, draft.current_team["name"],
                    _timer_task is None or _timer_task.done(),
                    _active_ping is None,
                )
                await _start_timer()

        except Exception as exc:
            log.warning("Missed-pick scanner error: %s", exc)


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    # Let the Draft List Bot's picks through; ignore all other bot messages.
    _from_draft_list = bool(DRAFT_LIST_BOT_ID and message.author.id == DRAFT_LIST_BOT_ID)
    if message.author.bot and not _from_draft_list:
        return

    # ── Challenge detection (atd-chat only) ──────────────────────────────────
    if (message.channel.id == ATD_CHAT_CHANNEL_ID
            and message.reference
            and draft.state == "active"
            and draft.current_team
            and message.content.strip().lower() == "challenge"
            and message.author.id not in draft.current_team["user_ids"]):
        # Resolve effective ping time — _ping_time is in-memory and resets on
        # restart. Fall back to draft.timer_start (persisted to disk) so
        # challenge validation still works after a bot restart/deploy.
        effective_ping_time = _ping_time
        if effective_ping_time is None and draft.timer_start:
            effective_ping_time = datetime.fromisoformat(draft.timer_start)

        if effective_ping_time is None:
            # No timer running at all — ignore
            return

        # Snapshot the team index BEFORE the network call. If a pick is
        # processed while we await fetch_message, the team will have advanced
        # and we must discard this challenge to avoid acting on the wrong turn.
        expected_team_idx = draft.current_team_idx
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.HTTPException):
            pass
        else:
            if (draft.state == "active"
                    and draft.current_team_idx == expected_team_idx
                    and ref_msg.author.id in draft.current_team["user_ids"]):
                try:
                    if ref_msg.created_at < effective_ping_time:
                        await message.reply(
                            "❌ **Invalid challenge** — the GM typed that message before they were pinged to pick."
                        )
                    elif ref_msg.id in _challenged_msg_ids:
                        await message.reply(
                            "❌ **Invalid challenge** — that message has already been challenged. It won't count."
                        )
                    else:
                        _challenged_msg_ids.add(ref_msg.id)
                        await _process_challenge(message.author.mention, message.author.display_name)
                except discord.HTTPException as e:
                    log.warning("Challenge reply failed (Discord error): %s", e)
        return

    # ── Pick detection (draft channel only) ──────────────────────────────────
    if message.channel.id == DRAFT_CHANNEL_ID:
        await _try_process_pick(message)
        # In roundless mode, also detect makeup picks from pending teams
        if draft.mode == "roundless" and draft.state in ("active", "paused", "window_paused"):
            await _try_process_roundless_makeup(message)

        # Format error: current GM posted something that looks like a pick attempt
        # but doesn't match the required format — tell them what to use.
        # Triggers on: starts with digit, has custom emoji, or has a price ($N).
        if (not message.content.startswith('!')
                and draft.state in ("active", "paused", "window_paused")
                and draft.current_team
                and _is_team_owner(message.author.id, draft.current_team)):
            content = message.content.strip()
            looks_like_pick = (
                bool(re.match(r'^\d', content))                        # starts with pick number
                or bool(re.search(r'<:[^:]+:\d+>', content))           # custom emoji
                or bool(_PRICE_RE.search(content))                     # price like $1
            )
            if looks_like_pick and not _PICK_RE.match(content):
                await message.channel.send(
                    f"❌ {message.author.mention} — wrong format. Use:\n"
                    f"{_pick_format(draft.overall_pick)}"
                )


async def _try_process_roundless_makeup(message: discord.Message):
    """Roundless mode only: process a makeup pick submitted by a pending team.
    Handles picks with past pick numbers AND out-of-turn posts using the current pick number.
    Updates money_spent, picks, last_pick_number, and clears pending_makeup."""
    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num_in_msg = int(match.group(1))
    if pick_num_in_msg > draft.overall_pick:
        return  # future pick number — ignore

    # Find the team by author
    team = next((t for t in draft.teams if message.author.id in t["user_ids"]), None)
    if not team:
        return

    if not team.get("pending_makeup"):
        return  # not a pending team

    # If this IS the team's official turn, let _try_process_pick handle it (advances the draft)
    if pick_num_in_msg == draft.overall_pick and draft.current_team is team:
        return

    # Skip if bot already reacted ✅ (already processed)
    already_done = any(r.emoji == "✅" and r.me for r in message.reactions)
    if already_done:
        return

    pick_raw = match.group(2).strip()

    # Extract price and update money_spent
    price_m = _PRICE_RE.search(pick_raw)
    if price_m:
        raw = (price_m.group(1) or price_m.group(2) or price_m.group(3) or "0")
        try:
            dollars = int(float(raw.lstrip("$")))
            team["money_spent"] = team.get("money_spent", 0) + dollars
        except ValueError:
            pass

    team["last_pick_number"] = pick_num_in_msg
    team["pending_makeup"]   = False
    team["picks"].append(pick_raw)
    draft.save()

    log.info("MAKEUP PICK | Team: %s | Pick #%d | %s", team["name"], pick_num_in_msg, pick_raw)
    await message.add_reaction("✅")


async def _try_process_pick(message: discord.Message, is_edit: bool = False):
    """Attempt to process a draft pick from a message. Called on both new and edited messages."""
    global _processing_picks, _timer_task, _window_task

    if draft.state not in ("active", "paused", "window_paused"):
        return

    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num = int(match.group(1))
    pick_raw = match.group(2).strip()

    if pick_num != draft.overall_pick:
        return

    # Guard against concurrent processing of the same pick (scanner + on_message race).
    # No await between check and add — safe in asyncio's cooperative threading.
    if pick_num in _processing_picks:
        log.info("PICK GUARD | Pick #%d already being processed — skipping duplicate", pick_num)
        return
    _processing_picks.add(pick_num)

    success = False
    try:
        team = draft.current_team

        is_commissioner_pick = (
            bool(DRAFT_LIST_BOT_ID and message.author.id == DRAFT_LIST_BOT_ID)
            or message.author.guild_permissions.administrator
            or any(r.name == COMMISSIONER_ROLE for r in message.author.roles)
        )
        if not _is_team_owner(message.author.id, team) and not is_commissioner_pick:
            return

        # Re-validate pick number hasn't changed since the check above
        if pick_num != draft.overall_pick:
            return

        # Duplicate check — reject before touching the timer
        player_name = _extract_player_name(pick_raw)
        player_key  = _pick_name_key(pick_raw)
        for t in draft.teams:
            for p in t.get("picks", []):
                if _pick_name_key(p) == player_key:
                    log.info(
                        "DUPLICATE PICK | Player: %s | Already on team: %s | Rejecting",
                        player_name, t["name"],
                    )
                    await message.add_reaction('❌')
                    return  # timer keeps running; Team Sheet Bot sends the "already on X" message

        # ── Valid pick ────────────────────────────────────────────────────────────
        log.info(
            "PICK | Overall #%d | Round %d Pick %d | Team: %s | Player: %s",
            draft.overall_pick, draft.round_number, draft.pick_in_round,
            team["name"], pick_raw,
        )
        if _timer_task and not _timer_task.done():
            _timer_task.cancel()
        if _window_task and not _window_task.done():
            _window_task.cancel()
        await _delete_active_ping()
        if draft.state in ("window_paused", "paused"):
            draft.state            = "active"
            draft.paused_remaining = None

        team["picks"].append(pick_raw)
        team["pending_makeup"] = False

        if draft.mode == "roundless":
            # Extract price to track money spent, and record pick position for queue ordering
            price_m = _PRICE_RE.search(pick_raw)
            if price_m:
                raw = (price_m.group(1) or price_m.group(2) or price_m.group(3) or "0")
                try:
                    dollars = int(float(raw.lstrip("$")))
                    team["money_spent"] = team.get("money_spent", 0) + dollars
                except ValueError:
                    pass
            team["last_pick_number"] = pick_num

        penalty_note = ""
        if player_name.lower() in PENALTY_PLAYERS:
            team_idx = draft.current_team_idx
            if team_idx not in draft.penalty_teams:
                draft.apply_penalty(team_idx)
                penalty_note = (
                    f"⚠️ **{team['name']}** drafted **{player_name}** - "
                    f"they will pick **last** every round from Round 6 onward."
                )

        draft.advance()
        draft.save()
        success = True

        await message.add_reaction("✅")

        if penalty_note:
            await message.channel.send(penalty_note)

        if draft.state == "complete":
            await message.channel.send("🏆 **Draft complete! Great picks everyone.**")
            return

    except Exception as exc:
        log.error("Error processing pick #%d: %s", pick_num, exc, exc_info=True)
        if not success:
            # Pick was not yet recorded — surface the error
            try:
                await message.channel.send(f"⚠️ Error processing pick: {exc}")
            except Exception:
                pass

    finally:
        _processing_picks.discard(pick_num)

    # Start the next timer OUTSIDE the main try block so failures are always surfaced
    if success and draft.state not in ("complete", None):
        try:
            await _start_timer()
        except Exception as exc:
            log.error("Timer start failed after pick #%d: %s", pick_num, exc, exc_info=True)
            channel = bot.get_channel(DRAFT_CHANNEL_ID)
            if channel:
                await channel.send(
                    f"⚠️ Pick recorded but the next timer failed to start: `{exc}`\n"
                    f"Use `!timerjumpto {draft.overall_pick}` to recover."
                )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Catch picks that were submitted as an edit to a prior message."""
    if after.author.bot:
        return
    if after.channel.id != DRAFT_CHANNEL_ID:
        return
    # Only process if the content actually changed to something new
    if before.content == after.content:
        return
    await _try_process_pick(after, is_edit=True)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # silently ignore — other bots in the same server handle these
    if isinstance(error, commands.CheckFailure):
        await ctx.send(str(error))
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"❌ You need the **{COMMISSIONER_ROLE}** role or administrator permissions.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing: `{error.param.name}`")
    else:
        raise error


# ── Setup commands ────────────────────────────────────────────────────────────

@bot.command(name="timerloadlotto")
@is_commissioner()
async def timerloadlotto(ctx):
    """
    Reads the most recent lotto from the lotto channel (id: LOTTO_CHANNEL_ID)
    and loads it as the draft order.

    Optionally, reply to a specific lotto message to load that one instead.

    Expected lotto line format:
        1. <:emoji:id> - @User
        14. <:emoji:id> - @User1 @User2   (co-owners)
    """
    global draft

    if draft.state not in ("idle", "setup", "lotto"):
        await ctx.send("❌ A draft is already active. Use `!timereset` first.")
        return

    # If the command is a reply, use that specific message
    ref = ctx.message.reference
    if ref:
        lotto_msg = await ctx.channel.fetch_message(ref.message_id)
    else:
        # Otherwise fetch the most recent message from the lotto channel
        lotto_channel = bot.get_channel(LOTTO_CHANNEL_ID)
        if not lotto_channel:
            await ctx.send(f"❌ Could not find lotto channel (id: {LOTTO_CHANNEL_ID}). Check bot permissions.")
            return
        async for msg in lotto_channel.history(limit=50):
            # Find the most recent message that looks like a lotto (has "1." in it)
            if re.search(r'^\s*1\.', msg.content, re.MULTILINE):
                lotto_msg = msg
                break
        else:
            await ctx.send(f"❌ No lotto message found in <#{LOTTO_CHANNEL_ID}>.")
            return

    teams = _parse_lotto_message(lotto_msg.content, ctx.guild)

    if not teams:
        await ctx.send(
            "❌ Could not parse that message as a lotto. Each line must look like:\n"
            "`1. <:emoji:id> - @User` or `1. emoji - @User1 @User2`"
        )
        return

    draft            = DraftState()
    draft.teams      = teams
    draft.pick_order = build_snake_order(len(teams))
    draft.state      = "lotto"
    draft.save()

    log.info("LOTTO LOADED | %d teams | Slots: %s",
             len(teams), [t["name"] for t in teams])

    lines = "\n".join(
        f"**{i+1}.** {_team_mentions(t)} ({t['name']})"
        for i, t in enumerate(draft.teams)
    )
    embed = discord.Embed(
        title=f"✅ Lotto loaded — {len(teams)} teams",
        description=lines,
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Run !timerstart to begin the draft.")
    await ctx.send(embed=embed)


@bot.command(name="timerlottoupdate")
@is_commissioner()
async def timerlottoupdate(ctx):
    """
    Re-reads the lotto channel and updates GM rosters for each slot.
    Preserves existing picks and skip counts — only updates user_ids and names.
    Useful when a co-owner is added to a team after the draft has started.
    """
    if draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No lotto loaded yet. Use `!timerloadlotto` first.")
        return

    lotto_channel = bot.get_channel(LOTTO_CHANNEL_ID)
    if not lotto_channel:
        await ctx.send(f"❌ Could not find lotto channel (id: {LOTTO_CHANNEL_ID}).")
        return

    # If the command is a reply, use that specific message
    lotto_msg = None
    ref = ctx.message.reference
    if ref:
        lotto_msg = await ctx.channel.fetch_message(ref.message_id)
    else:
        async for msg in lotto_channel.history(limit=50):
            if re.search(r'^\s*1\.', msg.content, re.MULTILINE):
                lotto_msg = msg
                break

    if not lotto_msg:
        await ctx.send(f"❌ No lotto message found in <#{LOTTO_CHANNEL_ID}>.")
        return

    updated_teams = _parse_lotto_message(lotto_msg.content, ctx.guild)
    if not updated_teams:
        await ctx.send("❌ Could not parse the lotto message.")
        return

    if len(updated_teams) != len(draft.teams):
        await ctx.send(
            f"❌ Team count mismatch — lotto has {len(updated_teams)} slots "
            f"but current draft has {len(draft.teams)}. Use `!timerloadlotto` to fully reload."
        )
        return

    changes = []
    for i, (old, new) in enumerate(zip(draft.teams, updated_teams)):
        if old["user_ids"] != new["user_ids"] or old["name"] != new["name"]:
            changes.append(f"Slot {i+1}: **{old['name']}** → **{new['name']}**")
            log.info("LOTTO UPDATE | Slot %d | %s → %s | user_ids: %s → %s",
                     i + 1, old["name"], new["name"], old["user_ids"], new["user_ids"])
            old["user_ids"] = new["user_ids"]
            old["name"]     = new["name"]

    draft.save()

    if changes:
        await ctx.send("✅ **Lotto updated:**\n" + "\n".join(changes))
    else:
        await ctx.send("✅ Lotto re-read — no changes detected.")


@bot.command(name="timersetup")
@is_commissioner()
async def timersetup(ctx, *_):
    global draft

    mentions = ctx.message.mentions
    if not mentions:
        await ctx.send("❌ Mention at least one user. Example: `!timersetup @Alice @Bob`")
        return

    draft       = DraftState()
    draft.teams = [
        {"user_ids": [m.id], "name": m.display_name, "picks": [], "skip_count": 0}
        for m in mentions
    ]
    draft.state = "setup"
    draft.save()

    lines = "\n".join(f"{i+1}. {t['name']}" for i, t in enumerate(draft.teams))
    await ctx.send(
        f"✅ **{len(draft.teams)} participants registered:**\n{lines}\n\n"
        f"Run `!timerlotto` to randomly assign positions, or `!timerorder 3 1 2 …` to set manually."
    )


@bot.command(name="timerlotto")
async def timerlotto(ctx):
    if draft.state not in ("setup", "lotto"):
        await ctx.send("❌ Run `!timersetup` or `!timerloadlotto` first.")
        return

    import random
    indices      = list(range(draft.num_teams))
    random.shuffle(indices)
    draft.teams      = [draft.teams[i] for i in indices]
    draft.pick_order = build_snake_order(draft.num_teams)
    draft.state      = "lotto"
    draft.save()

    lines = "\n".join(f"**{i+1}.** {_team_mentions(t)}" for i, t in enumerate(draft.teams))
    embed = discord.Embed(title="🎰 Lotto Results — Draft Order", description=lines, color=discord.Color.gold())
    embed.set_footer(text="Run !timerstart to begin.")
    await ctx.send(embed=embed)


@bot.command(name="timerorder")
@is_commissioner()
async def timerorder(ctx, *positions):
    if draft.state not in ("setup", "lotto"):
        await ctx.send("❌ Run `!timersetup` or `!timerloadlotto` first.")
        return

    try:
        idx = [int(p) - 1 for p in positions]
        if sorted(idx) != list(range(draft.num_teams)):
            raise ValueError
    except (ValueError, TypeError):
        await ctx.send(f"❌ Provide all {draft.num_teams} positions with no repeats.")
        return

    draft.teams      = [draft.teams[i] for i in idx]
    draft.pick_order = build_snake_order(draft.num_teams)
    draft.state      = "lotto"
    draft.save()

    lines = "\n".join(f"**{i+1}.** {_team_mentions(t)}" for i, t in enumerate(draft.teams))
    embed = discord.Embed(title="📋 Draft Order Set", description=lines, color=discord.Color.blue())
    embed.set_footer(text="Run !timerstart to begin.")
    await ctx.send(embed=embed)


@bot.command(name="timermode")
@is_commissioner()
async def timermode(ctx, mode: str = ""):
    """!timermode roundless | !timermode snake — switch draft mode (can be called before or during the draft)."""
    mode = mode.lower()
    if mode not in ("roundless", "snake"):
        await ctx.send(
            "❌ Usage: `!timermode roundless` or `!timermode snake`\n"
            "**roundless** — dynamic pick order based on money spent\n"
            "**snake** — fixed round-based snake order (default)"
        )
        return

    if draft.state == "idle":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    old_mode = draft.mode
    draft.mode = mode
    draft.save()

    if mode == "roundless":
        await ctx.send(
            f"✅ Switched to **roundless mode**. Pick order now computed dynamically:\n"
            f"1. Less money spent → picks sooner\n"
            f"2. Fewer picks made → picks sooner\n"
            f"3. More time since last pick → picks sooner\n\n"
            f"Picks must include price: `{draft.overall_pick}. :Emoji: Player Name $42 Year`"
        )
    else:
        await ctx.send(
            f"✅ Switched to **snake mode**. Fixed round-based order resumes from pick #{draft.overall_pick}."
        )


@bot.command(name="timerstart")
@is_commissioner()
async def timerstart(ctx, *label_parts):
    """!timerstart [roundless] [label] — begin the draft.
    Add 'roundless' for dynamic money-based pick order instead of snake rounds."""
    if draft.state != "lotto":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto` or `!timerlotto`.")
        return

    parts = list(label_parts)
    if parts and parts[0].lower() == "roundless":
        draft.mode = "roundless"
        parts = parts[1:]
    else:
        draft.mode = "snake"

    draft.state            = "active"
    draft.current_round    = 0
    draft.current_in_round = 0
    draft.draft_started    = datetime.now(timezone.utc).isoformat()
    draft.draft_label      = " ".join(parts) if parts else None
    draft.save()

    mode_note  = "\n🔄 **Roundless mode** — pick order determined by money spent, picks made, and time since last pick." if draft.mode == "roundless" else ""
    label_note = f" (**{draft.draft_label}**)" if draft.draft_label else ""
    await ctx.send(f"🏀 **The draft has started!**{label_note}{mode_note}")
    await _start_timer()


@bot.command(name="timerjumpto")
@is_commissioner()
async def timerjumpto(ctx, pick_number: int):
    """
    !timerjumpto <pick_number> — jump the draft to a specific overall pick number.
    Use this when picks were made manually before the bot started tracking.
    """
    if draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    if draft.mode == "roundless":
        if pick_number < 1:
            await ctx.send("❌ Pick number must be at least 1.")
            return
        new_round    = pick_number - 1   # current_round is the pick counter in roundless
        new_in_round = 0
    else:
        total_picks = draft.num_teams * ROUNDS
        if pick_number < 1 or pick_number > total_picks:
            await ctx.send(f"❌ Pick number must be between 1 and {total_picks}.")
            return
        zero_pick    = pick_number - 1
        new_round    = zero_pick // draft.num_teams
        new_in_round = zero_pick % draft.num_teams

    # Cancel any existing timer/window tasks and delete the stale ping embed
    global _timer_task, _window_task
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
    if _window_task and not _window_task.done():
        _window_task.cancel()
    await _delete_active_ping()

    draft.current_round    = new_round
    draft.current_in_round = new_in_round
    draft.paused_remaining = None
    draft.timer_start      = None
    draft.state            = "active"
    draft.save()

    team = draft.current_team
    log.info("JUMP | To pick %d | Round %d | In-round %d | Team: %s",
             pick_number, draft.round_number, draft.pick_in_round, team["name"] if team else "?")
    if draft.mode == "roundless":
        loc_str = f"pick #{pick_number}"
    else:
        loc_str = f"pick #{pick_number} (Round {draft.round_number}, pick {draft.pick_in_round})"
    await ctx.send(
        f"⏩ Jumped to **{loc_str}**.\n"
        f"Up now: {_team_mentions(team) if team else '?'}\nStarting timer…"
    )
    await _start_timer()


@bot.command(name="timersetpick")
@is_commissioner()
async def timersetpick(ctx, pick_number: int, member: discord.Member):
    """!timersetpick <pick_number> @GM — set the current pick number AND force a specific GM to be next."""
    if draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    # Find the team for the mentioned member
    team_idx = next(
        (i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]),
        None,
    )
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not registered as a GM in this draft.")
        return

    # Set pick number
    if draft.mode == "roundless":
        draft.current_round    = pick_number - 1
        draft.current_in_round = 0
    else:
        total_picks = draft.num_teams * ROUNDS
        if pick_number < 1 or pick_number > total_picks:
            await ctx.send(f"❌ Pick number must be between 1 and {total_picks}.")
            return
        zero_pick              = pick_number - 1
        draft.current_round    = zero_pick // draft.num_teams
        draft.current_in_round = zero_pick % draft.num_teams

    # Force the specified GM to be next (clears after one pick)
    draft.next_team_override = team_idx
    draft.paused_remaining   = None
    draft.timer_start        = None
    draft.state              = "active"
    draft.save()

    # Cancel stale timer and ping
    global _timer_task, _window_task
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
    if _window_task and not _window_task.done():
        _window_task.cancel()
    await _delete_active_ping()

    team = draft.teams[team_idx]
    await ctx.send(
        f"✅ Pick set to **#{pick_number}** | Next up: **{team['name']}** ({_team_mentions(team)})\n"
        f"Starting timer…"
    )
    await _start_timer()


# ── During-draft commands ─────────────────────────────────────────────────────


@bot.command(name="timerproxy")
@is_commissioner()
async def timerproxy(ctx, member: discord.Member):
    """
    !timerproxy @user — temporarily adds @user as a co-picker for the team currently on the clock.
    They can then type the pick naturally in the channel.
    Use !timerremoveproxy @user to remove them.
    """
    if draft.state != "active":
        await ctx.send("❌ No active draft.")
        return

    team = draft.current_team
    if not team:
        return

    if member.id in team["user_ids"]:
        await ctx.send(f"❌ {member.mention} is already a picker for **{team['name']}**.")
        return

    team["user_ids"].append(member.id)
    draft.save()

    log.info("PROXY ADD | Team: %s | Proxy: %s (%d)", team["name"], member.display_name, member.id)
    await ctx.send(
        f"✅ {member.mention} can now submit picks for **{team['name']}** while they're away.\n"
        f"Run `!timerremoveproxy {member.mention}` to remove them."
    )


@bot.command(name="timerremoveproxy")
@is_commissioner()
async def timerremoveproxy(ctx, member: discord.Member):
    """!timerremoveproxy @user — removes a proxy picker from whichever team they were added to."""
    if draft.state not in ("lotto", "active"):
        await ctx.send("❌ No draft in progress.")
        return

    # Find the team this user is a proxy on (but not an original owner)
    for team in draft.teams:
        if member.id in team["user_ids"]:
            team["user_ids"].remove(member.id)
            draft.save()
            log.info("PROXY REMOVE | Team: %s | Removed: %s (%d)", team["name"], member.display_name, member.id)
            await ctx.send(f"✅ Removed {member.mention} as a proxy for **{team['name']}**.")
            return

    await ctx.send(f"❌ {member.mention} is not listed as a proxy on any team.")


@bot.command(name="challenge")
async def challenge_cmd(ctx):
    """Immediately cut the current GM's timer to 10 minutes (3 challenges = instant skip)."""
    if draft.state != "active":
        await ctx.send("❌ No active draft.")
        return
    if not draft.current_team:
        await ctx.send("❌ No current pick.")
        return
    if ctx.author.id in draft.current_team["user_ids"]:
        await ctx.send("❌ You can't challenge yourself.")
        return
    await _process_challenge(ctx.author.mention, ctx.author.display_name)


@bot.command(name="timerskip")
async def timerskip(ctx):
    if draft.state != "active":
        await ctx.send("❌ No active draft.")
        return

    team     = draft.current_team
    is_privileged = (
        ctx.author.guild_permissions.administrator
        or any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles)
    )

    if not _is_team_owner(ctx.author.id, team) and not is_privileged:
        await ctx.send(f"❌ Only {_team_mentions(team)} or a commissioner can skip this pick.")
        return

    await ctx.send(f"⏩ {_team_mentions(team)} is skipping. **-10 min** from their future picks.")
    await _do_skip(auto=False)


@bot.command(name="timerunskip")
@is_commissioner()
async def timerunskip(ctx):
    """!timerunskip — undo the most recent skip, restoring the pick and reverting the skip penalty."""
    global _timer_task, _window_task

    if not draft.last_skip:
        await ctx.send("❌ No skip to undo.")
        return

    if draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No active draft.")
        return

    # Cancel any running timers
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
    if _window_task and not _window_task.done():
        _window_task.cancel()

    undo = draft.last_skip

    # Restore draft position
    draft.current_round    = undo["round"]
    draft.current_in_round = undo["in_round"]
    draft.state            = "active"
    draft.timer_start      = None
    draft.paused_remaining = None

    # Revert skip count (and last_pick_number for roundless)
    draft.teams[undo["team_idx"]]["skip_count"] = undo["prev_skip_count"]
    prev_lpn = undo.get("prev_last_pick_number")
    if prev_lpn is not None:
        draft.teams[undo["team_idx"]]["last_pick_number"] = prev_lpn

    draft.last_skip = None  # consumed — can't undo twice
    draft.save()

    team = draft.current_team
    log.info("UNDO SKIP | Pick #%d | Team: %s | Skip count restored to %d",
             draft.overall_pick, team["name"] if team else "?", undo["prev_skip_count"])

    await ctx.send(
        f"↩️ **Skip undone.** Restored to pick **#{draft.overall_pick}** — "
        f"{_team_mentions(team)} is back on the clock."
    )
    await _start_timer()


@bot.command(name="timerstatus")
async def timerstatus(ctx):
    if draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No active draft.")
        return

    if draft.state == "complete":
        await ctx.send("🏆 Draft is complete!")
        return

    team     = draft.current_team
    duration = draft.effective_timer(draft.round_number, draft.current_team_idx)

    if draft.state == "paused":
        remaining = draft.paused_remaining or 0
        time_left = f"⏸️ PAUSED — {int(remaining // 60)}m {int(remaining % 60)}s remaining"
    elif draft.state == "window_paused":
        remaining = draft.paused_remaining or 0
        time_left = f"🌙 WINDOW PAUSED — {int(remaining // 60)}m {int(remaining % 60)}s remaining (resumes 10am ET)"
    elif draft.timer_start:
        elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(draft.timer_start)).total_seconds()
        remaining = max(0, duration - elapsed)
        time_left = f"{int(remaining // 60)}m {int(remaining % 60)}s"
    else:
        time_left = "unknown"

    color = (discord.Color.dark_gray() if draft.state == "window_paused"
             else discord.Color.orange() if draft.state == "paused"
             else discord.Color.blue())

    if draft.mode == "roundless":
        embed = discord.Embed(title="Draft Status — Roundless", color=color)
        embed.add_field(name="Overall Pick", value=str(draft.overall_pick), inline=True)
        embed.add_field(name="Up Now",       value=_team_mentions(team),     inline=True)
        embed.add_field(name="Time Left",    value=time_left,                inline=True)
        embed.add_field(name="Base Timer",   value=f"{duration // 60} min",  inline=True)

        order = draft._roundless_sorted_order()
        current_idx = draft.current_team_idx
        queue_lines = []
        for pos, idx in enumerate(order[:8], 1):
            t = draft.teams[idx]
            money = t.get("money_spent", 0)
            picks = len(t.get("picks", []))
            arrow = " ← **ON CLOCK**" if idx == current_idx else ""
            queue_lines.append(f"**{pos}.** {t['name']} — ${money}, {picks} pick(s){arrow}")
        embed.add_field(name="Pick Queue", value="\n".join(queue_lines) or "—", inline=False)

        skippers = [(t["name"], t.get("skip_count", 0)) for t in draft.teams if t.get("skip_count", 0) > 0]
        if skippers:
            embed.add_field(
                name="Skip Penalties",
                value="\n".join(f"{n}: {c} skip(s) (−{c*10} min)" for n, c in skippers),
                inline=False,
            )
    else:
        embed = discord.Embed(title=f"Draft Status - Round {draft.round_number} of {ROUNDS}", color=color)
        embed.add_field(name="Overall Pick",  value=str(draft.overall_pick),  inline=True)
        embed.add_field(name="Pick in Round", value=str(draft.pick_in_round), inline=True)
        embed.add_field(name="Up Now",        value=_team_mentions(team),      inline=True)
        embed.add_field(name="Time Left",     value=time_left,                 inline=True)
        embed.add_field(name="Base Timer",    value=f"{duration // 60} min",   inline=True)

        if draft.penalty_teams:
            penalised = ", ".join(_team_mentions(draft.teams[i]) for i in draft.penalty_teams)
            embed.add_field(name="Pick Last (R6-10)", value=penalised, inline=False)

        skippers = [(t["name"], t.get("skip_count", 0)) for t in draft.teams if t.get("skip_count", 0) > 0]
        if skippers:
            embed.add_field(
                name="Skip Penalties",
                value="\n".join(f"{n}: {c} skip(s) (−{c*10} min)" for n, c in skippers),
                inline=False,
            )

    await ctx.send(embed=embed)


@bot.command(name="timerskiplist")
async def timerskiplist(ctx):
    """!timerskiplist — show each team's skip count and resulting timer"""
    if draft.state not in ("lotto", "active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft loaded.")
        return

    from config import ROUND_TIMERS, SKIP_PENALTY

    embed = discord.Embed(title="Skip Penalties", color=discord.Color.orange())

    any_skips = False
    for i, team in enumerate(draft.teams):
        skips = team.get("skip_count", 0)
        if skips == 0:
            continue
        any_skips = True
        is_as     = draft.is_active_skip(i)
        deduction = skips * SKIP_PENALTY
        as_tag    = " 🔴 **ACTIVE SKIP**" if is_as else ""

        if draft.mode == "roundless":
            from config import ROUNDLESS_TIMER
            effective = max(ROUNDLESS_TIMER - deduction, 0)
            base_min  = ROUNDLESS_TIMER // 60
            eff_min   = effective // 60
            value = (f"~~{base_min}m~~ → **instant skip**" if effective <= 0
                     else f"~~{base_min}m~~ → **{eff_min}m**")
        else:
            lines = []
            for r in range(1, ROUNDS + 1):
                base      = ROUND_TIMERS.get(r, 1800)
                effective = max(base - deduction, 0)
                base_min  = base // 60
                eff_min   = effective // 60
                if effective <= 0:
                    lines.append(f"R{r}: ~~{base_min}m~~ → **instant skip**")
                else:
                    lines.append(f"R{r}: ~~{base_min}m~~ → **{eff_min}m**")
            value = "\n".join(lines)

        embed.add_field(
            name=f"{team['name']} — {skips} skip(s) (−{skips * 10} min){as_tag}",
            value=value,
            inline=True,
        )

    if not any_skips:
        embed.description = "No skips recorded yet."

    await ctx.send(embed=embed)


@bot.command(name="timerskiphistory")
async def timerskiphistory(ctx, member: discord.Member = None):
    """
    !timerskiphistory           — all-time skip leaderboard across all ATDs
    !timerskiphistory @user     — full skip history for a specific GM
    """
    history = _load_skip_history()

    if not history:
        await ctx.send("📭 No skip history recorded yet.")
        return

    if member is None:
        # ── Leaderboard: aggregate by user_id ────────────────────────────────
        totals: dict[int, dict] = {}   # user_id → {name, skips, atds: set}
        for entry in history:
            for uid in entry["user_ids"]:
                if uid not in totals:
                    totals[uid] = {"name": entry["team_name"], "skips": 0, "atds": set()}
                totals[uid]["skips"] += 1
                label = entry.get("draft_label") or entry.get("draft_started", "?")
                totals[uid]["atds"].add(label)

        sorted_totals = sorted(totals.items(), key=lambda x: x[1]["skips"], reverse=True)

        lines = []
        for rank, (uid, data) in enumerate(sorted_totals, 1):
            member_obj = ctx.guild.get_member(uid)
            name = member_obj.display_name if member_obj else data["name"]
            atd_count = len(data["atds"])
            lines.append(
                f"**{rank}.** <@{uid}> ({name}) — **{data['skips']} skip(s)** across {atd_count} ATD(s)"
            )

        embed = discord.Embed(
            title="Skip History — All-Time Leaderboard",
            description="\n".join(lines),
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed)

    else:
        # ── Detail view for a specific GM ─────────────────────────────────────
        uid = member.id
        entries = [e for e in history if uid in e["user_ids"]]

        if not entries:
            await ctx.send(f"✅ {member.mention} has no skips on record.")
            return

        # Group by draft label
        by_draft: dict[str, list[dict]] = {}
        for entry in entries:
            label = entry.get("draft_label") or (
                datetime.fromisoformat(entry["draft_started"]).strftime("%b %d, %Y")
                if entry.get("draft_started") else "Unknown ATD"
            )
            by_draft.setdefault(label, []).append(entry)

        embed = discord.Embed(
            title=f"Skip History — {member.display_name}",
            description=f"**{len(entries)} total skip(s)** across {len(by_draft)} ATD(s)",
            color=discord.Color.orange(),
        )

        for label, draft_entries in by_draft.items():
            team_name = draft_entries[0]["team_name"]
            lines = []
            for e in draft_entries:
                ts = datetime.fromisoformat(e["timestamp"])
                date_str = ts.strftime("%b %d, %Y")
                skip_type = "timeout" if e.get("auto") else "manual"
                round_str = "" if e.get("mode") == "roundless" else f" (R{e['round_num']})"
                lines.append(f"Pick #{e['pick_num']}{round_str} — {skip_type} — {date_str}")
            embed.add_field(
                name=f"{label} — {len(draft_entries)} skip(s) as \"{team_name}\"",
                value="\n".join(lines),
                inline=False,
            )

        await ctx.send(embed=embed)


def _build_board_embed(chunks, page):
    chunk = chunks[page]
    total = len(chunks)
    title = "Draft Board" if total == 1 else f"Draft Board (page {page+1}/{total})"
    embed = discord.Embed(title=title, color=discord.Color.dark_blue())
    for team in chunk:
        picks = team.get("picks", [])
        pick_text = "\n".join(f"{j+1}. {p}" for j, p in enumerate(picks)) if picks else "_No picks yet_"
        embed.add_field(name=team["name"], value=pick_text, inline=True)
    return embed


class BoardView(discord.ui.View):
    def __init__(self, chunks):
        super().__init__(timeout=300)
        self.chunks = chunks
        self.page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page == len(self.chunks) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=_build_board_embed(self.chunks, self.page), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=_build_board_embed(self.chunks, self.page), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="timerboard")
async def timerboard(ctx):
    if draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft in progress.")
        return

    COMPLETE_PICKS = 10  # teams with this many picks are done and hidden from the board

    if draft.mode == "roundless":
        order = draft._roundless_sorted_order()
        current_idx = draft.current_team_idx
        lines = []
        pos = 1
        for idx in order:
            t = draft.teams[idx]
            picks = len(t.get("picks", []))
            if picks >= COMPLETE_PICKS:
                continue  # team is done — skip
            money = t.get("money_spent", 0)
            last  = t.get("last_pick_number", 0)
            skips = t.get("skip_count", 0)
            skip_str = f" | {skips}x skip" if skips else ""
            arrow = " ← **ON CLOCK**" if idx == current_idx else ""
            lines.append(f"**{pos}.** {t['name']} — ${money} | {picks} picks | last #{last}{skip_str}{arrow}")
            pos += 1
        desc = "\n".join(lines) if lines else "_All teams complete!_"
        if len(desc) > 4000:
            desc = desc[:3997] + "…"
        embed = discord.Embed(
            title=f"Roundless Draft — Pick Order  (Pick #{draft.overall_pick})",
            description=desc,
            color=discord.Color.dark_blue(),
        )
        await ctx.send(embed=embed)
    else:
        teams  = [t for t in draft.teams if len(t.get("picks", [])) < COMPLETE_PICKS]
        chunks = [teams[i:i+25] for i in range(0, len(teams), 25)]
        if not chunks:
            await ctx.send("✅ All teams have completed their picks!")
            return
        view = BoardView(chunks) if len(chunks) > 1 else None
        await ctx.send(embed=_build_board_embed(chunks, 0), view=view)


@bot.command(name="timersettimer")
@is_commissioner()
async def timersettimer(ctx, minutes: int):
    """!timersettimer <minutes> — override the timer for all future picks. Use 0 to revert to defaults."""
    if draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No draft in progress.")
        return

    channel = bot.get_channel(DRAFT_CHANNEL_ID)

    if minutes == 0:
        draft.timer_override = None
        draft.save()
        await ctx.send("✅ Timer override cleared — back to default round timers.")
        return

    draft.timer_override = minutes * 60
    draft.save()
    await ctx.send(f"✅ Timer set to **{minutes} minutes** for all future picks.")

    # If currently active, restart the current pick's timer with the new duration
    if draft.state == "active" and channel:
        await _start_timer(channel)


# ── Roundless sync commands ───────────────────────────────────────────────────

@bot.command(name="timerset")
@is_commissioner()
async def timerset(ctx, member: discord.Member, money: int, picks: int, last_pick: int):
    """!timerset @GM <money> <picks> <last_pick#> — set all three roundless stats at once.
    Example: !timerset @Admiral Ackbar 57 3 65"""
    team_idx = next((i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team = draft.teams[team_idx]
    team["money_spent"] = money
    current = len(team.get("picks", []))
    if picks > current:
        team.setdefault("picks", []).extend(["[manual]"] * (picks - current))
    elif picks < current:
        team["picks"] = team["picks"][:picks]
    team["last_pick_number"] = last_pick
    team["pending_makeup"]   = False
    draft.save()
    await ctx.send(
        f"✅ **{team['name']}** — money: **${money}** | picks: **{picks}** | last pick: **#{last_pick}** | pending cleared."
    )


@bot.command(name="timersetmoney")
@is_commissioner()
async def timersetmoney(ctx, member: discord.Member, amount: int):
    """!timersetmoney @GM <dollars> — set a team's total money spent (roundless setup).
    Example: !timersetmoney @Morgan 51"""
    team_idx = next((i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    draft.teams[team_idx]["money_spent"] = amount
    draft.save()
    await ctx.send(f"✅ **{draft.teams[team_idx]['name']}** money spent set to **${amount}**.")


@bot.command(name="timersetpicks")
@is_commissioner()
async def timersetpicks(ctx, member: discord.Member, count: int):
    """!timersetpicks @GM <count> — set a team's picks-made count (roundless setup).
    Example: !timersetpicks @Morgan 2"""
    team_idx = next((i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team = draft.teams[team_idx]
    current = len(team.get("picks", []))
    if count > current:
        team.setdefault("picks", []).extend(["[manual]"] * (count - current))
    elif count < current:
        team["picks"] = team["picks"][:count]
    draft.save()
    await ctx.send(f"✅ **{team['name']}** picks made set to **{count}**.")


@bot.command(name="timersetlastpick")
@is_commissioner()
async def timersetlastpick(ctx, member: discord.Member, pick_number: int):
    """!timersetlastpick @GM <pick#> — set the overall pick# when this team last picked (roundless setup).
    Example: !timersetlastpick @Morgan 39"""
    team_idx = next((i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    draft.teams[team_idx]["last_pick_number"] = pick_number
    draft.save()
    await ctx.send(f"✅ **{draft.teams[team_idx]['name']}** last pick number set to **#{pick_number}**.")


@bot.command(name="timeraddskip")
@is_commissioner()
async def timeraddskip(ctx, member: discord.Member, count: int = 1):
    """!timeraddskip @GM [count] — manually add skip(s) to a team (default 1).
    Example: !timeraddskip @Morgan   (adds 1 skip)
    Example: !timeraddskip @Morgan 2 (adds 2 skips)"""
    team_idx = next((i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team = draft.teams[team_idx]
    team["skip_count"] = team.get("skip_count", 0) + count
    draft.save()
    new_total = team["skip_count"]
    penalty   = new_total * 600 // 60
    await ctx.send(
        f"✅ **{team['name']}** skip count set to **{new_total}** "
        f"(-{penalty} min off future timers)."
    )


# ── Admin ─────────────────────────────────────────────────────────────────────

@bot.command(name="timerpause")
@is_commissioner()
async def timerpause(ctx):
    global _timer_task
    if draft.state == "paused":
        await ctx.send("❌ Draft is already paused. Use `!timerresume` to continue.")
        return
    if draft.state == "window_paused":
        await ctx.send("❌ Draft is already paused (draft window is closed). Timer resumes automatically at 10am ET.")
        return
    if draft.state != "active":
        await ctx.send("❌ No active draft to pause.")
        return

    team = draft.current_team
    duration = draft.effective_timer(draft.round_number, draft.current_team_idx)

    # Calculate how much time is left
    if draft.timer_start:
        elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(draft.timer_start)).total_seconds()
        remaining = max(0, int(duration - elapsed))
    else:
        remaining = duration

    if _timer_task and not _timer_task.done():
        _timer_task.cancel()

    draft.paused_remaining = remaining
    draft.timer_start      = None
    draft.state            = "paused"
    draft.save()

    mins = remaining // 60
    secs = remaining % 60
    log.info("PAUSE | Team: %s | Remaining: %dm %ds", team["name"], mins, secs)
    await ctx.send(
        f"⏸️ **Draft paused.** {_team_mentions(team)} has **{mins}m {secs}s** remaining.\n"
        f"Use `!timerresume` to continue."
    )


@bot.command(name="timerresume")
@is_commissioner()
async def timerresume(ctx):
    if draft.state != "paused":
        await ctx.send("❌ Draft is not paused.")
        return

    team      = draft.current_team
    remaining = draft.paused_remaining or draft.effective_timer(draft.round_number, draft.current_team_idx)

    draft.state            = "active"
    draft.timer_start      = datetime.now(timezone.utc).isoformat()
    draft.paused_remaining = None
    draft.save()

    mins = remaining // 60
    secs = remaining % 60
    log.info("RESUME | Team: %s | Remaining: %dm %ds", team["name"], mins, secs)
    await ctx.send(f"▶️ **Draft resumed.** {_team_mentions(team)} has **{mins}m {secs}s** to pick.")

    global _timer_task
    _timer_task = asyncio.create_task(_timer_loop(remaining, team["user_ids"]))


@bot.command(name="removeskip")
@is_commissioner()
async def removeskip(ctx, member: discord.Member):
    """
    Remove one skip from a GM who was wrongfully skipped.
    Usage: !removeskip @GM
    Decrements their skip_count by 1 (min 0) and saves.
    """
    if draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No active draft.")
        return

    team_idx = next(
        (i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]),
        None
    )
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    team = draft.teams[team_idx]
    current = team.get("skip_count", 0)
    if current <= 0:
        await ctx.send(f"❌ **{team['name']}** has no skips to remove.")
        return

    team["skip_count"] = current - 1
    draft.save()

    # Remove the most recent skip history entry for this team
    history = _load_skip_history()
    target_ids = set(team["user_ids"])
    # Find the last entry that belongs to this team
    remove_idx = None
    for i in range(len(history) - 1, -1, -1):
        if set(history[i].get("user_ids", [])) & target_ids:
            remove_idx = i
            break
    if remove_idx is not None:
        history.pop(remove_idx)
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

    await ctx.send(
        f"✅ Removed 1 skip from **{team['name']}** ({current} → {current - 1} skips)."
    )


@bot.command(name="clearpending")
@is_commissioner()
async def clearpending(ctx, member: discord.Member):
    """!clearpending @GM — clear a team's pending makeup flag so they re-enter the roundless queue normally."""
    team_idx = next(
        (i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]),
        None
    )
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    team = draft.teams[team_idx]
    if not team.get("pending_makeup"):
        await ctx.send(f"ℹ️ **{team['name']}** is not marked as pending.")
        return

    team["pending_makeup"] = False
    draft.save()
    await ctx.send(
        f"✅ **{team['name']}** pending flag cleared — they re-enter the queue based on "
        f"${team.get('money_spent', 0)} spent, {len(team.get('picks', []))} pick(s)."
    )


@bot.command(name="markpending")
@is_commissioner()
async def markpending(ctx, member: discord.Member):
    """
    Mark a GM as having a pending makeup pick (they were skipped and haven't picked yet).
    Their next turn will be skipped immediately instead of giving them a timer.
    Usage: !markpending @GM
    """
    if draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No active draft.")
        return

    team_idx = next(
        (i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]),
        None
    )
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    team = draft.teams[team_idx]
    team["pending_makeup"] = True
    draft.save()
    await ctx.send(
        f"✅ **{team['name']}** marked as having a pending makeup pick. "
        f"They will be skipped immediately on their next turn."
    )


@bot.command(name="addskip")
@is_commissioner()
async def addskip(ctx, member: discord.Member):
    """
    Add one skip to a GM (e.g. a missed pick before a restart that wasn't recorded).
    Usage: !addskip @GM
    """
    if draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No active draft.")
        return

    team_idx = next(
        (i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]),
        None
    )
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    team = draft.teams[team_idx]
    current = team.get("skip_count", 0)
    team["skip_count"] = current + 1
    draft.save()

    _append_skip_history({
        "draft_label":   draft.draft_label or draft.draft_started or "Unknown ATD",
        "draft_started": draft.draft_started,
        "user_ids":      list(team["user_ids"]),
        "team_name":     team["name"],
        "pick_num":      draft.overall_pick,
        "round_num":     draft.round_number,
        "auto":          True,
        "mode":          draft.mode,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

    new_count = current + 1
    as_note = f" — **⚡ Active Skip** (will be skipped immediately)" if new_count >= AS_THRESHOLD else ""
    await ctx.send(
        f"✅ Added 1 skip to **{team['name']}** ({current} → {new_count} skips){as_note}."
    )


@bot.command(name="timerpenalty")
@is_commissioner()
async def timerpenalty(ctx, *mentions: discord.Member):
    """
    Retroactively apply the LeBron/MJ penalty to one or more GMs.
    Usage: !timerpenalty @CCarp @Francis
    This moves those teams to the END of every round from Round 6 onward.
    """
    if draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No active draft.")
        return
    if not mentions:
        await ctx.send("❌ Usage: `!timerpenalty @GM1 @GM2 ...`")
        return

    applied = []
    already = []
    not_found = []

    for member in mentions:
        # Find which team this member belongs to
        team_idx = next(
            (i for i, t in enumerate(draft.teams) if member.id in t["user_ids"]),
            None
        )
        if team_idx is None:
            not_found.append(member.display_name)
        elif team_idx in draft.penalty_teams:
            already.append(draft.teams[team_idx]["name"])
        else:
            draft.apply_penalty(team_idx)
            applied.append(draft.teams[team_idx]["name"])

    draft.save()

    lines = []
    if applied:
        lines.append(f"✅ Penalty applied to: **{', '.join(applied)}** — they will pick last from Round 6 onward.")
    if already:
        lines.append(f"ℹ️ Already penalised: {', '.join(already)}")
    if not_found:
        lines.append(f"❌ Not in draft: {', '.join(not_found)}")

    await ctx.send("\n".join(lines) if lines else "Nothing changed.")


@bot.command(name="timereset")
@is_commissioner()
async def timereset(ctx):
    global draft, _timer_task
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
    draft = DraftState()
    draft.save()
    await ctx.send("🔄 Draft has been reset.")


@bot.command(name="timerhelp")
async def timerhelp(ctx):
    embed = discord.Embed(title="ATD Timer Bot - Command Reference", color=discord.Color.orange())

    embed.add_field(name="📌 How to Pick", value=(
        "Just type your pick in the draft channel - no command needed:\n"
        "`14. :YourEmoji: Marc Gasol 2012-13`\n"
        "The bot matches the pick number, confirms with ✅, and pings the next person."
    ), inline=False)

    embed.add_field(name="⚙️ Setup - LeComissioner Only", value=(
        "`!timerloadlotto` - reads the most recent lotto from the lotto channel automatically\n"
        "`!timerlottoupdate` - re-reads the lotto to pick up roster changes (e.g. new co-owner added)\n"
        "`!timerstart [name]` - begin the draft (optional name e.g. `ATD 101` is saved to skip history)\n"
        "`!timermode roundless` - switch to roundless (money-based) mode at any time\n"
        "`!timermode snake` - switch back to normal snake mode\n"
        "`!timerpause` - freeze the clock mid-pick\n"
        "`!timerresume` - resume from where it was paused\n"
        "`!timerjumpto <pick#>` - jump to a specific pick (use when picks were made before the bot started)\n"
        "`!timereset` - cancel and wipe the entire draft\n"
        "`!timerproxy @user` - let someone else pick for the current team while their GM is away\n"
        "`!timerremoveproxy @user` - remove a proxy once the GM is back"
    ), inline=False)

    embed.add_field(name="📋 During the Draft", value=(
        "`!timerskip` - skip your turn (costs **-10 min** on all your future picks)\n"
        "`!timerunskip` - *(commissioner)* undo the most recent skip — restores pick & reverts the penalty\n"
        "`!timerstatus` - show current round, pick number, who's up, and time remaining\n"
        "`!timerskiplist` - show every team's skip count and their adjusted timer per round\n"
        "`!timerskiphistory` - all-time skip leaderboard across all ATDs\n"
        "`!timerskiphistory @user` - full skip breakdown for a specific GM\n"
        "`!timerboard` - show all picks made so far"
    ), inline=False)

    embed.add_field(name="⏱️ Round Timers (Snake)", value=(
        "R1–2: **60 min**\n"
        "R3–8: **45 min**\n"
        "R9–10: **30 min**\n"
        "Each skip deducts **10 min** from all of that team's future picks."
    ), inline=True)

    embed.add_field(name="🔄 Roundless Mode", value=(
        "Pick order is computed **after every pick** based on:\n"
        "1. Less money spent → picks sooner\n"
        "2. Fewer picks made → picks sooner\n"
        "3. More time since last pick → picks sooner\n"
        "Picks must include price: `14. :Emoji: Player Name $42 Year`\n"
        "No fixed rounds — use `!timereset` to end the draft."
    ), inline=False)

    embed.add_field(name="⚡ Challenge Rules", value=(
        "After a GM is pinged, if they post in **#atd-chat**, anyone can reply to that message with `challenge`.\n"
        "• First/second challenge → GM's timer is cut to **10 minutes**.\n"
        "• Third challenge → GM is **skipped immediately**.\n"
        "Only 1 challenge counts per message. GMs cannot challenge themselves."
    ), inline=False)

    embed.add_field(name="⚠️ Special Rules", value=(
        "Drafting **LeBron James** or **Michael Jordan** → that team picks **last** in every round from R6–R10.\n"
        "**Round 3 flip** + **Round 6 flip** (ATD snake order).\n"
        "Co-owners: either GM on a team can submit the pick."
    ), inline=True)

    embed.add_field(name="🌙 Draft Window", value=(
        "Picks are only timed between **10:00 AM – midnight ET**.\n"
        "Outside that window the timer auto-pauses and resumes at 10am ET.\n"
        "Manual picks made outside the window are still accepted and advance the draft normally."
    ), inline=False)

    embed.set_footer(text="Warning fires at 5 min remaining. Auto-skip triggers on timeout.")
    await ctx.send(embed=embed)


# ── Run ───────────────────────────────────────────────────────────────────────

bot.run(DISCORD_TOKEN)
