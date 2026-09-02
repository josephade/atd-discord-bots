# db.py
# Sqlite persistence for ATD Draft Theme Bot.
#
# Picks (and everything else theme-specific) live only in this database —
# never in Google Sheets — until a GM's final roster is validated and
# written out by sheets.write_draft_results(). That's what keeps picks
# secret during the draft regardless of which theme is running.

import json
import os
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with _conn() as con:
        con.execute('''
            CREATE TABLE IF NOT EXISTS drafts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER,
                admin_channel_id INTEGER,
                theme            TEXT NOT NULL,
                name             TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'setup',
                settings_json    TEXT NOT NULL DEFAULT '{}',
                state_json       TEXT NOT NULL DEFAULT '{}',
                created_at       TEXT NOT NULL
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS drafters (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id          INTEGER NOT NULL REFERENCES drafts(id),
                user_id           INTEGER NOT NULL,
                display_name      TEXT,
                seat_index        INTEGER,
                division          INTEGER,
                skip_count        INTEGER NOT NULL DEFAULT 0,
                roster_submitted  INTEGER NOT NULL DEFAULT 0,
                state_json        TEXT NOT NULL DEFAULT '{}',
                UNIQUE(draft_id, user_id)
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS picks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id      INTEGER NOT NULL REFERENCES drafts(id),
                drafter_id    INTEGER NOT NULL REFERENCES drafters(id),
                player        TEXT NOT NULL,
                round_number  INTEGER,
                pick_number   INTEGER,
                auto_pick     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS relay_messages (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id         INTEGER NOT NULL REFERENCES drafts(id),
                from_drafter_id  INTEGER NOT NULL REFERENCES drafters(id),
                round_number     INTEGER,
                content          TEXT NOT NULL,
                created_at       TEXT NOT NULL
            )
        ''')


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if 'settings_json' in d:
        d['settings'] = json.loads(d.pop('settings_json') or '{}')
    if 'state_json' in d:
        d['state'] = json.loads(d.pop('state_json') or '{}')
    return d


# ── Drafts ────────────────────────────────────────────────────────────────────
def create_draft(guild_id: int, admin_channel_id: int | None, theme: str, name: str,
                  settings: dict) -> int:
    with _conn() as con:
        cur = con.execute(
            'INSERT INTO drafts (guild_id, admin_channel_id, theme, name, settings_json, state_json, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (guild_id, admin_channel_id, theme, name, json.dumps(settings), '{}', _now()),
        )
        return cur.lastrowid


def get_draft(draft_id: int) -> dict | None:
    with _conn() as con:
        return _row(con.execute('SELECT * FROM drafts WHERE id = ?', (draft_id,)).fetchone())


def get_draft_by_name(name: str) -> dict | None:
    with _conn() as con:
        return _row(con.execute(
            'SELECT * FROM drafts WHERE lower(name) = lower(?) ORDER BY id DESC LIMIT 1', (name,)
        ).fetchone())


def list_drafts(guild_id: int | None = None, status: str | None = None) -> list[dict]:
    q = 'SELECT * FROM drafts WHERE 1=1'
    params: list = []
    if guild_id is not None:
        q += ' AND guild_id = ?'
        params.append(guild_id)
    if status is not None:
        q += ' AND status = ?'
        params.append(status)
    q += ' ORDER BY id DESC'
    with _conn() as con:
        return [_row(r) for r in con.execute(q, params).fetchall()]


def set_draft_status(draft_id: int, status: str):
    with _conn() as con:
        con.execute('UPDATE drafts SET status = ? WHERE id = ?', (status, draft_id))


def set_draft_state(draft_id: int, state: dict):
    with _conn() as con:
        con.execute('UPDATE drafts SET state_json = ? WHERE id = ?', (json.dumps(state), draft_id))


def update_draft_settings(draft_id: int, patch: dict):
    draft = get_draft(draft_id)
    settings = {**draft['settings'], **patch}
    with _conn() as con:
        con.execute('UPDATE drafts SET settings_json = ? WHERE id = ?', (json.dumps(settings), draft_id))


# ── Drafters ──────────────────────────────────────────────────────────────────
def add_drafter(draft_id: int, user_id: int, display_name: str, seat_index: int | None = None,
                 division: int | None = None) -> int:
    with _conn() as con:
        cur = con.execute(
            'INSERT INTO drafters (draft_id, user_id, display_name, seat_index, division, state_json) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (draft_id, user_id, display_name, seat_index, division, '{}'),
        )
        return cur.lastrowid


def get_drafter(drafter_id: int) -> dict | None:
    with _conn() as con:
        return _row(con.execute('SELECT * FROM drafters WHERE id = ?', (drafter_id,)).fetchone())


def get_drafter_by_user(draft_id: int, user_id: int) -> dict | None:
    with _conn() as con:
        return _row(con.execute(
            'SELECT * FROM drafters WHERE draft_id = ? AND user_id = ?', (draft_id, user_id)
        ).fetchone())


def get_active_drafters_for_user(user_id: int) -> list[dict]:
    """All drafter rows for this user whose draft is currently setup/active/paused —
    used to route a bare DM to the right draft. Normally exactly one."""
    with _conn() as con:
        rows = con.execute('''
            SELECT drafters.* FROM drafters
            JOIN drafts ON drafts.id = drafters.draft_id
            WHERE drafters.user_id = ? AND drafts.status IN ('setup', 'active', 'paused')
        ''', (user_id,)).fetchall()
        return [_row(r) for r in rows]


def list_drafters(draft_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            'SELECT * FROM drafters WHERE draft_id = ? ORDER BY seat_index IS NULL, seat_index, id', (draft_id,)
        ).fetchall()
        return [_row(r) for r in rows]


def set_drafter_state(drafter_id: int, state: dict):
    with _conn() as con:
        con.execute('UPDATE drafters SET state_json = ? WHERE id = ?', (json.dumps(state), drafter_id))


def set_drafter_seat(drafter_id: int, seat_index: int):
    with _conn() as con:
        con.execute('UPDATE drafters SET seat_index = ? WHERE id = ?', (seat_index, drafter_id))


def set_drafter_division(drafter_id: int, division: int):
    with _conn() as con:
        con.execute('UPDATE drafters SET division = ? WHERE id = ?', (division, drafter_id))


def list_drafters_in_division(draft_id: int, division: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            'SELECT * FROM drafters WHERE draft_id = ? AND division = ? ORDER BY seat_index IS NULL, seat_index, id',
            (draft_id, division),
        ).fetchall()
        return [_row(r) for r in rows]


def update_drafter_state(drafter_id: int, patch: dict):
    drafter = get_drafter(drafter_id)
    state = {**drafter['state'], **patch}
    set_drafter_state(drafter_id, state)


def increment_skip(drafter_id: int) -> int:
    with _conn() as con:
        con.execute('UPDATE drafters SET skip_count = skip_count + 1 WHERE id = ?', (drafter_id,))
        return con.execute('SELECT skip_count FROM drafters WHERE id = ?', (drafter_id,)).fetchone()[0]


def set_roster_submitted(drafter_id: int, submitted: bool = True):
    with _conn() as con:
        con.execute('UPDATE drafters SET roster_submitted = ? WHERE id = ?', (int(submitted), drafter_id))


# ── Picks ─────────────────────────────────────────────────────────────────────
def record_pick(draft_id: int, drafter_id: int, player: str, round_number: int | None = None,
                 pick_number: int | None = None, auto_pick: bool = False) -> int:
    with _conn() as con:
        cur = con.execute(
            'INSERT INTO picks (draft_id, drafter_id, player, round_number, pick_number, auto_pick, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (draft_id, drafter_id, player, round_number, pick_number, int(auto_pick), _now()),
        )
        return cur.lastrowid


def get_picks(draft_id: int, drafter_id: int) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            'SELECT player FROM picks WHERE draft_id = ? AND drafter_id = ? ORDER BY id', (draft_id, drafter_id)
        ).fetchall()
        return [r[0] for r in rows]


def get_all_picks(draft_id: int) -> dict[int, list[str]]:
    with _conn() as con:
        rows = con.execute(
            'SELECT drafter_id, player FROM picks WHERE draft_id = ? ORDER BY id', (draft_id,)
        ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r['drafter_id'], []).append(r['player'])
    return out


def get_taken_players(draft_id: int) -> set[str]:
    with _conn() as con:
        rows = con.execute('SELECT player FROM picks WHERE draft_id = ?', (draft_id,)).fetchall()
        return {r[0].lower() for r in rows}


# ── Relay messages (e.g. Anonymous Partner Draft's partner chat) ───────────────
def add_relay_message(draft_id: int, from_drafter_id: int, round_number: int, content: str) -> int:
    with _conn() as con:
        cur = con.execute(
            'INSERT INTO relay_messages (draft_id, from_drafter_id, round_number, content, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (draft_id, from_drafter_id, round_number, content, _now()),
        )
        return cur.lastrowid


def relay_message_stats(draft_id: int, from_drafter_id: int) -> tuple[int, int]:
    """Returns (message_count, total_chars) sent by this drafter across the whole draft."""
    with _conn() as con:
        rows = con.execute(
            'SELECT content FROM relay_messages WHERE draft_id = ? AND from_drafter_id = ?',
            (draft_id, from_drafter_id),
        ).fetchall()
        return len(rows), sum(len(r[0]) for r in rows)
