import unittest
import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from roster_prices import numeric_price, read_prices


class RosterPriceTests(unittest.TestCase):
    def test_theme_public_negative_price_keeps_its_sign_for_sheet_writes(self):
        source = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        assignment = next(n for n in source.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_PRICE_RE' for t in n.targets))
        ns = {'re':re}
        exec(compile(ast.Module(body=[assignment],type_ignores=[]),'bot.py','exec'),ns)
        for price in ('$-1','-$1','$-1.25'):
            match = ns['_PRICE_RE'].search('187. <:Logo:123456> Nene 2013 '+price)
            self.assertIsNotNone(match)
            self.assertEqual(numeric_price(match.group(1)),numeric_price(price))

    def test_numeric_currency_and_legacy_text_remain_numbers_without_rounding_away_precision(self):
        for raw, expected in [(9, 9), ('$9', 9), ('$1,234.25', 1234.25), (11.292907258264, 11.292907258264), (0, 0), (-1.25, -1.25), ('$-1', -1)]:
            self.assertEqual(numeric_price(raw), expected)
            self.assertIsInstance(numeric_price(raw), float)
        for raw in ['', None, '   ']:
            self.assertEqual(numeric_price(raw), '')

    def test_price_reads_request_unformatted_values_and_preserve_an_empty_destination(self):
        manager = SimpleNamespace(_call=Mock(return_value=[[[11.292907258264]], []]))
        self.assertEqual(read_prices(manager, ['I2', 'I9']), [11.292907258264, ''])
        manager._call.assert_called_once_with('batch_get', ['I2', 'I9'], value_render_option='UNFORMATTED_VALUE')

    def test_bad_values_fail_before_the_swap(self):
        for raw in ['#REF!', 'not a price', float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                numeric_price(raw)
        with self.assertRaises(ValueError):
            read_prices(SimpleNamespace(_call=Mock(return_value=[])), ['I2', 'I9'])
