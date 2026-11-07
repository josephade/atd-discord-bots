"""SQLite storage for players, voters, votes and admin deletions."""
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from pricing import band_for

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    sheet_row INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    position TEXT NOT NULL,
    adp REAL NOT NULL,
    current_price INTEGER NOT NULL,
    band INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS voters (
    id INTEGER PRIMARY KEY,
    discord_username TEXT NOT NULL,
    session_token_hash TEXT UNIQUE,
    player_order TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deletion_batches (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('single', 'voter')),
    voter_id INTEGER NOT NULL REFERENCES voters(id),
    reason TEXT NOT NULL,
    vote_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    restored_at TEXT
);
CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY,
    voter_id INTEGER NOT NULL REFERENCES voters(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    answer_type TEXT NOT NULL CHECK (answer_type IN ('option', 'other', 'dont_know')),
    price INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    deleted_batch_id INTEGER REFERENCES deletion_batches(id),
    UNIQUE (voter_id, player_id)
);
CREATE INDEX IF NOT EXISTS votes_player ON votes(player_id);
CREATE INDEX IF NOT EXISTS votes_batch ON votes(deleted_batch_id);
CREATE TABLE IF NOT EXISTS admin_actions (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class Store:
    def __init__(self, path, clock=utc_now):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._clock = clock
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('PRAGMA journal_mode = WAL')
        self.db.executescript(SCHEMA)
        # Additive migration: preserve existing votes and deletion history.
        for table, column, definition in (
            ('voters', 'removed_batch_id', 'INTEGER REFERENCES deletion_batches(id)'),
            ('deletion_batches', 'removes_voter', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if column not in {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        self.db.commit()

    def close(self):
        self.db.close()

    # Players

    def seed_players(self, players):
        """Fill the players table once. A later import can't change a vote in progress."""
        if self.db.execute('SELECT 1 FROM players LIMIT 1').fetchone():
            return False
        with self.db:
            self.db.executemany(
                'INSERT INTO players (sheet_row, name, position, adp, current_price, band) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [(p['sheet_row'], p['name'], p['position'], p['adp'], p['current_price'],
                  band_for(p['current_price'])) for p in players],
            )
        return True

    def players(self):
        return self.db.execute('SELECT * FROM players ORDER BY sheet_row').fetchall()

    def player(self, player_id):
        return self.db.execute('SELECT * FROM players WHERE id = ?', (player_id,)).fetchone()

    def player_count(self):
        return self.db.execute('SELECT COUNT(*) FROM players').fetchone()[0]

    # Settings

    def get_setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute(
                'INSERT INTO settings (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value),
            )

    def voting_open(self):
        return self.get_setting('voting_open', '0') == '1'

    def admin_nonce(self):
        nonce = self.get_setting('admin_session_nonce')
        if nonce is None:
            nonce = self.rotate_admin_nonce()
        return nonce

    def rotate_admin_nonce(self):
        nonce = secrets.token_hex(16)
        self.set_setting('admin_session_nonce', nonce)
        return nonce

    # Voters

    def create_voter(self, username, order, ip_hash, session_hash_for):
        now = self._clock()
        with self.db:
            cursor = self.db.execute(
                'INSERT INTO voters (discord_username, player_order, ip_hash, created_at, last_seen_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (username, json.dumps(order), ip_hash, now, now),
            )
            voter_id = cursor.lastrowid
            self.db.execute(
                'UPDATE voters SET session_token_hash = ? WHERE id = ?',
                (session_hash_for(voter_id), voter_id),
            )
        return self.voter(voter_id)

    def voter(self, voter_id):
        return self.db.execute('SELECT * FROM voters WHERE id = ? AND removed_batch_id IS NULL', (voter_id,)).fetchone()

    def voter_by_session_hash(self, session_hash):
        return self.db.execute(
            'SELECT * FROM voters WHERE session_token_hash = ? AND removed_batch_id IS NULL', (session_hash,)
        ).fetchone()

    def voters(self):
        return self.db.execute('SELECT * FROM voters WHERE removed_batch_id IS NULL ORDER BY created_at, id').fetchall()

    # Votes

    def answers_for(self, voter_id):
        rows = self.db.execute('SELECT * FROM votes WHERE voter_id = ?', (voter_id,)).fetchall()
        return {row['player_id']: row for row in rows}

    def answered_count(self, voter_id):
        return self.db.execute(
            'SELECT COUNT(*) FROM votes WHERE voter_id = ?', (voter_id,)
        ).fetchone()[0]

    def save_vote(self, voter_id, player_id, answer_type, price):
        """One answer per voter and player. An edit never clears an admin exclusion."""
        now = self._clock()
        with self.db:
            self.db.execute(
                'INSERT INTO votes (voter_id, player_id, answer_type, price, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(voter_id, player_id) DO UPDATE SET '
                'answer_type = excluded.answer_type, price = excluded.price, '
                'updated_at = excluded.updated_at '
                'WHERE votes.answer_type IS NOT excluded.answer_type '
                'OR votes.price IS NOT excluded.price',
                (voter_id, player_id, answer_type, price, now, now),
            )
            self.db.execute('UPDATE voters SET last_seen_at = ? WHERE id = ?', (now, voter_id))

    def all_votes(self):
        return self.db.execute('SELECT * FROM votes ORDER BY created_at, id').fetchall()

    def active_votes(self):
        return self.db.execute(
            'SELECT votes.* FROM votes JOIN voters ON voters.id=votes.voter_id '
            'WHERE votes.deleted_at IS NULL AND voters.removed_batch_id IS NULL ORDER BY votes.created_at, votes.id'
        ).fetchall()

    def votes_for_player(self, player_id):
        return self.db.execute(
            'SELECT votes.*, voters.discord_username FROM votes '
            'JOIN voters ON voters.id = votes.voter_id '
            'WHERE votes.player_id = ? AND votes.deleted_at IS NULL AND voters.removed_batch_id IS NULL '
            'ORDER BY votes.price DESC, voters.discord_username',
            (player_id,),
        ).fetchall()

    def votes_for_voter(self, voter_id):
        return self.db.execute(
            'SELECT votes.*, players.name AS player_name, players.current_price, '
            'players.sheet_row FROM votes '
            'JOIN players ON players.id = votes.player_id '
            'WHERE votes.voter_id = ? ORDER BY votes.created_at, votes.id',
            (voter_id,),
        ).fetchall()

    # Deletions

    def _create_batch(self, scope, voter_id, reason, vote_ids, now):
        cursor = self.db.execute(
            'INSERT INTO deletion_batches (scope, voter_id, reason, vote_count, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (scope, voter_id, reason, len(vote_ids), now),
        )
        batch_id = cursor.lastrowid
        self.db.executemany(
            'UPDATE votes SET deleted_at = ?, deleted_batch_id = ? WHERE id = ?',
            [(now, batch_id, vote_id) for vote_id in vote_ids],
        )
        self.db.execute(
            'INSERT INTO admin_actions (action, target, created_at) VALUES (?, ?, ?)',
            (f'delete_{scope}', f'batch {batch_id}: {len(vote_ids)} votes, voter {voter_id}', now),
        )
        return batch_id

    def delete_vote(self, vote_id, reason):
        """Delete one active vote. Returns its batch ID, or None if there's nothing to delete."""
        row = self.db.execute(
            'SELECT voter_id FROM votes WHERE id = ? AND deleted_at IS NULL', (vote_id,)
        ).fetchone()
        if row is None:
            return None
        with self.db:
            return self._create_batch('single', row['voter_id'], reason, [vote_id], self._clock())

    def delete_voter_votes(self, voter_id, reason):
        """Batch only the votes still in results, so a restore can't revive earlier deletes."""
        rows = self.db.execute(
            'SELECT id FROM votes WHERE voter_id = ? AND deleted_at IS NULL', (voter_id,)
        ).fetchall()
        if not rows:
            return None
        with self.db:
            return self._create_batch('voter', voter_id, reason, [r['id'] for r in rows], self._clock())

    def delete_voter(self, voter_id, reason):
        """Remove a session and its included answers as one restorable batch."""
        with self.db:
            if self.voter(voter_id) is None:
                return None
            rows = self.db.execute('SELECT id FROM votes WHERE voter_id=? AND deleted_at IS NULL', (voter_id,)).fetchall()
            batch_id = self._create_batch('voter', voter_id, reason, [r['id'] for r in rows], self._clock())
            self.db.execute('UPDATE deletion_batches SET removes_voter=1 WHERE id=?', (batch_id,))
            self.db.execute('UPDATE voters SET removed_batch_id=? WHERE id=?', (batch_id, voter_id))
            return batch_id

    def restore_batch(self, batch_id):
        """Restore one batch. Returns the restored count, or None if it can't be restored."""
        batch = self.db.execute(
            'SELECT * FROM deletion_batches WHERE id = ? AND restored_at IS NULL', (batch_id,)
        ).fetchone()
        if batch is None:
            return None
        removed = self.db.execute('SELECT removed_batch_id FROM voters WHERE id=?', (batch['voter_id'],)).fetchone()
        if removed and removed['removed_batch_id'] not in (None, batch_id):
            raise ValueError('Restore the voter session first, then restore earlier answer batches.')
        now = self._clock()
        with self.db:
            if batch['removes_voter']:
                self.db.execute('UPDATE voters SET removed_batch_id=NULL WHERE id=? AND removed_batch_id=?', (batch['voter_id'], batch_id))
            restored = self.db.execute(
                'UPDATE votes SET deleted_at = NULL, deleted_batch_id = NULL WHERE deleted_batch_id = ?',
                (batch_id,),
            ).rowcount
            self.db.execute(
                'UPDATE deletion_batches SET restored_at = ? WHERE id = ?', (now, batch_id)
            )
            self.db.execute(
                'INSERT INTO admin_actions (action, target, created_at) VALUES (?, ?, ?)',
                ('restore_batch', f'batch {batch_id}: {restored} votes', now),
            )
        return restored

    def deletion_batches(self):
        return self.db.execute(
            'SELECT deletion_batches.*, voters.discord_username FROM deletion_batches '
            'JOIN voters ON voters.id = deletion_batches.voter_id '
            'ORDER BY deletion_batches.id DESC'
        ).fetchall()

    def log_action(self, action, target):
        with self.db:
            self.db.execute(
                'INSERT INTO admin_actions (action, target, created_at) VALUES (?, ?, ?)',
                (action, target, self._clock()),
            )

    def clear_voting_data(self):
        """Remove test voters and votes. Players and settings stay."""
        with self.db:
            self.db.execute('UPDATE voters SET removed_batch_id=NULL')
            counts = {}
            for table in ('votes', 'deletion_batches', 'voters', 'admin_actions'):
                counts[table] = self.db.execute(f'DELETE FROM {table}').rowcount
        return counts
