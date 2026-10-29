# roster.py
import math

# Final-roster validation shared by every theme: a submitted roster must be
# made entirely of players that drafter actually picked, and must fit the
# budget (when the theme has one).

def validate_drafted_by(picks: list[str], submitted: list[str],
                         extra_allowed: frozenset[str] = frozenset()) -> tuple[bool, list[str]]:
    """Every submitted player must appear in this drafter's own picks —
    unless explicitly exempted via extra_allowed (e.g. Pack Draft's $1
    filler players, which patch a roster nobody could otherwise afford)."""
    picks_lower = {p.lower() for p in picks}
    allowed_lower = {p.lower() for p in extra_allowed}
    errors = [p for p in submitted if p.lower() not in picks_lower and p.lower() not in allowed_lower]
    return (len(errors) == 0, errors)


def validate_budget(submitted: list[str], price_lookup: dict[str, float],
                     budget: float) -> tuple[bool, float, list[str]]:
    """Sums price_lookup[player] for each submitted player. Players missing
    from price_lookup are flagged as errors rather than silently costing $0."""
    total = 0.0
    missing = []
    for player in submitted:
        price = price_lookup.get(player, price_lookup.get(player.lower()))
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
        if price is None or not math.isfinite(price) or price < 0:
            missing.append(player)
        else:
            total += price
    ok = (not missing) and total <= budget
    return ok, total, missing


def validate_roster(picks: list[str], submitted: list[str], price_lookup: dict[str, float] | None = None,
                     budget: float | None = None, extra_allowed: frozenset[str] = frozenset()) -> tuple[bool, list[str]]:
    """Convenience wrapper combining both checks. price_lookup/budget are
    optional — pass None to skip the budget check entirely."""
    errors: list[str] = []

    drafted_ok, undrafted = validate_drafted_by(picks, submitted, extra_allowed)
    if not drafted_ok:
        errors.append(
            f"Not drafted by you: {', '.join(undrafted)}"
        )

    if price_lookup is not None and budget is not None:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            return False, errors + ['Budget must be a finite, non-negative amount.']
        if not math.isfinite(budget) or budget < 0:
            return False, errors + ['Budget must be a finite, non-negative amount.']
        budget_ok, total, missing = validate_budget(submitted, price_lookup, budget)
        if missing:
            errors.append(f"No price on file for: {', '.join(missing)}")
        elif total > budget:
            errors.append(f"Over budget: ${total:.2f} submitted, ${budget:.2f} available")

    return (len(errors) == 0, errors)
