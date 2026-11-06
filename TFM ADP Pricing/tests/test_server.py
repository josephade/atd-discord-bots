import csv
import io
import json
import os
import sys
import tempfile
import unittest

from aiohttp import CookieJar
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pricing
import security
import server
from config import Settings
from store import Store

SECRET = 'test-secret'
PASSWORD = 'correct horse'


class FakeEmail:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def __call__(self, settings, recipient, link, answered, total):
        if self.error:
            raise self.error
        self.sent.append(dict(recipient=recipient, link=link, answered=answered, total=total))


class SiteTestCase(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            session_secret=SECRET,
            ip_hash_salt='salt',
            db_path=os.path.join(self.tmp.name, 'test.sqlite3'),
            admin_password=PASSWORD,
            public_base_url='https://vote.example',
            cookie_secure=False,
        )
        self.email = FakeEmail()
        self.exports = []
        app = server.create_app(self.settings, email_sender=self.email, sheets_exporter=self.fake_export)
        self.store = app[server.STORE]
        self.store.set_setting('voting_open', '1')
        return app

    def fake_export(self, settings, tabs):
        self.exports.append(tabs)
        return 'https://sheets.example/results'

    async def new_client(self):
        client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def start_voter(self, client=None, username='Hoops Fan'):
        client = client or self.client
        response = await client.post('/api/session', json={'username': username})
        self.assertEqual(response.status, 200, await response.text())
        return client

    async def next_card(self, client=None):
        response = await (client or self.client).get('/api/card')
        self.assertEqual(response.status, 200)
        return await response.json()

    async def vote_next(self, client=None, index=2):
        card = await self.next_card(client)
        response = await (client or self.client).put(
            f"/api/votes/{card['player']['id']}",
            json={'answer_type': 'option', 'price': card['options'][index]},
        )
        self.assertEqual(response.status, 200, await response.text())
        return card

    async def login_admin(self, client=None):
        response = await (client or self.client).post('/api/admin/login', json={'password': PASSWORD})
        self.assertEqual(response.status, 200)

    async def player_row(self, player_id, client=None):
        response = await (client or self.client).get('/api/admin/players')
        data = await response.json()
        return next(row for row in data['players'] if row['id'] == player_id)


class VoterFlowTests(SiteTestCase):
    async def test_card_has_options_and_never_marks_the_current_price(self):
        await self.start_voter()
        card = await self.next_card()
        self.assertEqual(card['position'], 1)
        self.assertEqual(card['total'], 313)
        self.assertEqual(len(card['options']), 5)
        self.assertNotIn('current_price', json.dumps(card))

    async def test_card_includes_the_players_photo_or_none(self):
        await self.start_voter()
        photos = self.app[server.PHOTOS]
        seen_photo = seen_missing = False
        for position in range(1, 314):
            card = await (await self.client.get(f'/api/card?position={position}')).json()
            self.assertEqual(card['player']['photo'], photos.get(card['player']['name']))
            seen_photo = seen_photo or card['player']['photo'] is not None
            seen_missing = seen_missing or card['player']['photo'] is None
        self.assertTrue(seen_photo)
        self.assertTrue(seen_missing)

    async def test_photo_hosts_are_allowed_by_the_page_policy(self):
        policy = (await self.client.get('/')).headers['Content-Security-Policy']
        self.assertIn('https://cdn.nba.com', policy)
        self.assertIn('https://thumb.wikimedia.org', policy)
        self.assertIn('https://upload.wikimedia.org', policy)

    async def test_username_is_cleaned_and_validated(self):
        response = await self.client.post('/api/session', json={'username': '  @tfm_guy '})
        self.assertEqual((await response.json())['voter']['username'], 'tfm_guy')
        for bad in ('a', 'x' * 33, '', 12, None, 'bad\nname'):
            response = await self.client.post('/api/session', json={'username': bad})
            self.assertEqual(response.status, 400, bad)

    async def test_vote_advances_to_the_next_player(self):
        await self.start_voter()
        first = await self.vote_next()
        second = await self.next_card()
        self.assertNotEqual(first['player']['id'], second['player']['id'])
        self.assertEqual(second['position'], 2)
        self.assertEqual(second['answered'], 1)

    async def test_tampered_prices_are_rejected(self):
        await self.start_voter()
        card = await self.next_card()
        path = f"/api/votes/{card['player']['id']}"
        off_list = max(card['options']) + 1
        bad_bodies = [
            {'answer_type': 'option', 'price': off_list},
            {'answer_type': 'option', 'price': float(card['options'][0])},
            {'answer_type': 'option', 'price': str(card['options'][0])},
            {'answer_type': 'other', 'price': 0},
            {'answer_type': 'other', 'price': 101},
            {'answer_type': 'other', 'price': 12.5},
            {'answer_type': 'other', 'price': True},
            {'answer_type': 'admin', 'price': 5},
        ]
        for body in bad_bodies:
            response = await self.client.put(path, json=body)
            self.assertEqual(response.status, 400, body)
        self.assertEqual((await self.next_card())['answered'], 0)

    async def test_other_and_dont_know_are_saved(self):
        await self.start_voter()
        card = await self.next_card()
        await self.client.put(f"/api/votes/{card['player']['id']}", json={'answer_type': 'other', 'price': 77})
        back = await (await self.client.get('/api/card?position=1')).json()
        self.assertEqual(back['answer'], {'answer_type': 'other', 'price': 77})

        card = await self.next_card()
        # Any price sent with Don't know him is ignored.
        await self.client.put(f"/api/votes/{card['player']['id']}", json={'answer_type': 'dont_know', 'price': 9})
        back = await (await self.client.get('/api/card?position=2')).json()
        self.assertEqual(back['answer'], {'answer_type': 'dont_know', 'price': None})

    async def test_changing_an_answer_keeps_one_row(self):
        await self.start_voter()
        card = await self.vote_next(index=0)
        await self.client.put(
            f"/api/votes/{card['player']['id']}",
            json={'answer_type': 'option', 'price': card['options'][4]},
        )
        votes = self.store.all_votes()
        self.assertEqual(len(votes), 1)
        self.assertEqual(votes[0]['price'], card['options'][4])

    async def test_partial_voters_count_in_results(self):
        await self.start_voter()
        card = await self.vote_next()
        await self.login_admin()
        row = await self.player_row(card['player']['id'])
        self.assertEqual(row['votes'], 1)
        data = await (await self.client.get('/api/admin/players')).json()
        self.assertEqual(data['overview']['finished_voters'], 0)
        self.assertEqual(data['overview']['total_voters'], 1)

    async def test_finishing_every_player_reports_done(self):
        await self.start_voter()
        voter = self.store.voters()[0]
        for player_id in json.loads(voter['player_order']):
            self.store.save_vote(voter['id'], player_id, 'dont_know', None)
        card = await self.next_card()
        self.assertTrue(card['done'])
        self.assertEqual(card['answered'], 313)
        # Back still works after finishing.
        response = await self.client.get('/api/card?position=313')
        self.assertFalse((await response.json())['done'])

    async def test_saved_order_stays_the_same_across_requests(self):
        await self.start_voter()
        first = await (await self.client.get('/api/card?position=7')).json()
        again = await (await self.client.get('/api/card?position=7')).json()
        self.assertEqual(first['player'], again['player'])

    async def test_voting_requires_a_session(self):
        response = await self.client.get('/api/card')
        self.assertEqual(response.status, 401)
        response = await self.client.put('/api/votes/1', json={'answer_type': 'dont_know'})
        self.assertEqual(response.status, 401)

    async def test_closed_voting_blocks_new_voters_and_votes(self):
        await self.start_voter()
        card = await self.next_card()
        self.store.set_setting('voting_open', '0')
        response = await self.client.post('/api/session', json={'username': 'late voter'})
        self.assertEqual(response.status, 403)
        response = await self.client.put(f"/api/votes/{card['player']['id']}", json={'answer_type': 'dont_know'})
        self.assertEqual(response.status, 403)

    async def test_writes_must_be_json(self):
        response = await self.client.post('/api/session', data={'username': 'form post'})
        self.assertEqual(response.status, 415)

    async def test_new_voter_limit_per_ip(self):
        for i in range(10):
            await self.start_voter(username=f'voter {i}')
        response = await self.client.post('/api/session', json={'username': 'voter eleven'})
        self.assertEqual(response.status, 429)

    async def test_vote_rate_limit(self):
        await self.start_voter()
        card = await self.next_card()
        path = f"/api/votes/{card['player']['id']}"
        for _ in range(60):
            response = await self.client.put(path, json={'answer_type': 'dont_know'})
            self.assertEqual(response.status, 200)
        response = await self.client.put(path, json={'answer_type': 'dont_know'})
        self.assertEqual(response.status, 429)

    async def test_session_cookie_flags(self):
        settings = Settings(session_secret=SECRET, ip_hash_salt='s', db_path=':memory:', cookie_secure=True)
        app = server.create_app(settings, store=Store(':memory:'))
        app[server.STORE].set_setting('voting_open', '1')
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/session', json={'username': 'flags'})
            cookie = response.cookies[server.SESSION_COOKIE]
            self.assertTrue(cookie['httponly'])
            self.assertTrue(cookie['secure'])
            self.assertEqual(cookie['samesite'], 'Lax')

    async def test_security_headers_are_set(self):
        for path in ('/', '/api/me', '/admin'):
            response = await self.client.get(path)
            self.assertIn('Content-Security-Policy', response.headers, path)
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer', path)


class ResumeLinkTests(SiteTestCase):
    async def resume_url(self, client=None):
        data = await (await (client or self.client).get('/api/resume-link')).json()
        self.assertTrue(data['link'].startswith('https://vote.example/r/'))
        return data['link'].replace('https://vote.example', '')

    async def test_link_is_available_before_any_vote_or_email(self):
        await self.start_voter()
        self.assertTrue(await self.resume_url())
        self.assertEqual(self.email.sent, [])

    async def test_link_resumes_the_same_voter_on_another_device(self):
        await self.start_voter()
        await self.vote_next()
        path = await self.resume_url()

        other = await self.new_client()
        response = await other.get(path, allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/')
        card = await self.next_card(other)
        self.assertEqual(card['answered'], 1)
        self.assertEqual(card['position'], 2)
        self.assertEqual(len(self.store.voters()), 1)
        # The first device stays signed in, and the link can be reused.
        self.assertEqual((await self.next_card())['answered'], 1)
        self.assertEqual((await other.get(path, allow_redirects=False)).status, 302)

    async def test_invalid_links_are_refused(self):
        await self.start_voter()
        good = await self.resume_url()
        voter_id = self.store.voters()[0]['id']
        bad_paths = ['/r/garbage', f'/r/{voter_id}.wrong', f'/r/{voter_id + 1}.{good.split(".", 1)[1]}', '/r/999.abc']
        for path in bad_paths:
            client = await self.new_client()
            response = await client.get(path, allow_redirects=False)
            self.assertEqual(response.headers['Location'], '/?link=invalid', path)
            self.assertEqual((await client.get('/api/card')).status, 401, path)

    async def test_odd_links_and_ids_are_refused_without_a_server_error(self):
        for path in ('/r/1.%C3%A9%C3%A9', '/r/%C3%A9', '/r/%ED%A0%80', '/r/' + '9' * 5000):
            response = await self.client.get(path, allow_redirects=False)
            self.assertEqual(response.status, 302, path)
            self.assertEqual(response.headers['Location'], '/?link=invalid', path)

        await self.start_voter()
        for player_id in ('99999999999999999999999999', '-1', 'abc'):
            response = await self.client.put(f'/api/votes/{player_id}', json={'answer_type': 'dont_know'})
            self.assertEqual(response.status, 404, player_id)

        await self.login_admin()
        for path in ('/api/admin/players/99999999999999999999', '/api/admin/voters/99999999999999999999'):
            self.assertEqual((await self.client.get(path)).status, 404, path)

    async def test_odd_admin_cookies_are_refused(self):
        nonce = self.store.admin_nonce()
        for token in ('9' * 5000, '99999999999.é', '.', ''):
            self.assertFalse(security.admin_token_valid(SECRET, PASSWORD, nonce, token), token[:12])

    async def test_resume_token_is_never_stored(self):
        await self.start_voter()
        path = await self.resume_url()
        token = path.rsplit('/', 1)[1]
        voter = self.store.voters()[0]
        self.assertNotIn(token, [str(value) for value in tuple(voter)])

    async def test_closed_voting_stops_votes_through_a_link(self):
        await self.start_voter()
        path = await self.resume_url()
        self.store.set_setting('voting_open', '0')
        other = await self.new_client()
        await other.get(path, allow_redirects=False)
        response = await other.put('/api/votes/1', json={'answer_type': 'dont_know'})
        self.assertEqual(response.status, 403)

    async def test_retyping_a_username_starts_a_new_voter(self):
        await self.start_voter(username='same name')
        await self.vote_next()
        other = await self.new_client()
        await self.start_voter(other, username='same name')
        self.assertEqual((await self.next_card(other))['answered'], 0)
        self.assertEqual(len(self.store.voters()), 2)


class EmailTests(SiteTestCase):
    async def test_email_sends_the_link_and_keeps_no_address(self):
        await self.start_voter()
        await self.vote_next()
        response = await self.client.post('/api/continue-later', json={'email': 'fan@example.com'})
        data = await response.json()
        self.assertTrue(data['sent'])
        self.assertEqual(self.email.sent[0]['recipient'], 'fan@example.com')
        self.assertEqual(self.email.sent[0]['answered'], 1)
        self.assertEqual(self.email.sent[0]['link'], data['link'])
        dump = '\n'.join(self.store.db.iterdump())
        self.assertNotIn('fan@example.com', dump)

    async def test_failed_send_still_returns_the_link(self):
        await self.start_voter()
        self.email.error = OSError('smtp down')
        response = await self.client.post('/api/continue-later', json={'email': 'fan@example.com'})
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertFalse(data['sent'])
        self.assertIn('/r/', data['link'])

    async def test_bad_addresses_are_rejected(self):
        await self.start_voter()
        for bad in ('', 'nope', 'a b@c.d', 'x@y', None, 5, 'a@b.c\r\nBcc: z@z.z'):
            response = await self.client.post('/api/continue-later', json={'email': bad})
            self.assertEqual(response.status, 400, bad)
        self.assertEqual(self.email.sent, [])

    async def test_three_emails_per_hour(self):
        await self.start_voter()
        for _ in range(3):
            response = await self.client.post('/api/continue-later', json={'email': 'fan@example.com'})
            self.assertEqual(response.status, 200)
        response = await self.client.post('/api/continue-later', json={'email': 'fan@example.com'})
        self.assertEqual(response.status, 429)
        self.assertEqual(len(self.email.sent), 3)


class AdminAuthTests(SiteTestCase):
    async def test_admin_endpoints_require_login(self):
        routes = [
            ('GET', '/api/admin/players'), ('GET', '/api/admin/players/1'), ('GET', '/api/admin/voters'),
            ('GET', '/api/admin/voters/1'), ('GET', '/api/admin/deletion-batches'), ('GET', '/api/admin/export.csv'),
            ('DELETE', '/api/admin/votes/1'), ('DELETE', '/api/admin/voters/1/votes'),
            ('POST', '/api/admin/deletion-batches/1/restore'), ('PUT', '/api/admin/settings/voting-open'),
            ('POST', '/api/admin/export/sheets'), ('POST', '/api/admin/logout'),
        ]
        for method, path in routes:
            kwargs = {} if method == 'GET' else {'json': {}}
            response = await self.client.request(method, path, **kwargs)
            self.assertEqual(response.status, 401, path)

    async def test_voter_session_is_not_admin(self):
        await self.start_voter()
        self.assertEqual((await self.client.get('/api/admin/players')).status, 401)

    async def test_lockout_after_five_failures(self):
        for _ in range(5):
            response = await self.client.post('/api/admin/login', json={'password': 'wrong'})
            self.assertEqual(response.status, 401)
        response = await self.client.post('/api/admin/login', json={'password': PASSWORD})
        self.assertEqual(response.status, 429)

    async def test_logout_ends_the_session_even_with_a_copied_cookie(self):
        await self.login_admin()
        cookie = next(c for c in self.client.session.cookie_jar if c.key == server.ADMIN_COOKIE).value
        await self.client.post('/api/admin/logout', json={})
        other = await self.new_client()
        response = await other.get('/api/admin/players', cookies={server.ADMIN_COOKIE: cookie})
        self.assertEqual(response.status, 401)

    async def test_forged_and_expired_tokens_fail(self):
        nonce = self.store.admin_nonce()
        self.assertFalse(security.admin_token_valid(SECRET, PASSWORD, nonce, '99999999999.forged'))
        expired = security.admin_token(SECRET, PASSWORD, nonce, 1000)
        self.assertFalse(security.admin_token_valid(SECRET, PASSWORD, nonce, expired, now=2000))
        fresh = security.admin_token(SECRET, PASSWORD, nonce, 3000)
        self.assertTrue(security.admin_token_valid(SECRET, PASSWORD, nonce, fresh, now=2000))

    async def test_no_password_means_no_admin(self):
        settings = Settings(session_secret=SECRET, ip_hash_salt='s', db_path=':memory:', cookie_secure=False)
        app = server.create_app(settings, store=Store(':memory:'))
        async with TestClient(TestServer(app)) as client:
            self.assertEqual((await client.post('/api/admin/login', json={'password': ''})).status, 503)
            self.assertEqual((await client.get('/api/admin/players')).status, 401)


class DeletionTests(SiteTestCase):
    async def setUpAsync(self):
        await super().setUpAsync()
        await self.start_voter()
        await self.login_admin()

    async def vote_ids(self):
        return {row['id']: row for row in self.store.all_votes()}

    async def delete_vote(self, vote_id, reason='troll'):
        response = await self.client.delete(f'/api/admin/votes/{vote_id}', json={'reason': reason})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())['batch_id']

    async def delete_voter(self, voter_id, reason='troll account'):
        response = await self.client.delete(f'/api/admin/voters/{voter_id}/votes', json={'reason': reason})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())['batch_id']

    async def restore(self, batch_id):
        return await self.client.post(f'/api/admin/deletion-batches/{batch_id}/restore', json={})

    async def test_reason_is_required(self):
        await self.vote_next()
        vote_id = self.store.all_votes()[0]['id']
        for reason in ('', '   ', None, 'x' * 501):
            response = await self.client.delete(f'/api/admin/votes/{vote_id}', json={'reason': reason})
            self.assertEqual(response.status, 400, reason)
        self.assertIsNone(self.store.all_votes()[0]['deleted_at'])

    async def test_deleted_vote_leaves_results_and_restores(self):
        card = await self.vote_next()
        vote_id = self.store.all_votes()[0]['id']
        batch_id = await self.delete_vote(vote_id, 'spam')
        self.assertEqual((await self.player_row(card['player']['id']))['votes'], 0)

        batches = (await (await self.client.get('/api/admin/deletion-batches')).json())['batches']
        self.assertEqual(batches[0]['id'], batch_id)
        self.assertEqual(batches[0]['reason'], 'spam')
        self.assertEqual(batches[0]['scope'], 'single')
        self.assertEqual(batches[0]['vote_count'], 1)

        response = await self.restore(batch_id)
        self.assertEqual((await response.json())['restored'], 1)
        self.assertEqual((await self.player_row(card['player']['id']))['votes'], 1)
        vote = self.store.all_votes()[0]
        self.assertIsNone(vote['deleted_at'])
        self.assertIsNone(vote['deleted_batch_id'])

    async def test_bulk_restore_does_not_revive_an_earlier_single_delete(self):
        first = await self.vote_next()
        second = await self.vote_next()
        third = await self.vote_next()
        votes = {row['player_id']: row['id'] for row in self.store.all_votes()}
        voter_id = self.store.voters()[0]['id']

        single_batch = await self.delete_vote(votes[first['player']['id']], 'earlier individual delete')
        bulk_batch = await self.delete_voter(voter_id)
        self.assertNotEqual(single_batch, bulk_batch)
        self.assertEqual(self.store.active_votes(), [])

        response = await self.restore(bulk_batch)
        self.assertEqual((await response.json())['restored'], 2)
        active = {row['player_id'] for row in self.store.active_votes()}
        self.assertEqual(active, {second['player']['id'], third['player']['id']})
        self.assertEqual((await self.player_row(first['player']['id']))['votes'], 0)

        # The earlier single delete can still be restored by itself.
        await self.restore(single_batch)
        self.assertEqual(len(self.store.active_votes()), 3)

    async def test_editing_an_excluded_answer_keeps_it_out_of_results(self):
        card = await self.vote_next(index=0)
        vote_id = self.store.all_votes()[0]['id']
        batch_id = await self.delete_vote(vote_id)

        response = await self.client.put(
            f"/api/votes/{card['player']['id']}",
            json={'answer_type': 'option', 'price': card['options'][4]},
        )
        self.assertEqual(response.status, 200)
        vote = self.store.all_votes()[0]
        self.assertEqual(vote['price'], card['options'][4])
        self.assertIsNotNone(vote['deleted_at'])
        self.assertEqual(vote['deleted_batch_id'], batch_id)
        self.assertEqual((await self.player_row(card['player']['id']))['votes'], 0)

        # A later restore brings back the edited answer.
        await self.restore(batch_id)
        row = await self.player_row(card['player']['id'])
        self.assertEqual(row['votes'], 1)
        self.assertEqual(row['median'], card['options'][4])

    async def test_a_deleted_vote_still_counts_as_answered(self):
        card = await self.vote_next()
        await self.delete_vote(self.store.all_votes()[0]['id'])
        next_card = await self.next_card()
        self.assertNotEqual(next_card['player']['id'], card['player']['id'])
        self.assertEqual(next_card['answered'], 1)

    async def test_double_delete_and_double_restore_are_refused(self):
        await self.vote_next()
        vote_id = self.store.all_votes()[0]['id']
        batch_id = await self.delete_vote(vote_id)
        response = await self.client.delete(f'/api/admin/votes/{vote_id}', json={'reason': 'again'})
        self.assertEqual(response.status, 409)
        self.assertEqual((await self.restore(batch_id)).status, 200)
        self.assertEqual((await self.restore(batch_id)).status, 409)

    async def test_bulk_delete_with_nothing_left_is_refused(self):
        voter_id = self.store.voters()[0]['id']
        response = await self.client.delete(f'/api/admin/voters/{voter_id}/votes', json={'reason': 'none'})
        self.assertEqual(response.status, 409)

    async def test_deleted_votes_are_left_out_of_exports(self):
        await self.vote_next()
        await self.vote_next()
        await self.delete_vote(self.store.all_votes()[0]['id'])
        response = await self.client.get('/api/admin/export.csv')
        rows = list(csv.reader(io.StringIO(await response.text())))
        self.assertEqual(len(rows), 2)

        response = await self.client.post('/api/admin/export/sheets', json={})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(self.exports[0]['Votes']), 2)


class ExportTests(SiteTestCase):
    async def test_csv_neutralizes_formula_usernames(self):
        await self.start_voter(username='=HYPERLINK("x")')
        await self.vote_next()
        await self.login_admin()
        response = await self.client.get('/api/admin/export.csv')
        rows = list(csv.reader(io.StringIO(await response.text())))
        self.assertEqual(rows[1][0], '\'=HYPERLINK("x")')
        self.assertIn('attachment', response.headers['Content-Disposition'])

    async def test_results_match_a_hand_calculation(self):
        voter_prices = [10, 20, 60]
        player = self.store.players()[0]
        for index, price in enumerate(voter_prices):
            voter = self.store.create_voter(f'voter {index}', [player['id']], f'ip{index}', lambda i: f'hash{i}')
            self.store.save_vote(voter['id'], player['id'], 'other', price)
        voter = self.store.create_voter('unsure', [player['id']], 'ip9', lambda i: f'hash{i}')
        self.store.save_vote(voter['id'], player['id'], 'dont_know', None)

        await self.login_admin()
        row = await self.player_row(player['id'])
        self.assertEqual(row['votes'], 3)
        self.assertEqual(row['dont_know'], 1)
        self.assertEqual(row['median'], 20)
        self.assertEqual(row['average'], 30)
        self.assertEqual(row['low'], 10)
        self.assertEqual(row['high'], 60)
        self.assertEqual(row['gap'], 20 - player['current_price'])

        detail = await (await self.client.get(f"/api/admin/players/{player['id']}")).json()
        counts = {bar['price']: bar['count'] for bar in detail['histogram']}
        self.assertEqual(counts[20], 1)
        self.assertIn(player['current_price'], counts)
        self.assertEqual(len(detail['votes']), 4)


class FlagTests(SiteTestCase):
    def add_voter(self, name, ip='ip'):
        return self.store.create_voter(name, [], ip, lambda i: f'hash{i}')

    def add_vote(self, voter, player, answer_type, price, stamp):
        self.store.db.execute(
            'INSERT INTO votes (voter_id, player_id, answer_type, price, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
            (voter['id'], player['id'], answer_type, price, stamp, stamp),
        )
        self.store.db.commit()

    async def flags_for(self, voter_id):
        await self.login_admin()
        data = await (await self.client.get('/api/admin/voters')).json()
        row = next(v for v in data['voters'] if v['id'] == voter_id)
        return {flag['id'] for flag in row['flags']}

    def spaced(self, index, seconds):
        total = int(index * seconds)
        return f'2026-11-05T{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}.000Z'

    async def test_same_answer_needs_twenty_identical_positions(self):
        voter = self.add_voter('same', 'a')
        for index, player in enumerate(self.store.players()[:20]):
            price = pricing.options_for(player['current_price'])[0]
            self.add_vote(voter, player, 'option', price, self.spaced(index, 30))
        self.assertIn('same_answer', await self.flags_for(voter['id']))

    async def test_nineteen_identical_answers_are_not_flagged(self):
        voter = self.add_voter('nineteen', 'b')
        for index, player in enumerate(self.store.players()[:19]):
            price = pricing.options_for(player['current_price'])[0]
            self.add_vote(voter, player, 'option', price, self.spaced(index, 30))
        self.assertNotIn('same_answer', await self.flags_for(voter['id']))

    async def test_varied_answers_are_not_flagged(self):
        voter = self.add_voter('varied', 'c')
        for index, player in enumerate(self.store.players()[:20]):
            price = pricing.options_for(player['current_price'])[index % 5]
            self.add_vote(voter, player, 'option', price, self.spaced(index, 30))
        self.assertEqual(await self.flags_for(voter['id']), set())

    async def test_too_fast_averages_under_one_second(self):
        voter = self.add_voter('fast', 'd')
        for index, player in enumerate(self.store.players()[:20]):
            price = pricing.options_for(player['current_price'])[index % 5]
            self.add_vote(voter, player, 'option', price, self.spaced(index, 0.5))
        self.assertIn('too_fast', await self.flags_for(voter['id']))

    async def test_extreme_prices_over_a_quarter_of_votes(self):
        voter = self.add_voter('extreme', 'e')
        players = self.store.players()[:4]
        self.add_vote(voter, players[0], 'other', 1, self.spaced(0, 30))
        self.add_vote(voter, players[1], 'other', 1, self.spaced(1, 30))
        self.add_vote(voter, players[2], 'option', players[2]['current_price'], self.spaced(2, 30))
        self.add_vote(voter, players[3], 'option', players[3]['current_price'], self.spaced(3, 30))
        self.assertIn('extreme_prices', await self.flags_for(voter['id']))

    async def test_exactly_a_quarter_extreme_is_not_flagged(self):
        voter = self.add_voter('quarter', 'h')
        players = self.store.players()[:4]
        self.add_vote(voter, players[0], 'other', 1, self.spaced(0, 30))
        for index, player in enumerate(players[1:], start=1):
            self.add_vote(voter, player, 'option', player['current_price'], self.spaced(index, 30))
        self.assertNotIn('extreme_prices', await self.flags_for(voter['id']))

    async def test_no_player_option_alone_counts_as_extreme(self):
        for player in self.store.players():
            for price in pricing.options_for(player['current_price']):
                self.assertLess(price, player['current_price'] * 3, player['name'])
                self.assertGreaterEqual(price * 3, player['current_price'], player['name'])

    async def test_repeat_username_ignores_capitals(self):
        first = self.add_voter('SameName', 'f')
        self.add_voter('samename', 'g')
        self.assertIn('repeat_username', await self.flags_for(first['id']))

    async def test_same_device_needs_different_usernames(self):
        first = self.add_voter('one', 'shared')
        self.add_voter('two', 'shared')
        self.assertIn('same_device', await self.flags_for(first['id']))


class SheetsTabTests(unittest.TestCase):
    def test_tabs_and_swap_requests(self):
        import sheets_export
        players = [dict(
            id=1, name="Shaquille O'Neal", current_price=38, votes=6, dont_know=1, median=40,
            average=41.5, low=30, high=50, gap=2,
        )]
        gaps = dict(overpriced=[], underpriced=players)
        votes = [dict(username='=EVIL()', player="Shaquille O'Neal", answer_type='dont_know', price=None, time='t')]
        voters = [dict(
            username='=EVIL()', answered=1, votes_in_results=1, dont_know=1, finished=False,
            flags=[dict(id='too_fast', label='Too fast')],
        )]
        tabs = sheets_export.build_tabs(players, gaps, votes, voters, '2026-11-05T00:00:00.000Z')
        self.assertEqual(list(tabs), sheets_export.TAB_NAMES)
        self.assertEqual(tabs['Votes'][1], ['=EVIL()', "Shaquille O'Neal", 'dont_know', '', 't'])
        self.assertEqual(tabs['Voters'][1][-1], 'Too fast')

        existing = [
            {'properties': {'sheetId': 5, 'title': 'Summary'}},
            {'properties': {'sheetId': 6, 'title': 'My own notes'}},
        ]
        requests = sheets_export.replace_tabs_requests(existing, tabs)
        kinds = [next(iter(r)) for r in requests]
        self.assertEqual(kinds[0], 'updateSheetProperties')
        self.assertEqual(kinds.count('addSheet'), 5)
        self.assertEqual(requests[-1], {'deleteSheet': {'sheetId': 5}})
        self.assertNotIn({'deleteSheet': {'sheetId': 6}}, requests)

        sheet_ids = {name: i for i, name in enumerate(sheets_export.TAB_NAMES)}
        formats = sheets_export.format_requests(sheet_ids, tabs)
        self.assertEqual(sum('addChart' in r for r in formats), 2)
        self.assertEqual(sum('setDataValidation' in r for r in formats), 1)

    def test_lookup_formula_handles_apostrophes(self):
        import sheets_export
        formula = sheets_export.lookup_formula()
        self.assertIn('"""&B1&"""', formula)


if __name__ == '__main__':
    unittest.main()
