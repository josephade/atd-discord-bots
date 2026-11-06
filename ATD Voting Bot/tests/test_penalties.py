import copy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from penalties import LedgerResolver
from penalties import comparison_lines, carryover_details
from reveal import standings, record_text, comparison
from storage import Store


class PenaltyRevealTests(unittest.TestCase):
    def setUp(self):
        self.result = dict(team='SECRET TEAM', gm='SECRET GM', gm_ids=[1, 2], wins='25', losses='57')
        self.ledger = dict(teams=[dict(user_ids=[1, 2], caught_wins=5, market_wins=50, bought_guesses=10)])

    def test_separate_and_combined_records_do_not_mutate_official_record(self):
        before = copy.deepcopy(self.result)
        lines = comparison_lines(self.result, self.ledger)
        self.assertEqual(lines, ['Without buying 10 suspect guesses: **75-7**.',
                                'Without being caught by a correct suspect: **30-52**.',
                                'Without either deduction: **80-2**.'])
        self.assertEqual(self.result, before)
        self.assertNotIn('SECRET', '\n'.join(lines))
        self.assertNotIn('<@', '\n'.join(lines))

    def test_no_deduction_and_duo_not_double_counted(self):
        self.ledger['teams'][0].update(caught_wins=0, market_wins=0, bought_guesses=0)
        self.assertEqual(comparison_lines(self.result, self.ledger), [])
        self.ledger['teams'][0].update(caught_wins=5)
        self.assertEqual(comparison_lines(self.result, self.ledger),
                         ['Without being caught by a correct suspect: **30-52**.'])

    def test_rounding_preserves_season_total(self):
        self.result.update(wins='20.5', losses='61.5')
        self.ledger['teams'][0].update(caught_wins=0)
        self.assertEqual(comparison_lines(self.result, self.ledger),
                         ['Without buying 10 suspect guesses: **71-11**.'])

    def test_wrong_missing_mapping_and_impossible_record_stop_reveal(self):
        for ids in ([], [3], [1, 3]):
            with self.assertRaises(ValueError):
                comparison_lines(dict(self.result, gm_ids=ids), self.ledger)
        with self.assertRaises(ValueError):
            comparison_lines(dict(self.result, wins='60', losses='22'), self.ledger)

    def test_link_is_explicit_per_atd_and_persistent(self):
        with TemporaryDirectory() as directory:
            store = Store(directory + '/voting.db')
            self.assertIsNone(store.penalty_link('113'))
            store.set_penalty_link('114', 30)
            self.assertEqual(store.penalty_link('114'), 30)
            self.assertIsNone(store.penalty_link('113'))
            store.db.close()
            store = Store(directory + '/voting.db')
            self.assertEqual(store.penalty_link('114'), 30)
            store.db.close()

    def test_anonymous_record_matches_exact_team_without_gm_names(self):
        self.ledger['teams'][0]['team_name'] = 'SECRET TEAM'
        lines = comparison_lines(dict(self.result, gm_ids=[]), self.ledger)
        self.assertIn('**80-2**', lines[-1])
        self.assertNotIn('SECRET', '\n'.join(lines))
        with self.assertRaises(ValueError):
            comparison_lines(dict(self.result, gm_ids=[99]), self.ledger)

    def test_excess_deduction_displays_zero_and_carries_to_next_atd(self):
        self.result.update(wins='-10', losses='92')
        self.ledger['teams'][0]['caught_wins'] = 0
        before = copy.deepcopy(self.result)
        self.assertEqual(record_text(self.result), '0-82')
        self.assertEqual(comparison_lines(self.result, self.ledger, draft='114'), [
            'Without buying 10 suspect guesses: **40-42**.',
            'The remaining **10-win deduction carries over to ATD 115**.'])
        self.assertEqual(self.result, before)
        self.assertEqual(carryover_details(self.result, '114'), dict(
            carryover_wins=10, carryover_raw_wins='10', carryover_to_draft='115'))

    def test_carryover_rounding_and_zero_boundary(self):
        self.assertEqual(carryover_details(dict(wins='-9.6'), '114')['carryover_wins'], 10)
        self.assertEqual(carryover_details(dict(wins='0'), '114'), {})
        self.assertEqual(carryover_details(dict(wins='10'), '114'), {})
        self.assertIn('improved by 50', comparison(dict(wins='50'), dict(wins='-10', draft='114'), 1))

    def test_negative_wins_require_link_and_valid_total_and_ledger(self):
        rows = [['Team', 'Wins', 'Losses', 'GM'], ['Example', '-10', '92', 'Someone']]
        with self.assertRaises(ValueError):
            standings(rows)
        self.assertEqual(record_text(standings(rows, allow_negative_wins=True)[0]), '0-82')
        with self.assertRaises(ValueError):
            standings([rows[0], ['Example', '-10', '82', 'Someone']], allow_negative_wins=True)
        with self.assertRaises(ValueError):
            comparison_lines(dict(self.result, wins='-60', losses='142'), self.ledger, draft='114')

    def test_saved_carryover_is_idempotent_and_keeps_raw_formula_values(self):
        self.result.update(wins='-10', losses='92')
        self.result.update(carryover_details(self.result, '114'))
        with TemporaryDirectory() as directory:
            store = Store(directory + '/voting.db')
            for _ in range(2):
                store.save_reveal_results('114', [self.result], 'sheet', 1)
            saved = store.previous_reveal('115', 1)
            self.assertEqual(saved['wins'], '-10')
            self.assertEqual(saved['carryover_wins'], 10)
            self.assertEqual(saved['carryover_to_draft'], '115')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 1)
            # A later standings sync does not need the derived metadata in
            # its input, and correcting the record clears stale carryovers.
            clean = {k: v for k, v in self.result.items() if not k.startswith('carryover_')}
            store.save_reveal_results('114', [clean], 'sheet', 1, replace=True)
            self.assertEqual(store.previous_reveal('115', 1)['carryover_wins'], 10)
            store.save_reveal_results('114', [dict(self.result, wins='5', losses='77')], 'sheet', 1)
            self.assertNotIn('carryover_wins', store.previous_reveal('115', 1))
            store.db.close()


class LedgerResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_is_scoped_to_original_https_hostname(self):
        resolver = LedgerResolver('https://example.test', '192.0.2.1')
        resolver.delegate.resolve = AsyncMock(side_effect=OSError('DNS unavailable'))
        result = await resolver.resolve('example.test', 443)
        self.assertEqual(result[0]['hostname'], 'example.test')
        self.assertEqual(result[0]['host'], '192.0.2.1')
        with self.assertRaises(OSError):
            await resolver.resolve('other.test', 443)
        await resolver.close()
