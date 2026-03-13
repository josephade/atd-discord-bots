import asyncio
import discord
from discord.ext import commands
from discord import app_commands

from bot.models.nba_data import get_on_off
from bot.utils.embeds import onoff_embed
from bot.utils.season import extract_year_from_args, parse_season

SEASON_DESC = "Season start year e.g. 2003, 2016, 2024 (default: current)"


class OnOffCog(commands.Cog, name="OnOff"):
    def __init__(self, bot):
        self.bot = bot

    async def _run(self, dest, player: str, season: str):
        data, err = await asyncio.to_thread(get_on_off, player, season)
        if not data:
            msg = err or f"❌ No on/off data for **{player}** in **{season}**."
            if isinstance(dest, commands.Context):
                await dest.send(msg)
            else:
                await dest.followup.send(msg)
            return
        embed = onoff_embed(data, player)
        if isinstance(dest, commands.Context):
            await dest.send(embed=embed)
        else:
            await dest.followup.send(embed=embed)

    @commands.command(name="onoff")
    async def onoff_prefix(self, ctx, *, args: str = ""):
        """!onoff <player> [year] — e.g. !onoff Steph Curry 2016"""
        if not args:
            await ctx.send("Usage: `!onoff <player> [year]`\nExample: `!onoff Steph Curry 2016`")
            return
        player, season = extract_year_from_args(args.split())
        async with ctx.typing():
            await self._run(ctx, player, season)

    @app_commands.command(name="onoff", description="On/Off net rating splits for a player")
    @app_commands.describe(player="Player name (e.g. Steph Curry)", year=SEASON_DESC)
    async def onoff_slash(self, interaction: discord.Interaction, player: str, year: str = "2024"):
        await interaction.response.defer()
        season = parse_season(year) or "2024-25"
        await self._run(interaction, player, season)


async def setup(bot):
    await bot.add_cog(OnOffCog(bot))