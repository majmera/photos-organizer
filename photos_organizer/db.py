"""SQLite index of the library: items, where their files live, albums, and analysis results."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT UNIQUE NOT NULL,
    path        TEXT NOT NULL,           -- canonical file for this item
    filename    TEXT NOT NULL,           -- original filename, as Google Photos shows it
    kind        TEXT NOT NULL,           -- photo | video
    size        INTEGER NOT NULL,
    taken_at    REAL,                    -- unix seconds
    lat         REAL,
    lon         REAL,
    edited_path TEXT,                    -- Takeout's "-edited" copy, if any
    width       INTEGER,
    height      INTEGER,
    pixel_hash  TEXT,
    phash       TEXT,
    dhash       TEXT,
    sharpness   REAL,
    exposure    REAL,
    faces       INTEGER,
    eyes_open   REAL,
    framing     REAL,
    error       TEXT,
    analyzed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    path    TEXT PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS albums (
    id     INTEGER PRIMARY KEY,
    title  TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id),
    item_id  INTEGER NOT NULL REFERENCES items(id),
    PRIMARY KEY (album_id, item_id)
);

-- Duplicate groups. kind = exact | near.
CREATE TABLE IF NOT EXISTS groups (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_items (
    group_id INTEGER NOT NULL REFERENCES groups(id),
    item_id  INTEGER NOT NULL REFERENCES items(id),
    score    REAL,
    rank     INTEGER NOT NULL,
    keep     INTEGER NOT NULL,           -- tool's proposal
    PRIMARY KEY (group_id, item_id)
);

-- The user's overrides from the review page survive re-grouping.
CREATE TABLE IF NOT EXISTS decisions (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    keep    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    title    TEXT NOT NULL,
    kind     TEXT NOT NULL,              -- event | trip
    start_at REAL NOT NULL,
    end_at   REAL NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS event_items (
    event_id INTEGER NOT NULL REFERENCES events(id),
    item_id  INTEGER NOT NULL REFERENCES items(id),
    PRIMARY KEY (event_id, item_id)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def final_keep(conn: sqlite3.Connection, item_id: int, proposed: int) -> int:
    row = conn.execute("SELECT keep FROM decisions WHERE item_id = ?", (item_id,)).fetchone()
    return proposed if row is None else row["keep"]
