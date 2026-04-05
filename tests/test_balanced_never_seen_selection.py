"""Balanced 60/40 never-seen mix for passive full-library sessions."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.vocabulary import Vocabulary
from app.models.session import StudyModeType
from app.services.session_service import BUCKET_NEW_PERCENT, SessionService


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


def _passive_balanced_request(service: SessionService, session_size: int):
    return service._build_selection_request(
        mode="normal",
        study_mode=StudyModeType.PASSIVE,
        session_size=session_size,
        pool_kind="normal",
        use_due_first_split=True,
        now=datetime(2026, 1, 1, 12, 0, 0),
        balanced_never_seen_mix=True,
    )


def test_balanced_never_seen_60_40_when_both_pools_plenty(db_session):
    svc = SessionService(db_session, random_seed=42)
    for i in range(100):
        db_session.add(
            LearningUnit(
                text=f"p{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="lesson_polish_ukrainian_a.pdf",
            )
        )
    for i in range(100):
        db_session.add(
            LearningUnit(
                text=f"c{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="reader_czytaj_b.pdf",
            )
        )
    db_session.commit()

    req = _passive_balanced_request(svc, 20)
    target_new = int(20 * BUCKET_NEW_PERCENT)
    n_pl = round(target_new * 0.6)
    n_cz = target_new - n_pl

    picked = svc._pick_never_seen_balanced(req, target_new, set())
    assert len(picked) == target_new
    pl_n = sum(1 for u in picked if "polish_ukrainian" in u.source_pdf.lower())
    cz_n = sum(1 for u in picked if "czytaj" in u.source_pdf.lower())
    assert pl_n == n_pl
    assert cz_n == n_cz


def test_balanced_only_polish_ukrainian_fills_from_overflow(db_session):
    svc = SessionService(db_session, random_seed=42)
    for i in range(100):
        db_session.add(
            LearningUnit(
                text=f"p{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="polish_ukrainian_x.pdf",
            )
        )
    db_session.commit()

    req = _passive_balanced_request(svc, 20)
    target_new = int(20 * BUCKET_NEW_PERCENT)
    picked = svc._pick_never_seen_balanced(req, target_new, set())
    assert len(picked) == target_new
    assert all("polish_ukrainian" in u.source_pdf.lower() for u in picked)


def test_balanced_only_czytaj_fills_from_overflow(db_session):
    svc = SessionService(db_session, random_seed=42)
    for i in range(100):
        db_session.add(
            LearningUnit(
                text=f"c{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="vol_czytaj_1.pdf",
            )
        )
    db_session.commit()

    req = _passive_balanced_request(svc, 20)
    target_new = int(20 * BUCKET_NEW_PERCENT)
    picked = svc._pick_never_seen_balanced(req, target_new, set())
    assert len(picked) == target_new
    assert all("czytaj" in u.source_pdf.lower() for u in picked)


def test_balanced_partial_pl_shortage_uses_cz_overflow(db_session):
    """Fewer PL units than n_pl: overflow fills remaining slots from Czytaj tail."""
    svc = SessionService(db_session, random_seed=42)
    for i in range(10):
        db_session.add(
            LearningUnit(
                text=f"p{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="polish_ukrainian_s.pdf",
            )
        )
    for i in range(100):
        db_session.add(
            LearningUnit(
                text=f"c{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="czytaj_s.pdf",
            )
        )
    db_session.commit()

    req = _passive_balanced_request(svc, 20)
    target_new = 20
    n_pl = round(target_new * 0.6)
    assert n_pl == 12 and target_new - n_pl == 8

    picked = svc._pick_never_seen_balanced(req, target_new, set())
    assert len(picked) == target_new
    pl_n = sum(1 for u in picked if "polish_ukrainian" in u.source_pdf.lower())
    cz_n = sum(1 for u in picked if "czytaj" in u.source_pdf.lower())
    assert pl_n == 10
    assert cz_n == 10


def test_other_category_only_when_both_main_pools_insufficient_for_quotas(db_session):
    svc = SessionService(db_session, random_seed=42)
    for i in range(5):
        db_session.add(
            LearningUnit(
                text=f"p{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="polish_ukrainian_q.pdf",
            )
        )
    for i in range(3):
        db_session.add(
            LearningUnit(
                text=f"c{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="czytaj_q.pdf",
            )
        )
    for i in range(20):
        db_session.add(
            LearningUnit(
                text=f"o{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="misc_other.pdf",
            )
        )
    db_session.commit()

    req = _passive_balanced_request(svc, 20)
    target_new = 10
    picked = svc._pick_never_seen_balanced(req, target_new, set())
    assert len(picked) == 10
    other_n = sum(
        1
        for u in picked
        if "misc_other" in u.source_pdf and "polish_ukrainian" not in u.source_pdf.lower()
    )
    assert other_n == 2


def test_balanced_shortfall_fills_from_weighted_new_pool(db_session):
    """After never-seen quotas, remaining target_new slots use pool['new_units'] (SRS-safe fallback)."""
    svc = SessionService(db_session, random_seed=42)
    for i in range(2):
        db_session.add(
            LearningUnit(
                text=f"n{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="polish_ukrainian_fill.pdf",
            )
        )
    for i in range(30):
        u = LearningUnit(
            text=f"i{i}",
            translation="t",
            type=UnitType.WORD,
            source_pdf="czytaj_fill.pdf",
        )
        db_session.add(u)
        db_session.flush()
        db_session.add(
            LearningProgress(
                unit_id=u.id,
                introduced_at=None,
                confidence_score=0.0,
            )
        )
    db_session.commit()

    req = _passive_balanced_request(svc, 20)
    pool = svc._get_normal_pool(req)
    target_new = int(20 * BUCKET_NEW_PERCENT)
    selected_ids: set[int] = set()
    balanced = svc._pick_never_seen_balanced(req, target_new, selected_ids)
    assert len(balanced) == 2
    shortfall = target_new - len(balanced)
    fill = svc._weighted_random_sample(pool["new_units"], shortfall, selected_ids)
    assert len(fill) == shortfall
    assert len(selected_ids) == target_new


def test_source_filter_disables_balanced_in_create_session_flag(monkeypatch, db_session):
    """When source_pdfs is set, create_session must not turn on balanced_never_seen_mix."""
    monkeypatch.setattr("app.services.session_service.SESSION_SIZE", 5)
    monkeypatch.setattr(
        "app.services.session_service.build_lesson_to_vocab",
        lambda _db: {1: [1]},
    )

    db_session.add(Vocabulary(id=1, user_key="t", name="czytaj_01_01_only.pdf"))
    for i in range(8):
        db_session.add(
            LearningUnit(
                text=f"w{i}",
                translation="t",
                type=UnitType.WORD,
                source_pdf="czytaj_only.pdf",
                vocabulary_id=1,
            )
        )
    db_session.commit()

    seen_balanced: list[bool] = []
    orig = SessionService._build_selection_request

    def spy(self, **kwargs):
        seen_balanced.append(bool(kwargs.get("balanced_never_seen_mix")))
        return orig(self, **kwargs)

    monkeypatch.setattr(SessionService, "_build_selection_request", spy)

    SessionService(db_session, random_seed=42).create_session(
        mode=StudyModeType.PASSIVE,
        source_pdfs=["czytaj_only.pdf"],
        lesson_id=1,
    )
    assert seen_balanced and all(b is False for b in seen_balanced)
