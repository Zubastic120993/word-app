"""Vocabulary validator for Study Mode strict enforcement."""

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.learning_unit import LearningUnit, LearningProgress
from app.models.session import LearningSession, SessionUnit

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of vocabulary validation."""
    is_valid: bool
    unknown_words: list[str]
    allowed_words_used: list[str]
    message: str


class VocabularyValidator:
    """
    Validates AI output against allowed vocabulary in Study Mode.
    
    Allowed vocabulary includes:
    1. All units in the current learning session
    2. Previously learned units (if configured)
    
    Common words (articles, prepositions, etc.) are always allowed.
    """
    
    # Common words that are always allowed (not vocabulary items)
    # These are structural/grammatical words in target language (English)
    COMMON_WORDS = {
        # Articles
        "a", "an", "the",
        # Pronouns
        "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
        "my", "your", "his", "its", "our", "their", "mine", "yours", "ours", "theirs",
        "this", "that", "these", "those", "what", "which", "who", "whom", "whose",
        # Prepositions
        "in", "on", "at", "to", "for", "with", "by", "from", "of", "about", "into",
        "through", "during", "before", "after", "above", "below", "between", "under",
        # Conjunctions
        "and", "or", "but", "so", "if", "when", "while", "because", "although", "though",
        # Common verbs
        "is", "are", "was", "were", "be", "been", "being", "am",
        "have", "has", "had", "having",
        "do", "does", "did", "doing", "done",
        "will", "would", "could", "should", "can", "may", "might", "must",
        "say", "said", "says", "mean", "means", "meant",
        # Common adverbs
        "not", "no", "yes", "very", "also", "just", "only", "even", "still",
        "now", "then", "here", "there", "where", "how", "why",
        # Question words
        "what", "where", "when", "why", "how", "who", "which",
        # Numbers
        "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        # Other common
        "please", "thank", "thanks", "sorry", "okay", "ok", "well", "like",
        "more", "less", "much", "many", "some", "any", "all", "each", "every",
        "other", "another", "same", "different", "new", "old", "good", "bad",
        "first", "last", "next", "example", "word", "phrase", "sentence",
        "polish", "english", "language", "translation", "meaning",
    }

    # Grammar-focused whitelist (structural tokens allowed regardless of session vocab)
    GRAMMAR_WHITELIST = {
        "isn't", "aren't", "wasn't", "weren't", "don't", "doesn't", "didn't",
        "won't", "wouldn't", "can't", "couldn't", "shouldn't", "mustn't",
        "i'm", "you're", "we're", "they're", "he's", "she's", "it's",
        "i've", "you've", "we've", "they've", "i'll", "you'll", "we'll", "they'll",
    }
    
    def __init__(self, db: Session):
        """
        Initialize validator.
        
        Args:
            db: Database session.
        """
        self.db = db
    
    def get_allowed_vocabulary(
        self,
        session_id: Optional[int] = None,
        include_learned: bool = True,
        confidence_threshold: float = 0.7,
    ) -> set[str]:
        """
        Get the set of allowed vocabulary words.
        
        Args:
            session_id: Current learning session ID.
            include_learned: Include previously learned units.
            confidence_threshold: Confidence score above which unit is "learned".
            
        Returns:
            Set of allowed words (normalized, lowercase).
        """
        allowed = set()
        
        # Add static and grammar whitelists
        allowed.update(self.COMMON_WORDS)
        allowed.update(self.GRAMMAR_WHITELIST)
        
        # Add session units
        if session_id:
            session_units = (
                self.db.query(LearningUnit)
                .join(SessionUnit)
                .filter(SessionUnit.session_id == session_id)
                .all()
            )
            
            for unit in session_units:
                # Add source text words
                allowed.update(self._extract_words(unit.text))
                # Add translation words
                allowed.update(self._extract_words(unit.translation))
        
        # Add learned units if configured
        if include_learned:
            learned_units = (
                self.db.query(LearningUnit)
                .join(LearningProgress)
                .filter(LearningProgress.confidence_score >= confidence_threshold)
                .all()
            )
            
            for unit in learned_units:
                allowed.update(self._extract_words(unit.text))
                allowed.update(self._extract_words(unit.translation))
        
        # Lowercase once here so downstream token validation can preserve
        # original token casing long enough for proper-noun detection.
        return {word.lower() for word in allowed if word}
    
    def validate_response(
        self,
        response_text: str,
        session_id: Optional[int] = None,
        include_learned: bool = True,
    ) -> ValidationResult:
        """
        Validate AI response against allowed vocabulary.
        
        Args:
            response_text: The AI's response text.
            session_id: Current learning session ID.
            include_learned: Include previously learned units.
            
        Returns:
            ValidationResult with details.
        """
        # Get allowed vocabulary
        allowed_lemmas = self.get_allowed_vocabulary(
            session_id=session_id,
            include_learned=include_learned,
            confidence_threshold=settings.study_mode_confidence_threshold,
        )

        raw_tokens = re.findall(r"\b[\w\-']+\b", response_text)
        total_token_count = len(raw_tokens)

        # Find unknown words using deterministic token pipeline
        unknown = []
        used = []

        for token in raw_tokens:
            # Step 2: skip non-lexical or too-short tokens
            if len(token) < 2:
                continue
            if token.isdigit():
                continue
            if not any(ch.isalpha() for ch in token):
                continue

            # Step 3: proper noun detection before lowercasing
            if token[0].isupper() and token.lower() not in allowed_lemmas:
                continue

            # Step 4: normalize
            normalized_token = unicodedata.normalize("NFKC", token)
            # Step 5: lowercase
            word = normalized_token.lower()

            # Step 6: validate against allowed vocabulary
            if word in allowed_lemmas or self._is_variant_of_allowed(word, allowed_lemmas):
                used.append(word)
            else:
                unknown.append(word)
        
        # Deduplicate while preserving order
        unknown = list(dict.fromkeys(unknown))
        used = list(dict.fromkeys(used))
        
        is_valid = len(unknown) == 0
        
        if is_valid:
            message = "Response uses only allowed vocabulary."
        else:
            message = f"Response contains {len(unknown)} unknown words: {', '.join(unknown[:5])}"
            if len(unknown) > 5:
                message += f" (and {len(unknown) - 5} more)"

        logger.debug(
            "validator_summary: total_tokens=%d unknown=%d",
            total_token_count,
            len(unknown),
        )
        
        return ValidationResult(
            is_valid=is_valid,
            unknown_words=unknown,
            allowed_words_used=used,
            message=message,
        )
    
    def _extract_words(self, text: str) -> set[str]:
        """
        Extract and normalize words from text.
        
        Args:
            text: Input text.
            
        Returns:
            Set of normalized words.
        """
        if not text:
            return set()

        tokens = re.findall(r"\b[\w\-']+\b", text)
        words: set[str] = set()
        for token in tokens:
            if len(token) < 2:
                continue
            if token.isdigit():
                continue
            if not any(ch.isalpha() for ch in token):
                continue
            words.add(unicodedata.normalize("NFKC", token))
        return words
    
    def _is_variant_of_allowed(self, word: str, allowed: set[str]) -> bool:
        """
        Check if a word is a grammatical variant of an allowed word.
        
        Handles common English suffixes/variations.
        
        Args:
            word: Word to check.
            allowed: Set of allowed words.
            
        Returns:
            True if word appears to be a variant.
        """
        if word in allowed:
            return True

        for lemma in allowed:
            if len(lemma) >= 5:
                if word.startswith(lemma[:4]) and len(word) <= len(lemma) + 5:
                    return True
            else:
                if word.startswith(lemma[:3]) and len(word) <= len(lemma) + 4:
                    return True
        return False
    
    def build_vocabulary_context(
        self,
        session_id: Optional[int] = None,
        include_learned: bool = True,
    ) -> str:
        """
        Build a vocabulary context string for the AI prompt.
        
        Args:
            session_id: Current learning session ID.
            include_learned: Include previously learned units.
            
        Returns:
            Formatted vocabulary list for prompt.
        """
        units = []
        
        # Get session units
        if session_id:
            session_units = (
                self.db.query(LearningUnit)
                .join(SessionUnit)
                .filter(SessionUnit.session_id == session_id)
                .order_by(SessionUnit.position)
                .all()
            )
            units.extend(session_units)
        
        # Get learned units
        if include_learned:
            learned = (
                self.db.query(LearningUnit)
                .join(LearningProgress)
                .filter(LearningProgress.confidence_score >= settings.study_mode_confidence_threshold)
                .all()
            )
            # Avoid duplicates
            seen_ids = {u.id for u in units}
            units.extend([u for u in learned if u.id not in seen_ids])
        
        if not units:
            return "No vocabulary available."
        
        lines = ["ALLOWED VOCABULARY:"]
        for unit in units:
            pos = f" ({unit.part_of_speech})" if unit.part_of_speech else ""
            lines.append(f"- {unit.text}{pos} = {unit.translation}")
        
        return "\n".join(lines)
