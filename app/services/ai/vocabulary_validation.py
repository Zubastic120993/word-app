"""AI-assisted vocabulary validation service."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.config import settings
from app.services.ai.openai_client import OpenAIClient
from app.services.ai.base import AIMessage, AIRole

logger = logging.getLogger(__name__)


class ValidationLevel(str, Enum):
    """Level of validation needed for a unit."""
    CLEAN = "clean"        # No issues found
    SAFE_FIX = "safe_fix"  # Safe auto-fix (punctuation, obvious typo)
    REVIEW = "review"      # Needs user confirmation
    MANUAL = "manual"      # Needs manual editing


@dataclass
class ValidationSuggestion:
    """Suggestion for a vocabulary unit."""
    original_text: str
    original_translation: str
    suggested_text: Optional[str] = None
    suggested_translation: Optional[str] = None
    has_spelling_error: bool = False
    has_punctuation_error: bool = False
    confidence: float = 1.0  # 0.0-1.0, high = safe to auto-apply
    notes: Optional[str] = None
    
    @property
    def has_suggestions(self) -> bool:
        """Check if any suggestions were made."""
        return (
            self.suggested_text is not None or
            self.suggested_translation is not None or
            self.has_spelling_error or
            self.has_punctuation_error
        )
    
    @property
    def validation_level(self) -> ValidationLevel:
        """
        Determine validation level based on suggestion type and confidence.
        
        - clean: No issues
        - safe_fix: High confidence punctuation-only fix
        - review: Medium confidence or spelling fix
        - manual: Low confidence or major changes
        """
        if not self.has_suggestions:
            return ValidationLevel.CLEAN
        
        # Check if it's punctuation-only
        is_punctuation_only = (
            self.has_punctuation_error and 
            not self.has_spelling_error
        )
        
        # High confidence punctuation fix = safe
        if is_punctuation_only and self.confidence >= 0.9:
            return ValidationLevel.SAFE_FIX
        
        # High confidence spelling fix = review (diacritics matter)
        if self.confidence >= 0.8:
            return ValidationLevel.REVIEW
        
        # Low confidence = manual
        if self.confidence < 0.6:
            return ValidationLevel.MANUAL
        
        # Default to review
        return ValidationLevel.REVIEW


@dataclass
class ValidationResult:
    """Result of vocabulary validation."""
    suggestions: list[ValidationSuggestion] = field(default_factory=list)
    ai_available: bool = False
    ai_error: Optional[str] = None
    tokens_used: Optional[int] = None
    
    @property
    def summary(self) -> dict:
        """Get summary counts by validation level."""
        counts = {level: 0 for level in ValidationLevel}
        for s in self.suggestions:
            counts[s.validation_level] += 1
        return {
            "clean": counts[ValidationLevel.CLEAN],
            "safe_fix": counts[ValidationLevel.SAFE_FIX],
            "review": counts[ValidationLevel.REVIEW],
            "manual": counts[ValidationLevel.MANUAL],
            "auto_accepted": counts[ValidationLevel.CLEAN] + counts[ValidationLevel.SAFE_FIX],
            "needs_review": counts[ValidationLevel.REVIEW] + counts[ValidationLevel.MANUAL],
        }


def get_validation_prompt(source_lang: str, target_lang: str) -> str:
    """Generate validation prompt based on configured languages."""
    
    # Language-specific diacritics hints
    diacritics_hints = {
        "Polish": "Polish diacritics (ą, ć, ę, ł, ń, ó, ś, ź, ż)",
        "Ukrainian": "Ukrainian Cyrillic letters (і, ї, є, ґ)",
        "Russian": "Russian Cyrillic letters",
        "German": "German umlauts (ä, ö, ü, ß)",
        "French": "French accents (é, è, ê, ë, à, â, ç, etc.)",
        "Spanish": "Spanish accents (á, é, í, ó, ú, ñ, ü)",
    }
    
    source_hint = diacritics_hints.get(source_lang, f"{source_lang} special characters")
    target_hint = diacritics_hints.get(target_lang, f"{target_lang} special characters")
    
    return f"""You are a vocabulary validation assistant for a {source_lang}-{target_lang} learning app.

Your task is to check vocabulary pairs for spelling and punctuation errors.

INPUT: A JSON array of vocabulary pairs with "text" ({source_lang}) and "translation" ({target_lang}).

OUTPUT: A JSON array with the same items, adding these fields:
- "suggested_text": corrected {source_lang} text (only if there's an error, otherwise null)
- "suggested_translation": corrected {target_lang} translation (only if there's an error, otherwise null)  
- "has_spelling_error": true if spelling error found (includes missing diacritics)
- "has_punctuation_error": true if punctuation error found
- "confidence": number 0.0-1.0 indicating how confident you are in the correction
  - 1.0 = certain (obvious punctuation like missing period)
  - 0.9 = very confident (clear typo or missing diacritic)
  - 0.7 = confident (likely error but could be intentional)
  - 0.5 = uncertain (ambiguous, might be correct)
- "notes": brief explanation of corrections (if any)

Rules:
1. Check {source_lang} spelling including {source_hint}
2. Check {target_lang} spelling including {target_hint}
3. Check sentence punctuation (sentences should end with . ! ?)
4. Be conservative - only flag clear errors
5. Set confidence HIGH (0.9+) for punctuation fixes
6. Set confidence MEDIUM (0.7-0.8) for diacritic fixes
7. Set confidence LOW (<0.7) for uncertain changes
8. Preserve original casing style
9. Return ONLY valid JSON, no markdown or explanation
"""


class VocabularyValidator:
    """
    Service for AI-assisted vocabulary validation.
    
    Uses OpenAI to check spelling and punctuation of vocabulary pairs.
    Falls back gracefully if AI is unavailable.
    """
    
    MAX_CONCURRENT = 3

    def __init__(self):
        """Initialize validator with OpenAI client."""
        self.client = OpenAIClient()
        self.batch_size = settings.vocab_validation_batch_size
    
    @property
    def is_available(self) -> bool:
        """Check if validation is enabled and AI is available."""
        return (
            settings.vocab_validation_enabled and
            settings.openai_enabled and
            self.client.is_enabled
        )
    
    async def validate_units(
        self,
        units: list[dict],
    ) -> ValidationResult:
        """
        Validate vocabulary units using AI.
        
        Args:
            units: List of dicts with "text" and "translation" keys.
            
        Returns:
            ValidationResult with suggestions and status.
        """
        if not self.is_available:
            return ValidationResult(
                suggestions=[
                    ValidationSuggestion(
                        original_text=u.get("text", ""),
                        original_translation=u.get("translation", ""),
                    )
                    for u in units
                ],
                ai_available=False,
                ai_error="AI validation not available (OpenAI not configured)",
            )
        
        batches = [
            units[i:i + self.batch_size]
            for i in range(0, len(units), self.batch_size)
        ]

        semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)

        async def _run(batch):
            async with semaphore:
                try:
                    return await self._validate_batch(batch)
                except Exception as e:
                    logger.warning(f"Batch validation failed ({len(batch)} units): {e}")
                    fallback = [
                        ValidationSuggestion(
                            original_text=u.get("text", ""),
                            original_translation=u.get("translation", ""),
                        )
                        for u in batch
                    ]
                    return fallback, 0

        results = await asyncio.gather(*[_run(b) for b in batches])

        all_suggestions = []
        total_tokens = 0
        for batch_suggestions, tokens in results:
            all_suggestions.extend(batch_suggestions)
            total_tokens += tokens or 0
        
        return ValidationResult(
            suggestions=all_suggestions,
            ai_available=True,
            tokens_used=total_tokens,
        )
    
    async def _validate_batch(
        self,
        batch: list[dict],
    ) -> tuple[list[ValidationSuggestion], Optional[int]]:
        """
        Validate a batch of units.
        
        Args:
            batch: List of units to validate.
            
        Returns:
            Tuple of (suggestions, tokens_used).
        """
        # Prepare input
        input_data = [
            {"text": u.get("text", ""), "translation": u.get("translation", "")}
            for u in batch
        ]
        
        # Get language-aware prompt
        prompt = get_validation_prompt(
            settings.source_language,
            settings.target_language,
        )
        
        messages = [
            AIMessage(role=AIRole.SYSTEM, content=prompt),
            AIMessage(role=AIRole.USER, content=json.dumps(input_data, ensure_ascii=False)),
        ]
        
        # Call AI
        response = await self.client.generate(
            messages=messages,
            temperature=0.1,  # Low temperature for consistent results
            max_tokens=2500,
        )
        
        # Parse response
        suggestions = self._parse_response(response.content, batch)
        
        return suggestions, response.tokens_used
    
    def _parse_response(
        self,
        content: str,
        original_batch: list[dict],
    ) -> list[ValidationSuggestion]:
        """
        Parse AI response into ValidationSuggestion objects.
        
        Args:
            content: Raw AI response.
            original_batch: Original input for fallback.
            
        Returns:
            List of ValidationSuggestion objects.
        """
        try:
            # Try to extract JSON from response
            content = content.strip()
            
            # Handle markdown code blocks
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1])
            
            data = json.loads(content)
            
            if not isinstance(data, list):
                raise ValueError("Expected JSON array")
            
            suggestions = []
            for i, item in enumerate(data):
                original = original_batch[i] if i < len(original_batch) else {}
                
                suggestions.append(ValidationSuggestion(
                    original_text=original.get("text", ""),
                    original_translation=original.get("translation", ""),
                    suggested_text=item.get("suggested_text"),
                    suggested_translation=item.get("suggested_translation"),
                    has_spelling_error=item.get("has_spelling_error", False),
                    has_punctuation_error=item.get("has_punctuation_error", False),
                    confidence=float(item.get("confidence", 0.5)),
                    notes=item.get("notes"),
                ))
            
            return suggestions
            
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(f"Failed to parse AI response: {e}")
            
            # Fall back to no suggestions
            return [
                ValidationSuggestion(
                    original_text=u.get("text", ""),
                    original_translation=u.get("translation", ""),
                )
                for u in original_batch
            ]
