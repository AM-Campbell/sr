"""Database schema and initialization."""

import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    card_key TEXT NOT NULL,
    adapter TEXT NOT NULL,
    content JSON NOT NULL,
    content_hash TEXT NOT NULL,
    display_text TEXT,
    gradable BOOLEAN NOT NULL DEFAULT 1,
    source_line INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_path, card_key, adapter)
);

CREATE TABLE IF NOT EXISTS card_state (
    card_id INTEGER PRIMARY KEY REFERENCES cards(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive','deleted')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS card_relations (
    upstream_card_id INTEGER NOT NULL REFERENCES cards(id),
    downstream_card_id INTEGER NOT NULL REFERENCES cards(id),
    relation_type TEXT NOT NULL,
    PRIMARY KEY (upstream_card_id, downstream_card_id, relation_type)
);

CREATE TABLE IF NOT EXISTS card_tags (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (card_id, tag)
);

CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    session_id TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    grade INTEGER NOT NULL CHECK(grade IN (0, 1)),
    time_on_front_ms INTEGER,
    time_on_card_ms INTEGER,
    feedback TEXT CHECK(feedback IS NULL OR feedback IN ('too_hard','just_right','too_easy')),
    response JSON
);

CREATE TABLE IF NOT EXISTS recommendations (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    scheduler_id TEXT NOT NULL,
    time TEXT NOT NULL,
    precision_seconds INTEGER NOT NULL,
    PRIMARY KEY (card_id, scheduler_id)
);

CREATE TABLE IF NOT EXISTS card_flags (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    flag TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (card_id, flag)
);
"""


def init_db(db_path: pathlib.Path | str) -> sqlite3.Connection:
    db_path = pathlib.Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
