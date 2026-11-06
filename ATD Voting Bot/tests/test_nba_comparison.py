import unittest

from nba_comparison import historical_comparison, comparisons_for_standings, record_reaction, RECORDS


class HistoricalComparisonTests(unittest.TestCase):
    def test_reactions_describe_the_actual_record_and_seed(self):
        for wins in (0, 22, 40, 41, 46, 50, 73):
            row = dict(self.row(wins), seed=19)
            for index in range(8):
                text = record_reaction(row, index)
                self.assertIn('19th seed', text)
                self.assertTrue(str(wins) in text or f'{abs(2*wins-82)} more' in text)
                self.assertNotIn('motivation for the next draft', text)
                self.assertNotIn('uncomfortable questions', text)
        losing = record_reaction(dict(self.row(22), seed=29))
        self.assertIn('22-60', losing)
        self.assertIn('19 more wins', losing)
        winning = record_reaction(dict(self.row(46), seed=11))
        self.assertIn('4 wins short of 50', winning)

    def test_full_draft_has_unique_historical_seasons_and_reactions(self):
        import re
        rows = [dict(self.row(53), seed=i, team=f'Team {i}') for i in range(1, 33)]
        notes = comparisons_for_standings(rows)
        self.assertEqual(len(set(notes.values())), 32)
        self.assertEqual(len({re.search(r'\[([^]]+)\]', n)[1] for n in notes.values()}), 32)
        self.assertEqual(len({n.split('). ', 1)[1] for n in notes.values()}), 32)
        for note in notes.values():
            if 'Same record' in note:
                self.assertIn('(53-29)', note)
        self.assertEqual(notes, comparisons_for_standings(list(reversed(rows))))

    def test_rare_records_do_not_repeat_or_claim_false_exact_match(self):
        rows = [dict(self.row(9), seed=i, team=f'Team {i}') for i in range(1, 4)]
        notes = comparisons_for_standings(rows)
        self.assertIn('Same record', notes[1])
        self.assertIn('(9-73)', notes[1])
        self.assertIn('Closest historical comparison', notes[2])
        self.assertNotIn('Same record', notes[2])
        self.assertEqual(len(set(notes.values())), 3)

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
