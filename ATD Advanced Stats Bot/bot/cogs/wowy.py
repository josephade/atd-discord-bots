import asyncio
import discord
from discord.ext import commands
from discord import app_commands

from bot.models.nba_data import get_wowy
from bot.utils.embeds import wowy_embed
from bot.utils.season import extract_year_from_args, parse_season

SEASON_DESC = "Season start year e.g. 2003, 2016, 2024 (default: current)"


class WowyCog(commands.Cog, name="Wowy"):
    def __init__(self, bot):
        self.bot = bot

    async def _run(self, dest, player1: str, player2: str, season: str):
        data, err = await asyncio.to_thread(get_wowy, player1, player2, season)
        if not data:
            msg = err or f"❌ No WOWY data for **{player1}** & **{player2}** in **{season}**."
            if isinstance(dest, commands.Context):
                await dest.send(msg)
            else:
                await dest.followup.send(msg)
            return
        embed = wowy_embed(data, player1, player2)
        if isinstance(dest, commands.Context):
            await dest.send(embed=embed)
        else:
            await dest.followup.send(embed=embed)

    @commands.command(name="wowy")
    async def wowy_prefix(self, ctx, *, args: str = ""):
        """!wowy <player1> | <player2> [year] — e.g. !wowy Kobe Bryant | Shaquille ONeal 2001"""
        if "|" not in args:
            await ctx.send(
                "Usage: `!wowy <player1> | <player2> [year]`\n"
                "Example: `!wowy Kobe Bryant | Shaquille ONeal 2001`"
            )
            return
        left, right = args.split("|", 1)
        player1 = left.strip()
        player2, season = extract_year_from_args(right.strip().split())
        async with ctx.typing():
            await self._run(ctx, player1, player2, season)

    @app_commands.command(name="wowy", description="With/Without You — lineup splits for two players")
    @app_commands.describe(player1="First player", player2="Second player", year=SEASON_DESC)
    async def wowy_slash(self, interaction: discord.Interaction, player1: str, player2: str, year: str = "2024"):
        await interaction.response.defer()
        season = parse_season(year) or "2024-25"
        await self._run(interaction, player1, player2, season)


async def setup(bot):
    await bot.add_cog(WowyCog(bot))