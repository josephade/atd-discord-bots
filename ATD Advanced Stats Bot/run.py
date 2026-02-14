#!/usr/bin/env python3
"""
NBA Stats Bot - Main entry point
"""

from bot.bot_instance import StatsBot
from bot.config import DISCORD_TOKEN, logger

def main():
    """Main function to run the bot"""
    if not DISCORD_TOKEN:
        logger.error("No Discord token found! Please check your .env file.")
        return
    
    logger.info("Starting NBA Stats Bot...")
    bot = StatsBot()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()