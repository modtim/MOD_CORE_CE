"""wizard-rebuild §8 — cascade orchestrator.

Walks the 4 steps in order (A → B → C → D) and short-circuits when
a step produces a candidate above the confidence floor (default
0.7). Returns a merged ranked list across steps plus per-backend
rate-limit + error status so the UI can show why a step was
skipped.

Short-circuit semantics:
  - Step A produces candidate X with confidence 0.85, floor=0.7 →
    cascade STOPS at A; steps B/C/D are not run.
  - Steps A and B return only low-confidence candidates → cascade
    continues into C.
  - All four steps run, none clear floor → cascade returns the
    best-overall candidate (`reason="exhausted"`).
  - Only D produces a candidate → `reason="fallback-codegen"`.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import rate_limits
from ._types import Candidate, CascadeResult, StepResult
from .step_a_container_search import run_step_a
from .step_b_repo_search import run_step_b
from .step_c_library_search import run_step_c
from .step_d_codegen import run_step_d

logger = logging.getLogger(__name__)


DEFAULT_CONFIDENCE_FLOOR = 0.7


def _best(candidates: list[Candidate]) -> Optional[Candidate]:
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.confidence)


def run_cascade(
    db,
    *,
    description: str,
    tenant_id: int,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> CascadeResult:
    """Orchestrate the 4-step cascade and return the merged result.

    Args:
      db: SQLAlchemy session (forwarded to step B which reuses the
        existing /describe-search infra).
      description: user-provided natural-language description.
      tenant_id: per-tenant rate-limit scope.
      confidence_floor: short-circuit threshold; defaults to 0.7.

    Cascade stops as soon as a step produces a candidate with
    confidence ≥ floor. Otherwise it walks all 4 steps and returns
    the best-overall across them.
    """
    result = CascadeResult(
        description=description,
        confidence_floor=confidence_floor,
    )

    step_runners = [
        ("A", lambda: run_step_a(description, tenant_id)),
        ("B", lambda: run_step_b(db, description, tenant_id)),
        ("C", lambda: run_step_c(description, tenant_id)),
        ("D", lambda: run_step_d(description)),
    ]

    stopped_at: Optional[str] = None
    for step_name, fn in step_runners:
        try:
            step_result: StepResult = fn()
        except Exception as exc:
            logger.warning("cascade step %s raised: %s", step_name, exc)
            step_result = StepResult(step=step_name, errors={step_name: str(exc)[:200]})
        # Merge per-step rate-limit + error status into the cascade-
        # level dicts so callers see one consolidated view.
        for k, v in step_result.rate_limited.items():
            result.rate_limited[k] = v
        for k, v in step_result.errors.items():
            result.errors[k] = v
        result.per_step.append(step_result)
        result.candidates.extend(step_result.candidates)
        # GitHub (step B) must ALWAYS be consulted before we trust a match and
        # stop: step A (container registries) scores popular-but-wrong images
        # confidently, and short-circuiting there skipped the repo/library
        # search entirely — the wizard never reached GitHub. So a step-A
        # candidate can rank, but it cannot END the cascade; the earliest a
        # short-circuit may fire is after B. (The relevance gate keeps junk
        # below the floor; this guarantees the real source is still searched.)
        if step_name == "A":
            continue
        best = _best(result.candidates)
        if best and best.confidence >= confidence_floor:
            step_result.short_circuited = True
            stopped_at = step_name
            result.stopped_at_step = step_name  # type: ignore[assignment]
            break

    # Final ranking — across all run steps. Codegen sits at the
    # bottom by design unless nothing else fired.
    result.candidates.sort(key=lambda c: -c.confidence)

    if stopped_at:
        result.reason = f"short-circuited at step {stopped_at}"
    elif not result.candidates:
        result.reason = "no candidates from any step"
    elif all(c.kind == "codegen" for c in result.candidates):
        result.reason = "fallback-codegen"
    else:
        result.reason = "exhausted"

    # Snapshot CURRENT rate-limit state across every known backend so
    # the UI can show "Docker Hub: 7/10 remaining" even for backends
    # this cascade run didn't hit.
    snap = rate_limits.snapshot(tenant_id)
    for backend, status in snap.items():
        # Don't clobber a step-reported rate_limited entry — those
        # have the precise "you got blocked just now" message.
        result.rate_limited.setdefault(backend, status)

    return result
