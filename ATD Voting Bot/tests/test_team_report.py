import json
import unittest
from types import SimpleNamespace

import io
from PIL import Image
from team_report import match_team, team_report, report_from_rows, team_emoji_candidates
from report_image import render_team_report


def record(name, tier, timestamp='10/20/2026 12:00:00', error=''):
    return {'payload': json.dumps(dict(name=name, timestamp=timestamp,
        teams=[['Los Angeles Lakers', tier]], error=error))}


class TeamReportTests(unittest.TestCase):
    def test_rocket_logos_keep_their_identity_even_with_only_one_target_team(self):
        emojis = [SimpleNamespace(name=name) for name in ('Rockets', 'HOU', 'rockets~2', 'DVR', 'SDR', 'Denver')]
        self.assertEqual([e.name for e in team_emoji_candidates('Denver Rockets', emojis)], ['DVR'])
        self.assertEqual([e.name for e in team_emoji_candidates('Houston Rockets', emojis)], ['Rockets', 'HOU', 'rockets~2'])
        self.assertEqual([e.name for e in team_emoji_candidates('San Diego Rockets', emojis)], ['SDR'])
        for query in (':Rockets:', ':HOU:', '<:Rockets:123>'):
            with self.assertRaises(ValueError):
                match_team(query, ['Denver Rockets'])
        self.assertEqual(match_team(':DVR:', ['Denver Rockets']), 'Denver Rockets')

    def test_anonymous_image_never_draws_real_voter_names_including_standouts(self):
        from unittest.mock import patch
        from PIL import ImageDraw
        report = team_report([record(f'PrivateName{i}', 1) for i in range(10)]+[record('SecretStandout', 10)], 'lakers')
        original = ImageDraw.ImageDraw.text
        drawn = []
        def capture(draw, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, 'text', capture):
            anonymous = render_team_report('113', report, anonymous=True)
        self.assertFalse(any('PrivateName' in text or 'SecretStandout' in text for text in drawn))
        self.assertTrue(any('Hidden voter' in text for text in drawn))
        drawn.clear()
        with patch.object(ImageDraw.ImageDraw, 'text', capture):
            public = render_team_report('113', report)
        self.assertTrue(any('SecretStandout' in text for text in drawn))
        self.assertNotEqual(anonymous, public)

    def test_calculated_values_use_second_table_names_and_team_headers(self):
        header = ['Timestamp', 'Enter Discord Name', '[Los Angeles Lakers]', '[Boston Celtics]']
        rows = [header, ['10/28/2026 12:00:00', 'Soapz', 'Tier 1', 'Tier 2'],
                ['10/28/2026 13:00:00', 'Mk', 'Tier 1', 'Tier 3'], [],
                [header[0], header[1], header[3], header[2]],
                ['10/20/2026 12:00:00', 'mk', '18', '3.0'],
                ['10/20/2026 13:00:00', ' Soapz ', '9', '22.2']]
        report = report_from_rows(rows, 'lakers')
        self.assertEqual([(v['name'], v['calculated_value']) for v in report['votes']], [('Mk', '3'), ('Soapz', '22.2')])
        self.assertEqual(report['average'], 1)
        self.assertEqual(report['counts'], [2]+[0]*9)

    def test_missing_and_invalid_values_are_not_zero_and_deleted_votes_stay_deleted(self):
        header = ['Timestamp', 'Enter Discord Name', '[Los Angeles Lakers]']
        current = [['10/28/2026 12:00:00', name, 'Tier 2'] for name in ('A', 'B', 'C', 'D')]
        calculated = [['10/20/2026 12:00:00', name, value] for name, value in
                      [('A', ''), ('B', '#REF!'), ('C', 'NaN'), ('D', '0'), ('Deleted', '22')]]
        report = report_from_rows([header]+current+[header]+calculated, 'lakers')
        self.assertEqual({v['name']: v['calculated_value'] for v in report['votes']},
                         {'A': None, 'B': None, 'C': None, 'D': '0'})

    def test_duplicate_calculated_names_require_unambiguous_timestamp(self):
        header = ['Timestamp', 'Enter Discord Name', '[Los Angeles Lakers]']
        report = report_from_rows([header, ['10/28/2026 12:00:00', 'A', 'Tier 1'],
            ['10/28/2026 12:00:00', 'B', 'Tier 2'], header,
            ['10/20/2026 12:00:00', 'A', '8'], ['10/28/2026 12:00:00', 'A', '3'],
            ['10/20/2026 12:00:00', 'B', '5'], ['10/21/2026 12:00:00', 'B', '6']], 'lakers')
        self.assertEqual([v['calculated_value'] for v in report['votes']], ['3', None])

    def test_latest_original_ballot_controls_grouping_not_formula_table(self):
        header = ['Timestamp', 'Enter Discord Name', '[Los Angeles Lakers]']
        report = report_from_rows([header, ['10/20/2026 12:00:00', 'A', 'Tier 1'],
            ['10/28/2026 12:00:00', 'A', 'Tier 4'], header,
            ['10/20/2026 12:00:00', 'A', '22.2']], 'lakers')
        self.assertEqual([(v['tier'], v['calculated_value']) for v in report['votes']], [(4, '22.2')])

    def test_png_draws_calculated_value_and_unavailable_marker(self):
        from unittest.mock import patch
        from PIL import ImageDraw
        report = team_report([record('Soapz', 1), record('Mk', 2)], 'lakers')
        report['votes'][0]['calculated_value'] = '22.2'
        report['votes'][1]['calculated_value'] = None
        original = ImageDraw.ImageDraw.text
        drawn = []
        def capture(draw, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, 'text', capture):
            render_team_report('113', report)
        self.assertIn('Soapz(22.2)', drawn)
        self.assertIn('Mk(—)', drawn)

    def test_portland_fire_server_emoji_is_distinct_from_trail_blazers(self):
        teams = ['Portland Fire', 'Portland Trail Blazers']
        for query in ('<:PORF:1529021426785325187>', ':PORF:', ':porf:', 'Portland Fire', ':Fire:'):
            self.assertEqual(match_team(query, teams), 'Portland Fire')
        self.assertEqual(match_team(':POR:', teams), 'Portland Trail Blazers')

    def test_emoji_and_abbreviation_and_full_name(self):
        for query in (':lakers:', '<:Lakers:123456789>', '<a:LAL:123456789>', 'LAL', 'Los Angeles Lakers'):
            self.assertEqual(match_team(query, ['Los Angeles Lakers']), 'Los Angeles Lakers')
        with self.assertRaises(ValueError):
            match_team(':lakers:', ['Boston Celtics'])
        with self.assertRaises(ValueError):
            match_team('Portland', ['Portland Fire', 'Portland Trail Blazers'])

    def test_twenty_consensus_votes_and_one_tier_eight_standout(self):
        rows = [record(str(i), i % 3 + 1) for i in range(20)] + [record('Outlier', 8)]
        report = team_report(rows, ':lakers:')
        self.assertEqual([v['name'] for v in report['votes'] if v['standout']], ['Outlier'])
        self.assertEqual(sum(report['counts']), 21)

    def test_tied_votes_do_not_flag_adjacent_tiers_and_small_samples_do_not_flag(self):
        report = team_report([record(str(i), 2) for i in range(20)] + [record('Other', 3)], 'lakers')
        self.assertFalse(any(v['standout'] for v in report['votes']))
        self.assertFalse(any(v['standout'] for v in team_report([record('A', 1), record('B', 10)], 'lakers')['votes']))

    def test_newest_valid_vote_per_identity_including_aliases(self):
        member = SimpleNamespace(id=1, name='first', display_name='First', global_name=None, bot=False)
        rows = [record('alias', 3, '10/21/2026 12:00:00'), record('FIRST', 1),
                record('alias', 10, '10/22/2026 12:00:00', 'invalid')]
        report = team_report(rows, 'lakers', members=[member], aliases={'alias': 1})
        self.assertEqual([(v['name'], v['tier']) for v in report['votes']], [('alias', 3)])

    def test_revisions_and_invalid_tiers(self):
        report = team_report([record('A', 4), record('a', 2), record('B', 11)], 'lakers')
        self.assertEqual(report['counts'][3], 1)
        self.assertEqual(len(report['votes']), 1)

    def test_single_png_includes_all_voter_names_without_truncation(self):
        from unittest.mock import patch
        report = team_report([record(f'Voter{i}', i % 10 + 1) for i in range(71)], 'lakers')
        from PIL import ImageDraw
        original = ImageDraw.ImageDraw.text
        drawn = []
        def capture(draw, xy, text, *args, **kwargs):
            drawn.append((xy, text))
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, 'text', capture):
            png = render_team_report('112', report)
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.format, 'PNG')
        self.assertFalse(getattr(image, 'is_animated', False))
        for i in range(71):
            self.assertIn(f'Voter{i}', [text for _, text in drawn])
        self.assertTrue(all(0 <= xy[1] < image.height for xy, text in drawn))
