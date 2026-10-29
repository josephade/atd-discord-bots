import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from test_wins import command_namespace, typing


class TeamResultTests(unittest.TestCase):
    def setUp(self):
        self.ns = command_namespace()
        self.rows = [
            {'name': 'New Name', 'draft_wins': {95: 54, 96: 0}},
            {'name': 'Cam', 'draft_wins': {95: 54}},
            {'name': 'Other GM', 'draft_wins': {95: 60}},
        ]

    def line(self, owners, number='95', title=''):
        return self.ns['_team_wins_line'](number, owners, self.rows, title)

    def test_matching_co_gm_results_are_one_team_total(self):
        self.assertEqual(self.line(['old name', 'Cam']), '\nWins: **54**')

    def test_fallback_to_co_owner_with_recorded_result(self):
        self.assertEqual(self.line(['Unknown Owner', 'Cam']), '\nWins: **54**')

    def test_zero_is_a_recorded_result(self):
        self.assertEqual(self.line(['New Name'], '96'), '\nWins: **0**')

    def test_missing_result_does_not_borrow_another_draft(self):
        self.assertEqual(self.line(['New Name'], '97'), '')

    def test_conflicting_co_gm_results_are_omitted(self):
        self.assertEqual(self.line(['Cam', 'Other GM']), '')

    def test_unrelated_partial_name_is_not_used(self):
        self.assertEqual(self.line(['Camo']), '')

    def test_second_division_is_not_assigned_first_division_wins(self):
        self.assertEqual(self.line(['Cam'], title='ATD 95 - D2(Theme)'), '')

    def test_canonical_alias_row_has_priority(self):
        self.rows.append({'name': 'old name', 'draft_wins': {95: 12}})
        self.assertEqual(self.line(['old name']), '\nWins: **54**')


class GMTeamResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        self.ns.update(asyncio=asyncio, EMOJI_TEAM_MAP={},
                       _get_raw=AsyncMock(return_value=[['Name', 'Draft 95'], ['HT', '54'], ['Cam', '54']]),
                       _load_lottos=lambda: {'ATD 95': {'Florida Gators': {
                           'drafters': ['HT', 'Cam'], 'players': [{'name': 'Magic Johnson', 'year': '1986-87'}]}}},
                       _try_send_team_screenshot=AsyncMock(return_value=True))
        self.ctx = NS(typing=typing, send=AsyncMock(), guild=NS(emojis=[]))

    async def test_wins_in_gmteam_screenshot_caption(self):
        await self.ns['cmd_gmteam'](self.ctx, args='ATD 95 ht')
        caption = self.ns['_try_send_team_screenshot'].call_args.kwargs['caption']
        self.assertIn('Florida Gators', caption)
        self.assertIn('Co-owners: Cam', caption)
        self.assertIn('Wins: **54**', caption)

    async def test_wins_in_text_fallback(self):
        self.ns['_try_send_team_screenshot'].return_value = False
        await self.ns['cmd_gmteam'](self.ctx, args='ATD 95 ht')
        self.assertIn('Wins: **54**', self.ctx.send.call_args.kwargs['embed'].description)


if __name__ == '__main__':
    unittest.main()
