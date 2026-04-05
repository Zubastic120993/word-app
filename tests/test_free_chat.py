"""Tests for Free Chat history ordering and correction payloads."""

import asyncio

from app.services.ai.base import AIMessage, AIResponse, AIRole, AIStatus
from app.services.ai.free_chat import (
    FreeChatService,
    MAX_HISTORY_TURNS,
    _build_corrections,
    _trim_history,
)


class StubProvider:
    name = "stub"
    model_name = "stub-model"

    @property
    def provider_name(self) -> str:
        return self.name

    def create_messages(self, system_prompt, user_message, conversation_history=None):
        messages = [AIMessage(role=AIRole.SYSTEM, content=system_prompt)]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append(AIMessage(role=AIRole.USER, content=user_message))
        return messages

    async def generate(self, messages, temperature=0.7, max_tokens=None):
        return AIResponse(
            content="Assistant reply",
            model=self.model_name,
            provider=self.name,
        )

    async def check_health(self):
        return AIStatus(available=True, provider=self.name, model=self.model_name)


def test_trim_history_preserves_true_chronological_order():
    history = []
    for index in range(MAX_HISTORY_TURNS + 2):
        history.append(AIMessage(role=AIRole.USER, content=f"user-{index}"))
        history.append(AIMessage(role=AIRole.ASSISTANT, content=f"assistant-{index}"))

    trimmed = _trim_history(history)

    expected = []
    for index in range(2, MAX_HISTORY_TURNS + 2):
        expected.extend(
            [
                ("user", f"user-{index}"),
                ("assistant", f"assistant-{index}"),
            ]
        )

    assert [(message.role.value, message.content) for message in trimmed] == expected


def test_build_corrections_returns_structured_sentence_level_payload():
    corrections = _build_corrections("Ja być zmeczony", "Jestem zmęczony")

    assert corrections == [
        {
            "original": "Ja być zmeczony",
            "corrected": "Jestem zmęczony",
            "explanation": "Grammar and phrasing adjusted while preserving your meaning.",
        }
    ]


def test_free_chat_respond_returns_structured_corrections(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    async def fake_grammar_correct(sentence):
        return "Jestem zmęczony"

    monkeypatch.setattr(service, "_grammar_correct", fake_grammar_correct)
    monkeypatch.setattr(service, "_needs_structural_repair", lambda sentence: False)

    response = asyncio.run(
        service.respond(
            db=None,
            user_message="Ja być zmeczony",
            corrections_enabled=True,
        )
    )

    assert response.corrections == [
        {
            "original": "Ja być zmeczony",
            "corrected": "Jestem zmęczony",
            "explanation": "Grammar and phrasing adjusted while preserving your meaning.",
        }
    ]
    assert "Jestem zmęczony" in response.content
