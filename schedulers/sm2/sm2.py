"""SM-2 Scheduler — SuperMemo 2 algorithm with Anki-style learning steps.

Maintains per-card state: ease factor, interval, repetition count, learning step.
Uses its own SQLite database for scheduling state.

Learning flow:
  New card -> learning steps (1m, 10m) -> graduated (1 day, then SM-2)
  Lapsed review card -> relearning step (10m) -> review (min 1 day)
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from sr.models import Recommendation, ReviewEvent


# Learning steps in fractional days
LEARNING_STEPS = [1 / 1440, 10 / 1440]  # 1 minute, 10 minutes
RELEARNING_STEPS = [10 / 1440]           # 10 minutes
GRADUATING_INTERVAL = 1                   # 1 day
EASY_INTERVAL = 4                         # 4 days
MIN_LAPSE_INTERVAL = 1                    # 1 day after relearning


SM2_SCHEMA = """
CREATE TABLE IF NOT EXISTS sm2_state (
    card_id INTEGER PRIMARY KEY,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    interval_days REAL NOT NULL DEFAULT 0,
    repetitions INTEGER NOT NULL DEFAULT 0,
    learning_step INTEGER,
    last_review TEXT,
    next_review TEXT
);
"""

SM2_MIGRATIONS = [
    # Add learning_step column if missing (upgrade from old schema)
    """
    ALTER TABLE sm2_state ADD COLUMN learning_step INTEGER;
    """,
]


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
        self._migrate()

    def _migrate(self):
        """Apply schema migrations if needed."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sm2_state)")}
        if "learning_step" not in cols:
            for sql in SM2_MIGRATIONS:
                try:
                    self.conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
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
            learning_step = row["learning_step"]
        else:
            ef = 2.5
            interval = 0
            reps = 0
            learning_step = 0  # new card starts at step 0

        is_learning = learning_step is not None

        if is_learning:
            interval, learning_step, ef = self._process_learning(
                event, learning_step, ef, reps)
            if learning_step is None:
                # Graduated — set initial reps
                reps = 1
            else:
                reps = 0
        else:
            # Graduated card — normal SM-2
            if event.grade == 1:  # correct
                reps += 1
                if reps == 1:
                    interval = GRADUATING_INTERVAL
                elif reps == 2:
                    interval = 6
                else:
                    interval = interval * ef
                # Adjust ease factor based on feedback
                if event.feedback == "too_easy":
                    ef = min(ef + 0.15, 3.0)
                elif event.feedback == "too_hard":
                    ef = max(ef - 0.15, 1.3)
            else:  # lapse — enter relearning
                ef = max(ef - 0.2, 1.3)
                learning_step = 0
                interval = RELEARNING_STEPS[0]

        now = datetime.now(timezone.utc)
        next_review = now + timedelta(days=interval)
        next_review_str = next_review.strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("""
            INSERT OR REPLACE INTO sm2_state
            (card_id, ease_factor, interval_days, repetitions, learning_step, last_review, next_review)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (card_id, ef, interval, reps, learning_step,
              event.timestamp, next_review_str))
        self.conn.commit()

        precision = max(int(interval * 86400 * 0.1), 60)  # 10% of interval, min 60s
        return [Recommendation(card_id=card_id, time=next_review_str,
                               precision_seconds=precision)]

    def _process_learning(self, event, step, ef, reps):
        """Handle a review for a card in learning/relearning.

        Returns (interval, learning_step, ef).
        learning_step=None means the card has graduated.
        """
        # Determine which step list we're using
        is_relearning = reps > 0
        steps = RELEARNING_STEPS if is_relearning else LEARNING_STEPS

        if event.grade == 1:  # correct — advance to next step
            next_step = step + 1
            if next_step >= len(steps):
                # Graduate
                if is_relearning:
                    interval = max(MIN_LAPSE_INTERVAL, 1)
                else:
                    interval = GRADUATING_INTERVAL
                return interval, None, ef
            else:
                return steps[next_step], next_step, ef
        else:  # wrong — back to step 0
            return steps[0], 0, ef

    def on_card_created(self, card_id: int) -> Recommendation | None:
        """New card — schedule for immediate review (learning step 0)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("""
            INSERT OR REPLACE INTO sm2_state
            (card_id, ease_factor, interval_days, repetitions, learning_step)
            VALUES (?, 2.5, 0, 0, 0)
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
                INSERT OR REPLACE INTO sm2_state
                (card_id, ease_factor, interval_days, repetitions, learning_step, next_review)
                VALUES (?, ?, ?, ?, NULL, ?)
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

    def get_card_state(self, card_id: int) -> dict | None:
        """Snapshot the scheduler's internal state for a card (for undo)."""
        row = self.conn.execute(
            "SELECT * FROM sm2_state WHERE card_id = ?", (card_id,)).fetchone()
        return dict(row) if row else None

    def restore_card_state(self, card_id: int, state: dict | None) -> None:
        """Restore a previously snapshotted scheduler state (for undo)."""
        if state is None:
            self.conn.execute("DELETE FROM sm2_state WHERE card_id = ?", (card_id,))
        else:
            self.conn.execute("""
                INSERT OR REPLACE INTO sm2_state
                (card_id, ease_factor, interval_days, repetitions, learning_step, last_review, next_review)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (state["card_id"], state["ease_factor"], state["interval_days"],
                  state["repetitions"], state["learning_step"],
                  state.get("last_review"), state.get("next_review")))
        self.conn.commit()

    def close(self):
        """Close the scheduler's database connection."""
        self.conn.close()

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
