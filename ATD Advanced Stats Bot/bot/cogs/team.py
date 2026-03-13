import asyncio
import discord
from discord.ext import commands
from discord import app_commands

from bot.models.nba_data import get_team_stats
from bot.utils.embeds import team_embed
from bot.utils.season import extract_year_from_args, parse_season

SEASON_DESC = "Season start year e.g. 2003, 2016, 2024 (default: current)"


class TeamCog(commands.Cog, name="Team"):
    def __init__(self, bot):
        self.bot = bot

    async def _run(self, dest, team: str, season: str):
        data, err = await asyncio.to_thread(get_team_stats, team, season)
        if not data:
            msg = err or f"❌ No data for **{team}** in **{season}**."
            if isinstance(dest, commands.Context):
                await dest.send(msg)
            else:
                await dest.followup.send(msg)
            return
        embed = team_embed(data)
        if isinstance(dest, commands.Context):
            await dest.send(embed=embed)
        else:
            await dest.followup.send(embed=embed)

    @commands.command(name="team")
    async def team_prefix(self, ctx, *, args: str = ""):
        """!team <team> [year] — e.g. !team Lakers 2000"""
        if not args:
            await ctx.send("Usage: `!team <team name or abbrev> [year]`\nExample: `!team Lakers 2000`")
            return
        team, season = extract_year_from_args(args.split())
        async with ctx.typing():
            await self._run(ctx, team, season)

    @app_commands.command(name="team", description="Team stats and standings")
    @app_commands.describe(team="Team name or abbreviation (e.g. Lakers, GSW)", year=SEASON_DESC)
    async def team_slash(self, interaction: discord.Interaction, team: str, year: str = "2024"):
        await interaction.response.defer()
        season = parse_season(year) or "2024-25"
        await self._run(interaction, team, season)


async def setup(bot):
    await bot.add_cog(TeamCog(bot))