"""Publish anonymous clocks and window notices as Draft Theme Bot."""
import json
import logging
import discord
import db
import anon_clock
from clock_card import clock_embed
from themes.base import DraftContext, draft_lock


async def _publish_card(bot, draft_id):
    from timer_bridge import snapshot
    ctx = DraftContext(bot, draft_id)
    draft = ctx.draft
    if not draft or draft['theme'] != 'anon_budget_draft' or draft['status'] == 'cancelled':
        return
    state = ctx.state
    previous = state.get('theme_clock_card', {})
    if draft['status'] not in ('active', 'paused') and not previous:
        return
    channel_id = state.get('timer_channel_id')
    channel = bot.get_channel(channel_id) if channel_id else None
    if not channel:
        return
    data = snapshot(draft)
    if data.get('transitioning'):
        return
    if data['status'] == 'active' and data.get('current') and data['current'].get('deadline') is None and not data.get('timers_off'):
        return  # Opening a turn: wait until the saved deadline is ready.
    fields = ('status', 'finished', 'current', 'rounds', 'waiting_for_flux',
              'announcement_revision', 'timers_off', 'window_closed', 'window_notice')
    signature = json.dumps({k: data.get(k) for k in fields}, sort_keys=True)
    card = clock_embed(data)
    if previous.get('signature') != signature or previous.get('channel') != channel_id:
        message = await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())
        db.update_draft_state(draft_id, {'theme_clock_card': dict(id=message.id, channel=channel_id, signature=signature)})
        if previous.get('id') and previous.get('channel') == channel_id:
            try:
                await channel.get_partial_message(previous['id']).delete()
            except discord.HTTPException:
                pass
    return data


async def publish(bot, draft_id):
    # Pick handling can await DMs before saving the full team state. Share
    # its lock so a late save cannot erase the newly posted card's receipt.
    async with draft_lock(draft_id):
        data = await _publish_card(bot, draft_id)
    if data is None:
        return
    ctx = DraftContext(bot, draft_id)
    info = data.get('window_notice')
    if not info or info['key'] == data.get('window_delivery_complete'):
        return
    delivery = await anon_clock.window_delivery(draft_id)
    schedule_card = discord.Embed(title='Draft timer hours', description=info['text'], color=0x3498DB)
    for uid in delivery['pending_users']:
        if await ctx.dm(uid, embed=schedule_card):
            await anon_clock.window_delivery(draft_id, dict(key=delivery['key'], users=[uid], public=True))
    await anon_clock.window_delivery(draft_id, dict(key=delivery.get('key'), users=[], public=True))
