import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestServer

import roster_bridge


class RosterBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_roster_embed_preserves_pf_and_shows_all_empty_slots(self):
        rows = [dict(player='', year='', price='') for _ in range(10)]
        rows[3] = dict(player='LeBron James', year='2012-13', price='$43')
        embed = roster_bridge.roster_embed(dict(team='Cleveland Cavaliers', roster=rows), '<:Cavs:1234>')
        self.assertIn('**PF** \u00b7 LeBron James \u00b7 2012-13 \u00b7 $43', embed.fields[0].value)
        for position in ('PG', 'SG', 'SF', 'C'):
            self.assertIn(f'**{position}** \u00b7 \u2014', embed.fields[0].value)
        for bench in range(1, 6):
            self.assertIn(f'**{bench}** \u00b7 \u2014', embed.fields[1].value)
        self.assertEqual(embed.footer.text, '1/10 players')
        self.assertEqual(embed.thumbnail.url, 'https://cdn.discordapp.com/emojis/1234.png')
    async def test_server_uses_only_assigned_team_and_selection_channel(self):
        received = []
        async def receive(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer secret')
            received.append(await request.json())
            return web.json_response({'message': 'Updated privately'})
        app = web.Application()
        app.router.add_post('/roster-edit', receive)
        server = TestServer(app)
        await server.start_server()
        ctx = NS(draft={'theme': 'anon_budget_draft', 'guild_id': 10, 'state': {'teams': [
            dict(member_ids=[1, 2], logo='OUR'), dict(member_ids=[3], logo='THEIRS')]}},
                 state={'timer_channel_id': 99}, message=NS(id=1000))
        try:
            with patch.dict(os.environ, THEME_BRIDGE_KEY='secret', TEAM_SHEET_BRIDGE_URL=str(server.make_url('')).rstrip('/')):
                self.assertEqual(await roster_bridge.edit(ctx, {'id': 2}, 'swap', 'Jerome Kersey / Jamaal Wilkes'), 'Updated privately')
                error = await roster_bridge.edit(ctx, {'id': 999}, 'swap', 'Jerome Kersey / Jamaal Wilkes')
                self.assertIn('not configured', error)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]['logo'], 'OUR')
            self.assertEqual(received[0]['channel'], 99)
            self.assertEqual(received[0]['request_id'], 1000)
        finally:
            await server.close()
