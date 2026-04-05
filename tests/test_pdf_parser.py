"""Tests for PDF parser service."""

import pytest

from app.models.learning_unit import UnitType
from app.services.pdf_parser import PDFParser, extract_sentence_for_word


class TestPDFParserLineDetection:
    """Test unit type detection logic."""
    
    def test_word_detection_simple(self):
        """Single word without punctuation should be detected as word."""
        result = PDFParser.parse_line_standalone("cukier - sugar")
        assert result is not None
        assert result.unit_type == UnitType.WORD
        assert result.text == "cukier"
        assert result.translation == "sugar"
    
    def test_word_detection_with_pos(self):
        """Word with part of speech in parentheses."""
        result = PDFParser.parse_line_standalone("cukier (noun) - sugar")
        assert result is not None
        assert result.unit_type == UnitType.WORD
        assert result.text == "cukier"
        assert result.part_of_speech == "noun"
        assert result.translation == "sugar"
    
    def test_phrase_detection(self):
        """Multiple words without sentence punctuation should be phrase."""
        result = PDFParser.parse_line_standalone("Co to jest - What is this")
        assert result is not None
        assert result.unit_type == UnitType.PHRASE
        assert result.text == "Co to jest"
        assert result.translation == "What is this"
    
    def test_sentence_detection_period(self):
        """Text ending with period should be sentence."""
        result = PDFParser.parse_line_standalone("Jestem z Polski. - I am from Poland.")
        assert result is not None
        assert result.unit_type == UnitType.SENTENCE
        assert result.text == "Jestem z Polski."
        assert result.translation == "I am from Poland."
    
    def test_sentence_detection_question(self):
        """Text ending with question mark should be sentence."""
        result = PDFParser.parse_line_standalone("Co to jest? - What is this?")
        assert result is not None
        assert result.unit_type == UnitType.SENTENCE
        assert result.text == "Co to jest?"
    
    def test_sentence_detection_exclamation(self):
        """Text ending with exclamation mark should be sentence."""
        result = PDFParser.parse_line_standalone("Cześć! - Hello!")
        assert result is not None
        assert result.unit_type == UnitType.SENTENCE
        assert result.text == "Cześć!"


class TestPDFParserDelimiters:
    """Test delimiter handling."""
    
    def test_delimiter_space_dash_space(self):
        """Standard ' - ' delimiter."""
        result = PDFParser.parse_line_standalone("słowo - word")
        assert result is not None
        assert result.text == "słowo"
        assert result.translation == "word"
    
    def test_delimiter_space_endash_space(self):
        """En-dash ' – ' delimiter."""
        result = PDFParser.parse_line_standalone("słowo – word")
        assert result is not None
        assert result.text == "słowo"
        assert result.translation == "word"
    
    def test_delimiter_endash_only(self):
        """En-dash without spaces."""
        result = PDFParser.parse_line_standalone("słowo–word")
        assert result is not None
        assert result.text == "słowo"
        assert result.translation == "word"
    
    def test_delimiter_dash_only(self):
        """Regular dash without spaces (lowest priority)."""
        result = PDFParser.parse_line_standalone("słowo-word")
        assert result is not None
        assert result.text == "słowo"
        assert result.translation == "word"
    
    def test_no_delimiter_returns_none(self):
        """Line without delimiter should return None."""
        result = PDFParser.parse_line_standalone("just some text")
        assert result is None


class TestPDFParserPartOfSpeech:
    """Test part of speech extraction."""
    
    def test_noun_extraction(self):
        """Extract noun part of speech."""
        result = PDFParser.parse_line_standalone("dom (noun) - house")
        assert result is not None
        assert result.part_of_speech == "noun"
        assert result.text == "dom"
    
    def test_verb_extraction(self):
        """Extract verb part of speech."""
        result = PDFParser.parse_line_standalone("iść (verb) - to go")
        assert result is not None
        assert result.part_of_speech == "verb"
        assert result.text == "iść"
    
    def test_adjective_extraction(self):
        """Extract adjective part of speech."""
        result = PDFParser.parse_line_standalone("duży (adj) - big")
        assert result is not None
        assert result.part_of_speech == "adj"
        assert result.text == "duży"
    
    def test_no_pos_present(self):
        """No parentheses means no part of speech."""
        result = PDFParser.parse_line_standalone("kot - cat")
        assert result is not None
        assert result.part_of_speech is None
        assert result.text == "kot"
    
    def test_parentheses_in_translation(self):
        """Parentheses in translation should not affect source."""
        result = PDFParser.parse_line_standalone("pies - dog (animal)")
        assert result is not None
        assert result.text == "pies"
        assert result.translation == "dog (animal)"


class TestPDFParserEdgeCases:
    """Test edge cases and error handling."""
    
    def test_empty_line(self):
        """Empty line should return None."""
        result = PDFParser.parse_line_standalone("")
        assert result is None
    
    def test_whitespace_only(self):
        """Whitespace-only line should return None."""
        result = PDFParser.parse_line_standalone("   ")
        assert result is None
    
    def test_preserves_unicode(self):
        """Polish characters should be preserved."""
        result = PDFParser.parse_line_standalone("żółć - yellowness")
        assert result is not None
        assert result.text == "żółć"
    
    def test_complex_phrase(self):
        """Complex phrase with special characters."""
        result = PDFParser.parse_line_standalone("Jak się masz? - How are you?")
        assert result is not None
        assert result.unit_type == UnitType.SENTENCE
        assert result.text == "Jak się masz?"


class TestExtractSentenceForWord:
    """Tests for extract_sentence_for_word()."""

    def test_extracts_sentence_containing_word(self):
        text = "Ona ma kota. Pociąg odjechał ze stacji."
        result = extract_sentence_for_word("pociąg", text)
        assert result == "Pociąg odjechał ze stacji."

    def test_returns_none_when_word_not_in_text(self):
        result = extract_sentence_for_word("pociąg", "Nie ma tu nic.")
        assert result is None

    def test_no_substring_false_positive_for_short_word(self):
        """'ma' must NOT match inside 'mama'."""
        text = "Mama idzie do sklepu."
        result = extract_sentence_for_word("ma", text)
        assert result is None

    def test_single_word_matches_at_word_boundary(self):
        text = "Ona ma kota w domu."
        result = extract_sentence_for_word("ma", text)
        assert result == "Ona ma kota w domu."

    def test_returns_shortest_matching_sentence(self):
        text = "Ona ma kota. Ona ma dużego czarnego kota w ogrodzie."
        result = extract_sentence_for_word("ma", text)
        assert result == "Ona ma kota."

    def test_empty_word_returns_none(self):
        assert extract_sentence_for_word("", "Ona ma kota.") is None

    def test_empty_text_returns_none(self):
        assert extract_sentence_for_word("ma", "") is None

    def test_phrase_match_uses_substring(self):
        text = "To jest w porządku. Coś innego."
        result = extract_sentence_for_word("w porządku", text)
        assert result == "To jest w porządku."

    def test_sentence_over_250_chars_excluded(self):
        long_sentence = "Ona " + "bardzo " * 40 + "ma kota."  # > 250 chars
        short_sentence = "Ona ma kota."
        text = long_sentence + " " + short_sentence
        result = extract_sentence_for_word("ma", text)
        assert result == short_sentence

    def test_case_insensitive_match(self):
        text = "Ma ona kota."
        result = extract_sentence_for_word("ma", text)
        assert result == "Ma ona kota."

    def test_word_with_polish_diacritics(self):
        text = "Żółw pływa wolno. Coś innego."
        result = extract_sentence_for_word("żółw", text)
        assert result == "Żółw pływa wolno."

    def test_no_match_returns_none_when_only_substring(self):
        """'kot' must NOT match 'kota' (substring of inflected form)."""
        text = "Ona ma kota."
        result = extract_sentence_for_word("kot", text)
        assert result is None

    def test_splits_on_lowercase_start_sentence(self):
        """Sentence boundaries not followed by uppercase are now included."""
        text = "Ona idzie do sklepu. kupiła mleko i chleb."
        result = extract_sentence_for_word("mleko", text)
        assert result == "kupiła mleko i chleb."


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
