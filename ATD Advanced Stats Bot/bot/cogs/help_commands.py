import discord
from discord.ext import commands
from ..utils.formatters import ADVANCED_STATS
from ..config import DEFAULT_PREFIX

class HelpCommands(commands.Cog):
    """Help and information commands"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='helpadvanced', aliases=['statshelp'])
    async def help_advanced(self, ctx):
        """Show help for advanced stats commands"""
        prefix = await self.bot.get_prefix(ctx.message)
        if isinstance(prefix, list):
            prefix = prefix[0] if prefix else DEFAULT_PREFIX
        
        embed = discord.Embed(
            title="🏀 NBA Advanced Stats Bot Help",
            description="Get comprehensive NBA statistics including year-by-year advanced metrics",
            color=0xff9900
        )
        
        commands_list = [
            f"**{prefix}advancedstats [player]** - Get yearly advanced stats for a player",
            f"**{prefix}playerinfo [player]** - Get basic player information",
            f"**{prefix}setprefix [new_prefix]** - Change command prefix (Admin)",
            f"**{prefix}guildinfo** - Show current server info (Admin)",
            f"**{prefix}listguilds** - List all servers (Bot Owner)",
            f"**{prefix}helpadvanced** - Show this help message"
        ]
        
        embed.add_field(name="Commands", value="\n".join(commands_list), inline=False)
        
        # Show available advanced stats
        stats_sample = list(ADVANCED_STATS.items())[:8]
        stats_text = "\n".join([f"• **{k}** - {v[0]}" for k, v in stats_sample])
        stats_text += f"\n*...and {len(ADVANCED_STATS)-8} more*"
        
        embed.add_field(name="Advanced Stats Included", value=stats_text, inline=False)
        
        # Add note about data availability
        embed.add_field(
            name="📅 Data Availability",
            value="Advanced stats available from **1996-97 season** to present",
            inline=False
        )
        
        embed.set_footer(text=f"Current prefix: {prefix} • Data provided by NBA.com")
        await ctx.send(embed=embed)
    
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Handle command errors gracefully"""
        if isinstance(error, commands.CommandNotFound):
            prefix = await self.bot.get_prefix(ctx.message)
            if isinstance(error, commands.CommandNotFound):
                return
            await ctx.send(f"❌ Command not found. Try `{prefix}helpadvanced` to see available commands.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("❌ This command is only available to the bot owner.")
        elif isinstance(error, commands.MissingRequiredArgument):
            prefix = await self.bot.get_prefix(ctx.message)
            if isinstance(prefix, list):
                prefix = prefix[0] if prefix else DEFAULT_PREFIX
            await ctx.send(f"❌ Missing required argument. Use `{prefix}helpadvanced` for command usage.")

async def setup(bot):
    await bot.add_cog(HelpCommands(bot))