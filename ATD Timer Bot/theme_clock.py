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
        self.silent_channels = set()

    async def fetch(self, session, token=None, action=None, actor_id=None, request_id=None, notice=None):
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
        payload = {'token': token} if token else None
        if action:
            payload = dict(token=token, action=action, actor_id=actor_id, request_id=request_id)
            if notice is not None:
                payload["notice"] = notice
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as client:
            async with client.request('POST' if token or action else 'GET', path, params=params,
                                      headers=headers, json=payload) as response:
                if response.status != 200:
                    if response.status == 400:
                        error = await response.json()
                        raise ValueError(error.get('error', 'The turn could not be skipped.'))
                    raise ValueError('Theme Bot has not authorized this draft/channel link, or is unavailable.')
                data = await response.json()
        if data['mode'] != session.draft.mode:
            raise ValueError('Timer mode does not match the linked Theme draft. Use anon or partner as appropriate.')
        return data

    async def skip(self, session, actor_id, request_id):
        data = await self.fetch(session)
        if data.get('status') != 'active' or not data.get('current'):
            raise ValueError('No team is currently on the clock. Resume the draft or complete Flux first.')
        return await self.fetch(session, token=data['current']['token'], action='skip', actor_id=actor_id, request_id=request_id)

    def embed(self, data):
        from clock_card import clock_embed
        return clock_embed(data)

    async def render(self, session, data, force=False):
        if data.get('presenter') == 'theme':
            if session.channel_id not in self.silent_channels:
                old = self.messages.pop(session.channel_id, None)
                try:
                    if old:
                        await old.delete()
                    elif hasattr(session.channel, 'history'):
                        async for message in session.channel.history(limit=30):
                            if message.author.id == self.bot.user.id and any(
                                    e.title == 'Anonymous Budget Draft' for e in message.embeds):
                                await message.delete()
                                break
                except discord.HTTPException:
                    pass
                self.silent_channels.add(session.channel_id)
                self.signatures.pop(session.channel_id, None)
            return
        current = data.get('current')
        signature = (data['status'], data.get('finished'), str(current),
                     data.get('waiting_for_flux'), data.get('announcement_revision', 0), data.get('timers_off'), data.get('window_closed'), str(data.get('window_notice')))
        if not force and self.signatures.get(session.channel_id) == signature:
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
        if data.get('presenter') == 'theme':
            await self.render(session, data)
            return
        info = data.get('window_notice')
        if info and info['key'] != data.get('window_delivery_complete'):
            delivery = await self.fetch(session, action='window-notice')
            await self.render(session, data, force=not delivery.get('public', True))
            sent = []
            for uid in delivery.get('pending_users', []):
                try:
                    user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                    await user.send(embed=self.embed(data), allowed_mentions=discord.AllowedMentions.none())
                    sent.append(uid)
                except discord.HTTPException:
                    pass
            await self.fetch(session, action='window-notice', notice=dict(key=delivery.get('key'), users=sent, public=True))
        else:
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
