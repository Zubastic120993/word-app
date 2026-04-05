"""AI-backed example sentences and cloze prompt construction for cloze study mode."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import time
from typing import Optional

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.learning_unit import LearningUnit, UnitType
from app.services.ai.base import AIMessage, AIProvider, AIResponse, AIRole

logger = logging.getLogger(__name__)


def _is_phrase_unit(unit: LearningUnit) -> bool:
    text = (unit.text or "").strip()
    return len(text.split()) >= 5


def _word_matches_in_sentence(word: str, sentence: str) -> bool:
    word = word.lower().strip()
    sentence = sentence.lower()
    if len(word) < 4:
        return word in sentence
    return word[:4] in sentence


_BLANK = "________"
_STRIP_PUNCT_RE = re.compile(
    r"^[^\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+|[^\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+$"
)


def _tokenize(text: str) -> list[str]:
    """
    Split text into word and punctuation tokens preserving alignment.
    Each character belongs to exactly one token: word chars (including Polish
    letters) form word tokens; punctuation/space form single-char punctuation
    tokens. Alignment between sentence and cloze_prompt is guaranteed as long
    as make_cloze_prompt only replaces a word token in-place.
    """
    return re.findall(r"[^\W\d_]*[\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+|[^\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", text)


def _strip_punct(token: str) -> str:
    """Strip leading/trailing non-word characters (including Polish letters)."""
    return _STRIP_PUNCT_RE.sub("", token)


def get_cloze_answer(
    sentence: str,
    target: str,
    cloze_prompt: Optional[str] = None,
) -> Optional[str]:
    """
    Return the actual inflected word form that was blanked in the cloze sentence.

    Strategy 1 — positional via shared tokenizer (primary):
        Both sentence and cloze_prompt are tokenized with _tokenize(), which
        splits on word/punctuation boundaries identically for both strings.
        Token count is preserved because make_cloze_prompt replaces a word
        token in-place (never adds or removes tokens). The index where
        cloze_prompt has '________' maps directly to the original sentence token.
        Punctuation is stripped only after the match is found.

    Strategy 2 — stem matching (fallback when cloze_prompt unavailable):
        Strip trailing Polish vowel from target for a root stem, then find the
        sentence token whose lowercased form starts with that stem. Handles regular
        inflection (angielski → angielskiego) but not suppletive forms (iść → idę).
        For multiple candidates: prefer exact match, then longest.

    Returns None if neither strategy produces a result — caller falls back to unit.text.
    """
    # Strategy 1: positional alignment using shared tokenizer
    if cloze_prompt:
        sent_tokens = _tokenize(sentence)
        prompt_tokens = _tokenize(cloze_prompt)
        if len(sent_tokens) == len(prompt_tokens):
            for s_tok, p_tok in zip(sent_tokens, prompt_tokens):
                if p_tok == _BLANK:
                    result = _strip_punct(s_tok)
                    return result if result else None

    # Strategy 2: stem-based fallback
    words = target.strip().split()

    if len(words) > 1:
        # Multi-word target: find the phrase and extract last word (the blanked one)
        pattern = re.compile(re.escape(target), re.IGNORECASE)
        match = pattern.search(sentence)
        if not match:
            return None
        return _strip_punct(match.group(0).split()[-1]) or None

    # Single-word target: stem comparison with trailing-vowel stripping
    target_clean = target.strip().lower()
    target_stem = (
        re.sub(r"[aoeiuąęó]$", "", target_clean)
        if len(target_clean) > 3
        else target_clean
    )

    candidates = []
    for tok in _tokenize(sentence):
        clean = _strip_punct(tok)
        if clean and clean.lower().startswith(target_stem):
            candidates.append(clean)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Multiple candidates: prefer exact match, then longest (most inflected form)
    for c in candidates:
        if c.lower() == target_clean:
            return c
    return max(candidates, key=len)


def make_cloze_prompt(sentence: str, target: str) -> str:
    """
    Replace the first case-insensitive occurrence of target in sentence with a blank.
    For multi-word targets, only the last word in the matched phrase is blanked.
    Raises ValueError if target does not appear in sentence.
    """
    if not target:
        raise ValueError("target must be non-empty")
    words = target.strip().split()
    if len(words) > 1:
        pattern = re.compile(re.escape(target), re.IGNORECASE)
        if not pattern.search(sentence):
            raise ValueError(f"Target '{target}' not found in sentence")

        def replace_last_word(match: re.Match[str]) -> str:
            matched = match.group(0)
            words_in_match = matched.split()
            words_in_match[-1] = "________"
            return " ".join(words_in_match)

        return pattern.sub(replace_last_word, sentence, count=1)
    pattern = re.compile(re.escape(target), re.IGNORECASE)
    if not pattern.search(sentence):
        raise ValueError(f"Target '{target}' not found in sentence")
    return pattern.sub("________", sentence, count=1)


def _parse_json_object(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _ollama_chat_sync(messages: list[AIMessage], *, timeout: float) -> str:
    ollama_messages = [{"role": m.role.value, "content": m.content} for m in messages]
    payload = {
        "model": settings.ollama_model,
        "messages": ollama_messages,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    msg = data.get("message") or {}
    return (msg.get("content") or "").strip()


def _openai_chat_sync(messages: list[AIMessage], *, timeout: float) -> str:
    """Synchronous OpenAI chat completion (same shape as OpenAIClient), with a tight timeout."""
    openai_messages = [{"role": m.role.value, "content": m.content} for m in messages]
    payload = {
        "model": settings.openai_model,
        "messages": openai_messages,
        "temperature": 0.2,
        "max_tokens": 256,
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message") or {}
    return (msg.get("content") or "").strip()


def _generate_with_timeout(
    provider: AIProvider,
    messages: list[AIMessage],
    *,
    timeout: float,
) -> AIResponse:
    async def _run() -> AIResponse:
        return await asyncio.wait_for(
            provider.generate(messages, temperature=0.2, max_tokens=256),
            timeout=timeout,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


def _ai_completion(
    messages: list[AIMessage],
    *,
    ai_service: Optional[AIProvider],
    timeout: float,
) -> str:
    if ai_service is not None:
        try:
            resp = _generate_with_timeout(ai_service, messages, timeout=timeout)
        except asyncio.TimeoutError:
            raise httpx.TimeoutException("AI request timed out") from None
        return (resp.content or "").strip()

    prov = (settings.ai_provider or "ollama").strip().lower()
    if prov == "openai" and settings.openai_api_key:
        return _openai_chat_sync(messages, timeout=timeout)
    return _ollama_chat_sync(messages, timeout=timeout)


def generate_context_sentence(
    unit: LearningUnit,
    db: Session,
    ai_service: Optional[AIProvider] = None,
) -> bool:
    """
    Ask the model for a Polish sentence containing unit.text verbatim; cache on success.
    Returns False on validation failure, timeout, or any error (never raises).
    """
    word = (unit.text or "").strip()
    if not word:
        return False

    wc = len(word.split())
    if 2 <= wc <= 3:
        prompt = (
            f'Generate one example sentence in Polish that naturally uses the expression "{word}". '
            f'The sentence must contain "{word}" verbatim (same spelling). '
            "Keep it under 15 words. Use only common vocabulary. "
            "Also provide the English translation. "
            'Return JSON only: {"sentence": "...", "translation": "..."}'
        )
    else:
        prompt = (
            f'Generate one example sentence in Polish using the word "{word}" exactly as written '
            f'(same form, same spelling). The sentence must contain "{word}" verbatim. '
            "Keep it under 15 words. Use only common vocabulary. "
            "Also provide the English translation of the sentence. "
            'Return JSON only: {"sentence": "...", "translation": "..."}'
        )
    messages = [
        AIMessage(role=AIRole.SYSTEM, content="Reply with a single JSON object only, no markdown."),
        AIMessage(role=AIRole.USER, content=prompt),
    ]

    try:
        raw = _ai_completion(messages, ai_service=ai_service, timeout=5.0)
    except (httpx.TimeoutException, httpx.HTTPError, OSError, RuntimeError) as e:
        logger.info("cloze generation failed for unit_id=%s: %s", unit.id, e)
        return False
    except Exception as e:
        logger.info("cloze generation error for unit_id=%s: %s", unit.id, e)
        return False

    data = _parse_json_object(raw)
    if not data:
        return False
    sentence = str(data.get("sentence") or "").strip()
    translation = str(data.get("translation") or "").strip()
    if not sentence or not translation:
        return False

    if len(sentence.split()) < 3:
        return False

    unit.context_sentence = sentence
    unit.context_sentence_translation = translation
    try:
        db.add(unit)
        db.commit()
    except Exception as e:
        logger.warning("cloze DB commit failed for unit_id=%s: %s", unit.id, e)
        db.rollback()
        return False
    return True


def backfill_context_sentences(
    db: Session,
    limit: int = 50,
) -> dict:
    """Batch-generate context sentences for units that don't have one.

    Targets only words and short phrases (< 5 words) — long phrases and
    sentences are excluded from cloze mode anyway.

    Returns:
        processed  – number of units attempted
        succeeded  – number of units that got a sentence saved
        skipped    – units skipped (long phrase / sentence type)
        remaining  – units still missing context after this run
    """
    from app.models.learning_unit import UnitType

    # Filter long phrases (≥5 words) in SQL by counting spaces.
    # space_count = len(text) - len(text.replace(' ', '')) = word_count - 1
    # ≥5 words → ≥4 spaces, so eligible units have space_count < 4.
    _space_count = func.length(LearningUnit.text) - func.length(
        func.replace(LearningUnit.text, " ", "")
    )
    candidates = (
        db.query(LearningUnit)
        .filter(LearningUnit.context_sentence.is_(None))
        .filter(LearningUnit.type != UnitType.SENTENCE)
        .filter(_space_count < 4)
        .limit(limit)
        .all()
    )

    processed = 0
    succeeded = 0
    skipped = 0

    for unit in candidates:
        processed += 1
        if generate_context_sentence(unit, db):
            succeeded += 1
        time.sleep(0.15)

    # Remaining = all cloze-eligible units missing context:
    # words + phrases with < 5 words (consistent with _is_phrase_unit threshold).
    # Word count is not a DB column, so fetch words and short phrases separately.
    remaining_words = (
        db.query(LearningUnit)
        .filter(LearningUnit.context_sentence.is_(None))
        .filter(LearningUnit.type == UnitType.WORD)
        .count()
    )
    remaining_phrases = sum(
        1
        for u in db.query(LearningUnit)
        .filter(LearningUnit.context_sentence.is_(None))
        .filter(LearningUnit.type == UnitType.PHRASE)
        .all()
        if not _is_phrase_unit(u)
    )
    remaining = remaining_words + remaining_phrases

    return {
        "processed": processed,
        "succeeded": succeeded,
        "skipped": skipped,
        "remaining": remaining,
    }


def get_or_generate_sentence(
    unit: LearningUnit,
    db: Session,
    ai_service: Optional[AIProvider] = None,
) -> Optional[str]:
    """Return cached context sentence or generate once; None if unavailable."""
    if _is_phrase_unit(unit):
        return None
    if unit.context_sentence:
        return unit.context_sentence
    if generate_context_sentence(unit, db, ai_service):
        return unit.context_sentence
    return None
