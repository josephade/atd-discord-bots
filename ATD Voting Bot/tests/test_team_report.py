import json
import unittest
from types import SimpleNamespace

import io
from PIL import Image
from team_report import match_team, team_report
from report_image import render_team_report


def record(name, tier, timestamp='10/20/2026 12:00:00', error=''):
    return {'payload': json.dumps(dict(name=name, timestamp=timestamp,
        teams=[['Los Angeles Lakers', tier]], error=error))}


class TeamReportTests(unittest.TestCase):
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
