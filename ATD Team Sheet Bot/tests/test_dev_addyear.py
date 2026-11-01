import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock


class DeveloperYearTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_devaddyear')
        node.decorator_list = []
        self.edit = AsyncMock()
        self.ns = dict(_DEV_SWAP_USER_ID=1, _addyear_roster=self.edit,
                       _resolve_team_from_emoji=lambda ref: 'Iowa Hawkeyes' if ref == '<:Hawkeyes:123>' else None,
                       EMOJI_TEAM_MAP={'Hawkeyes': 'Iowa Hawkeyes'},
                       _command_input_error=lambda ctx: 'Usage: !devaddyear <team> / <player> / <year>')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), self.ns)
        self.ctx = NS(author=NS(id=1), send=AsyncMock())

    async def test_both_screenshot_formats_and_full_team_name_are_supported(self):
        for text in ('<:Hawkeyes:123> Magic Johnson 2015', '<:Hawkeyes:123> / Magic Johnson / 2015',
                     ':Hawkeyes: / Magic Johnson / 2015', 'Iowa Hawkeyes / Magic Johnson / 2015'):
            await self.ns['cmd_devaddyear'](self.ctx, args=text)
            self.edit.assert_awaited_with(self.ctx, 'Magic Johnson 2015', 'Iowa Hawkeyes', current_sheet_only=True)

    async def test_nonowner_cannot_edit(self):
        self.ctx.author.id = 2
        await self.ns['cmd_devaddyear'](self.ctx, args='Iowa Hawkeyes / Magic Johnson / 2015')
        self.edit.assert_not_awaited()
        self.assertIn('Only the bot owner', self.ctx.send.call_args.args[0])

    async def test_incomplete_inputs_do_not_reach_writer(self):
        for text in ('', ':Hawkeyes:', ':Hawkeyes: / Magic Johnson /', 'Iowa / Magic / 2015 / extra'):
            await self.ns['cmd_devaddyear'](self.ctx, args=text)
        self.edit.assert_not_awaited()
