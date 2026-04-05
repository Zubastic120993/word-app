"""Curriculum mode: PL–UA lesson scope (isolated create_session branch)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.session import LearningSession, SessionLifecycleStatus, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import InsufficientUnitsError, SessionService


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


def test_curriculum_lesson_session_strict_plua_only_with_czytaj_available(db_session, monkeypatch):
    """curriculum_mode=lesson uses only PL–UA; Czytaj is ignored even when present."""
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 5)
    db_session.add(
        Vocabulary(
            user_key="u",
            name="polish_ukrainian_dictionary_lesson01.docx",
            track_type="plua",
            lesson_index=1,
        )
    )
    db_session.add(
        Vocabulary(
            user_key="u",
            name="czytaj_01_01_x.docx",
            track_type="czytaj",
            lesson_index=None,
        )
    )
    db_session.flush()
    pl_vid = (
        db_session.query(Vocabulary.id)
        .filter(Vocabulary.name == "polish_ukrainian_dictionary_lesson01.docx")
        .scalar()
    )
    cz_vid = (
        db_session.query(Vocabulary.id)
        .filter(Vocabulary.name == "czytaj_01_01_x.docx")
        .scalar()
    )
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
    for i in range(20):
        db_session.add(
            LearningUnit(
                text=f"c{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="czytaj.pdf",
                vocabulary_id=cz_vid,
            )
        )
    db_session.commit()

    sess = SessionService(db_session, random_seed=42).create_session(
        mode=StudyModeType.PASSIVE,
        curriculum_mode="lesson",
    )
    assert len(sess.units) == 5
    plua_count = 0
    czytaj_count = 0
    seen_ids: set[int] = set()
    for su in sess.units:
        assert su.unit_id not in seen_ids
        seen_ids.add(su.unit_id)
        u = db_session.query(LearningUnit).filter(LearningUnit.id == su.unit_id).one()
        if u.vocabulary_id == pl_vid:
            plua_count += 1
        elif u.vocabulary_id == cz_vid:
            czytaj_count += 1
        else:
            pytest.fail(f"unexpected vocabulary_id {u.vocabulary_id}")
    assert plua_count == 5
    assert czytaj_count == 0


def test_curriculum_lesson_session_all_plua_when_no_czytaj_vocab(db_session, monkeypatch):
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

    sess = SessionService(db_session, random_seed=42).create_session(
        mode=StudyModeType.PASSIVE,
        curriculum_mode="lesson",
    )
    assert len(sess.units) == 5
    for su in sess.units:
        u = db_session.query(LearningUnit).filter(LearningUnit.id == su.unit_id).one()
        assert u.vocabulary_id == pl_vid


def test_curriculum_lesson_recall_requires_introduced_words(db_session, monkeypatch):
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 1)
    db_session.add(
        Vocabulary(
            user_key="u",
            name="polish_ukrainian_dictionary_lesson01.docx",
            track_type="plua",
            lesson_index=1,
        )
    )
    db_session.flush()
    vid = db_session.query(Vocabulary.id).scalar()
    db_session.add(
        LearningUnit(
            text="w",
            translation="t",
            type=UnitType.WORD,
            source_pdf="pl_ua.pdf",
            vocabulary_id=vid,
        )
    )
    db_session.commit()

    with pytest.raises(InsufficientUnitsError, match="Not enough introduced words"):
        SessionService(db_session, random_seed=1).create_session(
            mode=StudyModeType.RECALL,
            curriculum_mode="lesson",
        )


def test_curriculum_lesson_recall_succeeds_when_introduced(db_session, monkeypatch):
    """Lesson URL may start recall after introduction (no follow_up_session_id)."""
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
    now = datetime.now(UTC)
    units: list[LearningUnit] = []
    for i in range(8):
        u = LearningUnit(
            text=f"w{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="pl_ua.pdf",
            vocabulary_id=pl_vid,
        )
        db_session.add(u)
        units.append(u)
    db_session.flush()
    for u in units[:5]:
        db_session.add(LearningProgress(unit_id=u.id, introduced_at=now))
    db_session.commit()

    sess = SessionService(db_session, random_seed=42).create_session(
        mode=StudyModeType.RECALL,
        curriculum_mode="lesson",
    )
    assert sess.mode == StudyModeType.RECALL
    assert len(sess.units) == 5
    for su in sess.units:
        u = db_session.query(LearningUnit).filter(LearningUnit.id == su.unit_id).one()
        assert u.vocabulary_id == pl_vid


def test_lesson_passive_completion_offers_recall_follow_up(db_session, monkeypatch):
    """Continue-lesson passive should chain to recall on the same words (aligned with weak practice)."""
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
    for su in passive.units:
        svc.submit_answer(
            session_id=passive.id,
            unit_position=su.position,
            is_correct=True,
        )
    db_session.refresh(passive)
    rec = svc.get_next_recommendation(passive.id)
    assert rec["type"] == "recall"
    assert rec["follow_up_session_id"] == passive.id
    assert "lesson" in rec["message"].lower()


def test_study_page_data_skips_resuming_recall_for_lesson_entry(db_session, monkeypatch):
    """Lesson URL uses passive-only UI; do not hydrate an in-flight recall session into the page."""
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 5)
    db_session.add(Vocabulary(id=1, user_key="t", name="doc.pdf"))
    for i in range(10):
        db_session.add(
            LearningUnit(
                text=f"w{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="doc.pdf",
                vocabulary_id=1,
            )
        )
    db_session.commit()
    uids = [u.id for u in db_session.query(LearningUnit).limit(3).all()]
    recall_sess = LearningSession(
        mode=StudyModeType.RECALL,
        status=SessionLifecycleStatus.ACTIVE,
        locked=True,
        completed=False,
    )
    db_session.add(recall_sess)
    db_session.flush()
    for pos, uid in enumerate(uids, start=1):
        db_session.add(
            SessionUnit(
                session_id=recall_sess.id,
                unit_id=uid,
                position=pos,
                answered=False,
            )
        )
    db_session.commit()

    svc = SessionService(db_session, random_seed=1)
    assert svc.get_study_page_data(curriculum_mode="lesson")["session"] is None
    resumed = svc.get_study_page_data()
    assert resumed["session"] is not None
    assert resumed["session"]["id"] == recall_sess.id
