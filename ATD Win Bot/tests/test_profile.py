from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from test_wins import command_namespace, typing


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        self.profiles = {}
        self.drafters = [{'name': 'CCarp'}, {'name': 'New Name'}, {'name': 'Wrong GM'}]
        self.member = NS(id=123, display_name='CCarp', global_name='CCarp', name='ccarp')
        self.ctx = NS(author=NS(id=456), message=NS(mentions=[self.member]),
                      guild=NS(get_member=lambda uid: self.member), typing=typing, send=AsyncMock())
        self.ns.update(_load_profiles=lambda: self.profiles, _get_raw=AsyncMock(return_value=['data']),
                       _parse=lambda raw: (self.drafters, None, None),
                       _load_lottos=lambda: {}, _compute_seeds=lambda d, l: {},
                       _profile_by_name=lambda p, n: (None, None),
                       ProfileView=lambda guild, d, profile, lottos, seeds, **kwargs:
                       NS(get_embed=lambda: NS(drafter=d['name'])))

    async def run_profile(self, name):
        await self.ns['cmd_profile'](self.ctx, name=name)
        return self.ctx.send.call_args.kwargs['embed']

    async def test_plain_name_and_mention_find_same_profile(self):
        plain = await self.run_profile('ccarp')
        mention = await self.run_profile('<@123>')
        self.assertEqual(plain.drafter, 'CCarp')
        self.assertEqual(mention.drafter, plain.drafter)

    async def test_legacy_mention_and_whitespace(self):
        self.assertEqual((await self.run_profile('  <@!123>  ')).drafter, 'CCarp')

    async def test_linked_sheet_name_takes_priority_over_discord_nickname(self):
        self.profiles['123'] = {'sheet_name': 'old name'}
        self.assertEqual((await self.run_profile('<@123>')).drafter, 'New Name')

    async def test_unlinked_user_can_match_username_when_nickname_differs(self):
        self.member.display_name = 'Fancy Nickname'
        self.member.global_name = 'Another Nickname'
        self.assertEqual((await self.run_profile('<@123>')).drafter, 'CCarp')

    async def test_own_profile_without_arguments_still_works(self):
        self.profiles['456'] = {'sheet_name': 'CCarp'}
        self.assertEqual((await self.run_profile('')).drafter, 'CCarp')

    async def test_unmatched_mention_shows_linking_hint(self):
        self.member.display_name = self.member.global_name = self.member.name = 'Unknown'
        self.assertIn('!linkprofile', (await self.run_profile('<@123>')).description)

    async def test_unlinked_self_shows_both_supported_inputs(self):
        result = await self.run_profile('')
        self.assertIn('!profile @user', result.description)
        self.ns['_get_raw'].assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
