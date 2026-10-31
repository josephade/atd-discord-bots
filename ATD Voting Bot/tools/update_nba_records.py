"""Refresh the bundled, completed 82-game NBA regular-season comparisons."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests


def season_records(year):
    url = f'https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings?season={year}'
    response = requests.get(url, timeout=25)
    response.raise_for_status()
    data = response.json()
    result = []

    def walk(node):
        standings = node.get('standings', {})
        if standings.get('season') == year and standings.get('seasonType') == 2:
            for entry in standings.get('entries', []):
                stats = {s['name']: s.get('value') for s in entry['stats']}
                wins, losses = stats.get('wins'), stats.get('losses')
                if wins is None or losses is None or wins+losses != 82:
                    continue
                team = entry['team']
                name = team['displayName']
                # ESPN team metadata can use today's franchise name for old seasons.
                if team['id'] == '17' and year <= 2012:
                    name = 'New Jersey Nets'
                elif team['id'] == '25' and year <= 2008:
                    name = 'Seattle SuperSonics'
                elif team['id'] == '30' and year <= 2014:
                    name = 'Charlotte Bobcats'
                elif team['id'] == '3' and year <= 2013:
                    name = 'Charlotte Hornets' if year <= 2002 else 'New Orleans/Oklahoma City Hornets' if year in (2006, 2007) else 'New Orleans Hornets'
                elif team['id'] == '29' and year <= 2001:
                    name = 'Vancouver Grizzlies'
                elif team['id'] == '27' and year <= 1997:
                    name = 'Washington Bullets'
                result.append(dict(season=f'{year-1}-{str(year)[-2:]}', team=name,
                                   wins=int(wins), losses=int(losses), source=url))
        for child in node.get('children', []):
            walk(child)
    walk(data)
    if not result:
        raise ValueError(f'No completed 82-game standings returned for {year}')
    return result


if __name__ == '__main__':
    # Shortened seasons intentionally excluded: never equate 7-59 with 7-75.
    years = [y for y in range(1996, 2026) if y not in (1999, 2012, 2020, 2021)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = [r for season in pool.map(season_records, years) for r in season]
    records.append(dict(season='1972-73', team='Philadelphia 76ers', wins=9, losses=73,
                        source='https://api-hub.nba.com/news/history-season-review-1972-73'))
    records.append(dict(season='1992-93', team='Dallas Mavericks', wins=11, losses=71,
                        source='https://cdn.nba.com/teams/legacy/www.mavs.com/wp-content/uploads/2025/02/2024-25-Dallas-Mavericks-Media-Guide-2.pdf'))
    target = Path(__file__).resolve().parents[1]/'nba_records.json'
    target.write_text(json.dumps(records, indent=2)+'\n', encoding='utf-8')
    print(f'Saved {len(records)} verified season records; {len({r["wins"] for r in records})} distinct win totals.')
