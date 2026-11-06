"""Copy the 313 voting players from the ADP sheet into players.json.

Run this once before launch. The site seeds its database from players.json, and an
existing database is left alone, so later sheet edits can't change a vote in progress.
"""
import csv
import io
import json
import os
import sys
import urllib.request

SHEET_ID = '1CQyO93HKc5VlXsqS48dnPlkac153TDDoUsiqyhJwwOI'
SHEET_GID = '1913792129'
CSV_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}'
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'players.json')

PLAYER_COUNT = 313
FIRST_PLAYER = 'Michael Jordan'
LAST_PLAYER = 'Ben Simmons'


def parse_price(text):
    return int(text.strip().replace('$', '').replace(',', ''))


def parse_players(csv_text):
    reader = csv.reader(io.StringIO(csv_text))
    header = [cell.strip().lower() for cell in next(reader)]
    try:
        position_col = header.index('position')
        player_col = header.index('player')
        adp_col = header.index('adp')
        value_col = header.index('value')
    except ValueError as exc:
        raise ValueError(f'The sheet is missing a column: {exc}') from exc

    players = []
    for sheet_row, row in enumerate(reader, start=2):
        if len(players) == PLAYER_COUNT:
            break
        players.append(dict(
            sheet_row=sheet_row,
            name=row[player_col].strip(),
            position=row[position_col].strip(),
            # A stray $ appears on at least one ADP cell (Bernard King).
            adp=float(row[adp_col].strip().replace('$', '')),
            current_price=parse_price(row[value_col]),
        ))

    if len(players) != PLAYER_COUNT:
        raise ValueError(f'Expected {PLAYER_COUNT} players, found {len(players)}.')
    if players[0]['name'] != FIRST_PLAYER:
        raise ValueError(f"Row 2 should be {FIRST_PLAYER}, found {players[0]['name']!r}.")
    if players[-1]['name'] != LAST_PLAYER:
        raise ValueError(f"Row {PLAYER_COUNT + 1} should be {LAST_PLAYER}, found {players[-1]['name']!r}.")
    names = [player['name'] for player in players]
    if len(set(names)) != len(names) or not all(names):
        raise ValueError('Player names must be present and unique.')
    if any(player['current_price'] < 1 for player in players):
        raise ValueError('Every current price must be at least $1.')
    return players


def main():
    with urllib.request.urlopen(CSV_URL, timeout=30) as response:
        csv_text = response.read().decode('utf-8')
    players = parse_players(csv_text)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as handle:
        json.dump(players, handle, indent=1, ensure_ascii=False)
        handle.write('\n')
    print(f"Wrote {len(players)} players: {players[0]['name']} to {players[-1]['name']}.")


if __name__ == '__main__':
    sys.exit(main())
