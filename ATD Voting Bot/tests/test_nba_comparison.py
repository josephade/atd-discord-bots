import unittest

from nba_comparison import historical_comparison, RECORDS


class HistoricalComparisonTests(unittest.TestCase):
    def row(self, wins, losses=None):
        return dict(wins=str(wins), losses=str(82-wins if losses is None else losses))

    def test_sixers_roast_and_average_bulls_are_exact(self):
        roast = historical_comparison(self.row(9))
        self.assertIn('1972-73 Philadelphia 76ers', roast)
        self.assertIn('(9-73)', roast)
        self.assertIn('Lock in, bro', roast)
        average = historical_comparison(self.row(41))
        self.assertIn('2008-09 Chicago Bulls', average)
        self.assertIn('Perfectly average', average)
        self.assertIn('(41-41)', average)

    def test_fifty_plus_is_elite_and_uses_displayed_record(self):
        for wins in range(50, 74):
            self.assertIn('Elite territory', historical_comparison(self.row(wins)))
        result = historical_comparison(self.row('53.32721336', '28.67278664'))
        self.assertIn('(53-29)', result)
        self.assertIn('Same record', result)

    def test_no_match_never_claims_same_record_and_short_seasons_are_not_equated(self):
        for row in (self.row(82), self.row(70), self.row(41, 25)):
            result = historical_comparison(row)
            self.assertIn('Closest historical comparison', result)
            self.assertNotIn('Same record', result)

    def test_every_bundled_record_is_an_82_game_season_with_a_source(self):
        for record in RECORDS:
            self.assertEqual(record['wins']+record['losses'], 82)
            self.assertTrue(record['source'].startswith('https://'))
        for wins in range(9, 74):
            if wins not in (70, 71):
                self.assertTrue(any(r['wins'] == wins for r in RECORDS), wins)

    def test_matching_franchise_preferred_without_guessing(self):
        row = dict(self.row(22), team='Memphis Grizzlies')
        result = historical_comparison(row)
        self.assertIn('2017-18 Memphis Grizzlies', result)
        self.assertIn('(22-60)', result)
