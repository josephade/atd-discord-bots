import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from reveal import standings, record_text, gm_ids, comparison, ordinal
from storage import Store


def rows():
    return [['Team', 'Wins'], ['decoy', '99'], [],
            ['', 'Team ', 'Wins', 'Losses', 'GM'],
            ['', '[Boston Celtics]', '49.4', '32.6', 'B'],
            ['', '[Iowa Hawkeyes]', '53.32721336', '28.67278664', 'A / B']]


class RevealTests(unittest.TestCase):
    def test_reusable_gm_aliases_preserve_history_and_do_not_change_voter_aliases(self):
        with TemporaryDirectory() as directory:
            store = Store(directory+'/test.db')
            store.set_gm_alias('KevTheDuck', 386543892356530177)
            store.set_gm_alias('Kev', 386543892356530177)
            self.assertEqual(gm_ids(dict(gm='KEV'), [], store.gm_aliases()), [386543892356530177])
            self.assertEqual(gm_ids(dict(gm='KevTheDuck'), [], store.gm_aliases()), [386543892356530177])
            store.set_reveal_mapping('113', 'Team', [123])
            self.assertEqual(store.reveal_mapping('113', 'Team'), [123])
            self.assertEqual(store.aliases(), {})
            store.db.close()

    def test_seed_ordinals_include_teens(self):
        self.assertEqual([ordinal(n) for n in (1, 2, 3, 11, 12, 13, 21, 22, 23, 29, 30, 32)],
                         ['1st', '2nd', '3rd', '11th', '12th', '13th', '21st', '22nd', '23rd', '29th', '30th', '32nd'])

    def test_final_table_sorted_by_unrounded_wins_not_decoy_or_display_rounding(self):
        result = standings(rows())
        self.assertEqual([(r['seed'], r['team']) for r in result], [(1, 'Iowa Hawkeyes'), (2, 'Boston Celtics')])
        self.assertEqual(result[0]['wins'], '53.32721336')
        self.assertEqual(record_text(result[0]), '53-29')

    def test_invalid_and_duplicate_tables_fail_instead_of_revealing_partial_results(self):
        for value in ('', '#REF!', 'NaN', 'Infinity', '-1'):
            data = rows(); data[-1][2] = value
            with self.assertRaises(ValueError): standings(data)
        with self.assertRaises(ValueError): standings(rows()+rows())
        with self.assertRaises(ValueError): standings(rows()+[rows()[-1]])

    def test_half_win_rounding_keeps_season_total(self):
        self.assertEqual(record_text(dict(wins='40.5', losses='41.5')), '41-41')

    def test_only_exact_complete_gm_mapping_credits_all_partners(self):
        members = [SimpleNamespace(id=i, name=name, display_name=name, global_name=None, bot=False)
                   for i, name in [(1, 'A'), (2, 'B'), (3, 'Long Name')]]
        self.assertEqual(gm_ids(dict(gm='A / B'), members, {}), [1, 2])
        self.assertEqual(gm_ids(dict(gm='Long Name'), members, {}), [3])
        self.assertEqual(gm_ids(dict(gm='A Unknown'), members, {}), [])
        self.assertEqual(gm_ids(dict(gm='A / Unknown'), members, {}), [])
        self.assertEqual(gm_ids(dict(gm='anything'), members, {}, [1, 2]), [1, 2])

    def test_history_idempotent_draft_specific_and_can_correct_mapping(self):
        with TemporaryDirectory() as directory:
            store = Store(directory+'/test.db')
            result = dict(team='Iowa Hawkeyes', wins='20', losses='62', gm='A', gm_ids=[1, 2])
            store.save_reveal_results('113', [result], 'sheet', 1)
            store.save_reveal_results('113', [result], 'sheet', 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 1)
            self.assertIsNone(store.previous_reveal('113', 1))
            previous = store.previous_reveal('114', 1)
            self.assertEqual(previous['wins'], '20')
            self.assertIn('improved by 30 wins', comparison(dict(wins='50'), previous, 1))
            store.set_reveal_mapping('113', 'Iowa Hawkeyes', [3])
            self.assertIsNone(store.previous_reveal('114', 1))
            self.assertIsNotNone(store.previous_reveal('114', 3))
            self.assertEqual(store.reveal_mapping('113', 'IOWA HAWKEYES'), [3])
            self.assertEqual(store.reveal_mapping('114', 'Iowa Hawkeyes'), [])
            store.db.close()
