"""SM-2 Scheduler — SuperMemo 2 algorithm implementation.

Maintains per-card state: ease factor, interval, repetition count.
Uses its own SQLite database for scheduling state.
"""

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone


@dataclasses.dataclass
class Recommendation:
    card_id: int
    time: str
    precision_seconds: int


@dataclasses.dataclass
class ReviewEvent:
    card_id: int
    timestamp: str
    grade: int
    time_on_front_ms: int
    time_on_card_ms: int
    feedback: str | None
    response: dict | None


SM2_SCHEMA = """
CREATE TABLE IF NOT EXISTS sm2_state (
    card_id INTEGER PRIMARY KEY,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    interval_days REAL NOT NULL DEFAULT 0,
    repetitions INTEGER NOT NULL DEFAULT 0,
    last_review TEXT,
    next_review TEXT
);
"""


class Scheduler:
    scheduler_id = "sm2"

    def __init__(self, db_dir: str, core_db_path: str):
        self.db_dir = db_dir
        self.core_db_path = core_db_path
        db_path = f"{db_dir}/sm2.db"
        self.conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SM2_SCHEMA)
        self.conn.commit()

    def on_review(self, card_id: int, event: ReviewEvent) -> list[Recommendation]:
        """Process a review and update scheduling state."""
        row = self.conn.execute(
            "SELECT * FROM sm2_state WHERE card_id = ?", (card_id,)
        ).fetchone()

        if row:
            ef = row["ease_factor"]
            interval = row["interval_days"]
            reps = row["repetitions"]
        else:
            ef = 2.5
            interval = 0
            reps = 0

        if event.grade == 1:  # correct
            reps += 1
            if reps == 1:
                interval = 1
            elif reps == 2:
                interval = 6
            else:
                interval = interval * ef
            # Adjust ease factor based on feedback
            if event.feedback == "too_easy":
                ef = min(ef + 0.15, 3.0)
            elif event.feedback == "too_hard":
                ef = max(ef - 0.15, 1.3)
        else:  # incorrect
            reps = 0
            interval = 0.01  # ~15 minutes (fraction of a day)
            ef = max(ef - 0.2, 1.3)

        now = datetime.now(timezone.utc)
        next_review = now + timedelta(days=interval)
        next_review_str = next_review.strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("""
            INSERT OR REPLACE INTO sm2_state (card_id, ease_factor, interval_days, repetitions, last_review, next_review)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (card_id, ef, interval, reps, event.timestamp, next_review_str))
        self.conn.commit()

        precision = max(int(interval * 86400 * 0.1), 60)  # 10% of interval, min 60s
        return [Recommendation(card_id=card_id, time=next_review_str,
                               precision_seconds=precision)]

    def on_card_created(self, card_id: int) -> Recommendation | None:
        """New card — schedule for immediate review."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("""
            INSERT OR REPLACE INTO sm2_state (card_id, ease_factor, interval_days, repetitions)
            VALUES (?, 2.5, 0, 0)
        """, (card_id,))
        self.conn.commit()
        return Recommendation(card_id=card_id, time=now, precision_seconds=60)

    def on_card_replaced(self, old_card_id: int, new_card_id: int) -> Recommendation | None:
        """Migrate scheduling state from old card to new card."""
        row = self.conn.execute(
            "SELECT * FROM sm2_state WHERE card_id = ?", (old_card_id,)
        ).fetchone()

        if row:
            # Keep ease factor and some state, but reduce interval slightly
            ef = row["ease_factor"]
            interval = max(row["interval_days"] * 0.7, 1)  # reduce by 30%
            reps = max(row["repetitions"] - 1, 0)
            now = datetime.now(timezone.utc)
            next_review = now + timedelta(days=interval)
            next_review_str = next_review.strftime("%Y-%m-%d %H:%M:%S")

            self.conn.execute("""
                INSERT OR REPLACE INTO sm2_state (card_id, ease_factor, interval_days, repetitions, next_review)
                VALUES (?, ?, ?, ?, ?)
            """, (new_card_id, ef, interval, reps, next_review_str))
            self.conn.commit()

            precision = max(int(interval * 86400 * 0.1), 60)
            return Recommendation(card_id=new_card_id, time=next_review_str,
                                  precision_seconds=precision)
        else:
            return self.on_card_created(new_card_id)

    def on_card_status_changed(self, card_id: int, status: str) -> None:
        """Card status changed. Remove state for deleted cards."""
        if status == "deleted":
            self.conn.execute("DELETE FROM sm2_state WHERE card_id = ?", (card_id,))
            self.conn.commit()

    def on_relations_changed(self, card_ids: list[int]) -> list[Recommendation]:
        """Relations changed — no special handling in SM-2."""
        return []

    def compute_all(self, active_card_ids: list[int]) -> list[Recommendation]:
        """Full recompute of recommendations for all active cards."""
        recs = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for card_id in active_card_ids:
            row = self.conn.execute(
                "SELECT * FROM sm2_state WHERE card_id = ?", (card_id,)
            ).fetchone()
            if row and row["next_review"]:
                precision = max(int(row["interval_days"] * 86400 * 0.1), 60)
                recs.append(Recommendation(
                    card_id=card_id, time=row["next_review"],
                    precision_seconds=precision))
            else:
                recs.append(Recommendation(card_id=card_id, time=now,
                                           precision_seconds=60))
        return recs
