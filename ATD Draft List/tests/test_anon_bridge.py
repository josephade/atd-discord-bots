import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from anon_bridge import AnonymousLists, BridgeError
from test_autopick import namespace


def snapshot(picks=None, revision=1):
    return dict(picks=picks or [], revision=revision, version='saved', enabled=True, status='active')


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_owner_steal_report_does_not_trigger_draft_list_sbl_reply(self):
        self.client.owner=100
        self.ctx.guild=object()
        self.ctx.channel=SimpleNamespace(id=1533242104304631969)
        self.ctx.command.name='steal'
        self.assertFalse(await self.client.check(self.ctx))
        self.ctx.send.assert_not_awaited()
        self.client.request.assert_not_awaited()

    async def test_multiple_flux_rules_push_once_in_priority_order(self):
        self.data['100']['anon_draft_id'] = 24
        original = dict(player='Earlier', price=5)
        additions = [dict(player=name, price=20, flux_drop=drop) for name, drop in [('Al Horford', 2), ('Derek Harper', 2), ('Bob Lanier', 1)]]
        self.client.request.side_effect = [dict(snapshot([original]), can_fluxpick=True), snapshot(additions, revision=2)]
        await self.client.add_flux(self.ctx, additions)
        self.assertEqual(self.client.request.call_args.args[2]['picks'], additions)
        self.assertEqual(self.client.request.await_count, 2)
        self.assertIn('Larger drops qualify', self.ctx.send.call_args.args[0])

    async def test_duplicate_in_batch_does_not_push_any_rules(self):
        self.data['100']['anon_draft_id'] = 24
        self.client.request.return_value = dict(snapshot(), can_fluxpick=True)
        with self.assertRaises(BridgeError):
            await self.client.add_flux(self.ctx, [dict(player='Horford'), dict(player='horford')])
        self.assertEqual(self.client.request.await_count, 1)
    async def test_owner_enable_sends_current_price_source_to_theme(self):
        self.client.owner = 999
        self.ctx.author.id = 999
        source = dict(sheet='test-sheet-id', tab='Round Prices')
        self.client.price_source = lambda: source
        self.client.request.return_value = {'members': [dict(user_id='100', **snapshot())]}
        await self.client.mode(self.ctx, 'anon 24')
        self.assertEqual(self.client.request.call_args.args[2]['price_source'], source)

    async def test_fluxpick_rejects_unfinished_round_and_duplicate_player(self):
        self.data['100']['anon_draft_id'] = 24
        pick = dict(player='Booker', price=20, flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.client.request.return_value = dict(snapshot(), can_fluxpick=False, fluxpick_error='Finish your current-round pick')
        with self.assertRaisesRegex(BridgeError, 'current-round pick'):
            await self.client.add_flux(self.ctx, pick)
        self.client.request.return_value = dict(snapshot([pick]), can_fluxpick=True)
        await self.client.add_flux(self.ctx, pick)
        self.assertTrue(self.client.request.call_args.args[2]['replace_flux'])
    async def test_owner_enables_every_registered_gm_without_importing_normal_lists(self):
        self.client.owner = 999
        self.ctx.author.id = 999
        roster = {'members': [dict(user_id='100', **snapshot()), dict(user_id='101', **snapshot())]}
        self.client.request.return_value = roster
        await self.client.mode(self.ctx, 'anon 24')
        self.assertTrue(all(self.data[uid]['anon_draft_id'] == 24 for uid in ('100', '101')))
        self.assertEqual(self.data['100']['picks'], [])
        self.assertTrue(self.data['101']['anon_owner_managed'])
        self.assertIn('all 2 registered GMs', self.ctx.send.call_args.args[0])
        self.assertEqual(self.client.request.call_args.args[2], {'action': 'enable'})

    async def test_nonowner_cannot_change_everyones_mode(self):
        self.client.owner = 999
        with self.assertRaises(BridgeError): await self.client.mode(self.ctx, 'anon 24')
        self.client.request.assert_not_awaited()

    async def test_owner_normal_withdraws_all_team_lists_before_unlinking(self):
        self.client.owner = 100
        self.data['100']['anon_draft_id'] = 24
        self.data['101'] = dict(anon_draft_id=24, picks=[], enabled=True)
        await self.client.mode(self.ctx, 'normal')
        self.assertTrue(all(not self.client.linked(uid) for uid in ('100', '101')))
        self.assertEqual(self.client.request.call_args.args[2], {'action': 'disable'})

    async def asyncSetUp(self):
        self.data = {'100': dict(picks=[dict(player='Old normal pick')], enabled=True, emoji='OLD')}
        self.save = Mock()
        self.client = AnonymousLists(Mock(), self.data, self.save, lambda text: dict(player=text, price=1, year=''), lambda: 10)
        self.ctx = SimpleNamespace(author=SimpleNamespace(id=100), guild=None, send=AsyncMock(), command=SimpleNamespace(name='setlist'))
        self.client.request = AsyncMock(return_value=snapshot())

    async def test_enabling_mode_never_uploads_old_normal_list(self):
        await self.client.mode(self.ctx, 'anon 24')
        self.assertEqual(self.data['100']['anon_draft_id'], 24)
        self.assertEqual(self.data['100']['picks'], [])
        self.client.request.assert_awaited_once_with('100', 24)

    async def test_ranked_setlist_goes_only_to_private_api_with_version_guard(self):
        self.data['100']['anon_draft_id'] = 24
        picks = [dict(player='First', price=1, year=''), dict(player='Second', price=1, year='')]
        self.client.request.side_effect = [snapshot(revision=7), snapshot(picks, revision=8)]
        await self.client.handle(self.ctx, 'First\nSecond')
        payload = self.client.request.call_args.args[2]
        self.assertEqual(payload['picks'], picks)
        self.assertEqual(payload['expected_revision'], 7)
        self.assertTrue(payload['version'])
        self.assertEqual(self.data['100']['picks'], picks)

    async def test_server_clear_replaces_local_list_and_failed_update_does_not_restore_it(self):
        self.data['100']['anon_draft_id'] = 24
        self.client.request.side_effect = [snapshot(), BridgeError('unavailable')]
        with self.assertRaises(BridgeError): await self.client.handle(self.ctx, 'First')
        self.assertEqual(self.data['100']['picks'], [])
        self.assertTrue(self.client.linked(100))

    async def test_return_to_normal_withdraws_remote_queue_before_unlinking(self):
        self.data['100']['anon_draft_id'] = 24
        await self.client.mode(self.ctx, 'normal')
        payload = self.client.request.call_args.args[2]
        self.assertEqual(payload['picks'], [])
        self.assertFalse(payload['enabled'])
        self.assertFalse(self.client.linked(100))

    async def test_server_commands_cannot_publish_anonymous_list(self):
        self.data['100']['anon_draft_id'] = 24
        self.ctx.guild = object()
        self.assertFalse(await self.client.check(self.ctx))
        self.client.request.assert_not_awaited()

    async def test_normal_public_autopick_is_disabled_for_linked_gm(self):
        ns = namespace()
        ns['_data'] = {'100': dict(anon_draft_id=24, picks=[dict(player='Secret')])}
        ns['_last_autopick'] = None
        channel = SimpleNamespace(send=AsyncMock())
        await ns['_do_auto_pick_inner'](channel, SimpleNamespace(id=100), 1, set(), None)
        channel.send.assert_not_awaited()
