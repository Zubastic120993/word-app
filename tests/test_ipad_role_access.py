"""Tests for iPad client-role access control.

Phase 7 (Testing & Validation) of the iPad deploy & sync feature.

Verifies:
- iPad-role requests receive 403 on admin-only endpoints
  (upload, vocabulary CRUD, data import/export, AI chat).
- iPad-role requests are allowed on learning endpoints
  (session create, answer, progress, history).
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.config import settings
from app.models.learning_unit import LearningProgress, LearningUnit, UnitType
from app.models.session import StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SESSION_SIZE, SessionService
from app.utils.time import utc_now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    """Create a fresh in-memory database for each test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine,
    )

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
def ipad_settings(monkeypatch):
    """Enable iPad client mode and debug (for ?role=ipad query-param)."""
    monkeypatch.setattr(settings, "ipad_enabled", True)
    monkeypatch.setattr(settings, "debug", True)
    yield
    # monkeypatch auto-restores


@pytest.fixture
def client(test_db, ipad_settings):
    """Test client with iPad mode enabled."""
    return TestClient(app)


@pytest.fixture
def db_session(test_db):
    """Raw DB session for test-data setup."""
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
    for i in range(SESSION_SIZE + 20):
        unit = LearningUnit(
            text=f"slowo{i}",
            translation=f"word{i}",
            type=UnitType.WORD,
            source_pdf="czytaj_01_01_test.pdf",
            vocabulary_id=1,
        )
        db_session.add(unit)
        units.append(unit)
    db_session.commit()
    return units


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IPAD_ROLE = {"role": "ipad"}


def _ipad_url(path: str) -> str:
    """Append ?role=ipad to a path."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}role=ipad"


def _snapshot_db_state(db_session) -> dict[str, list[dict]]:
    """Capture all table rows so tests can detect inserts or updates from GET routes."""
    inspector = inspect(db_session.bind)
    state = {}

    for table_name in sorted(inspector.get_table_names()):
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        order_by = ", ".join(f'"{column}"' for column in columns)
        rows = db_session.connection().exec_driver_sql(
            f'SELECT * FROM "{table_name}" ORDER BY {order_by}'
        ).mappings().all()
        state[table_name] = [dict(row) for row in rows]

    return state


# ===================================================================
# PART 1 — Admin-only endpoints: iPad role must receive 403 Forbidden
# ===================================================================


class TestUploadRouterBlocked:
    """iPad-role requests to upload / vocabulary-CRUD endpoints → 403."""

    def test_get_units_forbidden(self, client):
        """GET /api/units is admin-only (upload router)."""
        response = client.get("/api/units?role=ipad")
        assert response.status_code == 403

    def test_post_units_forbidden(self, client):
        """POST /api/units is admin-only."""
        response = client.post(
            _ipad_url("/api/units"),
            json={"text": "test", "translation": "test"},
        )
        assert response.status_code == 403

    def test_get_single_unit_forbidden(self, client):
        """GET /api/units/{id} is admin-only."""
        response = client.get("/api/units/1?role=ipad")
        assert response.status_code == 403

    def test_put_unit_forbidden(self, client):
        """PUT /api/units/{id} is admin-only."""
        response = client.put(
            "/api/units/1?role=ipad",
            json={"text": "updated"},
        )
        assert response.status_code == 403

    def test_delete_unit_forbidden(self, client):
        """DELETE /api/units/{id} is admin-only."""
        response = client.delete("/api/units/1?role=ipad")
        assert response.status_code == 403

    def test_parse_pdf_forbidden(self, client):
        """POST /api/pdfs/parse is admin-only."""
        response = client.post(
            "/api/pdfs/parse?role=ipad",
            files={"file": ("test.pdf", b"fake-pdf-content", "application/pdf")},
        )
        assert response.status_code == 403

    def test_confirm_pdf_forbidden(self, client):
        """POST /api/pdfs/confirm is admin-only."""
        response = client.post(
            "/api/pdfs/confirm?role=ipad",
            json={"filename": "test.pdf", "original_units": [], "units": []},
        )
        assert response.status_code == 403

    def test_get_pdf_sources_forbidden(self, client):
        """GET /api/pdfs/sources is admin-only."""
        response = client.get("/api/pdfs/sources?role=ipad")
        assert response.status_code == 403

    def test_delete_pdf_units_forbidden(self, client):
        """DELETE /api/pdfs/{filename} is admin-only."""
        response = client.delete("/api/pdfs/test.pdf?role=ipad")
        assert response.status_code == 403

    def test_list_vocabularies_forbidden(self, client):
        """GET /api/vocabularies is admin-only."""
        response = client.get("/api/vocabularies?role=ipad")
        assert response.status_code == 403

    def test_list_vocabulary_groups_forbidden(self, client):
        """GET /api/vocabulary-groups is admin-only."""
        response = client.get("/api/vocabulary-groups?role=ipad")
        assert response.status_code == 403

    def test_create_vocabulary_group_forbidden(self, client):
        """POST /api/vocabulary-groups is admin-only."""
        response = client.post("/api/vocabulary-groups?role=ipad&name=TestGroup")
        assert response.status_code == 403

    def test_assign_vocabulary_to_group_forbidden(self, client):
        """PUT /api/vocabularies/{id}/group is admin-only."""
        response = client.put("/api/vocabularies/1/group?role=ipad")
        assert response.status_code == 403


class TestDataRouterBlocked:
    """iPad-role requests to data import/export endpoints → 403."""

    def test_export_forbidden(self, client):
        """GET /api/export is admin-only."""
        response = client.get("/api/export?role=ipad")
        assert response.status_code == 403

    def test_import_forbidden(self, client):
        """POST /api/import is admin-only."""
        response = client.post(
            "/api/import?role=ipad&confirm=true",
            files={"file": ("data.json", b'{"metadata":{}}', "application/json")},
        )
        assert response.status_code == 403

    def test_import_validate_forbidden(self, client):
        """POST /api/import/validate is admin-only."""
        response = client.post(
            "/api/import/validate?role=ipad",
            files={"file": ("data.json", b'{"metadata":{}}', "application/json")},
        )
        assert response.status_code == 403


class TestAIChatBlocked:
    """iPad-role requests to AI free-chat endpoints → 403."""

    def test_free_chat_respond_forbidden(self, client):
        """POST /api/ai/chat/respond is admin-only."""
        response = client.post(
            "/api/ai/chat/respond?role=ipad",
            json={"message": "hello"},
        )
        assert response.status_code == 403

    def test_free_chat_translate_forbidden(self, client):
        """POST /api/ai/chat/translate is admin-only."""
        response = client.post(
            "/api/ai/chat/translate?role=ipad",
            json={"text": "dom", "source_language": "Polish", "target_language": "English"},
        )
        assert response.status_code == 403

    def test_free_chat_clear_forbidden(self, client):
        """POST /api/ai/chat/clear is admin-only."""
        response = client.post("/api/ai/chat/clear?role=ipad")
        assert response.status_code == 403


# ===================================================================
# PART 2 — Learning endpoints: iPad role must be ALLOWED
# ===================================================================


class TestSessionEndpointsAllowed:
    """iPad-role requests to session endpoints → non-403 (allowed)."""

    def test_create_session_allowed(self, client, sample_units):
        """POST /api/sessions/create is accessible for iPad."""
        response = client.post(
            "/api/sessions/create?role=ipad",
            json={"mode": "passive"},
        )
        # Should succeed (200) — not 403
        assert response.status_code != 403
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data

    def test_submit_answer_allowed(self, client, sample_units):
        """POST /api/sessions/{id}/answer is accessible for iPad."""
        # First create a session
        create_resp = client.post(
            "/api/sessions/create?role=ipad",
            json={"mode": "passive"},
        )
        session_id = create_resp.json()["session_id"]

        # Get session to find a valid unit position
        session_resp = client.get(f"/api/sessions/{session_id}?role=ipad")
        first_position = session_resp.json()["units"][0]["position"]

        # Submit an answer using the actual first position
        response = client.post(
            f"/api/sessions/{session_id}/answer?role=ipad",
            json={"unit_position": first_position, "is_correct": True},
        )
        assert response.status_code != 403
        assert response.status_code == 200

    def test_get_session_allowed(self, client, sample_units):
        """GET /api/sessions/{id} is accessible for iPad."""
        # Create a session
        create_resp = client.post(
            "/api/sessions/create?role=ipad",
            json={"mode": "passive"},
        )
        session_id = create_resp.json()["session_id"]

        response = client.get(f"/api/sessions/{session_id}?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200

    def test_session_history_allowed(self, client, sample_units):
        """GET /api/sessions/history is accessible for iPad."""
        response = client.get("/api/sessions/history?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200

    def test_recall_availability_allowed(self, client, sample_units):
        """GET /api/sessions/recall-availability is accessible for iPad."""
        response = client.get("/api/sessions/recall-availability?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200

    def test_passive_intro_cap_allowed(self, client, sample_units):
        """GET /api/sessions/passive-intro-cap is accessible for iPad."""
        response = client.get("/api/sessions/passive-intro-cap?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200


class TestProgressEndpointsAllowed:
    """iPad-role requests to progress endpoints → non-403 (allowed)."""

    def test_selection_stats_allowed(self, client, sample_units):
        """GET /api/progress/selection-stats is accessible for iPad."""
        response = client.get("/api/progress/selection-stats?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200


class TestUIEndpointsAllowed:
    """iPad-role requests to learning UI pages → non-403 (allowed)."""

    def test_study_page_allowed(self, client, sample_units):
        """GET /study is accessible for iPad."""
        response = client.get("/study?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200

    def test_progress_page_allowed(self, client, sample_units):
        """GET /progress is accessible for iPad."""
        response = client.get("/progress?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200

    def test_history_page_allowed(self, client, sample_units):
        """GET /history is accessible for iPad."""
        response = client.get("/history?role=ipad")
        assert response.status_code != 403
        assert response.status_code == 200

    def test_home_redirects_to_study_for_ipad(self, client, sample_units):
        """GET / redirects iPad to /study (302)."""
        response = client.get("/?role=ipad", follow_redirects=False)
        assert response.status_code == 302
        assert "/study" in response.headers["location"]


class TestReadEndpointsArePure:
    """GET endpoints must not mutate database state."""

    def test_vocabularies_get_does_not_mutate_database(self, client, db_session, sample_units):
        before = _snapshot_db_state(db_session)

        response = client.get("/api/vocabularies")

        after = _snapshot_db_state(db_session)

        assert response.status_code == 200
        assert after == before

    def test_vocabulary_groups_get_does_not_mutate_database(self, client, db_session, sample_units):
        before = _snapshot_db_state(db_session)

        response = client.get("/api/vocabulary-groups")

        after = _snapshot_db_state(db_session)

        assert response.status_code == 200
        assert after == before

    def test_sessions_sources_does_not_mutate_database(self, client, db_session, sample_units):
        before = _snapshot_db_state(db_session)

        response = client.get("/api/sessions/sources?role=ipad")

        after = _snapshot_db_state(db_session)

        assert response.status_code == 200
        assert after == before

    def test_progress_page_does_not_mutate_database(
        self, client, db_session, sample_units, monkeypatch,
    ):
        monkeypatch.setattr(settings, "spread_overdue_when_above", 1)

        for unit in sample_units[:2]:
            db_session.add(
                LearningProgress(
                    unit_id=unit.id,
                    confidence_score=0.4,
                    introduced_at=utc_now(),
                    next_review_at=utc_now(),
                )
            )
        db_session.commit()

        before = _snapshot_db_state(db_session)

        response = client.get("/progress?role=ipad")

        after = _snapshot_db_state(db_session)

        assert response.status_code == 200
        assert after == before

    def test_home_page_snapshot_does_not_mutate_database(
        self, client, db_session, sample_units, monkeypatch,
    ):
        monkeypatch.setattr(settings, "spread_overdue_when_above", 1)

        overdue_at = utc_now() - timedelta(days=1)
        for unit in sample_units[:2]:
            db_session.add(
                LearningProgress(
                    unit_id=unit.id,
                    confidence_score=0.4,
                    introduced_at=overdue_at,
                    next_review_at=overdue_at,
                )
            )
        db_session.commit()

        before = _snapshot_db_state(db_session)

        response = client.get("/", follow_redirects=False)

        after = _snapshot_db_state(db_session)

        assert response.status_code == 200
        assert after == before


# ===================================================================
# PART 3 — Sanity: Mac role is unaffected
# ===================================================================


class TestMacRoleUnaffected:
    """Verify Mac-role (default) requests are not blocked."""

    def test_mac_can_access_admin_endpoint(self, client, sample_units):
        """Mac (no role param) should access admin endpoints normally."""
        # GET /api/units should work without ?role=ipad
        response = client.get("/api/units")
        assert response.status_code == 200

    def test_mac_can_access_export(self, client, sample_units):
        """Mac (no role param) should access data export."""
        response = client.get("/api/export")
        assert response.status_code == 200

    def test_mac_can_access_sessions(self, client, sample_units):
        """Mac (no role param) should access session endpoints."""
        response = client.get("/api/sessions/history")
        assert response.status_code == 200


# ===================================================================
# PART 4 — iPad disabled: all requests treated as Mac
# ===================================================================


class TestIpadDisabled:
    """When WORD_APP_IPAD_ENABLED=false, ?role=ipad is ignored."""

    @pytest.fixture
    def disabled_client(self, test_db, monkeypatch):
        """Test client with iPad mode explicitly disabled."""
        monkeypatch.setattr(settings, "ipad_enabled", False)
        monkeypatch.setattr(settings, "debug", True)
        return TestClient(app)

    def test_ipad_param_ignored_on_admin_endpoint(self, disabled_client, sample_units):
        """With iPad disabled, ?role=ipad does not trigger 403."""
        response = disabled_client.get("/api/units?role=ipad")
        # Should NOT be 403 — iPad feature is off, so role resolves to "mac"
        assert response.status_code == 200

    def test_ipad_param_ignored_on_export(self, disabled_client, sample_units):
        """With iPad disabled, ?role=ipad does not trigger 403 on export."""
        response = disabled_client.get("/api/export?role=ipad")
        assert response.status_code == 200

    def test_home_does_not_redirect_when_disabled(self, disabled_client, sample_units):
        """With iPad disabled, / does not redirect even with ?role=ipad."""
        response = disabled_client.get("/?role=ipad", follow_redirects=False)
        # Should serve the normal home page (200), not redirect
        assert response.status_code == 200
