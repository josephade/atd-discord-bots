"""Append Flux rounds to the price table, not the allocated sheet grid."""
import re


def column_letter(index):
    text = ''
    while index:
        index, digit = divmod(index-1, 26)
        text = chr(65+digit)+text
    return text


def prepare_round_columns(ws, rows, count=1):
    header = rows[0]
    value = [i+1 for i, name in enumerate(header) if name.strip().lower() == 'value']
    if len(value) != 1:
        raise ValueError('The price table must have one Value column.')
    prices = value+[i+1 for i, name in enumerate(header) if re.fullmatch(r'round\s+\d+', name.strip(), re.I)]
    start = max(prices)+1
    # Worksheet.col_count is cached and may be wrong after a commissioner
    # deletes columns in Sheets. Never use it to decide whether E exists.
    metadata = ws.spreadsheet.fetch_sheet_metadata(params={'fields': 'sheets(properties(sheetId,gridProperties(columnCount)))'})
    properties = next((sheet['properties'] for sheet in metadata.get('sheets', [])
                       if sheet['properties']['sheetId'] == ws.id), None)
    if properties is None:
        raise ValueError('Could not verify the live sheet grid. No Flux prices were written.')
    live_columns = properties['gridProperties']['columnCount']
    required_columns = start+count-1
    if live_columns < required_columns:
        ws.resize(cols=required_columns)
    occupied = any(str(cell).strip() for row in rows for cell in row[start-1:start-1+count])
    if occupied:
        # Keep adjacent notes/data intact rather than overwriting them.
        ws.spreadsheet.batch_update({'requests': [{'insertDimension': {
            'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': start-1, 'endIndex': start-1+count},
            'inheritFromBefore': True,
        }}]})
    return start
