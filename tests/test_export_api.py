"""Tests for export API endpoint."""

import pytest
import json
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
def populated_db():
    """Populate database with sample data."""
    db = TestSessionLocal()
    
    # Create settings
    db_settings = Settings(
        id=1,
        offline_mode=True,
        ai_provider="ollama",
        ollama_model="llama3.2",
        strict_mode=True,
        source_language="Polish",
        target_language="English",
    )
    db.add(db_settings)
    
    # Create learning units
    units = [
        LearningUnit(
            id=1,
            text="dom",
            type=UnitType.WORD,
            translation="house",
            source_pdf="test.pdf",
            created_at=datetime(2025, 1, 1, 10, 0, 0),
        ),
        LearningUnit(
            id=2,
            text="kot",
            type=UnitType.WORD,
            translation="cat",
            source_pdf="test.pdf",
            created_at=datetime(2025, 1, 1, 10, 0, 1),
        ),
    ]
    for unit in units:
        db.add(unit)
    
    # Create progress record
    progress = LearningProgress(
        id=1,
        unit_id=1,
        times_seen=5,
        times_correct=3,
        times_failed=2,
        confidence_score=0.6,
        last_seen=datetime(2025, 1, 10, 12, 0, 0),
    )
    db.add(progress)
    
    db.commit()
    db.close()


@pytest.fixture
def client():
    """Create test client."""
    with TestClient(app) as client:
        yield client


class TestExportApiResponse:
    """Tests for export API response format."""
    
    def test_export_returns_200(self, client, populated_db):
        """Verify export endpoint returns 200 OK."""
        response = client.get("/api/export")
        assert response.status_code == 200
    
    def test_export_returns_json_content_type(self, client, populated_db):
        """Verify export returns application/json content type."""
        response = client.get("/api/export")
        assert "application/json" in response.headers["content-type"]
    
    def test_export_returns_valid_json(self, client, populated_db):
        """Verify export returns valid JSON content."""
        response = client.get("/api/export")
        
        # Should not raise
        data = response.json()
        
        assert isinstance(data, dict)
        assert "metadata" in data
        assert "learning_units" in data
    
    def test_export_content_disposition_header(self, client, populated_db):
        """Verify export has Content-Disposition header for download."""
        response = client.get("/api/export")
        
        assert "content-disposition" in response.headers
        content_disp = response.headers["content-disposition"]
        
        assert "attachment" in content_disp
        assert "filename=" in content_disp
        assert ".json" in content_disp
    
    def test_export_filename_contains_timestamp(self, client, populated_db):
        """Verify export filename contains timestamp."""
        response = client.get("/api/export")
        
        content_disp = response.headers["content-disposition"]
        
        # Filename should contain word_app_export and timestamp pattern
        assert "word_app_export_" in content_disp
        # Should contain date pattern like 20250111
        import re
        assert re.search(r"\d{8}_\d{6}", content_disp)


class TestExportApiContent:
    """Tests for export API content."""
    
    def test_export_contains_metadata(self, client, populated_db):
        """Verify export contains metadata section."""
        response = client.get("/api/export")
        data = response.json()
        
        assert "metadata" in data
        assert "app_version" in data["metadata"]
        assert "export_timestamp" in data["metadata"]
        assert "source_language" in data["metadata"]
        assert "target_language" in data["metadata"]
    
    def test_export_contains_units(self, client, populated_db):
        """Verify export contains learning units."""
        response = client.get("/api/export")
        data = response.json()
        
        assert "learning_units" in data
        assert len(data["learning_units"]) == 2
        
        # Check unit data
        unit_texts = [u["text"] for u in data["learning_units"]]
        assert "dom" in unit_texts
        assert "kot" in unit_texts
    
    def test_export_contains_progress(self, client, populated_db):
        """Verify export contains progress records."""
        response = client.get("/api/export")
        data = response.json()
        
        assert "learning_progress" in data
        assert len(data["learning_progress"]) == 1
        assert data["learning_progress"][0]["unit_id"] == 1
    
    def test_export_contains_settings(self, client, populated_db):
        """Verify export contains settings."""
        response = client.get("/api/export")
        data = response.json()
        
        assert "settings" in data
        assert data["settings"]["source_language"] == "Polish"
        assert data["settings"]["target_language"] == "English"
    
    def test_export_empty_database(self, client):
        """Verify export works with empty database."""
        response = client.get("/api/export")
        
        assert response.status_code == 200
        data = response.json()
        
        assert len(data["learning_units"]) == 0
        assert len(data["learning_progress"]) == 0
        assert len(data["learning_sessions"]) == 0


class TestExportApiFormat:
    """Tests for export JSON formatting."""
    
    def test_export_json_is_human_readable(self, client, populated_db):
        """Verify export JSON is formatted with indentation."""
        response = client.get("/api/export")
        content = response.content.decode("utf-8")
        
        # Should have newlines and indentation (pretty printed)
        assert "\n" in content
        assert "  " in content  # Has indentation
    
    def test_export_json_can_be_reparsed(self, client, populated_db):
        """Verify export JSON can be parsed and contains valid data."""
        response = client.get("/api/export")
        content = response.content.decode("utf-8")
        
        # Parse the raw content
        data = json.loads(content)
        
        # Verify structure is intact
        assert "metadata" in data
        assert "learning_units" in data
        assert isinstance(data["learning_units"], list)
