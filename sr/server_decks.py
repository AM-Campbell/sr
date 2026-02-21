"""Decks web server: hierarchical deck browser with review launching."""

import http.server
import json
import threading
from importlib.resources import files

from sr.decks import build_deck_tree
from sr.schedulers import load_scheduler
from sr.server_review import ReviewHandler, ReviewSession


def _load_template(name: str) -> str:
    return files("sr.templates").joinpath(name).read_text()


class DecksHandler(http.server.BaseHTTPRequestHandler):
    conn = None
    sr_dir = None
    settings: dict = {}
    _get_adapter_fn = None
    _review_server = None
    _review_thread = None
    _review_lock = threading.Lock()

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

    def do_GET(self):
        if self.path == "/":
            body = _load_template("decks.html").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/tree":
            tree = build_deck_tree(self.conn)
            self._json_response(tree)
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if self.path == "/api/review":
            body = self._read_body()
            deck_path = body.get("path", "")
            review_port = self.settings.get("review_port", 8791)

            with DecksHandler._review_lock:
                if DecksHandler._review_server is not None:
                    if DecksHandler._review_thread.is_alive():
                        self._json_response({
                            "url": f"http://127.0.0.1:{review_port}/",
                            "note": "Review server already running"
                        })
                        return
                    else:
                        DecksHandler._review_server = None

                scheduler = None
                sched_name = self.settings.get("scheduler", "sm2")
                db_path = self.sr_dir / "sr.db"
                try:
                    scheduler = load_scheduler(sched_name, self.sr_dir, db_path)
                except Exception:
                    pass

                path_filter = deck_path if deck_path else None

                session = ReviewSession(
                    self.conn, scheduler, self.sr_dir, self.settings,
                    path_filter=path_filter,
                    get_adapter_fn=DecksHandler._get_adapter_fn)
                ReviewHandler.session = session
                ReviewHandler.settings = self.settings

                server = http.server.HTTPServer(
                    ("127.0.0.1", review_port), ReviewHandler)
                DecksHandler._review_server = server

                def serve():
                    try:
                        server.serve_forever()
                    finally:
                        server.server_close()
                        with DecksHandler._review_lock:
                            DecksHandler._review_server = None

                t = threading.Thread(target=serve, daemon=True)
                DecksHandler._review_thread = t
                t.start()

                url = f"http://127.0.0.1:{review_port}/"
                self._json_response({"url": url})
        else:
            self._error(404, "Not found")


def start_decks_server(conn, sr_dir, settings, get_adapter_fn=None):
    port = settings.get("review_port", 8791) + 2
    DecksHandler.conn = conn
    DecksHandler.sr_dir = sr_dir
    DecksHandler.settings = settings
    DecksHandler._get_adapter_fn = get_adapter_fn

    server = http.server.HTTPServer(("127.0.0.1", port), DecksHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Decks server running at {url}")
    print(f"Press Ctrl+C to stop")

    try:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDecks session ended.")
    finally:
        server.server_close()
