"""Study Mode AI service with strict vocabulary enforcement."""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.ai.base import AIProvider, AIMessage, AIRole
from app.services.ai.vocabulary_validator import VocabularyValidator, ValidationResult
from app.services.ai.prompts import get_study_mode_system_prompt
from app.services.analytics_service import record_event
from app.services.prompt_builder import (
    build_system_prompt,
    estimate_token_count,
    MAX_SYSTEM_PROMPT_TOKENS,
)

logger = logging.getLogger(__name__)


def _extract_response_text(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
            return parsed["response"]
    except Exception:
        pass
    return raw


@dataclass
class StudyModeResponse:
    """Response from Study Mode AI."""
    content: str
    validation: ValidationResult
    user_correct: bool
    ai_vocab_valid: bool
    session_id: Optional[int]
    provider: str
    model: str
    warning: Optional[str] = None


class StudyModeService:
    """
    AI service for Study Mode with strict vocabulary enforcement.
    
    In Study Mode:
    - AI can ONLY use vocabulary from the current session + learned units
    - AI output is validated against allowed vocabulary
    - Responses that violate vocabulary rules are flagged
    """
    
    def __init__(
        self,
        provider: AIProvider,
        db: Session,
    ):
        """
        Initialize Study Mode service.
        
        Args:
            provider: AI provider (Ollama or OpenAI).
            db: Database session.
        """
        self.provider = provider
        self.db = db
        self.validator = VocabularyValidator(db)
        self._conversation_history: list[AIMessage] = []

    def evaluate_user_answer(
        self,
        user_message: str,
        session_id: Optional[int] = None,
    ) -> bool:
        """
        Evaluate learner correctness independently from AI vocab enforcement.

        Current Study Mode flow does not provide structured expected-answer
        context for semantic grading in this service, so correctness remains
        the prior baseline behavior (valid user input assumed).
        """
        _ = (user_message, session_id)  # preserve signature for future semantic evaluator
        return True
    
    async def respond(
        self,
        user_message: str,
        session_id: Optional[int] = None,
        include_learned: bool = True,
        validate_output: bool = True,
        temperature: float = 0.3,  # Lower temperature for determinism
    ) -> StudyModeResponse:
        """
        Generate a response in Study Mode.
        
        Args:
            user_message: User's input.
            session_id: Current learning session ID.
            include_learned: Include previously learned vocabulary.
            validate_output: Whether to validate AI output.
            temperature: Sampling temperature.
            
        Returns:
            StudyModeResponse with validated content.
        """
        # Build vocabulary context
        vocab_context = self.validator.build_vocabulary_context(
            session_id=session_id,
            include_learned=include_learned,
        )
        
        # Get system prompt
        base_prompt = get_study_mode_system_prompt(vocab_context)
        system_prompt = build_system_prompt(base_prompt, correction_mode=True, mode="study")
        
        estimated = estimate_token_count(system_prompt)
        if estimated > MAX_SYSTEM_PROMPT_TOKENS:
            logger.warning(
                "StudyModeService.respond: system prompt ~%d tokens exceeds limit of %d",
                estimated, MAX_SYSTEM_PROMPT_TOKENS,
            )
        
        # Create messages
        messages = self.provider.create_messages(
            system_prompt=system_prompt,
            user_message=user_message,
            conversation_history=self._conversation_history,
        )
        
        # User correctness must be independent from AI vocab validation.
        user_validation = ValidationResult(
            is_valid=True,
            unknown_words=[],
            allowed_words_used=[],
            message="Validation skipped",
        )
        user_correct = self.evaluate_user_answer(
            user_message=user_message,
            session_id=session_id,
        )
        ai_vocab_valid = True
        final_content = None
        warning = None
        lesson_id = session_id
        ai_response = None

        try:
            if validate_output:
                for attempt in range(1, 3):
                    ai_response = await self.provider.generate(
                        messages=messages,
                        temperature=temperature,
                    )

                    content = ai_response.content
                    content = _extract_response_text(content)
                    logger.debug("study_mode_extracted_text=%s", content)
                    validation = self.validator.validate_response(
                        response_text=content,
                        session_id=session_id,
                        include_learned=include_learned,
                    )
                    ai_vocab_valid = validation.is_valid

                    # Keep lexical validation payload for response metadata only.
                    if attempt == 1:
                        user_validation = validation

                    if ai_vocab_valid:
                        final_content = content
                        break

                    try:
                        record_event(
                            db=self.db,
                            event_type="vocab_violation",
                            theme=None,
                            payload={
                                "unknown_words": validation.unknown_words,
                                "vocabulary_set_size": len(validation.allowed_words_used),
                                "lesson_id": lesson_id,
                                "regeneration_attempt": attempt,
                            },
                        )
                    except Exception:
                        pass

                    if attempt == 1:
                        messages.append(
                            AIMessage(
                                role=AIRole.SYSTEM,
                                content=(
                                    "STRICT RULE: You must use ONLY vocabulary from the allowed word list. "
                                    "Do NOT introduce new words."
                                ),
                            )
                        )
                else:
                    try:
                        record_event(
                            db=self.db,
                            event_type="vocab_enforcement_fallback",
                            theme=None,
                            payload={
                                "lesson_id": lesson_id,
                            },
                        )
                    except Exception:
                        pass

                    final_content = "Spróbuj odpowiedzieć jeszcze raz."
            else:
                ai_response = await self.provider.generate(
                    messages=messages,
                    temperature=temperature,
                )
                final_content = ai_response.content
        except Exception as e:
            logger.error(f"Study Mode AI generation failed: {e}")
            raise

        if validate_output and not user_correct:
            warning = (
                f"AI response contained vocabulary outside the allowed set: "
                f"{', '.join(user_validation.unknown_words[:3])}"
            )
            logger.warning(f"Study Mode validation failed: {user_validation.message}")
        
        if ai_response is None:
            raise RuntimeError("Study Mode generation produced no AI response.")
        
        # Update conversation history
        self._conversation_history.append(
            AIMessage(role=AIRole.USER, content=user_message)
        )
        self._conversation_history.append(
            AIMessage(role=AIRole.ASSISTANT, content=final_content or "")
        )
        
        # Limit history to last 10 exchanges
        if len(self._conversation_history) > 20:
            self._conversation_history = self._conversation_history[-20:]
        
        logger.info(
            "study_mode_result user_correct=%s ai_vocab_valid=%s session_id=%s provider=%s model=%s",
            user_correct,
            ai_vocab_valid,
            session_id,
            ai_response.provider,
            ai_response.model,
        )

        return StudyModeResponse(
            content=final_content or "",
            validation=user_validation,
            user_correct=user_correct,
            ai_vocab_valid=ai_vocab_valid,
            session_id=session_id,
            provider=ai_response.provider,
            model=ai_response.model,
            warning=None,
        )
    
    def clear_history(self) -> None:
        """Clear conversation history."""
        self._conversation_history = []
    
    async def get_vocabulary_help(
        self,
        word: str,
        session_id: Optional[int] = None,
    ) -> StudyModeResponse:
        """
        Get help for a specific vocabulary word.
        
        Args:
            word: The word to get help for.
            session_id: Current learning session ID.
            
        Returns:
            StudyModeResponse with explanation.
        """
        prompt = (
            f"Help me understand and remember this word: '{word}'. "
            f"Give me a simple explanation and an example sentence."
        )
        
        return await self.respond(
            user_message=prompt,
            session_id=session_id,
            include_learned=True,
        )
    
    async def generate_practice_sentence(
        self,
        word: str,
        session_id: Optional[int] = None,
    ) -> StudyModeResponse:
        """
        Generate a practice sentence using a word.
        
        Args:
            word: The word to practice.
            session_id: Current learning session ID.
            
        Returns:
            StudyModeResponse with practice sentence.
        """
        prompt = (
            f"Create a simple example sentence using '{word}'. "
            f"Only use words from my vocabulary list."
        )
        
        return await self.respond(
            user_message=prompt,
            session_id=session_id,
            include_learned=True,
            temperature=0.5,  # Slightly higher for creativity
        )
