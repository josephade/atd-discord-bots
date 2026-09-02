# roster.py
# Final-roster validation shared by every theme: a submitted roster must be
# made entirely of players that drafter actually picked, and must fit the
# budget (when the theme has one).

def validate_drafted_by(picks: list[str], submitted: list[str]) -> tuple[bool, list[str]]:
    """Every submitted player must appear in this drafter's own picks."""
    picks_lower = {p.lower() for p in picks}
    errors = [p for p in submitted if p.lower() not in picks_lower]
    return (len(errors) == 0, errors)


def validate_budget(submitted: list[str], price_lookup: dict[str, float],
                     budget: float) -> tuple[bool, float, list[str]]:
    """Sums price_lookup[player] for each submitted player. Players missing
    from price_lookup are flagged as errors rather than silently costing $0."""
    total = 0.0
    missing = []
    for player in submitted:
        price = price_lookup.get(player) or price_lookup.get(player.lower())
        if price is None:
            missing.append(player)
        else:
            total += price
    ok = (not missing) and total <= budget
    return ok, total, missing


def validate_roster(picks: list[str], submitted: list[str], price_lookup: dict[str, float] | None = None,
                     budget: float | None = None) -> tuple[bool, list[str]]:
    """Convenience wrapper combining both checks. price_lookup/budget are
    optional — pass None to skip the budget check entirely."""
    errors: list[str] = []

    drafted_ok, undrafted = validate_drafted_by(picks, submitted)
    if not drafted_ok:
        errors.append(
            f"Not drafted by you: {', '.join(undrafted)}"
        )

    if price_lookup is not None and budget is not None:
        budget_ok, total, missing = validate_budget(submitted, price_lookup, budget)
        if missing:
            errors.append(f"No price on file for: {', '.join(missing)}")
        elif total > budget:
            errors.append(f"Over budget: ${total:.2f} submitted, ${budget:.2f} available")

    return (len(errors) == 0, errors)
