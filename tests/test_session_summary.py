"""Tests for session summary metrics persistence."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, RecallResult
from app.models.session import LearningSession, SessionLifecycleStatus, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SESSION_SIZE, SessionService


@pytest.fixture
def db_session():
    """Create a fresh in-memory database for each test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def sample_units(db_session):
    """Create enough sample learning units for SESSION_SIZE sessions."""
    from app.models.learning_unit import UnitType
    if db_session.query(Vocabulary).filter(Vocabulary.id == 1).first() is None:
        db_session.add(
            Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf")
        )

    units = []
    for i in range(SESSION_SIZE + 15):
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


class TestSessionSummaryMetricsPersistence:
    """Test that session summary metrics are correctly persisted on completion."""
    
    def test_passive_session_metrics_persisted_on_completion(self, db_session, sample_units):
        """Verify metrics are stored when passive session completes."""
        # Create session
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Submit mixed answers: ~75% correct, ~25% wrong
        n_correct = int(SESSION_SIZE * 15 / 20)
        for i, unit in enumerate(session.units):
            is_correct = i < n_correct
            service.submit_answer(
                session_id=session.id,
                unit_position=unit.position,
                is_correct=is_correct,
            )
        
        # Refresh to get updated session
        db_session.refresh(session)
        
        # Verify session is completed
        assert session.completed is True
        assert session.status == SessionLifecycleStatus.COMPLETED
        assert session.completed_at is not None
        
        # Verify summary metrics are persisted
        assert session.summary_total_units == SESSION_SIZE
        assert session.summary_answered_units == SESSION_SIZE
        assert session.summary_correct_count == n_correct
        assert session.summary_partial_count == 0  # Passive mode has no partials
        assert session.summary_failed_count == SESSION_SIZE - n_correct
    
    def test_recall_session_metrics_with_partials(self, db_session, sample_units):
        """Verify metrics include partial count for recall sessions."""
        # Introduce words via Passive mode first (required for Recall gating)
        service = SessionService(db_session, random_seed=42)
        passive_session = service.create_session(mode=StudyModeType.PASSIVE)
        for su in passive_session.units:
            service.submit_answer(
                session_id=passive_session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        # Create recall mode session
        session = service.create_session(mode=StudyModeType.RECALL)
        
        # Get the actual units in the session to craft answers
        session_units = session.units
        
        # Submit answers with different results:
        # - 10 correct (exact match)
        # - 5 partial (1 typo)
        # - 5 failed (wrong answer)
        n_ok = int(SESSION_SIZE * 10 / 20)
        n_partial = int(SESSION_SIZE * 5 / 20)
        for i, su in enumerate(session_units):
            expected_text = su.unit.text
            if i < n_ok:
                # Exact match
                user_input = expected_text
            elif i < n_ok + n_partial:
                # One typo = partial
                user_input = expected_text + "x" if len(expected_text) > 1 else "xx"
            else:
                # Wrong answer = failed
                user_input = "completely_wrong_answer"
            
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input=user_input,
            )
        
        # Refresh session
        db_session.refresh(session)
        
        # Verify completion
        assert session.completed is True
        assert session.status == SessionLifecycleStatus.COMPLETED
        
        # Verify metrics
        # correct_count excludes partials under strict PARTIAL semantics
        n_fail = SESSION_SIZE - n_ok
        assert session.summary_total_units == SESSION_SIZE
        assert session.summary_answered_units == SESSION_SIZE
        assert session.summary_correct_count == n_ok  # Exact matches only
        assert session.summary_partial_count == n_partial
        assert session.summary_failed_count == n_fail  # partials + strict fails
    
    def test_incomplete_session_has_no_persisted_metrics(self, db_session, sample_units):
        """Verify metrics are only persisted when session completes."""
        # Create session
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Submit only 10 answers (half)
        for i, unit in enumerate(session.units[:10]):
            service.submit_answer(
                session_id=session.id,
                unit_position=unit.position,
                is_correct=True,
            )
        
        # Refresh session
        db_session.refresh(session)
        
        # Session should not be completed
        assert session.completed is False
        assert session.status == SessionLifecycleStatus.ACTIVE
        
        # Summary metrics should not be persisted
        assert session.summary_total_units is None
        assert session.summary_answered_units is None
        assert session.summary_correct_count is None
        assert session.summary_partial_count is None
        assert session.summary_failed_count is None


class TestRecallResultStoredOnSessionUnit:
    """Test that recall_result is stored on SessionUnit."""
    
    def test_recall_result_stored_for_recall_mode(self, db_session, sample_units):
        """Verify recall_result is stored on session units."""
        service = SessionService(db_session, random_seed=42)
        
        # Introduce words via Passive mode first (required for Recall gating)
        passive_session = service.create_session(mode=StudyModeType.PASSIVE)
        for su in passive_session.units:
            service.submit_answer(
                session_id=passive_session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        session = service.create_session(mode=StudyModeType.RECALL)
        
        # Get first unit
        su = session.units[0]
        expected_text = su.unit.text
        
        # Submit exact match answer
        service.submit_answer(
            session_id=session.id,
            unit_position=su.position,
            user_input=expected_text,
        )
        
        # Refresh unit
        db_session.refresh(su)
        
        # Verify recall_result is stored
        assert su.recall_result == RecallResult.CORRECT
        assert su.is_correct is True
    
    def test_partial_recall_result_stored(self, db_session, sample_units):
        """Verify partial result is stored on session unit."""
        service = SessionService(db_session, random_seed=42)
        
        # Introduce words via Passive mode first (required for Recall gating)
        passive_session = service.create_session(mode=StudyModeType.PASSIVE)
        for su in passive_session.units:
            service.submit_answer(
                session_id=passive_session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        session = service.create_session(mode=StudyModeType.RECALL)
        
        # Get first unit
        su = session.units[0]
        expected_text = su.unit.text
        
        # Submit answer with 1 typo
        user_input = expected_text + "x"
        service.submit_answer(
            session_id=session.id,
            unit_position=su.position,
            user_input=user_input,
        )
        
        # Refresh unit
        db_session.refresh(su)
        
        # Verify recall_result is stored as PARTIAL
        assert su.recall_result == RecallResult.PARTIAL
        assert su.is_correct is False
    
    def test_failed_recall_result_stored(self, db_session, sample_units):
        """Verify failed result is stored on session unit."""
        service = SessionService(db_session, random_seed=42)
        
        # Introduce words via Passive mode first (required for Recall gating)
        passive_session = service.create_session(mode=StudyModeType.PASSIVE)
        for su in passive_session.units:
            service.submit_answer(
                session_id=passive_session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        session = service.create_session(mode=StudyModeType.RECALL)
        
        # Get first unit
        su = session.units[0]
        
        # Submit completely wrong answer
        service.submit_answer(
            session_id=session.id,
            unit_position=su.position,
            user_input="wrong_answer_that_is_not_close",
        )
        
        # Refresh unit
        db_session.refresh(su)
        
        # Verify recall_result is stored as FAILED
        assert su.recall_result == RecallResult.FAILED
        assert su.is_correct is False
    
    def test_passive_mode_has_no_recall_result(self, db_session, sample_units):
        """Verify passive mode doesn't store recall_result."""
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Get first unit
        su = session.units[0]
        
        # Submit passive mode answer
        service.submit_answer(
            session_id=session.id,
            unit_position=su.position,
            is_correct=True,
        )
        
        # Refresh unit
        db_session.refresh(su)
        
        # Verify recall_result is None for passive mode
        assert su.recall_result is None
        assert su.is_correct is True


class TestSessionComputedProperties:
    """Test computed properties match persisted values."""
    
    def test_computed_properties_available_during_session(self, db_session, sample_units):
        """Verify computed properties work during active session."""
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Before any answers
        assert session.total_units == SESSION_SIZE
        assert session.answered_units == 0
        assert session.correct_count == 0
        assert session.partial_count == 0
        assert session.failed_count == 0
        
        # Answer first 5 correctly
        for su in session.units[:5]:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        db_session.refresh(session)
        
        # Check computed properties
        assert session.total_units == SESSION_SIZE
        assert session.answered_units == 5
        assert session.correct_count == 5
        assert session.failed_count == 0
