"""Authenticated internal roster edits. Responses never go to a Discord channel."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web


def create_app(key, resolve_team, get_channel, swap, addyear, receipt_path, roster=None):
    app = web.Application(client_max_size=8192)
    lock = asyncio.Lock()
    path = Path(receipt_path)

    async def edit(request):
        if not key or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer '+key):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
            action, text = payload['action'], payload['text'].strip()
            channel_id, guild_id = int(payload['channel']), int(payload['guild'])
            request_id = str(int(payload['request_id']))
            team_name = resolve_team(payload['logo'])
            channel = get_channel(channel_id)
            if action not in ('swap', 'addyear', 'roster') or (not text and action != 'roster') or len(text) > 500 or not team_name:
                raise ValueError()
            if channel is None or channel.guild.id != guild_id:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            return web.json_response({'error': 'Invalid private roster edit or unmapped team.'}, status=400)
        async with lock:
            if action == 'roster':
                if roster is None:
                    return web.json_response({'error': 'Private roster view is unavailable.'}, status=503)
                return web.json_response(await roster(channel_id, team_name))
            try:
                receipts = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                receipts = {}
            if request_id in receipts:
                return web.json_response(receipts[request_id])
            def save():
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps(receipts), encoding='utf-8')
                temp.replace(path)
            # Persist a guard before swapping: an interrupted response must
            # never cause a retry to swap the same two rows back again.
            receipts[request_id] = {'error': 'This edit may already have run. Ask the commissioner to check the roster before sending another swap.'}
            save()
            responses = []
            async def send(content=None, embed=None, **kwargs):
                responses.append(content if content is not None else embed.description or embed.title)
            @asynccontextmanager
            async def typing():
                yield
            ctx = SimpleNamespace(channel=SimpleNamespace(id=channel_id, typing=typing),
                                  author=SimpleNamespace(display_name='Anonymous GM'), send=send,
                                  command=SimpleNamespace(name=action, qualified_name=action),
                                  clean_prefix='!', invoked_with=action)
            if action == 'swap':
                await swap(ctx, text, team_name, current_sheet_only=True)
            else:
                await addyear(ctx, text, team_name, current_sheet_only=True)
            result = {'message': '\n'.join(responses) or 'Roster edit completed.'}
            receipts[request_id] = result
            # Discord message IDs are globally unique; retain a bounded log.
            receipts = dict(list(receipts.items())[-2000:])
            save()
            return web.json_response(result)
    app.router.add_post('/roster-edit', edit)
    return app


_runner = None


async def start(bot, resolve_team, swap, addyear, roster):
    global _runner
    key = os.getenv('THEME_BRIDGE_KEY', '')
    if _runner is not None or not key:
        return
    _runner = web.AppRunner(create_app(key, resolve_team, bot.get_channel, swap, addyear,
                                     '/data/anonymous_roster_receipts.json', roster=roster))
    await _runner.setup()
    await web.TCPSite(_runner, '::', 8081).start()
