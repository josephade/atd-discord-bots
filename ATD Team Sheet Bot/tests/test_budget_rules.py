"""Real Flux handler and budget calculations, with no Discord/Sheets traffic."""
import ast
from copy import deepcopy
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import budget_rules


class BudgetRules(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        entry = {'sheet_id': 'sheet-a', 'tab': 'Demo'}
        mapping = {99: entry, 100: dict(entry), 101: {**entry, 'tab': 'Other'},
                   102: {**entry, 'sheet_id': 'sheet-b'}}
        self.saved = {'channel_budget_bumps': {'99': 60}}
        self.ns = {'budget_rules': budget_rules, 're': re, 'FLUX_BOT_ID': 123,
                   'RISING_BUDGET_BUMP': 6, '_rising_budget_rules': {},
                   '_channel_sheet_map': mapping, '_channel_budget_bumps': {99: 60},
                   '_budget_override': None, 'TEAM_BUDGET': 100, '_channel_budgets': {},
                   '_load_config': lambda: deepcopy(self.saved), '_save_config': self.save}
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
        selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name in ('_get_budget', '_persist_rising_budget_rules', '_handle_flux_budget_update', 'cmd_risingbudget')]
        for node in selected:
            node.decorator_list = []
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'bot.py', 'exec'), self.ns)
        self.channel = NS(id=99, send=AsyncMock())
        self.ctx = NS(channel=self.channel, send=self.channel.send)

    def save(self, config):
        self.saved = deepcopy(config)

    def message(self, **changes):
        data = dict(author=NS(id=123, bot=True), channel=self.channel, id=500,
                    content='✅ **Round 2 Flux is done**')
        data.update(changes)
        return NS(**data)

    async def flux(self, **changes):
        await self.ns['_handle_flux_budget_update'](self.message(**changes))

    async def test_fixed_budget_ignores_flux_and_legacy_channel_bump(self):
        await self.flux()
        self.assertEqual(self.ns['_get_budget'](99), 100)
        self.assertEqual(self.ns['_rising_budget_rules'], {})
        self.channel.send.assert_not_awaited()

    async def test_explicit_rising_mode_updates_siblings_but_not_other_drafts(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        self.channel.send.reset_mock()
        await self.flux()
        for cid in (99, 100):
            self.assertEqual(self.ns['_get_budget'](cid), 106)
        for cid in (101, 102):
            self.assertEqual(self.ns['_get_budget'](cid), 100)
        self.channel.send.assert_awaited_once()
        self.assertIn('$106', self.channel.send.call_args.args[0])
        self.assertTrue(self.saved['rising_budget_rules'])

    async def test_repeated_completion_is_counted_once(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        self.channel.send.reset_mock()
        await self.flux()
        await self.flux()
        self.assertEqual(self.ns['_get_budget'](99), 106)
        self.channel.send.assert_awaited_once()
        await self.flux(id=501, content='✅ **Round 3 Flux is done**')
        self.assertEqual(self.ns['_get_budget'](99), 112)

    async def test_untrusted_and_in_progress_messages_do_not_change_budget(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        await self.flux(author=NS(id=124, bot=True))
        await self.flux(author=NS(id=123, bot=False))
        await self.flux(content='⏳ Flux is in progress…')
        await self.flux(content='Someone said Round 2 Flux is done')
        self.assertEqual(self.ns['_get_budget'](99), 100)

    async def test_off_clears_draft_bump_and_prevents_future_increases(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        await self.flux()
        await self.ns['cmd_risingbudget'](self.ctx, 'off')
        await self.flux(id=501)
        self.assertEqual(self.ns['_get_budget'](99), 100)
        self.assertEqual(self.ns['_get_budget'](100), 100)

    async def test_reusing_channel_does_not_inherit_previous_draft_mode(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        await self.flux()
        self.ns['_channel_sheet_map'][99] = {'sheet_id': 'sheet-a', 'tab': 'New Demo'}
        await self.flux(id=501)
        self.assertEqual(self.ns['_get_budget'](99), 100)
        self.assertEqual(self.ns['_get_budget'](100), 106)

    async def test_enabling_twice_preserves_existing_rising_budget(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        await self.flux()
        await self.ns['cmd_risingbudget'](self.ctx, 'on')
        self.assertEqual(self.ns['_get_budget'](99), 106)

    async def test_status_and_invalid_mode_do_not_change_rules(self):
        await self.ns['cmd_risingbudget'](self.ctx, 'status')
        self.assertIn('OFF (fixed budget)', self.channel.send.call_args.args[0])
        await self.ns['cmd_risingbudget'](self.ctx, 'wrong')
        self.assertIn('Usage:', self.channel.send.call_args.args[0])
        self.assertEqual(self.ns['_rising_budget_rules'], {})
