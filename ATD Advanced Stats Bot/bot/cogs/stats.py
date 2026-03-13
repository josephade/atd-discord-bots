import asyncio
import discord
from discord.ext import commands
from discord import app_commands

from bot.models.nba_data import get_player_stats
from bot.utils.embeds import stats_embed
from bot.utils.season import extract_year_from_args, parse_season

SEASON_DESC = "Season start year e.g. 2003, 2016, 2024 (default: current)"


class StatsCog(commands.Cog, name="Stats"):
    def __init__(self, bot):
        self.bot = bot

    # ── shared logic ──────────────────────────────────────────────────────────
    async def _run(self, dest, player: str, season: str):
        data, err = await asyncio.to_thread(get_player_stats, player, season)
        if not data:
            msg = err or f"❌ No stats found for **{player}** in **{season}**."
            if isinstance(dest, commands.Context):
                await dest.send(msg)
            else:
                await dest.followup.send(msg)
            return
        embed = stats_embed(data)
        if isinstance(dest, commands.Context):
            await dest.send(embed=embed)
        else:
            await dest.followup.send(embed=embed)

    # ── !stats ────────────────────────────────────────────────────────────────
    @commands.command(name="stats")
    async def stats_prefix(self, ctx, *, args: str = ""):
        """!stats <player> [year] — e.g. !stats Kobe Bryant 2005"""
        if not args:
            await ctx.send("Usage: `!stats <player> [year]`\nExample: `!stats Kobe Bryant 2005`")
            return
        player, season = extract_year_from_args(args.split())
        async with ctx.typing():
            await self._run(ctx, player, season)

    # ── /stats ────────────────────────────────────────────────────────────────
    @app_commands.command(name="stats", description="Full player stat line with key metrics")
    @app_commands.describe(player="Player name (e.g. Kobe Bryant)", year=SEASON_DESC)
    async def stats_slash(self, interaction: discord.Interaction, player: str, year: str = "2024"):
        await interaction.response.defer()
        season = parse_season(year) or "2024-25"
        await self._run(interaction, player, season)


async def setup(bot):
    await bot.add_cog(StatsCog(bot))