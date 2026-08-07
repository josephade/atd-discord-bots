"""
ATD Commish Audit Bot — passively watches for commissioner/admin-only
commands across the other ATD bots and mirrors every attempt (successful
or not) into a single audit-log channel, since each bot's own admin
actions currently only show up in its own Python logs.

This bot does not integrate with the other bots at all — it just watches
message text for known command names. It does not try to capture or
correlate the target bot's reply (that requires matching by channel+timing
and handling both plain-text and embed replies, which is unreliable) — v1
just logs who ran what, where, and when.
"""

import logging
import re

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, AUDIT_LOG_CHANNEL_ID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("commish-audit")

# ── Commish/admin-gated commands across every ATD bot (prefix: !) ────────────
# Compiled from a codebase scan of ATD Timer Bot, ATD Team Sheet Bot,
# ATD Win Bot, ATD Flux Bot, and ATD Sheet Bot. Update this set if any of
# those bots add/rename admin commands.

GATED_COMMANDS = {
    # ATD Timer Bot
    "timerloadlotto", "timerlottoupdate", "timersetup", "timergmlotto",
    "timerorder", "timerslotedit", "timermode", "timerstart", "timerpenalty",
    "timerrebuildorder", "timerjumpto", "timersetpick", "timeraddowner",
    "timerproxy", "timerremoveproxy", "timerunskip", "timersettimer",
    "timerset", "timersetmoney", "timersetpicks", "timersetlastpick",
    "timeraddskip", "timerpause", "timerresume", "removeskip", "timereset",
    # ATD Team Sheet Bot
    "reload", "setsheet", "addchannel", "removechannel", "channels",
    "sheetundo", "assignteam", "removeteam", "resetteams", "draftmatrix",
    # ATD Win Bot
    "adminset", "adminlink", "addteam", "edit",
    # ATD Flux Bot
    "fluxtrack", "fluxuntrack", "flux", "undoflux",
    # ATD Sheet Bot
    "helpatd", "newatd", "status", "undo", "redo", "endhighlight",
    "rehighlight", "force", "changehexcolour", "track", "untrack", "tracks",
}

# Longest names first so e.g. "timersetpick" can't be shadowed by a shorter
# alternative earlier in the pattern.
_COMMAND_RE = re.compile(
    r'^!(' + '|'.join(re.escape(c) for c in sorted(GATED_COMMANDS, key=len, reverse=True)) + r')(\s|$)',
    re.IGNORECASE,
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    log.info("Commish Audit Bot online — logged in as %s (id: %s)", bot.user, bot.user.id)
    if not AUDIT_LOG_CHANNEL_ID:
        log.warning("AUDIT_LOG_CHANNEL_ID is not set — nothing will be logged.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not AUDIT_LOG_CHANNEL_ID:
        return

    content = message.content.strip()
    match = _COMMAND_RE.match(content)
    if not match:
        return

    log_channel = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
    if not log_channel:
        log.warning("AUDIT_LOG_CHANNEL_ID %d not found in cache", AUDIT_LOG_CHANNEL_ID)
        return

    command_name = match.group(1).lower()
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    log.info("AUDIT | cmd=%s | user=%s | channel=%s", command_name, message.author, channel_name)

    try:
        await log_channel.send(
            f"🛠️ **{message.author.display_name}** ran `{content[:200]}` in <#{message.channel.id}>"
        )
    except discord.HTTPException as e:
        log.warning("Failed to post audit log entry: %s", e)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
