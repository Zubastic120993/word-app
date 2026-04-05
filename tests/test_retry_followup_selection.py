"""Tests for retry_failed_only follow-up selection (failed units prefix; tail refill disabled)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, RecallResult, UnitType
from app.models.session import LearningSession, SessionLifecycleStatus, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SESSION_SIZE, InsufficientUnitsError, SessionService


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
    """Enough units for full sessions and selection pools."""
    if db_session.query(Vocabulary).filter(Vocabulary.id == 1).first() is None:
        db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))

    units = []
    for i in range(SESSION_SIZE + 40):
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


def _failed_unit_ids_in_order(session: LearningSession) -> list[int]:
    return [
        su.unit_id
        for su in sorted(session.units, key=lambda u: u.position)
        if su.recall_result == RecallResult.FAILED
    ]


def test_retry_failed_prefixes_failed_units_in_order(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    _complete_recall_with_failures_first_n(service, recall_session, n_fail=5)
    db_session.refresh(recall_session)

    assert recall_session.status == SessionLifecycleStatus.COMPLETED
    failed_ordered = _failed_unit_ids_in_order(recall_session)
    assert len(failed_ordered) == 5

    new_session = service.create_session(
        mode=StudyModeType.RECALL,
        follow_up_session_id=recall_session.id,
        retry_failed_only=True,
    )
    db_session.refresh(new_session)
    result_ids = [su.unit_id for su in sorted(new_session.units, key=lambda u: u.position)]

    assert len(result_ids) == len(failed_ordered)
    assert result_ids == failed_ordered


def test_retry_follow_up_has_no_duplicate_unit_ids(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    _complete_recall_with_failures_first_n(service, recall_session, n_fail=8)
    db_session.refresh(recall_session)

    new_session = service.create_session(
        mode=StudyModeType.RECALL,
        follow_up_session_id=recall_session.id,
        retry_failed_only=True,
    )
    db_session.refresh(new_session)
    result_ids = [su.unit_id for su in sorted(new_session.units, key=lambda u: u.position)]

    assert len(result_ids) == 8
    assert len(result_ids) == len(set(result_ids))


def test_retry_with_small_failed_set_is_prefix_only_no_tail_fill(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    _complete_recall_with_failures_first_n(service, recall_session, n_fail=3)
    db_session.refresh(recall_session)

    failed_ordered = _failed_unit_ids_in_order(recall_session)
    assert len(failed_ordered) == 3

    new_session = service.create_session(
        mode=StudyModeType.RECALL,
        follow_up_session_id=recall_session.id,
        retry_failed_only=True,
    )
    db_session.refresh(new_session)
    result_ids = [su.unit_id for su in sorted(new_session.units, key=lambda u: u.position)]

    assert len(result_ids) == len(failed_ordered)
    assert result_ids == failed_ordered


def test_retry_priority_includes_failed_despite_narrow_source_pdf_filter(db_session, sample_units):
    """Retry must not drop failed ids when UI source filter excludes those units (e.g. after due-only)."""
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    _complete_recall_with_failures_first_n(service, recall_session, n_fail=3)
    db_session.refresh(recall_session)
    failed_ordered = _failed_unit_ids_in_order(recall_session)
    assert len(failed_ordered) == 3

    new_session = service.create_session(
        mode=StudyModeType.RECALL,
        follow_up_session_id=recall_session.id,
        retry_failed_only=True,
        source_pdfs=["definitely_not_in_db.pdf"],
    )
    db_session.refresh(new_session)
    result_ids = [su.unit_id for su in sorted(new_session.units, key=lambda u: u.position)]
    assert result_ids[:3] == failed_ordered


def test_retry_raises_when_no_failed_units(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    recall_session = _introduce_then_recall_session(service)
    for su in sorted(recall_session.units, key=lambda u: u.position):
        service.submit_answer(
            session_id=recall_session.id,
            unit_position=su.position,
            user_input=su.unit.text,
        )
    db_session.refresh(recall_session)
    assert _failed_unit_ids_in_order(recall_session) == []

    with pytest.raises(InsufficientUnitsError, match="No failed words to retry"):
        service.create_session(
            mode=StudyModeType.RECALL,
            follow_up_session_id=recall_session.id,
            retry_failed_only=True,
        )


def test_retry_failed_only_requires_follow_up_session_id(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    with pytest.raises(ValueError, match="retry_failed_only requires follow_up_session_id"):
        service.create_session(
            mode=StudyModeType.RECALL,
            retry_failed_only=True,
        )


def test_passive_follow_up_recall_prefix_despite_narrow_source_pdf_filter(db_session, sample_units):
    """Passive → recall recommendation must keep session units even if source filter mismatches."""
    service = SessionService(db_session, random_seed=42)
    passive = service.create_session(mode=StudyModeType.PASSIVE)
    passive_ids = [su.unit_id for su in sorted(passive.units, key=lambda u: u.position)]
    for su in passive.units:
        service.submit_answer(
            session_id=passive.id,
            unit_position=su.position,
            is_correct=True,
        )
    db_session.refresh(passive)
    assert passive.status == SessionLifecycleStatus.COMPLETED

    recall = service.create_session(
        mode=StudyModeType.RECALL,
        follow_up_session_id=passive.id,
        source_pdfs=["definitely_not_in_db.pdf"],
    )
    db_session.refresh(recall)
    result_ids = [su.unit_id for su in sorted(recall.units, key=lambda u: u.position)]
    assert len(result_ids) == SESSION_SIZE
    assert result_ids[:SESSION_SIZE] == passive_ids[:SESSION_SIZE]
