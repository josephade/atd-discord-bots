"""Draft List's private Anonymous Budget client; never posts public picks."""
import asyncio
import copy
import os
import uuid

import aiohttp
import discord
from flux_rules import saved_message


class BridgeError(Exception):
    pass


class AnonymousLists:
    HANDLED = {'setlist', 'addpick', 'insertpick', 'removepick', 'clearpicks',
               'pause', 'resume', 'mylist', 'lockpick', 'steal', 'block'}

    def __init__(self, bot, data, save, parse, guild, owner=None, price_source=None):
        self.bot, self.data, self.save, self.parse, self.guild = bot, data, save, parse, guild
        self.owner = owner
        self.price_source = price_source
        self.mode_lock = asyncio.Lock()
        self.locks = {}
        self.task = None

    def linked(self, uid):
        return bool(self.data.get(str(uid), {}).get('anon_draft_id'))

    async def request(self, uid, draft, payload=None, endpoint='picklist'):
        key = os.getenv('THEME_BRIDGE_KEY', '')
        guild = self.guild()
        if not key or not guild:
            raise BridgeError('Anonymous Budget integration is not configured. Ask the commissioner to configure the private Theme Bot connection.')
        url = os.getenv('THEME_BRIDGE_URL', 'http://atd-draft-theme-bot.internal:8081').rstrip('/')+'/'+endpoint
        params = {'draft': str(draft), 'user': str(uid), 'guild': str(guild)}
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                    async with session.request('POST' if payload is not None else 'GET', url, params=params,
                                               json=payload, headers={'Authorization': 'Bearer '+key}) as response:
                        result = await response.json()
                        if response.status != 200:
                            raise BridgeError(result.get('error', 'The private Theme Bot connection rejected this request.'))
                        return result
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                if attempt:
                    raise BridgeError('Theme Bot could not confirm your list. Normal public auto-picking remains disabled; use !mylist to check before retrying.') from None

    def apply(self, uid, snapshot):
        entry = self.data[str(uid)]
        entry.update(picks=snapshot['picks'], enabled=snapshot['enabled'],
                     anon_revision=snapshot['revision'], anon_version=snapshot['version'])
        self.save(self.data)

    async def refresh(self, uid):
        entry = self.data[str(uid)]
        snapshot = await self.request(uid, entry['anon_draft_id'])
        self.apply(uid, snapshot)
        return snapshot

    async def push(self, uid, snapshot, picks, enabled, *, replace_flux=False, submission_id=0):
        payload = dict(picks=picks, enabled=enabled, version=uuid.uuid4().hex,
                       expected_revision=snapshot['revision'])
        if replace_flux:
            payload.update(replace_flux=True, submission_id=submission_id)
        result = await self.request(uid, self.data[str(uid)]['anon_draft_id'], payload)
        self.apply(uid, result)
        return result

    async def add_flux(self, ctx, pick):
        additions = [pick] if isinstance(pick, dict) else pick
        uid = str(ctx.author.id)
        async with self.locks.setdefault(uid, asyncio.Lock()):
            snapshot = await self.refresh(uid)
            if not snapshot.get('can_fluxpick', False):
                raise BridgeError(snapshot.get('fluxpick_error') or 'Finish your current-round pick before setting a Flux rule.')
            picks, seen = [], set()
            for pick in additions:
                if pick['player'].casefold() in seen:
                    raise BridgeError('List each player once in the same message.')
                picks.append(pick)
                seen.add(pick['player'].casefold())
            result = await self.push(uid, snapshot, picks, snapshot['enabled'], replace_flux=True,
                                     submission_id=getattr(getattr(ctx, 'message', None), 'id', 0))
            if result.get('superseded'):
                await ctx.send('Your newer Flux list is already saved; this older message was ignored.')
                return
            await ctx.send(saved_message(result['picks']))
            if result.get('unavailable'):
                await ctx.send('⚠️ Already drafted and removed: **'+', '.join(result['unavailable'])+'**.')

    def parse_list(self, text):
        picks, seen = [], set()
        for line in text.splitlines():
            if line.strip():
                pick = self.parse(line.strip())
                key = pick['player'].casefold()
                if key not in seen:
                    picks.append(pick); seen.add(key)
        if not picks:
            raise BridgeError('DM `!setlist` followed by one player per line, each with a price. Example: `Michael Jordan $44`. Years are optional.')
        return picks

    async def mode(self, ctx, text):
        if self.owner is not None:
            if ctx.author.id != self.owner:
                raise BridgeError('Only the bot owner can change draft mode. Your owner enables Anonymous Budget for every registered GM at once.')
            async with self.mode_lock:
                await self.owner_mode(ctx, text)
            return
        await self.individual_mode(ctx, text)

    async def owner_mode(self, ctx, text):
        parts = text.split()
        if parts == ['normal']:
            drafts = {entry['anon_draft_id'] for entry in self.data.values() if entry.get('anon_draft_id')}
            for draft in drafts:
                await self.request(ctx.author.id, draft, {'action': 'disable'}, endpoint='picklist-members')
                for uid, entry in self.data.items():
                    if entry.get('anon_draft_id') == draft:
                        async with self.locks.setdefault(uid, asyncio.Lock()):
                            entry.pop('anon_draft_id', None)
                            entry.pop('anon_owner_managed', None)
                            entry.update(picks=[], enabled=True)
                self.save(self.data)
            await ctx.send('Normal draft mode enabled for everyone. Anonymous lists have been withdrawn. GMs can set fresh normal-draft lists.')
            return
        if len(parts) != 2 or parts[0] != 'anon' or not parts[1].isdigit():
            raise BridgeError('Owner: use `!draftmode anon <Theme draft ID>` to enable it for every registered GM, or `!draftmode normal` to withdraw everyone.')
        draft = int(parts[1])
        roster = await self.request(ctx.author.id, draft, endpoint='picklist-members')
        members = roster['members']
        for member in members:
            entry = self.data.get(member['user_id'], {})
            if entry.get('anon_draft_id') not in (None, draft):
                raise BridgeError('A GM is still linked to another anonymous draft. Run !draftmode normal before switching drafts.')
        payload = {'action': 'enable'}
        if self.price_source is not None:
            payload['price_source'] = self.price_source()
        roster = await self.request(ctx.author.id, draft, payload, endpoint='picklist-members')
        members = roster['members']
        for member in members:
            uid = member['user_id']
            async with self.locks.setdefault(uid, asyncio.Lock()):
                entry = self.data.setdefault(uid, {'picks': [], 'enabled': True, 'emoji': ''})
                entry.update(anon_draft_id=draft, anon_owner_managed=True)
                self.apply(uid, member)
        await ctx.send(f'Anonymous Budget mode enabled for all {len(members)} registered GMs in Theme draft {draft}, including co-GMs. '
                       'They can DM Draft List !setlist directly; no individual mode command or emoji setup is needed. Fresh lists require prices; years are optional.')

    async def sync_price_source(self, source):
        drafts = {entry['anon_draft_id'] for entry in self.data.values() if entry.get('anon_draft_id')}
        for draft in drafts:
            await self.request(self.owner, draft, {'action': 'prices', 'price_source': source}, endpoint='picklist-members')

    async def individual_mode(self, ctx, text):
        uid = str(ctx.author.id)
        parts = text.split()
        async with self.locks.setdefault(uid, asyncio.Lock()):
            if parts == ['normal']:
                if self.linked(uid):
                    snapshot = await self.refresh(uid)
                    await self.push(uid, snapshot, [], False)
                    self.data[uid].pop('anon_draft_id', None)
                    self.data[uid].update(picks=[], enabled=True)
                    self.save(self.data)
                await ctx.send('Normal draft mode enabled. Your anonymous list has been withdrawn. Set a fresh list for your normal draft.')
                return
            if len(parts) != 2 or parts[0] != 'anon' or not parts[1].isdigit():
                raise BridgeError('Use `!draftmode anon <Theme draft ID>` or `!draftmode normal`. Then DM `!setlist` with player prices.')
            draft = int(parts[1])
            if self.linked(uid) and self.data[uid]['anon_draft_id'] != draft:
                raise BridgeError('Withdraw your previous list with `!draftmode normal` before linking another draft.')
            snapshot = await self.request(uid, draft)
            if snapshot['status'] not in ('active', 'paused'):
                raise BridgeError('That draft is not active or paused. Ask the commissioner to start it first.')
            self.data.setdefault(uid, {'picks': [], 'enabled': True, 'emoji': ''})
            # Never upload an old normal-draft list merely by enabling this mode.
            self.data[uid]['anon_draft_id'] = draft
            self.apply(uid, snapshot)
            await ctx.send(f'Linked privately to Anonymous Budget draft {draft}. DM `!setlist` with one player per line and a price on each. '
                           'Years are optional. Theme Bot validates your budget and availability, and your team’s lists clear after an accepted pick. No team emoji setup is needed.')

    async def handle(self, ctx, text):
        uid, command = str(ctx.author.id), ctx.command.name
        async with self.locks.setdefault(uid, asyncio.Lock()):
            snapshot = await self.refresh(uid)
            picks, enabled = copy.deepcopy(snapshot['picks']), snapshot['enabled']
            if command == 'mylist':
                lines = [f"{i}. {p['player']}"+(f" '{p['year']}" if p.get('year') else '')+
                         (f" [Flux Round {p.get('flux_target_round', '?')}: drop >= ${p['flux_drop']:g}]" if 'flux_drop' in p else f" ${p['price']:g}") for i, p in enumerate(picks, 1)]
                await ctx.send(f"Anonymous Budget · Draft {self.data[uid]['anon_draft_id']} · {'Active' if enabled else 'Paused'}\n"+
                               ('\n'.join(lines) or 'Your list is empty. DM !setlist to add one.'))
                return
            if command in ('lockpick', 'steal', 'block'):
                raise BridgeError('Lock, steal and block commands are not supported in Anonymous Budget.')
            if command == 'setlist':
                picks = self.parse_list(text)
            elif command in ('addpick', 'insertpick'):
                position = len(picks)
                if command == 'insertpick':
                    number, _, text = text.partition(' ')
                    if not number.isdigit() or not 1 <= int(number) <= len(picks)+1:
                        raise BridgeError(f'Use !insertpick <position 1–{len(picks)+1}> <player> [$year] $price.')
                    position = int(number)-1
                new = self.parse_list(text)
                if len(new) != 1:
                    raise BridgeError('Add one player at a time, or use !setlist for multiple players.')
                if any(p['player'].casefold() == new[0]['player'].casefold() for p in picks):
                    raise BridgeError('That player is already in your list.')
                picks.insert(position, new[0])
            elif command == 'removepick':
                if not text.isdigit() or not 1 <= int(text) <= len(picks):
                    raise BridgeError('Use !removepick <position from !mylist>.')
                picks.pop(int(text)-1)
            elif command == 'clearpicks':
                picks = []
            elif command in ('pause', 'resume'):
                enabled = command == 'resume'
            result = await self.push(uid, snapshot, picks, enabled)
            await ctx.send(f"Anonymous list confirmed by Theme Bot: {len(result['picks'])} options; {'active' if result['enabled'] else 'paused'}. "
                           'If your team is already on the clock, Theme Bot may have submitted an option immediately. Use !mylist to check.')

    async def check(self, ctx):
        command = ctx.command.name
        if command == 'draftmode' or not self.linked(ctx.author.id) or command not in self.HANDLED:
            return True
        if ctx.guild is not None:
            await ctx.send('DM Draft List Bot to manage your anonymous list.')
            return False
        text = ctx.message.content.split(maxsplit=1)
        try:
            await self.handle(ctx, text[1].strip() if len(text) > 1 else '')
        except BridgeError as exc:
            await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
        return False

    async def poll(self):
        while not self.bot.is_closed():
            for uid in list(self.data):
                if self.linked(uid):
                    async with self.locks.setdefault(uid, asyncio.Lock()):
                        try:
                            await self.refresh(uid)
                        except BridgeError:
                            pass
            await asyncio.sleep(15)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.poll())
