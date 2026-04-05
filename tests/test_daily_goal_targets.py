"""Daily goal targets derived from recent completed-session history."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.session import LearningSession, SessionLifecycleStatus, StudyModeType
from app.services.daily_stats import compute_daily_goal_targets
from app.utils.time import utc_now


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


def test_compute_daily_goal_targets_no_history(db_session):
    assert compute_daily_goal_targets(db_session) == {"sessions": 10, "lessons": 2}


def test_compute_daily_goal_targets_from_weekly_average(db_session):
    """7 days before today: 7 sessions total → avg 1 → clamped to min 5 sessions."""
    now = utc_now().replace(tzinfo=None)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(1, 8):
        day = start_of_today - timedelta(days=i)
        db_session.add(
            LearningSession(
                mode=StudyModeType.PASSIVE,
                status=SessionLifecycleStatus.COMPLETED,
                locked=True,
                completed=True,
                completed_at=day.replace(hour=12),
            )
        )
    db_session.commit()
    out = compute_daily_goal_targets(db_session)
    assert out["sessions"] == 5
    assert out["lessons"] == 2


def test_compute_daily_goal_targets_high_volume_clamped(db_session):
    """35 sessions over 7 days → avg 5 → sessions 5, lessons 2."""
    now = utc_now().replace(tzinfo=None)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(1, 8):
        day = start_of_today - timedelta(days=i)
        for h in range(5):
            db_session.add(
                LearningSession(
                    mode=StudyModeType.PASSIVE,
                    status=SessionLifecycleStatus.COMPLETED,
                    locked=True,
                    completed=True,
                    completed_at=day.replace(hour=h),
                )
            )
    db_session.commit()
    out = compute_daily_goal_targets(db_session)
    assert out["sessions"] == 5
    assert out["lessons"] == 2


def test_compute_daily_goal_targets_realistic_average(db_session):
    """175 / 7 = 25 → sessions 25, lessons round(25/4) = 6."""
    now = utc_now().replace(tzinfo=None)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    per_day = [46, 21, 21, 9, 27, 16, 35]
    for day_idx, n in enumerate(per_day, start=1):
        day = start_of_today - timedelta(days=day_idx)
        for j in range(n):
            db_session.add(
                LearningSession(
                    mode=StudyModeType.PASSIVE,
                    status=SessionLifecycleStatus.COMPLETED,
                    locked=True,
                    completed=True,
                    completed_at=day.replace(hour=(j % 23), minute=j % 60),
                )
            )
    db_session.commit()
    out = compute_daily_goal_targets(db_session)
    assert out["sessions"] == 25
    assert out["lessons"] == 6
