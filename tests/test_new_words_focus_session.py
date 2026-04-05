"""Passive new_words_focus skips due-first split so Home 'new words' sessions are not due-heavy at the start."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.session import StudyModeType
from app.models.vocabulary import Vocabulary
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
    if db_session.query(Vocabulary).filter(Vocabulary.id == 1).first() is None:
        db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))
    for i in range(SESSION_SIZE + 20):
        db_session.add(
            LearningUnit(
                text=f"w{i}",
                translation=f"t{i}",
                type=UnitType.WORD,
                source_pdf="czytaj_01_01_test.pdf",
                vocabulary_id=1,
            )
        )
    db_session.commit()


def test_passive_new_words_focus_disables_due_first_split(db_session, sample_units, monkeypatch):
    captured: list[dict] = []
    orig = SessionService._build_selection_request

    def spy(self, **kwargs):
        captured.append(dict(kwargs))
        return orig(self, **kwargs)

    monkeypatch.setattr(SessionService, "_build_selection_request", spy)
    service = SessionService(db_session, random_seed=42)
    service.create_session(mode=StudyModeType.PASSIVE, new_words_focus=True)
    assert captured
    assert all(c.get("use_due_first_split") is False for c in captured)


def test_passive_default_uses_due_first_split(db_session, sample_units, monkeypatch):
    captured: list[dict] = []
    orig = SessionService._build_selection_request

    def spy(self, **kwargs):
        captured.append(dict(kwargs))
        return orig(self, **kwargs)

    monkeypatch.setattr(SessionService, "_build_selection_request", spy)
    service = SessionService(db_session, random_seed=42)
    service.create_session(mode=StudyModeType.PASSIVE, new_words_focus=False)
    assert captured
    assert all(c.get("use_due_first_split") is True for c in captured)


def test_new_words_focus_flag_on_selection_request(db_session, sample_units, monkeypatch):
    captured: list[dict] = []
    orig = SessionService._build_selection_request

    def spy(self, **kwargs):
        captured.append(dict(kwargs))
        return orig(self, **kwargs)

    monkeypatch.setattr(SessionService, "_build_selection_request", spy)
    SessionService(db_session, random_seed=42).create_session(
        mode=StudyModeType.PASSIVE, new_words_focus=True
    )
    assert all(c.get("new_words_focus") is True for c in captured)

    captured.clear()
    SessionService(db_session, random_seed=42).create_session(
        mode=StudyModeType.PASSIVE, new_words_focus=False
    )
    assert all(c.get("new_words_focus") is False for c in captured)


def test_new_words_focus_session_excludes_introduced_when_pool_allows(db_session, monkeypatch):
    """With enough passive-new units, do not fill with already-introduced review words."""
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 12)
    db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))
    fresh: list[LearningUnit] = []
    for i in range(30):
        u = LearningUnit(
            text=f"fresh{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(u)
        fresh.append(u)
    db_session.flush()
    for i in range(30):
        u = LearningUnit(
            text=f"old{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(u)
        db_session.flush()
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=datetime(2026, 1, 1, 12, 0, 0),
                confidence_score=0.85,
            )
        )
    db_session.commit()

    introduced_ids = {
        r.unit_id
        for r in db_session.query(LearningProgress).filter(LearningProgress.introduced_at.isnot(None))
    }

    sess = SessionService(db_session, random_seed=7).create_session(
        mode=StudyModeType.PASSIVE, new_words_focus=True
    )
    for su in sess.units:
        assert su.unit_id not in introduced_ids


def test_new_words_focus_ignores_source_filters(db_session, monkeypatch):
    """Passive new_words_focus uses the full library; UI source_pdfs must not shrink the new pool."""
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 8)
    db_session.add(
        Vocabulary(id=1, user_key="test", name="czytaj_01_01_vocab_a")
    )
    db_session.add(
        Vocabulary(id=2, user_key="test", name="czytaj_01_02_vocab_b")
    )
    db_session.flush()
    for i in range(3):
        db_session.add(
            LearningUnit(
                text=f"a_fresh{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="only_a.pdf",
                vocabulary_id=1,
            )
        )
    for i in range(25):
        db_session.add(
            LearningUnit(
                text=f"b_fresh{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="only_b.pdf",
                vocabulary_id=2,
            )
        )
    db_session.flush()
    for i in range(12):
        u = LearningUnit(
            text=f"a_old{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="only_a.pdf",
            vocabulary_id=1,
        )
        db_session.add(u)
        db_session.flush()
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=datetime(2026, 1, 1, 12, 0, 0),
                confidence_score=0.85,
            )
        )
    db_session.commit()

    introduced_ids = {
        r.unit_id
        for r in db_session.query(LearningProgress).filter(LearningProgress.introduced_at.isnot(None))
    }

    sess = SessionService(db_session, random_seed=11).create_session(
        mode=StudyModeType.PASSIVE,
        new_words_focus=True,
        source_pdfs=["only_a.pdf"],
    )
    assert len(sess.units) == 8
    pdfs_seen: set[str] = set()
    for su in sess.units:
        assert su.unit_id not in introduced_ids
        lu = db_session.query(LearningUnit).filter(LearningUnit.id == su.unit_id).one()
        pdfs_seen.add(lu.source_pdf)
    assert "only_b.pdf" in pdfs_seen


def test_new_words_focus_refill_never_pulls_introduced_short_session(db_session, monkeypatch):
    """When the passive-new pool is smaller than SESSION_SIZE, pad with nothing — no weak/review refill."""
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 12)
    db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))
    fresh: list[LearningUnit] = []
    for i in range(5):
        u = LearningUnit(
            text=f"fresh{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(u)
        fresh.append(u)
    db_session.flush()
    for i in range(25):
        u = LearningUnit(
            text=f"old{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(u)
        db_session.flush()
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=datetime(2026, 1, 1, 12, 0, 0),
                confidence_score=0.85,
            )
        )
    db_session.commit()

    sess = SessionService(db_session, random_seed=3).create_session(
        mode=StudyModeType.PASSIVE, new_words_focus=True
    )
    assert len(sess.units) == 5
    for su in sess.units:
        lp = (
            db_session.query(LearningProgress)
            .filter(LearningProgress.unit_id == su.unit_id)
            .first()
        )
        assert lp is None or lp.introduced_at is None
