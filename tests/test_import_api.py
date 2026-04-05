"""Tests for import API endpoint."""

import pytest
import json
import io
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, LearningProgress, Settings, UnitType
from app.models.session import LearningSession, SessionUnit


# Create a test engine with StaticPool for thread safety
TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)


def override_get_db():
    """Override database dependency for tests."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_test_db():
    """Setup and teardown test database for each test."""
    # Create all tables
    Base.metadata.create_all(bind=TEST_ENGINE)
    
    # Override the dependency
    app.dependency_overrides[get_db] = override_get_db
    
    yield
    
    # Clear overrides and drop tables
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def client():
    """Create test client."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def populated_db():
    """Populate database with sample data."""
    db = TestSessionLocal()
    
    unit = LearningUnit(
        id=1,
        text="existing",
        type=UnitType.WORD,
        translation="istniejący",
        source_pdf="existing.pdf",
        created_at=datetime(2025, 1, 1, 10, 0, 0),
    )
    db.add(unit)
    db.commit()
    db.close()


def create_valid_import_json() -> bytes:
    """Create valid import JSON as bytes."""
    data = {
        "metadata": {
            "app_version": "0.1.0",
            "export_timestamp": "2025-01-11T12:00:00Z",
            "source_language": "Polish",
            "target_language": "English",
            "session_size": 50,
        },
        "settings": None,
        "learning_units": [
            {
                "id": 1,
                "text": "imported",
                "type": "word",
                "translation": "importowany",
                "source_pdf": "import.pdf",
                "created_at": "2025-01-11T10:00:00Z",
            },
        ],
        "learning_progress": [],
        "learning_sessions": [],
        "session_units": [],
    }
    return json.dumps(data).encode("utf-8")


class TestImportBlockedWithoutConfirm:
    """Tests verifying import is blocked without confirm flag."""
    
    def test_import_without_confirm_rejected(self, client):
        """Verify import without confirm=true is rejected."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 400
        assert "confirm" in response.json()["detail"].lower()
    
    def test_import_with_confirm_false_rejected(self, client):
        """Verify import with confirm=false is rejected."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import?confirm=false",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 400
        assert "confirm" in response.json()["detail"].lower()


class TestImportSucceedsWithConfirm:
    """Tests verifying import succeeds with confirm flag."""
    
    def test_import_with_confirm_succeeds(self, client):
        """Verify import with confirm=true succeeds."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["units_imported"] == 1
    
    def test_import_replaces_existing_data(self, client, populated_db):
        """Verify import replaces existing data."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 200
        
        # Verify data was replaced by checking export
        export_response = client.get("/api/export")
        export_data = export_response.json()
        
        # Should have imported unit, not existing unit
        unit_texts = [u["text"] for u in export_data["learning_units"]]
        assert "imported" in unit_texts
        assert "existing" not in unit_texts


class TestImportFileValidation:
    """Tests for import file validation."""
    
    def test_non_json_file_rejected(self, client):
        """Verify non-JSON file is rejected."""
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.txt", b"not json", "text/plain")},
        )
        
        assert response.status_code == 400
        assert "json" in response.json()["detail"].lower()
    
    def test_invalid_json_rejected(self, client):
        """Verify invalid JSON content is rejected."""
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.json", b"invalid json {", "application/json")},
        )
        
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()
    
    def test_json_missing_required_fields_rejected(self, client):
        """Verify JSON missing required fields is rejected."""
        invalid_data = json.dumps({"metadata": {}}).encode("utf-8")
        
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.json", invalid_data, "application/json")},
        )
        
        assert response.status_code == 422  # Validation failed


class TestImportValidateEndpoint:
    """Tests for the import validation endpoint."""
    
    def test_validate_valid_file(self, client):
        """Verify validation passes for valid file."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import/validate",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert len(data["errors"]) == 0
    
    def test_validate_invalid_file(self, client):
        """Verify validation fails for invalid file."""
        invalid_data = json.dumps({
            "metadata": {
                "app_version": "0.1.0",
            },
            "learning_units": [],
        }).encode("utf-8")
        
        response = client.post(
            "/api/import/validate",
            files={"file": ("import.json", invalid_data, "application/json")},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0
    
    def test_validate_does_not_modify_data(self, client, populated_db):
        """Verify validation does not modify existing data."""
        file_content = create_valid_import_json()
        
        # Validate file
        response = client.post(
            "/api/import/validate",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 200
        
        # Verify existing data is unchanged
        export_response = client.get("/api/export")
        export_data = export_response.json()
        
        unit_texts = [u["text"] for u in export_data["learning_units"]]
        assert "existing" in unit_texts  # Original data still there
        assert "imported" not in unit_texts  # Import data not applied


class TestImportResponse:
    """Tests for import response format."""
    
    def test_import_response_contains_counts(self, client):
        """Verify import response contains item counts."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert "success" in data
        assert "units_imported" in data
        assert "sessions_imported" in data
        assert "backup_created" in data
    
    def test_import_response_indicates_backup(self, client):
        """Verify import response indicates backup was created."""
        file_content = create_valid_import_json()
        
        response = client.post(
            "/api/import?confirm=true",
            files={"file": ("import.json", file_content, "application/json")},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["backup_created"] is True
