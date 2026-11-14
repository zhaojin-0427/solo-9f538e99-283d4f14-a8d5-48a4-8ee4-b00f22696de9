"""Database helpers for the water-quality publishing app."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_date      TEXT    NOT NULL,
    version_no        INTEGER NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'candidate'
                              CHECK (status IN ('candidate', 'published', 'superseded')),
    original_filename TEXT    NOT NULL,
    stored_path       TEXT    NOT NULL,
    sha256            TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    row_count         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    published_at      TEXT,
    superseded_at     TEXT,
    UNIQUE (dataset_date, version_no)
);

CREATE INDEX IF NOT EXISTS idx_versions_date_status
    ON versions (dataset_date, status, version_no DESC);

CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    row_no        INTEGER NOT NULL,
    site          TEXT    NOT NULL,
    metric        TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL,
    observed_date TEXT    NOT NULL,
    value         REAL    NOT NULL,
    value_text    TEXT    NOT NULL,
    UNIQUE (version_id, site, metric, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_observations_version
    ON observations (version_id);

-- Corrections are bound to one observation of one published version.  The
-- original observation row (and therefore the original CSV value) is never
-- changed; the current effective value is the latest correction for the row.
-- A newly published version gets fresh observation rows, so corrections made
-- on an older version never carry over to it.
CREATE TABLE IF NOT EXISTS corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    observation_id  INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    revision_no     INTEGER NOT NULL,
    from_value      REAL    NOT NULL,
    from_value_text TEXT    NOT NULL,
    to_value        REAL    NOT NULL,
    to_value_text   TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    corrected_at    TEXT    NOT NULL,
    UNIQUE (observation_id, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_corrections_version
    ON corrections (version_id, observation_id, revision_no);
"""


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a connection suitable for explicit transactions."""
    conn = sqlite3.connect(
        str(database),
        timeout=15,
        isolation_level=None,  # explicit BEGIN / COMMIT, no implicit transactions
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db(database: str | Path) -> None:
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    last_error: sqlite3.Error | None = None
    for _ in range(5):
        conn = connect(database)
        try:
            conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:
            # Multiple gunicorn workers may initialise at the same moment.
            last_error = exc
        finally:
            conn.close()
    assert last_error is not None
    raise last_error


def get_db() -> sqlite3.Connection:
    """Return the request-scoped database connection."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def register_db(app) -> None:
    app.teardown_appcontext(close_db)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction atomically.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so concurrent
    candidate creation cannot allocate the same version number.  Any error,
    including a failing COMMIT, rolls the transaction back.  In particular,
    a failed publish while replacing a version leaves the previously published
    version untouched and fully queryable.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
