"""Unit tests for FSRS Phase 1 — field initialisation and shadow logging."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult, UnitType
from app.models.vocabulary import Vocabulary
from app.services.fsrs_service import (
    FSRS_DEFAULT_DIFFICULTY,
    FSRS_DEFAULT_STABILITY,
    FSRS_MAX_STABILITY,
    FSRS_REQUEST_RETENTION,
    _compute_initial_difficulty,
    _compute_initial_stability,
    apply_fsrs_scheduling,
    backfill_fsrs_fields,
    compute_next_interval_days,
    compute_retrievability,
    init_fsrs_fields,
    recall_result_to_fsrs_rating,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


_counter = 0


def _make_progress(
    db_session,
    *,
    confidence_score: float = 0.5,
    recall_fail_streak: int = 0,
    last_seen: datetime | None = None,
    next_review_at: datetime | None = None,
    fsrs_stability: float | None = None,
    fsrs_difficulty: float | None = None,
    fsrs_last_review: datetime | None = None,
) -> LearningProgress:
    global _counter
    _counter += 1
    uid = str(_counter)
    vocab = Vocabulary(user_key=f"u{uid}", name=f"v{uid}.pdf")
    db_session.add(vocab)
    db_session.flush()
    unit = LearningUnit(
        text=f"słowo{uid}",
        type=UnitType.WORD,
        translation=f"word{uid}",
        source_pdf=f"v{uid}.pdf",
        vocabulary_id=vocab.id,
        normalized_text=f"słowo{uid}",
        normalized_translation=f"word{uid}",
    )
    db_session.add(unit)
    db_session.flush()
    progress = LearningProgress(
        unit_id=unit.id,
        confidence_score=confidence_score,
        recall_fail_streak=recall_fail_streak,
        last_seen=last_seen,
        next_review_at=next_review_at,
        fsrs_stability=fsrs_stability,
        fsrs_difficulty=fsrs_difficulty,
        fsrs_last_review=fsrs_last_review,
    )
    db_session.add(progress)
    db_session.commit()
    db_session.refresh(progress)
    return progress


# ---------------------------------------------------------------------------
# _compute_initial_stability
# ---------------------------------------------------------------------------

class TestComputeInitialStability:
    def test_uses_interval_not_remaining_time(self):
        now = datetime(2026, 3, 31)
        p = LearningProgress(
            last_seen=now - timedelta(days=3),
            next_review_at=now + timedelta(days=4),  # 7-day interval
        )
        s = _compute_initial_stability(p)
        assert s == 7.0

    def test_fallback_when_last_seen_none(self):
        p = LearningProgress(last_seen=None, next_review_at=datetime(2026, 4, 1))
        assert _compute_initial_stability(p) == FSRS_DEFAULT_STABILITY

    def test_fallback_when_next_review_none(self):
        p = LearningProgress(last_seen=datetime(2026, 3, 28), next_review_at=None)
        assert _compute_initial_stability(p) == FSRS_DEFAULT_STABILITY

    def test_minimum_is_one(self):
        now = datetime(2026, 3, 31)
        # interval < 1 day
        p = LearningProgress(
            last_seen=now - timedelta(hours=6),
            next_review_at=now + timedelta(hours=6),
        )
        s = _compute_initial_stability(p)
        assert s == 1.0

    def test_capped_at_max_stability(self):
        now = datetime(2026, 3, 31)
        p = LearningProgress(
            last_seen=now - timedelta(days=5),
            next_review_at=now + timedelta(days=100),
        )
        s = _compute_initial_stability(p)
        assert s == FSRS_MAX_STABILITY


# ---------------------------------------------------------------------------
# _compute_initial_difficulty
# ---------------------------------------------------------------------------

class TestComputeInitialDifficulty:
    def test_high_confidence_low_difficulty(self):
        p = LearningProgress(confidence_score=1.0, recall_fail_streak=0)
        d = _compute_initial_difficulty(p)
        assert d == pytest.approx(3.0)  # 5.0 - 2.0 = 3.0

    def test_zero_confidence_mid_difficulty(self):
        p = LearningProgress(confidence_score=0.0, recall_fail_streak=0)
        d = _compute_initial_difficulty(p)
        assert d == pytest.approx(5.0)

    def test_fail_streak_increases_difficulty(self):
        p_no_streak = LearningProgress(confidence_score=0.5, recall_fail_streak=0)
        p_streak = LearningProgress(confidence_score=0.5, recall_fail_streak=5)
        assert _compute_initial_difficulty(p_streak) > _compute_initial_difficulty(p_no_streak)

    def test_clamped_at_minimum(self):
        p = LearningProgress(confidence_score=1.0, recall_fail_streak=0)
        # 5.0 - 2.0 = 3.0, not below 1.0
        assert _compute_initial_difficulty(p) >= 1.0

    def test_clamped_at_maximum(self):
        p = LearningProgress(confidence_score=0.0, recall_fail_streak=100)
        assert _compute_initial_difficulty(p) == 10.0

    def test_none_fields_treated_as_zero(self):
        p = LearningProgress(confidence_score=None, recall_fail_streak=None)
        d = _compute_initial_difficulty(p)
        assert 1.0 <= d <= 10.0


# ---------------------------------------------------------------------------
# init_fsrs_fields
# ---------------------------------------------------------------------------

class TestInitFsrsFields:
    def test_sets_all_three_fields(self):
        now = datetime(2026, 3, 28)
        p = LearningProgress(
            confidence_score=0.6,
            recall_fail_streak=1,
            last_seen=now,
            next_review_at=now + timedelta(days=7),
        )
        init_fsrs_fields(p)
        assert p.fsrs_stability is not None
        assert p.fsrs_difficulty is not None
        assert p.fsrs_last_review == now

    def test_idempotent_when_all_set(self):
        p = LearningProgress(
            confidence_score=0.9,
            recall_fail_streak=0,
            last_seen=datetime(2026, 3, 28),
            next_review_at=datetime(2026, 4, 4),
            fsrs_stability=99.0,
            fsrs_difficulty=99.0,
            fsrs_last_review=datetime(2026, 1, 1),
        )
        init_fsrs_fields(p)
        # Must not overwrite
        assert p.fsrs_stability == 99.0
        assert p.fsrs_difficulty == 99.0

    def test_new_word_no_timestamps_gets_defaults(self):
        p = LearningProgress(
            confidence_score=0.0,
            recall_fail_streak=0,
            last_seen=None,
            next_review_at=None,
        )
        init_fsrs_fields(p)
        assert p.fsrs_stability == FSRS_DEFAULT_STABILITY
        assert p.fsrs_difficulty == FSRS_DEFAULT_DIFFICULTY
        assert p.fsrs_last_review is None


# ---------------------------------------------------------------------------
# backfill_fsrs_fields
# ---------------------------------------------------------------------------

class TestBackfillFsrsFields:
    def test_populates_all_rows(self, db_session):
        now = datetime(2026, 3, 28)
        for i in range(3):
            _make_progress(
                db_session,
                last_seen=now,
                next_review_at=now + timedelta(days=7),
            )
        result = backfill_fsrs_fields(db_session)
        assert result["populated"] == 3
        assert result["skipped"] == 0
        assert result["total"] == 3

    def test_skips_already_populated_rows(self, db_session):
        now = datetime(2026, 3, 28)
        _make_progress(
            db_session,
            last_seen=now,
            next_review_at=now + timedelta(days=7),
            fsrs_stability=5.0,
            fsrs_difficulty=4.0,
            fsrs_last_review=now,
        )
        result = backfill_fsrs_fields(db_session)
        assert result["skipped"] == 1
        assert result["populated"] == 0

    def test_idempotent_second_run(self, db_session):
        now = datetime(2026, 3, 28)
        _make_progress(db_session, last_seen=now, next_review_at=now + timedelta(days=3))
        backfill_fsrs_fields(db_session)
        result2 = backfill_fsrs_fields(db_session)
        assert result2["skipped"] == 1
        assert result2["populated"] == 0

    def test_values_written_to_db(self, db_session):
        now = datetime(2026, 3, 28)
        p = _make_progress(db_session, last_seen=now, next_review_at=now + timedelta(days=7))
        backfill_fsrs_fields(db_session)
        db_session.refresh(p)
        assert p.fsrs_stability == 7.0
        assert p.fsrs_last_review == now


# ---------------------------------------------------------------------------
# recall_result_to_fsrs_rating
# ---------------------------------------------------------------------------

class TestRecallResultToFsrsRating:
    def test_failed_is_1(self):
        assert recall_result_to_fsrs_rating(RecallResult.FAILED, 0.9) == 1

    def test_partial_is_2(self):
        assert recall_result_to_fsrs_rating(RecallResult.PARTIAL, 0.9) == 2

    def test_correct_low_confidence_is_3(self):
        assert recall_result_to_fsrs_rating(RecallResult.CORRECT, 0.7) == 3

    def test_correct_high_confidence_is_4(self):
        assert recall_result_to_fsrs_rating(RecallResult.CORRECT, 0.85) == 4

    def test_correct_exactly_at_threshold_is_4(self):
        assert recall_result_to_fsrs_rating(RecallResult.CORRECT, 0.85) == 4

    def test_correct_just_below_threshold_is_3(self):
        assert recall_result_to_fsrs_rating(RecallResult.CORRECT, 0.849) == 3


# ---------------------------------------------------------------------------
# compute_retrievability / compute_next_interval_days
# ---------------------------------------------------------------------------

class TestFsrsMath:
    def test_retrievability_at_zero_elapsed(self):
        r = compute_retrievability(stability=7.0, days_elapsed=0)
        assert r == pytest.approx(1.0)

    def test_retrievability_decreases_with_time(self):
        r0 = compute_retrievability(stability=7.0, days_elapsed=0)
        r7 = compute_retrievability(stability=7.0, days_elapsed=7)
        assert r7 < r0

    def test_retrievability_at_stability_is_above_90pct(self):
        # At t=S, R should be close to 90% (FSRS design target)
        r = compute_retrievability(stability=7.0, days_elapsed=7.0)
        assert r == pytest.approx(0.9, abs=0.01)

    def test_next_interval_targets_request_retention(self):
        # Inverting should give back ~stability
        interval = compute_next_interval_days(stability=7.0)
        r = compute_retrievability(stability=7.0, days_elapsed=interval)
        assert r == pytest.approx(FSRS_REQUEST_RETENTION, abs=0.001)

    def test_next_interval_minimum_is_one_day(self):
        assert compute_next_interval_days(stability=0.001) == 1.0

    def test_zero_stability_safe(self):
        assert compute_retrievability(stability=0, days_elapsed=5) == 0.0
        assert compute_next_interval_days(stability=0) == 1.0


class TestApplyFsrsSchedulingFallback:
    def test_missing_fsrs_package_uses_builtin_scheduler(self, db_session, monkeypatch):
        now = datetime(2026, 4, 5, 9, 0, 0)
        progress = _make_progress(
            db_session,
            last_seen=now - timedelta(days=5),
            next_review_at=now,
            fsrs_stability=5.0,
            fsrs_difficulty=5.0,
            fsrs_last_review=now - timedelta(days=5),
        )

        def _missing():
            raise ModuleNotFoundError("No module named 'fsrs'")

        monkeypatch.setattr("app.services.fsrs_service._load_fsrs_components", _missing)

        due = apply_fsrs_scheduling(progress, fsrs_rating=4, prev_last_seen=progress.last_seen, now=now)

        assert due > now + timedelta(days=1)
        assert progress.fsrs_stability > 5.0
        assert progress.fsrs_difficulty < 5.0
        assert progress.fsrs_last_review == now

    def test_failed_rating_still_returns_one_day_in_builtin_scheduler(self, db_session, monkeypatch):
        now = datetime(2026, 4, 5, 9, 0, 0)
        progress = _make_progress(
            db_session,
            last_seen=now - timedelta(days=3),
            next_review_at=now,
            fsrs_stability=4.0,
            fsrs_difficulty=4.0,
            fsrs_last_review=now - timedelta(days=3),
        )

        def _missing():
            raise ModuleNotFoundError("No module named 'fsrs'")

        monkeypatch.setattr("app.services.fsrs_service._load_fsrs_components", _missing)

        due = apply_fsrs_scheduling(progress, fsrs_rating=1, prev_last_seen=progress.last_seen, now=now)

        assert due == now + timedelta(days=1)
        assert progress.fsrs_stability < 4.0
        assert progress.fsrs_last_review == now
