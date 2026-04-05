"""Tests for habit engine: streak break detection and previous streak computation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.learning_unit import RecallResult
from app.services.progress_metrics_service import (
    compute_learning_streak,
    compute_previous_streak,
)


def _progress(last_seen: datetime, correct: bool = True):
    return SimpleNamespace(
        last_recall_result=RecallResult.CORRECT if correct else RecallResult.FAILED,
        times_correct=1 if correct else 0,
        last_seen=last_seen,
    )


TODAY = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
YESTERDAY = TODAY - timedelta(days=1)
DAY2 = TODAY - timedelta(days=2)
DAY3 = TODAY - timedelta(days=3)
DAY4 = TODAY - timedelta(days=4)


# ── compute_previous_streak ────────────────────────────────────────────────

class TestComputePreviousStreak:
    def test_returns_zero_when_no_activity(self):
        assert compute_previous_streak([], today=TODAY) == 0

    def test_returns_zero_when_only_today_activity(self):
        rows = [_progress(TODAY)]
        assert compute_previous_streak(rows, today=TODAY) == 0

    def test_returns_one_for_single_day_yesterday(self):
        rows = [_progress(YESTERDAY)]
        assert compute_previous_streak(rows, today=TODAY) == 1

    def test_returns_correct_streak_over_multiple_days(self):
        rows = [_progress(YESTERDAY), _progress(DAY2), _progress(DAY3)]
        assert compute_previous_streak(rows, today=TODAY) == 3

    def test_streak_stops_at_gap(self):
        # Yesterday and 3 days ago but not 2 days ago
        rows = [_progress(YESTERDAY), _progress(DAY3)]
        assert compute_previous_streak(rows, today=TODAY) == 1

    def test_ignores_failed_recall(self):
        rows = [_progress(YESTERDAY, correct=False)]
        assert compute_previous_streak(rows, today=TODAY) == 0

    def test_ignores_none_rows(self):
        rows = [None, _progress(YESTERDAY), None]
        assert compute_previous_streak(rows, today=TODAY) == 1

    def test_today_activity_does_not_extend_yesterday_streak(self):
        # Streak is yesterday+today but previous streak (ending yesterday) is just yesterday
        rows = [_progress(TODAY), _progress(YESTERDAY)]
        # previous_streak looks at yesterday backwards — yesterday is there, so streak >= 1
        result = compute_previous_streak(rows, today=TODAY)
        assert result == 1

    def test_five_day_streak_ending_yesterday(self):
        rows = [_progress(YESTERDAY - timedelta(days=i)) for i in range(5)]
        assert compute_previous_streak(rows, today=TODAY) == 5


# ── Streak break detection logic ───────────────────────────────────────────

class TestStreakBreakDetection:
    """
    Test the combined logic: if today streak == 0 and previous streak >= 2,
    show break message. This is the rule used in get_learning_snapshot().
    """

    def _streak_broken_days(self, progress_rows, today=TODAY):
        streak_today = compute_learning_streak(progress_rows, today=today)
        if streak_today == 0:
            prev = compute_previous_streak(progress_rows, today=today)
            return prev if prev >= 2 else 0
        return 0

    def test_no_break_when_streak_active_today(self):
        rows = [_progress(TODAY), _progress(YESTERDAY)]
        assert self._streak_broken_days(rows) == 0

    def test_no_break_when_streak_is_only_one_day(self):
        # Yesterday had 1 day streak — not meaningful enough to show break
        rows = [_progress(YESTERDAY)]
        assert self._streak_broken_days(rows) == 0

    def test_break_detected_for_two_day_streak(self):
        rows = [_progress(YESTERDAY), _progress(DAY2)]
        assert self._streak_broken_days(rows) == 2

    def test_break_detected_for_seven_day_streak(self):
        rows = [_progress(YESTERDAY - timedelta(days=i)) for i in range(7)]
        assert self._streak_broken_days(rows) == 7

    def test_no_break_when_no_activity_at_all(self):
        assert self._streak_broken_days([]) == 0

    def test_no_break_message_repeated_next_day(self):
        # If user already practiced yesterday but nothing before, prev streak is 1 → no break
        rows = [_progress(YESTERDAY)]
        assert self._streak_broken_days(rows) == 0

    def test_break_reported_correctly_after_gap(self):
        # 5-day streak ending 2 days ago (yesterday has no activity)
        rows = [_progress(DAY2 - timedelta(days=i)) for i in range(5)]
        # Yesterday has no activity → previous_streak would check yesterday → not in dates → 0
        # So no break should fire (the streak already broke before yesterday)
        result = self._streak_broken_days(rows)
        # No break: yesterday not in learned_dates so previous streak = 0
        assert result == 0

    def test_threshold_boundary_exactly_two(self):
        rows = [_progress(YESTERDAY), _progress(DAY2)]
        assert self._streak_broken_days(rows) == 2


# ── Regression: compute_learning_streak unaffected ────────────────────────

class TestComputeStreakUnaffected:
    def test_existing_streak_still_works(self):
        rows = [_progress(TODAY), _progress(YESTERDAY), _progress(DAY2)]
        assert compute_learning_streak(rows, today=TODAY) == 3

    def test_zero_when_no_today_activity(self):
        rows = [_progress(YESTERDAY), _progress(DAY2)]
        assert compute_learning_streak(rows, today=TODAY) == 0
