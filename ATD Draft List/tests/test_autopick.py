"""Run without Discord/Google credentials: exercise bounded reads and auto-picks."""
import ast
import asyncio
import logging
import re
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock


def namespace():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    wanted = {'_is_taken', '_iter_player_rows', '_fetch_availability', '_fetch_availability_inner',
              '_player_available', '_resolve_canonical_name', '_do_auto_pick_inner', '_build_pick_message', 'on_message', '_with_default_year',
              '_current_draft_history', '_backfill_drafted_session'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    for node in nodes:
        node.decorator_list = []
    ns = dict(asyncio=asyncio, time=time, log=logging.getLogger('test'),
              discord=NS(TextChannel=object, User=object, Message=object, DMChannel=type('DMChannel', (), {})),
              SPREADSHEET_ID='test', PLAYERS_SHEET_NAME="Players' List",
              _availability_lock=threading.Lock(), _availability_cache=None,
              _availability_cache_ts=0, _AVAILABILITY_TTL=60,
              _canonical_names={}, _drafted_this_session=set(), re=re,
              get_default_year=lambda name: None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


def cell(name, taken=False):
    return {'formattedValue': name, 'effectiveFormat': {'backgroundColor':
            {'red': 0, 'green': 0, 'blue': 0} if taken else {}}}


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.ns = namespace()
        self.service = Mock()
        self.api = self.service.spreadsheets.return_value
        self.ns['_build_sheets_service'] = Mock(return_value=self.service)

    def test_large_formatted_sheet_is_read_in_bounded_filtered_batches(self):
        self.api.values.return_value.get.return_value.execute.return_value = {'values': [['Name']] * 501}
        self.api.get.return_value.execute.side_effect = [
            {'sheets': [{'data': [{'rowData': [{'values': [cell(name, taken)]}]}]}]}
            for name, taken in [('Shawn Kemp', True), ('Terry Porter', False), ('Other', False)]
        ]
        result = self.ns['_fetch_availability'](True)
        self.assertFalse(result['shawn kemp'])
        self.assertTrue(result['terry porter'])
        requests = self.api.get.call_args_list
        self.assertEqual([c.kwargs['ranges'] for c in requests], [
            ["'Players'' List'!A1:D250"], ["'Players'' List'!A251:D500"], ["'Players'' List'!A501:D501"]])
        for request in requests:
            self.assertEqual(request.kwargs['fields'],
                             'sheets(data(rowData(values(formattedValue,effectiveFormat(backgroundColor)))))')
        self.service.close.assert_called_once()
        self.assertEqual(self.ns['_resolve_canonical_name']('terry porter'), 'Terry Porter')
        self.assertEqual(self.ns['_fetch_availability'](), result)
        self.assertEqual(self.ns['_build_sheets_service'].call_count, 1)

    def test_empty_sheet_does_not_request_formatting(self):
        self.api.values.return_value.get.return_value.execute.return_value = {}
        self.assertEqual(self.ns['_fetch_availability'](True), {})
        self.api.get.assert_not_called()
        self.service.close.assert_called_once()

    def test_fetch_failure_closes_client_and_preserves_previous_cache(self):
        self.ns['_availability_cache'] = {'existing': True}
        self.ns['_canonical_names'] = {'existing': 'Existing'}
        self.api.values.return_value.get.return_value.execute.side_effect = RuntimeError('network')
        with self.assertRaisesRegex(RuntimeError, 'network'):
            self.ns['_fetch_availability'](True)
        self.service.close.assert_called_once()
        self.assertEqual(self.ns['_availability_cache'], {'existing': True})
        self.assertEqual(self.ns['_canonical_names'], {'existing': 'Existing'})

    def test_live_pick_overrides_stale_sheet(self):
        self.ns['_drafted_this_session'].add('shawn kemp')
        self.assertFalse(self.ns['_player_available']('Shawn Kemp', {'shawn kemp': True}, set()))
        self.assertTrue(self.ns['_player_available']('Terry Porter', {'terry porter': True}, set()))


class AutoPickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = namespace()
        self.user = NS(id=74, display_name='Crispy', send=AsyncMock())
        self.channel = NS(send=AsyncMock())
        self.ns.update(_data={'74': {'enabled': True, 'emoji': ':team:', 'picks': [
            {'player': 'shawn kemp'}, {'player': 'terry porter'}]}},
            _last_autopick=None,
            _resolve_emoji_input=lambda s: s, _is_valid_emoji=lambda s: True,
            _save_data=Mock(), _fetch_availability=Mock(return_value={'shawn kemp': False, 'terry porter': True}),
            _fetch_roster_names=Mock(return_value=set()))
        self.ns['_canonical_names'] = {'terry porter': 'Terry Porter'}

    async def test_crispy_pick_74_skips_kemp_and_posts_porter(self):
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.channel.send.assert_awaited_once_with('74. :team: Terry Porter')
        self.assertEqual(self.ns['_last_autopick']['pick_num'], 74)
        self.assertIn('terry porter', self.ns['_drafted_this_session'])
        self.assertEqual(self.ns['_data']['74']['picks'], [])
        self.user.send.assert_awaited_once()

    async def test_remaining_list_cannot_be_reused_next_round(self):
        self.ns['_fetch_availability'].return_value = {'shawn kemp': True, 'terry porter': True}
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.assertEqual(self.ns['_data']['74']['picks'], [])
        self.assertEqual(self.ns['_last_autopick']['retry_picks'], [{'player': 'terry porter'}])
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 104, set())
        self.channel.send.assert_awaited_once()

    async def test_auto_pick_clears_co_gm_list_but_preserves_other_teams(self):
        self.ns['_data']['75'] = {'emoji': ':team:', 'picks': [{'player': 'Rasheed Wallace'}]}
        other = {'emoji': ':other:', 'picks': [{'player': 'Someone Else'}]}
        self.ns['_data']['76'] = other
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.assertEqual(self.ns['_data']['74']['picks'], [])
        self.assertEqual(self.ns['_data']['75']['picks'], [])
        self.assertEqual(other['picks'], [{'player': 'Someone Else'}])

    async def test_same_turn_retry_does_not_consume_fresh_next_round_list(self):
        self.ns['_fetch_availability'].return_value = {'shawn kemp': True, 'terry porter': True}
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        retry = self.ns['_last_autopick']['retry_picks']
        fresh = [{'player': 'Rasheed Wallace'}]
        self.ns['_data']['74']['picks'] = fresh
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set(), retry)
        self.assertIs(self.ns['_data']['74']['picks'], fresh)
        self.assertEqual(self.channel.send.await_args.args[0], '74. :team: Terry Porter')

    async def test_failed_send_keeps_original_list(self):
        original = list(self.ns['_data']['74']['picks'])
        self.channel.send.side_effect = RuntimeError('send failed')
        with self.assertRaisesRegex(RuntimeError, 'send failed'):
            await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.assertEqual(self.ns['_data']['74']['picks'], original)
        self.assertNotIn('terry porter', self.ns['_drafted_this_session'])

    async def test_new_list_submitted_during_send_is_preserved(self):
        fresh = [{'player': 'Rasheed Wallace'}]
        async def send(_):
            self.ns['_data']['74']['picks'] = fresh
        self.channel.send.side_effect = send
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.assertIs(self.ns['_data']['74']['picks'], fresh)

    async def test_manual_pick_clears_owner_even_if_already_tracked(self):
        self.ns.update(DRAFT_CHANNEL_ID=1, TIMER_BOT_ID=999,
                       bot=NS(user=NS(id=1000), process_commands=AsyncMock()),
                       _has_sbl_intent=lambda s: False)
        self.ns['_drafted_this_session'].add('shawn kemp')
        author = NS(id=74, bot=False, display_name='Crispy')
        message = NS(author=author, channel=NS(id=1), content='74. Shawn Kemp', mentions=[], embeds=[])
        await self.ns['on_message'](message)
        self.assertEqual(self.ns['_data']['74']['picks'], [])

    async def test_manual_co_gm_pick_clears_team_list(self):
        self.ns.update(DRAFT_CHANNEL_ID=1, TIMER_BOT_ID=999,
                       bot=NS(user=NS(id=1000), process_commands=AsyncMock()),
                       _has_sbl_intent=lambda s: False)
        author = NS(id=75, bot=False, display_name='Partner')
        message = NS(author=author, channel=NS(id=1), content='74. :team: Shawn Kemp', mentions=[], embeds=[])
        await self.ns['on_message'](message)
        self.assertEqual(self.ns['_data']['74']['picks'], [])

    async def test_sheet_fetch_runs_off_the_discord_event_loop(self):
        loop_thread = threading.get_ident()
        def fetch(**kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return {'shawn kemp': False, 'terry porter': True}
        self.ns['_fetch_availability'] = fetch
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.channel.send.assert_awaited_once_with('74. :team: Terry Porter')

    async def test_paused_list_is_not_posted(self):
        self.ns['_data']['74']['enabled'] = False
        await self.ns['_do_auto_pick_inner'](self.channel, self.user, 74, set())
        self.channel.send.assert_not_awaited()
        self.ns['_fetch_availability'].assert_not_called()

    async def test_startup_does_not_carry_danny_ainge_from_previous_draft(self):
        self.ns.update(TIMER_BOT_ID=999, _has_sbl_intent=lambda s: False)
        self.ns['_data']['74']['picks'] = [{'player': 'Danny Ainge'}, {'player': 'Shawn Kemp'}]
        messages = [NS(content=c, author=NS(bot=False)) for c in [
            '2. Shawn Kemp 1995-96', '1. LeBron James 2012-13',
            '241. Danny Ainge 1987-88', '1. Michael Jordan 1990-91']]
        async def history(**kwargs):
            for message in messages:
                yield message
        await self.ns['_backfill_drafted_session'](NS(history=history))
        self.assertNotIn('danny ainge', self.ns['_drafted_this_session'])
        self.assertIn('shawn kemp', self.ns['_drafted_this_session'])
        self.assertTrue(self.ns['_player_available']('Danny Ainge', {'danny ainge': True}, set()))

    async def test_history_without_known_draft_start_is_not_trusted(self):
        self.ns['_has_sbl_intent'] = lambda s: False
        messages = [NS(content='241. Danny Ainge 1987-88')]
        self.assertEqual(self.ns['_current_draft_history'](messages), [])

    async def test_new_draft_first_pick_clears_stale_tracker_and_caches(self):
        self.ns.update(DRAFT_CHANNEL_ID=1, TIMER_BOT_ID=999,
                       bot=NS(user=NS(id=1000), process_commands=AsyncMock()),
                       _has_sbl_intent=lambda s: False,
                       _availability_cache={'danny ainge': False}, _roster_cache={'danny ainge'})
        self.ns['_drafted_this_session'].add('danny ainge')
        message = NS(author=NS(id=75, bot=False, display_name='GM'), channel=NS(id=1),
                     content='1. LeBron James', mentions=[], embeds=[])
        await self.ns['on_message'](message)
        self.assertNotIn('danny ainge', self.ns['_drafted_this_session'])
        self.assertIsNone(self.ns['_availability_cache'])
        self.assertIsNone(self.ns['_roster_cache'])


if __name__ == '__main__':
    unittest.main()
