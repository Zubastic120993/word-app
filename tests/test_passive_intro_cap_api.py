"""GET /api/sessions/passive-intro-cap — read-only cap snapshot for study UI."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app


@pytest.fixture
def client():
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
    yield TestClient(app)
    Base.metadata.drop_all(engine)
    app.dependency_overrides.clear()


def test_passive_intro_cap_returns_expected_keys(client):
    response = client.get("/api/sessions/passive-intro-cap")
    assert response.status_code == 200
    data = response.json()
    assert data["cap_exceeded"] is False
    assert data["words_introduced_today"] == 0
    assert isinstance(data["max_new_per_day"], int)
    assert data["max_new_per_day"] > 0
