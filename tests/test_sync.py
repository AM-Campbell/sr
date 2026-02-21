"""Tests for sr.sync — the most critical module."""

import json

from sr.db import init_db
from sr.models import Card, Recommendation
from sr.scanner import content_hash
from sr.sync import sync_cards


def _make_scan_result(source_path, adapter, cards, config=None):
    return (source_path, adapter, cards, config or {})


def test_new_card_creation():
    conn = init_db(":memory:")
    card = Card(key="q1", content={"q": "What?", "a": "That."}, display_text="What?", tags=["t1"])
    results = [_make_scan_result("/src/test.md", "basic_qa", [card])]
    stats = sync_cards(conn, results)
    assert stats["new"] == 1
    assert stats["unchanged"] == 0

    row = conn.execute("SELECT * FROM cards WHERE card_key='q1'").fetchone()
    assert row is not None
    assert row["adapter"] == "basic_qa"

    state = conn.execute("SELECT status FROM card_state WHERE card_id=?", (row["id"],)).fetchone()
    assert state["status"] == "active"

    tags = [r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=?", (row["id"],))]
    assert "t1" in tags
    conn.close()


def test_unchanged_card():
    conn = init_db(":memory:")
    card = Card(key="q1", content={"q": "What?"}, display_text="What?")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card])]

    sync_cards(conn, results)
    stats = sync_cards(conn, results)
    assert stats["unchanged"] == 1
    assert stats["new"] == 0
    conn.close()


def test_content_change_replacement_chain():
    conn = init_db(":memory:")
    card_v1 = Card(key="q1", content={"q": "v1"}, display_text="v1")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card_v1])]
    sync_cards(conn, results)

    old_row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    old_id = old_row["id"]

    card_v2 = Card(key="q1", content={"q": "v2"}, display_text="v2")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card_v2])]
    stats = sync_cards(conn, results)
    assert stats["updated"] == 1

    # Old card should be deleted
    old_state = conn.execute("SELECT status FROM card_state WHERE card_id=?", (old_id,)).fetchone()
    assert old_state["status"] == "deleted"

    # New card should exist and be active
    new_row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    new_id = new_row["id"]
    assert new_id != old_id
    new_state = conn.execute("SELECT status FROM card_state WHERE card_id=?", (new_id,)).fetchone()
    assert new_state["status"] == "active"

    # Replacement relation should exist
    rel = conn.execute(
        "SELECT * FROM card_relations WHERE upstream_card_id=? AND downstream_card_id=?",
        (old_id, new_id)).fetchone()
    assert rel is not None
    assert rel["relation_type"] == "is_replaced_by"
    conn.close()


def test_deleted_source():
    conn = init_db(":memory:")
    import pathlib
    card = Card(key="q1", content={"q": "hi"}, display_text="hi")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card])]
    sync_cards(conn, results, scanned_paths=[pathlib.Path("/src/test.md")])

    # Now scan with empty results but same scanned_paths
    stats = sync_cards(conn, [], scanned_paths=[pathlib.Path("/src/test.md")])
    assert stats["deleted"] == 1
    conn.close()


def test_suspension_preserved_on_unchanged():
    conn = init_db(":memory:")
    card = Card(key="q1", content={"q": "hi"}, display_text="hi")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card])]
    sync_cards(conn, results)

    # Manually suspend
    row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    conn.execute("UPDATE card_state SET status='inactive' WHERE card_id=?", (row["id"],))
    conn.commit()

    # Re-sync same content
    stats = sync_cards(conn, results)
    assert stats["unchanged"] == 1

    # Should still be inactive
    state = conn.execute("SELECT status FROM card_state WHERE card_id=?", (row["id"],)).fetchone()
    assert state["status"] == "inactive"
    conn.close()


def test_suspension_preserved_on_content_change():
    conn = init_db(":memory:")
    card_v1 = Card(key="q1", content={"q": "v1"}, display_text="v1")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card_v1])]
    sync_cards(conn, results)

    # Manually suspend
    row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    conn.execute("UPDATE card_state SET status='inactive' WHERE card_id=?", (row["id"],))
    conn.commit()

    # Change content
    card_v2 = Card(key="q1", content={"q": "v2"}, display_text="v2")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card_v2])]
    stats = sync_cards(conn, results)
    assert stats["updated"] == 1

    # New card should be inactive (preserving suspension)
    new_row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    new_state = conn.execute("SELECT status FROM card_state WHERE card_id=?", (new_row["id"],)).fetchone()
    assert new_state["status"] == "inactive"
    conn.close()


def test_tag_sync():
    conn = init_db(":memory:")
    card = Card(key="q1", content={"q": "hi"}, display_text="hi", tags=["a", "b"])
    results = [_make_scan_result("/src/test.md", "basic_qa", [card])]
    sync_cards(conn, results)

    row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    tags = {r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=?", (row["id"],))}
    assert tags == {"a", "b"}

    # Update tags
    card2 = Card(key="q1", content={"q": "hi"}, display_text="hi", tags=["b", "c"])
    results = [_make_scan_result("/src/test.md", "basic_qa", [card2])]
    sync_cards(conn, results)

    tags = {r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=?", (row["id"],))}
    assert tags == {"b", "c"}
    conn.close()


class FakeScheduler:
    scheduler_id = "fake"

    def __init__(self):
        self.created = []
        self.replaced = []
        self.status_changed = []

    def on_card_created(self, card_id):
        self.created.append(card_id)
        return Recommendation(card_id=card_id, time="2025-01-01 00:00:00", precision_seconds=60)

    def on_card_replaced(self, old_id, new_id):
        self.replaced.append((old_id, new_id))
        return Recommendation(card_id=new_id, time="2025-01-01 00:00:00", precision_seconds=60)

    def on_card_status_changed(self, card_id, status):
        self.status_changed.append((card_id, status))

    def on_relations_changed(self, card_ids):
        return []


def test_scheduler_hooks():
    conn = init_db(":memory:")
    sched = FakeScheduler()

    card = Card(key="q1", content={"q": "hi"}, display_text="hi")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card])]
    sync_cards(conn, results, scheduler=sched)
    assert len(sched.created) == 1

    # Content change triggers replacement
    card_v2 = Card(key="q1", content={"q": "v2"}, display_text="v2")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card_v2])]
    sync_cards(conn, results, scheduler=sched)
    assert len(sched.replaced) == 1
    conn.close()


def test_new_card_suspended_source():
    conn = init_db(":memory:")
    card = Card(key="q1", content={"q": "hi"}, display_text="hi")
    results = [_make_scan_result("/src/test.md", "basic_qa", [card], config={"suspended": True})]
    sync_cards(conn, results)

    row = conn.execute("SELECT id FROM cards WHERE card_key='q1'").fetchone()
    state = conn.execute("SELECT status FROM card_state WHERE card_id=?", (row["id"],)).fetchone()
    assert state["status"] == "inactive"
    conn.close()
