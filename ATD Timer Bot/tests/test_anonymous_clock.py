import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from discord.ext import commands
import admin_routing

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('anonymous_clock_under_test', ROOT / 'theme_clock.py')
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)


def load_commands(ns):
    ns.setdefault('_draft_channel_id', admin_routing.target)
    ns.setdefault('admin_routing', admin_routing)
    tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
    names = {'timermode', 'timerstart', 'timerlink', 'timerskip', 'anontimers', '_anonymous_command_guard'}
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


class AnonymousModes(unittest.IsolatedAsyncioTestCase):
    async def test_timer_off_on_routes_owner_identity_to_theme(self):
        session = NS(draft=NS(mode='anon', theme_draft_id=25))
        adapter = NS(fetch=AsyncMock(return_value={'status': 'active', 'timers_off': True}), render=AsyncMock())
        ns = load_commands(dict(_sessions={99: session}, _theme_clocks=adapter))
        ctx = NS(channel=NS(id=99), author=NS(id=100), invoked_with='timeroff', send=AsyncMock())
        await ns['anontimers'](ctx)
        adapter.fetch.assert_awaited_once_with(session, action='timeroff', actor_id=100)

    def test_disabled_clock_embed_never_requires_a_deadline(self):
        adapter = clock.ThemeClocks(None, {})
        data = dict(mode='anon', status='active', rounds=10, timers_off=True,
                    current=dict(logo='TEAM', round=3, pick=61, deadline=None))
        self.assertIn('off', adapter.embed(data).description)
        data.update(timers_off=False, window_closed=True)
        self.assertIn('10am ET', adapter.embed(data).description)
    async def test_linked_skip_routes_to_theme_without_local_skip_logic(self):
        session = NS(draft=NS(mode='anon', theme_draft_id=25))
        adapter = NS(skip=AsyncMock(return_value={'status': 'active'}), render=AsyncMock())
        fallback = AsyncMock()
        ns = load_commands(dict(_sessions={99: session}, _theme_clocks=adapter, _resolve_command_session=fallback))
        ctx = NS(channel=NS(id=99), author=NS(id=100), message=NS(id=123), send=AsyncMock())
        await ns['timerskip'](ctx)
        adapter.skip.assert_awaited_once_with(session, 100, 123)
        fallback.assert_not_awaited()

    def setup_command(self, state='idle'):
        session = NS(channel_id=99, draft=NS(mode='snake', state=state, theme_draft_id=None, save=Mock()))
        ns = load_commands({'_resolve_command_session': AsyncMock(return_value=session),
                            '_get_session': lambda cid: session, '_sessions': {99: session},
                            '_VALID_MODES': ('snake', 'roundless', 'snake+sbl', 'roundless+sbl', 'snake+budget', 'anon', 'partner'),
                            'commands': commands})
        ctx = NS(channel=NS(id=99), send=AsyncMock(), command=NS(name='timerstart'))
        return session, ns, ctx

    async def test_modes_can_be_selected_without_lotto(self):
        for mode in ('anon', 'partner'):
            session, ns, ctx = self.setup_command()
            await ns['timermode'](ctx, mode)
            self.assertEqual(session.draft.mode, mode)
            session.draft.save.assert_called_once_with(99)
            self.assertIn('Logo-only', ctx.send.call_args.args[0])

    async def test_existing_active_draft_cannot_be_overwritten(self):
        session, ns, ctx = self.setup_command('active')
        await ns['timermode'](ctx, 'partner')
        self.assertEqual(session.draft.mode, 'snake')
        session.draft.save.assert_not_called()

    async def test_linked_mode_cannot_change(self):
        session, ns, ctx = self.setup_command()
        session.draft.theme_draft_id = 42
        await ns['timermode'](ctx, 'partner')
        session.draft.save.assert_not_called()

    async def test_unlinked_anonymous_mode_can_return_to_regular_mode(self):
        session, ns, ctx = self.setup_command()
        session.draft.mode = 'partner'
        await ns['timermode'](ctx, 'snake')
        self.assertEqual(session.draft.mode, 'snake')
        session.draft.save.assert_called_once()

    async def test_legacy_mutations_are_blocked_but_status_allowed(self):
        session, ns, ctx = self.setup_command()
        session.draft.mode = 'partner'
        for name in ('timerstart', 'timerloadlotto', 'timerjumpto', 'timerpause'):
            ctx.command.name = name
            with self.assertRaises(commands.CheckFailure):
                await ns['_anonymous_command_guard'](ctx)
        ctx.command.name = 'timerstatus'
        self.assertTrue(await ns['_anonymous_command_guard'](ctx))
        ctx.command.name = 'timerskip'
        self.assertTrue(await ns['_anonymous_command_guard'](ctx))

    async def test_failed_link_does_not_persist_draft_id(self):
        session, ns, ctx = self.setup_command()
        session.draft.mode = 'partner'
        ns['_theme_clocks'] = NS(fetch=AsyncMock(side_effect=ValueError('Unavailable')))
        await ns['timerlink'](ctx, 42)
        self.assertIsNone(session.draft.theme_draft_id)
        session.draft.save.assert_not_called()


class ClockTransport(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_notice_comes_from_timer_with_logo_and_private_delivery_receipt(self):
        user = NS(send=AsyncMock())
        adapter = clock.ThemeClocks(Mock(get_user=Mock(return_value=user)), {})
        session = NS(channel_id=99, channel=NS(send=AsyncMock(return_value=NS(delete=AsyncMock()))))
        data = dict(mode='anon', status='active', rounds=10, clocks=[], timers_off=True,
                    current=dict(logo='TEAM_LOGO', round=5, pick=10, deadline=None),
                    window_notice=dict(key='2026-11-01:closed', text='Timers are OFF. Resume at 10 AM ET.'))
        adapter.fetch = AsyncMock(side_effect=[data, dict(mode='anon', key='2026-11-01:closed', pending_users=[100, 101], public=False), {}])
        await adapter.tick(session)
        self.assertEqual(user.send.await_count, 2)
        for call in user.send.call_args_list:
            self.assertIn('TEAM_LOGO', call.kwargs['embed'].description)
            self.assertIn('Resume at 10 AM', call.kwargs['embed'].description)
            self.assertFalse(call.kwargs['allowed_mentions'].users)
        self.assertIn('TEAM_LOGO', session.channel.send.call_args.kwargs['embed'].description)
        self.assertEqual(adapter.fetch.call_args.kwargs['notice']['users'], [100, 101])
        self.assertNotIn('100', session.channel.send.call_args.kwargs['embed'].description)

    async def test_completed_period_does_not_send_another_schedule_dm(self):
        bot = Mock()
        adapter = clock.ThemeClocks(bot, {})
        session = NS(channel_id=99, channel=NS(send=AsyncMock()))
        data = dict(mode='anon', status='active', rounds=10, clocks=[], current=None,
                    window_notice=dict(key='closed', text='Resume at 10 AM ET'), window_delivery_complete='closed')
        adapter.fetch = AsyncMock(return_value=data)
        await adapter.tick(session)
        adapter.fetch.assert_awaited_once()
        bot.get_user.assert_not_called()

    async def test_theme_presenter_suppresses_timer_cards_dms_and_removes_previous_card(self):
        bot = Mock()
        adapter = clock.ThemeClocks(bot, {})
        old = NS(delete=AsyncMock())
        adapter.messages[99] = old
        session = NS(channel_id=99, channel=NS(send=AsyncMock()))
        adapter.fetch = AsyncMock(return_value=dict(presenter='theme', clocks=[], current=None,
            window_notice=dict(key='closed', text='Timer off')))
        await adapter.tick(session)
        await adapter.tick(session)
        session.channel.send.assert_not_awaited()
        bot.get_user.assert_not_called()
        old.delete.assert_awaited_once()
        self.assertEqual(adapter.fetch.await_count, 2)

    async def test_manual_skip_pins_token_and_actor_and_rejects_waiting_clock(self):
        adapter = clock.ThemeClocks(Mock(), {})
        session = object()
        data = dict(status='active', current={'token': 'a:0:3'})
        adapter.fetch = AsyncMock(side_effect=[data, {'status': 'active', 'current': None}])
        await adapter.skip(session, 100, 1234)
        self.assertEqual(adapter.fetch.call_args.kwargs, dict(token='a:0:3', action='skip', actor_id=100, request_id=1234))
        adapter.fetch = AsyncMock(return_value=dict(status='active', current=None))
        with self.assertRaisesRegex(ValueError, 'No team'):
            await adapter.skip(session, 100, 1234)
        adapter.fetch.assert_awaited_once()

    async def test_authenticated_snapshot_and_expiry_use_same_channel_and_guild(self):
        requests = []
        data = {'mode': 'partner', 'status': 'active', 'finished': False, 'rounds': 10,
                'current': {'logo': '🦅', 'round': 3, 'deadline': 1, 'token': 'p:0:2'},
                'clocks': [{'token': 'p:0:2', 'deadline': 1}]}
        async def handler(request):
            self.assertEqual(request.headers.get('Authorization'), 'Bearer test-key')
            self.assertEqual(dict(request.query), {'channel': '99', 'guild': '1'})
            requests.append(request.method)
            if request.method == 'POST':
                self.assertEqual((await request.json())['token'], 'p:0:2')
                return web.json_response({**data, 'clocks': [], 'finished': True, 'current': None})
            return web.json_response(data)
        app = web.Application()
        app.router.add_route('*', '/clock/42', handler)
        async with TestServer(app) as server:
            channel = NS(guild=NS(id=1), send=AsyncMock(return_value=NS(edit=AsyncMock())))
            session = NS(channel_id=99, channel=channel, draft=NS(mode='partner', theme_draft_id=42))
            adapter = clock.ThemeClocks(Mock(), {})
            with patch.dict(os.environ, {'THEME_BRIDGE_KEY': 'test-key', 'THEME_BRIDGE_URL': str(server.make_url('')).rstrip('/')}):
                await adapter.tick(session)
            self.assertEqual(requests, ['GET', 'POST'])
            output = channel.send.call_args.kwargs
            self.assertFalse(output['allowed_mentions'].users)
            self.assertFalse(output['allowed_mentions'].roles)
            self.assertFalse(output['allowed_mentions'].everyone)
            self.assertIn('All picks and makeups', output['embed'].description)

    async def test_display_is_logo_only_and_has_no_budget(self):
        adapter = clock.ThemeClocks(Mock(), {})
        embed = adapter.embed({'mode': 'partner', 'status': 'active', 'rounds': 10,
                               'current': {'logo': '🦅', 'round': 3, 'deadline': 12345}})
        self.assertIn('🦅', embed.description)
        self.assertNotIn('<@', embed.description)
        self.assertNotIn('$', embed.description)
        self.assertIn('Round 3 of 10', embed.description)

    async def test_repeated_snapshot_does_not_spam_channel(self):
        adapter = clock.ThemeClocks(Mock(), {})
        session = NS(channel_id=99, channel=NS(send=AsyncMock(return_value=NS(edit=AsyncMock()))))
        data = {'mode': 'anon', 'status': 'paused', 'current': None}
        await adapter.render(session, data)
        await adapter.render(session, data)
        session.channel.send.assert_awaited_once()

    async def test_new_turn_and_makeup_pick_post_fresh_prompt_at_bottom(self):
        adapter = clock.ThemeClocks(Mock(), {})
        messages = [NS(delete=AsyncMock()) for _ in range(3)]
        session = NS(channel_id=99, channel=NS(send=AsyncMock(side_effect=messages)))
        data = {'mode': 'anon', 'status': 'active', 'rounds': 3,
                'current': {'logo': '🦅', 'round': 1, 'deadline': 12345, 'token': 'a:0:1', 'pick': 1}}
        await adapter.render(session, data)
        data = {**data, 'current': {**data['current'], 'logo': '🐻', 'token': 'a:1:2', 'pick': 2}}
        await adapter.render(session, data)
        await adapter.render(session, {**data, 'announcement_revision': 1})
        self.assertEqual(session.channel.send.await_count, 3)
        messages[0].delete.assert_awaited_once()
        messages[1].delete.assert_awaited_once()

    async def test_flux_hold_displays_no_on_clock_team(self):
        adapter = clock.ThemeClocks(Mock(), {})
        embed = adapter.embed({'mode': 'anon', 'status': 'active', 'waiting_for_flux': 2, 'current': None})
        self.assertIn('Round 1 finished', embed.description)
        self.assertIn('commissioner', embed.description)
        self.assertIn('No team is on the clock', embed.description)
        self.assertNotIn('🦅', embed.description)

    async def test_intermediate_pick_transition_does_not_post_false_round_end(self):
        adapter = clock.ThemeClocks(Mock(), {})
        adapter.fetch = AsyncMock(return_value={'clocks': [], 'transitioning': True})
        adapter.render = AsyncMock()
        await adapter.tick(NS())
        adapter.render.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
