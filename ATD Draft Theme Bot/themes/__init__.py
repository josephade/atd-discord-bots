# themes/__init__.py — theme registry.
# Adding a new draft format: write themes/<name>.py implementing Theme
# (see base.py), then register an instance below under a short key.

from .base import DraftContext, Theme
from .pack_draft import PackDraftTheme
from .partner_draft import PartnerDraftTheme
from .anon_draft import AnonDraftTheme
from .anon_budget_draft import AnonBudgetDraftTheme

REGISTRY: dict[str, Theme] = {
    PackDraftTheme.key: PackDraftTheme(),
    PartnerDraftTheme.key: PartnerDraftTheme(),
    AnonDraftTheme.key: AnonDraftTheme(),
    AnonBudgetDraftTheme.key: AnonBudgetDraftTheme(),
}


def get_theme(key: str) -> Theme:
    theme = REGISTRY.get(key)
    if not theme:
        available = ', '.join(REGISTRY)
        raise KeyError(f"Unknown theme '{key}'. Available: {available}")
    return theme
