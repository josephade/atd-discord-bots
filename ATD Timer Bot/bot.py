"""
ATD Timer Bot — Discord bot for managing timed ATD draft picks.
Supports multiple parallel drafts across different channels simultaneously.

Picks are detected automatically from messages in any registered draft channel.
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
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from config import (AS_THRESHOLD, ATD_CHAT_CHANNEL_ID, DISCORD_TOKEN,
                    DRAFT_CHANNEL_ID, DRAFT_LIST_BOT_ID, LOTTO_CHANNEL_ID,
                    PENALTY_PLAYERS, ROUNDS)
from draft import DraftState, HISTORY_FILE, build_snake_order, state_file, _state_dir

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

# ── Draft window (Eastern Time) ───────────────────────────────────────────────

_ET           = ZoneInfo("America/New_York")
_WINDOW_START = 10   # 10:00 AM ET (inclusive)


def _in_window() -> bool:
    return datetime.now(_ET).hour >= _WINDOW_START


def _secs_until_close() -> float:
    now   = datetime.now(_ET)
    close = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (close - now).total_seconds())


def _secs_until_open() -> float:
    now   = datetime.now(_ET)
    open_ = now.replace(hour=_WINDOW_START, minute=0, second=0, microsecond=0)
    if now.hour >= _WINDOW_START:
        open_ += timedelta(days=1)
    return max(0.0, (open_ - now).total_seconds())


# ── Commissioner check ────────────────────────────────────────────────────────

COMMISSIONER_ROLE = "LeComissioner"
DRAFTER_ROLE      = "Drafter"


def is_commissioner():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True
        if any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles):
            return True
        raise commands.CheckFailure(
            f"❌ You need the **{COMMISSIONER_ROLE}** role or administrator permissions."
        )
    return commands.check(predicate)


# ── Per-channel draft session ─────────────────────────────────────────────────

class DraftSession:
    """All mutable state for one draft channel."""

    def __init__(self, channel_id: int):
        self.channel_id              = channel_id
        self.draft                   = DraftState.load(channel_id)
        self.timer_task:   asyncio.Task | None = None
        self.window_task:  asyncio.Task | None = None
        self.active_ping:    discord.Message | None = None
        self.active_warning: discord.Message | None = None
        self.ping_time:          datetime | None = None
        self.challenge_count:    int  = 0
        self.challenged_msg_ids: set  = set()
        self.processing_picks:   set  = set()
        self.processed_msg_ids:  set  = set()  # prevents duplicate pick processing for same message
        self.pending_timer_start: bool = False

    @property
    def channel(self) -> discord.TextChannel | None:
        return bot.get_channel(self.channel_id)


# Registry: channel_id → DraftSession
_sessions: dict[int, DraftSession] = {}


def _get_session(channel_id: int) -> DraftSession:
    if channel_id not in _sessions:
        _sessions[channel_id] = DraftSession(channel_id)
    return _sessions[channel_id]


def _list_saved_channels() -> list[int]:
    """Scan the state directory for existing draft state files."""
    result = []
    try:
        for fname in os.listdir(_state_dir):
            m = re.match(r'^draft_state_(\d+)\.json$', fname)
            if m:
                result.append(int(m.group(1)))
    except OSError:
        pass
    return result


# ── Skip history (shared across all drafts) ───────────────────────────────────

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


# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches:  14. <:Pacers:123> Marc Gasol 2012-13
#           14. :Pacers: Marc Gasol 2012-13
#           14. Marc Gasol 2012-13
_PICK_RE = re.compile(
    r'^(\d+)\s*\.\s+'
    r'(?:<a?:[^:]+:\d+>|:[^:\s]+:|[\U0001F000-\U0001FFFF\U00002600-\U000027BF⌀-⛿✀-➿︀-️]+)?\s*'
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_player_name(raw: str) -> str:
    text = re.sub(r'<:[^:]+:\d+>', '', raw).strip()   # <:emoji:id>
    text = re.sub(r'^:[^:\s]+:\s*', '', text).strip()  # :emoji:
    text = re.sub(r'^[^a-zA-Z0-9]+', '', text).strip() # leading unicode emoji / symbols
    text = re.sub(r'^selects\s+', '', text, flags=re.IGNORECASE).strip()  # "selects" keyword
    # Strip a leading year that appears before the player name (e.g. "13 LeBron James" → "LeBron James")
    text = re.sub(r"^'?\d{2,4}(-\d{2,4})?\s+", '', text).strip()
    # Strip trailing year/season suffixes
    text = re.sub(r"\s+'?\d{2}'-?\d{0,2}$", '', text).strip()
    text = re.sub(r'\s+\d{4}(-\d{2,4})?$', '', text).strip()
    text = re.sub(r"\s+'?\d{2}'?$", '', text).strip()
    return text


def _pick_name_key(raw: str) -> str:
    name = _extract_player_name(raw)
    name = _PRICE_RE.sub('', name).strip()
    return name.lower()


def _team_mentions(team: dict) -> str:
    return " ".join(f"<@{uid}>" for uid in team["user_ids"])


def _is_team_owner(user_id: int, team: dict) -> bool:
    return user_id in team["user_ids"]


def _pick_title(s: DraftSession) -> str:
    if s.draft.mode == "roundless":
        return f"Pick {s.draft.overall_pick}"
    return f"Round {s.draft.round_number} of {ROUNDS}  -  Pick {s.draft.overall_pick}"


def _pick_format(s: DraftSession) -> str:
    if s.draft.mode == "roundless":
        return f"`{s.draft.overall_pick}. :YourEmoji: Player Name $Price Year`"
    return f"`{s.draft.overall_pick}. :YourEmoji: Player Name Year`"


def _parse_lotto_message(content: str, guild: discord.Guild) -> list[dict] | None:
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
    return [teams_by_pos[p] for p in sorted(teams_by_pos)]


# ── Timer helpers (all take a DraftSession) ───────────────────────────────────

async def _ping_current(s: DraftSession, remaining: int = None):
    team     = s.draft.current_team
    duration = (remaining if remaining is not None
                else s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx))

    log.info(
        "PING | ch=%d | Round %d Pick %d (overall #%d) | Team: %s | Timer: %d min",
        s.channel_id, s.draft.round_number, s.draft.pick_in_round,
        s.draft.overall_pick, team["name"], duration // 60,
    )

    deadline_ts = int(datetime.now(timezone.utc).timestamp()) + duration
    embed = discord.Embed(
        title=_pick_title(s),
        description=(
            f"{_team_mentions(team)} it's your turn!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in this channel:\n"
            f"{_pick_format(s)}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use !timerskip to pass." if s.draft.timer_override is not None else "Use !timerskip to pass (costs 10 min on future picks).")
    s.ping_time          = datetime.now(timezone.utc)
    s.challenge_count    = 0
    s.challenged_msg_ids = set()
    s.active_ping = await s.channel.send(content=_team_mentions(team), embed=embed)


async def _delete_active_ping(s: DraftSession):
    for msg in (s.active_ping, s.active_warning):
        if msg:
            try:
                await msg.delete()
            except discord.NotFound:
                pass
    s.active_ping    = None
    s.active_warning = None
    s.ping_time          = None
    s.challenge_count    = 0
    s.challenged_msg_ids = set()


async def _auto_pause_for_window(s: DraftSession, remaining: float, next_up: bool = False):
    team      = s.draft.current_team
    if not team:
        return
    remaining = max(0, int(remaining))
    s.draft.paused_remaining = remaining
    s.draft.timer_start      = None
    s.draft.state            = "window_paused"
    s.draft.save(s.channel_id)

    channel  = s.channel
    mins, sec = remaining // 60, remaining % 60
    log.info("WINDOW PAUSE | ch=%d | next_up=%s | Team: %s | Remaining: %dm %ds",
             s.channel_id, next_up, team["name"], mins, sec)

    if next_up:
        embed = discord.Embed(
            title=_pick_title(s),
            description=(
                f"{_team_mentions(team)} it's your turn!\n\n"
                f"🌙 Draft window is closed — your **{mins}m {sec}s** timer starts at **10:00 AM ET**.\n\n"
                f"Type your pick in this channel:\n"
                f"{_pick_format(s)}"
            ),
            color=discord.Color.dark_gray(),
        )
        embed.set_footer(text="Use !timerskip to pass." if s.draft.timer_override is not None else "Use !timerskip to pass (costs 10 min on future picks).")
        s.active_ping = await channel.send(content=_team_mentions(team), embed=embed)
    else:
        await channel.send(
            f"🌙 **Draft window closed** (midnight ET). Timer paused.\n"
            f"{_team_mentions(team)} has **{mins}m {sec}s** remaining — resumes at **10:00 AM ET**."
        )

    s.window_task = asyncio.create_task(_window_resume_task(s, _secs_until_open()))


async def _window_resume_task(s: DraftSession, sleep_secs: float):
    await asyncio.sleep(sleep_secs)
    if s.draft.state != "window_paused":
        return
    team = s.draft.current_team
    if not team:
        return

    remaining = (s.draft.paused_remaining
                 or s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx))
    s.draft.state            = "active"
    s.draft.timer_start      = datetime.now(timezone.utc).isoformat()
    s.draft.paused_remaining = None
    s.draft.save(s.channel_id)

    channel  = s.channel
    mins, sec = remaining // 60, remaining % 60
    log.info("WINDOW RESUME | ch=%d | Team: %s | Remaining: %dm %ds",
             s.channel_id, team["name"], mins, sec)

    deadline_ts = int(datetime.now(timezone.utc).timestamp()) + remaining
    embed = discord.Embed(
        title=_pick_title(s),
        description=(
            f"{_team_mentions(team)} it's your turn!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in this channel:\n"
            f"{_pick_format(s)}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use !timerskip to pass." if s.draft.timer_override is not None else "Use !timerskip to pass (costs 10 min on future picks).")
    s.active_ping = await channel.send(
        content=f"☀️ **Draft window open!** {_team_mentions(team)}", embed=embed
    )
    s.timer_task = asyncio.create_task(_timer_loop(s, remaining, team["user_ids"]))


async def _timer_loop(s: DraftSession, duration: int, user_ids: list[int]):
    channel = s.channel
    loop    = asyncio.get_event_loop()
    start   = loop.time()

    def _elapsed() -> float:
        return loop.time() - start

    def _remaining() -> float:
        return max(0.0, duration - _elapsed())

    def _still_their_turn() -> bool:
        return (
            s.draft.state == "active"
            and s.draft.current_team is not None
            and any(uid in s.draft.current_team["user_ids"] for uid in user_ids)
        )

    async def _checked_sleep(target_elapsed: float) -> bool:
        while True:
            rem_sleep = target_elapsed - _elapsed()
            if rem_sleep <= 0:
                return True
            sleep = min(rem_sleep, _secs_until_close(), 60.0)
            await asyncio.sleep(max(sleep, 0))
            if not _in_window():
                return False
            if _elapsed() >= target_elapsed - 0.5:
                return True

    mentions = " ".join(f"<@{uid}>" for uid in user_ids)

    if duration > 300:
        ok = await _checked_sleep(duration - 300)
        if not ok:
            if _still_their_turn():
                await _auto_pause_for_window(s, _remaining())
            return
        if _still_their_turn():
            log.info("WARNING | ch=%d | 5 min remaining | Team: %s",
                     s.channel_id, s.draft.current_team["name"] if s.draft.current_team else "?")
            s.active_warning = await channel.send(f"⚠️ {mentions} - **5 minutes remaining**!")

    ok = await _checked_sleep(duration)
    if not ok:
        if _still_their_turn():
            await _auto_pause_for_window(s, _remaining())
        return

    if _still_their_turn():
        log.info("TIMEOUT | ch=%d | Auto-skip | Team: %s",
                 s.channel_id, s.draft.current_team["name"] if s.draft.current_team else "?")
        await _do_skip(s, auto=True)


async def _process_challenge(s: DraftSession, challenger_mention: str, challenger_name: str):
    s.challenge_count += 1
    team    = s.draft.current_team
    channel = s.channel

    log.info("CHALLENGE #%d | ch=%d | Challenger: %s | Team: %s",
             s.challenge_count, s.channel_id, challenger_name, team["name"])

    if s.challenge_count >= 3:
        await channel.send(
            f"⚡ **Challenge #{s.challenge_count}!** {challenger_mention} challenged "
            f"{_team_mentions(team)} — **3 challenges reached, skipping immediately!**"
        )
        s.challenge_count = 0
        await _do_skip(s, auto=True)
        return

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()

    for msg in (s.active_ping, s.active_warning):
        if msg:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
    s.active_warning = None

    new_duration = 600
    deadline_ts  = int(datetime.now(timezone.utc).timestamp()) + new_duration

    embed = discord.Embed(
        title=_pick_title(s),
        description=(
            f"⚡ **Challenge #{s.challenge_count}!** {challenger_mention} challenged "
            f"{_team_mentions(team)}!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in this channel:\n"
            f"{_pick_format(s)}"
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="Use !timerskip to pass." if s.draft.timer_override is not None else "Use !timerskip to pass (costs 10 min on future picks).")

    s.active_ping      = await channel.send(content=_team_mentions(team), embed=embed)
    s.draft.timer_start = datetime.now(timezone.utc).isoformat()
    s.draft.save(s.channel_id)
    s.timer_task = asyncio.create_task(_timer_loop(s, new_duration, team["user_ids"]))


async def _start_timer(s: DraftSession):
    s.pending_timer_start = True
    try:
        await _start_timer_inner(s)
    finally:
        s.pending_timer_start = False


async def _start_timer_inner(s: DraftSession):
    current = asyncio.current_task()
    if s.timer_task and not s.timer_task.done() and s.timer_task is not current:
        s.timer_task.cancel()

    team = s.draft.current_team
    if not team or s.draft.state != "active":
        log.warning("_start_timer: ch=%d early return — team=%s state=%s",
                    s.channel_id, team["name"] if team else None, s.draft.state)
        return

    channel = s.channel
    if not channel:
        log.error("_start_timer: channel %d not found in cache", s.channel_id)
        return

    if not _in_window():
        duration = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)
        await _auto_pause_for_window(s, duration, next_up=True)
        return

    if s.draft.mode == "roundless" and team.get("pending_makeup"):
        log.info("PENDING MAKEUP SKIP | ch=%d | Pick %d | Team: %s",
                 s.channel_id, s.draft.overall_pick, team["name"])
        await channel.send(
            f"⏩ **{_team_mentions(team)} ({team['name']})** has a pending makeup pick — skipping immediately."
        )
        await _do_skip(s, auto=True)
        return

    if s.draft.is_active_skip(s.draft.current_team_idx):
        log.info("ACTIVE SKIP | ch=%d | Round %d | Pick %d | Team: %s | Skips: %d",
                 s.channel_id, s.draft.round_number, s.draft.overall_pick, team["name"],
                 team.get("skip_count", 0))
        await channel.send(
            f"⏩ **{_team_mentions(team)} ({team['name']})** is on "
            f"**Active Skip (AS)** — {AS_THRESHOLD}+ skips recorded. Skipping immediately."
        )
        await _do_skip(s, auto=True)
        return

    duration = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)

    if duration <= 0:
        log.info("TIMER ZERO | ch=%d | Round %d | Pick %d | Team: %s | Auto-skipping",
                 s.channel_id, s.draft.round_number, s.draft.overall_pick, team["name"])
        await channel.send(
            f"⏩ {_team_mentions(team)} — timer has been fully consumed by skip penalties. Auto-skipping."
        )
        await _do_skip(s, auto=True)
        return

    log.info(
        "TIMER START | ch=%d | Round %d | Pick %d | Team: %s | Duration: %d sec (%d min)",
        s.channel_id, s.draft.round_number, s.draft.overall_pick,
        team["name"], duration, duration // 60,
    )
    s.draft.timer_start = datetime.now(timezone.utc).isoformat()
    s.draft.save(s.channel_id)

    await _ping_current(s)
    s.timer_task = asyncio.create_task(_timer_loop(s, duration, team["user_ids"]))


async def _do_skip(s: DraftSession, auto: bool = False):
    current = asyncio.current_task()
    if s.timer_task and not s.timer_task.done() and s.timer_task is not current:
        s.timer_task.cancel()

    team = s.draft.current_team
    if not team:
        return

    pick_num   = s.draft.overall_pick
    team_idx   = s.draft.current_team_idx
    mentions   = _team_mentions(team)
    prev_skip  = team.get("skip_count", 0)
    skip_count = prev_skip + 1
    prev_last_pick = team.get("last_pick_number", 0)

    team["skip_count"] = skip_count
    team["pending_makeup"] = True
    if s.draft.mode == "roundless":
        team["last_pick_number"] = pick_num

    s.draft.last_skip = {
        "round":                 s.draft.current_round,
        "in_round":              s.draft.current_in_round,
        "team_idx":              team_idx,
        "prev_skip_count":       prev_skip,
        "prev_last_pick_number": prev_last_pick,
    }

    from config import SKIP_PENALTY
    if s.draft.timer_override is not None:
        skip_note = f"{skip_count} skip{'s' if skip_count != 1 else ''}"
    elif s.draft.mode == "roundless":
        from config import ROUNDLESS_TIMER
        next_timer_min = max((ROUNDLESS_TIMER - skip_count * SKIP_PENALTY) // 60, 0)
        skip_note = f"{skip_count} skip{'s' if skip_count != 1 else ''} - {next_timer_min}m left on future picks"
    else:
        from config import ROUND_TIMERS
        next_timer_min = max((ROUND_TIMERS.get(s.draft.round_number, 1800) - skip_count * SKIP_PENALTY) // 60, 0)
        skip_note = f"{skip_count} skip{'s' if skip_count != 1 else ''} - {next_timer_min}m left on future picks"

    log.info(
        "SKIP | ch=%d | %s | Team: %s | Total skips: %d",
        s.channel_id, "auto (timeout)" if auto else "manual", team["name"], skip_count,
    )

    _append_skip_history({
        "channel_id":    s.channel_id,
        "draft_label":   s.draft.draft_label or s.draft.draft_started or "Unknown ATD",
        "draft_started": s.draft.draft_started,
        "user_ids":      list(team["user_ids"]),
        "team_name":     team["name"],
        "pick_num":      pick_num,
        "round_num":     s.draft.round_number,
        "auto":          auto,
        "mode":          s.draft.mode,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

    await _delete_active_ping(s)

    s.draft.advance()
    s.draft.timer_start = None
    s.draft.save(s.channel_id)

    channel = s.channel
    await channel.send(
        f"**{pick_num}.** {mentions} skipped ({skip_note})"
    )

    if s.draft.state == "complete":
        await channel.send("🏆 **Draft complete!**")
        return

    await _start_timer(s)


async def _try_process_roundless_makeup(s: DraftSession, message: discord.Message):
    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num_in_msg = int(match.group(1))
    if pick_num_in_msg > s.draft.overall_pick:
        return

    team = next((t for t in s.draft.teams if message.author.id in t["user_ids"]), None)
    if not team or not team.get("pending_makeup"):
        return

    if pick_num_in_msg == s.draft.overall_pick and s.draft.current_team is team:
        return

    already_done = any(r.emoji == "✅" and r.me for r in message.reactions)
    if already_done:
        return

    pick_raw = match.group(2).strip()
    price_m  = _PRICE_RE.search(pick_raw)
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
    s.draft.save(s.channel_id)

    log.info("MAKEUP PICK | ch=%d | Team: %s | Pick #%d | %s",
             s.channel_id, team["name"], pick_num_in_msg, pick_raw)
    await message.add_reaction("✅")


async def _try_process_pick(s: DraftSession, message: discord.Message, is_edit: bool = False):
    if s.draft.state not in ("active", "paused", "window_paused"):
        return

    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num = int(match.group(1))
    pick_raw = match.group(2).strip()

    if pick_num != s.draft.overall_pick:
        return

    # Edits are allowed to re-process (content changed); duplicate fires of the
    # same message are not — guard against Discord re-triggering on_message.
    if not is_edit and message.id in s.processed_msg_ids:
        log.info("PICK GUARD | ch=%d | Message %d already processed", s.channel_id, message.id)
        return
    s.processed_msg_ids.add(message.id)

    if pick_num in s.processing_picks:
        log.info("PICK GUARD | ch=%d | Pick #%d already being processed", s.channel_id, pick_num)
        return
    s.processing_picks.add(pick_num)

    success = False
    try:
        team = s.draft.current_team

        is_commissioner_pick = (
            bool(DRAFT_LIST_BOT_ID and message.author.id == DRAFT_LIST_BOT_ID)
            or message.author.guild_permissions.administrator
            or any(r.name == COMMISSIONER_ROLE for r in message.author.roles)
            or any(r.name == DRAFTER_ROLE for r in message.author.roles)
        )
        if not _is_team_owner(message.author.id, team) and not is_commissioner_pick:
            return

        if pick_num != s.draft.overall_pick:
            return

        player_name = _extract_player_name(pick_raw)
        player_key  = _pick_name_key(pick_raw)
        for t in s.draft.teams:
            for p in t.get("picks", []):
                if _pick_name_key(p) == player_key:
                    log.info("DUPLICATE PICK | ch=%d | Player: %s | Already taken by: %s",
                             s.channel_id, player_name, t["name"])
                    await message.add_reaction('❌')
                    await message.channel.send(
                        f"❌ {message.author.mention} — **{player_name}** has already been taken by **{t['name']}**. Pick someone else."
                    )
                    return

        log.info(
            "PICK | ch=%d | Overall #%d | Round %d Pick %d | Team: %s | Player: %s",
            s.channel_id, s.draft.overall_pick, s.draft.round_number, s.draft.pick_in_round,
            team["name"], pick_raw,
        )
        if s.timer_task and not s.timer_task.done():
            s.timer_task.cancel()
        if s.window_task and not s.window_task.done():
            s.window_task.cancel()
        await _delete_active_ping(s)
        if s.draft.state in ("window_paused", "paused"):
            s.draft.state            = "active"
            s.draft.paused_remaining = None

        team["picks"].append(pick_raw)
        team["pending_makeup"] = False

        if s.draft.mode == "roundless":
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
            team_idx = s.draft.current_team_idx
            if team_idx not in s.draft.penalty_teams:
                s.draft.apply_penalty(team_idx)
                penalty_note = (
                    f"⚠️ **{team['name']}** drafted **{player_name}** - "
                    f"they will pick **last** every round from Round 6 onward."
                )

        s.draft.advance()
        s.draft.save(s.channel_id)
        success = True

        await message.add_reaction("✅")

        if penalty_note:
            await message.channel.send(penalty_note)

        if s.draft.state == "complete":
            await message.channel.send("🏆 **Draft complete! Great picks everyone.**")
            return

    except Exception as exc:
        log.error("Error processing pick #%d (ch=%d): %s", pick_num, s.channel_id, exc, exc_info=True)
        if not success:
            try:
                await message.channel.send(f"⚠️ Error processing pick: {exc}")
            except Exception:
                pass

    finally:
        s.processing_picks.discard(pick_num)

    if success and s.draft.state not in ("complete", None):
        try:
            await _start_timer(s)
        except Exception as exc:
            log.error("Timer start failed after pick #%d (ch=%d): %s",
                      pick_num, s.channel_id, exc, exc_info=True)
            channel = s.channel
            if channel:
                await channel.send(
                    f"⚠️ Pick recorded but the next timer failed to start: `{exc}`\n"
                    f"Use `!timerjumpto {s.draft.overall_pick}` to recover."
                )


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Bot online — logged in as %s (id: %s)", bot.user, bot.user.id)

    for ch_id in _list_saved_channels():
        s = _get_session(ch_id)
        channel = bot.get_channel(ch_id)
        ch_name = f"#{channel.name}" if channel else str(ch_id)
        log.info("Restoring session | ch=%d (%s) | state=%s", ch_id, ch_name, s.draft.state)

        if not channel:
            log.warning("Channel %d not in cache — will restore timers when first used", ch_id)
            continue

        if s.draft.state == "window_paused" and s.draft.current_team:
            team      = s.draft.current_team
            remaining = (s.draft.paused_remaining
                         or s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx))
            mins, sec = remaining // 60, remaining % 60

            if _in_window():
                s.draft.state            = "active"
                s.draft.timer_start      = datetime.now(timezone.utc).isoformat()
                s.draft.paused_remaining = None
                s.draft.save(ch_id)
                await channel.send(
                    f"🔄 Bot restarted - draft window is open. Resuming {_team_mentions(team)}'s turn "
                    f"(**{mins}m {sec}s** remaining)."
                )
                s.timer_task = asyncio.create_task(_timer_loop(s, remaining, team["user_ids"]))
                await _ping_current(s, remaining=remaining)
            else:
                await channel.send(
                    f"🔄 Bot restarted - draft window is closed. {_team_mentions(team)} has "
                    f"**{mins}m {sec}s** remaining.\nTimer will resume at **10:00 AM ET**."
                )
                s.window_task = asyncio.create_task(_window_resume_task(s, _secs_until_open()))

        elif s.draft.state == "active" and s.draft.timer_start and s.draft.current_team:
            elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(s.draft.timer_start)).total_seconds()
            duration  = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)
            remaining = duration - elapsed
            team      = s.draft.current_team

            if remaining <= 0:
                await channel.send(
                    f"🔄 Bot restarted - {_team_mentions(team)}'s time had already expired. Auto-skipping…"
                )
                await _do_skip(s, auto=True)
            else:
                s.timer_task = asyncio.create_task(_timer_loop(s, int(remaining), team["user_ids"]))
                await _ping_current(s, remaining=int(remaining))

    asyncio.create_task(_missed_pick_scanner())


async def _missed_pick_scanner():
    await asyncio.sleep(30)
    while True:
        await asyncio.sleep(30)
        try:
            for ch_id, s in list(_sessions.items()):
                if s.draft.state not in ("active", "paused", "window_paused"):
                    continue
                channel = s.channel
                if not channel:
                    continue

                expected_pick = s.draft.overall_pick

                async for msg in channel.history(limit=30):
                    if msg.author.bot:
                        continue
                    match = _PICK_RE.match(msg.content.strip())
                    if not match:
                        continue
                    if int(match.group(1)) != expected_pick:
                        continue
                    already_done = any(r.emoji == "✅" and r.me for r in msg.reactions)
                    if already_done:
                        break
                    log.info(
                        "MISSED PICK RECOVERED | ch=%d | Overall #%d | Author: %s | Content: %s",
                        ch_id, expected_pick, msg.author.display_name, msg.content[:80],
                    )
                    await _try_process_pick(s, msg)
                    break

                # Watchdog: active draft but no ping and dead/missing timer
                if (s.draft.state == "active"
                        and _in_window()
                        and s.draft.current_team
                        and not s.pending_timer_start
                        and (s.timer_task is None or s.timer_task.done() or s.active_ping is None)):
                    log.warning(
                        "WATCHDOG | ch=%d | No active ping / dead timer | Pick #%d | Team: %s — restarting",
                        ch_id, s.draft.overall_pick, s.draft.current_team["name"],
                    )
                    await _start_timer(s)

        except Exception as exc:
            log.warning("Missed-pick scanner error: %s", exc)


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    _from_draft_list = bool(DRAFT_LIST_BOT_ID and message.author.id == DRAFT_LIST_BOT_ID)
    if message.author.bot and not _from_draft_list:
        return

    # ── Challenge detection: reply in ATD_CHAT_CHANNEL_ID ────────────────────
    if (message.channel.id == ATD_CHAT_CHANNEL_ID
            and message.reference
            and message.content.strip().lower() == "challenge"):
        for ch_id, s in list(_sessions.items()):
            if s.draft.state != "active" or not s.draft.current_team:
                continue
            if message.author.id in s.draft.current_team["user_ids"]:
                continue

            effective_ping_time = s.ping_time
            if effective_ping_time is None and s.draft.timer_start:
                effective_ping_time = datetime.fromisoformat(s.draft.timer_start)
            if effective_ping_time is None:
                continue

            expected_team_idx = s.draft.current_team_idx
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            except (discord.NotFound, discord.HTTPException):
                continue

            if (s.draft.state == "active"
                    and s.draft.current_team_idx == expected_team_idx
                    and ref_msg.author.id in s.draft.current_team["user_ids"]):
                try:
                    if ref_msg.created_at < effective_ping_time:
                        await message.reply(
                            "❌ **Invalid challenge** — the GM typed that message before they were pinged to pick."
                        )
                    elif ref_msg.id in s.challenged_msg_ids:
                        await message.reply(
                            "❌ **Invalid challenge** — that message has already been challenged."
                        )
                    else:
                        s.challenged_msg_ids.add(ref_msg.id)
                        await _process_challenge(s, message.author.mention, message.author.display_name)
                except discord.HTTPException as e:
                    log.warning("Challenge reply failed: %s", e)
            break
        return

    # ── Pick detection: only in channels that have an active session ──────────
    if message.channel.id not in _sessions:
        return

    s = _sessions[message.channel.id]
    await _try_process_pick(s, message)

    if s.draft.mode == "roundless" and s.draft.state in ("active", "paused", "window_paused"):
        await _try_process_roundless_makeup(s, message)

    if (not message.content.startswith('!')
            and s.draft.state in ("active", "paused", "window_paused")
            and s.draft.current_team
            and _is_team_owner(message.author.id, s.draft.current_team)):
        content = message.content.strip()
        looks_like_pick = (
            bool(re.match(r'^\d', content))
            or bool(re.search(r'<:[^:]+:\d+>', content))
            or bool(_PRICE_RE.search(content))
        )
        if looks_like_pick and not _PICK_RE.match(content):
            await message.channel.send(
                f"❌ {message.author.mention} — wrong format. Use:\n"
                f"{_pick_format(s)}"
            )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot:
        return
    if after.channel.id not in _sessions:
        return
    if before.content == after.content:
        return
    await _try_process_pick(_sessions[after.channel.id], after, is_edit=True)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
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
    """Load the lotto from the lotto channel. Reply to a specific lotto message to use that one."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("idle", "setup", "lotto"):
        await ctx.send("❌ A draft is already active. Use `!timereset` first.")
        return

    ref = ctx.message.reference
    if ref:
        lotto_msg = await ctx.channel.fetch_message(ref.message_id)
    else:
        lotto_channel = bot.get_channel(LOTTO_CHANNEL_ID)
        if not lotto_channel:
            await ctx.send(f"❌ Could not find lotto channel (id: {LOTTO_CHANNEL_ID}).")
            return
        lotto_msg = None
        async for msg in lotto_channel.history(limit=50):
            if re.search(r'^\s*1\.', msg.content, re.MULTILINE):
                lotto_msg = msg
                break
        if not lotto_msg:
            await ctx.send(f"❌ No lotto message found in <#{LOTTO_CHANNEL_ID}>.")
            return

    teams = _parse_lotto_message(lotto_msg.content, ctx.guild)
    if not teams:
        await ctx.send(
            "❌ Could not parse that message as a lotto. Each line must look like:\n"
            "`1. <:emoji:id> - @User` or `1. emoji - @User1 @User2`"
        )
        return

    prev_timer_override = s.draft.timer_override  # preserve any !timersettimer set before lotto load
    s.draft            = DraftState()
    s.draft.teams      = teams
    s.draft.pick_order = build_snake_order(len(teams))
    s.draft.state      = "lotto"
    s.draft.timer_override = prev_timer_override
    s.draft.save(s.channel_id)

    log.info("LOTTO LOADED | ch=%d | %d teams | Slots: %s",
             s.channel_id, len(teams), [t["name"] for t in teams])

    lines = "\n".join(
        f"**{i+1}.** {_team_mentions(t)} ({t['name']})"
        for i, t in enumerate(s.draft.teams)
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
    """Re-read the lotto and update GM rosters without resetting picks."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No lotto loaded yet. Use `!timerloadlotto` first.")
        return

    lotto_msg = None
    ref = ctx.message.reference
    if ref:
        lotto_msg = await ctx.channel.fetch_message(ref.message_id)
    else:
        lotto_channel = bot.get_channel(LOTTO_CHANNEL_ID)
        if not lotto_channel:
            await ctx.send(f"❌ Could not find lotto channel (id: {LOTTO_CHANNEL_ID}).")
            return
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

    if len(updated_teams) != len(s.draft.teams):
        await ctx.send(
            f"❌ Team count mismatch — lotto has {len(updated_teams)} slots "
            f"but current draft has {len(s.draft.teams)}. Use `!timerloadlotto` to fully reload."
        )
        return

    changes = []
    for i, (old, new) in enumerate(zip(s.draft.teams, updated_teams)):
        if old["user_ids"] != new["user_ids"] or old["name"] != new["name"]:
            changes.append(f"Slot {i+1}: **{old['name']}** → **{new['name']}**")
            old["user_ids"] = new["user_ids"]
            old["name"]     = new["name"]

    s.draft.save(s.channel_id)
    if changes:
        await ctx.send("✅ **Lotto updated:**\n" + "\n".join(changes))
    else:
        await ctx.send("✅ Lotto re-read — no changes detected.")


@bot.command(name="timersetup")
@is_commissioner()
async def timersetup(ctx, *_):
    s = _get_session(ctx.channel.id)

    mentions = ctx.message.mentions
    if not mentions:
        await ctx.send("❌ Mention at least one user. Example: `!timersetup @Alice @Bob`")
        return

    s.draft       = DraftState()
    s.draft.teams = [
        {"user_ids": [m.id], "name": m.display_name, "picks": [], "skip_count": 0}
        for m in mentions
    ]
    s.draft.state = "setup"
    s.draft.save(s.channel_id)

    lines = "\n".join(f"{i+1}. {t['name']}" for i, t in enumerate(s.draft.teams))
    await ctx.send(
        f"✅ **{len(s.draft.teams)} participants registered:**\n{lines}\n\n"
        f"Run `!timerlotto` to randomly assign positions, or `!timerorder 3 1 2 …` to set manually."
    )


@bot.command(name="timergmlotto")
@is_commissioner()
async def timergmlotto(ctx, teams_per_gm: int = None, *_):
    """!timergmlotto <teams_per_gm> @GM1 @GM2 … — build a lotto from N GMs each getting <teams_per_gm> slots."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("idle", "setup", "lotto"):
        await ctx.send("❌ A draft is already active. Use `!timereset` first.")
        return

    if teams_per_gm is None or teams_per_gm < 1:
        await ctx.send("❌ Specify teams per GM first. Example: `!timergmlotto 5 @Alice @Bob @Carol`")
        return

    gms = ctx.message.mentions
    if not gms:
        await ctx.send("❌ Mention at least one GM. Example: `!timergmlotto 5 @Alice @Bob @Carol`")
        return

    import random

    def make_slot(gm):
        return {"user_ids": [gm.id], "name": gm.display_name, "picks": [], "skip_count": 0}

    # Shuffle once to set the order for round 1, then repeat that same order every round
    shuffled_gms = list(gms)
    random.shuffle(shuffled_gms)
    slots = [make_slot(gm) for _ in range(teams_per_gm) for gm in shuffled_gms]

    s.draft            = DraftState()
    s.draft.teams      = slots
    s.draft.pick_order = build_snake_order(len(slots))
    s.draft.state      = "lotto"
    s.draft.save(s.channel_id)

    lines = "\n".join(f"**{i+1}.** <@{t['user_ids'][0]}>" for i, t in enumerate(slots))
    embed = discord.Embed(
        title=f"✅ GM Lotto — {len(gms)} GMs × {teams_per_gm} teams = {len(slots)} slots",
        description=lines,
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Run !timerstart to begin the draft.")
    await ctx.send(embed=embed)


@bot.command(name="timerlotto")
async def timerlotto(ctx):
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("setup", "lotto"):
        await ctx.send("❌ Run `!timersetup` or `!timerloadlotto` first.")
        return

    import random
    indices          = list(range(s.draft.num_teams))
    random.shuffle(indices)
    s.draft.teams      = [s.draft.teams[i] for i in indices]
    s.draft.pick_order = build_snake_order(s.draft.num_teams)
    s.draft.state      = "lotto"
    s.draft.save(s.channel_id)

    lines = "\n".join(f"**{i+1}.** {_team_mentions(t)}" for i, t in enumerate(s.draft.teams))
    embed = discord.Embed(title="🎰 Lotto Results — Draft Order", description=lines, color=discord.Color.gold())
    embed.set_footer(text="Run !timerstart to begin.")
    await ctx.send(embed=embed)


@bot.command(name="timerorder")
@is_commissioner()
async def timerorder(ctx, *positions):
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("setup", "lotto"):
        await ctx.send("❌ Run `!timersetup` or `!timerloadlotto` first.")
        return

    try:
        idx = [int(p) - 1 for p in positions]
        if sorted(idx) != list(range(s.draft.num_teams)):
            raise ValueError
    except (ValueError, TypeError):
        await ctx.send(f"❌ Provide all {s.draft.num_teams} positions with no repeats.")
        return

    s.draft.teams      = [s.draft.teams[i] for i in idx]
    s.draft.pick_order = build_snake_order(s.draft.num_teams)
    s.draft.state      = "lotto"
    s.draft.save(s.channel_id)

    lines = "\n".join(f"**{i+1}.** {_team_mentions(t)}" for i, t in enumerate(s.draft.teams))
    embed = discord.Embed(title="📋 Draft Order Set", description=lines, color=discord.Color.blue())
    embed.set_footer(text="Run !timerstart to begin.")
    await ctx.send(embed=embed)


@bot.command(name="timermode")
@is_commissioner()
async def timermode(ctx, mode: str = ""):
    """!timermode roundless | !timermode snake"""
    s = _get_session(ctx.channel.id)

    mode = mode.lower()
    if mode not in ("roundless", "snake"):
        await ctx.send(
            "❌ Usage: `!timermode roundless` or `!timermode snake`\n"
            "**roundless** — dynamic pick order based on money spent\n"
            "**snake** — fixed round-based snake order (default)"
        )
        return

    if s.draft.state == "idle":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    s.draft.mode = mode
    s.draft.save(s.channel_id)

    if mode == "roundless":
        await ctx.send(
            f"✅ Switched to **roundless mode**. Pick order now computed dynamically:\n"
            f"1. Less money spent → picks sooner\n"
            f"2. Fewer picks made → picks sooner\n"
            f"3. More time since last pick → picks sooner\n\n"
            f"Picks must include price: `{s.draft.overall_pick}. :Emoji: Player Name $42 Year`"
        )
    else:
        await ctx.send(
            f"✅ Switched to **snake mode**. Fixed round-based order resumes from pick #{s.draft.overall_pick}."
        )


@bot.command(name="timerstart")
@is_commissioner()
async def timerstart(ctx, *label_parts):
    """!timerstart [roundless] [label] — begin the draft."""
    s = _get_session(ctx.channel.id)

    if s.draft.state != "lotto":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto` or `!timerlotto`.")
        return

    parts = list(label_parts)
    if parts and parts[0].lower() == "roundless":
        s.draft.mode = "roundless"
        parts = parts[1:]
    else:
        s.draft.mode = "snake"

    s.draft.state            = "active"
    s.draft.current_round    = 0
    s.draft.current_in_round = 0
    s.draft.draft_started    = datetime.now(timezone.utc).isoformat()
    s.draft.draft_label      = " ".join(parts) if parts else None
    s.draft.save(s.channel_id)

    mode_note  = "\n🔄 **Roundless mode** — pick order determined by money spent, picks made, and time since last pick." if s.draft.mode == "roundless" else ""
    label_note = f" (**{s.draft.draft_label}**)" if s.draft.draft_label else ""
    await ctx.send(f"🏀 **The draft has started!**{label_note}{mode_note}")
    await _start_timer(s)


@bot.command(name="timerpenalty")
@is_commissioner()
async def timerpenalty(ctx, pick_number: int):
    """!timerpenalty <pick_number> — manually apply LeBron/MJ penalty to the slot that made pick #N."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft loaded.")
        return

    n = s.draft.num_teams
    if n == 0:
        await ctx.send("❌ No teams loaded.")
        return

    if s.draft.mode == "roundless":
        await ctx.send("❌ Penalty is not applicable in roundless mode.")
        return

    overall = pick_number - 1
    if overall < 0 or overall >= n * ROUNDS:
        await ctx.send(f"❌ Pick number must be between 1 and {n * ROUNDS}.")
        return

    round_idx = overall // n
    in_round  = overall % n
    if round_idx >= len(s.draft.pick_order) or in_round >= len(s.draft.pick_order[round_idx]):
        await ctx.send("❌ Could not resolve that pick number to a slot.")
        return

    team_idx = s.draft.pick_order[round_idx][in_round]
    team     = s.draft.teams[team_idx]

    if team_idx in s.draft.penalty_teams:
        await ctx.send(f"ℹ️ **{team['name']}** (slot {team_idx + 1}) already has the penalty applied.")
        return

    s.draft.apply_penalty(team_idx)
    s.draft.save(s.channel_id)
    await ctx.send(
        f"⚠️ Penalty applied — **{team['name']}** (slot {team_idx + 1}, pick #{pick_number}) "
        f"will pick **last** every round from Round 6 onward."
    )


@bot.command(name="timerjumpto")
@is_commissioner()
async def timerjumpto(ctx, pick_number: int):
    """!timerjumpto <pick_number> — jump the draft to a specific overall pick number."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    if s.draft.mode == "roundless":
        if pick_number < 1:
            await ctx.send("❌ Pick number must be at least 1.")
            return
        new_round    = pick_number - 1
        new_in_round = 0
    else:
        total_picks = s.draft.num_teams * ROUNDS
        if pick_number < 1 or pick_number > total_picks:
            await ctx.send(f"❌ Pick number must be between 1 and {total_picks}.")
            return
        zero_pick    = pick_number - 1
        new_round    = zero_pick // s.draft.num_teams
        new_in_round = zero_pick % s.draft.num_teams

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()
    await _delete_active_ping(s)

    s.draft.current_round    = new_round
    s.draft.current_in_round = new_in_round
    s.draft.paused_remaining = None
    s.draft.timer_start      = None
    s.draft.state            = "active"
    s.draft.save(s.channel_id)

    team = s.draft.current_team
    log.info("JUMP | ch=%d | To pick %d | Round %d | In-round %d | Team: %s",
             s.channel_id, pick_number, s.draft.round_number, s.draft.pick_in_round,
             team["name"] if team else "?")
    loc_str = (f"pick #{pick_number}" if s.draft.mode == "roundless"
               else f"pick #{pick_number} (Round {s.draft.round_number}, pick {s.draft.pick_in_round})")
    await ctx.send(
        f"⏩ Jumped to **{loc_str}**.\n"
        f"Up now: {_team_mentions(team) if team else '?'}\nStarting timer…"
    )
    await _start_timer(s)


@bot.command(name="timersetpick")
@is_commissioner()
async def timersetpick(ctx, pick_number: int, member: discord.Member):
    """!timersetpick <pick_number> @GM — set pick number and force a specific GM to be next."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    team_idx = next(
        (i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]),
        None,
    )
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not registered as a GM in this draft.")
        return

    if s.draft.mode == "roundless":
        s.draft.current_round    = pick_number - 1
        s.draft.current_in_round = 0
    else:
        total_picks = s.draft.num_teams * ROUNDS
        if pick_number < 1 or pick_number > total_picks:
            await ctx.send(f"❌ Pick number must be between 1 and {total_picks}.")
            return
        zero_pick                = pick_number - 1
        s.draft.current_round    = zero_pick // s.draft.num_teams
        s.draft.current_in_round = zero_pick % s.draft.num_teams

    s.draft.next_team_override = team_idx
    s.draft.paused_remaining   = None
    s.draft.timer_start        = None
    s.draft.state              = "active"
    s.draft.save(s.channel_id)

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()
    await _delete_active_ping(s)

    team = s.draft.teams[team_idx]
    await ctx.send(
        f"✅ Pick set to **#{pick_number}** | Next up: **{team['name']}** ({_team_mentions(team)})\n"
        f"Starting timer…"
    )
    await _start_timer(s)


# ── During-draft commands ─────────────────────────────────────────────────────

@bot.command(name="timerproxy")
@is_commissioner()
async def timerproxy(ctx, member: discord.Member):
    """!timerproxy @user — temporarily add @user as a co-picker for the current team."""
    s = _get_session(ctx.channel.id)

    if s.draft.state != "active":
        await ctx.send("❌ No active draft.")
        return

    team = s.draft.current_team
    if not team:
        return

    if member.id in team["user_ids"]:
        await ctx.send(f"❌ {member.mention} is already a picker for **{team['name']}**.")
        return

    team["user_ids"].append(member.id)
    s.draft.save(s.channel_id)

    log.info("PROXY ADD | ch=%d | Team: %s | Proxy: %s (%d)",
             s.channel_id, team["name"], member.display_name, member.id)
    await ctx.send(
        f"✅ {member.mention} can now submit picks for **{team['name']}** while they're away.\n"
        f"Run `!timerremoveproxy {member.mention}` to remove them."
    )


@bot.command(name="timerremoveproxy")
@is_commissioner()
async def timerremoveproxy(ctx, member: discord.Member):
    """!timerremoveproxy @user — remove a proxy picker."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active"):
        await ctx.send("❌ No draft in progress.")
        return

    for team in s.draft.teams:
        if member.id in team["user_ids"]:
            team["user_ids"].remove(member.id)
            s.draft.save(s.channel_id)
            log.info("PROXY REMOVE | ch=%d | Team: %s | Removed: %s (%d)",
                     s.channel_id, team["name"], member.display_name, member.id)
            await ctx.send(f"✅ Removed {member.mention} as a proxy for **{team['name']}**.")
            return

    await ctx.send(f"❌ {member.mention} is not listed as a proxy on any team.")


@bot.command(name="challenge")
async def challenge_cmd(ctx):
    """Immediately cut the current GM's timer to 10 minutes (3 challenges = instant skip)."""
    s = _get_session(ctx.channel.id)

    if s.draft.state != "active":
        await ctx.send("❌ No active draft.")
        return
    if not s.draft.current_team:
        await ctx.send("❌ No current pick.")
        return
    if ctx.author.id in s.draft.current_team["user_ids"]:
        await ctx.send("❌ You can't challenge yourself.")
        return
    await _process_challenge(s, ctx.author.mention, ctx.author.display_name)


@bot.command(name="timerskip")
async def timerskip(ctx):
    s = _get_session(ctx.channel.id)

    if s.draft.state != "active":
        await ctx.send("❌ No active draft.")
        return

    team = s.draft.current_team
    is_privileged = (
        ctx.author.guild_permissions.administrator
        or any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles)
    )

    if not _is_team_owner(ctx.author.id, team) and not is_privileged:
        await ctx.send(f"❌ Only {_team_mentions(team)} or a commissioner can skip this pick.")
        return

    penalty_note = "" if s.draft.timer_override is not None else " **-10 min** from their future picks."
    await ctx.send(f"⏩ {_team_mentions(team)} is skipping.{penalty_note}")
    await _do_skip(s, auto=False)


@bot.command(name="timerunskip")
@is_commissioner()
async def timerunskip(ctx):
    """!timerunskip — undo the most recent skip."""
    s = _get_session(ctx.channel.id)

    if not s.draft.last_skip:
        await ctx.send("❌ No skip to undo.")
        return

    if s.draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No active draft.")
        return

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()

    undo = s.draft.last_skip
    s.draft.current_round    = undo["round"]
    s.draft.current_in_round = undo["in_round"]
    s.draft.state            = "active"
    s.draft.timer_start      = None
    s.draft.paused_remaining = None

    s.draft.teams[undo["team_idx"]]["skip_count"] = undo["prev_skip_count"]
    prev_lpn = undo.get("prev_last_pick_number")
    if prev_lpn is not None:
        s.draft.teams[undo["team_idx"]]["last_pick_number"] = prev_lpn

    s.draft.last_skip = None
    s.draft.save(s.channel_id)

    team = s.draft.current_team
    log.info("UNDO SKIP | ch=%d | Pick #%d | Team: %s | Skip count restored to %d",
             s.channel_id, s.draft.overall_pick, team["name"] if team else "?",
             undo["prev_skip_count"])

    await ctx.send(
        f"↩️ **Skip undone.** Restored to pick **#{s.draft.overall_pick}** — "
        f"{_team_mentions(team)} is back on the clock."
    )
    await _start_timer(s)


@bot.command(name="timerstatus")
async def timerstatus(ctx):
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No active draft.")
        return

    if s.draft.state == "complete":
        await ctx.send("🏆 Draft is complete!")
        return

    team     = s.draft.current_team
    duration = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)

    if s.draft.state == "paused":
        remaining = s.draft.paused_remaining or 0
        time_left = f"⏸️ PAUSED — {int(remaining // 60)}m {int(remaining % 60)}s remaining"
    elif s.draft.state == "window_paused":
        remaining = s.draft.paused_remaining or 0
        time_left = f"🌙 WINDOW PAUSED — {int(remaining // 60)}m {int(remaining % 60)}s remaining (resumes 10am ET)"
    elif s.draft.timer_start:
        elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(s.draft.timer_start)).total_seconds()
        remaining = max(0, duration - elapsed)
        time_left = f"{int(remaining // 60)}m {int(remaining % 60)}s"
    else:
        time_left = "unknown"

    color = (discord.Color.dark_gray() if s.draft.state == "window_paused"
             else discord.Color.orange() if s.draft.state == "paused"
             else discord.Color.blue())

    if s.draft.mode == "roundless":
        embed = discord.Embed(title="Draft Status — Roundless", color=color)
        embed.add_field(name="Overall Pick", value=str(s.draft.overall_pick), inline=True)
        embed.add_field(name="Up Now",       value=_team_mentions(team),       inline=True)
        embed.add_field(name="Time Left",    value=time_left,                  inline=True)
        embed.add_field(name="Base Timer",   value=f"{duration // 60} min",    inline=True)

        order       = s.draft._roundless_sorted_order()
        current_idx = s.draft.current_team_idx
        queue_lines = []
        for pos, idx in enumerate(order[:8], 1):
            t     = s.draft.teams[idx]
            money = t.get("money_spent", 0)
            picks = len(t.get("picks", []))
            arrow = " ← **ON CLOCK**" if idx == current_idx else ""
            queue_lines.append(f"**{pos}.** {t['name']} — ${money}, {picks} pick(s){arrow}")
        embed.add_field(name="Pick Queue", value="\n".join(queue_lines) or "—", inline=False)

        skippers = [(t["name"], t.get("skip_count", 0)) for t in s.draft.teams if t.get("skip_count", 0) > 0]
        if skippers:
            embed.add_field(
                name="Skip Penalties",
                value="\n".join(f"{n}: {c} skip(s) (−{c*10} min)" for n, c in skippers),
                inline=False,
            )
    else:
        embed = discord.Embed(title=f"Draft Status - Round {s.draft.round_number} of {ROUNDS}", color=color)
        embed.add_field(name="Overall Pick",  value=str(s.draft.overall_pick),  inline=True)
        embed.add_field(name="Pick in Round", value=str(s.draft.pick_in_round), inline=True)
        embed.add_field(name="Up Now",        value=_team_mentions(team),        inline=True)
        embed.add_field(name="Time Left",     value=time_left,                   inline=True)
        embed.add_field(name="Base Timer",    value=f"{duration // 60} min",     inline=True)

        if s.draft.penalty_teams:
            penalised = ", ".join(_team_mentions(s.draft.teams[i]) for i in s.draft.penalty_teams)
            embed.add_field(name="Pick Last (R6-10)", value=penalised, inline=False)

        skippers = [(t["name"], t.get("skip_count", 0)) for t in s.draft.teams if t.get("skip_count", 0) > 0]
        if skippers:
            embed.add_field(
                name="Skip Penalties",
                value="\n".join(f"{n}: {c} skip(s) (−{c*10} min)" for n, c in skippers),
                inline=False,
            )

    await ctx.send(embed=embed)


@bot.command(name="timerskiplist")
async def timerskiplist(ctx):
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft loaded.")
        return

    from config import ROUND_TIMERS, SKIP_PENALTY

    embed    = discord.Embed(title="Skip Penalties", color=discord.Color.orange())
    any_skips = False

    for i, team in enumerate(s.draft.teams):
        skips = team.get("skip_count", 0)
        if skips == 0:
            continue
        any_skips = True
        is_as     = s.draft.is_active_skip(i)
        deduction = skips * SKIP_PENALTY
        as_tag    = " 🔴 **ACTIVE SKIP**" if is_as else ""

        if s.draft.mode == "roundless":
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
    """!timerskiphistory | !timerskiphistory @user"""
    history = _load_skip_history()

    if not history:
        await ctx.send("📭 No skip history recorded yet.")
        return

    if member is None:
        totals: dict[int, dict] = {}
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
            name       = member_obj.display_name if member_obj else data["name"]
            atd_count  = len(data["atds"])
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
        uid     = member.id
        entries = [e for e in history if uid in e["user_ids"]]

        if not entries:
            await ctx.send(f"✅ {member.mention} has no skips on record.")
            return

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
                ts        = datetime.fromisoformat(e["timestamp"])
                date_str  = ts.strftime("%b %d, %Y")
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
        picks     = team.get("picks", [])
        pick_text = "\n".join(f"{j+1}. {p}" for j, p in enumerate(picks)) if picks else "_No picks yet_"
        embed.add_field(name=team["name"], value=pick_text, inline=True)
    return embed


class BoardView(discord.ui.View):
    def __init__(self, chunks):
        super().__init__(timeout=300)
        self.chunks = chunks
        self.page   = 0
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
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft in progress.")
        return

    COMPLETE_PICKS = 10

    if s.draft.mode == "roundless":
        order       = s.draft._roundless_sorted_order()
        current_idx = s.draft.current_team_idx
        lines = []
        pos   = 1
        for idx in order:
            t     = s.draft.teams[idx]
            picks = len(t.get("picks", []))
            if picks >= COMPLETE_PICKS:
                continue
            money = t.get("money_spent", 0)
            last  = t.get("last_pick_number", 0)
            skips = t.get("skip_count", 0)
            skip_str = f" | {skips}x skip" if skips else ""
            arrow    = " ← **ON CLOCK**" if idx == current_idx else ""
            lines.append(f"**{pos}.** {t['name']} — ${money} | {picks} picks | last #{last}{skip_str}{arrow}")
            pos += 1
        desc = "\n".join(lines) if lines else "_All teams complete!_"
        if len(desc) > 4000:
            desc = desc[:3997] + "…"
        embed = discord.Embed(
            title=f"Roundless Draft — Pick Order  (Pick #{s.draft.overall_pick})",
            description=desc,
            color=discord.Color.dark_blue(),
        )
        await ctx.send(embed=embed)
    else:
        teams  = [t for t in s.draft.teams if len(t.get("picks", [])) < COMPLETE_PICKS]
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
    s = _get_session(ctx.channel.id)

    if s.draft.state == "complete":
        await ctx.send("❌ Draft is already complete. Use `!timereset` first.")
        return

    if minutes == 0:
        s.draft.timer_override = None
        s.draft.save(s.channel_id)
        await ctx.send("✅ Timer override cleared — back to default round timers.")
        return

    s.draft.timer_override = minutes * 60
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ Timer set to **{minutes} minutes** for all future picks.")

    if s.draft.state == "active":
        await _delete_active_ping(s)
        await _start_timer(s)


# ── Roundless sync commands ───────────────────────────────────────────────────

@bot.command(name="timerset")
@is_commissioner()
async def timerset(ctx, member: discord.Member, money: int, picks: int, last_pick: int):
    """!timerset @GM <money> <picks> <last_pick#> — set all three roundless stats at once."""
    s        = _get_session(ctx.channel.id)
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team    = s.draft.teams[team_idx]
    team["money_spent"] = money
    current = len(team.get("picks", []))
    if picks > current:
        team.setdefault("picks", []).extend(["[manual]"] * (picks - current))
    elif picks < current:
        team["picks"] = team["picks"][:picks]
    team["last_pick_number"] = last_pick
    team["pending_makeup"]   = False
    s.draft.save(s.channel_id)
    await ctx.send(
        f"✅ **{team['name']}** — money: **${money}** | picks: **{picks}** | last pick: **#{last_pick}** | pending cleared."
    )


@bot.command(name="timersetmoney")
@is_commissioner()
async def timersetmoney(ctx, member: discord.Member, amount: int):
    """!timersetmoney @GM <dollars>"""
    s        = _get_session(ctx.channel.id)
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    s.draft.teams[team_idx]["money_spent"] = amount
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ **{s.draft.teams[team_idx]['name']}** money spent set to **${amount}**.")


@bot.command(name="timersetpicks")
@is_commissioner()
async def timersetpicks(ctx, member: discord.Member, count: int):
    """!timersetpicks @GM <count>"""
    s        = _get_session(ctx.channel.id)
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team    = s.draft.teams[team_idx]
    current = len(team.get("picks", []))
    if count > current:
        team.setdefault("picks", []).extend(["[manual]"] * (count - current))
    elif count < current:
        team["picks"] = team["picks"][:count]
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ **{team['name']}** picks made set to **{count}**.")


@bot.command(name="timersetlastpick")
@is_commissioner()
async def timersetlastpick(ctx, member: discord.Member, pick_number: int):
    """!timersetlastpick @GM <pick#>"""
    s        = _get_session(ctx.channel.id)
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    s.draft.teams[team_idx]["last_pick_number"] = pick_number
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ **{s.draft.teams[team_idx]['name']}** last pick number set to **#{pick_number}**.")


@bot.command(name="timeraddskip")
@is_commissioner()
async def timeraddskip(ctx, member: discord.Member, count: int = 1):
    """!timeraddskip @GM [count]"""
    s = _get_session(ctx.channel.id)

    # Prefer the currently active slot for this member; fall back to first slot they own
    cur = s.draft.current_team_idx
    if cur is not None and member.id in s.draft.teams[cur]["user_ids"]:
        team_idx = cur
    else:
        team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)

    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    team = s.draft.teams[team_idx]
    team["skip_count"] = team.get("skip_count", 0) + count
    s.draft.save(s.channel_id)
    new_total = team["skip_count"]
    penalty   = new_total * 600 // 60
    await ctx.send(
        f"✅ **{team['name']}** (slot {team_idx + 1}) skip count set to **{new_total}** "
        f"(-{penalty} min off future timers)."
    )


# ── Admin ─────────────────────────────────────────────────────────────────────

@bot.command(name="timerpause")
@is_commissioner()
async def timerpause(ctx):
    s = _get_session(ctx.channel.id)

    if s.draft.state == "paused":
        await ctx.send("❌ Draft is already paused. Use `!timerresume` to continue.")
        return
    if s.draft.state == "window_paused":
        await ctx.send("❌ Draft is already paused (draft window is closed). Timer resumes automatically at 10am ET.")
        return
    if s.draft.state != "active":
        await ctx.send("❌ No active draft to pause.")
        return

    team     = s.draft.current_team
    duration = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)

    if s.draft.timer_start:
        elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(s.draft.timer_start)).total_seconds()
        remaining = max(0, int(duration - elapsed))
    else:
        remaining = duration

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()

    s.draft.paused_remaining = remaining
    s.draft.timer_start      = None
    s.draft.state            = "paused"
    s.draft.save(s.channel_id)

    mins = remaining // 60
    secs = remaining % 60
    log.info("PAUSE | ch=%d | Team: %s | Remaining: %dm %ds",
             s.channel_id, team["name"], mins, secs)
    await ctx.send(
        f"⏸️ **Draft paused.** {_team_mentions(team)} has **{mins}m {secs}s** remaining.\n"
        f"Use `!timerresume` to continue."
    )


@bot.command(name="timerresume")
@is_commissioner()
async def timerresume(ctx):
    s = _get_session(ctx.channel.id)

    if s.draft.state != "paused":
        await ctx.send("❌ Draft is not paused.")
        return

    team      = s.draft.current_team
    remaining = (s.draft.paused_remaining
                 or s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx))

    s.draft.state            = "active"
    s.draft.timer_start      = datetime.now(timezone.utc).isoformat()
    s.draft.paused_remaining = None
    s.draft.save(s.channel_id)

    mins = remaining // 60
    secs = remaining % 60
    log.info("RESUME | ch=%d | Team: %s | Remaining: %dm %ds",
             s.channel_id, team["name"], mins, secs)
    await ctx.send(f"▶️ **Draft resumed.** {_team_mentions(team)} has **{mins}m {secs}s** to pick.")
    s.timer_task = asyncio.create_task(_timer_loop(s, remaining, team["user_ids"]))


@bot.command(name="removeskip")
@is_commissioner()
async def removeskip(ctx, member: discord.Member, count: int = 1):
    """!removeskip @GM [count] — remove one or more skips from a GM."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No active draft.")
        return

    if count < 1:
        await ctx.send("❌ Count must be at least 1.")
        return

    # Find all slots for this member that have skips — pick the one with the most
    slots_with_skips = [
        (i, t) for i, t in enumerate(s.draft.teams)
        if member.id in t["user_ids"] and t.get("skip_count", 0) > 0
    ]
    if not slots_with_skips:
        in_draft = any(member.id in t["user_ids"] for t in s.draft.teams)
        if not in_draft:
            await ctx.send(f"❌ {member.display_name} is not in the draft.")
        else:
            await ctx.send(f"❌ **{member.display_name}** has no skips to remove.")
        return

    team_idx, team = max(slots_with_skips, key=lambda x: x[1].get("skip_count", 0))
    removed = min(count, team["skip_count"])
    team["skip_count"] = team["skip_count"] - removed
    s.draft.save(s.channel_id)
    new_skips = team["skip_count"]
    await ctx.send(
        f"✅ Removed **{removed}** skip(s) from **{team['name']}** (slot {team_idx + 1}). "
        f"They now have **{new_skips}** skip(s)" +
        (f" (−{new_skips * 10} min)." if s.draft.timer_override is None else ".")
    )


@bot.command(name="timereset")
@is_commissioner()
async def timereset(ctx):
    """Cancel and wipe the draft for this channel."""
    s = _get_session(ctx.channel.id)

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()
    await _delete_active_ping(s)

    s.draft = DraftState()
    s.draft.save(s.channel_id)

    log.info("RESET | ch=%d", s.channel_id)
    await ctx.send("🗑️ Draft has been reset.")


@bot.command(name="timerhelp")
async def timerhelp(ctx):
    s = _get_session(ctx.channel.id)

    embed = discord.Embed(
        title="ATD Timer Bot — Command Reference",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Setup (commissioner only)",
        value=(
            "`!timerloadlotto` — Load lotto from lotto channel (reply to a message to use that one)\n"
            "`!timergmlotto <n> @GM1 @GM2 …` — Auto-build lotto: each GM gets <n> slots, randomly shuffled\n"
            "`!timerlottoupdate` — Re-read lotto to update GM rosters (preserves picks)\n"
            "`!timersetup @u1 @u2 …` — Manually register participants\n"
            "`!timerlotto` — Randomly shuffle draft order\n"
            "`!timerorder 3 1 2 …` — Set draft order manually\n"
            "`!timermode roundless|snake` — Switch draft mode\n"
            "`!timerstart [roundless] [label]` — Begin the draft\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="During draft",
        value=(
            "`!timerstatus` — Show current pick and time remaining\n"
            "`!timerboard` — Show all picks so far\n"
            "`!timerskip` — Skip your turn (−10 min on future picks)\n"
            "`!timerunskip` — Undo the last skip\n"
            "`!timerskiplist` — Show all teams' skip penalties\n"
            "`!timerskiphistory [@user]` — All-time skip leaderboard or per-GM history\n"
            "`challenge` (reply in atd-chat) — Cut current GM's timer to 10 min (3 = instant skip)\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin",
        value=(
            "`!timerpause` / `!timerresume` — Pause/resume the draft\n"
            "`!timerjumpto <pick#>` — Jump to a specific pick number\n"
            "`!timersetpick <pick#> @GM` — Jump and force a specific GM next\n"
            "`!timersettimer <min>` — Override timer (0 = revert to defaults)\n"
            "`!timerproxy @user` / `!timerremoveproxy @user` — Add/remove a proxy picker\n"
            "`!timeraddskip @GM [n]` / `!removeskip @GM` — Add/remove skips\n"
            "`!timerset @GM <money> <picks> <last#>` — Set all roundless stats at once\n"
            "`!timersetmoney` / `!timersetpicks` / `!timersetlastpick` — Set individual stats\n"
            "`!timereset` — Cancel and wipe this channel's draft\n"
        ),
        inline=False,
    )
    if s.draft.mode == "roundless" and s.draft.state != "idle":
        embed.add_field(
            name="Pick format (roundless)",
            value=f"`{s.draft.overall_pick}. :YourEmoji: Player Name $Price Year`",
            inline=False,
        )
    elif s.draft.state != "idle":
        embed.add_field(
            name="Pick format",
            value=f"`{s.draft.overall_pick}. :YourEmoji: Player Name Year`",
            inline=False,
        )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not set in .env")
    else:
        print("🚀 Starting ATD Timer Bot (multi-channel)…")
        bot.run(DISCORD_TOKEN)
