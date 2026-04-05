"""Tests for Session Resume — abandon_session service method."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, UnitType
from app.models.session import LearningSession, SessionLifecycleStatus, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SESSION_SIZE, SessionService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def sample_units(db_session):
    db_session.add(Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf"))
    for i in range(SESSION_SIZE + 5):
        db_session.add(LearningUnit(
            text=f"word{i}",
            translation=f"tr{i}",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        ))
    db_session.commit()


# ---------------------------------------------------------------------------
# abandon_session — service method
# ---------------------------------------------------------------------------

class TestAbandonSession:
    def test_abandon_created_session(self, db_session, sample_units):
        service = SessionService(db_session, random_seed=1)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        assert session.status == SessionLifecycleStatus.CREATED

        service.abandon_session(session.id)

        db_session.refresh(session)
        assert session.status == SessionLifecycleStatus.ABANDONED
        assert session.completed is False
        assert session.abandoned_at is not None

    def test_abandon_active_session(self, db_session, sample_units):
        """Session that has at least one answer (status=ACTIVE) can be abandoned."""
        service = SessionService(db_session, random_seed=2)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        # Submit one answer to move status to ACTIVE
        first_unit = sorted(session.units, key=lambda u: u.position)[0]
        service.submit_answer(session.id, unit_position=first_unit.position, is_correct=True)
        db_session.refresh(session)
        assert session.status == SessionLifecycleStatus.ACTIVE

        service.abandon_session(session.id)

        db_session.refresh(session)
        assert session.status == SessionLifecycleStatus.ABANDONED

    def test_abandon_raises_404_for_missing_session(self, db_session):
        service = SessionService(db_session)
        with pytest.raises(ValueError, match="not found"):
            service.abandon_session(99999)

    def test_abandon_raises_400_for_completed_session(self, db_session, sample_units):
        service = SessionService(db_session, random_seed=3)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        # Answer all units to complete the session
        for su in sorted(session.units, key=lambda u: u.position):
            service.submit_answer(session.id, unit_position=su.position, is_correct=True)
        db_session.refresh(session)
        assert session.status == SessionLifecycleStatus.COMPLETED

        with pytest.raises(ValueError, match="cannot be abandoned"):
            service.abandon_session(session.id)

    def test_abandon_does_not_affect_other_sessions(self, db_session, sample_units):
        """Abandoning one session must not touch any other session."""
        service = SessionService(db_session, random_seed=4)
        s1 = service.create_session(mode=StudyModeType.PASSIVE)
        # close_incomplete_sessions is called inside create_session, so s1 is the only active one
        s1_id = s1.id

        # Manually create a second session without going through create_session
        # (to bypass close_incomplete_sessions and test isolation directly)
        s2 = LearningSession(
            mode=StudyModeType.PASSIVE,
            status=SessionLifecycleStatus.CREATED,
            locked=True,
            completed=False,
        )
        db_session.add(s2)
        db_session.commit()
        s2_id = s2.id

        service.abandon_session(s1_id)

        db_session.refresh(s2)
        assert s2.status == SessionLifecycleStatus.CREATED, (
            "abandon_session must not touch unrelated sessions"
        )

    def test_abandon_idempotent_guard(self, db_session, sample_units):
        """Calling abandon twice raises ValueError on the second call."""
        service = SessionService(db_session, random_seed=5)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        service.abandon_session(session.id)

        with pytest.raises(ValueError, match="cannot be abandoned"):
            service.abandon_session(session.id)

    def test_resume_position_first_unanswered(self, db_session, sample_units):
        """After partial answers, answered_units count is correct for position restore."""
        service = SessionService(db_session, random_seed=6)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        units_sorted = sorted(session.units, key=lambda u: u.position)

        # Answer 3 units
        for su in units_sorted[:3]:
            service.submit_answer(session.id, unit_position=su.position, is_correct=True)

        # Simulate what get_study_page_data returns for answered_units
        db_session.refresh(session)
        answered_count = sum(1 for su in session.units if su.answered)
        assert answered_count == 3
        # currentPosition on frontend = answered_units + 1 = 4
        assert answered_count + 1 == 4
