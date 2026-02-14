import discord
from discord.ext import commands
from ..config import DEFAULT_PREFIX, logger

class AdminCommands(commands.Cog):
    """Administrative commands for guild management"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='setprefix')
    @commands.has_permissions(administrator=True)
    async def set_prefix(self, ctx, new_prefix: str):
        """Set a custom prefix for this guild (Admin only)"""
        if not ctx.guild:
            await ctx.send("This command can only be used in a server!")
            return
        
        self.bot.guild_configs.update_guild_config(ctx.guild.id, prefix=new_prefix)
        await ctx.send(f"✅ Command prefix changed to `{new_prefix}` for this server!")
    
    @commands.command(name='guildinfo')
    @commands.has_permissions(administrator=True)
    async def guild_info(self, ctx):
        """Get information about the current guild (Admin only)"""
        if not ctx.guild:
            await ctx.send("This command can only be used in a server!")
            return
        
        config = self.bot.guild_configs.get_guild_config(ctx.guild.id)
        prefix = config.get('prefix', DEFAULT_PREFIX)
        
        embed = discord.Embed(
            title=f"🏠 Guild Information: {ctx.guild.name}",
            color=0x3498db
        )
        
        info = [
            f"**Guild ID:** {ctx.guild.id}",
            f"**Owner:** {ctx.guild.owner}",
            f"**Members:** {ctx.guild.member_count}",
            f"**Created:** {ctx.guild.created_at.strftime('%Y-%m-%d')}",
            f"**Bot Prefix:** {prefix}",
            f"**Bot Joined:** {config.get('joined_at', 'Unknown')}",
            f"**Total Guilds:** {len(self.bot.guilds)}"
        ]
        
        embed.add_field(name="Details", value="\n".join(info), inline=False)
        await ctx.send(embed=embed)
    
    @commands.command(name='listguilds')
    @commands.is_owner()
    async def list_guilds(self, ctx):
        """List all guilds the bot is in (Bot owner only)"""
        embed = discord.Embed(
            title="🌐 Bot Guilds",
            description=f"Total: {len(self.bot.guilds)}",
            color=0xff9900
        )
        
        guild_list = []
        for guild in self.bot.guilds:
            config = self.bot.guild_configs.get_guild_config(guild.id)
            prefix = config.get('prefix', DEFAULT_PREFIX)
            guild_list.append(f"• **{guild.name}** (ID: {guild.id})\n  Members: {guild.member_count} | Prefix: {prefix}")
        
        # Split into multiple fields if needed
        chunks = [guild_list[i:i+10] for i in range(0, len(guild_list), 10)]
        
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name=f"Guilds {i*10+1}-{i*10+len(chunk)}",
                value="\n".join(chunk),
                inline=False
            )
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))