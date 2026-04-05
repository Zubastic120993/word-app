"""Unit tests for cloze sentence generation and prompt building."""

from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, UnitType
from app.models.vocabulary import Vocabulary
from app.services.cloze_service import (
    _is_phrase_unit,
    generate_context_sentence,
    get_or_generate_sentence,
    make_cloze_prompt,
)
@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def polish_unit(db_session):
    db_session.add(Vocabulary(id=1, user_key="t", name="f.pdf"))
    u = LearningUnit(
        text="pociąg",
        type=UnitType.WORD,
        translation="train",
        source_pdf="f.pdf",
        vocabulary_id=1,
        normalized_text="pociąg",
        normalized_translation="train",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def phrase_unit(db_session):
    db_session.add(Vocabulary(id=2, user_key="t2", name="g.pdf"))
    u = LearningUnit(
        text="w porządku",
        type=UnitType.PHRASE,
        translation="okay",
        source_pdf="g.pdf",
        vocabulary_id=2,
        normalized_text="w porządku",
        normalized_translation="okay",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def long_text_word_unit(db_session):
    db_session.add(Vocabulary(id=3, user_key="t3", name="h.pdf"))
    u = LearningUnit(
        text="jeden dwa trzy cztery pięć",
        type=UnitType.WORD,
        translation="one two three four five",
        source_pdf="h.pdf",
        vocabulary_id=3,
        normalized_text="jeden dwa trzy cztery pięć",
        normalized_translation="one two three four five",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def test_is_phrase_unit_false_for_short_phrase(phrase_unit):
    # "w porządku" = 2 words → below threshold, NOT a long phrase
    assert _is_phrase_unit(phrase_unit) is False


def test_is_phrase_unit_true_when_five_or_more_words(long_text_word_unit):
    assert _is_phrase_unit(long_text_word_unit) is True


def test_is_phrase_unit_false_for_single_word(polish_unit):
    assert _is_phrase_unit(polish_unit) is False


def test_is_phrase_unit_false_when_exactly_three_words(db_session):
    db_session.add(Vocabulary(id=10, user_key="t10", name="x.pdf"))
    db_session.commit()
    unit = LearningUnit(
        text="co to znaczy",
        type=UnitType.WORD,
        translation="what does it mean",
        source_pdf="x.pdf",
        vocabulary_id=10,
        normalized_text="co to znaczy",
        normalized_translation="what does it mean",
    )
    assert _is_phrase_unit(unit) is False


def test_is_phrase_unit_false_when_four_words(db_session):
    db_session.add(Vocabulary(id=11, user_key="t11", name="y.pdf"))
    db_session.commit()
    unit = LearningUnit(
        text="jeden dwa trzy cztery",
        type=UnitType.WORD,
        translation="one two three four",
        source_pdf="y.pdf",
        vocabulary_id=11,
        normalized_text="jeden dwa trzy cztery",
        normalized_translation="one two three four",
    )
    assert _is_phrase_unit(unit) is False


@patch("app.services.cloze_service.generate_context_sentence", return_value=False)
def test_get_or_generate_sentence_attempts_short_phrase(mock_gen, db_session, phrase_unit):
    # Short phrase (2 words) is now cloze-eligible — generation is attempted
    get_or_generate_sentence(phrase_unit, db_session, ai_service=None)
    mock_gen.assert_called_once()


def test_make_cloze_prompt_case_insensitive():
    s = make_cloze_prompt("Pociąg odjechał.", "pociąg")
    assert "________" in s
    assert "pociąg" not in s.lower()


def test_make_cloze_prompt_raises_when_target_missing():
    with pytest.raises(ValueError, match="not found"):
        make_cloze_prompt("Brak słowa tutaj.", "pociąg")


def test_make_cloze_prompt_two_word_blanks_only_last_word():
    s = make_cloze_prompt("Mam na imię Jan.", "na imię")
    assert s == "Mam na ________ Jan."


def test_make_cloze_prompt_three_word_blanks_only_last_word():
    s = make_cloze_prompt("To jest bardzo fajne.", "To jest bardzo")
    assert s == "To jest ________ fajne."


def test_make_cloze_prompt_single_word_blanks_whole_word():
    s = make_cloze_prompt("Pociąg odjechał.", "pociąg")
    assert s == "________ odjechał."


@patch("app.services.cloze_service._ai_completion")
def test_generate_accepts_inflected_form(mock_ai, db_session, polish_unit):
    # Word presence check removed — inflected forms are now accepted.
    # A sentence that doesn't contain the base form verbatim is still saved.
    mock_ai.return_value = '{"sentence": "Pociągiem jedzie wielu pasażerów.", "translation": "Many passengers travel by train."}'
    assert generate_context_sentence(polish_unit, db_session, ai_service=None) is True
    db_session.refresh(polish_unit)
    assert polish_unit.context_sentence == "Pociągiem jedzie wielu pasażerów."


@patch("app.services.cloze_service._ai_completion")
def test_generate_returns_false_on_timeout(mock_ai, db_session, polish_unit):
    mock_ai.side_effect = httpx.TimeoutException("timeout")
    assert generate_context_sentence(polish_unit, db_session, ai_service=None) is False


@patch("app.services.cloze_service._ai_completion")
def test_generate_writes_db_on_success(mock_ai, db_session, polish_unit):
    mock_ai.return_value = (
        '{"sentence": "To jest pociąg na peronie.", "translation": "This is a train on the platform."}'
    )
    assert generate_context_sentence(polish_unit, db_session, ai_service=None) is True
    db_session.refresh(polish_unit)
    assert polish_unit.context_sentence == "To jest pociąg na peronie."
    assert "train" in (polish_unit.context_sentence_translation or "").lower()
