from datetime import datetime
from ..config import CACHE_ENABLED, CACHE_TIMEOUT, logger

class PlayerCache:
    """Cache for player data to reduce API calls"""
    
    def __init__(self):
        self.cache = {}
    
    def get(self, key):
        """Get item from cache if valid"""
        if not CACHE_ENABLED or key not in self.cache:
            return None
        
        cache_time, value = self.cache[key]
        if (datetime.now() - cache_time).seconds < CACHE_TIMEOUT:
            return value
        
        # Cache expired
        del self.cache[key]
        return None
    
    def set(self, key, value):
        """Set item in cache"""
        if CACHE_ENABLED:
            self.cache[key] = (datetime.now(), value)
    
    def clear(self):
        """Clear all cache"""
        self.cache.clear()
        logger.info("Cache cleared")

# Global cache instance
player_cache = PlayerCache()