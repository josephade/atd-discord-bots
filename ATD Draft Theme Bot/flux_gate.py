"""Round boundaries wait for a genuine, new Flux Bot completion."""
import re
import time

import db
import themes
from config import FLUX_BOT_ID
from themes.base import DraftContext, draft_lock


def hold_for_round(ctx, state, round_number):
    if (not state.get('timer_channel_id') or not ctx.settings.get('require_flux', True)
            or round_number <= state.get('flux_ready_round', 1)):
        return False
    if not state.get('flux_wait'):
        state['flux_wait'] = {'round': round_number, 'since': time.time()}
    ctx.save_state(state)
    return True


async def observe(bot, message):
    if message.author.id != FLUX_BOT_ID or not message.author.bot:
        return False
    text = message.content.replace('*', '').strip()
    match = re.fullmatch(r'✅\s*Round\s+(\d+)\s+Flux is done', text, re.I)
    if not match:
        return False
    for draft in db.list_drafts():
        if draft['theme'] not in ('anon_budget_draft', 'partner_draft'):
            continue
        if draft['status'] not in ('active', 'paused'):
            continue
        if draft['state'].get('timer_channel_id') != message.channel.id:
            continue
        async with draft_lock(draft['id']):
            ctx = DraftContext(bot, draft['id'])
            state = ctx.state
            wait = state.get('flux_wait')
            if (not wait or message.created_at.timestamp() < wait['since']
                    or message.id <= state.get('last_flux_message_id', 0)):
                continue
            state['flux_ready_round'] = wait['round']
            state['last_flux_message_id'] = message.id
            state['last_flux_sheet_round'] = int(match.group(1))
            state.pop('flux_wait', None)
            ctx.save_state(state)
            if ctx.draft['status'] == 'active':
                theme = themes.get_theme(draft['theme'])
                if draft['theme'] == 'partner_draft':
                    await theme._drain_locked(ctx)
                else:
                    pick = state.get('live_pick_number')
                    if pick:
                        idx = theme._team_index_for_pick(pick, len(state['teams']), theme._settings(ctx)['rounds'])
                        await theme._open_pick_for_team(ctx, idx, pick)
    return True


async def recover(bot):
    # A completion posted during a Theme Bot restart must not leave a hold stuck.
    channels = {d['state']['timer_channel_id'] for d in db.list_drafts()
                if d['state'].get('flux_wait') and d['state'].get('timer_channel_id')
                and d['status'] in ('active', 'paused')}
    for channel_id in channels:
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        async for message in channel.history(limit=100, oldest_first=False):
            if message.author.id == FLUX_BOT_ID:
                await observe(bot, message)
