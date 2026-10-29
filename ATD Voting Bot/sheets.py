import json
import os
import re
from urllib.parse import urlparse, parse_qs
import gspread


def sheet_reference(url):
    match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', url)
    if not match:
        raise ValueError('Use the full Google Sheets URL, including gid for the response tab.')
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.update(parse_qs(parsed.fragment))
    if 'gid' not in params:
        return match.group(1), None
    try:
        gid = int(params['gid'][0])
    except ValueError:
        raise ValueError('The response tab gid must be numeric.') from None
    if gid < 0:
        raise ValueError('The response tab gid must be nonnegative.')
    return match.group(1), gid


class Sheets:
    def __init__(self):
        self.client = None

    def _worksheet(self, sheet, gid):
        if self.client is None:
            scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
            raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
            if raw:
                self.client = gspread.service_account_from_dict(json.loads(raw), scopes=scopes)
            else:
                self.client = gspread.service_account(filename=os.getenv('SERVICE_ACCOUNT_FILE', 'service_account.json'), scopes=scopes)
            self.client.set_timeout(20)
        book = self.client.open_by_key(sheet)
        return book.get_worksheet(0) if gid is None else book.get_worksheet_by_id(gid)

    def resolve_gid(self, sheet):
        """Resolve the first tab once, then persist its actual ID."""
        return self._worksheet(sheet, None).id

    def read(self, sheet, gid):
        return self._worksheet(sheet, gid).get_all_values()
