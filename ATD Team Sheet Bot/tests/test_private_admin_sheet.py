import ast
from pathlib import Path
import unittest
from unittest.mock import Mock


class PrivateAdminSheetTests(unittest.TestCase):
    def test_private_admin_defaults_to_selection_sheet_without_overwriting_explicit_mapping(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_ensure_private_admin_sheet')
        mapping = {934052208624930848: dict(sheet_id='sheet', tab='Test')}
        setter = Mock()
        ns = dict(_channel_sheet_map=mapping, _set_channel_sheet=setter)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ns['_ensure_private_admin_sheet']()
        setter.assert_called_once_with(1533242104304631969, 'Test', 'sheet')
        setter.reset_mock()
        mapping[1533242104304631969] = dict(sheet_id='other', tab='Owner choice')
        ns['_ensure_private_admin_sheet']()
        setter.assert_not_called()
