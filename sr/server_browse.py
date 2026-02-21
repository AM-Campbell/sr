"""Browse web server: card listing, filtering, and management UI."""

import http.server
import json
import subprocess
import urllib.parse
from importlib.resources import files

from sr.adapters import load_adapter
from sr.flags import add_flag, get_flags, remove_flag
from sr.server_review import _build_edit_command


def _load_template(name: str) -> str:
    return files("sr.templates").joinpath(name).read_text()


class BrowseHandler(http.server.BaseHTTPRequestHandler):
    conn = None
    sr_dir = None
    settings: dict = {}
    _get_adapter_fn = None

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

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _parse_path(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _resolve_adapter(self, name):
        if BrowseHandler._get_adapter_fn:
            return BrowseHandler._get_adapter_fn(name)
        return load_adapter(name, self.sr_dir)

    def do_GET(self):
        path, qs = self._parse_path()

        if path == "/":
            body = _load_template("browse.html").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/cards":
            status = qs.get("status", [None])[0]
            tag = qs.get("tag", [None])[0]
            flag = qs.get("flag", [None])[0]
            q = qs.get("q", [None])[0]
            off = int(qs.get("offset", [0])[0])
            lim = int(qs.get("limit", [50])[0])
            lim = min(lim, 200)

            where = ["cs.status != 'deleted'"]
            params = []
            if status:
                where.append("cs.status = ?")
                params.append(status)
            if tag:
                where.append("c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)")
                params.append(tag)
            if flag:
                where.append("c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)")
                params.append(flag)
            if q:
                where.append("(c.display_text LIKE ? OR c.source_path LIKE ?)")
                params.extend([f"%{q}%", f"%{q}%"])

            where_sql = " AND ".join(where)

            total = self.conn.execute(
                f"SELECT COUNT(*) as cnt FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE {where_sql}",
                params).fetchone()["cnt"]

            rows = self.conn.execute(f"""
                SELECT c.id, c.display_text, c.source_path, cs.status
                FROM cards c JOIN card_state cs ON c.id=cs.card_id
                WHERE {where_sql}
                ORDER BY c.id DESC LIMIT ? OFFSET ?
            """, params + [lim, off]).fetchall()

            cards = []
            for r in rows:
                cid = r["id"]
                tags = [row["tag"] for row in self.conn.execute(
                    "SELECT tag FROM card_tags WHERE card_id=?", (cid,))]
                flags = [row["flag"] for row in self.conn.execute(
                    "SELECT flag FROM card_flags WHERE card_id=?", (cid,))]
                cards.append({
                    "id": cid, "display_text": r["display_text"],
                    "source_path": r["source_path"], "status": r["status"],
                    "tags": tags, "flags": flags
                })
            self._json_response({"cards": cards, "total": total, "offset": off, "limit": lim})

        elif path.startswith("/api/cards/") and path.count("/") == 3:
            try:
                card_id = int(path.split("/")[3])
            except (ValueError, IndexError):
                self._error(400, "Invalid card ID")
                return
            row = self.conn.execute("""
                SELECT c.id, c.display_text, c.source_path, c.adapter, c.content, cs.status
                FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE c.id=?
            """, (card_id,)).fetchone()
            if not row:
                self._error(404, "Card not found")
                return
            tags = [r["tag"] for r in self.conn.execute(
                "SELECT tag FROM card_tags WHERE card_id=?", (card_id,))]
            flags = get_flags(self.conn, card_id)
            reviews = [dict(r) for r in self.conn.execute(
                "SELECT timestamp, grade, feedback FROM review_log WHERE card_id=? ORDER BY timestamp DESC LIMIT 20",
                (card_id,))]
            content = json.loads(row["content"])
            front_html = ""
            back_html = ""
            try:
                adapter = self._resolve_adapter(row["adapter"])
                front_html = adapter.render_front(content)
                back_html = adapter.render_back(content)
            except Exception as e:
                front_html = f'<div style="color:var(--wrong)">Render error: {e}</div>'
            self._json_response({
                "id": row["id"], "display_text": row["display_text"],
                "source_path": row["source_path"], "adapter": row["adapter"],
                "content": content, "status": row["status"],
                "tags": tags, "flags": flags, "reviews": reviews,
                "front_html": front_html, "back_html": back_html
            })

        elif path == "/api/tags":
            tags = [r["tag"] for r in self.conn.execute(
                "SELECT DISTINCT tag FROM card_tags ct JOIN card_state cs ON ct.card_id=cs.card_id "
                "WHERE cs.status != 'deleted' ORDER BY tag")]
            self._json_response(tags)

        elif path == "/api/flags":
            flags = [r["flag"] for r in self.conn.execute(
                "SELECT DISTINCT flag FROM card_flags cf JOIN card_state cs ON cf.card_id=cs.card_id "
                "WHERE cs.status != 'deleted' ORDER BY flag")]
            self._json_response(flags)

        else:
            self._error(404, "Not found")

    def do_POST(self):
        path, _ = self._parse_path()

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "cards":
            try:
                card_id = int(parts[2])
            except ValueError:
                self._error(400, "Invalid card ID")
                return
            action = parts[3]
            body = self._read_body()

            if action == "status":
                new_status = body.get("status")
                if new_status not in ("active", "inactive"):
                    self._error(400, "status must be 'active' or 'inactive'")
                    return
                self.conn.execute(
                    "UPDATE card_state SET status=?, updated_at=datetime('now') WHERE card_id=?",
                    (new_status, card_id))
                if new_status == "inactive":
                    self.conn.execute("DELETE FROM recommendations WHERE card_id=?", (card_id,))
                self.conn.commit()
                self._json_response({"ok": True})

            elif action == "flag":
                flag = body.get("flag")
                if not flag:
                    self._error(400, "flag is required")
                    return
                add_flag(self.conn, card_id, flag, body.get("note"))
                self._json_response({"ok": True})

            elif action == "unflag":
                flag = body.get("flag")
                if not flag:
                    self._error(400, "flag is required")
                    return
                remove_flag(self.conn, card_id, flag)
                self._json_response({"ok": True})

            elif action == "tag":
                tag = body.get("tag")
                if not tag:
                    self._error(400, "tag is required")
                    return
                self.conn.execute(
                    "INSERT OR IGNORE INTO card_tags (card_id, tag) VALUES (?, ?)",
                    (card_id, tag))
                self.conn.commit()
                self._json_response({"ok": True})

            elif action == "untag":
                tag = body.get("tag")
                if not tag:
                    self._error(400, "tag is required")
                    return
                self.conn.execute(
                    "DELETE FROM card_tags WHERE card_id=? AND tag=?",
                    (card_id, tag))
                self.conn.commit()
                self._json_response({"ok": True})

            elif action == "edit":
                row = self.conn.execute(
                    "SELECT source_path, content FROM cards WHERE id=?", (card_id,)).fetchone()
                if not row:
                    self._error(404, "Card not found")
                    return
                try:
                    content = json.loads(row["content"])
                    source_line = content.get("source_line", 1)
                    cmd = _build_edit_command(self.settings, row["source_path"], source_line)
                    subprocess.Popen(cmd, shell=True, start_new_session=True)
                    self._json_response({"ok": True})
                except Exception as e:
                    self._error(500, str(e))

            else:
                self._error(404, "Not found")
        else:
            self._error(404, "Not found")


def start_browse_server(conn, sr_dir, settings, get_adapter_fn=None):
    import threading
    port = settings.get("review_port", 8791) + 1
    BrowseHandler.conn = conn
    BrowseHandler.sr_dir = sr_dir
    BrowseHandler.settings = settings
    BrowseHandler._get_adapter_fn = get_adapter_fn

    server = http.server.HTTPServer(("127.0.0.1", port), BrowseHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Browse server running at {url}")
    print(f"Press Ctrl+C to stop")

    try:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBrowse session ended.")
    finally:
        server.server_close()
