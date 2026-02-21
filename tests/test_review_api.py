"""HTTP integration tests for review endpoints."""

import http.server
import json
import threading
import urllib.request

from sr.db import init_db
from sr.server_review import ReviewHandler, ReviewSession


class FakeAdapter:
    def render_front(self, content):
        return f"<div>{content.get('q', '')}</div>"

    def render_back(self, content):
        return f"<div>{content.get('a', '')}</div>"


def _setup_server(conn):
    """Set up a review server on an ephemeral port."""
    # Insert a test card
    conn.execute(
        "INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("/test.md", "q1", "basic_qa", '{"q":"Q1","a":"A1"}', "h1", "Q1", 1))
    conn.execute("INSERT INTO card_state (card_id, status) VALUES (1, 'active')")
    conn.commit()

    session = ReviewSession(conn, None, None, get_adapter_fn=lambda _: FakeAdapter())
    ReviewHandler.session = session
    ReviewHandler.settings = {}

    server = http.server.HTTPServer(("127.0.0.1", 0), ReviewHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, session


def _api(port, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Session-Token", token)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_get_root():
    conn = init_db(":memory:")
    server, port, session = _setup_server(conn)
    try:
        url = f"http://127.0.0.1:{port}/"
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200
            assert b"sr review" in resp.read()
    finally:
        server.shutdown()
        conn.close()


def test_session_and_next():
    conn = init_db(":memory:")
    server, port, session = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/session")
        token = data["session_token"]

        data = _api(port, "GET", "/api/next", token=token)
        assert data["done"] is False
        assert data["id"] == 1
        assert "Q1" in data["front_html"]
    finally:
        server.shutdown()
        conn.close()


def test_full_review_flow():
    conn = init_db(":memory:")
    server, port, session = _setup_server(conn)
    try:
        data = _api(port, "GET", "/api/session")
        token = data["session_token"]

        _api(port, "GET", "/api/next", token=token)
        flip_data = _api(port, "POST", "/api/flip", token=token)
        assert "A1" in flip_data["back_html"]

        grade_data = _api(port, "POST", "/api/grade", body={"grade": 1}, token=token)
        assert grade_data["ok"] is True

        next_data = _api(port, "GET", "/api/next", token=token)
        assert next_data["done"] is True
    finally:
        server.shutdown()
        conn.close()
