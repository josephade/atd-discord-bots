import asyncio
import re

import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

from player_data import (
    search_players,
    get_player_by_url,
    get_player_season,
    get_team_season,
    match_teams,
    TEAM_NAMES,
    BrefStructureChanged,
)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ACCENT_COLOR = 0xE8590C
FOOTER_ICON = "https://cdn-icons-png.flaticon.com/512/124/124010.png"
SELECTION_TIMEOUT = 30.0

SEASON_TYPE_ALIASES = {"ps": "playoffs", "playoffs": "playoffs", "playoff": "playoffs"}
YEAR_PATTERN = re.compile(r"^(19|20)\d{2}(-\d{2,4})?$")


def _to_end_year(token: str) -> str:
    """'2016' -> '2016'; '2015-16' or '2015-2016' -> '2016' (bref keys seasons by end year)."""
    if "-" not in token:
        return token
    start, end = token.split("-", 1)
    return start[:2] + end if len(end) == 2 else end


def _parse_lookup_args(raw: str) -> tuple[str, str | None, str]:
    """Splits '!lu' arguments into (player_name, end_year_or_None, season_type)."""
    tokens = raw.split()

    season_type = "regular"
    if tokens and tokens[-1].lower() in SEASON_TYPE_ALIASES:
        season_type = SEASON_TYPE_ALIASES[tokens[-1].lower()]
        tokens = tokens[:-1]

    end_year = None
    if tokens and YEAR_PATTERN.match(tokens[-1]):
        end_year = _to_end_year(tokens[-1])
        tokens = tokens[:-1]

    return " ".join(tokens), end_year, season_type


@bot.event
async def on_ready():
    print(f"✅ Lookup Bot logged in as {bot.user}")
    await bot.change_presence(activity=discord.Game(name="!lu <player name>"))


def _not_found_embed(query: str) -> discord.Embed:
    return discord.Embed(
        title="❌ Player Not Found",
        description=f"Couldn't find a player matching **{query}** on Basketball-Reference.",
        color=discord.Color.red(),
    )


def _error_embed() -> discord.Embed:
    return discord.Embed(
        title="⚠️ Lookup Failed",
        description="Basketball-Reference didn't respond in time. Please try again shortly.",
        color=discord.Color.orange(),
    )


def _season_not_found_embed(name: str, end_year: str, season_type: str) -> discord.Embed:
    label = "playoff" if season_type == "playoffs" else "regular season"
    return discord.Embed(
        title="❌ No Stats Found",
        description=f"No {label} record for **{name}** in the season ending {end_year}. Double-check the year.",
        color=discord.Color.red(),
    )


def _structure_changed_embed() -> discord.Embed:
    return discord.Embed(
        title="⚠️ Lookup Bot Needs an Update",
        description=(
            "Basketball-Reference changed the layout of their site, so this bot can no longer "
            "read player pages correctly. A maintainer needs to update the scraper before "
            "`!lu` will work again."
        ),
        color=discord.Color.orange(),
    )


def _build_player_embed(player: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏀 {player['name']}",
        url=player["url"],
        color=ACCENT_COLOR,
    )

    if player["image"]:
        embed.set_thumbnail(url=player["image"])

    embed.add_field(
        name="📋 Bio",
        value=(
            f"**Position:** {player['position']}\n"
            f"**Height:** {player['height']}\n"
            f"**Weight:** {player['weight']}"
        ),
        inline=False,
    )

    teams = " ➜ ".join(player["teams"]) if player["teams"] else "No team data on record."
    embed.add_field(name="🏟️ Teams Played For", value=teams, inline=False)

    accolades = player["accolades"]
    accolades_text = "\n".join(f"• {a}" for a in accolades) if accolades else "No major accolades on record."
    embed.add_field(name="🏆 Accolades", value=accolades_text, inline=False)

    career = player["career"]
    if career:
        embed.add_field(name="​", value="**📊 Career Averages**", inline=False)
        embed.add_field(name="PTS", value=career["ppg"], inline=True)
        embed.add_field(name="REB", value=career["rpg"], inline=True)
        embed.add_field(name="AST", value=career["apg"], inline=True)
        embed.add_field(name="STL", value=career["spg"], inline=True)
        embed.add_field(name="BLK", value=career["bpg"], inline=True)
        embed.add_field(name="GP", value=career["games"], inline=True)
        embed.add_field(name="FG%", value=career["fg_pct"], inline=True)
        embed.add_field(name="3P%", value=career["three_pct"], inline=True)
        embed.add_field(name="FT%", value=career["ft_pct"], inline=True)
        embed.set_footer(
            text=f"Lookup Bot • {career['seasons']} Seasons • Data via Basketball-Reference.com",
            icon_url=FOOTER_ICON,
        )
    else:
        embed.add_field(name="📊 Career Averages", value="Unavailable.", inline=False)
        embed.set_footer(text="Lookup Bot • Data via Basketball-Reference.com", icon_url=FOOTER_ICON)

    return embed


def _build_season_embed(data: dict, end_year: str, season_type: str) -> discord.Embed:
    basic = data["basic"]
    advanced = data["advanced"] or {}
    label = "Playoffs" if season_type == "playoffs" else "Regular Season"
    season_display = basic["season"] if basic["season"] != "-" else end_year

    embed = discord.Embed(
        title=f"🏀 {data['name']} — {season_display} {label}",
        url=data["url"],
        color=ACCENT_COLOR,
    )

    if data["image"]:
        embed.set_thumbnail(url=data["image"])

    embed.add_field(
        name="📋 Season Info",
        value=(
            f"**Team:** {TEAM_NAMES.get(basic['team'], basic['team'])}\n"
            f"**Games:** {basic['games']}\n"
            f"**MPG:** {basic['mp']}"
        ),
        inline=False,
    )

    try:
        pra = float(basic["ppg"]) + float(basic["rpg"]) + float(basic["apg"])
        pra_text = f"**{pra:.1f}**  ({basic['ppg']} PTS + {basic['rpg']} REB + {basic['apg']} AST)"
    except (TypeError, ValueError):
        pra_text = "Unavailable"
    embed.add_field(name="🔢 PRA (Pts + Reb + Ast)", value=pra_text, inline=False)

    embed.add_field(
        name="🎯 Shooting Splits",
        value=(
            f"**FG%:** {basic['fg_pct']} ({basic['fgm']}/{basic['fga']})\n"
            f"**3P%:** {basic['three_pct']} ({basic['three_m']}/{basic['three_a']})\n"
            f"**FT%:** {basic['ft_pct']} ({basic['ftm']}/{basic['fta']})"
        ),
        inline=False,
    )

    embed.add_field(name="TOV", value=basic["topg"], inline=True)
    embed.add_field(name="OREB", value=basic["orpg"], inline=True)
    embed.add_field(name="STL", value=basic["spg"], inline=True)
    embed.add_field(name="BLK", value=basic["bpg"], inline=True)
    embed.add_field(name="TS%", value=advanced.get("ts_pct", "-"), inline=True)

    if advanced:
        embed.add_field(
            name="📈 Advanced",
            value=(
                f"**PER:** {advanced['per']}\n"
                f"**USG%:** {advanced['usg_pct']}\n"
                f"**WS:** {advanced['ws']}\n"
                f"**BPM:** {advanced['bpm']}\n"
                f"**VORP:** {advanced['vorp']}"
            ),
            inline=False,
        )
        if advanced.get("awards") and advanced["awards"] != "-":
            embed.add_field(name="🏆 Season Awards", value=advanced["awards"].replace(",", ", "), inline=False)

    embed.set_footer(text=f"Lookup Bot • {label} • Data via Basketball-Reference.com", icon_url=FOOTER_ICON)
    return embed


def _team_season_not_found_embed(query: str, end_year: str) -> discord.Embed:
    return discord.Embed(
        title="❌ No Team Data Found",
        description=f"Couldn't find a season for **{query.title()}** ending in {end_year}. Double-check the team name and year.",
        color=discord.Color.red(),
    )


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _rank_text(ranks: dict, key: str) -> str:
    rank = ranks.get(key)
    return f" ({_ordinal(rank[0])} of {rank[1]})" if rank else ""


def _build_team_embed(data: dict) -> discord.Embed:
    stats = data["stats"]
    ranks = data["ranks"]

    embed = discord.Embed(
        title=f"🏀 {data['name']} — {data['season']}",
        url=data["url"],
        color=ACCENT_COLOR,
    )

    embed.add_field(name="📋 Record", value=f"**W:** {stats['wins']}   **L:** {stats['losses']}", inline=False)

    embed.add_field(
        name="📈 Ratings",
        value=(
            f"**ORtg:** {stats['off_rtg']}{_rank_text(ranks, 'off_rtg')}\n"
            f"**DRtg:** {stats['def_rtg']}{_rank_text(ranks, 'def_rtg')}\n"
            f"**Net Rtg:** {stats['net_rtg']}{_rank_text(ranks, 'net_rtg')}\n"
            f"**Pace:** {stats['pace']}{_rank_text(ranks, 'pace')}"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎯 Offense Four Factors",
        value=(
            f"**eFG%:** {stats['efg_pct']}{_rank_text(ranks, 'efg_pct')}\n"
            f"**TOV%:** {stats['tov_pct']}{_rank_text(ranks, 'tov_pct')}\n"
            f"**ORB%:** {stats['orb_pct']}{_rank_text(ranks, 'orb_pct')}\n"
            f"**FT/FGA:** {stats['ft_fga']}{_rank_text(ranks, 'ft_fga')}\n"
            f"**FTr:** {stats['ftr']}{_rank_text(ranks, 'ftr')}\n"
            f"**3PAr:** {stats['three_par']}{_rank_text(ranks, 'three_par')}"
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Defense Four Factors",
        value=(
            f"**eFG%:** {stats['opp_efg_pct']}{_rank_text(ranks, 'opp_efg_pct')}\n"
            f"**TOV%:** {stats['opp_tov_pct']}{_rank_text(ranks, 'opp_tov_pct')}\n"
            f"**DRB%:** {stats['drb_pct']}{_rank_text(ranks, 'drb_pct')}\n"
            f"**FT/FGA:** {stats['opp_ft_fga']}{_rank_text(ranks, 'opp_ft_fga')}"
        ),
        inline=False,
    )

    embed.set_footer(
        text=f"Lookup Bot • {data['total_teams']} teams • Data via Basketball-Reference.com",
        icon_url=FOOTER_ICON,
    )
    return embed


async def _send_team_result(ctx, team_abbr: str, end_year: str):
    async with ctx.typing():
        try:
            data = await get_team_season(team_abbr, end_year)
        except BrefStructureChanged as exc:
            print(f"⚠️ Basketball-Reference structure change detected: {exc}")
            await ctx.send(embed=_structure_changed_embed())
            return
        except Exception:
            await ctx.send(embed=_error_embed())
            return

    if not data.get("name"):
        await ctx.send(embed=_team_season_not_found_embed(TEAM_NAMES.get(team_abbr, team_abbr), end_year))
        return

    await ctx.send(embed=_build_team_embed(data))


async def _send_result(ctx, player_url: str, end_year: str | None, season_type: str):
    async with ctx.typing():
        try:
            if end_year:
                data = await get_player_season(player_url, end_year, season_type)
            else:
                data = await get_player_by_url(player_url)
        except BrefStructureChanged as exc:
            print(f"⚠️ Basketball-Reference structure change detected: {exc}")
            await ctx.send(embed=_structure_changed_embed())
            return
        except Exception:
            await ctx.send(embed=_error_embed())
            return

    if end_year:
        if not data["basic"]:
            await ctx.send(embed=_season_not_found_embed(data["name"], end_year, season_type))
            return
        await ctx.send(embed=_build_season_embed(data, end_year, season_type))
    else:
        await ctx.send(embed=_build_player_embed(data))


@bot.command(name="lu")
async def lookup(ctx, *, args: str = None):
    if not args:
        await ctx.send(
            "**Usage:**\n"
            "`!lu <player name>` — full career profile\n"
            "`!lu <player name> <year>` — regular season stats (e.g. `!lu Stephen Curry 2016` = 2015-16 season)\n"
            "`!lu <player name> <year> playoffs` — playoff stats for that year (or `ps`)\n"
            "`!lu <team name> <year>` — team season stats + league ranks (e.g. `!lu Warriors 2016`)"
        )
        return

    player_name, end_year, season_type = _parse_lookup_args(args)
    if not player_name:
        await ctx.send("I need a player or team name — e.g. `!lu Stephen Curry 2016 playoffs`")
        return

    async with ctx.typing():
        try:
            player_matches = await search_players(player_name)
        except BrefStructureChanged as exc:
            print(f"⚠️ Basketball-Reference structure change detected: {exc}")
            await ctx.send(embed=_structure_changed_embed())
            return
        except Exception:
            await ctx.send(embed=_error_embed())
            return

    # Team lookups need a year to fetch a season, so only offer them once one's given.
    team_abbrs = match_teams(player_name) if end_year else []

    candidates = [{"kind": "player", "name": c["name"], "url": c["url"]} for c in player_matches]
    candidates += [{"kind": "team", "name": TEAM_NAMES.get(abbr, abbr), "abbr": abbr} for abbr in team_abbrs]

    if not candidates:
        await ctx.send(embed=_not_found_embed(player_name))
        return

    if len(candidates) == 1:
        await _dispatch_candidate(ctx, candidates[0], end_year, season_type)
        return

    options = "\n".join(
        f"**{i + 1})** {c['name']}" + (" *(Team)*" if c["kind"] == "team" else "")
        for i, c in enumerate(candidates)
    )
    await ctx.send(
        f"🤔 **Did you mean?**\n{options}\n\n"
        f"Reply with a number (1–{len(candidates)}) to pick one, or `cancel`."
    )

    def check(m: discord.Message) -> bool:
        if m.author != ctx.author or m.channel != ctx.channel:
            return False
        content = m.content.strip().lower()
        return content == "cancel" or content.isdigit()

    try:
        reply = await bot.wait_for("message", check=check, timeout=SELECTION_TIMEOUT)
    except asyncio.TimeoutError:
        await ctx.send("⌛ No selection made — lookup cancelled.")
        return

    if reply.content.strip().lower() == "cancel":
        await ctx.send("❌ Lookup cancelled.")
        return

    choice = int(reply.content.strip())
    if not 1 <= choice <= len(candidates):
        await ctx.send(f"That's not one of the options (1–{len(candidates)}).")
        return

    await _dispatch_candidate(ctx, candidates[choice - 1], end_year, season_type)


async def _dispatch_candidate(ctx, candidate: dict, end_year: str | None, season_type: str):
    if candidate["kind"] == "team":
        await _send_team_result(ctx, candidate["abbr"], end_year)
    else:
        await _send_result(ctx, candidate["url"], end_year, season_type)


if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ ERROR: DISCORD_TOKEN not found in environment variables.")
