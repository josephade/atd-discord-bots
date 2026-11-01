from datetime import datetime
import unittest
import anon_clock


class ClockPolicyTests(unittest.TestCase):
    def test_round_schedule_and_ten_minutes_per_skip(self):
        settings = dict(base_timer_minutes=60, decay_minutes=0, floor_minutes=0)
        for round_number, base in ((1, 60), (2, 60), (3, 45), (8, 45), (9, 30), (10, 30)):
            pick = (round_number-1)*30+1
            self.assertEqual(anon_clock.minutes(settings, pick, 30, 0), base)
            self.assertEqual(anon_clock.minutes(settings, pick, 30, 2), base-20)
        self.assertEqual(anon_clock.minutes(settings, 271, 30, 4), 0)

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
