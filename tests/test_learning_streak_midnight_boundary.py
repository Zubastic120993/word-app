from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.learning_unit import RecallResult
from app.services.progress_metrics_service import compute_learning_streak


def test_learning_streak_across_utc_midnight_boundary():
    progress_rows = [
        SimpleNamespace(
            last_recall_result=RecallResult.CORRECT,
            times_correct=1,
            last_seen=datetime(2025, 1, 10, 23, 59, 59, tzinfo=UTC),
        ),
        SimpleNamespace(
            last_recall_result=RecallResult.CORRECT,
            times_correct=1,
            last_seen=datetime(2025, 1, 11, 0, 0, 1, tzinfo=UTC),
        ),
    ]

    streak = compute_learning_streak(
        progress_rows,
        today=datetime(2025, 1, 11, 0, 5, 0, tzinfo=UTC),
    )

    assert streak == 2
