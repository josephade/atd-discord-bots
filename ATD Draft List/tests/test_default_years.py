"""Default seasons must never replace a GM's explicit season."""
import ast
from pathlib import Path
import re
import runpy
import unittest

from test_autopick import namespace as auto_namespace


BOT_DIR = Path(__file__).resolve().parents[1]
DEFAULTS = runpy.run_path(str(BOT_DIR / 'player_years.py'))


def namespace():
    tree = ast.parse((BOT_DIR / 'bot.py').read_text(encoding='utf-8'))
    functions = {'_parse_pick_line', '_with_default_year', '_format_pick', '_build_pick_message'}
    constants = {'_PRICE_RE', '_YEAR_RE'}
    nodes = [n for n in tree.body if
             isinstance(n, ast.FunctionDef) and n.name in functions
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
    ns = dict(re=re, get_default_year=DEFAULTS['get_default_year'], _is_valid_emoji=lambda s: True)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


class DefaultYearTests(unittest.TestCase):
    def test_signed_currency_prices_keep_names_years_and_fractional_amounts(self):
        for price in ('$-1', '-$1', '\u2212$1'):
            pick = namespace()['_parse_pick_line']('Nene '+price)
            self.assertEqual(pick['player'],'Nene')
            self.assertEqual(pick['price'],-1)
        self.assertEqual(namespace()['_parse_pick_line']('Nene $-0.75')['price'],-.75)

    def setUp(self):
        self.ns = namespace()

    def test_omitted_year_uses_commissioner_default(self):
        for name, year in [('Shawn Kemp', '1995-96'), ('Larry Bird', '1985-86'), ('Michael Jordan', '1990-91')]:
            with self.subTest(player=name):
                self.assertEqual(self.ns['_parse_pick_line'](name)['year'], year)

    def test_explicit_years_always_win(self):
        for text, year in [("Shawn Kemp '94", '1993-94'), ('Shawn Kemp 1994-95', '1994-95'),
                           ('Shawn Kemp 1993', '1993'), ('Shawn Kemp 93-94', '1993-94'),
                           ('Shawn Kemp ’94', '1993-94')]:
            with self.subTest(text=text):
                self.assertEqual(self.ns['_parse_pick_line'](text)['year'], year)

    def test_default_keeps_price_and_normalizes_name_for_lookup(self):
        result = self.ns['_parse_pick_line']('1. shawn kemp $12')
        self.assertEqual(result, {'player': 'shawn kemp', 'year': '1995-96', 'price': 12})

    def test_unknown_player_stays_unset(self):
        self.assertIsNone(self.ns['_parse_pick_line']('Unknown Player')['year'])

    def test_existing_saved_list_uses_default_in_preview_and_post(self):
        pick = {'player': 'Shawn Kemp', 'year': None, 'price': 5, 'lock': True}
        self.assertIn("Shawn Kemp '96 $5", self.ns['_format_pick'](pick))
        self.assertEqual(self.ns['_build_pick_message'](74, ':team:', pick), "74. :team: Shawn Kemp '96 $5 lock")
        self.assertIsNone(pick['year'])

    def test_existing_saved_explicit_year_is_preserved(self):
        pick = {'player': 'Shawn Kemp', 'year': '1993-94'}
        self.assertEqual(self.ns['_build_pick_message'](74, ':team:', pick), "74. :team: Shawn Kemp '94")

    @unittest.skipUnless((BOT_DIR.parent / 'ATD Draft Theme Bot' / 'themes' / 'player_years.py').exists(),
                         'Cross-repository default-year parity is checked in the workspace.')
    def test_deployed_table_matches_theme_bots_commissioner_table(self):
        original = runpy.run_path(str(BOT_DIR.parent / 'ATD Draft Theme Bot' / 'themes' / 'player_years.py'))
        self.assertEqual(DEFAULTS['PLAYER_YEARS'], original['PLAYER_YEARS'])


class DefaultYearAutoPickTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_name_is_used_for_old_yearless_saved_pick(self):
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock, Mock
        ns = auto_namespace()
        ns.update(get_default_year=DEFAULTS['get_default_year'],
                  _data={'74': {'enabled': True, 'emoji': ':team:', 'picks': [{'player': 'porter'}]}},
                  _last_autopick=None, _resolve_emoji_input=lambda s: s,
                  _is_valid_emoji=lambda s: True, _save_data=Mock(),
                  _fetch_availability=lambda **kw: {'terry porter': True},
                  _fetch_roster_names=lambda **kw: set(),
                  _canonical_names={'terry porter': 'Terry Porter'})
        channel = NS(send=AsyncMock())
        user = NS(id=74, display_name='Crispy', send=AsyncMock())
        await ns['_do_auto_pick_inner'](channel, user, 74, set())
        channel.send.assert_awaited_once_with("74. :team: Terry Porter '91")


if __name__ == '__main__':
    unittest.main()
