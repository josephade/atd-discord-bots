"""Refresh the bundled !available history from Team Sheet ATD 15–112 tabs.

Run from this bot's directory with its normal Google Sheets credentials.
Only values are read; spreadsheet contents are never changed.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from config import SERVICE_ACCOUNT_FILE, SPREADSHEET_ID
from draft_history import summarize_team_sheet


def main():
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    client = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scopes))
    client.session.timeout = 45
    book = client.open_by_key(SPREADSHEET_ID)
    titles = [ws.title for ws in book.worksheets() if (
        (match := re.match(r'^ATD\s*(\d+)\b', ws.title, re.IGNORECASE))
        and 15 <= int(match.group(1)) <= 112
    )]
    tabs = {}
    for start in range(0, len(titles), 10):
        chunk = titles[start:start + 10]
        result = book.values_batch_get(["'" + title.replace("'", "''") + "'" for title in chunk])
        for title, values in zip(chunk, result['valueRanges']):
            tabs[title] = values.get('values', [])
    summary = summarize_team_sheet(tabs)
    if not summary['total_drafts'] or not summary['player_counts']:
        raise RuntimeError('No draft rosters found; preserving the existing history.')
    summary.update(source=f'Team Sheet:{book.id}', snapshot_date=datetime.now(timezone.utc).date().isoformat())
    target = Path(__file__).with_name('player_draft_history.json')
    target.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'Refreshed {summary["total_drafts"]} drafts across {len(titles)} Team Sheet tabs.')


if __name__ == '__main__':
    main()
