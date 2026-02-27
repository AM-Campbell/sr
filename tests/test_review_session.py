"""Tests for ReviewSession logic."""

import json
import time as time_mod

from sr.db import init_db
from sr.models import Card
from sr.review_session import ReviewSession
from sr.sync import sync_cards


def _setup_cards(conn, cards_data, adapter_name="mnmd"):
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


def test_grade_sets_undo_stack():
    """After grading, undo_stack has the card so undo can restore it."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    card = session.get_next_card()
    card_id = card["id"]
    session.flip()
    session.grade_current(1)

    # After grading, undo_stack should have the card we just graded
    assert len(session.undo_stack) == 1
    assert session.undo_stack[-1]["card"]["id"] == card_id
    # The graded card should be in reviewed_ids
    assert card_id in session.reviewed_ids
    assert session.reviewed == 1
    # current_card should be cleared
    assert session.current_card is None

    # The next card should be a different one
    next_card = session.get_next_card()
    assert next_card is not None
    assert next_card["id"] != card_id
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


def test_skip_current():
    """skip_current() reschedules to tomorrow, increments skipped (not reviewed)."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    card = session.get_next_card()
    card_id = card["id"]
    session.skip_current()

    assert session.skipped == 1
    assert session.reviewed == 0
    assert card_id in session.skipped_ids
    assert card_id in session.reviewed_ids
    assert session.current_card is None

    # Recommendation should exist for the skipped card
    rec = conn.execute("SELECT * FROM recommendations WHERE card_id=?", (card_id,)).fetchone()
    assert rec is not None
    assert rec["precision_seconds"] == 3600

    # Next card should be the other one
    card2 = session.get_next_card()
    assert card2 is not None
    assert card2["id"] != card_id
    conn.close()


def test_excluded_count():
    """excluded_count tracks how many cards were auto-excluded via mutual exclusivity."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    # Add mutual exclusivity relation between card 1 and card 2
    conn.execute(
        "INSERT INTO card_relations (upstream_card_id, downstream_card_id, relation_type) VALUES (1, 2, 'mutually_exclusive')")
    conn.commit()

    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    session.get_next_card()
    session.flip()
    session.grade_current(1)

    assert session.excluded_count == 1
    assert session.reviewed == 1
    # Card 2 should be excluded
    assert 2 in session.reviewed_ids
    # No more cards
    assert session.get_next_card() is None
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


def _setup_followed_by(conn, upstream_id, downstream_id):
    """Insert an is_followed_by_on_correct relation."""
    conn.execute(
        "INSERT INTO card_relations (upstream_card_id, downstream_card_id, relation_type) VALUES (?, ?, 'is_followed_by_on_correct')",
        (upstream_id, downstream_id))
    conn.commit()


def test_followed_by_on_correct():
    """Grade A correctly → next card is downstream B."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    _setup_followed_by(conn, 1, 2)
    # Give card 1 an earlier recommendation so it's served first
    conn.execute("INSERT INTO recommendations (card_id, scheduler_id, time, precision_seconds) VALUES (1, 'test', '2000-01-01 00:00:00', 3600)")
    conn.commit()

    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    card_a = session.get_next_card()
    assert card_a["id"] == 1
    session.flip()
    session.grade_current(1)  # correct

    card_b = session.get_next_card()
    assert card_b is not None
    assert card_b["id"] == 2
    conn.close()


def test_followed_by_on_correct_wrong_grade():
    """Grade wrong → followup cleared, normal card served."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
        ("/test.md", "q3", {"q": "Q3", "a": "A3"}, True, []),
    ])
    _setup_followed_by(conn, 1, 2)
    # Ensure card 1 is served first
    conn.execute("INSERT INTO recommendations (card_id, scheduler_id, time, precision_seconds) VALUES (1, 'test', '2000-01-01 00:00:00', 3600)")
    conn.commit()

    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    card_a = session.get_next_card()
    assert card_a["id"] == 1
    # Followup should be pre-fetched
    assert session._followup_card is not None
    assert session._followup_card["id"] == 2
    session.flip()
    session.grade_current(0)  # wrong — followup should be cleared

    assert session._followup_card is None
    conn.close()


def test_followed_by_on_correct_chain():
    """A→B→C chain works through consecutive correct answers."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
        ("/test.md", "q3", {"q": "Q3", "a": "A3"}, True, []),
    ])
    _setup_followed_by(conn, 1, 2)
    _setup_followed_by(conn, 2, 3)
    conn.execute("INSERT INTO recommendations (card_id, scheduler_id, time, precision_seconds) VALUES (1, 'test', '2000-01-01 00:00:00', 3600)")
    conn.commit()

    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    card = session.get_next_card()
    assert card["id"] == 1
    session.flip()
    session.grade_current(1)

    card = session.get_next_card()
    assert card["id"] == 2
    session.flip()
    session.grade_current(1)

    card = session.get_next_card()
    assert card["id"] == 3
    conn.close()


def test_followed_by_on_correct_already_reviewed():
    """Downstream already reviewed → followup not cached, normal query used."""
    conn = init_db(":memory:")
    _setup_cards(conn, [
        ("/test.md", "q1", {"q": "Q1", "a": "A1"}, True, []),
        ("/test.md", "q2", {"q": "Q2", "a": "A2"}, True, []),
    ])
    _setup_followed_by(conn, 1, 2)
    conn.execute("INSERT INTO recommendations (card_id, scheduler_id, time, precision_seconds) VALUES (1, 'test', '2000-01-01 00:00:00', 3600)")
    conn.commit()

    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())

    # Mark card 2 as already reviewed
    session._mark_reviewed(2)

    card_a = session.get_next_card()
    assert card_a["id"] == 1
    # Followup (card 2) was already reviewed, so it should not be cached
    assert session._followup_card is None
    conn.close()
