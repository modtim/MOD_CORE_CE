"""Phase 5 — self-healing variant patch dispatch endpoint.

    POST /node_definitions/{node_id}/variants/{variant}/patch

When a variant build / smoke probe fails, the caller (the variant-
status PATCH handler, post-P5.6) or an operator clicking "try to fix
automatically" in the wizard invokes this endpoint. The router
orchestrates the four stages of the Phase 5 patch loop:

    1. Load the node + resolve the last failing task's env.
    2. Gate via `patch_loop_state.evaluate`
       (kill switch, attempts cap, token budget, diff/log dedup).
    3. Pre-flight + deduct credits (PATCH_GENERATION ledger entry).
    4. Run `propose_patch` (LLM).
    5. Enqueue a new single-variant image_builder task with
       `INLINE_PATCH_DIFF` carrying the unified diff.

The router NEVER retries on its own — one invocation = one attempt.
Callers (the variant-status PATCH hook) run the bounded-retry logic
by calling this endpoint multiple times across subsequent failures.

Returns (200):
    {
        "dispatched": true,
        "task_id": int,
        "attempts_used": int,
        "attempts_remaining": int,
        "token_total": int,
        "token_remaining": int,
        "diff_hash": str,
        "confidence": float,
        "source": "llm" | "cache",
        "target_files": [str, ...]
    }

Refused (200, no credit charged beyond the refund):
    {
        "dispatched": false,
        "refuse_reason": str,
        "reason_code": "disabled" | "cap" | "token_budget" | "diff_dedup"
                       | "log_regression" | "bad_stage" | "llm_refused",
        "attempts_used": int,
        "attempts_remaining": int,
        "token_total": int,
        "token_remaining": int
    }
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel as PydanticBaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.variant_patch_dispatch_service import (
    get_node_definition,
    list_recent_tenant_tasks,
)
from utils import patch_loop_state
from utils.port_failure_patch import propose_patch, diff_hash

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["NodeDefinitions — Patch Loop"],
    prefix="/node-definitions",
)

# Credit cost per patch proposal — a tenant-visible line item. Keep
# small; the 20k token ceiling in patch_loop_state is the real
# hardstop. Operator tunable via MOD_PATCH_CREDIT_COST.
def _credit_cost_per_patch() -> int:
    try:
        return max(0, int(os.getenv("MOD_PATCH_CREDIT_COST", "5") or 5))
    except (TypeError, ValueError):
        return 5


def _charge_patch_credit(db: Session, *, tenant_id: int, amount: int,
                         user_id, notes: str, metadata: dict) -> tuple:
    """CARVE-U7 — the PATCH_GENERATION deduct, edition-safe and FAIL-CLOSED.

    utils/credit_manager.py is DENIED by open-core/manifest.yaml; the CE
    overlay ships a no-op stub ("Core has no credit system"), so on CE the
    ledger is not merely absent — nothing is ever charged and balances are
    unlimited. Absent module => (True, None): skip the charge, the feature
    still runs (CE offers no credit system at all).

    Any OTHER error is NOT swallowed: the old site caught broad Exception,
    logged "non-fatal" and ENQUEUED THE PATCH UN-CHARGED — a fail-open on
    the paid tree (an insufficient-credits or DB failure silently billed
    nothing). Here an error answers (False, reason) so the caller REFUSES
    the dispatch, matching the surrounding refuse shape. No exception text
    reaches the response.
    """
    from utils.paid_import import optional_paid
    _cm = optional_paid("utils.credit_manager")
    if _cm is None:
        return True, None
    try:
        outcome = _cm.CreditManager.deduct_credits(
            db=db,
            tenant_id=tenant_id,
            amount=amount,
            user_id=user_id,
            notes=notes,
            allow_advance=True,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "patch dispatch credit deduction errored — refusing the dispatch"
        )
        return False, "credit deduction failed"
    if isinstance(outcome, tuple):
        ok, err = bool(outcome[0]), outcome[1] if len(outcome) > 1 else None
    else:
        ok, err = bool(outcome), None
    return ok, err


class PatchDispatchRequest(PydanticBaseModel):
    """Body for POST /.../variants/{v}/patch."""
    stage: str = Field(
        ...,
        description='One of "build", "smoke_drift", "smoke_crash".',
    )
    log_tail: str = Field(
        ...,
        description="Worker log_tail for the failing build/smoke run.",
    )
    source_files: List[Dict[str, str]] = Field(
        ...,
        description=(
            "Post-port source tree for this variant: list of "
            "`{filename, body}` dicts."
        ),
    )
    port_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional PortPlan.port_report dict for prompt context.",
    )
    prior_explanation: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional output of port_failure_explain on this failure — "
            "enriches the LLM prompt."
        ),
    )
    porter_version: str = Field(default="v1")
    # Rebuild env to reuse — the caller typically hands the last
    # image_builder task's container_env (stripped of secrets) so we
    # don't need to re-derive it here. Required because the router
    # doesn't have ambient access to the original wizard payload.
    rebuild_env: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Container env for the rebuild image_builder task. When "
            "omitted, the router attempts to pull the most recent "
            "image_builder task's env for this tenant + node_name."
        ),
    )


def _compose_log_hash(log_tail: str) -> str:
    """Mirror port_failure_explain._log_hash's 32-char SHA-256 slice.
    Keeps the patch loop's log-hash contract aligned with what the
    explainer caches on."""
    import hashlib
    h = hashlib.sha256()
    h.update((log_tail or "")[:4096].encode("utf-8", errors="replace"))
    return h.hexdigest()[:32]


def _find_last_build_env(
    db: Session, tenant_id: int, node_name: str,
) -> Optional[Dict[str, str]]:
    """Best-effort lookup of the most recent image_builder task env
    for this (tenant, node_name). Used when the caller didn't supply
    `rebuild_env` explicitly."""
    # Filter on node_kind_label inside task_payload JSONB; fall back to a
    # node_id match when JSONB filtering isn't available.
    rows = list_recent_tenant_tasks(db, tenant_id=tenant_id)
    for row in rows:
        payload = row.task_payload or {}
        node_kind = payload.get("node_kind") if isinstance(payload, dict) else None
        if not node_kind or "image_builder" not in str(node_kind):
            continue
        container_env = payload.get("container_env") or {}
        if not isinstance(container_env, dict):
            continue
        if container_env.get("NODE_NAME") != node_name:
            continue
        return dict(container_env)
    return None


@router.post("/{node_id}/variants/{variant}/patch")
def dispatch_patch(
    node_id: int,
    variant: str,
    body: PatchDispatchRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    # -----------------------------------------------------------------
    # 1. Load + authorise the node.
    # -----------------------------------------------------------------
    node = get_node_definition(db, node_id=node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if node.tenant_id is not None and node.tenant_id != current_user.tenant_id \
            and not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=403,
            detail="Patch dispatch requires ownership of this node",
        )

    # -----------------------------------------------------------------
    # 2. Propose the patch FIRST so we can gate on its hashes + cost.
    #    The gate ordering matters: we pay the LLM token cost before
    #    knowing the final shape, but the patch_loop_state pre-flight
    #    has already rejected obviously-doomed requests (cap exceeded,
    #    kill switch, etc.) via a cheap check below.
    # -----------------------------------------------------------------
    log_hash = _compose_log_hash(body.log_tail)

    # Cheap gates — attempt cap + kill switch only. Actual diff/log
    # dedup runs AFTER the LLM call (we need the proposed diff hash).
    pre_decision = patch_loop_state.evaluate(
        custom_metadata=node.custom_metadata,
        variant=variant,
        stage=body.stage,
        proposed_log_hash=log_hash,
        proposed_diff_hash="",              # unknown yet — skip dedup
        proposed_token_cost=0,              # unknown yet — skip budget
    )
    if not pre_decision.allowed and pre_decision.reason in (
        "disabled", "cap", "bad_stage", "log_regression",
    ):
        return {
            "dispatched": False,
            "reason_code": pre_decision.reason,
            "refuse_reason": pre_decision.detail,
            "attempts_used": pre_decision.attempts_used,
            "attempts_remaining": pre_decision.attempts_remaining,
            "token_total": pre_decision.token_total,
            "token_remaining": pre_decision.token_remaining,
        }

    # Collect prior diff hashes so the LLM prompt can avoid repetition.
    prior_diff_hashes: List[str] = []
    try:
        slot = ((node.custom_metadata or {}).get(patch_loop_state.KEY_ATTEMPTS)
                or {}).get(variant, {}).get(body.stage, {})
        last = slot.get("last_diff_hash") if isinstance(slot, dict) else None
        if last:
            prior_diff_hashes.append(str(last))
    except Exception:
        pass

    proposal = propose_patch(
        stage=body.stage,
        log_tail=body.log_tail,
        variant=variant,
        source_files=body.source_files,
        port_report=body.port_report,
        prior_explanation=body.prior_explanation,
        prior_diff_hashes=prior_diff_hashes,
        porter_version=body.porter_version,
    )

    # -----------------------------------------------------------------
    # 3. LLM refused / produced something unusable — short-circuit.
    #    No credit charge (we didn't actually do useful work).
    # -----------------------------------------------------------------
    if proposal["source"] == "none":
        return {
            "dispatched": False,
            "reason_code": "llm_refused",
            "refuse_reason": proposal.get("refuse_reason") or "LLM produced no patch",
            "attempts_used": pre_decision.attempts_used,
            "attempts_remaining": pre_decision.attempts_remaining,
            "token_total": pre_decision.token_total,
            "token_remaining": pre_decision.token_remaining,
            "confidence": proposal.get("confidence", 0.0),
        }

    d_hash = diff_hash(proposal["diff"])
    token_cost = int(proposal.get("token_cost") or 0)

    # -----------------------------------------------------------------
    # 4. Final gate with the proposed hashes + token cost.
    # -----------------------------------------------------------------
    final = patch_loop_state.evaluate(
        custom_metadata=node.custom_metadata,
        variant=variant,
        stage=body.stage,
        proposed_log_hash=log_hash,
        proposed_diff_hash=d_hash,
        proposed_token_cost=token_cost,
    )
    if not final.allowed:
        return {
            "dispatched": False,
            "reason_code": final.reason,
            "refuse_reason": final.detail,
            "attempts_used": final.attempts_used,
            "attempts_remaining": final.attempts_remaining,
            "token_total": final.token_total,
            "token_remaining": final.token_remaining,
            "diff_hash": d_hash,
            "confidence": proposal.get("confidence", 0.0),
        }

    # -----------------------------------------------------------------
    # 5. Credit pre-flight (deduct BEFORE enqueue so an enqueue failure
    #    refunds cleanly). Cache hits cost 0 tokens and we still charge
    #    the fixed per-patch fee to keep accounting predictable; ops
    #    can override via MOD_PATCH_CREDIT_COST=0 to make cache hits
    #    free.
    # -----------------------------------------------------------------
    credit_cost = _credit_cost_per_patch()
    refund_needed = False
    if credit_cost > 0 and node.tenant_id is not None:
        ok, err = _charge_patch_credit(
            db,
            tenant_id=node.tenant_id,
            amount=credit_cost,
            user_id=current_user.id,
            notes=(
                f"P5 patch dispatch: node={node.name} variant={variant} "
                f"stage={body.stage} source={proposal['source']}"
            ),
            metadata={
                "entry_type_override": "patch_generation",
                "variant": variant,
                "stage": body.stage,
                "diff_hash": d_hash,
            },
        )
        if not ok:
            return {
                "dispatched": False,
                "reason_code": "credit_insufficient",
                "refuse_reason": err or "credit deduction failed",
                "attempts_used": final.attempts_used,
                "attempts_remaining": final.attempts_remaining,
                "token_total": final.token_total,
                "token_remaining": final.token_remaining,
                "diff_hash": d_hash,
                "confidence": proposal.get("confidence", 0.0),
            }
        refund_needed = True  # refund on subsequent failure

    # -----------------------------------------------------------------
    # 6. Resolve rebuild env + enqueue.
    # -----------------------------------------------------------------
    rebuild_env: Dict[str, str] = dict(body.rebuild_env or {})
    if not rebuild_env:
        resolved = _find_last_build_env(
            db, tenant_id=node.tenant_id, node_name=node.name,
        )
        if resolved:
            rebuild_env = resolved
    if not rebuild_env:
        # Refund the credit we just charged — the caller can't execute.
        if refund_needed:
            _refund_patch_credit(
                db, node.tenant_id, credit_cost, node.name, variant, body.stage,
            )
        return {
            "dispatched": False,
            "reason_code": "no_rebuild_env",
            "refuse_reason": (
                "No rebuild_env supplied and no prior image_builder "
                "task found for this node"
            ),
            "attempts_used": final.attempts_used,
            "attempts_remaining": final.attempts_remaining,
            "token_total": final.token_total,
            "token_remaining": final.token_remaining,
            "diff_hash": d_hash,
            "confidence": proposal.get("confidence", 0.0),
        }

    rebuild_env = dict(rebuild_env)
    rebuild_env["INLINE_PATCH_DIFF"] = proposal["diff"]
    rebuild_env["INLINE_PATCH_DIFF_VARIANT"] = variant
    # Carry the patch trace-id so downstream telemetry can correlate
    # the rebuild task back to the dispatch call.
    rebuild_env["MOD_PATCH_DIFF_HASH"] = d_hash

    # Phase 9 — forward the node's calibration config (if any) so the
    # Containerizer's intel_npu recipe splices custom dataset flags
    # into the optimum-cli export step. Skipped for non-NPU variants
    # and when no calibration has been attached to this node.
    try:
        calib = (node.custom_metadata or {}).get("calibration_inputs")
        if calib and isinstance(calib, dict):
            rebuild_env["INLINE_CALIBRATION_CONFIG"] = json.dumps(calib)
    except Exception:
        logger.debug("patch dispatch: calibration forward failed", exc_info=True)

    try:
        from routers.node_definitions import _enqueue_container_task
        new_task_id = _enqueue_container_task(
            db,
            tenant_id=node.tenant_id,
            image="imagebuilder:latest",
            env=rebuild_env,
            node_kind_label="image_builder_patch_retry",
        )
    except Exception:
        logger.exception("patch dispatch: image_builder enqueue failed")
        if refund_needed:
            _refund_patch_credit(
                db, node.tenant_id, credit_cost, node.name, variant, body.stage,
            )
        raise HTTPException(
            status_code=500,
            detail="Failed to enqueue rebuild task; credit refunded",
        )

    # -----------------------------------------------------------------
    # 7. Record the attempt + commit the node.
    # -----------------------------------------------------------------
    new_cm = patch_loop_state.record_attempt(
        custom_metadata=node.custom_metadata,
        variant=variant,
        stage=body.stage,
        log_hash=log_hash,
        diff_hash=d_hash,
        token_cost=token_cost,
    )
    node.custom_metadata = new_cm or None
    db.commit()

    logger.info(
        "P5 patch dispatch: node=%s variant=%s stage=%s task_id=%d "
        "source=%s confidence=%.2f tokens=%d",
        node.name, variant, body.stage, new_task_id,
        proposal["source"], proposal.get("confidence", 0.0), token_cost,
    )

    # Fresh decision post-record so the caller sees accurate counts.
    post_decision = patch_loop_state.evaluate(
        custom_metadata=new_cm,
        variant=variant,
        stage=body.stage,
        proposed_log_hash="",
        proposed_diff_hash="",
        proposed_token_cost=0,
    )
    return {
        "dispatched": True,
        "task_id": new_task_id,
        "attempts_used": post_decision.attempts_used,
        "attempts_remaining": post_decision.attempts_remaining,
        "token_total": post_decision.token_total,
        "token_remaining": post_decision.token_remaining,
        "diff_hash": d_hash,
        "confidence": proposal.get("confidence", 0.0),
        "source": proposal["source"],
        "target_files": proposal.get("target_files") or [],
    }


def _refund_patch_credit(
    db: Session, tenant_id: Optional[int], amount: int,
    node_name: str, variant: str, stage: str,
) -> None:
    """Best-effort refund path for a deduct-then-fail flow. Mirrors
    the port-failure refund helper in credit_manager; failure here is
    logged but never raised."""
    if tenant_id is None or amount <= 0:
        return
    # CARVE-U7 — utils/credit_manager.py is denied on the CE carve (the
    # overlay ships a no-op stub, and with no ledger there is nothing to
    # refund). Absent => skip. A PRESENT module that raises stays a
    # logged, non-fatal refund miss (this helper's documented contract:
    # "failure here is logged but never raised") — refund is compensation
    # for a charge already refused/undone, never an enforcement gate.
    from utils.paid_import import optional_paid
    _cm = optional_paid("utils.credit_manager")
    if _cm is None:
        return
    try:
        _cm.CreditManager.refund_credits_for_port_failure(
            db=db,
            tenant_id=tenant_id,
            amount=amount,
            node_name=node_name,
            n_failed=1,
            n_attempted=1,
        )
        logger.info(
            "P5 patch refund: tenant=%s amount=%d variant=%s stage=%s",
            tenant_id, amount, variant, stage,
        )
    except Exception:
        logger.exception("P5 patch refund failed (non-fatal)")


@router.get("/{node_id}/variants/{variant}/patch/status")
def get_patch_loop_status(
    node_id: int,
    variant: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    """Quick status probe — the wizard's failure modal polls this to
    render "Attempts: 2/3 — 14,000 tokens remaining" telemetry."""
    node = get_node_definition(db, node_id=node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if node.tenant_id is not None and node.tenant_id != current_user.tenant_id \
            and not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Not your node")

    summary = patch_loop_state.summary(node.custom_metadata)
    # Narrow the view to this (variant) — per-slot counts are returned
    # as-is (keyed by "variant.stage") so the UI can pick what to show.
    return {
        "node_id": node.id,
        "variant": variant,
        "summary": summary,
    }
