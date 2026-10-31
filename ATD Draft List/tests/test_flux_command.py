"""Exercise the real command parser and both storage routes without credentials."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from anon_bridge import BridgeError
from flux_rules import parse_prices, find_player, threshold, saved_message
from test_default_years import namespace as parser_namespace


class FluxCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1]/'bot.py'
        command = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == 'cmd_fluxpick')
        command.decorator_list = []
        self.bridge = NS(linked=Mock(return_value=True), add_flux=AsyncMock())
        self.ctx = NS(author=NS(id=100), send=AsyncMock())
        self.ns = parser_namespace()
        self.ns.update(asyncio=asyncio, json=json, BridgeError=BridgeError, flux_threshold=threshold,
                       find_player=find_player, saved_message=saved_message, _dm_only=lambda ctx: True, _anonymous_lists=self.bridge,
                       _flux_source=lambda: dict(sheet='test-sheet-id', tab='Prices'),
                       _fetch_flux_book=Mock(return_value=parse_prices([
                           ['Player', 'Value'], ['Al Horford', 20], ['Derek Harper', 15],
                           ['Bob Lanier', 10], ['Cade Cunningham', 30]])))
        exec(compile(ast.Module(body=[command], type_ignores=[]), 'bot.py', 'exec'), self.ns)

    async def test_multiline_screenshot_preserves_priority_drops_and_target(self):
        await self.ns['cmd_fluxpick'](self.ctx, text='\nAl Horford 2\nDerek Harper 2\nBob Lanier 1\nCade Cunningham 3')
        picks = self.bridge.add_flux.call_args.args[1]
        self.assertEqual([p['player'] for p in picks], ['Al Horford', 'Derek Harper', 'Bob Lanier', 'Cade Cunningham'])
        self.assertEqual([p['flux_drop'] for p in picks], [2, 2, 1, 3])
        self.assertTrue(all(p['flux_target_round'] == 2 for p in picks))
        self.ns['_fetch_flux_book'].assert_called_once()

    async def test_numbered_lines_optional_year_and_single_entry_remain_supported(self):
        await self.ns['cmd_fluxpick'](self.ctx, text="1. Al Horford '23 2\n2. Derek Harper 1")
        self.assertEqual(self.bridge.add_flux.call_args.args[1][0]['year'], '2022-23')
        await self.ns['cmd_fluxpick'](self.ctx, text='Al Horford 2')
        self.assertEqual(len(self.bridge.add_flux.call_args.args[1]), 1)

    async def test_bad_or_duplicate_line_never_pushes_partial_batch(self):
        for text in ('Al Horford 2\nDerek Harper -1', 'Al Horford 2\nUnknown Name 1',
                     'Al Horford 2\nal horford 3', 'Al Horford 2\nInvalid', ''):
            with self.subTest(text=text):
                await self.ns['cmd_fluxpick'](self.ctx, text=text)
                self.bridge.add_flux.assert_not_awaited()

    def normal_mode(self):
        self.bridge.linked.return_value = False
        data = {'100': dict(picks=[], enabled=True, flux_draft_start=1)}
        self.ns.update(_data=data, _ensure_user=Mock(), _flux_in_progress=False,
                       _normal_flux_window=AsyncMock(return_value=True), _save_data=Mock(),
                       _fetch_availability=Mock(return_value={}), _fetch_roster_names=Mock(return_value=set()),
                       _player_available=Mock(return_value=True))
        return data['100']

    async def test_normal_mode_saves_batch_once_and_rejects_repeat_without_partial_write(self):
        data = self.normal_mode()
        await self.ns['cmd_fluxpick'](self.ctx, text='Al Horford 2\nDerek Harper 1')
        self.assertEqual([p['player'] for p in data['picks']], ['Al Horford', 'Derek Harper'])
        self.assertEqual(len(data['flux_rule_attempts']), 2)
        self.ns['_save_data'].assert_called_once()
        await self.ns['cmd_fluxpick'](self.ctx, text='Bob Lanier 2\nAl Horford 1')
        self.assertEqual(len(data['picks']), 2)
        self.ns['_save_data'].assert_called_once()

    async def test_normal_mode_unavailable_later_entry_saves_nothing(self):
        data = self.normal_mode()
        self.ns['_player_available'].side_effect = [True, False]
        await self.ns['cmd_fluxpick'](self.ctx, text='Al Horford 2\nDerek Harper 1')
        self.assertEqual(data['picks'], [])
        self.ns['_save_data'].assert_not_called()
