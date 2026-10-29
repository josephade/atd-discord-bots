import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from test_wins import command_namespace, typing


class TeamGMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        self.ns['_get_raw'] = AsyncMock(return_value=[['Name', 'Draft 106'], ['New Name', '52'], ['Partner', '52']])
        self.lottos = {
            'ATD 106': {'Indiana Pacers': {'drafters': ['old name', 'Partner', 'New Name']}},
            'ATD 106-D2': {'Indiana Pacers': {'drafters': ['Division Two']}},
            'ATD 10': {'Indiana Pacers': {'drafters': ['Wrong draft']}},
        }

    def test_exact_team_aliases_and_duplicate_owners(self):
        result = self.ns['_team_gms']('106', ' Indiana   Pacers ', 'ATD 106 - Draft', self.lottos, {})
        self.assertEqual(result, ['New Name', 'Partner'])

    def test_correct_division(self):
        result = self.ns['_team_gms']('106', 'Indiana Pacers', 'ATD 106 - D2(Theme)', self.lottos, {})
        self.assertEqual(result, ['Division Two'])

    def test_no_partial_team_or_wrong_draft_fallback(self):
        self.assertEqual(self.ns['_team_gms']('107', 'Indiana Pacers', 'ATD 107', self.lottos, {}), [])
        self.assertEqual(self.ns['_team_gms']('106', 'Pacers', 'ATD 106', self.lottos, {}), [])

    def test_case_sensitive_owner_and_deleted_user(self):
        data = {'ATD 106': {'Indiana Pacers': {'drafters': ['CEO', 'Deleted User', '']}}}
        self.assertEqual(self.ns['_team_gms']('106', 'Indiana Pacers', 'ATD 106', data, {}), ['CEOOfKobeFans'])

    async def run_team(self, image_sent):
        ns = self.ns
        ns['asyncio'] = asyncio
        ns['_resolve_emoji'] = lambda q: q
        ns['_fetch_draft_tab'] = lambda n: ('ATD 106 - Draft', [['Roster']])
        ns['_find_team_roster'] = lambda rows, q: ('Indiana Pacers', [{'name': 'Gilbert Arenas', 'year': '2005-06'}])
        ns['_load_lottos'] = lambda: self.lottos
        ns['_load_profiles'] = lambda: {}
        ns['EMOJI_TEAM_MAP'] = {}
        ns['discord'].utils = NS(get=lambda *a, **k: None, escape_markdown=lambda s: s)
        ns['_try_send_team_screenshot'] = AsyncMock(return_value=image_sent)
        ctx = NS(typing=typing, send=AsyncMock(), guild=NS(emojis=[]))
        await ns['cmd_team'](ctx, args='atd 106 pacers')
        return ctx

    async def test_gms_in_image_caption(self):
        ctx = await self.run_team(True)
        caption = self.ns['_try_send_team_screenshot'].call_args.kwargs['caption']
        self.assertIn('GMs: **New Name**, **Partner**', caption)
        self.assertIn('Wins: **52**', caption)
        ctx.send.assert_not_awaited()

    async def test_gms_in_fallback_embed(self):
        ctx = await self.run_team(False)
        self.assertIn('GMs: **New Name**, **Partner**', ctx.send.call_args.kwargs['embed'].description)
        self.assertIn('Wins: **52**', ctx.send.call_args.kwargs['embed'].description)

    async def test_results_unavailable_still_sends_roster(self):
        self.ns['_get_raw'].side_effect = RuntimeError('Win Sheet unavailable')
        ctx = await self.run_team(False)
        self.assertNotIn('Wins:', ctx.send.call_args.kwargs['embed'].description)

    async def test_unknown_gm_label(self):
        self.lottos = {}
        ctx = await self.run_team(False)
        self.assertIn('GM: Not recorded', ctx.send.call_args.kwargs['embed'].description)


if __name__ == '__main__':
    unittest.main()
