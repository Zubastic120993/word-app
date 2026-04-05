"""Tests for primary curriculum map (PL–UA lesson_index on vocabularies)."""

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.vocabulary import Vocabulary
from app.services.lesson_service import (
    build_primary_curriculum_map,
    detect_current_plua_lesson,
    get_plua_lesson_progress,
    is_plua_lesson_completed,
)
from app.services.progress_metrics_service import MASTERY_THRESHOLD
from app.services.session_service import _is_lesson_completed


def _make_plua_vocab_with_units(db_session, n_units: int) -> tuple[Vocabulary, list[LearningUnit]]:
    v = Vocabulary(
        user_key="u",
        name="polish_ukrainian_dictionary_lesson01.docx",
        track_type="plua",
        lesson_index=1,
    )
    db_session.add(v)
    db_session.flush()
    units: list[LearningUnit] = []
    for i in range(n_units):
        u = LearningUnit(
            text=f"w{i}",
            type=UnitType.WORD,
            translation=f"t{i}",
            source_pdf="x.pdf",
            vocabulary_id=v.id,
            normalized_text=f"w{i}",
            normalized_translation=f"t{i}",
        )
        db_session.add(u)
        units.append(u)
    db_session.flush()
    return v, units


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


def test_build_primary_curriculum_map_groups_by_lesson(db_session):
    db_session.add_all(
        [
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson02.docx",
                track_type="plua",
                lesson_index=2,
            ),
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson01.docx",
                track_type="plua",
                lesson_index=1,
            ),
        ]
    )
    db_session.commit()

    m = build_primary_curriculum_map(db_session)
    assert list(m.keys()) == [1, 2]
    assert len(m[1]) == 1 and len(m[2]) == 1


def test_build_primary_curriculum_map_ignores_non_plua(db_session):
    db_session.add_all(
        [
            Vocabulary(
                user_key="u",
                name="czytaj_01_01_x.docx",
                track_type="czytaj",
                lesson_index=None,
            ),
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson01.docx",
                track_type="plua",
                lesson_index=1,
            ),
        ]
    )
    db_session.commit()

    m = build_primary_curriculum_map(db_session)
    assert m == {1: [2]}


def test_get_plua_lesson_progress_empty_map(db_session):
    assert get_plua_lesson_progress(db_session, 1) == {
        "total": 0,
        "introduced": 0,
        "percent": 0,
    }


def test_get_plua_lesson_progress_no_units(db_session):
    db_session.add(
        Vocabulary(
            user_key="u",
            name="polish_ukrainian_dictionary_lesson01.docx",
            track_type="plua",
            lesson_index=1,
        )
    )
    db_session.commit()
    assert get_plua_lesson_progress(db_session, 1) == {
        "total": 0,
        "introduced": 0,
        "percent": 0,
    }


def test_get_plua_lesson_progress_introduced_ratio(db_session):
    v = Vocabulary(
        user_key="u",
        name="polish_ukrainian_dictionary_lesson01.docx",
        track_type="plua",
        lesson_index=1,
    )
    db_session.add(v)
    db_session.flush()

    now = datetime.now(UTC)
    units: list[LearningUnit] = []
    for i in range(3):
        u = LearningUnit(
            text=f"w{i}",
            type=UnitType.WORD,
            translation=f"t{i}",
            source_pdf="x.pdf",
            vocabulary_id=v.id,
            normalized_text=f"w{i}",
            normalized_translation=f"t{i}",
        )
        db_session.add(u)
        units.append(u)
    db_session.flush()

    db_session.add(LearningProgress(unit_id=units[0].id, introduced_at=now))
    db_session.add(LearningProgress(unit_id=units[1].id, introduced_at=now))
    db_session.add(LearningProgress(unit_id=units[2].id, introduced_at=None))
    db_session.commit()

    out = get_plua_lesson_progress(db_session, 1)
    assert out["total"] == 3
    assert out["introduced"] == 2
    assert out["percent"] == 66


def test_detect_current_plua_lesson_empty_map_returns_1(db_session):
    assert detect_current_plua_lesson(db_session) == 1


def test_detect_current_plua_lesson_first_incomplete(monkeypatch, db_session):
    db_session.add_all(
        [
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson01.docx",
                track_type="plua",
                lesson_index=1,
            ),
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson02.docx",
                track_type="plua",
                lesson_index=2,
            ),
        ]
    )
    db_session.commit()

    def fake_completed(db, lesson_id, lesson_to_vocab):
        return lesson_id >= 2

    monkeypatch.setattr(
        "app.services.lesson_service._is_lesson_completed",
        fake_completed,
    )
    assert detect_current_plua_lesson(db_session) == 1


def test_detect_current_plua_lesson_all_complete_returns_max(monkeypatch, db_session):
    db_session.add_all(
        [
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson01.docx",
                track_type="plua",
                lesson_index=1,
            ),
            Vocabulary(
                user_key="u",
                name="polish_ukrainian_dictionary_lesson02.docx",
                track_type="plua",
                lesson_index=2,
            ),
        ]
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.lesson_service._is_lesson_completed",
        lambda db, lesson_id, lesson_to_vocab: True,
    )
    assert detect_current_plua_lesson(db_session) == 2


def test_is_plua_lesson_completed_empty_curriculum_false(db_session):
    """No PL–UA vocabulary rows → lesson cannot be complete."""
    assert is_plua_lesson_completed(db_session, 1) is False


def test_is_lesson_completed_no_units_in_lesson_false(db_session):
    v = Vocabulary(
        user_key="u",
        name="polish_ukrainian_dictionary_lesson01.docx",
        track_type="plua",
        lesson_index=1,
    )
    db_session.add(v)
    db_session.commit()
    assert _is_lesson_completed(db_session, 1, {1: [v.id]}) is False


def test_is_lesson_completed_passes_when_all_introduced_and_eighty_percent_mastered(
    db_session, caplog,
):
    now = datetime.now(UTC)
    v, units = _make_plua_vocab_with_units(db_session, 10)
    for i, u in enumerate(units):
        conf = MASTERY_THRESHOLD if i < 8 else 0.5
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=now,
                confidence_score=conf,
            )
        )
    db_session.commit()
    with caplog.at_level(logging.INFO, logger="app.services.session_service"):
        assert _is_lesson_completed(db_session, 1, {1: [v.id]}) is True
    assert "lesson completed via threshold" in caplog.text
    assert "mastery_ratio=0.80" in caplog.text


def test_is_lesson_completed_fails_when_not_all_introduced(db_session):
    now = datetime.now(UTC)
    v, units = _make_plua_vocab_with_units(db_session, 5)
    for i, u in enumerate(units):
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=now if i < 4 else None,
                confidence_score=MASTERY_THRESHOLD,
            )
        )
    db_session.commit()
    assert _is_lesson_completed(db_session, 1, {1: [v.id]}) is False


def test_is_lesson_completed_fails_when_mastery_ratio_below_pass_ratio(db_session):
    now = datetime.now(UTC)
    v, units = _make_plua_vocab_with_units(db_session, 10)
    for i, u in enumerate(units):
        conf = MASTERY_THRESHOLD if i < 7 else 0.5
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=now,
                confidence_score=conf,
            )
        )
    db_session.commit()
    assert _is_lesson_completed(db_session, 1, {1: [v.id]}) is False


def test_is_lesson_completed_fails_when_progress_row_missing(db_session):
    now = datetime.now(UTC)
    v, units = _make_plua_vocab_with_units(db_session, 3)
    for u in units[:2]:
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=now,
                confidence_score=MASTERY_THRESHOLD,
            )
        )
    db_session.commit()
    assert _is_lesson_completed(db_session, 1, {1: [v.id]}) is False


def test_is_plua_lesson_completed_true_when_eighty_percent_mastered(db_session):
    now = datetime.now(UTC)
    v, units = _make_plua_vocab_with_units(db_session, 5)
    for i, u in enumerate(units):
        conf = MASTERY_THRESHOLD if i < 4 else 0.2
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=now,
                confidence_score=conf,
            )
        )
    db_session.commit()
    assert is_plua_lesson_completed(db_session, 1) is True


