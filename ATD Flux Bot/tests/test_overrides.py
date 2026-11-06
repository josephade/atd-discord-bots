import copy
import json
import tempfile
import unittest
from pathlib import Path
from overrides import plan_values, load_plan, mark_applied


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(id='one-time', status='pending', channel_id=99, spreadsheet_id='sheet', worksheet_gid=123, round=4, vol=3,
                         players=[dict(player='Peja Stojakovic', previous_price=15, target_price=14, drop=1, value=14.070139298493)])
        self.rows = [['Player', 'Value', 'Round 2', 'Round 3'], ['Peja Stojaković', '$16', '$16', '$15'], ['Someone Else', '$10', '$10', '$9']]
        self.scope = dict(channel_id=99, spreadsheet_id='sheet', worksheet_gid=123, round_number=4, vol=3)

    def test_decimal_targets_and_unique_exact_normalized_name(self):
        self.assertEqual(plan_values(self.plan, self.rows, **self.scope), {1: 14.070139298493})

    def test_other_channels_sheets_tabs_and_rounds_are_untouched(self):
        for key, value in [('channel_id', 100), ('spreadsheet_id', 'other'), ('worksheet_gid', 124), ('round_number', 5)]:
            self.assertEqual(plan_values(self.plan, self.rows, **{**self.scope, key: value}), {})
        applied = {**self.plan, 'status': 'applied'}
        self.assertEqual(plan_values(applied, self.rows, **self.scope), {})

    def test_increases_are_allowed_within_volatility_and_keep_decimal_rounding(self):
        plan = copy.deepcopy(self.plan)
        plan['players'][0].update(target_price=18, drop=-3, value=18.123456789)
        self.assertEqual(plan_values(plan, self.rows, **self.scope), {1: 18.123456789})
        plan['players'][0].update(target_price=19, drop=-4, value=19.123456789)
        with self.assertRaises(ValueError):
            plan_values(plan, self.rows, **self.scope)

    def test_invalid_or_changed_prices_fail_before_any_sheet_writes(self):
        for key, value in [('value', 14.7), ('value', float('nan')), ('value', 14.0), ('drop', 2), ('previous_price', 16)]:
            plan = copy.deepcopy(self.plan)
            plan['players'][0][key] = value
            with self.assertRaises(ValueError):
                plan_values(plan, self.rows, **self.scope)
        with self.assertRaises(ValueError):
            plan_values(self.plan, self.rows, **{**self.scope, 'vol': 4})
        with self.assertRaises(ValueError):
            plan_values(self.plan, self.rows + [self.rows[1]], **self.scope)

    def test_consumed_plan_remains_auditable_but_cannot_apply_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps(self.plan))
            mark_applied(path, self.plan, 'G')
            saved = load_plan(path)
            self.assertEqual(saved['status'], 'applied')
            self.assertEqual(saved['applied_column'], 'G')
            self.assertEqual(plan_values(saved, self.rows, **self.scope), {})
