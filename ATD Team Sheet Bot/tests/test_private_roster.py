import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock
from aiohttp.test_utils import TestClient, TestServer

from private_roster import create_app


class PrivateRosterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        async def change(ctx, text, team, current_sheet_only):
            await ctx.send(f'Updated {team}: {text}')
        self.swap, self.year = AsyncMock(side_effect=change), AsyncMock(side_effect=change)
        self.resolve = Mock(side_effect=lambda logo: 'Cavaliers' if logo == 'TEAM' else None)
        self.roster = AsyncMock(return_value={'team': 'Cavaliers', 'roster': []})
        app = create_app('secret', self.resolve, lambda cid: NS(guild=NS(id=10)) if cid == 99 else None,
                         self.swap, self.year, Path(self.tmp.name)/'receipts.json', roster=self.roster)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.data = dict(action='swap', text='Jerome Kersey / Jamaal Wilkes', channel=99, guild=10,
                         request_id=1000, logo='TEAM')
        self.headers = {'Authorization': 'Bearer secret'}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_auth_channel_guild_and_logo_are_required_before_edit(self):
        response = await self.client.post('/roster-edit', json=self.data)
        self.assertEqual(response.status, 401)
        for changes in (dict(guild=11), dict(channel=98), dict(logo='OTHER'), dict(action='pick')):
            response = await self.client.post('/roster-edit', headers=self.headers, json={**self.data, **changes})
            self.assertEqual(response.status, 400)
        self.swap.assert_not_awaited()

    async def test_swap_is_private_pinned_to_sheet_and_idempotent(self):
        for _ in range(2):
            response = await self.client.post('/roster-edit', headers=self.headers, json=self.data)
            self.assertEqual(response.status, 200)
            self.assertIn('Updated Cavaliers', (await response.json())['message'])
        self.swap.assert_awaited_once()
        args = self.swap.call_args
        self.assertEqual(args.args[1:], ('Jerome Kersey / Jamaal Wilkes', 'Cavaliers'))
        self.assertTrue(args.kwargs['current_sheet_only'])
        self.assertEqual(args.args[0].channel.id, 99)

    async def test_addyear_uses_same_private_team_without_claim(self):
        response = await self.client.post('/roster-edit', headers=self.headers,
                                          json={**self.data, 'action': 'addyear', 'text': 'Larry Bird 1983-84'})
        self.assertEqual(response.status, 200)
        self.year.assert_awaited_once()
        self.assertEqual(self.year.call_args.args[1:], ('Larry Bird 1983-84', 'Cavaliers'))
        self.swap.assert_not_awaited()

    async def test_roster_reads_are_fresh_and_never_write_or_replay_old_receipt(self):
        for _ in range(2):
            response = await self.client.post('/roster-edit', headers=self.headers,
                json={**self.data, 'action': 'roster', 'text': ''})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['team'], 'Cavaliers')
        self.assertEqual(self.roster.await_count, 2)
        self.roster.assert_awaited_with(99, 'Cavaliers')
        self.swap.assert_not_awaited()
        self.year.assert_not_awaited()
        self.assertFalse((Path(self.tmp.name)/'receipts.json').exists())
