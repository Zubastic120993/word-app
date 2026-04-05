"""Follow-up recall: typed InsufficientUnitsError and API fallback behavior."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.session import LearningSession, StudyModeType
from app.services.session_service import InsufficientUnitsError, SessionService


def test_insufficient_units_error_optional_code():
    e = InsufficientUnitsError("Need more units")
    assert str(e) == "Need more units"
    assert e.code is None


def test_insufficient_units_error_follow_up_code():
    e = InsufficientUnitsError(
        "No eligible introduced words from follow-up session.",
        code="NO_FOLLOWUP_INTRODUCED",
    )
    assert e.code == "NO_FOLLOWUP_INTRODUCED"


def test_create_session_follow_up_fallback_returns_notice(monkeypatch):
    """Router retries without follow_up when NO_FOLLOWUP_INTRODUCED and returns fallback_notice."""

    def override_get_db():
        try:
            yield MagicMock()
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    mock_session = MagicMock(spec=LearningSession)
    mock_session.id = 999
    mock_session.mode = StudyModeType.RECALL
    mock_session.units = [MagicMock() for _ in range(3)]

    calls = {"n": 0}

    def fake_create_from_request(self, request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InsufficientUnitsError(
                "No eligible introduced words from follow-up session.",
                code="NO_FOLLOWUP_INTRODUCED",
            )
        return mock_session

    monkeypatch.setattr(
        SessionService,
        "create_session_from_request",
        fake_create_from_request,
    )

    try:
        client = TestClient(app)
        r = client.post(
            "/api/sessions/create",
            json={
                "mode": "recall",
                "follow_up_session_id": 2021,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == 999
        assert data["mode"] == "recall"
        assert data.get("fallback_notice")
        assert "previous session" in data["fallback_notice"].lower()
        assert calls["n"] == 2
    finally:
        app.dependency_overrides.clear()


def test_create_session_follow_up_then_recall_then_passive_fallback(monkeypatch):
    """NO_FOLLOWUP → INSUFFICIENT_INTRODUCED_RECALL → passive succeeds; response mode is passive."""

    def override_get_db():
        try:
            yield MagicMock()
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    mock_session = MagicMock(spec=LearningSession)
    mock_session.id = 1001
    mock_session.mode = StudyModeType.PASSIVE
    mock_session.units = [MagicMock() for _ in range(5)]

    calls = {"n": 0}

    def fake_create_from_request(self, request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InsufficientUnitsError(
                "No eligible introduced words from follow-up session.",
                code="NO_FOLLOWUP_INTRODUCED",
            )
        if calls["n"] == 2:
            raise InsufficientUnitsError(
                "Not enough introduced words for Recall mode. Please study in Passive mode first.",
                code="INSUFFICIENT_INTRODUCED_RECALL",
            )
        assert request.mode.value == "passive"
        return mock_session

    monkeypatch.setattr(
        SessionService,
        "create_session_from_request",
        fake_create_from_request,
    )

    try:
        client = TestClient(app)
        r = client.post(
            "/api/sessions/create",
            json={
                "mode": "recall",
                "follow_up_session_id": 2022,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == 1001
        assert data["mode"] == "passive"
        assert "Passive" in data["message"]
        assert "Passive mode" in (data.get("fallback_notice") or "")
        assert calls["n"] == 3
    finally:
        app.dependency_overrides.clear()
