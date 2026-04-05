import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, UnitType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SessionService, StudyModeType, SESSION_SIZE


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_units(db_session):
    vocab = Vocabulary(
        id=1,
        user_key="test",
        name="czytaj_01_01_test.pdf",
    )
    db_session.add(vocab)
    db_session.flush()

    # create more than SESSION_SIZE to allow proper selection
    for i in range(SESSION_SIZE + 20):
        unit = LearningUnit(
            text=f"word{i}",
            translation=f"trans{i}",
            type=UnitType.WORD,
            vocabulary_id=vocab.id,
            source_pdf=vocab.name,
        )
        db_session.add(unit)

    db_session.commit()


def test_normal_passive_session_has_full_unique_unit_set(db_session, sample_units):
    service = SessionService(db_session, random_seed=42)

    session = service.create_session(mode=StudyModeType.PASSIVE)

    ids = [su.unit_id for su in sorted(session.units, key=lambda u: u.position)]

    assert len(ids) == SESSION_SIZE
    assert len(ids) == len(set(ids))
