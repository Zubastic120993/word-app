"""Tests for Passive → Recall gating functionality.

Ensures that words must be acknowledged in Passive mode before appearing in Recall modes.
"""

import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, LearningProgress, UnitType
from app.models.session import LearningSession, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.utils.time import utc_now
from app.services.session_service import SessionService, InsufficientUnitsError, SESSION_SIZE


@pytest.fixture
def test_db():
    """Create a fresh shared in-memory database for each test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()
    
    app.dependency_overrides[get_db] = override_get_db
    
    yield TestingSessionLocal
    
    Base.metadata.drop_all(engine)
    app.dependency_overrides.clear()


@pytest.fixture
def client(test_db):
    """Create test client with database."""
    return TestClient(app)


@pytest.fixture
def db_session(test_db):
    """Get a database session."""
    return test_db()


@pytest.fixture
def sample_units(db_session):
    """Create sample learning units for testing."""
    if db_session.query(Vocabulary).filter(Vocabulary.id == 1).first() is None:
        db_session.add(
            Vocabulary(id=1, user_key="test", name="czytaj_01_01_test.pdf")
        )

    units = []
    for i in range(80):
        unit = LearningUnit(
            text=f"word{i}",
            type=UnitType.WORD,
            translation=f"translation{i}",
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(unit)
        units.append(unit)
    
    db_session.commit()
    return units


def test_introduced_at_field_exists(db_session, sample_units):
    """Test that introduced_at field exists in LearningProgress model."""
    # Create a progress record
    progress = LearningProgress(
        unit_id=sample_units[0].id,
        times_seen=0,
        times_correct=0,
        times_failed=0,
        confidence_score=0.0,
    )
    db_session.add(progress)
    db_session.commit()
    
    # Verify introduced_at is None by default
    assert progress.introduced_at is None
    
    # Can set introduced_at
    progress.introduced_at = utc_now()
    db_session.commit()
    assert progress.introduced_at is not None


def test_passive_mode_sets_introduced_at_on_first_interaction(db_session, sample_units):
    """Test that Passive mode sets introduced_at on first interaction."""
    service = SessionService(db_session)
    
    # Create a passive session
    session = service.create_session(mode=StudyModeType.PASSIVE)
    
    # Get first unit
    session_unit = session.units[0]
    unit_id = session_unit.unit_id
    
    # Check that progress doesn't exist or introduced_at is None
    progress = db_session.query(LearningProgress).filter(
        LearningProgress.unit_id == unit_id
    ).first()
    if progress:
        assert progress.introduced_at is None
    else:
        # Progress will be created on first answer
        pass
    
    # Submit first answer in passive mode (either "I know" or "I don't know")
    service.submit_answer(
        session_id=session.id,
        unit_position=1,
        is_correct=True,  # "I know"
    )
    
    # Verify introduced_at is now set
    progress = db_session.query(LearningProgress).filter(
        LearningProgress.unit_id == unit_id
    ).first()
    assert progress is not None
    assert progress.introduced_at is not None
    assert isinstance(progress.introduced_at, datetime)


def test_passive_mode_sets_introduced_at_idempotent(db_session, sample_units):
    """Test that introduced_at is only set once (idempotent)."""
    service = SessionService(db_session)
    
    # Create a passive session and answer first unit
    session = service.create_session(mode=StudyModeType.PASSIVE)
    service.submit_answer(
        session_id=session.id,
        unit_position=1,
        is_correct=True,
    )
    
    # Get the introduced_at timestamp
    session_unit = session.units[0]
    progress = db_session.query(LearningProgress).filter(
        LearningProgress.unit_id == session_unit.unit_id
    ).first()
    first_introduced_at = progress.introduced_at
    assert first_introduced_at is not None
    
    # Answer again in another passive session
    session2 = service.create_session(mode=StudyModeType.PASSIVE)
    # Find the same unit in the new session
    same_unit_session_unit = next(
        (su for su in session2.units if su.unit_id == session_unit.unit_id),
        None
    )
    if same_unit_session_unit:
        service.submit_answer(
            session_id=session2.id,
            unit_position=same_unit_session_unit.position,
            is_correct=False,  # "I don't know"
        )
        
        # Verify introduced_at hasn't changed
        db_session.refresh(progress)
        assert progress.introduced_at == first_introduced_at


def test_recall_mode_does_not_set_introduced_at(db_session, sample_units):
    """Test that Recall modes do NOT set introduced_at."""
    service = SessionService(db_session)
    
    # First, introduce ALL words in Passive mode (need SESSION_SIZE introduced for Recall)
    passive_session = service.create_session(mode=StudyModeType.PASSIVE)
    for su in passive_session.units:
        service.submit_answer(
            session_id=passive_session.id,
            unit_position=su.position,
            is_correct=True,
        )
    
    # Get the first unit and verify it's introduced
    session_unit = passive_session.units[0]
    progress = db_session.query(LearningProgress).filter(
        LearningProgress.unit_id == session_unit.unit_id
    ).first()
    original_introduced_at = progress.introduced_at
    assert original_introduced_at is not None
    
    # Now answer in Recall mode
    recall_session = service.create_session(mode=StudyModeType.RECALL)
    # Find the same unit
    same_unit_session_unit = next(
        (su for su in recall_session.units if su.unit_id == session_unit.unit_id),
        None
    )
    if same_unit_session_unit:
        service.submit_answer(
            session_id=recall_session.id,
            unit_position=same_unit_session_unit.position,
            user_input="word0",  # Correct answer
        )
        
        # Verify introduced_at hasn't changed
        db_session.refresh(progress)
        assert progress.introduced_at == original_introduced_at


def test_recall_mode_only_includes_introduced_words(db_session, sample_units):
    """Test that Recall modes only include words where introduced_at IS NOT NULL."""
    service = SessionService(db_session)
    
    # Introduce all words in Passive mode (need SESSION_SIZE introduced for Recall)
    passive_session = service.create_session(mode=StudyModeType.PASSIVE)
    for su in passive_session.units:
        service.submit_answer(
            session_id=passive_session.id,
            unit_position=su.position,
            is_correct=True,
        )
    
    # Verify these units are introduced
    introduced_unit_ids = set()
    for su in passive_session.units:
        progress = db_session.query(LearningProgress).filter(
            LearningProgress.unit_id == su.unit_id
        ).first()
        if progress and progress.introduced_at:
            introduced_unit_ids.add(su.unit_id)
    
    assert len(introduced_unit_ids) == SESSION_SIZE
    
    # Create a Recall session
    recall_session = service.create_session(mode=StudyModeType.RECALL)
    
    # Verify all units in Recall session are introduced
    for su in recall_session.units:
        progress = db_session.query(LearningProgress).filter(
            LearningProgress.unit_id == su.unit_id
        ).first()
        assert progress is not None
        assert progress.introduced_at is not None
        assert su.unit_id in introduced_unit_ids or len(introduced_unit_ids) >= SESSION_SIZE


def test_recall_mode_fails_when_insufficient_introduced_words(db_session, sample_units):
    """Test that Recall mode creation fails when insufficient introduced words exist."""
    service = SessionService(db_session)
    
    # Don't introduce any words - all should have introduced_at = NULL
    
    # Try to create Recall session - should fail
    with pytest.raises(InsufficientUnitsError) as exc_info:
        service.create_session(mode=StudyModeType.RECALL)
    
    assert "Not enough introduced words" in str(exc_info.value)
    assert "Passive mode first" in str(exc_info.value)


def test_passive_mode_prioritizes_unintroduced_words(db_session, sample_units):
    """Test that Passive mode prioritizes units where introduced_at IS NULL."""
    service = SessionService(db_session)
    
    # Introduce some words
    passive_session1 = service.create_session(mode=StudyModeType.PASSIVE)
    for i in range(1, 6):  # Answer first 5 units
        service.submit_answer(
            session_id=passive_session1.id,
            unit_position=i,
            is_correct=True,
        )
    
    # Create another Passive session
    passive_session2 = service.create_session(mode=StudyModeType.PASSIVE)
    
    # Count how many units in session2 are unintroduced
    unintroduced_count = 0
    for su in passive_session2.units:
        progress = db_session.query(LearningProgress).filter(
            LearningProgress.unit_id == su.unit_id
        ).first()
        if not progress or progress.introduced_at is None:
            unintroduced_count += 1
    
    # Should prioritize unintroduced words (most should be unintroduced)
    assert unintroduced_count >= SESSION_SIZE - 5


def test_recall_availability_api_endpoint(client, db_session, sample_units):
    """Test the recall availability API endpoint."""
    # Initially, no words are introduced
    response = client.get("/api/sessions/recall-availability")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is False
    assert data["introduced_count"] == 0
    assert data["required_count"] == SESSION_SIZE
    assert "Passive mode" in data["message"]
    
    # Introduce some words via Passive mode
    service = SessionService(db_session)
    passive_session = service.create_session(mode=StudyModeType.PASSIVE)
    for i in range(1, SESSION_SIZE + 1):  # Introduce exactly SESSION_SIZE words
        service.submit_answer(
            session_id=passive_session.id,
            unit_position=i,
            is_correct=True,
        )
    
    # Now check availability again
    response = client.get("/api/sessions/recall-availability")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is True
    assert data["introduced_count"] >= SESSION_SIZE
    assert "available" in data["message"].lower()


def test_introduced_at_does_not_affect_scoring(db_session, sample_units):
    """Test that introduced_at has NO effect on confidence_score, time decay, or SRS scheduling."""
    service = SessionService(db_session)
    
    # Create passive session and answer
    passive_session = service.create_session(mode=StudyModeType.PASSIVE)
    service.submit_answer(
        session_id=passive_session.id,
        unit_position=1,
        is_correct=True,
    )
    
    # Get progress
    session_unit = passive_session.units[0]
    progress = db_session.query(LearningProgress).filter(
        LearningProgress.unit_id == session_unit.unit_id
    ).first()
    
    # Record values before introducing
    confidence_before = progress.confidence_score
    next_review_before = progress.next_review_at
    times_seen_before = progress.times_seen
    times_correct_before = progress.times_correct
    
    # Verify introduced_at is set
    assert progress.introduced_at is not None
    
    # Answer again in passive mode
    passive_session2 = service.create_session(mode=StudyModeType.PASSIVE)
    same_unit_session_unit = next(
        (su for su in passive_session2.units if su.unit_id == session_unit.unit_id),
        None
    )
    if same_unit_session_unit:
        service.submit_answer(
            session_id=passive_session2.id,
            unit_position=same_unit_session_unit.position,
            is_correct=True,
        )
        
        # Refresh progress
        db_session.refresh(progress)
        
        # Verify scoring still works (confidence increased, times_seen increased)
        assert progress.times_seen > times_seen_before
        assert progress.times_correct > times_correct_before
        # Confidence should have increased (or at least be calculated)
        assert progress.confidence_score >= confidence_before
        # SRS scheduling should still work
        assert progress.next_review_at is not None


def test_recall_visual_and_audio_both_gated(db_session, sample_units):
    """Test that both Recall (Visual) and Recall (Audio) are gated by introduced_at."""
    service = SessionService(db_session)
    
    # No words introduced - both should fail
    with pytest.raises(InsufficientUnitsError):
        service.create_session(mode=StudyModeType.RECALL)
    
    with pytest.raises(InsufficientUnitsError):
        service.create_session(mode=StudyModeType.RECALL_AUDIO)
    
    # Introduce words
    passive_session = service.create_session(mode=StudyModeType.PASSIVE)
    for i in range(1, SESSION_SIZE + 1):
        service.submit_answer(
            session_id=passive_session.id,
            unit_position=i,
            is_correct=True,
        )
    
    # Now both should work
    recall_visual = service.create_session(mode=StudyModeType.RECALL)
    assert len(recall_visual.units) == SESSION_SIZE
    
    recall_audio = service.create_session(mode=StudyModeType.RECALL_AUDIO)
    assert len(recall_audio.units) == SESSION_SIZE
    
    # Verify all units in both sessions are introduced
    for su in recall_visual.units:
        progress = db_session.query(LearningProgress).filter(
            LearningProgress.unit_id == su.unit_id
        ).first()
        assert progress.introduced_at is not None
    
    for su in recall_audio.units:
        progress = db_session.query(LearningProgress).filter(
            LearningProgress.unit_id == su.unit_id
        ).first()
        assert progress.introduced_at is not None
