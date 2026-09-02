# themes/anon_draft.py
#
# Anonymous Draft: ~30-32 GMs, grouped into teams of 1-3 owners each, each
# team assigned a random logo nobody but the commissioner (and this bot)
# can trace back to real people. The draft itself actually runs on ATD
# Timer Bot in the real selection channel — Timer Bot never sees a real
# GM's identity, because every anonymous team is registered there with
# THIS bot as the sole "owner". This bot:
#   1. Watches Timer Bot's public "your turn" ping, maps the pick number to
#      a team via its own precomputed snake order (Timer Bot's ping never
#      names the team — see handle_timer_ping), and relays the deadline to
#      the real hidden GM(s) via DM.
#   2. Collects each GM's DM'd pick (`!pick <player> <year>`) — on a multi-
#      member team, whoever submits first becomes the proposer and every
#      other member gets an agree (`1`)-or-counter (`2`) prompt; countering
#      hands the proposer role to them instead, ping-ponging until someone
#      agrees or a 2-minute response window lapses (silence = agreement).
#   3. Posts the finalized pick into the channel in the exact format ATD
#      Draft List Bot already uses ("N. <logo> Player 'YY"), which Sheet
#      Bot and Team Sheet Bot pick up unmodified — this bot must be in
#      their EXTRA_TRUSTED_BOT_IDS allowlist for that to actually register.
#
# Picks are still recorded in this bot's own DB for `!mypicks`, but this
# theme's real source of truth for "what got drafted" is the live sheet —
# unlike Pack Draft/Partner Draft, nothing here is hidden-until-reveal;
# only GM IDENTITY is hidden, never the pick itself.

import logging
import re
import time

import db
import lotto
from config import SELF_BOT_ID
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock

log = logging.getLogger(__name__)

_timer = PickTimer()  # keyed by (draft_id, team_index) — one clock per team, not per drafter
_proposal_timer = PickTimer()  # same keying — the 2-minute agree-or-counter window for a pending proposal
_PROPOSAL_RESPONSE_MINUTES = 2.0

_YEAR_TOKEN_RE = re.compile(r"^'?\d{2,4}(-\d{2,4})?$")


def _display_year(year: str) -> str:
    """Render as the 2-digit "'YY" suffix ATD Draft List Bot's convention
    (and Team Sheet Bot's parser) expects. A GM typing the natural 4-digit
    year ("!pick Michael Jordan 1990") otherwise produces "Player '1990" —
    Team Sheet Bot's year regex only consumes the apostrophe together with a
    2-digit year, so a 4-digit one leaves the "'" stuck on the player name
    in the sheet cell instead of being stripped into the year column."""
    digits = year.split('-')[0]
    return digits[-2:] if len(digits) > 2 else digits

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
            'fallback_deadline_minutes': 60,  # used only if a ping's deadline can't be parsed
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
            f"Run `!fakelotto {ctx.draft_id}` next to get the Timer Bot registration message."
        )

    async def generate_fake_lotto(self, ctx: DraftContext) -> str:
        teams = ctx.state.get('teams', [])
        if not teams:
            return "No teams registered yet — DM `!setteams` first."
        self_id = SELF_BOT_ID or (ctx.bot.user.id if ctx.bot.user else None)
        if not self_id:
            return "❌ Couldn't determine this bot's own Discord ID — set SELF_BOT_ID in .env."
        lines = [f"{t['index'] + 1}. {t['logo']} - <@{self_id}>" for t in teams]
        return (
            "Post this in Timer Bot's channel, then reply to it with `!timerloadlotto` "
            "(every line intentionally mentions **this bot**, never a real GM):\n```\n"
            + '\n'.join(lines) + "\n```"
        )

    async def start(self, ctx: DraftContext) -> str:
        teams = ctx.state.get('teams', [])
        if not teams:
            raise ValueError("No teams registered — DM me `!setteams` first.")
        settings = self._settings(ctx)
        return (
            f"Anonymous Draft ready — {len(teams)} teams, {settings['rounds']} rounds. "
            f"Make sure Timer Bot's draft is running in this same channel — picks will be relayed "
            f"automatically as its turn pings come in."
        )

    # ── Turn detection (driven by bot.py's Timer Bot ping listener) ────────────
    async def handle_timer_ping(self, ctx: DraftContext, pick_number: int, deadline_ts: int | None,
                                 window_closed: bool = False) -> None:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            teams = state.get('teams', [])
            if not teams:
                return
            team_idx = self._team_index_for_pick(pick_number, len(teams), settings['rounds'])
            if team_idx is None:
                return
            await self._open_pick_for_team(ctx, team_idx, pick_number, deadline_ts, window_closed=window_closed)

    async def override_pick(self, ctx: DraftContext, pick_number: int, team_ref: str) -> str:
        async with draft_lock(ctx.draft_id):
            teams = ctx.state.get('teams', [])
            team_idx = self._resolve_team_ref(teams, team_ref)
            if team_idx is None:
                return f"❌ Couldn't find a team matching '{team_ref}' (use its number or exact logo)."
            deadline_ts = int(time.time()) + self._settings(ctx)['fallback_deadline_minutes'] * 60
            # force=True — this is the commish's explicit safety valve for
            # exactly the case the guard below exists to prevent accidental
            # duplicates of; an intentional manual override should win.
            await self._open_pick_for_team(ctx, team_idx, pick_number, deadline_ts, force=True)
            return f"✅ Manually opened pick #{pick_number} for team {teams[team_idx]['logo']}."

    async def _open_pick_for_team(self, ctx: DraftContext, team_idx: int, pick_number: int,
                                   deadline_ts: int | None, window_closed: bool = False,
                                   force: bool = False) -> None:
        state = ctx.state
        team = state['teams'][team_idx]
        settings = self._settings(ctx)
        price_hint = ' `<price>`' if settings['require_price'] else ''

        if not force and pick_number <= state.get('last_finalized_pick_number', 0):
            # A stale/duplicate "your turn" ping for a pick already finalized
            # (observed cause: ATD Timer Bot's watchdog scanner can resend a
            # ping if it runs in the split-second between a pick being
            # accepted and the next one's ping going out) — reopening it
            # would let the team draft twice for the same slot.
            return

        entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
        is_new = entry is None
        if is_new:
            entry = {'pick_number': pick_number, 'skipped': False, 'proposal': None, 'pending_from': []}
            team['open_picks'].append(entry)
            ctx.save_state(state)

            if await self._try_consume_queued_pick(ctx, state, team_idx, entry):
                return  # a teammate's standing pick already fired — nothing else to do

        if window_closed:
            # Timer Bot's own closed-window ping has no real deadline yet
            # (its timer only starts at 10 AM ET) — don't start a countdown
            # or pressure the GM with a fake urgent deadline. Just let them
            # know they're up and can submit early if they want; the real
            # timer gets started below once an actual active ping arrives.
            if is_new:
                for mid in team['member_ids']:
                    drafter = db.get_drafter(mid)
                    await ctx.dm(
                        drafter['user_id'],
                        f"🏀 **Pick #{pick_number} — your team is on the clock**, but the draft window "
                        f"is currently closed. You can DM me your pick early with `!pick <player> <year>`"
                        f"{price_hint} and it'll be accepted, or wait — I'll ping you again once the "
                        f"real timer starts at 10:00 AM ET.",
                    )
            return

        # A live/active ping. If we already have a running timer for this
        # exact pick (e.g. the window just reopened and re-sent, or Timer
        # Bot's ping fired twice), don't restart it or re-notify.
        if _timer.is_running((ctx.draft_id, team_idx)) and team.get('timer_pick_number') == pick_number:
            return

        team['timer_pick_number'] = pick_number
        ctx.save_state(state)

        remaining_minutes = max(1.0, (deadline_ts - time.time()) / 60)
        for mid in team['member_ids']:
            drafter = db.get_drafter(mid)
            await ctx.dm(
                drafter['user_id'],
                f"🏀 **Pick #{pick_number} — the timer is live!**\n"
                f"Deadline: <t:{deadline_ts}:R>\n"
                f"DM me your pick: `!pick <player> <year>`{price_hint}",
            )

        async def _on_timeout():
            await self._resolve_on_timeout(ctx, team_idx, pick_number)

        _timer.start((ctx.draft_id, team_idx), remaining_minutes, _on_timeout)

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
                await self._finalize_pick(ctx, team_idx, entry, player, year, price)
                price_part = f" ${price:.0f}" if price is not None else ''
                year_part = f" '{_display_year(year)}" if year else ''
                replied = await ctx.reply_to_trigger(
                    f"✅ Makeup pick recorded — **{entry['pick_number']}. {team['logo']} {player}{year_part}{price_part}**"
                )
                if not replied:
                    await ctx.dm(drafter['user_id'], f"✅ Makeup pick recorded — **{player}{year_part}{price_part}**")
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
                    f"**{old_proposal['player']} '{_display_year(old_proposal['year'])}**.",
                )

        other_ids = [mid for mid in team['member_ids'] if mid != drafter['id']]
        entry['proposal'] = {'player': player, 'year': year, 'price': price, 'by': drafter['id']}
        entry['pending_from'] = list(other_ids)
        ctx.save_state(state)

        price_note = f' at ${price:.0f}' if price is not None else ''
        for mid in other_ids:
            mate = db.get_drafter(mid)
            await ctx.dm(
                mate['user_id'],
                f"📝 **{drafter['display_name']}** proposed **{player} '{_display_year(year)}**{price_note} for pick #{entry['pick_number']}.\n"
                f"Reply `1` to agree, or `2` to pick someone else instead.\n"
                f"(No response in {_PROPOSAL_RESPONSE_MINUTES:.0f} minutes = automatically agreed.)",
            )

        async def _on_proposal_timeout():
            await self._resolve_proposal_timeout(ctx, team_idx, entry['pick_number'])

        _proposal_timer.start((ctx.draft_id, team_idx), _PROPOSAL_RESPONSE_MINUTES, _on_proposal_timeout)

        return f"✅ Proposed **{player} '{_display_year(year)}**{price_note}. Waiting for your teammate to respond (2 min)."

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
                    f"**{old_proposal['player']} '{_display_year(old_proposal['year'])}**.",
                )

        return "OK — DM me your pick: `!pick <player> <year>`"

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
            state = ctx.state
            team = state['teams'][team_idx]
            entry = next((p for p in team['open_picks'] if p['pick_number'] == pick_number), None)
            if entry is None:
                return  # already resolved by an in-time submission

            if team.get('timer_pick_number') == pick_number:
                team['timer_pick_number'] = None

            proposal = entry.get('proposal')
            if proposal:
                # The team's overall clock ran out with a proposal still on
                # the table — go with it rather than skipping the pick outright.
                entry['pending_from'] = []
                ctx.save_state(state)
                await self._finalize_pick(ctx, team_idx, entry, proposal['player'], proposal['year'], proposal['price'])
                return

            entry['skipped'] = True
            ctx.save_state(state)
            skip_msg = await ctx.announce(
                f"⏭️ **Pick {pick_number} skipped** — {_safe_logo_text(team['logo'])} didn't submit in time."
            )
            entry['skip_message_id'] = skip_msg.id if skip_msg else None
            ctx.save_state(state)
            for mid in team['member_ids']:
                mate = db.get_drafter(mid)
                await ctx.dm(
                    mate['user_id'],
                    f"⏰ Nobody on your team submitted pick #{pick_number} in time — skipped. "
                    f"DM me anytime with your pick and I'll record it as a makeup.",
                )

    async def _finalize_pick(self, ctx: DraftContext, team_idx: int, entry: dict,
                              player: str, year: str, price: float | None) -> None:
        state = ctx.state
        team = state['teams'][team_idx]
        pick_number = entry['pick_number']
        state['last_finalized_pick_number'] = max(state.get('last_finalized_pick_number', 0), pick_number)

        for mid in team['member_ids']:
            db.record_pick(ctx.draft_id, mid, player, round_number=None, pick_number=pick_number, auto_pick=False)

        if price is not None:
            team['budget_spent'] = team.get('budget_spent', 0.0) + price

        team['open_picks'] = [p for p in team['open_picks'] if p['pick_number'] != pick_number]
        if team.get('timer_pick_number') == pick_number:
            team['timer_pick_number'] = None
        ctx.save_state(state)

        _timer.cancel((ctx.draft_id, team_idx))
        _proposal_timer.cancel((ctx.draft_id, team_idx))

        price_part = f" ${price:.0f}" if price is not None else ''
        year_part = f" '{_display_year(year)}" if year else ''
        pick_line = f"{pick_number}. {team['logo']} {player}{year_part}{price_part}"
        # Posted as this bot — Timer Bot / Sheet Bot / Team Sheet Bot need this
        # bot's ID in their EXTRA_TRUSTED_BOT_IDS for it to actually register.
        if entry.get('skipped') and entry.get('skip_message_id'):
            # A makeup pick — thread it as a reply to the original public
            # "skipped" notice so it's visually linked instead of showing up
            # as an unrelated new message.
            await ctx.announce_reply_to(entry['skip_message_id'], pick_line)
        else:
            await ctx.announce(pick_line)

        for mid in team['member_ids']:
            drafter = db.get_drafter(mid)
            await ctx.dm(drafter['user_id'], f"✅ Your team's pick: **{pick_line}**")

    def _check_budget(self, ctx: DraftContext, team: dict, price: float | None) -> str | None:
        """Overridden by the Anon Budget subclass. Base format has no budget."""
        return None

    @staticmethod
    def _already_taken(ctx: DraftContext, player: str) -> str | None:
        """Returns the logo of the team that already drafted this player (in
        this draft), or None if it's still available. Checked here rather
        than relying on ATD Timer Bot's own duplicate-pick rejection —
        that happens after we've already told the GM their pick was
        finalized, since Timer Bot only rejects it once we post to the
        public channel."""
        key = player.strip().lower()
        all_picks = db.get_all_picks(ctx.draft_id)
        teams = ctx.state.get('teams', [])
        for drafter_id, players in all_picks.items():
            if not any(p.strip().lower() == key for p in players):
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
                "Your team isn't on the clock right now. DM me `!pick <player> <year>` anyway to "
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
        price_note = f' at ${price:.0f}' if price is not None else ''
        return (
            f"📌 Queued **{player} '{_display_year(year)}**{price_note} — I'll submit it automatically "
            f"the moment your team is on the clock, unless you or a teammate picks something else first."
        )

    async def _try_consume_queued_pick(self, ctx: DraftContext, state: dict, team_idx: int, entry: dict) -> bool:
        """Called right as a pick opens. Returns True if a teammate's
        standing queued pick was used (and thus already proposed/finalized),
        so the caller shouldn't also send the normal "you're on the clock"
        prompt. Tries each team member's queue in member-list order — a
        player who's since become unavailable or unaffordable is skipped
        (with a heads-up DM) in favor of the next queued option, if any."""
        team = state['teams'][team_idx]
        for mid in team['member_ids']:
            queuer = db.get_drafter(mid)
            queued = queuer['state'].get('queued_pick') if queuer else None
            if not queued:
                continue
            db.update_drafter_state(mid, {'queued_pick': None})
            player, year, price = queued['player'], queued['year'], queued['price']

            taken_by = self._already_taken(ctx, player)
            if taken_by:
                await ctx.dm(
                    queuer['user_id'],
                    f"⚠️ Your queued pick **{player}** was already drafted by {taken_by} by the time your "
                    f"team came up — DM me a new pick: `!pick <player> <year>`",
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
        if len(parts) < 2:
            return None
        require_price = self._settings(ctx)['require_price']
        price = None
        if require_price:
            if len(parts) < 3:
                return None
            try:
                price = float(parts[-1].lstrip('$'))
            except ValueError:
                return None
            parts = parts[:-1]
        year_token = parts[-1]
        if not _YEAR_TOKEN_RE.match(year_token):
            return None
        player = ' '.join(parts[:-1]).strip()
        if not player:
            return None
        return player, year_token.lstrip("'"), price

    def _submission_usage(self, ctx: DraftContext) -> str:
        if self._settings(ctx)['require_price']:
            return "Usage: `!pick <player> <year> <price>` (e.g. `!pick LeBron James 2013 42`)"
        return "Usage: `!pick <player> <year>` (e.g. `!pick LeBron James 2013`)"

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
                f"\nQueued pick: **{queued['player']} '{_display_year(queued['year'])}**{price_note} "
                f"(auto-submits when your team is on the clock)"
            )
        return (
            f"Team {team['logo']}. Picks so far: {picks}/{settings['rounds']}. "
            f"Open pick(s) owed: {open_count}.{budget_line}{queued_line}"
        )

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        # Timeouts are team-scoped (see _resolve_on_timeout / handle_timer_ping),
        # not per-drafter — nothing to do via this generic per-drafter hook.
        return

    def is_pick_phase_complete(self, ctx: DraftContext) -> bool:
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
        """Timers here track Timer Bot's own deadline, not a fresh clock —
        there's no sane "resume" without re-reading Timer Bot's current
        ping, which this bot doesn't retroactively fetch. A restart mid-pick
        just means the next Timer Bot ping (or `!anonoverride`) re-opens it."""
        return 0
