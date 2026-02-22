"""Tests for the SM-2 scheduler algorithm with learning steps."""

from sr.models import ReviewEvent


def _make_event(card_id, timestamp, grade=1, feedback=None):
    return ReviewEvent(card_id=card_id, timestamp=timestamp, grade=grade,
                       time_on_front_ms=1000, time_on_card_ms=2000,
                       feedback=feedback, response=None)


def _graduate(sched, card_id):
    """Run a card through all learning steps to graduate it."""
    sched.on_card_created(card_id)
    # Step 0 -> Step 1 (1 min -> 10 min)
    sched.on_review(card_id, _make_event(card_id, "2025-01-01 00:00:00"))
    # Step 1 -> Graduate (10 min -> 1 day)
    sched.on_review(card_id, _make_event(card_id, "2025-01-01 00:10:00"))


# ── Learning steps ────────────────────────────────────────────

def test_new_card_starts_learning(sample_scheduler):
    """A new card starts at learning step 0."""
    sched = sample_scheduler
    sched.on_card_created(1)
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["learning_step"] == 0
    assert row["repetitions"] == 0


def test_learning_step_advance(sample_scheduler):
    """First correct advances from step 0 to step 1."""
    sched = sample_scheduler
    sched.on_card_created(1)
    sched.on_review(1, _make_event(1, "2025-01-01 00:00:00"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["learning_step"] == 1
    assert row["repetitions"] == 0
    # Interval should be ~10 minutes (10/1440 days)
    assert abs(row["interval_days"] - 10 / 1440) < 0.001


def test_learning_graduation(sample_scheduler):
    """Second correct graduates the card (learning_step becomes None)."""
    sched = sample_scheduler
    _graduate(sched, 1)
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["learning_step"] is None
    assert row["repetitions"] == 1
    assert row["interval_days"] == 1  # graduating interval


def test_learning_again_resets_to_step_0(sample_scheduler):
    """Wrong answer during learning resets to step 0."""
    sched = sample_scheduler
    sched.on_card_created(1)
    # Advance to step 1
    sched.on_review(1, _make_event(1, "2025-01-01 00:00:00"))
    # Wrong answer
    sched.on_review(1, _make_event(1, "2025-01-01 00:10:00", grade=0))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["learning_step"] == 0
    # Interval should be ~1 minute (1/1440 days)
    assert abs(row["interval_days"] - 1 / 1440) < 0.001


# ── Graduated card behavior (SM-2) ───────────────────────────

def test_graduated_rep2(sample_scheduler):
    """After graduation, next correct gives interval=6."""
    sched = sample_scheduler
    _graduate(sched, 1)
    sched.on_review(1, _make_event(1, "2025-01-02 00:00:00"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["repetitions"] == 2
    assert row["interval_days"] == 6


def test_graduated_rep3(sample_scheduler):
    """Third rep uses interval * ease_factor."""
    sched = sample_scheduler
    _graduate(sched, 1)
    sched.on_review(1, _make_event(1, "2025-01-02 00:00:00"))
    sched.on_review(1, _make_event(1, "2025-01-08 00:00:00"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["repetitions"] == 3
    assert row["interval_days"] == 6 * 2.5  # 15.0


def test_feedback_adjusts_ef(sample_scheduler):
    """Feedback on graduated cards adjusts ease factor."""
    sched = sample_scheduler
    _graduate(sched, 1)
    sched.on_review(1, _make_event(1, "2025-01-02", feedback="too_easy"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["ease_factor"] == 2.65  # 2.5 + 0.15


def test_feedback_too_hard(sample_scheduler):
    sched = sample_scheduler
    _graduate(sched, 1)
    sched.on_review(1, _make_event(1, "2025-01-02", feedback="too_hard"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["ease_factor"] == 2.35  # 2.5 - 0.15


def test_feedback_during_learning_ignored(sample_scheduler):
    """Feedback doesn't change ease factor during learning steps."""
    sched = sample_scheduler
    sched.on_card_created(1)
    sched.on_review(1, _make_event(1, "2025-01-01 00:00:00", feedback="too_easy"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["ease_factor"] == 2.5  # unchanged


# ── Relearning (lapsed graduated card) ────────────────────────

def test_lapse_enters_relearning(sample_scheduler):
    """Wrong answer on graduated card enters relearning."""
    sched = sample_scheduler
    _graduate(sched, 1)
    sched.on_review(1, _make_event(1, "2025-01-02", grade=0))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["learning_step"] == 0
    assert row["ease_factor"] < 2.5  # penalized
    # Interval should be 10 minutes (relearning step)
    assert abs(row["interval_days"] - 10 / 1440) < 0.001


def test_relearning_graduation(sample_scheduler):
    """Correct answer in relearning graduates back to review."""
    sched = sample_scheduler
    _graduate(sched, 1)
    # Lapse
    sched.on_review(1, _make_event(1, "2025-01-02 00:00:00", grade=0))
    # Complete relearning (only 1 step)
    sched.on_review(1, _make_event(1, "2025-01-02 00:10:00"))
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=1").fetchone()
    assert row["learning_step"] is None
    assert row["interval_days"] >= 1  # min lapse interval


# ── Other ─────────────────────────────────────────────────────

def test_card_replacement(sample_scheduler):
    sched = sample_scheduler
    _graduate(sched, 1)
    sched.on_review(1, _make_event(1, "2025-01-02"))

    rec = sched.on_card_replaced(1, 2)
    assert rec is not None
    assert rec.card_id == 2
    row = sched.conn.execute("SELECT * FROM sm2_state WHERE card_id=2").fetchone()
    assert row is not None
    assert row["ease_factor"] == 2.5


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
