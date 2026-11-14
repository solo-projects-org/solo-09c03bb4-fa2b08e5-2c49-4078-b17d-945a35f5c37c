"""SQLite schema and connection helpers.

Publication model
-----------------
A batch is the unit of publication.  The original bytes are written to a
temporary directory, fsynced, parsed, then renamed into its final
``originals/<batch_id>/`` directory; the database rows (batch metadata,
raw rows, parse rejections, per-vessel verdicts and trusted points) are
written in a single ``BEGIN IMMEDIATE`` transaction and committed afterwards.

Only a committed batch can be read, so a reader never observes half of an
upload.  Because the original file is already durable and in place *before*
the commit, a committed batch is guaranteed to have its file.  A crash after
the rename but before the commit leaves an unreferenced directory which is
removed on the next start.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    uploaded_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'published',
    rows_total      INTEGER NOT NULL,
    accepted_count  INTEGER NOT NULL,
    rejected_count  INTEGER NOT NULL,
    vessel_count    INTEGER NOT NULL,
    original_path   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    line_no     INTEGER NOT NULL,
    station     TEXT NOT NULL,
    mmsi        TEXT NOT NULL,
    utc         INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    UNIQUE (batch_id, line_no)
);
CREATE INDEX IF NOT EXISTS raw_rows_mmsi ON raw_rows(mmsi);
CREATE INDEX IF NOT EXISTS raw_rows_batch ON raw_rows(batch_id);

CREATE TABLE IF NOT EXISTS rejects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    line_no     INTEGER NOT NULL,
    mmsi        TEXT,
    station     TEXT,
    utc         INTEGER,
    reason      TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rejects_mmsi ON rejects(mmsi);
CREATE INDEX IF NOT EXISTS rejects_batch ON rejects(batch_id);

CREATE TABLE IF NOT EXISTS vessels (
    mmsi            TEXT PRIMARY KEY,
    version         INTEGER NOT NULL,
    row_count       INTEGER NOT NULL,
    trusted_count   INTEGER NOT NULL,
    isolated_count  INTEGER NOT NULL,
    first_utc       INTEGER,
    last_utc        INTEGER,
    updated_at      TEXT NOT NULL,
    last_batch_id   TEXT
);

CREATE TABLE IF NOT EXISTS track_points (
    mmsi        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    raw_row_id  INTEGER NOT NULL,
    batch_id    TEXT NOT NULL,
    station     TEXT NOT NULL,
    utc         INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    speed_knots REAL,
    PRIMARY KEY (mmsi, version, seq)
);

CREATE TABLE IF NOT EXISTS verdicts (
    mmsi        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    raw_row_id  INTEGER NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    detail      TEXT,
    PRIMARY KEY (mmsi, version, raw_row_id)
);
CREATE INDEX IF NOT EXISTS verdicts_version ON verdicts(mmsi, version);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Repeatable-read snapshot; WAL readers are not blocked by writers."""

    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
