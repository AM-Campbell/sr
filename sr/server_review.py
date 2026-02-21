"""Review web server: serves the review UI and handles review API."""

import http.server
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib.resources import files

from sr.adapters import load_adapter
from sr.flags import add_flag, get_flags, remove_flag
from sr.models import ReviewEvent
from sr.sync import _upsert_recommendation


def _load_template(name: str) -> str:
    return files("sr.templates").joinpath(name).read_text()


class ReviewSession:
    def __init__(self, conn, scheduler, sr_dir, settings=None,
                 tag_filter=None, path_filter=None, flag_filter=None,
                 get_adapter_fn=None):
        self.conn = conn
        self.scheduler = scheduler
        self.sr_dir = sr_dir
        self.settings = settings or {}
        self.tag_filter = tag_filter
        self.path_filter = path_filter
        self.flag_filter = flag_filter
        self._get_adapter = get_adapter_fn or (lambda name: load_adapter(name, sr_dir))
        self.session_id = str(uuid.uuid4())
        self.token = str(uuid.uuid4())
        self.current_card = None
        self.previous_card = None
        self.flip_time = None
        self.serve_time = None
        self.reviewed = 0
        self.reviewed_ids: set[int] = set()

    def get_next_card(self) -> dict | None:
        query = """
            SELECT c.id, c.source_path, c.adapter, c.content, c.gradable
            FROM cards c
            JOIN card_state cs ON c.id = cs.card_id
            LEFT JOIN recommendations r ON c.id = r.card_id
            WHERE cs.status = 'active' AND c.gradable = 1
        """
        params: list = []

        if self.tag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)"
            params.append(self.tag_filter)
        if self.path_filter:
            query += " AND c.source_path LIKE ?"
            params.append(f"{self.path_filter}%")
        if self.flag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)"
            params.append(self.flag_filter)
        if self.reviewed_ids:
            placeholders = ",".join("?" * len(self.reviewed_ids))
            query += f" AND c.id NOT IN ({placeholders})"
            params.extend(self.reviewed_ids)

        query += " ORDER BY CASE WHEN r.time IS NULL THEN 1 ELSE 0 END, r.time ASC, c.id ASC LIMIT 1"

        row = self.conn.execute(query, params).fetchone()
        if not row:
            return None

        self.current_card = dict(row)
        self.serve_time = time.time()
        self.flip_time = None
        return self.current_card

    def flip(self) -> str:
        if not self.current_card:
            raise ValueError("No current card")
        self.flip_time = time.time()
        adapter = self._get_adapter(self.current_card["adapter"])
        content = json.loads(self.current_card["content"])
        try:
            return adapter.render_back(content)
        except Exception as e:
            return f'<div style="color:var(--wrong)">Render error (card {self.current_card["id"]}): {e}</div>'

    def grade_current(self, grade: int, feedback: str | None = None,
                      response: dict | None = None):
        if not self.current_card:
            raise ValueError("No current card")
        now = time.time()
        time_on_front_ms = int((self.flip_time - self.serve_time) * 1000) if self.flip_time else None
        time_on_card_ms = int((now - self.serve_time) * 1000) if self.serve_time else None

        card_id = self.current_card["id"]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("""
            INSERT INTO review_log (card_id, session_id, timestamp, grade, time_on_front_ms, time_on_card_ms, feedback, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (card_id, self.session_id, ts, grade, time_on_front_ms, time_on_card_ms,
              feedback, json.dumps(response) if response else None))
        self.conn.commit()

        if self.scheduler:
            event = ReviewEvent(
                card_id=card_id, timestamp=ts, grade=grade,
                time_on_front_ms=time_on_front_ms or 0,
                time_on_card_ms=time_on_card_ms or 0,
                feedback=feedback, response=response
            )
            try:
                recs = self.scheduler.on_review(card_id, event)
                for rec in (recs or []):
                    _upsert_recommendation(self.conn, rec, self.scheduler)
                self.conn.commit()
            except Exception as e:
                import sys
                print(f"Warning: scheduler on_review failed: {e}", file=sys.stderr)

        self.previous_card = self.current_card
        self.reviewed_ids.add(card_id)
        self.reviewed += 1
        self.current_card = None

    def remaining_count(self) -> int:
        query = """
            SELECT COUNT(*) as cnt FROM cards c
            JOIN card_state cs ON c.id = cs.card_id
            LEFT JOIN recommendations r ON c.id = r.card_id
            WHERE cs.status = 'active' AND c.gradable = 1
        """
        params: list = []
        if self.tag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)"
            params.append(self.tag_filter)
        if self.path_filter:
            query += " AND c.source_path LIKE ?"
            params.append(f"{self.path_filter}%")
        if self.flag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)"
            params.append(self.flag_filter)
        if self.reviewed_ids:
            placeholders = ",".join("?" * len(self.reviewed_ids))
            query += f" AND c.id NOT IN ({placeholders})"
            params.extend(self.reviewed_ids)
        return self.conn.execute(query, params).fetchone()["cnt"]

    def render_front(self, card: dict) -> str:
        adapter = self._get_adapter(card["adapter"])
        content = json.loads(card["content"])
        try:
            return adapter.render_front(content)
        except Exception as e:
            return f'<div style="color:var(--wrong)">Render error (card {card["id"]}): {e}</div>'


class ReviewHandler(http.server.BaseHTTPRequestHandler):
    session: ReviewSession = None
    settings: dict = {}

    def log_message(self, format, *args):
        pass

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg):
        self._json_response({"error": msg}, status)

    def _check_token(self) -> bool:
        token = self.headers.get("X-Session-Token")
        if token != self.session.token:
            self._error(403, "Invalid session token")
            return False
        return True

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self):
        if self.path == "/":
            body = _load_template("review.html").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/session":
            self._json_response({"session_token": self.session.token})
        elif self.path == "/api/next":
            if not self._check_token():
                return
            card = self.session.get_next_card()
            if not card:
                self._json_response({"done": True, "session_stats": {
                    "reviewed": self.session.reviewed, "remaining": 0}})
            else:
                front_html = self.session.render_front(card)
                flags = get_flags(self.session.conn, card["id"])
                self._json_response({
                    "done": False,
                    "id": card["id"],
                    "gradable": bool(card["gradable"]),
                    "front_html": front_html,
                    "flags": flags,
                    "session_stats": {
                        "reviewed": self.session.reviewed,
                        "remaining": self.session.remaining_count()
                    }
                })
        elif self.path == "/api/status":
            if not self._check_token():
                return
            self._json_response({
                "reviewed": self.session.reviewed,
                "remaining": self.session.remaining_count()
            })
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if self.path == "/api/flip":
            if not self._check_token():
                return
            try:
                back_html = self.session.flip()
                self._json_response({"back_html": back_html})
            except ValueError as e:
                self._error(400, str(e))

        elif self.path == "/api/grade":
            if not self._check_token():
                return
            body = self._read_body()
            grade = body.get("grade")
            if grade not in (0, 1):
                self._error(400, "grade must be 0 or 1")
                return
            try:
                self.session.grade_current(
                    grade, body.get("feedback"), body.get("response"))
                self._json_response({"ok": True})
            except ValueError as e:
                self._error(400, str(e))

        elif self.path == "/api/skip":
            if not self._check_token():
                return
            if self.session.current_card:
                self.session.previous_card = self.session.current_card
                self.session.reviewed_ids.add(self.session.current_card["id"])
                self.session.reviewed += 1
                self.session.current_card = None
            self._json_response({"ok": True})

        elif self.path == "/api/undo":
            if not self._check_token():
                return
            prev = self.session.previous_card
            if not prev:
                self._error(400, "Nothing to undo")
                return
            self.session.reviewed_ids.discard(prev["id"])
            self.session.current_card = prev
            self.session.serve_time = time.time()
            self.session.flip_time = time.time()
            self.session.previous_card = None
            front_html = self.session.render_front(prev)
            adapter = self.session._get_adapter(prev["adapter"])
            content = json.loads(prev["content"])
            try:
                back_html = adapter.render_back(content)
            except Exception as e:
                back_html = f'<div style="color:var(--wrong)">Render error: {e}</div>'
            self._json_response({"ok": True, "front_html": front_html, "back_html": back_html})

        elif self.path == "/api/flag":
            if not self._check_token():
                return
            body = self._read_body()
            flag = body.get("flag")
            if not flag:
                self._error(400, "flag is required")
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            add_flag(self.session.conn, card["id"], flag, body.get("note"))
            self._json_response({"ok": True, "flags": get_flags(self.session.conn, card["id"])})

        elif self.path == "/api/unflag":
            if not self._check_token():
                return
            body = self._read_body()
            flag = body.get("flag")
            if not flag:
                self._error(400, "flag is required")
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            remove_flag(self.session.conn, card["id"], flag)
            self._json_response({"ok": True, "flags": get_flags(self.session.conn, card["id"])})

        elif self.path == "/api/edit":
            if not self._check_token():
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            try:
                content = json.loads(card["content"])
                source_line = content.get("source_line", 1)
                cmd = _build_edit_command(self.session.settings, card["source_path"], source_line)
                subprocess.Popen(cmd, shell=True, start_new_session=True)
                self._json_response({"ok": True})
            except Exception as e:
                self._error(500, str(e))

        elif self.path == "/api/suspend":
            if not self._check_token():
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            card_id = card["id"]
            self.session.conn.execute(
                "UPDATE card_state SET status='inactive', updated_at=datetime('now') WHERE card_id=?",
                (card_id,))
            self.session.conn.execute("DELETE FROM recommendations WHERE card_id=?", (card_id,))
            self.session.conn.commit()
            if self.session.scheduler:
                try:
                    self.session.scheduler.on_card_status_changed(card_id, "inactive")
                except Exception:
                    pass
            self.session.previous_card = self.session.current_card
            self.session.reviewed_ids.add(card_id)
            self.session.current_card = None
            self._json_response({"ok": True, "suspended": True})

        else:
            self._error(404, "Not found")


def _build_edit_command(settings, file_path, line=1):
    template = settings.get("edit_command")
    if template:
        return template.replace("{file}", shlex.quote(file_path)).replace("{line}", str(line))
    editor = os.environ.get("EDITOR", "vim")
    for term_cmd in ["kitty -e", "alacritty -e", "foot", "xterm -e"]:
        if shutil.which(term_cmd.split()[0]):
            return f"{term_cmd} {editor} +{line} {shlex.quote(file_path)}"
    return f"{editor} +{line} {shlex.quote(file_path)}"


def start_review_server(conn, scheduler, sr_dir, settings,
                        tag_filter=None, path_filter=None, flag_filter=None,
                        get_adapter_fn=None):
    port = settings.get("review_port", 8791)
    session = ReviewSession(conn, scheduler, sr_dir, settings,
                            tag_filter, path_filter, flag_filter,
                            get_adapter_fn=get_adapter_fn)
    ReviewHandler.session = session
    ReviewHandler.settings = settings

    server = http.server.HTTPServer(("127.0.0.1", port), ReviewHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Review server running at {url}")
    print(f"Press Ctrl+C to stop")

    try:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nSession ended.")
        print(f"  Reviewed: {session.reviewed} cards")
    finally:
        server.server_close()
