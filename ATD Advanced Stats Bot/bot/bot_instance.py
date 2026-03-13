"""
StatsBot — the main discord.py Bot subclass.
Loads all cogs on startup.
"""

import discord
from discord.ext import commands

from bot.config import logger

COGS = [
    "bot.cogs.stats",
    "bot.cogs.onoff",
    "bot.cogs.wowy",
    "bot.cogs.lastx",
    "bot.cogs.team",
]

class StatsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,          # we use a custom !help
        )

    async def setup_hook(self):
        """Called automatically before the bot connects — load cogs here."""
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info(f"✅ Loaded cog: {cog}")
            except Exception as e:
                logger.error(f"❌ Failed to load cog {cog}: {e}")

        await self.tree.sync()
        logger.info("✅ Slash commands synced")

    async def on_ready(self):
        logger.info(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="NBA stats | !nba for help"
            )
        )