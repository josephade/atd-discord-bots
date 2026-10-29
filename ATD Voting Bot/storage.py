import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tracks (
                draft TEXT PRIMARY KEY, sheet TEXT NOT NULL, gid INTEGER NOT NULL,
                tiers INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS aliases (name TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                draft TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                next_try REAL NOT NULL DEFAULT 0, message_id INTEGER,
                PRIMARY KEY (draft, key));
        ''')

    def tracks(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM tracks WHERE active=1')]

    def register(self, draft, sheet, gid, tiers, ballots, active=True):
        with self.db:
            self.db.execute('INSERT INTO tracks VALUES (?,?,?,?,?)', (draft, sheet, gid, tiers, int(active)))
            for ballot in ballots:
                self._insert(draft, ballot, 'historical')

    def _insert(self, draft, ballot, status):
        from dataclasses import asdict
        self.db.execute('INSERT OR IGNORE INTO deliveries(draft,key,payload,status) VALUES(?,?,?,?)',
                        (draft, ballot.key, json.dumps(asdict(ballot)), status))

    def ingest(self, draft, ballots):
        with self.db:
            for ballot in ballots:
                self._insert(draft, ballot, 'invalid' if ballot.error else 'pending')

    def ready(self, draft):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM deliveries WHERE draft=? AND status IN ('pending','retry','unmatched') AND next_try<=?",
            (draft, time.time()))]

    def set_status(self, draft, key, status, detail='', delay=0, message_id=None):
        with self.db:
            self.db.execute('UPDATE deliveries SET status=?,detail=?,next_try=?,message_id=? WHERE draft=? AND key=?',
                            (status, detail, time.time()+delay, message_id, draft, key))

    def aliases(self):
        return dict(self.db.execute('SELECT name,user_id FROM aliases').fetchall())

    def alias(self, name, uid):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)', (name, uid))
            self.db.execute("UPDATE deliveries SET next_try=0 WHERE status='unmatched'")

    def counts(self):
        return list(self.db.execute('SELECT draft,status,COUNT(*) AS n FROM deliveries GROUP BY draft,status'))

    def retry(self, draft):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='pending',next_try=0 WHERE draft=? AND status IN ('blocked','retry','unmatched')", (draft,))

    def latest(self, draft, uid):
        return self.db.execute("SELECT * FROM deliveries WHERE draft=? AND status='sent' AND detail=? ORDER BY rowid DESC LIMIT 1", (draft, str(uid))).fetchone()

    def receipt_candidates(self, draft):
        return self.db.execute("SELECT * FROM deliveries WHERE draft=? ORDER BY rowid DESC", (draft,)).fetchall()

    def tier_count(self, draft):
        row = self.db.execute('SELECT tiers FROM tracks WHERE draft=?', (draft,)).fetchone()
        return row['tiers'] if row else 10
