"""Regression tests for theme-scoped session selection behavior."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.vocabulary import Vocabulary
from app.utils.time import utc_now
from app.services.session_service import SESSION_SIZE


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
    """Create a test client bound to the test database."""
    return TestClient(app)


@pytest.fixture
def db_session(test_db):
    """Get a database session for test setup."""
    session = test_db()
    yield session
    session.close()


@pytest.fixture
def themed_units(db_session):
    """Create theme/lesson/source data with due and weak progress variants."""
    now = utc_now().replace(tzinfo=None)

    vocabularies = [
        Vocabulary(id=3, user_key="core", name="czytaj_01_01_core.pdf"),
        Vocabulary(id=8, user_key="food_a", name="czytaj_02_01_food_a.pdf"),
        Vocabulary(id=9, user_key="food_b", name="czytaj_02_02_food_b.pdf"),
        Vocabulary(id=10, user_key="food_c", name="czytaj_04_01_food_c.pdf"),
        Vocabulary(id=15, user_key="work", name="czytaj_03_01_work.pdf"),
    ]
    db_session.add_all(vocabularies)
    db_session.flush()

    created = {
        "food_due_weak_ids": set(),
        "food_due_ids": set(),
    }

    def add_unit(vocabulary_id: int, source_pdf: str, text: str) -> LearningUnit:
        unit = LearningUnit(
            text=text,
            translation=f"{text}_en",
            type=UnitType.WORD,
            source_pdf=source_pdf,
            vocabulary_id=vocabulary_id,
        )
        db_session.add(unit)
        db_session.flush()
        return unit

    def add_progress(
        unit: LearningUnit,
        *,
        confidence: float,
        introduced: bool = True,
        due: bool = False,
        times_correct: int = 1,
        times_failed: int = 0,
    ) -> None:
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=confidence,
            introduced_at=now - timedelta(days=10) if introduced else None,
            next_review_at=(now - timedelta(days=1)) if due else (now + timedelta(days=5)),
            last_seen=now - timedelta(days=2),
            times_seen=max(1, times_correct + times_failed),
            times_correct=times_correct,
            times_failed=times_failed,
        )
        db_session.add(progress)

    for idx in range(8):
        unit = add_unit(3, "czytaj_01_01_core.pdf", f"core_{idx}")
        add_progress(unit, confidence=0.95, due=False, times_correct=4)

    for idx in range(75):
        unit = add_unit(8, "czytaj_02_01_food_a.pdf", f"food_a_{idx}")
        if idx < 3:
            add_progress(unit, confidence=0.2, due=True, times_correct=0, times_failed=2)
            created["food_due_weak_ids"].add(unit.id)
            created["food_due_ids"].add(unit.id)
        elif idx < 6:
            add_progress(unit, confidence=0.9, due=True, times_correct=4)
            created["food_due_ids"].add(unit.id)
        else:
            add_progress(unit, confidence=0.2, due=False, times_correct=0, times_failed=1)

    for idx in range(25):
        unit = add_unit(9, "czytaj_02_02_food_b.pdf", f"food_b_{idx}")
        if idx < 3:
            add_progress(unit, confidence=0.25, due=False, times_correct=0, times_failed=1)
        elif idx < 6:
            add_progress(unit, confidence=0.85, due=False, times_correct=3)
        else:
            add_progress(unit, confidence=0.22, due=False, times_correct=0, times_failed=1)

    for idx in range(20):
        add_unit(10, "czytaj_04_01_food_c.pdf", f"food_c_{idx}")

    for idx in range(10):
        unit = add_unit(15, "czytaj_03_01_work.pdf", f"work_{idx}")
        if idx < 3:
            add_progress(unit, confidence=0.2, due=True, times_correct=0, times_failed=2)

    db_session.commit()
    return created


def _create_session(client: TestClient, **payload):
    request = {"mode": "passive", "override_cap": True}
    request.update(payload)
    response = client.post("/api/sessions/create", json=request)
    assert response.status_code == 200
    return response


class TestSessionThemeSelectionApi:
    def test_theme_normal_session_filters_units_and_exposes_theme_metadata(
        self, client, themed_units
    ):
        response = _create_session(client, theme="food")
        session_id = response.json()["session_id"]

        session_response = client.get(f"/api/sessions/{session_id}")
        assert session_response.status_code == 200

        units = session_response.json()["units"]
        assert len(units) == SESSION_SIZE
        assert {unit["theme_id"] for unit in units} == {"food"}
        assert {unit["theme_name"] for unit in units} == {"Food"}
        assert {unit["unit"]["source_pdf"] for unit in units}.issubset(
            {"czytaj_02_01_food_a.pdf", "czytaj_02_02_food_b.pdf"}
        )

    def test_theme_weak_only_session_filters_units(self, client, themed_units):
        response = _create_session(client, theme="food", weak_only=True)
        session_id = response.json()["session_id"]

        session_response = client.get(f"/api/sessions/{session_id}")
        units = session_response.json()["units"]

        assert len(units) == SESSION_SIZE
        assert {unit["unit"]["source_pdf"] for unit in units}.issubset(
            {"czytaj_02_01_food_a.pdf", "czytaj_02_02_food_b.pdf"}
        )
        assert {unit["theme_id"] for unit in units} == {"food"}

    def test_theme_due_only_session_filters_units(self, client, themed_units):
        response = _create_session(client, theme="food", due_only=True)
        session_id = response.json()["session_id"]

        session_response = client.get(f"/api/sessions/{session_id}")
        units = session_response.json()["units"]

        assert len(units) == len(themed_units["food_due_ids"])
        assert {unit["unit"]["id"] for unit in units} == themed_units["food_due_ids"]
        assert {unit["theme_id"] for unit in units} == {"food"}

    def test_theme_and_lesson_filters_intersect(self, client, themed_units):
        response = _create_session(client, theme="food", lesson_id=2)
        session_id = response.json()["session_id"]

        session_response = client.get(f"/api/sessions/{session_id}")
        units = session_response.json()["units"]

        assert len(units) == SESSION_SIZE
        assert "czytaj_04_01_food_c.pdf" not in {unit["unit"]["source_pdf"] for unit in units}

    def test_theme_and_source_pdf_filters_intersect(self, client, themed_units):
        response = _create_session(
            client,
            theme="food",
            source_pdfs=["czytaj_02_01_food_a.pdf"],
        )
        session_id = response.json()["session_id"]

        session_response = client.get(f"/api/sessions/{session_id}")
        units = session_response.json()["units"]

        assert len(units) == SESSION_SIZE
        assert {unit["unit"]["source_pdf"] for unit in units} == {"czytaj_02_01_food_a.pdf"}
        assert {unit["theme_id"] for unit in units} == {"food"}

    def test_theme_with_no_due_units_returns_empty_status(self, client, themed_units):
        response = _create_session(client, theme="core_communication", due_only=True)

        assert response.json() == {
            "session_id": None,
            "status": "empty",
            "reason": "no_due_units_in_theme",
            "theme": "core_communication",
            "message": "No due words in theme: core_communication",
        }

    def test_invalid_theme_returns_http_400(self, client, themed_units):
        response = client.post(
            "/api/sessions/create",
            json={"mode": "passive", "override_cap": True, "theme": "invalid_theme"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Unknown theme: invalid_theme"

    def test_theme_weak_only_due_only_intersection(self, client, themed_units):
        response = _create_session(client, theme="food", weak_only=True, due_only=True)
        session_id = response.json()["session_id"]

        session_response = client.get(f"/api/sessions/{session_id}")
        units = session_response.json()["units"]

        assert len(units) == len(themed_units["food_due_weak_ids"])
        assert {unit["unit"]["id"] for unit in units} == themed_units["food_due_weak_ids"]
        assert {unit["theme_id"] for unit in units} == {"food"}
