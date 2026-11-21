"""Critic B — Search-Intent Auditor.

Watches the output of ``describe_intent.classify_intent()`` and asks
one question: do the extracted search keywords ACCURATELY cover the
user's described concept, with neither off-topic noise nor
over-narrow phrasing that would zero out the GitHub search?

Why this exists: the intent classifier has been observed producing
keyword sets that are either (a) too tangential to the user's stated
concept (off-topic) or (b) so specific that the GitHub search
returned 0 candidates against a real field of well-known repos
(over-narrow). Both failure modes were observed on 2026-05-26.

Scope: this critic does NOT police the ``intent`` field
(wrap_repo vs from_scratch) — only the ``search_keywords``. Intent
classification is its own concern.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from services.critic_loop import (
    LoopResult,
    Verdict,
    critic_loop,
    format_issues_for_proposer,
)
from utils.llm_client import LLMClientError, call_wizard_llm

from . import describe_intent
from .describe_intent import IntentResult, classify_intent

logger = logging.getLogger(__name__)


_CRITIC_SYSTEM_PROMPT = """You are reviewing a list of GitHub-search \
keywords that another assistant extracted from a user's description \
of a tool they want built.

Your ONLY job is to check whether those keywords ACCURATELY cover \
the user's stated concept:

1. ON-TOPIC: every keyword should relate to what the user actually \
described. Reject keywords that wandered off into a related-but- \
different domain.

2. COVERAGE: between them, the keywords should cover the core \
concept of what the user wants. If the user's concept has multiple \
canonical names (e.g. "image to 3D" could also be "single image \
3D reconstruction", "image-to-mesh"), the keyword set should \
include at least one variant likely to match.

3. SPECIFICITY: keywords should be the canonical short phrases real \
repos use in their descriptions ("face detection", "speech to text"), \
NOT user-prose phrases ("the thing that finds faces in photos") and \
NOT single overly-generic words ("ai", "tool", "model") that would \
match anything.

Output JSON only, no prose, no markdown:
{"ok": true} if the keyword set passes all three checks.
{"ok": false, "issues": ["..."]} if any fail. Each issue is one short \
sentence saying exactly which keyword is wrong and what to replace it \
with. Example: "Drop 'machine learning' — it's too generic. Add \
'single image 3d' as a coverage variant of 'image to 3d'."
"""


def _build_critic_user_prompt(description: str, keywords: List[str]) -> str:
    kw_block = "\n".join(f"- {k}" for k in keywords) if keywords else "(empty)"
    return (
        "The user described what they want to build:\n"
        f'"{description.strip()}"\n'
        "\n"
        "Extracted keywords to review:\n"
        f"{kw_block}\n"
    )


def _critic_call(
    *,
    description: str,
    keywords: List[str],
    llm: Callable[..., Any],
) -> Verdict:
    user_prompt = _build_critic_user_prompt(description, keywords)
    try:
        parsed = llm(
            system_prompt=_CRITIC_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=10,
            temperature=0.0,
            max_tokens=250,
        )
    except LLMClientError as e:
        logger.info("critic_search_intent LLM unavailable: %s", e)
        return Verdict(ok=True, issues=[], notes={"critic_error": str(e)})

    if not isinstance(parsed, dict):
        return Verdict(ok=True, issues=[], notes={"critic_error": "non-dict response"})

    ok = parsed.get("ok")
    if not isinstance(ok, bool):
        return Verdict(ok=True, issues=[], notes={"critic_error": "missing ok"})
    if ok:
        return Verdict(ok=True, issues=[])

    raw = parsed.get("issues") or []
    if not isinstance(raw, list):
        return Verdict(ok=False, issues=["(reviewer raised an objection but its response was malformed)"])
    issues = [str(i).strip() for i in raw if str(i).strip()][:5]
    if not issues:
        issues = ["(reviewer raised an objection with no concrete reason — try again)"]
    return Verdict(ok=False, issues=issues)


def classify_intent_with_critic(
    description: str,
    *,
    llm: Optional[Callable[..., Any]] = None,
    max_iters: int = 5,
) -> IntentResult:
    """Drop-in replacement for ``classify_intent`` that runs the
    keyword set through the Search-Intent Auditor and re-prompts on
    failure up to ``max_iters`` times.

    Returns the same ``IntentResult`` shape so the caller's
    downstream logic (GitHub query construction, fallback handling)
    is unchanged.
    """
    if llm is None:
        llm = call_wizard_llm

    def proposer(prev_attempt: Any, prior_issues: Optional[List[str]]) -> IntentResult:
        # If a prior critic objection exists, splice it onto the
        # description so the intent classifier sees the feedback
        # inline. Keep the structure neutral so an OK first attempt
        # doesn't pay any feedback-formatting overhead.
        if prior_issues and prev_attempt:
            prev_kw = ", ".join(prev_attempt.search_keywords) if isinstance(prev_attempt, IntentResult) else ""
            feedback = format_issues_for_proposer(prev_kw, prior_issues)
            augmented = f"{description}\n\n[Reviewer feedback on previous attempt:]\n{feedback}"
        else:
            augmented = description
        return classify_intent(augmented, llm=llm)

    def critic(output: IntentResult) -> Verdict:
        # Skip critic on the fallback path — the classifier already
        # signalled it couldn't produce useful keywords and the
        # caller has its own fallback handling. Nothing to police.
        if not isinstance(output, IntentResult) or output.fallback:
            return Verdict(ok=True, issues=[], notes={"skipped": "fallback_path"})
        if not output.search_keywords:
            return Verdict(ok=True, issues=[], notes={"skipped": "no_keywords"})
        return _critic_call(
            description=description,
            keywords=list(output.search_keywords),
            llm=llm,
        )

    result: LoopResult = critic_loop(
        proposer,
        critic,
        max_iters=max_iters,
        name="search_intent",
        logger_=logger,
    )
    return result.output
