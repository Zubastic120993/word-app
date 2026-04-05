"""Tests for Free Chat history ordering and correction payloads."""

import asyncio
import re

from app.services.ai.base import AIMessage, AIResponse, AIRole, AIStatus
from app.services.ai.free_chat import (
    AVOID_REPEATING_LABEL,
    FOCUS_LABEL,
    PRACTICE_WORDS_LABEL,
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


def test_session_vocab_persists_across_turns(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    asyncio.run(
        service.respond(
            db=None,
            user_message="start",
            session_vocab=[{"word": "sklep"}],
        )
    )

    response = asyncio.run(
        service.respond(
            db=None,
            user_message="continue",
        )
    )

    assert response.progress["session_vocab_total"] == 1


def test_assistant_exposure_from_final_response(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    async def fake_generate(*args, **kwargs):
        return AIResponse(
            content="sklep",
            provider="stub",
            model="stub-model",
            tokens_used=5,
        )

    monkeypatch.setattr(service.provider, "generate", fake_generate)

    response = asyncio.run(
        service.respond(
            db=None,
            user_message="...",
            session_vocab=[{"word": "sklep"}],
        )
    )

    assert response.progress["assistant_exposed_count"] == 1
    assert response.progress["covered_count"] == 1


def test_user_production_is_tracked(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    async def fake_generate(*args, **kwargs):
        return AIResponse(
            content="Rozumiem.",
            provider="stub",
            model="stub-model",
            tokens_used=5,
        )

    monkeypatch.setattr(service.provider, "generate", fake_generate)

    response = asyncio.run(
        service.respond(
            db=None,
            user_message="Idę do sklep",
            session_vocab=[{"word": "sklep"}],
        )
    )

    assert response.progress["user_produced_count"] == 1
    assert response.progress["covered_count"] == 1


def test_clear_resets_all_state():
    service = FreeChatService(provider=StubProvider())

    service._conversation_history = [AIMessage(role=AIRole.USER, content="hello")]
    service._session_vocab_active = True
    service._session_vocab_list = [{"original": "sklep", "normalized": "sklep"}]
    service._assistant_exposed_words.add("sklep")
    service._user_produced_words.add("dom")
    service._recently_used_words.append("sklep")

    service.clear_history()

    assert service._conversation_history == []
    assert service._session_vocab_active is False
    assert service._session_vocab_list == []
    assert len(service._assistant_exposed_words) == 0
    assert len(service._user_produced_words) == 0
    assert len(service._recently_used_words) == 0


def test_phrase_matching(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    async def fake_generate(*args, **kwargs):
        return AIResponse(
            content="Idę do sklep spożywczy",
            provider="stub",
            model="stub-model",
            tokens_used=10,
        )

    monkeypatch.setattr(service.provider, "generate", fake_generate)

    response = asyncio.run(
        service.respond(
            db=None,
            user_message="...",
            session_vocab=[{"word": "sklep spożywczy"}],
        )
    )

    assert response.progress["assistant_exposed_count"] == 1


def test_focus_prefers_unused(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    captured = {}

    def fake_create_messages(system_prompt, user_message, conversation_history):
        captured["prompt"] = system_prompt
        return []

    monkeypatch.setattr(service.provider, "create_messages", fake_create_messages)

    async def fake_generate(*args, **kwargs):
        return AIResponse(
            content="dom",
            provider="stub",
            model="stub-model",
            tokens_used=5,
        )

    monkeypatch.setattr(service.provider, "generate", fake_generate)

    asyncio.run(
        service.respond(
            db=None,
            user_message="start",
            session_vocab=[{"word": "sklep"}, {"word": "dom"}],
        )
    )

    asyncio.run(
        service.respond(
            db=None,
            user_message="continue",
        )
    )

    prompt = captured["prompt"]

    assert PRACTICE_WORDS_LABEL in prompt
    assert FOCUS_LABEL in prompt
    assert AVOID_REPEATING_LABEL in prompt

    match = re.search(
        rf"{re.escape(FOCUS_LABEL)}\s*(.*?)(?:\n{re.escape(AVOID_REPEATING_LABEL)}|\Z)",
        prompt,
        re.DOTALL,
    )
    assert match, "Focus block missing from system prompt"

    focus_section = match.group(1).strip()

    assert "sklep" in focus_section
    assert "dom" not in focus_section


def test_session_vocab_converges(monkeypatch):
    service = FreeChatService(provider=StubProvider())

    monkeypatch.setattr(
        "app.services.ai.free_chat.fetch_vocab_bias",
        lambda db: [],
    )

    responses = [
        "To jest dom",
        "To jest sklep",
    ]
    captured_prompts = []

    def fake_create_messages(system_prompt, user_message, conversation_history):
        captured_prompts.append(system_prompt)
        return []

    async def fake_generate(*args, **kwargs):
        content = responses.pop(0) if responses else "OK"
        return AIResponse(
            content=content,
            provider="stub",
            model="stub-model",
            tokens_used=0,
        )

    monkeypatch.setattr(service.provider, "create_messages", fake_create_messages)
    monkeypatch.setattr(service.provider, "generate", fake_generate)

    session_vocab = [
        {"word": "dom"},
        {"word": "sklep"},
        {"word": "pies"},
    ]

    asyncio.run(
        service.respond(
            db=None,
            user_message="Start",
            session_vocab=session_vocab,
        )
    )
    asyncio.run(
        service.respond(
            db=None,
            user_message="Continue",
        )
    )
    response = asyncio.run(
        service.respond(
            db=None,
            user_message="pies",
        )
    )
    asyncio.run(
        service.respond(
            db=None,
            user_message="Next",
        )
    )

    progress = response.progress
    assert progress["covered_count"] == progress["session_vocab_total"]

    prompt = captured_prompts[-1]
    assert PRACTICE_WORDS_LABEL in prompt
    assert FOCUS_LABEL in prompt
    assert AVOID_REPEATING_LABEL in prompt

    match = re.search(
        rf"{re.escape(FOCUS_LABEL)}\s*(.*?)(?:\n{re.escape(AVOID_REPEATING_LABEL)}|\Z)",
        prompt,
        re.DOTALL,
    )
    assert match, "Focus block missing from system prompt"

    focus_section = match.group(1).strip()
    assert focus_section == "" or focus_section.lower() == "none"
