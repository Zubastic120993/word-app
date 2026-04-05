"""Tests for duplicate vocabulary detection.

These tests ensure that:
1. normalize_text() properly normalizes text for comparison
2. Duplicates are detected during upload review
3. Duplicates are skipped during confirm
4. Manual unit creation rejects duplicates
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
from io import BytesIO

from app.main import app
from app.database import get_db, Base, engine
from app.models.learning_unit import LearningUnit, normalize_text


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


class TestNormalizeText:
    """Test the normalize_text function."""
    
    def test_lowercase(self):
        """Text should be lowercased."""
        assert normalize_text("CUKIER") == "cukier"
        assert normalize_text("Cukier") == "cukier"
    
    def test_strip_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        assert normalize_text("  cukier  ") == "cukier"
        assert normalize_text("\tcukier\n") == "cukier"
    
    def test_collapse_multiple_spaces(self):
        """Multiple spaces should become single space."""
        assert normalize_text("co  to   jest") == "co to jest"
    
    def test_preserve_polish_characters(self):
        """Polish diacritics should be preserved (not stripped)."""
        assert normalize_text("ŻÓŁĆ") == "żółć"
        assert normalize_text("Cześć") == "cześć"
        assert normalize_text("DZIĘKUJĘ") == "dziękuję"
    
    def test_unicode_normalization(self):
        """Unicode should be normalized to NFC form."""
        # NFC normalization ensures composed characters
        result = normalize_text("są")
        assert result == "są"
    
    def test_empty_string(self):
        """Empty string should return empty."""
        assert normalize_text("") == ""
        assert normalize_text("   ") == ""
    
    def test_identical_after_normalization(self):
        """Different inputs should match after normalization."""
        assert normalize_text("  CUKIER  ") == normalize_text("cukier")
        assert normalize_text("Co To Jest?") == normalize_text("co to jest?")


class TestDuplicateDetectionAtParse:
    """Tests for duplicate detection during parsing."""
    
    @patch('app.routers.upload.PDFParser')
    def test_duplicates_marked_during_parse(self, mock_parser_class, client, db_session):
        """Existing units should be marked as duplicates during parse."""
        # First, add an existing unit to the database
        existing_unit = LearningUnit(
            text="dom",
            translation="house",
            type="word",
            source_pdf="previous.pdf",
            normalized_text="dom",
            normalized_translation="house",
        )
        db_session.add(existing_unit)
        db_session.commit()
        
        # Setup mock parser to return units including a duplicate
        mock_parser = Mock()
        mock_result = Mock()
        mock_result.units = [
            Mock(
                text="dom",  # Duplicate!
                translation="house",
                type=Mock(value="word"),
                part_of_speech=None,
                page_number=1,
            ),
            Mock(
                text="kot",  # New unit
                translation="cat",
                type=Mock(value="word"),
                part_of_speech=None,
                page_number=1,
            ),
        ]
        mock_result.skipped_lines = 0
        mock_parser.parse_file.return_value = mock_result
        mock_parser_class.return_value = mock_parser
        
        # Parse the file
        files = {"file": ("test.pdf", BytesIO(b"%PDF-1.4 mock"), "application/pdf")}
        response = client.post("/api/pdfs/parse", files=files)
        
        assert response.status_code == 200
        data = response.json()
        
        # First unit should be marked as duplicate
        assert data["units"][0]["is_duplicate"] is True
        # Second unit should not be marked as duplicate
        assert data["units"][1]["is_duplicate"] is False
        # Duplicates count should be 1
        assert data["duplicates_found"] == 1


class TestDuplicateSkipAtConfirm:
    """Tests for duplicate skipping during confirmation."""
    
    def test_duplicates_skipped_at_confirm(self, client, db_session):
        """Units marked as duplicates should be skipped during confirm."""
        # First, add an existing unit
        existing_unit = LearningUnit(
            text="dom",
            translation="house",
            type="word",
            source_pdf="previous.pdf",
            normalized_text="dom",
            normalized_translation="house",
        )
        db_session.add(existing_unit)
        db_session.commit()
        
        units_before = db_session.query(LearningUnit).count()
        
        # Try to confirm units including a duplicate
        request_data = {
            "filename": "test.pdf",
            "units": [
                {"index": 0, "action": "accept", "text": None, "translation": None},
                {"index": 1, "action": "accept", "text": None, "translation": None},
            ],
            "original_units": [
                {
                    "index": 0,
                    "text": "dom",
                    "translation": "house",
                    "type": "word",
                    "is_duplicate": True,  # Marked as duplicate
                },
                {
                    "index": 1,
                    "text": "kot",
                    "translation": "cat",
                    "type": "word",
                    "is_duplicate": False,
                },
            ]
        }
        
        response = client.post("/api/pdfs/confirm", json=request_data)
        
        assert response.status_code == 200
        data = response.json()
        
        # Only 1 unit should be saved (the non-duplicate)
        assert data["units_saved"] == 1
        assert data["duplicates_skipped"] == 1
        
        # Verify in database
        units_after = db_session.query(LearningUnit).count()
        assert units_after == units_before + 1


class TestManualUnitDuplicateRejection:
    """Tests for manual unit creation rejecting duplicates."""
    
    def test_manual_unit_rejects_duplicate(self, client, db_session):
        """Manually adding a duplicate unit should be rejected."""
        # First, add an existing unit
        existing_unit = LearningUnit(
            text="dom",
            translation="house",
            type="word",
            source_pdf="manual",
            normalized_text="dom",
            normalized_translation="house",
        )
        db_session.add(existing_unit)
        db_session.commit()
        
        # Try to add the same unit
        response = client.post(
            "/api/units",
            params={"text": "dom", "translation": "house"},
        )
        
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]
    
    def test_manual_unit_rejects_normalized_duplicate(self, client, db_session):
        """Manually adding a unit that normalizes to existing should be rejected."""
        # Add existing unit
        existing_unit = LearningUnit(
            text="dom",
            translation="house",
            type="word",
            source_pdf="manual",
            normalized_text="dom",
            normalized_translation="house",
        )
        db_session.add(existing_unit)
        db_session.commit()
        
        # Try to add with different casing (should normalize to same)
        response = client.post(
            "/api/units",
            params={"text": "  DOM  ", "translation": "  HOUSE  "},
        )
        
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]


class TestDifferentMeaningsAllowed:
    """Test that same word with different translations is allowed."""
    
    def test_same_word_different_translation_allowed(self, client, db_session):
        """Same word with different translation should NOT be a duplicate."""
        # Add existing unit
        existing_unit = LearningUnit(
            text="zamek",
            translation="castle",  # One meaning
            type="word",
            source_pdf="manual",
            normalized_text="zamek",
            normalized_translation="castle",
        )
        db_session.add(existing_unit)
        db_session.commit()
        
        # Add same word with different translation (different meaning)
        response = client.post(
            "/api/units",
            params={"text": "zamek", "translation": "lock"},  # Different meaning
        )
        
        # Should succeed - different translation = different entry
        assert response.status_code == 200
        data = response.json()
        assert data["text"] == "zamek"
        assert data["translation"] == "lock"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
