import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from test_wins import command_namespace, typing


def team(players, owners=('GM',)):
    return {'drafters': list(owners), 'players': [{'name': p, 'year': '2000-01'} for p in players]}


class PlayerWinsTests(unittest.TestCase):
    def setUp(self):
        self.ns = command_namespace()
        self.build = self.ns['_build_player_win_totals']

    def test_confirmed_wilt_and_rip_variants_merge_into_canonical_players(self):
        ranking, _ = self.build({'ATD 1': {'One': team(['Wilt Chaimberlain', 'Rip Hamiliton'])},
                                 'ATD 113': {'Two': team(['Wilt Chamberlain', 'Richard Hamilton'])}},
                                [{'name': 'GM', 'draft_wins': {1: 28, 113: 25}}])
        self.assertEqual({p['name']: p['wins'] for p in ranking}, {'Wilt Chamberlain': 53, 'Richard Hamilton': 53})
        self.assertTrue(all(p['counted'] == 2 for p in ranking))

    def test_canonical_name_and_typo_on_same_roster_count_once(self):
        ranking, _ = self.build({'ATD 1': {'Team': team(['Wilt Chaimberlain', 'Wilt Chamberlain'])}},
                               [{'name': 'GM', 'draft_wins': {1: 28}}])
        self.assertEqual(ranking, [{'name': 'Wilt Chamberlain', 'wins': 28, 'counted': 1, 'appearances': 1}])

    def test_unconfirmed_similar_names_are_not_merged(self):
        key = self.ns['_player_key']
        self.assertNotEqual(key('Gary Payton'), key('Gary Payton II'))
        self.assertNotEqual(key('Anthony Parker'), key('Tony Parker'))
        self.assertNotEqual(key('Charles Oakley'), key('Charles Barkley'))

    def test_all_approved_aliases_share_the_canonical_identity_and_display(self):
        from player_aliases import PLAYER_ALIASES
        for alias, canonical in PLAYER_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(self.ns['_player_key'](alias), self.ns['_player_key'](canonical))
                self.assertEqual(self.ns['_canonical_player_name'](alias), canonical)
        self.assertEqual(self.ns['_player_key']('Robert Williams'), self.ns['_player_key']('Robert Williams III'))
        self.assertEqual(self.ns['_player_key']('Otto Porter'), self.ns['_player_key']('Otto Porter Jr.'))

    def test_ten_fifty_win_teams_give_lebron_five_hundred_wins(self):
        history = {f'ATD {n}': {'Team': team(['LeBron James'])} for n in range(1, 11)}
        ranking, gaps = self.build(history, [{'name': 'GM', 'draft_wins': {n: 50 for n in range(1, 11)}}])
        self.assertEqual(ranking, [{'name': 'LeBron James', 'wins': 500, 'counted': 10, 'appearances': 10}])
        self.assertEqual(gaps['counted_teams'], 10)

    def test_co_gms_duplicate_roster_entries_and_punctuation_do_not_multiply_wins(self):
        ranking, _ = self.build({'ATD 1': {'Team': team(['Michael Porter Jr.', 'Michael Porter Jr', 'MJ'], ('GM', 'Partner'))}},
            [{'name': 'GM', 'draft_wins': {1: 50}}, {'name': 'Partner', 'draft_wins': {1: 50}}])
        self.assertEqual(len(ranking), 2)
        self.assertTrue(all(p['wins'] == 50 and p['counted'] == 1 for p in ranking))

    def test_default_and_d1_aliases_are_one_team(self):
        t = team(['LeBron James'])
        ranking, _ = self.build({'ATD 1': {'Team': t}, 'ATD 1-D1': {'TEAM': t}}, [{'name': 'GM', 'draft_wins': {1: 50}}])
        self.assertEqual(ranking[0]['wins'], 50)
        self.assertEqual(ranking[0]['appearances'], 1)

    def test_zero_counts_but_missing_conflicting_and_other_division_results_do_not(self):
        history = {'ATD 1': {'Zero': team(['A']), 'Empty': team([])},
                   'ATD 2': {'Missing': team(['A', 'B'])},
                   'ATD 3': {'Conflict': team(['A'], ('GM', 'Partner'))},
                   'ATD 1-D2': {'Other division': team(['A'])}}
        ranking, gaps = self.build(history, [{'name': 'GM', 'draft_wins': {1: 0, 3: 50}},
                                             {'name': 'Partner', 'draft_wins': {3: 60}}])
        self.assertEqual(ranking, [{'name': 'A', 'wins': 0, 'counted': 1, 'appearances': 4}])
        self.assertEqual(gaps['unranked_players'], 1)
        self.assertEqual(gaps['empty_rosters'], 1)
        self.assertEqual(gaps['missing_results'], 2)
        self.assertEqual(gaps['unsupported_divisions'], 1)

    def test_aliases_and_missing_main_owner_can_use_known_co_owner(self):
        ranking, _ = self.build({'ATD 1': {'Team': team(['A'], ('Unknown', 'old name'))}},
                               [{'name': 'New Name', 'draft_wins': {1: 55}}])
        self.assertEqual(ranking[0]['wins'], 55)

    def test_player_on_two_distinct_teams_gets_both_teams_wins(self):
        ranking, gaps = self.build({'ATD 1': {'One': team(['A', 'B']), 'Two': team(['A', 'C'], ('Partner',))}},
            [{'name': 'GM', 'draft_wins': {1: 60}}, {'name': 'Partner', 'draft_wins': {1: 50}}])
        self.assertEqual([p['name'] for p in ranking], ['A', 'B', 'C'])
        self.assertEqual(ranking[0]['wins'], 110)
        self.assertEqual(ranking[0]['counted'], 2)

    def test_year_variants_merge_but_junior_is_distinct_and_ties_are_stable(self):
        history = {'ATD 1': {'One': team(['Tim Hardaway', 'Tim Hardaway Jr.', 'Z Player'])},
                   'ATD 2': {'Two': team(['Tim Hardaway Sr.', 'A Player'])}}
        ranking, _ = self.build(history, [{'name': 'GM', 'draft_wins': {1: 50, 2: 50}}])
        self.assertEqual(ranking[0]['name'], 'Tim Hardaway')
        self.assertEqual(ranking[0]['wins'], 100)
        self.assertEqual([p['name'] for p in ranking[1:]], ['A Player', 'Tim Hardaway Jr.', 'Z Player'])


class PlayerWinsCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        self.ns['asyncio'] = asyncio
        self.ns['_get_raw'] = AsyncMock(return_value=[['Name', 'Draft 1'], ['GM', '50']])
        self.ns['_load_lottos'] = lambda: {'ATD 1': {'Team': team([f'Player {i:02}' for i in range(32)])}}
        self.ctx = NS(typing=typing, send=AsyncMock())

    async def test_five_hundred_wins_in_ten_drafts_shows_fifty_average(self):
        self.ns['_get_raw'].return_value = [['Name'] + [f'Draft {i}' for i in range(1, 11)], ['GM'] + ['50'] * 10]
        self.ns['_load_lottos'] = lambda: {f'ATD {i}': {'Team': team(['LeBron James'])} for i in range(1, 11)}
        await self.ns['cmd_playerwins'](self.ctx)
        sent = self.ctx.send.call_args.kwargs
        self.assertRegex(sent['view'].lines[0], r'500W \| 50\.0$')
        self.assertIn('AVG/draft', sent['embed'].description)

    async def test_average_includes_recorded_zero_but_excludes_missing_results(self):
        self.ns['_get_raw'].return_value = [['Name', 'Draft 1', 'Draft 2', 'Draft 3'], ['GM', '50', '0', '']]
        self.ns['_load_lottos'] = lambda: {f'ATD {i}': {'Team': team(['A'])} for i in range(1, 4)}
        await self.ns['cmd_playerwins'](self.ctx)
        self.assertRegex(self.ctx.send.call_args.kwargs['view'].lines[0], r'50W \| 25\.0$')

    async def test_fifteen_per_page_with_running_ranks_and_buttons(self):
        await self.ns['cmd_playerwins'](self.ctx)
        view = self.ctx.send.call_args.kwargs['view']
        self.assertEqual(view.per_page, 15)
        self.assertEqual(view.total_pages, 3)
        self.assertEqual(len(view.get_embed().description.split('```')[1].strip().splitlines()), 15)
        self.assertIn('Player 00', view.get_embed().description)
        view.page = 2
        self.assertIn('31.', view.get_embed().description)
        self.assertIn('32.', view.get_embed().description)
        self.assertIn('Recorded results only.', view.get_embed().description)

    async def test_empty_results_explain_failure(self):
        self.ns['_get_raw'].return_value = []
        await self.ns['cmd_playerwins'](self.ctx)
        self.assertIn('Could not load', self.ctx.send.call_args.kwargs['embed'].description)

    async def test_unknown_results_do_not_show_false_zero_leaderboard(self):
        self.ns['_get_raw'].return_value = [['Name', 'Draft 1'], ['GM', '']]
        await self.ns['cmd_playerwins'](self.ctx)
        self.assertIn('Missing results are not counted as zero', self.ctx.send.call_args.kwargs['embed'].description)
