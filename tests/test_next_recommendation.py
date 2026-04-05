"""Regression: next recommendation must not chain follow-up recall after a completed recall session."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.session import LearningSession, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SESSION_SIZE, SessionService
from app.utils.time import utc_now


def _introduce_then_recall_session(service: SessionService) -> LearningSession:
    passive = service.create_session(mode=StudyModeType.PASSIVE)
    for su in passive.units:
        service.submit_answer(
            session_id=passive.id,
            unit_position=su.position,
            is_correct=True,
        )
    return service.create_session(mode=StudyModeType.RECALL)


def _complete_recall_with_failures_first_n(
    service: SessionService, session: LearningSession, n_fail: int
) -> None:
    for su in sorted(session.units, key=lambda u: u.position):
        if su.position <= n_fail:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input="completely_wrong_answer",
            )
        else:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input=su.unit.text,
            )


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def sample_units(db_session):
    if db_session.query(Vocabulary).filter(Vocabulary.id == 1).first() is None:
        db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))
    for i in range(SESSION_SIZE + 40):
        db_session.add(
            LearningUnit(
                text=f"word{i}",
                translation=f"translation{i}",
                type=UnitType.WORD,
                source_pdf="czytaj_01_01_test.pdf",
                vocabulary_id=1,
            )
        )
    db_session.commit()


def test_due_only_passive_completion_skips_recall_follow_up(db_session, sample_units):
    now = utc_now().replace(tzinfo=None)
    due_time = now - timedelta(days=1)
    units = db_session.query(LearningUnit).limit(SESSION_SIZE).all()
    for u in units:
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                times_seen=3,
                times_correct=2,
                times_failed=0,
                confidence_score=0.5,
                last_seen=now,
                next_review_at=due_time,
                introduced_at=now - timedelta(days=10),
            )
        )
    db_session.commit()

    service = SessionService(db_session, random_seed=42)
    passive = service.create_session(mode=StudyModeType.PASSIVE, due_only=True)
    for su in passive.units:
        service.submit_answer(
            session_id=passive.id,
            unit_position=su.position,
            is_correct=True,
        )
    db_session.refresh(passive)
    rec = service.get_next_recommendation(passive.id)
    assert rec.get("follow_up_session_id") is None
    assert rec.get("type") != "recall"


def test_weak_only_passive_completion_offers_recall_follow_up(db_session, sample_units):
    now = utc_now().replace(tzinfo=None)
    units = db_session.query(LearningUnit).limit(SESSION_SIZE).all()
    for u in units:
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                times_seen=3,
                times_correct=1,
                times_failed=2,
                confidence_score=0.35,
                last_seen=now,
                next_review_at=now + timedelta(days=2),
                introduced_at=now - timedelta(days=10),
            )
        )
    db_session.commit()

    service = SessionService(db_session, random_seed=42)
    passive = service.create_session(mode=StudyModeType.PASSIVE, weak_only=True)
    for su in passive.units:
        service.submit_answer(
            session_id=passive.id,
            unit_position=su.position,
            is_correct=True,
        )
    db_session.refresh(passive)
    rec = service.get_next_recommendation(passive.id)
    assert rec["type"] == "recall"
    assert rec["follow_up_session_id"] == passive.id


def test_passive_completion_still_recommends_recall_follow_up(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    passive = service.create_session(mode=StudyModeType.PASSIVE)
    for su in passive.units:
        service.submit_answer(
            session_id=passive.id,
            unit_position=su.position,
            is_correct=True,
        )
    db_session.refresh(passive)
    rec = service.get_next_recommendation(passive.id)
    assert rec["type"] == "recall"
    assert rec["follow_up_session_id"] == passive.id


def test_recall_completion_with_failures_still_offers_retry_follow_up(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    _complete_recall_with_failures_first_n(service, recall_session, n_fail=5)
    db_session.refresh(recall_session)
    rec = service.get_next_recommendation(recall_session.id)
    assert rec["type"] == "retry"
    assert rec["follow_up_session_id"] == recall_session.id
    assert rec["retry_failed_only"] is True


def test_due_only_recall_completion_with_failures_offers_retry_follow_up(
    db_session, sample_units
):
    now = utc_now().replace(tzinfo=None)
    due_time = now - timedelta(days=1)
    units = db_session.query(LearningUnit).limit(SESSION_SIZE).all()
    for u in units:
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                times_seen=3,
                times_correct=2,
                times_failed=0,
                confidence_score=0.5,
                last_seen=now,
                next_review_at=due_time,
                introduced_at=now - timedelta(days=10),
            )
        )
    db_session.commit()

    service = SessionService(db_session, random_seed=42)
    recall_due = service.create_session(mode=StudyModeType.RECALL, due_only=True)
    _complete_recall_with_failures_first_n(service, recall_due, n_fail=5)
    db_session.refresh(recall_due)
    rec = service.get_next_recommendation(recall_due.id)
    assert rec["type"] == "retry"
    assert rec["follow_up_session_id"] == recall_due.id
    assert rec["retry_failed_only"] is True


def test_recall_completion_does_not_recommend_follow_up_recall_again(
    db_session, sample_units, monkeypatch
):
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    _complete_recall_with_failures_first_n(service, recall_session, n_fail=0)
    db_session.refresh(recall_session)

    monkeypatch.setattr(
        "app.services.daily_stats.get_daily_dashboard_stats",
        lambda _db: {
            "overdue_word_count": 0,
            "cap_exceeded": True,
        },
    )

    rec = service.get_next_recommendation(recall_session.id)
    assert rec.get("follow_up_session_id") is None
    assert rec["type"] == "passive"
