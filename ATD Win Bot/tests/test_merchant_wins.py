import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from test_wins import command_namespace, typing


def team(players=('James Harden',), owners=('GM',)):
    return {'drafters': list(owners), 'players': [{'name': name} for name in players]}


class MerchantWinTests(unittest.TestCase):
    def setUp(self):
        self.ns = command_namespace()
        self.results = self.ns['_player_drafter_win_results']

    def test_only_wins_from_teams_with_the_player_are_included(self):
        history = {'ATD 1': {'Team': team()}, 'ATD 2': {'Team': team(players=['Stephen Curry'])}, 'ATD 3': {'Team': team()}}
        result = self.results('jamesharden', [{'name': 'GM', 'draft_wins': {1: 50, 2: 70}}], history)
        self.assertEqual(result['GM'], {'wins': 50, 'known': 1, 'missing': {'ATD 3'}})

    def test_aliases_duplicates_and_co_gms_each_get_one_team_result(self):
        history = {'ATD 1': {'Team': team(players=['Rip Hamiliton', 'Richard Hamilton'], owners=['old name', 'New Name', 'Partner'])}}
        rows = [{'name': 'New Name', 'draft_wins': {1: 55}}, {'name': 'Partner', 'draft_wins': {1: 55}}]
        result = self.results('richardhamilton', rows, history)
        self.assertEqual(set(result), {'New Name', 'Partner'})
        self.assertTrue(all(r == {'wins': 55, 'known': 1, 'missing': set()} for r in result.values()))

    def test_zero_is_known_and_no_result_is_not_a_zero(self):
        result = self.results('jamesharden', [{'name': 'GM', 'draft_wins': {1: 0}}],
            {'ATD 1': {'One': team()}, 'ATD 2': {'Two': team()}})
        self.assertEqual(result['GM'], {'wins': 0, 'known': 1, 'missing': {'ATD 2'}})

    def test_other_divisions_conflicting_results_and_unknown_gms_are_disclosed(self):
        history = {'ATD 1': {'One': team(owners=['GM', 'Partner'])},
                   'ATD 1D2': {'Two': team()}, 'ATD 2': {'Three': team(owners=['Unknown'])}}
        rows = [{'name': 'GM', 'draft_wins': {1: 50}}, {'name': 'Partner', 'draft_wins': {1: 60}}]
        result = self.results('jamesharden', rows, history)
        self.assertEqual(result['GM']['missing'], {'ATD 1', 'ATD 1D2'})
        self.assertEqual(result['Partner']['missing'], {'ATD 1'})
        self.assertEqual(result['Unknown'], {'wins': 0, 'known': 0, 'missing': {'ATD 2'}})


class MerchantWinCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        rows = [{'name': 'GM', 'draft_wins': {1: 50, 2: 0}}, {'name': 'Other', 'draft_wins': {1: 60}}]
        history = {'ATD 1': {'One': team(), 'Two': team(owners=['Other'])},
                   'ATD 2': {'One': team()}, 'ATD 3': {'One': team()}}
        self.ns.update(_get_raw=AsyncMock(return_value=['data']), _parse=lambda raw: (rows, [], []),
                       _load_lottos=lambda: history, MEDALS=['1.', '2.', '3.'], ADP_MAP={})
        self.ctx = NS(typing=typing, send=AsyncMock(), message=NS(mentions=[]))

    async def test_count_rank_is_preserved_and_only_missing_specific_drafts_are_listed(self):
        await self.ns['cmd_merchant'](self.ctx, args='James Harden')
        embed = self.ctx.send.call_args.kwargs['embed']
        self.assertIn('GM — ×3 · 50W', embed.description.splitlines()[0])
        self.assertIn('Other — ×1 · 60W', embed.description)
        self.assertEqual(embed.fields[0]['name'], 'Wins unavailable — excluded from totals')
        self.assertEqual(embed.fields[0]['value'], '**GM:** ATD 3')
        self.assertIn('Recorded team wins only', embed.footer['text'])

    async def test_all_missing_is_unavailable_instead_of_zero_wins(self):
        self.ns['_parse'] = lambda raw: ([{'name': 'GM', 'draft_wins': {}}], [], [])
        await self.ns['cmd_merchant'](self.ctx, args='James Harden')
        embed = self.ctx.send.call_args.kwargs['embed']
        self.assertIn('GM — ×3 · wins unavailable', embed.description)
        self.assertIn('ATD 1, ATD 2, ATD 3', embed.fields[0]['value'])
