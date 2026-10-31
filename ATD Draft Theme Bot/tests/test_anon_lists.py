import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_readiness  # Shared offline Sheets stub, installed before themes import.
import db
import themes
import anon_lists
from themes.base import DraftContext, _draft_locks
from themes.anon_draft import _timer, _proposal_timer


def option(name, price=5, year=''):
    return dict(player=name, year=year, price=price)


class AnonymousListTests(unittest.IsolatedAsyncioTestCase):
    def test_default_year_only_fills_omitted_year(self):
        ctx = DraftContext(self.bot, self.draft)
        self.assertEqual(self.theme._parse_submission(ctx, 'Michael Jordan $44'), ('Michael Jordan', '1990-91', 44))
        self.assertEqual(self.theme._parse_submission(ctx, "Michael Jordan '96 $44"), ('Michael Jordan', '96', 44))
        self.assertEqual(self.theme._parse_submission(ctx, 'Unknown Player $1'), ('Unknown Player', '', 1))

    async def test_start_dms_every_gm_with_lotto_from_setteams_order(self):
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'] = [dict(state['teams'][0], member_ids=[self.b], logo='SECOND-IN-DB-FIRST-IN-LOTTO'),
                          dict(state['teams'][0], member_ids=[self.a], logo='OTHER')]
        ctx.save_state(state)
        with patch.object(self.theme, '_open_pick_for_team', new_callable=AsyncMock), patch.object(ctx, 'dm', new_callable=AsyncMock) as dm:
            await self.theme.start(ctx)
        self.assertEqual([call.args[0] for call in dm.call_args_list], [101, 100])
        self.assertIn('Lotto number: **1**', dm.call_args_list[0].args[1])
        self.assertIn('SECOND-IN-DB-FIRST-IN-LOTTO', dm.call_args_list[0].args[1])
        self.assertIn('Lotto number: **2**', dm.call_args_list[1].args[1])
    async def test_partner_response_window_is_ten_minutes_without_extending_pick_clock(self):
        import time
        ctx = DraftContext(self.bot, self.draft)
        for remaining, expected in ((1800, 10), (180, 3)):
            with self.subTest(remaining=remaining):
                state = ctx.state
                state['teams'][0].update(member_ids=[self.a, self.b], timer_deadline=time.time()+remaining)
                deadline = state['teams'][0]['timer_deadline']
                ctx.save_state(state)
                with patch.object(_proposal_timer, 'start') as start:
                    reply = await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Michael Jordan $5')
                self.assertIn('up to 10 min', reply)
                self.assertIn('original pick clock keeps running', reply)
                self.assertAlmostEqual(start.call_args.args[1], expected, delta=0.05)
                self.assertEqual(ctx.state['teams'][0]['timer_deadline'], deadline)

    async def test_original_clock_expiry_submits_pending_proposal_once_not_skip(self):
        import time
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0].update(member_ids=[self.a, self.b], timer_deadline=time.time()+180)
        ctx.save_state(state)
        await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Michael Jordan $5')
        state = ctx.state
        state['teams'][0]['timer_deadline'] = time.time()-1
        ctx.save_state(state)
        await self.theme._resolve_on_timeout(ctx, 0, 1)
        await self.theme._resolve_proposal_timeout(ctx, 0, 1)
        self.assertEqual(db.get_picks(self.draft, self.a), ['Michael Jordan'])
        self.assertEqual(db.get_picks(self.draft, self.b), ['Michael Jordan'])
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 95)
        self.assertEqual(ctx.state['teams'][0].get('skip_count', 0), 0)
        self.channel.send.assert_awaited_once_with("1. TEAM Michael Jordan '91 $5")

    async def test_approval_window_expiry_submits_without_waiting_for_longer_pick_clock(self):
        import time
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0].update(member_ids=[self.a, self.b], timer_deadline=time.time()+1800)
        ctx.save_state(state)
        with patch.object(_proposal_timer, 'start') as start:
            await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Michael Jordan $5')
        self.assertEqual(start.call_args.args[1], 10)
        await start.call_args.args[2]()
        self.assertEqual(db.get_picks(self.draft, self.a), ['Michael Jordan'])
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 95)
    async def test_flux_rule_uses_latest_drop_and_live_cost_with_budget_validation(self):
        state = db.get_draft(self.draft)['state']; state['teams'][0]['budget_spent'] = 60
        db.set_draft_state(self.draft, state)
        pick = dict(option('Booker', 30), flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.prepare_flux(pick)
        await self.upload([pick])
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value', 'Round 2'], ['Booker', 30, 27]]):
            self.assertTrue(await self.consume())
        self.assertEqual(self.channel.send.call_args.args[0], '2. TEAM Booker $27')
        self.assertEqual(db.get_draft(self.draft)['state']['teams'][0]['budget_spent'], 87)

    async def test_fractional_displayed_flux_price_is_not_rounded_in_public_pick(self):
        state = db.get_draft(self.draft)['state']; state['teams'][0]['budget_spent'] = 60
        db.set_draft_state(self.draft, state)
        pick = dict(option('Booker', 30.5), flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.prepare_flux(pick)
        await self.upload([pick])
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value', 'Round 2'], ['Booker', '$30.50', '$27.50']]):
            self.assertTrue(await self.consume())
        self.assertEqual(self.channel.send.call_args.args[0], '2. TEAM Booker $27.5')
        self.assertEqual(db.get_draft(self.draft)['state']['teams'][0]['budget_spent'], 87.5)

    async def test_unmet_flux_rule_expires_with_dm_and_cannot_trigger_on_later_flux(self):
        import config
        state = db.get_draft(self.draft)['state']; state['teams'][0]['budget_spent'] = 60
        db.set_draft_state(self.draft, state)
        pick = dict(option('Booker', 30), flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.prepare_flux(pick)
        await self.upload([pick])
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value', 'Round 2'], ['Booker', 30, 29]]):
            self.assertFalse(await self.consume())
        self.assertEqual(len(db.get_drafter(self.a)['state']['draftlist_queue']['picks']), 1)
        message = SimpleNamespace(author=SimpleNamespace(bot=True, id=config.FLUX_BOT_ID),
            channel=SimpleNamespace(id=99), id=10000, content='✅ **Round 2 Flux is done**')
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value', 'Round 2'], ['Booker', 30, 32]]):
            await anon_lists.observe_flux(self.bot, message)
            await anon_lists.observe_flux(self.bot, message, run=True)
            await anon_lists.observe_flux(self.bot, message, run=True)
        self.channel.send.assert_not_awaited()
        self.assertEqual(db.get_drafter(self.a)['state']['draftlist_queue']['picks'], [])
        notices = self.bot.get_user.return_value.send.call_args_list
        self.assertEqual(len(notices), 1)
        self.assertIn('$30 \u2192 $32', notices[0].kwargs['content'])
        self.assertIn('Set new options', notices[0].kwargs['content'])

    async def test_flux_rule_never_exceeds_team_budget_and_retains_condition(self):
        pick = dict(option('Booker', 30), flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.prepare_flux(pick)
        await self.upload([pick])
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value', 'Round 2'], ['Booker', 30, 27]]):
            self.assertFalse(await self.consume())
        self.channel.send.assert_not_awaited()
        self.assertEqual(len(db.get_drafter(self.a)['state']['draftlist_queue']['picks']), 1)

    async def test_owner_roster_includes_cogms_and_rejects_nonowner(self):
        import config
        state = db.get_draft(self.draft)['state']; state['teams'][0]['member_ids'] = [self.a, self.b]
        db.set_draft_state(self.draft, state)
        roster = await anon_lists.roster(self.draft, config.OWNER_ID, 10, action='enable')
        self.assertEqual([m['user_id'] for m in roster['members']], ['100', '101'])
        with self.assertRaises(ValueError): await anon_lists.roster(self.draft, -1, 10, action='enable')

    async def test_owner_disable_prevents_late_upload_and_reenable_allows_fresh_lists(self):
        import config
        current = await self.upload([option('Choice')])
        await anon_lists.roster(self.draft, config.OWNER_ID, 10, action='disable')
        with self.assertRaises(ValueError):
            await self.upload([option('Late')], version='late', revision=current['revision'])
        roster = await anon_lists.roster(self.draft, config.OWNER_ID, 10, action='enable')
        member = roster['members'][0]
        self.assertTrue(member['enabled'])
        self.assertEqual(member['picks'], [])
        await self.upload([option('Fresh')], version='fresh', revision=member['revision'])

    def test_full_seasons_keep_the_ending_year(self):
        self.assertEqual(anon_lists.validate([option('Booker', year='2022-23')])[0]['year'], '23')
        self.assertEqual(anon_lists.validate([option('Roy', year='2008-09')])[0]['year'], '09')

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name)/'lists.db'); db.init_db(); _draft_locks.clear()
        self.theme = themes.get_theme('anon_budget_draft')
        self.price_patch = patch.object(self.theme, 'validate_round_price', new_callable=AsyncMock, return_value=None)
        self.price_patch.start()
        self.addCleanup(self.price_patch.stop)
        self.draft = db.create_draft(10, None, 'anon_budget_draft', 'Test', self.theme.default_settings())
        self.a = db.add_drafter(self.draft, 100, 'A')
        self.b = db.add_drafter(self.draft, 101, 'B')
        for mid in (self.a, self.b): db.update_drafter_state(mid, {'team_index': 0})
        db.set_draft_state(self.draft, {'teams': [dict(member_ids=[self.a], logo='TEAM', budget_spent=90,
            open_picks=[dict(pick_number=1, skipped=False, proposal=None, pending_from=[])], timer_pick_number=1,
            timer_deadline=None)], 'live_pick_number': 1, 'timer_channel_id': 99})
        db.set_draft_status(self.draft, 'paused')
        self.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=200)))
        self.bot = Mock(get_channel=Mock(return_value=self.channel), get_user=Mock(return_value=SimpleNamespace(send=AsyncMock())))
        self.advance = patch.object(self.theme, '_advance_live_pick', new_callable=AsyncMock)
        self.advance.start()

    async def asyncTearDown(self):
        self.advance.stop()
        for timer in (_timer, _proposal_timer):
            for key in list(timer._tasks): timer.cancel(key)
        await asyncio.sleep(0)
        self.tmp.cleanup()

    async def upload(self, picks, user=100, version='first', revision=0, enabled=True):
        return await anon_lists.update(self.bot, self.draft, user, 10,
            dict(version=version, expected_revision=revision, picks=picks, enabled=enabled))

    def prepare_flux(self, pick):
        pick.update(flux_from_round=1, flux_target_round=2)
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=2, flux_wait={'round': 2})
        state['teams'][0].update(open_picks=[], timer_pick_number=None)
        db.set_draft_state(self.draft, state)
        db.record_pick(self.draft, self.a, 'Prior Round Player', pick_number=1)

    async def consume(self):
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        if not state['teams'][0]['open_picks']:
            state.pop('flux_wait', None)
            state['teams'][0]['open_picks'] = [dict(pick_number=state['live_pick_number'], skipped=False, proposal=None, pending_from=[])]
            state['teams'][0]['timer_pick_number'] = state['live_pick_number']
            ctx.save_state(state)
        return await anon_lists.consume(ctx, ctx.state, 0, ctx.state['teams'][0]['open_picks'][0])

    async def test_flux_registration_requires_completed_current_pick_and_blocks_repeat_after_clear(self):
        pick = dict(option('Booker', 30), flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'), flux_from_round=1, flux_target_round=2)
        with self.assertRaisesRegex(ValueError, 'current-round pick'):
            await self.upload([pick])
        self.prepare_flux(pick)
        result = await self.upload([pick])
        result = await self.upload([], version='clear', revision=result['revision'])
        with self.assertRaisesRegex(ValueError, 'already submitted'):
            await self.upload([pick], version='repeat', revision=result['revision'])

    async def test_skip_is_numbered_in_public_draft_channel(self):
        import time
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['timer_deadline'] = time.time()-10
        db.set_draft_state(self.draft, state)
        await self.theme._resolve_on_timeout(DraftContext(self.bot, self.draft), 0, 1)
        self.channel.send.assert_awaited_once_with('1. TEAM skipped')

    async def test_makeup_replies_to_its_saved_skip_message(self):
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        await self.theme._skip_locked(ctx, 0, ctx.state['teams'][0]['open_picks'][0], 'Test skip')
        entry = ctx.state['teams'][0]['open_picks'][0]
        await self.theme._finalize_pick(ctx, 0, entry, 'Makeup Player', '', 5)
        call = self.channel.send.call_args
        self.assertEqual(call.kwargs['reference'].message_id, 200)
        self.assertEqual(call.kwargs['reference'].channel_id, 99)
        self.assertFalse(call.kwargs['allowed_mentions'].users)

    async def test_earlier_owed_pick_autoskips_new_turn_before_clock_or_lists(self):
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=2, flux_ready_round=2)
        state['teams'][0]['open_picks'][0]['skipped'] = True
        state['teams'][0]['timer_pick_number'] = None
        db.set_draft_state(self.draft, state)
        with patch.object(self.theme, '_try_consume_queued_pick', new_callable=AsyncMock) as consume:
            await self.theme._open_pick_for_team(DraftContext(self.bot, self.draft), 0, 2)
            consume.assert_not_awaited()
        self.assertTrue(all(p['skipped'] for p in db.get_draft(self.draft)['state']['teams'][0]['open_picks']))
        self.channel.send.assert_awaited_once_with('2. TEAM skipped')

    async def test_round_two_price_is_verified_before_acceptance_including_makeup(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=2, flux_price_source=dict(sheet='test-sheet-id', tab='Prices'))
        state['teams'][0].update(budget_spent=0)
        state['teams'][0]['open_picks'][0]['skipped'] = True
        db.set_draft_state(self.draft, state)
        db.set_draft_status(self.draft, 'active')
        rows = [['Player', 'Value', 'Round 2', 'Round 3'], ['Kevin Garnett', 38, 41, 40]]
        ctx = DraftContext(self.bot, self.draft)
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True, return_value=rows):
            error = await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Kevin Garnett $39')
            self.assertIn('costs $41 in Rounds 1', error)
            self.assertEqual(ctx.state['teams'][0]['budget_spent'], 0)
            self.channel.send.assert_not_awaited()
            await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Kevin Garnett $41')
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 41)

    async def test_makeup_before_flux_uses_finished_round_and_preserves_hold(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=2, flux_wait={'round': 2}, flux_price_source=dict(sheet='test-sheet-id', tab='Prices'))
        state['teams'][0].update(budget_spent=0, timer_pick_number=None)
        state['teams'][0]['open_picks'][0]['skipped'] = True
        db.set_draft_state(self.draft, state)
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value'], ['LeBron James', 40]]):
            await self.theme.on_pick(ctx, db.get_drafter(self.a), 'LeBron James $40')
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 40)
        self.assertEqual(ctx.state['flux_wait'], {'round': 2})
        self.assertEqual(ctx.state['live_pick_number'], 2)
        self.assertEqual(self.channel.send.call_args.args[0], "1. TEAM LeBron James '13 $40")

    async def test_waiting_for_flux_does_not_allow_an_unowed_next_round_pick(self):
        db.update_draft_state(self.draft, {'flux_wait': {'round': 2}})
        error = await self.theme.on_pick(DraftContext(self.bot, self.draft), db.get_drafter(self.a), 'LeBron James $40')
        self.assertIn('Only owed makeup picks', error)
        self.channel.send.assert_not_awaited()

    async def test_makeup_uses_highest_of_all_elapsed_rounds_and_rejects_gaps(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=3, flux_price_source=dict(sheet='test-sheet-id', tab='Prices'))
        state['teams'][0].update(budget_spent=0)
        entry = state['teams'][0]['open_picks'][0]
        entry['skipped'] = True
        db.set_draft_state(self.draft, state)
        ctx = DraftContext(self.bot, self.draft)
        for costs in ([50, 42, 40], [40, 50, 42], [40, 42, 50]):
            with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                              return_value=[['Player', 'Value', 'Round 2', 'Round 3', 'Round 4'], ['LeBron James', *costs, 99]]):
                self.assertIsNone(await self.theme.validate_round_price(ctx, 'LeBron James', 50, entry=entry))
                error = await self.theme.validate_round_price(ctx, 'LeBron James', 40, entry=entry)
                self.assertIn('highest price for your makeup pick', error)
        for rows in ([['Player', 'Value', 'Round 3'], ['LeBron James', 50, 40]],
                     [['Player', 'Value', 'Round 2', 'Round 3'], ['LeBron James', 50, '', 40]]):
            with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True, return_value=rows):
                self.assertIn('could not verify', await self.theme.validate_round_price(ctx, 'LeBron James', 50, entry=entry))

    async def test_ordinary_round_pick_uses_current_price_and_running_flux_blocks_makeup(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=2, flux_price_source=dict(sheet='test-sheet-id', tab='Prices'))
        db.set_draft_state(self.draft, state)
        ctx = DraftContext(self.bot, self.draft)
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value', 'Round 2'], ['LeBron James', 50, 40]]):
            self.assertIsNone(await self.theme.validate_round_price(ctx, 'LeBron James', 40))
            state.update(flux_in_progress=True)
            db.set_draft_state(self.draft, state)
            self.assertIn('currently running', await self.theme.validate_round_price(ctx, 'LeBron James', 40))

    async def test_manual_skip_checks_current_team_and_is_idempotent(self):
        import timer_bridge, time
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']; state['teams'][0]['timer_deadline'] = time.time()+100
        db.set_draft_state(self.draft, state)
        self.bot.get_guild.return_value = None
        payload = dict(token='a:0:1', actor_id=999, request_id=123)
        with self.assertRaisesRegex(ValueError, 'Only a GM'):
            await timer_bridge.skip_turn(self.bot, self.draft, payload)
        payload['actor_id'] = 100
        await timer_bridge.skip_turn(self.bot, self.draft, payload)
        await timer_bridge.skip_turn(self.bot, self.draft, payload)
        self.channel.send.assert_awaited_once_with('1. TEAM skipped')

    async def test_ranked_fallback_skips_taken_and_expensive_then_clears_all_options(self):
        db.record_pick(self.draft, self.b, 'Taken', pick_number=0)
        await self.upload([option('Taken'), option('Too Expensive', 11), option('Available'), option('Leftover')])
        self.assertTrue(await self.consume())
        text = self.channel.send.call_args.args[0]
        self.assertEqual(text, '1. TEAM Available $5')
        self.assertNotIn('100', text)
        self.assertEqual(db.get_draft(self.draft)['state']['teams'][0]['budget_spent'], 95)
        self.assertEqual(db.get_drafter(self.a)['state']['draftlist_queue']['picks'], [])

    async def test_replayed_upload_cannot_restore_consumed_list(self):
        await self.upload([option('Available')])
        await self.consume()
        result = await self.upload([option('Available')])
        self.assertEqual(result['picks'], [])
        self.channel.send.assert_awaited_once()

    async def test_invalid_lists_do_not_replace_previous_list(self):
        original = await self.upload([option('Safe')])
        for bad in (None, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                await self.upload([option('Bad', bad)], version='bad', revision=original['revision'])
        self.assertEqual(db.get_drafter(self.a)['state']['draftlist_queue']['picks'][0]['player'], 'Safe')

    async def test_nonparticipant_and_wrong_guild_cannot_submit(self):
        with self.assertRaises(ValueError): await self.upload([option('Player')], user=999)
        with self.assertRaises(ValueError): anon_lists.participant(self.draft, 100, 99)

    async def test_stale_revision_fails_and_pause_or_flux_never_picks(self):
        first = await self.upload([option('One')])
        with self.assertRaises(ValueError): await self.upload([option('Two')], version='second')
        db.set_draft_status(self.draft, 'active')
        db.update_draft_state(self.draft, {'flux_wait': {'round': 1}})
        await self.upload([option('Two')], version='second', revision=first['revision'])
        self.channel.send.assert_not_awaited()

    async def test_multi_gm_list_uses_normal_proposal_and_manual_pick_clears_both_lists(self):
        state = db.get_draft(self.draft)['state']; state['teams'][0]['member_ids'] = [self.a, self.b]
        db.set_draft_state(self.draft, state)
        await self.upload([option('Choice'), option('Fallback')])
        await self.upload([option('Other')], user=101)
        with patch.object(self.theme, '_propose', new_callable=AsyncMock) as proposal:
            self.assertTrue(await self.consume())
            proposal.assert_awaited_once()
        ctx = DraftContext(self.bot, self.draft)
        await self.theme._finalize_pick(ctx, 0, ctx.state['teams'][0]['open_picks'][0], 'Manual Choice', '', 2)
        for mid in (self.a, self.b): self.assertEqual(db.get_drafter(mid)['state']['draftlist_queue']['picks'], [])

    async def test_upload_on_clock_submits_immediately_once_under_concurrency(self):
        db.set_draft_status(self.draft, 'active')
        results = await asyncio.gather(self.upload([option('Choice')]), self.upload([option('Choice')]))
        self.channel.send.assert_awaited_once()
        self.assertTrue(all(r['picks'] == [] for r in results))

    async def test_all_unavailable_keeps_turn_open_and_completed_draft_allows_withdrawal(self):
        await self.upload([option('Expensive', 20)])
        self.assertFalse(await self.consume())
        self.assertTrue(db.get_draft(self.draft)['state']['teams'][0]['open_picks'])
        db.set_draft_status(self.draft, 'complete')
        current = db.get_drafter(self.a)['state']['draftlist_queue']
        result = await self.upload([], version='withdraw', revision=current['revision'], enabled=False)
        self.assertFalse(result['enabled'])

    async def test_resume_consumes_a_list_submitted_while_paused(self):
        await self.upload([option('Waiting')])
        db.set_draft_status(self.draft, 'active')
        await self.theme.resume_timers(DraftContext(self.bot, self.draft))
        self.assertEqual(self.channel.send.call_args.args[0], '1. TEAM Waiting $5')

    async def test_private_http_api_requires_key_and_correct_participant_guild(self):
        from aiohttp.test_utils import TestClient, TestServer
        from timer_bridge import create_app
        async with TestClient(TestServer(create_app(self.bot, 'test-private-key'))) as client:
            params = dict(draft=self.draft, user=100, guild=10)
            response = await client.get('/picklist', params=params)
            self.assertEqual(response.status, 401)
            headers = {'Authorization': 'Bearer test-private-key'}
            response = await client.get('/picklist', params=params, headers=headers)
            self.assertEqual(response.status, 200)
            response = await client.get('/picklist', params=dict(params, guild=999), headers=headers)
            self.assertEqual(response.status, 400)
            response = await client.post('/picklist', params=params, headers=headers,
                json=dict(version='api', expected_revision=0, picks=[option('Private')]))
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['picks'][0]['player'], 'Private')
