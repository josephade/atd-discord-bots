"""A priority formatting rule keeps drafted rows blue across future columns."""
MARKER = '=N("ATD Sheet Bot drafted rows")=0'


def managed_rule_requests(metadata, sheet_id, rows, color, *, remove=False,
                          replace=False, start_column=0):
    sheet = next(s for s in metadata['sheets'] if s['properties']['sheetId'] == sheet_id)
    rules = sheet.get('conditionalFormats', [])
    managed = []
    existing = set()
    for index, rule in enumerate(rules):
        condition = rule.get('booleanRule', {}).get('condition', {})
        if condition.get('type') != 'CUSTOM_FORMULA' or condition.get('values') != [
                {'userEnteredValue': MARKER}]:
            continue
        managed.append(index)
        for region in rule.get('ranges', []):
            existing.update(range(region['startRowIndex'] + 1, region['endRowIndex'] + 1))
    rows = {int(row) for row in rows if int(row) > 1}
    selected = rows if replace else existing - rows if remove else existing | rows
    requests = [{'deleteConditionalFormatRule': {'sheetId': sheet_id, 'index': i}}
                for i in reversed(managed)]
    if selected:
        requests.append({'addConditionalFormatRule': {'index': 0, 'rule': {
            'ranges': [{'sheetId': sheet_id, 'startRowIndex': row - 1,
                        'endRowIndex': row, 'startColumnIndex': start_column}
                       for row in sorted(selected)],
            'booleanRule': {'condition': {'type': 'CUSTOM_FORMULA',
                                         'values': [{'userEnteredValue': MARKER}]},
                            'format': {'backgroundColor': color}},
        }}})
    return requests
