"""Free off-sheet picks must not bypass failed reads or listed prices."""
import os
from pathlib import Path
import sys
import tempfile
import types
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

_temp_root = tempfile.TemporaryDirectory()
os.environ['STATE_DIR'] = _temp_root.name
sys.modules.setdefault('sheets', types.ModuleType('sheets'))

from flux_rules import parse_prices
from themes.anon_budget_draft import AnonBudgetDraftTheme, budget_price_record


class OffSheetPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlisted_player_is_zero_including_historical_makeup(self):
        theme = AnonBudgetDraftTheme()
        ctx = NS(state={'teams': [{}, {}], 'live_pick_number': 5,
                        'flux_price_source': {'sheet': 'test', 'tab': 'ADP'}})
        rows = [['Player', 'Value', 'Round 2', 'Round 3'], ['Michael Jordan', 44, 42, 40]]
        with patch('sheets.load_flux_rule_rows', create=True, return_value=rows):
            self.assertIsNone(await theme.validate_round_price(ctx, 'Unlisted Player', 0))
            self.assertIsNone(await theme.validate_round_price(
                ctx, 'Unlisted Player', 0, entry={'skipped': True, 'pick_number': 1}))
            self.assertIn('free ($0)', await theme.validate_round_price(ctx, 'Unlisted Player', 5))
            self.assertIn('free ($0)', await theme.validate_round_price(ctx, 'Unlisted Player', -5))

    async def test_bad_sheet_missing_round_and_read_failure_do_not_become_free(self):
        theme = AnonBudgetDraftTheme()
        ctx = NS(state={'teams': [{}], 'live_pick_number': 2,
                        'flux_price_source': {'sheet': 'test', 'tab': 'ADP'}})
        for rows in ([], [['Name', 'Other']], [['Player', 'Value']],
                     [['Player', 'Value'], ['Michael Jordan', 44]]):
            with patch('sheets.load_flux_rule_rows', create=True, return_value=rows):
                self.assertIsNotNone(await theme.validate_round_price(ctx, 'Unlisted Player', 0))
        with patch('sheets.load_flux_rule_rows', create=True, side_effect=RuntimeError('offline')):
            self.assertIn('could not read', await theme.validate_round_price(ctx, 'Unlisted Player', 0))

    def test_listed_players_and_punctuation_keep_their_prices(self):
        book = parse_prices([['Player', 'Value'], ["Shaquille O'Neal", 38], ['Michael Jordan', 44]])
        self.assertEqual(budget_price_record(book, 'Shaquille O’Neal')['current'], 38)
        with self.assertRaisesRegex(ValueError, 'spelling'):
            budget_price_record(book, 'Michaell Jordan')

    def test_ambiguous_duplicate_and_blank_listed_price_are_not_free(self):
        for rows in ([['Player', 'Value'], ['Michael Jordan', 44], ['Michael Jordan', 40]],
                     [['Player', 'Value'], ['Michael Jordan', 44], ['Michael Redd', 5]]):
            with self.assertRaises(ValueError):
                budget_price_record(parse_prices(rows), 'Michael')
        book = parse_prices([['Player', 'Value'], ['Michael Jordan', '']])
        self.assertIsNone(budget_price_record(book, 'Michael Jordan')['current'])
        self.assertFalse(budget_price_record(book, 'Michael Jordan').get('off_sheet', False))
