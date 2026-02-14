import json
import os
from ..config import CONFIG_FILE, logger

class GuildConfigManager:
    """Manage guild-specific configurations"""
    
    def __init__(self):
        self.configs = self.load_configs()
    
    def load_configs(self):
        """Load guild configurations from file"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.error(f"Error loading configs: {e}")
                return {}
        return {}
    
    def save_configs(self):
        """Save guild configurations to file"""
        try:
            # Ensure data directory exists
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.configs, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving guild configs: {e}")
    
    def get_guild_config(self, guild_id):
        """Get configuration for a specific guild"""
        return self.configs.get(str(guild_id), {})
    
    def update_guild_config(self, guild_id, **kwargs):
        """Update configuration for a specific guild"""
        guild_id = str(guild_id)
        if guild_id not in self.configs:
            self.configs[guild_id] = {}
        
        for key, value in kwargs.items():
            self.configs[guild_id][key] = value
        
        self.save_configs()
    
    def remove_guild(self, guild_id):
        """Remove a guild from configs"""
        guild_id = str(guild_id)
        if guild_id in self.configs:
            del self.configs[guild_id]
            self.save_configs()
    
    def get_prefix(self, guild_id):
        """Get custom prefix for a guild"""
        return self.get_guild_config(guild_id).get('prefix', None)