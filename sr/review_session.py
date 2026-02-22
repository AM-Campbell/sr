"""ReviewSession: manages card review state independent of HTTP."""

import json
import os
import shlex
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone

from datetime import timedelta

from sr.adapters import load_adapter
from sr.flags import add_flag, get_flags, remove_flag
from sr.models import Recommendation, ReviewEvent
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
        self.skipped = 0
        self.suspended = 0
        self.skipped_ids: set[int] = set()
        self.excluded_count = 0
        self.reviewed_ids: set[int] = set()
        # Temp table for reviewed IDs — avoids unbounded NOT IN (?, ?, ...) clauses
        self._reviewed_table = f"_reviewed_{uuid.uuid4().hex[:8]}"
        self.conn.execute(f"CREATE TEMP TABLE {self._reviewed_table} (card_id INTEGER PRIMARY KEY)")
        self.initial_total = self.remaining_count()

    def _mark_reviewed(self, card_id: int):
        """Add a card ID to the reviewed set and temp table."""
        if card_id not in self.reviewed_ids:
            self.reviewed_ids.add(card_id)
            self.conn.execute(f"INSERT OR IGNORE INTO {self._reviewed_table} VALUES (?)", (card_id,))

    def _unmark_reviewed(self, card_id: int):
        """Remove a card ID from the reviewed set and temp table."""
        self.reviewed_ids.discard(card_id)
        self.conn.execute(f"DELETE FROM {self._reviewed_table} WHERE card_id = ?", (card_id,))

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
            clauses.append(f"c.id NOT IN (SELECT card_id FROM {self._reviewed_table})")
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
            ORDER BY CASE WHEN r.time IS NULL THEN 1 ELSE 0 END, r.time ASC, RANDOM()
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

        # Save old recommendation and scheduler state before scheduler overwrites them
        old_rec_row = self.conn.execute(
            "SELECT * FROM recommendations WHERE card_id=?", (card_id,)).fetchone()
        old_rec = dict(old_rec_row) if old_rec_row else None
        old_sched_state = None
        if self.scheduler and hasattr(self.scheduler, 'get_card_state'):
            old_sched_state = self.scheduler.get_card_state(card_id)

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

        # Check if the scheduler wants to show this card again soon
        # (learning steps, relearning). If so, keep it out of reviewed_ids
        # so it reappears when its recommendation time arrives.
        new_rec = self.conn.execute(
            "SELECT time FROM recommendations WHERE card_id=?", (card_id,)).fetchone()
        restudy_soon = False
        if new_rec and new_rec["time"]:
            try:
                rec_time = datetime.strptime(new_rec["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                minutes_away = (rec_time - datetime.now(timezone.utc)).total_seconds() / 60
                restudy_soon = minutes_away < 30
            except (ValueError, TypeError):
                pass

        # Exclude mutually exclusive siblings regardless
        self._mark_reviewed(card_id)
        excluded = self._exclude_mutually_exclusive(card_id)
        if restudy_soon:
            # Let the card come back when its recommendation is due
            self._unmark_reviewed(card_id)
        self.undo_stack.append({"card": self.current_card, "excluded_ids": excluded,
                                "old_rec": old_rec, "old_sched_state": old_sched_state,
                                "grade": grade})
        self.reviewed += 1
        self.current_card = None

    def skip_current(self):
        """Skip the current card, rescheduling it to tomorrow."""
        if not self.current_card:
            raise ValueError("No current card")
        card_id = self.current_card["id"]
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        rec = Recommendation(card_id=card_id, time=tomorrow, precision_seconds=3600)
        if self.scheduler:
            _upsert_recommendation(self.conn, rec, self.scheduler)
        else:
            self.conn.execute("""
                INSERT OR REPLACE INTO recommendations (card_id, scheduler_id, time, precision_seconds)
                VALUES (?, 'manual', ?, ?)
            """, (card_id, tomorrow, 3600))
        self.conn.commit()
        self._mark_reviewed(card_id)
        self.skipped_ids.add(card_id)
        excluded = self._exclude_mutually_exclusive(card_id)
        self.undo_stack.append({"card": self.current_card, "excluded_ids": excluded, "was_skip": True})
        self.skipped += 1
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
            self._mark_reviewed(sid)
        self.excluded_count += len(excluded)
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
