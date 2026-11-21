"""δ §21.5 — rewrite scripted Describe-tab prompts to be specific to the
user's described use case.

The Describe-tab chat in `WorkflowBuilder/lib/describe-chat.ts` has a
small set of canonical system prompts ("what kind of thing goes in?",
"and what should come out?", etc.). They work, but they're generic.
This module re-phrases them around the user's actual description so
each question feels conversational rather than form-driven.

Same LLM the wizard already uses (`call_wizard_llm`). Short timeout —
the chat is interactive and we can't make the user wait 30s for
polish. On any failure (LLM unavailable, timeout, malformed
response) `rewrite_prompt()` returns ``{"ok": False}`` and the
caller renders the scripted version unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from utils.llm_client import LLMClientError, call_wizard_llm


logger = logging.getLogger(__name__)

# Prompt keys the frontend is allowed to ask us to rewrite. Anything
# outside this list returns a 400 — keeps the LLM out of unintended
# surface area and makes the contract grep-able.
ALLOWED_PROMPT_KEYS = frozenset({
    "pick_repo",
    "ask_input",
    "ask_output",
    "ready_repo",
    "ready_scratch",
})

# Hard cap on rewritten text length. The frontend's chat bubble can
# handle longer, but anything over this is almost certainly the LLM
# going off-script.
_MAX_TEXT_LEN = 400

_SYSTEM_PROMPT = """You re-phrase one short question in a friendly chat assistant. \
The user is non-technical and is describing a custom tool they want built. \
Re-phrase the canonical question so it feels specific to THEIR description, \
while keeping the EXACT same information request — don't ask anything extra, \
don't drop anything the original asked for, don't change the intent.

Rules:
- 1 sentence preferred, 2 maximum. No lists, no headers, no code.
- Plain conversational English. Don't introduce jargon the user didn't use.
- If you can't improve on the canonical version, return it unchanged.
- Output JSON only: {"text": "your re-phrased version"}.
"""


def _build_user_prompt(
    *,
    prompt_key: str,
    canonical_text: str,
    description: str,
    chosen_repo: Optional[str],
    input_answer: Optional[str],
    output_answer: Optional[str],
) -> str:
    lines = [
        f"The user described what they want to build:",
        f'"{description.strip()}"',
        "",
    ]
    if chosen_repo:
        lines.append(f"They picked this repo as the starting point: {chosen_repo}")
        lines.append("")
    if input_answer:
        lines.append(f'They already told us the input is: "{input_answer.strip()}"')
        lines.append("")
    if output_answer:
        lines.append(f'They already told us the output is: "{output_answer.strip()}"')
        lines.append("")
    lines.append(f"Canonical question to re-phrase (prompt_key={prompt_key}):")
    lines.append(f'"{canonical_text.strip()}"')
    return "\n".join(lines)


def rewrite_prompt(
    *,
    prompt_key: str,
    canonical_text: str,
    description: str,
    chosen_repo: Optional[str] = None,
    input_answer: Optional[str] = None,
    output_answer: Optional[str] = None,
    llm: Optional[Callable[..., Any]] = None,
    timeout_s: int = 6,
) -> Dict[str, Any]:
    """Personalise one scripted chat prompt.

    Returns ``{"ok": True, "text": "..."}`` when the LLM returned a
    usable, length-bounded string; ``{"ok": False}`` on any failure
    (unconfigured, timeout, malformed JSON, refusal). The caller
    falls back to ``canonical_text`` when ``ok=False``.
    """
    prompt_key = (prompt_key or "").strip()
    if prompt_key not in ALLOWED_PROMPT_KEYS:
        return {"ok": False, "error": "unknown prompt_key"}
    description = (description or "").strip()
    canonical_text = (canonical_text or "").strip()
    if not description or not canonical_text:
        return {"ok": False, "error": "missing description or canonical_text"}

    if llm is None:
        llm = call_wizard_llm

    user_prompt = _build_user_prompt(
        prompt_key=prompt_key,
        canonical_text=canonical_text,
        description=description,
        chosen_repo=chosen_repo,
        input_answer=input_answer,
        output_answer=output_answer,
    )
    try:
        parsed = llm(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=timeout_s,
            # Slightly warmer than default — we want phrasing variety,
            # not robotic minor edits.
            temperature=0.5,
            # Hard cap on tokens; rewrites are short.
            max_tokens=120,
        )
    except LLMClientError as e:
        logger.info("describe-rewrite LLM unavailable: %s", e)
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.warning("describe-rewrite LLM raised: %s", e)
        return {"ok": False, "error": "llm raised"}

    if not isinstance(parsed, dict):
        return {"ok": False, "error": "non-dict response"}
    text = parsed.get("text")
    if not isinstance(text, str):
        return {"ok": False, "error": "no text field"}
    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    if len(text) > _MAX_TEXT_LEN:
        # Trim rather than reject — the leading content is usually the
        # useful part and the LLM sometimes appends meta-commentary.
        text = text[:_MAX_TEXT_LEN].rstrip() + "…"

    return {"ok": True, "text": text}
