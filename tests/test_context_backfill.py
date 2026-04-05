"""Tests for context sentence backfill service and endpoint."""

import os
os.environ["WORD_APP_TESTING"] = "1"

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, UnitType
from app.models.vocabulary import Vocabulary
from app.services.cloze_service import (
    backfill_context_sentences,
    _word_matches_in_sentence,
)

DEFAULT_USER_KEY = "default"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = Session(bind=engine)
    yield session
    session.close()


@pytest.fixture()
def client():
    return TestClient(app)


def _vocab(db):
    v = db.query(Vocabulary).first()
    if not v:
        v = Vocabulary(user_key=DEFAULT_USER_KEY, name="Test")
        db.add(v)
        db.commit()
        db.refresh(v)
    return v


def _unit(db, text, unit_type=UnitType.WORD, context_sentence=None):
    v = _vocab(db)
    u = LearningUnit(
        text=text,
        type=unit_type,
        translation=f"{text}_en",
        source_pdf="test.docx",
        vocabulary_id=v.id,
        context_sentence=context_sentence,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── backfill_context_sentences unit tests ────────────────────────────────────

class TestBackfillContextSentences:
    def test_returns_zero_when_no_units(self, db):
        result = backfill_context_sentences(db, limit=50)
        assert result["processed"] == 0
        assert result["succeeded"] == 0
        assert result["remaining"] == 0

    def test_skips_units_that_already_have_context(self, db):
        _unit(db, "dom", context_sentence="Mam duży dom.")
        result = backfill_context_sentences(db, limit=50)
        assert result["processed"] == 0
        assert result["remaining"] == 0

    def test_processes_word_without_context(self, db):
        _unit(db, "pies")
        with patch("app.services.cloze_service.generate_context_sentence", return_value=True):
            result = backfill_context_sentences(db, limit=50)
        assert result["processed"] == 1

    def test_counts_succeeded_on_true(self, db):
        _unit(db, "kot")
        with patch("app.services.cloze_service.generate_context_sentence", return_value=True):
            result = backfill_context_sentences(db, limit=50)
        assert result["succeeded"] == 1

    def test_counts_failed_as_not_succeeded(self, db):
        _unit(db, "kot")
        with patch("app.services.cloze_service.generate_context_sentence", return_value=False):
            result = backfill_context_sentences(db, limit=50)
        assert result["succeeded"] == 0
        assert result["processed"] == 1

    def test_skips_long_phrases(self, db):
        # ≥5 words — excluded by SQL filter, never reaches Python
        _unit(db, "jeden dwa trzy cztery pięć", unit_type=UnitType.PHRASE)
        result = backfill_context_sentences(db, limit=50)
        assert result["skipped"] == 0
        assert result["processed"] == 0

    def test_short_phrase_is_not_skipped(self, db):
        # 2-word phrase should be processed (cloze-eligible)
        _unit(db, "dobry dzień", unit_type=UnitType.PHRASE)
        with patch("app.services.cloze_service.generate_context_sentence", return_value=True):
            result = backfill_context_sentences(db, limit=50)
        assert result["processed"] == 1
        assert result["skipped"] == 0

    def test_skips_sentence_type(self, db):
        _unit(db, "Idę do szkoły.", unit_type=UnitType.SENTENCE)
        result = backfill_context_sentences(db, limit=50)
        assert result["processed"] == 0

    def test_respects_limit(self, db):
        for i in range(10):
            _unit(db, f"word{i}")
        with patch("app.services.cloze_service.generate_context_sentence", return_value=True):
            result = backfill_context_sentences(db, limit=3)
        assert result["processed"] <= 3

    def test_remaining_counts_words_only(self, db):
        for i in range(5):
            _unit(db, f"word{i}")

        def _fill(unit, db_session):
            unit.context_sentence = "Przykład."
            db_session.commit()
            return True

        with patch("app.services.cloze_service.generate_context_sentence", side_effect=_fill):
            result = backfill_context_sentences(db, limit=2)
        assert result["remaining"] == 3

    def test_remaining_includes_short_phrases(self, db):
        _unit(db, "dobry dzień", unit_type=UnitType.PHRASE)  # 2 words — eligible
        _unit(db, "pies")                                     # word — eligible
        with patch("app.services.cloze_service.generate_context_sentence", return_value=False):
            result = backfill_context_sentences(db, limit=50)
        assert result["remaining"] == 2

    def test_remaining_excludes_long_phrases(self, db):
        _unit(db, "jeden dwa trzy cztery pięć", unit_type=UnitType.PHRASE)  # 5 words — excluded
        _unit(db, "pies")
        with patch("app.services.cloze_service.generate_context_sentence", return_value=False):
            result = backfill_context_sentences(db, limit=50)
        assert result["remaining"] == 1  # only the word

    def test_idempotent_already_filled(self, db):
        u = _unit(db, "dom")
        u.context_sentence = "To jest dom."
        db.commit()
        with patch("app.services.cloze_service.generate_context_sentence") as mock_gen:
            backfill_context_sentences(db, limit=50)
        mock_gen.assert_not_called()


# ── API endpoint tests ────────────────────────────────────────────────────────

class TestBackfillEndpoint:
    def test_endpoint_returns_200(self, client):
        with patch("app.services.cloze_service.generate_context_sentence", return_value=True):
            resp = client.post("/api/admin/backfill-context")
        assert resp.status_code == 200

    def test_endpoint_returns_expected_keys(self, client):
        with patch("app.services.cloze_service.generate_context_sentence", return_value=True):
            resp = client.post("/api/admin/backfill-context")
        data = resp.json()
        assert "processed" in data
        assert "succeeded" in data
        assert "skipped" in data
        assert "remaining" in data

    def test_endpoint_limit_param_respected(self, client, db):
        for i in range(10):
            _unit(db, f"word{i}")
        with patch("app.services.cloze_service.generate_context_sentence", return_value=False):
            resp = client.post("/api/admin/backfill-context?limit=3")
        data = resp.json()
        assert data["processed"] <= 3

    def test_endpoint_rejects_limit_above_200(self, client):
        resp = client.post("/api/admin/backfill-context?limit=201")
        assert resp.status_code == 422

    def test_endpoint_rejects_limit_zero(self, client):
        resp = client.post("/api/admin/backfill-context?limit=0")
        assert resp.status_code == 422


# ── _word_matches_in_sentence tests ──────────────────────────────────────────

class TestWordMatchesInSentence:
    def test_exact_match_short_word(self):
        assert _word_matches_in_sentence("ma", "Ona ma kota.") is True

    def test_exact_no_match_short_word(self):
        assert _word_matches_in_sentence("ma", "Idę do szkoły.") is False

    def test_stem_match_inflected_form(self):
        # "dom" → stem "dom" appears in "domu"
        assert _word_matches_in_sentence("dom", "Mieszkam w dużym domu.") is True

    def test_stem_match_plural(self):
        # stem "słow" (first 4 chars of "słowo") appears in "słowa"
        assert _word_matches_in_sentence("słowo", "Uczę się nowych słowa.") is True

    def test_stem_no_match(self):
        assert _word_matches_in_sentence("pies", "Uczę się polskiego.") is False

    def test_case_insensitive(self):
        assert _word_matches_in_sentence("Dom", "To jest duży DOM.") is True

    def test_exact_match_long_word_verbatim(self):
        assert _word_matches_in_sentence("szkoła", "Idę do szkoły.") is True

    def test_short_word_substring_match(self):
        # short words use `in` (substring) — "ma" is in "mam kota"
        assert _word_matches_in_sentence("ma", "Mam kota.") is True
