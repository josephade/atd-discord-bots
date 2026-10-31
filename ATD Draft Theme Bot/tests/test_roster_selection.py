"""Roster commands must never fall back to unrelated historical picks."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class RosterSelectionTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        self.db = Mock()
        self.ns = {'db': self.db}
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_resolve_roster_drafter')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), self.ns)
        self.resolve = self.ns['_resolve_roster_drafter']

    def test_latest_cancelled_anon_view_does_not_return_completed_auto_draft(self):
        self.db.get_drafters_for_user.return_value = [dict(draft_id=21, id=1), dict(draft_id=28, id=2)]
        self.db.get_draft.return_value = dict(id=28, status='cancelled', theme='anon_budget_draft')
        draft, member = self.resolve(100, read_only=True)
        self.assertEqual((draft['id'], member['id']), (28, 2))
        self.db.get_draft.assert_called_once_with(28)

    def test_cancelled_latest_draft_blocks_edits_instead_of_editing_old_draft(self):
        self.db.get_drafters_for_user.return_value = [dict(draft_id=21), dict(draft_id=28)]
        self.db.get_draft.return_value = dict(id=28, status='cancelled')
        self.assertIsNone(self.resolve(100))
        self.db.get_draft.assert_called_once_with(28)

    def test_latest_running_or_completed_draft_is_selected_before_older_active_draft(self):
        for status in ('active', 'paused', 'complete', 'setup'):
            self.db.get_drafters_for_user.return_value = [dict(draft_id=10), dict(draft_id=29)]
            self.db.get_draft.return_value = dict(id=29, status=status)
            self.assertEqual(self.resolve(100)[0]['id'], 29)

    def test_no_draft_returns_no_roster(self):
        self.db.get_drafters_for_user.return_value = []
        self.assertIsNone(self.resolve(100, read_only=True))
