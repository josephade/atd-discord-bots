"""Exercise the deployed findbest callback with captured output, never Discord sends."""
import ast
import asyncio
import json
import os
import re
import unicodedata
from pathlib import Path

import gspread


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class View:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_embed(self):
        return self.kwargs


class Context:
    def typing(self):
        return Typing()

    async def send(self, *, embed, view):
        print(embed['title'])
        print(embed['subtitle'])
        print('First entry:', embed['lines'][0])
        assert '57W' in embed['lines'][0]
        assert 'JSoapz' in embed['lines'][0]
        assert 'ATD 112' in embed['lines'][0]
        print('Verified the live callback ranks JSoapz ATD 112 first. No Discord messages sent.')


async def get_raw():
    client = gspread.service_account_from_dict(
        json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON']),
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    return client.open_by_key(os.environ['SPREADSHEET_ID']).worksheet('Win Sheet').get_all_values()


def main():
    source = ast.parse(Path('/app/bot.py').read_text())
    functions = {'_fold', '_name_variants', '_all_name_variants', '_safe_int',
                 '_safe_pct', '_parse', 'cmd_findbest'}
    constants = {'DRAFTER_ALIASES', '_CASE_SENSITIVE_NAME_OVERRIDES', 'RECENT_COUNT'}
    selected = []
    for node in source.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            node.decorator_list = []
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            selected.append(node)
    import sys
    sys.path.insert(0, '/app')
    from aliases import DRAFTER_ALIASES
    namespace = {'re': re, 'unicodedata': unicodedata, 'DRAFTER_ALIASES': DRAFTER_ALIASES,
                 '_in_channel': lambda ctx: True, '_get_raw': get_raw,
                 '_load_lottos': lambda: json.loads(Path('/data/lottos.json').read_text()),
                 'PlayerListView': View}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'live-findbest', 'exec'), namespace)
    asyncio.run(namespace['cmd_findbest'](Context(), player_name='Sam Jones'))


if __name__ == '__main__':
    main()
