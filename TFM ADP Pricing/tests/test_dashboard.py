from datetime import datetime, timezone
import unittest
from test_server import SiteTestCase
import results


class DashboardMetricsTests(unittest.TestCase):
    def test_middle_half_describes_spread_not_only_the_average(self):
        players = [dict(id=1, name='Example', current_price=20)]
        votes = [dict(player_id=1, answer_type='other', price=price) for price in (10, 20, 30, 40)]
        row = results.player_rows(players, votes)[0]
        self.assertEqual((row['median'], row['q1'], row['q3']), (25, 17.5, 32.5))
        self.assertEqual(results.player_rows(players, [votes[0]])[0]['q1'], 10)
        self.assertIsNone(results.player_rows(players, [])[0]['q1'])

    def test_empty_dashboard_and_completion_boundaries(self):
        now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        empty = results.dashboard([], [], [], [], [], [], now=now)
        self.assertEqual(empty['average_completion'], 0)
        self.assertEqual(len(empty['activity']), 7)
        self.assertEqual(empty['recent_answers'], [])
        players = [dict(id=i, name=f'Player {i}') for i in range(4)]
        voters = [dict(id=i, discord_username=f'Voter {i}', last_seen_at='2026-11-05T11:00:00Z') for i in range(3)]
        all_votes = [dict(voter_id=0, player_id=i) for i in range(4)] + [dict(voter_id=1, player_id=0)]
        dashboard = results.dashboard(players, voters, all_votes, [],
                                      [dict(votes=0) for _ in players], [], now=now)
        self.assertEqual(dashboard['average_completion'], 41.7)
        self.assertEqual([b['count'] for b in dashboard['completion']], [1, 0, 1, 0, 0, 1])
        self.assertEqual(dashboard['active_voters_24h'], 3)
        self.assertEqual(dashboard['excluded_answers'], 5)


class AdminDashboardTests(SiteTestCase):
    async def test_metrics_remain_consistent_across_delete_and_restore(self):
        await self.start_voter()
        await self.login_admin()
        await self.vote_next()
        await self.vote_next()
        card = await self.next_card()
        response = await self.client.put(f"/api/votes/{card['player']['id']}", json={'answer_type': 'dont_know'})
        self.assertEqual(response.status, 200)
        vote = self.store.all_votes()[0]
        batch = self.store.delete_vote(vote['id'], 'Excluded for dashboard test')
        response = await self.client.get('/api/admin/players')
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['overview']['total_votes'], 2)
        self.assertEqual(data['dashboard']['excluded_answers'], 1)
        self.assertEqual(data['dashboard']['average_completion'], round(3 / 313 * 100, 1))
        self.assertEqual(sum(d['price_votes'] + d['dont_know'] for d in data['dashboard']['activity']), 2)
        self.assertNotIn(vote['id'], [v['vote_id'] for v in data['dashboard']['recent_answers']])
        self.assertEqual(len(data['dashboard']['completion']), 6)
        self.store.restore_batch(batch)
        restored = await (await self.client.get('/api/admin/players')).json()
        self.assertEqual(restored['dashboard']['excluded_answers'], 0)
        self.assertEqual(restored['overview']['total_votes'], 3)

    async def test_detail_statistics_and_last_export_are_explicit(self):
        await self.start_voter()
        await self.login_admin()
        card = await self.vote_next()
        detail = await (await self.client.get(f"/api/admin/players/{card['player']['id']}")).json()
        self.assertEqual(detail['summary']['votes'], 1)
        self.assertEqual(detail['summary']['median'], detail['summary']['q1'])
        self.store.log_action('export_csv', 'votes')
        data = await (await self.client.get('/api/admin/players')).json()
        self.assertEqual(data['last_export']['action'], 'export_csv')
        self.assertIn('generated_at', data)
