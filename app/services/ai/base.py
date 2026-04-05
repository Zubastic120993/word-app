"""Base classes and types for AI providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AIRole(str, Enum):
    """Role in conversation."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class AIMessage:
    """A single message in a conversation."""
    role: AIRole
    content: str


@dataclass
class AIResponse:
    """Response from an AI provider."""
    content: str
    model: str
    provider: str
    tokens_used: Optional[int] = None
    finish_reason: Optional[str] = None
    raw_response: Optional[dict] = None


@dataclass
class AIStatus:
    """Status of an AI provider."""
    available: bool
    provider: str
    model: Optional[str] = None
    error: Optional[str] = None
    details: dict = field(default_factory=dict)


class AIProvider(ABC):
    """
    Abstract base class for AI providers.
    
    Implementations must provide:
    - generate(): Generate a response from messages
    - check_health(): Check if the provider is available
    """
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the provider (e.g., 'ollama', 'openai')."""
        pass
    
    @abstractmethod
    async def generate(
        self,
        messages: list[AIMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AIResponse:
        """
        Generate a response from the AI model.
        
        Args:
            messages: Conversation history.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in response.
            
        Returns:
            AIResponse with generated content.
        """
        pass
    
    @abstractmethod
    async def check_health(self) -> AIStatus:
        """
        Check if the AI provider is available and healthy.
        
        Returns:
            AIStatus indicating availability.
        """
        pass
    
    def create_messages(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[list[AIMessage]] = None,
    ) -> list[AIMessage]:
        """
        Create a message list for generation.
        
        Args:
            system_prompt: System instructions.
            user_message: Current user input.
            conversation_history: Optional previous messages.
            
        Returns:
            List of AIMessage objects.
        """
        messages = [AIMessage(role=AIRole.SYSTEM, content=system_prompt)]
        
        if conversation_history:
            messages.extend(conversation_history)
        
        messages.append(AIMessage(role=AIRole.USER, content=user_message))
        
        return messages
