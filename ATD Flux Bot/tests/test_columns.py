import unittest
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock
from columns import prepare_round_columns, column_letter
from overrides import plan_values


class FluxColumnTests(unittest.TestCase):
    def worksheet(self, columns=14):
        return NS(col_count=columns, id=123, add_cols=Mock(), resize=Mock(), spreadsheet=NS(batch_update=Mock(),
            fetch_sheet_metadata=Mock(return_value={'sheets':[{'properties':{'sheetId':123,'gridProperties':{'columnCount':columns}}}]})))

    def test_round_two_uses_e_despite_empty_allocated_grid(self):
        ws = self.worksheet(14)
        self.assertEqual(prepare_round_columns(ws, [['Position', 'Player', 'ADP', 'Value'], ['Big', 'Player', 1, 30]]), 5)
        ws.add_cols.assert_not_called()
        ws.spreadsheet.batch_update.assert_not_called()

    def test_later_round_and_multiple_rounds_remain_adjacent(self):
        ws = self.worksheet(26)
        rows = [['Position', 'Player', 'ADP', 'Value', 'Round 2', 'Round 3']]
        self.assertEqual(prepare_round_columns(ws, rows, 3), 7)
        ws.add_cols.assert_not_called()

    def test_adjacent_unrelated_data_is_shifted_instead_of_overwritten(self):
        ws = self.worksheet()
        self.assertEqual(prepare_round_columns(ws, [['Position', 'Player', 'ADP', 'Value', 'Notes']], 2), 5)
        request = ws.spreadsheet.batch_update.call_args.args[0]['requests'][0]['insertDimension']
        self.assertEqual((request['range']['startIndex'], request['range']['endIndex']), (4, 6))

    def test_unheaded_data_below_header_is_also_preserved(self):
        ws = self.worksheet()
        prepare_round_columns(ws, [['Position', 'Player', 'ADP', 'Value'], ['', '', '', '', 'Keep me']])
        ws.spreadsheet.batch_update.assert_called_once()

    def test_grid_only_expands_when_needed_and_wide_letters_are_correct(self):
        ws = self.worksheet(4)
        self.assertEqual(prepare_round_columns(ws, [['Position', 'Player', 'ADP', 'Value']], 3), 5)
        ws.resize.assert_called_once_with(cols=7)
        self.assertEqual(column_letter(27), 'AA')
        self.assertEqual(column_letter(15), 'O')

    def test_deleted_columns_with_stale_cached_width_expand_to_e(self):
        ws = self.worksheet(14)
        ws.spreadsheet.fetch_sheet_metadata.return_value['sheets'][0]['properties']['gridProperties']['columnCount'] = 4
        self.assertEqual(prepare_round_columns(ws, [['Position','Player','ADP','Value']]), 5)
        ws.resize.assert_called_once_with(cols=5)
        ws.add_cols.assert_not_called()

    def test_stale_smaller_width_never_shrinks_the_live_grid(self):
        ws = self.worksheet(4)
        ws.spreadsheet.fetch_sheet_metadata.return_value['sheets'][0]['properties']['gridProperties']['columnCount'] = 14
        prepare_round_columns(ws, [['Position','Player','ADP','Value']])
        ws.resize.assert_not_called()

    def test_old_widely_spaced_round_is_preserved_as_existing_history(self):
        ws = self.worksheet(26)
        self.assertEqual(prepare_round_columns(ws, [['Position', 'Player', 'ADP', 'Value']+['']*10+['Round 2']]), 16)


class FluxCommandTests(unittest.IsolatedAsyncioTestCase):
    def namespace(self, rows):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'flux_bot.py').read_text(encoding='utf-8'))
        names = {'flux', 'fluxplayers', 'get_existing_rounds', 'col_letter_by_name'}
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        for node in nodes:
            node.decorator_list = []
        ws = NS(col_count=14, id=123, add_cols=Mock(), resize=Mock(), spreadsheet=NS(batch_update=Mock(),
                fetch_sheet_metadata=Mock(return_value={'sheets':[{'properties':{'sheetId':123,'gridProperties':{'columnCount':14}}}]})),
                get_all_values=Mock(return_value=rows), update_cell=Mock(), update=Mock())
        ns = dict(discord=NS(Member=NS), has_commish_role=lambda user: True, get_ws=lambda channel: (NS(id='sheet'), ws),
                  log=Mock(), prepare_round_columns=prepare_round_columns, column_letter=column_letter,
                  load_plan=Mock(return_value=None), plan_values=plan_values, mark_applied=Mock(), OVERRIDES_FILE='test-plan.json',
                  ROUND_2_TEMPLATE='base={VAL}', ROUND_3_PLUS_TEMPLATE='base={VAL};previous={PREV}')
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'flux_bot.py', 'exec'), ns)
        return ns, ws, NS(author=NS(id=1), channel=NS(id=99), send=AsyncMock())

    async def test_normal_flux_writes_round_two_in_e_not_o(self):
        ns, ws, ctx = self.namespace([['Position', 'Player', 'ADP', 'Value'], ['Wing', 'Michael Jordan', 1, 44]])
        await ns['flux'](ctx, 3)
        ws.update_cell.assert_called_once_with(1, 5, 'Round 2')
        self.assertEqual(ws.update.call_args.args[0], 'E2:E2')
        self.assertEqual(ws.update.call_args.args[1], [['base=D']])

    async def test_player_flux_uses_f_g_and_previous_round_two_prices(self):
        ns, ws, ctx = self.namespace([['Position', 'Player', 'ADP', 'Value', 'Round 2'], ['Wing', 'Michael Jordan', 1, 44, 42]])
        await ns['fluxplayers'](ctx, 3, 2, players_raw='Michael Jordan')
        self.assertEqual([call.args for call in ws.update_cell.call_args_list], [(1, 6, 'Round 3'), (1, 7, 'Round 4')])
        self.assertEqual([call.args[0] for call in ws.update.call_args_list], ['F2:F2', 'G2:G2'])
        self.assertEqual(ws.update.call_args_list[0].args[1], [['base=D;previous=E']])
        self.assertEqual(ws.update.call_args_list[1].args[1], [['base=D;previous=F']])

    async def test_round_four_overrides_only_selected_row_and_freezes_before_marking_applied(self):
        rows = [['Position', 'Player', 'ADP', 'Value', 'Round 2', 'Round 3'],
                ['Wing', 'Vince Carter', 1, 20, 20, '$22'], ['Guard', 'Other Player', 2, 10, 12, 11]]
        ns, ws, ctx = self.namespace(rows)
        plan = dict(id='test', status='pending', channel_id=99, spreadsheet_id='sheet', worksheet_gid=123, round=4, vol=3,
                    players=[dict(player='Vince Carter', previous_price=22, target_price=20, drop=2, value=20.070139298493)])
        ns['load_plan'].return_value = plan
        ns['mark_applied'].side_effect = lambda *args: self.assertTrue(ws.spreadsheet.batch_update.called)
        await ns['flux'](ctx, 3)
        self.assertEqual(ws.update.call_args.args, ('G2:G3', [[20.070139298493], ['base=D;previous=F']]))
        ns['mark_applied'].assert_called_once_with('test-plan.json', plan, 'G')
