"""SQLite storage. The extractor and the bot run as separate processes; WAL mode lets them share the file."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import questions, settings

# Sheet status lifecycle:
#   pending -> auto_ok | needs_review | failed | other_format
#   (other_format: real feedback, but not answers numbered 1-5; full text kept in unmapped_text)
#   needs_review -> reviewed
SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    spot_check    INTEGER NOT NULL DEFAULT 0,
    rotation      INTEGER NOT NULL DEFAULT 0,  -- degrees clockwise to make the text upright
    legibility    REAL,
    sheet_flags   TEXT NOT NULL DEFAULT '[]',
    unmapped_text TEXT,
    raw_primary   TEXT,
    raw_verify    TEXT,
    claimed_by    INTEGER,
    claimed_at    TEXT,
    reviewed_by   INTEGER,
    reviewed_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sheets_status ON sheets(status);

CREATE TABLE IF NOT EXISTS answers (
    sheet_id      INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    question_id   TEXT NOT NULL,
    ai_text       TEXT NOT NULL DEFAULT '',
    ai_blank      INTEGER NOT NULL DEFAULT 0,
    ai_confidence REAL,
    ai_makes_sense INTEGER,
    verify_text   TEXT,
    agreement     REAL,
    flags         TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL,          -- auto_ok | needs_review | reviewed
    final_text    TEXT,
    final_script  TEXT,                   -- geez | latin | mixed | none
    reviewer_raw  TEXT,                   -- what the reviewer typed, before transliteration
    reviewed_by   INTEGER,
    reviewed_at   TEXT,
    PRIMARY KEY (sheet_id, question_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sheets)")}
    if "rotation" not in cols:
        conn.execute("ALTER TABLE sheets ADD COLUMN rotation INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE sheets SET status='other_format' WHERE status='not_a_sheet'")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def add_sheet(conn: sqlite3.Connection, path: str, sha256: str) -> bool:
    ts = now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO sheets (path, sha256, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (path, sha256, ts, ts),
    )
    return cur.rowcount == 1


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) n FROM sheets GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------- review queue ----------

def claim_next(conn: sqlite3.Connection, user_id: int, exclude: set[int] = frozenset()) -> sqlite3.Row | None:
    """Return the sheet this reviewer already holds, or claim the next free one.

    Claims expire after CLAIM_MINUTES so an abandoned sheet goes back into the queue.
    """
    stale = (datetime.now(timezone.utc) - timedelta(minutes=settings.claim_minutes)).isoformat(timespec="seconds")
    with tx(conn):
        row = conn.execute(
            "SELECT * FROM sheets WHERE status='needs_review' AND claimed_by=? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        if row and row["id"] not in exclude:
            conn.execute("UPDATE sheets SET claimed_at=? WHERE id=?", (now(), row["id"]))
            return row
        exclude = tuple(exclude) or (-1,)  # "NOT IN (NULL)" would match nothing
        placeholders = ",".join("?" * len(exclude))
        row = conn.execute(
            f"""SELECT * FROM sheets
                WHERE status='needs_review'
                  AND (claimed_by IS NULL OR claimed_by=? OR claimed_at < ?)
                  AND id NOT IN ({placeholders})
                ORDER BY spot_check, id LIMIT 1""",
            (user_id, stale, *exclude),
        ).fetchone()
        if row:
            conn.execute("UPDATE sheets SET claimed_by=?, claimed_at=? WHERE id=?", (user_id, now(), row["id"]))
        return row


def release(conn: sqlite3.Connection, sheet_id: int, user_id: int) -> None:
    conn.execute("UPDATE sheets SET claimed_by=NULL, claimed_at=NULL WHERE id=? AND claimed_by=?", (sheet_id, user_id))


def answers_for(conn: sqlite3.Connection, sheet_id: int) -> list[sqlite3.Row]:
    order = {q.id: i for i, q in enumerate(questions())}
    rows = conn.execute("SELECT * FROM answers WHERE sheet_id=?", (sheet_id,)).fetchall()
    return sorted(rows, key=lambda r: order.get(r["question_id"], 999))


def pending_answers(conn: sqlite3.Connection, sheet_id: int) -> list[sqlite3.Row]:
    return [a for a in answers_for(conn, sheet_id) if a["status"] == "needs_review"]


def save_review(conn: sqlite3.Connection, sheet_id: int, question_id: str, text: str, script: str,
                user_id: int, raw: str | None = None) -> None:
    conn.execute(
        """UPDATE answers SET status='reviewed', final_text=?, final_script=?, reviewer_raw=?,
                              reviewed_by=?, reviewed_at=?
           WHERE sheet_id=? AND question_id=?""",
        (text, script, raw if raw is not None else text, user_id, now(), sheet_id, question_id),
    )
    conn.execute("UPDATE sheets SET claimed_at=? WHERE id=?", (now(), sheet_id))


def reopen_answer(conn: sqlite3.Connection, sheet_id: int, question_id: str) -> None:
    conn.execute(
        """UPDATE answers SET status='needs_review', final_text=NULL, final_script=NULL, reviewer_raw=NULL,
                              reviewed_by=NULL, reviewed_at=NULL
           WHERE sheet_id=? AND question_id=?""",
        (sheet_id, question_id),
    )


def finish_sheet_if_done(conn: sqlite3.Connection, sheet_id: int, user_id: int) -> bool:
    with tx(conn):
        left = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE sheet_id=? AND status='needs_review'", (sheet_id,)
        ).fetchone()[0]
        if left:
            return False
        conn.execute(
            """UPDATE sheets SET status='reviewed', reviewed_by=?, reviewed_at=?, claimed_by=NULL,
                                 claimed_at=NULL, updated_at=? WHERE id=?""",
            (user_id, now(), now(), sheet_id),
        )
        return True


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)


# ---------- checking automatically accepted sheets ----------

def claim_next_auto(conn: sqlite3.Connection, user_id: int, exclude: set[int] = frozenset()) -> sqlite3.Row | None:
    """Next auto-accepted sheet nobody has checked yet, least confident first."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=settings.claim_minutes)).isoformat(timespec="seconds")
    exclude = tuple(exclude) or (-1,)
    with tx(conn):
        row = conn.execute(
            f"""SELECT s.* FROM sheets s
                WHERE s.status='auto_ok'
                  AND (s.claimed_by IS NULL OR s.claimed_by=? OR s.claimed_at < ?)
                  AND s.id NOT IN ({",".join("?" * len(exclude))})
                ORDER BY (SELECT MIN(COALESCE(a.ai_confidence, 0)) FROM answers a
                          WHERE a.sheet_id=s.id AND a.ai_blank=0), s.id
                LIMIT 1""",
            (user_id, stale, *exclude),
        ).fetchone()
        if row:
            conn.execute("UPDATE sheets SET claimed_by=?, claimed_at=? WHERE id=?", (user_id, now(), row["id"]))
        return row


def confirm_sheet(conn: sqlite3.Connection, sheet_id: int, user_id: int) -> None:
    """A human looked at every answer on the sheet and it is correct as shown."""
    ts = now()
    with tx(conn):
        conn.execute(
            """UPDATE answers SET status='reviewed', final_text=COALESCE(final_text, ai_text),
                                  final_script=COALESCE(final_script, 'none'),
                                  reviewer_raw=COALESCE(reviewer_raw, ai_text),
                                  reviewed_by=COALESCE(reviewed_by, ?), reviewed_at=COALESCE(reviewed_at, ?)
               WHERE sheet_id=?""",
            (user_id, ts, sheet_id),
        )
        conn.execute(
            """UPDATE sheets SET status='reviewed', reviewed_by=?, reviewed_at=?, claimed_by=NULL,
                                 claimed_at=NULL, updated_at=? WHERE id=?""",
            (user_id, ts, ts, sheet_id),
        )


def open_answer(conn: sqlite3.Connection, sheet_id: int, question_id: str) -> None:
    """Send one answer of an already accepted/reviewed sheet back for correction. Its current text is kept."""
    conn.execute(
        """UPDATE answers SET status='needs_review',
                  flags=CASE WHEN flags LIKE '%manual_check%' THEN flags
                             ELSE json_insert(flags, '$[#]', 'manual_check') END
           WHERE sheet_id=? AND question_id=?""",
        (sheet_id, question_id),
    )
