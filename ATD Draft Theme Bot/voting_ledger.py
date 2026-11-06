"""Read-only authenticated ledger for the terminal-hosted Voting Bot."""
import hmac
import os
from aiohttp import web
import db
import suspects

_runner = None


def payload(draft):
    totals = {tuple(r['member_ids']): r for r in suspects.win_deduction_summary(draft)}
    entries = []
    for team in suspects.teams(draft):
        users = tuple(sorted(db.get_drafter(mid)['user_id'] for mid in team['member_ids']))
        summary = totals.get(users, {})
        team_name = suspects._ALIASES.get(suspects._emoji_name(team.get('logo', '')), '')
        entries.append(dict(team_name=team_name, user_ids=db.anonymous_ledger_users(draft['id'], users),
                            caught_wins=summary.get('caught_wins', 0),
                            market_wins=summary.get('market_wins', 0),
                            bought_guesses=summary.get('bought_guesses', 0)))
    return dict(theme_draft_id=draft['id'], guild_id=draft['guild_id'], teams=entries)


def create_app(key):
    async def health(request):
        return web.json_response({'ok': True})

    async def ledger(request):
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + key):
            raise web.HTTPUnauthorized()
        try:
            draft_id = int(request.match_info['draft'])
            guild_id = int(request.query['guild'])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest()
        draft = db.get_draft(draft_id)
        if not draft or draft['guild_id'] != guild_id or draft['theme'] not in suspects.ANON_THEMES:
            raise web.HTTPNotFound()
        return web.json_response(payload(draft))

    app = web.Application()
    app.router.add_get('/health', health)
    app.router.add_get('/penalties/{draft}', ledger)
    return app


async def start():
    global _runner
    key = os.getenv('VOTING_LEDGER_KEY', '')
    if _runner or not key:
        return
    _runner = web.AppRunner(create_app(key))
    await _runner.setup()
    await web.TCPSite(_runner, '0.0.0.0', 8082).start()
