from datetime import datetime
import unittest
import anon_clock


class ClockPolicyTests(unittest.TestCase):
    def test_reported_denver_2304_turn_has_one_active_hour_plus_ten_hour_pause(self):
        start=datetime(2026,11,1,23,4,9,tzinfo=anon_clock.ET).timestamp()
        deadline=anon_clock.add_seconds(start,3600)
        self.assertEqual(datetime.fromtimestamp(deadline,anon_clock.ET).isoformat(),'2026-11-02T10:04:09-05:00')
        self.assertEqual(deadline-start,11*3600)
        self.assertEqual(anon_clock.seconds_left(deadline,start),3600)
        midnight=datetime(2026,11,2,0,tzinfo=anon_clock.ET).timestamp()
        self.assertEqual(anon_clock.seconds_left(deadline,midnight),4*60+9)
        self.assertTrue(anon_clock.in_window(start))
        self.assertFalse(anon_clock.in_window(midnight))

    def test_midnight_is_a_deadline_when_full_clock_fits_before_closing(self):
        start=datetime(2026,11,1,23,tzinfo=anon_clock.ET).timestamp()
        deadline=anon_clock.add_seconds(start,3600)
        self.assertEqual(datetime.fromtimestamp(deadline,anon_clock.ET).isoformat(),'2026-11-02T00:00:00-05:00')

    def test_spring_forward_and_fall_back_preserve_local_ten_am_resume(self):
        for year,month,day,next_day,pause_hours in ((2026,3,7,8,9),(2026,10,31,1,11)):
            start=datetime(year,month,day,23,50,tzinfo=anon_clock.ET).timestamp()
            deadline=anon_clock.add_seconds(start,1800)
            local=datetime.fromtimestamp(deadline,anon_clock.ET)
            self.assertEqual((local.day,local.hour,local.minute),(next_day,10,20))
            self.assertEqual(deadline-start,pause_hours*3600+1800)
            self.assertEqual(anon_clock.seconds_left(deadline,start),1800)

    def test_overnight_card_distinguishes_active_allowance_from_wall_clock_deadline(self):
        from clock_card import clock_embed
        start=datetime(2026,11,1,23,4,9,tzinfo=anon_clock.ET).timestamp()
        deadline=anon_clock.add_seconds(start,3600)
        details=anon_clock.clock_details(deadline,{},start)
        self.assertEqual(details,anon_clock.clock_details(deadline,{},start+20))
        data=dict(mode='anon',status='active',rounds=10,current=dict(logo='TEAM',pick=52,round=2,
                  deadline=deadline,timer_minutes=60,**details))
        description=clock_embed(data).description
        self.assertIn('Active timer allowance: 60 minutes',description)
        self.assertIn('midnight to 10:00 AM ET',description)
        self.assertIn('10:04 AM EST',description)
        self.assertNotIn('Turn expires',description)

    def test_closed_card_shows_frozen_seconds_and_does_not_change_overnight(self):
        from clock_card import clock_embed
        start=datetime(2026,11,1,23,4,9,tzinfo=anon_clock.ET).timestamp()
        deadline=anon_clock.add_seconds(start,3600)
        one=datetime(2026,11,2,1,tzinfo=anon_clock.ET).timestamp()
        nine=datetime(2026,11,2,9,tzinfo=anon_clock.ET).timestamp()
        details=anon_clock.clock_details(deadline,{},one)
        self.assertEqual(details,anon_clock.clock_details(deadline,{},nine))
        data=dict(mode='anon',status='active',rounds=10,window_closed=True,current=dict(logo='TEAM',pick=52,
                  round=2,deadline=deadline,timer_minutes=60,**details))
        self.assertIn('4m 9s',clock_embed(data).description)

    def test_round_schedule_and_ten_minutes_per_skip(self):
        settings = dict(base_timer_minutes=60, decay_minutes=0, floor_minutes=0)
        for round_number, base in ((1, 60), (2, 60), (3, 45), (6, 45), (7, 30), (8, 30), (9, 30), (10, 30)):
            pick = (round_number-1)*30+1
            self.assertEqual(anon_clock.minutes(settings, pick, 30, 0), base)
            self.assertEqual(anon_clock.minutes(settings, pick, 30, 2), base-20)
        self.assertEqual(anon_clock.minutes(settings, 271, 30, 4), 0)
        for round_number in range(7, 11):
            pick = (round_number-1)*30+1
            self.assertEqual([anon_clock.minutes(settings, pick, 30, skips) for skips in range(5)], [30,20,10,0,0])

    def test_migration_preserves_elapsed_time_and_never_deducts_twice(self):
        from types import SimpleNamespace
        settings = dict(base_timer_minutes=60, decay_minutes=10, floor_minutes=0, timer_window_et=False)
        for skips, old_left, new_left in [(0,35,20), (1,30,15), (2,20,5), (3,10,0)]:
            team = dict(timer_pick_number=181, skip_count=skips, timer_deadline=10000+old_left*60, open_picks=[])
            state = dict(live_pick_number=181, teams=[team]+[{} for _ in range(29)])
            ctx = SimpleNamespace(state=state, draft={'settings':settings}, save_state=lambda s: None)
            anon_clock.migrate_round_seven_clock(ctx,10000)
            self.assertEqual(team['timer_deadline'],10000+new_left*60)
            anon_clock.migrate_round_seven_clock(ctx,10000)
            self.assertEqual(team['timer_deadline'],10000+new_left*60)

    def test_migration_reduces_a_manually_paused_clock(self):
        from types import SimpleNamespace
        team = dict(timer_pick_number=181, skip_count=1, timer_deadline=None, paused_timer_seconds=35*60)
        state = dict(live_pick_number=181, teams=[team]+[{} for _ in range(29)], timers_off=True)
        ctx = SimpleNamespace(state=state, draft={'settings':dict(base_timer_minutes=60,floor_minutes=0)}, save_state=lambda s:None)
        anon_clock.migrate_round_seven_clock(ctx,10000)
        self.assertEqual(team['paused_timer_seconds'],20*60)

    def test_midnight_freeze_and_dst_boundary_resume_with_exact_remaining_time(self):
        start = datetime(2026, 10, 31, 23, 50, tzinfo=anon_clock.ET).timestamp()
        result = anon_clock.add_seconds(start, 30*60)
        self.assertEqual(datetime.fromtimestamp(result, anon_clock.ET).strftime('%Y-%m-%d %H:%M'), '2026-11-01 10:20')
        self.assertEqual(anon_clock.seconds_left(result, start), 1800)
        overnight = datetime(2026, 11, 1, 5, tzinfo=anon_clock.ET).timestamp()
        self.assertEqual(anon_clock.seconds_left(result, overnight), 1200)
        self.assertFalse(anon_clock.in_window(overnight))

    def test_pick_started_after_hours_receives_full_timer_at_ten(self):
        start = datetime(2026, 11, 2, 2, tzinfo=anon_clock.ET).timestamp()
        result = anon_clock.add_seconds(start, 3600)
        self.assertEqual(datetime.fromtimestamp(result, anon_clock.ET).strftime('%H:%M'), '11:00')
        self.assertEqual(anon_clock.seconds_left(result, start), 3600)

    def test_manual_off_blocks_timeouts_even_during_window(self):
        now = datetime(2026, 11, 2, 14, tzinfo=anon_clock.ET).timestamp()
        self.assertTrue(anon_clock.running({}, {}, now))
        self.assertFalse(anon_clock.running({'timers_off': True}, {}, now))
