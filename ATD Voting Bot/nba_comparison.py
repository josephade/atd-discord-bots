"""Historical flavour for reveals, using a bundled source-backed record table."""
import json
from pathlib import Path

from reveal import record_text

RECORDS = json.loads(Path(__file__).with_name('nba_records.json').read_text(encoding='utf-8'))


def historical_comparison(row):
    wins, losses = map(int, record_text(row).split('-'))
    exact = [r for r in RECORDS if (r['wins'], r['losses']) == (wins, losses)]
    candidates = exact or RECORDS
    preferred = {41: ('2008-09', 'Chicago Bulls'), 72: ('1995-96', 'Chicago Bulls'),
                 73: ('2015-16', 'Golden State Warriors'), 9: ('1972-73', 'Philadelphia 76ers')}
    chosen = min(candidates, key=lambda r: (
        abs(r['wins']-wins)+abs(r['losses']-losses),
        (r['season'], r['team']) != preferred.get(wins),
        r['team'] != row.get('team'), -int(r['season'][:4]), r['team']))
    source = chosen['source']
    if 'espn.com' in source:
        source = 'https://www.espn.com/nba/standings/_/season/'+source.rsplit('=', 1)[-1]
    team = f"[{chosen['season']} {chosen['team']}](<{source}>)"
    historical_record = f"{chosen['wins']}-{chosen['losses']}"
    if exact:
        if wins < 30:
            return f"You're just as bad as the {team} ({historical_record}). Lock in, bro."
        lead = f'Same record as the {team} ({historical_record}). '
    else:
        lead = f'Closest historical comparison in the record library: the {team} ({historical_record}). '
    if wins >= 50:
        return lead+'Elite territory. Now back it up in the playoffs.'
    if wins == losses:
        return lead+"Perfectly average. Time to push past the middle of the pack."
    if wins > losses:
        return lead+"A winning record, but there's another level. Push for 50."
    return lead+"Below .500. Lock in, bro — you've got work to do."
