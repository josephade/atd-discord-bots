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
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from config import AS_THRESHOLD, DISCORD_TOKEN, DRAFT_CHANNEL_ID, LOTTO_CHANNEL_ID, PENALTY_PLAYERS, ROUNDS
from draft import DraftState, build_snake_order

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
_timer_task:  asyncio.Task | None = None
_window_task: asyncio.Task | None = None

# ── Draft window (Eastern Time) ───────────────────────────────────────────────
# Picks are only pinged/timed between 10am ET and midnight ET.
# Outside that window the timer auto-pauses and resumes at 10am.

_ET            = ZoneInfo("America/New_York")
_WINDOW_START  = 10   # 10:00 AM ET (inclusive)
_WINDOW_END    = 0    # midnight ET (exclusive — i.e. hours 0-9 are outside)


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


def _extract_player_name(raw: str) -> str:
    text = re.sub(r'<:[^:]+:\d+>', '', raw).strip()
    text = re.sub(r'^:[^:\s]+:\s*', '', text).strip()
    text = re.sub(r'\s+\d{4}(-\d{2,4})?$', '', text).strip()
    return text


def _team_mentions(team: dict) -> str:
    """Return mention string for all co-owners of a team slot."""
    return " ".join(f"<@{uid}>" for uid in team["user_ids"])


def _is_team_owner(user_id: int, team: dict) -> bool:
    return user_id in team["user_ids"]


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

async def _ping_current(channel: discord.TextChannel):
    team     = draft.current_team
    duration = draft.effective_timer(draft.round_number, draft.current_team_idx)
    minutes  = duration // 60

    log.info(
        "PING | Round %d Pick %d (overall #%d) | Team: %s | Timer: %d min",
        draft.round_number, draft.pick_in_round, draft.overall_pick,
        team["name"], minutes,
    )

    embed = discord.Embed(
        title=f"Round {draft.round_number} of {ROUNDS}  —  Pick {draft.overall_pick}",
        description=(
            f"{_team_mentions(team)} it's your turn!\n\n"
            f"You have **{minutes} minutes** to pick.\n\n"
            f"Type your pick in this channel:\n"
            f"`{draft.overall_pick}. :YourEmoji: Player Name Year`"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use !timerskip to pass (costs 10 min on future picks).")
    # Mention must be in message content (not just the embed) to trigger Discord push notifications
    await channel.send(content=_team_mentions(team), embed=embed)


async def _auto_pause_for_window(remaining: float):
    """Pause the timer because the draft window just closed (midnight ET)."""
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
    log.info("WINDOW PAUSE | Team: %s | Remaining: %dm %ds", team["name"], mins, s)
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

    embed = discord.Embed(
        title=f"Round {draft.round_number} of {ROUNDS}  —  Pick {draft.overall_pick}",
        description=(
            f"{_team_mentions(team)} it's your turn!\n\n"
            f"You have **{mins}m {s}s** left to pick.\n\n"
            f"Type your pick in this channel:\n"
            f"`{draft.overall_pick}. :YourEmoji: Player Name Year`"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use !timerskip to pass (costs 10 min on future picks).")
    await channel.send(content=f"☀️ **Draft window open!** {_team_mentions(team)}", embed=embed)

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

    # ── 10-min warning ────────────────────────────────────────────────────────
    if duration > 600:
        ok = await _checked_sleep(duration - 600)
        if not ok:
            if _still_their_turn():
                await _auto_pause_for_window(_remaining())
            return
        if _still_their_turn():
            log.info("WARNING | 10 min remaining | Team: %s", draft.current_team["name"] if draft.current_team else "?")
            await channel.send(f"⚠️ {mentions} — **10 minutes remaining** to pick!")

    # ── 5-min warning ─────────────────────────────────────────────────────────
    if duration > 300:
        ok = await _checked_sleep(duration - 300)
        if not ok:
            if _still_their_turn():
                await _auto_pause_for_window(_remaining())
            return
        if _still_their_turn():
            log.info("WARNING | 5 min remaining | Team: %s", draft.current_team["name"] if draft.current_team else "?")
            await channel.send(f"⚠️ {mentions} — **5 minutes remaining**!")

    # ── Final countdown ───────────────────────────────────────────────────────
    ok = await _checked_sleep(duration)
    if not ok:
        if _still_their_turn():
            await _auto_pause_for_window(_remaining())
        return

    if _still_their_turn():
        log.info("TIMEOUT | Auto-skip triggered | Team: %s", draft.current_team["name"] if draft.current_team else "?")
        await channel.send(f"⏰ Time's up for {mentions}! Auto-skipping…")
        await _do_skip(auto=True)


async def _start_timer():
    global _timer_task
    # Don't cancel the timer task if we're being called from within it —
    # that would raise CancelledError here and prevent the next ping from sending.
    current = asyncio.current_task()
    if _timer_task and not _timer_task.done() and _timer_task is not current:
        _timer_task.cancel()

    team = draft.current_team
    if not team or draft.state != "active":
        return

    channel = bot.get_channel(DRAFT_CHANNEL_ID)

    # Outside draft window — pause immediately instead of starting a countdown
    if not _in_window():
        duration = draft.effective_timer(draft.round_number, draft.current_team_idx)
        await _auto_pause_for_window(duration)
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

    team["skip_count"] = team.get("skip_count", 0) + 1
    label = "⏩ Auto-skipped" if auto else "⏩ Skipped"
    log.info(
        "SKIP | %s | Team: %s | Total skips: %d",
        "auto (timeout)" if auto else "manual",
        team["name"], team["skip_count"],
    )

    draft.advance()
    draft.timer_start = None
    draft.save()

    channel = bot.get_channel(DRAFT_CHANNEL_ID)
    await channel.send(f"{label} - **-10 min** applied to {team['name']}'s future picks.")

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

    if draft.state == "window_paused" and draft.current_team:
        # Bot restarted while window-paused — reschedule the wakeup
        team = draft.current_team
        remaining = draft.paused_remaining or draft.effective_timer(draft.round_number, draft.current_team_idx)
        mins, s = remaining // 60, remaining % 60
        log.info("RESTART | Window-paused | Team: %s | Remaining: %dm %ds", team["name"], mins, s)
        await channel.send(
            f"🔄 Bot restarted — draft window is closed. {_team_mentions(team)} has **{mins}m {s}s** remaining.\n"
            f"Timer will resume at **10:00 AM ET**."
        )
        global _window_task
        _window_task = asyncio.create_task(_window_resume_task(_secs_until_open()))

    elif draft.state == "active" and draft.timer_start and draft.current_team:
        elapsed   = (datetime.now(timezone.utc) - datetime.fromisoformat(draft.timer_start)).total_seconds()
        duration  = draft.effective_timer(draft.round_number, draft.current_team_idx)
        remaining = duration - elapsed

        team     = draft.current_team
        mentions = _team_mentions(team)

        if remaining <= 0:
            await channel.send(f"🔄 Bot restarted — {mentions}'s time had already expired. Auto-skipping…")
            await _do_skip(auto=True)
        else:
            await channel.send(f"🔄 Bot restarted — {mentions} has **{int(remaining // 60)} min** remaining.")
            global _timer_task
            _timer_task = asyncio.create_task(_timer_loop(int(remaining), team["user_ids"]))


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.author.bot:
        return
    if message.channel.id != DRAFT_CHANNEL_ID:
        return
    if draft.state not in ("active", "paused", "window_paused"):
        return

    match = _PICK_RE.match(message.content.strip())
    if not match:
        return

    pick_num = int(match.group(1))
    pick_raw = match.group(2).strip()

    if pick_num != draft.overall_pick:
        return

    team = draft.current_team

    is_commissioner_pick = (
        message.author.guild_permissions.administrator
        or any(r.name == COMMISSIONER_ROLE for r in message.author.roles)
    )
    if not _is_team_owner(message.author.id, team) and not is_commissioner_pick:
        await message.reply(f"❌ It's not your turn — waiting on {_team_mentions(team)}.")
        return

    # ── Valid pick ────────────────────────────────────────────────────────────
    log.info(
        "PICK | Overall #%d | Round %d Pick %d | Team: %s | Player: %s",
        draft.overall_pick, draft.round_number, draft.pick_in_round,
        team["name"], pick_raw,
    )
    global _timer_task, _window_task
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
    if _window_task and not _window_task.done():
        _window_task.cancel()
    # If pick was made during window_paused, restore active so advance() works
    if draft.state == "window_paused":
        draft.state            = "active"
        draft.paused_remaining = None

    team["picks"].append(pick_raw)

    player_name  = _extract_player_name(pick_raw)
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

    await message.add_reaction("✅")

    if penalty_note:
        await message.channel.send(penalty_note)

    if draft.state == "complete":
        await message.channel.send("🏆 **Draft complete! Great picks everyone.**")
        return

    await _start_timer()


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
    if draft.state not in ("lotto", "active"):
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


@bot.command(name="timerstart")
@is_commissioner()
async def timerstart(ctx):
    if draft.state != "lotto":
        await ctx.send("❌ Load a lotto first with `!timerloadlotto` or `!timerlotto`.")
        return

    draft.state            = "active"
    draft.current_round    = 0
    draft.current_in_round = 0
    draft.save()

    await ctx.send("🏀 **The draft has started!**")
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

    total_picks = draft.num_teams * ROUNDS
    if pick_number < 1 or pick_number > total_picks:
        await ctx.send(f"❌ Pick number must be between 1 and {total_picks}.")
        return

    # Convert overall pick number to round + position within round
    zero_pick      = pick_number - 1
    new_round      = zero_pick // draft.num_teams
    new_in_round   = zero_pick % draft.num_teams

    draft.current_round    = new_round
    draft.current_in_round = new_in_round
    draft.paused_remaining = None
    draft.timer_start      = None
    draft.state            = "active"
    draft.save()

    team = draft.current_team
    log.info("JUMP | To pick %d | Round %d | In-round %d | Team: %s",
             pick_number, draft.round_number, draft.pick_in_round, team["name"] if team else "?")
    await ctx.send(
        f"⏩ Jumped to **pick #{pick_number}** (Round {draft.round_number}, pick {draft.pick_in_round}).\n"
        f"Up now: {_team_mentions(team) if team else '?'}\nStarting timer…"
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

    embed = discord.Embed(
        title=f"Draft Status — Round {draft.round_number} of {ROUNDS}",
        color=discord.Color.dark_gray() if draft.state == "window_paused" else discord.Color.orange() if draft.state == "paused" else discord.Color.blue(),
    )
    embed.add_field(name="Overall Pick",  value=str(draft.overall_pick),   inline=True)
    embed.add_field(name="Pick in Round", value=str(draft.pick_in_round),   inline=True)
    embed.add_field(name="Up Now",        value=_team_mentions(team),        inline=True)
    embed.add_field(name="Time Left",     value=time_left,                   inline=True)
    embed.add_field(name="Base Timer",    value=f"{duration // 60} min",     inline=True)

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
    """!timerskiplist — show each team's skip count and resulting timer per round"""
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
        is_as    = draft.is_active_skip(i)
        deduction = skips * SKIP_PENALTY
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
        as_tag = " 🔴 **ACTIVE SKIP**" if is_as else ""
        embed.add_field(
            name=f"{team['name']} — {skips} skip(s) (−{skips * 10} min){as_tag}",
            value="\n".join(lines),
            inline=True,
        )

    if not any_skips:
        embed.description = "No skips recorded yet."

    await ctx.send(embed=embed)


@bot.command(name="timerboard")
async def timerboard(ctx):
    if draft.state not in ("active", "paused", "window_paused", "complete"):
        await ctx.send("❌ No draft in progress.")
        return

    embed = discord.Embed(title="Draft Board", color=discord.Color.dark_blue())
    for team in draft.teams:
        picks     = team.get("picks", [])
        pick_text = "\n".join(f"{j+1}. {p}" for j, p in enumerate(picks)) if picks else "_No picks yet_"
        embed.add_field(name=team["name"], value=pick_text, inline=True)

    await ctx.send(embed=embed)


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
        "`!timerstart` - begin the draft after the lotto is loaded\n"
        "`!timerpause` - freeze the clock mid-pick\n"
        "`!timerresume` - resume from where it was paused\n"
        "`!timerjumpto <pick#>` - jump to a specific pick (use when picks were made before the bot started)\n"
        "`!timereset` - cancel and wipe the entire draft\n"
        "`!timerproxy @user` - let someone else pick for the current team while their GM is away\n"
        "`!timerremoveproxy @user` - remove a proxy once the GM is back"
    ), inline=False)

    embed.add_field(name="📋 During the Draft", value=(
        "`!timerskip` - skip your turn (costs **-10 min** on all your future picks)\n"
        "`!timerstatus` - show current round, pick number, who's up, and time remaining\n"
        "`!timerskiplist` - show every team's skip count and their adjusted timer per round\n"
        "`!timerboard` - show all picks made so far"
    ), inline=False)

    embed.add_field(name="⏱️ Round Timers", value=(
        "R1–2: **60 min**\n"
        "R3–8: **45 min**\n"
        "R9–10: **30 min**\n"
        "Each skip deducts **10 min** from all of that team's future picks."
    ), inline=True)

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

    embed.set_footer(text="Warnings fire at 10 min and 5 min remaining. Auto-skip triggers on timeout.")
    await ctx.send(embed=embed)


# ── Run ───────────────────────────────────────────────────────────────────────

bot.run(DISCORD_TOKEN)
