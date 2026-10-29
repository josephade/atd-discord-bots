"""Player appearance counts from historical Team Sheet roster tabs."""

import json
from pathlib import Path
import re
import unicodedata


def player_key(name: str) -> str:
    folded = unicodedata.normalize('NFKD', name.casefold())
    folded = ''.join(c for c in folded if not unicodedata.combining(c))
    words = re.sub(r'[^a-z0-9]+', ' ', folded).split()
    # Historical rosters omit "Sr."; retain "Jr." to distinguish sons.
    if words and words[-1] == 'sr':
        words.pop()
    return ' '.join(words)


def summarize_team_sheet(tabs: dict) -> dict:
    """Count roster appearances across ATD 15–112, grouping tabs by draft number.

    Year and two-peak Off/Def headers identify roster columns. A roster can
    have a blank row between starters and bench, so don't stop at that gap.
    """
    counts = {}
    draft_numbers = set()
    source_tabs = []
    for title, rows in tabs.items():
        match = re.match(r'^ATD\s*(\d+)\b', title, re.IGNORECASE)
        if not match or not 15 <= int(match.group(1)) <= 112:
            continue
        draft_numbers.add(int(match.group(1)))
        source_tabs.append(title)
        for r, row in enumerate(rows):
            for col in range(len(row) - 1):
                label = row[col + 1].strip().lower()
                if label not in ('year', 'o', 'off'):
                    continue
                if label != 'year' and (
                    col + 2 >= len(row) or row[col + 2].strip().lower() not in ('d', 'def')
                ):
                    continue
                players = set()
                for player_row in rows[r + 1:r + 13]:
                    if col + 1 < len(player_row) and player_row[col + 1].strip().lower() in ('year', 'o', 'off'):
                        break
                    if col < len(player_row):
                        key = player_key(player_row[col])
                        if key and any(c.isalpha() for c in key):
                            players.add(key)
                for key in players:
                    counts[key] = counts.get(key, 0) + 1
    return {
        'total_drafts': len(draft_numbers),
        'draft_numbers': sorted(draft_numbers),
        'source_tabs': source_tabs,
        'player_counts': counts,
    }


def load_history() -> dict:
    path = Path(__file__).with_name('player_draft_history.json')
    return json.loads(path.read_text(encoding='utf-8'))
