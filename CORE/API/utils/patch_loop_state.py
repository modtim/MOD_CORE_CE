"""Phase 5 — patch-loop bounded-retry + circuit-breaker state.

Persists to `NodeDefinition.custom_metadata.patch_attempts`. Each
(variant, stage) pair has its own independent retry budget so a
build-loop failure doesn't burn the smoke-drift budget (the plan's
rule #8).

Shape on disk (JSONB):

    patch_attempts:
      <variant>:
        <stage>:
          n:               int  — attempts used (starts at 0)
          last_log_hash:   str  — log-hash of the last failed build
          last_diff_hash:  str  — diff-hash of the last accepted patch
          token_total:     int  — cumulative LLM tokens for this slot
          first_attempt_at: iso8601

    # Node-level totals, aggregated across variants/stages.
    patch_attempts_meta:
      token_total:   int
      first_attempt_at: iso8601

Where `stage` ∈ {"build", "smoke_drift", "smoke_crash"}.

Circuit breakers enforced here (Phase 5 plan §CRITICAL circuit-breakers):

  #1  Diff-hash dedup               — reject when new diff == prior diff.
  #2  Log-hash regression           — reject when new build log == prior log
                                     (patch did nothing).
  #3  Hard 3-attempt cap per slot.
  #4  20k token per-node ceiling.
  #5  Tenant kill switch            — env var MOD_PATCH_LOOP_ENABLED=0.
  #6  Diff size sanity              — handled in port_failure_patch.
  #7  Credit pre-flight             — caller's job (variant_patch_dispatch).
  #8  Stage-separated budgets       — slot granularity (variant, stage).

The module is pure state manipulation: no LLM calls, no ImageBuilder
dispatch, no credit deduction. Callers compose.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ATTEMPT_CAP_PER_SLOT = 3
TOKEN_CAP_PER_NODE = 20_000
ALLOWED_STAGES = ("build", "smoke_drift", "smoke_crash")

# Names for the JSONB keys on NodeDefinition.custom_metadata. Callers
# use these constants rather than raw strings to keep drift in check.
KEY_ATTEMPTS = "patch_attempts"
KEY_META = "patch_attempts_meta"


@dataclass
class LoopDecision:
    """Outcome of `evaluate` — tells the caller whether to invoke the
    LLM, and why if not."""
    allowed: bool
    reason: str                   # "ok" | "disabled" | "cap" | "token_budget"
                                  # | "diff_dedup" | "log_regression" | "bad_stage"
    attempts_used: int            # n in the slot, post-record if recorded
    attempts_remaining: int       # max(0, CAP - n)
    token_total: int              # node-level cumulative (post-record)
    token_remaining: int          # max(0, TOKEN_CAP - token_total)
    detail: str = ""              # extra diagnostic string for logs


def _kill_switch_active() -> bool:
    """Env-controlled tenant kill switch. Ops flips this to "0" to
    stop the patch loop cluster-wide during incidents."""
    return os.getenv("MOD_PATCH_LOOP_ENABLED", "1").strip().lower() in (
        "0", "false", "no", "off",
    )


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _get_slot(
    custom_metadata: Optional[Dict[str, Any]],
    variant: str,
    stage: str,
) -> Dict[str, Any]:
    """Extract (or synthesize) the per-(variant, stage) slot dict from
    a NodeDefinition.custom_metadata blob. Returns a fresh empty slot
    when the path is absent or any nested value is corrupted (non-dict
    where a dict was expected — JSONB schema drift)."""
    cm = custom_metadata if isinstance(custom_metadata, dict) else {}
    attempts = cm.get(KEY_ATTEMPTS)
    attempts = attempts if isinstance(attempts, dict) else {}
    by_variant = attempts.get(variant)
    by_variant = by_variant if isinstance(by_variant, dict) else {}
    by_stage = by_variant.get(stage)
    by_stage = by_stage if isinstance(by_stage, dict) else {}
    return {
        "n": int(by_stage.get("n") or 0),
        "last_log_hash": str(by_stage.get("last_log_hash") or ""),
        "last_diff_hash": str(by_stage.get("last_diff_hash") or ""),
        "token_total": int(by_stage.get("token_total") or 0),
        "first_attempt_at": str(by_stage.get("first_attempt_at") or ""),
    }


def _get_node_totals(custom_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cm = custom_metadata if isinstance(custom_metadata, dict) else {}
    meta = cm.get(KEY_META)
    meta = meta if isinstance(meta, dict) else {}
    return {
        "token_total": int(meta.get("token_total") or 0),
        "first_attempt_at": str(meta.get("first_attempt_at") or ""),
    }


def evaluate(
    *,
    custom_metadata: Optional[Dict[str, Any]],
    variant: str,
    stage: str,
    proposed_log_hash: str,
    proposed_diff_hash: str,
    proposed_token_cost: int,
) -> LoopDecision:
    """Gate check — does the caller have budget to proceed? Returns a
    LoopDecision without mutating state (mutation happens in
    `record_attempt`).

    Callers:
      1. Call `evaluate` with the proposed patch's hashes + cost.
      2. If `decision.allowed`, proceed with the LLM call / apply.
      3. On completion (success or failure), call `record_attempt`
         with the actual final state.
    """
    if _kill_switch_active():
        return LoopDecision(
            allowed=False,
            reason="disabled",
            attempts_used=0,
            attempts_remaining=0,
            token_total=0,
            token_remaining=TOKEN_CAP_PER_NODE,
            detail="MOD_PATCH_LOOP_ENABLED=0 (tenant kill switch)",
        )

    if stage not in ALLOWED_STAGES:
        return LoopDecision(
            allowed=False,
            reason="bad_stage",
            attempts_used=0,
            attempts_remaining=0,
            token_total=0,
            token_remaining=TOKEN_CAP_PER_NODE,
            detail=f"stage {stage!r} not in {ALLOWED_STAGES}",
        )

    slot = _get_slot(custom_metadata, variant, stage)
    totals = _get_node_totals(custom_metadata)

    attempts_used = slot["n"]
    attempts_remaining = max(0, ATTEMPT_CAP_PER_SLOT - attempts_used)
    token_total = totals["token_total"]
    token_remaining = max(0, TOKEN_CAP_PER_NODE - token_total)

    if attempts_remaining <= 0:
        return LoopDecision(
            allowed=False,
            reason="cap",
            attempts_used=attempts_used,
            attempts_remaining=0,
            token_total=token_total,
            token_remaining=token_remaining,
            detail=(
                f"({variant}, {stage}) hit {ATTEMPT_CAP_PER_SLOT}-attempt "
                f"cap"
            ),
        )

    # Token-budget pre-flight — even an on-budget slot is blocked when
    # the node-level ceiling is full.
    if token_total + max(0, proposed_token_cost) > TOKEN_CAP_PER_NODE:
        return LoopDecision(
            allowed=False,
            reason="token_budget",
            attempts_used=attempts_used,
            attempts_remaining=attempts_remaining,
            token_total=token_total,
            token_remaining=token_remaining,
            detail=(
                f"proposed +{proposed_token_cost} tokens would exceed "
                f"{TOKEN_CAP_PER_NODE}-token node cap "
                f"(current {token_total})"
            ),
        )

    # Diff-hash dedup — refuse to re-apply an identical diff.
    if proposed_diff_hash and proposed_diff_hash == slot["last_diff_hash"]:
        return LoopDecision(
            allowed=False,
            reason="diff_dedup",
            attempts_used=attempts_used,
            attempts_remaining=attempts_remaining,
            token_total=token_total,
            token_remaining=token_remaining,
            detail=(
                "LLM returned the same diff as the last attempt — "
                "loop is stuck"
            ),
        )

    # Log-hash regression — the prior attempt's patch landed but
    # the rebuild failed with the *same* log. Patch did nothing.
    if proposed_log_hash and proposed_log_hash == slot["last_log_hash"] \
            and attempts_used > 0:
        return LoopDecision(
            allowed=False,
            reason="log_regression",
            attempts_used=attempts_used,
            attempts_remaining=attempts_remaining,
            token_total=token_total,
            token_remaining=token_remaining,
            detail=(
                "new build log hashes identical to prior attempt — "
                "patch had no effect"
            ),
        )

    return LoopDecision(
        allowed=True,
        reason="ok",
        attempts_used=attempts_used,
        attempts_remaining=attempts_remaining,
        token_total=token_total,
        token_remaining=token_remaining,
    )


def record_attempt(
    *,
    custom_metadata: Optional[Dict[str, Any]],
    variant: str,
    stage: str,
    log_hash: str,
    diff_hash: str,
    token_cost: int,
) -> Dict[str, Any]:
    """Mutate the custom_metadata dict shape to reflect one completed
    patch attempt. Returns the new dict — caller assigns back onto
    `node.custom_metadata` and commits.

    Does NOT re-check caps — caller is expected to have gated via
    `evaluate` first. Idempotency is the caller's responsibility
    (same HTTP PATCH retried twice WILL count as 2 attempts here).
    """
    cm = dict(custom_metadata) if isinstance(custom_metadata, dict) else {}
    raw_attempts = cm.get(KEY_ATTEMPTS)
    attempts = dict(raw_attempts) if isinstance(raw_attempts, dict) else {}
    raw_by_variant = attempts.get(variant)
    by_variant = dict(raw_by_variant) if isinstance(raw_by_variant, dict) else {}
    raw_by_stage = by_variant.get(stage)
    by_stage = dict(raw_by_stage) if isinstance(raw_by_stage, dict) else {}

    now_iso = _utcnow_iso()
    n_before = int(by_stage.get("n") or 0)
    slot = {
        "n": n_before + 1,
        "last_log_hash": str(log_hash or ""),
        "last_diff_hash": str(diff_hash or ""),
        "token_total": int(by_stage.get("token_total") or 0) + max(0, int(token_cost)),
        "first_attempt_at": str(by_stage.get("first_attempt_at") or now_iso),
    }
    by_variant[stage] = slot
    attempts[variant] = by_variant
    cm[KEY_ATTEMPTS] = attempts

    raw_meta = cm.get(KEY_META)
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    meta["token_total"] = int(meta.get("token_total") or 0) + max(0, int(token_cost))
    meta["first_attempt_at"] = str(meta.get("first_attempt_at") or now_iso)
    meta["last_attempt_at"] = now_iso
    cm[KEY_META] = meta
    return cm


def reset_slot(
    *,
    custom_metadata: Optional[Dict[str, Any]],
    variant: str,
    stage: Optional[str] = None,
) -> Dict[str, Any]:
    """Clear the retry counter for a slot (or for all stages of one
    variant when `stage is None`). Used when a successful smoke
    verification post-patch signals we can forgive prior attempts —
    operator hand-fixes and wants to re-enable the loop."""
    cm = dict(custom_metadata) if isinstance(custom_metadata, dict) else {}
    raw_attempts = cm.get(KEY_ATTEMPTS)
    attempts = dict(raw_attempts) if isinstance(raw_attempts, dict) else {}
    if variant not in attempts:
        return cm
    if stage is None:
        attempts.pop(variant, None)
    else:
        raw_by_variant = attempts[variant]
        by_variant = dict(raw_by_variant) if isinstance(raw_by_variant, dict) else {}
        by_variant.pop(stage, None)
        if by_variant:
            attempts[variant] = by_variant
        else:
            attempts.pop(variant, None)
    if attempts:
        cm[KEY_ATTEMPTS] = attempts
    else:
        cm.pop(KEY_ATTEMPTS, None)
    return cm


def summary(custom_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Admin-dashboard summary: total attempts, total tokens, per-slot
    counts. Read-only."""
    cm = custom_metadata or {}
    attempts = cm.get(KEY_ATTEMPTS) or {}
    meta = cm.get(KEY_META) or {}
    total_attempts = 0
    per_slot: Dict[str, int] = {}
    for v, by_variant in attempts.items():
        if not isinstance(by_variant, dict):
            continue
        for s, slot in by_variant.items():
            if not isinstance(slot, dict):
                continue
            n = int(slot.get("n") or 0)
            total_attempts += n
            per_slot[f"{v}.{s}"] = n
    return {
        "total_attempts": total_attempts,
        "token_total": int(meta.get("token_total") or 0),
        "token_remaining": max(
            0, TOKEN_CAP_PER_NODE - int(meta.get("token_total") or 0)
        ),
        "per_slot": per_slot,
        "first_attempt_at": meta.get("first_attempt_at") or "",
        "last_attempt_at": meta.get("last_attempt_at") or "",
        "kill_switch_active": _kill_switch_active(),
    }


__all__ = [
    "LoopDecision",
    "ATTEMPT_CAP_PER_SLOT",
    "TOKEN_CAP_PER_NODE",
    "ALLOWED_STAGES",
    "KEY_ATTEMPTS",
    "KEY_META",
    "evaluate",
    "record_attempt",
    "reset_slot",
    "summary",
]
