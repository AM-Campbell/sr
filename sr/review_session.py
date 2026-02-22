"""ReviewSession: manages card review state independent of HTTP."""

import json
import os
import shlex
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone

from sr.adapters import load_adapter
from sr.flags import add_flag, get_flags, remove_flag
from sr.models import ReviewEvent
from sr.sync import _upsert_recommendation


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
        self.undo_stack: list[dict] = []  # stack of {card, excluded_ids}
        self.flip_time = None
        self.serve_time = None
        self.reviewed = 0
        self.reviewed_ids: set[int] = set()

    def _filter_clause(self) -> tuple[str, list]:
        """Build the WHERE filters shared by card queries."""
        clauses = []
        params: list = []
        if self.tag_filter:
            clauses.append("c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)")
            params.append(self.tag_filter)
        if self.path_filter:
            clauses.append("c.source_path LIKE ?")
            params.append(f"{self.path_filter}%")
        if self.flag_filter:
            clauses.append("c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)")
            params.append(self.flag_filter)
        if self.reviewed_ids:
            placeholders = ",".join("?" * len(self.reviewed_ids))
            clauses.append(f"c.id NOT IN ({placeholders})")
            params.extend(self.reviewed_ids)
        extra = (" AND " + " AND ".join(clauses)) if clauses else ""
        return extra, params

    def get_next_card(self) -> dict | None:
        extra, params = self._filter_clause()
        row = self.conn.execute(f"""
            SELECT c.id, c.source_path, c.adapter, c.content, c.gradable, c.source_line
            FROM cards c
            JOIN card_state cs ON c.id = cs.card_id
            LEFT JOIN recommendations r ON c.id = r.card_id
            WHERE cs.status = 'active' AND c.gradable = 1
              AND (r.time IS NULL OR r.time <= datetime('now')){extra}
            ORDER BY CASE WHEN r.time IS NULL THEN 1 ELSE 0 END, r.time ASC, c.id ASC
            LIMIT 1
        """, params).fetchone()
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
                print(f"Warning: scheduler on_review failed: {e}", file=sys.stderr)

        self.reviewed_ids.add(card_id)
        excluded = self._exclude_mutually_exclusive(card_id)
        self.undo_stack.append({"card": self.current_card, "excluded_ids": excluded})
        self.reviewed += 1
        self.current_card = None

    def _exclude_mutually_exclusive(self, card_id: int) -> set[int]:
        """Add mutually exclusive siblings to reviewed_ids so they're skipped.
        Returns the set of sibling IDs that were newly excluded."""
        rows = self.conn.execute("""
            SELECT downstream_card_id AS sibling FROM card_relations
            WHERE upstream_card_id = ? AND relation_type = 'mutually_exclusive'
            UNION
            SELECT upstream_card_id AS sibling FROM card_relations
            WHERE downstream_card_id = ? AND relation_type = 'mutually_exclusive'
        """, (card_id, card_id)).fetchall()
        excluded = set()
        for row in rows:
            sid = row["sibling"]
            if sid not in self.reviewed_ids:
                excluded.add(sid)
            self.reviewed_ids.add(sid)
        return excluded

    def remaining_count(self) -> int:
        extra, params = self._filter_clause()
        return self.conn.execute(f"""
            SELECT COUNT(*) as cnt FROM cards c
            JOIN card_state cs ON c.id = cs.card_id
            LEFT JOIN recommendations r ON c.id = r.card_id
            WHERE cs.status = 'active' AND c.gradable = 1
              AND (r.time IS NULL OR r.time <= datetime('now')){extra}
        """, params).fetchone()["cnt"]

    def render_front(self, card: dict) -> str:
        adapter = self._get_adapter(card["adapter"])
        content = json.loads(card["content"])
        try:
            return adapter.render_front(content)
        except Exception as e:
            return f'<div style="color:var(--wrong)">Render error (card {card["id"]}): {e}</div>'


def _build_edit_command(settings, file_path, line=1):
    template = settings.get("edit_command")
    if template:
        return template.replace("{file}", shlex.quote(file_path)).replace("{line}", str(line))
    editor = os.environ.get("EDITOR", "vim")
    for term_cmd in ["kitty -e", "alacritty -e", "foot", "xterm -e"]:
        if shutil.which(term_cmd.split()[0]):
            return f"{term_cmd} {editor} +{line} {shlex.quote(file_path)}"
    return f"{editor} +{line} {shlex.quote(file_path)}"
