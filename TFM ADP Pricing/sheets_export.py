"""Rebuild the separate results spreadsheet. This never touches the live ADP sheet."""
import json

import gspread

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SUMMARY_HEADER_ROW = 3
LOOKUP_HEADER_ROW = 3
LOOKUP_ROWS = 110
TAB_NAMES = ['Summary', 'Biggest Gaps', 'Player Lookup', 'Votes', 'Voters']


def configured(settings):
    return bool(settings.export_spreadsheet_id and (settings.service_account_json or settings.service_account_file))


def _blank(value):
    return '' if value is None else value


def lookup_formula():
    # Double quotes around the name keep players like Shaquille O'Neal working.
    return (
        '=QUERY(Votes!A:E,"select D, count(D) where B = """&B1&""" and D is not null '
        'group by D order by D label D \'Price\', count(D) \'Votes\'",1)'
    )


def build_tabs(players, gaps, votes, voters, exported_at):
    """Plain rows for each tab. Written as raw values, so a username can't become a formula."""
    summary = [
        ['Exported at', exported_at, 'These figures describe votes. They are not new prices.'],
        [],
        ['Player', 'Current price', 'Price votes', "Don't know", 'Median', 'Average', 'Low', 'High', 'Median minus current'],
    ]
    for row in sorted(players, key=lambda r: (-r['current_price'], r['id'])):
        summary.append([
            row['name'], row['current_price'], row['votes'], row['dont_know'], _blank(row['median']),
            _blank(row['average']), _blank(row['low']), _blank(row['high']), _blank(row['gap']),
        ])

    biggest = [['Direction', 'Player', 'Current price', 'Median', 'Median minus current', 'Price votes']]
    for label, rows in (('Most overpriced', gaps['overpriced']), ('Most underpriced', gaps['underpriced'])):
        for row in rows:
            biggest.append([label, row['name'], row['current_price'], row['median'], row['gap'], row['votes']])

    default_player = players[0]['name'] if players else ''
    lookup = [['Player', default_player, 'Pick a player from the dropdown in B1.']]

    vote_rows = [['Username', 'Player', 'Answer type', 'Price', 'Time']]
    for row in votes:
        vote_rows.append([row['username'], row['player'], row['answer_type'], _blank(row['price']), row['time']])

    voter_rows = [['Username', 'Answered', 'Votes in results', "Don't know", 'Finished', 'Flags']]
    for row in voters:
        voter_rows.append([
            row['username'], row['answered'], row['votes_in_results'], row['dont_know'],
            'Yes' if row['finished'] else 'No', ', '.join(flag['label'] for flag in row['flags']),
        ])

    return {
        'Summary': summary,
        'Biggest Gaps': biggest,
        'Player Lookup': lookup,
        'Votes': vote_rows,
        'Voters': voter_rows,
    }


def replace_tabs_requests(existing, tabs):
    """Atomically swap old tabs for new ones, so the file always keeps at least one sheet."""
    requests = []
    old_ids = []
    for sheet in existing:
        props = sheet['properties']
        if props['title'] in TAB_NAMES:
            old_ids.append(props['sheetId'])
            requests.append({'updateSheetProperties': {
                'properties': {'sheetId': props['sheetId'], 'title': f"_old_{props['sheetId']}"},
                'fields': 'title',
            }})
    for index, name in enumerate(TAB_NAMES):
        rows = max(len(tabs[name]) + 10, LOOKUP_ROWS if name == 'Player Lookup' else 0)
        columns = max(26 if name in {'Biggest Gaps', 'Player Lookup'} else 0, 12)
        requests.append({'addSheet': {'properties': {
            'title': name,
            'index': index,
            'gridProperties': {'rowCount': rows, 'columnCount': columns},
        }}})
    for sheet_id in old_ids:
        requests.append({'deleteSheet': {'sheetId': sheet_id}})
    return requests


def _source(sheet_id, start_row, end_row, column):
    return {'sourceRange': {'sources': [{
        'sheetId': sheet_id, 'startRowIndex': start_row, 'endRowIndex': end_row,
        'startColumnIndex': column, 'endColumnIndex': column + 1,
    }]}}


def _chart(sheet_id, title, chart_type, domain, series, anchor_column, axis_titles):
    bottom_title, left_title = axis_titles
    return {'addChart': {'chart': {
        'spec': {
            'title': title,
            'basicChart': {
                'chartType': chart_type,
                'legendPosition': 'NO_LEGEND',
                'headerCount': 1,
                'axis': [
                    {'position': 'BOTTOM_AXIS', 'title': bottom_title},
                    {'position': 'LEFT_AXIS', 'title': left_title},
                ],
                'domains': [{'domain': domain}],
                'series': [{'series': series, 'targetAxis': 'BOTTOM_AXIS' if chart_type == 'BAR' else 'LEFT_AXIS'}],
            },
        },
        'position': {'overlayPosition': {
            'anchorCell': {'sheetId': sheet_id, 'rowIndex': 1, 'columnIndex': anchor_column},
            'widthPixels': 760,
            'heightPixels': 760 if chart_type == 'BAR' else 420,
        }},
    }}}


def format_requests(sheet_ids, tabs):
    gaps_id = sheet_ids['Biggest Gaps']
    lookup_id = sheet_ids['Player Lookup']
    summary_players = len(tabs['Summary']) - SUMMARY_HEADER_ROW
    gap_rows = len(tabs['Biggest Gaps'])

    requests = []
    header_rows = {'Summary': SUMMARY_HEADER_ROW, 'Biggest Gaps': 1, 'Player Lookup': LOOKUP_HEADER_ROW, 'Votes': 1, 'Voters': 1}
    for name, header_row in header_rows.items():
        requests.append({'updateSheetProperties': {
            'properties': {'sheetId': sheet_ids[name], 'gridProperties': {'frozenRowCount': header_row}},
            'fields': 'gridProperties.frozenRowCount',
        }})
        requests.append({'repeatCell': {
            'range': {
                'sheetId': sheet_ids[name], 'startRowIndex': header_row - 1, 'endRowIndex': header_row,
            },
            'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
            'fields': 'userEnteredFormat.textFormat.bold',
        }})
        requests.append({'autoResizeDimensions': {'dimensions': {
            'sheetId': sheet_ids[name], 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 9,
        }}})

    requests.append({'setDataValidation': {
        'range': {'sheetId': lookup_id, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': 1, 'endColumnIndex': 2},
        'rule': {
            'condition': {'type': 'ONE_OF_RANGE', 'values': [{
                'userEnteredValue': f'=Summary!$A${SUMMARY_HEADER_ROW + 1}:$A${SUMMARY_HEADER_ROW + summary_players}',
            }]},
            'showCustomUi': True,
            'strict': True,
        },
    }})
    requests.append(_chart(
        gaps_id, 'Biggest gaps: median vote minus current price ($)', 'BAR',
        _source(gaps_id, 0, gap_rows, 1), _source(gaps_id, 0, gap_rows, 4), 7,
        ('Median minus current price ($)', 'Player'),
    ))
    requests.append(_chart(
        lookup_id, 'Votes at each price', 'COLUMN',
        _source(lookup_id, LOOKUP_HEADER_ROW - 1, LOOKUP_ROWS, 0),
        _source(lookup_id, LOOKUP_HEADER_ROW - 1, LOOKUP_ROWS, 1), 3,
        ('Price ($)', 'Votes'),
    ))
    return requests


def client_for(settings):
    if settings.service_account_json:
        return gspread.service_account_from_dict(json.loads(settings.service_account_json), scopes=SCOPES)
    return gspread.service_account(filename=settings.service_account_file, scopes=SCOPES)


def export_to_sheets(settings, tabs):
    """Blocking export: call it from a worker thread. Returns the spreadsheet's URL."""
    spreadsheet = client_for(settings).open_by_key(settings.export_spreadsheet_id)
    existing = spreadsheet.fetch_sheet_metadata()['sheets']
    swap = spreadsheet.batch_update({'requests': replace_tabs_requests(existing, tabs)})
    sheet_ids = {
        reply['addSheet']['properties']['title']: reply['addSheet']['properties']['sheetId']
        for reply in swap['replies'] if 'addSheet' in reply
    }

    # RAW keeps usernames as plain text. Only the fixed lookup formula is entered as a formula.
    spreadsheet.values_batch_update({
        'valueInputOption': 'RAW',
        'data': [{'range': f"'{name}'!A1", 'values': rows} for name, rows in tabs.items()],
    })
    spreadsheet.values_update(
        f"'Player Lookup'!A{LOOKUP_HEADER_ROW}",
        params={'valueInputOption': 'USER_ENTERED'},
        body={'values': [[lookup_formula()]]},
    )
    spreadsheet.batch_update({'requests': format_requests(sheet_ids, tabs)})
    return spreadsheet.url
