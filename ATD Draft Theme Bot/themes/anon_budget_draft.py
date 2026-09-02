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


class AnonBudgetDraftTheme(AnonDraftTheme):
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
        if price < 0:
            return "Price can't be negative."
        settings = self._settings(ctx)
        budget = settings['budget']
        spent = team.get('budget_spent', 0.0)
        if spent + price > budget:
            remaining = budget - spent
            return (
                f"❌ That's ${price:.0f}, but your team only has ${remaining:.0f} left "
                f"of your ${budget:.0f} pooled budget."
            )
        return None
