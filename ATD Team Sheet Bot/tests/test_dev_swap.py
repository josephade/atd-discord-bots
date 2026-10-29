import ast
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock
from draft_history import player_key


@asynccontextmanager
async def typing():
    yield


class DeveloperSwapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf8')
        nodes = []
        for node in ast.parse(source).body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in ('cmd_swap', 'cmd_devswap'):
                node.decorator_list = []
                nodes.append(node)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_DEV_SWAP_USER_ID' for t in node.targets):
                nodes.append(node)
        self.ns = {'_swap_roster': AsyncMock(), '_team_owners': {'12': 'Own Team'}, '_resolve_team_from_emoji': lambda s: None}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), self.ns)
        self.ctx = NS(author=NS(id=self.ns['_DEV_SWAP_USER_ID']), send=AsyncMock())

    async def test_developer_can_select_any_team_without_claiming_it(self):
        await self.ns['cmd_devswap'](self.ctx, args='Houston Rockets / Michael Porter Jr. / Gus Johnson')
        self.ns['_swap_roster'].assert_awaited_once_with(self.ctx, 'Michael Porter Jr. / Gus Johnson', 'Houston Rockets', current_sheet_only=True)

    async def test_other_users_and_server_admins_cannot_use_override(self):
        self.ctx.author = NS(id=12, guild_permissions=NS(administrator=True))
        await self.ns['cmd_devswap'](self.ctx, args='Houston Rockets / A / B')
        self.ns['_swap_roster'].assert_not_awaited()
        self.assertIn('Only the bot developer', self.ctx.send.call_args.args[0])

    async def test_regular_swap_remains_limited_to_claimed_team(self):
        self.ctx.author.id = 12
        await self.ns['cmd_swap'](self.ctx, args='A / B')
        self.ns['_swap_roster'].assert_awaited_once_with(self.ctx, 'A / B', 'Own Team')

    async def test_developer_accepts_slot_numbers_and_team_emojis(self):
        self.ns['_resolve_team_from_emoji'] = lambda s: 'Houston Rockets'
        await self.ns['cmd_devswap'](self.ctx, args='<:rockets:123> / Michael Porter Jr. / 8')
        self.ns['_swap_roster'].assert_awaited_once_with(self.ctx, 'Michael Porter Jr. / 8', 'Houston Rockets', current_sheet_only=True)

    async def test_invalid_input_and_unclaimed_regular_user_do_not_write(self):
        await self.ns['cmd_devswap'](self.ctx, args='Houston Rockets / A')
        await self.ns['cmd_swap'](self.ctx, args='A / B')
        self.ns['_swap_roster'].assert_not_awaited()

    async def test_shared_swap_moves_name_year_price_and_stays_on_current_sheet(self):
        source = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf8')
        nodes = [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ('_swap_roster', '_find_roster_slot')]
        rows = [['Houston Rockets', 'Year', 'Price'], ['Michael Porter Jr', '2025-26', '3'], ['Gus Johnson', '1969-70', '4']] + [['', '', ''] for _ in range(8)]
        manager = NS(_call=Mock(side_effect=lambda method, *args: rows if method == 'get_all_values' else None),
                     _find_team_cell=Mock(return_value=(1, 1)), _tab_has_price_column=lambda rows: True)
        get_manager = Mock(return_value=manager)
        ns = {'player_key': player_key, '_get_manager': get_manager, '_channel_sheet_map': {1: {}, 2: {}},
              'gspread': NS(utils=NS(rowcol_to_a1=lambda r, c: f'R{r}C{c}')),
              'discord': NS(Embed=lambda **kw: NS(set_footer=lambda **kw: None), Color=NS(green=lambda: 1)),
              '_friendly_sheet_error': lambda exc: str(exc)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
        self.ctx.channel = NS(id=1, typing=typing)
        self.ctx.author.display_name = 'jsoapz'
        await ns['_swap_roster'](self.ctx, 'Michael Porter Jr. / Gus Johnson', 'Houston Rockets', current_sheet_only=True)
        updates = manager._call.call_args.args
        self.assertEqual(updates[0], 'batch_update')
        self.assertEqual([u['values'][0][0] for u in updates[1]], ['Gus Johnson', '1969-70', 'Michael Porter Jr', '2025-26', '4', '3'])
        get_manager.assert_called_once_with(1)
        get_manager.reset_mock(); get_manager.return_value = None; manager._call.reset_mock()
        await ns['_swap_roster'](self.ctx, 'Michael Porter Jr. / 8', 'Houston Rockets', current_sheet_only=True)
        manager._call.assert_not_called()
        get_manager.assert_called_once_with(1)
