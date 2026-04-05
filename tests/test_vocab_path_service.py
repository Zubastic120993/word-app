import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.services.vocab_path_service import (
    classify_vocab_source_pdf,
    compute_next_vocab_focus,
)


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


def test_classify_vocab_source_pdf():
    assert classify_vocab_source_pdf("foo_polish_ukrainian_bar.pdf") == "pl_ua"
    assert classify_vocab_source_pdf("CZYTaj_1.PDF") == "czytaj"
    assert classify_vocab_source_pdf("other.pdf") == "other"
    assert classify_vocab_source_pdf("") == "other"


def test_compute_next_vocab_focus_prefers_pl_ua_then_czytaj_alphabetical(db_session):
    db_session.add_all(
        [
            LearningUnit(
                text="c1",
                translation="t",
                type=UnitType.WORD,
                source_pdf="z_czytaj_a.pdf",
            ),
            LearningUnit(
                text="c2",
                translation="t",
                type=UnitType.WORD,
                source_pdf="a_czytaj_b.pdf",
            ),
            LearningUnit(
                text="p1",
                translation="t",
                type=UnitType.WORD,
                source_pdf="b_polish_ukrainian_x.pdf",
            ),
            LearningUnit(
                text="p2",
                translation="t",
                type=UnitType.WORD,
                source_pdf="a_polish_ukrainian_y.pdf",
            ),
        ]
    )
    db_session.commit()

    focus = compute_next_vocab_focus(db_session)
    assert focus is not None
    assert focus["source"] == "a_polish_ukrainian_y.pdf"
    assert focus["type"] == "pl_ua"
    assert focus["remaining"] == 1


def test_compute_next_vocab_focus_skips_sources_with_progress(db_session):
    u_pl = LearningUnit(
        text="p",
        translation="t",
        type=UnitType.WORD,
        source_pdf="polish_ukrainian_only.pdf",
    )
    u_cz = LearningUnit(
        text="c",
        translation="t",
        type=UnitType.WORD,
        source_pdf="czytaj_only.pdf",
    )
    db_session.add_all([u_pl, u_cz])
    db_session.commit()
    db_session.add(LearningProgress(unit_id=u_pl.id))
    db_session.commit()

    focus = compute_next_vocab_focus(db_session)
    assert focus is not None
    assert focus["source"] == "czytaj_only.pdf"
    assert focus["type"] == "czytaj"


def test_compute_next_vocab_focus_returns_none_when_all_seen(db_session):
    u = LearningUnit(
        text="p",
        translation="t",
        type=UnitType.WORD,
        source_pdf="polish_ukrainian_only.pdf",
    )
    db_session.add(u)
    db_session.commit()
    db_session.add(LearningProgress(unit_id=u.id))
    db_session.commit()

    assert compute_next_vocab_focus(db_session) is None
