"""Tests for the SM-2 scheduler algorithm."""

from sr.models import ReviewEvent


def test_correct_grade_rep1(sample_scheduler):
    sched = sample_scheduler
    rec = sched.on_card_created(1)
    assert rec is not None

    event = ReviewEvent(card_id=1, timestamp="2025-01-01 00:00:00", grade=1,
                        time_on_front_ms=1000, time_on_card_ms=2000,
                        feedback=None, response=None)
    recs = sched.on_review(1, event)
    assert len(recs) == 1
    # After first correct: interval = 1 day
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["repetitions"] == 1
    assert row["interval_days"] == 1


def test_correct_grade_rep2(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)

    for i in range(2):
        event = ReviewEvent(card_id=1, timestamp=f"2025-01-0{i+1} 00:00:00", grade=1,
                            time_on_front_ms=1000, time_on_card_ms=2000,
                            feedback=None, response=None)
        sched.on_review(1, event)

    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["repetitions"] == 2
    assert row["interval_days"] == 6


def test_correct_grade_rep3(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)

    for i in range(3):
        event = ReviewEvent(card_id=1, timestamp=f"2025-01-0{i+1} 00:00:00", grade=1,
                            time_on_front_ms=1000, time_on_card_ms=2000,
                            feedback=None, response=None)
        sched.on_review(1, event)

    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["repetitions"] == 3
    assert row["interval_days"] == 6 * 2.5  # 15.0


def test_incorrect_grade_resets(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)

    # First correct
    event = ReviewEvent(card_id=1, timestamp="2025-01-01", grade=1,
                        time_on_front_ms=1000, time_on_card_ms=2000,
                        feedback=None, response=None)
    sched.on_review(1, event)

    # Then incorrect
    event = ReviewEvent(card_id=1, timestamp="2025-01-02", grade=0,
                        time_on_front_ms=1000, time_on_card_ms=2000,
                        feedback=None, response=None)
    sched.on_review(1, event)

    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["repetitions"] == 0
    assert row["interval_days"] == 0.01  # ~15 min
    assert row["ease_factor"] < 2.5


def test_feedback_adjusts_ef(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)

    event = ReviewEvent(card_id=1, timestamp="2025-01-01", grade=1,
                        time_on_front_ms=1000, time_on_card_ms=2000,
                        feedback="too_easy", response=None)
    sched.on_review(1, event)

    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["ease_factor"] == 2.65  # 2.5 + 0.15


def test_feedback_too_hard(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)

    event = ReviewEvent(card_id=1, timestamp="2025-01-01", grade=1,
                        time_on_front_ms=1000, time_on_card_ms=2000,
                        feedback="too_hard", response=None)
    sched.on_review(1, event)

    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["ease_factor"] == 2.35  # 2.5 - 0.15


def test_card_replacement(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)

    # Review the original card
    event = ReviewEvent(card_id=1, timestamp="2025-01-01", grade=1,
                        time_on_front_ms=1000, time_on_card_ms=2000,
                        feedback=None, response=None)
    sched.on_review(1, event)

    # Replace
    rec = sched.on_card_replaced(1, 2)
    assert rec is not None
    assert rec.card_id == 2

    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=2").fetchone()
    assert row is not None
    assert row["ease_factor"] == 2.5  # Preserved


def test_card_created_scheduling(sample_scheduler):
    rec = sample_scheduler.on_card_created(42)
    assert rec.card_id == 42
    assert rec.precision_seconds == 60


def test_card_status_deleted(sample_scheduler):
    sched = sample_scheduler
    sched.on_card_created(1)
    sched.on_card_status_changed(1, "deleted")
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row is None
