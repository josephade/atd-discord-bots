"""Recover accepted Theme pick events missed during a brief disconnect."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
import discord
from draft_history import player_key

THEME_BOT_ID = 1536036867999268877


def ensure_pick(manager, data):
    def existing():
        return manager._find_existing_player(data['player'], manager._call('get_all_values'))
    found = existing()
    if found:
        return ('existing', None) if player_key(found)==player_key(data['team']) else ('conflict', found)
    ok, result = manager.add_player(data['team'], data['player'], year=data['year'], price=data['price'],
                                    position_override=data['position_override'], bench_only=data['bench_only'])
    if ok:
        return 'added', result
    found = existing()  # A live event may have written it while recovery was waiting.
    if found and player_key(found)==player_key(data['team']):
        return 'existing', None
    return 'failed', result


async def recover_recent(bot, mappings, get_manager, parse_message, log_audit, silent_channels=(), require_price=False):
    log = logging.getLogger(__name__)
    cutoff = discord.utils.utcnow()-timedelta(minutes=15)
    recovered = 0
    for channel_id, entry in list(mappings.items()):
        if channel_id in silent_channels:
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue
        expected = dict(entry)
        since = cutoff
        if entry.get('linked_at'):
            linked = datetime.fromisoformat(entry['linked_at'])
            if linked.tzinfo is None:
                linked = linked.replace(tzinfo=timezone.utc)
            since = max(since, linked)
        try:
            async for message in channel.history(limit=300, after=since, oldest_first=True):
                if message.author.id != THEME_BOT_ID or not re.match(r'^\s*\d+\.\s+', message.content):
                    continue
                if re.fullmatch(r'\s*\d+\.\s+.+?\s+skipped\s*', message.content, re.I) or 'needs to make up the pick in DMs' in message.content:
                    continue
                if any(str(r.emoji)=='\u2705' and r.me for r in message.reactions):
                    continue
                if mappings.get(channel_id) != expected:
                    break  # Do not replay into a newly selected draft tab.
                data, error = parse_message(message.content)
                if error or require_price and not data.get('price'):
                    continue
                manager = await get_manager(channel_id)
                if manager is None:
                    break
                status, detail = await asyncio.to_thread(ensure_pick, manager, data)
                if status not in ('added', 'existing'):
                    log.warning('Could not recover Theme pick %s: %s %s', message.id, status, detail)
                    continue
                await message.add_reaction('\u2705')
                log_audit(dict(timestamp=discord.utils.utcnow().isoformat(), action='restart_recovery',
                    channel_id=channel_id, source_message_id=message.id, user_id=message.author.id,
                    player=data['player'], team=data['team'], year=data['year'], price=data['price'], recovery_status=status))
                recovered += 1
        except Exception:
            log.exception('Recent Theme pick recovery failed for channel %s', channel_id)
    return recovered
