"""Private authenticated clock API. Responses contain no GM IDs or budgets."""
import hmac
import os
import time

from aiohttp import web
import db
import themes
import anon_clock
from themes.base import DraftContext, draft_lock

MODES = {'partner_draft': 'partner', 'anon_budget_draft': 'anon'}
_runner = None


def snapshot(draft):
    state = draft['state']
    now=time.time()
    settings = themes.get_theme(draft['theme'])._settings(DraftContext(None, draft['id']))
    mode = MODES[draft['theme']]
    clocks = []
    current = None
    if mode == 'partner':
        turn = themes.get_theme('partner_draft')._turn(state) if state.get('turns') else None
        for i, pair in enumerate(state.get('pairs', [])):
            for slot, deadline in pair.get('deadlines', {}).items():
                clock = {'token': f'p:{i}:{slot}', 'deadline': deadline,
                         'round': int(slot) + 1, 'logo': pair.get('logo', f'🏀 Team {i + 1}')}
                clock['pick'] = next((n + 1 for n, t in enumerate(state.get('turns', []))
                                      if t == {'pair': i, 'round': int(slot)}), None)
                clocks.append(clock)
                if turn == {'pair': i, 'round': int(slot)}:
                    current = clock
        rounds = draft['settings'].get('roster_size', 10)
    else:
        for i, team in enumerate(state.get('teams', [])):
            deadline = team.get('timer_deadline')
            pick = team.get('timer_pick_number')
            if pick is not None:
                clock = {'token': f'a:{i}:{pick}', 'deadline': deadline,
                         'round': (pick - 1) // max(1, len(state['teams'])) + 1,
                         'pick': pick, 'logo': team['logo']}
                clock['timer_minutes']=anon_clock.minutes(settings,pick,len(state['teams']),team.get('skip_count',0))
                clock.update(anon_clock.clock_details(deadline,settings,now))
                if deadline is not None:
                    clocks.append(clock)
                if pick == state.get('live_pick_number'):
                    current = clock
        rounds = draft['settings'].get('rounds', 10)
    waiting = state.get('flux_wait', {}).get('round')
    clock_running = (anon_clock.running(state, settings, now) if mode == 'anon' else True)
    finished = themes.get_theme(draft['theme']).is_pick_phase_complete(DraftContext(None, draft['id']))
    scheduled_remaining = (state.get('turn_index', 0) < len(state.get('turns', [])) if mode == 'partner'
                           else state.get('live_pick_number') is not None)
    return {'draft_id': draft['id'], 'mode': mode, 'status': draft['status'],
            'presenter': 'theme' if mode == 'anon' else 'timer',
            'rounds': rounds, 'current': None if waiting else current,
            'window_notice': anon_clock.window_info(draft,now),
            'window_delivery_complete': state.get('theme_window_delivery', {}).get('key') if state.get('theme_window_delivery', {}).get('complete') else None,
            'waiting_for_flux': waiting,
            'timers_off': bool(state.get('timers_off')),
            'window_closed': mode == 'anon' and settings.get('timer_window_et', True) and not anon_clock.in_window(now),
            'announcement_revision': state.get('announcement_revision', 0),
            'transitioning': bool(draft['status'] == 'active' and scheduled_remaining and not current and not waiting and not finished),
            'clocks': clocks if draft['status'] == 'active' and not waiting and clock_running else [],
            'finished': finished}


async def expire(bot, draft_id, token):
    draft = db.get_draft(draft_id)
    if not draft or draft['status'] != 'active':
        return
    ctx = DraftContext(bot, draft_id)
    # A stale/retried expiry never resolves a newer turn.
    clock = next((c for c in snapshot(draft)['clocks'] if c['token'] == token), None)
    if clock is None or clock['deadline'] > time.time():
        return
    theme = themes.get_theme(draft['theme'])
    kind, team, slot = token.split(':')
    if kind == 'p':
        async with draft_lock(draft_id):
            fresh = next((c for c in snapshot(ctx.draft)['clocks'] if c['token'] == token), None)
            if ctx.draft['status'] != 'active' or fresh is None or fresh['deadline'] > time.time():
                return
            await theme._resolve_locked(ctx, int(team), int(slot), forced=True)
            await theme._drain_locked(ctx)
    else:
        await theme._resolve_on_timeout(ctx, int(team), int(slot))


async def skip_turn(bot, draft_id, payload):
    from config import OWNER_ID
    actor = int(payload.get('actor_id', 0))
    request_id = int(payload.get('request_id', 0))
    async with draft_lock(draft_id):
        draft = db.get_draft(draft_id)
        if not draft or draft['status'] != 'active':
            raise ValueError('The draft must be active before skipping a turn.')
        if draft['theme'] != 'anon_budget_draft':
            raise ValueError('Manual !timerskip is currently available for Anonymous Budget drafts.')
        state = draft['state']
        if request_id and request_id in state.get('skip_request_ids', []):
            return
        current = snapshot(draft).get('current')
        if not current or current['token'] != payload.get('token'):
            raise ValueError('The turn changed or no team is on the clock. Check the latest timer prompt.')
        _, index, pick_number = current['token'].split(':')
        index, pick_number = int(index), int(pick_number)
        members = [db.get_drafter(mid)['user_id'] for mid in state['teams'][index]['member_ids']]
        permitted = actor in members or actor == OWNER_ID
        if not permitted:
            guild = bot.get_guild(draft['guild_id'])
            member = guild.get_member(actor) if guild else None
            if member is not None:
                roles = getattr(member, 'roles', [])
                permitted = (getattr(getattr(member, 'guild_permissions', None), 'administrator', False) is True
                             or isinstance(roles, (list, tuple)) and any(r.name == 'LeComissioner' for r in roles))
        if not permitted:
            raise ValueError('Only a GM on the current team, a commissioner, or the owner can skip this turn.')
        if request_id:
            state['skip_request_ids'] = (state.get('skip_request_ids', [])+[request_id])[-500:]
            db.set_draft_state(draft_id, state)
        entry = next(p for p in state['teams'][index]['open_picks'] if p['pick_number'] == pick_number)
        await themes.get_theme(draft['theme'])._skip_locked(DraftContext(bot, draft_id), index, entry, 'Your team passed its turn with !timerskip.')


def create_app(bot, key):
    async def picklist_members(request):
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + key):
            raise web.HTTPUnauthorized()
        import anon_lists
        try:
            draft_id, user_id, guild_id = (int(request.query[k]) for k in ('draft', 'user', 'guild'))
            action = None
            if request.method == 'POST':
                payload = await request.json()
                action = payload.get('action') if isinstance(payload, dict) else ''
                if action not in ('enable', 'disable', 'prices'):
                    raise ValueError('Use enable or disable for the owner draft mode.')
            source = payload.get('price_source') if request.method == 'POST' else None
            result = await anon_lists.roster(draft_id, user_id, guild_id, action=action, price_source=source)
            return web.json_response(result)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def picklist(request):
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + key):
            raise web.HTTPUnauthorized()
        import anon_lists
        try:
            draft_id, user_id, guild_id = (int(request.query[k]) for k in ('draft', 'user', 'guild'))
            if request.method == 'POST':
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError('Expected a list update object.')
                result = await anon_lists.update(bot, draft_id, user_id, guild_id, payload)
            else:
                result = anon_lists.view(*anon_lists.participant(draft_id, user_id, guild_id))
            return web.json_response(result)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def handle(request):
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + key):
            raise web.HTTPUnauthorized()
        try:
            draft_id = int(request.match_info['draft'])
            channel = int(request.query['channel'])
            guild = int(request.query['guild'])
        except (ValueError, KeyError):
            raise web.HTTPBadRequest()
        draft = db.get_draft(draft_id)
        if (not draft or draft['theme'] not in MODES or draft['guild_id'] != guild
                or draft['state'].get('timer_channel_id') != channel):
            raise web.HTTPNotFound()
        if request.method == 'POST':
            payload = await request.json()
            if payload.get('action') == 'window-notice':
                return web.json_response(await anon_clock.window_delivery(draft_id, ack=payload.get('notice')))
            elif payload.get('action') in ('timeron', 'timeroff'):
                try:
                    await anon_clock.toggle(bot, draft_id, int(payload.get('actor_id', 0)), payload['action'] == 'timeron')
                except (ValueError, TypeError) as exc:
                    return web.json_response({'error': str(exc)}, status=400)
            elif payload.get('action') == 'skip':
                try:
                    await skip_turn(bot, draft_id, payload)
                except (ValueError, TypeError) as exc:
                    return web.json_response({'error': str(exc)}, status=400)
            else:
                await expire(bot, draft_id, str(payload.get('token', '')))
        return web.json_response(snapshot(db.get_draft(draft_id)))

    app = web.Application(client_max_size=32768)
    app.router.add_get('/picklist-members', picklist_members)
    app.router.add_post('/picklist-members', picklist_members)
    app.router.add_get('/picklist', picklist)
    app.router.add_post('/picklist', picklist)
    app.router.add_get('/clock/{draft}', handle)
    app.router.add_post('/clock/{draft}', handle)
    return app


async def start(bot):
    global _runner
    import voting_ledger
    await voting_ledger.start()
    key = os.getenv('THEME_BRIDGE_KEY', '')
    if _runner or not key:
        return
    app = create_app(bot, key)
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, '::', int(os.getenv('THEME_BRIDGE_PORT', '8081'))).start()
