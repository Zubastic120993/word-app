"""Tests for compute_review_forecast — SRS review count per day for 14 days."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.progress_metrics_service import compute_review_forecast


def _progress(next_review_at):
    return SimpleNamespace(next_review_at=next_review_at)


TODAY = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
TOMORROW = TODAY + timedelta(days=1)
DAY2 = TODAY + timedelta(days=2)
DAY13 = TODAY + timedelta(days=13)   # last day in window (14-day inclusive)
DAY14 = TODAY + timedelta(days=14)   # first day OUTSIDE window
YESTERDAY = TODAY - timedelta(days=1)


class TestComputeReviewForecast:
    def test_returns_14_entries(self):
        result = compute_review_forecast([], now=TODAY)
        assert len(result) == 14

    def test_all_zero_when_no_rows(self):
        result = compute_review_forecast([], now=TODAY)
        assert all(d["count"] == 0 for d in result)

    def test_first_entry_is_today(self):
        result = compute_review_forecast([], now=TODAY)
        assert result[0]["date"] == TODAY.date().isoformat()

    def test_last_entry_is_day13(self):
        result = compute_review_forecast([], now=TODAY)
        assert result[-1]["date"] == DAY13.date().isoformat()

    def test_counts_row_due_today(self):
        rows = [_progress(TODAY)]
        result = compute_review_forecast(rows, now=TODAY)
        assert result[0]["count"] == 1

    def test_counts_row_due_tomorrow(self):
        rows = [_progress(TOMORROW)]
        result = compute_review_forecast(rows, now=TODAY)
        assert result[0]["count"] == 0
        assert result[1]["count"] == 1

    def test_counts_row_due_last_day_in_window(self):
        rows = [_progress(DAY13)]
        result = compute_review_forecast(rows, now=TODAY)
        assert result[13]["count"] == 1

    def test_excludes_row_outside_window(self):
        rows = [_progress(DAY14)]
        result = compute_review_forecast(rows, now=TODAY)
        assert all(d["count"] == 0 for d in result)

    def test_excludes_past_row(self):
        rows = [_progress(YESTERDAY)]
        result = compute_review_forecast(rows, now=TODAY)
        assert all(d["count"] == 0 for d in result)

    def test_aggregates_multiple_rows_same_day(self):
        rows = [_progress(TOMORROW), _progress(TOMORROW), _progress(TOMORROW)]
        result = compute_review_forecast(rows, now=TODAY)
        assert result[1]["count"] == 3

    def test_skips_none_rows(self):
        rows = [None, _progress(TODAY), None]
        result = compute_review_forecast(rows, now=TODAY)
        assert result[0]["count"] == 1

    def test_skips_row_with_none_next_review_at(self):
        rows = [_progress(None)]
        result = compute_review_forecast(rows, now=TODAY)
        assert all(d["count"] == 0 for d in result)

    def test_label_is_short_weekday(self):
        result = compute_review_forecast([], now=TODAY)
        # 2025-06-15 is a Sunday
        assert result[0]["label"] == "Sun"
        assert result[1]["label"] == "Mon"

    def test_multiple_days_counted_independently(self):
        rows = [_progress(TODAY), _progress(TOMORROW), _progress(DAY2)]
        result = compute_review_forecast(rows, now=TODAY)
        assert result[0]["count"] == 1
        assert result[1]["count"] == 1
        assert result[2]["count"] == 1

    def test_custom_days_parameter(self):
        result = compute_review_forecast([], days=7, now=TODAY)
        assert len(result) == 7

    def test_respects_custom_days_window(self):
        rows = [_progress(TODAY + timedelta(days=7))]
        result = compute_review_forecast(rows, days=7, now=TODAY)
        assert all(d["count"] == 0 for d in result)
