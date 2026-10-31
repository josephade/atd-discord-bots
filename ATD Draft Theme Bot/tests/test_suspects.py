"""Anonymous guesses against real draft state and a temporary SQLite ledger."""
import ast
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock

import discord

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_temp_root = tempfile.TemporaryDirectory()
os.environ['STATE_DIR'] = _temp_root.name
sys.modules.setdefault('sheets', types.ModuleType('sheets'))

import db
import suspects
from themes.base import _draft_locks
from themes.partner_draft import PartnerDraftTheme


class SuspectTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_wrong_and_correct_guesses_dm_target_without_repeat_on_replay(self):
        user = types.SimpleNamespace(send=AsyncMock())
        bot = Mock(get_user=Mock(return_value=user))
        await suspects.guess(bot, self.draft_id, 901, 100, 101, '\U0001f985')
        text = user.send.call_args.kwargs['content']
        self.assertIn('guess was incorrect', text)
        self.assertNotIn('Lakers', text)
        await suspects.guess(bot, self.draft_id, 902, 100, 101, ':Lakers:')
        self.assertIn('correctly suspected', user.send.call_args.kwargs['content'])
        self.assertIn('5 wins', user.send.call_args.kwargs['content'])
        await suspects.guess(bot, self.draft_id, 902, 100, 101, ':Lakers:')
        self.assertEqual(user.send.await_count, 2)
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name) / 'suspects.db')
        db.init_db()
        _draft_locks.clear()
        self.draft_id, self.members = self.make_draft()
        self.nonce = 1000

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def make_draft(self, mode='anon_budget_draft', guild=1, channel=10, count=4):
        draft_id = db.create_draft(guild, channel, mode, f'Test {mode}', {'rounds': 10, 'roster_size': 10})
        members = [db.add_drafter(draft_id, 100 + n, f'GM{n}') for n in range(count * (2 if mode == 'partner_draft' else 1))]
        logos = ['<:Nets~3:1111>', '<:Lakers:2222>', '🦅', '🐻']
        if mode == 'partner_draft':
            pairs = []
            for i in range(count):
                pair = {'a': members[2 * i], 'b': members[2 * i + 1], 'logo': logos[i], 'completed_rounds': []}
                pairs.append(pair)
                for member in (pair['a'], pair['b']):
                    db.update_drafter_state(member, {'pair_index': i})
            state = {'pairs': pairs, 'turns': PartnerDraftTheme._orders(count, 10), 'turn_index': 0}
        else:
            state = {'teams': [{'member_ids': [member], 'logo': logos[i]} for i, member in enumerate(members)],
                     'live_pick_number': 1}
            for i, member in enumerate(members):
                db.update_drafter_state(member, {'team_index': i})
        state['timer_channel_id'] = channel + 1
        db.set_draft_state(draft_id, state)
        db.set_draft_status(draft_id, 'active')
        return draft_id, members

    async def guess(self, user=100, target=101, emoji=':Lakers:', draft_id=None, nonce=None):
        self.nonce += 1
        return await suspects.guess(None, draft_id or self.draft_id, nonce or self.nonce, user, target, emoji)

    def change_state(self, **changes):
        state = db.get_draft(self.draft_id)['state']
        state.update(changes)
        db.set_draft_state(self.draft_id, state)

    async def test_correct_during_draft_records_five_wins(self):
        result = await self.guess()
        self.assertEqual(result['status'], 'correct')
        self.assertEqual(result['remaining'], 2)
        row = db.suspect_attempts(self.draft_id)[0]
        self.assertEqual((row['target_id'], row['penalty_kind'], row['penalty_amount']), (101, 'wins', 5))
        self.assertEqual(db.get_draft(self.draft_id)['status'], 'active')

    async def test_wrong_guesses_use_three_attempts_without_revealing_team(self):
        for expected in (2, 1, 0):
            result = await self.guess(emoji='🦅')
            self.assertEqual(result['status'], 'wrong')
            self.assertEqual(result['remaining'], expected)
        self.assertEqual((await self.guess())['status'], 'limit')
        rows = db.suspect_attempts(self.draft_id)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(not row['correct'] and row['penalty_amount'] == 0 for row in rows))

    async def test_after_final_scheduled_round_uses_odds_even_with_makeups(self):
        self.change_state(scheduled_turns_finished=True, live_pick_number=None)
        result = await self.guess()
        self.assertEqual((result['penalty_kind'], result['penalty_amount']), ('odds_percent', 10))

    async def test_complete_but_not_revealed_uses_odds(self):
        db.set_draft_status(self.draft_id, 'complete')
        self.assertEqual((await self.guess())['penalty_kind'], 'odds_percent')

    async def test_reveal_closes_guesses_without_using_attempt(self):
        self.change_state(identities_revealed=True)
        self.assertEqual((await self.guess())['status'], 'revealed')
        self.assertEqual(db.suspect_attempts(self.draft_id), [])

    async def test_self_teammate_unknown_emoji_and_nonparticipant_not_charged(self):
        for options in ({'target': 100}, {'target': 999}, {'user': 999},
                        {'emoji': ':Unknown:'}, {'emoji': ':Nets:'}):
            self.assertEqual((await self.guess(**options))['status'], 'invalid')
        self.assertEqual(db.suspect_attempts(self.draft_id), [])

    async def test_custom_emoji_id_and_aliases_match(self):
        self.assertTrue(suspects.logo_matches('<:Renamed:1111>', '<:Nets~3:1111>'))
        self.assertTrue(suspects.logo_matches(':Nets:', '<:Nets~3:1111>'))
        self.assertTrue(suspects.logo_matches(':LAL:', '<:Lakers:2222>'))
        self.assertFalse(suspects.logo_matches('<:Lakers:3333>', '<:Lakers:2222>'))
        self.assertTrue(suspects.logo_matches('🦅', '🦅'))

    async def test_duplicate_message_and_concurrent_catches_do_not_repeat_penalty(self):
        result = await self.guess(nonce=500)
        self.assertEqual(result['status'], 'correct')
        self.assertEqual((await self.guess(nonce=500))['status'], 'replay')
        self.assertEqual((await self.guess(user=102))['status'], 'caught')
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 1)
        results = await asyncio.gather(self.guess(target=103, emoji='🐻'),
                                       self.guess(user=102, target=103, emoji='🐻'))
        self.assertEqual(sorted(r['status'] for r in results), ['caught', 'correct'])

    async def test_once_caught_during_draft_cannot_be_penalized_again_afterward(self):
        await self.guess()
        self.change_state(scheduled_turns_finished=True)
        self.assertEqual((await self.guess(user=102))['status'], 'caught')
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 1)

    async def test_attempts_persist_over_reinitialization_and_reset_for_new_draft(self):
        for _ in range(3):
            await self.guess(emoji='🦅')
        db.init_db()
        self.assertEqual((await self.guess())['status'], 'limit')
        another, _ = self.make_draft(channel=20)
        self.assertEqual((await self.guess(draft_id=another))['status'], 'correct')

    async def test_partner_catches_either_owner_and_rejects_teammate_guess(self):
        partner, members = self.make_draft(mode='partner_draft')
        self.assertEqual((await self.guess(draft_id=partner, target=101, emoji=':Nets:'))['status'], 'invalid')
        self.assertEqual((await self.guess(draft_id=partner, target=103))['status'], 'correct')
        self.assertEqual((await self.guess(draft_id=partner, target=102))['status'], 'correct')

    async def test_partner_final_turns_finished_uses_odds(self):
        partner, _ = self.make_draft(mode='partner_draft')
        state = db.get_draft(partner)['state']
        state['turn_index'] = len(state['turns'])
        db.set_draft_state(partner, state)
        self.assertEqual((await self.guess(draft_id=partner, target=102))['penalty_kind'], 'odds_percent')

    async def test_channel_and_server_resolution_do_not_choose_wrong_draft(self):
        other, _ = self.make_draft(channel=20)
        self.assertEqual(suspects.resolve_draft(1, 11, 100)[0]['id'], self.draft_id)
        self.assertEqual(suspects.resolve_draft(1, 21, 100)[0]['id'], other)
        self.assertIsNone(suspects.resolve_draft(1, 30, 100)[0])
        self.assertIsNone(suspects.resolve_draft(2, 11, 100)[0])

    async def test_cancelled_setup_and_non_anonymous_drafts_are_closed(self):
        for status in ('cancelled', 'setup'):
            db.set_draft_status(self.draft_id, status)
            self.assertEqual((await self.guess())['status'], 'closed')
        plain, _ = self.make_draft(mode='pack_draft', channel=30)
        self.assertEqual((await self.guess(draft_id=plain))['status'], 'closed')

    async def test_ambiguous_duplicate_team_logo_is_not_charged(self):
        state = db.get_draft(self.draft_id)['state']
        state['teams'][2]['logo'] = state['teams'][1]['logo']
        db.set_draft_state(self.draft_id, state)
        self.assertEqual((await self.guess())['status'], 'invalid')
        self.assertEqual(db.suspect_attempts(self.draft_id), [])

    async def test_owner_ledger_is_private_and_lists_recorded_penalties(self):
        await self.guess()
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspects')
        node.decorator_list = []
        ns = {'OWNER_ID': 100, 'OWNER_REPORT_CHANNEL_ID': 99, 'db': db,
              '_resolve_draft': lambda text: db.get_draft(self.draft_id), '_send_chunked': AsyncMock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=100),
                                    guild=types.SimpleNamespace(id=1, default_role=object()),
                                    channel=types.SimpleNamespace(id=99, permissions_for=Mock(return_value=types.SimpleNamespace(view_channel=False))),
                                    send=AsyncMock())
        await ns['cmd_suspects'](ctx, draft_ref=str(self.draft_id))
        text = ns['_send_chunked'].call_args.args[1]
        self.assertIn('<@101>', text)
        self.assertIn('−5 wins', text)
        ns['_send_chunked'].reset_mock()
        ctx.channel.id = 98
        await ns['cmd_suspects'](ctx, draft_ref=str(self.draft_id))
        ns['_send_chunked'].assert_not_awaited()
        ctx.channel.id, ctx.author.id = 99, 102
        await ns['cmd_suspects'](ctx, draft_ref=str(self.draft_id))
        ns['_send_chunked'].assert_not_awaited()

    async def test_actual_command_replies_with_requested_penalty_and_no_pick_content(self):
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspect')
        node.decorator_list = []
        ns = {'bot': None, 'discord': discord}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=934052158532378634),
                                    author=types.SimpleNamespace(id=100), message=types.SimpleNamespace(id=800), reply=AsyncMock())
        target = types.SimpleNamespace(id=101, mention='<@101>')
        await ns['cmd_suspect'](ctx, target, emoji=':Lakers:')
        self.assertEqual(ctx.reply.call_args.args, ())
        self.assertEqual(ctx.reply.call_args.kwargs['embed'].description,
                         '<@101> You have been caught out being the GM of <:Lakers:2222>, you lose 5 wins')
        self.assertFalse(ctx.reply.call_args.kwargs['mention_author'])
        self.change_state(scheduled_turns_finished=True)
        ctx.message.id = 801
        target.id, target.mention = 102, '<@102>'
        await ns['cmd_suspect'](ctx, target, emoji='🦅')
        self.assertIn('you lose 10% odds in your next matchup', ctx.reply.call_args.kwargs['embed'].description)

    async def test_command_only_runs_in_chat_and_private_channel_with_shared_attempts(self):
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspect')
        node.decorator_list = []
        ns = {'bot': None, 'discord': discord}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=11),
                                    author=types.SimpleNamespace(id=100), message=types.SimpleNamespace(id=900), reply=AsyncMock())
        target = types.SimpleNamespace(id=101, mention='<@101>')
        for channel in (11, 10, 123456):
            ctx.channel.id = channel
            await ns['cmd_suspect'](ctx, target, emoji='🦅')
            self.assertIn('only available', ctx.reply.call_args.args[0])
        self.assertEqual(db.suspect_attempts(self.draft_id), [])
        for channel in (934052158532378634, 1533242104304631969, 934052158532378634):
            ctx.channel.id = channel
            ctx.message.id += 1
            await ns['cmd_suspect'](ctx, target, emoji='🦅')
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 3)
        ctx.channel.id = 1533242104304631969
        ctx.message.id += 1
        await ns['cmd_suspect'](ctx, target, emoji=':Lakers:')
        self.assertIn('used all 3', ctx.reply.call_args.args[0])
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 3)


if __name__ == '__main__':
    unittest.main()
