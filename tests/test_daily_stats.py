from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.session import LearningSession, StudyModeType
from app.services.daily_stats import (
    DUE_RECALL_URGENT_THRESHOLD,
    _utc_now_naive,
    get_daily_dashboard_stats,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_utc_now_naive_returns_naive_datetime():
    now = _utc_now_naive()
    assert now.tzinfo is None


def test_daily_stats_counts_introduced_today_with_utc_naive_boundary(db_session, monkeypatch):
    fixed_now = datetime(2026, 3, 17, 12, 0, 0)
    monkeypatch.setattr("app.services.daily_stats._utc_now_naive", lambda: fixed_now)

    unit_today = LearningUnit(
        text="today",
        translation="dzisiaj",
        type=UnitType.WORD,
        source_pdf="test.pdf",
    )
    unit_yesterday = LearningUnit(
        text="yesterday",
        translation="wczoraj",
        type=UnitType.WORD,
        source_pdf="test.pdf",
    )
    db_session.add_all([unit_today, unit_yesterday])
    db_session.commit()

    db_session.add_all(
        [
            LearningProgress(
                unit_id=unit_today.id,
                introduced_at=datetime(2026, 3, 17, 0, 0, 1),
            ),
            LearningProgress(
                unit_id=unit_yesterday.id,
                introduced_at=datetime(2026, 3, 16, 23, 59, 59),
            ),
            LearningSession(
                mode=StudyModeType.PASSIVE,
                completed_at=datetime(2026, 3, 17, 1, 0, 0),
            ),
            LearningSession(
                mode=StudyModeType.RECALL,
                completed_at=datetime(2026, 3, 17, 2, 0, 0),
                summary_correct_count=3,
                summary_answered_units=5,
            ),
            LearningSession(
                mode=StudyModeType.RECALL,
                completed_at=datetime(2026, 3, 16, 22, 0, 0),
                summary_correct_count=10,
                summary_answered_units=10,
            ),
        ]
    )
    db_session.commit()

    stats = get_daily_dashboard_stats(db_session)

    assert stats["words_introduced_today"] == 1
    assert stats["passive_sessions_today"] == 1
    assert stats["recall_sessions_today"] == 1
    assert stats["recall_accuracy_today"] == 0.6


def test_recommended_plan_urgent_recall_when_due_above_threshold(db_session, monkeypatch):
    monkeypatch.setattr("app.services.daily_stats._utc_now_naive", lambda: datetime(2026, 3, 17, 12, 0, 0))
    monkeypatch.setattr(
        "app.services.daily_stats.count_due",
        lambda _rows, now=None: DUE_RECALL_URGENT_THRESHOLD + 1,
    )
    stats = get_daily_dashboard_stats(db_session)
    assert stats["recommended_plan"] == "Start with overdue recall reviews."


def test_recommended_plan_guided_new_words_when_due_positive_within_threshold(db_session, monkeypatch):
    monkeypatch.setattr("app.services.daily_stats._utc_now_naive", lambda: datetime(2026, 3, 17, 12, 0, 0))
    monkeypatch.setattr(
        "app.services.daily_stats.count_due",
        lambda _rows, now=None: 3,
    )
    u = LearningUnit(
        text="w",
        translation="t",
        type=UnitType.WORD,
        source_pdf="lesson_polish_ukrainian_a.pdf",
    )
    db_session.add(u)
    db_session.commit()

    stats = get_daily_dashboard_stats(db_session)
    assert stats["overdue_word_count"] == 3
    assert "3 due words" in stats["recommended_plan"]
    assert "lesson_polish_ukrainian_a.pdf" in stats["recommended_plan"]


def test_recommended_plan_no_focus_when_due_positive_but_no_never_seen(db_session, monkeypatch):
    monkeypatch.setattr("app.services.daily_stats._utc_now_naive", lambda: datetime(2026, 3, 17, 12, 0, 0))
    monkeypatch.setattr(
        "app.services.daily_stats.count_due",
        lambda _rows, now=None: 2,
    )
    stats = get_daily_dashboard_stats(db_session)
    assert stats["recommended_plan"] == (
        "You have 2 due words. You may introduce new words."
    )


def test_recommended_plan_zero_due_with_focus(db_session, monkeypatch):
    monkeypatch.setattr("app.services.daily_stats._utc_now_naive", lambda: datetime(2026, 3, 17, 12, 0, 0))
    monkeypatch.setattr("app.services.daily_stats.count_due", lambda _rows, now=None: 0)
    u = LearningUnit(
        text="w",
        translation="t",
        type=UnitType.WORD,
        source_pdf="x_czytaj_1.pdf",
    )
    db_session.add(u)
    db_session.commit()

    stats = get_daily_dashboard_stats(db_session)
    assert "Continue with x_czytaj_1.pdf" in stats["recommended_plan"]
    assert "1 new words remaining" in stats["recommended_plan"]


def test_recommended_plan_zero_due_no_focus(db_session, monkeypatch):
    monkeypatch.setattr("app.services.daily_stats._utc_now_naive", lambda: datetime(2026, 3, 17, 12, 0, 0))
    monkeypatch.setattr("app.services.daily_stats.count_due", lambda _rows, now=None: 0)
    stats = get_daily_dashboard_stats(db_session)
    assert stats["recommended_plan"] == "No overdue words. You may introduce new words."
