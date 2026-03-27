import asyncio
import difflib
import re
import time

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, DRAFT_CHANNEL_ID, ROUNDS, PICK_TIMEOUT_SECONDS
from draft_manager import DraftManager, DraftState
import ai_drafter

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


def _get_draft(channel_id: int) -> DraftManager:
    """Return the DraftManager for this channel, creating one if needed."""
    if channel_id not in _drafts:
        _drafts[channel_id] = DraftManager()
    return _drafts[channel_id]


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

            # Check if the player was already drafted by someone else
            already = _resolve_player(raw_name, list(dm.drafted), cutoff=0.75)
            if already:
                await channel.send(
                    f"❌ **{already}** has already been drafted. Please choose another player."
                )
                continue

            # Check if the player exists in the available pool
            player = _resolve_player(raw_name, available)
            if not player:
                await channel.send(
                    f"⚠️ **{raw_name}** not found in the player pool. Please re-enter your pick."
                )
                continue
            # Valid pick — exit loop

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

    # ── Step 1: Total teams ───────────────────────────────────────────────
    await ctx.send(
        "🏀 **ATD Draft Setup**\n"
        "How many total teams? *(2–30)*"
    )
    try:
        msg = await bot.wait_for('message', check=_same_author, timeout=120)
        total_teams = int(msg.content.strip())
        if not 2 <= total_teams <= 30:
            raise ValueError
    except (asyncio.TimeoutError, ValueError):
        await ctx.send("❌ Invalid input — draft cancelled.")
        _remove_draft(ctx.channel.id)
        return

    # ── Step 2: Human players ─────────────────────────────────────────────
    await ctx.send(
        f"Got it — **{total_teams} teams**!\n"
        f"How many will be **human players**? *(1–{total_teams})*"
    )
    try:
        msg = await bot.wait_for('message', check=_same_author, timeout=120)
        human_count = int(msg.content.strip())
        if not 1 <= human_count <= total_teams:
            raise ValueError
    except (asyncio.TimeoutError, ValueError):
        await ctx.send("❌ Invalid input — draft cancelled.")
        _remove_draft(ctx.channel.id)
        return

    # ── Step 3: Collect human player mentions ────────────────────────────
    if human_count == 1:
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

    # ── Step 4: Load player pool ─────────────────────────────────────────
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

    # ── Step 5: Assign teams, resolve guild emojis, display draft board ──
    dm.setup(total_teams, human_ids)

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

    ai_count = total_teams - human_count
    await ctx.send(
        f"**{total_teams} teams** | **{human_count} human** | **{ai_count} AI bot{'s' if ai_count != 1 else ''}**\n"
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


@bot.command(name='draftboard')
async def draft_board(ctx: commands.Context):
    """Show current rosters mid-draft."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state != DraftState.ACTIVE:
        await ctx.send("No active draft here.")
        return
    for team in dm.teams:
        await ctx.send(embed=_team_roster_embed(team, dm.pick_number, dm.total_picks))


@bot.command(name='draftpool')
async def draft_pool(ctx: commands.Context):
    """Show remaining available players."""
    if not _is_draft_channel(ctx.channel):
        return
    dm = _drafts.get(ctx.channel.id)
    if dm is None or dm.state != DraftState.ACTIVE:
        await ctx.send("No active draft here.")
        return
    available = dm.available_players
    pages = [available[i:i+30] for i in range(0, len(available), 30)]
    for idx, page in enumerate(pages, 1):
        embed = discord.Embed(
            title=f"Available Players (Page {idx}/{len(pages)})",
            description="\n".join(f"• {p}" for p in page),
            color=0x3498db,
        )
        await ctx.send(embed=embed)


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
            "1. How many total teams? *(2–30)*\n"
            "2. How many are human players?\n"
            "3. Tag the human players (if more than 1)\n\n"
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
        name="📋 `!draftboard`",
        value="Shows every team's current roster mid-draft - useful for checking fit before your pick.",
        inline=False,
    )

    e.add_field(
        name="📝 `!draftpool`",
        value="Lists all players still available to be drafted (paginated by 30).",
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

    e.set_footer(text="ATD Draft Bot • works in this channel and any thread off it")
    await ctx.send(embed=e)


@bot.event
async def on_ready():
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
