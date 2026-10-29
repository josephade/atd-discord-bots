import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from votes import parse_rows, resolve_member
from storage import Store
from render import render_tierlist
from sheets import sheet_reference


def rows(name='Soapz', timestamp='10/12/2026 12:07:13'):
    return [['Timestamp', 'Enter Discord Name', '[Lakers]', 'Rate teams [Bulls]', '', 'Tier 1', 'Tier 1'],
            [timestamp, name, 'Tier 1', 'Tier 12', '', '100', '999']]


def member(uid, name, nick=None, bot=False):
    return SimpleNamespace(id=uid, name=name, display_name=nick or name, global_name=None, bot=bot)


class BallotTests(unittest.TestCase):
    def test_exact_tiers_and_ignore_derived_tables(self):
        data = rows()+[['Mean', '', '4.6', '2'], rows()[0], ['Soapz', '', '1', '30']]
        ballot, = parse_rows(data)
        self.assertEqual(ballot.teams, [('Lakers', 1), ('Bulls', 12)])
        self.assertFalse(ballot.error)
        self.assertEqual(ballot.key, parse_rows(rows())[0].key)

    def test_timestamped_formula_copy_after_repeated_header_is_excluded(self):
        original = rows()
        copied = rows()
        copied[1][2:4] = ['1', '30']
        data = original + [[], original[0]] + [copied[1]]
        ballots = parse_rows(data)
        self.assertEqual(len(ballots), 1)
        self.assertEqual(ballots[0].teams, [('Lakers', 1), ('Bulls', 12)])

    def test_bad_or_missing_ballot_is_not_deliverable(self):
        data = rows('')
        data[1][2] = ''
        ballot, = parse_rows(data)
        self.assertIn('Lakers', ballot.error)
        self.assertIn('Discord name', ballot.error)

    def test_missing_identity_fails_live_but_allows_offline_preview(self):
        data = [['Tidsmerke', '[Team]'], ['29.07.2026 kl. 03.04.26', 'Tier 8']]
        with self.assertRaises(ValueError):
            parse_rows(data)
        self.assertEqual(parse_rows(data, require_name=False)[0].teams, [('Team', 8)])

    def test_edited_votes_new_key_derived_edits_same_key(self):
        data = rows()
        key = parse_rows(data)[0].key
        data[1][-1] = '12'
        self.assertEqual(key, parse_rows(data)[0].key)
        data[1][2] = 'Tier 2'
        self.assertNotEqual(key, parse_rows(data)[0].key)

    def test_30_plus_teams(self):
        data = [['Timestamp', 'Enter Discord Name']+[f'[Team {i}]' for i in range(35)],
                ['10/12/2026 12:07:13', 'Soapz']+['Tier 2']*35]
        ballot, = parse_rows(data)
        self.assertEqual(len(ballot.teams), 35)
        image = Image.open(io.BytesIO(render_tierlist('112', ballot, 10)))
        self.assertEqual(image.format, 'PNG')
        self.assertGreater(image.height, 750)

    def test_sheet_gid_fragment_and_query(self):
        self.assertEqual(sheet_reference('https://docs.google.com/spreadsheets/d/abc/edit#gid=123'), ('abc', 123))
        self.assertEqual(sheet_reference('https://docs.google.com/spreadsheets/d/abc/edit?gid=456'), ('abc', 456))
        self.assertEqual(sheet_reference('https://docs.google.com/spreadsheets/d/abc/edit'), ('abc', None))
        with self.assertRaises(ValueError):
            sheet_reference('https://docs.google.com/spreadsheets/d/abc/edit?gid=wrong')


class MatchingTests(unittest.TestCase):
    def test_exact_name_alias_and_collision(self):
        a, b = member(1, 'soapz'), member(2, 'other', 'Soapz')
        self.assertIs(resolve_member(' SOAPZ ', [a], {}), a)
        self.assertIsNone(resolve_member('Soap', [a], {}))
        self.assertIsNone(resolve_member('Soapz', [a, b], {}))
        self.assertIs(resolve_member('Soapz', [a, b], {'soapz': 1}), a)
        self.assertIsNone(resolve_member('Soapz', [a], {'soapz': 999}))
        self.assertIsNone(resolve_member('soapz', [member(3, 'soapz', bot=True)], {}))


class StorageTests(unittest.TestCase):
    def test_history_import_is_inactive_and_not_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'voting.db')
            store.register('111', 'sheet', 10, 10, parse_rows(rows()), active=False)
            self.assertEqual(store.tracks(), [])
            self.assertEqual(store.ready('111'), [])
            self.assertEqual(len(store.receipt_candidates('111')), 1)
            self.assertEqual(store.receipt_candidates('111')[0]['status'], 'historical')
            store.db.close()

    def test_baseline_new_delivery_restart_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'voting.db'
            store = Store(path)
            old = parse_rows(rows())
            store.register('112', 'sheet', 123, 12, old)
            store.ingest('112', old)
            self.assertEqual(store.ready('112'), [])
            new = parse_rows(rows(timestamp='10/12/2026 13:07:13'))
            store.ingest('112', new)
            store.ingest('112', new)
            self.assertEqual(len(store.ready('112')), 1)
            store.set_status('112', new[0].key, 'sent', '123', message_id=999)
            store.db.close()
            store = Store(path)
            store.ingest('112', new)
            store.retry('112')
            self.assertEqual(store.ready('112'), [])
            self.assertEqual(store.latest('112', 123)['message_id'], 999)
            store.set_status('112', new[0].key, 'blocked')
            store.retry('112')
            self.assertEqual(len(store.ready('112')), 1)
            store.set_status('112', new[0].key, 'uncertain')
            store.retry('112')
            self.assertEqual(store.ready('112'), [])
            store.alias('soapz', 123)
            self.assertEqual(store.aliases(), {'soapz': 123})
            store.db.close()


if __name__ == '__main__':
    unittest.main()
