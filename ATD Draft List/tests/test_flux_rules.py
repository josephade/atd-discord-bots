import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from flux_rules import parse_prices, qualify, threshold, saved_message, failure_message, failure_reports
from test_autopick import namespace


class FluxRuleTests(unittest.TestCase):
    def test_large_list_confirmation_and_failures_stay_readable_and_within_discord_limit(self):
        picks = [dict(player=f'Player {i}', flux_drop=3) for i in range(100)]
        text = saved_message(picks)
        self.assertIn('100 options', text)
        self.assertIn('+95 more', text)
        self.assertLess(len(text), 1000)
        notices = [failure_message(pick, {}) for pick in picks]
        reports = failure_reports(notices)
        self.assertTrue(all(len(report) < 2000 for report in reports))
        self.assertLess(len(reports), 10)
        self.assertIn('100 rules removed', reports[0])
    def test_drop_greater_than_requested_also_qualifies(self):
        book = parse_prices([['Player', 'Value', 'Round 2'], ['Alvan Adams', 10, 7]])
        self.assertEqual(qualify(dict(player='Alvan Adams', flux_drop=2, flux_target_round=2), book)['price'], 7)
    def test_rule_is_only_for_its_upcoming_flux_not_a_previous_or_later_one(self):
        rule = dict(player='Booker', flux_drop=3, flux_target_round=3)
        book = parse_prices([['Player', 'Value', 'Round 2'], ['Booker', 30, 27]])
        self.assertIsNone(qualify(rule, book))
        rule['flux_target_round'] = 2
        book = parse_prices([['Player', 'Value', 'Round 2', 'Round 3'], ['Booker', 30, 27, 24]])
        self.assertIsNone(qualify(rule, book))

    def test_duplicate_rows_do_not_block_unrelated_players_or_guess_conflicting_prices(self):
        book = parse_prices([['Player', 'Value', 'Round 2'], ['Booker', 30, 27], ['Booker', 30, 27],
                             ['Conflicting', 30, 27], ['Conflicting', 20, 19]])
        self.assertEqual(qualify(dict(player='Booker', flux_drop=3), book)['price'], 27)
        self.assertIsNone(qualify(dict(player='Conflicting', flux_drop=1), book))

    def test_only_latest_flux_drop_counts_not_cumulative_drop(self):
        book = parse_prices([['Player', 'Value', 'Round 2', 'Round 3'], ['Booker', 30, 28, 27]])
        self.assertIsNone(qualify(dict(player='Booker', flux_drop=3), book))
        self.assertEqual(qualify(dict(player='Booker', flux_drop=1), book)['price'], 27)

    def test_exact_threshold_and_fractional_prices_use_real_current_cost(self):
        book = parse_prices([['Player', 'Value', 'Round 2'], ['Booker', '$30.50', '27.50']])
        pick = qualify(dict(player='Booker', flux_drop=3, price=999), book)
        self.assertEqual(pick['price'], 27.5)
        self.assertIsNone(qualify(dict(player='Booker', flux_drop=3.1), book))

    def test_invalid_or_missing_newest_cells_and_no_flux_do_not_fall_back(self):
        for bad in ('', '#REF!', 'NaN', 'Infinity', -1):
            book = parse_prices([['Player', 'Value', 'Round 2', 'Round 3'], ['Booker', 30, 20, bad]])
            self.assertIsNone(qualify(dict(player='Booker', flux_drop=3), book))
        for header, row in [(['Player', 'Value'], ['Booker', 30]),
                            (['Player', 'Value', 'Round 3'], ['Booker', 30, 20])]:
            self.assertIsNone(qualify(dict(player='Booker', flux_drop=3), parse_prices([header, row])))

    def test_increase_and_ambiguous_player_do_not_qualify(self):
        book = parse_prices([['Player', 'Value', 'Round 2'], ['Michael Jordan', 40, 42], ['DeAndre Jordan', 10, 5]])
        self.assertIsNone(qualify(dict(player='Michael Jordan', flux_drop=0), book))
        self.assertIsNone(qualify(dict(player='Jordan', flux_drop=3), book))
        for bad in ('abc', '-1', 'NaN', 'inf'):
            with self.assertRaises(ValueError): threshold(bad)

    def test_round_columns_sort_numerically_and_portable_rules_match_theme(self):
        book = parse_prices([['Player', 'Round 10', 'Value', 'Round 9'], ['Booker', 20, 40, 23]])
        self.assertEqual(qualify(dict(player='Booker', flux_drop=3), book)['price'], 20)
        source = Path(__file__).resolve().parents[1]/'flux_rules.py'
        mirror = source.parent.parent/'ATD Draft Theme Bot'/'flux_rules.py'
        if mirror.exists(): self.assertEqual(source.read_text(encoding='utf-8'), mirror.read_text(encoding='utf-8'))


class FluxAutoPickTests(unittest.IsolatedAsyncioTestCase):
    async def test_flux_completion_rechecks_current_turn_without_public_gm_mentions(self):
        ns = namespace()
        ns.update(DRAFT_CHANNEL_ID=1, _flux_in_progress=True,
                  _flux_turn=dict(pick_num=1, user_ids=[100]), _maybe_flux_pick=AsyncMock(), _settle_normal_flux=AsyncMock())
        message = NS(author=NS(bot=True, id=1466803932310405181), channel=NS(id=1),
                     content='✅ **Round 3 Flux is done**')
        await ns['on_message'](message)
        self.assertFalse(ns['_flux_in_progress'])
        ns['_maybe_flux_pick'].assert_awaited_once_with('100')

    async def test_qualified_pick_uses_live_cost_and_preserves_existing_turn_wipe(self):
        ns = namespace()
        source = dict(sheet='test-sheet', tab='Prices')
        pick = dict(player='Booker', year='2022-23', price=30, flux_drop=3, flux_source=source, flux_target_round=2)
        books = {json.dumps(source, sort_keys=True): parse_prices([['Player', 'Value', 'Round 2'], ['Booker', 30, 27]])}
        ns.update(json=json, _flux_books=AsyncMock(return_value=books), qualify_flux_pick=qualify,
            _flux_source=lambda: source, _flux_turn=dict(pick_num=1, user_ids=[100]),
            _data={'100': dict(enabled=True, emoji='TEAM', picks=[pick])}, _last_autopick=None,
            _resolve_emoji_input=lambda s: s, _is_valid_emoji=lambda s: True, _save_data=Mock(),
            _fetch_availability=Mock(return_value={'booker': True}), _fetch_roster_names=Mock(return_value=set()))
        channel, user = NS(send=AsyncMock()), NS(id=100, display_name='GM', send=AsyncMock())
        await ns['_do_auto_pick_inner'](channel, user, 1, set())
        channel.send.assert_awaited_once_with("1. TEAM Booker '23 $27")
        self.assertEqual(ns['_data']['100']['picks'], [])

    async def test_unmet_condition_keeps_list_and_never_posts(self):
        ns = namespace()
        source = dict(sheet='test-sheet', tab='Prices')
        pick = dict(player='Booker', flux_drop=3, flux_source=source, flux_target_round=2)
        books = {json.dumps(source, sort_keys=True): parse_prices([['Player', 'Value', 'Round 2'], ['Booker', 30, 29]])}
        ns.update(json=json, _flux_books=AsyncMock(return_value=books), qualify_flux_pick=qualify,
            _data={'100': dict(enabled=True, emoji='TEAM', picks=[pick])}, _last_autopick=None,
            _resolve_emoji_input=lambda s: s, _is_valid_emoji=lambda s: True, _save_data=Mock(),
            _fetch_availability=Mock(return_value={'booker': True}), _fetch_roster_names=Mock(return_value=set()))
        channel, user = NS(send=AsyncMock()), NS(id=100, display_name='GM', send=AsyncMock())
        await ns['_do_auto_pick_inner'](channel, user, 1, set())
        channel.send.assert_not_awaited()
        self.assertEqual(ns['_data']['100']['picks'], [pick])
