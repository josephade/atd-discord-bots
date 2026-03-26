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

# One draft at a time
_draft: DraftManager = DraftManager()


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


async def _run_draft(channel: discord.TextChannel):
    """
    Core draft loop.  Runs until all picks are complete, then writes results.
    """
    dm = _draft
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
            )
            dm.record_pick(player)
            print(f"[Draft] Pick #{pick_num:>3} | AI   | {team.name:<28} | {player}")
            await _announce_pick(channel, pick_num, team, player)
            continue

        # ── Human pick ───────────────────────────────────────────────────
        print(f"[Draft] Pick #{pick_num:>3} | Human | {team.name:<28} | waiting…")
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
        # Each bad pick resets the per-message timer, but the overall deadline
        # is fixed from when the pick prompt was sent.
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
            print(f"[Draft] Pick #{pick_num:>3} | Human | {team.name:<28} | {player}")
            await _announce_pick(channel, pick_num, team, player)
        else:
            player = ai_drafter.pick(team.picks, available, player_adp=dm.player_adp, pool_size=len(dm.player_pool))
            dm.record_pick(player)
            print(f"[Draft] Pick #{pick_num:>3} | Auto  | {team.name:<28} | {player} (timeout)")
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

    dm.state = DraftState.IDLE


# ── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name='draft')
async def draft_cmd(ctx: commands.Context):
    """Start an ATD draft session."""
    if ctx.channel.id != DRAFT_CHANNEL_ID:
        return

    if _draft.state != DraftState.IDLE:
        await ctx.send("⚠️ A draft is already in progress. Use `!draftcancel` to cancel it.")
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
                return
        except asyncio.TimeoutError:
            await ctx.send("❌ Timed out — draft cancelled.")
            return

    # ── Step 4: Load player pool ─────────────────────────────────────────
    await ctx.send("⏳ Loading player pool from Google Sheets…")
    try:
        count = _draft.load_player_pool()
        if count == 0:
            await ctx.send("❌ Player pool is empty — check the spreadsheet tab name.")
            return
        await ctx.send(f"✅ Loaded **{count} players** from the pool.")
    except Exception as e:
        await ctx.send(f"❌ Failed to load player pool: {e}")
        return

    # ── Step 5: Assign teams, resolve guild emojis, display draft board ──
    _draft.setup(total_teams, human_ids)

    # Resolve server custom emojis for each team.
    # Tries each candidate name (case-insensitive) and uses the first match.
    # Falls back to 🏀 if no custom emoji exists in the server for that team.
    guild_emojis_by_name = {e.name.lower(): e for e in ctx.guild.emojis}

    for team in _draft.teams:
        candidates = _TEAM_EMOJI_CANDIDATES.get(team.name, [])
        found = next(
            (str(guild_emojis_by_name[c.lower()])
             for c in candidates if c.lower() in guild_emojis_by_name),
            None
        )
        team.emoji = found if found else '🏀'
        print(f"[Setup] Team: {team.name:<28} emoji={team.emoji} owner={team.owner_id or 'AI'}")

    lines = ["**🏀 Draft Order:**"]
    for i, team in enumerate(_draft.teams, 1):
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
    bot.loop.create_task(_run_draft(ctx.channel))


@bot.command(name='draftcancel')
async def draft_cancel(ctx: commands.Context):
    """Cancel the current draft."""
    if ctx.channel.id != DRAFT_CHANNEL_ID:
        return
    if _draft.state == DraftState.IDLE:
        await ctx.send("No draft is currently running.")
        return
    _draft.state = DraftState.IDLE
    _draft.__init__()   # reset all state
    await ctx.send("🛑 Draft cancelled.")


@bot.command(name='draftboard')
async def draft_board(ctx: commands.Context):
    """Show current rosters mid-draft."""
    if ctx.channel.id != DRAFT_CHANNEL_ID:
        return
    if _draft.state != DraftState.ACTIVE:
        await ctx.send("No active draft.")
        return
    for team in _draft.teams:
        await ctx.send(embed=_team_roster_embed(team, _draft.pick_number, _draft.total_picks))


@bot.command(name='draftpool')
async def draft_pool(ctx: commands.Context):
    """Show remaining available players."""
    if ctx.channel.id != DRAFT_CHANNEL_ID:
        return
    if _draft.state != DraftState.ACTIVE:
        await ctx.send("No active draft.")
        return
    available = _draft.available_players
    # Split into pages of 30
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
    if ctx.channel.id != DRAFT_CHANNEL_ID:
        return

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
            "Teams and NBA logos are assigned automatically. "
            "Draft order is randomised, then uses a **snake format**."
        ),
        inline=False,
    )

    e.add_field(
        name="🛑 `!draftcancel`",
        value="Cancel the current draft at any time. Clears all state.",
        inline=False,
    )

    e.add_field(
        name="📋 `!draftboard`",
        value="Shows every team's current roster mid-draft — useful for checking fit before your pick.",
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
        name="🎯 How to Make a Pick",
        value=(
            "When it's your turn the bot will tag you and give you **30 seconds**.\n"
            "Type your pick in this format:\n"
            "```\n<pick#>. <team-emoji> Player Name\n```\n"
            "**Example:** `5. 🗽 LeBron James`\n\n"
            "• You can include a year at the end and it will be ignored — name is all that matters\n"
            "• If you spell a name slightly wrong the bot will fuzzy-match it\n"
            "• If the player is already drafted you'll be told and can pick again\n"
            "• If you run out of time the bot auto-picks the best available player for your team"
        ),
        inline=False,
    )

    e.add_field(
        name="🤖 How the AI Drafts",
        value=(
            "AI teams use the spreadsheet **ADP** (Average Draft Position) as their base value.\n"
            "Lower ADP = picked earlier. On top of that the AI factors in:\n\n"
            "• **Fit** — avoids stacking 3+ ball-dominant players\n"
            "• **Positional need** — fills empty roster slots as rounds progress\n"
            "• **Shooting** — prioritises adding a shooter if the team has none\n"
            "• **Portability** — prefers players that work alongside any star in later rounds\n"
            "• **Position limits** — never drafts a 3rd player at a position (2 slots max per spot)"
        ),
        inline=False,
    )

    e.add_field(
        name="🗂️ Draft Format",
        value=(
            "**10 rounds**, snake order.\n"
            "Round 1: picks go 1 → N\n"
            "Round 2: picks go N → 1\n"
            "Round 3: picks go 1 → N … and so on.\n\n"
            "Each team ends up with **10 players** — 5 starters + 5 bench (one per position: PG SG SF PF C).\n"
            "When the draft ends results are saved to Google Sheets automatically."
        ),
        inline=False,
    )

    e.set_footer(text="ATD Draft Bot • all commands only work in this channel")
    await ctx.send(embed=e)


@bot.event
async def on_ready():
    print(f"✅ ATD Draft Bot ready — logged in as {bot.user}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        pass


# ── Run ──────────────────────────────────────────────────────────────────────
bot.run(DISCORD_TOKEN)
