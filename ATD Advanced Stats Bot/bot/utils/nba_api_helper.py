import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from nba_api.stats.endpoints import playercareerstats, commonplayerinfo, leaguedashplayerstats
from nba_api.stats.static import players
from ..config import NBA_API_TIMEOUT, NBA_API_RETRIES, logger
import nba_api

def create_nba_session():
    """Create a requests session with retry strategy for NBA API"""
    session = requests.Session()
    retry_strategy = Retry(
        total=NBA_API_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# Set custom session for NBA API
# nba_api.requests.sessions = create_nba_session()

class NBAApiHelper:
    """Helper class for NBA API interactions"""
    
    @staticmethod
    def find_player(player_name):
        """Find player by name with fuzzy matching"""
        # Clean up input (remove year if someone types "LeBron James 2023")
        import re
        clean_name = re.sub(r'\s+\d{4}(-\d{2})?$', '', player_name).strip()
        
        # Try exact match first
        player_list = players.find_players_by_full_name(clean_name)
        if player_list:
            return player_list[0]
        
        # Try active players search
        active_players = players.get_active_players()
        matches = [p for p in active_players if clean_name.lower() in p['full_name'].lower()]
        
        if matches:
            return matches[0]
        
        # Try all players (including retired)
        all_players = players.get_players()
        matches = [p for p in all_players if clean_name.lower() in p['full_name'].lower()]
        
        # Sort by relevance (exact matches first)
        if matches:
            # Prioritize exact matches
            exact_matches = [p for p in matches if p['full_name'].lower() == clean_name.lower()]
            if exact_matches:
                return exact_matches[0]
            return matches[0]
        
        return None
    
    @staticmethod
    def get_player_info(player_id):
        """Get detailed player information"""
        try:
            player_info = commonplayerinfo.CommonPlayerInfo(
                player_id=player_id, 
                timeout=NBA_API_TIMEOUT
            )
            return player_info.get_data_frames()[0]
        except Exception as e:
            logger.error(f"Error fetching player info: {e}")
            return None
    
    @staticmethod
    def get_career_stats(player_id):
        """Get career stats for a player"""
        try:
            # Request per game stats
            career = playercareerstats.PlayerCareerStats(
                player_id=player_id,
                per_mode36='PerGame',
                timeout=NBA_API_TIMEOUT
            )
            return career
        except Exception as e:
            logger.error(f"Error fetching career stats: {e}")
            return None
    
    @staticmethod
    def get_advanced_stats(career_stats):
        """Extract advanced stats from career stats safely"""
        if not career_stats:
            return None

        try:
            # Get all data frames
            dfs = career_stats.get_data_frames()
            
            # Log available data frames for debugging
            logger.info(f"Number of dataframes: {len(dfs)}")
            for i, df in enumerate(dfs):
                if df is not None and not df.empty:
                    logger.info(f"DF {i} columns: {list(df.columns)}")
            
            # Advanced stats are typically in the last dataframe (index 2)
            # Or look for dataframe with PER column
            for df in dfs:
                if df is not None and not df.empty:
                    # Check for advanced stats columns
                    if "PER" in df.columns or "TS_PCT" in df.columns:
                        logger.info(f"Found advanced stats dataframe with {len(df)} rows")
                        return df
            
            # If we couldn't find it, try to explicitly request advanced stats
            # This is a fallback - create a new request specifically for advanced stats
            try:
                # Try to get the player ID from the career_stats object
                if hasattr(career_stats, 'parameters') and 'PlayerID' in career_stats.parameters:
                    player_id = career_stats.parameters['PlayerID']
                    
                    # Request specifically for advanced stats
                    advanced_stats = playercareerstats.PlayerCareerStats(
                        player_id=player_id,
                        per_mode36='PerGame',
                        season_type_all_star='Regular Season',
                        timeout=NBA_API_TIMEOUT
                    )
                    
                    # Get all dataframes again
                    adv_dfs = advanced_stats.get_data_frames()
                    for df in adv_dfs:
                        if df is not None and not df.empty and ("PER" in df.columns or "TS_PCT" in df.columns):
                            return df
            except Exception as e:
                logger.error(f"Error in fallback advanced stats request: {e}")
            
            return None

        except Exception as e:
            logger.error(f"Error extracting advanced stats: {e}")
            return None
    
    @staticmethod
    def get_season_advanced_stats(player_id, season):
        """
        Fetch advanced stats for a specific season using the correct endpoint.
        season example: '2023'
        """

        try:
            year = int(season)
            season_id = f"{year}-{str(year+1)[-2:]}"
        except:
            return None

        try:
            stats = leaguedashplayerstats.LeagueDashPlayerStats(
                season=season_id,
                season_type_all_star="Regular Season",
                per_mode_detailed="PerGame",
                measure_type_detailed_defense="Advanced",
                timeout=NBA_API_TIMEOUT
            )

            df = stats.get_data_frames()[0]

            player_row = df[df["PLAYER_ID"] == player_id]

            return player_row if not player_row.empty else None

        except Exception as e:
            logger.error(f"Advanced season fetch error: {e}")
            return None