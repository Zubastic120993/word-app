"""Integration tests for CLOZE study mode."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult, UnitType
from app.models.session import SessionLifecycleStatus, StudyModeType
from app.models.vocabulary import Vocabulary
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

    units = []
    for i in range(70):
        unit = LearningUnit(
            text=f"słowo{i}",
            type=UnitType.WORD,
            translation=f"word{i}",
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
            normalized_text=f"słowo{i}",
            normalized_translation=f"word{i}",
        )
        db_session.add(unit)
        units.append(unit)
    db_session.commit()
    return units


@pytest.fixture
def introduced_units(db_session, sample_units):
    now = utc_now()
    for unit in sample_units:
        progress = LearningProgress(
            unit_id=unit.id,
            times_seen=1,
            times_correct=1,
            times_failed=0,
            confidence_score=0.5,
            last_seen=now - timedelta(hours=1),
            introduced_at=now - timedelta(days=1),
            next_review_at=now - timedelta(hours=1),
            recall_fail_streak=0,
            is_blocked=False,
        )
        db_session.add(progress)
    db_session.commit()
    return sample_units


def _fake_cloze_fields_mixed(self, selected_units):
    out = []
    for i, u in enumerate(selected_units):
        if i % 2 == 0:
            out.append(
                {
                    "exercise_type": "cloze",
                    "cloze_prompt": f"Zdanie ze ________ i {u.text}.",
                    "context_sentence_translation": f"Sentence with blank and {u.text}.",
                }
            )
        else:
            out.append(
                {
                    "exercise_type": "recall",
                    "cloze_prompt": None,
                    "context_sentence_translation": None,
                }
            )
    return out


def test_cloze_session_mixed_exercise_types(monkeypatch, db_session, introduced_units):
    monkeypatch.setattr(SessionService, "_build_cloze_session_unit_fields", _fake_cloze_fields_mixed)
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.CLOZE)
    assert session.mode == StudyModeType.CLOZE
    cloze_n = sum(1 for su in session.units if su.exercise_type == "cloze")
    recall_n = sum(1 for su in session.units if su.exercise_type == "recall")
    assert cloze_n > 0 and recall_n > 0
    for su in session.units:
        if su.exercise_type == "cloze":
            assert su.cloze_prompt and "________" in su.cloze_prompt
            assert su.context_sentence_translation


def test_cloze_answer_updates_progress_like_recall(monkeypatch, db_session, introduced_units):
    monkeypatch.setattr(
        SessionService,
        "_build_cloze_session_unit_fields",
        lambda self, units: [
            {
                "exercise_type": "recall",
                "cloze_prompt": None,
                "context_sentence_translation": None,
            }
            for _ in units
        ],
    )
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.CLOZE)
    su = session.units[0]
    conf_before = db_session.query(LearningProgress).filter_by(unit_id=su.unit_id).first().confidence_score

    service.submit_answer(session_id=session.id, unit_position=1, user_input="wrong_wrong")
    prog = db_session.query(LearningProgress).filter_by(unit_id=su.unit_id).first()
    assert prog.last_recall_result == RecallResult.FAILED
    assert prog.recall_fail_streak >= 1
    assert prog.confidence_score <= conf_before

    service.submit_answer(session_id=session.id, unit_position=2, user_input=session.units[1].unit.text)
    prog2 = db_session.query(LearningProgress).filter_by(unit_id=session.units[1].unit_id).first()
    assert prog2.last_recall_result == RecallResult.CORRECT


def test_cloze_correct_unblocks(monkeypatch, db_session, sample_units):
    now = utc_now()
    for i, unit in enumerate(sample_units):
        progress = LearningProgress(
            unit_id=unit.id,
            times_seen=10,
            times_correct=0,
            times_failed=8,
            confidence_score=0.1,
            last_seen=now - timedelta(hours=1),
            introduced_at=now - timedelta(days=1),
            next_review_at=now - timedelta(hours=1),
            recall_fail_streak=5 if i == 0 else 0,
            is_blocked=True if i == 0 else False,
        )
        db_session.add(progress)
    db_session.commit()

    monkeypatch.setattr(
        SessionService,
        "_build_cloze_session_unit_fields",
        lambda self, units: [
            {
                "exercise_type": "recall",
                "cloze_prompt": None,
                "context_sentence_translation": None,
            }
            for _ in units
        ],
    )
    service = SessionService(db_session, random_seed=42)
    # Blocked units are only eligible in weak-only sessions.
    session = service.create_session(mode=StudyModeType.CLOZE, weak_only=True)
    target = sample_units[0]
    su = next(s for s in session.units if s.unit_id == target.id)
    service.submit_answer(
        session_id=session.id,
        unit_position=su.position,
        user_input=target.text,
    )
    prog = db_session.query(LearningProgress).filter_by(unit_id=target.id).first()
    assert prog.is_blocked is False


def test_cloze_completion_offers_retry_like_recall(monkeypatch, db_session, introduced_units):
    monkeypatch.setattr(
        SessionService,
        "_build_cloze_session_unit_fields",
        lambda self, units: [
            {
                "exercise_type": "recall",
                "cloze_prompt": None,
                "context_sentence_translation": None,
            }
            for _ in units
        ],
    )
    service = SessionService(db_session, random_seed=42)
    session = service.create_session(mode=StudyModeType.CLOZE)
    units_sorted = sorted(session.units, key=lambda u: u.position)
    for su in units_sorted:
        if su.position <= 3:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input="bad",
            )
        else:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input=su.unit.text,
            )

    db_session.refresh(session)
    assert session.status == SessionLifecycleStatus.COMPLETED
    rec = service.get_next_recommendation(session.id)
    assert rec.get("type") == "retry"
    assert rec.get("retry_failed_only") is True
