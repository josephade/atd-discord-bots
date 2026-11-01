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
    occupied = any(str(cell).strip() for row in rows for cell in row[start-1:start-1+count])
    if occupied:
        # Keep adjacent notes/data intact rather than overwriting them.
        ws.spreadsheet.batch_update({'requests': [{'insertDimension': {
            'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': start-1, 'endIndex': start-1+count},
            'inheritFromBefore': True,
        }}]})
    elif ws.col_count < start+count-1:
        ws.add_cols(start+count-1-ws.col_count)
    return start
