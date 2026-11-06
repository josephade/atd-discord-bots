"""Offline integration tests: real SQLite and themes, no Discord/Sheets traffic."""
import ast
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_root_tmp = tempfile.TemporaryDirectory()
os.environ['STATE_DIR'] = _root_tmp.name
# No credentials or external service clients are needed for these tests.
fake_sheets = types.ModuleType('sheets')
fake_sheets.load_player_pool = Mock()
fake_sheets.load_prices = Mock()
sys.modules['sheets'] = fake_sheets

import db
import roster
from themes.base import DraftContext, _draft_locks
from themes import partner_draft as partner
from themes.anon_budget_draft import AnonBudgetDraftTheme
from themes.anon_draft import AnonDraftTheme

NAMES = ['LeBron James', 'Michael Jordan', 'Stephen Curry', 'Larry Bird',
         'Magic Johnson', 'Tim Duncan', 'Kobe Bryant', 'Kevin Garnett',
         'Dirk Nowitzki', 'David Robinson', 'Chris Paul', 'Scottie Pippen']


class PartnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name) / 'test.db')
        db.init_db()
        _draft_locks.clear()
        self.theme = partner.PartnerDraftTheme()
        self.highlight = patch.object(partner, 'highlight_taken', new_callable=AsyncMock)
        self.mock_highlight = self.highlight.start()
        price_validation = patch.object(AnonBudgetDraftTheme, 'validate_round_price', new_callable=AsyncMock, return_value=None)
        price_validation.start()
        self.addCleanup(price_validation.stop)
        fake_sheets.load_player_pool.side_effect = None
        fake_sheets.load_prices.side_effect = None
        fake_sheets.load_player_pool.return_value = (NAMES, {})
        fake_sheets.load_prices.return_value = {name: 10 for name in NAMES}

    async def asyncTearDown(self):
        for key in list(partner._timer._tasks):
            partner._timer.cancel(key)
        await asyncio.sleep(0)
        self.highlight.stop()
        self.tmp.cleanup()

    async def make(self, members=4, **settings):
        options = {**self.theme.default_settings(), 'roster_size': 2, 'timer_window_et': False, **settings}
        draft_id = db.create_draft(1, None, 'partner_draft', 'Test', options)
        for n in range(members):
            db.add_drafter(draft_id, 1000 + draft_id * 100 + n, f'Owner{n}')
        ctx = DraftContext(Mock(get_user=Mock(return_value=None)), draft_id)
        ctx.bot.get_user.return_value = types.SimpleNamespace(send=AsyncMock())
        ctx.dm = AsyncMock(return_value=True)
        ctx.announce = AsyncMock()
        return ctx

    async def start(self, **settings):
        ctx = await self.make(**settings)
        with patch.object(partner.random, 'shuffle', side_effect=lambda values: None):
            await self.theme.start(ctx)
        db.set_draft_status(ctx.draft_id, 'active')
        return ctx

    def member(self, ctx, pair=0, role='a'):
        return db.get_drafter(ctx.state['pairs'][pair][role])

    async def lists(self, ctx, pair, text):
        for role in ('a', 'b'):
            await self.theme.on_list(ctx, self.member(ctx, pair, role), text)

    async def force(self, ctx, i=0, r=0):
        async with partner.draft_lock(ctx.draft_id):
            await self.theme._resolve_locked(ctx, i, r, forced=True)
            await self.theme._drain_locked(ctx)

    async def test_two_round_shuffle_and_reverse_persisted(self):
        ctx = await self.start(roster_size=4)
        turns = ctx.state['turns']
        self.assertEqual([x['pair'] for x in turns], [0, 1, 1, 0, 0, 1, 1, 0])
        for r in (0, 2):
            self.assertEqual([x['pair'] for x in turns if x['round'] == r],
                             list(reversed([x['pair'] for x in turns if x['round'] == r + 1])))
        await self.theme.resume_timers(ctx)
        self.assertEqual(ctx.state['turns'], turns)

    async def test_queued_lists_wait_for_turn_and_only_one_pick_per_round(self):
        ctx = await self.start()
        await self.lists(ctx, 1, ', '.join(NAMES[3:6]))
        self.assertEqual(db.get_all_picks(ctx.draft_id), {})
        await self.lists(ctx, 0, ', '.join(NAMES[:3]))
        self.assertEqual(ctx.state['turn_index'], 2)
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx)['id']), [NAMES[0]])
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx, 1)['id']), [NAMES[3]])
        await self.force(ctx, 0, 0)
        self.assertEqual(len(db.get_picks(ctx.draft_id, self.member(ctx)['id'])), 1)

    async def test_neither_submits_skip_then_makeup_without_rewinding(self):
        ctx = await self.start()
        await self.force(ctx)
        self.assertEqual(ctx.state['pairs'][0]['owed_rounds'], [0])
        self.assertEqual(ctx.state['turn_index'], 1)
        self.assertFalse(self.theme.is_pick_phase_complete(ctx))
        for role in ('a', 'b'):
            await self.theme.claim_makeup(ctx, self.member(ctx, role=role), ', '.join(NAMES[:3]))
        self.assertEqual(ctx.state['turn_index'], 1)
        self.assertEqual(ctx.state['pairs'][0]['owed_rounds'], [])
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx)['id']), [NAMES[0]])
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx, role='b')['id']), [NAMES[0]])

    async def test_one_partner_at_expiry_uses_their_valid_list(self):
        ctx = await self.start()
        await self.theme.on_list(ctx, self.member(ctx, role='b'), ', '.join(NAMES[:3]))
        await self.force(ctx)
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx)['id']), [NAMES[0]])

    async def test_shared_choice_and_alternating_priority(self):
        ctx = await self.start(members=2)
        await self.theme.on_list(ctx, self.member(ctx), ', '.join(NAMES[:3]))
        await self.theme.on_list(ctx, self.member(ctx, role='b'), ', '.join([NAMES[3], NAMES[1], NAMES[4]]))
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx)['id']), [NAMES[1]])
        await self.theme.on_list(ctx, self.member(ctx), ', '.join(NAMES[3:6]))
        await self.theme.on_list(ctx, self.member(ctx, role='b'), ', '.join(NAMES[6:9]))
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx)['id']), [NAMES[1], NAMES[6]])
        self.assertTrue(self.theme.is_pick_phase_complete(ctx))

    async def test_canonical_list_and_invalid_players(self):
        ctx = await self.start()
        member = self.member(ctx)
        for text in ('Fake Player, Michael Jordan, Larry Bird', 'LeBron James, LeBron James, Larry Bird', 'Larry Bird'):
            await self.theme.on_list(ctx, member, text)
            self.assertEqual(ctx.state['pairs'][0]['lists'], {})
        result = self.theme._parse_list(ctx, 'lebron james, MICHAEL JORDAN, Stephen Curry')
        self.assertEqual(result, NAMES[:3])

    async def test_taken_players_and_stale_queued_list_filtered(self):
        ctx = await self.start()
        await self.lists(ctx, 1, ', '.join(NAMES[:3]))
        await self.lists(ctx, 0, ', '.join(NAMES[:3]))
        self.assertEqual(db.get_picks(ctx.draft_id, self.member(ctx, 1)['id']), [NAMES[1]])
        response = await self.theme.on_list(ctx, self.member(ctx, 1), ', '.join(NAMES[:3]))
        self.assertIn('already drafted', response)

    async def test_price_source_errors_fail_before_pairs_or_dms(self):
        for prices in ({}, {NAMES[0]: 10}, {n: float('nan') for n in NAMES}):
            ctx = await self.make()
            fake_sheets.load_prices.return_value = prices
            with self.assertRaises(ValueError):
                await self.theme.start(ctx)
            self.assertEqual(ctx.state, {})
            ctx.dm.assert_not_awaited()
        ctx = await self.make()
        fake_sheets.load_prices.side_effect = RuntimeError('offline')
        with self.assertRaises(ValueError):
            await self.theme.start(ctx)
        self.assertEqual(ctx.state, {})

    async def test_budget_counts_shared_team_once_and_rejects_overspend(self):
        ctx = await self.start(members=2, budget=15)
        await self.lists(ctx, 0, ', '.join(NAMES[:3]))
        self.assertEqual(self.theme._remaining(ctx, ctx.state['pairs'][0]), 5)
        response = await self.theme.on_list(ctx, self.member(ctx), ', '.join(NAMES[3:6]))
        self.assertIn('$5', response)
        state = ctx.state
        state['prices'][NAMES[3]] = 5
        ctx.save_state(state)
        for role in ('a', 'b'):
            await self.theme.on_list(ctx, self.member(ctx, role=role), f'{NAMES[3]}, {NAMES[4]}, {NAMES[5]}')
        self.assertEqual(len(db.get_picks(ctx.draft_id, self.member(ctx)['id'])), 1)
        # Corrupt persisted lists cannot bypass the transactional budget guard.
        before = ctx.state
        self.assertFalse(db.commit_partner_pick(ctx.draft_id, 0, 1, NAMES[4], False, before, before))
        self.assertEqual(len(db.get_picks(ctx.draft_id, self.member(ctx)['id'])), 1)

    async def test_transaction_rejects_stale_state_and_rolls_back_both_members(self):
        ctx = await self.start()
        before = ctx.state
        altered = copy.deepcopy(before)
        altered['turn_index'] = 1
        ctx.save_state(altered)
        self.assertFalse(db.commit_partner_pick(ctx.draft_id, 0, 0, NAMES[0], False, before, before))
        self.assertEqual(db.get_all_picks(ctx.draft_id), {})
        self.assertEqual(ctx.state, altered)

    async def test_exact_remaining_budget_and_no_sheet_roster_write(self):
        ctx = await self.start(members=2, budget=15, list_size=1)
        await self.lists(ctx, 0, NAMES[0])
        state = ctx.state
        state['prices'][NAMES[1]] = 5
        ctx.save_state(state)
        await self.lists(ctx, 0, NAMES[1])
        self.assertEqual(self.theme._remaining(ctx, ctx.state['pairs'][0]), 0)
        self.assertEqual(self.mock_highlight.await_count, 2)
        self.assertTrue(self.theme.is_pick_phase_complete(ctx))

    async def test_budget_proposal_rechecked_before_finalizing(self):
        ctx = await self.make()
        member = db.list_drafters(ctx.draft_id)[0]
        entry = {'pick_number': 1, 'proposal': {'player': NAMES[0]}, 'pending_from': [member['id']]}
        ctx.save_state({'live_pick_number': 1, 'teams': [{
            'member_ids': [member['id']], 'budget_spent': 90, 'open_picks': [entry],
            'timer_pick_number': 1, 'timer_deadline': time.time() + 20}]})
        theme = AnonBudgetDraftTheme()
        with patch.object(AnonDraftTheme, '_finalize_pick', new_callable=AsyncMock) as finalize, \
             patch.object(theme, '_start_team_timer') as arm:
            await theme._finalize_pick(ctx, 0, entry, NAMES[0], '2013', 20)
            finalize.assert_not_awaited()
            self.assertIsNone(ctx.state['teams'][0]['open_picks'][0]['proposal'])
            self.assertTrue(0 < arm.call_args.args[-1] < .34)
            await theme._finalize_pick(ctx, 0, entry, NAMES[0], '2013', 10)
            finalize.assert_awaited_once()

    async def test_partner_manual_submission_is_unnecessary_and_cannot_replace_picks(self):
        ctx = await self.start(members=2, roster_size=1)
        await self.lists(ctx, 0, ', '.join(NAMES[:3]))
        member = self.member(ctx)
        channel = types.SimpleNamespace(send=AsyncMock())
        ctx.bot.get_channel.return_value = channel
        # Give the draft a reporting channel, without creating a Discord client.
        with db._conn() as con:
            con.execute('UPDATE drafts SET admin_channel_id=99 WHERE id=?', (ctx.draft_id,))
        source = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_submitroster')
        node.decorator_list = []
        ns = {'db': db, 'roster': roster, 'DraftContext': DraftContext, 'bot': ctx.bot,
              'themes': types.SimpleNamespace(get_theme=lambda key: self.theme),
              '_dm_only': lambda event: True,
              '_resolve_active_drafter': lambda uid: (ctx.draft, member),
              '_dedup_teams': lambda *args: [db.get_drafter(member['id'])],
              '_team_roster_embed': Mock(side_effect=AssertionError('Premature roster disclosure'))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        event = types.SimpleNamespace(author=types.SimpleNamespace(id=member['user_id']), send=AsyncMock())
        await ns['cmd_submitroster'](event, text=NAMES[5])
        self.assertIn('No roster submission is needed', event.send.call_args.args[0])
        channel.send.assert_not_awaited()
        self.assertFalse(db.get_drafter(member['id'])['roster_submitted'])
        self.assertEqual(self.theme.final_roster_rows(ctx, member), [NAMES[0]])
        self.assertTrue(self.theme.roster_ready(ctx, member))

    async def test_reveal_uses_completed_picks_without_submissions(self):
        ctx = await self.start(members=2, roster_size=1)
        await self.lists(ctx, 0, ', '.join(NAMES[:3]))
        self.assertTrue(all(not d['roster_submitted'] for d in ctx.drafters()))
        fake_sheets.write_draft_results = Mock(return_value='Results')
        ctx.bot.get_user.return_value = types.SimpleNamespace(send=AsyncMock())
        event = types.SimpleNamespace(send=AsyncMock())
        source = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_reveal_draft')
        ns = {'db': db, 'DraftContext': DraftContext, 'bot': ctx.bot, 'asyncio': asyncio,
              'themes': types.SimpleNamespace(get_theme=lambda key: self.theme),
              '_resolve_draft': lambda text: ctx.draft, 'sheets': fake_sheets,
              'ROSTER_SPREADSHEET_ID': 'test', 'ROSTER_TAB_NAME': 'Results', 'log': Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        await ns['_reveal_draft'](event, str(ctx.draft_id))
        fake_sheets.write_draft_results.assert_called_once()
        exported_teams = fake_sheets.write_draft_results.call_args.args[2]
        self.assertEqual(list(exported_teams.values()), [[NAMES[0]]])
        self.assertEqual(ctx.draft['status'], 'complete')
        self.assertTrue(ctx.state['identities_revealed'])
        # A skipped round still blocks reveal and never exports partial teams.
        other = await self.start(members=2, roster_size=1)
        await self.force(other)
        ns['_resolve_draft'] = lambda text: other.draft
        fake_sheets.write_draft_results.reset_mock()
        await ns['_reveal_draft'](event, str(other.draft_id))
        fake_sheets.write_draft_results.assert_not_called()

    async def test_budget_draft_roster_is_its_picks_without_submission(self):
        ctx = await self.make(members=2, rounds=1)
        member = ctx.drafters()[0]
        ctx.save_state({'teams': [{'member_ids': [member['id']]}]})
        theme = AnonBudgetDraftTheme()
        self.assertFalse(theme.roster_ready(ctx, member))
        db.record_pick(ctx.draft_id, member['id'], NAMES[0], pick_number=1)
        self.assertTrue(theme.roster_ready(ctx, member))
        self.assertEqual(theme.final_roster_rows(ctx, member), [NAMES[0]])
        self.assertFalse(db.get_drafter(member['id'])['roster_submitted'])

    async def test_resending_team_does_not_reveal_partner(self):
        ctx = await self.start()
        member = self.member(ctx)
        user = types.SimpleNamespace(id=member['user_id'], display_name=member['display_name'])
        user.send = AsyncMock()
        ctx.bot.get_user.return_value = user
        event = types.SimpleNamespace(send=AsyncMock(), message=types.SimpleNamespace(mentions=[user]))
        source = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_theme')
        node.decorator_list = []
        ns = {'db': db, 'DraftContext': DraftContext, 'bot': ctx.bot,
              'themes': types.SimpleNamespace(get_theme=lambda key: self.theme),
              '_resolve_draft': lambda text: ctx.draft}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        await ns['cmd_theme'](event, 'team', rest=f'{ctx.draft_id} @owner')
        private = user.send.call_args.kwargs['content']
        partner_name = self.member(ctx, role='b')['display_name']
        self.assertIn('Anonymous team', private)
        self.assertNotIn(partner_name, private)

    async def test_pause_resume_and_restart_keep_remaining_clock(self):
        ctx = await self.start()
        state = ctx.state
        state['pairs'][0]['deadlines']['0'] = time.time() + 42
        ctx.save_state(state)
        db.set_draft_status(ctx.draft_id, 'paused')
        await self.theme.cancel(ctx)
        self.assertEqual(ctx.state['pairs'][0]['deadlines'], {})
        remaining = ctx.state['pairs'][0]['paused_remaining']['0']
        self.assertTrue(40 < remaining <= 42)
        await self.force(ctx)
        self.assertEqual(ctx.state['turn_index'], 0)
        db.set_draft_status(ctx.draft_id, 'active')
        self.assertEqual(await self.theme.resume_timers(ctx), 1)
        self.assertTrue(39 < partner._timer.remaining_seconds((ctx.draft_id, 0, 0)) <= 42)
        partner._timer.cancel((ctx.draft_id, 0, 0))
        self.assertEqual(await self.theme.resume_timers(ctx), 1)
        self.assertTrue(39 < partner._timer.remaining_seconds((ctx.draft_id, 0, 0)) <= 42)

    async def test_expired_restart_skips_and_two_drafts_have_separate_timers(self):
        ctx = await self.start()
        other = await self.start()
        self.assertTrue(partner._timer.is_running((other.draft_id, 0, 0)))
        partner._timer.cancel((ctx.draft_id, 0, 0))
        state = ctx.state
        state['pairs'][0]['deadlines']['0'] = time.time() - 20
        ctx.save_state(state)
        await self.theme.resume_timers(ctx)
        await asyncio.sleep(.05)
        self.assertEqual(ctx.state['turn_index'], 1)
        self.assertEqual(ctx.state['pairs'][0]['owed_rounds'], [0])
        self.assertEqual(other.state['turn_index'], 0)
        self.assertTrue(partner._timer.is_running((other.draft_id, 0, 0)))

    async def test_privacy_guard_and_failed_dm_does_not_charge(self):
        ctx = await self.start()
        for text in ('LeBron James', 'James', 'LBJ', 'MJ', '<@12345>', 'my name is Joe',
                     'https://example.com', 'Owner1', '@someone', 'discord.gg/abc'):
            self.assertTrue(self.theme._blocked_message(ctx, text), text)
        self.assertFalse(self.theme._blocked_message(ctx, 'We should prioritize defense and rebounding.'))
        ctx.dm.return_value = False
        member = self.member(ctx)
        response = await self.theme.on_message(ctx, member, 'We should prioritize defense.')
        self.assertIn('allowance was not used', response)
        self.assertEqual(db.relay_message_stats(ctx.draft_id, member['id']), (0, 0))
        ctx.dm.return_value = True
        await self.theme.on_message(ctx, member, 'We should prioritize defense.')
        self.assertEqual(db.relay_message_stats(ctx.draft_id, member['id'])[0], 1)

    async def test_final_roster_price_requirements_fail_closed(self):
        ctx = await self.start()
        self.assertFalse(self.theme.validate_final_roster(ctx, self.member(ctx), [NAMES[0]])[0])
        self.assertFalse(self.theme.validate_final_roster(ctx, self.member(ctx), [NAMES[0]] * 2)[0])
        self.assertTrue(self.theme.validate_final_roster(ctx, self.member(ctx), NAMES[:2])[0])
        state = ctx.state
        state['prices'] = {}
        ctx.save_state(state)
        with self.assertRaises(ValueError):
            self.theme.roster_requirements(ctx)

    async def linked_partner(self):
        ctx = await self.make()
        ctx.save_state({'timer_channel_id': 99})
        with patch.object(partner.random, 'shuffle', side_effect=lambda values: None):
            await self.theme.start(ctx)
        db.set_draft_status(ctx.draft_id, 'active')
        return ctx

    async def test_external_partner_clock_has_no_local_timer_or_identity_payload(self):
        import json
        import timer_bridge
        ctx = await self.linked_partner()
        self.assertEqual(ctx.state['timer_channel_id'], 99)
        self.assertFalse(partner._timer.is_running((ctx.draft_id, 0, 0)))
        data = timer_bridge.snapshot(ctx.draft)
        self.assertEqual(data['mode'], 'partner')
        self.assertEqual(data['current']['token'], 'p:0:0')
        serialized = json.dumps(data)
        for member in ctx.drafters():
            self.assertNotIn(member['display_name'], serialized)
            self.assertNotIn(str(member['user_id']), serialized)
        self.assertNotIn('budget', serialized)
        self.assertNotIn('prices', serialized)
        self.assertNotIn('lists', serialized)

    async def test_external_expiry_is_idempotent_and_pause_safe(self):
        import timer_bridge
        ctx = await self.linked_partner()
        await timer_bridge.expire(ctx.bot, ctx.draft_id, 'p:0:0')
        self.assertEqual(ctx.state['turn_index'], 0)  # Too early.
        state = ctx.state
        state['pairs'][0]['deadlines']['0'] = time.time() - 1
        ctx.save_state(state)
        db.set_draft_status(ctx.draft_id, 'paused')
        await timer_bridge.expire(ctx.bot, ctx.draft_id, 'p:0:0')
        self.assertEqual(ctx.state['turn_index'], 0)
        db.set_draft_status(ctx.draft_id, 'active')
        await asyncio.gather(*(timer_bridge.expire(ctx.bot, ctx.draft_id, 'p:0:0') for _ in range(2)))
        self.assertEqual(ctx.state['turn_index'], 1)
        self.assertEqual(ctx.state['pairs'][0]['owed_rounds'], [0])
        for role in ('a', 'b'):
            await self.theme.claim_makeup(ctx, self.member(ctx, role=role), ', '.join(NAMES[:3]))
        self.assertEqual(ctx.state['turn_index'], 1)

    async def test_clock_api_requires_secret_and_matching_draft_channel_guild(self):
        import timer_bridge
        from aiohttp.test_utils import TestClient, TestServer
        ctx = await self.linked_partner()
        async with TestClient(TestServer(timer_bridge.create_app(ctx.bot, 'test-key'))) as client:
            path = f'/clock/{ctx.draft_id}?channel=99&guild=1'
            self.assertEqual((await client.get(path)).status, 401)
            headers = {'Authorization': 'Bearer test-key'}
            self.assertEqual((await client.get(path, headers=headers)).status, 200)
            self.assertEqual((await client.get(path.replace('channel=99', 'channel=98'), headers=headers)).status, 404)
            self.assertEqual((await client.get(path.replace('guild=1', 'guild=2'), headers=headers)).status, 404)
            state = ctx.state
            state['pairs'][0]['deadlines']['0'] = time.time() - 1
            ctx.save_state(state)
            response = await client.post(path, headers=headers, json={'token': 'p:0:0'})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['current']['token'], 'p:1:0')
            await client.post(path, headers=headers, json={'token': 'p:0:0'})
            self.assertEqual(ctx.state['turn_index'], 1)

    async def test_budget_final_scheduled_skip_keeps_makeups_open(self):
        import timer_bridge
        from themes import anon_draft
        ctx = await self.make(rounds=1)
        members = ctx.drafters()[:2]
        with db._conn() as con:
            con.execute("UPDATE drafts SET theme='anon_budget_draft' WHERE id=?", (ctx.draft_id,))
        teams = []
        for i, member in enumerate(members):
            db.update_drafter_state(member['id'], {'team_index': i})
            teams.append({'logo': f'Team {i + 1}', 'member_ids': [member['id']], 'open_picks': [],
                          'budget_spent': 0, 'timer_pick_number': None, 'timer_deadline': None, 'skip_count': 0})
        ctx.save_state({'timer_channel_id': 99, 'teams': teams})
        ctx.bot.get_channel.return_value = types.SimpleNamespace(send=AsyncMock())
        theme = AnonBudgetDraftTheme()
        await theme.start(ctx)
        db.set_draft_status(ctx.draft_id, 'active')
        state = ctx.state
        state['teams'][0]['timer_deadline'] = time.time() + 42
        ctx.save_state(state)
        db.set_draft_status(ctx.draft_id, 'paused')
        await theme.cancel(ctx)
        db.set_draft_status(ctx.draft_id, 'active')
        await theme.resume_timers(ctx)
        self.assertTrue(39 < ctx.state['teams'][0]['timer_deadline'] - time.time() <= 42)
        for i in range(2):
            self.assertFalse(anon_draft._timer.is_running((ctx.draft_id, i)))
            state = ctx.state
            state['teams'][i]['timer_deadline'] = time.time() - 1
            ctx.save_state(state)
            await timer_bridge.expire(ctx.bot, ctx.draft_id, f'a:{i}:{i + 1}')
        self.assertEqual(ctx.draft['status'], 'active')
        self.assertTrue(ctx.state['scheduled_turns_finished'])
        self.assertFalse(timer_bridge.snapshot(ctx.draft)['finished'])
        for i, member in enumerate(members):
            await theme.on_pick(ctx, db.get_drafter(member['id']), f'{NAMES[i]} 2013 10')
        self.assertEqual(ctx.draft['status'], 'complete')
        self.assertTrue(timer_bridge.snapshot(ctx.draft)['finished'])
        ctx.bot.get_channel.assert_any_call(99)
        self.assertNotIn(934052208624930848, [call.args[0] for call in ctx.bot.get_channel.call_args_list])

    async def budget_demo(self, count=2, rounds=3):
        ctx = await self.make(members=count, rounds=rounds)
        members = ctx.drafters()
        with db._conn() as con:
            con.execute("UPDATE drafts SET theme='anon_budget_draft' WHERE id=?", (ctx.draft_id,))
        teams = []
        for i, member in enumerate(members):
            db.update_drafter_state(member['id'], {'team_index': i})
            teams.append({'logo': f'Team {i + 1}', 'member_ids': [member['id']], 'open_picks': [],
                          'budget_spent': 0, 'timer_pick_number': None, 'timer_deadline': None, 'skip_count': 0})
        ctx.save_state({'timer_channel_id': 99, 'teams': teams})
        ctx.bot.get_channel.return_value = types.SimpleNamespace(send=AsyncMock(return_value=types.SimpleNamespace(id=500)))
        theme = AnonBudgetDraftTheme()
        await theme.start(ctx)
        db.set_draft_status(ctx.draft_id, 'active')
        return ctx, theme, [db.get_drafter(m['id']) for m in members]

    def flux_message(self, ctx, **changes):
        import flux_gate
        fields = dict(author=types.SimpleNamespace(id=flux_gate.FLUX_BOT_ID, bot=True),
                      content='✅ **Round 2 Flux is done**', id=1000,
                      channel=types.SimpleNamespace(id=99), created_at=datetime.now(timezone.utc))
        fields.update(changes)
        return types.SimpleNamespace(**fields)

    async def test_numbered_picks_and_two_team_flux_boundary(self):
        import timer_bridge
        import flux_gate
        ctx, theme, members = await self.budget_demo()
        await theme.on_pick(ctx, members[0], 'Michael Jordan 1991 44')
        await theme.on_pick(ctx, members[1], 'LeBron James 2013 43')
        posted = [call.args[0] for call in ctx.bot.get_channel.return_value.send.call_args_list]
        self.assertEqual(posted, ["1. Team 1 Michael Jordan '91 $44", "2. Team 2 LeBron James '13 $43"])
        data = timer_bridge.snapshot(ctx.draft)
        self.assertEqual(data['waiting_for_flux'], 2)
        self.assertIsNone(data['current'])
        self.assertEqual(data['clocks'], [])
        self.assertEqual(await theme.resume_timers(ctx), 0)
        response = await theme.on_pick(ctx, members[1], 'Larry Bird 1986 20')
        self.assertIn('Flux', response)
        self.assertEqual(len(db.get_picks(ctx.draft_id, members[1]['id'])), 1)
        await flux_gate.observe(ctx.bot, self.flux_message(ctx))
        data = timer_bridge.snapshot(ctx.draft)
        self.assertIsNone(data['waiting_for_flux'])
        self.assertEqual(data['current']['pick'], 3)
        self.assertEqual(data['current']['logo'], 'Team 2')
        deadline = data['current']['deadline']
        await flux_gate.observe(ctx.bot, self.flux_message(ctx))
        self.assertEqual(timer_bridge.snapshot(ctx.draft)['current']['deadline'], deadline)

    async def test_human_old_wrong_channel_and_in_progress_flux_do_not_release(self):
        import flux_gate
        ctx, theme, members = await self.budget_demo()
        await theme.on_pick(ctx, members[0], 'Michael Jordan 1991 44')
        await theme.on_pick(ctx, members[1], 'LeBron James 2013 43')
        messages = [self.flux_message(ctx, author=types.SimpleNamespace(id=123, bot=False)),
                    self.flux_message(ctx, channel=types.SimpleNamespace(id=98)),
                    self.flux_message(ctx, created_at=datetime.fromtimestamp(1, timezone.utc)),
                    self.flux_message(ctx, content='⏳ Flux is in progress…')]
        for message in messages:
            await flux_gate.observe(ctx.bot, message)
            self.assertEqual(ctx.state['flux_wait']['round'], 2)

    async def test_thirty_team_round_ends_at_30_and_next_pick_is_31(self):
        import flux_gate
        import timer_bridge
        ctx, theme, members = await self.budget_demo(count=30, rounds=2)
        ctx.dm.reset_mock()
        await theme._advance_live_pick(ctx, 30)
        self.assertEqual(ctx.state['live_pick_number'], 31)
        self.assertEqual(ctx.state['flux_wait']['round'], 2)
        ctx.dm.assert_not_awaited()
        await flux_gate.observe(ctx.bot, self.flux_message(ctx))
        self.assertEqual(timer_bridge.snapshot(ctx.draft)['current']['pick'], 31)

    async def test_flux_completed_while_paused_does_not_start_clock_until_resume(self):
        import flux_gate
        import timer_bridge
        ctx, theme, members = await self.budget_demo()
        await theme.on_pick(ctx, members[0], 'Michael Jordan 1991 44')
        await theme.on_pick(ctx, members[1], 'LeBron James 2013 43')
        db.set_draft_status(ctx.draft_id, 'paused')
        await theme.cancel(ctx)
        await flux_gate.observe(ctx.bot, self.flux_message(ctx))
        self.assertEqual(ctx.draft['status'], 'paused')
        self.assertIsNone(timer_bridge.snapshot(ctx.draft)['current'])
        db.set_draft_status(ctx.draft_id, 'active')
        await theme.resume_timers(ctx)
        self.assertEqual(timer_bridge.snapshot(ctx.draft)['current']['pick'], 3)

    async def test_partner_waits_for_flux_before_reversed_second_round(self):
        import flux_gate
        import timer_bridge
        ctx = await self.linked_partner()
        order = ctx.state['turns']
        await self.lists(ctx, 0, ', '.join(NAMES[:3]))
        await self.lists(ctx, 1, ', '.join(NAMES[3:6]))
        self.assertEqual(ctx.state['flux_wait']['round'], 2)
        self.assertEqual(timer_bridge.snapshot(ctx.draft)['clocks'], [])
        await flux_gate.observe(ctx.bot, self.flux_message(ctx))
        data = timer_bridge.snapshot(ctx.draft)
        self.assertEqual(data['current']['token'], 'p:1:1')
        self.assertEqual(ctx.state['turns'], order)

    async def test_private_turn_dm_shows_shared_remaining_budget_and_optional_year(self):
        ctx, theme, members = await self.budget_demo()
        for spent, expected in ((44, '$56'), (100, '$0')):
            state = ctx.state
            state['teams'][0]['budget_spent'] = spent
            ctx.save_state(state)
            ctx.dm.reset_mock()
            await theme._open_pick_for_team(ctx, 0, 1, force=True)
            message = ctx.dm.call_args.args[1]
            self.assertIn('Budget remaining:', message)
            self.assertIn(expected, message)
            self.assertIn('$100', message)
            self.assertIn('!pick <player> [year] <price>', message)
        ctx.bot.get_channel.return_value.send.assert_not_awaited()

    async def test_optional_year_finalizes_without_dangling_apostrophe(self):
        ctx, theme, members = await self.budget_demo(rounds=1)
        await theme.on_pick(ctx, members[0], 'Michael Jordan $44')
        await theme.on_pick(ctx, members[1], 'LeBron James 2013 $43')
        messages = [call.args[0] for call in ctx.bot.get_channel.return_value.send.call_args_list]
        self.assertEqual(messages, ["1. Team 1 Michael Jordan '91 $44", "2. Team 2 LeBron James '13 $43"])
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 44)

    async def test_optional_year_proposal_and_agreement(self):
        ctx, theme, members = await self.budget_demo(rounds=1)
        state = ctx.state
        state['teams'] = [state['teams'][0]]
        state['teams'][0]['member_ids'].append(members[1]['id'])
        ctx.save_state(state)
        db.update_drafter_state(members[1]['id'], {'team_index': 0})
        proposal = await theme.on_pick(ctx, members[0], 'Michael Jordan $44')
        self.assertIn("**Michael Jordan '91**", proposal)
        self.assertIn("Michael Jordan '91", proposal)
        await theme.on_pick(ctx, db.get_drafter(members[1]['id']), '1')
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 44)
        self.assertEqual(db.get_picks(ctx.draft_id, members[0]['id']), ['Michael Jordan'])
        self.assertEqual(db.get_picks(ctx.draft_id, members[1]['id']), ['Michael Jordan'])

    async def test_optional_year_queued_pick_is_consumed(self):
        ctx, theme, members = await self.budget_demo()
        result = await theme.on_pick(ctx, members[1], 'Larry Bird $20')
        self.assertIn("**Larry Bird '86**", result)
        self.assertEqual(db.get_drafter(members[1]['id'])['state']['queued_pick']['year'], '1985-86')
        await theme.on_pick(ctx, members[0], 'Michael Jordan $44')
        self.assertEqual(db.get_picks(ctx.draft_id, members[1]['id']), ['Larry Bird'])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.theme = AnonBudgetDraftTheme()
        self.ctx = types.SimpleNamespace(settings={'budget': 100})

    def test_parser_rejects_nonfinite_prices_but_accepts_negative_prices(self):
        for price in ('NaN', 'inf', '-inf'):
            self.assertIsNone(self.theme._parse_submission(self.ctx, 'LeBron James 2013 ' + price))
        self.assertEqual(self.theme._parse_submission(self.ctx, 'LeBron James 2013 $40')[2], 40)
        for price in ('$-1', '-$1', '-1', '\u2212$1', '($1)'):
            self.assertEqual(self.theme._parse_submission(self.ctx, 'Nene ' + price)[2], -1)
        self.assertIsNone(self.theme._check_budget(self.ctx, {'budget_spent': 0}, -1))
        self.assertIsNone(self.theme._check_budget(self.ctx, {'budget_spent': -1}, 101))
        self.assertIn('$101', self.theme._budget_note(self.ctx, {'budget_spent': -1}))

    def test_year_is_optional_but_budget_price_is_required(self):
        for text, expected in (
                ('LeBron James $43', ('LeBron James', '2012-13', 43)),
                ('LeBron James 43', ('LeBron James', '2012-13', 43)),
                ('LeBron James 2013 $43', ('LeBron James', '2013', 43)),
                ("LeBron James '13 $43", ('LeBron James', '13', 43)),
                ('LeBron James 2012-13 $43', ('LeBron James', '2012-13', 43)),
                ('Nene $0', ('Nene', '', 0))):
            self.assertEqual(self.theme._parse_submission(self.ctx, text), expected)
        for text in ('LeBron James', 'LeBron James $NaN', 'LeBron James $inf', '$43', '2013 $43'):
            self.assertIsNone(self.theme._parse_submission(self.ctx, text))

    def test_budget_remaining_and_invalid_values(self):
        for value in (float('nan'), float('inf'), 'invalid', None):
            self.assertIsNotNone(self.theme._check_budget(self.ctx, {}, value))
        self.assertIsNone(self.theme._check_budget(self.ctx, {'budget_spent': 60}, 40))
        self.assertIsNotNone(self.theme._check_budget(self.ctx, {'budget_spent': 60}, 41))
        for key in ('budget', 'budget_spent'):
            for value in (float('nan'), float('inf'), 'invalid') + ((-1,) if key == 'budget' else ()):
                ctx = types.SimpleNamespace(settings={'budget': value if key == 'budget' else 100})
                self.assertIsNotNone(self.theme._check_budget(ctx, {key: value} if key != 'budget' else {}, 1))

    def test_roster_zero_price_and_invalid_budget(self):
        self.assertTrue(roster.validate_roster(['Free'], ['Free'], {'Free': 0}, 0)[0])
        for value in (float('nan'), float('inf'), -1, 'bad'):
            self.assertFalse(roster.validate_roster(['Free'], ['Free'], {'Free': 0}, value)[0])
            if value != -1:
                self.assertFalse(roster.validate_roster(['Free'], ['Free'], {'Free': value}, 100)[0])
        self.assertTrue(roster.validate_roster(['Free'], ['Free'], {'Free': -1}, 100)[0])


if __name__ == '__main__':
    unittest.main()
