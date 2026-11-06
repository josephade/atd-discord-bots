# themes/anon_budget_draft.py
#
# Anonymous Budget Draft — identical to Anonymous Draft (see anon_draft.py
# for the full mechanism: team logos, Timer Bot relay, consensus/priority,
# skip/makeup) except every pick submission must include a price, and that
# price is checked live against the team's own pooled budget (shared across
# all 1-3 owners) before it's accepted — not validated later, unlike Pack
# Draft's end-of-draft budget check.

from .anon_draft import AnonDraftTheme
from .base import DraftContext
import math
import time
import db
import asyncio
import sheets
import logging
import difflib
import re
import unicodedata
from flux_rules import parse_prices, find_player, amount

log = logging.getLogger(__name__)


def budget_price_record(book, player):
    """Absent players are free; ambiguous/invalid listed prices are not."""
    if not book:
        raise ValueError('The ADP sheet has no player rows; check the price source.')
    try:
        return find_player(book, player)
    except ValueError:
        fingerprint = lambda name: re.sub(r'[^a-z0-9]', '', ''.join(
            c for c in unicodedata.normalize('NFKD', name).casefold()
            if not unicodedata.combining(c)))
        query = fingerprint(player)
        same = [record for record in book.values() if fingerprint(record['player']) == query]
        if len(same) == 1 and not same[0].get('ambiguous'):
            return same[0]
        # Do not treat an ambiguous abbreviation or a punctuation variant of
        # conflicting duplicate rows as a missing/free player.
        if same or any(query and query in fingerprint(r['player']) for r in book.values()):
            raise ValueError('Use a full player name that uniquely matches the ADP sheet.') from None
        close = difflib.get_close_matches(query, [fingerprint(r['player']) for r in book.values()],
                                         n=1, cutoff=.92)
        if close:
            suggestion = next(r['player'] for r in book.values() if fingerprint(r['player']) == close[0])
            raise ValueError(f'Check the player spelling: did you mean {suggestion}? No free pick was accepted.') from None
        return dict(player=player, current=amount(0), previous=None, off_sheet=True)


class AnonBudgetDraftTheme(AnonDraftTheme):
    requires_roster_submission = False
    key = 'anon_budget_draft'
    display_name = 'Anonymous Budget Draft'

    async def validate_round_price(self, ctx, player, price, entry=None):
        from config import POOL_SPREADSHEET_ID, POOL_TAB_NAME
        state = ctx.state
        source = state.get('flux_price_source') or {'sheet': POOL_SPREADSHEET_ID, 'tab': POOL_TAB_NAME}
        count = max(1, len(state.get('teams', [])))
        live = state.get('live_pick_number')
        round_number = (live-1)//count+1 if live else self._settings(ctx)['rounds']
        if state.get('flux_wait'):
            round_number = max(1, state['flux_wait']['round']-1)
        if state.get('flux_in_progress'):
            return 'Flux is currently running. Wait for its completion before submitting your pick; no pick was accepted.'
        skipped_round = (entry['pick_number']-1)//count+1 if entry and entry.get('skipped') else None
        # Once a later Flux has been released, a makeup costs the player's
        # historical maximum, including the Round 1 baseline before the skip.
        first_round = 1 if skipped_round is not None and skipped_round < round_number else round_number
        description = (f'Round {round_number}' if first_round == round_number else
                       f'Rounds {first_round}\u2013{round_number} (highest price for your makeup pick)')
        try:
            rows = await asyncio.to_thread(sheets.load_flux_rule_rows, source)
            records = [budget_price_record(parse_prices(rows, number), player)
                       for number in range(first_round, round_number+1)]
            if not records or any(r['current'] is None for r in records):
                raise ValueError(f'{player} has a blank or invalid price in the required round history.')
            record = max(records, key=lambda r: r['current'])
        except ValueError as exc:
            log.warning('Price validation failed for draft %s, %s: %s', getattr(ctx, 'draft_id', '?'), source['tab'], exc)
            return f'I could not verify {player} against {description} prices: {exc} No pick was accepted.'
        except Exception:
            log.exception('Could not read Flux source for draft %s, tab %s', getattr(ctx, 'draft_id', '?'), source.get('tab'))
            return f'I could not read the price sheet ({source.get("tab", "unknown tab")}) to verify {player}. No pick was accepted. Check sheet access and !fluxsource, then try again.'
        if amount(price) != record['current']:
            if record.get('off_sheet'):
                return f'{player} is not on the ADP sheet and is free ($0). Submit your pick again with $0.'
            entered = f'${price:g}' if isinstance(price, (int, float)) else 'no valid price'
            return f"{record['player']} costs ${record['current']:g} in {description}; you entered {entered}. Submit your pick again with ${record['current']:g}."
        return None

    async def on_pick(self, ctx, drafter, text):
        parsed = self._parse_submission(ctx, text)
        if parsed is not None:
            player, year, price = parsed
            index = drafter['state'].get('team_index')
            team = ctx.state['teams'][index] if index is not None else {}
            entry = next(iter(team.get('open_picks', [])), None)
            if ctx.state.get('flux_wait') and not (entry and entry.get('skipped')):
                return await super().on_pick(ctx, drafter, text)
            error = await self.validate_round_price(ctx, player, price, entry=entry)
            if error:
                return error
        return await super().on_pick(ctx, drafter, text)

    def default_settings(self) -> dict:
        return {
            **super().default_settings(),
            'require_price': True,
            'budget': 100,
        }

    def _check_budget(self, ctx: DraftContext, team: dict, price: float | None) -> str | None:
        if price is None:
            return "A price is required for every pick in this draft."
        try:
            price = float(price)
        except (TypeError, ValueError):
            return "Price must be a finite number."
        if not math.isfinite(price):
            return "Price must be a finite number."
        settings = self._settings(ctx)
        try:
            budget = float(settings['budget'])
            spent = float(team.get('budget_spent', 0.0))
        except (TypeError, ValueError):
            return 'Your team budget is invalid. Ask the commissioner to check the settings.'
        if budget < 0 or not all(math.isfinite(value) for value in (budget, spent)):
            return 'Your team budget is invalid. Ask the commissioner to check the settings.'
        if spent + price > budget:
            remaining = budget - spent
            return (
                f"❌ That's ${price:g}, but your team only has ${remaining:g} left "
                f"of your ${budget:g} pooled budget."
            )
        return None

    async def _finalize_pick(self, ctx, team_idx, entry, player, year, price):
        # A different owed pick may have spent money while this proposal waited.
        state = ctx.state
        team = state['teams'][team_idx]
        current = next((p for p in team['open_picks'] if p['pick_number'] == entry['pick_number']), None)
        if current is None:
            return
        error = await self.validate_round_price(ctx, player, price, entry=current)
        if not error:
            error = self._check_budget(ctx, team, price)
        taken_by = self._already_taken(ctx, player)
        if taken_by:
            error = f'{player} has already been drafted by {taken_by}. Pick someone else.'
        if error:
            current['proposal'] = None
            current['pending_from'] = []
            ctx.save_state(state)
            if not current.get('skipped') and state.get('live_pick_number') == current['pick_number']:
                # Keep the original deadline. An invalid proposal at expiry
                # gets skipped by the next callback instead of a fresh clock.
                deadline = team.get('timer_deadline')
                seconds = max(0, deadline-time.time()) if deadline is not None else self._settings(ctx)['base_timer_minutes']*60
                team['timer_pick_number'] = current['pick_number']
                self._start_team_timer(ctx, state, team_idx, seconds / 60)
            for member_id in team['member_ids']:
                member = db.get_drafter(member_id)
                await ctx.dm(member['user_id'], f'{error}\nSubmit a new pick within your remaining budget.')
            return
        await super()._finalize_pick(ctx, team_idx, current, player, year, price)
