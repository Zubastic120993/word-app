"""OpenAI client for optional cloud AI integration."""

import logging
from typing import Optional

import httpx

from app.config import settings
from app.services.ai.base import (
    AIProvider,
    AIMessage,
    AIResponse,
    AIStatus,
)

logger = logging.getLogger(__name__)


class OpenAIClient(AIProvider):
    """
    Client for OpenAI API.
    
    This is an OPTIONAL secondary provider, disabled by default.
    Only used when explicitly enabled and API key is provided.
    """
    
    BASE_URL = "https://api.openai.com/v1"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """
        Initialize OpenAI client.
        
        Args:
            api_key: OpenAI API key (default from settings).
            model: Model name to use (default from settings).
        """
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model
        self._enabled = settings.openai_enabled and bool(self.api_key)
    
    @property
    def provider_name(self) -> str:
        return "openai"
    
    @property
    def is_enabled(self) -> bool:
        """Check if OpenAI is enabled and configured."""
        return self._enabled
    
    async def generate(
        self,
        messages: list[AIMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AIResponse:
        """
        Generate a response using OpenAI.
        
        Args:
            messages: Conversation history.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.
            
        Returns:
            AIResponse with generated content.
            
        Raises:
            RuntimeError: If OpenAI is not enabled.
            httpx.HTTPError: If request fails.
        """
        if not self.is_enabled:
            raise RuntimeError(
                "OpenAI is not enabled. Set WORD_APP_OPENAI_ENABLED=true "
                "and provide WORD_APP_OPENAI_API_KEY."
            )
        
        # Convert messages to OpenAI format
        openai_messages = [
            {"role": msg.role.value, "content": msg.content}
            for msg in messages
        ]
        
        # Build request payload
        payload = {
            "model": self.model,
            "messages": openai_messages,
            "temperature": temperature,
        }
        
        if max_tokens:
            payload["max_tokens"] = max_tokens
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        
        # Extract response
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        
        usage = data.get("usage", {})
        
        return AIResponse(
            content=content,
            model=self.model,
            provider=self.provider_name,
            tokens_used=usage.get("total_tokens"),
            finish_reason=choice.get("finish_reason"),
            raw_response=data,
        )
    
    async def check_health(self) -> AIStatus:
        """
        Check if OpenAI is enabled and accessible.
        
        Returns:
            AIStatus with availability info.
        """
        if not self.is_enabled:
            return AIStatus(
                available=False,
                provider=self.provider_name,
                error="OpenAI is disabled or API key not configured.",
                details={"enabled": False},
            )
        
        try:
            # Simple health check by listing models
            headers = {
                "Authorization": f"Bearer {self.api_key}",
            }
            
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.BASE_URL}/models",
                    headers=headers,
                )
                response.raise_for_status()
            
            return AIStatus(
                available=True,
                provider=self.provider_name,
                model=self.model,
                details={"enabled": True},
            )
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                error = "Invalid API key"
            else:
                error = f"HTTP {e.response.status_code}"
            return AIStatus(
                available=False,
                provider=self.provider_name,
                error=error,
                details={"enabled": True},
            )
        except Exception as e:
            logger.error(f"OpenAI health check failed: {e}")
            return AIStatus(
                available=False,
                provider=self.provider_name,
                error=str(e),
                details={"enabled": True},
            )
