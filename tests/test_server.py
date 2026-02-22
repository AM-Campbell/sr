"""HTTP integration tests for the unified server."""

import http.server
import json
import threading
import urllib.request
import urllib.error

from sr.db import init_db
from sr.server import AppHandler


class FakeAdapter:
    def render_front(self, content):
        return f"<div>{content.get('q', '')}</div>"

    def render_back(self, content):
        return f"<div>{content.get('a', '')}</div>"


def _setup_server(conn, scheduler=None, get_adapter_fn=None):
    """Set up a unified server on an ephemeral port."""
    AppHandler.conn = conn
    AppHandler.sr_dir = None
    AppHandler.settings = {}
    AppHandler._scheduler = scheduler
    AppHandler._get_adapter_fn = get_adapter_fn or (lambda n: FakeAdapter())
    AppHandler._review_session = None

    server = http.server.HTTPServer(("127.0.0.1", 0), AppHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _api(port, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Session-Token", token)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _api_status(port, method, path, body=None, token=None):
    """Like _api but returns (status_code, parsed_body) without raising."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Session-Token", token)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _insert_review_cards(conn, num_cards=1):
    """Insert simple review cards."""
    for i in range(1, num_cards + 1):
        conn.execute(
            "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("/test.md", f"q{i}", "mnmd", json.dumps({"q": f"Q{i}", "a": f"A{i}"}),
             f"h{i}", f"Q{i}", 1))
        conn.execute(f"INSERT INTO card_state (card_id, status) VALUES ({i}, 'active')")
    conn.commit()


def _insert_browse_cards(conn):
    """Insert cards for browse testing with tags."""
    cards = [
        ("/test.md", "q1", {"q": "What is Python?", "a": "A language"}, "What is Python?"),
        ("/test.md", "q2", {"q": "What is Java?", "a": "Another language"}, "What is Java?"),
        ("/other.md", "q3", {"q": "What is Rust?", "a": "Systems lang"}, "What is Rust?"),
    ]
    for i, (path, key, content, display) in enumerate(cards, 1):
        conn.execute(
            "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (path, key, "mnmd", json.dumps(content), f"h{i}", display, 1))
        conn.execute(f"INSERT INTO card_state (card_id, status) VALUES ({i}, 'active')")
    conn.execute("INSERT INTO card_tags (card_id, tag) VALUES (1, 'python')")
    conn.commit()


def _insert_deck_cards(conn):
    """Insert cards across multiple source paths for deck testing."""
    cards = [
        ("/notes/python/basics.md", "q1", '{"q":"Q1","a":"A1"}', "active"),
        ("/notes/python/advanced.md", "q2", '{"q":"Q2","a":"A2"}', "active"),
        ("/notes/java/intro.md", "q3", '{"q":"Q3","a":"A3"}', "active"),
    ]
    for i, (path, key, content, status) in enumerate(cards, 1):
        conn.execute(
            "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (path, key, "mnmd", content, f"h{i}", f"Q{i}", 1))
        conn.execute("INSERT INTO card_state (card_id, status) VALUES (?, ?)", (i, status))
    # Make q1 due
    conn.execute(
        "INSERT INTO recommendations (card_id, scheduler_id, time, precision_seconds) "
        "VALUES (1, 'sm2', datetime('now', '-1 hour'), 60)")
    conn.commit()


# ── HTML root ──────────────────────────────────────────────────

def test_get_root():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        url = f"http://127.0.0.1:{port}/"
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200
            body = resp.read()
            assert b"<title>sr</title>" in body
    finally:
        server.shutdown()
        conn.close()


# ── Decks API ──────────────────────────────────────────────────

def test_decks_tree():
    conn = init_db(":memory:")
    _insert_deck_cards(conn)
    server, port = _setup_server(conn)
    try:
        tree = _api(port, "GET", "/api/decks/tree")
        assert isinstance(tree, list)
        assert len(tree) > 0
        total = sum(n["total"] for n in tree)
        assert total == 3
        due = sum(n["due"] for n in tree)
        assert due == 1
        active = sum(n["active"] for n in tree)
        assert active == 3
    finally:
        server.shutdown()
        conn.close()


def test_decks_tree_empty():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        tree = _api(port, "GET", "/api/decks/tree")
        assert tree == []
    finally:
        server.shutdown()
        conn.close()


def test_decks_tree_has_leaves():
    conn = init_db(":memory:")
    _insert_deck_cards(conn)
    server, port = _setup_server(conn)
    try:
        tree = _api(port, "GET", "/api/decks/tree")
        leaves = _find_leaves(tree)
        assert len(leaves) >= 1
        for leaf in leaves:
            assert leaf["is_leaf"] is True
            assert leaf["children"] == []
    finally:
        server.shutdown()
        conn.close()


# ── Review API ─────────────────────────────────────────────────

def test_review_session_guard():
    """Hitting review endpoints without a session returns 409."""
    conn = init_db(":memory:")
    _insert_review_cards(conn)
    server, port = _setup_server(conn)
    try:
        status, body = _api_status(port, "GET", "/api/review/next", token="any")
        assert status == 409
        assert "No active" in body.get("error", "")
    finally:
        server.shutdown()
        conn.close()


def test_review_start_and_next():
    conn = init_db(":memory:")
    _insert_review_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "POST", "/api/review/start", body={})
        token = data["session_token"]
        assert token

        data = _api(port, "GET", "/api/review/next", token=token)
        assert data["done"] is False
        assert data["id"] == 1
        assert "Q1" in data["front_html"]
    finally:
        server.shutdown()
        conn.close()


def test_review_full_flow():
    conn = init_db(":memory:")
    _insert_review_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "POST", "/api/review/start", body={})
        token = data["session_token"]

        _api(port, "GET", "/api/review/next", token=token)
        flip_data = _api(port, "POST", "/api/review/flip", token=token)
        assert "A1" in flip_data["back_html"]

        grade_data = _api(port, "POST", "/api/review/grade", body={"grade": 1}, token=token)
        assert grade_data["ok"] is True

        next_data = _api(port, "GET", "/api/review/next", token=token)
        assert next_data["done"] is True
    finally:
        server.shutdown()
        conn.close()


def test_review_undo():
    conn = init_db(":memory:")
    _insert_review_cards(conn, num_cards=2)
    server, port = _setup_server(conn)
    try:
        token = _api(port, "POST", "/api/review/start", body={})["session_token"]

        first = _api(port, "GET", "/api/review/next", token=token)
        first_id = first["id"]
        _api(port, "POST", "/api/review/flip", token=token)
        _api(port, "POST", "/api/review/grade", body={"grade": 0}, token=token)

        undo_data = _api(port, "POST", "/api/review/undo", token=token)
        assert undo_data["ok"] is True
        assert "Q" in undo_data["front_html"]
        assert "A" in undo_data["back_html"]

        _api(port, "POST", "/api/review/grade", body={"grade": 1}, token=token)
        second = _api(port, "GET", "/api/review/next", token=token)
        assert second["done"] is False
        assert second["id"] != first_id
    finally:
        server.shutdown()
        conn.close()


def test_review_invalid_token():
    conn = init_db(":memory:")
    _insert_review_cards(conn)
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/review/start", body={})
        status, body = _api_status(port, "GET", "/api/review/next", token="wrong-token")
        assert status == 403
        assert "Invalid" in body.get("error", "")
    finally:
        server.shutdown()
        conn.close()


def test_review_invalid_grade():
    conn = init_db(":memory:")
    _insert_review_cards(conn)
    server, port = _setup_server(conn)
    try:
        token = _api(port, "POST", "/api/review/start", body={})["session_token"]
        _api(port, "GET", "/api/review/next", token=token)
        _api(port, "POST", "/api/review/flip", token=token)
        status, body = _api_status(port, "POST", "/api/review/grade",
                                   body={"grade": 5}, token=token)
        assert status == 400
        assert "grade" in body.get("error", "").lower()
    finally:
        server.shutdown()
        conn.close()


def test_review_undo_nothing():
    conn = init_db(":memory:")
    _insert_review_cards(conn)
    server, port = _setup_server(conn)
    try:
        token = _api(port, "POST", "/api/review/start", body={})["session_token"]
        status, body = _api_status(port, "POST", "/api/review/undo", token=token)
        assert status == 400
        assert "Nothing" in body.get("error", "")
    finally:
        server.shutdown()
        conn.close()


def test_review_session_replacement():
    """Starting a new session should discard the old one."""
    conn = init_db(":memory:")
    _insert_review_cards(conn, num_cards=2)
    server, port = _setup_server(conn)
    try:
        token1 = _api(port, "POST", "/api/review/start", body={})["session_token"]
        # Use first session
        _api(port, "GET", "/api/review/next", token=token1)

        # Start new session
        token2 = _api(port, "POST", "/api/review/start", body={})["session_token"]
        assert token1 != token2

        # Old token should fail
        status, _ = _api_status(port, "GET", "/api/review/next", token=token1)
        assert status == 403

        # New token works
        data = _api(port, "GET", "/api/review/next", token=token2)
        assert data["done"] is False
    finally:
        server.shutdown()
        conn.close()


# ── Browse API ─────────────────────────────────────────────────

def test_browse_card_listing():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/browse/cards?status=active")
        assert data["total"] == 3
        assert len(data["cards"]) == 3
    finally:
        server.shutdown()
        conn.close()


def test_browse_filter_by_tag():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/browse/cards?tag=python")
        assert data["total"] == 1
        assert data["cards"][0]["tags"] == ["python"]
    finally:
        server.shutdown()
        conn.close()


def test_browse_card_detail():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/browse/cards/1")
        assert data["id"] == 1
        assert "Python" in data["front_html"]
    finally:
        server.shutdown()
        conn.close()


def test_browse_status_toggle():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/browse/cards/1/status", body={"status": "inactive"})
        row = conn.execute("SELECT status FROM card_state WHERE card_id=1").fetchone()
        assert row["status"] == "inactive"

        _api(port, "POST", "/api/browse/cards/1/status", body={"status": "active"})
        row = conn.execute("SELECT status FROM card_state WHERE card_id=1").fetchone()
        assert row["status"] == "active"
    finally:
        server.shutdown()
        conn.close()


def test_browse_flag_management():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/browse/cards/1/flag", body={"flag": "edit_later"})
        flags = conn.execute("SELECT flag FROM card_flags WHERE card_id=1").fetchall()
        assert len(flags) == 1

        _api(port, "POST", "/api/browse/cards/1/unflag", body={"flag": "edit_later"})
        flags = conn.execute("SELECT flag FROM card_flags WHERE card_id=1").fetchall()
        assert len(flags) == 0
    finally:
        server.shutdown()
        conn.close()


def test_browse_tag_management():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/browse/cards/2/tag", body={"tag": "new_tag"})
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=2")]
        assert "new_tag" in tags

        _api(port, "POST", "/api/browse/cards/2/untag", body={"tag": "new_tag"})
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=2")]
        assert "new_tag" not in tags
    finally:
        server.shutdown()
        conn.close()


def test_browse_search_by_display_text():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/browse/cards?q=Python")
        assert data["total"] == 1
        assert "Python" in data["cards"][0]["display_text"]
    finally:
        server.shutdown()
        conn.close()


def test_browse_search_by_source_path():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/browse/cards?q=other.md")
        assert data["total"] == 1
        assert "other.md" in data["cards"][0]["source_path"]
    finally:
        server.shutdown()
        conn.close()


def test_browse_search_no_results():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/browse/cards?q=nonexistent_xyz")
        assert data["total"] == 0
        assert len(data["cards"]) == 0
    finally:
        server.shutdown()
        conn.close()


def test_browse_tags_endpoint():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        tags = _api(port, "GET", "/api/browse/tags")
        assert "python" in tags
    finally:
        server.shutdown()
        conn.close()


def test_browse_flags_endpoint():
    conn = init_db(":memory:")
    _insert_browse_cards(conn)
    server, port = _setup_server(conn)
    try:
        # Add a flag first
        conn.execute("INSERT INTO card_flags (card_id, flag) VALUES (1, 'test_flag')")
        conn.commit()
        flags = _api(port, "GET", "/api/browse/flags")
        assert "test_flag" in flags
    finally:
        server.shutdown()
        conn.close()


# ── 404 ────────────────────────────────────────────────────────

def test_404_on_unknown_path():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        status, body = _api_status(port, "GET", "/api/unknown")
        assert status == 404
    finally:
        server.shutdown()
        conn.close()


# ── Helpers ────────────────────────────────────────────────────

def _find_leaves(nodes):
    result = []
    for n in nodes:
        if n["is_leaf"]:
            result.append(n)
        elif n.get("children"):
            result.extend(_find_leaves(n["children"]))
    return result
