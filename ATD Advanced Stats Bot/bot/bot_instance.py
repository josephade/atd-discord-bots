import discord
from discord.ext import commands
from .config import DEFAULT_PREFIX, logger
from .models.guild_config import GuildConfigManager

class StatsBot(commands.Bot):
    """Custom bot class with additional features"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(command_prefix=self.get_prefix, intents=intents)
        
        # Initialize managers
        self.guild_configs = GuildConfigManager()
        self.page_cache = {}  # For reaction pagination
    
    async def get_prefix(self, message):
        """Get custom prefix for guild"""
        if not message.guild:
            return commands.when_mentioned_or(DEFAULT_PREFIX)(self, message)
        
        custom_prefix = self.guild_configs.get_prefix(message.guild.id)
        if custom_prefix:
            return commands.when_mentioned_or(custom_prefix)(self, message)
        
        return commands.when_mentioned_or(DEFAULT_PREFIX)(self, message)
    
    async def setup_hook(self):
        """Setup hook for loading cogs"""
        await self.load_extension('bot.cogs.player_commands')
        await self.load_extension('bot.cogs.admin_commands')
        await self.load_extension('bot.cogs.help_commands')
        logger.info("All cogs loaded successfully")
    
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f'{self.user} has connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} guilds')
        
        # Register guilds in config if not present
        for guild in self.guilds:
            config = self.guild_configs.get_guild_config(guild.id)
            if not config:
                self.guild_configs.update_guild_config(
                    guild.id,
                    name=guild.name,
                    prefix=DEFAULT_PREFIX,
                    joined_at=str(guild.me.joined_at),
                    member_count=guild.member_count
                )
    
    async def on_guild_join(self, guild):
        """Track when bot joins a new guild"""
        self.guild_configs.update_guild_config(
            guild.id,
            name=guild.name,
            prefix=DEFAULT_PREFIX,
            joined_at=str(guild.me.joined_at),
            member_count=guild.member_count
        )
        logger.info(f'Joined new guild: {guild.name} (ID: {guild.id})')
    
    async def on_guild_remove(self, guild):
        """Track when bot leaves a guild"""
        self.guild_configs.remove_guild(guild.id)
        logger.info(f'Left guild: {guild.name} (ID: {guild.id})')