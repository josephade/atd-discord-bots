"""Pure Flux price rules: compare only the last two chronological price columns."""
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).replace('’', "'").casefold().split())


def amount(value):
    try:
        number = Decimal(str(value).strip().replace('$', '').replace(',', ''))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def parse_prices(rows, round_number=None):
    if not rows:
        raise ValueError('The Flux price sheet is empty.')
    header = [key(h) for h in rows[0]]
    names = [i for i, h in enumerate(header) if h in ('player', 'player name', 'name')]
    if len(names) != 1 or header.count('value') != 1:
        raise ValueError('The Flux sheet needs Player and Value headers, plus Round N columns after Flux.')
    cols = [(1, header.index('value'))]
    for i, text in enumerate(header):
        match = re.fullmatch(r'round\s+(\d+)', text)
        if match:
            cols.append((int(match[1]), i))
    cols.sort()
    if round_number is not None:
        if round_number not in {number for number, _ in cols}:
            raise ValueError(f'Round {round_number} prices are not available in the configured Flux sheet.')
        cols = [(number, col) for number, col in cols if number <= round_number]
    if len({r for r, _ in cols}) != len(cols):
        raise ValueError('Duplicate Flux round columns.')
    result = {}
    for row in rows[1:]:
        name = str(row[names[0]]).strip() if names[0] < len(row) else ''
        if not name:
            continue
        def cell(col):
            return amount(row[col]) if col < len(row) else None
        # Never bridge a blank or invalid newest column back to an old price.
        record = dict(player=name, round=cols[-1][0], current=cell(cols[-1][1]),
                      previous=cell(cols[-2][1]) if len(cols) > 1 and cols[-1][0] == cols[-2][0]+1 else None)
        existing = result.get(key(name))
        if existing is not None:
            if (existing.get('ambiguous') or
                    (existing['current'], existing['previous']) != (record['current'], record['previous'])):
                existing.update(current=None, previous=None, ambiguous=True)
            continue
        result[key(name)] = record
    return result


def find_player(book, name):
    if key(name) in book:
        if book[key(name)].get('ambiguous'):
            raise ValueError('That player has conflicting duplicate prices in the Flux sheet. Ask a commissioner to correct the rows.')
        return book[key(name)]
    matches = [record for name_key, record in book.items() if key(name) and key(name) in name_key]
    if len(matches) != 1:
        raise ValueError('Use a full player name that uniquely matches the Flux price sheet.')
    if matches[0].get('ambiguous'):
        raise ValueError('That player has conflicting duplicate prices in the Flux sheet.')
    return matches[0]


def threshold(value):
    result = amount(value)
    if result is None:
        raise ValueError('Drop amount must be a finite, non-negative number. Example: !fluxpick Devin Booker 3')
    return float(result)


def qualify(pick, book):
    if 'flux_drop' not in pick:
        return dict(pick)
    try:
        record = find_player(book, pick['player'])
    except ValueError:
        return None
    current, previous = record['current'], record['previous']
    if pick.get('flux_target_round') is not None and record['round'] != pick['flux_target_round']:
        return None
    drop = amount(pick['flux_drop'])
    if current is None or previous is None or drop is None or previous-current < drop:
        return None
    price = int(current) if current == current.to_integral_value() else float(current)
    return dict(pick, player=record['player'], price=price)


def failure_message(pick, book):
    try:
        record = find_player(book, pick['player'])
    except ValueError:
        return f"**{pick['player']}** \u00b7 prices unavailable"
    current, previous = record['current'], record['previous']
    if current is None or previous is None:
        detail = 'prices unavailable'
    elif current > previous:
        detail = f"${previous:g} \u2192 ${current:g} (\u2191${current-previous:g})"
    elif current == previous:
        detail = f"${current:g} unchanged"
    else:
        detail = f"${previous:g} \u2192 ${current:g} (\u2193${previous-current:g})"
    return f"**{pick['player']}** \u00b7 {detail} \u00b7 needed \u2193${pick['flux_drop']:g}"


def saved_message(picks):
    if not picks:
        return "Your updated Flux list is empty. Submit new options with `!fluxpick`."
    lines = [f"**\u2705 Next Flux \u00b7 {len(picks)} option{'s' if len(picks) != 1 else ''}**"]
    lines += [f"{i}. {p['player'][:100]} \u00b7 drop \u2265 ${p['flux_drop']:g}" for i, p in enumerate(picks[:5], 1)]
    if len(picks) > 5:
        lines.append(f"+{len(picks)-5} more \u00b7 `!mylist` to view all")
    lines.append('First eligible option on your turn. Larger drops qualify.')
    return '\n'.join(lines)


def failure_reports(notices):
    header = f'**Flux results \u00b7 {len(notices)} rules removed**\n'
    chunks, chunk = [], header
    for notice in notices:
        line = notice[:1500]+'\n'
        if len(chunk)+len(line) > 1800:
            chunks.append(chunk.rstrip())
            chunk = '**Flux results (continued)**\n'
        chunk += line
    if notices:
        chunks.append(chunk+'\nSet new options with `!fluxpick`.')
    return chunks

