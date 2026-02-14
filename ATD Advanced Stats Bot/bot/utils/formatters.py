import discord
import pandas as pd
from .converters import safe_float_conversion

# Advanced stats categories with formatting info
ADVANCED_STATS = {
    'PER': ('Player Efficiency Rating', 'float', 1),
    'TS_PCT': ('True Shooting Percentage', 'percentage', 1),
    'FG3_PCT': ('3-Point Percentage', 'percentage', 1),
    'FT_PCT': ('Free Throw Percentage', 'percentage', 1),
    'FTA_RATE': ('Free Throw Attempt Rate', 'float', 3),
    'OREB_PCT': ('Offensive Rebound Percentage', 'percentage', 1),
    'DREB_PCT': ('Defensive Rebound Percentage', 'percentage', 1),
    'REB_PCT': ('Total Rebound Percentage', 'percentage', 1),
    'AST_PCT': ('Assist Percentage', 'percentage', 1),
    'STL_PCT': ('Steal Percentage', 'percentage', 1),
    'BLK_PCT': ('Block Percentage', 'percentage', 1),
    'TOV_PCT': ('Turnover Percentage', 'percentage', 1),
    'USG_PCT': ('Usage Percentage', 'percentage', 1),
    'OWS': ('Offensive Win Shares', 'float', 1),
    'DWS': ('Defensive Win Shares', 'float', 1),
    'WS': ('Win Shares', 'float', 1),
    'WS_48': ('Win Shares Per 48 Minutes', 'float', 3),
    'OBPM': ('Offensive Box Plus/Minus', 'float', 1),
    'DBPM': ('Defensive Box Plus/Minus', 'float', 1),
    'BPM': ('Box Plus/Minus', 'float', 1),
    'VORP': ('Value Over Replacement Player', 'float', 1)
}

class StatsFormatter:
    """Format statistics for Discord embeds"""
    
    @staticmethod
    def format_player_info(player_data, player_name):
        """Format player information embed"""
        embed = discord.Embed(
            title=f"👤 {player_data.get('DISPLAY_FIRST_LAST', player_name)}",
            color=0x3498db
        )
        
        details = [
            f"**Team:** {player_data.get('TEAM_CITY', 'N/A')} {player_data.get('TEAM_NAME', 'N/A')}",
            f"**Position:** {player_data.get('POSITION', 'N/A')}",
            f"**Jersey:** #{player_data.get('JERSEY', 'N/A')}",
            f"**Height:** {player_data.get('HEIGHT', 'N/A')}",
            f"**Weight:** {player_data.get('WEIGHT', 'N/A')} lbs",
            f"**College:** {player_data.get('SCHOOL', 'N/A')}",
            f"**Country:** {player_data.get('COUNTRY', 'N/A')}",
            f"**Draft:** {player_data.get('DRAFT_YEAR', 'N/A')} Round {player_data.get('DRAFT_ROUND', 'N/A')} Pick {player_data.get('DRAFT_NUMBER', 'N/A')}"
        ]
        
        embed.add_field(name="Player Information", value="\n".join(details), inline=False)
        return embed
    
    @staticmethod
    def format_yearly_stats(player_name, career_data):
        """Format yearly advanced stats into pages"""
        if career_data.empty:
            return None
        
        # Sort by season (most recent first)
        career_data = career_data.sort_values('SEASON_ID', ascending=False)
        
        pages = []
        current_page = []
        page_number = 1
        total_pages = max(1, (len(career_data) + 4) // 5)  # 5 seasons per page
        
        for idx, season in career_data.iterrows():
            season_id = season.get('SEASON_ID', 'Unknown')
            
            # Safely get values
            per = safe_float_conversion(season.get('PER'))
            ts_pct = safe_float_conversion(season.get('TS_PCT'))
            usg_pct = safe_float_conversion(season.get('USG_PCT'))
            bpm = safe_float_conversion(season.get('BPM'))
            ws_48 = safe_float_conversion(season.get('WS_48'))
            
            per_str = f"{per:.1f}" if per is not None else "N/A"
            ts_str = f"{ts_pct*100:.1f}%" if ts_pct is not None else "N/A"
            usg_str = f"{usg_pct*100:.1f}%" if usg_pct is not None else "N/A"
            bpm_str = f"{bpm:.1f}" if bpm is not None else "N/A"
            ws48_str = f"{ws_48:.3f}" if ws_48 is not None else "N/A"
            
            season_text = f"**{season_id}** | PER: {per_str} | TS%: {ts_str} | USG%: {usg_str} | BPM: {bpm_str} | WS/48: {ws48_str}"
            
            current_page.append(season_text)
            
            # Create new page every 5 seasons
            if len(current_page) == 5 or idx == career_data.index[-1]:
                embed = discord.Embed(
                    title=f"📊 Yearly Advanced Stats: {player_name}",
                    description=f"Page {page_number}/{total_pages}",
                    color=0x00ff00
                )
                
                # Add career summary on first page
                if page_number == 1 and len(career_data) > 0:
                    summary = StatsFormatter._get_career_summary(career_data)
                    embed.add_field(name="📈 Career Summary", value=summary, inline=False)
                    embed.add_field(name="-" * 50, value="", inline=False)
                
                embed.add_field(
                    name=f"Seasons ({len(current_page)} shown)",
                    value="\n".join(current_page),
                    inline=False
                )
                
                embed.set_footer(text="Data from 1996-97 to present • Stats per season")
                
                pages.append((embed, page_number, total_pages))
                current_page = []
                page_number += 1
        
        return pages
    
    @staticmethod
    def _get_career_summary(career_data):
        """Calculate and format career summary"""
        career_per = career_data['PER'].apply(safe_float_conversion).dropna().mean()
        career_ts = career_data['TS_PCT'].apply(safe_float_conversion).dropna().mean()
        career_usg = career_data['USG_PCT'].apply(safe_float_conversion).dropna().mean()
        career_bpm = career_data['BPM'].apply(safe_float_conversion).dropna().mean()
        career_ws48 = career_data['WS_48'].apply(safe_float_conversion).dropna().mean()
        career_vorp = career_data['VORP'].apply(safe_float_conversion).dropna().mean()
        
        career_per_str = f"{career_per:.1f}" if not pd.isna(career_per) else "N/A"
        career_ts_str = f"{career_ts*100:.1f}%" if not pd.isna(career_ts) else "N/A"
        career_usg_str = f"{career_usg*100:.1f}%" if not pd.isna(career_usg) else "N/A"
        career_bpm_str = f"{career_bpm:.1f}" if not pd.isna(career_bpm) else "N/A"
        career_ws48_str = f"{career_ws48:.3f}" if not pd.isna(career_ws48) else "N/A"
        career_vorp_str = f"{career_vorp:.1f}" if not pd.isna(career_vorp) else "N/A"
        
        return (
            f"PER: {career_per_str} | TS%: {career_ts_str} | USG%: {career_usg_str}\n"
            f"BPM: {career_bpm_str} | WS/48: {career_ws48_str} | VORP: {career_vorp_str}"
        )