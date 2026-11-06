"""Scoped, one-time commissioner price overrides applied before Flux is frozen."""
import json
import math
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def load_plan(path):
    file = Path(path)
    return json.loads(file.read_text(encoding='utf-8')) if file.exists() else None


def normalize(name):
    return ''.join(c for c in unicodedata.normalize('NFD', name).casefold() if c.isalnum())


def plan_values(plan, rows, *, channel_id, spreadsheet_id, worksheet_gid, round_number, vol):
    if not plan or plan.get('status') != 'pending':
        return {}
    scope = (str(channel_id), str(spreadsheet_id), str(worksheet_gid), round_number)
    target = (str(plan['channel_id']), str(plan['spreadsheet_id']), str(plan['worksheet_gid']), plan['round'])
    if scope != target:
        return {}
    if vol != plan['vol']:
        raise ValueError('The queued price overrides require !flux %s.' % plan['vol'])
    header = rows[0]
    player_col = header.index('Player')
    previous_col = header.index('Round %s' % (round_number - 1))
    result = {}
    for item in plan['players']:
        matches = [(i, row) for i, row in enumerate(rows[1:], start=1)
                   if len(row) > player_col and normalize(row[player_col]) == normalize(item['player'])]
        if len(matches) != 1:
            raise ValueError('Override player must match exactly one sheet row: ' + item['player'])
        index, row = matches[0]
        if index in result:
            raise ValueError('Duplicate player in queued price overrides.')
        previous = float(str(row[previous_col]).replace('$', '').replace(',', '').strip())
        if math.floor(previous + .5) != item['previous_price']:
            raise ValueError('Round %s price changed for %s; check queued overrides before Flux.' % (round_number - 1, item['player']))
        value = float(item['value'])
        if not math.isfinite(value) or value < 0 or value.is_integer() or math.floor(value + .5) != item['target_price']:
            raise ValueError('Invalid queued decimal price for ' + item['player'])
        # Signed drop: negative values represent requested increases.
        if item['previous_price'] - item['target_price'] != item['drop'] or abs(item['drop']) > vol:
            raise ValueError('Invalid queued price change for ' + item['player'])
        result[index] = value
    return result


def mark_applied(path, plan, column):
    current = load_plan(path)
    if not current or current.get('id') != plan['id']:
        raise ValueError('The queued override plan changed while Flux was running.')
    current.update(status='applied', applied_column=column, applied_at=datetime.now(timezone.utc).isoformat())
    file = Path(path)
    temp = file.with_suffix('.tmp')
    temp.write_text(json.dumps(current, indent=2), encoding='utf-8')
    os.replace(temp, file)
