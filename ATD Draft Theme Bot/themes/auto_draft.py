# themes/auto_draft.py
#
# Auto Draft: every round, every team DMs this bot a ranked list of up to
# `list_size` (default 10) players instead of making one live pick at a
# time — standing in for a human mod who'd otherwise collect those lists
# and adjudicate them by hand. Once every team has submitted (or the
# round's grace timer expires), the whole round resolves at once, using
# the standard "immediate acceptance" (Boston) mechanism:
#   - Each team's current bid is the first player still left on its own
#     list, in its own preference order.
#   - A player bid by exactly one team goes straight to them.
#   - A player bid by two or more teams at DIFFERENT ranks just goes to
#     whoever ranked it higher (lower rank number) outright — no lottery.
#   - A player bid by two or more teams at the SAME rank is a true tie,
#     settled by an equal-odds hidden lottery among just those teams. The
#     winner is sent to the back of a separate tiebreaker waiver order —
#     bookkeeping only (shown via `!theme status`); it never weights the
#     lottery itself, which stays equal-odds every time.
#   - Every team that didn't win its current bid (lost a tie, or got
#     outranked) drops to the next still-available name on its own list
#     and re-bids — repeating until every team has either won a player or
#     run out of listed names for the round.
# A team that runs out of names without winning anything just gets no
# pick that round — rounds don't guarantee one pick per team.
#
# Picks are never written to the Team Sheet by this bot directly — each
# finalized pick is instead announced as a bare "<logo> <Player>" message
# (no markdown, no extra text) in the draft's admin channel, which is what
# lets ATD Team Sheet Bot auto-parse and write it the moment it sees a
# recognized team logo + player name (year optional, same as any other
# chat-driven pick there). That means the admin channel this draft was
# created in must be the channel ATD Team Sheet Bot has mapped to the
# sheet (`!setsheet`), and `!setteams`' logo column must use the exact
# emoji ATD Team Sheet Bot's emoji_map.py (or its built-in unicode map)
# already recognizes for that team — same requirement Anonymous/Nation
# Flux drafts already have. Every OTHER announcement here (round recaps,
# tiebreaker/skip notices) deliberately avoids a raw logo token — see
# _safe_logo_text — because a message with more than one real team emoji
# in it gets misparsed as a single garbage pick instead of being ignored.

import logging
import random
import re
import time

import db
import lotto
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock, highlight_taken

log = logging.getLogger(__name__)

_timer = PickTimer()  # keyed by draft_id — one shared grace clock per round, not per team

_CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')


def _safe_logo_text(logo: str) -> str:
    """De-fangs a custom-emoji logo token for any announcement that ISN'T
    a clean single pick trigger — see the module docstring for why."""
    m = _CUSTOM_EMOJI_RE.search(logo)
    return f':{m.group(1)}:' if m else logo


class AutoDraftTheme(Theme):
    key = 'auto_draft'
    display_name = 'Auto Draft'

    def default_settings(self) -> dict:
        return {
            'rounds': 10,
            'list_size': 10,
            'response_deadline_minutes': 1440,  # 24h grace window per round
        }

    def _settings(self, ctx: DraftContext) -> dict:
        return {**self.default_settings(), **ctx.settings}

    # ── Setup (DM-only, commish) ─────────────────────────────────────────────
    async def setup_teams(self, ctx: DraftContext, text: str) -> str:
        parsed = lotto.parse_team_lines(text)
        if not parsed:
            return (
                "❌ No valid team lines found. Format: `1. 🦅 - @GM1 @GM2`, one team per line, in draft order — "
                "the logo must be the exact emoji ATD Team Sheet Bot already recognizes for that team, since "
                "that's what lets picks auto-write to the sheet."
            )

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
                'skip_count': 0,
            })

        for team in teams:
            for mid in team['member_ids']:
                db.update_drafter_state(mid, {'team_index': team['index']})

        state = ctx.state
        state['teams'] = teams
        state['tiebreak_order'] = [t['index'] for t in teams]
        ctx.save_state(state)

        total_gms = sum(len(t['member_ids']) for t in teams)
        return (
            f"✅ Registered {len(teams)} teams ({total_gms} GMs total) — line order is also the starting "
            f"tiebreaker waiver order. Run `!theme start {ctx.draft_id}` to open round 1."
        )

    async def start(self, ctx: DraftContext) -> str:
        teams = ctx.state.get('teams', [])
        if not teams:
            raise ValueError("No teams registered — DM me `!setteams` first.")
        settings = self._settings(ctx)
        state = ctx.state
        state['round_index'] = 0
        state['lists'] = {}
        ctx.save_state(state)
        await self._open_round(ctx)
        return (
            f"Auto Draft started — {len(teams)} teams, {settings['rounds']} rounds. "
            f"Round 1 is open: DM me `!list P1, P2, ..., P{settings['list_size']}`."
        )

    # ── Round lifecycle ──────────────────────────────────────────────────────
    async def _open_round(self, ctx: DraftContext) -> None:
        state = ctx.state
        settings = self._settings(ctx)
        round_number = state['round_index'] + 1
        deadline_ts = int(time.time() + settings['response_deadline_minutes'] * 60)

        for team in state['teams']:
            for mid in team['member_ids']:
                drafter = db.get_drafter(mid)
                await ctx.dm(
                    drafter['user_id'],
                    f"🤖 **Auto Draft — Round {round_number}** is open.\n"
                    f"DM me a ranked list of up to {settings['list_size']} players, most wanted first: "
                    f"`!list Player One, Player Two, Player Three, ...`\n"
                    f"Deadline: <t:{deadline_ts}:R> — anyone still missing by then gets resolved with "
                    f"whatever they've submitted (or nothing, if they never did).",
                )

        self._start_round_timer(ctx, state, settings['response_deadline_minutes'])

    def _start_round_timer(self, ctx: DraftContext, state: dict, minutes: float) -> None:
        state['round_deadline'] = time.time() + minutes * 60
        ctx.save_state(state)

        async def _on_timeout():
            async with draft_lock(ctx.draft_id):
                await self._resolve_round(ctx, forced=True)

        _timer.start(ctx.draft_id, minutes, _on_timeout)

    # ── List submission ──────────────────────────────────────────────────────
    async def on_list(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            if state.get('round_index', 0) >= settings['rounds']:
                return "Auto Draft has finished all its rounds."

            team_idx = drafter['state'].get('team_index')
            if team_idx is None:
                return "You're not assigned to a team yet."

            players = self._parse_list(text, settings['list_size'])
            if players is None:
                return f"Submit 1-{settings['list_size']} distinct players, comma- or line-separated."

            already_taken = db.get_taken_players(ctx.draft_id)
            available = [p for p in players if p.strip().lower() not in already_taken]
            dropped = len(players) - len(available)
            if not available:
                return "❌ Every player on that list has already been drafted — submit a fresh list."

            lists = state.setdefault('lists', {})
            lists[str(team_idx)] = available
            ctx.save_state(state)

            note = f" ({dropped} already-drafted name(s) dropped.)" if dropped else ''

            teams = state['teams']
            if len(lists) >= len(teams):
                _timer.cancel(ctx.draft_id)
                round_number = state['round_index'] + 1
                await self._resolve_round(ctx)
                return f"✅ List received{note} — everyone's in, round {round_number} is resolving now."

            if not _timer.is_running(ctx.draft_id):
                self._start_round_timer(ctx, state, settings['response_deadline_minutes'])

            round_number = state['round_index'] + 1
            return f"✅ List received for round {round_number}{note}. Waiting on {len(teams) - len(lists)} more team(s)."

    # ── Round resolution ─────────────────────────────────────────────────────
    # Callers must already hold draft_lock(ctx.draft_id) — this is never an
    # independent entry point itself (on_list and the round timer's own
    # timeout callback each acquire the lock once, then call this).
    async def _resolve_round(self, ctx: DraftContext, forced: bool = False) -> None:
        state = ctx.state
        settings = self._settings(ctx)
        if state.get('round_index', 0) >= settings['rounds']:
            return  # already finished — a stale timer fired after completion

        teams = state['teams']
        lists_raw = state.get('lists', {})
        skipped_teams = []
        lists: dict[int, list[str]] = {}
        for team in teams:
            entry = lists_raw.get(str(team['index']))
            if entry is None:
                team['skip_count'] = team.get('skip_count', 0) + 1
                skipped_teams.append(team['index'])
            lists[team['index']] = entry or []

        already_taken = db.get_taken_players(ctx.draft_id)
        assigned, tie_events, new_order = self._resolve_assignments(
            lists, already_taken, state.get('tiebreak_order', [t['index'] for t in teams]),
        )
        state['tiebreak_order'] = new_order

        round_number = state['round_index'] + 1
        for team in teams:
            player = assigned.get(team['index'])
            if player is None:
                continue
            for mid in team['member_ids']:
                db.record_pick(ctx.draft_id, mid, player, round_number=round_number, pick_number=None, auto_pick=False)
            await highlight_taken(player)
            # Bare "<logo> <Player>" — nothing else in the message — so ATD
            # Team Sheet Bot (watching this same admin channel) auto-parses
            # and writes it the instant it posts. See the module docstring.
            await ctx.announce(f"{team['logo']} {player}")

        state['lists'] = {}
        state['round_index'] = round_number
        ctx.save_state(state)
        _timer.cancel(ctx.draft_id)

        by_team = {t['index']: t for t in teams}
        result_lines = []
        for team in teams:
            player = assigned.get(team['index'])
            safe_logo = _safe_logo_text(team['logo'])
            result_lines.append(f"• {safe_logo}: **{player}**" if player else f"• {safe_logo}: *(no pick)*")
        tie_lines = [
            f"🎲 Tiebreaker lottery for **{player}** among "
            f"{', '.join(_safe_logo_text(by_team[t]['logo']) for t in tied)} — "
            f"{_safe_logo_text(by_team[winner]['logo'])} won it."
            for player, tied, winner in tie_events
        ]
        skip_line = (
            f"\n⏭️ No list received from: {', '.join(_safe_logo_text(by_team[i]['logo']) for i in skipped_teams)}."
            if skipped_teams else ''
        )
        summary = (
            f"🤖 **Auto Draft — Round {round_number} recap:**\n" + '\n'.join(result_lines)
            + ('\n' + '\n'.join(tie_lines) if tie_lines else '') + skip_line
        )
        await ctx.announce(summary)

        for team in teams:
            player = assigned.get(team['index'])
            msg = f"🏀 Your team drafted **{player}**!" if player else (
                "😬 Your team didn't land a pick this round — every name on your list was taken or lost a tiebreak."
            )
            for mid in team['member_ids']:
                drafter = db.get_drafter(mid)
                await ctx.dm(drafter['user_id'], msg)

        if round_number >= settings['rounds']:
            db.set_draft_status(ctx.draft_id, 'complete')
            await ctx.announce(f"🏁 **Auto Draft complete!** All {settings['rounds']} rounds resolved.")
            return

        await self._open_round(ctx)

    @staticmethod
    def _resolve_assignments(
        lists: dict[int, list[str]], already_taken: set[str], tiebreak_order: list[int],
    ) -> tuple[dict[int, str], list[tuple[str, list[int], int]], list[int]]:
        """Runs the immediate-acceptance waterfall for one round. Returns
        (team_idx -> winning player, [(player, [tied team_idxs], winner_idx)]
        for every lottery actually held, updated tiebreak order)."""
        pointers = {team_idx: 0 for team_idx in lists}
        assigned: dict[int, str] = {}
        taken_this_round: set[str] = set()
        tie_events: list[tuple[str, list[int], int]] = []
        order = list(tiebreak_order)
        pending = {team_idx for team_idx, names in lists.items() if names}

        while pending:
            candidates: dict[int, tuple[str, int]] = {}
            exhausted_now = set()
            for team_idx in pending:
                team_list = lists[team_idx]
                p = pointers[team_idx]
                while p < len(team_list) and (
                    team_list[p].strip().lower() in already_taken
                    or team_list[p].strip().lower() in taken_this_round
                ):
                    p += 1
                pointers[team_idx] = p
                if p >= len(team_list):
                    exhausted_now.add(team_idx)
                    continue
                candidates[team_idx] = (team_list[p], p)

            pending -= exhausted_now
            if not candidates:
                break

            by_key: dict[str, list[tuple[int, int, str]]] = {}
            for team_idx, (player, rank) in candidates.items():
                key = player.strip().lower()
                by_key.setdefault(key, []).append((team_idx, rank, player))

            progressed = False
            for key, bids in by_key.items():
                min_rank = min(r for _, r, _ in bids)
                top = [(t, p) for t, r, p in bids if r == min_rank]
                display_player = top[0][1]
                if len(top) == 1:
                    winner = top[0][0]
                else:
                    top_team_idxs = [t for t, _ in top]
                    winner = random.choice(top_team_idxs)
                    tie_events.append((display_player, top_team_idxs, winner))
                    if winner in order:
                        order.remove(winner)
                    order.append(winner)
                assigned[winner] = display_player
                taken_this_round.add(key)
                pending.discard(winner)
                pointers[winner] += 1
                progressed = True

            if not progressed:
                break

        return assigned, tie_events, order

    # ── Status / misc ────────────────────────────────────────────────────────
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        settings = self._settings(ctx)
        team_idx = drafter['state'].get('team_index')
        if team_idx is None:
            return "You're not assigned to a team yet."
        state = ctx.state
        round_number = state.get('round_index', 0) + 1
        if state.get('round_index', 0) >= settings['rounds']:
            return "Auto Draft has finished all its rounds."
        submitted = str(team_idx) in state.get('lists', {})
        picks = len(db.get_picks(ctx.draft_id, drafter['id']))
        waiver_pos = state.get('tiebreak_order', []).index(team_idx) + 1 if team_idx in state.get('tiebreak_order', []) else '?'
        return (
            f"Round {round_number}/{settings['rounds']}. Your list this round: "
            f"{'submitted' if submitted else 'not submitted yet'}. "
            f"Picks so far: {picks}. Tiebreaker waiver position: {waiver_pos}/{len(state.get('teams', []))}."
        )

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        # Resolution is round-wide (see _resolve_round's own shared timer),
        # not per-drafter — nothing to do via this generic hook.
        return

    def is_pick_phase_complete(self, ctx: DraftContext, drafter: dict | None = None) -> bool:
        settings = self._settings(ctx)
        return ctx.state.get('round_index', 0) >= settings['rounds']

    # ── Team grouping (1-3 drafters share 1 team) ───────────────────────────
    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        idx = drafter['state'].get('team_index')
        return f'auto-team-{idx}' if idx is not None else str(drafter['id'])

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

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_list(text: str, max_size: int) -> list[str] | None:
        parts = [p.strip() for p in text.replace('\n', ',').split(',') if p.strip()]
        if not (1 <= len(parts) <= max_size):
            return None
        if len({p.lower() for p in parts}) != len(parts):
            return None
        return parts

    # ── Timer lifecycle (restart-safety) ────────────────────────────────────
    async def cancel(self, ctx: DraftContext) -> None:
        state = ctx.state
        state['round_deadline'] = None
        ctx.save_state(state)
        _timer.cancel(ctx.draft_id)

    async def resume_timers(self, ctx: DraftContext) -> int:
        settings = self._settings(ctx)
        state = ctx.state
        if state.get('round_index', 0) >= settings['rounds']:
            return 0
        if _timer.is_running(ctx.draft_id):
            return 0

        deadline = state.get('round_deadline')
        teams = state.get('teams', [])
        if deadline is None:
            self._start_round_timer(ctx, ctx.state, settings['response_deadline_minutes'])
            return 1

        remaining = deadline - time.time()
        if remaining <= 0:
            for team in teams:
                for mid in team['member_ids']:
                    drafter = db.get_drafter(mid)
                    await ctx.dm(drafter['user_id'], "🔄 Reconnected — this round's deadline had already passed while I was offline. Resolving now.")
            async with draft_lock(ctx.draft_id):
                await self._resolve_round(ctx, forced=True)
        else:
            minutes = remaining / 60
            for team in teams:
                for mid in team['member_ids']:
                    drafter = db.get_drafter(mid)
                    await ctx.dm(drafter['user_id'], f"🔄 Reconnected — this round still has **{minutes:.0f} minutes** left.")
            self._start_round_timer(ctx, ctx.state, minutes)
        return 1
