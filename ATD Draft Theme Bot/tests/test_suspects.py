"""Anonymous guesses against real draft state and a temporary SQLite ledger."""
import ast
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

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
    async def test_target_cap_reply_preserves_attempt_and_does_not_dm_accuser(self):
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspect')
        node.decorator_list = []
        ns = {'bot': None, 'discord': discord}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=1566394824288894976),
            author=types.SimpleNamespace(id=100, send=AsyncMock()), message=types.SimpleNamespace(id=900), reply=AsyncMock())
        with patch.object(suspects, 'guess', new_callable=AsyncMock, return_value={'status': 'target_limit', 'remaining': 1}):
            await ns['cmd_suspect'](ctx, types.SimpleNamespace(id=101, mention='<@101>'), emoji=':Lakers:')
        reply = ctx.reply.call_args.args[0]
        self.assertIn('5 suspect guesses', reply)
        self.assertIn('No guess was used', reply)
        self.assertIn('remaining attempts are unchanged', reply)
        ctx.author.send.assert_not_awaited()

    async def test_all_unsuccessful_guesses_have_same_public_reply_without_accuser_dm(self):
        from unittest.mock import patch
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspect')
        node.decorator_list = []
        ns = {'bot': None, 'discord': discord}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=1566394824288894976),
                                    author=types.SimpleNamespace(id=100, send=AsyncMock()),
                                    message=types.SimpleNamespace(id=900), reply=AsyncMock())
        target = types.SimpleNamespace(id=101, mention='<@101>')
        for status in ('invalid', 'wrong', 'limit', 'caught'):
            with patch.object(suspects, 'guess', new_callable=AsyncMock,
                              return_value=dict(status=status, message='You cannot suspect your own team or a teammate.', remaining=0)):
                await ns['cmd_suspect'](ctx, target, emoji=':Lakers:')
            reply = ctx.reply.call_args.args[0]
            if status in ('limit', 'caught'):
                self.assertIn('no lives left', reply)
            elif status == 'wrong':
                self.assertIn('Your guess was wrong', reply)
                self.assertIn('!market', reply)
            else:
                self.assertIn('No guess was submitted', reply)
            self.assertNotIn('own team', reply)
            self.assertNotIn('teammate', reply)
            self.assertFalse(ctx.reply.call_args.kwargs['mention_author'])
        ctx.author.send.assert_not_awaited()
        ctx.author.send.side_effect = discord.Forbidden(types.SimpleNamespace(status=403, reason='Forbidden'), 'Cannot DM')
        with patch.object(suspects, 'guess', new_callable=AsyncMock, return_value=dict(status='invalid', message='Secret reason')):
            await ns['cmd_suspect'](ctx, target, emoji=':Lakers:')
        self.assertIn('No guess was submitted', ctx.reply.call_args.args[0])
        ctx.author.send.assert_not_awaited()

    async def test_caught_duo_cannot_lose_more_than_five_wins_across_either_partner(self):
        draft, _ = self.make_draft(mode='partner_draft', channel=60)
        first = await self.guess(draft_id=draft, target=102)
        self.assertEqual(first['penalty_amount'], 5)
        # A different accuser and the other partner still cannot stack a catch.
        second = await self.guess(draft_id=draft, user=104, target=103)
        self.assertEqual(second['status'], 'caught')
        self.assertEqual(second['remaining'], 1)
        state = db.get_draft(draft)['state']
        state['scheduled_turns_finished'] = True
        db.set_draft_state(draft, state)
        self.assertEqual((await self.guess(draft_id=draft, user=104, target=103))['status'], 'caught')
        rows = db.suspect_attempts(draft)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(row['penalty_amount'] for row in rows), 5)

    async def test_already_caught_target_with_another_emoji_does_not_spend_attempt(self):
        await self.guess()
        result = await self.guess(user=102, emoji='🐻')
        self.assertEqual(result['status'], 'caught')
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 1)

    async def test_simultaneous_catches_of_both_partners_only_create_one_penalty(self):
        draft, _ = self.make_draft(mode='partner_draft', channel=70)
        results = await asyncio.gather(self.guess(draft_id=draft, user=100, target=102),
                                       self.guess(draft_id=draft, user=104, target=103))
        self.assertEqual(sorted(r['status'] for r in results), ['caught', 'correct'])
        self.assertEqual(len(db.suspect_attempts(draft)), 1)
    async def test_duo_partners_share_one_attempt_in_both_anonymous_formats(self):
        for mode in ('anon_budget_draft', 'partner_draft'):
            if mode == 'partner_draft':
                draft, _ = self.make_draft(mode=mode, channel=30)
                user, partner, target, logo = 100, 101, 102, ':Lakers:'
            else:
                draft, members = self.make_draft(channel=40)
                state = db.get_draft(draft)['state']
                state['teams'][0]['member_ids'].append(members[1])
                state['teams'].pop(1)
                db.set_draft_state(draft, state)
                user, partner, target, logo = 100, 101, 102, '🐻'
            # The first valid wrong guess spends the shared attempt.
            result = await self.guess(user=user, target=target, emoji=logo, draft_id=draft)
            expected = 'correct' if mode == 'partner_draft' else 'wrong'
            self.assertEqual(result['status'], expected)
            self.assertEqual(result['remaining'], 0)
            result = await self.guess(user=partner, target=target, emoji=logo, draft_id=draft)
            self.assertEqual(result['status'], 'caught' if expected == 'correct' else 'limit')
            self.assertEqual(len(db.suspect_attempts(draft)), 1)
            db.init_db()
            self.assertEqual((await self.guess(user=partner, target=target, emoji=logo, draft_id=draft))['status'], 'caught' if expected == 'correct' else 'limit')

    async def test_concurrent_duo_guesses_only_spend_once(self):
        draft, _ = self.make_draft(mode='partner_draft', channel=50)
        results = await asyncio.gather(
            self.guess(user=100, target=102, emoji=':Lakers:', draft_id=draft),
            self.guess(user=101, target=104, emoji='🐻', draft_id=draft))
        self.assertEqual(sum(r['status'] == 'limit' for r in results), 1)
        self.assertEqual(len(db.suspect_attempts(draft)), 1)
    async def test_valid_wrong_and_correct_guesses_dm_target_without_repeat_on_replay(self):
        user = types.SimpleNamespace(send=AsyncMock())
        bot = Mock(get_user=Mock(return_value=user))
        await suspects.guess(bot, self.draft_id, 901, 100, 101, '\U0001f985')
        text = user.send.call_args.kwargs['content']
        self.assertIn('guess was incorrect', text)
        self.assertNotIn('Lakers', text)
        await suspects.guess(bot, self.draft_id, 902, 102, 101, ':Lakers:')
        self.assertIn('correctly suspected', user.send.call_args.kwargs['content'])
        self.assertIn('5 wins', user.send.call_args.kwargs['content'])
        await suspects.guess(bot, self.draft_id, 902, 102, 101, ':Lakers:')
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

    async def test_guesses_left_uses_shared_duo_balance_and_confirmed_purchases(self):
        draft_id, members = self.make_draft(mode='partner_draft', channel=80)
        eligible = lambda: {g['user_id'] for g in suspects.guessers_with_attempts(db.get_draft(draft_id))}
        self.assertTrue({100, 101} <= eligible())
        await self.guess(draft_id=draft_id, user=100, target=102, emoji='🦅')
        self.assertFalse({100, 101} & eligible())
        purchase = db.create_suspect_purchase(draft_id, 9001, 101, [100, 101], 2)
        self.assertFalse({100, 101} & eligible())
        db.finish_suspect_purchase(purchase['id'], 101, [100, 101], True)
        self.assertTrue({100, 101} <= eligible())
        self.assertEqual(db.suspect_remaining(draft_id, [100, 101]), 2)
        for gm in suspects.guessers_with_attempts(db.get_draft(draft_id)):
            self.assertEqual(set(gm), {'user_id', 'display_name'})

    async def test_guesses_left_includes_former_member_ledger(self):
        await self.guess(user=100, target=102, emoji=':Lakers:')
        db.update_drafter_state(self.members[1], {'former_user_ids': [100]})
        self.assertEqual(db.suspect_remaining(self.draft_id, [101]), 0)

    async def test_guesses_left_command_shows_names_without_logos_pairing_or_counts(self):
        draft_id, _ = self.make_draft(mode='partner_draft', channel=90)
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_guessesleft')
        node.decorator_list = []
        ns = {'db': db, 'discord': discord, 'themes': suspects.themes, 're': suspects.re}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        guild = types.SimpleNamespace(id=1, get_member=lambda uid: types.SimpleNamespace(name=f'user{uid}'))
        ctx = types.SimpleNamespace(guild=guild, channel=types.SimpleNamespace(id=1566394824288894976),
                                    author=types.SimpleNamespace(id=100), send=AsyncMock(), reply=AsyncMock())
        await ns['cmd_guessesleft'](ctx, draft_id)
        embed = ctx.send.call_args.kwargs['embed']
        self.assertEqual(embed.description.splitlines(), [f'• user{uid}' for uid in range(100, 108)])
        self.assertNotIn('Nets', embed.description)
        self.assertNotIn('Lakers', embed.description)
        ctx.reply.assert_not_awaited()

    async def test_guesses_left_closes_after_identity_reveal(self):
        self.change_state(identities_revealed=True)
        self.assertEqual(suspects.guessers_with_attempts(db.get_draft(self.draft_id)), [])

    def change_state(self, **changes):
        state = db.get_draft(self.draft_id)['state']
        state.update(changes)
        db.set_draft_state(self.draft_id, state)

    async def test_correct_during_draft_records_five_wins(self):
        result = await self.guess()
        self.assertEqual(result['status'], 'correct')
        self.assertEqual(result['remaining'], 0)
        row = db.suspect_attempts(self.draft_id)[0]
        self.assertEqual((row['target_id'], row['penalty_kind'], row['penalty_amount']), (101, 'wins', 5))
        self.assertEqual(db.get_draft(self.draft_id)['status'], 'active')

    async def test_wrong_guess_uses_single_attempt_without_revealing_team(self):
        for expected in (0,):
            result = await self.guess(emoji='🦅')
            self.assertEqual(result['status'], 'wrong')
            self.assertEqual(result['remaining'], expected)
        self.assertEqual((await self.guess())['status'], 'limit')
        rows = db.suspect_attempts(self.draft_id)
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(not row['correct'] and row['penalty_amount'] == 0 for row in rows))

    async def test_after_final_scheduled_round_still_uses_wins_with_makeups(self):
        self.change_state(scheduled_turns_finished=True, live_pick_number=None)
        result = await self.guess()
        self.assertEqual((result['penalty_kind'], result['penalty_amount']), ('wins', 5))

    async def test_complete_but_not_seeded_still_uses_wins(self):
        db.set_draft_status(self.draft_id, 'complete')
        self.assertEqual((await self.guess())['penalty_kind'], 'wins')

    def playoff_fixture(self):
        draft = db.create_draft(1,10,'anon_budget_draft','Playoffs',{'rounds':10})
        members = [db.add_drafter(draft,1000+i,f'GM{i}') for i in range(17)]
        db.set_draft_state(draft,{'teams':[{'logo':f':Team{i}:','member_ids':[m]} for i,m in enumerate(members)],
                                  'scheduled_turns_finished':True})
        db.set_draft_status(draft,'complete')
        return draft

    async def test_explicit_release_selects_exactly_16_and_preserves_history(self):
        draft=self.playoff_fixture()
        first=await suspects.guess(None,draft,7000,1000,1001,':Team1:')
        self.assertEqual(first['penalty_kind'],'wins')
        original=db.suspect_attempts(draft)
        entries='\n'.join(str(i) for i in range(1,17))
        for bad in ('1,2',entries+',16',entries.replace('16','99')):
            with self.assertRaises(ValueError): await suspects.release_seeding(None,draft,1,999,bad)
            self.assertNotIn('suspect_seeding',db.get_draft(draft)['state'])
        await suspects.release_seeding(None,draft,1,999,entries)
        await suspects.release_seeding(None,draft,1,999,entries)
        db.init_db()
        self.assertEqual(len(suspects.playoff_logos(db.get_draft(draft))),16)
        self.assertEqual(db.suspect_attempts(draft),original)
        result=await suspects.guess(None,draft,7001,1002,1003,':Team3:')
        self.assertEqual((result['penalty_kind'],result['penalty_amount']),('odds_percent',10))
        self.assertEqual((await suspects.guess(None,draft,7002,1004,1001,':Team1:'))['status'],'caught')
        # Invalid public team does not spend an attempt; an eligible wrong guess does.
        self.assertEqual((await suspects.guess(None,draft,7003,1005,1006,':Team16:'))['status'],'ineligible')
        self.assertEqual((await suspects.guess(None,draft,7004,1005,1016,':Team6:'))['remaining'],0)
        with self.assertRaises(ValueError):
            await suspects.release_seeding(None,draft,1,999,','.join(str(i) for i in range(2,18)))

    async def test_admin_commands_are_private_owner_only_and_ineligible_reply_is_free(self):
        tree=ast.parse((ROOT/'bot.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name in ('cmd_suspectseeding','cmd_suspectstatus','cmd_suspect')]
        for node in nodes:node.decorator_list=[]
        ns={'bot':None,'discord':discord,'OWNER_ID':999,'OWNER_REPORT_CHANNEL_ID':77,'db':db}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'bot.py','exec'),ns)
        ctx=types.SimpleNamespace(author=types.SimpleNamespace(id=999),guild=types.SimpleNamespace(id=1,default_role=None),
            channel=types.SimpleNamespace(id=78,permissions_for=lambda role:types.SimpleNamespace(view_channel=False)),send=AsyncMock())
        with patch.object(suspects,'release_seeding',new_callable=AsyncMock) as release:
            await ns['cmd_suspectseeding'](ctx,self.draft_id,entries='1')
            await ns['cmd_suspectstatus'](ctx,self.draft_id)
            ctx.channel.id=77;ctx.author.id=100
            await ns['cmd_suspectseeding'](ctx,self.draft_id,entries='1')
            release.assert_not_awaited();ctx.send.assert_not_awaited()
        ctx.author.id=100;ctx.channel.id=1566394824288894976
        ctx.message=types.SimpleNamespace(id=777);ctx.reply=AsyncMock()
        with patch.object(suspects,'guess',new_callable=AsyncMock,return_value={'status':'ineligible'}):
            await ns['cmd_suspect'](ctx,types.SimpleNamespace(id=101),emoji=':Lakers:')
        self.assertIn('No guess was used',ctx.reply.call_args.args[0])
        self.assertNotIn('GM',ctx.reply.call_args.args[0])

    async def test_release_rejects_early_wrong_guild_and_revealed_drafts(self):
        with self.assertRaises(ValueError):
            await suspects.release_seeding(None,self.draft_id,1,999,'1,2')
        draft=self.playoff_fixture()
        entries=','.join(str(i) for i in range(1,17))
        with self.assertRaises(ValueError):await suspects.release_seeding(None,draft,999,999,entries)
        state=db.get_draft(draft)['state'];state['identities_revealed']=True;db.set_draft_state(draft,state)
        with self.assertRaises(ValueError):await suspects.release_seeding(None,draft,1,999,entries)

    async def test_reveal_closes_guesses_without_using_attempt(self):
        self.change_state(identities_revealed=True)
        self.assertEqual((await self.guess())['status'], 'revealed')
        self.assertEqual(db.suspect_attempts(self.draft_id), [])

    async def test_unknown_emoji_and_nonparticipant_not_charged(self):
        for options in ({'user': 999}, {'emoji': ':Unknown:'}):
            self.assertEqual((await self.guess(**options))['status'], 'invalid')
        self.assertEqual(db.suspect_attempts(self.draft_id), [])

    async def test_guessing_nonparticipant_spends_only_life_and_cannot_repeat_four_times(self):
        first = await self.guess(target=999, emoji=':Nets:')
        self.assertEqual(first['status'], 'wrong')
        self.assertEqual(first['remaining'], 0)
        for _ in range(3):
            self.assertEqual((await self.guess(target=999, emoji=':Nets:'))['status'], 'limit')
        rows = db.suspect_attempts(self.draft_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['charged'], 1)
        self.assertEqual(rows[0]['penalty_amount'], 0)

    async def test_self_and_own_team_guesses_count_as_wrong_once(self):
        for i, options in enumerate(({'target': 100}, {'emoji': ':Nets:'})):
            draft, _ = self.make_draft(channel=80+i)
            first = await self.guess(draft_id=draft, **options)
            self.assertEqual(first['status'], 'wrong')
            self.assertEqual(first['remaining'], 0)
            for _ in range(3):
                self.assertEqual((await self.guess(draft_id=draft, **options))['status'], 'limit')
            rows = db.suspect_attempts(draft)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['penalty_amount'], 0)

    async def test_own_team_guess_also_uses_duo_partners_life(self):
        draft, _ = self.make_draft(mode='partner_draft', channel=90)
        first = await self.guess(draft_id=draft, user=100, target=101, emoji=':Nets:')
        self.assertEqual(first['status'], 'wrong')
        self.assertEqual((await self.guess(draft_id=draft, user=101, target=102))['status'], 'limit')
        self.assertEqual(len(db.suspect_attempts(draft)), 1)

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
        another, _ = self.make_draft(channel=20)
        results = await asyncio.gather(self.guess(target=103, emoji='🐻', draft_id=another),
                                       self.guess(user=102, target=103, emoji='🐻', draft_id=another))
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
        self.assertEqual((await self.guess(draft_id=partner, target=101, emoji=':Nets:'))['status'], 'wrong')
        self.assertEqual((await self.guess(draft_id=partner, user=104, target=103))['status'], 'correct')
        self.assertEqual((await self.guess(draft_id=partner, user=106, target=102))['status'], 'caught')

    async def test_partner_final_turns_finished_still_uses_wins(self):
        partner, _ = self.make_draft(mode='partner_draft')
        state = db.get_draft(partner)['state']
        state['turn_index'] = len(state['turns'])
        db.set_draft_state(partner, state)
        self.assertEqual((await self.guess(draft_id=partner, target=102))['penalty_kind'], 'wins')

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
        self.assertIn('Caught by a correct suspect', text)
        self.assertNotIn('Lakers', text)
        self.assertEqual(text.count('<@101>'), 1)
        ns['_send_chunked'].reset_mock()
        ctx.channel.id = 98
        await ns['cmd_suspects'](ctx, draft_ref=str(self.draft_id))
        ns['_send_chunked'].assert_not_awaited()
        ctx.channel.id, ctx.author.id = 99, 102
        await ns['cmd_suspects'](ctx, draft_ref=str(self.draft_id))
        ns['_send_chunked'].assert_not_awaited()

    async def test_win_summary_combines_catch_and_repeated_market_purchases(self):
        await self.guess()
        for message in (9101, 9102):
            purchase = db.create_suspect_purchase(self.draft_id, message, 101, [101], 1)
            db.finish_suspect_purchase(purchase['id'], 101, [101], True)
        db.create_suspect_purchase(self.draft_id, 9103, 102, [102], 2)
        # Playoff odds penalties are not regular-season win deductions.
        result = db.record_suspect(self.draft_id, 9104, 103, 102, 'PRIVATE LOGO', True,
                                   'post_draft', db.get_draft(self.draft_id)['state'])
        self.assertEqual(result['status'], 'correct')
        summary = suspects.win_deduction_summary(db.get_draft(self.draft_id))
        self.assertEqual(summary, [dict(member_ids=(101,), caught_wins=5,
                                       market_wins=10, bought_guesses=2, total_wins=15)])

    async def test_win_summary_duo_has_one_shared_total(self):
        draft_id, _ = self.make_draft(mode='partner_draft', channel=100)
        await self.guess(draft_id=draft_id, target=102)
        for message, buyer in ((9201, 102), (9202, 103)):
            purchase = db.create_suspect_purchase(draft_id, message, buyer, [102, 103], 1)
            db.finish_suspect_purchase(purchase['id'], buyer, [102, 103], True)
        summary = suspects.win_deduction_summary(db.get_draft(draft_id))
        self.assertEqual(summary, [dict(member_ids=(102, 103), caught_wins=5,
                                       market_wins=10, bought_guesses=2, total_wins=15)])
    async def test_actual_command_replies_with_requested_penalty_and_no_pick_content(self):
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspect')
        node.decorator_list = []
        ns = {'bot': None, 'discord': discord}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=1566394824288894976),
                                    author=types.SimpleNamespace(id=100), message=types.SimpleNamespace(id=800), reply=AsyncMock())
        target = types.SimpleNamespace(id=101, mention='<@101>')
        await ns['cmd_suspect'](ctx, target, emoji=':Lakers:')
        self.assertEqual(ctx.reply.call_args.args, ())
        self.assertEqual(ctx.reply.call_args.kwargs['embed'].description,
                         '<@101> You have been caught out being the GM of <:Lakers:2222>, you lose 5 wins')
        self.assertFalse(ctx.reply.call_args.kwargs['mention_author'])
        self.change_state(scheduled_turns_finished=True)
        ctx.message.id = 801
        ctx.author.id = 103
        target.id, target.mention = 102, '<@102>'
        await ns['cmd_suspect'](ctx, target, emoji='🦅')
        self.assertIn('you lose 5 wins', ctx.reply.call_args.kwargs['embed'].description)

    async def test_command_only_runs_in_suspect_and_private_channel_with_shared_attempts(self):
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_suspect')
        node.decorator_list = []
        ns = {'bot': None, 'discord': discord}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=11),
                                    author=types.SimpleNamespace(id=100, send=AsyncMock()), message=types.SimpleNamespace(id=900), reply=AsyncMock())
        target = types.SimpleNamespace(id=101, mention='<@101>')
        for channel in (11, 10, 123456, 934052158532378634):
            ctx.channel.id = channel
            await ns['cmd_suspect'](ctx, target, emoji='🦅')
            self.assertIn('<#1566394824288894976>', ctx.reply.call_args.args[0])
            self.assertIn('Your guess has not been used', ctx.reply.call_args.args[0])
            self.assertEqual(db.suspect_attempts(self.draft_id), [])
        self.assertEqual(db.suspect_attempts(self.draft_id), [])
        for channel in (1566394824288894976, 1533242104304631969, 1566394824288894976):
            ctx.channel.id = channel
            ctx.message.id += 1
            await ns['cmd_suspect'](ctx, target, emoji='🦅')
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 1)
        ctx.channel.id = 1533242104304631969
        ctx.message.id += 1
        await ns['cmd_suspect'](ctx, target, emoji=':Lakers:')
        self.assertEqual(ctx.reply.call_args.args[0], 'You have no lives left. Use `!market` to buy extra guesses.')
        ctx.author.send.assert_not_awaited()
        self.assertEqual(len(db.suspect_attempts(self.draft_id)), 1)


if __name__ == '__main__':
    unittest.main()
