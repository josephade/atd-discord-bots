#!/usr/bin/env python3
"""
Generate players.json for the ATD Advanced Stats Bot.
Uses nba_api static data for the full player list, then fetches
2024-25 team assignments from LeagueDashPlayerStats.

Run: python generate-data.py
Output: players.json
"""

import json
import unicodedata
import re
import time
import os

def make_slug(name):
    """Convert a player name to a URL slug.
    e.g. 'Nikola Jokic' -> 'nikola-jokic'
         'LeBron James' -> 'lebron-james'
         "De'Aaron Fox" -> 'deaaron-fox'
    """
    # Normalize unicode (decompose accented chars)
    normalized = unicodedata.normalize('NFD', name)
    # Strip combining diacritical marks
    ascii_str = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
    # Lowercase
    ascii_str = ascii_str.lower()
    # Remove apostrophes and dots
    ascii_str = ascii_str.replace("'", '').replace('.', '')
    # Replace spaces with hyphens
    ascii_str = ascii_str.replace(' ', '-')
    # Remove any remaining non-alphanumeric chars except hyphens
    ascii_str = re.sub(r'[^a-z0-9-]', '', ascii_str)
    # Collapse multiple hyphens
    ascii_str = re.sub(r'-+', '-', ascii_str).strip('-')
    return ascii_str


def main():
    print("Loading nba_api static player list...")
    from nba_api.stats.static import players as static_players
    all_players = static_players.get_players()
    print(f"  Found {len(all_players)} total players.")

    # Build base dict keyed by player_id
    player_map = {}
    for p in all_players:
        player_map[p['id']] = {
            'id': p['id'],
            'full_name': p['full_name'],
            'slug': make_slug(p['full_name']),
            'team': None,
            'is_active': p['is_active'],
        }

    print("Fetching 2024-25 team assignments from LeagueDashPlayerStats...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "Connection": "keep-alive",
    }

    # Retry up to 3 times
    dash_data = None
    for attempt in range(1, 4):
        try:
            from nba_api.stats.endpoints import leaguedashplayerstats
            dash = leaguedashplayerstats.LeagueDashPlayerStats(
                season="2024-25",
                per_mode_detailed="PerGame",
                headers=headers,
                timeout=60,
            )
            dash_data = dash.get_data_frames()[0]
            print(f"  Fetched {len(dash_data)} active player rows.")
            break
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            if attempt < 3:
                print(f"  Retrying in 5 seconds...")
                time.sleep(5)

    if dash_data is not None:
        for _, row in dash_data.iterrows():
            pid = int(row['PLAYER_ID'])
            team_abbr = str(row.get('TEAM_ABBREVIATION', '') or '').strip()
            if pid in player_map and team_abbr:
                player_map[pid]['team'] = team_abbr
    else:
        print("  WARNING: Could not fetch team assignments. Team field will be null.")

    # Build sorted output list — active players first, then inactive
    result = sorted(
        player_map.values(),
        key=lambda p: (not p['is_active'], p['full_name'])
    )

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'players.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    active_count = sum(1 for p in result if p['is_active'])
    with_team = sum(1 for p in result if p['team'])
    print(f"\nDone! Saved {len(result)} players to players.json")
    print(f"  Active: {active_count}  |  With team assignment: {with_team}")


if __name__ == '__main__':
    main()
