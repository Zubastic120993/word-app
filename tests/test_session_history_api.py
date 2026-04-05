"""Tests for session history API endpoint."""

import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, UnitType
from app.models.session import LearningSession, SessionLifecycleStatus, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.utils.time import utc_now
from app.services.session_service import SESSION_SIZE, SessionService


def _count_selects(bind, fn):
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(bind, "before_cursor_execute", before_cursor_execute)
    try:
        fn()
    finally:
        event.remove(bind, "before_cursor_execute", before_cursor_execute)
    return len(statements)


@pytest.fixture
def test_db():
    """Create a fresh shared in-memory database for each test."""
    # Use StaticPool with check_same_thread=False to share connection
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
    """Get database session for test setup."""
    session = test_db()
    yield session
    session.close()


@pytest.fixture
def sample_units(db_session):
    """Create enough sample learning units for SESSION_SIZE sessions."""
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


class TestSessionHistoryEndpoint:
    """Test GET /api/sessions/history endpoint."""
    
    def test_empty_history_returns_empty_list(self, client, db_session, sample_units):
        """Test that empty history returns empty list."""
        response = client.get("/api/sessions/history")
        
        assert response.status_code == 200
        data = response.json()
        assert data["sessions"] == []
        assert data["total"] == 0
        assert data["limit"] == 20
        assert data["offset"] == 0

    def test_history_excludes_active_sessions(self, client, db_session, sample_units):
        """Test that active sessions are excluded from history results."""
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        session.status = SessionLifecycleStatus.ACTIVE
        db_session.commit()

        response = client.get("/api/sessions/history")

        assert response.status_code == 200
        data = response.json()
        assert all(item["status"] != "ACTIVE" for item in data["sessions"])

    def test_completed_session_appears_in_history(self, client, db_session, sample_units):
        """Test that completed sessions appear in history."""
        # Create and complete a session
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Complete all units
        for su in session.units:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        db_session.commit()
        
        # Get history
        response = client.get("/api/sessions/history")
        
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["sessions"]) == 1
        
        session_item = data["sessions"][0]
        assert session_item["session_id"] == session.id
        assert session_item["status"] == "completed"
        assert session_item["total_units"] == SESSION_SIZE
        assert session_item["correct_count"] == SESSION_SIZE
    
    def test_history_ordered_by_completed_at_desc(self, client, db_session, sample_units):
        """Test that sessions are ordered by completed_at DESC."""
        service = SessionService(db_session, random_seed=42)
        
        # Create and complete 3 sessions with delays
        session_ids = []
        for i in range(3):
            session = service.create_session(mode=StudyModeType.PASSIVE)
            for su in session.units:
                service.submit_answer(
                    session_id=session.id,
                    unit_position=su.position,
                    is_correct=True,
                )
            session_ids.append(session.id)
            
            # Manually set completed_at to control ordering
            db_session.refresh(session)
            session.completed_at = utc_now() - timedelta(hours=3-i)
            db_session.commit()
        
        # Get history
        response = client.get("/api/sessions/history")
        
        assert response.status_code == 200
        data = response.json()
        
        # Sessions should be ordered by completed_at DESC (most recent first)
        result_ids = [s["session_id"] for s in data["sessions"]]
        assert result_ids == list(reversed(session_ids))
    
    def test_pagination_with_limit(self, client, db_session, sample_units):
        """Test pagination with limit parameter."""
        service = SessionService(db_session, random_seed=42)
        
        # Create 5 completed sessions
        for i in range(5):
            session = service.create_session(mode=StudyModeType.PASSIVE)
            for su in session.units:
                service.submit_answer(
                    session_id=session.id,
                    unit_position=su.position,
                    is_correct=True,
                )
        
        # Request with limit=2
        response = client.get("/api/sessions/history?limit=2")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data["sessions"]) == 2
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 0
    
    def test_pagination_with_offset(self, client, db_session, sample_units):
        """Test pagination with offset parameter."""
        service = SessionService(db_session, random_seed=42)
        
        # Create 5 completed sessions
        session_ids = []
        for i in range(5):
            session = service.create_session(mode=StudyModeType.PASSIVE)
            for su in session.units:
                service.submit_answer(
                    session_id=session.id,
                    unit_position=su.position,
                    is_correct=True,
                )
            session_ids.append(session.id)
        
        # Request with offset=2
        response = client.get("/api/sessions/history?limit=3&offset=2")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data["sessions"]) == 3
        assert data["total"] == 5
        assert data["offset"] == 2

    def test_history_page_data_batches_session_vocabulary_queries(self, db_session, sample_units):
        """History page data should not execute one vocabulary query per session."""
        service = SessionService(db_session, random_seed=42)

        for _ in range(3):
            session = service.create_session(mode=StudyModeType.PASSIVE)
            for su in session.units:
                service.submit_answer(
                    session_id=session.id,
                    unit_position=su.position,
                    is_correct=True,
                )

        select_count = _count_selects(
            db_session.bind,
            lambda: service.get_history_page_data(limit=20, offset=0),
        )

        assert select_count <= 8
    
    def test_response_schema_contains_required_fields(self, client, db_session, sample_units):
        """Test that response contains all required fields."""
        service = SessionService(db_session, random_seed=42)
        
        # Introduce words via Passive mode first (required for Recall gating)
        passive_session = service.create_session(mode=StudyModeType.PASSIVE)
        for su in passive_session.units:
            service.submit_answer(
                session_id=passive_session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        # Create a recall mode session
        session = service.create_session(mode=StudyModeType.RECALL)
        
        n_ok = int(SESSION_SIZE * 10 / 20)
        n_partial = int(SESSION_SIZE * 5 / 20)
        n_fail = SESSION_SIZE - n_ok
        # Complete with mix of correct, partial, and failed
        for i, su in enumerate(session.units):
            expected_text = su.unit.text
            if i < n_ok:
                user_input = expected_text  # Correct
            elif i < n_ok + n_partial:
                user_input = expected_text + "x"  # Partial (1 typo)
            else:
                user_input = "wrong_answer"  # Failed
            
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input=user_input,
            )
        
        # Get history
        response = client.get("/api/sessions/history")
        
        assert response.status_code == 200
        data = response.json()
        
        session_item = data["sessions"][0]
        
        # Verify all required fields present
        assert "session_id" in session_item
        assert "date" in session_item
        assert "mode" in session_item
        assert "total_units" in session_item
        assert "correct_count" in session_item
        assert "partial_count" in session_item
        assert "failed_count" in session_item
        assert "status" in session_item
        
        # Verify values
        # correct_count excludes partials under strict PARTIAL semantics
        assert session_item["mode"] == "recall"
        assert session_item["total_units"] == SESSION_SIZE
        assert session_item["correct_count"] == n_ok  # Exact matches only
        assert session_item["partial_count"] == n_partial
        assert session_item["failed_count"] == n_fail
    
    def test_abandoned_session_marked_correctly(self, client, db_session, sample_units):
        """Test that an interrupted session is marked as abandoned."""
        service = SessionService(db_session, random_seed=42)
        
        # Create session and answer only some units
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Answer only first 5 units
        for su in session.units[:5]:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                is_correct=True,
            )

        service.close_incomplete_sessions()
        
        # Get history
        response = client.get("/api/sessions/history")
        
        assert response.status_code == 200
        data = response.json()
        
        session_item = data["sessions"][0]
        assert session_item["status"] == "abandoned"
    
    def test_active_session_marked_correctly(self, client, db_session, sample_units):
        """Test that untouched active sessions are excluded from history."""
        service = SessionService(db_session, random_seed=42)
        
        # Create session but don't answer anything
        session = service.create_session(mode=StudyModeType.PASSIVE)
        db_session.commit()
        
        # Get history
        response = client.get("/api/sessions/history")
        
        assert response.status_code == 200
        data = response.json()
        assert data["sessions"] == []
    
    def test_limit_validation_min(self, client, db_session, sample_units):
        """Test that limit must be at least 1."""
        response = client.get("/api/sessions/history?limit=0")
        
        assert response.status_code == 422  # Validation error
    
    def test_limit_validation_max(self, client, db_session, sample_units):
        """Test that limit cannot exceed 100."""
        response = client.get("/api/sessions/history?limit=101")
        
        assert response.status_code == 422  # Validation error
    
    def test_offset_validation_min(self, client, db_session, sample_units):
        """Test that offset must be non-negative."""
        response = client.get("/api/sessions/history?offset=-1")
        
        assert response.status_code == 422  # Validation error


class TestSessionHistoryModes:
    """Test history behavior with different session modes."""
    
    def test_passive_mode_session_in_history(self, client, db_session, sample_units):
        """Test passive mode sessions appear correctly."""
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        for su in session.units:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        response = client.get("/api/sessions/history")
        data = response.json()
        
        assert data["sessions"][0]["mode"] == "passive"
        assert data["sessions"][0]["partial_count"] == 0  # Passive has no partials
    
    def test_recall_mode_session_in_history(self, client, db_session, sample_units):
        """Test recall mode sessions appear correctly."""
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
        
        for su in session.units:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                user_input=su.unit.text,  # All correct
            )
        
        response = client.get("/api/sessions/history")
        data = response.json()
        
        assert data["sessions"][0]["mode"] == "recall"


class TestSessionDetailView:
    """Test GET /history/{session_id} UI endpoint."""
    
    def test_session_detail_page_renders(self, client, db_session, sample_units):
        """Test that session detail page renders successfully."""
        # Create and complete a session
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        for su in session.units:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        db_session.commit()
        
        # Access detail page
        response = client.get(f"/history/{session.id}")
        
        assert response.status_code == 200
        assert b"Session" in response.content
    
    def test_session_detail_shows_units(self, client, db_session, sample_units):
        """Test that session detail shows unit information."""
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
        
        # Answer first unit
        first_unit = session.units[0]
        service.submit_answer(
            session_id=session.id,
            unit_position=first_unit.position,
            user_input=first_unit.unit.text,  # Correct answer
        )
        
        db_session.commit()
        
        # Access detail page
        response = client.get(f"/history/{session.id}")
        
        assert response.status_code == 200
        # Should contain the unit's text
        assert first_unit.unit.text.encode() in response.content
    
    def test_nonexistent_session_redirects(self, client, db_session, sample_units):
        """Test that accessing nonexistent session redirects to history."""
        response = client.get("/history/99999", follow_redirects=False)
        
        assert response.status_code == 302
        assert response.headers["location"] == "/history"
    
    def test_session_detail_is_readonly(self, client, db_session, sample_units):
        """Test that session detail page is read-only (no forms for editing)."""
        service = SessionService(db_session, random_seed=42)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        
        # Complete session
        for su in session.units:
            service.submit_answer(
                session_id=session.id,
                unit_position=su.position,
                is_correct=True,
            )
        
        db_session.commit()
        
        response = client.get(f"/history/{session.id}")
        
        assert response.status_code == 200
        # Should NOT contain any form elements for answering
        content = response.content.decode()
        assert '<form' not in content.lower() or 'method="post"' not in content.lower()
