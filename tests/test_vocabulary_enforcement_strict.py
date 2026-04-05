import pytest
from app.services.ai.vocabulary_validator import VocabularyValidator


class DummyDB:
    """Minimal dummy DB to satisfy constructor."""
    pass


def run_validator(text, allowed):
    validator = VocabularyValidator(db=DummyDB())

    # Override vocabulary source to avoid DB dependency
    validator.get_allowed_vocabulary = lambda *args, **kwargs: {
        w.lower() for w in allowed
    }

    result = validator.validate_response(
        response_text=text,
        session_id=None,
        include_learned=False,
    )

    return {
        "is_valid": result.is_valid,
        "unknown_words": result.unknown_words,
        "vocabulary_set_size": len({w.lower() for w in allowed}),
    }


# ------------------------------------------------------------
# TESTS
# ------------------------------------------------------------

def test_exact_match_passes():
    result = run_validator("dom", ["dom"])
    assert result["is_valid"] is True
    assert result["unknown_words"] == []


def test_unknown_word_fails():
    result = run_validator("nieznane", ["dom"])
    assert result["is_valid"] is False
    assert "nieznane" in result["unknown_words"]


def test_proper_noun_bypasses():
    result = run_validator("Warszawa", ["dom"])
    assert result["is_valid"] is True


def test_numeric_token_skipped():
    result = run_validator("12345", ["dom"])
    assert result["is_valid"] is True


def test_short_token_skipped():
    result = run_validator("a", ["dom"])
    assert result["is_valid"] is True


def test_morphology_within_boundary_passes():
    result = run_validator("domowy", ["domowy"])
    assert result["is_valid"] is True


def test_morphology_exceeding_boundary_fails():
    result = run_validator("domooooooooooo", ["dom"])
    assert result["is_valid"] is False


def test_mixed_tokens():
    result = run_validator("dom nieznane", ["dom"])
    assert result["is_valid"] is False
    assert "nieznane" in result["unknown_words"]


def test_fallback_phrase_passes():
    result = run_validator(
        "Spróbuj odpowiedzieć jeszcze raz.",
        ["spróbuj", "odpowiedzieć", "jeszcze", "raz"]
    )
    assert result["is_valid"] is True


def test_vocabulary_size_reporting():
    result = run_validator("dom", ["dom", "kot"])
    assert result["vocabulary_set_size"] == 2
