"""GM-authorized anonymous roster changes through Team Sheet's private API."""
import os
import asyncio
import aiohttp
import discord
import re

import suspects


async def edit(ctx, drafter, action, text):
    matches = [team for team in suspects.teams(ctx.draft) if drafter['id'] in team['member_ids']]
    if len(matches) != 1 or not ctx.state.get('timer_channel_id'):
        return 'Your anonymous team or selection channel is not configured. Ask the commissioner to check the draft.'
    key = os.getenv('THEME_BRIDGE_KEY', '')
    if not key:
        return 'Private roster editing is not configured. Ask the commissioner to check the bot connection.'
    if ctx.message is None:
        return 'Send this command directly to Theme Bot in a DM.'
    payload = dict(action=action, text=text, logo=matches[0]['logo'],
                   channel=ctx.state['timer_channel_id'], guild=ctx.draft['guild_id'], request_id=ctx.message.id)
    url = os.getenv('TEAM_SHEET_BRIDGE_URL', 'http://atd-team-sheet-bot.internal:8081').rstrip('/')+'/roster-edit'
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            async with session.post(url, json=payload, headers={'Authorization': 'Bearer '+key}) as response:
                result = await response.json()
                if response.status != 200:
                    return result.get('error', 'The roster edit could not be confirmed. Ask the commissioner to check the team sheet.')
                if action == 'roster' and 'roster' in result:
                    return result
                return result.get('message') or result.get('error')
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        if action == 'roster':
            return 'I could not load your team sheet. Please try again in a moment.'
        return 'The roster edit could not be confirmed. Check with the commissioner before retrying a swap; it may already have been applied.'


def roster_embed(data, logo=''):
    embed = discord.Embed(title=data['team'], color=discord.Color.gold())
    rows = data['roster']
    def line(index, label):
        row = rows[index] if index < len(rows) else {}
        player = row.get('player') or '\u2014'
        details = ' \u00b7 '.join(str(row.get(k, '')) for k in ('year', 'price') if row.get(k))
        return f"**{label}** \u00b7 {player}"+(f" \u00b7 {details}" if details and row.get('player') else '')
    embed.add_field(name='Starters', value='\n'.join(line(i, pos) for i, pos in enumerate(('PG', 'SG', 'SF', 'PF', 'C'))), inline=False)
    embed.add_field(name='Bench', value='\n'.join(line(i+5, str(i+1)) for i in range(5)), inline=False)
    count = sum(bool(row.get('player')) for row in rows)
    embed.set_footer(text=f'{count}/10 players')
    match = re.fullmatch(r'<a?:[^:]+:(\d+)>', logo)
    if match:
        embed.set_thumbnail(url=f'https://cdn.discordapp.com/emojis/{match[1]}.png')
    return embed


async def show(ctx, drafter):
    result = await edit(ctx, drafter, 'roster', '')
    if isinstance(result, dict):
        team = next(t for t in suspects.teams(ctx.draft) if drafter['id'] in t['member_ids'])
        return roster_embed(result, team['logo'])
    return result
