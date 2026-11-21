"""Critic A — Chat-Question Auditor.

Watches the output of ``describe_rewrite.rewrite_prompt()`` and asks
one question: does the rewritten chat prompt ask ONLY about information
the user hasn't already given us, and does it AVOID inventing examples
(file types, modes, tools) the user didn't mention?

Why this exists: the rewriter, when given "Reconstruct 3D scenes from
a sequence of images using photogrammetry" and the canonical
"What kind of thing goes in?", produced "what kind of images or
videos will you be using…" — redundant ("images" was already in the
description) and hallucinatory ("or videos" was invented).

This critic is laser-focused on those two failure modes and nothing
else. It does not police phrasing, tone, length, etc.
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

# Import the MODULE (not the function) so callers — including the
# existing endpoint tests — can monkeypatch ``node_builder.describe_rewrite.rewrite_prompt``
# and have the patch reach our proposer. A ``from … import rewrite_prompt``
# binds a separate name in this module's namespace which monkeypatch
# can't reach.
from . import describe_rewrite as _describe_rewrite_mod

logger = logging.getLogger(__name__)


_CRITIC_SYSTEM_PROMPT = """You are reviewing one rephrased chat question \
that a build wizard is about to show to a non-technical user.

Your ONLY job is to check two things:

1. Does the rephrased question ask about information the user did NOT \
already provide in their description? If the user already named a file \
type, mode, or example in their description, the rephrased question \
must not ask them to clarify it again.

2. Does the rephrased question AVOID inventing examples the user did \
not mention? If the user said "images", the question must not mention \
"videos" or "audio" or any file type the user did not name.

Output JSON only, no prose, no markdown:
{"ok": true} if the question passes both checks.
{"ok": false, "issues": ["..."]} if it fails. Each issue is one short \
sentence explaining EXACTLY which check failed and how to fix it. \
Example: "The user said 'images' but the question asks about 'images \
or videos'. Drop 'or videos'."
"""


def _build_critic_user_prompt(
    *,
    description: str,
    canonical_text: str,
    rewritten_text: str,
    chosen_repo: Optional[str],
    input_answer: Optional[str],
    output_answer: Optional[str],
) -> str:
    lines = [
        "The user's original description:",
        f'"{description.strip()}"',
        "",
    ]
    if chosen_repo:
        lines.append(f'They picked this repo: "{chosen_repo}"')
        lines.append("")
    if input_answer:
        lines.append(f'They already named the input: "{input_answer}"')
        lines.append("")
    if output_answer:
        lines.append(f'They already named the output: "{output_answer}"')
        lines.append("")
    lines.append("Canonical question being rephrased:")
    lines.append(f'"{canonical_text}"')
    lines.append("")
    lines.append("Rephrased question to review:")
    lines.append(f'"{rewritten_text}"')
    return "\n".join(lines)


def _critic_call(
    *,
    description: str,
    canonical_text: str,
    rewritten_text: str,
    chosen_repo: Optional[str],
    input_answer: Optional[str],
    output_answer: Optional[str],
    llm: Callable[..., Any],
) -> Verdict:
    user_prompt = _build_critic_user_prompt(
        description=description,
        canonical_text=canonical_text,
        rewritten_text=rewritten_text,
        chosen_repo=chosen_repo,
        input_answer=input_answer,
        output_answer=output_answer,
    )
    try:
        parsed = llm(
            system_prompt=_CRITIC_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=6,
            temperature=0.0,
            max_tokens=200,
        )
    except LLMClientError as e:
        # Treat critic outages as "no objection" — proposer wins.
        # Logged here so a sustained outage shows up in the operator
        # log rather than silently bypassing every critic gate.
        logger.info("critic_chat_question LLM unavailable: %s", e)
        return Verdict(ok=True, issues=[], notes={"critic_error": str(e)})

    if not isinstance(parsed, dict):
        return Verdict(ok=True, issues=[], notes={"critic_error": "non-dict response"})

    ok = parsed.get("ok")
    if not isinstance(ok, bool):
        return Verdict(ok=True, issues=[], notes={"critic_error": "missing ok"})
    if ok:
        return Verdict(ok=True, issues=[])

    raw_issues = parsed.get("issues") or []
    if not isinstance(raw_issues, list):
        return Verdict(ok=False, issues=["(reviewer raised an objection but its response was malformed)"])
    issues = [str(i).strip() for i in raw_issues if str(i).strip()][:5]
    if not issues:
        issues = ["(reviewer raised an objection with no concrete reason — try again)"]
    return Verdict(ok=False, issues=issues)


def rewrite_prompt_with_critic(
    *,
    prompt_key: str,
    canonical_text: str,
    description: str,
    chosen_repo: Optional[str] = None,
    input_answer: Optional[str] = None,
    output_answer: Optional[str] = None,
    llm: Optional[Callable[..., Any]] = None,
    max_iters: int = 5,
) -> dict:
    """Drop-in replacement for ``describe_rewrite.rewrite_prompt`` that
    runs the result through the Chat-Question Auditor and re-prompts
    on failure up to ``max_iters`` times.

    Returns the same shape as ``rewrite_prompt()`` so callers don't
    need to branch on whether the critic was applied. On critic
    exhaustion, returns the last attempt (graceful degrade).
    """
    if llm is None:
        llm = call_wizard_llm

    def proposer(prev_attempt: Any, prior_issues: Optional[List[str]]) -> dict:
        # Convert prior critic issues into an additional_instructions
        # block the rewriter prompt builder doesn't natively accept;
        # we splat it onto the description so the rewriter LLM sees
        # both the original ask AND the issue list in the same turn.
        if prior_issues and prev_attempt:
            prev_text = (prev_attempt or {}).get("text", "")
            issue_block = format_issues_for_proposer(prev_text, prior_issues)
            augmented_description = f"{description}\n\n[Reviewer feedback on previous attempt:]\n{issue_block}"
        else:
            augmented_description = description
        return _describe_rewrite_mod.rewrite_prompt(
            prompt_key=prompt_key,
            canonical_text=canonical_text,
            description=augmented_description,
            chosen_repo=chosen_repo,
            input_answer=input_answer,
            output_answer=output_answer,
            llm=llm,
        )

    def critic(output: dict) -> Verdict:
        # Skip critic if the proposer itself failed (e.g. LLM down,
        # malformed JSON, refusal). Caller will fall back to the
        # canonical scripted prompt; nothing for us to police.
        if not output or not output.get("ok"):
            return Verdict(ok=True, issues=[], notes={"skipped": "proposer_failed"})
        return _critic_call(
            description=description,
            canonical_text=canonical_text,
            rewritten_text=output.get("text", ""),
            chosen_repo=chosen_repo,
            input_answer=input_answer,
            output_answer=output_answer,
            llm=llm,
        )

    result: LoopResult = critic_loop(
        proposer,
        critic,
        max_iters=max_iters,
        name="chat_question",
        logger_=logger,
    )
    return result.output
