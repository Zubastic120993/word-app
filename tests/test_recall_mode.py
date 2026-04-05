"""Tests for Active Recall mode functionality."""

import pytest

from app.services.session_service import (
    normalize_input,
    evaluate_answer,
    levenshtein_distance,
    EvaluationMode,
)
from app.models.learning_unit import RecallResult


class TestNormalizeInput:
    """Test input normalization for recall mode."""
    
    def test_lowercase(self):
        """Input should be lowercased."""
        assert normalize_input("CUKIER") == "cukier"
        assert normalize_input("Cukier") == "cukier"
    
    def test_strip_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        assert normalize_input("  cukier  ") == "cukier"
        assert normalize_input("\tcukier\n") == "cukier"
    
    def test_collapse_multiple_spaces(self):
        """Multiple spaces should become single space."""
        assert normalize_input("co  to   jest") == "co to jest"
    
    def test_preserve_polish_characters(self):
        """Polish diacritics should be preserved."""
        assert normalize_input("ŻÓŁĆ") == "żółć"
        assert normalize_input("Cześć") == "cześć"
        assert normalize_input("DZIĘKUJĘ") == "dziękuję"
    
    def test_empty_string(self):
        """Empty string should return empty."""
        assert normalize_input("") == ""
        assert normalize_input("   ") == ""
    
    def test_unicode_normalization(self):
        """Unicode should be normalized to NFC form."""
        result = normalize_input("są")
        assert result == "są"
    
    def test_strip_punctuation_disabled(self):
        """Punctuation preserved when strip_punctuation=False."""
        assert normalize_input("cześć!", strip_punctuation=False) == "cześć!"
        assert normalize_input("co to?", strip_punctuation=False) == "co to?"
    
    def test_strip_punctuation_enabled(self):
        """Punctuation removed when strip_punctuation=True."""
        assert normalize_input("cześć!", strip_punctuation=True) == "cześć"
        assert normalize_input("co to?", strip_punctuation=True) == "co to"
        assert normalize_input("tak, oczywiście.", strip_punctuation=True) == "tak oczywiście"
    
    def test_strip_various_punctuation(self):
        """Various punctuation marks should be stripped."""
        assert normalize_input("test...", strip_punctuation=True) == "test"
        assert normalize_input("(test)", strip_punctuation=True) == "test"
        assert normalize_input("\"test\"", strip_punctuation=True) == "test"
        assert normalize_input("test—test", strip_punctuation=True) == "testtest"


class TestEvaluateAnswerStrictMode:
    """Test answer evaluation in strict mode."""
    
    def test_exact_match(self):
        """Exact match should be correct."""
        result = evaluate_answer("cukier", "cukier", EvaluationMode.STRICT)
        assert result.is_correct is True
        assert result.evaluation_mode == EvaluationMode.STRICT
    
    def test_case_insensitive(self):
        """Comparison should be case-insensitive."""
        result = evaluate_answer("CUKIER", "cukier", EvaluationMode.STRICT)
        assert result.is_correct is True
    
    def test_punctuation_difference_in_strict_is_partial(self):
        """Punctuation difference in strict mode gives partial credit."""
        result = evaluate_answer("cześć", "cześć!", EvaluationMode.STRICT)
        assert result.result == RecallResult.PARTIAL
        assert result.is_correct is False
    
    def test_punctuation_only_mistake_detected(self):
        """Punctuation-only mistakes should be flagged and get partial credit."""
        result = evaluate_answer("cześć", "cześć!", EvaluationMode.STRICT)
        assert result.is_correct is False
        assert result.result == RecallResult.PARTIAL
        assert result.punctuation_only_mistake is True
    
    def test_punctuation_only_mistake_not_flagged_when_correct(self):
        """No punctuation flag when answer is correct."""
        result = evaluate_answer("cześć!", "cześć!", EvaluationMode.STRICT)
        assert result.is_correct is True
        assert result.punctuation_only_mistake is False
    
    def test_real_mistake_not_punctuation_only(self):
        """Real mistakes should not be flagged as punctuation-only."""
        result = evaluate_answer("czesc", "cześć!", EvaluationMode.STRICT)
        assert result.is_correct is False
        assert result.punctuation_only_mistake is False


class TestEvaluateAnswerLexicalMode:
    """Test answer evaluation in lexical mode (default)."""
    
    def test_exact_match(self):
        """Exact match should be correct."""
        result = evaluate_answer("cukier", "cukier", EvaluationMode.LEXICAL)
        assert result.is_correct is True
        assert result.evaluation_mode == EvaluationMode.LEXICAL
    
    def test_punctuation_ignored(self):
        """Punctuation differences should be ignored."""
        result = evaluate_answer("cześć", "cześć!", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_extra_punctuation_ignored(self):
        """Extra punctuation from user should be ignored."""
        result = evaluate_answer("cześć!!!", "cześć", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_sentence_punctuation_ignored(self):
        """Sentence-ending punctuation should be ignored."""
        result = evaluate_answer("jestem z polski", "Jestem z Polski.", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_question_mark_ignored(self):
        """Question marks should be ignored."""
        result = evaluate_answer("co to jest", "Co to jest?", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_diacritics_still_required(self):
        """Polish diacritics must still match."""
        result = evaluate_answer("zolc", "żółć", EvaluationMode.LEXICAL)
        assert result.is_correct is False
        
        result = evaluate_answer("czesc", "cześć", EvaluationMode.LEXICAL)
        assert result.is_correct is False
    
    def test_default_mode_is_lexical(self):
        """Default evaluation mode should be lexical."""
        result = evaluate_answer("cześć", "cześć!")
        assert result.is_correct is True
        assert result.evaluation_mode == EvaluationMode.LEXICAL
    
    def test_no_punctuation_flag_in_lexical(self):
        """Punctuation-only mistake flag not used in lexical mode."""
        result = evaluate_answer("cześć", "cześć!", EvaluationMode.LEXICAL)
        assert result.punctuation_only_mistake is False  # Not applicable


class TestEvaluateAnswerEdgeCases:
    """Test edge cases for answer evaluation."""
    
    def test_phrase_match_lexical(self):
        """Phrase comparison should work in lexical mode."""
        result = evaluate_answer("co to jest", "Co to jest?", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_sentence_match_lexical(self):
        """Sentence comparison should work in lexical mode."""
        result = evaluate_answer("jestem z polski", "Jestem z Polski.", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_comma_ignored_lexical(self):
        """Commas should be ignored in lexical mode."""
        result = evaluate_answer("tak oczywiście", "tak, oczywiście", EvaluationMode.LEXICAL)
        assert result.is_correct is True
    
    def test_evaluation_returns_all_fields(self):
        """Evaluation should return all expected fields."""
        result = evaluate_answer("  CUKIER  ", "cukier", EvaluationMode.LEXICAL)
        
        assert result.user_input == "  CUKIER  "
        assert result.normalized_input == "cukier"
        assert result.expected_answer == "cukier"
        assert result.normalized_expected == "cukier"
        assert result.is_correct is True
        assert result.evaluation_mode == EvaluationMode.LEXICAL
        assert result.punctuation_only_mistake is False


class TestLevenshteinDistance:
    """Test Levenshtein distance calculation for typo detection."""
    
    def test_identical_strings(self):
        """Identical strings have distance 0."""
        assert levenshtein_distance("cukier", "cukier") == 0
    
    def test_single_substitution(self):
        """Single character substitution = distance 1."""
        assert levenshtein_distance("cukier", "cukiar") == 1
    
    def test_single_insertion(self):
        """Single character insertion = distance 1."""
        assert levenshtein_distance("cukier", "cukierr") == 1
    
    def test_single_deletion(self):
        """Single character deletion = distance 1."""
        assert levenshtein_distance("cukier", "cukir") == 1
    
    def test_two_substitutions(self):
        """Two character substitutions = distance 2."""
        assert levenshtein_distance("cukier", "cukiar") == 1
        assert levenshtein_distance("cukier", "cakiar") == 2
    
    def test_empty_string(self):
        """Distance to empty string is length of other string."""
        assert levenshtein_distance("abc", "") == 3
        assert levenshtein_distance("", "xyz") == 3
    
    def test_completely_different(self):
        """Completely different strings."""
        assert levenshtein_distance("abc", "xyz") == 3
    
    def test_diacritics_as_different_chars(self):
        """Diacritics are treated as different characters."""
        # ś is different from s
        assert levenshtein_distance("cześć", "czesc") == 2  # ś→s, ć→c


class TestRecallResultScoring:
    """Test that evaluate_answer returns correct RecallResult."""
    
    def test_exact_match_is_correct(self):
        """Exact match should return RecallResult.CORRECT."""
        result = evaluate_answer("cukier", "cukier")
        assert result.result == RecallResult.CORRECT
        assert result.is_correct is True
    
    def test_case_difference_is_correct(self):
        """Case difference only should be CORRECT (normalization handles it)."""
        result = evaluate_answer("CUKIER", "cukier")
        assert result.result == RecallResult.CORRECT
        assert result.is_correct is True
    
    def test_single_typo_is_partial(self):
        """Single character typo should return RecallResult.PARTIAL."""
        result = evaluate_answer("cukiar", "cukier")  # a instead of e
        assert result.result == RecallResult.PARTIAL
        assert result.is_correct is False
        assert result.typo_distance == 1
    
    def test_single_missing_char_is_partial(self):
        """Single missing character should return RecallResult.PARTIAL."""
        result = evaluate_answer("cukir", "cukier")  # missing e
        assert result.result == RecallResult.PARTIAL
        assert result.is_correct is False
        assert result.typo_distance == 1
    
    def test_single_extra_char_is_partial(self):
        """Single extra character should return RecallResult.PARTIAL."""
        result = evaluate_answer("cukierr", "cukier")  # extra r
        assert result.result == RecallResult.PARTIAL
        assert result.is_correct is False
        assert result.typo_distance == 1
    
    def test_two_typos_is_failed(self):
        """Two or more character differences should return RecallResult.FAILED."""
        result = evaluate_answer("cukiar", "cukier")  # 1 typo - partial
        assert result.result == RecallResult.PARTIAL
        
        result = evaluate_answer("cakiar", "cukier")  # 2 typos - failed
        assert result.result == RecallResult.FAILED
        assert result.is_correct is False
        assert result.typo_distance == 2
    
    def test_completely_wrong_is_failed(self):
        """Completely wrong answer should return RecallResult.FAILED."""
        result = evaluate_answer("dom", "cukier")
        assert result.result == RecallResult.FAILED
        assert result.is_correct is False
    
    def test_diacritics_required_missing_is_failed(self):
        """Missing diacritics should be FAILED (not partial)."""
        # żółć → zolc is 4 character differences
        result = evaluate_answer("zolc", "żółć")
        assert result.result == RecallResult.FAILED
        assert result.is_correct is False
    
    def test_single_diacritic_missing_is_partial(self):
        """Single diacritic difference might be partial."""
        # cześć → czesc is 2 differences (ś→s, ć→c)
        result = evaluate_answer("czesc", "cześć")
        assert result.result == RecallResult.FAILED
        assert result.typo_distance == 2
    
    def test_punctuation_only_in_lexical_is_correct(self):
        """Punctuation-only difference in lexical mode is CORRECT."""
        result = evaluate_answer("cześć", "cześć!", EvaluationMode.LEXICAL)
        assert result.result == RecallResult.CORRECT
        assert result.is_correct is True
    
    def test_punctuation_only_in_strict_is_partial(self):
        """Punctuation-only difference in strict mode is PARTIAL."""
        result = evaluate_answer("cześć", "cześć!", EvaluationMode.STRICT)
        assert result.result == RecallResult.PARTIAL
        assert result.is_correct is False
        assert result.punctuation_only_mistake is True


class TestPartialCreditEdgeCases:
    """Edge cases for partial credit scoring."""
    
    def test_empty_input_is_failed(self):
        """Empty input should be failed."""
        result = evaluate_answer("", "cukier")
        assert result.result == RecallResult.FAILED
        assert result.is_correct is False
    
    def test_whitespace_only_is_failed(self):
        """Whitespace-only input should be failed."""
        result = evaluate_answer("   ", "cukier")
        assert result.result == RecallResult.FAILED
        assert result.is_correct is False
    
    def test_phrase_single_typo_is_partial(self):
        """Phrase with single typo should be partial."""
        result = evaluate_answer("co to jast", "co to jest")  # a instead of e
        assert result.result == RecallResult.PARTIAL
        assert result.is_correct is False
    
    def test_phrase_two_typos_is_failed(self):
        """Phrase with two typos should be failed."""
        result = evaluate_answer("ca to jast", "co to jest")  # 2 typos
        assert result.result == RecallResult.FAILED
        assert result.is_correct is False
    
    def test_result_field_always_present(self):
        """Result field should always be present in evaluation."""
        result = evaluate_answer("anything", "something")
        assert hasattr(result, 'result')
        assert result.result in [RecallResult.CORRECT, RecallResult.PARTIAL, RecallResult.FAILED]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
