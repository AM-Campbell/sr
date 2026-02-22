"""Tests for sr.flags."""

from sr.db import init_db
from sr.flags import add_flag, get_flags, remove_flag


def _insert_test_card(conn):
    conn.execute(
        "INSERT INTO cards (source_path, card_key, adapter, content, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("/test.md", "q1", "mnmd", '{"q":"hi"}', "abc123"))
    card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO card_state (card_id, status) VALUES (?, 'active')", (card_id,))
    conn.commit()
    return card_id


def test_add_and_get_flag():
    conn = init_db(":memory:")
    cid = _insert_test_card(conn)
    add_flag(conn, cid, "edit_later", "needs review")
    flags = get_flags(conn, cid)
    assert len(flags) == 1
    assert flags[0]["flag"] == "edit_later"
    assert flags[0]["note"] == "needs review"
    conn.close()


def test_remove_flag():
    conn = init_db(":memory:")
    cid = _insert_test_card(conn)
    add_flag(conn, cid, "edit_later")
    remove_flag(conn, cid, "edit_later")
    flags = get_flags(conn, cid)
    assert len(flags) == 0
    conn.close()


def test_duplicate_flag_replaces():
    conn = init_db(":memory:")
    cid = _insert_test_card(conn)
    add_flag(conn, cid, "edit_later", "note1")
    add_flag(conn, cid, "edit_later", "note2")
    flags = get_flags(conn, cid)
    assert len(flags) == 1
    assert flags[0]["note"] == "note2"
    conn.close()


def test_multiple_flags():
    conn = init_db(":memory:")
    cid = _insert_test_card(conn)
    add_flag(conn, cid, "edit_later")
    add_flag(conn, cid, "needs_work")
    flags = get_flags(conn, cid)
    assert len(flags) == 2
    conn.close()
