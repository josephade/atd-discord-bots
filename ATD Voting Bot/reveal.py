"""Final sheet standings and conservative Discord GM attribution."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from votes import normalize, resolve_member


def standings(rows):
    tables = []
    for index, row in enumerate(rows):
        for col, value in enumerate(row):
            if normalize(value) == 'team' and [normalize(c) for c in row[col+1:col+4]] == ['wins', 'losses', 'gm']:
                tables.append((index, col))
    if len(tables) != 1:
        raise ValueError('Expected one final standings table headed Team, Wins, Losses, GM in the voting tab.')
    index, col = tables[0]
    result, seen = [], set()
    for row in rows[index+1:]:
        cells = [(row[i].strip() if i < len(row) else '') for i in range(col, col+4)]
        if not any(cells):
            break
        team, wins, losses, gm = cells
        team = team.strip('[] ')
        try:
            w, l = Decimal(wins), Decimal(losses)
        except InvalidOperation:
            raise ValueError(f'Final standings for {team or "an unnamed team"} have missing or invalid wins/losses.') from None
        if not team or not w.is_finite() or not l.is_finite() or w < 0 or l < 0:
            raise ValueError('Final standings contain an invalid team or record.')
        if normalize(team) in seen:
            raise ValueError(f'Duplicate standings entry for {team}.')
        seen.add(normalize(team))
        result.append(dict(team=team, wins=str(w), losses=str(l), gm=gm))
    if not result:
        raise ValueError('The final standings table has no teams yet.')
    # Exact formula values determine seeds; sheet order breaks exact ties.
    result.sort(key=lambda r: Decimal(r['wins']), reverse=True)
    return [dict(r, seed=i+1) for i, r in enumerate(result)]


def record_text(row):
    # Keep displayed records consistent with the formula's season total.
    wins, losses = Decimal(row['wins']), Decimal(row['losses'])
    w = int(wins.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    total = wins+losses
    l = int(total)-w if total == total.to_integral_value() else int(losses.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return f'{w}-{l}'


def ordinal(number):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    return f'{number}{suffix}'


def gm_ids(row, members, aliases, explicit=()):
    if explicit:
        return list(dict.fromkeys(explicit))
    def identify(name):
        # Confirmed IDs remain valid historical identities after leaving a guild.
        if normalize(name) in aliases:
            return int(aliases[normalize(name)])
        member = resolve_member(name, members, aliases)
        return member.id if member else None

    whole = identify(row['gm'])
    if whole:
        return [whole]
    parts = re.split(r'\s*(?:/|,|&|\band\b)\s*', row['gm'], flags=re.I)
    resolved = [identify(p) for p in parts if p.strip()]
    # Never partially credit an ambiguous co-GM cell.
    return list(dict.fromkeys(resolved)) if resolved and all(resolved) else []


def comparison(current, previous, user_id):
    delta = Decimal(current['wins'])-Decimal(previous['wins'])
    amount = format(abs(delta).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP), 'f').removesuffix('.0')
    change = f'improved by {amount}' if delta > 0 else f'decreased by {amount}' if delta < 0 else 'unchanged at the same number of'
    return f"<@{user_id}>: {change} wins compared with ATD {previous['draft']} (formula values)."
