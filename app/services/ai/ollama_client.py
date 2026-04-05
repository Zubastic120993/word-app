"""Ollama AI client for local-first AI integration."""

import logging
from typing import Optional

import httpx

from app.config import settings
from app.services.ai.base import (
    AIProvider,
    AIMessage,
    AIResponse,
    AIStatus,
    AIRole,
)

logger = logging.getLogger(__name__)


class OllamaClient(AIProvider):
    """
    Client for Ollama local AI server.
    
    Ollama provides local LLM inference at localhost:11434.
    This is the primary (default) AI provider for the app.
    """
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        """
        Initialize Ollama client.
        
        Args:
            base_url: Ollama server URL (default from settings).
            model: Model name to use (default from settings).
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url or settings.ollama_base_url
        self.model = model or settings.ollama_model
        self.timeout = timeout or settings.ollama_timeout
    
    @property
    def provider_name(self) -> str:
        return "ollama"
    
    async def generate(
        self,
        messages: list[AIMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AIResponse:
        """
        Generate a response using Ollama.
        
        Args:
            messages: Conversation history.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens (num_predict in Ollama).
            
        Returns:
            AIResponse with generated content.
            
        Raises:
            httpx.HTTPError: If request fails.
        """
        # Convert messages to Ollama format
        ollama_messages = [
            {"role": msg.role.value, "content": msg.content}
            for msg in messages
        ]
        
        # Build request payload
        payload = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }
        
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        
        # Extract response
        message = data.get("message", {})
        content = message.get("content", "")
        
        return AIResponse(
            content=content,
            model=self.model,
            provider=self.provider_name,
            tokens_used=data.get("eval_count"),
            finish_reason=data.get("done_reason"),
            raw_response=data,
        )
    
    async def check_health(self) -> AIStatus:
        """
        Check if Ollama is running and the model is available.
        
        Returns:
            AIStatus with availability info.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Check if Ollama is running
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
                
                # Get available models
                models = data.get("models", [])
                model_names = [m.get("name", "").split(":")[0] for m in models]
                
                # Check if our model is available
                model_available = any(
                    self.model.split(":")[0] in name 
                    for name in model_names
                )
                
                return AIStatus(
                    available=True,
                    provider=self.provider_name,
                    model=self.model if model_available else None,
                    error=None if model_available else f"Model '{self.model}' not found",
                    details={
                        "base_url": self.base_url,
                        "available_models": model_names,
                        "model_available": model_available,
                    },
                )
                
        except httpx.ConnectError:
            return AIStatus(
                available=False,
                provider=self.provider_name,
                error="Cannot connect to Ollama. Is it running?",
                details={"base_url": self.base_url},
            )
        except Exception as e:
            logger.error(f"Ollama health check failed: {e}")
            return AIStatus(
                available=False,
                provider=self.provider_name,
                error=str(e),
                details={"base_url": self.base_url},
            )
    
    async def pull_model(self, model: Optional[str] = None) -> bool:
        """
        Pull a model from Ollama registry.
        
        Args:
            model: Model name to pull (default: configured model).
            
        Returns:
            True if successful.
        """
        model = model or self.model
        
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(
                    f"{self.base_url}/api/pull",
                    json={"name": model, "stream": False},
                )
                response.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Failed to pull model {model}: {e}")
            return False
