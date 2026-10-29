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


class AnonBudgetDraftTheme(AnonDraftTheme):
    requires_roster_submission = False
    key = 'anon_budget_draft'
    display_name = 'Anonymous Budget Draft'

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
            return "Price must be a finite, non-negative amount."
        if not math.isfinite(price) or price < 0:
            return "Price must be a finite, non-negative amount."
        settings = self._settings(ctx)
        try:
            budget = float(settings['budget'])
            spent = float(team.get('budget_spent', 0.0))
        except (TypeError, ValueError):
            return 'Your team budget is invalid. Ask the commissioner to check the settings.'
        if not all(math.isfinite(value) and value >= 0 for value in (budget, spent)):
            return 'Your team budget is invalid. Ask the commissioner to check the settings.'
        if spent + price > budget:
            remaining = budget - spent
            return (
                f"❌ That's ${price:.0f}, but your team only has ${remaining:.0f} left "
                f"of your ${budget:.0f} pooled budget."
            )
        return None

    async def _finalize_pick(self, ctx, team_idx, entry, player, year, price):
        # A different owed pick may have spent money while this proposal waited.
        state = ctx.state
        team = state['teams'][team_idx]
        current = next((p for p in team['open_picks'] if p['pick_number'] == entry['pick_number']), None)
        if current is None:
            return
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
                seconds = max(0, (team.get('timer_deadline') or time.time()) - time.time())
                team['timer_pick_number'] = current['pick_number']
                self._start_team_timer(ctx, state, team_idx, seconds / 60)
            for member_id in team['member_ids']:
                member = db.get_drafter(member_id)
                await ctx.dm(member['user_id'], f'{error}\nSubmit a new pick within your remaining budget.')
            return
        await super()._finalize_pick(ctx, team_idx, current, player, year, price)
