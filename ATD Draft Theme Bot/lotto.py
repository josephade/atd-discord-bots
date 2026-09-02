# lotto.py
# Parses the same lotto-message shape ATD Draft List Bot's
# _extract_lotto_lines() already reads — numbered line, optional emoji,
# a dash, then one or more @mentions — so a commissioner can reply to the
# league's existing lottery post to set draft order, instead of this bot
# running its own separate lottery.
#
# Example line this matches:  "3. 🏀 - @Alice"
# Only the first mention on a line is used if more than one is present.

import re

_LOTTO_LINE_RE = re.compile(r'^\s*(\d+)\.\s*(.*?)\s*-\s*((?:<@!?\d+>\s*)+)$')


def parse_lotto_order(text: str) -> list[int]:
    """Returns Discord user IDs in draft-pick order (line number order)."""
    order: list[tuple[int, int]] = []
    for line in text.splitlines():
        m = _LOTTO_LINE_RE.match(line.strip())
        if not m:
            continue
        pick_num = int(m.group(1))
        mention = re.search(r'<@!?(\d+)>', m.group(3))
        if mention:
            order.append((pick_num, int(mention.group(1))))
    order.sort(key=lambda t: t[0])
    return [uid for _, uid in order]


def parse_team_lines(text: str) -> list[dict]:
    """Same line shape as parse_lotto_order, but keeps the logo (the text
    between the pick number and the dash) and EVERY mention on the line,
    not just the first — for Anonymous Draft team setup, where one line is
    one team of 1-3 real GMs behind a single logo.

    Returns [{'order_index': int (0-based, by line number), 'logo': str,
    'user_ids': [int, ...]}, ...], sorted by line number."""
    teams: list[tuple[int, str, list[int]]] = []
    for line in text.splitlines():
        m = _LOTTO_LINE_RE.match(line.strip())
        if not m:
            continue
        pos = int(m.group(1))
        logo = m.group(2).strip()
        user_ids = [int(uid) for uid in re.findall(r'<@!?(\d+)>', m.group(3))]
        if user_ids:
            teams.append((pos, logo, user_ids))
    teams.sort(key=lambda t: t[0])
    return [
        {'order_index': i, 'logo': logo, 'user_ids': user_ids}
        for i, (_pos, logo, user_ids) in enumerate(teams)
    ]
