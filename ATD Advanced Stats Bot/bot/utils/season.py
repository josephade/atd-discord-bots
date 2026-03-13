"""
Season parsing utilities shared across all cogs.
"""

import re

DEFAULT_SEASON = "2024-25"


def parse_season(year_input: str) -> str | None:
    """
    Convert flexible user input to NBA season format.
      '2003'    → '2003-04'
      '2003-04' → '2003-04'
      '03-04'   → None  (too ambiguous)
    Returns None if the input is unrecognisable.
    """
    s = year_input.strip()
    if re.match(r'^\d{4}-\d{2}$', s):
        return s
    if re.match(r'^\d{4}$', s):
        y = int(s)
        return f"{y}-{str(y + 1)[2:]}"
    return None


def extract_year_from_args(parts: list[str]) -> tuple[str, str]:
    """
    Given a tokenised arg list, pop a trailing year if present.
    Returns (remaining_player_string, season).

    Example:
        ['LeBron', 'James', '2012'] → ('LeBron James', '2012-13')
        ['LeBron', 'James']         → ('LeBron James', '2024-25')
    """
    if parts and re.match(r'^\d{4}(-\d{2})?$', parts[-1]):
        season = parse_season(parts[-1]) or DEFAULT_SEASON
        return " ".join(parts[:-1]), season
    return " ".join(parts), DEFAULT_SEASON