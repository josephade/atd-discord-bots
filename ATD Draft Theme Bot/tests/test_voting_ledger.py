import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
from aiohttp import web
import test_suspects
import voting_ledger


class VotingLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_and_guild_scope(self):
        app = voting_ledger.create_app('private-key')
        handler = next(route.handler for route in app.router.routes()
                       if route.method == 'GET' and '/penalties/' in str(route.resource))
        request = NS(headers={}, match_info={'draft': '30'}, query={'guild': '1'})
        with self.assertRaises(web.HTTPUnauthorized):
            await handler(request)
        request.headers['Authorization'] = 'Bearer private-key'
        with patch.object(voting_ledger.db, 'get_draft', return_value=dict(guild_id=2)):
            with self.assertRaises(web.HTTPNotFound):
                await handler(request)

    def test_payload_has_no_logos_names_or_playoff_odds(self):
        draft = dict(id=30, guild_id=1)
        with patch.object(voting_ledger.suspects, 'teams', return_value=[{'logo': 'SECRET', 'member_ids': [11, 12]}]), \
             patch.object(voting_ledger.suspects, 'win_deduction_summary', return_value=[
                 dict(member_ids=(100, 101), caught_wins=5, market_wins=10, bought_guesses=2)]), \
             patch.object(voting_ledger.db, 'get_drafter', side_effect=lambda mid: {'user_id': {11: 100, 12: 101}[mid]}), \
             patch.object(voting_ledger.db, 'anonymous_ledger_users', return_value=[100, 101]):
            data = voting_ledger.payload(draft)
        self.assertEqual(data, dict(theme_draft_id=30, guild_id=1, teams=[
            dict(team_name='', user_ids=[100, 101], caught_wins=5, market_wins=10, bought_guesses=2)]))
