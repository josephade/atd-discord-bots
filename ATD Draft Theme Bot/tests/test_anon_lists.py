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
    async def test_queued_duo_proposal_creates_main_clock_before_returning(self):
        import timer_bridge
        db.set_draft_status(self.draft,'active')
        state = db.get_draft(self.draft)['state']
        state['teams'][0].update(member_ids=[self.a,self.b],open_picks=[],timer_pick_number=None,timer_deadline=None)
        db.set_draft_state(self.draft,state)
        db.update_drafter_state(self.a,{'queued_pick':dict(player='Michael Jordan',year='91',price=5)})
        ctx = DraftContext(self.bot,self.draft)
        await self.theme._open_pick_for_team(ctx,0,1)
        team = ctx.state['teams'][0]
        self.assertEqual(team['timer_pick_number'],1)
        self.assertIsNotNone(team['timer_deadline'])
        entry = team['open_picks'][0]
        self.assertEqual(entry['proposal']['player'],'Michael Jordan')
        self.assertLessEqual(entry['proposal_deadline'],team['timer_deadline'])
        current = timer_bridge.snapshot(db.get_draft(self.draft))['current']
        self.assertEqual(current['pick'],1)
        self.assertIsNotNone(current['deadline'])

    async def test_restart_repairs_orphaned_duo_proposal_without_resetting_agreement_time(self):
        import time
        db.set_draft_status(self.draft,'active')
        state = db.get_draft(self.draft)['state']
        deadline = time.time()+300
        state['teams'][0].update(member_ids=[self.a,self.b],timer_pick_number=None,timer_deadline=None)
        entry = state['teams'][0]['open_picks'][0]
        entry.update(proposal=dict(player='Michael Jordan',year='91',price=5,by=self.a),pending_from=[self.b],proposal_deadline=deadline)
        db.set_draft_state(self.draft,state)
        ctx = DraftContext(self.bot,self.draft)
        await self.theme.resume_timers(ctx)
        team = ctx.state['teams'][0]
        self.assertEqual(team['timer_pick_number'],1)
        # Five minutes of the 60-minute first-round clock have already elapsed.
        self.assertAlmostEqual(team['timer_deadline']-time.time(),55*60,delta=1)
        self.assertAlmostEqual(team['open_picks'][0]['proposal_deadline'],deadline,delta=1)
        self.assertEqual(team['open_picks'][0]['proposal']['player'],'Michael Jordan')

    async def test_verified_negative_pick_credits_budget_and_rejects_wrong_price(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['budget_spent'] = 0
        state['flux_price_source'] = dict(sheet='test-sheet-id',tab='Prices')
        db.set_draft_state(self.draft,state)
        db.set_draft_status(self.draft,'active')
        ctx = DraftContext(self.bot,self.draft)
        with patch.object(anon_lists.sheets,'load_flux_rule_rows',create=True,return_value=[['Player','Value'],['Nene',-1]]):
            self.assertIn('costs $-1',await self.theme.on_pick(ctx,db.get_drafter(self.a),'Nene $-2'))
            self.assertEqual(ctx.state['teams'][0]['budget_spent'],0)
            await self.theme.on_pick(ctx,db.get_drafter(self.a),'Nene $-1')
        self.assertEqual(ctx.state['teams'][0]['budget_spent'],-1)
        self.assertEqual(db.get_picks(self.draft,self.a),['Nene'])
        self.assertIn('$-1',self.channel.send.call_args.args[0])
        self.assertEqual(anon_lists.validate([option('Nene',-1)])[0]['price'],-1)

    async def test_three_skip_penalties_immediately_skip_round_seven_without_starting_a_clock(self):
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=7,flux_ready_round=7,timers_off=True)
        state['teams'][0].update(skip_count=3, open_picks=[], timer_pick_number=None)
        db.set_draft_state(self.draft,state)
        with patch.object(self.theme,'_skip_locked',new_callable=AsyncMock) as skip, patch.object(self.theme,'_start_team_timer') as start:
            await self.theme._open_pick_for_team(DraftContext(self.bot,self.draft),0,7)
        skip.assert_awaited_once()
        self.assertIn('no time',skip.call_args.args[-1])
        start.assert_not_called()
        self.bot.get_user.assert_not_called()

    async def test_theme_publishes_exact_clock_card_once_then_reposts_changed_turn(self):
        import clock_presenter
        self.channel.get_partial_message = Mock(return_value=SimpleNamespace(delete=AsyncMock()))
        await clock_presenter.publish(self.bot, self.draft)
        await clock_presenter.publish(self.bot, self.draft)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.call_args.kwargs['embed'].title, 'Anonymous Budget Draft')
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['timer_deadline'] = 1234567890
        db.set_draft_state(self.draft, state)
        await clock_presenter.publish(self.bot, self.draft)
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertIn('TEAM', self.channel.send.call_args.kwargs['embed'].description)
        self.channel.get_partial_message.return_value.delete.assert_awaited_once()
        saved = db.get_draft(self.draft)['state']['theme_clock_card']
        self.assertEqual(saved['id'], 200)

    async def test_timezone_transition_dms_every_gm_schedule_only_and_records_deliveries(self):
        import clock_presenter
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['timer_deadline'] = 1234567890
        db.set_draft_state(self.draft, state)
        db.update_draft_settings(self.draft, {'timer_window_et': True})
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0]['member_ids'] = [self.a, self.b]
        ctx.save_state(state)
        users = {uid: SimpleNamespace(send=AsyncMock()) for uid in (100, 101)}
        self.bot.get_user.side_effect = users.get
        # Timer Bot delivered the old notice; Theme must still DM its own card.
        with patch('anon_clock.in_window', return_value=False):
            import anon_clock
            period = anon_clock.window_info(db.get_draft(self.draft))['key']
            db.update_draft_state(self.draft, {'theme_window_delivery': dict(key='previous:open', users=[], complete=True, public=True)})
            await clock_presenter.publish(self.bot, self.draft)
            await clock_presenter.publish(self.bot, self.draft)
        self.channel.send.assert_awaited_once()
        card = self.channel.send.call_args.kwargs['embed']
        for user in users.values():
            user.send.assert_awaited_once()
            self.assertEqual(user.send.call_args.kwargs['embed'].title, 'Draft timer hours')
            self.assertNotIn('is on the clock', user.send.call_args.kwargs['embed'].description)
            self.assertIn('Timers are OFF', user.send.call_args.kwargs['embed'].description)
        self.assertTrue(db.get_draft(self.draft)['state']['theme_window_delivery']['complete'])

    async def test_starting_draft_in_either_window_never_broadcasts_personal_turn(self):
        import clock_presenter
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['timer_deadline'] = 1234567890
        db.set_draft_state(self.draft, state)
        db.update_draft_settings(self.draft, {'timer_window_et': True})
        for opened in (True, False):
            db.update_draft_state(self.draft, {'theme_window_delivery': {}})
            with patch('anon_clock.in_window', return_value=opened):
                self.channel.get_partial_message = Mock(return_value=SimpleNamespace(delete=AsyncMock()))
                for n in range(3): await clock_presenter.publish(self.bot, self.draft)
            self.assertTrue(db.get_draft(self.draft)['state']['theme_window_delivery']['complete'])
        self.bot.get_user.assert_not_called()

    async def test_presenter_never_posts_placeholder_before_deadline_is_saved(self):
        import clock_presenter
        db.set_draft_status(self.draft, 'active')
        await clock_presenter.publish(self.bot, self.draft)
        self.channel.send.assert_not_awaited()
        self.assertNotIn('theme_clock_card', db.get_draft(self.draft)['state'])

    async def test_clock_publication_waits_for_in_progress_pick_state_save(self):
        import clock_presenter
        from themes.base import draft_lock
        db.set_draft_status(self.draft, 'active')
        async with draft_lock(self.draft):
            stale_state = db.get_draft(self.draft)['state']
            stale_state['teams'][0]['timer_deadline'] = 1234567890
            task = asyncio.create_task(clock_presenter.publish(self.bot, self.draft))
            await asyncio.sleep(0)
            self.channel.send.assert_not_awaited()
            db.set_draft_state(self.draft, stale_state)
        await task
        self.channel.send.assert_awaited_once()
        self.assertIn('Turn expires', self.channel.send.call_args.kwargs['embed'].description)
        saved = db.get_draft(self.draft)['state']['theme_clock_card']
        self.assertEqual(saved['id'], 200)
        await clock_presenter.publish(self.bot, self.draft)
        self.channel.send.assert_awaited_once()

    async def test_turn_card_only_goes_to_both_gms_on_current_team(self):
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0]['member_ids'] = [self.a, self.b]
        other = db.add_drafter(self.draft, 102, 'Other team')
        db.update_drafter_state(other, {'team_index': 1})
        state['teams'].append(dict(member_ids=[other], logo='OTHER', open_picks=[], skip_count=0, timer_pick_number=None))
        ctx.save_state(state)
        users = {uid: SimpleNamespace(send=AsyncMock()) for uid in (100, 101, 102)}
        self.bot.get_user.side_effect = users.get
        await self.theme._open_pick_for_team(ctx, 0, 1)
        for uid in (100, 101):
            users[uid].send.assert_awaited_once()
            self.assertIn('TEAM', users[uid].send.call_args.kwargs['embed'].description)
            self.assertIn('on the clock', users[uid].send.call_args.kwargs['content'])
        users[102].send.assert_not_awaited()

    async def test_timer_window_delivery_receipts_survive_retries_and_include_all_partners(self):
        import anon_clock
        from datetime import datetime
        db.set_draft_status(self.draft, 'active')
        db.update_draft_settings(self.draft, {'timer_window_et': True})
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0]['member_ids'] = [self.a, self.b]
        ctx.save_state(state)
        def stamp(hour): return datetime(2026, 11, 1, hour, tzinfo=anon_clock.ET).timestamp()
        db.update_draft_state(self.draft, {'theme_window_delivery': dict(key='2026-10-31:open', complete=True)})
        first = await anon_clock.window_delivery(self.draft, timestamp=stamp(0))
        self.assertEqual(first['pending_users'], [100, 101])
        partial = await anon_clock.window_delivery(self.draft, dict(key=first['key'], users=[100], public=True), timestamp=stamp(5))
        self.assertEqual(partial['pending_users'], [101])
        done = await anon_clock.window_delivery(self.draft, dict(key=first['key'], users=[101], public=True), timestamp=stamp(5))
        self.assertEqual(done['pending_users'], [])
        morning = await anon_clock.window_delivery(self.draft, timestamp=stamp(10))
        self.assertEqual(morning['pending_users'], [100, 101])
        self.assertFalse(morning['public'])
        self.channel.send.assert_not_awaited()
        self.bot.get_user.assert_not_called()

    async def test_window_snapshot_has_schedule_but_no_gm_identities(self):
        import anon_clock, timer_bridge
        from datetime import datetime
        db.set_draft_status(self.draft, 'active')
        db.update_draft_settings(self.draft, {'timer_window_et': True})
        at_midnight = datetime(2026, 11, 1, 0, tzinfo=anon_clock.ET).timestamp()
        info = anon_clock.window_info(db.get_draft(self.draft), at_midnight)
        opening = int(datetime(2026, 11, 1, 10, tzinfo=anon_clock.ET).timestamp())
        self.assertIn(str(opening), info['text'])
        db.update_draft_state(self.draft, {'timers_off': True})
        info = anon_clock.window_info(db.get_draft(self.draft), opening)
        self.assertIn('remain OFF', info['text'])
        snapshot = timer_bridge.snapshot(db.get_draft(self.draft))
        self.assertIn('window_notice', snapshot)
        self.assertNotIn('pending_users', str(snapshot))

    async def test_window_receipt_action_requires_authenticated_matching_channel(self):
        from aiohttp.test_utils import TestClient, TestServer
        from timer_bridge import create_app
        db.set_draft_status(self.draft, 'active')
        db.update_draft_settings(self.draft, {'timer_window_et': True})
        async with TestClient(TestServer(create_app(self.bot, 'key'))) as client:
            path = f'/clock/{self.draft}?channel=99&guild=10'
            response = await client.post(path, json={'action': 'window-notice'})
            self.assertEqual(response.status, 401)
            response = await client.post(path.replace('channel=99', 'channel=98'), headers={'Authorization': 'Bearer key'}, json={'action': 'window-notice'})
            self.assertEqual(response.status, 404)
            response = await client.post(path, headers={'Authorization': 'Bearer key'}, json={'action': 'window-notice'})
            self.assertEqual(response.status, 200)

    async def test_phone_apostrophes_split_year_from_player_before_price_lookup(self):
        ctx = DraftContext(self.bot, self.draft)
        for quote in ("'", '\u2018', '\u2019', '\u02bc', '\u00b4', '`'):
            with self.subTest(quote=quote):
                self.assertEqual(self.theme._parse_submission(ctx, f'Tracy McGrady {quote}03 $31'),
                                 ('Tracy McGrady', '03', 31))
        self.assertEqual(self.theme._parse_submission(ctx, "Shaquille O\u2019Neal \u201803 $38"),
                         ("Shaquille O'Neal", '03', 38))

    async def test_tracy_screenshot_pick_verifies_clean_name_and_records_explicit_year(self):
        self.price_patch.stop()
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0]['budget_spent'] = 0
        state['flux_price_source'] = dict(sheet='test-sheet-id', tab='Prices')
        ctx.save_state(state)
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value'], ['Tracy McGrady', 31]]):
            await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Tracy McGrady \u201803 $31')
        self.assertEqual(db.get_picks(self.draft, self.a), ['Tracy McGrady'])
        self.assertEqual(ctx.state['teams'][0]['pick_prices']['1']['year'], '03')
        self.assertEqual(ctx.state['teams'][0]['budget_spent'], 31)
        self.assertIn("Tracy McGrady '03 $31", self.channel.send.call_args.args[0])

    async def test_already_off_legacy_turn_gets_one_notice_on_repeat_command(self):
        import anon_clock, config
        db.set_draft_status(self.draft, 'active')
        db.update_draft_state(self.draft, {'timers_off': True})
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, False)
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, False)
        send = self.bot.get_user.return_value.send
        send.assert_awaited_once()
        self.assertIn('Timers are now off', send.call_args.kwargs['content'])
    async def test_timer_toggle_notifies_both_current_cogms_once_and_no_other_team(self):
        import anon_clock, config, time
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0].update(member_ids=[self.a, self.b], timer_deadline=time.time()+300)
        other = db.add_drafter(self.draft, 102, 'Other team')
        state['teams'].append(dict(member_ids=[other], logo='OTHER', open_picks=[], timer_pick_number=None))
        ctx.save_state(state)
        users = {uid: SimpleNamespace(send=AsyncMock()) for uid in (100, 101, 102)}
        self.bot.get_user.side_effect = users.get
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, False)
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, False)
        for uid in (100, 101):
            users[uid].send.assert_awaited_once()
            self.assertIn('Timers are now off', users[uid].send.call_args.kwargs['content'])
        users[102].send.assert_not_awaited()
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, True)
        for uid in (100, 101):
            self.assertEqual(users[uid].send.await_count, 2)
            self.assertIn('Timers are back on', users[uid].send.call_args.kwargs['content'])
            self.assertIn('Pick deadline:', users[uid].send.call_args.kwargs['content'])

    async def test_no_current_team_is_notified_during_flux_hold(self):
        import anon_clock, config
        db.set_draft_status(self.draft, 'active')
        db.update_draft_state(self.draft, {'flux_wait': {'round': 2}})
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, False)
        self.bot.get_user.return_value.send.assert_not_awaited()
    async def test_private_clock_api_off_on_is_owner_only_and_keeps_live_turn(self):
        import config, time
        from aiohttp.test_utils import TestClient, TestServer
        from timer_bridge import create_app
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0]['timer_deadline'] = time.time()+240
        ctx.save_state(state)
        async with TestClient(TestServer(create_app(self.bot, 'key'))) as client:
            path = f'/clock/{self.draft}?channel=99&guild=10'
            headers = {'Authorization': 'Bearer key'}
            response = await client.post(path, headers=headers, json=dict(action='timeroff', actor_id=config.OWNER_ID))
            data = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(data['current']['pick'], 1)
            self.assertIsNone(data['current']['deadline'])
            self.assertEqual(data['clocks'], [])
            self.assertTrue(data['timers_off'])
            response = await client.post(path, headers=headers, json=dict(action='timeron', actor_id=-1))
            self.assertEqual(response.status, 400)
            response = await client.post(path, headers=headers, json=dict(action='timeron', actor_id=config.OWNER_ID))
            data = await response.json()
            self.assertFalse(data['timers_off'])
            self.assertTrue(230 < data['current']['deadline']-time.time() <= 240)

    async def test_teammate_approval_does_not_expire_while_timers_off(self):
        import anon_clock, config, time
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['timers_off'] = True
        state['teams'][0].update(member_ids=[self.a, self.b], paused_timer_seconds=300)
        ctx.save_state(state)
        await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Michael Jordan $5')
        self.assertFalse(_proposal_timer.is_running((self.draft, 0)))
        await self.theme._resolve_proposal_timeout(ctx, 0, 1)
        self.channel.send.assert_not_awaited()
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, True)
        entry = ctx.state['teams'][0]['open_picks'][0]
        self.assertTrue(290 < entry['proposal_deadline']-time.time() <= 300)
        self.assertLessEqual(entry['proposal_deadline'], ctx.state['teams'][0]['timer_deadline'])
    async def test_timer_off_freezes_expiry_and_timer_on_preserves_remaining(self):
        import anon_clock, config, time
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'][0]['timer_deadline'] = time.time()+240
        ctx.save_state(state)
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, False)
        frozen = ctx.state['teams'][0]['paused_timer_seconds']
        self.assertTrue(235 < frozen <= 240)
        self.assertIsNone(ctx.state['teams'][0]['timer_deadline'])
        await self.theme._resolve_on_timeout(ctx, 0, 1)
        self.channel.send.assert_not_awaited()
        self.assertEqual(ctx.state['teams'][0].get('skip_count', 0), 0)
        await anon_clock.toggle(self.bot, self.draft, config.OWNER_ID, True)
        self.assertTrue(230 < ctx.state['teams'][0]['timer_deadline']-time.time() <= 240)
        with self.assertRaises(ValueError):
            await anon_clock.toggle(self.bot, self.draft, -1, False)

    async def test_picks_and_next_team_dm_continue_while_timers_are_off(self):
        self.advance.stop()
        db.set_draft_status(self.draft, 'active')
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['timers_off'] = True
        state['teams'].append(dict(member_ids=[self.b], logo='SECOND', budget_spent=0,
                                   open_picks=[], timer_pick_number=None, timer_deadline=None, skip_count=0))
        db.update_drafter_state(self.b, {'team_index': 1})
        ctx.save_state(state)
        await self.theme.on_pick(ctx, db.get_drafter(self.a), 'Michael Jordan $5')
        self.assertEqual(ctx.state['live_pick_number'], 2)
        self.assertEqual(ctx.state['teams'][1]['timer_pick_number'], 2)
        self.assertIsNone(ctx.state['teams'][1]['timer_deadline'])
        messages = [call.kwargs.get('content', '') for call in self.bot.get_user.return_value.send.call_args_list]
        self.assertTrue(any('SECOND' in text and 'Timers are off' in text for text in messages))
    async def owner_submit(self, text='Michael Jordan $5', **changes):
        import owner_picks
        payload = dict(bot=self.bot, draft_id=self.draft, actor_id=owner_picks.OWNER_ID,
                       channel_id=1533242104304631969, guild_id=10,
                       team_ref='1', text=text, message_id=123)
        with patch.object(owner_picks, 'OWNER_REPORT_CHANNEL_ID', 1533242104304631969):
            return await owner_picks.submit(**(payload | changes))

    async def test_owner_pick_finalizes_for_cogms_without_approval_and_audits(self):
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['member_ids'] = [self.a, self.b]
        db.set_draft_state(self.draft, state)
        result = await self.owner_submit()
        self.assertIn('Recorded pick #1', result)
        self.assertEqual(db.get_picks(self.draft, self.a), ['Michael Jordan'])
        self.assertEqual(db.get_picks(self.draft, self.b), ['Michael Jordan'])
        state = db.get_draft(self.draft)['state']
        self.assertEqual(state['teams'][0]['budget_spent'], 95)
        self.assertEqual(state['owner_pick_audit'][0]['message_id'], 123)
        self.theme._advance_live_pick.assert_awaited_once()

    async def test_owner_pick_rejects_wrong_actor_channel_dm_guild_and_paused(self):
        db.set_draft_status(self.draft, 'active')
        for changes in (dict(actor_id=-1), dict(channel_id=99), dict(guild_id=None), dict(guild_id=11)):
            result = await self.owner_submit(**changes)
            self.assertNotIn('Recorded pick', result)
        db.set_draft_status(self.draft, 'paused')
        self.assertIn('must be active', await self.owner_submit())
        self.channel.send.assert_not_awaited()
        self.assertEqual(db.get_picks(self.draft, self.a), [])

    async def test_owner_pick_enforces_taken_budget_price_and_open_turn(self):
        db.set_draft_status(self.draft, 'active')
        db.record_pick(self.draft, self.b, 'Taken', pick_number=0)
        for text in ('Taken $5', 'Too Expensive $20'):
            self.assertNotIn('Recorded pick', await self.owner_submit(text))
        self.theme.validate_round_price.return_value = 'Wrong round price'
        self.assertEqual(await self.owner_submit(), 'Wrong round price')
        self.theme.validate_round_price.return_value = None
        state = db.get_draft(self.draft)['state']; state['teams'][0]['open_picks'] = []
        db.set_draft_state(self.draft, state)
        self.assertIn('not on the clock', await self.owner_submit())
        self.channel.send.assert_not_awaited()

    async def test_owner_makeup_during_flux_hold_replies_to_original_skip(self):
        db.set_draft_status(self.draft, 'active')
        state = db.get_draft(self.draft)['state']
        state.update(live_pick_number=2, flux_wait={'round': 2})
        state['teams'][0]['open_picks'][0].update(skipped=True, skip_message_id=555)
        db.set_draft_state(self.draft, state)
        self.assertIn('Recorded pick #1', await self.owner_submit(team_ref='TEAM'))
        self.assertEqual(self.channel.send.call_args.kwargs['reference'].message_id, 555)
        self.theme._advance_live_pick.assert_not_awaited()
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
        self.assertAlmostEqual(start.call_args.args[1], 10, delta=0.05)
        state = ctx.state
        state['teams'][0]['open_picks'][0]['proposal_deadline'] = time.time()-1
        ctx.save_state(state)
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
        self.assertEqual(roster['draft']['id'],self.draft)
        self.assertEqual(roster['members'][0]['logo'],db.get_draft(self.draft)['state']['teams'][0]['logo'])
        self.assertIn('display_name',roster['members'][0])
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
        self.draft = db.create_draft(10, None, 'anon_budget_draft', 'Test', {**self.theme.default_settings(), 'timer_window_et': False})
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

    async def test_flux_registration_requires_completed_current_pick_but_allows_replacement(self):
        pick = dict(option('Booker', 30), flux_drop=3, flux_source=dict(sheet='test-sheet-id', tab='Prices'), flux_from_round=1, flux_target_round=2)
        with self.assertRaisesRegex(ValueError, 'current-round pick'):
            await self.upload([pick])
        self.prepare_flux(pick)
        result = await self.upload([pick])
        result = await self.upload([], version='clear', revision=result['revision'])
        result = await self.upload([pick], version='repeat', revision=result['revision'])
        self.assertEqual(result['picks'][0]['player'], 'Booker')

    async def test_newer_replacement_ignores_stale_revision_and_older_submission(self):
        pick = dict(option('Sam Jones'), flux_drop=0, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.prepare_flux(pick)
        async def replace(picks, message):
            return await anon_lists.update(self.bot, self.draft, 100, 10,
                dict(version=str(message), picks=picks, replace_flux=True, submission_id=message, expected_revision=0))
        await replace([pick], 10)
        other = dict(pick, player='Joe Johnson', flux_drop=2)
        result = await replace([other, pick], 12)
        self.assertEqual([p['player'] for p in result['picks']], ['Joe Johnson', 'Sam Jones'])
        result = await replace([pick], 11)
        self.assertTrue(result['superseded'])
        self.assertEqual(len(result['picks']), 2)

    async def test_accepted_pick_removes_other_teams_paused_queue_and_notifies_once(self):
        db.update_drafter_state(self.b, {'team_index': 1, 'draftlist_queue': dict(
            picks=[option('Sam Jones'), option('Joe Johnson')], revision=3, enabled=False)})
        ctx = DraftContext(self.bot, self.draft)
        state = ctx.state
        state['teams'].append(dict(member_ids=[self.b], logo='OTHER', open_picks=[]))
        ctx.save_state(state)
        await self.theme._finalize_pick(ctx, 0, state['teams'][0]['open_picks'][0], 'Sam Jones', '65', 6)
        queue = db.get_drafter(self.b)['state']['draftlist_queue']
        self.assertEqual([p['player'] for p in queue['picks']], ['Joe Johnson'])
        self.assertEqual(queue['revision'], 4)
        send = self.bot.get_user.return_value.send
        texts = [call.kwargs.get('content', '') for call in send.call_args_list]
        self.assertEqual(sum('was drafted by TEAM' in text for text in texts), 1)
        count = send.await_count
        await anon_lists.prune_taken(DraftContext(self.bot, self.draft))
        self.assertEqual(send.await_count, count)

    async def test_replacement_filters_taken_options_without_rejecting_other_choices(self):
        pick = dict(option('Sam Jones'), flux_drop=0, flux_source=dict(sheet='test-sheet-id', tab='Prices'))
        self.prepare_flux(pick)
        db.record_pick(self.draft, self.b, 'Sam Jones', pick_number=0)
        result = await anon_lists.update(self.bot, self.draft, 100, 10,
            dict(version='replace', picks=[pick, dict(pick, player='Joe Johnson')], replace_flux=True, submission_id=15))
        self.assertEqual(result['unavailable'], ['Sam Jones'])
        self.assertEqual([p['player'] for p in result['picks']], ['Joe Johnson'])

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

    async def test_round_three_makeup_after_flux_uses_round_one_peak(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        count = len(state['teams'])
        state.update(live_pick_number=3*count+1, flux_price_source=dict(sheet='test-sheet-id', tab='Prices'))
        entry = dict(pick_number=2*count+1, skipped=True)
        db.set_draft_state(self.draft, state)
        ctx = DraftContext(self.bot, self.draft)
        rows = [['Player', 'Value', 'Round 2', 'Round 3', 'Round 4', 'Round 5'],
                ['LeBron James', 10, 7, 5, 5, 99]]
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True, return_value=rows):
            self.assertIsNone(await self.theme.validate_round_price(ctx, 'LeBron James', 10, entry=entry))
            error = await self.theme.validate_round_price(ctx, 'LeBron James', 5, entry=entry)
            self.assertIn('costs $10 in Rounds 1', error)
            # A normal Round 4 turn still uses the current $5 price.
            self.assertIsNone(await self.theme.validate_round_price(ctx, 'LeBron James', 5))
            # Before Round 4 Flux, making up Round 3 still costs Round 3's $5.
            state['flux_wait'] = {'round': 4}
            db.set_draft_state(self.draft, state)
            self.assertIsNone(await self.theme.validate_round_price(ctx, 'LeBron James', 5, entry=entry))
        state.pop('flux_wait')
        db.set_draft_state(self.draft, state)
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Round 2', 'Round 3', 'Round 4'], ['LeBron James', 7, 5, 5]]):
            self.assertIn('could not verify', await self.theme.validate_round_price(ctx, 'LeBron James', 5, entry=entry))

    async def test_off_adp_pick_records_zero_without_spending_budget(self):
        self.price_patch.stop()
        state = db.get_draft(self.draft)['state']
        state['flux_price_source'] = dict(sheet='test-sheet-id', tab='Prices')
        db.set_draft_state(self.draft, state)
        db.set_draft_status(self.draft, 'active')
        with patch.object(anon_lists.sheets, 'load_flux_rule_rows', create=True,
                          return_value=[['Player', 'Value'], ['Michael Jordan', 44]]):
            await self.theme.on_pick(DraftContext(self.bot, self.draft), db.get_drafter(self.a),
                                     'Unlisted Player $0')
        self.assertEqual(db.get_draft(self.draft)['state']['teams'][0]['budget_spent'], 90)
        self.assertIn('Unlisted Player', db.get_picks(self.draft, self.a))
        self.assertIn('Unlisted Player $0', self.channel.send.call_args.args[0])

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
        for bad in (None, float('nan'), float('inf')):
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
