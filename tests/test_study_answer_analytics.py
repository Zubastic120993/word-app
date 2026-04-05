from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_service_module
from app.database import Base
from app.models.learning_unit import LearningUnit
from app.models.practice_event import PracticeEvent
from app.models.session import StudyModeType
from app.models.vocabulary import Vocabulary
from app.schemas.session import AnswerRequest
from app.services.analytics_service import (
    STUDY_ANSWER_EVENT_TYPE,
    get_study_answer_metrics,
    get_study_answer_metrics_between,
    get_study_answer_metrics_since,
    get_study_calendar_week_activity,
    record_study_answer_event,
)
from app.services.session_service import SESSION_SIZE, SessionService


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
    from app.models.learning_unit import UnitType

    if db_session.query(Vocabulary).filter(Vocabulary.id == 1).first() is None:
        db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))

    units = []
    for i in range(SESSION_SIZE + 10):
        unit = LearningUnit(
            text=f"word{i}",
            translation=f"translation{i}",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(unit)
        units.append(unit)
    db_session.commit()
    return units


def _study_events(db_session):
    return (
        db_session.query(PracticeEvent)
        .filter(PracticeEvent.event_type == STUDY_ANSWER_EVENT_TYPE)
        .order_by(PracticeEvent.id.asc())
        .all()
    )


def test_correct_answer_analytics_emission(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.PASSIVE)
    session_unit = session.units[0]

    service.submit_answer(
        session_id=session.id,
        unit_position=session_unit.position,
        is_correct=True,
    )

    events = _study_events(db_session)
    assert len(events) == 1
    assert events[0].payload["session_id"] == session.id
    assert events[0].payload["unit_id"] == session_unit.unit_id
    assert events[0].payload["answer_index"] == session_unit.position
    assert events[0].payload["result"] == "correct"
    assert datetime.fromisoformat(events[0].payload["timestamp"])

    metrics = get_study_answer_metrics(db_session, session_id=session.id)
    assert metrics["total_answers"] == 1
    assert metrics["correct_answers"] == 1
    assert metrics["incorrect_answers"] == 0
    assert metrics["success_rate"] == 1.0
    assert metrics["failure_rate"] == 0.0


def test_incorrect_answer_analytics_emission(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.PASSIVE)
    session_unit = session.units[0]

    service.submit_answer(
        session_id=session.id,
        unit_position=session_unit.position,
        is_correct=False,
    )

    events = _study_events(db_session)
    assert len(events) == 1
    assert events[0].payload["result"] == "incorrect"

    metrics = get_study_answer_metrics(db_session, session_id=session.id)
    assert metrics["total_answers"] == 1
    assert metrics["correct_answers"] == 0
    assert metrics["incorrect_answers"] == 1
    assert metrics["success_rate"] == 0.0
    assert metrics["failure_rate"] == 1.0


def test_duplicate_answer_submission_returns_idempotent_response_and_emits_once(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.PASSIVE)
    session_unit = session.units[0]
    answer = AnswerRequest(unit_position=session_unit.position, is_correct=True)

    first_response = service.submit_answer_and_build_response(session.id, answer)
    second_response = service.submit_answer_and_build_response(session.id, answer)

    assert first_response.session_id == second_response.session_id == session.id
    assert first_response.unit_position == second_response.unit_position == session_unit.position
    assert len(_study_events(db_session)) == 1


def test_retry_same_answer_does_not_double_count_analytics(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.PASSIVE)
    session_unit = session.units[0]
    answer = AnswerRequest(unit_position=session_unit.position, is_correct=False)

    service.submit_answer_and_build_response(session.id, answer)
    service.submit_answer_and_build_response(session.id, answer)

    metrics = get_study_answer_metrics(db_session, session_id=session.id)
    assert len(_study_events(db_session)) == 1
    assert metrics["total_answers"] == 1
    assert metrics["correct_answers"] == 0
    assert metrics["incorrect_answers"] == 1
    assert metrics["failure_rate"] == 1.0


def test_study_answer_dedupe_prevents_double_count(db_session):
    created = record_study_answer_event(
        db_session,
        session_id=11,
        unit_id=22,
        answer_index=3,
        result="correct",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    duplicate = record_study_answer_event(
        db_session,
        session_id=11,
        unit_id=22,
        answer_index=3,
        result="incorrect",
        timestamp=datetime(2026, 1, 1, 12, 1, 0),
    )

    assert created is True
    assert duplicate is False
    assert len(_study_events(db_session)) == 1

    metrics = get_study_answer_metrics(db_session, session_id=11)
    assert metrics["total_answers"] == 1
    assert metrics["correct_answers"] == 1
    assert metrics["incorrect_answers"] == 0
    assert metrics["success_rate"] == 1.0


def test_study_answer_analytics_uses_fallback_answer_index(db_session):
    created = record_study_answer_event(
        db_session,
        session_id=7,
        unit_id=8,
        answer_index=None,
        fallback_answer_index=9,
        result="incorrect",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
    )

    assert created is True
    events = _study_events(db_session)
    assert len(events) == 1
    assert events[0].payload["answer_index"] == 9


def test_get_study_calendar_week_activity_empty(db_session):
    anchor = datetime(2026, 3, 21, 12, 0, 0)
    r = get_study_calendar_week_activity(db_session, anchor, week_offset=0)
    assert r["per_day"] == [0] * 7
    assert r["total_answers"] == 0
    assert r["start_day"].isoformat() == "2026-03-15"
    assert r["end_day"].isoformat() == "2026-03-21"


def test_get_study_calendar_week_activity_dedupes_same_day(db_session):
    anchor = datetime(2026, 3, 21, 12, 0, 0)
    day = datetime(2026, 3, 20, 14, 0, 0)
    for _ in range(3):
        db_session.add(
            PracticeEvent(
                event_type=STUDY_ANSWER_EVENT_TYPE,
                theme=None,
                payload={
                    "session_id": 1,
                    "unit_id": 1,
                    "answer_index": 0,
                    "result": "correct",
                    "timestamp": "2026-03-20T14:00:00",
                },
                created_at=day,
            )
        )
    db_session.commit()
    r = get_study_calendar_week_activity(db_session, anchor, week_offset=0)
    assert r["total_answers"] == 1
    assert sum(r["per_day"]) == 1
    assert r["per_day"][5] == 1


def test_get_study_calendar_week_activity_splits_days_and_matches_sum(db_session):
    anchor = datetime(2026, 3, 21, 12, 0, 0)
    db_session.add(
        PracticeEvent(
            event_type=STUDY_ANSWER_EVENT_TYPE,
            theme=None,
            payload={
                "session_id": 1,
                "unit_id": 1,
                "answer_index": 0,
                "result": "correct",
                "timestamp": "2026-03-19",
            },
            created_at=datetime(2026, 3, 19, 9, 0, 0),
        )
    )
    db_session.add(
        PracticeEvent(
            event_type=STUDY_ANSWER_EVENT_TYPE,
            theme=None,
            payload={
                "session_id": 1,
                "unit_id": 2,
                "answer_index": 1,
                "result": "incorrect",
                "timestamp": "2026-03-21",
            },
            created_at=datetime(2026, 3, 21, 9, 0, 0),
        )
    )
    db_session.commit()
    r = get_study_calendar_week_activity(db_session, anchor, week_offset=0)
    assert r["total_answers"] == 2
    assert sum(r["per_day"]) == 2
    assert r["per_day"][4] == 1
    assert r["per_day"][6] == 1


def test_get_study_calendar_week_activity_prior_offset(db_session):
    anchor = datetime(2026, 3, 21, 12, 0, 0)
    db_session.add(
        PracticeEvent(
            event_type=STUDY_ANSWER_EVENT_TYPE,
            theme=None,
            payload={
                "session_id": 1,
                "unit_id": 1,
                "answer_index": 0,
                "result": "correct",
                "timestamp": "2026-03-10",
            },
            created_at=datetime(2026, 3, 10, 12, 0, 0),
        )
    )
    db_session.commit()
    r = get_study_calendar_week_activity(db_session, anchor, week_offset=1)
    assert r["start_day"].isoformat() == "2026-03-08"
    assert r["end_day"].isoformat() == "2026-03-14"
    assert r["total_answers"] == 1
    assert sum(r["per_day"]) == 1


def test_get_study_answer_metrics_between_respects_bounds(db_session):
    low = datetime(2026, 1, 1, 0, 0, 0)
    high = datetime(2026, 1, 10, 0, 0, 0)
    inside = datetime(2026, 1, 5, 12, 0, 0)
    outside = datetime(2026, 1, 12, 12, 0, 0)

    for unit_id, created_at, result in (
        (1, inside, "correct"),
        (2, outside, "incorrect"),
    ):
        db_session.add(
            PracticeEvent(
                event_type=STUDY_ANSWER_EVENT_TYPE,
                theme=None,
                payload={
                    "session_id": 1,
                    "unit_id": unit_id,
                    "answer_index": unit_id,
                    "result": result,
                    "timestamp": "2026-01-01T12:00:00",
                },
                created_at=created_at,
            )
        )
    db_session.commit()

    m = get_study_answer_metrics_between(db_session, low, high)
    assert m["total_answers"] == 1
    assert m["correct_answers"] == 1


def test_get_study_answer_metrics_since_filters_by_created_at(db_session):
    old = datetime(2020, 1, 1, 12, 0, 0)
    recent = datetime(2026, 1, 10, 12, 0, 0)
    db_session.add(
        PracticeEvent(
            event_type=STUDY_ANSWER_EVENT_TYPE,
            theme=None,
            payload={
                "session_id": 1,
                "unit_id": 1,
                "answer_index": 0,
                "result": "correct",
                "timestamp": "2020-01-01T12:00:00",
            },
            created_at=old,
        )
    )
    db_session.add(
        PracticeEvent(
            event_type=STUDY_ANSWER_EVENT_TYPE,
            theme=None,
            payload={
                "session_id": 1,
                "unit_id": 2,
                "answer_index": 1,
                "result": "incorrect",
                "timestamp": "2026-01-10T12:00:00",
            },
            created_at=recent,
        )
    )
    db_session.commit()

    since = datetime(2026, 1, 1, 0, 0, 0)
    windowed = get_study_answer_metrics_since(db_session, since)
    assert windowed["total_answers"] == 1
    assert windowed["incorrect_answers"] == 1
    assert windowed["success_rate"] == 0.0

    all_time = get_study_answer_metrics_since(db_session, None)
    assert all_time["total_answers"] == 2
    assert all_time["correct_answers"] == 1
    assert all_time["incorrect_answers"] == 1


def test_analytics_failure_does_not_break_session_flow(db_session, sample_units, monkeypatch, caplog):
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.PASSIVE)
    session_unit = session.units[0]

    def _raise(*args, **kwargs):
        raise RuntimeError("analytics down")

    monkeypatch.setattr(session_service_module, "record_study_answer_event", _raise)

    with caplog.at_level("WARNING"):
        updated_unit, _ = service.submit_answer(
            session_id=session.id,
            unit_position=session_unit.position,
            is_correct=True,
        )

    assert updated_unit.answered is True
    assert updated_unit.is_correct is True
    assert len(_study_events(db_session)) == 0
    assert "Study-answer analytics write failed" in caplog.text
