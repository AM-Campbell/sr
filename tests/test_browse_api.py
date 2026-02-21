"""HTTP integration tests for browse endpoints."""

import http.server
import json
import threading
import urllib.request

from sr.db import init_db
from sr.server_browse import BrowseHandler


class FakeAdapter:
    def render_front(self, content):
        return f"<div>{content.get('q', '')}</div>"

    def render_back(self, content):
        return f"<div>{content.get('a', '')}</div>"


def _setup_server(conn):
    # Insert test cards
    for i in range(3):
        conn.execute(
            "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"/test.md", f"q{i+1}", "basic_qa", json.dumps({"q": f"Q{i+1}", "a": f"A{i+1}"}),
             f"h{i+1}", f"Q{i+1}", 1))
        conn.execute(f"INSERT INTO card_state (card_id, status) VALUES ({i+1}, 'active')")
    conn.execute("INSERT INTO card_tags (card_id, tag) VALUES (1, 'python')")
    conn.commit()

    BrowseHandler.conn = conn
    BrowseHandler.sr_dir = None
    BrowseHandler.settings = {}
    BrowseHandler._get_adapter_fn = lambda name: FakeAdapter()

    server = http.server.HTTPServer(("127.0.0.1", 0), BrowseHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _api(port, method, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_card_listing():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/cards?status=active")
        assert data["total"] == 3
        assert len(data["cards"]) == 3
    finally:
        server.shutdown()
        conn.close()


def test_card_filter_by_tag():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/cards?tag=python")
        assert data["total"] == 1
        assert data["cards"][0]["tags"] == ["python"]
    finally:
        server.shutdown()
        conn.close()


def test_card_detail():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/cards/1")
        assert data["id"] == 1
        assert "Q1" in data["front_html"]
    finally:
        server.shutdown()
        conn.close()


def test_status_toggle():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/cards/1/status", body={"status": "inactive"})
        row = conn.execute("SELECT status FROM card_state WHERE card_id=1").fetchone()
        assert row["status"] == "inactive"

        _api(port, "POST", "/api/cards/1/status", body={"status": "active"})
        row = conn.execute("SELECT status FROM card_state WHERE card_id=1").fetchone()
        assert row["status"] == "active"
    finally:
        server.shutdown()
        conn.close()


def test_flag_management():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/cards/1/flag", body={"flag": "edit_later"})
        flags = conn.execute("SELECT flag FROM card_flags WHERE card_id=1").fetchall()
        assert len(flags) == 1

        _api(port, "POST", "/api/cards/1/unflag", body={"flag": "edit_later"})
        flags = conn.execute("SELECT flag FROM card_flags WHERE card_id=1").fetchall()
        assert len(flags) == 0
    finally:
        server.shutdown()
        conn.close()


def test_tag_management():
    conn = init_db(":memory:")
    server, port = _setup_server(conn)
    try:
        _api(port, "POST", "/api/cards/2/tag", body={"tag": "new_tag"})
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=2")]
        assert "new_tag" in tags

        _api(port, "POST", "/api/cards/2/untag", body={"tag": "new_tag"})
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=2")]
        assert "new_tag" not in tags
    finally:
        server.shutdown()
        conn.close()
