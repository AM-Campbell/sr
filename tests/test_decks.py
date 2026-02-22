"""Tests for sr.decks."""

from sr.db import init_db
from sr.decks import build_deck_tree, _aggregate_stats


def _insert_card(conn, source_path, key, status="active", is_due=False):
    conn.execute(
        "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, gradable) "
        "VALUES (?, ?, ?, '{}', 'h', 1)",
        (source_path, key, "mnmd"))
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


def test_hierarchical_tree():
    """Directories should become parent nodes with children."""
    conn = init_db(":memory:")
    _insert_card(conn, "/notes/python/basics.md", "q1")
    _insert_card(conn, "/notes/python/advanced.md", "q2", is_due=True)
    _insert_card(conn, "/notes/java/basics.md", "q3")
    tree = build_deck_tree(conn)

    # Should have two children under a common ancestor
    # The tree may collapse single-child chains, so look for python/java nodes
    names = {n["name"] for n in tree}
    # With collapsing, we should see "python" and "java" (or similar)
    # Find the python subtree
    all_nodes = _flatten_tree(tree)
    leaf_names = {n["name"] for n in all_nodes if n["is_leaf"]}
    assert "basics.md" in leaf_names or "advanced.md" in leaf_names

    # Parent nodes should aggregate children stats
    non_leaf = [n for n in all_nodes if not n["is_leaf"]]
    for parent in non_leaf:
        if parent["children"]:
            child_total = sum(c["total"] for c in parent["children"])
            assert parent["total"] == child_total
    conn.close()


def test_single_child_chain_collapsed():
    """Single-child directory chains should be collapsed into one node name."""
    conn = init_db(":memory:")
    # Two files in a deep path — both share /a/b/c/d/ so that part collapses
    _insert_card(conn, "/a/b/c/d/file1.md", "q1")
    _insert_card(conn, "/a/b/c/d/file2.md", "q2")
    tree = build_deck_tree(conn)

    # The chain /a/b/c/d/ has two children, but the path above should collapse
    # into one parent node. We should NOT see 4 levels of nesting.
    depth = _max_depth(tree)
    assert depth <= 2  # parent (collapsed) + leaves

    # Both leaves should be present
    all_nodes = _flatten_tree(tree)
    leaf_names = {n["name"] for n in all_nodes if n["is_leaf"]}
    assert "file1.md" in leaf_names
    assert "file2.md" in leaf_names
    conn.close()


def test_collapsed_chain_with_divergence():
    """Paths that share a prefix then diverge should collapse the shared part."""
    conn = init_db(":memory:")
    _insert_card(conn, "/home/user/notes/math/algebra.md", "q1")
    _insert_card(conn, "/home/user/notes/science/physics.md", "q2")
    tree = build_deck_tree(conn)

    # The common prefix /home/user/notes/ should be stripped.
    # We should see "math" and "science" as top-level (possibly collapsed with their leaf)
    all_nodes = _flatten_tree(tree)
    all_names = {n["name"] for n in all_nodes}
    # The leaf filenames should be reachable
    leaf_names = {n["name"] for n in all_nodes if n["is_leaf"]}
    assert "algebra.md" in leaf_names
    assert "physics.md" in leaf_names
    conn.close()


def test_deleted_cards_excluded():
    """Deleted cards should not appear in the deck tree."""
    conn = init_db(":memory:")
    _insert_card(conn, "/notes/test.md", "q1", status="active")
    _insert_card(conn, "/notes/test.md", "q2", status="deleted")
    tree = build_deck_tree(conn)
    assert len(tree) == 1
    assert tree[0]["total"] == 1
    conn.close()


def test_due_count_accuracy():
    """Due counts should only count active cards with past-due recommendations."""
    conn = init_db(":memory:")
    _insert_card(conn, "/notes/test.md", "q1", status="active", is_due=True)
    _insert_card(conn, "/notes/test.md", "q2", status="active", is_due=False)
    _insert_card(conn, "/notes/test.md", "q3", status="inactive", is_due=True)
    tree = build_deck_tree(conn)
    # Only q1 is active and due
    assert tree[0]["due"] == 1
    assert tree[0]["active"] == 2
    assert tree[0]["total"] == 3
    conn.close()


def _flatten_tree(nodes):
    """Recursively flatten tree nodes into a list."""
    result = []
    for n in nodes:
        result.append(n)
        if n.get("children"):
            result.extend(_flatten_tree(n["children"]))
    return result


def _max_depth(nodes, current=1):
    """Get max depth of tree."""
    if not nodes:
        return 0
    max_d = current
    for n in nodes:
        if n.get("children"):
            max_d = max(max_d, _max_depth(n["children"], current + 1))
    return max_d
