"""Tests for sr.db."""

from sr.db import init_db


def test_schema_creation():
    conn = init_db(":memory:")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    assert "cards" in tables
    assert "card_state" in tables
    assert "card_relations" in tables
    assert "card_tags" in tables
    assert "review_log" in tables
    assert "recommendations" in tables
    assert "card_flags" in tables
    conn.close()


def test_foreign_keys_enabled():
    conn = init_db(":memory:")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    conn.close()


def test_idempotent_schema():
    conn = init_db(":memory:")
    # Running init again should not error
    from sr.db import SCHEMA
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def test_file_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    assert db_path.exists()
    wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert wal == "wal"
    conn.close()
