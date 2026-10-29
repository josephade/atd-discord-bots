"""Private authenticated clock API. Responses contain no GM IDs or budgets."""
import hmac
import os
import time

from aiohttp import web
import db
import themes
from themes.base import DraftContext, draft_lock

MODES = {'partner_draft': 'partner', 'anon_budget_draft': 'anon'}
_runner = None


def snapshot(draft):
    state = draft['state']
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
            if deadline is not None and pick is not None:
                clock = {'token': f'a:{i}:{pick}', 'deadline': deadline,
                         'round': (pick - 1) // max(1, len(state['teams'])) + 1,
                         'pick': pick, 'logo': team['logo']}
                clocks.append(clock)
                if pick == state.get('live_pick_number'):
                    current = clock
        rounds = draft['settings'].get('rounds', 10)
    waiting = state.get('flux_wait', {}).get('round')
    finished = themes.get_theme(draft['theme']).is_pick_phase_complete(DraftContext(None, draft['id']))
    scheduled_remaining = (state.get('turn_index', 0) < len(state.get('turns', [])) if mode == 'partner'
                           else state.get('live_pick_number') is not None)
    return {'draft_id': draft['id'], 'mode': mode, 'status': draft['status'],
            'rounds': rounds, 'current': None if waiting else current,
            'waiting_for_flux': waiting,
            'announcement_revision': state.get('announcement_revision', 0),
            'transitioning': bool(draft['status'] == 'active' and scheduled_remaining and not current and not waiting and not finished),
            'clocks': clocks if draft['status'] == 'active' and not waiting else [],
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


def create_app(bot, key):
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
            await expire(bot, draft_id, str(payload.get('token', '')))
        return web.json_response(snapshot(db.get_draft(draft_id)))

    app = web.Application(client_max_size=4096)
    app.router.add_get('/clock/{draft}', handle)
    app.router.add_post('/clock/{draft}', handle)
    return app


async def start(bot):
    global _runner
    key = os.getenv('THEME_BRIDGE_KEY', '')
    if _runner or not key:
        return
    app = create_app(bot, key)
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, '::', int(os.getenv('THEME_BRIDGE_PORT', '8081'))).start()
