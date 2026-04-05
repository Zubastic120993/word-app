"""
Tests for PDF upload safety guarantees.

These tests ensure that:
1. Uploading/parsing a PDF creates ZERO learning units in the database
2. Only the /api/pdfs/confirm endpoint can write to the database
3. Rejecting all units results in zero inserts
4. Missing decisions block the confirm operation
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
from io import BytesIO

from app.main import app
from app.database import get_db, Base, engine
from app.models.learning_unit import LearningUnit


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def db_session():
    """Create a fresh database session for testing."""
    # Drop and recreate all tables to ensure schema is up-to-date
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    
    from sqlalchemy.orm import Session
    session = Session(bind=engine)
    
    yield session
    
    # Cleanup
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def mock_pdf_content():
    """Create mock PDF content."""
    return b"%PDF-1.4 mock content"


class TestParseEndpointSafety:
    """Tests for /api/pdfs/parse endpoint - should NEVER write to DB."""
    
    @patch('app.routers.upload.PDFParser')
    def test_parse_creates_zero_units(self, mock_parser_class, client, db_session):
        """
        CRITICAL: Parsing a PDF must create ZERO learning units in the database.
        """
        # Setup mock parser
        mock_parser = Mock()
        mock_result = Mock()
        mock_result.units = [
            Mock(
                text="dom",
                translation="house",
                type=Mock(value="word"),
                part_of_speech=None,
                page_number=1,
            ),
            Mock(
                text="kot",
                translation="cat",
                type=Mock(value="word"),
                part_of_speech=None,
                page_number=1,
            ),
        ]
        mock_result.skipped_lines = 0
        mock_parser.parse_file.return_value = mock_result
        mock_parser_class.return_value = mock_parser
        
        # Count units before
        units_before = db_session.query(LearningUnit).count()
        
        # Create a mock PDF file
        files = {"file": ("test.pdf", BytesIO(b"%PDF-1.4 mock"), "application/pdf")}
        
        # Call parse endpoint
        response = client.post("/api/pdfs/parse", files=files)
        
        # Verify response is successful
        assert response.status_code == 200
        data = response.json()
        assert data["total_parsed"] == 2
        
        # CRITICAL: Verify NO units were created
        units_after = db_session.query(LearningUnit).count()
        assert units_after == units_before, \
            f"Parse endpoint created {units_after - units_before} units! Expected 0."
    
    @patch('app.routers.upload.PDFParser')
    def test_parse_returns_units_without_saving(self, mock_parser_class, client, db_session):
        """Parse should return parsed units but NOT save them."""
        mock_parser = Mock()
        mock_result = Mock()
        mock_result.units = [
            Mock(
                text="słowo",
                translation="word",
                type=Mock(value="word"),
                part_of_speech="noun",
                page_number=1,
            ),
        ]
        mock_result.skipped_lines = 0
        mock_parser.parse_file.return_value = mock_result
        mock_parser_class.return_value = mock_parser
        
        files = {"file": ("vocab.pdf", BytesIO(b"%PDF"), "application/pdf")}
        response = client.post("/api/pdfs/parse", files=files)
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify units are returned for review
        assert len(data["units"]) == 1
        assert data["units"][0]["text"] == "słowo"
        assert data["units"][0]["translation"] == "word"


class TestConfirmEndpointSafety:
    """Tests for /api/pdfs/confirm endpoint - the ONLY way to write to DB."""
    
    def test_confirm_requires_all_decisions(self, client):
        """
        Confirm endpoint must reject requests with missing decisions.
        """
        # Request with missing decisions (only 1 of 2 units has decision)
        request_data = {
            "filename": "test.pdf",
            "units": [
                {"index": 0, "action": "accept", "text": None, "translation": None}
                # Missing index 1
            ],
            "original_units": [
                {"index": 0, "text": "dom", "translation": "house", "type": "word"},
                {"index": 1, "text": "kot", "translation": "cat", "type": "word"},
            ]
        }
        
        response = client.post("/api/pdfs/confirm", json=request_data)
        
        # Should be rejected
        assert response.status_code == 400
        assert "Missing decisions" in response.json()["detail"]
    
    def test_confirm_rejects_invalid_actions(self, client):
        """Confirm endpoint must reject invalid action values."""
        request_data = {
            "filename": "test.pdf",
            "units": [
                {"index": 0, "action": "pending", "text": None, "translation": None},  # Invalid
            ],
            "original_units": [
                {"index": 0, "text": "dom", "translation": "house", "type": "word"},
            ]
        }
        
        response = client.post("/api/pdfs/confirm", json=request_data)
        
        assert response.status_code == 400
        assert "Invalid actions" in response.json()["detail"]
    
    def test_reject_all_creates_zero_units(self, client, db_session):
        """
        CRITICAL: Rejecting all units must result in ZERO database inserts.
        """
        units_before = db_session.query(LearningUnit).count()
        
        request_data = {
            "filename": "rejected.pdf",
            "units": [
                {"index": 0, "action": "reject", "text": None, "translation": None},
                {"index": 1, "action": "reject", "text": None, "translation": None},
                {"index": 2, "action": "reject", "text": None, "translation": None},
            ],
            "original_units": [
                {"index": 0, "text": "dom", "translation": "house", "type": "word"},
                {"index": 1, "text": "kot", "translation": "cat", "type": "word"},
                {"index": 2, "text": "pies", "translation": "dog", "type": "word"},
            ]
        }
        
        response = client.post("/api/pdfs/confirm", json=request_data)
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify response reports zero saved
        assert data["units_saved"] == 0
        assert data["units_rejected"] == 3
        
        # CRITICAL: Verify database has no new units
        units_after = db_session.query(LearningUnit).count()
        assert units_after == units_before, \
            f"Reject-all created {units_after - units_before} units! Expected 0."
    
    def test_confirm_only_saves_accepted_units(self, client, db_session):
        """Only accepted/edited units should be saved to database."""
        units_before = db_session.query(LearningUnit).count()
        
        request_data = {
            "filename": "mixed.pdf",
            "units": [
                {"index": 0, "action": "accept", "text": None, "translation": None},
                {"index": 1, "action": "reject", "text": None, "translation": None},
                {"index": 2, "action": "edit", "text": "edited", "translation": "edited trans"},
            ],
            "original_units": [
                {"index": 0, "text": "dom", "translation": "house", "type": "word"},
                {"index": 1, "text": "kot", "translation": "cat", "type": "word"},
                {"index": 2, "text": "pies", "translation": "dog", "type": "word"},
            ]
        }
        
        response = client.post("/api/pdfs/confirm", json=request_data)
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify counts
        assert data["units_saved"] == 2  # accept + edit
        assert data["units_rejected"] == 1
        
        # Verify database
        units_after = db_session.query(LearningUnit).count()
        assert units_after == units_before + 2


class TestLegacyEndpointRemoved:
    """Tests to ensure legacy direct-write endpoint is removed."""
    
    def test_legacy_upload_endpoint_not_found(self, client):
        """
        The legacy /api/pdfs/upload endpoint should NOT exist.
        This endpoint used to write directly to DB without review.
        """
        files = {"file": ("test.pdf", BytesIO(b"%PDF"), "application/pdf")}
        response = client.post("/api/pdfs/upload", files=files)
        
        # Should return 404 or 405 (not found)
        assert response.status_code in (404, 405), \
            "Legacy /api/pdfs/upload endpoint should be removed!"


class TestDatabaseIsolation:
    """Tests to ensure proper database isolation during upload flow."""
    
    @patch('app.routers.upload.PDFParser')
    def test_parse_error_does_not_affect_database(self, mock_parser_class, client, db_session):
        """If parsing fails, database should remain unchanged."""
        mock_parser = Mock()
        mock_parser.parse_file.side_effect = Exception("Parse error")
        mock_parser_class.return_value = mock_parser
        
        units_before = db_session.query(LearningUnit).count()
        
        files = {"file": ("bad.pdf", BytesIO(b"%PDF"), "application/pdf")}
        response = client.post("/api/pdfs/parse", files=files)
        
        assert response.status_code == 500
        
        # Database unchanged
        units_after = db_session.query(LearningUnit).count()
        assert units_after == units_before
    
    def test_confirm_partial_failure_rollback(self, client, db_session):
        """If confirm fails partway through, changes should be rolled back."""
        # This test ensures transactional integrity
        # Note: SQLite may handle this differently than PostgreSQL
        pass  # Placeholder for more complex rollback testing
