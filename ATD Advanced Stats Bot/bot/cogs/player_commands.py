import discord
from discord.ext import commands
from ..utils.nba_api_helper import NBAApiHelper
from ..utils.formatters import StatsFormatter
from ..utils.converters import safe_float_conversion
from ..utils.cache import player_cache
from ..config import logger

class PlayerCommands(commands.Cog):
    """Commands for player statistics"""
    
    def __init__(self, bot):
        self.bot = bot
        self.nba_helper = NBAApiHelper()
    
    @commands.command(name='playerinfo', aliases=['pi', 'player'])
    async def player_info(self, ctx, *, player_name):
        """Get basic information about a player"""
        guild_id = ctx.guild.id if ctx.guild else None
        logger.info(f"Player info command used in guild: {guild_id} by user: {ctx.author.name}")
        
        async with ctx.typing():
            try:
                # Check cache first
                cache_key = f"player_info_{player_name.lower()}"
                cached_player = player_cache.get(cache_key)
                
                if cached_player:
                    player_id, player_data = cached_player
                else:
                    player = self.nba_helper.find_player(player_name)
                    if not player:
                        await ctx.send(f"❌ Could not find player: {player_name}")
                        return
                    
                    player_id = player['id']
                    player_data = self.nba_helper.get_player_info(player_id)
                    if player_data is None or player_data.empty:
                        await ctx.send(f"❌ Could not fetch info for {player_name}")
                        return
                    
                    player_data = player_data.iloc[0]
                    player_cache.set(cache_key, (player_id, player_data))
                
                # Create embed
                embed = StatsFormatter.format_player_info(player_data, player_name)
                
                # Try to get career averages
                try:
                    career_stats = self.nba_helper.get_career_stats(player_id)
                    if career_stats:
                        career_totals = career_stats.get_data_frames()[0]
                        if not career_totals.empty:
                            last_season = career_totals.iloc[-1]
                            pts = safe_float_conversion(last_season.get('PTS'))
                            reb = safe_float_conversion(last_season.get('REB'))
                            ast = safe_float_conversion(last_season.get('AST'))
                            
                            stats_text = []
                            if pts is not None:
                                stats_text.append(f"**PPG:** {pts:.1f}")
                            if reb is not None:
                                stats_text.append(f"**RPG:** {reb:.1f}")
                            if ast is not None:
                                stats_text.append(f"**APG:** {ast:.1f}")
                            
                            if stats_text:
                                embed.add_field(
                                    name="Latest Season Averages",
                                    value=" | ".join(stats_text),
                                    inline=True
                                )
                except Exception as e:
                    logger.warning(f"Could not fetch career stats: {e}")
                
                embed.set_footer(text=f"Data from NBA.com • Requested by {ctx.author.name}")
                await ctx.send(embed=embed)
                
            except Exception as e:
                logger.error(f"Error in playerinfo: {e}")
                await ctx.send(f"❌ An error occurred: {str(e)}")
    
    @commands.command(name='advancedstats', aliases=['advstats', 'as', 'yearlystats', 'ys'])
    async def advanced_stats(self, ctx, *, query):
        """
        Get yearly advanced NBA stats for a player
        Usage: !advancedstats <player name> [season]
        Example: !advancedstats LeBron James
        Example: !advancedstats LeBron James 2023
        Example: !advancedstats Kevin Garnett 2004
        """
        
        guild_id = ctx.guild.id if ctx.guild else None
        logger.info(f"Advanced stats command used in guild: {guild_id} by user: {ctx.author.name}")
        
        # Parse query for player name and optional season
        import re
        season_pattern = r'\s+(\d{4}(?:-\d{2})?)$'
        season_match = re.search(season_pattern, query)
        
        if season_match:
            season = season_match.group(1)
            player_name = re.sub(season_pattern, '', query).strip()
        else:
            season = None
            player_name = query.strip()
        
        async with ctx.typing():
            try:
                player = self.nba_helper.find_player(player_name)
                
                if not player:
                    await ctx.send(f"❌ Could not find player: {player_name}")
                    return
                
                # Get player info for name
                player_info = self.nba_helper.get_player_info(player['id'])
                if player_info is None or player_info.empty:
                    full_name = player_name
                else:
                    full_name = player_info.iloc[0].get('DISPLAY_FIRST_LAST', player_name)
                
                # Get advanced stats (optionally for specific season)
                if season:
                    advanced_stats = self.nba_helper.get_season_advanced_stats(player['id'], season)
                    if advanced_stats is None or advanced_stats.empty:
                        await ctx.send(f"❌ No advanced stats available for {full_name} in season {season}")
                        return
                    
                    # Format single season view
                    embed = await self._format_single_season(full_name, advanced_stats, season)
                    await ctx.send(embed=embed)
                else:
                    # Get all advanced stats
                    career_stats = self.nba_helper.get_career_stats(player['id'])
                    if not career_stats:
                        await ctx.send(f"❌ Could not fetch stats for {full_name}")
                        return
                    
                    advanced_stats = self.nba_helper.get_advanced_stats(career_stats)
                    
                    if advanced_stats is None or advanced_stats.empty:
                        await ctx.send(f"❌ No advanced stats available for {full_name} (stats only available from 1996-97 season)")
                        return
                    
                    # Format and send yearly stats
                    pages = StatsFormatter.format_yearly_stats(full_name, advanced_stats)
                    
                    if not pages:
                        await ctx.send(f"❌ Could not format stats for {full_name}")
                        return
                    
                    message = await ctx.send(embed=pages[0][0])
                    
                    if len(pages) > 1:
                        await message.add_reaction('◀️')
                        await message.add_reaction('▶️')
                        
                        self.bot.page_cache[message.id] = {
                            'pages': pages,
                            'current_page': 0,
                            'author_id': ctx.author.id,
                            'guild_id': guild_id
                        }
                        
            except Exception as e:
                logger.error(f"Error processing advanced stats command: {e}")
                await ctx.send(f"❌ An error occurred: {str(e)}")

    async def _format_single_season(self, player_name, season_data, season):
        row = season_data.iloc[0]

        embed = discord.Embed(
            title=f"📊 {player_name} — {season}",
            color=0x2ecc71
        )

        def pct(val):
            try:
                return f"{float(val)*100:.1f}%"
            except:
                return "N/A"

        def num(val, d=1):
            try:
                return f"{float(val):.{d}f}"
            except:
                return "N/A"

        # 🎯 Efficiency
        efficiency = (
            f"**TS%:** {pct(row.get('TS_PCT'))}\n"
            f"**eFG%:** {pct(row.get('EFG_PCT'))}\n"
            f"**USG%:** {pct(row.get('USG_PCT'))}\n"
            f"**FTA Rate:** {num(row.get('FTA_RATE'), 3)}\n"
            f"**TOV%:** {pct(row.get('TOV_PCT'))}"
        )

        # 📈 Team Impact
        impact = (
            f"**Off Rating:** {num(row.get('OFF_RATING'))}\n"
            f"**Def Rating:** {num(row.get('DEF_RATING'))}\n"
            f"**Net Rating:** {num(row.get('NET_RATING'))}\n"
            f"**PIE:** {pct(row.get('PIE'))}\n"
            f"**Pace:** {num(row.get('PACE'), 1)}"
        )

        # 🛡 Playmaking & Rebounding
        playmaking = (
            f"**AST%:** {pct(row.get('AST_PCT'))}\n"
            f"**REB%:** {pct(row.get('REB_PCT'))}\n"
            f"**OREB%:** {pct(row.get('OREB_PCT'))}\n"
            f"**DREB%:** {pct(row.get('DREB_PCT'))}"
        )

        embed.add_field(name="🎯 Efficiency Profile", value=efficiency, inline=True)
        embed.add_field(name="📈 Team Impact", value=impact, inline=True)
        embed.add_field(name="🛡 Playmaking & Rebounding", value=playmaking, inline=False)

        # 🔥 NEW SECTION — ON/OFF IMPACT
        on_off = self.nba_helper.get_on_off_stats(row.get("PLAYER_ID"), season)

        if on_off:
            on_off_text = (
                f"**On Court +/-:** {on_off['on_plus_minus']:.1f}\n"
                f"**Off Court +/-:** {on_off['off_plus_minus']:.1f}\n"
                f"**Swing:** {on_off['swing']:.1f}\n"
                # f"**Team W% On:** {on_off['on_w_pct']:.3f}\n"
                # f"**Team W% Off:** {on_off['off_w_pct']:.3f}"
            )

            embed.add_field(name="📊 On / Off Impact", value=on_off_text, inline=False)

        # except Exception:
        #     pass  # silently skip if endpoint fails

        embed.set_footer(text="NBA.com Advanced Data • 1996-97 to present")

        return embed
 


    
async def setup(bot):
    await bot.add_cog(PlayerCommands(bot))
