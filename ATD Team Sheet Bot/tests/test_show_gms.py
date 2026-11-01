import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import budget_rules


class GMVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in ('cmd_showgms', '_teamshot_caption')]
        for node in nodes:
            node.decorator_list = []
        self.cfg = {'other_setting': 123}
        def save(cfg):
            self.cfg = deepcopy(cfg)
        self.ns = dict(budget_rules=budget_rules, _DEV_SWAP_USER_ID=1,
                       _channel_sheet_map={99: dict(sheet_id='A', tab='ATD 113'),
                                           100: dict(sheet_id='A', tab='ATD 113'),
                                           101: dict(sheet_id='A', tab='ATD 114')},
                       _load_config=lambda: deepcopy(self.cfg), _save_config=Mock(side_effect=save),
                       _team_owners={'10': 'Cavaliers', '11': 'Cavaliers', '12': 'Hofstra'},
                       _command_input_error=lambda *args, **kw: 'Usage: !showgms on/off')
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), self.ns)
        self.ctx = NS(author=NS(id=1), channel=NS(id=99), send=AsyncMock())

    async def test_hidden_by_default_and_on_off_persist_per_sheet_across_channels(self):
        caption = self.ns['_teamshot_caption']
        self.assertEqual(caption('Cavaliers', 99), '**Cavaliers**')
        await self.ns['cmd_showgms'](self.ctx, 'on')
        self.assertIn('<@10> / <@11>', caption('Cavaliers', 100))
        self.assertNotIn('GM:', caption('Cavaliers', 101))
        # Reusing a channel for another tab cannot carry the visibility along.
        self.ns['_channel_sheet_map'][99]['tab'] = 'ATD 114'
        self.assertNotIn('GM:', caption('Cavaliers', 99))
        self.ctx.channel.id = 100
        await self.ns['cmd_showgms'](self.ctx, 'off')
        self.assertNotIn('GM:', caption('Cavaliers', 100))
        self.assertEqual(self.cfg['other_setting'], 123)

    async def test_nonowner_bad_input_and_unmapped_channel_cannot_enable(self):
        self.ctx.author.id = 2
        await self.ns['cmd_showgms'](self.ctx, 'on')
        self.ctx.author.id = 1
        await self.ns['cmd_showgms'](self.ctx, 'yes')
        self.ctx.channel.id = 999
        await self.ns['cmd_showgms'](self.ctx, 'on')
        self.ns['_save_config'].assert_not_called()

    async def test_no_argument_reports_without_changing_visibility(self):
        await self.ns['cmd_showgms'](self.ctx)
        self.assertIn('hidden', self.ctx.send.call_args.args[0])
        self.ns['_save_config'].assert_not_called()
