# themes/anon_draft.py
#
# Anonymous Draft: ~30-32 GMs, grouped into teams of 1-3 owners each, each
# team assigned a logo nobody but the commissioner (and this bot) can trace
# back to real people. Turn order runs entirely inside this bot — no
# external turn-order bot involved. This bot:
#   1. Drives its own precomputed snake order (_team_index_for_pick):
#      _advance_live_pick opens the next pick the instant the current one
#      resolves (finalized or skipped), with its own decaying per-team
#      timer (_open_pick_for_team/_start_team_timer) — same base/decay/
#      floor shape Pack Draft uses, just applied per team here.
#   2. Collects each GM's DM'd pick (`!pick <player> <year>`) — on a multi-
#      member team, whoever submits first becomes the proposer and every
#      other member gets an agree (`1`)-or-counter (`2`) prompt; countering
#      hands the proposer role to them instead, ping-ponging until someone
#      agrees or a 10-minute response window lapses (silence = agreement),
#      capped by the original pick deadline.
#   3. Posts each finalized pick as "<logo> picks <Player> [year] [price]"
#      into a single fixed results channel (_RESULTS_CHANNEL_ID) — the only
#      public record of a pick; there's no other downstream bot integration.
#
# Picks are still recorded in this bot's own DB for `!mypicks`, but only GM
# IDENTITY is ever hidden here — the pick itself is public the moment it's
# made, unlike Pack Draft/Partner Draft's hidden-until-reveal model.

import logging
import math
import re
import time
import discord

import db
import lotto
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock
from .player_years import get_default_year

log = logging.getLogger(__name__)

_timer = PickTimer()  # keyed by (draft_id, team_index) — one clock per team, not per drafter
_proposal_timer = PickTimer()  # same keying — up to 10 minutes, within the original pick clock
_PROPOSAL_RESPONSE_MINUTES = 10.0

# This bot now runs turn order itself instead of watching ATD Timer Bot's
# pings (see _open_pick_for_team/_advance_live_pick) — every finalized pick
# is announced here as a plain "<logo> picks <Player> [year] [price]" line,
# not the "N. <logo> Player 'YY $price" format ATD Draft List Bot/Sheet
# Bot/Team Sheet Bot used to auto-parse. Those integrations are gone; this
# channel is now the only public record of a pick.
_RESULTS_CHANNEL_ID = 934052208624930848

_YEAR_TOKEN_RE = re.compile(r"^'?\d{2,4}(-\d{2,4})?$")

# Apostrophe-lookalike normalization for _already_taken's duplicate-pick
# check — a curly apostrophe from one GM's phone and a straight one from
# another's otherwise silently pass the "already drafted" check as
# different strings, letting the same real player get drafted twice.
_APOSTROPHE_VARIANTS = str.maketrans({
    '‘': "'", '’': "'", 'ʼ': "'", '´': "'", '`': "'",
})


def _normalize_name(text: str) -> str:
    return text.translate(_APOSTROPHE_VARIANTS)


def _display_year(year: str) -> str:
    """Render as the 2-digit "'YY" suffix ATD Draft List Bot's convention
    (and Team Sheet Bot's parser) expects. A GM typing the natural 4-digit
    year ("!pick Michael Jordan 1990") otherwise produces "Player '1990" —
    Team Sheet Bot's year regex only consumes the apostrophe together with a
    2-digit year, so a 4-digit one leaves the "'" stuck on the player name
    in the sheet cell instead of being stripped into the year column."""
    digits = year.split('-')[-1]
    return digits[-2:] if len(digits) > 2 else digits


def _year_suffix(year: str) -> str:
    return f" '{_display_year(year)}" if year else ''

# Team Sheet Bot treats any message starting with "N." or containing a raw
# custom-emoji token as a pick attempt (its `looks_like_pick` check) and
# reacts with a ❌ if it can't resolve a position for the "player" — a skip
# notice using the same "N. <logo> ..." shape as a real pick line trips that
# false positive. _safe_logo_text strips a custom-emoji token down to its
# plain name for non-pick announcements; unicode emoji logos pass through
# untouched since Team Sheet Bot's pick-attempt check never looks at those.
_CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')


def _safe_logo_text(logo: str) -> str:
    m = _CUSTOM_EMOJI_RE.search(logo)
    return f':{m.group(1)}:' if m else logo


class AnonDraftTheme(Theme):
    key = 'anon_draft'
    display_name = 'Anonymous Draft'

    def default_settings(self) -> dict:
        return {
            'rounds': 10,
            'require_price': False,   # the Anon Budget subclass flips this on
            'budget': None,           # set by the Anon Budget subclass
            # Same decaying-timer shape Pack Draft uses, applied per team
            # instead of per seat — each skip knocks decay_minutes off the
            # team's next clock, down to floor_minutes. floor_minutes=0 means
            # a team that keeps skipping eventually gets skipped immediately.
            'base_timer_minutes': 60,
            'decay_minutes': 10,
            'floor_minutes': 0,
        }

    def _settings(self, ctx: DraftContext) -> dict:
        return {**self.default_settings(), **ctx.settings}

    # ── Setup (DM-only, owner-only — this is where the secret mapping lives) ───
    async def setup_teams(self, ctx: DraftContext, text: str) -> str:
        parsed = lotto.parse_team_lines(text)
        if not parsed:
            return "❌ No valid team lines found. Format: `1. 🦅 - @GM1 @GM2`, one team per line."

        seen_users: set[int] = set()
        for entry in parsed:
            if not (1 <= len(entry['user_ids']) <= 3):
                return f"❌ Team on line {entry['order_index'] + 1} has {len(entry['user_ids'])} member(s) — must be 1-3."
            for uid in entry['user_ids']:
                if uid in seen_users:
                    return f"❌ <@{uid}> is listed on more than one team."
                seen_users.add(uid)

        teams = []
        for entry in parsed:
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
                member_ids.append(db.add_drafter(ctx.draft_id, uid, display_name))

            teams.append({
                'index': len(teams),
                'logo': entry['logo'],
                'member_ids': member_ids,
                'budget_spent': 0.0,
                'open_picks': [],       # [{'pick_number': int, 'skipped': bool, 'proposal': {...}|None, 'pending_from': [drafter_id]}]
                'timer_pick_number': None,
                'timer_deadline': None,
                'skip_count': 0,
            })

        for team in teams:
            for mid in team['member_ids']:
                db.update_drafter_state(mid, {'team_index': team['index']})

        state = ctx.state
        state['teams'] = teams
        ctx.save_state(state)

        total_gms = sum(len(t['member_ids']) for t in teams)
        return (
            f"✅ Registered {len(teams)} teams ({total_gms} GMs total). "
            f"Run `!theme start {ctx.draft_id}` to open pick #1."
        )

    async def start(self, ctx: DraftContext) -> str:
        teams = ctx.state.get('teams', [])
        if not teams:
            raise ValueError("No teams registered — DM me `!setteams` first.")
        settings = self._settings(ctx)
        state = ctx.state
        state['last_finalized_pick_number'] = 0
        state['live_pick_number'] = 1
        ctx.save_state(state)
        for lotto_number, team in enumerate(teams, 1):
            for member_id in team['member_ids']:
                member = db.get_drafter(member_id)
                await ctx.dm(member['user_id'],
                             f"**{ctx.draft['name']}**\nYour team: {team['logo']}\n"
                             f"Lotto number: **{lotto_number}** \u00b7 Round 1 pick **#{lotto_number}**")
        await self._open_pick_for_team(ctx, 0, 1)
        return f"Anonymous Draft started — {len(teams)} teams, {settings['rounds']} rounds. Pick #1 is now open."

    # ── Turn order (self-driven — no external bot involved) ─────────────────
    async def override_pick(self, ctx: DraftContext, pick_number: int, team_ref: str) -> str:
        async with draft_lock(ctx.draft_id):
            state = ctx.state
            teams = state.get('teams', [])
            team_idx = self._resolve_team_ref(teams, team_ref)
            if team_idx is None:
                return f"❌ Couldn't find a team matching '{team_ref}' (use its number or exact logo)."
            state['live_pick_number'] = pick_number
            ctx.save_state(state)
            # force=True — this is the commish's explicit safety valve for
            # exactly the case the guard below exists to prevent accidental
            # duplicates of; an intentional manual override should win.
            await self._open_pick_for_team(ctx, team_idx, pick_number, force=True)
            return f"✅ Manually opened pick #{pick_number} for team {teams[team_idx]['logo']}."

    async def _open_pick_for_team(self, ctx: DraftContext, team_idx: int, pick_number: int, force: bool = False) -> None:
        state = ctx.state
        team = state['teams'][team_idx]
        settings = self._settings(ctx)
        from flux_gate import hold_for_round
        if hold_for_round(ctx, state, (pick_number - 1) // len(state['teams']) + 1):
            return

        if not force and pick_number <= state.get('last_finalized_pick_number', 0):
            # Defensive: don't reopen a pick that's already been finalized.
            return

        entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
        if entry is not None and entry.get('skipped'):
            if state.get('live_pick_number') == pick_number:
                await self._advance_live_pick(ctx, pick_number)
            return
        is_new = entry is None
        if is_new:
            entry = {'pick_number': pick_number, 'skipped': False, 'proposal': None, 'pending_from': []}
            team['open_picks'].append(entry)
            ctx.save_state(state)

            if any(p.get('skipped') and p['pick_number'] < pick_number for p in team['open_picks']):
                await self._skip_locked(ctx, team_idx, entry, 'An earlier skipped pick is still owed, so this turn was automatically skipped too.')
                return

            if await self._try_consume_queued_pick(ctx, state, team_idx, entry):
                return  # a teammate's standing pick already fired — nothing else to do

        # Already have a running timer for this exact pick (e.g. this got
        # called twice in a row somehow) — don't restart it or re-notify.
        if _timer.is_running((ctx.draft_id, team_idx)) and team.get('timer_pick_number') == pick_number:
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
                f"🏀 **Pick #{pick_number} — you're on the clock!**\n"
                f"Your team: {team['logo']}\n"
                + self._budget_note(ctx, team) +
                f"Deadline: <t:{deadline_ts}:R>\n"
                f"DM me your pick: `{self._submission_format(ctx)}`",
            )

        self._start_team_timer(ctx, state, team_idx, minutes)

    def _start_team_timer(self, ctx: DraftContext, state: dict, team_idx: int, minutes: float) -> None:
        # Persisted as a real wall-clock deadline, not just an in-memory
        # asyncio delay, so resume_timers() can work out genuine remaining
        # time instead of granting a fresh full clock after a restart.
        deadline = time.time() + minutes * 60
        state['teams'][team_idx]['timer_deadline'] = deadline
        ctx.save_state(state)

        if state.get('timer_channel_id'):
            _timer.cancel((ctx.draft_id, team_idx))
            return

        async def _on_timeout():
            # Re-fetch fresh rather than capturing pick_number now — an
            # override could reopen a different pick for this team between
            # scheduling and firing.
            fresh_team = ctx.state['teams'][team_idx]
            pick_number = fresh_team.get('timer_pick_number')
            if pick_number is not None:
                await self._resolve_on_timeout(ctx, team_idx, pick_number)

        _timer.start((ctx.draft_id, team_idx), minutes, _on_timeout)

    async def _advance_live_pick(self, ctx: DraftContext, pick_number: int) -> None:
        """Called once the pick_number that was the current live turn
        resolves (finalized OR skipped) — moves the snake sequence on to
        the next pick regardless of which. A later makeup pick for an old
        skipped slot must NOT trigger this — callers only invoke it when
        pick_number was the draft's live_pick_number at the time."""
        state = ctx.state
        teams = state.get('teams', [])
        settings = self._settings(ctx)
        n_teams = len(teams)
        total_picks = n_teams * settings['rounds']
        next_pick_number = pick_number + 1
        if next_pick_number > total_picks:
            state['live_pick_number'] = None
            state['scheduled_turns_finished'] = True
            ctx.save_state(state)
            if self.is_pick_phase_complete(ctx):
                db.set_draft_status(ctx.draft_id, 'complete')
                await ctx.announce(f"🏁 **Anonymous Draft complete!** All {total_picks} picks made.")
            else:
                await ctx.announce('Scheduled turns finished. Outstanding makeup picks are still open.')
            return
        state['live_pick_number'] = next_pick_number
        ctx.save_state(state)
        next_team_idx = self._team_index_for_pick(next_pick_number, n_teams, settings['rounds'])
        await self._open_pick_for_team(ctx, next_team_idx, next_pick_number)

    @staticmethod
    def _team_index_for_pick(pick_number: int, n_teams: int, rounds: int) -> int | None:
        """Standard snake order: round 0 forward, round 1 reversed, etc.
        Assumption, not verified against Timer Bot's own snake internals —
        `!anonoverride` exists specifically in case this ever drifts."""
        if n_teams <= 0 or not (1 <= pick_number <= n_teams * rounds):
            return None
        idx0 = pick_number - 1
        round_idx = idx0 // n_teams
        pos = idx0 % n_teams
        return pos if round_idx % 2 == 0 else n_teams - 1 - pos

    @staticmethod
    def _resolve_team_ref(teams: list[dict], ref: str) -> int | None:
        ref = ref.strip()
        if ref.isdigit():
            idx = int(ref) - 1
            return idx if 0 <= idx < len(teams) else None
        for t in teams:
            if t['logo'] == ref:
                return t['index']
        return None

    # ── Pick submission ──────────────────────────────────────────────────────
    # Multi-member teams no longer resolve by having everyone submit blind and
    # majority/priority breaking ties — a pick now goes out to the channel
    # only once every teammate has actively agreed to it (or stayed silent
    # past the response window, which counts as agreeing). Whoever submits
    # first becomes the proposer; every other member gets an agree-or-counter
    # prompt, and countering hands the proposer role to them instead — this
    # can ping-pong back and forth until someone agrees or the response timer
    # lapses.
    async def on_pick(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            team_idx = drafter['state'].get('team_index')
            if team_idx is None:
                return "You're not assigned to a team yet."

            state = ctx.state
            team = state['teams'][team_idx]
            if state.get('flux_wait') and not any(p.get('skipped') for p in team['open_picks']):
                return 'Waiting for a commissioner to complete Flux before opening the next round. Only owed makeup picks can be submitted now.'
            if not team['open_picks']:
                # Anonymous Draft hides GM identity, so a GM who'll be away
                # for their actual turn can't just ask a friend to pick for
                # them — instead they can leave a standing pick with the bot
                # now, and it fires automatically the moment the team is
                # actually on the clock (still going through the normal
                # teammate agree-or-counter flow on a multi-member team).
                return await self._queue_pick(ctx, team, drafter, text)

            entry = team['open_picks'][0]  # oldest owed pick first
            is_makeup = entry['skipped']
            stripped = text.strip()

            if is_makeup:
                # A skipped pick resolves the instant anyone on the team
                # submits — no consensus needed, the live window already
                # passed and getting back on track matters more.
                parsed = self._parse_submission(ctx, text)
                if parsed is None:
                    return self._submission_usage(ctx)
                player, year, price = parsed
                taken_by = self._already_taken(ctx, player)
                if taken_by:
                    return f"❌ **{player}** has already been drafted by {taken_by}. Pick again."
                budget_error = self._check_budget(ctx, team, price)
                if budget_error:
                    return budget_error
                # _finalize_pick posts the public announcement and DMs a
                # private confirmation to every teammate — nothing else
                # needed here.
                await self._finalize_pick(ctx, team_idx, entry, player, year, price)
                return ''

            if len(team['member_ids']) == 1:
                # Solo team — nobody to consult.
                parsed = self._parse_submission(ctx, text)
                if parsed is None:
                    return self._submission_usage(ctx)
                player, year, price = parsed
                taken_by = self._already_taken(ctx, player)
                if taken_by:
                    return f"❌ **{player}** has already been drafted by {taken_by}. Pick again."
                budget_error = self._check_budget(ctx, team, price)
                if budget_error:
                    return budget_error
                await self._finalize_pick(ctx, team_idx, entry, player, year, price)
                return ''

            proposal = entry.get('proposal')
            pending_from = entry.get('pending_from', [])

            # A bare "1"/"2" only means anything if this drafter currently
            # owes a response to an active proposal.
            if stripped in ('1', '2') and proposal is not None and drafter['id'] in pending_from:
                if stripped == '1':
                    return await self._handle_agree(ctx, state, team_idx, entry, drafter)
                return await self._handle_reject(ctx, state, team_idx, entry, drafter)

            parsed = self._parse_submission(ctx, text)
            if parsed is None:
                if proposal is not None and drafter['id'] in pending_from:
                    return "Reply `1` to agree, or `2` to pick someone else instead."
                return self._submission_usage(ctx)
            player, year, price = parsed
            taken_by = self._already_taken(ctx, player)
            if taken_by:
                return f"❌ **{player}** has already been drafted by {taken_by}. Pick again."
            budget_error = self._check_budget(ctx, team, price)
            if budget_error:
                return budget_error

            return await self._propose(ctx, state, team_idx, entry, drafter, player, year, price)

    async def _propose(self, ctx: DraftContext, state: dict, team_idx: int, entry: dict, drafter: dict,
                        player: str, year: str, price: float | None) -> str:
        year = year or get_default_year(player) or ''
        # state/entry are exactly what on_pick already fetched via ctx.state —
        # deliberately NOT re-fetched here. ctx.state re-reads straight from
        # the DB, so a fresh fetch would be a *different* object graph than
        # entry; mutating entry and then ctx.save_state()-ing a separately
        # fetched state silently drops the mutation (this used to be exactly
        # that bug — proposals/agreements never actually persisted).
        team = state['teams'][team_idx]
        old_proposal = entry.get('proposal')

        if old_proposal and old_proposal['by'] != drafter['id']:
            # Submitting directly (instead of replying `2` first) still counts
            # as countering whatever was on the table.
            old_proposer = db.get_drafter(old_proposal['by'])
            if old_proposer:
                await ctx.dm(
                    old_proposer['user_id'],
                    f"↩️ **{drafter['display_name']}** picked someone else instead of "
                    f"**{old_proposal['player']}{_year_suffix(old_proposal['year'])}**.",
                )

        other_ids = [mid for mid in team['member_ids'] if mid != drafter['id']]
        entry['proposal'] = {'player': player, 'year': year, 'price': price, 'by': drafter['id']}
        entry['pending_from'] = list(other_ids)
        ctx.save_state(state)

        price_note = f' at ${price:g}' if price is not None else ''
        for mid in other_ids:
            mate = db.get_drafter(mid)
            await ctx.dm(
                mate['user_id'],
                f"📝 **{drafter['display_name']}** proposed **{player}{_year_suffix(year)}**{price_note} for pick #{entry['pick_number']}.\n"
                f"Reply `1` to agree, or `2` to pick someone else instead.\n"
                f"You have up to {_PROPOSAL_RESPONSE_MINUTES:.0f} minutes to respond. Your original pick clock keeps running. "
                f"If either timer expires first, this proposal is automatically submitted.",
            )

        async def _on_proposal_timeout():
            await self._resolve_proposal_timeout(ctx, team_idx, entry['pick_number'])

        response_minutes = _PROPOSAL_RESPONSE_MINUTES
        if team.get('timer_deadline') is not None:
            response_minutes = min(response_minutes, max(0, team['timer_deadline']-time.time()) / 60)
        _proposal_timer.start((ctx.draft_id, team_idx), response_minutes, _on_proposal_timeout)

        return (f"✅ Proposed **{player}{_year_suffix(year)}**{price_note}. "
                f"Waiting for your teammate to respond (up to {_PROPOSAL_RESPONSE_MINUTES:.0f} min). "
                "Your original pick clock keeps running; the proposal is automatically submitted when either timer expires.")

    async def _handle_agree(self, ctx: DraftContext, state: dict, team_idx: int, entry: dict, drafter: dict) -> str:
        entry['pending_from'] = [mid for mid in entry['pending_from'] if mid != drafter['id']]
        ctx.save_state(state)

        if entry['pending_from']:
            return "✅ You agreed. Waiting on the rest of your team."

        proposal = entry['proposal']
        _proposal_timer.cancel((ctx.draft_id, team_idx))
        await self._finalize_pick(ctx, team_idx, entry, proposal['player'], proposal['year'], proposal['price'])
        return ''

    async def _handle_reject(self, ctx: DraftContext, state: dict, team_idx: int, entry: dict, drafter: dict) -> str:
        old_proposal = entry.get('proposal')
        entry['proposal'] = None
        entry['pending_from'] = []
        ctx.save_state(state)
        _proposal_timer.cancel((ctx.draft_id, team_idx))

        if old_proposal:
            proposer = db.get_drafter(old_proposal['by'])
            if proposer:
                await ctx.dm(
                    proposer['user_id'],
                    f"↩️ **{drafter['display_name']}** wants to pick someone else instead of "
                    f"**{old_proposal['player']}{_year_suffix(old_proposal['year'])}**.",
                )

        return f"OK — DM me your pick: `{self._submission_format(ctx)}`"

    async def _resolve_proposal_timeout(self, ctx: DraftContext, team_idx: int, pick_number: int) -> None:
        async with draft_lock(ctx.draft_id):
            state = ctx.state
            team = state['teams'][team_idx]
            entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
            if entry is None or not entry.get('proposal'):
                return  # already resolved (finalized, or superseded) before this fired

            proposal = entry['proposal']
            entry['pending_from'] = []
            ctx.save_state(state)
            await self._finalize_pick(ctx, team_idx, entry, proposal['player'], proposal['year'], proposal['price'])

    async def _resolve_on_timeout(self, ctx: DraftContext, team_idx: int, pick_number: int) -> None:
        async with draft_lock(ctx.draft_id):
            if ctx.draft['status'] != 'active':
                return
            state = ctx.state
            team = state['teams'][team_idx]
            entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
            if entry is None or entry.get('skipped'):
                return  # already resolved by an in-time submission
            if state.get('timer_channel_id') and (team.get('timer_deadline') is None or team['timer_deadline'] > time.time()):
                return

            if team.get('timer_pick_number') == pick_number:
                team['timer_pick_number'] = None
                team['timer_deadline'] = None

            proposal = entry.get('proposal')
            if proposal:
                # The team's overall clock ran out with a proposal still on
                # the table — go with it rather than skipping the pick outright.
                entry['pending_from'] = []
                ctx.save_state(state)
                await self._finalize_pick(ctx, team_idx, entry, proposal['player'], proposal['year'], proposal['price'])
                return

            await self._skip_locked(ctx, team_idx, entry, 'Nobody on your team submitted this pick in time.')

    async def _skip_locked(self, ctx, team_idx, entry, reason):
        state = ctx.state
        team = state['teams'][team_idx]
        current = next((p for p in team['open_picks'] if p['pick_number'] == entry['pick_number']), None)
        if current is None or current.get('skipped'):
            return
        pick_number = current['pick_number']
        current.update(skipped=True, proposal=None, pending_from=[])
        team['skip_count'] = team.get('skip_count', 0)+1
        if team.get('timer_pick_number') == pick_number:
            team.update(timer_pick_number=None, timer_deadline=None)
        ctx.save_state(state)
        _timer.cancel((ctx.draft_id, team_idx))
        _proposal_timer.cancel((ctx.draft_id, team_idx))
        channel = ctx.bot.get_channel(state.get('timer_channel_id')) if state.get('timer_channel_id') else None
        sent = await channel.send(f"{pick_number}. {team['logo']} skipped") if channel else await ctx.announce(f"{pick_number}. {team['logo']} skipped")
        if isinstance(getattr(sent, 'id', None), int):
            fresh = ctx.state
            saved = next(p for p in fresh['teams'][team_idx]['open_picks'] if p['pick_number'] == pick_number)
            saved['skip_message_id'] = sent.id
            saved['skip_channel_id'] = getattr(channel, 'id', state.get('timer_channel_id'))
            ctx.save_state(fresh)
        for mid in team['member_ids']:
            mate = db.get_drafter(mid)
            await ctx.dm(mate['user_id'], f"⏰ Pick #{pick_number} skipped. {reason} DM me your pick to make it up; it will fill your oldest owed slot.")
        if pick_number == state.get('live_pick_number'):
            await self._advance_live_pick(ctx, pick_number)

    async def _finalize_pick(self, ctx: DraftContext, team_idx: int, entry: dict,
                              player: str, year: str, price: float | None) -> None:
        year = year or get_default_year(player) or ''
        state = ctx.state
        team = state['teams'][team_idx]
        pick_number = entry['pick_number']
        state['last_finalized_pick_number'] = max(state.get('last_finalized_pick_number', 0), pick_number)

        for mid in team['member_ids']:
            db.record_pick(ctx.draft_id, mid, player, round_number=None, pick_number=pick_number, auto_pick=False)

        if self.key == 'anon_budget_draft':
            from anon_lists import clear_team
            clear_team(team)

        if price is not None:
            team['budget_spent'] = team.get('budget_spent', 0.0) + price
            team.setdefault('pick_prices', {})[str(pick_number)] = {'player': player, 'year': year, 'price': price}

        team['open_picks'] = [p for p in team['open_picks'] if p['pick_number'] != pick_number]
        if team.get('timer_pick_number') == pick_number:
            team['timer_pick_number'] = None
            team['timer_deadline'] = None
        was_live = pick_number == state.get('live_pick_number')
        ctx.save_state(state)

        _timer.cancel((ctx.draft_id, team_idx))
        _proposal_timer.cancel((ctx.draft_id, team_idx))

        price_part = f" ${price:g}" if price is not None else ''
        year_part = f" '{_display_year(year)}" if year else ''

        results_channel_id = ctx.state.get('timer_channel_id') or _RESULTS_CHANNEL_ID
        results_channel = ctx.bot.get_channel(results_channel_id)
        if results_channel:
            options = {}
            if entry.get('skipped') and not entry.get('skip_message_id') and hasattr(results_channel, 'history'):
                try:
                    async for previous in results_channel.history(limit=300):
                        if (previous.author.id == ctx.bot.user.id and previous.content.strip() == f"{pick_number}. {team['logo']} skipped"):
                            entry['skip_message_id'] = previous.id
                            break
                except Exception:
                    log.warning('Could not locate the original skip message for pick %s', pick_number)
            if entry.get('skipped') and entry.get('skip_message_id'):
                options = dict(reference=discord.MessageReference(message_id=entry['skip_message_id'],
                    channel_id=results_channel_id, fail_if_not_exists=False), allowed_mentions=discord.AllowedMentions.none())
            sent = await results_channel.send(f"{pick_number}. {team['logo']} {player}{year_part}{price_part}", **options)
            fresh = ctx.state
            fresh['announcement_revision'] = fresh.get('announcement_revision', 0) + 1
            if isinstance(getattr(sent, 'id', None), int):
                fresh['last_pick_message_id'] = sent.id
            ctx.save_state(fresh)
        else:
            log.warning(
                "AnonDraft | results channel %s not found/not cached — pick announcement not posted",
                results_channel_id,
            )

        for mid in team['member_ids']:
            drafter = db.get_drafter(mid)
            await ctx.dm(drafter['user_id'], f"✅ Your team's pick: **{player}{year_part}{price_part}**")

        if was_live:
            await self._advance_live_pick(ctx, pick_number)
        elif ctx.state.get('scheduled_turns_finished') and self.is_pick_phase_complete(ctx):
            db.set_draft_status(ctx.draft_id, 'complete')
            await ctx.announce('All picks and makeup picks are finished. The draft is ready to reveal.')

    def _check_budget(self, ctx: DraftContext, team: dict, price: float | None) -> str | None:
        """Overridden by the Anon Budget subclass. Base format has no budget."""
        return None

    @staticmethod
    def _already_taken(ctx: DraftContext, player: str) -> str | None:
        """Returns the logo of the team that already drafted this player (in
        this draft), or None if it's still available. Apostrophe-normalized
        on both sides — a curly apostrophe from one GM's phone and a
        straight one from another's would otherwise compare as different
        strings, letting the same real player get drafted twice."""
        key = _normalize_name(player.strip().lower())
        all_picks = db.get_all_picks(ctx.draft_id)
        teams = ctx.state.get('teams', [])
        for drafter_id, players in all_picks.items():
            if not any(_normalize_name(p.strip().lower()) == key for p in players):
                continue
            drafter = db.get_drafter(drafter_id)
            team_idx = drafter['state'].get('team_index') if drafter else None
            if team_idx is not None and team_idx < len(teams):
                return teams[team_idx]['logo']
            return 'another team'
        return None

    # ── Queued picks — "leave a pick with the bot" for a GM who won't be
    # around when their team's actual turn comes up. Anonymous Draft hides
    # identity, so there's no way for them to just hand it off to a friend
    # like they could in a normal draft. `!pick <player> <year>` DM'd while
    # the team isn't on the clock is treated as setting this standing pick
    # instead of a live submission; it's consumed the moment the team's
    # turn opens, going through the exact same propose/consensus path a
    # live submission would (so a multi-member team's other GM still gets
    # the normal agree-or-counter prompt — silence still auto-agrees). ────
    async def _queue_pick(self, ctx: DraftContext, team: dict, drafter: dict, text: str) -> str:
        stripped = text.strip()
        if stripped.lower() in ('cancel', 'clear', 'none'):
            had_one = bool(drafter['state'].get('queued_pick'))
            db.update_drafter_state(drafter['id'], {'queued_pick': None})
            return "🗑️ Cleared your queued pick." if had_one else "You don't have a queued pick set."

        parsed = self._parse_submission(ctx, text)
        if parsed is None:
            return (
                f"Your team isn't on the clock right now. DM me `{self._submission_format(ctx)}` anyway to "
                "**queue** it — I'll submit it automatically the moment your team is up "
                "(`!pick cancel` clears a queued pick)."
            )
        player, year, price = parsed
        taken_by = self._already_taken(ctx, player)
        if taken_by:
            return f"❌ **{player}** has already been drafted by {taken_by}. Try someone else."
        budget_error = self._check_budget(ctx, team, price)
        if budget_error:
            return budget_error

        db.update_drafter_state(drafter['id'], {'queued_pick': {'player': player, 'year': year, 'price': price}})
        price_note = f' at ${price:g}' if price is not None else ''
        return (
            f"📌 Queued **{player}{_year_suffix(year)}**{price_note} — I'll submit it automatically "
            f"the moment your team is on the clock, unless you or a teammate picks something else first."
        )

    async def _try_consume_queued_pick(self, ctx: DraftContext, state: dict, team_idx: int, entry: dict) -> bool:
        """Called right as a pick opens. Returns True if a teammate's
        standing queued pick was used (and thus already proposed/finalized),
        so the caller shouldn't also send the normal "you're on the clock"
        prompt. Tries each team member's queue in member-list order — a
        player who's since become unavailable or unaffordable is skipped
        (with a heads-up DM) in favor of the next queued option, if any."""
        if self.key == 'anon_budget_draft':
            from anon_lists import consume
            if await consume(ctx, state, team_idx, entry):
                return True
        team = state['teams'][team_idx]
        for mid in team['member_ids']:
            queuer = db.get_drafter(mid)
            queued = queuer['state'].get('queued_pick') if queuer else None
            if not queued:
                continue
            db.update_drafter_state(mid, {'queued_pick': None})
            player, year, price = queued['player'], queued['year'], queued['price']

            if self.key == 'anon_budget_draft':
                error = await self.validate_round_price(ctx, player, price)
                if error:
                    await ctx.dm(queuer['user_id'], error)
                    continue

            taken_by = self._already_taken(ctx, player)
            if taken_by:
                await ctx.dm(
                    queuer['user_id'],
                    f"⚠️ Your queued pick **{player}** was already drafted by {taken_by} by the time your "
                    f"team came up — DM me a new pick: `{self._submission_format(ctx)}`",
                )
                continue

            budget_error = self._check_budget(ctx, team, price)
            if budget_error:
                await ctx.dm(
                    queuer['user_id'],
                    f"⚠️ Your queued pick couldn't be used — {budget_error}\nDM me a new pick.",
                )
                continue

            if len(team['member_ids']) == 1:
                await self._finalize_pick(ctx, team_idx, entry, player, year, price)
            else:
                await self._propose(ctx, state, team_idx, entry, queuer, player, year, price)
            return True
        return False

    # ── Submission parsing ──────────────────────────────────────────────────
    def _parse_submission(self, ctx: DraftContext, text: str) -> tuple[str, str, float | None] | None:
        parts = text.strip().split()
        if not parts:
            return None
        require_price = self._settings(ctx)['require_price']
        price = None
        if require_price:
            if len(parts) < 2:
                return None
            try:
                price = float(parts[-1].lstrip('$'))
                if not math.isfinite(price) or price < 0:
                    return None
            except ValueError:
                return None
            parts = parts[:-1]
        year = ''
        if len(parts) > 1 and _YEAR_TOKEN_RE.fullmatch(parts[-1]):
            year = parts.pop().lstrip("'")
        player = ' '.join(parts).strip()
        if not player or not any(c.isalpha() for c in player):
            return None
        return player, year or get_default_year(player) or '', price

    def _submission_format(self, ctx: DraftContext) -> str:
        return '!pick <player> [year] <price>' if self._settings(ctx)['require_price'] else '!pick <player> [year]'

    def _budget_note(self, ctx: DraftContext, team: dict) -> str:
        budget = self._settings(ctx).get('budget')
        if budget is None:
            return ''
        try:
            budget, spent = float(budget), float(team.get('budget_spent', 0))
        except (ValueError, TypeError):
            return 'Budget unavailable — ask the commissioner to check it.\n'
        if not all(math.isfinite(v) and v >= 0 for v in (budget, spent)):
            return 'Budget unavailable — ask the commissioner to check it.\n'
        return f'Budget remaining: **${budget - spent:g}** of **${budget:g}**.\n'

    def _submission_usage(self, ctx: DraftContext) -> str:
        if self._settings(ctx)['require_price']:
            return f"Usage: `{self._submission_format(ctx)}`. Year is optional: `!pick LeBron James $42` or `!pick LeBron James 2013 $42`."
        return f"Usage: `{self._submission_format(ctx)}`. Year is optional: `!pick LeBron James` or `!pick LeBron James 2013`."

    # ── Status / misc ────────────────────────────────────────────────────────
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        team_idx = drafter['state'].get('team_index')
        if team_idx is None:
            return "You're not assigned to a team yet."
        settings = self._settings(ctx)
        team = ctx.state['teams'][team_idx]
        picks = len(db.get_picks(ctx.draft_id, drafter['id']))
        open_count = len(team['open_picks'])
        budget_line = ''
        if settings['budget'] is not None:
            budget_line = f"\nBudget spent: ${team.get('budget_spent', 0):.0f} / ${settings['budget']:.0f}"
        queued = drafter['state'].get('queued_pick')
        queued_line = ''
        if queued:
            price_note = f" ${queued['price']:.0f}" if queued.get('price') is not None else ''
            queued_line = (
                f"\nQueued pick: **{queued['player']}{_year_suffix(queued['year'])}**{price_note} "
                f"(auto-submits when your team is on the clock)"
            )
        return (
            f"Team {team['logo']}. Picks so far: {picks}/{settings['rounds']}. "
            f"Open pick(s) owed: {open_count}.{budget_line}{queued_line}"
        )

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        # Timeouts are team-scoped (see _resolve_on_timeout), not
        # per-drafter — nothing to do via this generic per-drafter hook.
        return

    def is_pick_phase_complete(self, ctx: DraftContext, drafter: dict | None = None) -> bool:
        settings = self._settings(ctx)
        teams = ctx.state.get('teams', [])
        if not teams:
            return False
        return all(
            len(db.get_picks(ctx.draft_id, team['member_ids'][0])) >= settings['rounds']
            for team in teams
        )

    # ── Team grouping (1-3 drafters share 1 team) ───────────────────────────
    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        idx = drafter['state'].get('team_index')
        return f'anon-team-{idx}' if idx is not None else str(drafter['id'])

    def team_label(self, ctx: DraftContext, drafter: dict) -> str:
        idx = drafter['state'].get('team_index')
        if idx is None:
            return drafter['display_name'] or str(drafter['user_id'])
        return ctx.state['teams'][idx]['logo']

    def linked_drafter_ids(self, ctx: DraftContext, drafter: dict) -> list[int]:
        idx = drafter['state'].get('team_index')
        if idx is None:
            return [drafter['id']]
        return list(ctx.state['teams'][idx]['member_ids'])

    async def resume_timers(self, ctx: DraftContext) -> int:
        """Called on bot startup/reconnect (and by the overnight auto-pause
        job when it reopens the draft at 10 AM) — timers only live in this
        process's memory, so a restart silently drops any countdown that
        was mid-flight even though team/pick state itself (in sqlite)
        survives fine. Resumes from the persisted deadline (see
        _start_team_timer) so a team gets its genuine remaining time, not a
        fresh full clock — or an immediate resolution if it had already
        expired while the bot was down. Proposal-response timers (the
        agree-or-counter window) are NOT resumed. A pending proposal still
        resolves at the original pick deadline after a restart, and a GM
        can resolve it manually via `1`/`2`."""
        settings = self._settings(ctx)
        state = ctx.state
        if state.get('flux_wait'):
            return 0
        if self.key == 'anon_budget_draft' and ctx.draft['status'] == 'active':
            from anon_lists import consume
            async with draft_lock(ctx.draft_id):
                state = ctx.state
                for idx, team in enumerate(state.get('teams', [])):
                    entry = next((p for p in team.get('open_picks', []) if p['pick_number'] == state.get('live_pick_number')), None)
                    if entry and await consume(ctx, state, idx, entry):
                        return 1
            state = ctx.state
        teams = state.get('teams', [])
        live = state.get('live_pick_number')
        if live and teams and not any(t.get('timer_pick_number') == live for t in teams):
            idx = self._team_index_for_pick(live, len(teams), settings['rounds'])
            await self._open_pick_for_team(ctx, idx, live)
            return 1
        resumed = 0
        for team_idx, team in enumerate(teams):
            pick_number = team.get('timer_pick_number')
            if pick_number is None or _timer.is_running((ctx.draft_id, team_idx)):
                continue

            # No "reconnected" DM to every GM here on purpose — this fires
            # on every gateway reconnect AND every process restart (a
            # routine deploy included), not just a genuine extended
            # outage, so it was spamming everyone far more often than it
            # was actually useful. The timer itself still re-arms (or
            # resolves immediately if the deadline already passed) exactly
            # as before — only the notification, and the DM-success gate
            # on starting the timer, are gone.
            deadline = team.get('timer_deadline')
            if deadline is None:
                minutes = (team.pop('paused_timer_seconds') / 60 if 'paused_timer_seconds' in team else PickTimer.effective_minutes(
                    settings['base_timer_minutes'], settings['decay_minutes'], settings['floor_minutes'], team.get('skip_count', 0),
                ))
                self._start_team_timer(ctx, state, team_idx, minutes)
                resumed += 1
                continue

            remaining_seconds = deadline - time.time()
            if remaining_seconds <= 0 and not state.get('timer_channel_id'):
                await self._resolve_on_timeout(ctx, team_idx, pick_number)
            else:
                minutes = remaining_seconds / 60
                self._start_team_timer(ctx, ctx.state, team_idx, minutes)
            resumed += 1
        return resumed

    async def cancel(self, ctx: DraftContext) -> None:
        # Clear the persisted deadline (not just the in-memory task) so a
        # later resume_timers() grants a fresh full clock instead of
        # computing negative/expired remaining time — e.g. the overnight
        # auto-pause window is ~10 hours, which would otherwise read as
        # "already expired" and immediately auto-skip every team the
        # instant the draft reopens at 10 AM.
        state = ctx.state
        for team_idx, team in enumerate(state.get('teams', [])):
            if team.get('timer_deadline') is not None:
                team['paused_timer_seconds'] = max(0, team['timer_deadline'] - time.time())
            team['timer_deadline'] = None
            _timer.cancel((ctx.draft_id, team_idx))
            _proposal_timer.cancel((ctx.draft_id, team_idx))
        ctx.save_state(state)
