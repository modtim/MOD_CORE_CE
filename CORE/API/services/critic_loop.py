"""Generic proposer + critic loop helper.

Pattern: a proposer LLM generates output; a critic LLM checks it
against a one-job criterion and returns {ok, issues}. If !ok, feed
the issues back into the proposer with the previous attempt visible
("you said X; the issues were Y; try again"). Bounded retry.

Three specialised wizard critics use this helper:
  - Chat-Question Auditor (critic_chat_question.py)
  - Search-Intent Auditor (critic_search_intent.py)
  - Artifact Auditor      (critic_artifacts.py)

The helper does NOT call the LLM itself — both ``proposer`` and
``critic`` are caller-supplied closures. This keeps the helper
LLM-client-agnostic and trivially testable: tests inject pure
Python functions that return deterministic verdicts.

Failure modes:
  - Critic itself raises / returns malformed verdict → log warning,
    treat as ok=True (ship the proposer's output unverified rather
    than block on infra failure). Matches Tim's #4 decision.
  - Proposer raises → re-raise; we have no useful retry strategy
    when the proposer itself is broken.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """One critic verdict. ok=True ends the loop; ok=False + issues
    feeds back into the next proposer attempt."""
    ok: bool
    issues: List[str] = field(default_factory=list)
    # Optional free-form notes the critic wants to attach for
    # observability (not fed back into the proposer prompt).
    notes: dict = field(default_factory=dict)


@dataclass
class LoopResult:
    """What ``critic_loop`` returns to the caller. ``output`` is the
    final proposer output (even if the loop exhausted retries with
    ok=False). ``ok`` reflects whether the final attempt passed the
    critic. ``iterations`` is how many proposer calls were made."""
    output: Any
    ok: bool
    iterations: int
    last_verdict: Optional[Verdict]


ProposerFn = Callable[[Optional[Any], Optional[List[str]]], Any]
"""Signature: proposer(prev_attempt, prior_issues) → output.

The first arg is None on the first iteration; on retry, it's
whatever the proposer returned last time. The second arg is None
on first iteration; on retry, the issues list from the last
critic verdict. Implementations decide how to weave both into
their LLM prompt.
"""

CriticFn = Callable[[Any], Verdict]
"""Signature: critic(proposer_output) → Verdict.

Critics are stateless one-shot evaluators. They see the
proposer's output and return ok/issues. They do NOT see the
user's original input — caller closes over that.
"""


def critic_loop(
    proposer: ProposerFn,
    critic: CriticFn,
    *,
    max_iters: int = 5,
    name: str,
    logger_: Optional[logging.Logger] = None,
) -> LoopResult:
    """Run proposer → critic until critic says ok or ``max_iters``
    attempts have been made.

    ``name`` is included in every structured log line so we can
    answer "how often does loop X save us vs. just retry pointlessly"
    after launch traffic has accumulated.
    """
    log = logger_ or logger
    prev_attempt: Optional[Any] = None
    prior_issues: Optional[List[str]] = None
    last_verdict: Optional[Verdict] = None

    for iteration in range(1, max_iters + 1):
        t0 = time.monotonic()
        output = proposer(prev_attempt, prior_issues)
        proposer_ms = int((time.monotonic() - t0) * 1000)

        t1 = time.monotonic()
        verdict = _safe_critic(critic, output, log=log, name=name)
        critic_ms = int((time.monotonic() - t1) * 1000)

        last_verdict = verdict
        log.info(
            "critic_loop name=%s iter=%d ok=%s issues=%d proposer_ms=%d critic_ms=%d",
            name,
            iteration,
            verdict.ok,
            len(verdict.issues),
            proposer_ms,
            critic_ms,
            extra={
                "critic_loop": {
                    "name": name,
                    "iteration": iteration,
                    "ok": verdict.ok,
                    "issues": verdict.issues,
                    "proposer_ms": proposer_ms,
                    "critic_ms": critic_ms,
                    "notes": verdict.notes,
                }
            },
        )

        if verdict.ok:
            return LoopResult(
                output=output,
                ok=True,
                iterations=iteration,
                last_verdict=verdict,
            )

        prev_attempt = output
        prior_issues = list(verdict.issues)

    log.warning(
        "critic_loop name=%s exhausted max_iters=%d with ok=False; "
        "shipping last attempt unverified",
        name,
        max_iters,
    )
    return LoopResult(
        output=prev_attempt,
        ok=False,
        iterations=max_iters,
        last_verdict=last_verdict,
    )


def _safe_critic(critic: CriticFn, output: Any, *, log: logging.Logger, name: str) -> Verdict:
    """Per Tim's #4: if the critic itself fails (raises, returns
    non-Verdict), don't block the proposer's output. Log it and
    return ok=True so the loop exits gracefully."""
    try:
        verdict = critic(output)
    except Exception as e:
        log.warning(
            "critic_loop name=%s critic raised %s; shipping proposer output unverified",
            name,
            type(e).__name__,
        )
        return Verdict(ok=True, issues=[], notes={"critic_error": str(e)})

    if not isinstance(verdict, Verdict):
        log.warning(
            "critic_loop name=%s critic returned non-Verdict %s; shipping output unverified",
            name,
            type(verdict).__name__,
        )
        return Verdict(ok=True, issues=[], notes={"critic_error": "non-verdict"})

    return verdict


def format_issues_for_proposer(prev_attempt: Any, issues: List[str]) -> str:
    """Standard way to render a previous attempt + critic issues into
    a feedback block the proposer prompt can paste in. Keeps the
    previous attempt visible (per Tim's #7) so the model has full
    context of what it said before."""
    if not issues:
        return ""
    issues_block = "\n".join(f"- {i}" for i in issues)
    return (
        "Your previous attempt produced this:\n"
        f"---\n{prev_attempt}\n---\n"
        "A reviewer raised these specific issues:\n"
        f"{issues_block}\n"
        "Address each issue above and try again. Keep everything else the same."
    )
