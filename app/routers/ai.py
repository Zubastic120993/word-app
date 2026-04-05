"""API router for AI endpoints (Study Mode + Free Chat)."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import require_mac_role
from app.database import get_db
from app.schemas.ai import (
    StudyModeRequest,
    StudyModeResponse,
    FreeChatRequest,
    FreeChatResponse,
    TranslateRequest,
    AIStatusResponse,
    ClearHistoryResponse,
    RetryValidationRequest,
    RetryValidationResponse,
)
from app.services.ai.ollama_client import OllamaClient
from app.services.ai.openai_client import OpenAIClient
from app.services.ai.study_mode import StudyModeService
from app.services.ai.free_chat import FreeChatService
from app.services.ai.base import AIProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

_free_chat_service: Optional[FreeChatService] = None


def get_ai_provider() -> AIProvider:
    """
    Get the configured AI provider.
    
    Priority:
    1. OpenAI if explicitly enabled
    2. Ollama (default, local-first)
    """
    if settings.openai_enabled and settings.openai_api_key:
        return OpenAIClient()
    return OllamaClient()


def get_study_mode_service(db: Session = Depends(get_db)) -> StudyModeService:
    """Get or create Study Mode service."""
    provider = get_ai_provider()
    return StudyModeService(provider=provider, db=db)


def get_free_chat_service() -> FreeChatService:
    """Get or create Free Chat service."""
    global _free_chat_service
    
    if _free_chat_service is None:
        provider = get_ai_provider()
        _free_chat_service = FreeChatService(provider=provider)
    
    return _free_chat_service


@router.get("/status", response_model=AIStatusResponse)
async def get_ai_status() -> AIStatusResponse:
    """
    Check AI provider availability.
    
    Returns status of the configured AI provider (Ollama or OpenAI).
    """
    provider = get_ai_provider()
    status = await provider.check_health()
    
    return AIStatusResponse(
        available=status.available,
        provider=status.provider,
        model=status.model,
        error=status.error,
        details=status.details,
    )


@router.post("/study/respond", response_model=StudyModeResponse)
async def study_mode_respond(
    request: StudyModeRequest,
    db: Session = Depends(get_db),
) -> StudyModeResponse:
    """
    Generate a response in Study Mode.
    
    Study Mode is STRICT:
    - AI may only use vocabulary from the current session + learned units
    - Response is validated against allowed vocabulary
    - Violations are flagged but response is still returned
    
    Args:
        request: Study mode request with message and session context.
        
    Returns:
        AI response with validation results.
    """
    service = get_study_mode_service(db)
    
    try:
        response = await service.respond(
            user_message=request.message,
            session_id=request.session_id,
            include_learned=request.include_learned,
            validate_output=request.validate_output,
        )
        
        return StudyModeResponse(
            content=response.content,
            user_correct=response.user_correct,
            ai_vocab_valid=response.ai_vocab_valid,
            unknown_words=response.validation.unknown_words,
            warning=response.warning,
            session_id=response.session_id,
            provider=response.provider,
            model=response.model,
        )
        
    except Exception as e:
        logger.error(f"Study Mode error: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"AI service unavailable: {str(e)}",
        )


@router.post("/chat/respond", response_model=FreeChatResponse, dependencies=[Depends(require_mac_role)])
async def free_chat_respond(
    request: FreeChatRequest,
    db: Session = Depends(get_db),
) -> FreeChatResponse:
    """
    Generate a response in Free Chat mode.
    
    Free Chat is UNRESTRICTED:
    - No vocabulary limitations
    - Natural conversation
    - No effect on learning progress
    - Maintains separate conversation history from Study Mode
    
    Args:
        request: Free chat request with message.
        db: Database session for vocab bias lookup.
        
    Returns:
        AI response.
    """
    service = get_free_chat_service()
    
    try:
        response = await service.respond(
            db=db,
            user_message=request.message,
            scenario=request.scenario,
            theme=request.theme,
            temperature=request.temperature,
            corrections_enabled=request.corrections_enabled,
            session_vocab=request.session_vocab,
        )
        
        return FreeChatResponse(
            content=response.content,
            provider=response.provider,
            model=response.model,
            tokens_used=response.tokens_used,
            corrections=response.corrections,
            progress=response.progress,
        )
        
    except Exception as e:
        logger.error(f"Free Chat error: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"AI service unavailable: {str(e)}",
        )


@router.post("/chat/validate-retry", response_model=RetryValidationResponse)
async def validate_retry(
    request: RetryValidationRequest,
    db: Session = Depends(get_db),
):
    """
    Validate retry attempt using AI semantic evaluation.
    Stateless endpoint.
    """
    service = get_free_chat_service()

    try:
        result = await service.validate_retry(db=db, request=request)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Retry validation failed: {str(e)}"
        )


class HintRequest(BaseModel):
    original: str
    correct: str
    user_attempt: str


class RetryRevealRequest(BaseModel):
    attempt_number: Optional[int] = None
    theme: Optional[str] = None


@router.post("/chat/retry-revealed")
async def retry_revealed(
    request: RetryRevealRequest,
    db: Session = Depends(get_db),
):
    """Record analytics event for retry reveal after max attempts."""
    service = get_free_chat_service()
    service.record_retry_revealed(
        db=db,
        theme=request.theme,
        attempt_number=request.attempt_number,
    )
    return {"status": "ok"}


@router.post("/chat/hint")
async def get_hint(request: HintRequest):
    """Provide AI-generated contextual hint for retry."""
    from app.services.ai.free_chat import generate_hint

    hint = await generate_hint(
        original=request.original,
        correct=request.correct,
        user_attempt=request.user_attempt,
    )

    return {"hint": hint}


@router.post("/chat/translate", response_model=FreeChatResponse, dependencies=[Depends(require_mac_role)])
async def translate_text(request: TranslateRequest) -> FreeChatResponse:
    """
    Translate text between languages.
    
    This is a utility endpoint that doesn't affect conversation history.
    
    Args:
        request: Translation request.
        
    Returns:
        Translation result.
    """
    service = get_free_chat_service()
    
    try:
        response = await service.translate(
            text=request.text,
            source_language=request.source_language,
            target_language=request.target_language,
        )
        
        return FreeChatResponse(
            content=response.content,
            provider=response.provider,
            model=response.model,
            tokens_used=response.tokens_used,
        )
        
    except Exception as e:
        logger.error(f"Translation error: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"AI service unavailable: {str(e)}",
        )


@router.post("/study/clear", response_model=ClearHistoryResponse)
async def clear_study_history(
    db: Session = Depends(get_db),
) -> ClearHistoryResponse:
    """
    Clear Study Mode conversation history.
    
    Resets the conversation context for Study Mode.
    """
    service = get_study_mode_service(db)
    service.clear_history()
    
    return ClearHistoryResponse(
        message="Study Mode conversation history cleared.",
        mode="study",
    )


@router.post("/chat/clear", response_model=ClearHistoryResponse, dependencies=[Depends(require_mac_role)])
async def clear_chat_history() -> ClearHistoryResponse:
    """
    Clear Free Chat conversation history.
    
    Resets the conversation context for Free Chat.
    """
    global _free_chat_service
    
    if _free_chat_service:
        _free_chat_service.clear_history()
    
    return ClearHistoryResponse(
        message="Free Chat conversation history cleared.",
        mode="chat",
    )
