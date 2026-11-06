import copy
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest

from row_highlights import MARKER, managed_rule_requests


class RowHighlightTests(unittest.TestCase):
    def metadata(self, ranges=None):
        rules = [{'gradientRule': {'minpoint': {'type': 'MIN'}}}]
        if ranges is not None:
            rules.append({'ranges': ranges, 'booleanRule': {
                'condition': {'type': 'CUSTOM_FORMULA',
                              'values': [{'userEnteredValue': MARKER}]},
                'format': {'backgroundColor': {'blue': 1}}}})
        return {'sheets': [{'properties': {'sheetId': 7}, 'conditionalFormats': rules}]}

    def test_priority_rule_covers_adp_and_future_flux_columns(self):
        meta = self.metadata()
        original = copy.deepcopy(meta)
        req = managed_rule_requests(meta, 7, [221], {'blue': 1})[0]
        rule = req['addConditionalFormatRule']
        self.assertEqual(rule['index'], 0)
        self.assertEqual(rule['rule']['ranges'], [{'sheetId': 7, 'startRowIndex': 220,
                                                  'endRowIndex': 221, 'startColumnIndex': 0}])
        self.assertEqual(meta, original)

    def test_new_pick_preserves_existing_rows_without_duplicates(self):
        meta = self.metadata([{'startRowIndex': 218, 'endRowIndex': 221}])
        req = managed_rule_requests(meta, 7, [221, 222], {'blue': 1})
        self.assertEqual(req[0], {'deleteConditionalFormatRule': {'sheetId': 7, 'index': 1}})
        self.assertEqual([r['startRowIndex'] for r in req[1]['addConditionalFormatRule']['rule']['ranges']],
                         [218, 219, 220, 221])

    def test_undo_removes_only_target_row(self):
        meta = self.metadata([{'startRowIndex': 218, 'endRowIndex': 221}])
        req = managed_rule_requests(meta, 7, [220], {'blue': 1}, remove=True)
        self.assertEqual([r['startRowIndex'] for r in req[1]['addConditionalFormatRule']['rule']['ranges']],
                         [218, 220])

    def test_last_undo_removes_rule_without_touching_gradient(self):
        meta = self.metadata([{'startRowIndex': 220, 'endRowIndex': 221}])
        self.assertEqual(managed_rule_requests(meta, 7, [221], {}, remove=True),
                         [{'deleteConditionalFormatRule': {'sheetId': 7, 'index': 1}}])

    def test_repair_replaces_list_and_does_not_include_header(self):
        req = managed_rule_requests(self.metadata([{'startRowIndex': 8, 'endRowIndex': 9}]),
                                    7, [1, 219, 221], {'blue': 1}, replace=True)
        self.assertEqual([r['startRowIndex'] for r in req[1]['addConditionalFormatRule']['rule']['ranges']],
                         [218, 220])


class HighlightIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_and_undo_change_only_background_and_managed_rule(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'discord_sheet_highlighter.py').read_text(encoding='utf-8'))
        selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in {'col_to_index', '_update_highlight', 'apply_highlight', 'clear_highlight'}]
        self.assertEqual(len(selected), 4)
        ns = dict(asyncio=asyncio, highlight_locks={}, ROW_START_COL='A', HIGHLIGHT_COLOR={'blue': 1},
                  managed_rule_requests=managed_rule_requests, batch_update_with_retry=AsyncMock())
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'highlighter', 'exec'), ns)
        sheet = SimpleNamespace(id='book', fetch_sheet_metadata=Mock(return_value={
            'sheets': [{'properties': {'sheetId': 7}}]}))
        ws = SimpleNamespace(id=7)
        await ns['apply_highlight'](sheet, ws, 221, {'blue': 1})
        requests = ns['batch_update_with_retry'].call_args.args[1]['requests']
        self.assertIn('addConditionalFormatRule', requests[0])
        fill = requests[-1]['repeatCell']
        self.assertEqual(fill['fields'], 'userEnteredFormat.backgroundColor')
        self.assertNotIn('endColumnIndex', fill['range'])
        sheet.fetch_sheet_metadata.return_value = RowHighlightTests().metadata([
            {'startRowIndex': 220, 'endRowIndex': 221}])
        await ns['clear_highlight'](sheet, ws, 221)
        requests = ns['batch_update_with_retry'].call_args.args[1]['requests']
        self.assertEqual(requests[0], {'deleteConditionalFormatRule': {'sheetId': 7, 'index': 1}})
        self.assertEqual(requests[-1]['repeatCell']['cell'], {'userEnteredFormat': {}})
