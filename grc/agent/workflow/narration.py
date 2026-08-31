"""LLM narration and read-only Q&A for user-facing workflow replies.

The host supplies facts.  The model writes the English; callers must not
hard-code stage-specific chat scripts.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_NARRATION_PROMPT = """You write a short user-facing reply for a radio engineering assistant.
Use Markdown. Cover only:
1. what you just did
2. the current status
3. what you will do next
4. what the user should do now, if anything
5. a brief invitation to ask, suggest, or discuss
When the user needs to answer or choose, put those asks in a short Markdown list.
Keep wording concise and friendly. A few light emoji are welcome; do not decorate every sentence.
Do not repeat a specification table. Do not dump logs, IDs, tool names, or internal field names.
Do not invent actions that are not in the facts. At most five short sentences or one short list.
"""

_QUESTION_PROMPT = """You are a radio engineering assistant answering a user question.
Use only the provided context (specification, flowgraph notes, and workflow status).
Answer the question first, in Markdown.
Then briefly include: what you just did, the current status, what happens next, whether the user needs to act now, and that they may ask more or suggest a change.
If the user must choose or supply a value, use a short Markdown list.
A few light emoji are welcome. Do not advance the workflow, invent measurements, or dump logs, IDs, or internal field names.
If the context does not contain the answer, say so plainly and point to what is known.
Keep it short.
"""


def narrate_turn(
    *,
    user_text: str = "",
    facts: Optional[Mapping[str, Any]] = None,
    fallback: str = "",
) -> str:
    """Rewrite a turn into a concise user-facing reply, or keep ``fallback``."""
    text = _chat(
        _NARRATION_PROMPT,
        {
            "user_text": str(user_text or ""),
            "facts": dict(facts or {}),
            "fallback": str(fallback or ""),
        },
    )
    return text or str(fallback or "").strip()


def answer_question(
    *,
    user_text: str,
    context: Optional[Mapping[str, Any]] = None,
    fallback: str = "",
) -> str:
    """Answer a read-only question without changing workflow state."""
    text = _chat(
        _QUESTION_PROMPT,
        {
            "question": str(user_text or ""),
            "context": dict(context or {}),
        },
    )
    if text:
        return text
    return str(fallback or "").strip() or (
        "I can discuss the current specification, flowgraph, and next step. "
        "Ask a concrete question, suggest a change, or confirm to continue."
    )


def _chat(system: str, payload: Dict[str, Any]) -> str:
    from ..llm import chat, intent_test_bypass_enabled, is_configured

    if not is_configured() or intent_test_bypass_enabled():
        return ""
    try:
        content = chat(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Narration LLM failed: %s", exc)
        return ""
    return str(content or "").strip()
