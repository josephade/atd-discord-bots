"""
feedback/db.py
SQLite persistence for draft history, team reviews, weight proposals, and weight history.
"""

import json
import os
import sqlite3
from datetime import datetime

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "draft_feedback.db")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    """Create all tables if they don't exist. Call once at bot startup."""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS drafts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                num_teams   INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending_review'
                -- status: pending_review | reviewing | reviewed
            );

            CREATE TABLE IF NOT EXISTS team_drafts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id    INTEGER NOT NULL REFERENCES drafts(id),
                team_name   TEXT    NOT NULL,
                picks       TEXT    NOT NULL,   -- JSON array of player names
                verdict     TEXT,               -- null | approved | rejected
                reviewed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                team_draft_id   INTEGER NOT NULL REFERENCES team_drafts(id),
                reasons         TEXT    NOT NULL,   -- JSON array of reason keys
                reviewed_by     TEXT    NOT NULL,
                timestamp       TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weight_proposals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id    INTEGER NOT NULL REFERENCES drafts(id),
                proposals   TEXT    NOT NULL,   -- JSON list of {key, old, new, reason}
                status      TEXT    NOT NULL DEFAULT 'pending',
                -- status: pending | confirmed | cancelled
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weight_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                weight_key  TEXT    NOT NULL,
                old_value   REAL    NOT NULL,
                new_value   REAL    NOT NULL,
                draft_id    INTEGER,
                note        TEXT
            );
        """)
        # Migrations — safe to run on existing databases
        try:
            con.execute("ALTER TABLE drafts ADD COLUMN started_by TEXT")
        except Exception:
            pass  # column already exists


# ── Draft ────────────────────────────────────────────────────────────────────

def save_draft(num_teams: int, teams: dict[str, list[str]],
               started_by: str | None = None) -> int:
    """
    Persist a completed draft.
    teams: {team_name: [pick1, pick2, ...]}
    started_by: Discord username or mention of whoever ran !draft (None = all-AI watch).
    Returns the new draft_id.
    """
    ts = datetime.utcnow().isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO drafts (timestamp, num_teams, status, started_by) VALUES (?, ?, 'pending_review', ?)",
            (ts, num_teams, started_by),
        )
        draft_id = cur.lastrowid
        for team_name, picks in teams.items():
            con.execute(
                "INSERT INTO team_drafts (draft_id, team_name, picks) VALUES (?, ?, ?)",
                (draft_id, team_name, json.dumps(picks)),
            )
    return draft_id


def get_latest_draft_id() -> int | None:
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM drafts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def get_draft_teams(draft_id: int) -> list[dict]:
    """Return list of {id, team_name, picks, verdict} for a draft."""
    with _conn() as con:
        rows = con.execute(
            "SELECT id, team_name, picks, verdict FROM team_drafts WHERE draft_id = ? ORDER BY id",
            (draft_id,),
        ).fetchall()
    return [
        {"id": r["id"], "team_name": r["team_name"],
         "picks": json.loads(r["picks"]), "verdict": r["verdict"]}
        for r in rows
    ]


def get_draft_status(draft_id: int) -> str | None:
    with _conn() as con:
        row = con.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return row["status"] if row else None


def set_draft_status(draft_id: int, status: str) -> None:
    with _conn() as con:
        con.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))


def get_draft_history(limit: int = 10) -> list[dict]:
    """Return the most recent drafts, newest first."""
    with _conn() as con:
        rows = con.execute(
            "SELECT id, timestamp, num_teams, status, started_by "
            "FROM drafts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Reviews ──────────────────────────────────────────────────────────────────

def record_verdict(
    team_draft_id: int,
    verdict: str,           # "approved" | "rejected"
    reasons: list[str],     # list of reason keys (empty if approved)
    reviewed_by: str,
) -> None:
    ts = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            "UPDATE team_drafts SET verdict = ?, reviewed_at = ? WHERE id = ?",
            (verdict, ts, team_draft_id),
        )
        if reasons:
            con.execute(
                "INSERT INTO reviews (team_draft_id, reasons, reviewed_by, timestamp) VALUES (?, ?, ?, ?)",
                (team_draft_id, json.dumps(reasons), reviewed_by, ts),
            )


def get_unreviewed_team(draft_id: int) -> dict | None:
    """Return the next team in this draft that hasn't been reviewed yet."""
    with _conn() as con:
        row = con.execute(
            "SELECT id, team_name, picks FROM team_drafts WHERE draft_id = ? AND verdict IS NULL ORDER BY id LIMIT 1",
            (draft_id,),
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "team_name": row["team_name"], "picks": json.loads(row["picks"])}


def get_review_summary(draft_id: int) -> dict:
    """
    Returns aggregated review data for a draft:
    {
        total: int,
        approved: int,
        rejected: int,
        reason_counts: {reason_key: count}
    }
    """
    with _conn() as con:
        teams = con.execute(
            "SELECT id, verdict FROM team_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchall()

        approved = sum(1 for t in teams if t["verdict"] == "approved")
        rejected = sum(1 for t in teams if t["verdict"] == "rejected")

        rejected_ids = [t["id"] for t in teams if t["verdict"] == "rejected"]
        reason_counts: dict[str, int] = {}
        if rejected_ids:
            placeholders = ",".join("?" * len(rejected_ids))
            review_rows = con.execute(
                f"SELECT reasons FROM reviews WHERE team_draft_id IN ({placeholders})",
                rejected_ids,
            ).fetchall()
            for row in review_rows:
                for reason in json.loads(row["reasons"]):
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        "total": len(teams),
        "approved": approved,
        "rejected": rejected,
        "reason_counts": reason_counts,
    }


# ── Weight proposals ─────────────────────────────────────────────────────────

def save_proposal(draft_id: int, proposals: list[dict]) -> int:
    """
    proposals: list of {key, old_value, new_value, reason, pct_change}
    Returns proposal_id.
    """
    ts = datetime.utcnow().isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO weight_proposals (draft_id, proposals, status, created_at) VALUES (?, ?, 'pending', ?)",
            (draft_id, json.dumps(proposals), ts),
        )
    return cur.lastrowid


def get_pending_proposal() -> dict | None:
    """Return the most recent pending proposal, or None."""
    with _conn() as con:
        row = con.execute(
            "SELECT id, draft_id, proposals FROM weight_proposals WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "draft_id": row["draft_id"], "proposals": json.loads(row["proposals"])}


def confirm_proposal(proposal_id: int, applied_keys: list[str]) -> None:
    """Mark proposal confirmed; skipped items are noted via applied_keys."""
    with _conn() as con:
        con.execute(
            "UPDATE weight_proposals SET status = 'confirmed' WHERE id = ?",
            (proposal_id,),
        )


def cancel_proposal(proposal_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE weight_proposals SET status = 'cancelled' WHERE id = ?",
            (proposal_id,),
        )


# ── Weight history ────────────────────────────────────────────────────────────

def log_weight_change(
    weight_key: str,
    old_value: float,
    new_value: float,
    draft_id: int | None = None,
    note: str = "",
) -> None:
    ts = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            "INSERT INTO weight_history (timestamp, weight_key, old_value, new_value, draft_id, note) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, weight_key, old_value, new_value, draft_id, note),
        )


def get_weight_history(limit: int = 20) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT timestamp, weight_key, old_value, new_value, note FROM weight_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
