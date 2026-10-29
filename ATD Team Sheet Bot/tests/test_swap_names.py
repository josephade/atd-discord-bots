import ast
from pathlib import Path
import unittest

from draft_history import player_key


class SwapNameTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf8')
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_find_roster_slot')
        ns = {'player_key': player_key}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        self.find = ns['_find_roster_slot']
        self.slots = {8: {'name': 'Gus Johnson', 'year': '1969-70'},
                      9: {'name': 'Michael Porter Jr', 'year': '2025-26'}}

    def test_rockets_swap_accepts_jr_with_period(self):
        for name in ['michael porter jr.', 'Michael Porter Jr', '  MICHAEL PORTER JR.  ']:
            slot, error = self.find(name, self.slots)
            self.assertIs(slot, self.slots[9])
            self.assertIsNone(error)
        self.assertIs(self.find('gus johnson', self.slots)[0], self.slots[8])

    def test_partial_names_still_require_unambiguous_match(self):
        self.slots[1] = {'name': 'Otto Porter Jr.'}
        self.assertIsNone(self.find('Porter', self.slots)[0])
        self.assertIn('Multiple matches', self.find('Porter', self.slots)[1])
        self.assertIsNone(self.find('Missing Player', self.slots)[0])
        self.assertIsNone(self.find('...', self.slots)[0])

    def test_senior_and_junior_remain_distinct(self):
        slots = {1: {'name': 'Tim Hardaway'}, 2: {'name': 'Tim Hardaway Jr'}}
        self.assertIs(self.find('Tim Hardaway Sr.', slots)[0], slots[1])
        self.assertIs(self.find('Tim Hardaway Jr.', slots)[0], slots[2])


if __name__ == '__main__':
    unittest.main()
