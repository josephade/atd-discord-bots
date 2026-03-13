import asyncio
import discord
from discord.ext import commands
from discord import app_commands

from bot.models.nba_data import get_last_x_games
from bot.utils.embeds import lastx_embed
from bot.utils.season import extract_year_from_args, parse_season

SEASON_DESC = "Season start year e.g. 2003, 2016, 2024 (default: current)"


class LastXCog(commands.Cog, name="LastX"):
    def __init__(self, bot):
        self.bot = bot

    async def _run(self, dest, player: str, games: int, season: str):
        data, err = await asyncio.to_thread(get_last_x_games, player, games, season)
        if not data:
            msg = err or f"❌ No game log for **{player}** in **{season}**."
            if isinstance(dest, commands.Context):
                await dest.send(msg)
            else:
                await dest.followup.send(msg)
            return
        embed = lastx_embed(data, player, games)
        if isinstance(dest, commands.Context):
            await dest.send(embed=embed)
        else:
            await dest.followup.send(embed=embed)

    @commands.command(name="lastx")
    async def lastx_prefix(self, ctx, *, args: str = ""):
        """!lastx <player> [games] [year] — e.g. !lastx Kevin Durant 10 2012"""
        if not args:
            await ctx.send(
                "Usage: `!lastx <player> [games] [year]`\n"
                "Example: `!lastx Kevin Durant 10 2012`"
            )
            return

        parts = args.split()
        # Extract year from end
        remaining, season = extract_year_from_args(parts)
        rem_parts = remaining.split()

        # Extract game count if last token is a digit
        games = 5
        if rem_parts and rem_parts[-1].isdigit():
            games = max(1, min(82, int(rem_parts[-1])))
            player = " ".join(rem_parts[:-1])
        else:
            player = remaining

        if not player:
            await ctx.send("Usage: `!lastx <player> [games] [year]`")
            return

        async with ctx.typing():
            await self._run(ctx, player, games, season)

    @app_commands.command(name="lastx", description="Player performance over last X games")
    @app_commands.describe(
        player="Player name",
        games="Number of last games (default: 5)",
        year=SEASON_DESC,
    )
    async def lastx_slash(self, interaction: discord.Interaction, player: str, games: int = 5, year: str = "2024"):
        await interaction.response.defer()
        season = parse_season(year) or "2024-25"
        games  = max(1, min(82, games))
        await self._run(interaction, player, games, season)


async def setup(bot):
    await bot.add_cog(LastXCog(bot))