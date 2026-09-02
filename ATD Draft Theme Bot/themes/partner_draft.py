# themes/partner_draft.py
#
# Anonymous Partner Draft: drafters are randomly paired, anonymously, into
# one team per pair. Each round both partners DM a ranked list of
# `list_size` players; a shared player is drafted, otherwise the round's
# priority partner's highest-available listed player is taken. Partners can
# also relay a strictly-limited number of messages to each other, with
# player names/identity blocked from the relay.
#
# A drafted player is marked taken on the ADP sheet the moment a pick
# resolves (that never reveals which team took them), but the Team Sheet is
# NOT written live — per the format's own rule that teams stay hidden until
# every pair has submitted its final roster, that write happens once, at
# `!theme reveal`, same as any format with GM identity to protect.
#
# Scoping note (baseline pass): rounds advance independently *per pair* —
# a pair resolves round N the moment both members (or the grace timer) says
# so, without waiting on other pairs. The source material also describes a
# whole-draft "pick order that resets every 2 rounds" layered on top of
# this, which reads as coordinating turn order *across* pairs (not just
# within one) — that part needs clarification before it can be implemented,
# so it's intentionally left out of this baseline. See on_status/start for
# where to hook it in later.

import asyncio
import logging
import random

import db
import sheets
from config import POOL_SPREADSHEET_ID, POOL_TAB_NAME
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock, highlight_taken

log = logging.getLogger(__name__)

_timer = PickTimer()  # keyed by pair_index, not drafter_id — one grace clock per pair


class PartnerDraftTheme(Theme):
    key = 'partner_draft'
    display_name = 'Anonymous Partner Draft'

    def default_settings(self) -> dict:
        return {
            'list_size': 3,
            'roster_size': 10,
            'budget': 100,
            'response_deadline_minutes': 15,
            'msg_limit': 5,
            'char_limit': 500,
        }

    def _settings(self, ctx: DraftContext) -> dict:
        return {**self.default_settings(), **ctx.settings}

    # ── Start ────────────────────────────────────────────────────────────────
    async def start(self, ctx: DraftContext) -> str:
        drafters = ctx.drafters()
        if len(drafters) < 2 or len(drafters) % 2 != 0:
            raise ValueError('Need an even number of drafters (2+) to pair everyone up.')

        loop = asyncio.get_running_loop()
        names, _adp = await loop.run_in_executor(
            None, sheets.load_player_pool, POOL_SPREADSHEET_ID, POOL_TAB_NAME
        )
        try:
            prices = await loop.run_in_executor(None, sheets.load_prices, POOL_SPREADSHEET_ID, POOL_TAB_NAME)
        except Exception:
            log.warning('PartnerDraft | price load failed — budget check will be skipped', exc_info=True)
            prices = {}

        shuffled = list(drafters)
        random.shuffle(shuffled)
        pairs = []
        for i in range(0, len(shuffled), 2):
            pairs.append({'a': shuffled[i]['id'], 'b': shuffled[i + 1]['id'], 'round_index': 0})

        state = {'pairs': pairs, 'pool_names': names, 'prices': prices}
        ctx.save_state(state)

        for pair_index, pair in enumerate(pairs):
            a = db.get_drafter(pair['a'])
            b = db.get_drafter(pair['b'])
            db.set_drafter_state(a['id'], {'pair_index': pair_index, 'pair_role': 'a', 'current_list': None, 'list_round': None})
            db.set_drafter_state(b['id'], {'pair_index': pair_index, 'pair_role': 'b', 'current_list': None, 'list_round': None})
            for d in (a, b):
                await ctx.dm(
                    d['user_id'],
                    "🤝 **Anonymous Partner Draft has started!** You've been paired with a partner — "
                    "you won't know who they are, and they won't know you.\n\n"
                    f"**Round 1:** submit a ranked list of {self._settings(ctx)['list_size']} players with "
                    "`!list Player One, Player Two, Player Three`.\n"
                    "If you both list the same player, that's the pick. Otherwise, the round's priority "
                    "partner's highest-available pick is used. Round 1 priority: **Partner A**.\n\n"
                    "You can also message your partner with `!msg <text>` — no player names, initials, or "
                    "identity, and you're capped at "
                    f"{self._settings(ctx)['msg_limit']} messages / {self._settings(ctx)['char_limit']} characters total.",
                )

        return f'Anonymous Partner Draft started with {len(pairs)} pair(s).'

    # ── List submission ──────────────────────────────────────────────────────
    async def on_list(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            pair_index = drafter['state'].get('pair_index')
            if pair_index is None:
                return "You're not part of an active pairing right now."
            pair = state['pairs'][pair_index]

            picks_so_far = len(db.get_picks(ctx.draft_id, drafter['id']))
            if picks_so_far >= settings['roster_size']:
                return "Your team's roster is already full — no more rounds to submit for."

            players = self._parse_list(text, settings['list_size'])
            if players is None:
                return f"Submit exactly {settings['list_size']} distinct players, comma- or line-separated."

            drafter['state']['current_list'] = players
            drafter['state']['list_round'] = pair['round_index']
            db.set_drafter_state(drafter['id'], drafter['state'])

            partner_id = pair['b'] if drafter['state']['pair_role'] == 'a' else pair['a']
            partner = db.get_drafter(partner_id)
            partner_ready = (
                partner['state'].get('current_list') is not None
                and partner['state'].get('list_round') == pair['round_index']
            )

            if partner_ready:
                _timer.cancel(pair_index)
                await self._resolve_pair_round(ctx, pair_index)
                return "✅ List received — your partner had already submitted, resolving now."

            if not _timer.is_running(pair_index):
                minutes = settings['response_deadline_minutes']

                async def _on_grace_timeout():
                    await self._resolve_pair_round(ctx, pair_index, forced=True)

                _timer.start(pair_index, minutes, _on_grace_timeout)

            return (
                f"✅ List received for round {pair['round_index'] + 1}. Waiting on your partner "
                f"(or a {settings['response_deadline_minutes']}-minute grace period)."
            )

    async def _resolve_pair_round(self, ctx: DraftContext, pair_index: int, forced: bool = False) -> None:
        state = ctx.state
        pair = state['pairs'][pair_index]
        a = db.get_drafter(pair['a'])
        b = db.get_drafter(pair['b'])

        list_a = a['state'].get('current_list') if a['state'].get('list_round') == pair['round_index'] else None
        list_b = b['state'].get('current_list') if b['state'].get('list_round') == pair['round_index'] else None
        if not list_a and not list_b:
            return

        taken = db.get_taken_players(ctx.draft_id)
        priority_role = 'a' if pair['round_index'] % 2 == 0 else 'b'
        auto = False

        if list_a and list_b:
            b_lower = {p.lower() for p in list_b}
            match = next((p for p in list_a if p.lower() in b_lower), None)
            if match and match.lower() not in taken:
                chosen = match
            else:
                auto = True
                priority_list = list_a if priority_role == 'a' else list_b
                chosen = next((p for p in priority_list if p.lower() not in taken), None)
        else:
            auto = True
            only_list = list_a or list_b
            chosen = next((p for p in only_list if p.lower() not in taken), None)

        if chosen is None:
            note = "⚠️ All of your listed players are already taken elsewhere — please submit a fresh `!list`."
            a['state']['current_list'] = None
            db.set_drafter_state(a['id'], a['state'])
            b['state']['current_list'] = None
            db.set_drafter_state(b['id'], b['state'])
            await ctx.dm(a['user_id'], note)
            await ctx.dm(b['user_id'], note)
            return

        pick_num = len(db.get_picks(ctx.draft_id, a['id'])) + 1
        db.record_pick(ctx.draft_id, a['id'], chosen, round_number=pair['round_index'] + 1, pick_number=pick_num, auto_pick=auto)
        db.record_pick(ctx.draft_id, b['id'], chosen, round_number=pair['round_index'] + 1, pick_number=pick_num, auto_pick=auto)
        await highlight_taken(chosen)

        a['state']['current_list'] = None
        db.set_drafter_state(a['id'], a['state'])
        b['state']['current_list'] = None
        db.set_drafter_state(b['id'], b['state'])

        pair['round_index'] += 1
        ctx.save_state(state)
        _timer.cancel(pair_index)

        settings = self._settings(ctx)
        if pick_num >= settings['roster_size']:
            tail = " Your roster is complete — submit it with `!submitroster Player One, Player Two, ...` when ready."
        else:
            next_priority = 'A' if pair['round_index'] % 2 == 0 else 'B'
            tail = f" Round {pair['round_index'] + 1} priority: Partner {next_priority}."

        result_txt = f"🏀 Your team drafted **{chosen}**!{tail}"
        await ctx.dm(a['user_id'], result_txt)
        await ctx.dm(b['user_id'], result_txt)

    # ── Partner messaging ────────────────────────────────────────────────────
    async def on_message(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            text = text.strip()
            if not text:
                return 'Include a message after `!msg`.'

            count, chars = db.relay_message_stats(ctx.draft_id, drafter['id'])
            if count >= settings['msg_limit']:
                return f"❌ You've already used all {settings['msg_limit']} messages to your partner."
            if chars + len(text) > settings['char_limit']:
                return f"❌ That would put you over your {settings['char_limit']}-character total limit ({chars} used so far)."

            lowered = text.lower()
            hit = next((n for n in state.get('pool_names', []) if n.lower() in lowered), None)
            if hit:
                return "❌ Message blocked — it looks like it names a player. Rephrase without naming anyone (no names/initials/identity)."

            pair_index = drafter['state'].get('pair_index')
            if pair_index is None:
                return "You're not part of an active pairing right now."
            pair = state['pairs'][pair_index]
            partner_id = pair['b'] if drafter['state']['pair_role'] == 'a' else pair['a']
            partner = db.get_drafter(partner_id)

            db.add_relay_message(ctx.draft_id, drafter['id'], pair['round_index'], text)
            await ctx.dm(partner['user_id'], f"💬 Message from your anonymous partner:\n> {text}")

            count2, chars2 = db.relay_message_stats(ctx.draft_id, drafter['id'])
            return f"✅ Sent. ({count2}/{settings['msg_limit']} messages, {chars2}/{settings['char_limit']} chars used)"

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        # Grace timers here are per-pair, not per-drafter — handled directly
        # via _resolve_pair_round's timer callback in on_list(). Nothing to
        # do through the generic per-drafter hook for this theme.
        return

    # ── Status / misc ────────────────────────────────────────────────────────
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        settings = self._settings(ctx)
        pair_index = drafter['state'].get('pair_index')
        if pair_index is None:
            return "You're not part of an active pairing right now."
        pair = ctx.state['pairs'][pair_index]
        picks_made = len(db.get_picks(ctx.draft_id, drafter['id']))
        role = drafter['state'].get('pair_role', '?').upper()
        priority = 'A' if pair['round_index'] % 2 == 0 else 'B'
        submitted = drafter['state'].get('current_list') is not None and drafter['state'].get('list_round') == pair['round_index']
        return (
            f"Team picks so far: {picks_made}/{settings['roster_size']}.\n"
            f"You are Partner {role}. Round {pair['round_index'] + 1} priority: Partner {priority}.\n"
            f"Your list this round: {'submitted' if submitted else 'not submitted yet'}."
        )

    def is_pick_phase_complete(self, ctx: DraftContext) -> bool:
        settings = self._settings(ctx)
        seen_pairs = set()
        for d in ctx.drafters():
            pair_index = d['state'].get('pair_index')
            if pair_index is None or pair_index in seen_pairs:
                continue
            seen_pairs.add(pair_index)
            if len(db.get_picks(ctx.draft_id, d['id'])) < settings['roster_size']:
                return False
        return True

    # ── Team grouping (2 drafters share 1 team) ─────────────────────────────
    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        return f"pair-{drafter['state'].get('pair_index')}"

    def team_label(self, ctx: DraftContext, drafter: dict) -> str:
        pair_index = drafter['state'].get('pair_index')
        pair = ctx.state['pairs'][pair_index]
        a = db.get_drafter(pair['a'])
        b = db.get_drafter(pair['b'])
        return f"{a['display_name']} & {b['display_name']}"

    def linked_drafter_ids(self, ctx: DraftContext, drafter: dict) -> list[int]:
        pair_index = drafter['state'].get('pair_index')
        pair = ctx.state['pairs'][pair_index]
        return [pair['a'], pair['b']]

    def roster_requirements(self, ctx: DraftContext) -> tuple[dict | None, float | None]:
        settings = self._settings(ctx)
        prices = ctx.state.get('prices') or {}
        if not prices:
            return None, None
        return prices, settings['budget']

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_list(text: str, size: int) -> list[str] | None:
        parts = [p.strip() for p in text.replace('\n', ',').split(',') if p.strip()]
        if len(parts) != size:
            return None
        if len({p.lower() for p in parts}) != size:
            return None
        return parts
