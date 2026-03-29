import asyncio
import difflib
import re
import time

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, DRAFT_CHANNEL_ID, ROUNDS, PICK_TIMEOUT_SECONDS
from draft_manager import DraftManager, DraftState
from player_data import get_pool_category
import ai_drafter
from feedback import db as fdb
from feedback import proposer as fproposer
from feedback.analyzer import REASON_LABELS

# Candidate emoji names to try for each NBA team (checked in order, case-insensitive).
# Add more variants here if you upload emojis under different names.
_TEAM_EMOJI_CANDIDATES: dict[str, list[str]] = {
    "Atlanta Hawks":           ["Hawks", "Atlanta", "hawks"],
    "Boston Celtics":          ["Celtics", "Boston"],
    "Brooklyn Nets":           ["Nets", "Brooklyn"],
    "Charlotte Hornets":       ["Hornets", "Charlotte", "Bobcats"],
    "Chicago Bulls":           ["Bulls", "Chicago"],
    "Cleveland Cavaliers":     ["Cavs", "Cavaliers", "Cleveland"],
    "Dallas Mavericks":        ["Mavericks", "Mavs", "Dallas"],
    "Denver Nuggets":          ["Nuggets", "Denver", "nuggets"],
    "Detroit Pistons":         ["Piston", "Pistons", "Detroit", "pistons"],
    "Golden State Warriors":   ["Warriors", "GSW"],
    "Houston Rockets":         ["Rockets", "Houston", "rockets"],
    "Indiana Pacers":          ["Pacers", "Indiana"],
    "Los Angeles Clippers":    ["Clippers", "LAC"],
    "Los Angeles Lakers":      ["Lakers", "LAL"],
    "Memphis Grizzlies":       ["Grizzlies", "Memphis"],
    "Miami Heat":              ["Heat", "Miami", "heat"],
    "Milwaukee Bucks":         ["Bucks", "Milwaukee", "Milwaukee_Bucks"],
    "Minnesota Timberwolves":  ["Minn", "Timberwolves", "Minnesota", "min", "MIN"],
    "New Orleans Pelicans":    ["Pelicans", "NewOrleans", "pelicans"],
    "New York Knicks":         ["Knicks", "NYK", "NewYork"],
    "Oklahoma City Thunder":   ["OKC", "Thunder", "Oklahoma"],
    "Orlando Magic":           ["Magic", "Orlando"],
    "Philadelphia 76ers":      ["76ers", "Sixers", "Philadelphia"],
    "Phoenix Suns":            ["Suns", "Phoenix"],
    "Portland Trail Blazers":  ["Blazers", "TrailBlazers", "Portland", "trailblazers"],
    "Sacramento Kings":        ["Kings", "Sacramento", "kings"],
    "San Antonio Spurs":       ["Spurs", "SanAntonio", "San_Antonio_Spurs"],
    "Toronto Raptors":         ["Raptors", "Toronto"],
    "Utah Jazz":               ["Jazz", "Utah"],
    "Washington Wizards":      ["Wizards", "Washington"],
}

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# One DraftManager per channel/thread — keyed by channel ID.
# Cleaned up when a draft finishes or is cancelled.
_drafts: dict[int, DraftManager] = {}
_draft_tasks: dict[int, asyncio.Task] = {}


# ── Channel guard ─────────────────────────────────────────────────────────────

def _is_draft_channel(channel: discord.abc.MessageableChannel) -> bool:
    """
    Returns True if the channel is allowed to run drafts:
      - the main DRAFT_CHANNEL_ID channel, OR
      - any thread whose parent channel is DRAFT_CHANNEL_ID.
    """
    if channel.id == DRAFT_CHANNEL_ID:
        return True
    if isinstance(channel, discord.Thread) and channel.parent_id == DRAFT_CHANNEL_ID:
        return True
    return False


_WEIGHT_ROLES = {"ATD Bot Developer", "ATD Bot Tester"}

def _has_weight_role(member: discord.Member) -> bool:
    """Returns True if the member has at least one of the weight-management roles."""
    return any(r.name in _WEIGHT_ROLES for r in member.roles)


def _get_draft(channel_id: int) -> DraftManager:
    """Return the DraftManager for this channel, creating one if needed."""
    if channel_id not in _drafts:
        _drafts[channel_id] = DraftManager()
    return _drafts[channel_id]


def _save_draft_to_db(dm: DraftManager, started_by: str | None = None) -> int | None:
    """Persist all team rosters to the feedback DB. Returns draft_id or None on error."""
    try:
        teams = {team.name: list(team.picks) for team in dm.teams}
        return fdb.save_draft(num_teams=len(dm.teams), teams=teams, started_by=started_by)
    except Exception as exc:
        print(f"[Feedback] Failed to save draft: {exc}")
        return None


def _remove_draft(channel_id: int) -> None:
    _drafts.pop(channel_id, None)
    task = _draft_tasks.pop(channel_id, None)
    if task and not task.done():
        task.cancel()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_player(text: str, pool: list[str], cutoff: float = 0.6) -> str | None:
    """
    Fuzzy-match `text` against `pool`.
    Returns the best match or None if confidence is below `cutoff`.
    """
    text = text.strip()
    for name in pool:
        if name.lower() == text.lower():
            return name
    matches = difflib.get_close_matches(text, pool, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def _parse_pick_message(content: str) -> str | None:
    """
    Extract player name from a pick message of the form:
        <number>. <anything> <PlayerName>
    Everything after the first token following '.' is the candidate name.
    Strips trailing year patterns and punctuation.
    """
    # Remove leading pick number "54." or "54 ."
    content = re.sub(r'^\s*\d+\s*\.?\s*', '', content).strip()

    # Remove Discord custom emoji <:name:id>
    content = re.sub(r'<a?:\w+:\d+>', '', content).strip()

    # Strip common year patterns at the end
    content = re.sub(r"'?\d{2}-\d{2}\b.*$", '', content, flags=re.IGNORECASE).strip()
    content = re.sub(r'\b\d{4}-\d{4}\b.*$', '', content).strip()
    content = re.sub(r'\b\d{4}-\d{2}\b.*$', '', content).strip()
    content = re.sub(r"\b'?\d{2}\b.*$", '', content).strip()

    # Strip trailing punctuation
    content = content.strip('.,;:()')

    return content if content else None


def _team_roster_embed(team, pick_num: int, total_picks: int) -> discord.Embed:
    """Embed showing a team's current picks."""
    lines = [f"{i+1}. {p}" for i, p in enumerate(team.picks)] or ["*(no picks yet)*"]
    e = discord.Embed(
        title=f"{team.emoji} {team.name}",
        description="\n".join(lines),
        color=0x1a73e8,
    )
    e.set_footer(text=f"Pick {pick_num} of {total_picks}")
    return e


async def _announce_pick(channel, pick_num: int, team, player: str, auto: bool = False):
    """Send the official pick announcement in the ATD pick format."""
    suffix = " *(auto-pick — time expired)*" if auto else ""
    await channel.send(f"**{pick_num}.** {team.emoji} {player}{suffix}")


async def _run_draft(channel: discord.TextChannel, dm: DraftManager):
    """
    Core draft loop.  Runs until all picks are complete, then writes results.
    Each thread/channel gets its own DraftManager passed in directly.
    """
    total = dm.total_picks

    while not dm.is_complete():
        team     = dm.current_team
        pick_num = dm.pick_number
        rnd      = dm.round_number

        # ── Round header ────────────────────────────────────────────────
        if (pick_num - 1) % dm.total_teams == 0:
            await channel.send(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏀 **Round {rnd} of {ROUNDS}**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

        available = dm.available_players

        # ── AI pick ──────────────────────────────────────────────────────
        if team.is_ai:
            await asyncio.sleep(2)   # brief pause for realism
            player = ai_drafter.pick(
                team.picks, available,
                player_adp=dm.player_adp,
                pool_size=len(dm.player_pool),
                overall_pick=dm.pick_number,
                num_teams=dm.total_teams,
            )
            dm.record_pick(player)
            print(f"[Draft:{channel.id}] Pick #{pick_num:>3} | AI   | {team.name:<28} | {player}")
            await _announce_pick(channel, pick_num, team, player)
            continue

        # ── Human pick ───────────────────────────────────────────────────
        print(f"[Draft:{channel.id}] Pick #{pick_num:>3} | Human | {team.name:<28} | waiting…")
        await channel.send(
            f"🎯 Pick **#{pick_num}** — {team.emoji} **{team.name}** "
            f"(<@{team.owner_id}>)\n"
            f"*You have {PICK_TIMEOUT_SECONDS}s. Format:* `{pick_num}. {team.emoji} Player Name`"
        )

        def _is_valid_pick(msg: discord.Message) -> bool:
            return (
                msg.channel.id == channel.id
                and msg.author.id == team.owner_id
                and bool(re.match(r'^\s*\d+', msg.content))
            )

        # Retry loop — user gets unlimited retries within the timeout window.
        player = None
        deadline = asyncio.get_event_loop().time() + PICK_TIMEOUT_SECONDS

        while player is None:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break

            try:
                msg = await bot.wait_for('message', check=_is_valid_pick, timeout=remaining)
            except asyncio.TimeoutError:
                break

            raw_name = _parse_pick_message(msg.content)
            if not raw_name:
                continue

            # Check if the player exists in the available pool first.
            # Do this before the already-drafted check so that an exact (or
            # near-exact) match in the available pool is never incorrectly
            # flagged as a different already-drafted player with a similar name
            # (e.g. "Gus Johnson" being fuzzy-matched to "Marques Johnson").
            player = _resolve_player(raw_name, available)
            if player:
                # Valid pick — exit loop
                break

            # Not in the available pool — check if it's already drafted
            already = _resolve_player(raw_name, list(dm.drafted), cutoff=0.75)
            if already:
                await channel.send(
                    f"❌ **{already}** has already been drafted. Please choose another player."
                )
                continue

            await channel.send(
                f"⚠️ **{raw_name}** not found in the player pool. Please re-enter your pick."
            )

        if player:
            dm.record_pick(player)
            print(f"[Draft:{channel.id}] Pick #{pick_num:>3} | Human | {team.name:<28} | {player}")
            await _announce_pick(channel, pick_num, team, player)
        else:
            player = ai_drafter.pick(team.picks, available, player_adp=dm.player_adp, pool_size=len(dm.player_pool), overall_pick=dm.pick_number, num_teams=dm.total_teams)
            dm.record_pick(player)
            print(f"[Draft:{channel.id}] Pick #{pick_num:>3} | Auto  | {team.name:<28} | {player} (timeout)")
            await _announce_pick(channel, pick_num, team, player, auto=True)

    # ── Draft complete ────────────────────────────────────────────────────
    dm.state = DraftState.COMPLETE
    await channel.send("✅ **Draft complete!** Writing results to Google Sheets…")

    try:
        tab = dm.write_results()
        await channel.send(f"📋 Results saved to tab **`{tab}`**.")
    except Exception as e:
        await channel.send(f"❌ Failed to write results: {e}")

    draft_id = _save_draft_to_db(dm, started_by=dm.started_by)
    if draft_id:
        await channel.send(
            f"📊 Draft #{draft_id} saved for review. "
            f"Run `!draftreview` when ready to evaluate teams."
        )

    # Show final rosters
    for team in dm.teams:
        embed = discord.Embed(
            title=f"{team.emoji} {team.name}",
            color=0x2ecc71,
        )
        owner_tag = "🤖 AI" if team.is_ai else f"<@{team.owner_id}>"
        embed.set_author(name=owner_tag)
        for i, p in enumerate(team.picks, 1):
            embed.add_field(name=f"Pick {i}", value=p, inline=True)
        await channel.send(embed=embed)

    # Clean up — free the slot so a new draft can start in this thread
    _remove_draft(channel.id)


async def _run_draft_sim(channel: discord.TextChannel, dm: DraftManager):
    """
    Fast-sim all remaining picks to the end.
    Every team is treated as AI — no delays, no waiting for human input.
    """
    while not dm.is_complete():
        team     = dm.current_team
        pick_num = dm.pick_number
        rnd      = dm.round_number

        if (pick_num - 1) % dm.total_teams == 0:
            await channel.send(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏀 **Round {rnd} of {ROUNDS}**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

        available = dm.available_players
        player = ai_drafter.pick(
            team.picks, available,
            player_adp=dm.player_adp,
            pool_size=len(dm.player_pool),
            overall_pick=dm.pick_number,
            num_teams=dm.total_teams,
        )
        dm.record_pick(player)
        print(f"[Sim:{channel.id}] Pick #{pick_num:>3} | {team.name:<28} | {player}")
        await _announce_pick(channel, pick_num, team, player)

    dm.state = DraftState.COMPLETE
    await channel.send("✅ **Sim complete!** Writing results to Google Sheets…")

    try:
        tab = dm.write_results()
        await channel.send(f"📋 Results saved to tab **`{tab}`**.")
    except Exception as e:
        await channel.send(f"❌ Failed to write results: {e}")

    draft_id = _save_draft_to_db(dm, started_by=dm.started_by)
    if draft_id:
        await channel.send(
            f"📊 Draft #{draft_id} saved for review. "
            f"Run `!draftreview` when ready to evaluate teams."
        )

    for team in dm.teams:
        embed = discord.Embed(title=f"{team.emoji} {team.name}", color=0x2ecc71)
        owner_tag = "🤖 AI" if team.is_ai else f"<@{team.owner_id}>"
        embed.set_author(name=owner_tag)
        for i, p in enumerate(team.picks, 1):
            embed.add_field(name=f"Pick {i}", value=p, inline=True)
        await channel.send(embed=embed)

    _remove_draft(channel.id)


# ── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name='draft')
async def draft_cmd(ctx: commands.Context):
    """Start an ATD draft session in this channel or thread."""
    if not _is_draft_channel(ctx.channel):
        return

    dm = _get_draft(ctx.channel.id)

    if dm.state != DraftState.IDLE:
        await ctx.send("⚠️ A draft is already in progress here. Use `!draftcancel` to cancel it.")
        return

    def _same_author(m: discord.Message) -> bool:
        return m.author == ctx.author and m.channel == ctx.channel

    # ── Step 1: Mode ──────────────────────────────────────────────────────
    await ctx.send(
        "🏀 **ATD Draft Setup**\n"
        "Choose a mode:\n"
        "`1` — **Standard** — each human controls 1 team\n"
        "`2` — **Multi-team** — you control multiple teams and compete vs AI\n"
        "`3` — **Watch** — fully AI draft, no human teams"
    )
    try:
        msg = await bot.wait_for('message', check=_same_author, timeout=120)
        mode = int(msg.content.strip())
        if mode not in (1, 2, 3):
            raise ValueError
    except (asyncio.TimeoutError, ValueError):
        await ctx.send("❌ Invalid input — draft cancelled.")
        _remove_draft(ctx.channel.id)
        return

    # ── Step 2: Total teams ───────────────────────────────────────────────
    await ctx.send("How many total teams? *(2–32)*")
    try:
        msg = await bot.wait_for('message', check=_same_author, timeout=120)
        total_teams = int(msg.content.strip())
        if not 2 <= total_teams <= 32:
            raise ValueError
    except (asyncio.TimeoutError, ValueError):
        await ctx.send("❌ Invalid input — draft cancelled.")
        _remove_draft(ctx.channel.id)
        return

    # ── Step 3: Resolve human_ids based on mode ───────────────────────────
    if mode == 3:
        # Watch mode — all AI
        human_ids = []

    elif mode == 2:
        # Multi-team — one human controls multiple teams
        await ctx.send(
            f"Got it — **{total_teams} teams**!\n"
            f"How many teams do **you** want to control? *(1–{total_teams - 1})*"
        )
        try:
            msg = await bot.wait_for('message', check=_same_author, timeout=120)
            my_team_count = int(msg.content.strip())
            if not 1 <= my_team_count <= total_teams - 1:
                raise ValueError
        except (asyncio.TimeoutError, ValueError):
            await ctx.send("❌ Invalid input — draft cancelled.")
            _remove_draft(ctx.channel.id)
            return
        human_ids = [ctx.author.id] * my_team_count

    else:
        # Standard mode — one ID per human player
        await ctx.send(
            f"Got it — **{total_teams} teams**!\n"
            f"How many will be **human players**? *(0–{total_teams})*"
        )
        try:
            msg = await bot.wait_for('message', check=_same_author, timeout=120)
            human_count = int(msg.content.strip())
            if not 0 <= human_count <= total_teams:
                raise ValueError
        except (asyncio.TimeoutError, ValueError):
            await ctx.send("❌ Invalid input — draft cancelled.")
            _remove_draft(ctx.channel.id)
            return

        if human_count == 0:
            human_ids = []
        elif human_count == 1:
            human_ids = [ctx.author.id]
        else:
            await ctx.send(
                f"Tag the **{human_count} human players** "
                f"(e.g. `@User1 @User2 ...`):"
            )
            try:
                msg = await bot.wait_for('message', check=_same_author, timeout=120)
                human_ids = [m.id for m in msg.mentions]
                if len(human_ids) != human_count:
                    await ctx.send(
                        f"❌ Expected {human_count} mentions, got {len(human_ids)}. "
                        "Draft cancelled."
                    )
                    _remove_draft(ctx.channel.id)
                    return
            except asyncio.TimeoutError:
                await ctx.send("❌ Timed out — draft cancelled.")
                _remove_draft(ctx.channel.id)
                return

    # ── Step 4: Lotto position ───────────────────────────────────────────
    human_positions = None   # None = random shuffle (default)
    if human_ids:
        await ctx.send(
            "🎰 **Draft Position**\n"
            "`1` — Random lottery (bot assigns your slot)\n"
            "`2` — Choose your spot"
        )
        try:
            msg = await bot.wait_for('message', check=_same_author, timeout=60)
            lotto_choice = int(msg.content.strip())
            if lotto_choice not in (1, 2):
                raise ValueError
        except (asyncio.TimeoutError, ValueError):
            await ctx.send("❌ Invalid input — draft cancelled.")
            _remove_draft(ctx.channel.id)
            return

        if lotto_choice == 2:
            human_positions = []
            taken: set[int] = set()
            # In multi-team mode the same user picks multiple slots
            num_slots = len(human_ids)
            for i in range(num_slots):
                slot_label = f"team {i+1}" if num_slots > 1 else "your team"
                while True:
                    await ctx.send(
                        f"Pick a draft slot for {slot_label} *(1–{total_teams})*:"
                        + (f" *(taken: {', '.join(str(s) for s in sorted(taken))})*" if taken else "")
                    )
                    try:
                        msg = await bot.wait_for('message', check=_same_author, timeout=60)
                        slot = int(msg.content.strip())
                        if not 1 <= slot <= total_teams or slot in taken:
                            await ctx.send(f"❌ Slot {slot} is invalid or already chosen. Try again.")
                            continue
                        human_positions.append(slot)
                        taken.add(slot)
                        break
                    except (asyncio.TimeoutError, ValueError):
                        await ctx.send("❌ Timed out — draft cancelled.")
                        _remove_draft(ctx.channel.id)
                        return

    await ctx.send("⏳ Loading player pool from Google Sheets…")
    try:
        count = dm.load_player_pool()
        if count == 0:
            await ctx.send("❌ Player pool is empty — check the spreadsheet tab name.")
            _remove_draft(ctx.channel.id)
            return
        await ctx.send(f"✅ Loaded **{count} players** from the pool.")
    except Exception as e:
        await ctx.send(f"❌ Failed to load player pool: {e}")
        _remove_draft(ctx.channel.id)
        return

    # ── Step 6: Assign teams, resolve guild emojis, display draft board ──
    dm.setup(total_teams, human_ids, human_positions=human_positions)
    dm.started_by = str(ctx.author) if human_ids else None  # None = all-AI watch mode

    guild_emojis_by_name = {e.name.lower(): e for e in ctx.guild.emojis}

    for team in dm.teams:
        candidates = _TEAM_EMOJI_CANDIDATES.get(team.name, [])
        found = next(
            (str(guild_emojis_by_name[c.lower()])
             for c in candidates if c.lower() in guild_emojis_by_name),
            None
        )
        team.emoji = found if found else '🏀'
        print(f"[Setup:{ctx.channel.id}] Team: {team.name:<28} emoji={team.emoji} owner={team.owner_id or 'AI'}")

    lines = ["**🏀 Draft Order:**"]
    for i, team in enumerate(dm.teams, 1):
        lines.append(f"{i}. {team.display()}")
    await ctx.send("\n".join(lines))

    human_count = len(human_ids)
    ai_count = total_teams - len(set(human_ids))  # unique humans = human-controlled teams
    if mode == 3:
        human_label = "**0 humans** (watch mode)"
    elif mode == 2:
        human_label = f"**{human_count} teams** controlled by {ctx.author.mention} (multi-team)"
    else:
        human_label = "**0 humans** (watch mode)" if human_count == 0 else f"**{human_count} human{'s' if human_count > 1 else ''}**"
    await ctx.send(
        f"**{total_teams} teams** | {human_label} | **{ai_count} AI bot{'s' if ai_count != 1 else ''}**\n"
        f"**{ROUNDS} rounds** | Snake draft\n\n"
        f"Starting in **5 seconds…**"
    )
    await asyncio.sleep(5)

    # ── Step 6: Run the draft ─────────────────────────────────────────────
    async def _run_draft_safe():
        try:
            await _run_draft(ctx.channel, dm)
        except asyncio.CancelledError:
            pass  # clean cancel via !draftcancel — no message needed
        except Exception as exc:
            print(f"[Draft:{ctx.channel.id}] FATAL ERROR: {exc}")
            import traceback
            traceback.print_exc()
            _remove_draft(ctx.channel.id)
            try:
                await ctx.channel.send(
                    f"❌ **Draft crashed** — `{exc}`\n"
                    "Check the terminal for the full traceback. Use `!draft` to start a new draft."
                )
            except Exception:
                pass

    task = bot.loop.create_task(_run_draft_safe())
    _draft_tasks[ctx.channel.id] = task


@bot.command(name='draftcancel')
async def draft_cancel(ctx: commands.Context):
    """Cancel the current draft in this channel/thread."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state == DraftState.IDLE:
        await ctx.send("No draft is currently running here.")
        return
    _remove_draft(ctx.channel.id)
    await ctx.send("🛑 Draft cancelled.")


@bot.command(name='draftskip')
async def draft_skip(ctx: commands.Context):
    """Sim all remaining picks to the end with AI — no delays."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state != DraftState.ACTIVE:
        await ctx.send("No active draft here to skip.")
        return

    # Cancel the running task (may be mid-wait for a human pick) without
    # removing the DraftManager — we want to continue from current pick.
    task = _draft_tasks.pop(ctx.channel.id, None)
    if task and not task.done():
        task.cancel()
        await asyncio.sleep(0)   # yield so the cancelled task can clean up

    await ctx.send("⏩ **Skipping to end** — simming all remaining picks with AI…")

    async def _run_sim_safe():
        try:
            await _run_draft_sim(ctx.channel, dm)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[Sim:{ctx.channel.id}] FATAL ERROR: {exc}")
            import traceback
            traceback.print_exc()
            _remove_draft(ctx.channel.id)
            try:
                await ctx.channel.send(f"❌ **Sim crashed** — `{exc}`")
            except Exception:
                pass

    task = bot.loop.create_task(_run_sim_safe())
    _draft_tasks[ctx.channel.id] = task


@bot.command(name='draftstatus')
async def draft_status(ctx: commands.Context):
    """Show the live status of the current draft in this channel."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state == DraftState.IDLE:
        await ctx.send("No active draft in this channel.")
        return

    if dm.state == DraftState.COMPLETE:
        await ctx.send("The draft in this channel has finished.")
        return

    team = dm.current_team
    total = dm.total_picks
    done = dm.current_pick
    pct = round(done / total * 100)
    remaining = len(dm.available_players)

    started_label = f"**Started by:** {dm.started_by}" if dm.started_by else "**Mode:** AI watch"

    embed = discord.Embed(title="📋 Draft Status", color=0xf5a623)
    embed.add_field(name="Pick", value=f"**{dm.pick_number}** of {total}", inline=True)
    embed.add_field(name="Round", value=f"**{dm.round_number}** of {ROUNDS}", inline=True)
    embed.add_field(name="Progress", value=f"**{pct}%** complete", inline=True)
    embed.add_field(name="On the Clock", value=f"{team.emoji} **{team.name}**", inline=True)
    embed.add_field(name="Type", value="🤖 AI pick" if team.is_ai else f"👤 <@{team.owner_id}>", inline=True)
    embed.add_field(name="Players Left", value=f"**{remaining}** available", inline=True)
    embed.add_field(name="\u200b", value=started_label, inline=False)
    await ctx.send(embed=embed)


@bot.command(name='drafthistory')
async def draft_history(ctx: commands.Context):
    """Show the last 10 completed drafts."""
    if not _is_draft_channel(ctx.channel):
        return
    rows = fdb.get_draft_history(limit=10)
    if not rows:
        await ctx.send("No drafts recorded yet.")
        return

    status_icons = {
        "pending_review": "⏳",
        "reviewing":      "🔍",
        "reviewed":       "✅",
    }
    lines = []
    for r in rows:
        ts = r["timestamp"][:10]  # YYYY-MM-DD
        icon = status_icons.get(r["status"], "❓")
        human = r.get("started_by") or "AI only (watch)"
        lines.append(
            f"{icon} **Draft #{r['id']}** — {ts} | {r['num_teams']} teams | "
            f"Started by: **{human}** | {r['status'].replace('_', ' ').title()}"
        )

    embed = discord.Embed(
        title="📜 Draft History (Last 10)",
        description="\n".join(lines),
        color=0x95a5a6,
    )
    embed.set_footer(text="⏳ pending review  🔍 reviewing  ✅ reviewed")
    await ctx.send(embed=embed)


@bot.command(name='draftboard')
async def draft_board(ctx: commands.Context, team_emoji: str = None):
    """Show current rosters mid-draft. Pass a team emoji to see just that team."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state != DraftState.ACTIVE:
        await ctx.send("No active draft here.")
        return
    if team_emoji:
        match = next((t for t in dm.teams if t.emoji == team_emoji), None)
        if match is None:
            await ctx.send(f"No team found with emoji {team_emoji}.")
            return
        await ctx.send(embed=_team_roster_embed(match, dm.pick_number, dm.total_picks))
    else:
        for team in dm.teams:
            await ctx.send(embed=_team_roster_embed(team, dm.pick_number, dm.total_picks))


class _PoolPageView(discord.ui.View):
    """Paginated player pool viewer with ◀ / ▶ buttons."""

    def __init__(self, pages: list[list[str]], title_prefix: str, total: int, requester_id: int):
        super().__init__(timeout=300)
        self.pages        = pages
        self.title_prefix = title_prefix
        self.total        = total
        self.requester_id = requester_id
        self.page         = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.pages) - 1

    def _build_embed(self) -> discord.Embed:
        page_players = self.pages[self.page]
        return discord.Embed(
            title=(
                f"Available {self.title_prefix}s "
                f"(Page {self.page + 1}/{len(self.pages)}) — {self.total} remaining"
            ),
            description="\n".join(f"• {p}" for p in page_players),
            color=0x3498db,
        )

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Use `!draftpool` to open your own view.", ephemeral=True)
            return
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Use `!draftpool` to open your own view.", ephemeral=True)
            return
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


@bot.command(name='draftpool')
async def draft_pool(ctx: commands.Context, category: str = None):
    """Show remaining available players. Optionally filter by Guard / Wing / Forward / Big."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state != DraftState.ACTIVE:
        await ctx.send("No active draft here.")
        return

    available_set = set(dm.available_players)

    if category:
        cat_list = get_pool_category(category)
        if cat_list is None:
            await ctx.send(
                "❌ Unknown category. Use: `!draftpool Guard`, `!draftpool Wing`, "
                "`!draftpool Forward`, or `!draftpool Big`."
            )
            return
        players = [p for p in cat_list if p in available_set]
        title_prefix = category.capitalize()
    else:
        players = dm.available_players
        title_prefix = "All"

    if not players:
        await ctx.send(f"No available {category or 'players'} remaining.")
        return

    pages = [players[i:i+30] for i in range(0, len(players), 30)]
    view = _PoolPageView(pages, title_prefix, len(players), ctx.author.id)
    await ctx.send(embed=view._build_embed(), view=view)


@bot.command(name='drafthelp')
async def draft_help(ctx: commands.Context):
    """Show all ATD Draft Bot commands and how the draft works."""

    e = discord.Embed(
        title="🏀 ATD Draft Bot — Command Guide",
        color=0xf5a623,
    )

    e.add_field(
        name="🚀 `!draft`",
        value=(
            "Start a new draft session. The bot will ask:\n"
            "1. **Mode** — Standard / Multi-team / Watch\n"
            "2. How many total teams? *(2–32)*\n"
            "3. Mode-specific setup (see below)\n\n"
            "**Modes:**\n"
            "• `Standard` — each human controls 1 team, rest are AI\n"
            "• `Multi-team` — you control multiple teams and compete vs AI\n"
            "• `Watch` — fully AI draft, no human input\n\n"
            "Works in the main draft channel **or any thread** created from it. "
            "Multiple drafts can run simultaneously - one per thread."
        ),
        inline=False,
    )

    e.add_field(
        name="🛑 `!draftcancel`",
        value="Cancel the draft running in **this** channel or thread.",
        inline=False,
    )

    e.add_field(
        name="⏩ `!draftskip`",
        value="Sim the rest of the draft instantly — all remaining picks are made by AI with no delays. Useful for testing or finishing a draft fast.",
        inline=False,
    )

    e.add_field(
        name="📊 `!draftstatus`",
        value="Shows the live status of the current draft — pick number, round, who's on the clock, and how many players are left.",
        inline=False,
    )

    e.add_field(
        name="📜 `!drafthistory`",
        value="Shows the last 10 completed drafts — date, number of teams, who started it, and whether it's been reviewed.",
        inline=False,
    )

    e.add_field(
        name="📋 `!draftboard` / `!draftboard <emoji>`",
        value=(
            "Shows every team's current roster mid-draft.\n"
            "Pass a team emoji to see just that one team — e.g. `!draftboard 🦁`"
        ),
        inline=False,
    )

    e.add_field(
        name="📝 `!draftpool` / `!draftpool <category>`",
        value=(
            "Lists all players still available to be drafted (paginated by 30).\n"
            "Filter by position group: `!draftpool Guard`, `!draftpool Wing`, "
            "`!draftpool Forward`, `!draftpool Big`"
        ),
        inline=False,
    )

    e.add_field(
        name="❓ `!drafthelp`",
        value="Shows this guide.",
        inline=False,
    )

    e.add_field(
        name="How to Make a Pick",
        value=(
            "When it's your turn the bot will tag you and give you **30 seconds**.\n"
            "Type your pick in this format:\n"
            "```\n<pick#>. <team-emoji> Player Name\n```\n"
            "**Example:** `5. 🗽 LeBron James`\n\n"
        ),
        inline=False,
    )

    e.add_field(
        name="🤖 How the AI Picks — Point System",
        value=(
            "Each player gets an **Effective ADP** - lower = picked sooner.\n\n"
            "**~55% Base ADP** - players go roughly in ADP order.\n"
            "**~20% Positional need** - starter slots fill before bench (+100 bench penalty).\n"
            "**~15% Team fit** - penalises bad combos (ball-dominant stack, 2 non-scoring bigs, soft big duos) and rewards covering weaknesses.\n"
            "**~6% Scoring** - every team needs 2-3 shot creators in the starting 5.\n"
            "**~4% Tier/needs** - 1 player from each of the 10 tiers; rim protection; spacing."
        ),
        inline=False,
    )

    e.add_field(
        name="─────────────────────────────",
        value="**Draft Review & Weight Tuning**",
        inline=False,
    )

    e.add_field(
        name="🔍 `!draftreview`",
        value=(
            "Start a review session for the most recent draft.\n"
            "The bot walks you through each team one by one with **Approve / Reject** buttons.\n"
            "On rejection, pick the reason(s) from a button menu.\n"
            "When all teams are reviewed, the bot analyses patterns and posts proposed weight changes."
        ),
        inline=False,
    )

    e.add_field(
        name="✅ `!confirmweights`",
        value="Apply all proposed weight changes from the latest review. The AI updates immediately — no restart needed.",
        inline=False,
    )

    e.add_field(
        name="⏭️ `!skipweights 1 3`",
        value="Skip specific proposals by number before confirming. E.g. `!skipweights 1 3` skips proposals #1 and #3, applies the rest.",
        inline=False,
    )

    e.add_field(
        name="✏️ `!setweight 2 55`",
        value="Manually override a proposed value before confirming. E.g. `!setweight 2 55` sets proposal #2's new value to 55.",
        inline=False,
    )

    e.add_field(
        name="🚫 `!cancelweights`",
        value="Discard all pending weight proposals without applying anything.",
        inline=False,
    )

    e.add_field(
        name="⚖️ `!currentweights`",
        value="Show every current weight value — the full list of AI draft settings and their numbers.",
        inline=False,
    )

    e.add_field(
        name="📜 `!weighthistory`",
        value="Show the last 20 weight changes — what changed, from what to what, and which draft triggered it.",
        inline=False,
    )

    e.set_footer(text="ATD Draft Bot • works in this channel and any thread off it")
    await ctx.send(embed=e)


# ── Draft Review & RLHF Commands ─────────────────────────────────────────────
#
# Workflow:
#   1. After a draft ends, bot posts "Run !draftreview"
#   2. !draftreview starts walking through each team with Approve / Reject buttons
#   3. On Reject, a second set of reason buttons appears
#   4. After all teams reviewed, bot runs pattern analysis and posts proposals
#   5. You confirm with !confirmweights, skip some with !skipweights, or override
#      individual values with !setweight before confirming


class _RejectReasonsView(discord.ui.View):
    """Second-step view: multi-select reason buttons shown after a rejection."""

    def __init__(self, team_draft_id: int, reviewer_id: int, review_view: discord.ui.View):
        super().__init__(timeout=300)
        self._team_draft_id = team_draft_id
        self._reviewer_id   = reviewer_id
        self._review_view   = review_view
        self._selected: set[str] = set()

        for key, label in REASON_LABELS.items():
            btn = discord.ui.Button(label=label, custom_id=key, style=discord.ButtonStyle.secondary)
            btn.callback = self._toggle_reason
            self.add_item(btn)

        confirm = discord.ui.Button(label="✅ Confirm Rejection", style=discord.ButtonStyle.danger, row=4)
        confirm.callback = self._confirm
        self.add_item(confirm)

    async def _toggle_reason(self, interaction: discord.Interaction):
        if interaction.user.id != self._reviewer_id:
            await interaction.response.send_message("Not your review session.", ephemeral=True)
            return
        key = interaction.data["custom_id"]
        if key in self._selected:
            self._selected.discard(key)
        else:
            self._selected.add(key)
        selected_labels = [REASON_LABELS[k] for k in self._selected]
        await interaction.response.edit_message(
            content=f"**Selected reasons:** {', '.join(selected_labels) or '_none yet_'}\nClick reasons to toggle, then Confirm.",
            view=self,
        )

    async def _confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self._reviewer_id:
            await interaction.response.send_message("Not your review session.", ephemeral=True)
            return
        reasons = list(self._selected) or ["no_reason_given"]
        fdb.record_verdict(self._team_draft_id, "rejected", reasons, str(interaction.user))
        await interaction.response.edit_message(
            content=f"❌ Rejected. Reasons: {', '.join(REASON_LABELS.get(r, r) for r in reasons)}",
            view=None,
        )
        # Advance the review
        await _advance_review(interaction.channel, _active_reviews.get(interaction.channel.id))


class _TeamReviewView(discord.ui.View):
    """Approve / Reject buttons for a single team."""

    def __init__(self, team_draft_id: int, reviewer_id: int):
        super().__init__(timeout=None)
        self._team_draft_id = team_draft_id
        self._reviewer_id   = reviewer_id

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self._reviewer_id:
            await interaction.response.send_message("Not your review session.", ephemeral=True)
            return
        fdb.record_verdict(self._team_draft_id, "approved", [], str(interaction.user))
        await interaction.response.edit_message(content="✅ Approved!", view=None)
        await _advance_review(interaction.channel, _active_reviews.get(interaction.channel.id))

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self._reviewer_id:
            await interaction.response.send_message("Not your review session.", ephemeral=True)
            return
        reasons_view = _RejectReasonsView(self._team_draft_id, self._reviewer_id, self)
        await interaction.response.edit_message(
            content="Select all reasons that apply, then click **Confirm Rejection**:",
            view=reasons_view,
        )


# draft_id → reviewer_id
_active_reviews: dict[int, dict] = {}   # channel_id → {draft_id, reviewer_id}


async def _advance_review(channel, session: dict | None):
    """Post the next unreviewed team, or wrap up if all done."""
    if session is None:
        return
    draft_id    = session["draft_id"]
    reviewer_id = session["reviewer_id"]

    team = fdb.get_unreviewed_team(draft_id)
    if team is None:
        # All teams reviewed — run analysis
        _active_reviews.pop(channel.id, None)
        fdb.set_draft_status(draft_id, "reviewed")
        summary = fdb.get_review_summary(draft_id)
        await channel.send(fproposer.format_summary_message(draft_id, summary))

        proposals = fproposer.build_proposals(draft_id)
        if proposals:
            proposal_id = fdb.save_proposal(draft_id, proposals)
            await channel.send(fproposer.format_proposals_message(proposals, proposal_id))
        else:
            await channel.send("✅ No weight changes needed from this draft — signals within normal range.")
        return

    # Post next team
    embed = discord.Embed(title=team["team_name"], color=0x3498db)
    for i, pick in enumerate(team["picks"], 1):
        embed.add_field(name=f"Pick {i}", value=pick, inline=True)
    view = _TeamReviewView(team["id"], reviewer_id)
    await channel.send(embed=embed, view=view)


@bot.command(name='draftreview')
async def draftreview_cmd(ctx: commands.Context, draft_id: int = 0):
    """
    Start reviewing teams from the most recent draft (or a specific draft_id).
    Usage: !draftreview [draft_id]
    """
    if draft_id == 0:
        draft_id = fdb.get_latest_draft_id()
        if draft_id is None:
            await ctx.send("No drafts saved yet. Run a draft first.")
            return

    status = fdb.get_draft_status(draft_id)
    if status is None:
        await ctx.send(f"Draft #{draft_id} not found.")
        return
    if status == "reviewed":
        await ctx.send(f"Draft #{draft_id} has already been fully reviewed. Use `!draftreview {draft_id}` if you want to re-run (unreviewed entries only).")

    # If a session is already active for a different draft, block.
    # If it's the same draft (or stale from a disconnect), resume it.
    existing = _active_reviews.get(ctx.channel.id)
    if existing and existing["draft_id"] != draft_id:
        await ctx.send("A review session for a different draft is already active in this channel.")
        return

    teams = fdb.get_draft_teams(draft_id)
    total      = len(teams)
    unreviewed = sum(1 for t in teams if t["verdict"] is None)
    resuming   = existing is not None

    _active_reviews[ctx.channel.id] = {"draft_id": draft_id, "reviewer_id": ctx.author.id}
    fdb.set_draft_status(draft_id, "reviewing")

    prefix = "▶️ **Resuming**" if resuming else "📋 **Reviewing**"
    await ctx.send(
        f"{prefix} Draft #{draft_id} — {unreviewed}/{total} teams remaining.\n"
        f"React to each team embed with ✅ Approve or ❌ Reject."
    )
    await _advance_review(ctx.channel, _active_reviews[ctx.channel.id])


@bot.command(name='confirmweights')
async def confirmweights_cmd(ctx: commands.Context):
    """Apply all pending weight proposals. Usage: !confirmweights"""
    if not _has_weight_role(ctx.author):
        await ctx.send("❌ You need the **ATD Bot Developer** or **ATD Bot Tester** role to change weights.")
        return
    pending = fdb.get_pending_proposal()
    if not pending:
        await ctx.send("No pending weight proposals. Run `!draftreview` first.")
        return

    applied = fproposer.apply_proposals(
        pending["id"], pending["proposals"], draft_id=pending["draft_id"]
    )
    if applied:
        await ctx.send(
            f"✅ **{len(applied)} weight(s) updated:**\n" + "\n".join(applied)
        )
    else:
        await ctx.send("No changes were applied (all proposals were at bounds).")


@bot.command(name='skipweights')
async def skipweights_cmd(ctx: commands.Context, *indices: int):
    """
    Apply proposals but skip specific numbers.
    Usage: !skipweights 1 3   (skips proposals #1 and #3, applies the rest)
    """
    if not _has_weight_role(ctx.author):
        await ctx.send("❌ You need the **ATD Bot Developer** or **ATD Bot Tester** role to change weights.")
        return
    pending = fdb.get_pending_proposal()
    if not pending:
        await ctx.send("No pending weight proposals.")
        return

    applied = fproposer.apply_proposals(
        pending["id"], pending["proposals"],
        skip_indices=list(indices),
        draft_id=pending["draft_id"],
    )
    skipped = len(pending["proposals"]) - len(applied)
    await ctx.send(
        f"✅ Applied {len(applied)} change(s), skipped {skipped}.\n" +
        ("\n".join(applied) if applied else "_No changes._")
    )


@bot.command(name='setweight')
async def setweight_cmd(ctx: commands.Context, index: int, value: float):
    """
    Override the proposed value for one item before confirming.
    Usage: !setweight 2 55
    Then run !confirmweights to apply all (including the override).
    """
    if not _has_weight_role(ctx.author):
        await ctx.send("❌ You need the **ATD Bot Developer** or **ATD Bot Tester** role to change weights.")
        return
    pending = fdb.get_pending_proposal()
    if not pending:
        await ctx.send("No pending weight proposals.")
        return
    proposals = pending["proposals"]
    if index < 1 or index > len(proposals):
        await ctx.send(f"Invalid proposal index. Must be 1–{len(proposals)}.")
        return
    proposals[index - 1]["new_value"] = value
    proposals[index - 1]["pct_change"] = round(
        (value - proposals[index - 1]["old_value"]) / proposals[index - 1]["old_value"] * 100, 1
    )
    # Re-save the updated proposals list
    import json
    with __import__("sqlite3").connect(__import__("os").path.join(
        __import__("os").path.dirname(__file__), "draft_feedback.db"
    )) as con:
        con.execute(
            "UPDATE weight_proposals SET proposals = ? WHERE id = ?",
            (json.dumps(proposals), pending["id"]),
        )
    p = proposals[index - 1]
    await ctx.send(
        f"Updated proposal #{index}: `{p['key']}` → **{value}**\n"
        f"Run `!confirmweights` to apply all changes."
    )


@bot.command(name='cancelweights')
async def cancelweights_cmd(ctx: commands.Context):
    """Discard the current pending weight proposals without applying anything."""
    if not _has_weight_role(ctx.author):
        await ctx.send("❌ You need the **ATD Bot Developer** or **ATD Bot Tester** role to change weights.")
        return
    pending = fdb.get_pending_proposal()
    if not pending:
        await ctx.send("No pending weight proposals.")
        return
    fdb.cancel_proposal(pending["id"])
    await ctx.send("🗑️ Weight proposals cancelled.")


@bot.command(name='currentweights')
async def currentweights_cmd(ctx: commands.Context):
    """Show all current weight values."""
    w = ai_drafter.W
    lines = [f"`{k}`: **{v}**" for k, v in w.items()]
    # Split into two columns for readability
    half = len(lines) // 2
    embed = discord.Embed(title="⚙️ Current AI Draft Weights", color=0x95a5a6)
    embed.add_field(name="Penalties & Bonuses (1)", value="\n".join(lines[:half]), inline=True)
    embed.add_field(name="Penalties & Bonuses (2)", value="\n".join(lines[half:]), inline=True)
    await ctx.send(embed=embed)


@bot.command(name='weighthistory')
async def weighthistory_cmd(ctx: commands.Context):
    """Show the last 15 weight changes."""
    history = fdb.get_weight_history(limit=15)
    if not history:
        await ctx.send("No weight changes recorded yet.")
        return
    lines = []
    for h in history:
        ts = h["timestamp"][:10]
        arrow = "↑" if h["new_value"] > h["old_value"] else "↓"
        lines.append(f"`{h['weight_key']}` {h['old_value']} → **{h['new_value']}** {arrow} _{ts}_")
    await ctx.send("**⚙️ Weight Change History**\n" + "\n".join(lines))


@bot.event
async def on_ready():
    fdb.init_db()
    print(f"✅ ATD Draft Bot ready — logged in as {bot.user}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"[CommandError] {ctx.command}: {error}")
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)


# ── Run ──────────────────────────────────────────────────────────────────────
bot.run(DISCORD_TOKEN)
