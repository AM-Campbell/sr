"""Tests for sr.decks."""

from sr.db import init_db
from sr.decks import build_deck_tree, _aggregate_stats


def _insert_card(conn, source_path, key, status="active", is_due=False):
    conn.execute(
        "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, gradable) "
        "VALUES (?, ?, ?, '{}', 'h', 1)",
        (source_path, key, "basic_qa"))
    card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO card_state (card_id, status) VALUES (?, ?)", (card_id, status))
    if is_due:
        conn.execute(
            "INSERT INTO recommendations (card_id, scheduler_id, time, precision_seconds) "
            "VALUES (?, 'sm2', datetime('now', '-1 hour'), 60)", (card_id,))
    conn.commit()
    return card_id


def test_empty_tree():
    conn = init_db(":memory:")
    tree = build_deck_tree(conn)
    assert tree == []
    conn.close()


def test_single_source():
    conn = init_db(":memory:")
    _insert_card(conn, "/notes/python.md", "q1")
    _insert_card(conn, "/notes/python.md", "q2", is_due=True)
    tree = build_deck_tree(conn)
    assert len(tree) == 1
    assert tree[0]["total"] == 2
    assert tree[0]["active"] == 2
    assert tree[0]["due"] == 1
    conn.close()


def test_multiple_sources():
    conn = init_db(":memory:")
    _insert_card(conn, "/notes/python.md", "q1")
    _insert_card(conn, "/notes/java.md", "q2")
    tree = build_deck_tree(conn)
    assert len(tree) == 2
    total = sum(n["total"] for n in tree)
    assert total == 2
    conn.close()


def test_aggregate_stats():
    d = {
        "__stats__": {"total": 1, "active": 1, "due": 0},
        "child": {
            "__stats__": {"total": 2, "active": 1, "due": 1}
        }
    }
    stats = _aggregate_stats(d)
    assert stats["total"] == 3
    assert stats["active"] == 2
    assert stats["due"] == 1


def test_inactive_cards_counted():
    conn = init_db(":memory:")
    _insert_card(conn, "/notes/test.md", "q1", status="active")
    _insert_card(conn, "/notes/test.md", "q2", status="inactive")
    tree = build_deck_tree(conn)
    assert tree[0]["total"] == 2
    assert tree[0]["active"] == 1
    conn.close()
