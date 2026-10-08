# themes/nation_flux.py
#
# Nation Flux: 4 independent 8-team drafts — Earth, Fire, Water, Air — each
# running its own snake-order, name-a-player draft (same shape as the
# self-driven Anonymous Draft engine, just scoped across 4 divisions instead
# of one flat team list). Prices are NOT typed by GMs — they're read live
# from ATD Flux Bot's own Google Sheet, which gets a new "Round N" price
# column every time the commish runs !flux there.
#
# Each nation carries one unique power, usable exactly once for the whole
# draft, activated by a >half-of-the-nation majority — no vote timer. Every
# GM who DMs `!activate` either opens the proposal (the first one) or adds
# their own vote to an already-open one; each vote shows up as another bot
# reaction on a single message posted in the nation's own thread, so the
# whole nation can watch support build live. The moment enough GMs have
# DMed `!activate`, the power fires immediately:
#   Earth — only Earth's own teams get a one-time random +$1-5 budget bump.
#   Fire  — the OTHER 3 nations (not Fire) pay the HIGHER of a player's last
#           2 sheet prices on every pick. Fire itself pays the normal price.
#   Water — ONLY Water's own picks are charged the LOWER of a player's last
#           2 prices. Nobody else gets this, including Fire.
#   Air   — has no independent activation. It can only react (`!airreact`)
#           WHILE another nation's own proposal is still open (not yet
#           activated), needing the same majority vote from Air's own
#           nation:
#             react to Fire  -> Air's nation is exempt from Fire's tax too.
#             react to Water -> Air's nation gets an ENHANCED discount (low
#                                of the last 3 prices, not 2).
#             react to Earth -> Air's nation gets a flat +$1 budget bump.
#
# An activated power lapses automatically the next time !flux runs on the
# sheet — "is X still active" is always computed live against the sheet's
# current price-column count, never a manually-cleared flag, so there's no
# separate expiry code path to get wrong.

import asyncio
import logging
import random
import re
import time

import discord

import db
import lotto
import sheets
from config import FLUX_SPREADSHEET_ID, FLUX_WORKSHEET_GID
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock

log = logging.getLogger(__name__)

_timer = PickTimer()        # keyed by (draft_id, div_idx, team_idx) — one clock per team

_NATION_NAMES = ['Earth', 'Fire', 'Water', 'Air']
_NATION_KEYS = ['earth', 'fire', 'water', 'air']
_NATION_EMOJI = ['🌍', '🔥', '🌊', '💨']
_EARTH, _FIRE, _WATER, _AIR = range(4)
_SECTION_HEADERS = {name.upper(): idx for idx, name in enumerate(_NATION_NAMES)}

# Results channel Anonymous Draft already posts to for this league — also
# the same channel ATD Flux Bot itself tracks for !flux (see its .env).
_RESULTS_CHANNEL_ID = 934052208624930848

_YEAR_TOKEN_RE = re.compile(r"^'?\d{2,4}(-\d{2,4})?$")

# Apostrophe-lookalike normalization, same table used elsewhere in this
# bot — a curly apostrophe from one GM's phone and a straight one from
# another's would otherwise compare as different strings.
_APOSTROPHE_VARIANTS = str.maketrans({
    '‘': "'", '’': "'", 'ʼ': "'", '´': "'", '`': "'",
})


def _normalize_name(text: str) -> str:
    return text.translate(_APOSTROPHE_VARIANTS)


class NationFluxTheme(Theme):
    key = 'nation_flux'
    display_name = 'Nation Flux'

    def default_settings(self) -> dict:
        return {
            'rounds': 10,
            'budget': 120,
            'base_timer_minutes': 60,
            'decay_minutes': 10,
            'floor_minutes': 0,  # a team that keeps skipping eventually gets skipped immediately
        }

    def _settings(self, ctx: DraftContext) -> dict:
        return {**self.default_settings(), **ctx.settings}

    def division_label(self, ctx: DraftContext, idx: int) -> str:
        return _NATION_NAMES[idx] if 0 <= idx < 4 else f"Division {idx + 1}"

    # ── Setup (DM-only, commish) ─────────────────────────────────────────────
    async def setup_teams(self, ctx: DraftContext, text: str) -> str:
        sections: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
        current = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.upper() in _SECTION_HEADERS:
                current = _SECTION_HEADERS[stripped.upper()]
                continue
            if current is not None and stripped:
                sections[current].append(line)

        seen_users: set[int] = set()
        parsed_by_division: dict[int, list[dict]] = {}
        for div_idx in range(4):
            parsed = lotto.parse_team_lines('\n'.join(sections[div_idx]))
            if len(parsed) != 8:
                return (
                    f"❌ **{_NATION_NAMES[div_idx]}** has {len(parsed)} team(s) — needs exactly 8. "
                    f"Format: one `EARTH`/`FIRE`/`WATER`/`AIR` header per nation, then "
                    f"`1. Team Name - @GM1 @GM2` lines underneath (8 per nation)."
                )
            for entry in parsed:
                if not (1 <= len(entry['user_ids']) <= 3):
                    return f"❌ {_NATION_NAMES[div_idx]} team on line {entry['order_index'] + 1} has {len(entry['user_ids'])} member(s) — must be 1-3."
                for uid in entry['user_ids']:
                    if uid in seen_users:
                        return f"❌ <@{uid}> is listed on more than one team."
                    seen_users.add(uid)
            parsed_by_division[div_idx] = parsed

        divisions: dict[str, dict] = {}
        for div_idx in range(4):
            teams = []
            for entry in parsed_by_division[div_idx]:
                member_ids = []
                for uid in entry['user_ids']:
                    drafter = db.get_drafter_by_user(ctx.draft_id, uid)
                    if drafter:
                        member_ids.append(drafter['id'])
                        continue
                    display_name = str(uid)
                    try:
                        user = ctx.bot.get_user(uid) or await ctx.bot.fetch_user(uid)
                        if user:
                            display_name = getattr(user, 'display_name', None) or user.name
                    except Exception:
                        pass
                    drafter_id = db.add_drafter(ctx.draft_id, uid, display_name, division=div_idx)
                    member_ids.append(drafter_id)
                team_idx = len(teams)
                for mid in member_ids:
                    db.update_drafter_state(mid, {'team_index': team_idx})
                teams.append({
                    'index': team_idx, 'name': entry['logo'], 'member_ids': member_ids,
                    'budget_spent': 0.0, 'open_picks': [],
                    'timer_pick_number': None, 'timer_deadline': None, 'skip_count': 0,
                })
            divisions[str(div_idx)] = {'teams': teams, 'last_finalized_pick_number': 0, 'live_pick_number': None}

        state = ctx.state
        state['divisions'] = divisions
        state['powers'] = {
            'fire': {'used': False, 'sheet_round_count': None},
            'water': {'used': False, 'sheet_round_count': None},
            'earth': {'used': False, 'sheet_round_count': None},
        }
        state['air_reacted_to'] = None
        state['proposals'] = {}
        state['air_reaction'] = None
        ctx.save_state(state)

        return f"✅ Registered 4 nations x 8 teams ({len(seen_users)} GMs total). Run `!theme start {ctx.draft_id}` to open pick #1 in every nation."

    async def start(self, ctx: DraftContext) -> str:
        state = ctx.state
        divisions = state.get('divisions', {})
        if not divisions:
            raise ValueError("No teams registered — DM me `!setteams` first.")
        for div_idx in range(4):
            div_state = state['divisions'][str(div_idx)]
            div_state['last_finalized_pick_number'] = 0
            div_state['live_pick_number'] = 1
        ctx.save_state(state)
        threads = []
        for div_idx in range(4):
            thread = await self._get_or_create_thread(ctx, div_idx)
            threads.append(thread)
        for div_idx in range(4):
            await self._open_pick_for_team(ctx, div_idx, 0, 1)
        settings = self._settings(ctx)
        thread_lines = '\n'.join(
            f"• {_NATION_NAMES[i]}: {t.mention if t else '⚠️ thread creation failed, falling back to the results channel'}"
            for i, t in enumerate(threads)
        )
        return (
            f"Nation Flux started — 4 nations x 8 teams, {settings['rounds']} rounds each. "
            f"Pick #1 is open in every nation, each posting to its own thread:\n{thread_lines}"
        )

    # ── Turn order (self-driven, one independent engine per nation) ─────────
    @staticmethod
    def _team_index_for_pick(pick_number: int, n_teams: int, rounds: int) -> int | None:
        if n_teams <= 0 or not (1 <= pick_number <= n_teams * rounds):
            return None
        idx0 = pick_number - 1
        round_idx = idx0 // n_teams
        pos = idx0 % n_teams
        return pos if round_idx % 2 == 0 else n_teams - 1 - pos

    async def _get_or_create_thread(self, ctx: DraftContext, div_idx: int):
        """Returns a sendable channel for this nation's own pick feed — a
        thread off the shared results channel, created once and cached in
        state['divisions'][div]['thread_id']. With 4 nations free-running
        independently, posting every pick into one shared channel would
        interleave all 4 drafts together; a dedicated thread per nation
        keeps each one legible on its own. Falls back to the plain results
        channel (never None, unless that's unreachable too) if thread
        creation/lookup ever fails — e.g. the bot lacks "Create Public
        Threads", or the thread was deleted mid-draft — so a pick can
        never silently go unposted."""
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        thread_id = div_state.get('thread_id')

        results_channel = ctx.bot.get_channel(_RESULTS_CHANNEL_ID)
        if results_channel is None:
            try:
                results_channel = await ctx.bot.fetch_channel(_RESULTS_CHANNEL_ID)
            except Exception:
                log.warning("NationFlux | results channel %s not found/reachable", _RESULTS_CHANNEL_ID)
                return None

        if thread_id:
            thread = ctx.bot.get_channel(thread_id)
            if thread is None:
                try:
                    thread = await ctx.bot.fetch_channel(thread_id)
                except Exception:
                    thread = None
            if thread is not None:
                return thread
            log.warning("NationFlux | %s's thread %s is gone — recreating", _NATION_NAMES[div_idx], thread_id)

        try:
            thread = await results_channel.create_thread(
                name=f"{_NATION_NAMES[div_idx]} Draft", type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,
            )
        except Exception:
            log.exception("NationFlux | failed to create %s's thread — falling back to the results channel", _NATION_NAMES[div_idx])
            return results_channel

        div_state['thread_id'] = thread.id
        ctx.save_state(state)
        return thread

    @staticmethod
    async def _add_vote_reaction(ctx: DraftContext, proposal: dict, emoji: str) -> None:
        """Adds one more bot reaction to a proposal's thread message — the
        visible vote tally. The bot adds the reaction itself (on behalf of
        whichever GM just DMed !activate/!airreact) rather than reading
        reactions users click themselves, so the real vote count always
        stays in proposal['yes_votes'] and can't be spoofed or miscounted
        by someone reacting without actually voting."""
        thread_id = proposal.get('thread_id')
        message_id = proposal.get('message_id')
        if not thread_id or not message_id:
            return
        channel = ctx.bot.get_channel(thread_id)
        if channel is None:
            try:
                channel = await ctx.bot.fetch_channel(thread_id)
            except Exception:
                return
        try:
            message = await channel.fetch_message(message_id)
            await message.add_reaction(emoji)
        except Exception:
            log.warning("NationFlux | couldn't add a vote reaction to message %s", message_id)

    async def _open_pick_for_team(self, ctx: DraftContext, div_idx: int, team_idx: int, pick_number: int, force: bool = False) -> None:
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        team = div_state['teams'][team_idx]
        settings = self._settings(ctx)

        if not force and pick_number <= div_state.get('last_finalized_pick_number', 0):
            return

        entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
        if entry is None:
            entry = {'pick_number': pick_number, 'skipped': False}
            team['open_picks'].append(entry)
            ctx.save_state(state)

        if _timer.is_running((ctx.draft_id, div_idx, team_idx)) and team.get('timer_pick_number') == pick_number:
            return

        team['timer_pick_number'] = pick_number
        ctx.save_state(state)

        minutes = PickTimer.effective_minutes(
            settings['base_timer_minutes'], settings['decay_minutes'], settings['floor_minutes'], team.get('skip_count', 0),
        )
        deadline_ts = int(time.time() + minutes * 60)
        for mid in team['member_ids']:
            drafter = db.get_drafter(mid)
            await ctx.dm(
                drafter['user_id'],
                f"🏀 **{_NATION_NAMES[div_idx]} — Pick #{pick_number}** — you're on the clock!\n"
                f"Deadline: <t:{deadline_ts}:R>\n"
                f"DM me your pick: `!pick <player> [year]`",
            )

        self._start_team_timer(ctx, state, div_idx, team_idx, minutes)

    def _start_team_timer(self, ctx: DraftContext, state: dict, div_idx: int, team_idx: int, minutes: float) -> None:
        deadline = time.time() + minutes * 60
        state['divisions'][str(div_idx)]['teams'][team_idx]['timer_deadline'] = deadline
        ctx.save_state(state)

        async def _on_timeout():
            fresh_team = ctx.state['divisions'][str(div_idx)]['teams'][team_idx]
            pick_number = fresh_team.get('timer_pick_number')
            if pick_number is not None:
                await self._resolve_on_timeout(ctx, div_idx, team_idx, pick_number)

        _timer.start((ctx.draft_id, div_idx, team_idx), minutes, _on_timeout)

    async def _advance_live_pick(self, ctx: DraftContext, div_idx: int, pick_number: int) -> None:
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        settings = self._settings(ctx)
        n_teams = len(div_state['teams'])
        total_picks = n_teams * settings['rounds']
        next_pick_number = pick_number + 1
        if next_pick_number > total_picks:
            div_state['live_pick_number'] = None
            ctx.save_state(state)
            thread = await self._get_or_create_thread(ctx, div_idx)
            if thread:
                await thread.send(f"🏁 **{_NATION_NAMES[div_idx]}** has finished picking!")
            await self._maybe_complete_draft(ctx)
            return
        div_state['live_pick_number'] = next_pick_number
        ctx.save_state(state)
        next_team_idx = self._team_index_for_pick(next_pick_number, n_teams, settings['rounds'])
        await self._open_pick_for_team(ctx, div_idx, next_team_idx, next_pick_number)

    async def _maybe_complete_draft(self, ctx: DraftContext) -> None:
        state = ctx.state
        if all(state['divisions'][str(i)].get('live_pick_number') is None for i in range(4)):
            db.set_draft_status(ctx.draft_id, 'complete')
            await ctx.announce("🏁 **Nation Flux complete!** All 4 nations have finished picking.")

    async def _resolve_on_timeout(self, ctx: DraftContext, div_idx: int, team_idx: int, pick_number: int) -> None:
        async with draft_lock(ctx.draft_id):
            state = ctx.state
            div_state = state['divisions'][str(div_idx)]
            team = div_state['teams'][team_idx]
            entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
            if entry is None:
                return  # already resolved by an in-time submission

            if team.get('timer_pick_number') == pick_number:
                team['timer_pick_number'] = None
                team['timer_deadline'] = None

            entry['skipped'] = True
            team['skip_count'] = team.get('skip_count', 0) + 1
            was_live = pick_number == div_state.get('live_pick_number')
            ctx.save_state(state)
            thread = await self._get_or_create_thread(ctx, div_idx)
            skip_msg = f"⏭️ **Pick {pick_number} skipped** — {team['name']} didn't submit in time."
            if thread:
                await thread.send(skip_msg)
            else:
                await ctx.announce(f"⏭️ **{_NATION_NAMES[div_idx]} Pick {pick_number} skipped** — {team['name']} didn't submit in time.")
            for mid in team['member_ids']:
                mate = db.get_drafter(mid)
                await ctx.dm(
                    mate['user_id'],
                    f"⏰ Nobody on your team submitted pick #{pick_number} in time — skipped. "
                    f"DM me anytime with your pick and I'll record it as a makeup.",
                )
            if was_live:
                await self._advance_live_pick(ctx, div_idx, pick_number)

    # ── Pricing (read live from ATD Flux Bot's sheet, never typed by a GM) ──
    async def _price_snapshot(self, ctx: DraftContext) -> tuple[dict[str, list[float]], int]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, sheets.load_flux_price_history, FLUX_SPREADSHEET_ID, FLUX_WORKSHEET_GID)
        except Exception:
            log.exception("NationFlux | failed to read the Flux Bot price sheet")
            return {}, 0

    @staticmethod
    def _player_prices(history: dict[str, list[float]], player: str) -> list[float]:
        key = _normalize_name(player.strip().lower())
        for name, prices in history.items():
            if _normalize_name(name.lower()) == key:
                return prices
        return []

    @staticmethod
    def _power_active(state: dict, power_key: str, round_count: int) -> bool:
        p = state.get('powers', {}).get(power_key)
        return bool(p and p.get('used') and p.get('sheet_round_count') == round_count)

    def _compute_price(self, state: dict, div_idx: int, player: str, history: dict, round_count: int) -> tuple[float | None, str]:
        prices = self._player_prices(history, player)
        base_price = prices[-1] if prices else None
        fire_on = self._power_active(state, 'fire', round_count)
        water_on = self._power_active(state, 'water', round_count)
        air_target = state.get('air_reacted_to')

        if fire_on and div_idx != _FIRE:
            exempt = div_idx == _AIR and air_target == 'fire'
            if not exempt and len(prices) >= 2:
                return max(prices[-2:]), ' (Fire tax: high of last 2)'

        if div_idx == _WATER and water_on and len(prices) >= 2:
            return min(prices[-2:]), ' (Water discount: low of last 2)'

        if div_idx == _AIR and air_target == 'water' and water_on and len(prices) >= 3:
            return min(prices[-3:]), ' (Air enhanced discount: low of last 3)'

        return base_price, ''

    @staticmethod
    def _budget_bonus(state: dict, div_idx: int, round_count: int) -> float:
        earth = state.get('powers', {}).get('earth', {})
        earth_on = bool(earth.get('used') and earth.get('sheet_round_count') == round_count)
        bonus = 0.0
        if earth_on and div_idx == _EARTH:
            bonus += earth.get('roll', 0)
        if earth_on and div_idx == _AIR and state.get('air_reacted_to') == 'earth':
            bonus += 1
        return bonus

    # ── Pick submission ───────────────────────────────────────────────────────
    @staticmethod
    def _parse_submission(text: str) -> tuple[str, str] | None:
        parts = text.strip().split()
        if not parts:
            return None
        if len(parts) >= 2 and _YEAR_TOKEN_RE.match(parts[-1]):
            return ' '.join(parts[:-1]).strip(), parts[-1].lstrip("'")
        player = ' '.join(parts).strip()
        return (player, '') if player else None

    def _already_taken(self, ctx: DraftContext, div_idx: int, player: str) -> str | None:
        """Scoped to THIS nation only — each nation drafts from its own
        independent pool, so the same player can be drafted once per
        nation without conflict."""
        key = _normalize_name(player.strip().lower())
        div_state = ctx.state['divisions'][str(div_idx)]
        for team in div_state['teams']:
            if not team['member_ids']:
                continue
            picks = db.get_picks(ctx.draft_id, team['member_ids'][0])
            if any(_normalize_name(p.strip().lower()) == key for p in picks):
                return team['name']
        return None

    async def on_pick(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            div_idx = drafter.get('division')
            if div_idx is None:
                return "You're not assigned to a nation yet."
            team_idx = drafter['state'].get('team_index')
            if team_idx is None:
                return "You're not assigned to a team yet."

            state = ctx.state
            div_state = state['divisions'][str(div_idx)]
            team = div_state['teams'][team_idx]
            if not team['open_picks']:
                return "Your team isn't on the clock right now."

            entry = team['open_picks'][0]
            parsed = self._parse_submission(text)
            if parsed is None:
                return "Usage: `!pick <player> [year]`"
            player, year = parsed

            taken_by = self._already_taken(ctx, div_idx, player)
            if taken_by:
                return f"❌ **{player}** has already been drafted by {taken_by}. Pick again."

            history, round_count = await self._price_snapshot(ctx)
            price, price_note = self._compute_price(state, div_idx, player, history, round_count)
            if price is None:
                return f"❌ **{player}** isn't in the price pool — check the spelling."

            settings = self._settings(ctx)
            budget = settings['budget'] + self._budget_bonus(state, div_idx, round_count)
            if team.get('budget_spent', 0.0) + price > budget:
                remaining = budget - team.get('budget_spent', 0.0)
                return f"❌ That's ${price:.0f}, but your team only has ${remaining:.0f} left of your ${budget:.0f} budget."

            await self._finalize_pick(ctx, div_idx, team_idx, entry, player, year, price, price_note)
            return ''

    async def _finalize_pick(self, ctx: DraftContext, div_idx: int, team_idx: int, entry: dict,
                              player: str, year: str, price: float, price_note: str) -> None:
        state = ctx.state
        div_state = state['divisions'][str(div_idx)]
        team = div_state['teams'][team_idx]
        pick_number = entry['pick_number']
        div_state['last_finalized_pick_number'] = max(div_state.get('last_finalized_pick_number', 0), pick_number)

        for mid in team['member_ids']:
            db.record_pick(ctx.draft_id, mid, player, round_number=None, pick_number=pick_number, auto_pick=False)

        team['budget_spent'] = team.get('budget_spent', 0.0) + price
        team['open_picks'] = [p for p in team['open_picks'] if p['pick_number'] != pick_number]
        if team.get('timer_pick_number') == pick_number:
            team['timer_pick_number'] = None
            team['timer_deadline'] = None
        was_live = pick_number == div_state.get('live_pick_number')
        ctx.save_state(state)

        _timer.cancel((ctx.draft_id, div_idx, team_idx))

        year_part = f" '{year}" if year else ''
        price_part = f" ${price:.0f}{price_note}"

        thread = await self._get_or_create_thread(ctx, div_idx)
        if thread:
            await thread.send(f"{team['name']} picks **{player}**{year_part}{price_part}")
        else:
            log.warning("NationFlux | no thread or results channel available for %s", _NATION_NAMES[div_idx])

        for mid in team['member_ids']:
            drafter = db.get_drafter(mid)
            await ctx.dm(drafter['user_id'], f"✅ Your team's pick: **{player}{year_part}{price_part}**")

        if was_live:
            await self._advance_live_pick(ctx, div_idx, pick_number)

    # ── Powers: activation vote (Earth/Fire/Water) ───────────────────────────
    @staticmethod
    def _votes_needed(div_state: dict) -> int:
        total_gms = sum(len(t['member_ids']) for t in div_state['teams'])
        return total_gms // 2 + 1

    async def propose_power(self, ctx: DraftContext, drafter: dict) -> str:
        """Backs `!activate`. The FIRST GM to DM this for their nation opens
        the proposal — posts a message in the nation's own thread and adds
        the nation's emoji as the first reaction. Every GM after that who
        DMs `!activate` just adds their own vote: one more bot reaction on
        that same message, immediately, no time limit. The instant enough
        GMs have voted (majority of the nation), the power fires."""
        async with draft_lock(ctx.draft_id):
            div_idx = drafter.get('division')
            if div_idx is None:
                return "You're not assigned to a nation yet."
            if div_idx == _AIR:
                return "Air doesn't have an independent power — use `!airreact` while another nation's vote is open."

            power_key = _NATION_KEYS[div_idx]
            state = ctx.state
            powers = state.setdefault('powers', {})
            power_state = powers.get(power_key, {'used': False, 'sheet_round_count': None})
            if power_state.get('used'):
                return f"❌ {_NATION_NAMES[div_idx]}'s power has already been used this draft."

            if power_key in ('fire', 'water'):
                _history, round_count = await self._price_snapshot(ctx)
                if round_count < 2:
                    return "❌ Can't activate yet — Flux Bot hasn't generated a Round 2 price column on the sheet yet."

            proposals = state.setdefault('proposals', {})
            proposal = proposals.get(str(div_idx))
            div_state = state['divisions'][str(div_idx)]
            needed = self._votes_needed(div_state)
            emoji = _NATION_EMOJI[div_idx]

            if proposal is None:
                thread = await self._get_or_create_thread(ctx, div_idx)
                msg_text = (
                    f"{emoji} **{drafter['display_name']}** wants to activate {_NATION_NAMES[div_idx]}'s power!\n"
                    f"Other {_NATION_NAMES[div_idx]} GMs: DM me `!activate` too — needs {needed} total ({emoji} 1/{needed} so far)."
                )
                message = await thread.send(msg_text) if thread else await ctx.announce(msg_text)
                if message:
                    try:
                        await message.add_reaction(emoji)
                    except Exception:
                        log.warning("NationFlux | couldn't add initial reaction for %s's proposal", _NATION_NAMES[div_idx])
                proposals[str(div_idx)] = {
                    'power': power_key, 'proposed_by': drafter['id'], 'yes_votes': [drafter['id']],
                    'thread_id': thread.id if thread else None, 'message_id': message.id if message else None,
                }
                ctx.save_state(state)
                return f"✅ Proposed activating {_NATION_NAMES[div_idx]}'s power — posted in your nation's thread. Needs {needed} total (1/{needed} so far)."

            if drafter['id'] in proposal['yes_votes']:
                return "You've already voted for this."
            proposal['yes_votes'].append(drafter['id'])
            ctx.save_state(state)
            await self._add_vote_reaction(ctx, proposal, emoji)

            if len(proposal['yes_votes']) >= needed:
                return await self._activate_power(ctx, div_idx)
            return f"✅ Vote counted ({len(proposal['yes_votes'])}/{needed})."

    async def _activate_power(self, ctx: DraftContext, div_idx: int) -> str:
        state = ctx.state
        power_key = _NATION_KEYS[div_idx]
        state.get('proposals', {}).pop(str(div_idx), None)
        _history, round_count = await self._price_snapshot(ctx)
        powers = state.setdefault('powers', {})
        entry = {'used': True, 'sheet_round_count': round_count}
        if power_key == 'earth':
            entry['roll'] = random.randint(1, 5)
        powers[power_key] = entry
        ctx.save_state(state)

        if power_key == 'earth':
            msg = f"🌍 **Earth's power activated!** Every Earth team's budget is up ${entry['roll']} until the next flux."
        elif power_key == 'fire':
            msg = "🔥 **Fire's power activated!** Every OTHER nation now pays the higher of a player's last 2 prices, until the next flux. Fire itself is unaffected."
        else:
            msg = "🌊 **Water's power activated!** Water's own picks are now charged the lower of a player's last 2 prices, until the next flux."

        thread = await self._get_or_create_thread(ctx, div_idx)
        if thread:
            await thread.send(f"✅ **ACTIVATED** — {msg}")
        await ctx.announce(msg)
        for div in range(4):
            for team in state['divisions'][str(div)]['teams']:
                for mid in team['member_ids']:
                    d = db.get_drafter(mid)
                    await ctx.dm(d['user_id'], msg)
        return msg

    # ── Air's reaction ────────────────────────────────────────────────────────
    async def air_react(self, ctx: DraftContext, drafter: dict) -> str:
        """Backs `!airreact` — same propose-or-vote pattern as `!activate`,
        scoped to Air's own thread/nation. Only startable while another
        nation's own proposal is still open (not yet activated); once
        started, subsequent votes don't re-check that window, so a reaction
        already in progress can still finish even if the target's own vote
        resolves first."""
        async with draft_lock(ctx.draft_id):
            div_idx = drafter.get('division')
            if div_idx != _AIR:
                return "Only Air can use `!airreact`."

            state = ctx.state
            if state.get('air_reacted_to'):
                return "❌ Air's power has already been used this draft."

            reaction = state.get('air_reaction')
            div_state = state['divisions'][str(_AIR)]
            needed = self._votes_needed(div_state)
            emoji = _NATION_EMOJI[_AIR]

            if reaction is None:
                proposals = state.get('proposals', {})
                target_div = next((int(k) for k in proposals if int(k) != _AIR), None)
                if target_div is None:
                    return "❌ Air can only react while another nation's activation vote is open — nothing is open right now."
                target_power = proposals[str(target_div)]['power']

                if target_power == 'water':
                    _history, round_count = await self._price_snapshot(ctx)
                    if round_count < 3:
                        return "❌ Air can't react to Water yet — that reaction needs a Round 3 price column on the sheet."

                thread = await self._get_or_create_thread(ctx, _AIR)
                msg_text = (
                    f"{emoji} **{drafter['display_name']}** wants Air to react to {_NATION_NAMES[target_div]}'s power!\n"
                    f"Other Air GMs: DM me `!airreact` too — needs {needed} total ({emoji} 1/{needed} so far)."
                )
                message = await thread.send(msg_text) if thread else await ctx.announce(msg_text)
                if message:
                    try:
                        await message.add_reaction(emoji)
                    except Exception:
                        log.warning("NationFlux | couldn't add initial reaction for Air's reaction proposal")
                state['air_reaction'] = {
                    'target_division': target_div, 'target_power': target_power,
                    'proposed_by': drafter['id'], 'yes_votes': [drafter['id']],
                    'thread_id': thread.id if thread else None, 'message_id': message.id if message else None,
                }
                ctx.save_state(state)
                return f"✅ Proposed Air's reaction to {_NATION_NAMES[target_div]} — posted in Air's thread. Needs {needed} total (1/{needed} so far)."

            if drafter['id'] in reaction['yes_votes']:
                return "You've already voted for this."
            reaction['yes_votes'].append(drafter['id'])
            ctx.save_state(state)
            await self._add_vote_reaction(ctx, reaction, emoji)

            if len(reaction['yes_votes']) >= needed:
                return await self._finalize_air_reaction(ctx)
            return f"✅ Vote counted ({len(reaction['yes_votes'])}/{needed})."

    async def _finalize_air_reaction(self, ctx: DraftContext) -> str:
        state = ctx.state
        reaction = state.get('air_reaction')
        if not reaction:
            return ''
        target_power = reaction['target_power']
        state['air_reaction'] = None
        state['air_reacted_to'] = target_power
        ctx.save_state(state)

        if target_power == 'fire':
            msg = "💨 **Air reacted!** Air's own nation is now exempt from Fire's price tax."
        elif target_power == 'water':
            msg = "💨 **Air reacted!** Air's own nation now gets an enhanced discount — the lower of a player's last 3 prices."
        else:
            msg = "💨 **Air reacted!** Air's own nation gets a +$1 budget bump."

        thread = await self._get_or_create_thread(ctx, _AIR)
        if thread:
            await thread.send(f"✅ **ACTIVATED** — {msg}")
        await ctx.announce(msg)
        div_state = state['divisions'][str(_AIR)]
        for team in div_state['teams']:
            for mid in team['member_ids']:
                d = db.get_drafter(mid)
                await ctx.dm(d['user_id'], msg)
        return msg

    # ── Status / misc ────────────────────────────────────────────────────────
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        div_idx = drafter.get('division')
        if div_idx is None:
            return "You're not assigned to a nation yet."
        team_idx = drafter['state'].get('team_index')
        team = ctx.state['divisions'][str(div_idx)]['teams'][team_idx]
        settings = self._settings(ctx)
        picks = len(db.get_picks(ctx.draft_id, drafter['id']))
        return (
            f"Nation: {_NATION_NAMES[div_idx]}. Team: {team['name']}. "
            f"Picks so far: {picks}/{settings['rounds']}. Open pick(s) owed: {len(team['open_picks'])}. "
            f"Budget spent: ${team.get('budget_spent', 0):.0f} / ${settings['budget']:.0f}"
        )

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        # Timeouts are team-scoped (see _resolve_on_timeout), not per-drafter.
        return

    def is_pick_phase_complete(self, ctx: DraftContext, drafter: dict | None = None) -> bool:
        settings = self._settings(ctx)
        state = ctx.state
        divisions = state.get('divisions', {})
        if not divisions:
            return False
        if drafter is not None and drafter.get('division') is not None:
            div_state = divisions.get(str(drafter['division']))
            if not div_state:
                return False
            return all(
                len(db.get_picks(ctx.draft_id, mid)) >= settings['rounds']
                for team in div_state['teams'] for mid in team['member_ids']
            )
        return all(
            len(db.get_picks(ctx.draft_id, mid)) >= settings['rounds']
            for div in divisions.values() for team in div['teams'] for mid in team['member_ids']
        )

    # ── Team grouping ─────────────────────────────────────────────────────────
    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        div = drafter.get('division')
        idx = drafter['state'].get('team_index')
        if div is None or idx is None:
            return str(drafter['id'])
        return f'nation-{div}-team-{idx}'

    def team_label(self, ctx: DraftContext, drafter: dict) -> str:
        div = drafter.get('division')
        idx = drafter['state'].get('team_index')
        if div is None or idx is None:
            return drafter['display_name'] or str(drafter['user_id'])
        team = ctx.state['divisions'][str(div)]['teams'][idx]
        return f"{_NATION_NAMES[div]} — {team['name']}"

    def linked_drafter_ids(self, ctx: DraftContext, drafter: dict) -> list[int]:
        div = drafter.get('division')
        idx = drafter['state'].get('team_index')
        if div is None or idx is None:
            return [drafter['id']]
        return list(ctx.state['divisions'][str(div)]['teams'][idx]['member_ids'])

    # ── Timer lifecycle (restart-safety, overnight auto-pause) ─────────────────
    async def cancel(self, ctx: DraftContext) -> None:
        state = ctx.state
        for div_idx in range(4):
            div_state = state.get('divisions', {}).get(str(div_idx))
            if not div_state:
                continue
            for team_idx, team in enumerate(div_state['teams']):
                team['timer_deadline'] = None
                _timer.cancel((ctx.draft_id, div_idx, team_idx))
        ctx.save_state(state)

    async def resume_timers(self, ctx: DraftContext) -> int:
        settings = self._settings(ctx)
        state = ctx.state
        resumed = 0
        for div_idx in range(4):
            div_state = state.get('divisions', {}).get(str(div_idx))
            if not div_state:
                continue
            for team_idx, team in enumerate(div_state['teams']):
                pick_number = team.get('timer_pick_number')
                if pick_number is None or _timer.is_running((ctx.draft_id, div_idx, team_idx)):
                    continue

                # No "reconnected" DM to every GM here on purpose — this
                # fires on every gateway reconnect AND every process
                # restart (a routine deploy included), not just a genuine
                # extended outage, so it was spamming everyone far more
                # often than it was actually useful. The timer itself
                # still re-arms (or resolves/auto-picks immediately if the
                # deadline already passed) exactly as before — only the
                # notification, and the DM-success gate on starting the
                # timer, are gone.
                deadline = team.get('timer_deadline')
                if deadline is None:
                    minutes = PickTimer.effective_minutes(
                        settings['base_timer_minutes'], settings['decay_minutes'], settings['floor_minutes'], team.get('skip_count', 0),
                    )
                    self._start_team_timer(ctx, ctx.state, div_idx, team_idx, minutes)
                    resumed += 1
                    continue

                remaining = deadline - time.time()
                if remaining <= 0:
                    await self._resolve_on_timeout(ctx, div_idx, team_idx, pick_number)
                else:
                    minutes = remaining / 60
                    self._start_team_timer(ctx, ctx.state, div_idx, team_idx, minutes)
                resumed += 1
        return resumed
