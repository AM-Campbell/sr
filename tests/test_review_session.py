"""Tests for ReviewSession logic."""

import json
import time as time_mod

from sr.db import init_db
from sr.models import Card
from sr.server_review import ReviewSession
from sr.sync import sync_cards


def _setup_cards(conn, cards_data, adapter_name="basic_qa"):
    """Helper to insert cards directly into db."""
    for source_path, key, content, gradable, tags in cards_data:
        chash = "hash_" + key
        cur = conn.execute("""
            INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (source_path, key, adapter_name, json.dumps(content), chash, content.get("q", ""), gradable))
        card_id = cur.lastrowid
        conn.execute("INSERT INTO card_state (card_id, status) VALUES (?, 'active')", (card_id,))
        for tag in tags:
            conn.execute("INSERT OR IGNORE INTO card_tags (card_id, tag) VALUES (?, ?)", (card_id, tag))
    conn.commit()


class FakeAdapter:
    def render_front(self, content):
        return f"<div>{content.get('q', '')}</div>"

    def render_back(self, content):
        return f"<div>{content.get('a', '')}</div>"


def test_get_next_card():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    card = session.get_next_card()
    assert card is not None
    assert card["id"] == 1
    conn.close()


def test_get_next_card_none():
    conn = init_db(":memory:")
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    assert session.get_next_card() is None
    conn.close()


def test_flip():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    session.get_next_card()
    back = session.flip()
    assert "A1" in back
    conn.close()


def test_grade():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    session.get_next_card()
    session.flip()
    session.grade_current(1)
    assert session.reviewed == 1
    assert 1 in session.reviewed_ids

    # Review log should have entry
    log = conn.execute("SELECT * FROM review_log").fetchall()
    assert len(log) == 1
    assert log[0]["grade"] == 1
    conn.close()


def test_undo():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    session.get_next_card()
    session.flip()
    session.grade_current(1)
    assert session.reviewed == 1

    # Undo
    prev = session.previous_card
    session.reviewed_ids.discard(prev["id"])
    session.current_card = prev
    session.previous_card = None
    assert prev["id"] not in session.reviewed_ids
    conn.close()


def test_tag_filter():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, ["python"]),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, ["java"]),
    ])
    session = ReviewSession(conn, None, None, tag_filter="python",
                            get_adapter_fn=lambda _: FakeAdapter())
    card = session.get_next_card()
    assert card is not None
    # Should be the python card
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM card_tags WHERE card_id=?", (card["id"],))]
    assert "python" in tags
    conn.close()


def test_path_filter():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/notes/python.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/notes/java.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    session = ReviewSession(conn, None, None, path_filter="/notes/python",
                            get_adapter_fn=lambda _: FakeAdapter())
    card = session.get_next_card()
    assert card is not None
    assert "python" in card["source_path"]
    conn.close()


def test_reviewed_ids_exclusion():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    c1 = session.get_next_card()
    session.flip()
    session.grade_current(1)
    c2 = session.get_next_card()
    assert c2 is not None
    assert c2["id"] != c1["id"]
    conn.close()


def test_remaining_count():
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    assert session.remaining_count() == 2
    session.get_next_card()
    session.flip()
    session.grade_current(1)
    assert session.remaining_count() == 1
    conn.close()
