"""Anonymous Theme Bot clocks; no identities, spending or pick selection here."""
import asyncio
import os
import time

import aiohttp
import discord


class ThemeClocks:
    def __init__(self, bot, sessions):
        self.bot, self.sessions = bot, sessions
        self.task = None
        self.messages = {}
        self.signatures = {}
        self.errors = set()

    async def fetch(self, session, token=None):
        key = os.getenv('THEME_BRIDGE_KEY', '')
        if not key:
            raise ValueError('The Theme Bot connection is not configured.')
        channel = session.channel
        if not channel:
            raise ValueError('The draft channel is unavailable.')
        url = os.getenv('THEME_BRIDGE_URL', 'http://atd-draft-theme-bot.internal:8081')
        params = {'channel': str(session.channel_id), 'guild': str(channel.guild.id)}
        headers = {'Authorization': 'Bearer ' + key}
        path = f'{url}/clock/{session.draft.theme_draft_id}'
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as client:
            async with client.request('POST' if token else 'GET', path, params=params,
                                      headers=headers, json={'token': token} if token else None) as response:
                if response.status != 200:
                    raise ValueError('Theme Bot has not authorized this draft/channel link, or is unavailable.')
                data = await response.json()
        if data['mode'] != session.draft.mode:
            raise ValueError('Timer mode does not match the linked Theme draft. Use anon or partner as appropriate.')
        return data

    def embed(self, data):
        current = data.get('current')
        if data.get('finished'):
            text = 'All picks and makeups are finished. The commissioner can reveal the draft.'
        elif data.get('waiting_for_flux'):
            upcoming = data['waiting_for_flux']
            text = (f'**Round {upcoming - 1} finished.**\n\n'
                    'Waiting for a commissioner to run **ATD Flux Bot** (`!flux 3`, `!flux 4` or `!flux 5`).\n'
                    f'Round {upcoming} will open after Flux completes. **No team is on the clock.**')
        elif data['status'] != 'active':
            text = f'Draft **{data["status"]}**. Waiting for Theme Bot.'
        elif data.get('transitioning'):
            text = 'Preparing the next team’s turn.'
        elif not current:
            text = 'Scheduled turns finished. Waiting for outstanding makeup picks.'
        else:
            pick_note = f' · Pick {current["pick"]}' if current.get('pick') else ''
            text = (f'{current["logo"]} **is on the clock!**\n\n**Round {current["round"]} of {data["rounds"]}{pick_note}**\n'
                    f'Turn expires <t:{int(current["deadline"])}:R>.\n'
                    'Submit your pick/list privately to ATD Draft Theme Bot.')
        return discord.Embed(title='Anonymous Partner Draft' if data['mode'] == 'partner' else 'Anonymous Budget Draft',
                             description=text, color=0x3498DB)

    async def render(self, session, data):
        current = data.get('current')
        signature = (data['status'], data.get('finished'), str(current),
                     data.get('waiting_for_flux'), data.get('announcement_revision', 0))
        if self.signatures.get(session.channel_id) == signature:
            return
        kwargs = {'embed': self.embed(data), 'allowed_mentions': discord.AllowedMentions.none()}
        old = self.messages.get(session.channel_id)
        new = await session.channel.send(**kwargs)
        self.messages[session.channel_id] = new
        self.signatures[session.channel_id] = signature
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def tick(self, session):
        data = await self.fetch(session)
        # Each callback is idempotent at Theme Bot. Lost replies can be retried.
        for clock in data.get('clocks', []):
            if clock['deadline'] <= time.time():
                data = await self.fetch(session, clock['token'])
        if data.get('transitioning'):
            return
        await self.render(session, data)

    def start(self):
        if self.task and not self.task.done():
            return
        async def run():
            while not self.bot.is_closed():
                for session in list(self.sessions.values()):
                    if not session.draft.theme_draft_id or not session.channel:
                        continue
                    try:
                        await self.tick(session)
                        self.errors.discard(session.channel_id)
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        # Never skip locally or fall back to an old snake order.
                        if session.channel_id not in self.errors:
                            await session.channel.send(
                                'Clock updates are temporarily unavailable. Reconnecting to Theme Bot.',
                                allowed_mentions=discord.AllowedMentions.none())
                            self.errors.add(session.channel_id)
                            self.signatures.pop(session.channel_id, None)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception('Anonymous clock update failed')
                await asyncio.sleep(2)
        self.task = asyncio.create_task(run())
