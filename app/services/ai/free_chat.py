"""Free Chat AI service with unrestricted conversation."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.schemas.ai import RetryValidationRequest, RetryValidationResponse
from app.services.analytics_service import record_event
from app.services.ai.base import AIProvider, AIMessage, AIResponse, AIRole
from app.services.ai.prompts import get_free_chat_system_prompt
from app.services.prompt_builder import (
    build_system_prompt,
    estimate_token_count,
    MAX_SYSTEM_PROMPT_TOKENS,
)
from app.utils.time import utc_now
from app.services.vocab_service import fetch_vocab_bias

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 10
SESSION_VOCAB_TTL = 3600  # seconds
CHECKPOINT_MESSAGE_THRESHOLD = 10
CHECKPOINT_USAGE_THRESHOLD = 0.6
CHECKPOINT_SIZE = 5


def _provider_label(provider: AIProvider) -> str:
    return getattr(provider, "name", provider.__class__.__name__)


def _model_label(provider: AIProvider) -> str:
    return getattr(provider, "model_name", "checkpoint")


def _trim_history(history: list[AIMessage]) -> list[AIMessage]:
    """
    Keep only the last MAX_HISTORY_TURNS exchanges while preserving
    the original message sequence.
    """
    if not history:
        return []

    max_messages = MAX_HISTORY_TURNS * 2
    return history[-max_messages:]


def _looks_like_rewrite(text: str) -> bool:
    """
    Heuristic to detect when the model rewrites a sentence
    but does not return structured corrections JSON.
    This is used only for drift logging.
    """
    if not text:
        return False

    lower = text.lower()

    rewrite_indicators = [
        "→",
        "powinno być",
        "poprawna forma",
        "zamiast",
        "powinieneś",
        "powinnaś",
    ]

    return any(indicator in lower for indicator in rewrite_indicators)



def _build_corrections(
    original: str,
    corrected: str,
    explanation: str = "",
) -> list[dict]:
    """Return the structured corrections payload expected by the UI."""
    if not corrected:
        return []

    normalized_original = " ".join(original.strip().split())
    normalized_corrected = " ".join(corrected.strip().split())
    if not normalized_original or normalized_original == normalized_corrected:
        return []

    return [
        {
            "original": original,
            "corrected": corrected,
            "explanation": explanation or "Grammar and phrasing adjusted while preserving your meaning.",
        }
    ]


async def generate_hint(original: str, correct: str, user_attempt: str) -> str:
    """Generate a short AI hint for a retry attempt without revealing the answer."""
    from app.services.ai.openai_client import OpenAIClient
    from app.services.ai.ollama_client import OllamaClient
    from app.config import settings

    if settings.openai_enabled and settings.openai_api_key:
        provider = OpenAIClient()
    else:
        provider = OllamaClient()

    system_prompt = """
You are a Polish language tutor providing a short hint.

Rules:
- Do NOT provide the full correct sentence.
- Do NOT rewrite the answer.
- Do NOT reveal missing words directly.
- Provide one short guiding hint sentence only.
- Focus on the type of mistake (tense, agreement, modifier, spelling, etc.).
- Maximum 1 sentence.
"""

    user_prompt = f"""
Original sentence:
{original}

Correct sentence:
{correct}

User attempt:
{user_attempt}
"""

    try:
        messages = provider.create_messages(
            system_prompt=system_prompt,
            user_message=user_prompt,
            conversation_history=None,
        )

        ai_response = await provider.generate(
            messages=messages,
            temperature=0.2,
            max_tokens=60,
        )

        return ai_response.content.strip()

    except Exception:
        return ""


@dataclass
class FreeChatResponse:
    """Response from Free Chat AI."""
    content: str
    provider: str
    model: str
    tokens_used: Optional[int] = None
    corrections: list[dict] | None = None
    progress: Optional[dict] = None


class FreeChatService:
    """
    AI service for Free Chat mode with no restrictions.
    
    In Free Chat mode:
    - No vocabulary restrictions
    - Natural conversation
    - No effect on learning progress
    - Separate conversation context from Study Mode
    """
    
    def __init__(self, provider: AIProvider):
        """
        Initialize Free Chat service.
        
        Args:
            provider: AI provider (Ollama or OpenAI).
        """
        self.provider = provider
        self._conversation_history: list[AIMessage] = []
        self._vocab_context: list[str] = []
        self._vocab_cached_at: datetime | None = None
        self._session_vocab_active: bool = False  # set on first message with session_vocab; persists for conversation lifetime
        self._session_vocab_list: list = []  # stored on first message; used for rotation nudge on subsequent turns
        self._session_vocab_used: set = set()  # tracks which session vocab words have appeared in conversation
        self._theme_tracker = {
            "user_messages": 0,
            "used_words": set(),
            "first_try_correct": set(),
            "corrected_words": set(),
            "checkpoint_eligible": False,
            "checkpoint_active": False,
            "checkpoint_done": False,
            "checkpoint_questions": [],
            "checkpoint_index": 0,
            "checkpoint_score": 0,
            "checkpoint_offered": False,
            "theme_vocab_total": 0,
            "current_theme": None,
            "checkpoint_theme_vocab": set(),
        }
    
    def _ensure_vocab_context(self, db: Session) -> None:
        now = utc_now()

        if (
            not self._vocab_context
            or self._vocab_cached_at is None
            or (now - self._vocab_cached_at).total_seconds() > SESSION_VOCAB_TTL
        ):
            self._vocab_context = fetch_vocab_bias(db)
            self._vocab_cached_at = now

    async def respond(
        self,
        db: Session,
        user_message: str,
        scenario: Optional[str] = "free",
        theme: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        corrections_enabled: bool = False,
        session_vocab: Optional[list] = None,
    ) -> FreeChatResponse:
        """
        Generate a response in Free Chat mode.
        
        Args:
            user_message: User's input.
            scenario: Conversation scenario (free, restaurant, hotel, etc.).
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.
            
        Returns:
            FreeChatResponse with generated content.
        """
        correction_already_handled = False

        if theme != self._theme_tracker.get("current_theme"):
            self._theme_tracker = {
                "user_messages": 0,
                "used_words": set(),
                "first_try_correct": set(),
                "corrected_words": set(),
                "checkpoint_eligible": False,
                "checkpoint_active": False,
                "checkpoint_done": False,
                "checkpoint_questions": [],
                "checkpoint_index": 0,
                "checkpoint_score": 0,
                "checkpoint_offered": False,
                "theme_vocab_total": 0,
                "current_theme": theme,
                "checkpoint_theme_vocab": set(),
            }

        self._ensure_vocab_context(db)

        self._theme_tracker["user_messages"] += 1

        # Remember if this conversation was started with session vocab — persists for all subsequent messages
        if session_vocab:
            self._session_vocab_active = True
            self._session_vocab_list = session_vocab
            # Reset any in-progress checkpoint state from a previous conversation
            self._theme_tracker["checkpoint_active"] = False
            self._theme_tracker["checkpoint_eligible"] = False
            self._theme_tracker["checkpoint_offered"] = False
            self._theme_tracker["checkpoint_done"] = False
            self._theme_tracker["checkpoint_index"] = 0
            self._theme_tracker["checkpoint_score"] = 0
            self._theme_tracker["checkpoint_questions"] = []

        # Checkpoint logic is theme-based — skip entirely when session_vocab is active
        if not self._session_vocab_active:
            theme_vocab = set(self._vocab_context or [])
            self._theme_tracker["theme_vocab_total"] = len(theme_vocab)
            normalized_input = user_message.lower()
            for word in theme_vocab:
                if word.lower() in normalized_input:
                    self._theme_tracker["used_words"].add(word)

            yes_words = {"yes", "ok", "start", "sure"}
            if (
                self._theme_tracker["checkpoint_eligible"]
                and not self._theme_tracker["checkpoint_active"]
                and user_message.lower().strip() in yes_words
            ):
                self._theme_tracker["checkpoint_active"] = True
                self._theme_tracker["checkpoint_eligible"] = False
                self._theme_tracker["checkpoint_offered"] = False
                self._theme_tracker["checkpoint_questions"] = build_checkpoint_questions(theme_vocab)
                self._theme_tracker["checkpoint_index"] = 0
                self._theme_tracker["checkpoint_score"] = 0
                self._theme_tracker["checkpoint_theme_vocab"] = set(theme_vocab)
                if not self._theme_tracker["checkpoint_questions"]:
                    self._theme_tracker["checkpoint_active"] = False
                    self._theme_tracker["checkpoint_done"] = True
                    return FreeChatResponse(
                        content="Checkpoint skipped: no theme vocabulary available right now. Let's continue practicing.",
                        provider=_provider_label(self.provider),
                        model=_model_label(self.provider),
                        progress=self._build_progress_payload(),
                    )
                question = next_checkpoint_question(self)
                return FreeChatResponse(
                    content=question,
                    provider=_provider_label(self.provider),
                    model=_model_label(self.provider),
                    progress=self._build_progress_payload(),
                )

            if self._theme_tracker["checkpoint_active"]:
                idx = self._theme_tracker["checkpoint_index"]
                questions = self._theme_tracker["checkpoint_questions"]
                if idx >= len(questions):
                    self._theme_tracker["checkpoint_active"] = False
                    self._theme_tracker["checkpoint_done"] = True
                    return FreeChatResponse(
                        content="Checkpoint complete!\nScore: 0/5\n\nGreat work. Let's continue practicing.",
                        provider=_provider_label(self.provider),
                        model=_model_label(self.provider),
                        progress=self._build_progress_payload(),
                    )

                expected_word = questions[idx]
                is_correct = await self._validate_checkpoint_answer(user_message, expected_word)
                if is_correct:
                    self._theme_tracker["checkpoint_score"] += 1

                self._theme_tracker["checkpoint_index"] += 1

                if self._theme_tracker["checkpoint_index"] >= CHECKPOINT_SIZE:
                    score = self._theme_tracker["checkpoint_score"]
                    self._theme_tracker["checkpoint_active"] = False
                    self._theme_tracker["checkpoint_done"] = True
                    return FreeChatResponse(
                        content=(
                            f"Checkpoint complete!\n"
                            f"Score: {score}/{CHECKPOINT_SIZE}\n\n"
                            "Great work. Let's continue practicing."
                        ),
                        provider=_provider_label(self.provider),
                        model=_model_label(self.provider),
                        progress=self._build_progress_payload(),
                    )

                return FreeChatResponse(
                    content=next_checkpoint_question(self),
                    provider=_provider_label(self.provider),
                    model=_model_label(self.provider),
                    progress=self._build_progress_payload(),
                )

        # When session_vocab is active, use it as the sole vocab source.
        # When absent, fall back to _vocab_context (theme/general bias).
        vocab_bias = None if self._session_vocab_active else self._vocab_context

        # Save raw user message before any tip injection — grammar correction must use the original text.
        raw_user_message = user_message

        # Rotation nudge: every 2nd turn when session vocab is active, hint at the next word in the list
        if self._session_vocab_active and self._session_vocab_list:
            turn = self._theme_tracker["user_messages"]
            if turn % 2 == 0:
                hint_word = self._session_vocab_list[turn % len(self._session_vocab_list)]["word"]
                user_message = user_message + f"\n\n[Practice tip: if it fits naturally, try using '{hint_word}']"

        # Get system prompt
        base_prompt = get_free_chat_system_prompt()
        system_prompt = build_system_prompt(
            base_prompt,
            scenario=scenario,
            vocabulary_bias=vocab_bias,
            correction_mode=corrections_enabled,
            mode="practice",
            session_vocab=session_vocab or [],
        )
        
        estimated = estimate_token_count(system_prompt)
        if estimated > MAX_SYSTEM_PROMPT_TOKENS:
            logger.warning(
                "FreeChatService.respond: system prompt ~%d tokens exceeds limit of %d",
                estimated, MAX_SYSTEM_PROMPT_TOKENS,
            )

        # Create messages with trimmed history
        trimmed_history = _trim_history(self._conversation_history)
        messages = self.provider.create_messages(
            system_prompt=system_prompt,
            user_message=user_message,
            conversation_history=trimmed_history,
        )
        
        # Generate response
        try:
            ai_response = await self.provider.generate(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.error(f"Free Chat AI generation failed: {e}")
            raise

        correction_mode = corrections_enabled
        response_text = ai_response.content
        corrections = []

        # TWO-LAYER GRAMMAR ENFORCEMENT (SURFACE + STRUCTURAL)
        if (
            correction_mode
            and not correction_already_handled
            and not self._theme_tracker.get("checkpoint_active", False)
        ):
            original = raw_user_message  # use unmodified input — not the tip-appended version
            grammar_result = await self._grammar_correct(original)
            if isinstance(grammar_result, tuple):
                final_corrected, correction_explanation = grammar_result
            else:
                final_corrected = grammar_result
                correction_explanation = ""

            if (
                final_corrected
                and self._normalize_text(final_corrected)
                != self._normalize_text(original)
                and len(final_corrected) >= len(original) * 0.7
                and not re.search(r'[a-ząćęłńóśźż][A-ZĄĆĘŁŃÓŚŹŻ]', final_corrected)
            ):
                corrections = _build_corrections(original, final_corrected, correction_explanation)
                response_text = f"{final_corrected}\n\n{response_text}"
                correction_already_handled = True

        if not self._session_vocab_active and not self._theme_tracker["checkpoint_done"]:
            total = self._theme_tracker.get("theme_vocab_total", 0)
            used = len(self._theme_tracker["used_words"])
            msg_count = self._theme_tracker["user_messages"]

            if total > 0 and (
                msg_count >= CHECKPOINT_MESSAGE_THRESHOLD
                or (used / total) >= CHECKPOINT_USAGE_THRESHOLD
            ):
                self._theme_tracker["checkpoint_eligible"] = True

        if (
            not self._session_vocab_active
            and self._theme_tracker["checkpoint_eligible"]
            and not self._theme_tracker["checkpoint_active"]
            and not self._theme_tracker["checkpoint_offered"]
        ):
            response_text += (
                "\n\nYou’ve practiced a good portion of this theme.\n"
                "We are ready for a short checkpoint (5 questions).\n"
                "Would you like to start now?"
            )
            self._theme_tracker["checkpoint_offered"] = True

        # Update conversation history
        self._conversation_history.append(
            AIMessage(role=AIRole.USER, content=user_message)
        )
        self._conversation_history.append(
            AIMessage(role=AIRole.ASSISTANT, content=response_text)
        )
        
        # Limit history to last MAX_HISTORY_TURNS exchanges
        max_messages = MAX_HISTORY_TURNS * 2
        if len(self._conversation_history) > max_messages:
            self._conversation_history = self._conversation_history[-max_messages:]

        # Track session vocab usage — scan both user message and AI reply
        if self._session_vocab_active and self._session_vocab_list:
            clean_user = user_message.split("[Practice tip:")[0].lower()
            for item in self._session_vocab_list:
                word = item.get("word", "").lower()
                if word and word in clean_user:
                    self._session_vocab_used.add(word)

        return FreeChatResponse(
            content=response_text,
            provider=ai_response.provider,
            model=ai_response.model,
            tokens_used=ai_response.tokens_used,
            corrections=corrections if corrections_enabled else None,
            progress=self._build_progress_payload(),
        )
    
    def clear_history(self) -> None:
        """Clear conversation history."""
        self._conversation_history = []
        self._theme_tracker = {
            "user_messages": 0,
            "used_words": set(),
            "first_try_correct": set(),
            "corrected_words": set(),
            "checkpoint_eligible": False,
            "checkpoint_active": False,
            "checkpoint_done": False,
            "checkpoint_questions": [],
            "checkpoint_index": 0,
            "checkpoint_score": 0,
            "checkpoint_offered": False,
            "theme_vocab_total": 0,
            "current_theme": None,
            "checkpoint_theme_vocab": set(),
        }
    
    def get_history(self) -> list[dict]:
        """
        Get conversation history as list of dicts.
        
        Returns:
            List of message dicts with role and content.
        """
        return [
            {"role": msg.role.value, "content": msg.content}
            for msg in self._conversation_history
        ]
    
    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> FreeChatResponse:
        """
        Translate text between languages.
        
        Args:
            text: Text to translate.
            source_language: Source language.
            target_language: Target language.
            
        Returns:
            FreeChatResponse with translation.
        """
        prompt = (
            f"Translate the following from {source_language} to {target_language}. "
            f"Only provide the translation, no explanation:\n\n{text}"
        )
        
        # Don't add to conversation history for utility functions
        base_prompt = get_free_chat_system_prompt()
        system_prompt = build_system_prompt(base_prompt, mode="practice")
        
        estimated = estimate_token_count(system_prompt)
        if estimated > MAX_SYSTEM_PROMPT_TOKENS:
            logger.warning(
                "FreeChatService.translate: system prompt ~%d tokens exceeds limit of %d",
                estimated, MAX_SYSTEM_PROMPT_TOKENS,
            )
        
        messages = [
            AIMessage(role=AIRole.SYSTEM, content=system_prompt),
            AIMessage(role=AIRole.USER, content=prompt),
        ]
        
        ai_response = await self.provider.generate(
            messages=messages,
            temperature=0.3,  # Lower for accuracy
        )
        
        return FreeChatResponse(
            content=ai_response.content,
            provider=ai_response.provider,
            model=ai_response.model,
            tokens_used=ai_response.tokens_used,
        )
    
    async def explain_grammar(self, db: Session, topic: str) -> FreeChatResponse:
        """
        Explain a grammar topic.
        
        Args:
            db: Database session for vocab bias lookup.
            topic: Grammar topic to explain.
            
        Returns:
            FreeChatResponse with explanation.
        """
        prompt = (
            f"Explain this grammar topic in simple terms with examples: {topic}"
        )
        
        return await self.respond(
            db=db,
            user_message=prompt,
            temperature=0.5,
        )

    async def validate_retry(
        self,
        db: Session,
        request: RetryValidationRequest,
    ) -> RetryValidationResponse:
        """
        Validate whether the user's retry attempt correctly fixes
        the original incorrect sentence.
        This method is stateless and does NOT use conversation history.
        """

        system_prompt = """
You are a Polish sentence validator helping a language learner.

Your task: determine whether the user's attempt is a grammatically correct Polish sentence
that preserves the meaning of the original (incorrect) sentence.

ACCEPT if:
- The attempt is grammatically correct Polish
- AND preserves the core meaning of the original sentence
- The attempt does NOT need to match the reference correction word-for-word

REJECT if:
- The attempt still has grammar or spelling errors
- OR the attempt significantly changes the meaning

Allowed:
- Different but equally valid word choice
- Different word order (Polish is flexible)
- Omission of optional subject pronouns (e.g. 'Ja')

Return JSON only:
{"is_correct": true | false}
"""

        user_prompt = f"""
ORIGINAL SENTENCE (incorrect):
{request.original_sentence}

USER ATTEMPT:
{request.user_attempt}
"""

        messages = self.provider.create_messages(
            system_prompt=system_prompt,
            user_message=user_prompt,
            conversation_history=[],
        )

        try:
            ai_response = await self.provider.generate(
                messages=messages,
                temperature=0.0,
                max_tokens=200,
            )
        except Exception as e:
            raise e

        raw = ai_response.content.strip()

        # Remove markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            if len(lines) > 2:
                raw = "\n".join(lines[1:-1]).strip()

        # Extract JSON object if extra text exists
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end+1]

        is_correct = False
        feedback = None
        try:
            data = json.loads(raw)
            is_correct = data.get("is_correct", False)
            feedback = data.get("feedback")
        except Exception:
            is_correct = False
            feedback = "Validation parsing failed."

        record_event(
            db=db,
            event_type="retry_attempt",
            theme=request.theme,
            payload={
                "attempt_number": request.attempt_number,
                "is_correct": is_correct,
            },
        )

        if is_correct:
            record_event(
                db=db,
                event_type="retry_resolved",
                theme=request.theme,
                payload={
                    "attempt_number": request.attempt_number,
                },
            )

        return RetryValidationResponse(
            is_correct=is_correct,
            feedback=feedback,
        )

    def record_retry_revealed(
        self,
        db: Session,
        theme: Optional[str],
        attempt_number: Optional[int],
    ) -> None:
        record_event(
            db=db,
            event_type="retry_revealed",
            theme=theme,
            payload={
                "attempt_number": attempt_number,
            },
        )

    def _build_progress_payload(self) -> dict:
        return {
            "theme_used": len(self._theme_tracker["used_words"]),
            "theme_total": self._theme_tracker.get("theme_vocab_total", 0),
            "user_messages": self._theme_tracker["user_messages"],
            "checkpoint_done": self._theme_tracker["checkpoint_done"],
            "checkpoint_active": self._theme_tracker["checkpoint_active"],
            "checkpoint_score": self._theme_tracker["checkpoint_score"],
            "checkpoint_index": self._theme_tracker["checkpoint_index"],
            "session_vocab_used": len(self._session_vocab_used),
            "session_vocab_total": len(self._session_vocab_list),
            "session_vocab_used_words": list(self._session_vocab_used),
        }

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""
        return " ".join(text.strip().lower().split())

    def _needs_structural_repair(self, sentence: str) -> bool:
        tokens = sentence.strip().split()
        if len(tokens) <= 2:
            return True

        # Heuristic: starts with common verb-like token without explicit subject signal.
        first = tokens[0].lower()
        common_verbs = ["jest", "lubi", "chce", "mam", "robi"]
        sentence_lower = sentence.lower()
        if first in common_verbs and not any(
            pron in sentence_lower for pron in ["ja ", "jestem", "mam ", "lubię", "chcę"]
        ):
            return True

        return False

    async def _grammar_correct(self, sentence: str) -> tuple[str, str]:
        """
        Deterministic grammar correction via JSON contract.
        Returns (corrected_sentence, explanation).
        Returns (original, "") on any failure or when no error detected.
        """
        try:
            system_prompt = (
                "You are a strict Polish grammar corrector.\n"
                "Return JSON only in this exact format:\n"
                '{"has_errors": true/false, "corrected": "...", "explanation": "..."}\n\n'
                "CORRECT sentences — you MUST return has_errors: false:\n"
                "- Grammatically correct sentences, even if simple (e.g. 'Ja mam kilka hobby', 'mam hobby', 'idę do domu').\n"
                "- Sentences with uppercase first word ('Ja', 'Dom') — capitalization is NOT an error.\n"
                "- Sentences missing a period or question mark at the end — trailing punctuation is NOT an error.\n"
                "- NEVER invent errors. If nothing is wrong, return has_errors: false.\n"
                "- If has_errors is false, corrected MUST equal the original input exactly.\n\n"
                "ERRORS — set has_errors: true ONLY for:\n"
                "- Spelling mistakes (e.g. 'kilke' → 'kilka').\n"
                "- Missing or wrong diacritics that change meaning (e.g. 'duzy' → 'duży').\n"
                "- Incorrect word forms or wrong case endings.\n"
                "- Structural errors — ALWAYS trigger for these:\n"
                "  • double negation misuse: sentence contains both 'nie' and 'nigdy' used incorrectly (e.g. 'nie nigdy nie byłem' → 'nigdy nie byłem')\n"
                "  • invalid word combinations that a native speaker would never use\n"
                "- Fix ALL errors in one pass — do not fix only one if multiple exist.\n"
                "- Do NOT paraphrase, shorten, or rephrase.\n"
                "- Do NOT change meaning, names, or word order.\n"
                "- corrected must fix everything that is wrong — not just one error.\n"
                "- explanation: JSON array of short notes, one per error (max 8 words each). "
                "Each explanation MUST start with one of: Spelling / Case / Verb / Gender / Tense / Preposition / Structure. "
                "Each note MUST show the full correct form, not just the changed word — e.g. 'na żywo' not 'żywo', not 'żywo' → 'żywo'. "
                "Never compare identical strings. "
                "For structural errors, briefly state the rule (e.g. 'Structure: use nie OR nigdy, not both'). "
                "Examples: [\"Spelling: 'kilke' → 'kilka'\", \"Case: 'nowogo' → 'nowego'\", "
                "\"Verb: 'idzie' → 'idziesz'\", \"Gender: 'dobry' → 'dobra'\", "
                "\"Preposition: 'na żywo' not 'żywo'\", \"Structure: use 'nie' OR 'nigdy', not both\"]. "
                "If has_errors is false, set explanation to [].\n"
                "- Output JSON only."
            )
            messages = self.provider.create_messages(
                system_prompt=system_prompt,
                user_message=sentence,
                conversation_history=[],
            )
            response = await self.provider.generate(
                messages=messages,
                temperature=0.0,
                max_tokens=200,
            )
            raw = (response.content or "").strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            result = json.loads(raw)
            if result.get("has_errors") and result.get("corrected"):
                corrected = result["corrected"].strip()
                # Reject hallucination artifacts:
                # 1. Dramatically shorter than original (paraphrase/truncation)
                if len(corrected) < len(sentence) * 0.7:
                    return sentence, ""
                # 2. Contains lowercase-then-uppercase mid-token (e.g. "cCześć") — model artifact
                if re.search(r'[a-ząćęłńóśźż][A-ZĄĆĘŁŃÓŚŹŻ]', corrected):
                    return sentence, ""
                raw_explanation = result.get("explanation") or []
                if isinstance(raw_explanation, list):
                    explanation = "\n".join(s.strip() for s in raw_explanation if s)
                else:
                    explanation = str(raw_explanation).strip()
                return corrected, explanation
            return sentence, ""
        except Exception:
            return sentence, ""

    async def _structural_repair(self, sentence: str) -> str:
        try:
            system_prompt = (
                "You are a Polish grammar repair assistant.\n"
                "Rewrite the sentence to make it grammatically complete.\n"
                "Preserve original meaning.\n"
                "Do NOT change names.\n"
                "Do NOT invent new ideas.\n"
                "Only add minimal grammar necessary.\n"
                "Return ONLY the corrected sentence."
            )
            messages = self.provider.create_messages(
                system_prompt=system_prompt,
                user_message=sentence,
                conversation_history=[],
            )
            response = await self.provider.generate(
                messages=messages,
                temperature=0.0,
                max_tokens=100,
            )
            corrected = (response.content or "").strip()
            if not corrected:
                return sentence
            return corrected
        except Exception:
            return sentence

    async def _validate_checkpoint_answer(self, user_sentence: str, target_word: str) -> bool:
        system_prompt = """
You are a Polish sentence validator for a vocabulary checkpoint.
Decide if the user used the target Polish word in a valid Polish sentence.
Allow minor word order variation.
Return JSON only:
{"is_correct": true | false}
"""
        user_prompt = (
            f"TARGET WORD:\n{target_word}\n\n"
            f"USER SENTENCE:\n{user_sentence}\n"
        )
        messages = self.provider.create_messages(
            system_prompt=system_prompt,
            user_message=user_prompt,
            conversation_history=[],
        )
        try:
            ai_response = await self.provider.generate(
                messages=messages,
                temperature=0.0,
                max_tokens=80,
            )
            raw = ai_response.content.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                if len(lines) > 2:
                    raw = "\n".join(lines[1:-1]).strip()
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]
            data = json.loads(raw)
            return bool(data.get("is_correct", False))
        except Exception:
            return target_word.lower() in user_sentence.lower()

def build_checkpoint_questions(theme_vocab):
    vocab_list = list(theme_vocab)
    return vocab_list[:CHECKPOINT_SIZE]


def next_checkpoint_question(service: FreeChatService):
    idx = service._theme_tracker["checkpoint_index"]
    questions = service._theme_tracker["checkpoint_questions"]
    if idx >= len(questions):
        return "Checkpoint complete."
    word = questions[idx]
    return f"Checkpoint Question {idx + 1}/5:\nUse the word '{word}' in a correct Polish sentence."
