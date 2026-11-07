import json
import os
import random
import sys
import unittest
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import import_players
import pricing


def load_players():
    with open(os.path.join(ROOT, 'players.json'), encoding='utf-8') as handle:
        players = json.load(handle)
    return [dict(p, id=i, band=pricing.band_for(p['current_price'])) for i, p in enumerate(players, start=1)]


class OptionTests(unittest.TestCase):
    def test_spec_examples(self):
        self.assertEqual(pricing.options_for(44), [36, 40, 44, 48, 52])
        self.assertEqual(pricing.options_for(43), [35, 39, 43, 47, 51])
        self.assertEqual(pricing.options_for(20), [16, 18, 20, 22, 24])
        self.assertEqual(pricing.options_for(15), [11, 13, 15, 17, 19])
        self.assertEqual(pricing.options_for(12), [10, 11, 12, 13, 14])
        self.assertEqual(pricing.options_for(2), [1, 2, 3, 4, 5])

    def test_every_player_gets_five_valid_options(self):
        for player in load_players():
            options = pricing.options_for(player['current_price'])
            self.assertEqual(len(set(options)), 5, player['name'])
            self.assertEqual(options, sorted(options), player['name'])
            self.assertGreaterEqual(options[0], 1, player['name'])
            self.assertIn(player['current_price'], options, player['name'])
            self.assertLessEqual(options[-1], pricing.OTHER_MAX, player['name'])

    def test_other_accepts_only_whole_dollars_in_range(self):
        for value in (0, 1, 57, 100):
            self.assertTrue(pricing.valid_other(value))
        for value in (101, -3, 12.5, '12', None, True):
            self.assertFalse(pricing.valid_other(value))


class PlayerFileTests(unittest.TestCase):
    def test_players_run_from_jordan_to_ben_simmons(self):
        players = load_players()
        self.assertEqual(len(players), 313)
        self.assertEqual(players[0]['name'], 'Michael Jordan')
        self.assertEqual(players[-1]['name'], 'Ben Simmons')
        self.assertEqual(len({p['name'] for p in players}), 313)

    def test_photos_use_allowed_hosts_and_known_players(self):
        with open(os.path.join(ROOT, 'player_photos.json'), encoding='utf-8') as handle:
            photos = json.load(handle)
        names = {p['name'] for p in load_players()}
        self.assertLessEqual(set(photos), names)
        self.assertIn('Kobe Bryant', photos)
        for name, photo in photos.items():
            self.assertTrue(photo['url'].startswith(('https://cdn.nba.com/', 'https://thumb.wikimedia.org/', 'https://upload.wikimedia.org/')), name)
            if 'wikimedia' in photo['url']:
                self.assertTrue(photo['credit_url'].startswith('https://en.wikipedia.org/wiki/'), name)

    def test_band_sizes_match_the_spec(self):
        self.assertEqual(Counter(p['band'] for p in load_players()), {1: 61, 2: 60, 3: 192})

    def test_import_rejects_a_sheet_that_ends_on_the_wrong_player(self):
        rows = ['Position ,Player,ADP,Value']
        rows.append('Guard/Wing,Michael Jordan,1.50,$44')
        rows.extend(f'Guard,Player {i},{i}.00,$2' for i in range(2, 314))
        with self.assertRaisesRegex(ValueError, 'Ben Simmons'):
            import_players.parse_players('\n'.join(rows))

    def test_import_reads_trimmed_headers_and_dollar_values(self):
        rows = ['Position ,Player,ADP,Value']
        rows.append('Guard/Wing,Michael Jordan,1.50,$44')
        rows.append('Wing/Forward,Bernard King,$191,$5')
        rows.extend(f'Guard ,Player {i},{i}.00,$2' for i in range(3, 313))
        rows.append('Guard,Ben Simmons,302.60,$2')
        rows.append('Wing,Below The Cutoff,303.00,$1')
        players = import_players.parse_players('\n'.join(rows))
        self.assertEqual(len(players), 313)
        self.assertEqual(players[0]['current_price'], 44)
        self.assertEqual(players[1]['adp'], 191.0)
        self.assertEqual(players[2]['position'], 'Guard')
        self.assertEqual(players[-1]['sheet_row'], 314)


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.players = load_players()
        self.band = {p['id']: p['band'] for p in self.players}

    def test_order_contains_every_player_once(self):
        order = pricing.build_order(self.players, random.Random(1))
        self.assertEqual(sorted(order), sorted(p['id'] for p in self.players))

    def test_thirty_batches_of_two_two_six_then_a_final_thirteen(self):
        order = pricing.build_order(self.players, random.Random(2))
        for start in range(0, 300, 10):
            self.assertEqual(Counter(self.band[i] for i in order[start:start + 10]), {1: 2, 2: 2, 3: 6}, start)
        self.assertEqual(Counter(self.band[i] for i in order[300:]), {1: 1, 3: 12})

    def test_batches_are_shuffled_and_orders_differ(self):
        rng = random.Random(3)
        first = pricing.build_order(self.players, rng)
        second = pricing.build_order(self.players, rng)
        self.assertNotEqual(first, second)
        patterns = {tuple(self.band[i] for i in first[start:start + 10]) for start in range(0, 300, 10)}
        self.assertGreater(len(patterns), 1)


if __name__ == '__main__':
    unittest.main()
