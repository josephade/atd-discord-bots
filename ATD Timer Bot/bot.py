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
  !timerskip             Skip your turn (-5 min on future picks)
  !timerstatus           Current pick, round, and time remaining
  !timerboard            Show all picks so far
  !timerdm on|off|status Opt in/out of DMs when it's your turn (DM or server, global)
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
from rapidfuzz import fuzz, process

from config import (ATD_CHAT_CHANNEL_ID, DISCORD_TOKEN,
                    DRAFT_CHANNEL_ID, DRAFT_LIST_BOT_ID, DRAFT_RECAP_CHANNEL_ID,
                    LOTTO_CHANNEL_ID, PENALTY_PLAYERS, ROUNDS,
                    SBL_STEALS_PER_TEAM, SBL_BLOCKS_PER_TEAM, SBL_LOCKS_PER_TEAM,
                    TRUSTED_BOT_IDS)
from draft import DraftState, HISTORY_FILE, build_snake_order, reroll_from_round, state_file, _state_dir
from adp import ADP_MAP

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


# Channels that aren't a draft themselves, but are allowed to remotely view or
# operate on whichever draft is currently in progress elsewhere (e.g. a
# general/admin channel).
_REMOTE_VIEW_CHANNELS = {934052158532378634, 1471629828208988314}

_ACTIVE_DRAFT_STATES = ("active", "paused", "window_paused")

# Broader than _ACTIVE_DRAFT_STATES: covers a draft from lotto-loaded through
# complete, i.e. anything past blank/idle. Used to resolve during-draft admin
# commands (skip, addpick, setmoney, ...) remotely — deliberately excludes
# setup commands (!timerloadlotto, !timerstart, !timereset, ...), which bind
# a session to whatever channel they're run in and must stay channel-explicit.
_IN_PROGRESS_STATES = ("lotto", "active", "paused", "window_paused", "complete")


async def _resolve_viewable_session(ctx) -> DraftSession | None:
    """Session to use for a read-only status command (!timerboard,
    !timersblstatus). In a normal draft channel, that's just this channel.
    In a remote-view channel, auto-detect the single currently-active draft
    across all tracked channels. Sends an explanatory message and returns
    None if there isn't exactly one to show."""
    if ctx.channel.id not in _REMOTE_VIEW_CHANNELS:
        return _get_session(ctx.channel.id)

    active = [s for s in _sessions.values() if s.draft.state in _ACTIVE_DRAFT_STATES]
    if not active:
        await ctx.send("❌ No active draft found in any tracked channel.")
        return None
    if len(active) > 1:
        names = ", ".join(f"<#{s.channel_id}>" for s in active)
        await ctx.send(
            f"❌ Multiple drafts are active right now ({names}) — run this command "
            f"in the specific draft's channel instead."
        )
        return None
    return active[0]


async def _resolve_command_session(ctx) -> DraftSession | None:
    """Session to use for a during-draft admin command (!timerskip,
    !timeraddpick, !timerpause, ...). In a normal draft channel, that's just
    this channel. In a remote channel, auto-detect the single draft currently
    in progress (lotto-loaded through complete) across all tracked channels.
    Sends an explanatory message and returns None if there isn't exactly one
    to act on."""
    if ctx.channel.id not in _REMOTE_VIEW_CHANNELS:
        return _get_session(ctx.channel.id)

    candidates = [s for s in _sessions.values() if s.draft.state in _IN_PROGRESS_STATES]
    if not candidates:
        await ctx.send("❌ No draft in progress in any tracked channel.")
        return None
    if len(candidates) > 1:
        names = ", ".join(f"<#{s.channel_id}>" for s in candidates)
        await ctx.send(
            f"❌ Multiple drafts are in progress right now ({names}) — run this command "
            f"in the specific draft's channel instead."
        )
        return None
    return candidates[0]


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


# ── Challenge history (shared across all drafts) ──────────────────────────────

CHALLENGE_HISTORY_FILE = os.path.join(_state_dir, "challenge_history.json")


def _load_challenge_history() -> list[dict]:
    try:
        with open(CHALLENGE_HISTORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_challenge_history(entry: dict):
    history = _load_challenge_history()
    history.append(entry)
    with open(CHALLENGE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ── Pick-time history (shared across all drafts, all-time) ─────────────────────

PICK_TIME_HISTORY_FILE = os.path.join(_state_dir, "pick_time_history.json")


def _load_pick_time_history() -> list[dict]:
    try:
        with open(PICK_TIME_HISTORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_pick_time_history(entry: dict):
    history = _load_pick_time_history()
    history.append(entry)
    with open(PICK_TIME_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ── Draft recap ───────────────────────────────────────────────────────────────

def _build_draft_recap(s: DraftSession) -> discord.Embed:
    """Summarize a just-finished draft: best value pick, biggest reach (both
    vs. ADP_MAP), and most skips/challenges for this specific draft."""
    best_value    = None  # (delta, player_name, team_name, pick_num, adp)
    biggest_reach = None

    for team in s.draft.teams:
        picks        = team.get("picks", [])
        pick_numbers = team.get("pick_numbers", [])
        for pick_raw, pick_num in zip(picks, pick_numbers):
            player_name = _extract_player_name(pick_raw)
            adp = ADP_MAP.get(player_name.lower())
            if adp is None:
                continue
            delta = pick_num - adp  # positive = picked later than ADP (value); negative = reach
            if delta > 0 and (best_value is None or delta > best_value[0]):
                best_value = (delta, player_name, team["name"], pick_num, adp)
            if delta < 0 and (biggest_reach is None or delta < biggest_reach[0]):
                biggest_reach = (delta, player_name, team["name"], pick_num, adp)

    skip_history      = _load_skip_history()
    challenge_history = _load_challenge_history()
    this_draft_skips = [
        h for h in skip_history
        if h.get("channel_id") == s.channel_id and h.get("draft_started") == s.draft.draft_started
    ]
    this_draft_challenges = [
        h for h in challenge_history
        if h.get("channel_id") == s.channel_id and h.get("draft_started") == s.draft.draft_started
    ]

    skip_counts = {}
    for h in this_draft_skips:
        skip_counts[h["team_name"]] = skip_counts.get(h["team_name"], 0) + 1
    challenge_counts = {}
    for h in this_draft_challenges:
        challenge_counts[h["team_name"]] = challenge_counts.get(h["team_name"], 0) + 1

    most_skips      = max(skip_counts.items(), key=lambda x: x[1]) if skip_counts else None
    most_challenged = max(challenge_counts.items(), key=lambda x: x[1]) if challenge_counts else None

    label = s.draft.draft_label or f"Draft in <#{s.channel_id}>"
    embed = discord.Embed(title=f"📋 Draft Recap — {label}", color=discord.Color.gold())

    if best_value:
        delta, name, team_name, pick_num, adp = best_value
        embed.add_field(
            name="💎 Best Value Pick",
            value=f"**{name}** — picked #{pick_num} by **{team_name}**\n(ADP {adp:.1f} — a {delta:.1f}-spot steal)",
            inline=False,
        )
    if biggest_reach:
        delta, name, team_name, pick_num, adp = biggest_reach
        embed.add_field(
            name="📈 Biggest Reach",
            value=f"**{name}** — picked #{pick_num} by **{team_name}**\n(ADP {adp:.1f} — a {-delta:.1f}-spot reach)",
            inline=False,
        )
    if most_skips:
        team_name, count = most_skips
        embed.add_field(name="⏩ Most Skips", value=f"**{team_name}** — {count} skip{'s' if count != 1 else ''}", inline=True)
    if most_challenged:
        team_name, count = most_challenged
        embed.add_field(name="⚡ Most Challenged", value=f"**{team_name}** — challenged {count} time{'s' if count != 1 else ''}", inline=True)

    total_picks = sum(len(t.get("picks", [])) for t in s.draft.teams)
    embed.set_footer(text=f"{s.draft.num_teams} teams  ·  {total_picks} total picks")
    return embed


async def _post_draft_recap(s: DraftSession):
    if not DRAFT_RECAP_CHANNEL_ID:
        return
    channel = bot.get_channel(DRAFT_RECAP_CHANNEL_ID)
    if not channel:
        log.warning("DRAFT_RECAP_CHANNEL_ID %d not found in cache — recap not posted", DRAFT_RECAP_CHANNEL_ID)
        return
    try:
        await channel.send(embed=_build_draft_recap(s))
    except Exception as exc:
        log.error("Failed to build/post draft recap for ch=%d: %s", s.channel_id, exc, exc_info=True)


# ── Pick-turn DM preferences (global per-user, shared across all drafts) ─────

DM_PREFS_FILE = os.path.join(_state_dir, "dm_prefs.json")


def _load_dm_prefs() -> dict:
    try:
        with open(DM_PREFS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_dm_prefs(prefs: dict):
    with open(DM_PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def _dm_enabled(user_id: int) -> bool:
    return _load_dm_prefs().get(str(user_id), False)


async def _dm_team(s: DraftSession, team: dict, deadline_ts: int, header: str):
    """DM every opted-in GM on `team` that it's their turn to pick."""
    prefs = _load_dm_prefs()
    opted_in = [uid for uid in team["user_ids"] if prefs.get(str(uid), False)]
    if not opted_in:
        return

    channel = s.channel
    link = (f"https://discord.com/channels/{channel.guild.id}/{channel.id}"
            if channel and channel.guild else None)
    location = f"[{channel.name}]({link})" if link else "your draft channel"

    embed = discord.Embed(
        title=header,
        description=(
            f"**{team['name']}** — it's your turn to pick!\n\n"
            f"⏱️ Pick deadline: <t:{deadline_ts}:R>\n\n"
            f"Type your pick in {location}:\n"
            f"{_pick_format(s)}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Turn this off anytime with !timerdm off")

    for uid in opted_in:
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(embed=embed)
        except discord.Forbidden:
            log.info("DM blocked | user=%d has DMs disabled", uid)
        except Exception as exc:
            log.warning("DM failed | user=%d | %s", uid, exc)


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

# Captures (rather than discards) the team emoji from a pick/steal message.
# Team Sheet Bot / ADP Bot resolve fantasy team identity from this emoji via
# their own emoji_map.py — they have no concept of a GM's Discord name, which
# is all Timer Bot tracks internally. SBL confirmations must forward the raw
# emoji, not team["name"], so those bots can resolve the real sheet team.
_EMOJI_CAPTURE_RE = re.compile(
    r'(<a?:[^:]+:\d+>|:[^:\s]+:|[\U0001F000-\U0001FFFF\U00002600-\U000027BF⌀-⛿✀-➿︀-️]+)'
)


def _extract_team_emoji(content: str) -> str | None:
    m = _EMOJI_CAPTURE_RE.search(content)
    return m.group(1) if m else None

# Matches a single lotto line:
#   1. <:emoji:id> - <@userid> <@userid2>
#   2. 🦢 - <@userid>
_LOTTO_LINE_RE = re.compile(
    r'^\s*(\d+)\.'          # position number
    r'.*?-\s*'              # anything up to the first dash
    r'(.*)',                 # rest of line (mentions extracted separately)
)

# Matches prices in roundless pick messages: $42, ($42), (42), 42$
_PRICE_RE = re.compile(
    r'\(?(-?\$\d+(?:\.\d+)?)\)?'
    r'|\((\d+(?:\.\d+)?)\)'
    r'|\b(\d+(?:\.\d+)?)\$'
)

# Matches a trailing lock marker on a pick message (SBL mode): 🔒, a custom
# :lock: emoji, or the word "lock"/"locked" at the very end.
_LOCK_MARKER_RE = re.compile(r'\s*(?:🔒|<a?:lock:\d+>|\block(?:ed)?\b)\s*$', re.IGNORECASE)

# Free-form SBL steal/block intent, anywhere in a message.
_STEAL_INTENT_RE = re.compile(r'\b(steal|steals|stole|stolen|stealing)\b', re.IGNORECASE)
_BLOCK_INTENT_RE = re.compile(r'\b(block|blocks|blocked|blocking)\b', re.IGNORECASE)


def _has_sbl_intent(content: str) -> bool:
    """True if content unambiguously carries steal XOR block intent (works
    whether or not a numbered pick prefix like '2. ' is present)."""
    content = content.strip()
    if not content or content.startswith('!'):
        return False
    is_steal = bool(_STEAL_INTENT_RE.search(content))
    is_block = bool(_BLOCK_INTENT_RE.search(content))
    return is_steal != is_block


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_price(raw: str) -> int | None:
    m = _PRICE_RE.search(raw)
    if not m:
        return None
    digits = (m.group(1) or m.group(2) or m.group(3) or "0")
    try:
        return int(float(digits.replace("$", "")))
    except ValueError:
        return None


def _extract_player_name(raw: str) -> str:
    text = re.sub(r'<:[^:]+:\d+>', '', raw).strip()   # <:emoji:id>
    text = re.sub(r'^:[^:\s]+:\s*', '', text).strip()  # :emoji:
    text = re.sub(r'^[^a-zA-Z0-9]+', '', text).strip() # leading unicode emoji / symbols
    text = re.sub(r'^selects?\s+', '', text, flags=re.IGNORECASE).strip()  # "select"/"selects" keyword
    # A lock marker can land mid-string (e.g. "James Worthy ($11) 🔒 86-87" —
    # price, then lock, then year) — _LOCK_MARKER_RE upstream only strips one
    # anchored at the very end, so a marker anywhere earlier survives into the
    # stored name otherwise.
    text = re.sub(r'\s*(?:🔒|<a?:lock:\d+>)\s*', ' ', text, flags=re.IGNORECASE).strip()
    text = _PRICE_RE.sub('', text).strip()             # price, wherever it appears
    # An empty price placeholder (e.g. "( )" or "()" with no digits inside)
    # isn't matched by _PRICE_RE, which requires at least one digit — left
    # unstripped, it sits after a trailing year and blocks the year-stripping
    # regexes below (which require the year to be at the very end of the
    # string), corrupting the stored name with leftover junk.
    text = re.sub(r'\(\s*\)', '', text).strip()
    # Strip a leading year that appears before the player name (e.g. "13 LeBron James" → "LeBron James")
    # Apostrophe class covers straight (') and the curly quotes (’ ‘) that
    # mobile keyboards/Discord auto-substitute — a mismatch here (e.g. "26’"
    # left unstripped) breaks duplicate-pick / steal-target name matching.
    text = re.sub(r"^['’‘]?\d{2,4}(-\d{2,4})?\s+", '', text).strip()
    # Strip trailing year/season suffixes
    text = re.sub(r"\s+['’‘]?\d{2}['’‘]-?\d{0,2}$", '', text).strip()
    text = re.sub(r'\s+\d{4}(-\d{2,4})?$', '', text).strip()
    # Two-digit season range with no apostrophe (e.g. "85-86") — not covered
    # by the apostrophe'd variant above or the 4-digit variant below.
    text = re.sub(r'\s+\d{2}-\d{2}$', '', text).strip()
    text = re.sub(r"\s+['’‘]?\d{2}['’‘]?$", '', text).strip()
    text = re.sub(r'\s+', ' ', text).strip()  # collapse doubled spaces left by mid-string removals above
    return text


def _pick_name_key(raw: str) -> str:
    return _extract_player_name(raw).lower()


def _team_mentions(team: dict) -> str:
    return " ".join(f"<@{uid}>" for uid in team["user_ids"])


def _is_team_owner(user_id: int, team: dict) -> bool:
    return user_id in team["user_ids"]


def _pick_title(s: DraftSession) -> str:
    if s.draft.order_mode == "roundless":
        return f"Pick {s.draft.overall_pick}"
    return f"Round {s.draft.round_number} of {ROUNDS}  -  Pick {s.draft.overall_pick}"


def _pick_format(s: DraftSession) -> str:
    if s.draft.order_mode == "roundless":
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
        # The lotto line already carries the team's emoji (between the pick
        # number and the dash) — capture it here instead of discarding it,
        # so commands like !timernext can show a real team logo without
        # waiting on an SBL pick message to supply one.
        teams_by_pos[pos] = {
            "user_ids":  user_ids,
            "name":      " / ".join(names),
            "picks":     [],
            "skip_count": 0,
            "emoji":     _extract_team_emoji(line),
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
    embed.set_footer(text="Use !timerskip to pass (costs 5 min on future picks).")
    s.ping_time          = datetime.now(timezone.utc)
    s.challenge_count    = 0
    s.challenged_msg_ids = set()
    s.active_ping = await s.channel.send(content=_team_mentions(team), embed=embed)
    await _dm_team(s, team, deadline_ts, _pick_title(s))


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
        embed.set_footer(text="Use !timerskip to pass (costs 5 min on future picks).")
        s.active_ping = await channel.send(content=_team_mentions(team), embed=embed)
        deadline_ts = int(datetime.now(timezone.utc).timestamp()) + int(_secs_until_open()) + remaining
        await _dm_team(s, team, deadline_ts, _pick_title(s))
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
    embed.set_footer(text="Use !timerskip to pass (costs 5 min on future picks).")
    s.active_ping = await channel.send(
        content=f"☀️ **Draft window open!** {_team_mentions(team)}", embed=embed
    )
    await _dm_team(s, team, deadline_ts, _pick_title(s))
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

    _append_challenge_history({
        "channel_id":    s.channel_id,
        "draft_label":   s.draft.draft_label or s.draft.draft_started or "Unknown ATD",
        "draft_started": s.draft.draft_started,
        "team_name":     team["name"],
        "challenger":    challenger_name,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

    if s.challenge_count >= 3:
        await channel.send(
            f"⚡ **Challenge #{s.challenge_count}!** {challenger_mention} challenged "
            f"{_team_mentions(team)} — **3 challenges reached, skipping immediately!**"
        )
        s.challenge_count = 0
        await _do_skip(s, auto=True)
        return

    if s.draft.state == "window_paused":
        # Don't start a live timer while the window's closed — that would run
        # a real pick-clock during closed hours alongside the still-sleeping
        # _window_resume_task. Just record the challenge and cap the time
        # that gets restored so the reduced clock kicks in automatically when
        # the window reopens.
        s.draft.paused_remaining = min(s.draft.paused_remaining or 600, 600)
        s.draft.save(s.channel_id)
        await channel.send(
            f"⚡ **Challenge #{s.challenge_count}!** {challenger_mention} challenged "
            f"{_team_mentions(team)} — draft window is closed, so the reduced "
            f"**10 min** timer will start when it reopens at **10:00 AM ET**."
        )
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
    embed.set_footer(text="Use !timerskip to pass (costs 5 min on future picks).")

    s.active_ping      = await channel.send(content=_team_mentions(team), embed=embed)
    s.draft.timer_start = datetime.now(timezone.utc).isoformat()
    s.draft.save(s.channel_id)
    await _dm_team(s, team, deadline_ts, f"⚡ Challenged! {_pick_title(s)}")
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
    # "window_paused" is included alongside "active" — this function is what
    # decides (via _in_window() below) whether to actually start a live
    # timer or re-pause for the closed window, so a caller landing here
    # while still window_paused (e.g. a commissioner skip during closed
    # hours) must be allowed through rather than silently dropped, or the
    # next team never gets pinged.
    if not team or s.draft.state not in ("active", "window_paused"):
        log.warning("_start_timer: ch=%d early return — team=%s state=%s",
                    s.channel_id, team["name"] if team else None, s.draft.state)
        return

    channel = s.channel
    if not channel:
        log.error("_start_timer: channel %d not found in cache", s.channel_id)
        return

    # A block just restored this GM's prior remaining time instead of a
    # fresh timer — consume it once here so it feeds both the window-closed
    # (paused_remaining) and window-open paths below identically.
    if s.draft.next_timer_override_secs is not None:
        duration = s.draft.next_timer_override_secs
        s.draft.next_timer_override_secs = None
    else:
        duration = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)

    if not _in_window():
        await _auto_pause_for_window(s, duration, next_up=True)
        return

    if s.draft.order_mode == "roundless" and team.get("pending_makeup"):
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
            f"**Active Skip (AS)** — {s.draft.active_skip_threshold()}+ skips recorded. Skipping immediately."
        )
        await _do_skip(s, auto=True)
        return

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
    s.draft.state        = "active"  # self-heals a caller that got here while still "window_paused"
    s.draft.timer_start  = datetime.now(timezone.utc).isoformat()
    s.draft.save(s.channel_id)

    await _ping_current(s)
    s.timer_task = asyncio.create_task(_timer_loop(s, duration, team["user_ids"]))


async def _do_skip(s: DraftSession, auto: bool = False):
    current = asyncio.current_task()
    if s.timer_task and not s.timer_task.done() and s.timer_task is not current:
        s.timer_task.cancel()
    # A commissioner can now force a skip while the draft window is closed
    # (state == "window_paused"), which means a _window_resume_task from the
    # skipped team's pause may still be sleeping — left uncancelled, it'd
    # fire later for whichever team is current by then, alongside the fresh
    # one _start_timer below spawns for the new team, double-triggering the
    # window-open ping/timer.
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()

    team = s.draft.current_team
    if not team:
        return

    pick_num   = s.draft.overall_pick
    team_idx   = s.draft.current_team_idx
    served_from_queue = bool(
        s.draft.sbl_enabled and s.draft.repick_queue and s.draft.repick_queue[0][0] == team_idx
    )
    mentions   = _team_mentions(team)
    prev_skip  = team.get("skip_count", 0)
    skip_count = prev_skip + 1
    prev_last_pick = team.get("last_pick_number", 0)

    team["skip_count"] = skip_count
    team["pending_makeup"] = True
    if s.draft.order_mode == "roundless":
        team["last_pick_number"] = pick_num

    s.draft.last_skip = {
        "round":                 s.draft.current_round,
        "in_round":              s.draft.current_in_round,
        "team_idx":              team_idx,
        "prev_skip_count":       prev_skip,
        "prev_last_pick_number": prev_last_pick,
    }

    if s.draft.timer_override is not None:
        skip_note = f"{skip_count} skip{'s' if skip_count != 1 else ''}"
    else:
        next_timer_min = s.draft.effective_timer(s.draft.round_number, team_idx) // 60
        timer_note = "instant skip (AS)" if next_timer_min <= 0 else f"{next_timer_min}m left on future picks"
        skip_note = f"{skip_count} skip{'s' if skip_count != 1 else ''} - {timer_note}"

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

    s.draft.advance(served_from_queue=served_from_queue)
    s.draft.timer_start = None
    s.draft.save(s.channel_id)

    channel = s.channel
    await channel.send(
        f"**{pick_num}.** {mentions} skipped ({skip_note})"
    )

    if s.draft.state == "complete":
        await channel.send("🏆 **Draft complete!**")
        await _post_draft_recap(s)
        return

    await _start_timer(s)


# ── Steal / Block / Lock (SBL) ────────────────────────────────────────────────

_NAME_NONLETTER_RE = re.compile(r'[^A-Za-z\s]')
_NAME_WHITESPACE_RE = re.compile(r'\s+')


def _normalize_name(text: str) -> str:
    text = _NAME_NONLETTER_RE.sub(' ', text)
    text = _NAME_WHITESPACE_RE.sub(' ', text)
    return text.strip().lower()


def _sbl_ineligible_reason(s: DraftSession, pick_num: int, rec: dict) -> str | None:
    """None if targetable right now; otherwise a short reason why not."""
    if rec.get("protected"):
        return "that was a repick made after being blocked/stolen — it's protected from further steals/blocks."
    if rec.get("locked"):
        return "that player is locked — immune to steal/block for the rest of the draft."
    if not s.draft.sbl_eligible(pick_num):
        return "that pick is outside the current round's eligible window."
    return None


def _find_sbl_target(content: str, s: DraftSession):
    """Fuzzy-match a player name mentioned in `content` against all known
    picks (not just currently-eligible ones, so a specific reason can be
    given for e.g. a protected repick). Returns (pick_number, record,
    ineligible_reason) — record is None if nothing matched at all;
    ineligible_reason is None if the match is currently targetable."""
    all_records = [(int(num_str), rec) for num_str, rec in s.draft.pick_records.items()]
    if not all_records:
        return None, None, None

    msg_norm = _normalize_name(content)

    # Same real player can legitimately appear under multiple pick numbers
    # (e.g. two different teams each drafted a "Paul Pressey") — when names
    # tie on length, prefer whichever copy is actually targetable right now
    # (in the current eligible window, unlocked, unprotected) over an older
    # copy that just happens to share dict insertion order priority, then
    # fall back to the most recent pick number.
    def _tiebreak(nr):
        n, r = nr
        return (-len(r["player_name"]), _sbl_ineligible_reason(s, n, r) is not None, -n)

    substr_matches = [
        (n, r) for n, r in all_records
        if _normalize_name(r["player_name"]) and _normalize_name(r["player_name"]) in msg_norm
    ]
    if substr_matches:
        substr_matches.sort(key=_tiebreak)
        pick_num, rec = substr_matches[0]
        return pick_num, rec, _sbl_ineligible_reason(s, pick_num, rec)

    norm_by_num = {n: _normalize_name(r["player_name"]) for n, r in all_records}
    hit = process.extractOne(msg_norm, list(norm_by_num.values()), scorer=fuzz.token_sort_ratio)
    if not hit or hit[1] < 75:
        return None, None, None
    fuzzy_matches = sorted(
        (nr for nr in all_records if _normalize_name(nr[1]["player_name"]) == hit[0]),
        key=_tiebreak,
    )
    if fuzzy_matches:
        n, r = fuzzy_matches[0]
        return n, r, _sbl_ineligible_reason(s, n, r)
    return None, None, None


def _find_latest_unreclaimed_steal_against(s: DraftSession, team_idx: int):
    """Most recent pick record that's an unreclaimed steal taken FROM this
    team — used as an implicit block target when a bare 'block' message (no
    player name, no resolvable reply) doesn't give _find_sbl_target anything
    to match against. This is overwhelmingly the common real case: a GM who
    just got stolen from typing/replying just 'block' with nothing else."""
    candidates = [
        (int(num_str), rec) for num_str, rec in s.draft.pick_records.items()
        if rec.get("is_steal_result") and rec.get("stolen_from_team_idx") == team_idx
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda nr: nr[0], reverse=True)
    return candidates[0]


async def _sbl_reject(message: discord.Message, text: str) -> bool:
    """Send a rejection reply AND add a ❌ reaction to the original message.
    The reaction (not just processed_msg_ids, which is in-memory only and
    resets on every restart) is what lets _missed_pick_scanner() reliably
    recognize this message as already handled, even across a redeploy —
    without it, a rejected steal/block gets silently re-attempted and
    re-rejected every 30s once the in-memory dedup state is gone."""
    try:
        await message.add_reaction('❌')
    except discord.HTTPException:
        pass
    await message.reply(text)
    return False


async def _try_process_sbl_action(s: DraftSession, message: discord.Message,
                                   override_acting_member: discord.Member = None) -> bool:
    """Detect free-form steal/block intent in a GM's message and, if legal, apply it.
    Returns True if a steal or block was actually applied.

    Called for ANY message carrying steal/block intent, regardless of whether
    SBL is actually enabled — a message like "3. steal LeBron James $34" must
    never fall through to normal pick processing even in a plain snake draft,
    or it gets literally recorded as a player named "steal LeBron James"
    (and silently escapes the duplicate-pick check, since that text doesn't
    match the real LeBron James record either).

    override_acting_member: when set (only by !timersblfor), the action is
    attributed to this member instead of message.author/mentions — lets a
    commissioner act on behalf of a GM using THEIR eligibility/charges."""
    if s.draft.state not in ("active", "paused", "window_paused"):
        return False

    content = message.content.strip()
    if not content or (override_acting_member is None and content.startswith('!')):
        return False

    is_steal = bool(_STEAL_INTENT_RE.search(content))
    is_block = bool(_BLOCK_INTENT_RE.search(content))
    if is_steal == is_block:  # neither, or both (ambiguous) — ignore
        return False

    # Guards against reprocessing: Discord can re-fire on_message on
    # reconnect, and _missed_pick_scanner() may independently rediscover the
    # same message later. Once an attempt has been made (success OR a
    # rejection like "no target found"), never retry it — otherwise a
    # rejected message would get silently re-attempted, and eventually
    # succeed once the game state around it happens to change (or spam
    # rejection replies every 30s from the scanner).
    if message.id in s.processed_msg_ids:
        return False
    s.processed_msg_ids.add(message.id)

    if not s.draft.sbl_enabled:
        return await _sbl_reject(
            message,
            f"❌ You can't {'steal' if is_steal else 'block'} — "
            f"Steal/Block/Lock isn't enabled for this draft.",
        )

    # Support "reply to the pick with just 'block'/'steal'" — pull the target
    # player name from the referenced message if the reply itself doesn't name one.
    search_content = content
    if message.reference:
        try:
            ref_msg = message.reference.resolved
            if not isinstance(ref_msg, discord.Message):
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            search_content = f"{content} {ref_msg.content}".strip()
        except (discord.NotFound, discord.HTTPException):
            pass

    if override_acting_member is not None:
        acting_id      = override_acting_member.id
        acting_mention = override_acting_member.mention
    else:
        # ATD Draft List Bot relays steal/block on an absentee GM's behalf — it
        # mentions the acting GM rather than being able to post as them, so the
        # acting team must be resolved from that mention instead of message.author.
        is_relayed = bool(DRAFT_LIST_BOT_ID and message.author.id == DRAFT_LIST_BOT_ID and message.mentions)
        acting_id  = message.mentions[0].id if is_relayed else message.author.id
        acting_mention = message.mentions[0].mention if is_relayed else message.author.mention

    author_team_idx = next(
        (i for i, t in enumerate(s.draft.teams) if acting_id in t["user_ids"]),
        None,
    )
    if author_team_idx is None:
        return await _sbl_reject(
            message,
            f"❌ {acting_mention} — couldn't match you to a team in this draft.",
        )
    author_team = s.draft.teams[author_team_idx]

    if is_steal:
        if author_team.get("sbl_owed_protection"):
            return await _sbl_reject(
                message,
                "❌ You were just blocked/stolen from — you must make an original pick before you can steal.",
            )
        if s.draft.current_team_idx != author_team_idx:
            return await _sbl_reject(message, "❌ You can only steal on your own turn.")
        # Normally a stealer can never be serving a repick-queue turn (the
        # sbl_owed_protection check above rules it out) — but a commissioner
        # can clear that flag by hand to let a queued team steal instead of
        # making an original pick. Captured now, before queue_repick() below
        # inserts the victim at the front and would make this unreadable.
        author_served_from_queue = bool(
            s.draft.sbl_enabled and s.draft.repick_queue and s.draft.repick_queue[0][0] == author_team_idx
        )
        if author_team.get("steals_remaining", SBL_STEALS_PER_TEAM) <= 0:
            return await _sbl_reject(message, f"❌ {acting_mention} — you have no steals remaining.")

        pick_num, rec, ineligible_reason = _find_sbl_target(search_content, s)
        if rec is None:
            return await _sbl_reject(message, "❌ Couldn't identify an eligible player to steal in your message.")
        if ineligible_reason:
            return await _sbl_reject(message, f"❌ Can't steal **{rec['player_name']}** — {ineligible_reason}")
        if rec["team_idx"] == author_team_idx:
            return await _sbl_reject(message, "❌ You can't steal your own pick.")

        if (s.draft.budget_max is not None and rec.get("price") is not None
                and author_team.get("money_spent", 0) + rec["price"] > s.draft.budget_max):
            return await _sbl_reject(
                message,
                f"❌ {acting_mention} — you ahhh is broke, you can't afford **{rec['player_name']}**.",
            )

        victim_idx  = rec["team_idx"]
        victim_team = s.draft.teams[victim_idx]
        old_pick_num = pick_num           # voided pick's number — reopened for the victim's repick
        new_pick_num = s.draft.overall_pick  # the stealer's own turn — always a fresh number here,
        # since sbl_owed_protection (checked above) rules out the stealer
        # themselves currently serving a repick-queue turn.
        price = rec.get("price")

        author_team["steals_remaining"] = author_team.get("steals_remaining", SBL_STEALS_PER_TEAM) - 1

        # Remove the player from the victim's roster bookkeeping entirely —
        # they no longer own this pick.
        victim_team["picks"] = [p for p in victim_team.get("picks", []) if _pick_name_key(p) != rec["name_key"]]
        if old_pick_num in victim_team.get("pick_numbers", []):
            victim_team["pick_numbers"].remove(old_pick_num)

        # Re-key the record under the stealer's own turn number; the old
        # number is freed up for the victim's eventual repick.
        del s.draft.pick_records[str(old_pick_num)]
        rec["team_idx"]             = author_team_idx
        rec["is_steal_result"]      = True
        rec["stolen_from_team_idx"] = victim_idx  # lets the original owner block-reclaim later

        # This record's "remaining time" now belongs to the stealer, not the
        # original picker — overwrite it with the stealer's own clock at the
        # moment of stealing, since a future block of THIS pick should
        # restore the stealer's time, not the original victim's.
        steal_remaining = None
        if s.draft.timer_start:
            _dur = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)
            _elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(s.draft.timer_start)).total_seconds()
            steal_remaining = max(0, int(_dur - _elapsed))
        rec["remaining_at_pick"] = steal_remaining

        s.draft.pick_records[str(new_pick_num)] = rec

        # Keep this in the same "Name $price" shape normal picks use — an
        # annotation baked into the name text would corrupt future
        # duplicate-name matching against _pick_name_key().
        stolen_raw = f"{rec['player_name']} ${price}" if price is not None else rec['player_name']
        author_team.setdefault("picks", []).append(stolen_raw)
        author_team.setdefault("pick_numbers", []).append(new_pick_num)

        if price is not None:
            victim_team["money_spent"] = max(victim_team.get("money_spent", 0) - price, 0)
            author_team["money_spent"] = author_team.get("money_spent", 0) + price

        victim_team["sbl_owed_protection"] = True
        s.draft.queue_repick(victim_idx, old_pick_num)

        # A steal message can also declare a lock on the stolen player in the
        # same breath (e.g. "3. Steal Kawhi Leonard ($34) 🔒") — recognize it
        # here since this message never reaches _try_process_pick.
        lock_note = ""
        if _LOCK_MARKER_RE.search(content):
            if author_team.get("locks_remaining", SBL_LOCKS_PER_TEAM) > 0:
                rec["locked"] = True
                author_team["locks_remaining"] = author_team.get("locks_remaining", SBL_LOCKS_PER_TEAM) - 1
                lock_note = f" {acting_mention} also locked **{rec['player_name']}** in!"
            else:
                lock_note = " ⚠️ No locks remaining — the steal was not locked."

        if s.timer_task and not s.timer_task.done():
            s.timer_task.cancel()
        if s.window_task and not s.window_task.done():
            s.window_task.cancel()
        await _delete_active_ping(s)
        if s.draft.state in ("window_paused", "paused"):
            s.draft.state            = "active"
            s.draft.paused_remaining = None

        # Team Sheet Bot / ADP Bot resolve fantasy team identity from the
        # emoji, not the GM's Discord name — make sure the destination
        # emoji is fresh from this message before it goes in the protocol line.
        author_team["emoji"] = _extract_team_emoji(content) or author_team.get("emoji")

        s.draft.advance(served_from_queue=author_served_from_queue)
        s.draft.save(s.channel_id)

        log.info("SBL STEAL | ch=%d | player=%s | %s -> %s | pick %d moved to %d | locked=%s",
                  s.channel_id, rec["player_name"], victim_team["name"], author_team["name"],
                  old_pick_num, new_pick_num, rec["locked"])
        await message.add_reaction("🔓")
        if rec["locked"]:
            await message.add_reaction("🔒")
        await message.channel.send(
            f"🔓 **STEAL** — {acting_mention} stole **{rec['player_name']}** "
            f"from **{victim_team['name']}**!{lock_note} {victim_team['name']} is back on the clock "
            f"for pick **{old_pick_num}**.\n"
            f"SBL_STEAL | {rec['player_name']} | {victim_team.get('emoji') or ''} | {author_team.get('emoji') or ''} | {price or ''}"
        )
        await _start_timer(s)
        return True

    if is_block:
        if author_team.get("blocks_remaining", SBL_BLOCKS_PER_TEAM) <= 0:
            return await _sbl_reject(message, f"❌ {acting_mention} — you have no blocks remaining.")

        pick_num, rec, ineligible_reason = _find_sbl_target(search_content, s)
        if rec is None:
            fallback = _find_latest_unreclaimed_steal_against(s, author_team_idx)
            if fallback:
                pick_num, rec = fallback
                ineligible_reason = _sbl_ineligible_reason(s, pick_num, rec)
            else:
                return await _sbl_reject(message, "❌ Couldn't identify an eligible player to block in your message.")
        if ineligible_reason:
            return await _sbl_reject(message, f"❌ Can't block **{rec['player_name']}** — {ineligible_reason}")
        if rec["team_idx"] == author_team_idx:
            return await _sbl_reject(message, "❌ You can't block your own pick.")

        victim_idx  = rec["team_idx"]
        victim_team = s.draft.teams[victim_idx]

        # If this player was stolen, ANY block on it gives the player back
        # directly to whoever they were originally stolen from — not just
        # when that original owner happens to be the one doing the blocking
        # — instead of voiding to the pool. "Blocking the steal" rather than
        # blocking a pick outright.
        original_owner_idx  = rec.get("stolen_from_team_idx")
        original_owner_team = s.draft.teams[original_owner_idx] if original_owner_idx is not None else None
        is_reclaim = bool(rec.get("is_steal_result") and original_owner_idx is not None)

        refund_note = ""
        if rec.get("is_steal_result"):
            victim_team["steals_remaining"] = victim_team.get("steals_remaining", SBL_STEALS_PER_TEAM) + 1
            refund_note = f" {victim_team['name']}'s steal charge has been refunded."

        author_team["blocks_remaining"] = author_team.get("blocks_remaining", SBL_BLOCKS_PER_TEAM) - 1
        victim_team["picks"] = [p for p in victim_team["picks"] if _pick_name_key(p) != rec["name_key"]]
        if pick_num in victim_team.get("pick_numbers", []):
            victim_team["pick_numbers"].remove(pick_num)
        price = rec.get("price")
        if price is not None:
            victim_team["money_spent"] = max(victim_team.get("money_spent", 0) - price, 0)
        del s.draft.pick_records[str(pick_num)]
        victim_team["sbl_owed_protection"] = True
        victim_team["sbl_barred_player_key"] = rec["name_key"]  # can't repick the same player who got blocked
        s.draft.queue_repick(victim_idx, pick_num)

        reclaim_note = ""
        if is_reclaim:
            # Give the player straight back to the original owner (whoever
            # they were stolen from — not necessarily whoever's blocking).
            # The original steal already queued that original owner for an
            # emergency repick (to replace the player it lost) — the reclaim
            # directly satisfies that exact obligation, so reuse ITS pick
            # number instead of minting a brand new one. Minting a new number
            # here would both skip a gap in the sequence for whoever picks
            # next AND let this team draft twice for a single loss (once via
            # the reclaim, again via the now-separately-still-queued repick).
            reclaim_num = None
            for i, (t_idx, reopened_num) in enumerate(s.draft.repick_queue):
                if t_idx == original_owner_idx:
                    reclaim_num = reopened_num
                    s.draft.repick_queue.pop(i)
                    break
            if reclaim_num is None:
                # No matching queued obligation found (shouldn't normally
                # happen) — fall back to minting a fresh number so the
                # reclaim still registers correctly.
                s.draft.total_picks_made += 1
                reclaim_num = s.draft.total_picks_made

            reclaimed_raw = f"{rec['player_name']} ${price}" if price is not None else rec['player_name']
            original_owner_team.setdefault("picks", []).append(reclaimed_raw)
            original_owner_team.setdefault("pick_numbers", []).append(reclaim_num)
            if price is not None:
                original_owner_team["money_spent"] = original_owner_team.get("money_spent", 0) + price
            s.draft.register_pick_record(reclaim_num, rec["player_name"], rec["name_key"], original_owner_idx, price)
            # This reclaim satisfies the original owner's own queued repick
            # obligation directly (they never type a pick for it), so it
            # never goes through _try_process_pick — the only place that
            # normally consumes sbl_owed_protection. Left unset, this stale
            # flag would incorrectly attach "protected" to their next
            # unrelated normal pick instead of the repick that actually
            # earned it.
            original_owner_team.pop("sbl_owed_protection", None)
            reclaim_note = f" **{rec['player_name']}** is back on {original_owner_team['name']}'s roster!"

        # When the victim comes back up for their repick, give them back
        # whatever time was left on their own clock when they made this pick
        # instead of a fresh full timer — getting blocked shouldn't hand them
        # a free reset.
        if rec.get("remaining_at_pick") is not None:
            s.draft.next_timer_override_secs = rec["remaining_at_pick"]

        # Always interrupt whoever's currently on the clock — the team that
        # just got blocked takes priority now, ahead of anyone already
        # waiting (queue_repick puts them at the front). Their turn isn't
        # lost: repick-queue picks don't advance the normal rotation, so
        # whoever was interrupted resumes automatically once the queue drains.
        if s.timer_task and not s.timer_task.done():
            s.timer_task.cancel()
        if s.window_task and not s.window_task.done():
            s.window_task.cancel()
        await _delete_active_ping(s)
        if s.draft.state in ("window_paused", "paused"):
            s.draft.state            = "active"
            s.draft.paused_remaining = None

        s.draft.save(s.channel_id)

        log.info("SBL BLOCK | ch=%d | player=%s | victim=%s | by=%s | reclaim=%s",
                  s.channel_id, rec["player_name"], victim_team["name"], author_team["name"], is_reclaim)
        clock_note = f"{victim_team['name']} is back on the clock now for pick **{pick_num}**!"
        title = "🔁 **STEAL BLOCKED**" if is_reclaim else "🚫 **BLOCK**"
        verb  = "blocked the steal of" if is_reclaim else "blocked"
        await message.add_reaction("🔁" if is_reclaim else "🚫")
        await message.channel.send(
            f"{title} — {acting_mention} {verb} **{rec['player_name']}** "
            f"({victim_team['name']})!{refund_note}{reclaim_note} {clock_note}\n"
            + (f"SBL_STEAL | {rec['player_name']} | {victim_team.get('emoji') or ''} | {original_owner_team.get('emoji') or ''} | {price or ''}"
               if is_reclaim else
               f"SBL_BLOCK | {rec['player_name']} | {victim_team['name']}")
        )
        await _start_timer(s)
        return True


async def _try_process_roundless_makeup(s: DraftSession, message: discord.Message):
    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num_in_msg = int(match.group(1))
    if pick_num_in_msg > s.draft.overall_pick:
        return

    team_idx = next((i for i, t in enumerate(s.draft.teams) if message.author.id in t["user_ids"]), None)
    if team_idx is None or not s.draft.teams[team_idx].get("pending_makeup"):
        return
    team = s.draft.teams[team_idx]

    if pick_num_in_msg == s.draft.overall_pick and s.draft.current_team is team:
        return

    already_done = any(r.emoji == "✅" and r.me for r in message.reactions)
    if already_done:
        return

    pick_raw = match.group(2).strip()

    wants_lock = bool(s.draft.sbl_enabled and _LOCK_MARKER_RE.search(pick_raw))
    if wants_lock:
        pick_raw = _LOCK_MARKER_RE.sub('', pick_raw).strip()

    player_key = _pick_name_key(pick_raw)
    for t in s.draft.teams:
        for p in t.get("picks", []):
            if _pick_name_key(p) == player_key:
                log.info("DUPLICATE MAKEUP PICK | ch=%d | Player: %s | Already taken by: %s",
                         s.channel_id, _extract_player_name(pick_raw), t["name"])
                await message.add_reaction('❌')
                await message.channel.send(
                    f"❌ {message.author.mention} — **{_extract_player_name(pick_raw)}** has already "
                    f"been taken by **{t['name']}**. Pick someone else."
                )
                return

    if s.draft.sbl_enabled and team.get("sbl_barred_player_key") == player_key:
        log.info("SBL BARRED MAKEUP REPICK | ch=%d | Player: %s | Team: %s",
                  s.channel_id, _extract_player_name(pick_raw), team["name"])
        await message.add_reaction('❌')
        await message.channel.send(
            f"❌ {message.author.mention} — **{_extract_player_name(pick_raw)}** was just blocked "
            f"from you. Pick someone else.\nSBL_VETO | {_extract_player_name(pick_raw)}"
        )
        return

    price_m = _PRICE_RE.search(pick_raw)
    price_dollars = None
    if price_m:
        raw = (price_m.group(1) or price_m.group(2) or price_m.group(3) or "0")
        try:
            price_dollars = int(float(raw.lstrip("$")))
            team["money_spent"] = team.get("money_spent", 0) + price_dollars
        except ValueError:
            pass

    team["last_pick_number"] = pick_num_in_msg
    team["pending_makeup"]   = False
    team["picks"].append(pick_raw)
    team.setdefault("pick_numbers", []).append(pick_num_in_msg)

    # Register with the same SBL table normal picks use — without this, a
    # makeup pick (someone catching up a skipped turn) is invisible to
    # steal/block targeting and can never legally be hit. Also consume any
    # owed-protection flag here, same as a normal pick would — otherwise it
    # leaks past this pick and wrongly attaches to the team's next *normal*
    # turn instead of the catch-up pick that actually earned it.
    lock_note = ""
    if s.draft.sbl_enabled:
        player_name = _extract_player_name(pick_raw)
        # See the equivalent comment in _try_process_pick: protected must
        # reflect whether this pick actually came from the front of the
        # repick queue, not the separately-tracked sbl_owed_protection flag,
        # which can be cleared without the repick it was meant for happening.
        served_from_queue = bool(
            s.draft.repick_queue and s.draft.repick_queue[0][0] == team_idx
        )
        team.pop("sbl_owed_protection", None)
        team.pop("sbl_barred_player_key", None)
        s.draft.register_pick_record(
            pick_num_in_msg, player_name, player_key, team_idx, price_dollars,
            protected=served_from_queue,
        )
        if wants_lock:
            if team.get("locks_remaining", SBL_LOCKS_PER_TEAM) > 0:
                s.draft.pick_records[str(pick_num_in_msg)]["locked"] = True
                team["locks_remaining"] = team.get("locks_remaining", SBL_LOCKS_PER_TEAM) - 1
                lock_note = f"\n🔒 **{player_name}** is now locked — immune to steal/block for the rest of the draft."
            else:
                lock_note = "\n⚠️ You have no locks remaining — this pick was **not** locked."

    s.draft.save(s.channel_id)

    log.info("MAKEUP PICK | ch=%d | Team: %s | Pick #%d | %s",
             s.channel_id, team["name"], pick_num_in_msg, pick_raw)
    await message.add_reaction("✅")
    if lock_note:
        await message.channel.send(lock_note)


async def _try_process_pick_price_edit(s: DraftSession, message: discord.Message, pick_num: int, pick_raw: str):
    """A GM edited an already-recorded pick (typically to add a price they
    forgot). Retroactively correct money_spent / the stored pick record —
    does not re-validate, re-advance, or touch turn order."""
    team = next((t for t in s.draft.teams if message.author.id in t["user_ids"]), None)
    if not team:
        return
    pick_numbers = team.get("pick_numbers", [])
    if pick_num not in pick_numbers:
        return
    idx = pick_numbers.index(pick_num)
    picks = team.get("picks", [])
    if idx >= len(picks):
        return

    old_price = _extract_price(picks[idx])
    new_price = _extract_price(pick_raw)
    if new_price is None or new_price == old_price:
        return

    picks[idx] = pick_raw
    team["money_spent"] = team.get("money_spent", 0) - (old_price or 0) + new_price

    if s.draft.sbl_enabled:
        rec = s.draft.pick_records.get(str(pick_num))
        if rec:
            rec["price"]      = new_price
            rec["player_name"] = _extract_player_name(pick_raw)
            rec["name_key"]    = _pick_name_key(pick_raw)

    s.draft.save(s.channel_id)
    log.info("PICK EDIT | ch=%d | pick=%d | team=%s | price %s -> %s",
              s.channel_id, pick_num, team["name"], old_price, new_price)
    try:
        await message.add_reaction("💰")
    except discord.HTTPException:
        pass


async def _try_process_pick(s: DraftSession, message: discord.Message, is_edit: bool = False):
    if s.draft.state not in ("active", "paused", "window_paused"):
        return

    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num = int(match.group(1))
    pick_raw = match.group(2).strip()

    wants_lock = bool(s.draft.sbl_enabled and _LOCK_MARKER_RE.search(pick_raw))
    if wants_lock:
        pick_raw = _LOCK_MARKER_RE.sub('', pick_raw).strip()

    if pick_num != s.draft.overall_pick:
        # An edit to a pick that's already been recorded (e.g. adding a price
        # that was forgotten the first time) doesn't re-enter the normal flow
        # below — it just needs its price/money bookkeeping corrected.
        if is_edit and pick_num < s.draft.overall_pick:
            await _try_process_pick_price_edit(s, message, pick_num, pick_raw)
            return

        # A genuine team owner's late pick attempt for a number that's
        # already passed used to fail completely silently — no reaction, no
        # explanation — which reads as "the bot ignored me" even when the
        # real story is "you're too late AND that player's gone anyway."
        # Only fires for actual team owners (not just anyone whose chat
        # happens to start with "N. ..."), and only when the player really
        # is already drafted, to avoid false positives on unrelated messages
        # that match the pick regex by accident.
        if (not is_edit and pick_num < s.draft.overall_pick
                and any(message.author.id in t["user_ids"] for t in s.draft.teams)):
            player_name = _extract_player_name(pick_raw)
            player_key  = _pick_name_key(pick_raw)
            for t in s.draft.teams:
                for p in t.get("picks", []):
                    if _pick_name_key(p) == player_key:
                        try:
                            await message.add_reaction('⏱️')
                        except discord.HTTPException:
                            pass
                        await message.channel.send(
                            f"⏱️ {message.author.mention} — Pick #{pick_num} already passed (we're on "
                            f"pick #{s.draft.overall_pick} now), and **{player_name}** was already taken "
                            f"by **{t['name']}** anyway."
                        )
                        return
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
        # Captured once, up front: in roundless mode current_team_idx is
        # recomputed dynamically from live team stats, and this function is
        # about to mutate the picking team's stats (picks/last_pick_number).
        # Re-reading current_team_idx after that would silently pick up
        # whoever the dynamic sort now ranks first, not who actually picked.
        team_idx = s.draft.current_team_idx
        served_from_queue = bool(
            s.draft.sbl_enabled and s.draft.repick_queue and s.draft.repick_queue[0][0] == team_idx
        )
        team     = s.draft.current_team

        is_commissioner_pick = (
            message.author.id in TRUSTED_BOT_IDS
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

        if s.draft.sbl_enabled and team.get("sbl_barred_player_key") == player_key:
            log.info("SBL BARRED REPICK | ch=%d | Player: %s | Team: %s",
                      s.channel_id, player_name, team["name"])
            await message.add_reaction('❌')
            await message.channel.send(
                f"❌ {message.author.mention} — **{player_name}** was just blocked from you. Pick someone else.\n"
                f"SBL_VETO | {player_name}"
            )
            return

        if s.draft.budget_max is not None:
            pick_price = _extract_price(pick_raw)
            if pick_price is not None and team.get("money_spent", 0) + pick_price > s.draft.budget_max:
                log.info("BUDGET EXCEEDED | ch=%d | Player: %s | Team: %s | spent=%s + %s > cap=%s",
                          s.channel_id, player_name, team["name"], team.get("money_spent", 0),
                          pick_price, s.draft.budget_max)
                await message.add_reaction('❌')
                await message.channel.send(
                    f"❌ {message.author.mention} — you ahhh is broke, you can't afford **{player_name}**."
                )
                return

        log.info(
            "PICK | ch=%d | Overall #%d | Round %d Pick %d | Team: %s | Player: %s",
            s.channel_id, s.draft.overall_pick, s.draft.round_number, s.draft.pick_in_round,
            team["name"], pick_raw,
        )

        # How much time was left on this GM's own clock when they made this
        # pick — restored (instead of a fresh timer) if this pick later gets
        # blocked, so blocking someone doesn't hand them a free full reset.
        remaining_at_pick = None
        pick_elapsed = None
        if s.draft.timer_start:
            _effective_dur = s.draft.effective_timer(s.draft.round_number, s.draft.current_team_idx)
            _elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(s.draft.timer_start)).total_seconds()
            remaining_at_pick = max(0, int(_effective_dur - _elapsed))
            pick_elapsed = max(0.0, _elapsed)

        if s.timer_task and not s.timer_task.done():
            s.timer_task.cancel()
        if s.window_task and not s.window_task.done():
            s.window_task.cancel()
        await _delete_active_ping(s)
        if s.draft.state in ("window_paused", "paused"):
            s.draft.state            = "active"
            s.draft.paused_remaining = None

        team["picks"].append(pick_raw)
        team.setdefault("pick_numbers", []).append(pick_num)
        # Elapsed time from when this GM's timer started to when they
        # actually picked — recorded to the cross-draft history !avgtimepicker
        # reads from. Only recorded when a real timer was running for this
        # pick (not e.g. a commissioner force/correction with no live clock).
        if pick_elapsed is not None:
            _append_pick_time_history({
                "channel_id":    s.channel_id,
                "draft_label":   s.draft.draft_label or s.draft.draft_started or "Unknown ATD",
                "draft_started": s.draft.draft_started,
                "user_ids":      list(team["user_ids"]),
                "team_name":     team["name"],
                "pick_num":      pick_num,
                "elapsed_seconds": pick_elapsed,
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            })
        team["pending_makeup"] = False

        price_dollars = _extract_price(pick_raw)

        # Tracked unconditionally, not just in roundless mode — if this draft
        # ever switches into roundless (e.g. snake for the first two rounds,
        # then !timermode roundless+sbl), the dynamic order needs accurate
        # money/last-pick history from every pick that came before the switch.
        if price_dollars is not None:
            team["money_spent"] = team.get("money_spent", 0) + price_dollars
        team["last_pick_number"] = pick_num

        lock_note = ""
        if s.draft.sbl_enabled:
            team["emoji"] = _extract_team_emoji(message.content) or team.get("emoji")
            # protected must come from served_from_queue (whether THIS pick
            # was actually pulled from the front of the repick queue), not
            # from sbl_owed_protection — that flag is tracked separately on
            # the team and can end up cleared (by a reclaim, an admin queue
            # correction, etc.) without the repick it was meant for ever
            # having happened, silently losing the protection it should grant.
            team.pop("sbl_owed_protection", None)
            team.pop("sbl_barred_player_key", None)
            s.draft.register_pick_record(
                pick_num, player_name, player_key, team_idx, price_dollars,
                protected=served_from_queue, remaining_at_pick=remaining_at_pick,
            )
            if wants_lock:
                if team.get("locks_remaining", SBL_LOCKS_PER_TEAM) > 0:
                    s.draft.pick_records[str(pick_num)]["locked"] = True
                    team["locks_remaining"] = team.get("locks_remaining", SBL_LOCKS_PER_TEAM) - 1
                    lock_note = f"\n🔒 **{player_name}** is now locked — immune to steal/block for the rest of the draft."
                else:
                    lock_note = "\n⚠️ You have no locks remaining — this pick was **not** locked."

        penalty_note = ""
        if player_name.lower() in PENALTY_PLAYERS and s.draft.mode != "snake+budget":
            if team_idx not in s.draft.penalty_teams:
                s.draft.apply_penalty(team_idx)
                penalty_note = (
                    f"⚠️ **{team['name']}** drafted **{player_name}** - "
                    f"they will pick **last** every round from Round 6 onward."
                )

        s.draft.advance(served_from_queue=served_from_queue)
        s.draft.save(s.channel_id)
        success = True

        await message.add_reaction("✅")

        if lock_note:
            await message.channel.send(lock_note)

        if penalty_note:
            await message.channel.send(penalty_note)

        if s.draft.state == "complete":
            await message.channel.send("🏆 **Draft complete! Great picks everyone.**")
            await _post_draft_recap(s)
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
                draft_started = (
                    datetime.fromisoformat(s.draft.draft_started)
                    if s.draft.draft_started else None
                )

                async for msg in channel.history(limit=30):
                    if msg.author.bot:
                        continue
                    if msg.id in s.processed_msg_ids:
                        continue
                    # Never touch a message from before the current draft
                    # started — e.g. a leftover from a prior draft in this
                    # same channel that got reset. processed_msg_ids only
                    # tracks what THIS process has seen since it last
                    # restarted, so it can't be relied on alone to exclude
                    # messages that predate a !timereset.
                    if draft_started and msg.created_at < draft_started:
                        continue

                    # A steal/block declaration never reaches _try_process_pick
                    # normally — on_message routes it to _try_process_sbl_action
                    # instead — so the scanner must do the same, or it'll record
                    # the raw declaration as a literal pick (e.g. "Steal LeBron
                    # James" as a player name).
                    if _has_sbl_intent(msg.content):
                        already_done = any(
                            r.emoji in ("🔓", "🚫", "❌", "🔁") and r.me for r in msg.reactions
                        )
                        if already_done:
                            continue
                        log.info(
                            "MISSED SBL ACTION RECOVERED | ch=%d | Author: %s | Content: %s",
                            ch_id, msg.author.display_name, msg.content[:80],
                        )
                        await _try_process_sbl_action(s, msg)
                        break

                    match = _PICK_RE.match(msg.content.strip())
                    if not match:
                        continue
                    if int(match.group(1)) != expected_pick:
                        continue
                    already_done = any(r.emoji in ("✅", "❌") and r.me for r in msg.reactions)
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

    _from_trusted_bot = message.author.id in TRUSTED_BOT_IDS
    if message.author.bot and not _from_trusted_bot:
        return

    # ── Challenge detection: reply in ATD_CHAT_CHANNEL_ID, its threads, or a
    # remote admin channel ──────────────────────────────────────────────────
    _in_chat = (message.channel.id == ATD_CHAT_CHANNEL_ID
                or getattr(message.channel, 'parent_id', None) == ATD_CHAT_CHANNEL_ID
                or message.channel.id in _REMOTE_VIEW_CHANNELS)
    if (_in_chat
            and message.reference
            and message.content.strip().lower() == "challenge"):
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.HTTPException):
            return

        for ch_id, s in list(_sessions.items()):
            if s.draft.state not in ("active", "window_paused") or not s.draft.current_team:
                continue
            if message.author.id in s.draft.current_team["user_ids"]:
                continue
            # This session's challenge only applies if the replied-to message
            # was sent by the team currently on the clock in THIS draft.
            if ref_msg.author.id not in s.draft.current_team["user_ids"]:
                continue

            effective_ping_time = s.ping_time
            if effective_ping_time is None and s.draft.timer_start:
                effective_ping_time = datetime.fromisoformat(s.draft.timer_start)
            # While the window is closed, timer_start is cleared and a fresh
            # ping for this turn may not have set ping_time either (see
            # _auto_pause_for_window) — there's no reliable "were they on the
            # clock yet" timestamp to check in that case, so don't block the
            # challenge on it.
            if effective_ping_time is None and s.draft.state != "window_paused":
                continue

            try:
                if effective_ping_time is not None and ref_msg.created_at < effective_ping_time:
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

    if message.content.startswith('!'):
        return

    s = _sessions[message.channel.id]

    # A pick message like "2. Steal Michael Jordan" carries steal/block intent —
    # it must NOT also be treated as a literal pick of a player named "Steal
    # Michael Jordan", even if SBL isn't enabled for this draft (in which case
    # it should just be rejected, not silently accepted as a real player name).
    # Check intent first and, if present, handle it exclusively instead of
    # falling through to normal pick processing.
    if _has_sbl_intent(message.content):
        await _try_process_sbl_action(s, message)
        return

    await _try_process_pick(s, message)

    if s.draft.state in ("active", "paused", "window_paused"):
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
    s = _sessions[after.channel.id]
    if _has_sbl_intent(after.content):
        await _try_process_sbl_action(s, after)
        return
    await _try_process_pick(s, after, is_edit=True)


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


# Matches a single drafter-list line for !lottery, e.g.:
#   1. :Syracuse: - HT/Liam
#   20. TBD  - Francis
# Deliberately permissive on spacing (seen inconsistently in real lotto
# messages) and doesn't require an @mention — team/drafter text is carried
# through verbatim, not resolved to a real Discord user.
_LOTTERY_LINE_RE = re.compile(r'^\s*\d+\.\s*(.+?)\s*-\s*(.+?)\s*$')


@bot.command(name="lottery")
@is_commissioner()
async def lottery(ctx):
    """!lottery — reply to a message listing drafters (one numbered line each,
    e.g. '1. :Emoji: - Drafter Name') to shuffle it into a fresh random lotto.
    Standalone announcement — doesn't touch any draft session/state."""
    ref = ctx.message.reference
    if not ref:
        await ctx.send("❌ Reply to the message listing the drafters with `!lottery`.")
        return

    src_msg = await ctx.channel.fetch_message(ref.message_id)
    entries = [
        (m.group(1), m.group(2))
        for line in src_msg.content.splitlines()
        if (m := _LOTTERY_LINE_RE.match(line))
    ]
    if not entries:
        await ctx.send(
            "❌ Could not find any drafter lines in that message. Each line must look like:\n"
            "`1. :Emoji: - Drafter Name`"
        )
        return

    import random
    random.shuffle(entries)
    lines = "\n".join(f"{i + 1}. {team} - {drafter}" for i, (team, drafter) in enumerate(entries))

    embed = discord.Embed(
        title=f"🎰 New Lottery — {len(entries)} drafters",
        description=lines,
        color=discord.Color.gold(),
    )
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
        # Backfill the team's emoji too — additive only (a draft loaded
        # before this existed, or a line with no emoji, shouldn't lose or
        # report a "change" for it), so it isn't gated behind name/id changes.
        if new.get("emoji") and not old.get("emoji"):
            old["emoji"] = new["emoji"]

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


@bot.command(name="timerslotedit")
@is_commissioner()
async def timerslotedit(ctx, slot: int, *, args: str = ""):
    """!timerslotedit <slot#> @user1 @user2 OR <discord_id> <name> — replace a
    lotto slot's owners. `slot#` is the team's fixed ROSTER position (1..N),
    NOT its position in the current pick order — those shift on a reroll.
    To edit whoever is actually scheduled to make a specific overall pick
    number, use !timerpickedit instead."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No lotto loaded.")
        return

    if slot < 1 or slot > len(s.draft.teams):
        await ctx.send(f"❌ Slot must be between 1 and {len(s.draft.teams)}.")
        return

    team = s.draft.teams[slot - 1]

    # Collect user_ids from mentions first, then bare IDs, then resolve display names
    new_ids   = [m.id for m in ctx.message.mentions]
    new_names = [m.display_name for m in ctx.message.mentions]

    # Also parse raw IDs and plain names from args (after stripping mention tokens)
    remaining = re.sub(r'<@!?\d+>', '', args).strip()
    for token in re.split(r'\s+', remaining):
        if not token:
            continue
        if token.isdigit() and len(token) > 6:
            uid = int(token)
            member = ctx.guild.get_member(uid)
            new_ids.append(uid)
            new_names.append(member.display_name if member else str(uid))
        elif token:
            new_ids.append(0)   # unknown — no Discord ID
            new_names.append(token)

    if not new_ids:
        await ctx.send("❌ Provide at least one user (@mention, Discord ID, or name).")
        return

    team["user_ids"] = [uid for uid in new_ids if uid != 0]
    team["name"]     = " / ".join(new_names)
    s.draft.save(s.channel_id)

    lines = "\n".join(f"**{i+1}.** {t['name']}" for i, t in enumerate(s.draft.teams))
    embed = discord.Embed(
        title=f"✅ Slot {slot} Updated",
        description=f"**New owners:** {' / '.join(new_names)}\n\n{lines}",
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"This is roster slot {slot}, not pick order — use !timerpickedit <pick#> to edit by pick number instead.")
    await ctx.send(embed=embed)


@bot.command(name="timerpickedit")
@is_commissioner()
async def timerpickedit(ctx, pick_number: int, *, args: str = ""):
    """!timerpickedit <pick#> @user1 @user2 OR <discord_id> <name> — replace
    the owners of whoever is ACTUALLY scheduled to make a given overall pick
    number, resolved through the live pick order (so it accounts for any
    reroll) rather than a raw roster position. Snake mode only — roundless
    order is computed live from stats, so there's no fixed "pick #N is team
    X" to edit."""
    s = _get_session(ctx.channel.id)

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No lotto loaded.")
        return

    if s.draft.order_mode == "roundless":
        await ctx.send(
            "❌ `!timerpickedit` only works for snake-order drafts — roundless "
            "pick order is computed live from stats, so there's no fixed pick "
            "slot to edit. Use `!timerslotedit` instead."
        )
        return

    total_picks = s.draft.num_teams * ROUNDS
    if pick_number < 1 or pick_number > total_picks:
        await ctx.send(f"❌ Pick number must be between 1 and {total_picks}.")
        return

    zero_pick = pick_number - 1
    round_idx = zero_pick // s.draft.num_teams
    in_round  = zero_pick % s.draft.num_teams

    if round_idx >= len(s.draft.pick_order):
        await ctx.send("❌ That pick's round hasn't been generated yet.")
        return

    team_idx = s.draft.pick_order[round_idx][in_round]
    team     = s.draft.teams[team_idx]

    new_ids   = [m.id for m in ctx.message.mentions]
    new_names = [m.display_name for m in ctx.message.mentions]

    remaining = re.sub(r'<@!?\d+>', '', args).strip()
    for token in re.split(r'\s+', remaining):
        if not token:
            continue
        if token.isdigit() and len(token) > 6:
            uid = int(token)
            member = ctx.guild.get_member(uid)
            new_ids.append(uid)
            new_names.append(member.display_name if member else str(uid))
        elif token:
            new_ids.append(0)   # unknown — no Discord ID
            new_names.append(token)

    if not new_ids:
        await ctx.send("❌ Provide at least one user (@mention, Discord ID, or name).")
        return

    old_name = team["name"]
    team["user_ids"] = [uid for uid in new_ids if uid != 0]
    team["name"]     = " / ".join(new_names)
    s.draft.save(s.channel_id)

    round_num     = round_idx + 1
    pick_in_round = in_round + 1
    log.info("PICK EDIT | ch=%d | Pick #%d (R%d P%d) | Team idx=%d | %s -> %s",
              s.channel_id, pick_number, round_num, pick_in_round, team_idx, old_name, team["name"])

    embed = discord.Embed(
        title=f"✅ Pick #{pick_number} Updated",
        description=(
            f"Round {round_num}, pick {pick_in_round} → roster slot **{team_idx + 1}**\n"
            f"**{old_name}** → **{team['name']}**"
        ),
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed)


_VALID_MODES = ("snake", "roundless", "snake+sbl", "roundless+sbl", "snake+budget")


def _sbl_note(s: "DraftSession") -> str:
    if not s.draft.sbl_enabled:
        return ""
    return (
        f"\n🎯 **Steal/Block/Lock enabled** — each GM gets "
        f"{SBL_STEALS_PER_TEAM} steals, {SBL_BLOCKS_PER_TEAM} blocks, {SBL_LOCKS_PER_TEAM} lock. "
        f"Eligible window: the current round ({s.draft.num_teams} picks). Use `!timersblhelp` for details."
    )


@bot.command(name="timermode", aliases=["timerswitch"])
@is_commissioner()
async def timermode(ctx, mode: str = ""):
    """!timermode snake | roundless | snake+sbl | roundless+sbl | snake+budget"""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    mode = mode.lower()
    if mode not in _VALID_MODES:
        await ctx.send(
            "❌ Usage: `!timermode <mode>` where mode is one of:\n"
            "**snake** — fixed round-based snake order (default)\n"
            "**roundless** — dynamic pick order based on money spent\n"
            "**snake+sbl** — snake order plus Steal/Block/Lock\n"
            "**roundless+sbl** — roundless order plus Steal/Block/Lock\n"
            "**snake+budget** — snake order, no LeBron/MJ penalty, no round 3/6 flip"
        )
        return

    if s.draft.state == "idle":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    was_sbl_enabled = s.draft.sbl_enabled
    s.draft.mode = mode

    # SBL just turned on: picks made before this point were never registered
    # into pick_records (registration is gated behind sbl_enabled), so
    # they'd otherwise be untargetable even when still within the eligible
    # window. Backfill them from each team's own pick history.
    backfilled = 0
    if s.draft.sbl_enabled and not was_sbl_enabled:
        for team_idx, team in enumerate(s.draft.teams):
            for pick_raw, pick_num in zip(team.get("picks", []), team.get("pick_numbers", [])):
                if str(pick_num) in s.draft.pick_records:
                    continue
                s.draft.register_pick_record(
                    pick_num,
                    _extract_player_name(pick_raw),
                    _pick_name_key(pick_raw),
                    team_idx,
                    _extract_price(pick_raw),
                )
                backfilled += 1

    s.draft.save(s.channel_id)

    order_note = (
        f"✅ Switched to **{mode}**.\n"
        + (
            "Pick order now computed dynamically:\n"
            "1. Less money spent → picks sooner\n"
            "2. Fewer picks made → picks sooner\n"
            "3. More time since last pick → picks sooner\n\n"
            f"Picks must include price: `{s.draft.overall_pick}. :Emoji: Player Name $42 Year`"
            if mode.startswith("roundless") else
            f"Fixed round-based order resumes from pick #{s.draft.overall_pick}."
        )
        + _sbl_note(s)
        + (f"\n📋 Backfilled {backfilled} earlier pick(s) so they're steal/block-eligible if still in window." if backfilled else "")
    )
    await ctx.send(order_note)


@bot.command(name="timerstart")
@is_commissioner()
async def timerstart(ctx, *label_parts):
    """!timerstart [snake|roundless|snake+sbl|roundless+sbl|snake+budget] [label] — begin the draft."""
    s = _get_session(ctx.channel.id)

    if s.draft.state != "lotto":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto` or `!timerlotto`.")
        return

    parts = list(label_parts)
    if parts and parts[0].lower() in _VALID_MODES:
        s.draft.mode = parts[0].lower()
        parts = parts[1:]
    else:
        s.draft.mode = "snake"

    # Rebuilt now that mode is finally locked in — !timerloadlotto/!timerlotto/
    # !timerorder ran before the mode was chosen, so whatever flip pattern they
    # used was provisional. No picks exist yet (state was still "lotto"), so
    # nothing already drafted is disturbed by rebuilding from round 0.
    if s.draft.order_mode == "snake":
        s.draft.pick_order = build_snake_order(s.draft.num_teams, flips=(s.draft.mode != "snake+budget"))

    s.draft.state            = "active"
    s.draft.current_round    = 0
    s.draft.current_in_round = 0
    s.draft.total_picks_made = 0
    s.draft.pick_records     = {}
    s.draft.repick_queue     = []
    s.draft.draft_started    = datetime.now(timezone.utc).isoformat()
    s.draft.draft_label      = " ".join(parts) if parts else None
    s.draft.save(s.channel_id)

    mode_note  = "\n🔄 **Roundless mode** — pick order determined by money spent, picks made, and time since last pick." if s.draft.order_mode == "roundless" else ""
    label_note = f" (**{s.draft.draft_label}**)" if s.draft.draft_label else ""
    await ctx.send(f"🏀 **The draft has started!**{label_note}{mode_note}{_sbl_note(s)}")
    await _start_timer(s)


@bot.command(name="timerpenalty")
@is_commissioner()
async def timerpenalty(ctx, pick_number: int):
    """!timerpenalty <pick_number> — manually apply LeBron/MJ penalty to the slot that made pick #N."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("lotto", "active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft loaded.")
        return

    n = s.draft.num_teams
    if n == 0:
        await ctx.send("❌ No teams loaded.")
        return

    if s.draft.order_mode == "roundless":
        await ctx.send("❌ Penalty is not applicable in roundless mode.")
        return

    if s.draft.mode == "snake+budget":
        await ctx.send("❌ Penalty is not applicable in snake+budget mode.")
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


@bot.command(name="timerlottoreroll")
@is_commissioner()
async def timerlottoreroll(ctx):
    """!timerlottoreroll — draw a brand new lottery for the current round onward,
    leaving every round already drafted untouched. Only valid at the start of
    a round, before that round's first pick (e.g. Rising Budget Flux's re-roll
    at the start of Rounds 3, 5, 7, 9)."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No draft in progress.")
        return

    if s.draft.order_mode != "snake":
        await ctx.send("❌ Lottery reroll only applies to snake-order drafts.")
        return

    if s.draft.current_in_round != 0:
        await ctx.send(
            f"❌ Can only reroll at the start of a round — Round {s.draft.round_number} "
            f"already has picks made. Wait for Round {s.draft.round_number + 1}."
        )
        return

    s.draft.pick_order = reroll_from_round(
        s.draft.pick_order, s.draft.num_teams, s.draft.current_round, s.draft.penalty_teams,
        flips=(s.draft.mode != "snake+budget"),
    )

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()
    await _delete_active_ping(s)
    if s.draft.state in ("window_paused", "paused"):
        s.draft.state            = "active"
        s.draft.paused_remaining = None

    s.draft.save(s.channel_id)

    new_order = s.draft.pick_order[s.draft.current_round]
    lines = "\n".join(
        f"**{i + 1}.** {_team_mentions(s.draft.teams[idx])} ({s.draft.teams[idx]['name']})"
        for i, idx in enumerate(new_order)
    )
    embed = discord.Embed(
        title=f"🎲 Lottery Reroll — Round {s.draft.round_number} onward",
        description=lines,
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=f"Rounds 1–{s.draft.round_number - 1} are unaffected. "
             f"New order resumes at pick #{s.draft.overall_pick}."
    )
    log.info("LOTTO REROLL | ch=%d | Round %d | New order: %s",
              s.channel_id, s.draft.round_number, [s.draft.teams[i]["name"] for i in new_order])
    await ctx.send(embed=embed)
    await _start_timer(s)


@bot.command(name="timerrebuildorder")
@is_commissioner()
async def timerrebuildorder(ctx):
    """!timerrebuildorder — Rebuild the snake pick order (applies penalty fixes)."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No draft in progress.")
        return
    s.draft.pick_order = build_snake_order(
        s.draft.num_teams, s.draft.penalty_teams, flips=(s.draft.mode != "snake+budget")
    )
    s.draft.save(s.channel_id)
    penalty_names = ", ".join(s.draft.teams[i]["name"] for i in s.draft.penalty_teams) if s.draft.penalty_teams else "none"
    await ctx.send(f"✅ Pick order rebuilt for {s.draft.num_teams} teams. Penalty teams: {penalty_names}")


@bot.command(name="timerjumpto")
@is_commissioner()
async def timerjumpto(ctx, pick_number: int):
    """!timerjumpto <pick_number> — jump the draft to a specific overall pick number."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ Load a lotto first with `!timerloadlotto`.")
        return

    if s.draft.order_mode == "roundless":
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
    # When SBL is on, overall_pick is driven by total_picks_made rather than
    # current_round/current_in_round — without this the displayed/effective
    # pick number silently stays wherever it was before the jump.
    if s.draft.sbl_enabled:
        s.draft.total_picks_made = pick_number - 1
    s.draft.paused_remaining = None
    s.draft.timer_start      = None
    s.draft.state            = "active"
    s.draft.save(s.channel_id)

    team = s.draft.current_team
    log.info("JUMP | ch=%d | To pick %d | Round %d | In-round %d | Team: %s",
             s.channel_id, pick_number, s.draft.round_number, s.draft.pick_in_round,
             team["name"] if team else "?")
    loc_str = (f"pick #{pick_number}" if s.draft.order_mode == "roundless"
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
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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

    if s.draft.order_mode == "roundless":
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
    # When SBL is on, overall_pick is driven by total_picks_made rather than
    # current_round/current_in_round — without this the displayed/effective
    # pick number silently stays wherever it was before the jump.
    if s.draft.sbl_enabled:
        s.draft.total_picks_made = pick_number - 1
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

@bot.command(name="timeraddowner")
@is_commissioner()
async def timeraddowner(ctx, slot: int, member: discord.Member):
    """!timeraddowner <slot#> @user — add a co-GM to a specific lotto slot."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No draft loaded.")
        return

    if slot < 1 or slot > len(s.draft.teams):
        await ctx.send(f"❌ Invalid slot. Must be between 1 and {len(s.draft.teams)}.")
        return

    team = s.draft.teams[slot - 1]

    if member.id in team["user_ids"]:
        await ctx.send(f"❌ {member.mention} is already on **{team['name']}**.")
        return

    team["user_ids"].append(member.id)
    team["name"] = " / ".join(
        (ctx.guild.get_member(uid).display_name if ctx.guild.get_member(uid) else str(uid))
        for uid in team["user_ids"]
    )
    s.draft.save(s.channel_id)

    log.info("ADD OWNER | ch=%d | Slot %d Team: %s | Added: %s (%d)",
             s.channel_id, slot, team["name"], member.display_name, member.id)
    await ctx.send(f"✅ {member.mention} added to slot **{slot}** — **{team['name']}**.")


@bot.command(name="timerremoveowner")
@is_commissioner()
async def timerremoveowner(ctx, slot: int, member: discord.Member):
    """!timerremoveowner <slot#> @user — remove a co-GM from a specific lotto slot."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("lotto", "active", "paused", "window_paused"):
        await ctx.send("❌ No draft loaded.")
        return

    if slot < 1 or slot > len(s.draft.teams):
        await ctx.send(f"❌ Invalid slot. Must be between 1 and {len(s.draft.teams)}.")
        return

    team = s.draft.teams[slot - 1]

    if member.id not in team["user_ids"]:
        await ctx.send(f"❌ {member.mention} isn't on **{team['name']}**.")
        return

    if len(team["user_ids"]) <= 1:
        await ctx.send(
            f"❌ Can't remove {member.mention} — they're the only owner of **{team['name']}**. "
            f"Add a replacement with `!timeraddowner` first."
        )
        return

    team["user_ids"].remove(member.id)
    team["name"] = " / ".join(
        (ctx.guild.get_member(uid).display_name if ctx.guild.get_member(uid) else str(uid))
        for uid in team["user_ids"]
    )
    s.draft.save(s.channel_id)

    log.info("REMOVE OWNER | ch=%d | Slot %d Team: %s | Removed: %s (%d)",
             s.channel_id, slot, team["name"], member.display_name, member.id)
    await ctx.send(f"✅ {member.mention} removed from slot **{slot}** — now **{team['name']}**.")


@bot.command(name="timerproxy")
@is_commissioner()
async def timerproxy(ctx, member: discord.Member):
    """!timerproxy @user — temporarily add @user as a co-picker for the current team."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("active", "window_paused"):
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
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    is_privileged = (
        ctx.author.guild_permissions.administrator
        or any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles)
    )

    # A commissioner can force a skip even while the draft window is closed
    # (state == "window_paused") — everyone else still has to wait for the
    # window to reopen, same as before.
    if s.draft.state == "window_paused" and not is_privileged:
        await ctx.send("❌ Draft window is closed. Only a commissioner can skip right now.")
        return
    if s.draft.state not in ("active", "window_paused"):
        await ctx.send("❌ No active draft.")
        return

    team = s.draft.current_team

    if not _is_team_owner(ctx.author.id, team) and not is_privileged:
        await ctx.send(f"❌ Only {_team_mentions(team)} or a commissioner can skip this pick.")
        return

    penalty_note = " **-5 min** from their future picks."
    await ctx.send(f"⏩ {_team_mentions(team)} is skipping.{penalty_note}")
    await _do_skip(s, auto=False)


@bot.command(name="timerunskip")
@is_commissioner()
async def timerunskip(ctx):
    """!timerunskip — undo the most recent skip."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return

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

    if s.draft.order_mode == "roundless":
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


@bot.command(name="timercheckmode", aliases=["timerwhatmode"])
async def timercheckmode(ctx):
    """!timercheckmode — show which draft mode this channel is currently in.
    Read-only — anyone can run it, unlike !timermode which requires
    commissioner/admin to actually change it."""
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return

    if s.draft.state == "idle":
        await ctx.send("❌ No draft loaded in this channel.")
        return

    mode_descriptions = {
        "snake":         "fixed round-based snake order",
        "roundless":     "dynamic pick order based on money spent",
        "snake+sbl":     "snake order plus Steal/Block/Lock",
        "roundless+sbl": "roundless order plus Steal/Block/Lock",
        "snake+budget":  "snake order, no LeBron/MJ penalty, no round 3/6 flip",
    }
    desc = mode_descriptions.get(s.draft.mode)
    label = f"**{s.draft.mode}** — {desc}" if desc else f"**{s.draft.mode}**"
    await ctx.send(f"🔧 This channel's draft mode: {label}")


@bot.command(name="timerskiplist")
async def timerskiplist(ctx):
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("lotto", "active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft loaded.")
        return

    from config import ROUND_TIMERS

    embed    = discord.Embed(title="Skip Penalties", color=discord.Color.orange())
    any_skips = False

    for i, team in enumerate(s.draft.teams):
        skips = team.get("skip_count", 0)
        if skips == 0:
            continue
        any_skips = True
        is_as  = s.draft.is_active_skip(i)
        as_tag = " 🔴 **ACTIVE SKIP**" if is_as else ""

        if s.draft.order_mode == "roundless":
            from config import ROUNDLESS_TIMER
            base_min = ROUNDLESS_TIMER // 60
            eff_min  = s.draft.effective_timer(s.draft.round_number, i) // 60
            value = (f"~~{base_min}m~~ → **instant skip**" if eff_min <= 0
                     else f"~~{base_min}m~~ → **{eff_min}m**")
        else:
            lines = []
            for r in range(1, ROUNDS + 1):
                base_min = ROUND_TIMERS.get(r, 1800) // 60
                eff_min  = s.draft.effective_timer(r, i) // 60
                if eff_min <= 0:
                    lines.append(f"R{r}: ~~{base_min}m~~ → **instant skip**")
                else:
                    lines.append(f"R{r}: ~~{base_min}m~~ → **{eff_min}m**")
            value = "\n".join(lines)

        embed.add_field(
            name=f"{team['name']} — {skips} skip(s){as_tag}",
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
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft in progress.")
        return

    COMPLETE_PICKS = 10

    if s.draft.order_mode == "roundless":
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


@bot.command(name="timernext")
async def timernext(ctx, count: int = 10):
    """!timernext [count] — preview the next N picks in order (default 10,
    max 30). Roundless mode reflects the live dynamic sort, which can shift
    as real picks land; snake mode reflects the current pick_order, which
    can shift if a reroll or penalty rebuild happens before then."""
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("active", "paused", "window_paused"):
        await ctx.send("❌ No active draft.")
        return

    count = max(1, min(count, 30))
    lines = []

    if s.draft.order_mode == "roundless":
        order = s.draft._roundless_sorted_order()
        for pos, idx in enumerate(order[:count], 1):
            t     = s.draft.teams[idx]
            emoji = f"{t['emoji']} " if pos == 1 and t.get('emoji') else ""
            arrow = " ← **ON CLOCK**" if pos == 1 else ""
            lines.append(f"**{pos}.** {emoji}{t['name']}{arrow}")
        if not lines:
            await ctx.send("✅ All teams have completed their picks!")
            return
    else:
        pending = []
        if s.draft.next_team_override is not None:
            pending.append((s.draft.overall_pick, s.draft.next_team_override))
        if s.draft.sbl_enabled and s.draft.repick_queue:
            pending.extend((reopened_pick_num, team_idx) for team_idx, reopened_pick_num in s.draft.repick_queue)

        rnd, in_rnd = s.draft.current_round, s.draft.current_in_round
        overall = rnd * s.draft.num_teams + in_rnd + 1
        while rnd < ROUNDS and rnd < len(s.draft.pick_order) and len(pending) < count:
            team_idx = s.draft.pick_order[rnd][in_rnd]
            pending.append((overall, team_idx))
            overall += 1
            in_rnd += 1
            if in_rnd >= s.draft.num_teams:
                in_rnd = 0
                rnd += 1

        for pos, (pick_num, team_idx) in enumerate(pending[:count], 1):
            t     = s.draft.teams[team_idx]
            emoji = f"{t['emoji']} " if pos == 1 and t.get('emoji') else ""
            arrow = " ← **ON CLOCK**" if pos == 1 else ""
            lines.append(f"**{pos}.** Pick #{pick_num} — {emoji}{t['name']}{arrow}")

        if not lines:
            await ctx.send("✅ Draft complete — no picks remaining.")
            return

    embed = discord.Embed(
        title=f"📋 Next {len(lines)} Pick{'s' if len(lines) != 1 else ''}",
        description="\n".join(lines),
        color=discord.Color.dark_blue(),
    )
    if s.draft.sbl_enabled:
        embed.set_footer(text="Assumes no further steals/blocks reorder things before then.")
    await ctx.send(embed=embed)


@bot.command(name="nextpick")
async def nextpick(ctx, member: discord.Member):
    """!nextpick @GM — show this team's pick number in every upcoming round
    (up to 10), read straight from the live pick order — so it already
    reflects the round 3/6 flip and any LeBron/MJ pick-last penalty. Snake
    drafts only: roundless has no fixed per-round schedule to preview."""
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return

    if s.draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft in progress.")
        return

    if s.draft.order_mode != "snake":
        await ctx.send("❌ `!nextpick` only applies to snake-order drafts — roundless has no fixed per-round schedule.")
        return

    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in this draft.")
        return

    team = s.draft.teams[team_idx]
    num_teams = s.draft.num_teams

    lines = []
    for r in range(s.draft.current_round, min(s.draft.current_round + 10, ROUNDS, len(s.draft.pick_order))):
        round_order = s.draft.pick_order[r]
        if team_idx not in round_order:
            continue
        pos = round_order.index(team_idx)
        overall = r * num_teams + pos + 1
        arrow = " ← **ON CLOCK**" if overall == s.draft.overall_pick else ""
        lines.append(f"Round **{r + 1}** — Pick **#{overall}**{arrow}")

    if not lines:
        await ctx.send(f"✅ **{team['name']}** has no picks remaining.")
        return

    embed = discord.Embed(
        title=f"📋 Upcoming Picks — {team['name']}",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Rounds 3 and 6 carry ATD's extra reversal, so consecutive rounds can land closer together than a plain snake would.")
    await ctx.send(embed=embed)


@bot.command(name="timerdrafts")
async def timerdrafts(ctx):
    """!timerdrafts — list every currently active draft and which channel
    it's in. Works from anywhere, not tied to a specific draft channel."""
    active = [s for s in _sessions.values() if s.draft.state in _ACTIVE_DRAFT_STATES]
    if not active:
        await ctx.send("📭 No active drafts right now.")
        return

    state_note = {"paused": " ⏸️ paused", "window_paused": " 🌙 window closed"}
    lines = []
    for s in active:
        d = s.draft
        progress = (f"Pick #{d.overall_pick}" if d.order_mode == "roundless"
                    else f"Round {d.round_number} of {ROUNDS} — Pick {d.pick_in_round}")
        label = d.draft_label or "Unlabeled draft"
        lines.append(f"<#{s.channel_id}> — **{label}** ({d.mode}) — {progress}{state_note.get(d.state, '')}")

    embed = discord.Embed(
        title=f"🏀 Active Drafts ({len(active)})",
        description="\n".join(lines),
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed)


def _fmt_duration(secs: float) -> str:
    m, sec = divmod(int(secs), 60)
    return f"{m}m {sec}s"


def _build_avgtimepicker_embed(pages: list[list[str]], page: int) -> discord.Embed:
    total = len(pages)
    title = "⏱️ Average Time to Pick — All-Time" if total == 1 \
        else f"⏱️ Average Time to Pick — All-Time (page {page + 1}/{total})"
    embed = discord.Embed(
        title=title,
        description="\n".join(pages[page]) or "No entries.",
        color=discord.Color.dark_blue(),
    )
    embed.set_footer(text="Quickest average pickers rank highest.")
    return embed


class AvgTimePickerView(discord.ui.View):
    def __init__(self, pages: list[list[str]]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page  = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page == len(self.pages) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=_build_avgtimepicker_embed(self.pages, self.page), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=_build_avgtimepicker_embed(self.pages, self.page), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="avgtimepicker")
async def avgtimepicker(ctx, member: discord.Member = None):
    """!avgtimepicker | !avgtimepicker @user — rank GMs by average time
    taken to make a pick (from when their timer started to when they
    actually picked), all-time across every ATD. Quickest average ranks
    highest. Works from any channel — not tied to a specific draft."""
    history = _load_pick_time_history()

    if not history:
        await ctx.send("📭 No timed picks recorded yet.")
        return

    if member is None:
        totals: dict[int, dict] = {}
        for entry in history:
            for uid in entry["user_ids"]:
                if uid not in totals:
                    totals[uid] = {"name": entry["team_name"], "total": 0.0, "picks": 0, "atds": set()}
                totals[uid]["total"] += entry["elapsed_seconds"]
                totals[uid]["picks"] += 1
                label = entry.get("draft_label") or entry.get("draft_started", "?")
                totals[uid]["atds"].add(label)

        sorted_totals = sorted(totals.items(), key=lambda x: x[1]["total"] / x[1]["picks"])
        rows = []
        for rank, (uid, data) in enumerate(sorted_totals, 1):
            member_obj = ctx.guild.get_member(uid) if ctx.guild else None
            name       = member_obj.display_name if member_obj else data["name"]
            avg        = data["total"] / data["picks"]
            atd_count  = len(data["atds"])
            rows.append(
                f"**{rank}.** <@{uid}> ({name}) — **{_fmt_duration(avg)}** avg "
                f"({data['picks']} pick(s) across {atd_count} ATD(s))"
            )

        pages = [rows[i:i + 10] for i in range(0, len(rows), 10)] or [[]]
        view  = AvgTimePickerView(pages) if len(pages) > 1 else None
        await ctx.send(embed=_build_avgtimepicker_embed(pages, 0), view=view)

    else:
        uid     = member.id
        entries = [e for e in history if uid in e["user_ids"]]

        if not entries:
            await ctx.send(f"✅ {member.mention} has no timed picks on record.")
            return

        by_draft: dict[str, list[dict]] = {}
        for entry in entries:
            label = entry.get("draft_label") or (
                datetime.fromisoformat(entry["draft_started"]).strftime("%b %d, %Y")
                if entry.get("draft_started") else "Unknown ATD"
            )
            by_draft.setdefault(label, []).append(entry)

        overall_avg = sum(e["elapsed_seconds"] for e in entries) / len(entries)
        embed = discord.Embed(
            title=f"⏱️ Average Time to Pick — {member.display_name}",
            description=f"**{_fmt_duration(overall_avg)}** avg across **{len(entries)}** pick(s) "
                        f"in {len(by_draft)} ATD(s)",
            color=discord.Color.orange(),
        )
        for label, draft_entries in by_draft.items():
            team_name  = draft_entries[0]["team_name"]
            draft_avg  = sum(e["elapsed_seconds"] for e in draft_entries) / len(draft_entries)
            lines = [
                f"Pick #{e['pick_num']} — {_fmt_duration(e['elapsed_seconds'])}"
                for e in draft_entries
            ]
            embed.add_field(
                name=f"{label} — {_fmt_duration(draft_avg)} avg as \"{team_name}\"",
                value="\n".join(lines),
                inline=False,
            )
        await ctx.send(embed=embed)


@bot.command(name="timersettimer")
@is_commissioner()
async def timersettimer(ctx, minutes: int):
    """!timersettimer <minutes> — override the timer for all future picks. Use 0 to revert to defaults."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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
    s = await _resolve_command_session(ctx)
    if s is None:
        return
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
    s = await _resolve_command_session(ctx)
    if s is None:
        return
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
    s = await _resolve_command_session(ctx)
    if s is None:
        return
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
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    s.draft.teams[team_idx]["last_pick_number"] = pick_number
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ **{s.draft.teams[team_idx]['name']}** last pick number set to **#{pick_number}**.")


@bot.command(name="timeraddpick")
@is_commissioner()
async def timeraddpick(ctx, member: discord.Member, pick_number: int, *, pick_text: str):
    """!timeraddpick @GM <pick_number> <player ...> — manually record a pick the
    bot missed (e.g. a message sent before a bugfix landed). Clears pending_makeup."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team = s.draft.teams[team_idx]

    pick_text  = pick_text.strip()
    player_key = _pick_name_key(pick_text)
    for t in s.draft.teams:
        for p in t.get("picks", []):
            if _pick_name_key(p) == player_key:
                await ctx.send(
                    f"❌ **{_extract_player_name(pick_text)}** has already been taken by **{t['name']}**."
                )
                return

    if pick_number in team.get("pick_numbers", []):
        await ctx.send(f"❌ **{team['name']}** already has a pick recorded as #{pick_number}.")
        return

    team.setdefault("picks", []).append(pick_text)
    team.setdefault("pick_numbers", []).append(pick_number)
    team["last_pick_number"] = pick_number
    team["pending_makeup"]   = False

    price = _extract_price(pick_text)
    if price is not None:
        team["money_spent"] = team.get("money_spent", 0) + price

    s.draft.save(s.channel_id)
    log.info("MANUAL ADD PICK | ch=%d | Team: %s | Pick #%d | %s",
              s.channel_id, team["name"], pick_number, pick_text)
    await ctx.send(f"✅ Added pick **#{pick_number}** for **{team['name']}**: {pick_text}")


@bot.command(name="timerreplacepick")
@is_commissioner()
async def timerreplacepick(ctx, member: discord.Member, pick_number: int, *, pick_text: str):
    """!timerreplacepick @GM <pick_number> <player ...> — overwrite an already-recorded
    pick that was entered wrong. Adjusts money_spent for the price difference."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return
    team = s.draft.teams[team_idx]

    pick_numbers = team.get("pick_numbers", [])
    if pick_number not in pick_numbers:
        await ctx.send(
            f"❌ **{team['name']}** has no pick recorded as #{pick_number} — use `!timeraddpick` instead."
        )
        return
    idx   = pick_numbers.index(pick_number)
    picks = team.get("picks", [])

    pick_text  = pick_text.strip()
    player_key = _pick_name_key(pick_text)
    for t in s.draft.teams:
        for j, p in enumerate(t.get("picks", [])):
            if t is team and j == idx:
                continue  # the slot being replaced — not a conflict with itself
            if _pick_name_key(p) == player_key:
                await ctx.send(
                    f"❌ **{_extract_player_name(pick_text)}** has already been taken by **{t['name']}**."
                )
                return

    old_text  = picks[idx]
    old_price = _extract_price(old_text)
    new_price = _extract_price(pick_text)
    picks[idx] = pick_text
    team["money_spent"] = max(team.get("money_spent", 0) - (old_price or 0) + (new_price or 0), 0)

    # Keep the SBL pick_records entry (a separate table used for steal/block
    # targeting) in sync — otherwise a later steal of this player would carry
    # over the stale name/price instead of the correction just made here.
    if s.draft.sbl_enabled:
        rec = s.draft.pick_records.get(str(pick_number))
        if rec:
            rec["player_name"] = _extract_player_name(pick_text)
            rec["name_key"]    = player_key
            rec["price"]       = new_price

    s.draft.save(s.channel_id)
    log.info("MANUAL REPLACE PICK | ch=%d | Team: %s | Pick #%d | %r -> %r",
              s.channel_id, team["name"], pick_number, old_text, pick_text)
    await ctx.send(f"✅ Pick **#{pick_number}** for **{team['name']}** replaced:\n~~{old_text}~~ → {pick_text}")


@bot.command(name="timeraddskip")
@is_commissioner()
async def timeraddskip(ctx, member: discord.Member, count: int = 1):
    """!timeraddskip @GM [count]"""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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


# ── Budget cap ────────────────────────────────────────────────────────────────

@bot.command(name="timersetbudget")
@is_commissioner()
async def timersetbudget(ctx, amount: int = None):
    """!timersetbudget <amount> — cap total money_spent per team; omit amount to clear the cap."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    if amount is None:
        s.draft.budget_max = None
        s.draft.save(s.channel_id)
        await ctx.send("✅ Budget cap cleared — no spending limit.")
        return
    if amount <= 0:
        await ctx.send("❌ Usage: `!timersetbudget <amount>` (positive number), or omit to clear the cap.")
        return
    s.draft.budget_max = amount
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ Budget cap set to **${amount}** per team. Picks or steals that would exceed it are rejected.")


# ── Steal / Block / Lock (SBL) commands ─────────────────────────────────────

@bot.command(name="timersblstatus")
async def timersblstatus(ctx):
    """!timersblstatus — show each GM's remaining steals/blocks/locks."""
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return
    if not s.draft.sbl_enabled:
        await ctx.send("❌ Steal/Block/Lock isn't enabled for this draft.")
        return

    lines = []
    for t in s.draft.teams:
        lines.append(
            f"**{t['name']}** — 🔓 steals: {t.get('steals_remaining', SBL_STEALS_PER_TEAM)} "
            f"| 🚫 blocks: {t.get('blocks_remaining', SBL_BLOCKS_PER_TEAM)} "
            f"| 🔒 locks: {t.get('locks_remaining', SBL_LOCKS_PER_TEAM)}"
        )
    embed = discord.Embed(
        title="Steal / Block / Lock — Remaining Charges",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Eligible window: current round ({s.draft.num_teams} picks) — currently round {s.draft.sbl_window(s.draft.overall_pick) + 1}")
    await ctx.send(embed=embed)


@bot.command(name="timersbladjust")
@is_commissioner()
async def timersbladjust(ctx, member: discord.Member, kind: str, amount: int):
    """!timersbladjust @GM steals|blocks|locks <n> — manual correction."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    if not s.draft.sbl_enabled:
        await ctx.send("❌ Steal/Block/Lock isn't enabled for this draft.")
        return

    kind = kind.lower().rstrip('s') + 's'  # normalize steal/steals -> "steals"
    field_map = {
        "steals": "steals_remaining",
        "blocks": "blocks_remaining",
        "locks":  "locks_remaining",
    }
    if kind not in field_map:
        await ctx.send("❌ Usage: `!timersbladjust @GM steals|blocks|locks <n>`")
        return

    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    s.draft.teams[team_idx][field_map[kind]] = amount
    s.draft.save(s.channel_id)
    await ctx.send(f"✅ **{s.draft.teams[team_idx]['name']}** {kind} set to **{amount}**.")


# ── Direct pick/queue overrides ─────────────────────────────────────────────
# These exist for exactly the kind of tangled steal/block chains that
# !timerjumpto alone can't fix: reassigning a pick that ended up on the
# wrong roster, forcing a priority makeup turn without a real steal/block
# to trigger it, and inspecting/clearing the repick queue directly.

@bot.command(name="timerreassignpick")
@is_commissioner()
async def timerreassignpick(ctx, pick_number: int, member: discord.Member):
    """!timerreassignpick <pick_number> @GM — move an existing pick to a
    different GM's roster, moving its price between the two teams' money_spent
    too. Marks it as a steal from the old owner (stolen_from_team_idx) so a
    future block on it reclaims correctly, and clears any lock/protected flag."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    rec = s.draft.pick_records.get(str(pick_number))
    if not rec:
        await ctx.send(f"❌ No pick record found for #{pick_number}.")
        return

    new_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if new_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    old_idx = rec["team_idx"]
    if old_idx == new_idx:
        await ctx.send(f"❌ **{s.draft.teams[old_idx]['name']}** already owns pick #{pick_number}.")
        return

    old_team = s.draft.teams[old_idx]
    new_team = s.draft.teams[new_idx]
    price    = rec.get("price")

    old_team["picks"] = [p for p in old_team.get("picks", []) if _pick_name_key(p) != rec["name_key"]]
    if pick_number in old_team.get("pick_numbers", []):
        old_team["pick_numbers"].remove(pick_number)
    if price is not None:
        old_team["money_spent"] = max(old_team.get("money_spent", 0) - price, 0)

    raw = f"{rec['player_name']} ${price}" if price is not None else rec['player_name']
    new_team.setdefault("picks", []).append(raw)
    new_team.setdefault("pick_numbers", []).append(pick_number)
    if price is not None:
        new_team["money_spent"] = new_team.get("money_spent", 0) + price

    rec["team_idx"]             = new_idx
    rec["is_steal_result"]      = True
    rec["stolen_from_team_idx"] = old_idx
    rec["locked"]               = False
    rec["protected"]            = False

    s.draft.save(s.channel_id)
    log.info("FORCE REASSIGN PICK | ch=%d | Pick #%d | %s | %s -> %s",
              s.channel_id, pick_number, rec["player_name"], old_team["name"], new_team["name"])
    await ctx.send(
        f"✅ Pick **#{pick_number}** (**{rec['player_name']}**) moved from "
        f"**{old_team['name']}** to **{new_team['name']}**."
    )


@bot.command(name="timerforcequeue")
@is_commissioner()
async def timerforcequeue(ctx, member: discord.Member, pick_number: int):
    """!timerforcequeue @GM <pick_number> — force a priority makeup turn for
    @GM at pick_number, ahead of the normal rotation, without a real steal/
    block to trigger it (e.g. finishing a botched manual correction). Always
    interrupts whoever's currently on the clock, same as a real block/steal."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    if not s.draft.sbl_enabled:
        await ctx.send("❌ Steal/Block/Lock isn't enabled for this draft.")
        return

    team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
    if team_idx is None:
        await ctx.send(f"❌ {member.display_name} is not in the draft.")
        return

    team = s.draft.teams[team_idx]
    team["sbl_owed_protection"] = True
    s.draft.repick_queue = [e for e in s.draft.repick_queue if e[0] != team_idx]
    s.draft.repick_queue.insert(0, (team_idx, pick_number))

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()
    await _delete_active_ping(s)
    if s.draft.state in ("window_paused", "paused"):
        s.draft.state            = "active"
        s.draft.paused_remaining = None

    s.draft.save(s.channel_id)
    log.info("FORCE QUEUE | ch=%d | Team: %s | Pick #%d", s.channel_id, team["name"], pick_number)
    await ctx.send(f"✅ **{team['name']}** forced to the front of the queue for pick **#{pick_number}**.")
    await _start_timer(s)


@bot.command(name="timerclearqueue")
@is_commissioner()
async def timerclearqueue(ctx, *, target: str):
    """!timerclearqueue @GM — remove @GM's entries from the repick queue.
    !timerclearqueue all — clear the entire queue. Whichever team ends up
    on the clock afterward gets interrupted/re-pinged immediately."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    target = target.strip()
    removed: list[tuple[int, int]]
    if target.lower() == "all":
        removed = list(s.draft.repick_queue)
        s.draft.repick_queue = []
    else:
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            await ctx.send("❌ Usage: `!timerclearqueue @GM` or `!timerclearqueue all`.")
            return
        team_idx = next((i for i, t in enumerate(s.draft.teams) if member.id in t["user_ids"]), None)
        if team_idx is None:
            await ctx.send(f"❌ {member.display_name} is not in the draft.")
            return
        removed = [e for e in s.draft.repick_queue if e[0] == team_idx]
        s.draft.repick_queue = [e for e in s.draft.repick_queue if e[0] != team_idx]

    if not removed:
        await ctx.send("⚠️ Nothing to remove — the queue is already clear of that.")
        return

    # Removing the queue entry cancels the obligation it represents — leaving
    # sbl_owed_protection/sbl_barred_player_key set would falsely block that
    # team's next real steal/pick attempt with "you were just blocked/stolen
    # from" even though there's no queue entry left backing that claim.
    for team_idx, _ in removed:
        team = s.draft.teams[team_idx]
        team.pop("sbl_owed_protection", None)
        team.pop("sbl_barred_player_key", None)

    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    if s.window_task and not s.window_task.done():
        s.window_task.cancel()
    await _delete_active_ping(s)
    if s.draft.state in ("window_paused", "paused"):
        s.draft.state            = "active"
        s.draft.paused_remaining = None

    s.draft.save(s.channel_id)
    desc = ", ".join(f"{s.draft.teams[t]['name']} (#{n})" for t, n in removed)
    log.info("CLEAR QUEUE | ch=%d | Removed: %s", s.channel_id, desc)
    await ctx.send(f"✅ Removed from queue: {desc}")
    await _start_timer(s)


@bot.command(name="timerqueuestatus")
async def timerqueuestatus(ctx):
    """!timerqueuestatus — show the current repick queue, front to back."""
    s = await _resolve_viewable_session(ctx)
    if s is None:
        return

    if not s.draft.repick_queue:
        await ctx.send("📭 Repick queue is empty.")
        return

    lines = [
        f"**{i + 1}.** {s.draft.teams[team_idx]['name']} — pick **#{pick_num}**"
        for i, (team_idx, pick_num) in enumerate(s.draft.repick_queue)
    ]
    embed = discord.Embed(
        title="🔁 Repick Queue",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    embed.set_footer(text="Front of the queue is on the clock now, ahead of the normal rotation.")
    await ctx.send(embed=embed)


class _SBLProxyMessage:
    """Lightweight stand-in for discord.Message, used only by !timersblfor so
    it can reuse _try_process_sbl_action's exact logic instead of duplicating
    the steal/block/lock rules in a second place."""
    def __init__(self, ctx: commands.Context, content: str):
        self._real     = ctx.message
        self.content    = content
        self.id         = ctx.message.id
        self.channel    = ctx.channel
        self.author     = ctx.author
        self.mentions   = []
        self.reference  = None

    async def add_reaction(self, emoji):
        try:
            await self._real.add_reaction(emoji)
        except discord.HTTPException:
            pass

    async def reply(self, text):
        await self._real.reply(text)


@bot.command(name="timersblfor")
@is_commissioner()
async def timersblfor(ctx, member: discord.Member, *, action_text: str):
    """!timersblfor @GM <block|steal|lock ...> — perform a steal/block/lock on
    behalf of an absentee/AFK GM, charged against THEIR remaining steals/
    blocks/locks (not the commissioner's own team). Name the player directly
    in the text — e.g. `!timersblfor @Solid block Kevin McHale`."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return

    action_text = action_text.strip()
    if not _has_sbl_intent(action_text):
        await ctx.send(
            "❌ Must contain clear steal/block/lock intent, e.g. "
            "`!timersblfor @GM block Kevin McHale`."
        )
        return

    proxy = _SBLProxyMessage(ctx, action_text)
    await _try_process_sbl_action(s, proxy, override_acting_member=member)


@bot.command(name="timerunlock")
@is_commissioner()
async def timerunlock(ctx, pick_number: int):
    """!timerunlock <pick_number> — remove the lock flag from a specific pick
    (e.g. it was locked by mistake), making that player stealable/blockable
    again. Refunds the lock charge to whichever team currently owns the pick."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    if not s.draft.sbl_enabled:
        await ctx.send("❌ Steal/Block/Lock isn't enabled for this draft.")
        return

    rec = s.draft.pick_records.get(str(pick_number))
    if not rec:
        await ctx.send(f"❌ No pick record found for #{pick_number}.")
        return
    if not rec.get("locked"):
        await ctx.send(f"❌ Pick #{pick_number} (**{rec['player_name']}**) isn't locked.")
        return

    rec["locked"] = False
    team = s.draft.teams[rec["team_idx"]]
    team["locks_remaining"] = team.get("locks_remaining", SBL_LOCKS_PER_TEAM) + 1
    s.draft.save(s.channel_id)

    log.info("MANUAL UNLOCK | ch=%d | Pick #%d | Player: %s | Refunded to: %s",
              s.channel_id, pick_number, rec["player_name"], team["name"])
    await ctx.send(
        f"🔓 Pick **#{pick_number}** (**{rec['player_name']}**) is no longer locked — "
        f"eligible for steal/block again. Lock charge refunded to **{team['name']}**."
    )


@bot.command(name="timerlock")
@is_commissioner()
async def timerlock(ctx, pick_number: int):
    """!timerlock <pick_number> — manually mark a pick as locked (e.g. the 🔒
    marker was in the wrong spot in the pick message and didn't auto-detect).
    Spends a lock charge from whichever team currently owns the pick."""
    s = await _resolve_command_session(ctx)
    if s is None:
        return
    if not s.draft.sbl_enabled:
        await ctx.send("❌ Steal/Block/Lock isn't enabled for this draft.")
        return

    rec = s.draft.pick_records.get(str(pick_number))
    if not rec:
        await ctx.send(f"❌ No pick record found for #{pick_number}.")
        return
    if rec.get("locked"):
        await ctx.send(f"❌ Pick #{pick_number} (**{rec['player_name']}**) is already locked.")
        return

    team = s.draft.teams[rec["team_idx"]]
    if team.get("locks_remaining", SBL_LOCKS_PER_TEAM) <= 0:
        await ctx.send(f"❌ **{team['name']}** has no lock charges remaining.")
        return

    rec["locked"] = True
    team["locks_remaining"] = team.get("locks_remaining", SBL_LOCKS_PER_TEAM) - 1
    s.draft.save(s.channel_id)

    log.info("MANUAL LOCK | ch=%d | Pick #%d | Player: %s | Team: %s",
              s.channel_id, pick_number, rec["player_name"], team["name"])
    await ctx.send(
        f"🔒 Pick **#{pick_number}** (**{rec['player_name']}**) is now locked — "
        f"immune to steal/block for the rest of the draft. Charge spent by **{team['name']}**."
    )


@bot.command(name="timersblhelp")
async def timersblhelp(ctx):
    """!timersblhelp — explain the Steal/Block/Lock ruleset."""
    embed = discord.Embed(title="🎯 Steal / Block / Lock", color=discord.Color.gold())
    embed.add_field(
        name="Enabling",
        value=(
            "`!timermode snake+sbl` / `!timermode roundless+sbl` — switch an active draft\n"
            "`!timerstart snake+sbl` / `!timerstart roundless+sbl` — start a draft with SBL on"
        ),
        inline=False,
    )
    embed.add_field(
        name="Charges (per GM, for the whole draft)",
        value=f"🔓 {SBL_STEALS_PER_TEAM} steals · 🚫 {SBL_BLOCKS_PER_TEAM} blocks · 🔒 {SBL_LOCKS_PER_TEAM} lock",
        inline=False,
    )
    embed.add_field(
        name="Steal — on your own turn",
        value=(
            "Type a message containing \"steal\"/\"stolen\" and the player's name "
            "(e.g. `steal LeBron James`), or just reply \"steal\" to their pick message. "
            "Only works on your own turn, on a player picked in the **current round** "
            "(one pick per team), who isn't locked or protected. The victim goes "
            "back on the clock immediately after your pick."
        ),
        inline=False,
    )
    embed.add_field(
        name="Block — anytime, any GM",
        value=(
            "Type a message containing \"block\" and the player's name "
            "(e.g. `block LeBron James`). Not turn-gated — usable anytime a target is "
            "eligible. The player is removed entirely (goes back into the pool) and the "
            "victim GM is queued for an emergency repick.\n"
            "**Exception:** if you block a player who was stolen *from you*, you get them "
            "back on your own roster directly instead of them going to the pool."
        ),
        inline=False,
    )
    embed.add_field(
        name="Lock — when you make your own pick",
        value="Append 🔒 (or the word \"lock\") to your numbered pick to spend a lock charge and make it permanently safe from steal/block.",
        inline=False,
    )
    embed.add_field(
        name="Rules",
        value=(
            "• A pick can only be hit (blocked/stolen) once — the resulting repick is permanently safe.\n"
            "• If you're blocked/stolen from, your next pick must be an original pick (no steal).\n"
            "• If your steal gets blocked, you get the steal charge back. If it gets re-stolen, you don't.\n"
            "• A stolen player keeps their original price."
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin corrections",
        value=(
            "`!timersbladjust @GM steals|blocks|locks <n>` — set a GM's remaining charges\n"
            "`!timerlock <pick#>` — manually lock a pick that the 🔒 marker missed, spends the charge\n"
            "`!timerunlock <pick#>` — remove a mistaken lock, refunds the charge\n"
            "`!timersblfor @GM block/steal/lock <player>` — perform a steal/block/lock on behalf of "
            "an absentee GM, charged against THEIR remaining charges (not yours)\n"
            "`!timerreassignpick <pick#> @GM` — move an existing pick to a different GM's roster "
            "(moves the price too, marks it stolen-from the old owner)\n"
            "`!timerforcequeue @GM <pick#>` — force a priority makeup turn for @GM, ahead of the "
            "normal rotation, without a real steal/block to trigger it\n"
            "`!timerclearqueue @GM` / `!timerclearqueue all` — remove entries from the repick queue\n"
            "`!timerqueuestatus` — show the current repick queue"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


# ── Admin ─────────────────────────────────────────────────────────────────────

@bot.command(name="timerpause")
@is_commissioner()
async def timerpause(ctx):
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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
    s = await _resolve_command_session(ctx)
    if s is None:
        return

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


@bot.command(name="timerdm")
async def timerdm(ctx, action: str = ""):
    """!timerdm on|off|status — opt in/out of DM pings for your turn to pick.
    Works the same in DMs with the bot or in a server channel. Global across
    every draft you're in — no need to toggle it per-channel."""
    action = action.lower().strip()
    uid    = str(ctx.author.id)
    prefs  = _load_dm_prefs()

    if action == "on":
        prefs[uid] = True
        _save_dm_prefs(prefs)
        await ctx.send(
            "✅ Pick-turn DMs are now **ON**. I'll message you here whenever it's "
            "your turn to pick, across every draft you're in. Turn it off anytime with `!timerdm off`."
        )
    elif action == "off":
        prefs[uid] = False
        _save_dm_prefs(prefs)
        await ctx.send("🔕 Pick-turn DMs are now **OFF**.")
    elif action == "status":
        state = "**ON** ✅" if prefs.get(uid, False) else "**OFF** 🔕"
        await ctx.send(f"Your pick-turn DMs are currently {state}.")
    else:
        state = "**ON** ✅" if prefs.get(uid, False) else "**OFF** 🔕"
        await ctx.send(
            "**Usage:** `!timerdm on` / `!timerdm off` / `!timerdm status`\n"
            f"Your pick-turn DMs are currently {state}."
        )


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
            "`!lottery` — Reply to a drafter list to shuffle it into a fresh random lotto (standalone, doesn't load a draft)\n"
            "`!timergmlotto <n> @GM1 @GM2 …` — Auto-build lotto: each GM gets <n> slots, randomly shuffled\n"
            "`!timerlottoupdate` — Re-read lotto to update GM rosters (preserves picks)\n"
            "`!timersetup @u1 @u2 …` — Manually register participants\n"
            "`!timerlotto` — Randomly shuffle draft order\n"
            "`!timerorder 3 1 2 …` — Set draft order manually\n"
            "`!timermode snake|roundless|snake+sbl|roundless+sbl|snake+budget` — Switch draft mode\n"
            "`!timercheckmode` — Show the current draft mode (read-only, anyone can run it)\n"
            "`!timerstart [mode] [label]` — Begin the draft (mode defaults to snake)\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="During draft",
        value=(
            "`!timerstatus` — Show current pick and time remaining\n"
            "`!timerboard` — Show all picks so far\n"
            "`!timernext [count]` — Preview the next N upcoming picks (default 10)\n"
            "`!nextpick @GM` — Show one GM's pick number in every upcoming round (snake drafts only)\n"
            "`!timerdrafts` — List every currently active draft and its channel (works anywhere)\n"
            "`!timerskip` — Skip your turn (−5 min on future picks)\n"
            "`!timerunskip` — Undo the last skip\n"
            "`!timerskiplist` — Show all teams' skip penalties\n"
            "`!timerskiphistory [@user]` — All-time skip leaderboard or per-GM history\n"
            "`!avgtimepicker [@user]` — All-time avg time-to-pick leaderboard, or one GM's history (quickest first, works anywhere)\n"
            "`challenge` (reply in atd-chat) — Cut current GM's timer to 10 min (3 = instant skip)\n"
            "`!timerdm on|off|status` — Opt in/out of DMs when it's your turn (works in DM or server, global across all your drafts)\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin",
        value=(
            "`!timerpause` / `!timerresume` — Pause/resume the draft\n"
            "`!timerjumpto <pick#>` — Jump to a specific pick number\n"
            "`!timerlottoreroll` — Draw a brand new lottery for the current round onward (snake only, at a round's start)\n"
            "`!timersetpick <pick#> @GM` — Jump and force a specific GM next\n"
            "`!timerslotedit <slot#> @u1 @u2` — Replace a lotto slot's owners (by fixed roster position)\n"
            "`!timerpickedit <pick#> @u1 @u2` — Replace whoever's actually scheduled for a pick number (accounts for rerolls)\n"
            "`!timersettimer <min>` — Override timer (0 = revert to defaults)\n"
            "`!timeraddowner <slot#> @user` — Add a co-GM to a specific lotto slot\n"
            "`!timerremoveowner <slot#> @user` — Remove a co-GM from a specific lotto slot\n"
            "`!timerproxy @user` / `!timerremoveproxy @user` — Add/remove a proxy picker\n"
            "`!timeraddskip @GM [n]` / `!removeskip @GM` — Add/remove skips\n"
            "`!timerset @GM <money> <picks> <last#>` — Set all roundless stats at once\n"
            "`!timersetmoney` / `!timersetpicks` / `!timersetlastpick` — Set individual stats\n"
            "`!timeraddpick @GM <pick#> <player ...>` — Manually record a pick the bot missed\n"
            "`!timerreplacepick @GM <pick#> <player ...>` — Overwrite a wrongly-recorded pick\n"
            "`!timersetbudget <amount>` — Cap total spend per team (omit amount to clear)\n"
            "`!timereset` — Cancel and wipe this channel's draft\n"
        ),
        inline=False,
    )
    if s.draft.order_mode == "roundless" and s.draft.state != "idle":
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
    if s.draft.sbl_enabled:
        embed.add_field(
            name="Steal / Block / Lock",
            value="`!timersblstatus` — remaining charges · `!timersblhelp` — full rules",
            inline=False,
        )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not set in .env")
    else:
        print("🚀 Starting ATD Timer Bot (multi-channel)…")
        bot.run(DISCORD_TOKEN)
