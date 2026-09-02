# themes/pack_draft.py
#
# Pack Draft: drafters are split into divisions (default 4 x 8). Each
# division gets its own set of packs, generated from 11 consecutive
# ADP tiers (one player per tier per pack) — divisions draw independently
# from the same tiers, so the same real player can end up seeded into more
# than one division's packs. Each round every drafter in a division opens
# a fresh pack; picks pass round-robin, snake-reversing direction each
# round, until every drafter has `rounds * pack_size` (default 33) picks.
#
# Teams stay fully hidden — including from the ADP-mark-taken sheet's
# perspective of "who" (never "who", only "taken") — until every drafter
# has submitted a validated final roster; the Team Sheet is written once,
# at `!theme reveal`, same as Anonymous Partner Draft.
#
# Optional live spectator feed: if settings['public_channel_id'] is set, a
# Discord thread gets created per division under that channel at !theme
# start, and every pick/pack-routing event is posted there by SEAT NUMBER
# only — never a real name — so onlookers can watch pace and player
# availability live without learning what's on anyone's team.
#
# Draft order within each division comes from the league's existing lotto
# message (see lotto.py) via `!importorder`, not a bot-run lottery.

import asyncio
import logging
import random
import re
import time

import discord

import db
import positions as positions_module
import sheets
from config import POOL_SPREADSHEET_ID, POOL_TAB_NAME
from emoji_map import EMOJI_TEAM_MAP
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock, highlight_taken

log = logging.getLogger(__name__)

_timer = PickTimer()

_CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')
_YEAR_TOKEN_RE = re.compile(r"^'?\d{2,4}(-\d{2,4})?$")


def _resolve_team_name(raw: str) -> str:
    """!importorder's logo field is often a Discord custom-emoji token
    (<:Lakers:id>), which is meaningless literal text once written into a
    Google Sheet cell — resolve it through the same EMOJI_TEAM_MAP ATD Team
    Sheet Bot uses (copied here — the two bots don't share a directory) to
    get the real team name instead. Falls back to the emoji's bare name, or
    the raw text as-is if it isn't an emoji token at all (e.g. the commish
    just typed a plain team name directly)."""
    raw = (raw or '').strip()
    m = _CUSTOM_EMOJI_RE.search(raw)
    if m:
        return EMOJI_TEAM_MAP.get(m.group(1), m.group(1))
    return EMOJI_TEAM_MAP.get(raw, raw)


class PackDraftTheme(Theme):
    key = 'pack_draft'
    display_name = 'Pack Draft'

    def default_settings(self) -> dict:
        return {
            'num_divisions': 4,
            'drafters_per_division': 8,
            'pack_size': 11,          # also the number of ADP tiers (one tier per pack slot)
            'tier_size': 32,          # players per ADP tier; must be >= drafters_per_division * rounds
            'rounds': 3,              # picks_per_drafter = rounds * pack_size (default 33)
            'roster_positions': ['PG', 'SG', 'SF', 'PF', 'C'],  # both starters and bench use these
            'budget': 100,
            'enforce_budget': True,   # set false to run Pack Draft with no money at all, even if the pool sheet has prices
            'base_timer_minutes': 60,
            'decay_minutes': 10,
            'floor_minutes': 10,
            'public_channel_id': None,  # optional: channel to create a live per-division spectator thread under
            'pack_delivery_gap_seconds': 3,  # pause before handing a seat its NEXT queued pack right after a
            # pick, so that DM doesn't land on top of (or before) the "you drafted X" confirmation and get
            # missed — set to 0 to disable.
        }

    def _settings(self, ctx: DraftContext) -> dict:
        return {**self.default_settings(), **ctx.settings}

    @staticmethod
    def _division_label(idx: int) -> str:
        return chr(ord('A') + idx)

    @staticmethod
    def _parse_division(text: str, num_divisions: int) -> int:
        text = text.strip().upper()
        max_label = chr(ord('A') + num_divisions - 1)
        if not text:
            raise ValueError(f"Provide a division letter (A-{max_label}) or number (1-{num_divisions}).")
        if text.isdigit():
            idx = int(text) - 1
        elif len(text) == 1 and text.isalpha():
            idx = ord(text) - ord('A')
        else:
            raise ValueError(f"Couldn't parse division '{text}' — use a letter (A-{max_label}) or number (1-{num_divisions}).")
        if not (0 <= idx < num_divisions):
            raise ValueError(f"Division must be A-{max_label} or 1-{num_divisions}.")
        return idx

    # ── Seats (1-3 GMs each) ────────────────────────────────────────────────
    # A "seat" is the actual unit that owns a pack instance and a timer —
    # `drafters_per_division` counts seats, not individual GMs. Every field
    # that has to be visible to whichever teammate happens to check
    # (current_pack_instance, queue, timer_deadline) is mirrored onto every
    # member's own drafter row so !status/!pack keep working unmodified for
    # each of them; only skip_count is tracked on a single "primary" member
    # (lowest drafter id) since it's only ever used as decay-timer input,
    # never displayed per-person.
    def _seat_members(self, ctx: DraftContext, div_idx: int, seat_index: int) -> list[dict]:
        members = [d for d in db.list_drafters_in_division(ctx.draft_id, div_idx) if d['seat_index'] == seat_index]
        members.sort(key=lambda d: d['id'])
        return members

    @staticmethod
    def _seat_primary(members: list[dict]) -> dict:
        return members[0]  # already sorted by id in _seat_members

    @staticmethod
    def _seat_timer_key(ctx: DraftContext, div_idx: int, seat_index: int) -> tuple:
        return (ctx.draft_id, div_idx, seat_index)

    # ── Draft order import (from the league's existing lotto message) ─────────
    async def import_draft_order(self, ctx: DraftContext, division: str, team_lines: list[dict]) -> str:
        settings = self._settings(ctx)
        try:
            div_idx = self._parse_division(division, settings['num_divisions'])
        except ValueError as e:
            return f"❌ {e}"

        needed = settings['drafters_per_division']
        if len(team_lines) != needed:
            return (f"❌ Found {len(team_lines)} line(s) in that message, but this draft needs exactly "
                    f"{needed} seats per division.")
        bad = [i + 1 for i, t in enumerate(team_lines) if not (1 <= len(t['user_ids']) <= 3)]
        if bad:
            return f"❌ Line(s) {', '.join(map(str, bad))} need 1-3 @mentions — a seat can be solo, a duo, or a trio."

        div_label = self._division_label(div_idx)
        total_gms = 0
        failed_dms = []
        for seat, entry in enumerate(team_lines):
            team_name = _resolve_team_name(entry['logo'])
            for uid in entry['user_ids']:
                total_gms += 1
                drafter = db.get_drafter_by_user(ctx.draft_id, uid)
                display_name = str(uid)
                try:
                    user = ctx.bot.get_user(uid) or await ctx.bot.fetch_user(uid)
                    if user:
                        display_name = getattr(user, 'display_name', None) or user.name
                except Exception:
                    pass

                if drafter:
                    db.set_drafter_seat(drafter['id'], seat)
                    db.set_drafter_division(drafter['id'], div_idx)
                    drafter_id = drafter['id']
                else:
                    drafter_id = db.add_drafter(ctx.draft_id, uid, display_name, seat_index=seat, division=div_idx)
                db.update_drafter_state(drafter_id, {'team_name': team_name})

                delivered = await ctx.dm(
                    uid,
                    f"🏀 You're drafting in Division {div_label} — your team is **{team_name}**.",
                )
                if not delivered:
                    failed_dms.append(display_name)

        note = f"\n⚠️ Couldn't DM: {', '.join(failed_dms)} — let them know their team manually." if failed_dms else ''
        return f"✅ Division {div_label} order set ({needed} seats, {total_gms} GMs, team names captured).{note}"

    # ── Pack generation ──────────────────────────────────────────────────────
    async def generate_packs(self, ctx: DraftContext, division: str) -> str:
        settings = self._settings(ctx)
        try:
            div_idx = self._parse_division(division, settings['num_divisions'])
        except ValueError as e:
            return f"❌ {e}"

        pack_size = settings['pack_size']
        tier_size = settings['tier_size']
        packs_per_division = settings['drafters_per_division'] * settings['rounds']
        if tier_size < packs_per_division:
            return (f"❌ tier_size ({tier_size}) must be >= drafters_per_division * rounds "
                     f"({packs_per_division}) so every tier can seed every pack.")

        loop = asyncio.get_running_loop()
        names, adp = await loop.run_in_executor(None, sheets.load_player_pool, POOL_SPREADSHEET_ID, POOL_TAB_NAME)
        ranked = sorted((n for n in names if n in adp), key=lambda n: adp[n])
        needed = pack_size * tier_size
        if len(ranked) < needed:
            return f"❌ Pool only has {len(ranked)} ranked (ADP) players — need {needed} for {pack_size} tiers of {tier_size}."

        packs: list[list[str]] = [[] for _ in range(packs_per_division)]
        for tier_idx in range(pack_size):
            tier_players = ranked[tier_idx * tier_size:(tier_idx + 1) * tier_size]
            random.shuffle(tier_players)
            for pack_idx in range(packs_per_division):
                packs[pack_idx].append(tier_players[pack_idx])
            # the remaining tier_size - packs_per_division players are intentionally left unseeded

        self._store_generated_packs(ctx, div_idx, packs)
        result = f"✅ Generated {packs_per_division} packs of {pack_size} for Division {self._division_label(div_idx)}."
        warning = await self._check_budget_feasibility(ctx, packs)
        if warning:
            result += f"\n{warning}"
        return result

    async def load_packs(self, ctx: DraftContext, division: str, text: str) -> str:
        settings = self._settings(ctx)
        try:
            div_idx = self._parse_division(division, settings['num_divisions'])
        except ValueError as e:
            return f"❌ {e}"

        pack_size = settings['pack_size']
        packs_per_division = settings['drafters_per_division'] * settings['rounds']

        parsed: dict[int, list[str]] = {}
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^pack\s*(\d+)\s*:\s*(.+)$', line, re.IGNORECASE)
            if not m:
                return f"❌ Couldn't parse line: `{line}`. Expected `Pack N: Player One, Player Two, ...`"
            pack_num = int(m.group(1))
            players = [p.strip() for p in m.group(2).split(',') if p.strip()]
            if len(players) != pack_size:
                return f"❌ Pack {pack_num} has {len(players)} players — needs exactly {pack_size}."
            parsed[pack_num] = players

        missing = [n for n in range(1, packs_per_division + 1) if n not in parsed]
        if missing:
            return f"❌ Missing pack(s): {', '.join(map(str, missing))}. Need packs 1-{packs_per_division}."

        packs = [parsed[n] for n in range(1, packs_per_division + 1)]
        self._store_generated_packs(ctx, div_idx, packs)
        result = f"✅ Loaded {packs_per_division} manual packs for Division {self._division_label(div_idx)}."
        warning = await self._check_budget_feasibility(ctx, packs)
        if warning:
            result += f"\n{warning}"
        return result

    def _store_generated_packs(self, ctx: DraftContext, div_idx: int, packs: list[list[str]]) -> None:
        state = ctx.state
        state.setdefault('divisions', {})
        div_state = state['divisions'].setdefault(str(div_idx), {'round_index': 0, 'direction': 1, 'pack_instances': {}})
        div_state['generated_packs'] = packs
        ctx.save_state(state)

    # ── Start ────────────────────────────────────────────────────────────────
    async def start(self, ctx: DraftContext) -> str:
        settings = self._settings(ctx)
        num_divisions = settings['num_divisions']
        per_division = settings['drafters_per_division']
        packs_needed = per_division * settings['rounds']

        loop = asyncio.get_running_loop()
        try:
            prices = await loop.run_in_executor(None, sheets.load_prices, POOL_SPREADSHEET_ID, POOL_TAB_NAME)
        except Exception:
            log.warning('PackDraft | price load failed — budget check will be skipped', exc_info=True)
            prices = {}

        state = ctx.state
        state.setdefault('divisions', {})
        state['prices'] = prices

        for div_idx in range(num_divisions):
            drafters = db.list_drafters_in_division(ctx.draft_id, div_idx)
            seats = sorted({d['seat_index'] for d in drafters if d['seat_index'] is not None})
            if seats != list(range(per_division)):
                raise ValueError(
                    f"Division {self._division_label(div_idx)} has {len(seats)} seat(s) assigned ({len(drafters)} "
                    f"GM(s) total), needs exactly {per_division} seats with a clean draft order. Run "
                    f"`!importorder {self._division_label(div_idx)}` as a reply to that division's lotto message."
                )

            div_state = state['divisions'].setdefault(str(div_idx), {'round_index': 0, 'direction': 1, 'pack_instances': {}})
            if len(div_state.get('generated_packs', [])) != packs_needed:
                ctx.save_state(state)
                result = await self.generate_packs(ctx, self._division_label(div_idx))
                if result.startswith('❌'):
                    raise ValueError(result)
                state = ctx.state

        ctx.save_state(state)

        for div_idx in range(num_divisions):
            await self._ensure_division_thread(ctx, div_idx)
            await self._open_round(ctx, div_idx, round_index=0)

        return (
            f"Pack Draft started — {num_divisions} division(s) of {per_division}, "
            f"{settings['rounds']} rounds of {settings['pack_size']}-player packs."
        )

    async def _open_round(self, ctx: DraftContext, div_idx: int, round_index: int) -> None:
        log.info("PackDraft | _open_round: div=%s round_index=%s", div_idx, round_index)
        settings = self._settings(ctx)
        per_division = settings['drafters_per_division']
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        packs = div_state['generated_packs']
        round_packs = packs[round_index * per_division:(round_index + 1) * per_division]

        div_state['round_index'] = round_index
        div_state['pack_instances'] = {
            f'{round_index}-{seat}': {'cards': list(round_packs[seat]), 'holder_seat': seat}
            for seat in range(per_division)
        }
        ctx.save_state(state)

        await self._thread_announce(
            ctx, div_state,
            f"🃏 **Round {round_index + 1}** begins — {per_division} fresh packs opened, one per seat.",
        )

        for seat in range(per_division):
            instance_id = f'{round_index}-{seat}'
            members = self._seat_members(ctx, div_idx, seat)
            for m in members:
                # update_drafter_state (merge), not set_drafter_state (raw
                # overwrite) — the latter used to wipe team_name/roster_slots/
                # player_years/roster_unslotted the instant a new round opened.
                db.update_drafter_state(m['id'], {'current_pack_instance': instance_id, 'queue': []})
            await self._deliver_pack_dm(ctx, div_idx, seat, members, instance_id, div_state['pack_instances'][instance_id]['cards'])

    # ── Pick handling ────────────────────────────────────────────────────────
    # A duo/trio seat gets the identical pack DM'd to every member at once —
    # whoever's !pick lands first (i.e. first to acquire draft_lock) wins it
    # for the team. The re-fetch-after-lock below is what actually enforces
    # that: a teammate whose command was already in flight when the winning
    # pick landed sees current_pack_instance already cleared once they
    # finally get the lock, and is told there's nothing to pick — no
    # separate "first wins" logic needed beyond re-reading fresh state.
    async def on_pick(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            drafter = db.get_drafter(drafter['id'])
            div_idx = drafter.get('division')
            if div_idx is None:
                return "You're not assigned to a division yet."
            state = ctx.state
            div_state = state.get('divisions', {}).get(str(div_idx))
            instance_id = drafter['state'].get('current_pack_instance')
            if not div_state or not instance_id or instance_id not in div_state['pack_instances']:
                return "You don't have a pack in front of you right now — sit tight."

            cards = div_state['pack_instances'][instance_id]['cards']
            player, year = self._resolve_pick(text, cards)
            if not player:
                return f"**{text.strip()}** isn't in your current pack. Your options:\n" + self._format_pack(cards, self._active_prices(ctx))
            result = await self._commit_pick(ctx, drafter, div_idx, instance_id, player, auto=False)
            if year:
                # Same effect as a follow-up !addyear, done inline — set_player_year
                # isn't called directly since it acquires this same draft_lock itself.
                slots, years, unslotted = self._ensure_roster_slots(ctx, drafter)
                years[player.lower()] = year
                self._save_roster(drafter, slots, unslotted, years)
                result += f" Year set to **'{year}**."
            return result

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        """drafter can be any member of the seat — the whole seat (all
        teammates) times out together, since nobody on the team picked."""
        async with draft_lock(ctx.draft_id):
            log.info("PackDraft | on_timeout entered for draft=%s drafter_id=%s", ctx.draft_id, drafter['id'])
            drafter = db.get_drafter(drafter['id'])
            div_idx = drafter.get('division')
            if div_idx is None:
                log.warning("PackDraft | on_timeout: drafter_id=%s has no division — aborting", drafter['id'])
                return
            members = self._seat_members(ctx, div_idx, drafter['seat_index'])
            primary = self._seat_primary(members)
            state = ctx.state
            div_state = state.get('divisions', {}).get(str(div_idx))
            instance_id = drafter['state'].get('current_pack_instance')
            if not div_state or not instance_id or instance_id not in div_state['pack_instances']:
                log.info(
                    "PackDraft | on_timeout: seat=%s div=%s already resolved (instance=%s not active) — no-op",
                    drafter['seat_index'], div_idx, instance_id,
                )
                return

            cards = div_state['pack_instances'][instance_id]['cards']
            if not cards:
                log.warning("PackDraft | on_timeout: instance %s for seat %s has no cards — aborting", instance_id, drafter['seat_index'])
                return
            player = random.choice(cards)
            log.info(
                "PackDraft | on_timeout: auto-picking %r from %s for seat=%s div=%s",
                player, instance_id, drafter['seat_index'], div_idx,
            )

            skip_count = db.increment_skip(primary['id'])
            primary['skip_count'] = skip_count  # keep in sync — _commit_pick may immediately
            # deliver the team's next queued pack, which sizes its timer off this value.
            settings = self._settings(ctx)
            next_minutes = PickTimer.effective_minutes(
                settings['base_timer_minutes'], settings['decay_minutes'], settings['floor_minutes'], skip_count,
            )
            team_note = ' for your team' if len(members) > 1 else ''
            for m in members:
                await ctx.dm(
                    m['user_id'],
                    f"⏰ Time expired — auto-picked **{player}**{team_note}.\n"
                    f"Your timer is now **{next_minutes:.0f} min** next time you're on the clock (skip #{skip_count}).",
                )
            await self._commit_pick(ctx, primary, div_idx, instance_id, player, auto=True)

    async def force_skip(self, ctx: DraftContext, drafter: dict) -> str:
        if drafter.get('division') is None:
            return f"{drafter['display_name']} isn't assigned to a division."
        if not drafter['state'].get('current_pack_instance'):
            return f"{drafter['display_name']} doesn't have an active pack right now."
        await self.on_timeout(ctx, drafter)
        return f"⏭️ Forced a skip for **{drafter['display_name']}**'s team."

    async def _commit_pick(self, ctx: DraftContext, drafter: dict, div_idx: int, instance_id: str,
                            player: str, auto: bool) -> str:
        """drafter is whoever actually picked (or the seat's primary member,
        for an auto-pick) — the resulting pick/pack/timer effects apply to
        every teammate at their seat, not just them."""
        state = ctx.state
        settings = self._settings(ctx)
        per_division = settings['drafters_per_division']
        div_state = state['divisions'][str(div_idx)]
        picker_seat = drafter['seat_index']
        members = self._seat_members(ctx, div_idx, picker_seat)
        log.info(
            "PackDraft | _commit_pick: %r from %s by seat=%s div=%s (auto=%s, members=%s)",
            player, instance_id, picker_seat, div_idx, auto, [m['id'] for m in members],
        )

        _timer.cancel(self._seat_timer_key(ctx, div_idx, picker_seat))
        self._clear_timer_deadline(members)

        pick_count = len(db.get_picks(ctx.draft_id, drafter['id'])) + 1
        for m in members:
            db.record_pick(
                ctx.draft_id, m['id'], player,
                round_number=div_state['round_index'] + 1, pick_number=pick_count, auto_pick=auto,
            )
        await highlight_taken(player)

        instance = div_state['pack_instances'][instance_id]
        instance['cards'].remove(player)

        if not instance['cards']:
            del div_state['pack_instances'][instance_id]
            pack_status = 'that pack is now empty'
            log.info("PackDraft | instance %s fully drained and removed (div=%s)", instance_id, div_idx)
        else:
            next_seat = (picker_seat + div_state['direction']) % per_division
            instance['holder_seat'] = next_seat
            await self._route_pack_to_seat(ctx, div_idx, state, next_seat, instance_id)
            pack_status = f"pack now with Seat {next_seat + 1} ({len(instance['cards'])} left)"

        icon = '⏰' if auto else '✅'
        verb = 'auto-picked' if auto else 'picked'
        await self._thread_announce(
            ctx, div_state, f"{icon} Seat {picker_seat + 1} {verb} **{player}** — {pack_status}",
        )

        # Teammates who didn't make this pick find out who did — they
        # already know each other (this isn't Anonymous Draft), and an
        # auto-pick already got its own "time expired" DM to everyone above.
        if not auto and len(members) > 1:
            for m in members:
                if m['id'] == drafter['id']:
                    continue
                await ctx.dm(m['user_id'], f"📢 **{drafter['display_name']}** picked **{player}** for your team.")

        total_picks = settings['rounds'] * settings['pack_size']
        remaining = total_picks - pick_count
        prefix = 'Auto-picked' if auto else 'Drafted'
        result = f"✅ {prefix} **{player}**. {remaining} pick(s) left for you."

        # Every member's current_pack_instance/queue is kept identical, so
        # the pop only needs to happen once, against any one of them. Skip
        # (and drop) any queued instance that no longer exists — defensive
        # against a stale/duplicate queue entry pointing at an instance
        # that's since been fully drained and deleted, which would
        # otherwise KeyError below.
        shared_queue = list(members[0]['state'].get('queue', []))
        next_instance = None
        while shared_queue:
            candidate = shared_queue.pop(0)
            if candidate in div_state['pack_instances']:
                next_instance = candidate
                break
            log.warning(
                "PackDraft | _commit_pick: dropping stale queued instance %s for seat %s (div %s) — no longer exists",
                candidate, picker_seat, div_idx,
            )
        for m in members:
            db.update_drafter_state(m['id'], {'current_pack_instance': next_instance, 'queue': shared_queue})
        ctx.save_state(state)

        if next_instance:
            if not auto:
                # Send the pick confirmation now, before the gap, instead of
                # leaving it to bot.py's caller once this whole call returns
                # — otherwise it would arrive bundled with (or even after)
                # the next pack DM below and be easy to miss.
                replied = await ctx.reply_to_trigger(result)
                if not replied:
                    await ctx.dm(drafter['user_id'], result)
                result = ''
            gap = settings.get('pack_delivery_gap_seconds', 3)
            if gap:
                await asyncio.sleep(gap)
            await self._deliver_pack_dm(ctx, div_idx, picker_seat, members, next_instance, div_state['pack_instances'][next_instance]['cards'])

        if not div_state['pack_instances']:
            await self._advance_or_complete_division(ctx, div_idx)

        return result

    async def _route_pack_to_seat(self, ctx: DraftContext, div_idx: int, state: dict, seat_index: int, instance_id: str) -> None:
        div_state = state['divisions'][str(div_idx)]
        members = self._seat_members(ctx, div_idx, seat_index)
        primary = self._seat_primary(members)
        current = primary['state'].get('current_pack_instance')
        queue = list(primary['state'].get('queue', []))

        if current == instance_id or instance_id in queue:
            # Defensive: this exact pack is already this seat's current or
            # already waiting in their queue — routing it again would create
            # a duplicate that could later break queue-popping (e.g. handing
            # back an instance that's since been fully drained and deleted).
            # Shouldn't happen under normal single-lap routing; guard against
            # it regardless of the exact trigger.
            log.warning(
                "PackDraft | _route_pack_to_seat: instance %s already assigned/queued for seat %s (div %s) — skipping duplicate route",
                instance_id, seat_index, div_idx,
            )
            return

        if current is None:
            log.info("PackDraft | routing %s directly to seat=%s (div=%s) — was free", instance_id, seat_index, div_idx)
            for m in members:
                db.update_drafter_state(m['id'], {'current_pack_instance': instance_id})
            ctx.save_state(state)
            await self._deliver_pack_dm(ctx, div_idx, seat_index, members, instance_id, div_state['pack_instances'][instance_id]['cards'])
        else:
            log.info(
                "PackDraft | queuing %s for seat=%s (div=%s) — busy with %s, queue now %s",
                instance_id, seat_index, div_idx, current, queue + [instance_id],
            )
            for m in members:
                db.update_drafter_state(m['id'], {'queue': queue + [instance_id]})

    async def _advance_or_complete_division(self, ctx: DraftContext, div_idx: int) -> None:
        settings = self._settings(ctx)
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        next_round = div_state['round_index'] + 1
        log.info("PackDraft | _advance_or_complete_division: div=%s next_round=%s of %s", div_idx, next_round, settings['rounds'])

        if next_round >= settings['rounds']:
            await self._complete_division(ctx, div_idx)
            return

        div_state['direction'] *= -1
        ctx.save_state(state)
        await self._open_round(ctx, div_idx, next_round)

    async def _complete_division(self, ctx: DraftContext, div_idx: int) -> None:
        log.info("PackDraft | _complete_division: div=%s", div_idx)
        seen_seats: set[int] = set()
        for d in db.list_drafters_in_division(ctx.draft_id, div_idx):
            if d['seat_index'] is not None and d['seat_index'] not in seen_seats:
                seen_seats.add(d['seat_index'])
                _timer.cancel(self._seat_timer_key(ctx, div_idx, d['seat_index']))
            picks = db.get_picks(ctx.draft_id, d['id'])
            roster_text = await self.format_team(ctx, d, picks)
            await ctx.dm(
                d['user_id'],
                f"🏁 Pack Draft picking is complete for your division!\n\n{roster_text}\n\n"
                f"Want to replace anyone, position-wise? `!swap <Player A> / <Player B>` or "
                f"`!swap <Player> / <slot>` to rearrange. Happy with it? Run `!submitroster` with no "
                f"arguments to submit exactly this roster.",
            )
        label = self._division_label(div_idx)
        await ctx.announce(f"🏁 Division {label} finished picking — waiting on roster submissions.")
        div_state = ctx.state['divisions'][str(div_idx)]
        await self._thread_announce(ctx, div_state, f"🏁 Division {label} finished picking — waiting on roster submissions.")

    async def on_roster_submitted(self, ctx: DraftContext, drafter: dict) -> None:
        div_idx = drafter.get('division')
        if div_idx is None:
            return
        div_state = ctx.state.get('divisions', {}).get(str(div_idx))
        if not div_state or not div_state.get('thread_id'):
            return

        seen_seats: set[int] = set()
        missing_seats = []
        for d in db.list_drafters_in_division(ctx.draft_id, div_idx):
            if d['seat_index'] is None or d['seat_index'] in seen_seats:
                continue
            seen_seats.add(d['seat_index'])
            if not d['roster_submitted']:
                missing_seats.append(f"Seat {d['seat_index'] + 1}")
        label = self._division_label(div_idx)
        if missing_seats:
            progress = f"{len(missing_seats)} left to submit: {', '.join(missing_seats)}."
        else:
            progress = "All submitted — waiting on the commish to reveal."
        await self._thread_announce(ctx, div_state, f"📋 A roster was submitted for Division {label}. {progress}")

    # ── Status / misc ────────────────────────────────────────────────────────
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        settings = self._settings(ctx)
        total_picks = settings['rounds'] * settings['pack_size']
        picks_made = len(db.get_picks(ctx.draft_id, drafter['id']))
        div_idx = drafter.get('division')
        div_label = self._division_label(div_idx) if div_idx is not None else '?'
        instance_id = drafter['state'].get('current_pack_instance')

        if not instance_id:
            queue_len = len(drafter['state'].get('queue', []))
            waiting = f" ({queue_len} more queued up)" if queue_len else ''
            return f"Division {div_label}. Picks so far: {picks_made}/{total_picks}. No pack in hand right now{waiting}."

        state = ctx.state
        cards = state.get('divisions', {}).get(str(div_idx), {}).get('pack_instances', {}).get(instance_id, {}).get('cards', [])
        timer_key = self._seat_timer_key(ctx, div_idx, drafter['seat_index'])
        remaining = self._format_remaining(_timer.remaining_seconds(timer_key))
        return (
            f"Division {div_label}. Picks so far: {picks_made}/{total_picks}. On the clock: {remaining}.\n"
            f"Your current pack:\n{self._format_pack(cards, self._active_prices(ctx))}"
        )

    async def resend_current(self, ctx: DraftContext, drafter: dict) -> str:
        div_idx = drafter.get('division')
        instance_id = drafter['state'].get('current_pack_instance')
        if div_idx is None or not instance_id:
            return "You don't have an active pack right now."
        state = ctx.state
        cards = state.get('divisions', {}).get(str(div_idx), {}).get('pack_instances', {}).get(instance_id, {}).get('cards', [])
        if not cards:
            return "You don't have an active pack right now."
        pack_text = self._format_pack(cards, self._active_prices(ctx))
        deadline = drafter['state'].get('timer_deadline')
        deadline_part = f" Deadline: <t:{int(deadline)}:R>" if deadline else ''
        return f"🃏 **Your current pack ({len(cards)} players):**\n{pack_text}\n\nReply with `!pick <player name> [year]`.{deadline_part}"

    async def admin_status(self, ctx: DraftContext) -> str:
        settings = self._settings(ctx)
        state = ctx.state
        lines = []
        for div_idx in range(settings['num_divisions']):
            div_state = state.get('divisions', {}).get(str(div_idx))
            drafters = db.list_drafters_in_division(ctx.draft_id, div_idx)
            label = self._division_label(div_idx)
            if not div_state or not drafters:
                lines.append(f"**Division {label}**: not set up yet.")
                continue
            lines.append(
                f"**Division {label}** — round {div_state['round_index'] + 1}/{settings['rounds']}, "
                f"direction {'forward' if div_state['direction'] == 1 else 'reversed'}"
            )
            seats = sorted({d['seat_index'] for d in drafters if d['seat_index'] is not None})
            for seat in seats:
                members = [d for d in drafters if d['seat_index'] == seat]
                primary = self._seat_primary(members)
                names = ' & '.join(d['display_name'] for d in members)
                picks = len(db.get_picks(ctx.draft_id, primary['id']))
                instance_id = primary['state'].get('current_pack_instance')
                pack_size = len(div_state['pack_instances'].get(instance_id, {}).get('cards', [])) if instance_id else 0
                queue_len = len(primary['state'].get('queue', []))
                timer_key = self._seat_timer_key(ctx, div_idx, seat)
                clock = f", on the clock: {self._format_remaining(_timer.remaining_seconds(timer_key))}" if instance_id else ''
                lines.append(
                    f"  • Seat {seat + 1} ({names}): {picks} picks, pack in hand: {pack_size} cards, "
                    f"queued: {queue_len}, skips: {primary['skip_count']}{clock}"
                )
        return '\n'.join(lines)

    @staticmethod
    def _format_remaining(seconds: float | None) -> str:
        if seconds is None:
            return 'no active timer'
        total = int(seconds)
        m, s = divmod(total, 60)
        return f'{m}m {s}s left'

    def is_pick_phase_complete(self, ctx: DraftContext) -> bool:
        settings = self._settings(ctx)
        total_picks = settings['rounds'] * settings['pack_size']
        return all(
            len(db.get_picks(ctx.draft_id, d['id'])) >= total_picks
            for d in ctx.drafters()
        )

    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        """One team per seat, not per drafter — a duo/trio must dedupe to a
        single column at reveal and a single roster-submission requirement,
        the same way Anonymous Draft's teams do."""
        div_idx = drafter.get('division')
        if div_idx is None or drafter.get('seat_index') is None:
            return str(drafter['id'])
        return f"{div_idx}-{drafter['seat_index']}"

    def team_label(self, ctx: DraftContext, drafter: dict) -> str:
        """The sheet column header at reveal — the team name captured by
        !importorder, not the GM's real Discord name. Falls back to their
        display name for drafters added some other way (no team name set)."""
        return drafter['state'].get('team_name') or drafter['display_name'] or str(drafter['user_id'])

    def linked_drafter_ids(self, ctx: DraftContext, drafter: dict) -> list[int]:
        """A roster submission from any one teammate covers the whole seat —
        same reasoning as team_key above."""
        div_idx = drafter.get('division')
        if div_idx is None or drafter.get('seat_index') is None:
            return [drafter['id']]
        return [d['id'] for d in self._seat_members(ctx, div_idx, drafter['seat_index'])]

    def final_roster_rows(self, ctx: DraftContext, drafter: dict) -> list:
        roster = drafter['state'].get('final_roster', [])
        years = drafter['state'].get('player_years', {})
        prices = self._active_prices(ctx)
        rows = []
        for player in roster:
            year = years.get(player.lower()) or ''
            price = self._price_of(prices, player)
            price_str = f"${price:.0f}" if price is not None else ''
            rows.append((player, year, price_str))
        return rows

    # ── Roster ───────────────────────────────────────────────────────────────
    def validate_final_roster(self, ctx: DraftContext, drafter: dict, submitted: list[str]) -> tuple[bool, list[str]]:
        # Position/slot fit is intentionally NOT enforced here — the sheet
        # write (write_draft_results) is just one column of names, it has no
        # concept of starter/bench slots, so this was a self-imposed rule
        # with no functional purpose. Budget (via roster.validate_roster,
        # called before this) is the only thing that should actually reject
        # a submission.
        settings = self._settings(ctx)
        slot_positions = settings['roster_positions']
        needed = len(slot_positions) * 2
        if len(submitted) != needed:
            return False, [f"Need exactly {needed} players ({len(slot_positions)} starters + {len(slot_positions)} bench), got {len(submitted)}."]
        if len(set(p.lower() for p in submitted)) != len(submitted):
            return False, ["Duplicate players in your submission."]
        return True, []

    # ── Editable roster (persists across !mypicks calls, unlike the
    # stateless auto-slot used for `!submitroster`'s explicit-list path) ────
    @staticmethod
    def _slot_labels(slot_positions: list[str]) -> list[tuple[str, str]]:
        return [('Starting', pos) for pos in slot_positions] + [('Bench', pos) for pos in slot_positions]

    def _ensure_roster_slots(self, ctx: DraftContext, drafter: dict) -> tuple[list[str | None], dict[str, str], list[str]]:
        """Auto-places any drafted player not yet tracked into the first
        open eligible slot, without disturbing slots/swaps already made —
        safe to call every time (!mypicks, !swap, !addyear all call this
        first to pick up picks made since the drafter last looked)."""
        settings = self._settings(ctx)
        slot_positions = settings['roster_positions']
        labels = self._slot_labels(slot_positions)
        n = len(labels)

        state = drafter['state']
        slots: list[str | None] = list(state.get('roster_slots') or [])
        years: dict[str, str] = dict(state.get('player_years') or {})
        unslotted: list[str] = list(state.get('roster_unslotted') or [])

        if len(slots) < n:
            slots += [None] * (n - len(slots))
        elif len(slots) > n:
            # settings shrank mid-draft — don't lose players, just bump them out to unslotted
            unslotted += [p for p in slots[n:] if p]
            slots = slots[:n]

        placed_lower = {p.lower() for p in slots if p} | {p.lower() for p in unslotted}
        picks = db.get_picks(ctx.draft_id, drafter['id'])
        changed = False

        for player in picks:
            if player.lower() in placed_lower:
                continue
            placed_lower.add(player.lower())
            changed = True
            eligible = positions_module.get_positions(player) or slot_positions
            placed = False
            # Walk the player's *own* preferred position order first (not
            # fixed PG->C slot order), starters before bench.
            for want_prefix in ('Starting', 'Bench'):
                for pos in eligible:
                    i = next((i for i, (prefix, p) in enumerate(labels) if prefix == want_prefix and p == pos and slots[i] is None), None)
                    if i is not None:
                        slots[i] = player
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                unslotted.append(player)

        if changed:
            self._save_roster(drafter, slots, unslotted, years)

        return slots, years, unslotted

    @staticmethod
    def _save_roster(drafter: dict, slots: list, unslotted: list, years: dict) -> None:
        state = drafter['state']
        state['roster_slots'] = slots
        state['roster_unslotted'] = unslotted
        state['player_years'] = years
        db.set_drafter_state(drafter['id'], state)

    async def format_picks(self, ctx: DraftContext, drafter: dict, picks: list[str]) -> str:
        """!mypicks — everything drafted so far, sorted by price (highest
        first). For the roster/lineup view instead, see format_team."""
        if not picks:
            return "You haven't drafted anyone yet."

        prices = self._active_prices(ctx)
        if prices:
            ordered = sorted(
                picks,
                key=lambda p: (self._price_of(prices, p) is None, -(self._price_of(prices, p) or 0), p.lower()),
            )
            lines = ['**Your picks, sorted by price (highest first):**']
            for i, p in enumerate(ordered, start=1):
                price = self._price_of(prices, p)
                tag = f' (${price:.0f})' if price is not None else ' (no price on file)'
                lines.append(f'{i}. {p}{tag}')
        else:
            ordered = sorted(picks, key=str.lower)
            lines = ['**Your picks (no price data available — sorted alphabetically):**']
            for i, p in enumerate(ordered, start=1):
                lines.append(f'{i}. {p}')

        lines.append('')
        lines.append("`!myteam` — your current roster/lineup · `!add <player>` — place one · `!swap` — rearrange")
        return '\n'.join(lines)

    async def format_team(self, ctx: DraftContext, drafter: dict, picks: list[str]) -> str:
        """!myteam — the roster/lineup view (5 starters + 5 bench by position)."""
        if not picks:
            return "You haven't drafted anyone yet."

        settings = self._settings(ctx)
        labels = self._slot_labels(settings['roster_positions'])
        slots, years, unslotted = self._ensure_roster_slots(ctx, drafter)
        prices = self._active_prices(ctx)

        lines = ['**Your roster so far:**']
        total = 0.0
        priced_count = 0
        for i, (prefix, pos) in enumerate(labels):
            player = slots[i]
            if player:
                year = years.get(player.lower())
                name_part = f"{player} '{year}" if year else player
                price = self._price_of(prices, player)
                if price is not None:
                    total += price
                    priced_count += 1
                    display = f"{name_part} (${price:.0f})"
                else:
                    display = name_part
            else:
                display = '—'
            lines.append(f"{i + 1}. {prefix} {pos}: {display}")

        if prices and priced_count:
            budget = settings['budget']
            over = total > budget
            note = ' ⚠️ over budget' if over else ''
            lines.append('')
            lines.append(f"**Budget: ${total:.0f} / ${budget:.0f}**{note}")

        if unslotted:
            lines.append('')
            unslotted_display = []
            for p in unslotted:
                price = self._price_of(prices, p)
                unslotted_display.append(f"{p} (${price:.0f})" if price is not None else p)
            lines.append(f"**Also drafted ({len(unslotted)}):** " + ', '.join(unslotted_display))
            lines.append(
                "Use `!add " + unslotted[0] + "` to auto-place one, or `!swap <player> / <slot>` to choose exactly where."
            )

        return '\n'.join(lines)

    def _resolve_roster_ref(self, ctx: DraftContext, drafter: dict, ref: str, slots: list) -> tuple[str, object] | None:
        """Resolve a !swap argument to ('slot', index) or ('player', name)."""
        ref = ref.strip()
        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(slots):
                return ('slot', idx)
            return None
        picks = db.get_picks(ctx.draft_id, drafter['id'])
        match = next((p for p in picks if p.lower() == ref.lower()), None)
        if not match:
            candidates = [p for p in picks if ref.lower() in p.lower()]
            if len(candidates) == 1:
                match = candidates[0]
        return ('player', match) if match else None

    # Roster edits below (_save_roster) write the whole drafter.state blob
    # back wholesale — without a lock/re-fetch, an edit racing a pack
    # routing/timeout mid-flight for the same drafter (e.g. during the await
    # inside a DM send) could silently clobber whichever finished last, the
    # same class of bug fixed in on_pick above.
    async def swap_slots(self, ctx: DraftContext, drafter: dict, arg_a: str, arg_b: str) -> str:
        async with draft_lock(ctx.draft_id):
            drafter = db.get_drafter(drafter['id'])
            settings = self._settings(ctx)
            slot_positions = settings['roster_positions']
            labels = self._slot_labels(slot_positions)
            slots, years, unslotted = self._ensure_roster_slots(ctx, drafter)
            n = len(slots)

            ref_a = self._resolve_roster_ref(ctx, drafter, arg_a, slots)
            ref_b = self._resolve_roster_ref(ctx, drafter, arg_b, slots)
            if ref_a is None:
                return f"❌ Couldn't find **{arg_a}** — must be a slot number (1-{n}) or a player you've drafted."
            if ref_b is None:
                return f"❌ Couldn't find **{arg_b}** — must be a slot number (1-{n}) or a player you've drafted."

            def _slot_index(ref):
                if ref[0] == 'slot':
                    return ref[1]
                for i, v in enumerate(slots):
                    if v and v.lower() == ref[1].lower():
                        return i
                return None

            idx_a, idx_b = _slot_index(ref_a), _slot_index(ref_b)
            player_a = ref_a[1] if ref_a[0] == 'player' else None
            player_b = ref_b[1] if ref_b[0] == 'player' else None

            if idx_a is None and idx_b is None:
                return f"Neither of those is in a roster slot yet — reference a slot number (1-{n}) to place one."
            if idx_a is not None and idx_a == idx_b:
                return "Those are already the same slot."

            if idx_a is None:
                unslotted = [p for p in unslotted if p.lower() != player_a.lower()]
                if slots[idx_b]:
                    unslotted.append(slots[idx_b])
                slots[idx_b] = player_a
            elif idx_b is None:
                unslotted = [p for p in unslotted if p.lower() != player_b.lower()]
                if slots[idx_a]:
                    unslotted.append(slots[idx_a])
                slots[idx_a] = player_b
            else:
                slots[idx_a], slots[idx_b] = slots[idx_b], slots[idx_a]

            self._save_roster(drafter, slots, unslotted, years)

            warnings = []
            for idx in (idx_a, idx_b):
                if idx is not None and slots[idx]:
                    prefix, pos = labels[idx]
                    eligible = positions_module.get_positions(slots[idx])
                    if eligible and pos not in eligible:
                        warnings.append(f"⚠️ **{slots[idx]}** isn't normally {pos}-eligible ({prefix} {pos}, slot {idx + 1}) — swapped anyway, but double check before `!submitroster`.")

            note = ('\n' + '\n'.join(warnings)) if warnings else ''
            picks = db.get_picks(ctx.draft_id, drafter['id'])
            roster_text = await self.format_team(ctx, drafter, picks)
            return f"✅ Updated.{note}\n\n{roster_text}"

    async def set_player_year(self, ctx: DraftContext, drafter: dict, player: str, year: str) -> str:
        async with draft_lock(ctx.draft_id):
            drafter = db.get_drafter(drafter['id'])
            picks = db.get_picks(ctx.draft_id, drafter['id'])
            match = next((p for p in picks if p.lower() == player.lower()), None)
            if not match:
                candidates = [p for p in picks if player.lower() in p.lower()]
                if len(candidates) == 1:
                    match = candidates[0]
            if not match:
                return f"❌ **{player}** isn't one of your drafted players."

            slots, years, unslotted = self._ensure_roster_slots(ctx, drafter)
            years[match.lower()] = year.strip()
            self._save_roster(drafter, slots, unslotted, years)
            picks = db.get_picks(ctx.draft_id, drafter['id'])
            roster_text = await self.format_team(ctx, drafter, picks)
            return f"✅ **{match}**'s year set to **{year.strip()}**.\n\n{roster_text}"

    async def add_player(self, ctx: DraftContext, drafter: dict, player: str) -> str:
        async with draft_lock(ctx.draft_id):
            drafter = db.get_drafter(drafter['id'])
            settings = self._settings(ctx)
            slot_positions = settings['roster_positions']
            labels = self._slot_labels(slot_positions)
            slots, years, unslotted = self._ensure_roster_slots(ctx, drafter)

            picks = db.get_picks(ctx.draft_id, drafter['id'])
            match = next((p for p in picks if p.lower() == player.lower()), None)
            if not match:
                candidates = [p for p in picks if player.lower() in p.lower()]
                if len(candidates) == 1:
                    match = candidates[0]
            if not match:
                return f"❌ **{player}** isn't one of your drafted players."

            if match in slots:
                idx = slots.index(match)
                prefix, pos = labels[idx]
                return f"**{match}** is already on your roster ({prefix} {pos}, slot {idx + 1}). Use `!swap` to move them."

            eligible = positions_module.get_positions(match) or slot_positions
            placed_idx = None
            for want_prefix in ('Starting', 'Bench'):
                for pos in eligible:
                    i = next((i for i, (prefix, p) in enumerate(labels) if prefix == want_prefix and p == pos and slots[i] is None), None)
                    if i is not None:
                        placed_idx = i
                        break
                if placed_idx is not None:
                    break

            if placed_idx is None:
                return (f"❌ No open slot for **{match}** — every position they're eligible for is full. "
                        f"Use `!swap {match} / <slot>` to bump someone out instead.")

            slots[placed_idx] = match
            unslotted = [p for p in unslotted if p.lower() != match.lower()]
            self._save_roster(drafter, slots, unslotted, years)

            prefix, pos = labels[placed_idx]
            picks = db.get_picks(ctx.draft_id, drafter['id'])
            roster_text = await self.format_team(ctx, drafter, picks)
            return f"✅ Added **{match}** to your roster ({prefix} {pos}, slot {placed_idx + 1}).\n\n{roster_text}"

    def current_roster_list(self, ctx: DraftContext, drafter: dict) -> list[str] | None:
        slots, _years, _unslotted = self._ensure_roster_slots(ctx, drafter)
        if not slots or any(s is None for s in slots):
            return None
        return list(slots)

    def roster_requirements(self, ctx: DraftContext) -> tuple[dict | None, float | None]:
        prices = self._active_prices(ctx)
        if not prices:
            return None, None
        return prices, self._settings(ctx)['budget']

    def best_affordable_roster(self, ctx: DraftContext, drafter: dict) -> list[str] | None:
        """Fallback for !theme rostertimer: fills every starter/bench slot
        from this drafter's picks, within budget, biased toward spending on
        the 5 STARTERS specifically — bench stays at whatever's cheapest,
        starters get whatever budget that leaves.

        1. Establish feasibility with the CHEAPEST legal roster overall
           (cheapest player first, placed into any open eligible slot). If
           even that busts budget, or slots can't all be filled at all
           (including because a drafted player has no known position — no
           "assume eligible everywhere" fallback here, unlike the
           interactive `!add`/`!myteam` auto-slotting, since nobody's
           watching an unattended deadline-driven auto-submit to catch a
           bad guess), bail out.
        2. Hill-climb ONLY the 5 starter slots — repeatedly upgrade to a
           pricier eligible player as long as the swap still fits the
           budget — leaving bench at its established cheap baseline.
        Not guaranteed globally optimal (that's an NP-hard matching +
        knapsack problem) but a solid, budget-safe approximation."""
        settings = self._settings(ctx)
        slot_positions = settings['roster_positions']
        labels = self._slot_labels(slot_positions)
        prices = self._active_prices(ctx)
        budget = settings['budget']
        if not prices or budget is None:
            return None

        picks = db.get_picks(ctx.draft_id, drafter['id'])
        priced = [(p, self._price_of(prices, p)) for p in set(picks)]
        priced = [(p, pr) for p, pr in priced if pr is not None]  # unpriced players would fail validate_budget anyway

        def eligible(player: str) -> list[str]:
            return positions_module.get_positions(player)  # [] (never a fallback) if unlisted

        priced = [(p, pr) for p, pr in priced if eligible(p)]

        slot_candidates = [
            [(p, pr) for p, pr in priced if pos in eligible(p)]
            for _prefix, pos in labels
        ]

        # Phase 1: cheapest-first placement to establish feasibility.
        slots: list[str | None] = [None] * len(labels)
        slot_prices: list[float] = [0.0] * len(labels)
        for player, price in sorted(priced, key=lambda t: t[1]):
            for i, (_prefix, pos) in enumerate(labels):
                if slots[i] is None and pos in eligible(player):
                    slots[i] = player
                    slot_prices[i] = price
                    break

        if any(s is None for s in slots):
            return None  # no legal roster exists at all, regardless of budget
        if sum(slot_prices) > budget:
            return None  # even the cheapest legal roster busts the budget

        # Phase 2: hill-climb starters only — upgrade to pricier eligible
        # players while the swap still fits, until nothing more improves.
        # Bench intentionally stays at its Phase-1 cheap baseline — except
        # when a starter steals its own position's bench player (Option A
        # below), which is exactly what's supposed to happen: Phase 1 fills
        # slots in label order with no regard for starter-vs-bench, so it
        # often parks the cheap pick in the starter slot and the pricier
        # one on the bench purely because starters come first in the list.
        n_positions = len(slot_positions)
        starter_idxs = list(range(n_positions))  # _slot_labels puts starters before bench, same position order
        used = set(slots)
        improved = True
        while improved:
            improved = False
            spent = sum(slot_prices)
            for i in starter_idxs:
                current_player, current_price = slots[i], slot_prices[i]

                # Option A: swap with this position's own bench slot if
                # that's more valuable and the swap still fits — this is
                # the move a pure "upgrade to an unused candidate" search
                # can never find, since the bench occupant already counts
                # as "used".
                bench_i = i + n_positions
                bench_player, bench_price = slots[bench_i], slot_prices[bench_i]
                if bench_price > current_price and spent - current_price + bench_price <= budget:
                    slots[i], slots[bench_i] = bench_player, current_player
                    slot_prices[i], slot_prices[bench_i] = bench_price, current_price
                    spent = spent - current_price + bench_price
                    improved = True
                    continue

                # Option B: upgrade to any other unused, pricier candidate.
                headroom = budget - spent + current_price
                best_player, best_price = current_player, current_price
                for player, price in slot_candidates[i]:
                    if player in used and player != current_player:
                        continue
                    if price > best_price and price <= headroom:
                        best_player, best_price = player, price
                if best_player != current_player:
                    used.discard(current_player)
                    used.add(best_player)
                    slots[i] = best_player
                    slot_prices[i] = best_price
                    spent = spent - current_price + best_price
                    improved = True

        return slots

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _deliver_pack_dm(self, ctx: DraftContext, div_idx: int, seat_index: int,
                                members: list[dict], instance_id: str, cards: list[str]) -> None:
        settings = self._settings(ctx)
        primary = self._seat_primary(members)
        minutes = PickTimer.effective_minutes(
            settings['base_timer_minutes'], settings['decay_minutes'], settings['floor_minutes'], primary['skip_count'],
        )
        div_label = self._division_label(div_idx)
        prices = self._active_prices(ctx)
        team_note = f" — {len(members)}-GM team, first `!pick` wins" if len(members) > 1 else ''
        # Discord's <t:...:R> renders as a live, auto-updating countdown
        # client-side ("in 59 minutes" ticking down on its own) — no bot
        # edits needed, unlike a plain "You have N minutes" static line.
        deadline_ts = int(time.time() + minutes * 60)
        content = (
            f"🃏 **Division {div_label} — your pack{team_note} ({len(cards)} players):**\n{self._format_pack(cards, prices)}\n\n"
            f"Reply with `!pick <player name> [year]`. Deadline: <t:{deadline_ts}:R>"
        )
        delivered_any = False
        failed = []
        for m in members:
            if await ctx.dm(m['user_id'], content):
                delivered_any = True
            else:
                failed.append(m)

        if failed:
            # Don't skip the timer just because one teammate's DM failed —
            # only skip it if NOBODY on the seat can see the pack at all.
            names = ', '.join(f"**{m['display_name']}** (<@{m['user_id']}>)" for m in failed)
            await ctx.announce(
                f"⚠️ Couldn't DM {names} their Division {div_label} pack. They likely have server-member DMs "
                f"off, or have blocked the bot. `!pack` in DM shows it fine once fixed."
            )
        if not delivered_any:
            await ctx.announce(
                f"⚠️ Nobody on Seat {seat_index + 1} (Division {div_label}) could be DMed — no timer started. "
                f"`!packskip <@{primary['user_id']}>` can force the team along."
            )
            return
        self._start_pick_timer(ctx, div_idx, seat_index, members, minutes)

    def _start_pick_timer(self, ctx: DraftContext, div_idx: int, seat_index: int,
                           members: list[dict], minutes: float) -> None:
        # Persisted as a real deadline (wall-clock, survives restarts) —
        # not just an in-memory asyncio delay — so resume_timers() can work
        # out genuine remaining time instead of granting a fresh full clock.
        # One shared timer per seat, not per member, so a duo/trio's clock
        # only ever fires once.
        deadline = time.time() + minutes * 60
        log.info(
            "PackDraft | _start_pick_timer: seat=%s div=%s minutes=%.2f deadline=%s members=%s",
            seat_index, div_idx, minutes, deadline, [m['id'] for m in members],
        )
        for m in members:
            db.update_drafter_state(m['id'], {'timer_deadline': deadline})

        async def _on_timeout():
            log.info("PackDraft | seat timer closure firing: seat=%s div=%s primary=%s", seat_index, div_idx, members[0]['id'])
            await self.on_timeout(ctx, members[0])

        _timer.start(self._seat_timer_key(ctx, div_idx, seat_index), minutes, _on_timeout)

    @staticmethod
    def _clear_timer_deadline(members: list[dict]) -> None:
        for m in members:
            db.update_drafter_state(m['id'], {'timer_deadline': None})

    async def resume_timers(self, ctx: DraftContext) -> int:
        """Called on bot startup/reconnect for each active draft — timers
        only live in this process's memory, so a restart silently drops any
        countdown that was mid-flight even though the pack/pick state itself
        (in sqlite) survives fine. Resumes from the persisted deadline (see
        _start_pick_timer) so a seat gets its genuine remaining time, not a
        fresh full clock — or an immediate auto-pick if it had already
        expired while the bot was down. One resume per seat, not per member.
        Returns how many were resumed (timer restarted or auto-picked)."""
        settings = self._settings(ctx)
        resumed = 0
        seen_seats: set[tuple] = set()
        for drafter in ctx.drafters():
            div_idx = drafter.get('division')
            instance_id = drafter['state'].get('current_pack_instance')
            if div_idx is None or not instance_id:
                continue
            seat_index = drafter['seat_index']
            seat_key = (div_idx, seat_index)
            if seat_key in seen_seats or _timer.is_running(self._seat_timer_key(ctx, div_idx, seat_index)):
                continue
            seen_seats.add(seat_key)

            members = self._seat_members(ctx, div_idx, seat_index)
            primary = self._seat_primary(members)
            deadline = primary['state'].get('timer_deadline')
            if deadline is None:
                # No persisted deadline (e.g. pre-existing state from before
                # this was tracked) — fall back to a fresh full timer.
                minutes = PickTimer.effective_minutes(
                    settings['base_timer_minutes'], settings['decay_minutes'], settings['floor_minutes'], primary['skip_count'],
                )
                delivered_any = False
                for m in members:
                    ok = await ctx.dm(
                        m['user_id'],
                        f"🔄 Reconnected — your timer has been reset to **{minutes:.0f} minutes** for your current pack "
                        f"(no prior deadline on file). Use `!pack` to see it again.",
                    )
                    delivered_any = delivered_any or ok
                if delivered_any:
                    self._start_pick_timer(ctx, div_idx, seat_index, members, minutes)
                    resumed += 1
                else:
                    await ctx.announce(
                        f"⚠️ Couldn't DM Seat {seat_index + 1}'s team on reconnect — no timer started. "
                        f"`!pack` in DM will still work once fixed."
                    )
                continue

            remaining_seconds = deadline - time.time()
            if remaining_seconds <= 0:
                for m in members:
                    await ctx.dm(m['user_id'], "🔄 Reconnected — your timer had already run out while I was offline. Auto-picking now.")
                await self.on_timeout(ctx, primary)
            else:
                minutes = remaining_seconds / 60
                for m in members:
                    await ctx.dm(
                        m['user_id'],
                        f"🔄 Reconnected — you still have **{minutes:.0f} minutes** left on your current pack. "
                        f"Use `!pack` to see it again.",
                    )
                self._start_pick_timer(ctx, div_idx, seat_index, members, minutes)
            resumed += 1
        return resumed

    async def cancel(self, ctx: DraftContext) -> None:
        seen_seats: set[tuple] = set()
        for drafter in ctx.drafters():
            div_idx = drafter.get('division')
            self._clear_timer_deadline([drafter])
            if div_idx is None:
                continue
            seat_key = (div_idx, drafter['seat_index'])
            if seat_key in seen_seats:
                continue
            seen_seats.add(seat_key)
            _timer.cancel(self._seat_timer_key(ctx, div_idx, drafter['seat_index']))

    # ── Live spectator threads (optional — see module docstring) ───────────────
    async def _ensure_division_thread(self, ctx: DraftContext, div_idx: int) -> None:
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        if div_state.get('thread_id'):
            return  # already created (e.g. a restart mid-round-0 re-ran start())
        channel_id = self._settings(ctx).get('public_channel_id')
        if not channel_id:
            return
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            log.warning("PackDraft | public_channel_id %s not found/not cached", channel_id)
            return
        label = self._division_label(div_idx)
        try:
            thread = await channel.create_thread(
                name=f"Division {label} — {ctx.draft['name']}",
                type=discord.ChannelType.public_thread,
            )
            div_state['thread_id'] = thread.id
            ctx.save_state(state)
            await thread.send(
                f"🃏 **Division {label}** pack draft is starting. Picks and pack movement are tracked "
                f"here by **seat number only** — rosters stay hidden until reveal."
            )
        except Exception:
            log.warning("PackDraft | failed to create division thread for div=%s", div_idx, exc_info=True)

    async def _thread_announce(self, ctx: DraftContext, div_state: dict, content: str) -> None:
        thread_id = div_state.get('thread_id')
        if not thread_id:
            return
        thread = ctx.bot.get_channel(thread_id)
        if not thread:
            return
        try:
            await thread.send(content)
        except Exception:
            log.warning("PackDraft | failed to post to division thread %s", thread_id, exc_info=True)

    def _active_prices(self, ctx: DraftContext) -> dict:
        """Prices to display/enforce — empty when enforce_budget is off, even
        if the pool sheet has a price column, so a "no money" draft doesn't
        show confusing price tags everywhere."""
        if not self._settings(ctx)['enforce_budget']:
            return {}
        return ctx.state.get('prices') or {}

    @staticmethod
    def _price_of(prices: dict, player: str) -> float | None:
        return prices.get(player, prices.get(player.lower()))

    async def _check_budget_feasibility(self, ctx: DraftContext, packs: list[list[str]]) -> str | None:
        """Warns (via the admin channel, and the returned string) if even the
        cheapest possible legal-roster-sized selection from these packs
        would already bust the budget — a necessary-but-not-sufficient check
        (ignores position requirements, so real infeasibility can be worse
        than this suggests), but enough to catch an obviously bad pool/budget
        mismatch before 32 people spend an hour drafting into a dead end."""
        settings = self._settings(ctx)
        prices = self._active_prices(ctx)
        if not prices:
            return None

        needed = len(settings['roster_positions']) * 2
        seeded = {p for pack in packs for p in pack}
        priced = sorted(price for p in seeded if (price := self._price_of(prices, p)) is not None)
        if len(priced) < needed:
            return None  # not enough priced players seeded to evaluate meaningfully

        cheapest_total = sum(priced[:needed])
        budget = settings['budget']
        if cheapest_total <= budget:
            return None

        warning = (
            f"⚠️ Budget warning: even the {needed} cheapest priced players seeded into these packs "
            f"total ${cheapest_total:.0f}, over the ${budget:.0f} budget — it may be impossible to "
            f"build a legal roster from them. Consider raising `budget`, lowering `tier_size` (biases "
            f"toward cheaper tiers), or `enforce_budget=false`."
        )
        await ctx.announce(warning)
        return warning

    @classmethod
    def _format_pack(cls, cards: list[str], prices: dict | None = None) -> str:
        prices = prices or {}
        lines = []
        for i, c in enumerate(cards):
            price = cls._price_of(prices, c)
            tag = f' (${price:.0f})' if price is not None else ''
            lines.append(f'{i + 1}. {c}{tag}')
        return '\n'.join(lines)

    @staticmethod
    def _resolve_player(text: str, cards: list[str]) -> str | None:
        query = text.strip().lower()
        if not query:
            return None
        for c in cards:
            if c.lower() == query:
                return c
        matches = [c for c in cards if query in c.lower()]
        if len(matches) == 1:
            return matches[0]
        return None

    @classmethod
    def _resolve_pick(cls, text: str, cards: list[str]) -> tuple[str | None, str | None]:
        """Parses `!pick <player> [year]` — if the last token looks like a
        year and stripping it still resolves to a card in this pack, treats
        it as the year (e.g. `!pick LeBron James 2013`). Otherwise falls
        back to matching the whole text with no year, same as before."""
        parts = text.strip().split()
        if len(parts) >= 2 and _YEAR_TOKEN_RE.match(parts[-1]):
            year = parts[-1].lstrip("'")
            player = cls._resolve_player(' '.join(parts[:-1]), cards)
            if player:
                return player, year
        return cls._resolve_player(text, cards), None
