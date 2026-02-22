"""Unified web server: single handler for decks, browse, and review."""

import http.server
import json
import subprocess
import threading
import urllib.parse
from importlib.resources import files

from sr.adapters import load_adapter
from sr.config import list_vaults, register_vault
from sr.decks import build_deck_tree
from sr.flags import add_flag, get_flags, remove_flag
from sr.review_session import ReviewSession, _build_edit_command
from sr.schedulers import load_scheduler


def _load_template(name: str) -> str:
    return files("sr.templates").joinpath(name).read_text()


class AppHandler(http.server.BaseHTTPRequestHandler):
    conn = None
    sr_dir = None
    settings: dict = {}
    _get_adapter_fn = None
    _scheduler = None
    _review_session: ReviewSession | None = None

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
        if AppHandler._get_adapter_fn:
            return AppHandler._get_adapter_fn(name)
        return load_adapter(name, self.sr_dir)

    def _require_session(self) -> ReviewSession | None:
        session = AppHandler._review_session
        if session is None:
            self._error(409, "No active review session")
            return None
        return session

    def _check_token(self, session: ReviewSession) -> bool:
        token = self.headers.get("X-Session-Token")
        if token != session.token:
            self._error(403, "Invalid session token")
            return False
        return True

    @staticmethod
    def _session_stats(session: ReviewSession, remaining: int | None = None) -> dict:
        return {
            "reviewed": session.reviewed,
            "skipped": session.skipped,
            "suspended": session.suspended,
            "excluded": session.excluded_count,
            "remaining": remaining if remaining is not None else session.remaining_count(),
            "initial_total": session.initial_total,
        }

    # ── GET ──────────────────────────────────────────────────────────

    def do_GET(self):
        path, qs = self._parse_path()

        # HTML
        if path == "/":
            body = _load_template("app.html").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ── Decks ──
        elif path == "/api/decks/tree":
            tree = build_deck_tree(self.conn)
            self._json_response(tree)

        # ── Review ──
        elif path == "/api/review/next":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            card = session.get_next_card()
            if not card:
                self._json_response({"done": True,
                    "session_stats": self._session_stats(session, remaining=0)})
            else:
                front_html = session.render_front(card)
                flags = get_flags(session.conn, card["id"])
                self._json_response({
                    "done": False,
                    "id": card["id"],
                    "gradable": bool(card["gradable"]),
                    "front_html": front_html,
                    "flags": flags,
                    "session_stats": self._session_stats(session)
                })

        elif path == "/api/review/status":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            self._json_response(self._session_stats(session))

        # ── Browse ──
        elif path == "/api/browse/cards":
            self._handle_browse_cards(qs)

        elif path.startswith("/api/browse/cards/") and path.count("/") == 4:
            try:
                card_id = int(path.split("/")[4])
            except (ValueError, IndexError):
                self._error(400, "Invalid card ID")
                return
            self._handle_browse_card_detail(card_id)

        elif path == "/api/browse/tags":
            tags = [r["tag"] for r in self.conn.execute(
                "SELECT DISTINCT tag FROM card_tags ct JOIN card_state cs ON ct.card_id=cs.card_id "
                "WHERE cs.status != 'deleted' ORDER BY tag")]
            self._json_response(tags)

        elif path == "/api/browse/flags":
            flags = [r["flag"] for r in self.conn.execute(
                "SELECT DISTINCT flag FROM card_flags cf JOIN card_state cs ON cf.card_id=cs.card_id "
                "WHERE cs.status != 'deleted' ORDER BY flag")]
            self._json_response(flags)

        elif path == "/api/browse/paths":
            paths = [r["source_path"] for r in self.conn.execute(
                "SELECT DISTINCT c.source_path FROM cards c JOIN card_state cs ON c.id=cs.card_id "
                "WHERE cs.status != 'deleted' ORDER BY c.source_path")]
            self._json_response(paths)

        # ── Vault ──
        elif path == "/api/vault":
            import pathlib
            vault = pathlib.Path(AppHandler.sr_dir).parent.resolve() if AppHandler.sr_dir else None
            if vault:
                self._json_response({"name": vault.name, "path": str(vault)})
            else:
                self._json_response({"name": "", "path": ""})

        elif path == "/api/vaults":
            import pathlib
            vaults = list_vaults()
            active_vault = pathlib.Path(AppHandler.sr_dir).parent.resolve() if AppHandler.sr_dir else None
            active_path = str(active_vault) if active_vault else ""
            result = []
            for v in vaults:
                result.append({
                    "name": v.name,
                    "path": str(v),
                    "active": str(v) == active_path
                })
            self._json_response(result)

        else:
            self._error(404, "Not found")

    # ── POST ─────────────────────────────────────────────────────────

    def do_POST(self):
        path, _ = self._parse_path()

        # ── Review session management ──
        if path == "/api/review/start":
            self._handle_review_start()

        elif path == "/api/review/flip":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            try:
                back_html = session.flip()
                self._json_response({"back_html": back_html})
            except ValueError as e:
                self._error(400, str(e))

        elif path == "/api/review/grade":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            body = self._read_body()
            grade = body.get("grade")
            if grade not in (0, 1):
                self._error(400, "grade must be 0 or 1")
                return
            try:
                session.grade_current(
                    grade, body.get("feedback"), body.get("response"))
                self._json_response({"ok": True})
            except ValueError as e:
                self._error(400, str(e))

        elif path == "/api/review/skip":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            try:
                session.skip_current()
                self._json_response({"ok": True})
            except ValueError as e:
                self._error(400, str(e))

        elif path == "/api/review/undo":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            if not session.undo_stack:
                self._error(400, "Nothing to undo")
                return
            import time
            entry = session.undo_stack.pop()
            prev = entry["card"]
            card_id = prev["id"]
            session._unmark_reviewed(card_id)
            for sid in entry["excluded_ids"]:
                session._unmark_reviewed(sid)
            session.excluded_count -= len(entry["excluded_ids"])
            if entry.get("was_skip"):
                session.skipped -= 1
                session.skipped_ids.discard(card_id)
                # Remove the skip recommendation
                session.conn.execute(
                    "DELETE FROM recommendations WHERE card_id=?", (card_id,))
                session.conn.commit()
            elif entry.get("was_suspend"):
                session.suspended -= 1
                # Restore card to active and restore old recommendation
                session.conn.execute(
                    "UPDATE card_state SET status='active', updated_at=datetime('now') WHERE card_id=?",
                    (card_id,))
                old_rec = entry.get("old_rec")
                if old_rec:
                    session.conn.execute(
                        "INSERT OR REPLACE INTO recommendations (card_id, scheduler_id, time, precision_seconds) VALUES (?, ?, ?, ?)",
                        (old_rec["card_id"], old_rec["scheduler_id"], old_rec["time"], old_rec["precision_seconds"]))
                session.conn.commit()
                if session.scheduler:
                    try:
                        session.scheduler.on_card_status_changed(card_id, "active")
                    except Exception:
                        pass
            else:
                session.reviewed -= 1
                # Remove the review_log entry for this grade
                session.conn.execute(
                    "DELETE FROM review_log WHERE id = (SELECT MAX(id) FROM review_log WHERE card_id=? AND session_id=?)",
                    (card_id, session.session_id))
                # Restore old recommendation (or remove the new one if there was none)
                session.conn.execute("DELETE FROM recommendations WHERE card_id=?", (card_id,))
                old_rec = entry.get("old_rec")
                if old_rec:
                    session.conn.execute(
                        "INSERT OR REPLACE INTO recommendations (card_id, scheduler_id, time, precision_seconds) VALUES (?, ?, ?, ?)",
                        (old_rec["card_id"], old_rec["scheduler_id"], old_rec["time"], old_rec["precision_seconds"]))
                session.conn.commit()
                # Restore scheduler internal state (ease factor, interval, etc.)
                if session.scheduler and hasattr(session.scheduler, 'restore_card_state'):
                    old_sched = entry.get("old_sched_state")
                    session.scheduler.restore_card_state(card_id, old_sched)
            session.current_card = prev
            session.serve_time = time.time()
            session.flip_time = time.time()
            front_html = session.render_front(prev)
            adapter = session._get_adapter(prev["adapter"])
            content = json.loads(prev["content"])
            try:
                back_html = adapter.render_back(content)
            except Exception as e:
                back_html = f'<div style="color:var(--wrong)">Render error: {e}</div>'
            self._json_response({
                "ok": True, "front_html": front_html, "back_html": back_html,
                "session_stats": self._session_stats(session)
            })

        elif path == "/api/review/flag":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            body = self._read_body()
            flag = body.get("flag")
            if not flag:
                self._error(400, "flag is required")
                return
            card = session.current_card
            if not card:
                self._error(400, "No current card")
                return
            add_flag(session.conn, card["id"], flag, body.get("note"))
            self._json_response({"ok": True, "flags": get_flags(session.conn, card["id"])})

        elif path == "/api/review/unflag":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            body = self._read_body()
            flag = body.get("flag")
            if not flag:
                self._error(400, "flag is required")
                return
            card = session.current_card
            if not card:
                self._error(400, "No current card")
                return
            remove_flag(session.conn, card["id"], flag)
            self._json_response({"ok": True, "flags": get_flags(session.conn, card["id"])})

        elif path == "/api/review/edit":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            card = session.current_card
            if not card:
                self._error(400, "No current card")
                return
            try:
                source_line = card.get("source_line") or 1
                cmd = _build_edit_command(session.settings, card["source_path"], source_line)
                subprocess.Popen(cmd, shell=True, start_new_session=True)
                self._json_response({"ok": True})
            except Exception as e:
                self._error(500, str(e))

        elif path == "/api/review/suspend":
            session = self._require_session()
            if not session:
                return
            if not self._check_token(session):
                return
            card = session.current_card
            if not card:
                self._error(400, "No current card")
                return
            card_id = card["id"]
            # Save recommendation before deleting for undo
            old_rec = session.conn.execute(
                "SELECT * FROM recommendations WHERE card_id=?", (card_id,)).fetchone()
            session.conn.execute(
                "UPDATE card_state SET status='inactive', updated_at=datetime('now') WHERE card_id=?",
                (card_id,))
            session.conn.execute("DELETE FROM recommendations WHERE card_id=?", (card_id,))
            session.conn.commit()
            if session.scheduler:
                try:
                    session.scheduler.on_card_status_changed(card_id, "inactive")
                except Exception:
                    pass
            session._mark_reviewed(card_id)
            excluded = session._exclude_mutually_exclusive(card_id)
            session.undo_stack.append({
                "card": session.current_card, "excluded_ids": excluded,
                "was_suspend": True, "old_rec": dict(old_rec) if old_rec else None,
            })
            session.suspended += 1
            session.current_card = None
            self._json_response({"ok": True, "suspended": True})

        # ── Vault ──
        elif path == "/api/vault/switch":
            self._handle_vault_switch()

        # ── Browse POST ──
        elif path == "/api/browse/bulk/status":
            self._handle_bulk_status()

        elif path.startswith("/api/browse/cards/") and path.count("/") == 5:
            parts = path.strip("/").split("/")
            try:
                card_id = int(parts[3])
            except (ValueError, IndexError):
                self._error(400, "Invalid card ID")
                return
            action = parts[4]
            self._handle_browse_action(card_id, action)

        else:
            self._error(404, "Not found")

    # ── Review helpers ───────────────────────────────────────────────

    def _handle_review_start(self):
        body = self._read_body()
        path_filter = body.get("path") or None
        tag_filter = body.get("tag") or None
        flag_filter = body.get("flag") or None

        scheduler = AppHandler._scheduler
        if scheduler is None and self.sr_dir:
            sched_name = self.settings.get("scheduler", "sm2")
            db_path = self.sr_dir / "sr.db"
            try:
                scheduler = load_scheduler(sched_name, self.sr_dir, db_path)
                AppHandler._scheduler = scheduler
            except Exception:
                pass

        session = ReviewSession(
            self.conn, scheduler, self.sr_dir, self.settings,
            tag_filter=tag_filter, path_filter=path_filter,
            flag_filter=flag_filter,
            get_adapter_fn=AppHandler._get_adapter_fn)
        AppHandler._review_session = session
        self._json_response({"session_token": session.token})

    # ── Vault helpers ────────────────────────────────────────────────

    def _handle_vault_switch(self):
        import pathlib
        from sr.app import App
        body = self._read_body()
        new_path = body.get("path")
        if not new_path:
            self._error(400, "path is required")
            return
        vault = pathlib.Path(new_path).expanduser().resolve()
        if not vault.exists():
            self._error(400, "Path does not exist")
            return
        try:
            app = App(vault=vault)
            app.sr_dir.mkdir(parents=True, exist_ok=True)
            app.init_db()
            try:
                app.load_scheduler()
            except Exception:
                pass
        except Exception as e:
            self._error(500, f"Failed to open vault: {e}")
            return

        # Close old connections
        old_scheduler = AppHandler._scheduler
        if old_scheduler and hasattr(old_scheduler, 'close'):
            try:
                old_scheduler.close()
            except Exception:
                pass
        if AppHandler.conn:
            try:
                AppHandler.conn.close()
            except Exception:
                pass

        AppHandler.conn = app.conn
        AppHandler.sr_dir = app.sr_dir
        AppHandler.settings = app.settings
        AppHandler._scheduler = app.scheduler
        AppHandler._review_session = None

        register_vault(vault)

        self._json_response({"name": vault.name, "path": str(vault)})

    # ── Browse helpers ───────────────────────────────────────────────

    def _handle_bulk_status(self):
        body = self._read_body()
        card_ids = body.get("card_ids", [])
        new_status = body.get("status")
        if new_status not in ("active", "inactive"):
            self._error(400, "status must be 'active' or 'inactive'")
            return
        if not card_ids:
            self._error(400, "card_ids is required")
            return
        placeholders = ",".join("?" * len(card_ids))
        self.conn.execute(
            f"UPDATE card_state SET status=?, updated_at=datetime('now') WHERE card_id IN ({placeholders})",
            [new_status] + list(card_ids))
        if new_status == "inactive":
            self.conn.execute(
                f"DELETE FROM recommendations WHERE card_id IN ({placeholders})",
                list(card_ids))
        self.conn.commit()
        scheduler = AppHandler._scheduler
        if scheduler:
            for cid in card_ids:
                try:
                    scheduler.on_card_status_changed(cid, new_status)
                except Exception:
                    pass
        self._json_response({"ok": True, "updated": len(card_ids)})

    def _handle_browse_cards(self, qs):
        status = qs.get("status", [None])[0]
        tag = qs.get("tag", [None])[0]
        flag = qs.get("flag", [None])[0]
        path_filter = qs.get("path", [None])[0]
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
        if path_filter:
            where.append("c.source_path LIKE ?")
            params.append(f"{path_filter}%")
        if q:
            where.append("(c.display_text LIKE ? OR c.source_path LIKE ? OR c.content LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

        where_sql = " AND ".join(where)

        total = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE {where_sql}",
            params).fetchone()["cnt"]

        rows = self.conn.execute(f"""
            SELECT c.id, c.display_text, c.source_path, cs.status,
                   (SELECT GROUP_CONCAT(tag, '\t') FROM card_tags WHERE card_id=c.id) as _tags,
                   (SELECT GROUP_CONCAT(flag, '\t') FROM card_flags WHERE card_id=c.id) as _flags
            FROM cards c JOIN card_state cs ON c.id=cs.card_id
            WHERE {where_sql}
            ORDER BY c.id DESC LIMIT ? OFFSET ?
        """, params + [lim, off]).fetchall()

        cards = []
        for r in rows:
            tags = r["_tags"].split("\t") if r["_tags"] else []
            flags = r["_flags"].split("\t") if r["_flags"] else []
            cards.append({
                "id": r["id"], "display_text": r["display_text"],
                "source_path": r["source_path"], "status": r["status"],
                "tags": tags, "flags": flags
            })
        self._json_response({"cards": cards, "total": total, "offset": off, "limit": lim})

    def _handle_browse_card_detail(self, card_id):
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
            "front_html": front_html, "back_html": back_html,
        })

    def _handle_browse_action(self, card_id, action):
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
                "SELECT source_path, source_line FROM cards WHERE id=?", (card_id,)).fetchone()
            if not row:
                self._error(404, "Card not found")
                return
            try:
                source_line = row["source_line"] or 1
                cmd = _build_edit_command(self.settings, row["source_path"], source_line)
                subprocess.Popen(cmd, shell=True, start_new_session=True)
                self._json_response({"ok": True})
            except Exception as e:
                self._error(500, str(e))

        else:
            self._error(404, "Not found")


class _ReusableServer(http.server.HTTPServer):
    allow_reuse_address = True


def start_server(conn, sr_dir, settings, scheduler=None, get_adapter_fn=None):
    port = settings.get("review_port", 8791)
    AppHandler.conn = conn
    AppHandler.sr_dir = sr_dir
    AppHandler.settings = settings
    AppHandler._get_adapter_fn = get_adapter_fn
    AppHandler._scheduler = scheduler
    AppHandler._review_session = None

    server = _ReusableServer(("127.0.0.1", port), AppHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"sr running at {url}")
    print(f"Press Ctrl+C to stop")

    try:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
