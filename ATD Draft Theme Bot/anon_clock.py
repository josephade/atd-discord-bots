"""Anonymous clocks count only 10:00-midnight America/New_York time."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def in_window(timestamp):
    return datetime.fromtimestamp(timestamp, ET).hour >= 10


def add_seconds(timestamp, seconds):
    current = timestamp
    remaining = max(0, seconds)
    while True:
        local = datetime.fromtimestamp(current, ET)
        opening = datetime.combine(local.date(), time(10), ET).timestamp()
        closing = datetime.combine(local.date()+timedelta(days=1), time(), ET).timestamp()
        current = max(current, opening)
        room = closing-current
        if remaining <= room:
            return current+remaining
        remaining -= room
        current = datetime.combine(local.date()+timedelta(days=1), time(10), ET).timestamp()


def seconds_left(deadline, timestamp):
    total = 0
    current = timestamp
    while current < deadline:
        local = datetime.fromtimestamp(current, ET)
        opening = datetime.combine(local.date(), time(10), ET).timestamp()
        closing = datetime.combine(local.date()+timedelta(days=1), time(), ET).timestamp()
        total += max(0, min(deadline, closing)-max(current, opening))
        current = closing
    return total


def minutes(settings, pick, team_count, skips):
    round_number = (pick-1)//max(1, team_count)+1
    base = settings['base_timer_minutes']
    if settings.get('round_timer_schedule', True):
        base = 60 if round_number <= 2 else 45 if round_number <= 8 else 30
    deduction = 10 if settings.get('round_timer_schedule', True) else settings['decay_minutes']
    return max(settings.get('floor_minutes', 0), base-deduction*skips)


def deadline(timestamp, seconds, settings):
    return add_seconds(timestamp, seconds) if settings.get('timer_window_et', True) else timestamp+seconds


def remaining(deadline_value, timestamp, settings):
    return seconds_left(deadline_value, timestamp) if settings.get('timer_window_et', True) else max(0, deadline_value-timestamp)


def running(state, settings, timestamp):
    return not state.get('timers_off') and (not settings.get('timer_window_et', True) or in_window(timestamp))


async def toggle(bot, draft_id, actor_id, enabled):
    import db
    import themes
    from config import OWNER_ID
    from themes.base import DraftContext, draft_lock
    if actor_id != OWNER_ID:
        raise ValueError('Only the bot owner can turn anonymous draft timers on or off.')
    async with draft_lock(draft_id):
        ctx = DraftContext(bot, draft_id)
        draft = ctx.draft
        if not draft or draft['theme'] not in ('anon_budget_draft', 'anon_draft'):
            raise ValueError('Select an Anonymous Budget or Anonymous Draft.')
        if draft['status'] != 'active':
            raise ValueError('The draft must be active. Timer controls do not resume a manually paused draft.')
        if bool(ctx.state.get('timers_off')) == (not enabled):
            await notify_current(ctx, enabled)
            return
        theme = themes.get_theme(draft['theme'])
        if not enabled:
            await theme.cancel(ctx)
        db.update_draft_state(draft_id, {'timers_off': not enabled})
    if enabled:
        await theme.resume_timers(ctx)
    await notify_current(ctx, enabled)


async def notify_current(ctx, enabled):
    import db
    import time as clock_time
    import themes
    state = ctx.state
    if (ctx.draft['status'] != 'active' or bool(state.get('timers_off')) != (not enabled)
            or state.get('flux_wait')):
        return
    live = state.get('live_pick_number')
    if live is None:
        return
    team = next((team for team in state.get('teams', [])
                 if team.get('timer_pick_number') == live and any(
                     p['pick_number'] == live and not p.get('skipped') for p in team.get('open_picks', []))), None)
    if team is None:
        return
    notice_key = f'{live}:{"on" if enabled else "off"}'
    if state.get('last_timer_notice') == notice_key:
        return
    if not enabled:
        notice = (f'\u23f8\ufe0f **Timers are now off \u00b7 Pick #{live}**\n'
                  'Your remaining time is frozen. You can still DM `!pick` to submit your player. '
                  'There is no automatic skip or teammate-approval timeout while timers are off.')
    else:
        settings = themes.get_theme(ctx.draft['theme'])._settings(ctx)
        active = running(state, settings, clock_time.time())
        notice = (f'\u25b6\ufe0f **Timers are back on \u00b7 Pick #{live}**\n'+
                  ('Your countdown has resumed with your saved remaining time.' if active else
                   'Timer hours are currently closed. Your saved countdown resumes at **10am ET**. You can still pick now.'))
        if team.get('timer_deadline') is not None:
            notice += f"\nPick deadline: <t:{int(team['timer_deadline'])}:R>."
    for member_id in dict.fromkeys(team['member_ids']):
        member = db.get_drafter(member_id)
        if member:
            await ctx.dm(member['user_id'], notice)
    db.update_draft_state(ctx.draft_id, {'last_timer_notice': notice_key})


def window_info(draft, timestamp=None):
    import time as clock_time
    if draft['theme'] != 'anon_budget_draft' or draft['status'] != 'active' or not draft['settings'].get('timer_window_et', True):
        return None
    now = clock_time.time() if timestamp is None else timestamp
    local = datetime.fromtimestamp(now, ET)
    opened = in_window(now)
    key = f'{local.date()}:{"open" if opened else "closed"}'
    opening = int(datetime.combine(local.date(), time(10), ET).timestamp())
    closing = int(datetime.combine(local.date()+timedelta(days=1), time(), ET).timestamp())
    manual = draft['state'].get('timers_off')
    if not opened:
        text = ('**Timers are OFF for the night**\n'
                f'Resume: **10:00 AM ET** (<t:{opening}:F> / <t:{opening}:R>).\n'
                'Remaining time is preserved. You can still DM `!pick` overnight.')
        if manual:
            text += '\nThe commissioner has also disabled timers; they stay off until manually enabled.'
    elif manual:
        text = '**Timer hours are OPEN - 10:00 AM ET**\nTimers remain OFF until the commissioner enables them. You can still pick.'
    else:
        text = ('**Timers are back ON - 10:00 AM ET**\nCountdowns resume with saved remaining time.\n'
                f'Next closure: **midnight ET** (<t:{closing}:F>).')
    return dict(key=key, text=text, opened=opened)


async def window_delivery(draft_id, ack=None, timestamp=None):
    """Theme delivery receipts; earlier Timer Bot DMs do not suppress Theme DMs."""
    import db
    from themes.base import draft_lock
    async with draft_lock(draft_id):
        draft = db.get_draft(draft_id)
        info = window_info(draft, timestamp)
        if not info:
            return dict(mode='anon', pending_users=[], public=True)
        state = draft['state']
        receipt = state.get('theme_window_delivery', {})
        if receipt.get('key') != info['key']:
            baseline = not receipt
            receipt = dict(key=info['key'], users=[], public=baseline, complete=baseline)
        members = {mid for team in state.get('teams', []) for mid in team.get('member_ids', [])}
        users = {d['user_id'] for d in db.list_drafters(draft_id) if d['id'] in members}
        if ack and ack.get('key') == info['key'] and not receipt.get('complete'):
            delivered = set(receipt.get('users', [])) | (set(ack.get('users', [])) & users)
            receipt.update(users=sorted(delivered), public=receipt.get('public') or ack.get('public') is True)
            receipt['complete'] = users.issubset(delivered) and receipt['public']
        db.update_draft_state(draft_id, {'theme_window_delivery': receipt})
        return dict(mode='anon', key=info['key'], public=receipt.get('public', False),
                    pending_users=[] if receipt.get('complete') else sorted(users-set(receipt.get('users', []))))
