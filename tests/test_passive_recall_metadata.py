"""Passive recall chain and per-unit selection_reason persistence."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.session import LearningSession, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services import session_service as ss_mod
from app.services.session_service import SESSION_SIZE, SessionService
from app.utils.time import utc_now


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


def test_passive_recall_chain_weak_written_to_db(db_session, sample_units):
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
    db_session.refresh(passive)
    assert passive.passive_recall_chain == "weak"


def test_passive_recall_chain_null_for_normal_passive(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)
    passive = service.create_session(mode=StudyModeType.PASSIVE)
    db_session.refresh(passive)
    assert passive.passive_recall_chain is None


def test_passive_recall_chain_null_for_due_only_passive(db_session, sample_units):
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
    db_session.refresh(passive)
    assert passive.passive_recall_chain is None


def test_lesson_passive_writes_passive_recall_chain_lesson(db_session, monkeypatch):
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 5)
    db_session.add(
        Vocabulary(
            user_key="u",
            name="polish_ukrainian_dictionary_lesson01.docx",
            track_type="plua",
            lesson_index=1,
        )
    )
    db_session.flush()
    pl_vid = db_session.query(Vocabulary.id).scalar()
    for i in range(8):
        db_session.add(
            LearningUnit(
                text=f"w{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="pl_ua.pdf",
                vocabulary_id=pl_vid,
            )
        )
    db_session.commit()

    svc = SessionService(db_session, random_seed=42)
    passive = svc.create_session(mode=StudyModeType.PASSIVE, curriculum_mode="lesson")
    db_session.refresh(passive)
    assert passive.passive_recall_chain == "lesson"


def test_next_recommendation_uses_db_after_ephemeral_caches_cleared(
    db_session, sample_units
):
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

    ss_mod._SESSION_PASSIVE_RECALL_AFTER.clear()
    ss_mod._SESSION_SELECTION_REASONS.clear()

    rec = service.get_next_recommendation(passive.id)
    assert rec["type"] == "recall"
    assert rec["follow_up_session_id"] == passive.id
    assert "difficult" in rec["message"].lower()
    assert rec.get("weak_only") is True


def test_get_session_uses_persisted_selection_reason_when_cache_cold(
    db_session, sample_units
):
    service = SessionService(db_session, random_seed=42)
    passive = service.create_session(mode=StudyModeType.PASSIVE)
    first = db_session.query(SessionUnit).filter(SessionUnit.session_id == passive.id).first()
    assert first is not None
    assert first.selection_reason is not None

    ss_mod._SESSION_SELECTION_REASONS.clear()

    loaded = service.get_session(passive.id)
    assert loaded is not None
    su0 = sorted(loaded.units, key=lambda u: u.position)[0]
    assert su0.selection_reason is not None
