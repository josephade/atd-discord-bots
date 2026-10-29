"""Anonymous pairs, two-round shuffled snake blocks, and persistent makeups."""
import asyncio
import math
import random
import re
import time
import unicodedata
import copy

import db
import sheets
from config import POOL_SPREADSHEET_ID, POOL_TAB_NAME
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock, highlight_taken

_timer = PickTimer()  # (draft_id, pair_index, round_index)


def name_key(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', text).casefold() if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', text)


class PartnerDraftTheme(Theme):
    requires_roster_submission = False
    key = 'partner_draft'
    display_name = 'Anonymous Partner Draft'

    def default_settings(self):
        return {'list_size': 3, 'roster_size': 10, 'budget': 100,
                'response_deadline_minutes': 15, 'msg_limit': 5, 'char_limit': 500}

    def _settings(self, ctx):
        return {**self.default_settings(), **ctx.settings}

    def _budget(self, ctx):
        try:
            budget = float(self._settings(ctx)['budget'])
        except (ValueError, TypeError):
            raise ValueError('Budget must be a finite, non-negative amount.')
        if not math.isfinite(budget) or budget < 0:
            raise ValueError('Budget must be a finite, non-negative amount.')
        return budget

    def _prices(self, ctx):
        raw = ctx.state.get('prices', {})
        if not raw:
            raise ValueError('Player prices are unavailable. The commissioner must reload valid prices before drafting.')
        prices = {}
        for name, value in raw.items():
            try:
                value = float(value)
            except (ValueError, TypeError):
                raise ValueError(f'Invalid player price for {name}.')
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid player price for {name}.')
            prices[name_key(name)] = value
        return prices

    @staticmethod
    def _orders(count, rounds):
        turns = []
        for r in range(0, rounds, 2):
            order = list(range(count))
            random.shuffle(order)
            turns.extend({'pair': p, 'round': r} for p in order)
            if r + 1 < rounds:
                turns.extend({'pair': p, 'round': r + 1} for p in reversed(order))
        return turns

    async def start(self, ctx):
        drafters, settings = ctx.drafters(), self._settings(ctx)
        if ctx.state.get('pairs'):
            raise ValueError('This draft has already been initialized. Use pause/resume instead of starting it again.')
        if len(drafters) < 2 or len(drafters) % 2:
            raise ValueError('Need an even number of drafters (2+) to pair everyone up.')
        for setting in ('list_size', 'roster_size', 'msg_limit', 'char_limit'):
            if not isinstance(settings[setting], int) or settings[setting] < 1:
                raise ValueError(f'{setting} must be a positive integer.')
        try:
            minutes = float(settings['response_deadline_minutes'])
        except (ValueError, TypeError):
            raise ValueError('Turn time must be a positive number of minutes.')
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError('Turn time must be a positive number of minutes.')
        self._budget(ctx)
        try:
            names, _ = await asyncio.to_thread(sheets.load_player_pool, POOL_SPREADSHEET_ID, POOL_TAB_NAME)
            raw_prices = await asyncio.to_thread(sheets.load_prices, POOL_SPREADSHEET_ID, POOL_TAB_NAME)
        except Exception as exc:
            raise ValueError('Could not load the player pool and prices. Nothing was started; try again after fixing the source.') from exc
        pool = {}
        for name in names:
            pool.setdefault(name_key(name), name)
        if not pool or len(pool) < len(drafters) // 2 * settings['roster_size']:
            raise ValueError('The player pool is empty or too small to fill every team.')
        price_index = {}
        for name, price in raw_prices.items():
            try:
                price = float(price)
            except (ValueError, TypeError):
                continue
            if math.isfinite(price) and price >= 0:
                price_index[name_key(name)] = price
        missing = [name for key, name in pool.items() if key not in price_index]
        if missing:
            raise ValueError(f'Missing/invalid prices for {len(missing)} pool players, including: {", ".join(missing[:5])}. Nothing was started.')
        shuffled = list(drafters)
        random.shuffle(shuffled)
        pairs = [{'a': shuffled[i]['id'], 'b': shuffled[i + 1]['id'], 'lists': {},
                  'deadlines': {}, 'paused_remaining': {}, 'owed_rounds': [], 'completed_rounds': []}
                 for i in range(0, len(shuffled), 2)]
        from emoji_map import EMOJI_TEAM_MAP
        guild = ctx.bot.get_guild(ctx.draft['guild_id']) if ctx.bot else None
        emojis = getattr(guild, 'emojis', ())
        logos, seen = [], set()
        for emoji in emojis if isinstance(emojis, (list, tuple)) else ():
            team = EMOJI_TEAM_MAP.get(emoji.name)
            if team and team not in seen:
                logos.append(str(emoji))
                seen.add(team)
        random.shuffle(logos)
        for i, pair in enumerate(pairs):
            pair['logo'] = logos[i] if i < len(logos) else f'🏀 Team {i + 1}'
        state = {**ctx.state, 'pairs': pairs, 'pool_names': list(pool.values()),
                 'prices': {name: price_index[key] for key, name in pool.items()},
                 'turns': self._orders(len(pairs), settings['roster_size']), 'turn_index': 0}
        ctx.save_state(state)
        for i, pair in enumerate(pairs):
            for role in ('a', 'b'):
                member = db.get_drafter(pair[role])
                db.update_drafter_state(member['id'], {'pair_index': i, 'pair_role': role})
                await ctx.dm(member['user_id'],
                    f'🤝 Anonymous Partner Draft started. You are Partner {role.upper()}; your teammate stays anonymous.\n'
                    f'Your team logo: {pair["logo"]}.\n'
                    f'DM `!list P1, P2, P3` with {settings["list_size"]} ranked players. Lists can be queued before your turn.\n'
                    'Team order is shuffled every two rounds; the second round reverses the first. Partner A has first-round priority, then priority alternates.\n'
                    f'Each turn lasts {minutes:g} minutes. No usable submission at expiry means a skipped turn with a makeup owed.\n'
                    '`!makeup P1, P2, P3` submits a list for your oldest skipped round. `!list 2 | P1, P2, P3` targets a specific round.\n'
                    f'Pooled budget: ${self._budget(ctx):g}. Use `!msg` without player names, initials, or identity; {settings["msg_limit"]} messages / {settings["char_limit"]} characters total.\n'
                    'Your completed picks are your final roster. No roster submission is needed.')
        # The command marks the draft active after start() returns. Persist the
        # first deadline now; callback checks status before doing any work.
        first = state['turns'][0]
        self._arm(ctx, state, first['pair'], first['round'], minutes * 60)
        for member_id in (pairs[first['pair']]['a'], pairs[first['pair']]['b']):
            await ctx.dm(db.get_drafter(member_id)['user_id'],
                         'Your team has the first turn. DM `!list P1, P2, P3` now; Partner A has priority.')
        return f'Anonymous Partner Draft started with {len(pairs)} teams. The first turn is open; lists may be queued.'

    @staticmethod
    def _turn(state):
        i = state.get('turn_index', 0)
        return state['turns'][i] if i < len(state['turns']) else None

    def _arm(self, ctx, state, pair_index, round_index, seconds):
        pair, slot = state['pairs'][pair_index], str(round_index)
        pair['deadlines'][slot] = time.time() + max(0, seconds)
        pair['paused_remaining'].pop(slot, None)
        ctx.save_state(state)
        if state.get('timer_channel_id'):
            _timer.cancel((ctx.draft_id, pair_index, round_index))
            return
        async def expired():
            async with draft_lock(ctx.draft_id):
                if ctx.draft['status'] != 'active':
                    return
                fresh = ctx.state
                deadline = fresh['pairs'][pair_index]['deadlines'].get(slot)
                if deadline is None or deadline > time.time() + .05:
                    return
                await self._resolve_locked(ctx, pair_index, round_index, forced=True)
                await self._drain_locked(ctx)
        _timer.start((ctx.draft_id, pair_index, round_index), max(0, seconds) / 60, expired)

    def _remaining(self, ctx, pair):
        prices = self._prices(ctx)
        picks = db.get_picks(ctx.draft_id, pair['a'])
        if any(name_key(p) not in prices for p in picks):
            raise ValueError('A drafted player has no valid price. Ask the commissioner to repair the prices.')
        return self._budget(ctx) - sum(prices[name_key(p)] for p in picks)

    def _parse_list(self, ctx, text):
        parts = [p.strip() for p in text.replace('\n', ',').split(',') if p.strip()]
        pool = {name_key(name): name for name in ctx.state['pool_names']}
        if len(parts) != self._settings(ctx)['list_size'] or len({name_key(p) for p in parts}) != len(parts):
            raise ValueError(f'Submit exactly {self._settings(ctx)["list_size"]} distinct players, comma- or line-separated.')
        unknown = [p for p in parts if name_key(p) not in pool]
        if unknown:
            raise ValueError('Not in the player pool: ' + ', '.join(unknown) + '. Use the full pool names.')
        return [pool[name_key(p)] for p in parts]

    async def on_list(self, ctx, drafter, text):
        async with draft_lock(ctx.draft_id):
            if ctx.draft['status'] != 'active':
                return 'This draft is not active. Lists cannot resolve while it is paused or cancelled.'
            if ctx.state.get('flux_wait'):
                return 'Waiting for a commissioner to complete Flux before the next round opens.'
            drafter = db.get_drafter(drafter['id'])
            i = drafter['state'].get('pair_index')
            if i is None:
                return "You're not part of a pairing."
            state, explicit = ctx.state, None
            if '|' in text:
                head, text = text.split('|', 1)
                if not head.strip().isdigit():
                    return 'Use `!list <round number> | P1, P2, P3`.'
                explicit = int(head.strip()) - 1
            pair, turn = state['pairs'][i], self._turn(state)
            target = explicit
            if target is None:
                if turn and turn['pair'] == i:
                    target = turn['round']
                elif pair['owed_rounds']:
                    target = min(pair['owed_rounds'])
                else:
                    target = next((t['round'] for t in state['turns'][state['turn_index']:] if t['pair'] == i), None)
            if target is None or target not in range(self._settings(ctx)['roster_size']):
                return 'Your team has no remaining rounds to submit for.'
            if target in pair['completed_rounds']:
                return 'Your team already drafted for that round.'
            if target not in pair['owed_rounds'] and not any(t == {'pair': i, 'round': target} for t in state['turns'][state['turn_index']:]):
                return 'That round is not available to your team.'
            try:
                players = self._parse_list(ctx, text)
                prices, remaining = self._prices(ctx), self._remaining(ctx, pair)
                taken = {name_key(p) for p in db.get_taken_players(ctx.draft_id)}
                if any(name_key(p) in taken for p in players):
                    return 'Your list includes a player already drafted. Replace them and submit again.'
                if any(prices[name_key(p)] > remaining for p in players):
                    return f'Your team has ${remaining:g} left. Every listed player must fit that remaining budget.'
            except (ValueError, KeyError) as exc:
                return str(exc)
            pair['lists'].setdefault(str(target), {})[str(drafter['id'])] = players
            ctx.save_state(state)
            if target in pair['owed_rounds']:
                if str(target) not in pair['deadlines']:
                    self._arm(ctx, state, i, target, float(self._settings(ctx)['response_deadline_minutes']) * 60)
                await self._resolve_locked(ctx, i, target)
            await self._drain_locked(ctx)
            return f'✅ List saved for Round {target + 1}. It resolves on your team’s turn, or as a makeup when both partners respond/the grace period expires.'

    async def _resolve_pair_round(self, ctx, pair_index, forced=False):
        """Compatibility entry point; all resolutions take the same draft lock."""
        async with draft_lock(ctx.draft_id):
            turn = self._turn(ctx.state)
            if turn and turn['pair'] == pair_index:
                await self._resolve_locked(ctx, pair_index, turn['round'], forced)
                await self._drain_locked(ctx)

    async def _resolve_locked(self, ctx, i, r, forced=False):
        if ctx.draft['status'] != 'active' or ctx.state.get('flux_wait'):
            return False
        state = ctx.state
        expected_state = copy.deepcopy(state)
        pair, slot = state['pairs'][i], str(r)
        turn = self._turn(state)
        makeup = r in pair['owed_rounds']
        if r in pair['completed_rounds'] or (not makeup and turn != {'pair': i, 'round': r}):
            return False
        lists = pair['lists'].get(slot, {})
        a, b = lists.get(str(pair['a'])), lists.get(str(pair['b']))
        if not forced and not (a and b):
            return False
        try:
            prices, remaining = self._prices(ctx), self._remaining(ctx, pair)
        except ValueError as exc:
            await ctx.announce(f'Partner Draft cannot resolve: {exc}')
            return False
        pool = {name_key(n): n for n in state['pool_names']}
        taken = {name_key(p) for p in db.get_taken_players(ctx.draft_id)}
        def eligible(names):
            return [pool[name_key(n)] for n in (names or []) if name_key(n) in pool and name_key(n) not in taken
                    and name_key(n) in prices and prices[name_key(n)] <= remaining]
        first, second = (eligible(a), eligible(b)) if r % 2 == 0 else (eligible(b), eligible(a))
        common = next((p for p in first if p in second), None)
        candidates = first or second
        chosen = common or next(iter(candidates), None)
        if chosen is None:
            if not forced:
                pair['lists'].pop(slot, None)
                ctx.save_state(state)
            else:
                pair['lists'].pop(slot, None)
                pair['deadlines'].pop(slot, None)
                if not makeup:
                    pair['owed_rounds'].append(r)
                    state['turn_index'] += 1
                ctx.save_state(state)
                _timer.cancel((ctx.draft_id, i, r))
            for member_id in (pair['a'], pair['b']):
                member = db.get_drafter(member_id)
                await ctx.dm(member['user_id'], f'Round {r + 1}: no usable player list. ' +
                    ('The turn was skipped; a makeup is owed. Use `!makeup P1, P2, P3`.' if forced else 'Submit a fresh list of available, affordable pool players.'))
            return forced and not makeup
        pair['completed_rounds'].append(r)
        pair['owed_rounds'] = [x for x in pair['owed_rounds'] if x != r]
        pair['lists'].pop(slot, None)
        pair['deadlines'].pop(slot, None)
        pair['paused_remaining'].pop(slot, None)
        if not makeup:
            state['turn_index'] += 1
        if not db.commit_partner_pick(ctx.draft_id, i, r, chosen, not bool(common), state, expected_state):
            return False
        _timer.cancel((ctx.draft_id, i, r))
        await highlight_taken(chosen)
        for member_id in (pair['a'], pair['b']):
            member = db.get_drafter(member_id)
            await ctx.dm(member['user_id'], f'🏀 Round {r + 1}: your team drafted **{chosen}**. Budget remaining: ${remaining - prices[name_key(chosen)]:g}.')
        return True

    async def _drain_locked(self, ctx):
        while ctx.draft['status'] == 'active':
            state = ctx.state
            turn = self._turn(state)
            if turn is None:
                return
            i, r = turn['pair'], turn['round']
            from flux_gate import hold_for_round
            if hold_for_round(ctx, state, r + 1):
                return
            pair, slot = state['pairs'][i], str(r)
            if slot not in pair['deadlines']:
                self._arm(ctx, state, i, r, float(self._settings(ctx)['response_deadline_minutes']) * 60)
                for member_id in (pair['a'], pair['b']):
                    member = db.get_drafter(member_id)
                    await ctx.dm(member['user_id'], f'⏱ Your team’s Round {r + 1} turn is open. Priority: Partner {"A" if r % 2 == 0 else "B"}. DM `!list P1, P2, P3`.')
            if not await self._resolve_locked(ctx, i, r):
                return

    async def claim_makeup(self, ctx, drafter, text):
        i = db.get_drafter(drafter['id'])['state'].get('pair_index')
        owed = ctx.state['pairs'][i]['owed_rounds'] if i is not None else []
        if not owed:
            return 'Your team has no skipped rounds to make up.'
        return await self.on_list(ctx, drafter, f'{min(owed) + 1} | {text}')

    def _blocked_message(self, ctx, text):
        clean = ''.join(c for c in unicodedata.normalize('NFKC', text) if unicodedata.category(c) != 'Cf')
        clean = re.sub(r'\s+', ' ', clean)
        if re.search(r'<[@#][^>]*>|@\S+|\b\d{15,22}\b|https?://|www\.|discord\.gg|\S+@\S+|\b(?:my name is|my username|my discord|call me|you know me as)\b', clean, re.I):
            return True
        words = ' ' + re.sub(r'[^a-z0-9]+', ' ', clean.casefold()) + ' '
        compact = name_key(clean)
        for player in ctx.state['pool_names']:
            if name_key(player) in compact:
                return True
            surname = name_key(player.split()[-1])
            if len(surname) >= 4 and f' {surname} ' in words:
                return True
        initials = {''.join(word[0] for word in re.findall(r'[A-Za-z]+', n)).upper() for n in ctx.state['pool_names']}
        initials.update(''.join(re.findall('[A-Z]', n)) for n in ctx.state['pool_names'])
        if any(re.sub('[^A-Z]', '', token) in initials for token in re.findall(r'\b(?:[A-Z]\.?){2,4}\b', clean)):
            return True
        for member in ctx.drafters():
            names = [member.get('display_name') or '']
            user = ctx.bot.get_user(member['user_id']) if ctx.bot else None
            if user:
                names.extend(getattr(user, attr, '') or '' for attr in ('name', 'display_name', 'global_name'))
            phrases = [re.sub(r'[^a-z0-9]+', ' ', n.casefold()).strip() for n in names]
            if any(len(name_key(n)) >= 3 and f' {phrase} ' in words for n, phrase in zip(names, phrases)):
                return True
        return False

    async def on_message(self, ctx, drafter, text):
        async with draft_lock(ctx.draft_id):
            if ctx.draft['status'] != 'active':
                return 'This draft is not active.'
            text = text.strip()
            if not text:
                return 'Include a message after `!msg`.'
            settings = self._settings(ctx)
            count, chars = db.relay_message_stats(ctx.draft_id, drafter['id'])
            if count >= settings['msg_limit'] or chars + len(text) > settings['char_limit']:
                return 'Your partner-message allowance would be exceeded.'
            if self._blocked_message(ctx, text):
                return 'Message blocked: remove player names/initials, identities, mentions and links.'
            i = drafter['state'].get('pair_index')
            if i is None:
                return "You're not part of a pairing."
            pair = ctx.state['pairs'][i]
            partner = db.get_drafter(pair['b'] if drafter['id'] == pair['a'] else pair['a'])
            if not await ctx.dm(partner['user_id'], f'💬 Your anonymous partner:\n> {text}'):
                return 'Could not deliver the message. Your allowance was not used.'
            db.add_relay_message(ctx.draft_id, drafter['id'], None, text)
            return f'✅ Sent ({count + 1}/{settings["msg_limit"]} messages, {chars + len(text)}/{settings["char_limit"]} characters).'

    async def cancel(self, ctx):
        async with draft_lock(ctx.draft_id):
            state = ctx.state
            for i, pair in enumerate(state.get('pairs', [])):
                for slot, deadline in list(pair.get('deadlines', {}).items()):
                    pair['paused_remaining'][slot] = max(0, deadline - time.time())
                    _timer.cancel((ctx.draft_id, i, int(slot)))
                pair['deadlines'] = {}
            ctx.save_state(state)

    async def resume_timers(self, ctx):
        async with draft_lock(ctx.draft_id):
            if ctx.draft['status'] != 'active':
                return 0
            state = ctx.state
            if state.get('flux_wait'):
                return 0
            resumed = 0
            for i, pair in enumerate(state.get('pairs', [])):
                for slot in set(pair['deadlines']) | set(pair['paused_remaining']):
                    if _timer.is_running((ctx.draft_id, i, int(slot))):
                        continue
                    seconds = pair['paused_remaining'].get(slot, max(0, pair['deadlines'].get(slot, time.time()) - time.time()))
                    self._arm(ctx, state, i, int(slot), seconds)
                    resumed += 1
            await self._drain_locked(ctx)
            return resumed

    async def on_timeout(self, ctx, drafter):
        return

    async def on_status(self, ctx, drafter):
        i = drafter['state'].get('pair_index')
        if i is None:
            return "You're not part of a pairing."
        pair, turn = ctx.state['pairs'][i], self._turn(ctx.state)
        owed = ', '.join(str(r + 1) for r in sorted(pair['owed_rounds'])) or 'none'
        return (f'Team: {pair.get("logo", "🏀")}. Picks: {len(pair["completed_rounds"])}/{self._settings(ctx)["roster_size"]}. '
                f'Budget remaining: ${self._remaining(ctx, pair):g}.\n'
                f'Current turn: {"your team" if turn and turn["pair"] == i else "another team" if turn else "scheduled turns finished"}. '
                f'Makeup rounds owed: {owed}.')

    def is_pick_phase_complete(self, ctx, drafter=None):
        return bool(ctx.state.get('pairs')) and all(len(p['completed_rounds']) == self._settings(ctx)['roster_size'] for p in ctx.state['pairs'])

    def team_key(self, ctx, drafter):
        return f'pair-{drafter["state"].get("pair_index")}'

    def team_label(self, ctx, drafter):
        pair = ctx.state['pairs'][drafter['state']['pair_index']]
        return f'{pair.get("logo", "")} {db.get_drafter(pair["a"])["display_name"]} & {db.get_drafter(pair["b"])["display_name"]}'.strip()

    def linked_drafter_ids(self, ctx, drafter):
        pair = ctx.state['pairs'][drafter['state']['pair_index']]
        return [pair['a'], pair['b']]

    def roster_requirements(self, ctx):
        prices = self._prices(ctx)
        return {name: prices[name_key(name)] for name in ctx.state['pool_names']}, self._budget(ctx)

    def validate_final_roster(self, ctx, drafter, submitted):
        size = self._settings(ctx)['roster_size']
        if len(submitted) != size or len({name_key(p) for p in submitted}) != size:
            return False, [f'Submit exactly {size} distinct drafted players.']
        return True, []
