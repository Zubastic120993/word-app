"""Tests for Quick Add → Inbox and Move (triage) endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, engine
from app.models.learning_unit import LearningUnit
from app.models.vocabulary import Vocabulary
from app.routers.upload import DEFAULT_USER_KEY, INBOX_VOCAB_NAME


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    from sqlalchemy.orm import Session
    session = Session(bind=engine)
    yield session
    session.close()


# ── Quick Add ──────────────────────────────────────────────────────────────

class TestQuickAddToInbox:
    def test_adds_unit_to_inbox_vocabulary(self, client, db):
        res = client.post("/api/units/inbox", json={"text": "pociąg", "translation": "train"})
        assert res.status_code == 200
        data = res.json()
        assert data["text"] == "pociąg"
        assert data["translation"] == "train"

        inbox = db.query(Vocabulary).filter(
            Vocabulary.user_key == DEFAULT_USER_KEY,
            Vocabulary.name == INBOX_VOCAB_NAME,
        ).first()
        assert inbox is not None

        unit = db.query(LearningUnit).filter(LearningUnit.id == data["id"]).first()
        assert unit is not None
        assert unit.vocabulary_id == inbox.id
        assert unit.source_pdf == INBOX_VOCAB_NAME

    def test_creates_inbox_vocabulary_on_first_use(self, client, db):
        inbox_before = db.query(Vocabulary).filter(
            Vocabulary.name == INBOX_VOCAB_NAME
        ).first()
        assert inbox_before is None

        client.post("/api/units/inbox", json={"text": "słowo", "translation": "word"})

        inbox_after = db.query(Vocabulary).filter(
            Vocabulary.name == INBOX_VOCAB_NAME
        ).first()
        assert inbox_after is not None

    def test_rejects_duplicate(self, client):
        client.post("/api/units/inbox", json={"text": "pociąg", "translation": "train"})
        res = client.post("/api/units/inbox", json={"text": "pociąg", "translation": "train"})
        assert res.status_code == 400

    def test_rejects_empty_text(self, client):
        res = client.post("/api/units/inbox", json={"text": "", "translation": "train"})
        assert res.status_code == 400

    def test_rejects_empty_translation(self, client):
        res = client.post("/api/units/inbox", json={"text": "pociąg", "translation": ""})
        assert res.status_code == 400

    def test_detects_word_type(self, client):
        res = client.post("/api/units/inbox", json={"text": "pociąg", "translation": "train"})
        assert res.json()["type"] == "word"

    def test_detects_phrase_type(self, client):
        res = client.post("/api/units/inbox", json={"text": "w porządku", "translation": "okay"})
        assert res.json()["type"] == "phrase"

    def test_detects_sentence_type(self, client):
        res = client.post("/api/units/inbox", json={"text": "Gdzie jest toaleta?", "translation": "Where is the toilet?"})
        assert res.json()["type"] == "sentence"

    def test_strips_whitespace(self, client, db):
        res = client.post("/api/units/inbox", json={"text": "  kot  ", "translation": "  cat  "})
        assert res.status_code == 200
        assert res.json()["text"] == "kot"
        assert res.json()["translation"] == "cat"


# ── Get Inbox ──────────────────────────────────────────────────────────────

class TestGetInboxUnits:
    def test_returns_empty_list_when_no_inbox(self, client):
        res = client.get("/api/units/inbox")
        assert res.status_code == 200
        assert res.json() == []

    def test_returns_inbox_units(self, client):
        client.post("/api/units/inbox", json={"text": "pociąg", "translation": "train"})
        client.post("/api/units/inbox", json={"text": "kot", "translation": "cat"})

        res = client.get("/api/units/inbox")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 2
        texts = {u["text"] for u in data}
        assert texts == {"pociąg", "kot"}

    def test_inbox_unit_has_required_fields(self, client):
        client.post("/api/units/inbox", json={"text": "pociąg", "translation": "train"})
        units = client.get("/api/units/inbox").json()
        u = units[0]
        assert "id" in u
        assert "text" in u
        assert "translation" in u
        assert "type" in u


# ── Move (triage) ──────────────────────────────────────────────────────────

class TestMoveUnitFromInbox:
    def _add_inbox_unit(self, client, text="pociąg", translation="train"):
        res = client.post("/api/units/inbox", json={"text": text, "translation": translation})
        assert res.status_code == 200
        return res.json()["id"]

    def _create_vocabulary(self, db, name="My Vocab"):
        vocab = Vocabulary(user_key=DEFAULT_USER_KEY, name=name)
        db.add(vocab)
        db.commit()
        db.refresh(vocab)
        return vocab

    def test_moves_unit_to_target_vocabulary(self, client, db):
        unit_id = self._add_inbox_unit(client)
        target = self._create_vocabulary(db)

        res = client.put(f"/api/units/{unit_id}/move", json={"vocabulary_id": target.id})
        assert res.status_code == 200

        db.expire_all()
        unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
        assert unit.vocabulary_id == target.id
        assert unit.source_pdf == target.name

    def test_unit_no_longer_in_inbox_after_move(self, client, db):
        unit_id = self._add_inbox_unit(client)
        target = self._create_vocabulary(db)
        client.put(f"/api/units/{unit_id}/move", json={"vocabulary_id": target.id})

        inbox_units = client.get("/api/units/inbox").json()
        ids = [u["id"] for u in inbox_units]
        assert unit_id not in ids

    def test_move_to_nonexistent_vocabulary_returns_404(self, client):
        unit_id = self._add_inbox_unit(client)
        res = client.put(f"/api/units/{unit_id}/move", json={"vocabulary_id": 99999})
        assert res.status_code == 404

    def test_move_nonexistent_unit_returns_404(self, client, db):
        target = self._create_vocabulary(db)
        res = client.put("/api/units/99999/move", json={"vocabulary_id": target.id})
        assert res.status_code == 404

    def test_move_is_idempotent(self, client, db):
        unit_id = self._add_inbox_unit(client)
        target = self._create_vocabulary(db)

        r1 = client.put(f"/api/units/{unit_id}/move", json={"vocabulary_id": target.id})
        r2 = client.put(f"/api/units/{unit_id}/move", json={"vocabulary_id": target.id})
        assert r1.status_code == 200
        assert r2.status_code == 200
