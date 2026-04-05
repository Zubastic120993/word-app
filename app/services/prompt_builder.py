"""
Centralized system prompt construction for AI Practice.

IMPORTANT:
- This is the single entry point for building system prompts.
- Scenario modifier intentionally precedes vocabulary modifier.
"""

from app.config import settings

MAX_SYSTEM_PROMPT_TOKENS = 800


def estimate_token_count(text: str) -> int:
    """Rough token estimate based on whitespace splitting."""
    return len(text.split())


def build_system_prompt(
    base_prompt: str,
    scenario: str | None = None,
    vocabulary_bias: list[str] | None = None,
    correction_mode: bool = False,
    intent: str | None = None,
    mode: str = "practice",
    session_vocab: list[dict] | None = None,
) -> str:
    # Session vocab injection leads the prompt — must appear before base identity so model prioritizes it
    session_prefix = session_vocab_modifier(session_vocab) if session_vocab else ""
    prompt = session_prefix + base_prompt

    # Scenario comes next — defines conversation context priority
    if scenario:
        prompt += scenario_modifier(scenario)

    # Vocabulary bias refines lexical preference (secondary priority)
    if vocabulary_bias:
        prompt += vocabulary_modifier(vocabulary_bias)

    if mode == "study":
        prompt += correction_modifier(correction_mode)

    if intent:
        prompt += intent_modifier(intent)

    return prompt


def scenario_modifier(scenario: str) -> str:
    """
    Injects a scenario role into the system prompt.
    Scenario has priority over vocabulary bias.
    """

    scenarios = {
        "restaurant": (
            "You are a waiter in a restaurant. "
            "Encourage the user to order food and drinks. "
            "Keep conversation practical and realistic."
        ),
        "hotel": (
            "You are a hotel receptionist. "
            "Help the user check in, ask for details, and discuss room preferences."
        ),
        "doctor": (
            "You are a doctor. "
            "Ask about symptoms and respond professionally but simply."
        ),
        "job_interview": (
            "You are conducting a job interview. "
            "Ask relevant questions and keep responses realistic."
        ),
    }

    if not scenario or scenario == "free":
        return ""

    scenario_prompt = scenarios.get(scenario)

    if not scenario_prompt:
        return ""

    return f"\n\nScenario context:\n{scenario_prompt}\n"


def vocabulary_modifier(words: list[str]) -> str:
    """
    Injects a soft vocabulary bias into the system prompt.

    Rules:
    - Soft preference only
    - Do NOT restrict vocabulary
    - Do NOT force unnatural usage
    - Keep concise to avoid token bloat
    """

    if not words:
        return ""

    word_list = ", ".join(words)

    return (
        "\n\n"
        f"Prefer using the following {settings.source_language} words naturally in conversation:\n"
        f"{word_list}\n\n"
        "Do not force them unnaturally, and do not restrict yourself strictly to this list."
    )


def correction_modifier(correction_mode: bool = True) -> str:
    if not correction_mode:
        return ""
    return """
When the user writes in Polish, analyze grammar and spelling strictly.

You MUST detect and correct:

- Grammatical errors
- Incorrect verb forms
- Wrong case usage
- Agreement errors
- Word order errors (if unnatural)
- Spelling mistakes
- Missing Polish diacritics

STRICT ORTHOGRAPHY RULE:

Missing Polish characters (ł, ą, ę, ś, ć, ź, ż, ń, ó)
MUST be treated as spelling errors.

For example:
- "glodny" → "głodny"
- "byc" → "być"
- "cześć" written as "czesc" → must be corrected

You must enforce proper Polish spelling.

Return JSON only:

{
  "response": "...",
  "corrections": [
    {
      "original": "...",
      "corrected": "...",
      "explanation": "..."
    }
  ]
}

If the user writes in English,
respond normally with:

{
  "response": "...",
  "corrections": []
}
"""


def session_vocab_modifier(vocab: list[dict]) -> str:
    """
    Injects recently studied words into the system prompt.
    Fires only on the first message (client-side guard ensures single send).
    """
    if not vocab:
        return ""

    word_list = ", ".join(
        f"{v['word']} ({v['translation']})" if v.get("translation") else v["word"]
        for v in vocab
    )

    return (
        "\n\nYou MUST base the conversation on the following Polish words:\n\n"
        f"{word_list}\n\n"
        "Your role: conversational Polish tutor. Your primary goal is to guide the conversation using these words. "
        "Keep the conversation natural, but actively steer it toward situations where the above words can be used. "
        "Ask questions that naturally invite these words and incorporate them into your replies. "
        "Reintroduce unused words as the conversation continues. "
        "Do not wait for the user to introduce the vocabulary — take initiative. "
        "Try to use or prompt at least one of these words in each reply when possible. "
        "No drills, no lists, no checkpoints, no structured testing. "
        "Just natural conversation with purposeful vocabulary guidance.\n"
    )


def intent_modifier(intent: str) -> str:
    # Placeholder for future extension
    return ""
