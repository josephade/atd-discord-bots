"""Apply the commissioner's ATD 112 standings to the Win Bot's source sheet."""
import argparse
import json
import os
import re
from pathlib import Path

import gspread
from gspread.utils import rowcol_to_a1, a1_to_rowcol

SHEET = '1-8H9CcKcnRE6AfbrutnwNbQymSj7LR26l1yBiSI7zKw'
# Confirmed current Win Sheet names; every co-drafter receives the team record.
WINS = {
    'Ice': 25, 'Cruz': 26, 'Knave': 27, 'AJ(Tr1p)': 29,
    'Morgan': 31, 'Margiela(Smoggy)': 33, 'Elpolo': 34,
    'Shadows': 34, 'Joko': 34, 'Caliph': 36, 'Qrganic': 38,
    'Silveryo': 39, 'CCarp': 40, 'Pepe': 40,
    'Boneless': 41, 'Kay': 41, 'Pizards': 41, 'Mk(Clips)': 41,
    'Ryadad': 41, 'RussellWestbrookFan': 43, 'Liam(Hakeem Lowry)': 43,
    'Mondon': 44, 'Hayden': 44, 'Blocked By Bam': 44, 'Po': 44,
    'Itsyaboi.idk': 44, 'Klaw': 45, 'Matisse': 45, 'Precision80': 46,
    'Element': 47, 'Crispy': 49, 'Goobynky': 50, 'Bony': 51,
    'Sorslab': 51, 'Theo': 51, 'Admiral Ackbar': 55,
    'TelluricSolid31': 55, 'JSoapz': 57,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--deployed', action='store_true')
    args = parser.parse_args()
    base = Path(__file__).resolve().parent.parent
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    if args.deployed:
        client = gspread.service_account_from_dict(
            json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON']), scopes=scopes)
    else:
        client = gspread.service_account(
            filename=str(base / 'ATD Voting Bot/service_account.json'), scopes=scopes)
    client.set_timeout(30)
    book = client.open_by_key(SHEET)
    ws = book.worksheet('Win Sheet')
    before = ws.get_all_values(value_render_option='FORMULA')
    headers = before[0]
    names = {}
    for rownum, row in enumerate(before[1:], 2):
        if row and str(row[0]).strip():
            key = str(row[0]).strip()
            if key in names:
                raise ValueError(f'Duplicate profile: {key}')
            names[key] = rownum
    missing = set(WINS) - names.keys()
    if missing:
        raise ValueError(f'Unmatched profiles: {sorted(missing)}')
    if 'Draft 112' in headers:
        col = headers.index('Draft 112')
        for name, wins in WINS.items():
            row = before[names[name] - 1]
            if col >= len(row) or str(row[col]) != str(wins):
                raise ValueError(f'Existing Draft 112 differs for {name}; no changes made')
        print(f'Already applied: all {len(WINS)} profiles match. No changes made.')
        return
    col = headers.index('Total')
    assert headers[col - 1].strip() == 'Draft 111'
    summary = headers[col:]
    assert summary == ['Total', '%', 'Wins over .500', 'Max W', 'Min W', '# of Drafts']
    print(f'Validated {len(WINS)} profiles; adding Draft 112 before Total.')
    if not args.apply:
        return
    rawdir = Path('/tmp/atd-112-win-backup') if args.deployed else base / 'ATD History Research/raw'
    rawdir.mkdir(exist_ok=True)
    (rawdir / 'win-sheet-before-112.json').write_text(json.dumps(before), encoding='utf-8')

    def shifted_formula(formula):
        # Insert shifts summary references; also expand each draft range to include 112.
        def shift(match):
            letters, row = match.groups()
            _, oldcol = a1_to_rowcol(letters.replace('$', '') + '1')
            if oldcol > col:
                letters = rowcol_to_a1(1, oldcol + 1)[:-1]
                if match.group(1).startswith('$'):
                    letters = '$' + letters
            return letters + row
        formula = re.sub(r'(\$?[A-Z]+)(\$?\d+)', shift, formula)
        oldend = rowcol_to_a1(1, col)[:-1]
        newend = rowcol_to_a1(1, col + 1)[:-1]
        return re.sub(r':' + oldend + r'(\d+)', ':' + newend + r'\1', formula)

    sheet_id = ws.id
    column_rows = [{'values': [{'userEnteredValue': {'stringValue': 'Draft 112'}}]}]
    by_row = {names[name]: wins for name, wins in WINS.items()}
    for rownum in range(2, len(before) + 1):
        cell = {'userEnteredValue': {'numberValue': by_row[rownum]}} if rownum in by_row else {}
        column_rows.append({'values': [cell]})
    requests = [
        {'insertDimension': {'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS',
                                      'startIndex': col, 'endIndex': col + 1},
                             'inheritFromBefore': True}},
        {'copyPaste': {'source': {'sheetId': sheet_id, 'startRowIndex': 0,
                                 'endRowIndex': len(before), 'startColumnIndex': col - 1,
                                 'endColumnIndex': col},
                       'destination': {'sheetId': sheet_id, 'startRowIndex': 0,
                                       'endRowIndex': len(before), 'startColumnIndex': col,
                                       'endColumnIndex': col + 1},
                       'pasteType': 'PASTE_FORMAT'}},
        {'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': 0, 'columnIndex': col},
                         'rows': column_rows, 'fields': 'userEnteredValue'}},
    ]
    for rownum, row in enumerate(before[1:], 2):
        for offset, value in enumerate(row[col:]):
            if isinstance(value, str) and value.startswith('='):
                requests.append({'updateCells': {
                    'start': {'sheetId': sheet_id, 'rowIndex': rownum - 1,
                              'columnIndex': col + 1 + offset},
                    'rows': [{'values': [{'userEnteredValue': {
                        'formulaValue': shifted_formula(value)}}]}],
                    'fields': 'userEnteredValue'}})
    # Read once more to reject changes made since validation.
    if ws.get_all_values(value_render_option='FORMULA') != before:
        raise RuntimeError('Sheet changed during preparation; rerun without applying stale data')
    book.batch_update({'requests': requests})
    after = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
    formulas = ws.get_all_values(value_render_option='FORMULA')
    assert after[0][col] == 'Draft 112'
    for name, wins in WINS.items():
        rownum = names[name]
        row = after[rownum - 1]
        assert row[col] == wins, name
        total = sum(v for v in row[1:col + 1] if isinstance(v, (int, float)))
        assert row[col + 1] == total, f'{name}: total mismatch'
        assert row[col + 6] == sum(isinstance(v, (int, float)) for v in row[1:col + 1]), name
    for old, new in zip(before, formulas):
        assert old[:col] == new[:col], 'Historical cells changed'
    (rawdir / 'win-sheet-after-112.json').write_text(json.dumps(after), encoding='utf-8')
    print(f'Updated and verified {len(WINS)} profiles, totals and draft counts.')
    print(f'https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid={sheet_id}')


if __name__ == '__main__':
    main()
