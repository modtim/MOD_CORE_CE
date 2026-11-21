"""Business logic for the CE-eligible slice of the `node_authoring` domain
(openspec: stack-training-and-prewarm, Gate 2 / D9; CE-launch Gate 4
decision: "CE fanout; marketplace/team-sharing business logic is the non-CE slice").

Companion to `crud/node_authoring.py` (the ONLY place this domain's
`db.query` lives). The router (`routers/node_definitions_domains/
node_authoring.py`) stays HTTP-only: parse input, call a function here,
map a `ServiceError` to `HTTPException`, shape the response.

Extracted verbatim from the router during the Gate-2 migration —
behaviour-preserving MOVE. Every function below is the same logic that
used to live inline in the router handler of the same name; only the
data access now goes through `crud.node_authoring` and error raising
goes through `ServiceError` subclasses instead of bare `HTTPException`.

404-vs-403: fetches stay tenant-UN-scoped in `crud.node_authoring`;
tenant/ownership policy is applied HERE so a legit 403 doesn't collapse
into a 404 (SHARED RULES).

Non-CE marketplace / tenant-team-sharing business logic (publish/curate/
browse-gate, team grants, packages, try-before-you-buy) moved OUT of this
file entirely to `services/node_marketplace_paid.py` — CE does not ship
it, and nothing below depends on it.

Build-budget/credit-charge enforcement (the node-creation charge +
the P9v build-failure credit refund) moved to `services/
node_build_budget_paid.py`. This file still needs those two call sites
(inside `create_or_update_node_definition` and `patch_variant_status`),
so it imports that module behind a guarded `try/except ImportError` (see
`_budget_paid` below): present (every build today) => charge/refund
exactly as before the split; absent (a hypothetical CE checkout without
that file) => the step is skipped and node creation proceeds unmetered.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified as _flag_modified

import models
import schemas
from crud import node_authoring as node_crud
from routers.node_definitions_domains import _shared as sh
from services._base import ConflictError, NotFoundError, ServiceError
from utils import node_cost as _node_cost


# =============================================================================
# Local error types (services/_base.py has not_found/conflict but no
# forbidden/bad_request/etc — SHARED RULES: declare what's needed here).
# =============================================================================


class ForbiddenError(ServiceError):
    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


class UnprocessableError(ServiceError):
    def __init__(self, message: str = "Unprocessable", *, details: Any = None):
        super().__init__(message, code="unprocessable_entity", details=details)


class UnauthorizedError(ServiceError):
    def __init__(self, message: str = "Unauthorized", *, details: Any = None):
        super().__init__(message, code="unauthorized", details=details)


class ServiceUnavailableError(ServiceError):
    def __init__(self, message: str = "Unavailable", *, details: Any = None):
        super().__init__(message, code="unavailable", details=details)


# Router-side code -> HTTP status map. Kept alongside the errors so the
# router's `_raise_http` is a one-liner. Mirrors api_conventions'
# code_for_status but expressed the other direction (code -> status),
# since these services raise pre-classified errors, not raw statuses.
STATUS_BY_CODE = {
    "not_found": 404,
    "conflict": 409,
    "forbidden": 403,
    "bad_request": 400,
    "unprocessable_entity": 422,
    "unauthorized": 401,
    "unavailable": 503,
}


# =============================================================================
# Non-CE build-budget module (CE-launch Gate 4) — optional import. See the
# module docstring above. `_budget_paid is None` in a CE checkout that
# omits `services/node_build_budget_paid.py`; every call site below guards
# on it before charging/refunding.
# =============================================================================
try:
    from services import node_build_budget_paid as _budget_paid
except ImportError:  # pragma: no cover — exercised by the CE-absence smoke test
    _budget_paid = None


# =============================================================================
# Build spec / variant lifecycle
# =============================================================================


def get_build_spec(db, *, spec_id: int, auth) -> Any:
    sh.assert_build_scope(auth, build_spec_id=spec_id)
    rec = node_crud.get_build_spec_by_id(db, spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    if auth.via == "build_token" and rec.tenant_id != auth.tenant_id:
        raise ForbiddenError("tenant scope")
    return rec.spec_json


def get_build_spec_context(db, *, spec_id: int, auth):
    sh.assert_build_scope(auth, build_spec_id=spec_id)
    rec = node_crud.get_build_spec_by_id(db, spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    if auth.via == "build_token" and rec.tenant_id != auth.tenant_id:
        raise ForbiddenError("tenant scope")
    spec = rec.spec_json or {}
    art = spec.get("context_artifact")
    if not art or not art.get("bucket") or not art.get("object_key"):
        raise NotFoundError("build has no context artifact")
    from utils.storage import get_service_client
    mc = get_service_client()
    try:
        obj = mc.get_object(art["bucket"], art["object_key"])
    except Exception as e:  # noqa: BLE001
        raise NotFoundError(f"context artifact missing: {e}")
    from fastapi.responses import StreamingResponse as _Streaming
    return _Streaming(
        obj.stream(64 * 1024),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{art.get("filename", "context.tar.gz")}"'},
    )


def _rebuild_hero_push_context(rec) -> Optional[Dict[str, Any]]:
    """node-code-edit-rebuild (node-version-pinning GA.NMF.F8) — the context a
    rebuild-digest recording needs, or None when this spec is not a
    single-variant rebuild of a known node.

    ``services.node_build_dispatch.seed_spec_from_revision`` stamps
    ``rebuild_node_id`` + ``rebuild_scope`` onto the persisted spec. Only a
    ``single_variant`` rebuild needs this gap closed: the full-fanout rebuild
    re-registers the node (its DAG keeps the register stage), and non-rebuild
    builds (wizard, build farm, raw-context, …) never set these fields, so
    they are never touched here.
    """
    spec_data = rec.spec_json or {}
    rebuild_node_id = spec_data.get("rebuild_node_id")
    if not rebuild_node_id:
        return None
    if (spec_data.get("rebuild_scope") or "") != "single_variant":
        return None
    return {"node_id": int(rebuild_node_id)}


def _handle_rebuild_digest_recording(db, rec, spec_id, request) -> None:
    """node-code-edit-rebuild (node-version-pinning GA.NMF.F8) — record a
    single-variant rebuild's pushed image digest for the rebuilt node.

    The single-variant rebuild DAG (``node_builder.build_dag.generator.
    generate_single_variant_rebuild_dag``) is ``build → push → sample-test``:
    it omits the register/manifest stages, so without this the rebuilt image's
    digest never reaches ``NodeDefinition.image_digest`` and no
    ``node_versions`` row is appended — pinning's "update available" never
    fires and new runs can't stamp the new digest. This closes that gap,
    reusing the same writer the legacy ImageBuilder path uses
    (``set_node_manifest_metadata`` → ``record_node_version``).

    Mirrors how the full-build register path decides. In the full build DAG the
    ``register`` node only fires once the HERO sample-test passes
    (``register_deps`` = ``[sample-test-hero]``), so a node's
    ``image_digest`` is written only for a verified hero push. We reproduce
    that gate across two stage results that both arrive here
    (``post_stage_result``):

      1. ``push``/``push_variant`` SUCCESS for a rebuild hero push → stash the
         pushed digest on the spec (``rebuild_pushed_digest``). A failed push
         carries no digest and stashes nothing.
      2. ``sample_test_variant`` SUCCESS for a rebuild hero sample-test → read
         the stashed digest and record it: ``image_digest = digest`` +
         ``record_node_version`` (a new append-only ``node_versions`` row),
         then clear the stash. A failed hero sample-test records nothing —
         matching register never firing when the hero publish-gate fails.

    Only the HERO variant is recorded: a single-variant rebuild pushes exactly
    one variant, and its ``:latest`` tag (``variant == "hero"`` — see
    ``build_dag/submit`` name_to_stage + ``register_node.py``
    ``executable_path = ...:latest``) is the one that maps to
    ``NodeDefinition.image_digest``. A non-hero accelerator variant rebuild
    (``:latest-<variant>``) is never the node's primary image, so it is
    ignored here and the node's digest is left unchanged.

    Idempotent: ``record_node_version`` returns the existing row when the
    digest is unchanged, so re-pushing an identical image adds no version row.
    Never raises — a digest-recording hiccup must not break the stage
    acknowledgement. ``manifest_ref`` is intentionally left alone (rebuilds
    emit no signed manifest; only ``image_digest`` moves).
    """
    variant = (request.variant or "hero")
    if variant != "hero":
        return
    ctx = _rebuild_hero_push_context(rec)
    if ctx is None:
        return

    if request.stage in ("push", "push_variant"):
        if not request.success:
            return
        digest = ((request.payload or {}).get("digest") or "").strip()
        if not digest:
            return
        spec_data = dict(rec.spec_json or {})
        spec_data["rebuild_pushed_digest"] = digest
        rec.spec_json = spec_data
        _flag_modified(rec, "spec_json")
        try:
            db.commit()
        except Exception:  # noqa: BLE001 — non-fatal
            sh.logger.exception(
                "rebuild digest: stashing pushed digest failed for spec=%d — non-fatal",
                spec_id,
            )
        return

    if request.stage == "sample_test_variant":
        if not request.success:
            return
        digest = ((rec.spec_json or {}).get("rebuild_pushed_digest") or "").strip()
        if not digest:
            sh.logger.warning(
                "rebuild digest: hero sample-test passed but no pushed digest "
                "stashed for spec=%d; not recording", spec_id,
            )
            return
        try:
            nd = node_crud.get_node_definition_unscoped(db, ctx["node_id"])
            if nd is None:
                sh.logger.warning(
                    "rebuild digest: node %s (spec=%d) not found; not recording",
                    ctx["node_id"], spec_id,
                )
                return
            if (nd.image_digest or "").strip() == digest:
                # Unchanged digest — still clear the stale stash, but no write.
                spec_data = dict(rec.spec_json or {})
                if spec_data.pop("rebuild_pushed_digest", None) is not None:
                    rec.spec_json = spec_data
                    _flag_modified(rec, "spec_json")
                    db.commit()
                return
            nd.image_digest = digest
            sh.crud.record_node_version(db, nd)
            spec_data = dict(rec.spec_json or {})
            spec_data.pop("rebuild_pushed_digest", None)
            rec.spec_json = spec_data
            _flag_modified(rec, "spec_json")
            db.commit()
            sh.logger.info(
                "rebuild digest: node %s image_digest -> %s (spec=%d variant=hero)",
                nd.id, digest[:19], spec_id,
            )
        except Exception:  # noqa: BLE001 — non-fatal: ack the stage regardless
            sh.logger.exception(
                "rebuild digest recording failed for spec=%d — non-fatal", spec_id,
            )


def post_stage_result(db, *, spec_id: int, request, auth) -> Dict[str, Any]:
    sh.assert_build_scope(auth, build_spec_id=spec_id)
    rec = node_crud.get_build_spec_by_id(db, spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    sh.logger.info(
        "build-spec stage-result: spec=%d stage=%s variant=%s ok=%s",
        spec_id, request.stage, request.variant, request.success,
    )

    # Item #3 (fullproof extension) — auto-bump tenant harbor quota
    # on a push failure caused by storage-exceeded.
    if not request.success and request.stage in ("push", "push_variant"):
        try:
            stderr = (request.payload or {}).get("stderr_tail", "")
            import re as _re
            m = _re.search(
                r"denied:\s*(?:adding|subtracting)\s+([\d.]+)\s*(GiB|MiB|KiB|B)\s+of\s+storage\s+resource.*"
                r"current\s+usage\s+of\s+([\d.]+)\s*\w+.*"
                r"upper\s+limit\s+of\s+([\d.]+)\s*\w+",
                stderr, _re.IGNORECASE | _re.DOTALL,
            )
            if m and rec.tenant_id:
                _adding = float(m.group(1))
                _current = float(m.group(3))
                _limit = float(m.group(4))
                _needed_gib = (_current + _adding)
                _new_limit_gib = max(50, int(_needed_gib * 2))
                from utils.paid_import import optional_paid
                _thc_mod = optional_paid("utils.tenant_harbor_client")
                if _thc_mod is None:
                    sh.logger.info(
                        "CE carve: utils.tenant_harbor_client not installed — harbor quota "
                        "auto-bump skipped for tenant=%d (would be %d GiB)",
                        rec.tenant_id, _new_limit_gib,
                    )
                else:
                    ok = _thc_mod.TenantHarborClient().set_project_quota(
                        rec.tenant_id, _new_limit_gib * 1024 * 1024 * 1024,
                    )
                    if ok:
                        sh.logger.warning(
                            "harbor quota auto-bump: tenant=%d limit=%.1f→%d GiB (push needed "
                            "%.2f+%.2f=%.2f GiB > old limit %.1f). Retrying push.",
                            rec.tenant_id, _limit, _new_limit_gib,
                            _current, _adding, _needed_gib, _limit,
                        )
                    else:
                        sh.logger.warning(
                            "harbor quota auto-bump: tenant=%d set_project_quota returned False",
                            rec.tenant_id,
                        )
        except Exception:  # noqa: BLE001
            sh.logger.exception("harbor quota auto-bump failed — non-fatal")

    # Move C — capture install recipes on smoke pass.
    if request.success and request.stage in (
        "smoke", "smoke_variant", "sample_test", "sample_test_variant",
        "test_and_repair", "test_and_repair_variant",
    ):
        try:
            from node_builder.install_recipes import capture_install_recipe
            spec_data = dict(rec.spec_json or {})
            variant = (request.variant or "hero").lower()
            if variant == "hero":
                df = spec_data.get("hero_dockerfile") or ""
                accel = (spec_data.get("process_type") or "CPU").lower()
            else:
                df = ""
                for v in (spec_data.get("variants") or []):
                    if isinstance(v, dict) and v.get("variant", "").lower() == variant:
                        df = v.get("dockerfile") or ""
                        break
                accel = variant
            if df:
                n_updated = capture_install_recipe(
                    db, tenant_id=rec.tenant_id, spec_data=spec_data, accelerator=accel, dockerfile=df,
                )
                if n_updated:
                    sh.logger.info(
                        "install_recipe: captured %d recipe(s) for spec=%d variant=%s accel=%s",
                        n_updated, spec_id, variant, accel,
                    )
        except Exception:  # noqa: BLE001
            sh.logger.exception(
                "install_recipe capture failed for spec=%d variant=%s — non-fatal",
                spec_id, request.variant,
            )

    # node-code-edit-rebuild (node-version-pinning GA.NMF.F8) — the
    # single-variant rebuild DAG omits the register/manifest stages, so its
    # pushed image digest would never reach NodeDefinition.image_digest +
    # node_versions. This records it (gated on hero push + hero sample-test,
    # mirroring the full-build register gate), reusing record_node_version.
    _handle_rebuild_digest_recording(db, rec, spec_id, request)

    return {"ok": True}


# GO.8 (23.7) — the on-change regression gate's variant-change trigger path.
# ``node_builder.variant_test.change_gate`` keys a "variant change" on the
# variant matrix moving (its TRIGGER_GLOBS half); a Node's ``gpu_variants``
# map IS that matrix, so every mutation below reports the change under this
# canonical path and the gate re-verifies ONLY the affected variants. Dark by
# default (``node_build.variant_change_gate_enabled``) inside the gate itself.
_VARIANT_MATRIX_PATH = "node_builder/build_dag/variant_strategy.py"


def _variant_change_gate(db, *, recipes=(), changed_paths=(), label=""):
    """Best-effort on-change gate hook (GO.8 23.7) for a variant/recipe save.

    ``change_gate.enqueue_on_change`` no-ops while the gate is dark and
    re-verifies only the affected (node, variant) pairs when it is on. Any
    failure — import, settings, DB — is logged and swallowed: the user's
    mutation already succeeded and must never be failed by the gate.
    """
    try:
        from node_builder.variant_test import change_gate as _change_gate
        summary = _change_gate.enqueue_on_change(
            db,
            changed_paths=tuple(changed_paths),
            recipes=tuple(recipes),
        )
        if summary.get("enqueued"):
            sh.logger.info(
                "change_gate: %s re-queued %d affected variant(s)",
                label, summary["enqueued"],
            )
    except Exception:  # noqa: BLE001 — the save already succeeded
        sh.logger.warning(
            "change_gate hook failed (non-fatal) for %s", label, exc_info=True,
        )


def sync_variants_from_hero(db, *, request, auth):
    sh.assert_build_scope(auth, build_spec_id=request.build_spec_id)
    rec = node_crud.get_build_spec_by_id(db, request.build_spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")

    spec_data = dict(rec.spec_json or {})
    mode = ((spec_data.get("build_plan") or {}).get("mode") or "").strip()

    hero_df = spec_data.get("hero_dockerfile") or ""
    if not hero_df:
        return sh._SyncVariantsFromHeroResponse(propagated=0, skipped_non_image=0)

    variants = list(spec_data.get("variants") or [])
    torch_markers = ("cu118", "cu121", "cu124", "rocm5", "/whl/cpu")

    def _has_torch_split(v_df: str) -> bool:
        return any(m in v_df for m in torch_markers) and \
            any(m not in hero_df for m in torch_markers if m in v_df)

    propagated = 0
    skipped = 0
    for v in variants:
        if not isinstance(v, dict):
            continue
        v_df = v.get("dockerfile") or ""
        if mode not in ("image", "dockerfile") and _has_torch_split(v_df):
            skipped += 1
            continue
        if v_df != hero_df:
            v["dockerfile"] = hero_df
            propagated += 1
    spec_data["variants"] = variants
    rec.spec_json = spec_data
    _flag_modified(rec, "spec_json")
    db.commit()
    # GO.8 (23.7) — the per-variant Dockerfiles just moved (hero propagated):
    # that IS a variant-definition change, so the on-change gate re-verifies
    # the affected variants. Best-effort; the sync already saved.
    if propagated:
        _variant_change_gate(
            db,
            changed_paths=(_VARIANT_MATRIX_PATH,),
            label="sync_variants_from_hero (variant dockerfiles)",
        )
    return sh._SyncVariantsFromHeroResponse(propagated=propagated, skipped_non_image=skipped)


def variant_verified(db, *, request, auth):
    sh.assert_build_scope(auth, build_spec_id=request.build_spec_id)

    spec_row = node_crud.get_build_spec_by_id(db, request.build_spec_id)
    if spec_row is None:
        raise NotFoundError("build_spec not found")

    sample_row = node_crud.get_verification_by_spec_and_variant(
        db, request.build_spec_id, request.variant,
    )
    nd_id = sample_row.node_definition_id if sample_row else None
    if nd_id is None:
        return sh._VariantVerifiedResponse(
            node_definition_id=None,
            variant=request.variant,
            verdict=request.verdict,
            gpu_variants_after={},
        )

    nd = node_crud.get_node_definition_unscoped(db, nd_id)
    if nd is None:
        raise NotFoundError("node_definition not found")

    gpu_variants = dict(nd.gpu_variants or {})
    if request.verdict in ("verified", "marginal", "drift"):
        if request.image_ref:
            gpu_variants[request.variant] = request.image_ref
        elif request.variant not in gpu_variants:
            base = (nd.executable_path or "").split(":")[0]
            if base:
                gpu_variants[request.variant] = f"{base}:latest-{request.variant}"
    elif request.verdict in ("failed", "incompatible"):
        gpu_variants.pop(request.variant, None)

    nd.gpu_variants = gpu_variants
    _flag_modified(nd, "gpu_variants")

    if sample_row.node_definition_id is None:
        sample_row.node_definition_id = nd_id

    db.commit()

    # GO.8 (23.7) — the variant matrix moved (a variant entered/left service):
    # the on-change gate re-verifies the affected variants. Best-effort.
    _variant_change_gate(
        db,
        changed_paths=(_VARIANT_MATRIX_PATH,),
        label="variant_verified (gpu_variants mutation)",
    )

    return sh._VariantVerifiedResponse(
        node_definition_id=nd_id,
        variant=request.variant,
        verdict=request.verdict,
        gpu_variants_after=gpu_variants,
    )


def build_spec_eta(db, *, spec_id: int, current_user):
    rec = node_crud.get_build_spec_by_id(db, spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    if rec.tenant_id != current_user.tenant_id and not getattr(
        current_user, "is_platform_admin", False,
    ):
        raise ForbiddenError("cross-tenant access denied")
    spec_data = rec.spec_json or {}
    plan = spec_data.get("build_plan") if isinstance(spec_data.get("build_plan"), dict) else {}
    variants = spec_data.get("variants") or []
    variant_count = 1 + (len(variants) if isinstance(variants, list) else 0)

    from node_builder.eta import estimate_eta, detect_stall
    eta_s = estimate_eta(plan or spec_data, variant_count=variant_count)

    stall_reason: Optional[str] = None
    if rec.workflow_run_id:
        wf = node_crud.get_workflow_run_by_id(db, rec.workflow_run_id)
        if wf is not None and wf.status:
            last_event = node_crud.get_last_workflow_run_event(db, wf.id)
            last_age_s: Optional[float] = None
            if last_event is not None and last_event.ts:
                last_age_s = (
                    datetime.now(timezone.utc) - last_event.ts
                ).total_seconds()
            stall_reason = detect_stall(str(wf.status), last_age_s)

    return sh._BuildSpecEtaResponse(
        spec_id=spec_id,
        eta_seconds=eta_s,
        eta_minutes=max(1, eta_s // 60),
        variant_count=variant_count,
        stall_reason=stall_reason,
    )


def retry_with_elevated_budget(db, *, request, current_user):
    rec = node_crud.get_build_spec_by_id(db, request.build_spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    if rec.tenant_id != current_user.tenant_id:
        raise ForbiddenError("cross-tenant access denied")
    spec_data = dict(rec.spec_json or {})

    bp = spec_data.get("build_plan")
    tier: str = "unknown"
    if isinstance(bp, dict) and bp.get("mode"):
        try:
            from node_builder.build_plan.types import BuildPlan as _BP
            tier = _BP(**bp).tier()
        except Exception:
            tier = "unknown"
    if tier == "low":
        raise BadRequestError(
            f"plan tier is {tier!r}; already at max budget. Submit a "
            "support ticket for operator review."
        )

    current_max = int(spec_data.get(
        "repair_attempts_outer_max",
        10 if tier == "unknown" else
        {"high": 3, "medium": 8, "low": 15}[tier],
    ))
    new_max = current_max + 15
    spec_data["repair_attempts_outer_max"] = new_max
    spec_data["repair_attempts_outer_used"] = 0
    spec_data["repair_attempts_inner_used"] = 0
    spec_data["infra_transient_retries_used"] = 0
    rec.spec_json = spec_data
    _flag_modified(rec, "spec_json")
    db.commit()
    return sh._RetryElevatedBudgetResponse(
        build_spec_id=request.build_spec_id,
        new_outer_max=new_max,
        outer_used_reset_to=0,
    )


def finalize_partial_publish(db, *, request, current_user):
    rec = node_crud.get_build_spec_by_id(db, request.build_spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    if rec.tenant_id != current_user.tenant_id:
        raise ForbiddenError("cross-tenant access denied")

    hero_row = node_crud.get_verification_by_spec_and_variant(
        db, request.build_spec_id, "hero",
    )
    if hero_row is None or hero_row.verdict not in ("verified", "marginal"):
        raise BadRequestError(
            "partial-publish requires hero sample-test to have passed "
            "the publish gate (verdict=verified or marginal); current "
            f"hero verdict is {hero_row.verdict if hero_row else 'none'!r}"
        )

    nd_id = hero_row.node_definition_id
    if nd_id is None:
        raise BadRequestError(
            "hero verification row not yet promoted to a NodeDefinition; "
            "wait for register to fire then retry.",
        )

    nd = node_crud.get_node_definition_unscoped(db, nd_id)
    if nd is None:
        raise NotFoundError("node_definition not found")

    rows = node_crud.list_verifications_for_node(db, nd_id)
    verified: List[str] = []
    skipped: List[str] = []
    new_gpu_variants = dict(nd.gpu_variants or {})
    for r in rows:
        if r.verdict in ("verified", "marginal", "drift"):
            verified.append(r.variant)
        else:
            skipped.append(r.variant)
            new_gpu_variants.pop(r.variant, None)
            r.verdict = "incompatible"
            if not r.scored_against_variant:
                r.scored_against_variant = "user-skipped"

    nd.gpu_variants = new_gpu_variants
    _flag_modified(nd, "gpu_variants")
    db.commit()

    # GO.8 (23.7) — partial publish dropped variants from the matrix: the
    # on-change gate re-verifies what the change can affect. Best-effort.
    if skipped:
        _variant_change_gate(
            db,
            changed_paths=(_VARIANT_MATRIX_PATH,),
            label="finalize_partial_publish (gpu_variants drop)",
        )

    return sh._FinalizePartialPublishResponse(
        node_definition_id=nd_id,
        verified_variants=verified,
        skipped_variants=skipped,
    )


def submit_wizard_support_ticket(db, *, request, current_user):
    rec = node_crud.get_build_spec_by_id(db, request.build_spec_id)
    if rec is None:
        raise NotFoundError("build_spec not found")
    if rec.tenant_id != current_user.tenant_id:
        raise ForbiddenError("cross-tenant access denied")
    spec_data = dict(rec.spec_json or {})
    history = list(spec_data.get("repair_history") or [])
    history_tail = history[-200:]

    ticket_payload: Dict[str, Any] = {
        "build_spec_id": request.build_spec_id,
        "tenant_id": rec.tenant_id,
        "user_id": current_user.id,
        "user_message": (request.user_message or "")[:2000],
        "build_plan": spec_data.get("build_plan"),
        "applied_deltas": (spec_data.get("build_plan") or {}).get("applied_deltas") or [],
        "repair_history_tail": history_tail,
        "source_repo_url": spec_data.get("source_repo_url"),
        "wrap_base_image": spec_data.get("wrap_base_image"),
        "inline_source_excerpt": (spec_data.get("inline_source_body") or "")[:2000],
        "node_name": spec_data.get("node_name"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    event = models.AuditEvent(
        tenant_id=rec.tenant_id,
        user_id=current_user.id,
        event_name="wizard_support_ticket",
        details=ticket_payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return sh._SupportTicketResponse(
        ticket_id=event.id,
        build_spec_id=request.build_spec_id,
    )


def finalize_variant_verification(db, *, request, auth):
    sh.assert_build_scope(auth, build_spec_id=request.build_spec_id)
    rows = node_crud.list_unfinalized_verifications_for_spec(db, request.build_spec_id)
    for r in rows:
        r.node_definition_id = request.node_definition_id
    db.commit()
    return sh._FinalizeVerificationResponse(promoted=len(rows))


def _incumbent_rows(rows):
    """One row per variant: highest best_score, tie → most recent scored_at.

    23.1a — a per-variant "fix my code" re-verify appends a fresh
    ``node_variant_verification`` row for a variant that already has one
    (different build_spec_id), so ``list_verifications_for_node`` can return
    several rows for the same variant. Every surface that reads this document
    (the verdict list, the node card chips, the drift-ruling lookup) expects
    one row per variant, so the incumbent wins — the same rule
    ``variant_test.dispatcher.record_variant_test`` applies within a row.
    """
    def _score_of(r) -> float:
        return float(r.best_score if r.best_score is not None else r.score or 0.0)

    def _scored_rank(r) -> int:
        # scored_at is tz-aware from either writer, but a hand-seeded row can
        # carry None; None never beats a timestamp. Rank by epoch seconds so
        # naive/aware mixing can never raise.
        ts = r.scored_at
        if ts is None:
            return -1
        try:
            return int(ts.timestamp())
        except Exception:  # noqa: BLE001
            return -1

    best: Dict[str, Any] = {}
    for r in rows:
        incumbent = best.get(r.variant)
        if incumbent is None or (_score_of(r), _scored_rank(r)) > (
            _score_of(incumbent), _scored_rank(incumbent)
        ):
            best[r.variant] = r
    order = {id(r): i for i, r in enumerate(rows)}
    return sorted(best.values(), key=lambda r: order[id(r)])


def get_variant_verification(db, *, node_def_id: int, current_user):
    nd = node_crud.get_node_definition_unscoped(db, node_def_id)
    if nd is None:
        raise NotFoundError("node definition not found")
    if nd.tenant_id is not None and nd.tenant_id != current_user.tenant_id:
        raise ForbiddenError("cross-tenant access denied")
    rows = node_crud.list_verifications_for_node(db, node_def_id)
    # 23.1a — "fix my code": a single-variant rebuild appends a NEW
    # node_variant_verification row (its own build_spec_id) for a variant the
    # node already has a row for, so the list can carry several rows per
    # variant. The UI re-reads exactly this document after the re-verify and
    # must show ONE row per variant, updated in place — so collapse to the
    # incumbent, same best-attempt rule the dispatcher uses for one row:
    # highest best_score, tie broken by the most recent scored_at. Every
    # attempt still exists in the DB; attempt_history keeps the regressions.
    # Other variants are untouched by this — the collapse is per variant.
    rows = _incumbent_rows(rows)
    reported = {r.variant for r in rows}
    expected: set = set()
    backlink_row = next(
        (r for r in rows if r.build_spec_id is not None),
        None,
    )
    if backlink_row is not None:
        spec_rec = node_crud.get_build_spec_by_id(db, backlink_row.build_spec_id)
        if spec_rec is not None:
            sj = spec_rec.spec_json or {}
            hero_key = (sj.get("hero_variant") or "hero").strip()
            expected.add(hero_key or "hero")
            for v in (sj.get("variants") or []):
                if isinstance(v, dict) and v.get("variant"):
                    expected.add(v["variant"])
    if not expected:
        expected = set((nd.gpu_variants or {}).keys())
    pending = sorted(expected - reported)
    return sh._GetVariantVerificationResponse(
        node_definition_id=node_def_id,
        reference_variant=nd.reference_variant or "hero",
        variants=[
            sh._VariantVerificationRow(
                variant=r.variant,
                score=float(r.score) if r.score is not None else None,
                verdict=r.verdict,
                scored_against_variant=r.scored_against_variant,
                output_type_at_test=r.output_type_at_test,
                scored_at=r.scored_at.isoformat() if r.scored_at else None,
                attempt_count=int(r.attempt_count or 1),
                best_score=float(r.best_score) if r.best_score is not None else None,
                image_digest=r.image_digest,
                attempt_history=list(r.attempt_history or []),
                drift_ruling=r.drift_ruling,
                awaiting_drift_ruling=(
                    r.verdict == "drift" and r.drift_ruling is None
                ),
            )
            for r in rows
        ],
        pending_variants=pending,
    )


def rule_on_drifted_variant(db, *, node_def_id: int, variant: str, request, user):
    from node_builder.variant_test.dispatcher import DRIFT_RULINGS

    if request.ruling not in DRIFT_RULINGS:
        raise UnprocessableError(f"ruling must be one of {', '.join(DRIFT_RULINGS)}")

    nd = node_crud.get_node_definition_unscoped(db, node_def_id)
    if nd is None:
        raise NotFoundError("node_definition not found")
    if nd.tenant_id is not None and nd.tenant_id != user.tenant_id:
        raise ForbiddenError("Not your node")

    row = node_crud.get_verification_by_node_and_variant(db, node_def_id, variant)
    if row is None:
        raise NotFoundError(f"no verification row for variant {variant!r}")

    row.drift_ruling = request.ruling
    row.drift_ruled_at = datetime.now(timezone.utc)
    row.drift_ruled_by_user_id = user.id

    if request.ruling == "drop" and nd.gpu_variants:
        gv = dict(nd.gpu_variants)
        if gv.pop(variant, None) is not None:
            nd.gpu_variants = gv
            _flag_modified(nd, "gpu_variants")

    db.commit()

    # GO.8 (23.7) — a drop ruling removed the variant from the matrix: the
    # on-change gate re-verifies the affected variants. Best-effort.
    if request.ruling == "drop":
        _variant_change_gate(
            db,
            changed_paths=(_VARIANT_MATRIX_PATH,),
            label="rule_on_drifted_variant (gpu_variants drop)",
        )

    return sh._DriftRulingResponse(
        node_definition_id=node_def_id,
        variant=variant,
        ruling=request.ruling,
        in_service=(request.ruling == "publish"),
    )


def patch_reference_variant(db, *, node_def_id: int, request, current_user):
    nd = node_crud.get_node_definition_unscoped(db, node_def_id)
    if nd is None:
        raise NotFoundError("node definition not found")
    if nd.tenant_id is not None and nd.tenant_id != current_user.tenant_id:
        raise ForbiddenError("cross-tenant access denied")

    new_ref = (request.reference_variant or "").strip()
    if not new_ref:
        raise UnprocessableError("reference_variant must be non-empty")

    from node_builder.variant_test.dispatcher import rescore_against_new_reference
    try:
        result = rescore_against_new_reference(
            db, node_definition_id=node_def_id,
            new_reference_variant=new_ref,
        )
    except ValueError as ve:
        raise BadRequestError(str(ve))

    nd.reference_variant = new_ref
    db.commit()

    return sh._PatchReferenceVariantResponse(
        node_definition_id=node_def_id,
        reference_variant=new_ref,
        updated=result["updated"],
        skipped=result["skipped"],
    )


# =============================================================================
# Marketplace, tenant-team sharing, packages, try-before-you-buy — non-CE
# (moved to services/node_marketplace_paid.py; CE does not ship it. The
# router imports that module directly behind its own guarded try/except —
# nothing in this CE-clean file calls into it.)
# =============================================================================


# =============================================================================
# Describe stats (the one query-bearing endpoint in the describe/* family)
# =============================================================================


def admin_describe_stats(db, *, current_user):
    if not (current_user.is_platform_admin or False):
        raise ForbiddenError("platform admin only")

    top_repos: List[Dict[str, Any]] = []
    try:
        nodes = node_crud.list_nodes_with_custom_metadata(db)
        repo_counts: Dict[str, Dict[str, Any]] = {}
        for n in nodes:
            md = n.custom_metadata or {}
            describe_source = md.get("describe_source") if isinstance(md, dict) else None
            if not describe_source:
                continue
            repo_url = describe_source.get("repo_full_name") or describe_source.get("repo_url")
            if not repo_url:
                continue
            entry = repo_counts.setdefault(repo_url, {
                "repo": repo_url,
                "build_count": 0,
                "first_built_at": None,
                "last_built_at": None,
            })
            entry["build_count"] += 1
            created = n.created_at.isoformat() if getattr(n, "created_at", None) else None
            if created:
                if entry["first_built_at"] is None or created < entry["first_built_at"]:
                    entry["first_built_at"] = created
                if entry["last_built_at"] is None or created > entry["last_built_at"]:
                    entry["last_built_at"] = created
        top_repos = sorted(
            repo_counts.values(),
            key=lambda e: e["build_count"],
            reverse=True,
        )[:25]
    except Exception as e:
        sh.logger.warning("describe-stats top_repos lookup failed: %s", e)

    total_searches = 0
    total_synthesis = 0
    cache_hits = 0
    cache_misses = 0
    rate_limited = 0
    try:
        client = sh._redis_for_describe()
        keys = client.keys("mod:describe:*:counter:*")
        for key in keys:
            try:
                k = key.decode("utf-8") if isinstance(key, bytes) else key
                val = int(client.get(key) or 0)
                if "search:counter:total" in k:
                    total_searches += val
                elif "synth:counter:total" in k:
                    total_synthesis += val
                elif "search:counter:cache_hit" in k:
                    cache_hits += val
                elif "search:counter:cache_miss" in k:
                    cache_misses += val
                elif "rate_limited" in k:
                    rate_limited += val
            except Exception:
                continue
    except Exception:
        pass

    cache_total = cache_hits + cache_misses
    cache_ratio: Optional[float] = (
        (cache_hits / cache_total) if cache_total > 0 else None
    )

    return sh._DescribeStatsResponse(
        top_repos=top_repos,
        total_searches_24h=total_searches,
        total_synthesis_24h=total_synthesis,
        cache_hit_ratio=cache_ratio,
        rate_limited_calls_24h=rate_limited,
    )


# =============================================================================
# Remediation / federation / built-image cache
# =============================================================================


def remediation_lookup(db, *, error_signature: str, error_text: Optional[str], auth):
    sig = (error_signature or "").strip()
    if not sig:
        return sh._RemediationLookupResponse()

    rows = node_crud.list_remediations_by_signature(db, sig)
    known_bad = [
        {"diff_hash": r.diff_hash, "excerpt": (r.diff or "")[:200]}
        for r in sorted(
            (r for r in rows if r.success_count == 0 and r.failure_count >= 1),
            key=lambda r: r.failure_count,
            reverse=True,
        )[:10]
    ]
    positive_rows = [r for r in rows if r.success_count > 0]
    if positive_rows:
        positive_rows.sort(
            key=lambda r: (
                r.success_count - r.failure_count,
                r.success_count,
                r.last_used_at,
            ),
            reverse=True,
        )
        row = positive_rows[0]
        return sh._RemediationLookupResponse(
            diff=row.diff,
            diff_hash=row.diff_hash,
            success_count=row.success_count,
            failure_count=row.failure_count,
            model=row.model,
            match_kind="exact",
            promoted=row.promoted_at is not None,
            trust=sh._trust_score(row.success_count, row.failure_count),
            known_bad=known_bad,
        )

    if error_text:
        try:
            from utils.remediation_vector_store import embed_error, find_similar_remediation_id
            vec = embed_error(error_text)
            if vec is not None:
                hit = find_similar_remediation_id(vec)
                if hit is not None:
                    rid, score = hit
                    sim_row = node_crud.get_remediation_by_id(db, rid)
                    if sim_row is not None:
                        return sh._RemediationLookupResponse(
                            diff=sim_row.diff,
                            diff_hash=sim_row.diff_hash,
                            success_count=sim_row.success_count,
                            failure_count=sim_row.failure_count,
                            model=sim_row.model,
                            match_kind="semantic",
                            similarity=score,
                            promoted=sim_row.promoted_at is not None,
                            trust=sh._trust_score(
                                sim_row.success_count, sim_row.failure_count,
                            ),
                            known_bad=known_bad,
                        )
        except Exception as e:
            sh.logger.info("semantic remediation lookup failed: %s", e)

    try:
        fed_rows = node_crud.list_federated_by_signature(db, sig)
    except Exception:
        fed_rows = []
    if fed_rows:
        bad_peers = sh._bad_federation_peers(db)
        fed_rows = [r for r in fed_rows if r.source_peer not in bad_peers]
        if not fed_rows:
            fed_rows = []
    if fed_rows:
        fed_rows.sort(
            key=lambda r: (
                1 if r.promoted_at_source else 0,
                1 if r.diff else 0,
                r.success_count - r.failure_count,
                r.success_count,
            ),
            reverse=True,
        )
        best = fed_rows[0]
        return sh._RemediationLookupResponse(
            diff=best.diff,
            diff_hash=best.diff_hash,
            success_count=best.success_count,
            failure_count=best.failure_count,
            model=None,
            match_kind="federated",
            similarity=None,
            promoted=best.promoted_at_source,
            trust=sh._trust_score(best.success_count, best.failure_count),
            known_bad=known_bad,
        )

    return sh._RemediationLookupResponse(known_bad=known_bad)


def remediation_record(db, *, request, auth) -> Dict[str, Any]:
    sig = (request.error_signature or "").strip()
    if not sig or not request.diff or not request.diff_hash:
        raise BadRequestError("error_signature, diff, and diff_hash are all required")
    now = datetime.now(timezone.utc)
    existing = node_crud.get_remediation_by_signature_and_diff(db, sig, request.diff_hash)
    if existing is not None:
        existing.success_count += 1
        existing.last_used_at = now
        sh._maybe_promote(existing, now)
        db.commit()
        if request.error_text:
            sh._index_remediation_embedding(existing.id, request.error_text)
        return {
            "id": existing.id,
            "success_count": existing.success_count,
            "failure_count": existing.failure_count,
            "promoted": existing.promoted_at is not None,
            "created": False,
        }
    row = models.BuildRemediation(
        error_signature=sig,
        error_sample=(request.error_sample or "")[:2000] or None,
        diff=request.diff,
        diff_hash=request.diff_hash,
        model=request.model,
        success_count=1,
        created_at=now,
        last_used_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if request.error_text:
        sh._index_remediation_embedding(row.id, request.error_text)
    return {
        "id": row.id,
        "success_count": row.success_count,
        "failure_count": row.failure_count,
        "promoted": row.promoted_at is not None,
        "created": True,
    }


def remediation_record_failure(db, *, request, current_user) -> Dict[str, Any]:
    sig = (request.error_signature or "").strip()
    if not sig or not request.diff_hash:
        raise BadRequestError("error_signature and diff_hash are both required")
    row = node_crud.get_remediation_by_signature_and_diff(db, sig, request.diff_hash)
    if row is None:
        return {"recorded": False, "reason": "no matching row"}
    now = datetime.now(timezone.utc)
    row.failure_count += 1
    row.last_failure_at = now
    db.commit()
    return {
        "recorded": True,
        "id": row.id,
        "success_count": row.success_count,
        "failure_count": row.failure_count,
        "trust": sh._trust_score(row.success_count, row.failure_count),
    }


def remediation_record_failed_attempt(db, *, request, auth) -> Dict[str, Any]:
    sig = (request.error_signature or "").strip()
    if not sig or not request.diff or not request.diff_hash:
        raise BadRequestError("error_signature, diff, and diff_hash are all required")
    now = datetime.now(timezone.utc)
    existing = node_crud.get_remediation_by_signature_and_diff(db, sig, request.diff_hash)
    if existing is not None:
        existing.failure_count += 1
        existing.last_failure_at = now
        db.commit()
        return {
            "recorded": True,
            "id": existing.id,
            "success_count": existing.success_count,
            "failure_count": existing.failure_count,
            "created": False,
        }
    row = models.BuildRemediation(
        error_signature=sig,
        error_sample=(request.error_sample or "")[:2000] or None,
        diff=request.diff,
        diff_hash=request.diff_hash,
        success_count=0,
        failure_count=1,
        created_at=now,
        last_used_at=now,
        last_failure_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "recorded": True,
        "id": row.id,
        "success_count": row.success_count,
        "failure_count": row.failure_count,
        "created": True,
    }


def federation_remediation_share(db, *, request, authorization: Optional[str], origin: Optional[str]) -> Dict[str, Any]:
    from utils.paid_import import require_paid
    _m = require_paid("utils.federation", "Cross-tenant federation share")
    rejection = _m.validate_inbound(request.model_dump(), authorization or "")
    if rejection is not None:
        raise UnauthorizedError(rejection)
    src = (origin or "").strip()
    if not src:
        raise BadRequestError("X-MOD-Federation-Origin header is required")
    now = datetime.now(timezone.utc)
    row = node_crud.get_federated_by_signature_diff_peer(
        db, request.error_signature, request.diff_hash, src,
    )
    if row is not None:
        row.success_count = max(int(request.success_count), row.success_count)
        row.failure_count = max(int(request.failure_count), row.failure_count)
        row.promoted_at_source = bool(request.promoted_at_source)
        row.last_seen_at = now
        if request.diff and not row.diff:
            row.diff = request.diff
        db.commit()
        return {"id": row.id, "created": False}
    row = models.FederatedRemediation(
        error_signature=request.error_signature,
        diff_hash=request.diff_hash,
        diff=request.diff,
        source_peer=src,
        success_count=int(request.success_count),
        failure_count=int(request.failure_count),
        promoted_at_source=bool(request.promoted_at_source),
        last_seen_at=now,
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "created": True}


def built_image_cache_lookup(db, *, source_hash: str, image_name: str, variant: str, current_user):
    sh_ = (source_hash or "").strip()
    iname = (image_name or "").strip()
    if not sh_ or not iname:
        return sh._BuiltImageCacheLookupResponse()
    row = node_crud.get_built_image_cache_entry(
        db, source_hash=sh_, image_name=iname, variant=(variant or "hero"),
    )
    if row is None:
        return sh._BuiltImageCacheLookupResponse()
    try:
        row.hit_count += 1
        row.last_used_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
    return sh._BuiltImageCacheLookupResponse(
        image_digest=row.image_digest,
        source_registry=row.source_registry,
        hit_count=row.hit_count,
    )


def built_image_cache_record(db, *, request, current_user) -> Dict[str, Any]:
    if not (request.source_hash or "").strip() or not (request.image_name or "").strip():
        raise BadRequestError("source_hash and image_name are required")
    if not request.image_digest or not request.source_registry:
        raise BadRequestError("image_digest and source_registry are required")
    now = datetime.now(timezone.utc)
    existing = node_crud.get_built_image_cache_entry(
        db, source_hash=request.source_hash, image_name=request.image_name,
        variant=(request.variant or "hero"),
    )
    if existing is not None:
        existing.image_digest = request.image_digest
        existing.source_registry = request.source_registry
        existing.last_used_at = now
        db.commit()
        return {"id": existing.id, "created": False}
    row = models.BuiltImageCache(
        source_hash=request.source_hash,
        image_name=request.image_name,
        variant=(request.variant or "hero"),
        image_digest=request.image_digest,
        source_registry=request.source_registry,
        hit_count=0,
        last_used_at=now,
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "created": True}


def build_task_status(db, *, task_id: int, current_user) -> Dict[str, Any]:
    task = node_crud.get_api_task_by_id(db, task_id)
    if task is None:
        raise NotFoundError("task not found")
    if task.tenant_id != current_user.tenant_id:
        raise ForbiddenError("not your task")
    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)
    rp = task.result_payload or {}
    log_tail = rp.get("log_tail") or "" if isinstance(rp, dict) else ""
    response: Dict[str, Any] = {
        "task_id": task.id,
        "status": status_value,
    }
    persisted_deferred = rp.get("deferred_registration") if isinstance(rp, dict) else None
    if persisted_deferred:
        response["deferred_registration"] = persisted_deferred
    elif status_value == "completed":
        deferred = sh._extract_deferred_registration(log_tail)
        if deferred:
            response["deferred_registration"] = {
                "node_config": deferred.get("node_config") or {},
                "sample_test_result": deferred.get("sample_test_result"),
                "image": deferred.get("image"),
                "image_name": deferred.get("image_name"),
                "approved": None,
                "finalized_at": None,
            }
    if status_value == "failed":
        sf = sh._extract_sample_test_failure(log_tail)
        if sf:
            response["sample_test_failure"] = {
                "sample_test_result": sf.get("sample_test_result"),
                "image": sf.get("image"),
            }
        if task.error_message:
            response["error"] = task.error_message
    return response


def finalize_registration(db, *, request, current_user) -> Dict[str, Any]:
    task = node_crud.get_api_task_by_id(db, request.build_task_id)
    if task is None:
        raise NotFoundError("build task not found")
    if task.tenant_id != current_user.tenant_id:
        raise ForbiddenError("not your build task")
    rp = task.result_payload or {}
    deferred = rp.get("deferred_registration") if isinstance(rp, dict) else None
    if not deferred:
        log_tail = rp.get("log_tail") or "" if isinstance(rp, dict) else ""
        parsed = sh._extract_deferred_registration(log_tail)
        if not parsed:
            raise BadRequestError("this build did not defer registration")
        deferred = {
            "node_config": parsed.get("node_config") or {},
            "sample_test_result": parsed.get("sample_test_result"),
            "image": parsed.get("image"),
            "image_name": parsed.get("image_name"),
            "approved": None,
            "finalized_at": None,
        }
    if deferred.get("finalized_at"):
        return {
            "status": "already_finalized",
            "node_definition_id": deferred.get("node_definition_id"),
            "approved": deferred.get("approved"),
        }
    now = datetime.now(timezone.utc)
    deferred["finalized_at"] = now.isoformat()
    deferred["approved"] = bool(request.approved)
    if not request.approved:
        deferred["decline_reason"] = (request.decline_reason or "")[:500]
        rp["deferred_registration"] = deferred
        task.result_payload = rp
        db.commit()
        sh.logger.info(
            "user declined deferred registration build_task=%d reason=%s by=%s",
            task.id, request.decline_reason, current_user.email,
        )
        return {"status": "declined", "build_task_id": task.id}
    node_config = deferred.get("node_config") or {}
    if not node_config or not node_config.get("name"):
        raise ServiceError(
            "deferred build is missing node_config; cannot register", code="internal_error",
        )
    image_ref = node_config.get("executable_path") or ""
    if image_ref:
        admission = sh.node_admission.validate_base_image_ref(image_ref)
        if not admission.passed:
            raise UnprocessableError(
                "admission check failed",
                details={"admission_rule": admission.rule, "reason": admission.reason},
            )
    # Build the NodeDefinition row inline. Mirrors what
    # imagebuilder's register_node would have done.
    # 1805 — NodeDefinition has no `category`/`tier` column (never had since
    # 3c47cc68c first wrote this construction). Map them the way
    # crud.create_node_definition does: the wizard/imagebuilder node_config key
    # for category is `type` (schemas.NodeDefinitionBase.type, the imagebuilder
    # sets node_config["type"] = category; "General" is that column's default),
    # and tier is the deferred pair node_definitions.cost_credits_by_tier +
    # cost_basis, written ONLY when an explicit map was sent AND the columns
    # exist in the live DB (utils.node_cost.tier_columns_available gate — a
    # pre-migration DB rejects SQL naming them). `version` is an Integer column
    # the create schema never carries, so a "1.0.0" string is dropped (None ==
    # column default 0).
    _nd_kwargs = dict(
        tenant_id=current_user.tenant_id,
        name=node_config["name"],
        description=node_config.get("description") or "",
        executable_path=node_config.get("executable_path") or "",
        type=node_config.get("type") or "General",
        cost_in_credits=int(node_config.get("cost_in_credits") or 1),
        gpu_variants=node_config.get("gpu_variants") or None,
        node_kind=(
            node_config.get("node_kind")
            or sh._derive_node_kind_from_image(node_config.get("executable_path"))
        ),
    )
    _explicit_tiers = _normalise_explicit_tiers(
        node_config.get("cost_credits_by_tier") or node_config.get("cost_tiers")
    )
    if _explicit_tiers is not None and _node_cost.tier_columns_available(db):
        _nd_kwargs["cost_credits_by_tier"] = dict(_explicit_tiers)
        _nd_kwargs["cost_basis"] = "manual"
        _nd_kwargs["cost_in_credits"] = _explicit_tiers["medium"]
    nd = models.NodeDefinition(**_nd_kwargs)
    db.add(nd)
    db.flush()
    # GSEC FX.73 p2 (Q56=A) — this build path registers the
    # NodeDefinition inline (not via crud.create_node_definition, which
    # has its own hook), so the licence manual-review queue hook is
    # repeated here: nothing is known about a wizard-authored node's
    # licence yet, so this is deliberately unknown/unknown.
    deferred["node_definition_id"] = nd.id
    rp["deferred_registration"] = deferred
    task.result_payload = rp
    db.commit()
    db.refresh(nd)
    sh.logger.info(
        "user approved deferred registration build_task=%d node=%d by=%s",
        task.id, nd.id, current_user.email,
    )
    return {
        "status": "registered",
        "build_task_id": task.id,
        "node_definition_id": nd.id,
        "node_name": nd.name,
    }


def admin_remediation_stats(db, *, current_user) -> Dict[str, Any]:
    if not getattr(current_user, "is_platform_admin", False):
        raise ForbiddenError("Platform admin only")
    import os as _os
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    agg = node_crud.remediation_cache_aggregate_stats(db, cutoff_24h, cutoff_7d)
    fed_total = node_crud.federated_remediation_total(db)
    fed_peers = node_crud.federated_remediation_peer_aggregates(db)
    top_promoted = node_crud.list_top_promoted_remediations_by_success(db, limit=10)
    bic_total, bic_total_hits = node_crud.built_image_cache_total_and_hits(db)

    sum_success = agg["sum_success"]
    sum_failure = agg["sum_failure"]
    return {
        "remediation_cache": {
            "total_entries": agg["total_remediations"],
            "promoted_entries": agg["promoted"],
            "promoted_last_7d": agg["promoted_7d"],
            "used_last_24h": agg["used_24h"],
            "lifetime_success_count": int(sum_success),
            "lifetime_failure_count": int(sum_failure),
            "lifetime_trust": (
                float(sum_success) / float(sum_success + sum_failure)
                if (sum_success + sum_failure) > 0 else 0.0
            ),
        },
        "top_promoted": [
            {
                "id": r.id,
                "error_signature": r.error_signature[:16] + "...",
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "model": r.model,
                "error_sample": (r.error_sample or "")[:160],
            }
            for r in top_promoted
        ],
        "federation": {
            "enabled": bool(_os.getenv("MOD_FEDERATION_MODE", "off").lower() != "off"),
            "mode": _os.getenv("MOD_FEDERATION_MODE", "off").lower(),
            "total_inbound_entries": fed_total,
            "peers": [
                {
                    "url": p.source_peer,
                    "entries": int(p.entries),
                    "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
                }
                for p in fed_peers
            ],
        },
        "source_hash_fast_path": {
            "tracked_builds": bic_total,
            "lifetime_hits": int(bic_total_hits),
        },
    }


def feedback_node_broken(db, *, request, current_user) -> Dict[str, Any]:
    if not request.is_broken:
        return {"recorded": False, "reason": "thumbs-up signals not used by remediation trust"}
    nd = node_crud.get_node_definition_unscoped(db, request.node_definition_id)
    if nd is None:
        raise NotFoundError("node definition not found")
    row = node_crud.get_most_recently_used_remediation(db)
    if row is None:
        return {"recorded": False, "reason": "no remediation history to attribute to"}
    now = datetime.now(timezone.utc)
    row.failure_count += 1
    row.last_failure_at = now
    db.commit()
    sh.logger.info(
        "user-broken-node feedback: node=%d remediation=%d reason=%s by=%s",
        nd.id, row.id, request.reason, current_user.email,
    )
    return {
        "recorded": True,
        "remediation_id": row.id,
        "failure_count": row.failure_count,
    }


def admin_demote_remediation(db, *, remediation_id: int, current_user) -> Dict[str, Any]:
    if not getattr(current_user, "is_platform_admin", False):
        raise ForbiddenError("Platform admin only")
    row = node_crud.get_remediation_by_id(db, remediation_id)
    if row is None:
        raise NotFoundError("remediation not found")
    was_promoted = row.promoted_at is not None
    if not was_promoted:
        return {"id": row.id, "demoted": False, "reason": "not currently promoted"}
    row.promoted_at = None
    db.commit()
    sh.logger.info(
        "admin demoted remediation id=%d (success=%d failure=%d) by user=%s",
        row.id, row.success_count, row.failure_count, current_user.email,
    )
    return {
        "id": row.id,
        "demoted": True,
        "success_count": row.success_count,
        "failure_count": row.failure_count,
    }


def remediation_promoted(db, *, current_user) -> Dict[str, Any]:
    rows = node_crud.list_promoted_remediations(db, limit=200)
    return {
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "error_signature": r.error_signature,
                "error_sample": r.error_sample,
                "diff_hash": r.diff_hash,
                "model": r.model,
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "trust": sh._trust_score(r.success_count, r.failure_count),
                "promoted_at": r.promoted_at.isoformat() if r.promoted_at else None,
            }
            for r in rows
        ],
    }


# =============================================================================
# Core NodeDefinition CRUD-shaped endpoints
# =============================================================================


def _normalise_explicit_tiers(raw) -> Optional[Dict[str, int]]:
    """sz-tcp.5c — validate + normalise an explicit tier map on a register payload.

    The wizard's tier prices ride build_specs.spec_json.cost_tiers (sz-tcp.5b)
    and are forwarded verbatim by the build-task register stage, so they can be
    floats (e.g. medium=2.5). models.py's ck_node_def_cost_in_credits_matches_medium
    CHECK casts the stored medium with ::int, so every tier is normalised to a
    non-negative int, rounding half-up, before it reaches the column.

    Same shape rules (and the same 422-class error) as the wizard's own
    _validate_cost_tiers in services/node_definitions_node_wizard_service.py:
    object, all three tier keys present, numeric (bool rejected), non-negative.
    Extra keys are ignored. Returns None when the caller sent no map (legacy
    behaviour, unchanged).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise UnprocessableError("cost_credits_by_tier must be a non-empty object")
    tiers: Dict[str, int] = {}
    for key in ("small", "medium", "large"):
        if key not in raw:
            raise UnprocessableError(
                f"cost_credits_by_tier is missing the required tier key '{key}'"
            )
        val = raw[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise UnprocessableError(
                f"cost_credits_by_tier['{key}'] must be a number of credits"
            )
        if val < 0:
            raise UnprocessableError(
                f"cost_credits_by_tier['{key}'] must be non-negative"
            )
        tiers[key] = int(Decimal(str(val)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return tiers


# =============================================================================
# B24-FIX3 (CE launch, security) — the ONE authority-scoped ce_included strip.
# =============================================================================
# `ce_included` is a SYSTEM-only CE marker (see the B24-FIX2 note at the create
# branch below: services/node_scanner.py:1165 stamps it TRUE on CREATE for keep-
# listed names and the WorkflowBuilder palette HIDES local twins from it). Every
# CREATE path must run this same check before crud.create_node_definition, so it
# lives here once instead of copy-pasted per call site — the round-2 review found
# two direct crud callers (routers/node_definitions_domains/task_queue_scheduler.
# py:251 image upload, :344 external authoring) that bypassed the inline guard.
#
# Fail closed: `auth` is the server-side auth context when the caller has one
# (BuildAuthContext); the scheduler endpoints authenticate through user/API-key
# dependencies that return ONLY a models.User, so they pass auth=None and the
# getattr below strips. Nothing user-facing can satisfy BOTH arms.
def strip_user_ce_included(node_def, auth, user):
    """Return `node_def` with a caller-supplied ce_included dropped unless the
    caller is the platform service identity on the master key."""
    if not (
        getattr(auth, "via", None) == "master_key"
        and sh._is_service_account(user)
    ):
        return node_def.model_copy(update={"ce_included": None})
    return node_def


def create_or_update_node_definition(db, *, node_def, response, auth):
    current_user = auth.user
    if auth.via == "build_token":
        if node_def.build_spec_id is not None:
            sh.assert_build_scope(auth, build_spec_id=node_def.build_spec_id)
        else:
            node_def.build_spec_id = auth.build_spec_id
        if node_def.tenant_id is not None and int(node_def.tenant_id) != int(auth.tenant_id):
            raise ForbiddenError(
                f"Build-task token scoped to tenant {auth.tenant_id} "
                f"cannot register a node for tenant {node_def.tenant_id}."
            )
        node_def.tenant_id = auth.tenant_id
    try:
        if not node_def.node_kind:
            derived = sh._derive_node_kind_from_image(node_def.executable_path)
            if derived:
                node_def.node_kind = derived

        if node_def.tenant_id is not None or sh._is_service_account(current_user):
            effective_tenant_id = node_def.tenant_id
        else:
            effective_tenant_id = current_user.tenant_id

        # sz-tcp.5c — an EXPLICIT tier map on the incoming payload (wizard-chosen
        # prices forwarded by the build-task register stage) is authoritative:
        # it overrides the GB.TIER.0.4 "never clobber a measured/operator-set
        # price" guard below, and it must land verbatim (normalised to ints) on
        # node_definitions.cost_credits_by_tier with cost_basis='manual'. None
        # == no tiers sent == legacy behaviour, unchanged.
        _explicit_tiers = _normalise_explicit_tiers(
            getattr(node_def, "cost_credits_by_tier", None)
        )
        _tier_cols = _node_cost.tier_columns_available(db)

        sh._run_admission(
            "image",
            {"executable_path": node_def.executable_path},
            current_user,
            effective_tenant_id=effective_tenant_id,
        )

        if effective_tenant_id is None:
            existing_node_def = node_crud.get_system_node_definition_by_name(db, node_def.name)
        else:
            existing_node_def = sh.crud.get_node_definition_by_name(
                db=db, name=node_def.name, tenant_id=effective_tenant_id
            )

        if existing_node_def:
            prev_path = existing_node_def.executable_path
            prev_scan_status = (existing_node_def.scan_status or "").lower()
            update_payload = node_def.model_dump(exclude={'tenant_id', 'build_spec_id'})
            update_payload.pop('node_role', None)
            update_payload.pop('system_only', None)
            # FX.73 p4c — ce_included is CREATE-only (the CE seed stamp from the
            # node-seed keep-list). A re-scan must not clobber it, so drop it
            # from the update payload exactly like node_role / gpu_variants.
            update_payload.pop('ce_included', None)
            existing_variants = existing_node_def.gpu_variants or {}
            if existing_variants:
                update_payload.pop('gpu_variants', None)
            # sz-tcp.5c — an EXPLICIT tier map overrides the GB.TIER.0.4 guard
            # below: the wizard's chosen prices are the operator's own decision,
            # so they land verbatim (basis 'manual', cost_in_credits = medium so
            # ck_node_def_cost_in_credits_matches_medium holds). Without tiers
            # the guard is unchanged.
            if _explicit_tiers is not None and _tier_cols:
                update_payload['cost_credits_by_tier'] = dict(_explicit_tiers)
                update_payload['cost_basis'] = "manual"
                update_payload['cost_in_credits'] = _explicit_tiers["medium"]
            # GB.TIER.0.4 (b): a re-scan/rebuild never clobbers a measured,
            # operator-set or free price; otherwise the listener re-derives
            # the tiers. cost_basis is unreadable pre-migration (deferred
            # column absent), so there keep today's behavior.
            elif (
                _tier_cols
                and existing_node_def.cost_basis in ("calibrated", "manual", "free")
            ):
                update_payload.pop('cost_in_credits', None)
                update_payload.pop('cost_credits_by_tier', None)
                update_payload.pop('cost_basis', None)
            else:
                # No explicit map: the tier columns are never written here (the
                # models.py listener re-derives them from cost_in_credits), and
                # the create model's None defaults must not be setattr'd onto
                # the NOT-NULL columns.
                update_payload.pop('cost_credits_by_tier', None)
                update_payload.pop('cost_basis', None)
            update_data = schemas.NodeDefinitionUpdate(**update_payload)
            updated_def = sh.crud.update_node_definition(
                db=db, db_node_def=existing_node_def, node_def_update=update_data
            )

            try:
                sh._clear_soft_deletes_for_node_refs(
                    db, effective_tenant_id, updated_def,
                )
            except Exception as e:  # noqa: BLE001
                sh.logger.warning(
                    "Failed to clear stale soft-deletes on re-register "
                    "for node %d (non-critical): %s",
                    updated_def.id, e,
                )

            if sh._is_saas() and updated_def.executable_path and effective_tenant_id:
                path_changed = prev_path != updated_def.executable_path
                already_ok = prev_scan_status in ("approved", "clean")
                if path_changed or not already_ok:
                    try:
                        sh._dispatch_trivy_scan(
                            db, updated_def.id, updated_def.executable_path,
                            effective_tenant_id,
                        )
                    except Exception as e:
                        sh.logger.warning(
                            "Failed to re-dispatch security scan on update "
                            "for node %d (non-critical): %s",
                            updated_def.id, e,
                        )
            return updated_def
        else:
            if effective_tenant_id is not None:
                billing_user = sh._effective_billing_user(
                    db, current_user, getattr(node_def, "build_spec_id", None),
                )
                if _budget_paid is not None:
                    _budget_paid.charge_node_creation(db, billing_user, node_def.name)

            effective_user_id = None

            # sz-tcp.5c — an explicit tier map on a fresh create lands verbatim
            # (basis 'manual', cost_in_credits = the map's medium so
            # ck_node_def_cost_in_credits_matches_medium holds). Gated on the
            # tier columns existing: a live pre-migration DB would reject SQL
            # that names them.
            if _explicit_tiers is not None and _tier_cols:
                node_def = node_def.model_copy(
                    update={
                        "cost_credits_by_tier": dict(_explicit_tiers),
                        "cost_basis": "manual",
                        "cost_in_credits": _explicit_tiers["medium"],
                    }
                )
            else:
                # No explicit map: strip any caller-supplied cost_basis /
                # cost_credits_by_tier unconditionally — these are only ever
                # set by this service (basis='manual' above), never by the
                # caller directly, or a tenant could POST cost_basis='free'
                # (or any other basis) to dodge billing on create. When
                # _tier_cols is False (pre-migration) this also keeps the
                # columns out of the INSERT entirely (SQLAlchemy omits a None
                # server_default column), so no SQL names them.
                node_def = node_def.model_copy(
                    update={"cost_credits_by_tier": None, "cost_basis": None}
                )

            # B24-FIX2 (CE launch, security) — `ce_included` is a SYSTEM-ONLY
            # marker: services/node_scanner.py:1165 stamps it TRUE on CREATE for
            # keep-listed names, and the WorkflowBuilder palette HIDES local
            # twins from it. It is declared on schemas.NodeDefinitionBase, so it
            # IS part of the create body a plain user can POST — a user who sets
            # it on their own `human_approval` row gets that row hidden from
            # their own palette. Fail closed: only the platform service identity
            # (the node scan, which authenticates with the master key as the
            # seeded api_service user) may stamp it; every other caller's value
            # is dropped, the same CREATE-only guard the re-register branch
            # applies at :1662 (`update_payload.pop('ce_included', None)`).
            # The crud exclude set is NOT widened: the scanner registers through
            # this same crud.create_node_definition and relies on passing
            # ce_included=True, so the strip is authority-scoped, not global.
            if not (
                getattr(auth, "via", None) == "master_key"
                and sh._is_service_account(current_user)
            ):
                node_def = node_def.model_copy(update={"ce_included": None})

            new_def = sh.crud.create_node_definition(
                db=db,
                node_def=node_def,
                tenant_id=effective_tenant_id,
                user_id=effective_user_id,
            )

            try:
                sh._clear_soft_deletes_for_node_refs(
                    db, effective_tenant_id, new_def,
                )
            except Exception as e:  # noqa: BLE001
                sh.logger.warning(
                    "Failed to clear stale soft-deletes on create "
                    "for node %d (non-critical): %s",
                    new_def.id, e,
                )

            # Node images are NOT part of the canonical storage total
            # (storage_measure._DB_SUMS_SQL never sums node_definitions.
            # image_size_bytes; they live in Harbor — owner decision on
            # whether to count them is still pending). Do not adjust
            # tenants.storage_used_bytes here.

            if sh._is_saas() and new_def.executable_path and effective_tenant_id:
                try:
                    sh._dispatch_trivy_scan(db, new_def.id, new_def.executable_path, effective_tenant_id)
                except Exception as e:
                    sh.logger.warning("Failed to dispatch security scan (non-critical): %s", e)

            sh._auto_calibrate_new_node_hook(
                db, new_def, current_user, effective_tenant_id,
            )

            response.status_code = sh.status.HTTP_201_CREATED
            return new_def

    except sh.HTTPException:
        raise
    except ServiceError:
        raise
    except Exception as e:
        sh.logger.error("An unexpected error occurred while processing the node definition: %s", e)
        raise ServiceError(
            "An unexpected error occurred while processing the node definition",
            code="internal_error",
        )


def patch_variant_status(db, *, payload, current_user) -> Dict[str, Any]:
    effective_tenant_id = (
        current_user.tenant_id
        if not sh._is_service_account(current_user)
        else None
    )
    node = node_crud.find_node_by_name_scoped(db, payload.node_name, effective_tenant_id)
    if not node:
        raise NotFoundError(f"Node definition {payload.node_name!r} not found")

    current_gv = dict(node.gpu_variants or {})
    for v in payload.failed:
        current_gv.pop(v, None)
    node.gpu_variants = current_gv or None

    current_ir = dict(node.incompatible_reasons or {})
    for v, reason in payload.failed.items():
        current_ir[v] = reason
    for v in payload.succeeded:
        current_ir.pop(v, None)
    node.incompatible_reasons = current_ir or None

    if payload.failed:
        try:
            from utils.port_failure_explain import explain_failure
            current_cm = dict(node.custom_metadata or {})
            fx_map = dict(current_cm.get("failure_explanations") or {})
            for v, reason in payload.failed.items():
                fx = explain_failure(
                    stage="build",
                    log_tail=reason,
                    variant=v,
                )
                fx["original_reason"] = reason
                fx_map[v] = fx
            for v in payload.succeeded:
                fx_map.pop(v, None)
            if fx_map:
                current_cm["failure_explanations"] = fx_map
            else:
                current_cm.pop("failure_explanations", None)
            node.custom_metadata = current_cm or None
        except Exception:
            sh.logger.exception(
                "P9o explainer failed (non-blocking) for node=%s", node.name,
            )

    if payload.failed:
        try:
            from utils import patch_loop_state as _pls
            current_cm = dict(node.custom_metadata or {})
            candidates = dict(current_cm.get("patch_loop_candidates") or {})
            for v, reason in payload.failed.items():
                r_lower = (reason or "").lower()
                if "smoke" in r_lower and "drift" in r_lower:
                    stage = "smoke_drift"
                elif "smoke" in r_lower:
                    stage = "smoke_crash"
                else:
                    stage = "build"
                decision = _pls.evaluate(
                    custom_metadata=current_cm,
                    variant=v,
                    stage=stage,
                    proposed_log_hash="",
                    proposed_diff_hash="",
                    proposed_token_cost=0,
                )
                candidates[v] = {
                    "stage": stage,
                    "reason_head": (reason or "")[:120],
                    "allowed": decision.allowed,
                    "reason_code": decision.reason,
                    "attempts_used": decision.attempts_used,
                    "attempts_remaining": decision.attempts_remaining,
                    "token_total": decision.token_total,
                    "token_remaining": decision.token_remaining,
                    "detail": decision.detail,
                }
            for v in payload.succeeded:
                candidates.pop(v, None)
            if candidates:
                current_cm["patch_loop_candidates"] = candidates
            else:
                current_cm.pop("patch_loop_candidates", None)
            node.custom_metadata = current_cm or None
        except Exception:
            sh.logger.exception(
                "P5 patch_loop_candidates seeding failed for node=%s",
                node.name,
            )

        try:
            from utils.slack_dispatcher import dispatch_slack_event
            for v, reason in payload.failed.items():
                event = (
                    "port.smoke_drift"
                    if "smoke" in (reason or "").lower() and "drift" in (reason or "").lower()
                    else "port.build_failed"
                )
                dispatch_slack_event(
                    db=db,
                    tenant_id=node.tenant_id,
                    event_type=event,
                    payload={
                        "node_name": node.name,
                        "variant": v,
                        "reason": (reason or "")[:200],
                    },
                )
        except Exception:
            sh.logger.debug("Slack port-failure dispatch skipped", exc_info=True)

    current_vvs = dict(node.variant_verification_status or {})
    newly_pending: List[str] = []
    for v in payload.succeeded:
        if current_vvs.get(v) not in ("pending", "verified"):
            newly_pending.append(v)
        current_vvs.setdefault(v, "pending")
    for v in payload.failed:
        current_vvs[v] = "build_failed"
    node.variant_verification_status = current_vvs or None

    db.commit()

    # GO.8 (23.7) — ImageBuilder's build-all/push-all outcome just moved the
    # variant matrix (failed variants dropped, succeeded ones pending): the
    # on-change gate re-verifies the affected variants. Best-effort.
    if payload.failed or payload.succeeded:
        _variant_change_gate(
            db,
            changed_paths=(_VARIANT_MATRIX_PATH,),
            label="patch_variant_status (gpu_variants mutation)",
        )

    import os as _os
    if (_os.getenv("MOD_AUTO_REFUND_PORT_FAILURES", "1") == "1"
            and node.tenant_id is not None
            and payload.failed
            and _budget_paid is not None):
        _budget_paid.refund_port_failures(db, node=node, payload=payload)

    if _os.getenv("MOD_VARIANT_SMOKE_ENABLED", "0") == "1":
        for v in newly_pending:
            try:
                sh._enqueue_variant_smoke_task(db, node, v)
            except Exception:
                sh.logger.exception(
                    "variant_smoke enqueue failed node=%s variant=%s — "
                    "continuing; variant will stay 'pending' and be picked "
                    "up by the next sweep",
                    node.name, v,
                )

    return {
        "id": node.id,
        "name": node.name,
        "gpu_variants": node.gpu_variants,
        "incompatible_reasons": node.incompatible_reasons,
        "variant_verification_status": node.variant_verification_status,
    }


def get_scan_queue_context(db, *, node_definition_id: int, current_user) -> Dict[str, Any]:
    nd = sh.crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if not nd:
        raise NotFoundError("Node definition not found")

    _node_kind = models.ApiTaskQueue.task_payload.op("->>")("node_kind")
    in_flight = node_crud.count_in_flight_scan_tasks(db, _node_kind)

    pending_rows = node_crud.list_pending_scan_tasks(db, _node_kind)
    pending_total = len(pending_rows)

    this_position = None
    for idx, task in enumerate(pending_rows, start=1):
        env = (task.task_payload or {}).get("container_env") or {}
        if str(env.get("NODE_DEFINITION_ID", "")) == str(node_definition_id):
            this_position = idx
            break

    last_attempt = None
    last_task = node_crud.get_last_scan_task_for_node(db, _node_kind, node_definition_id)
    if last_task is not None:
        last_attempt = {
            "task_id": last_task.id,
            "status": last_task.status.value if hasattr(last_task.status, "value") else str(last_task.status),
            "completed_at": last_task.completed_at.isoformat() if last_task.completed_at else None,
            "error_message": last_task.error_message,
        }

    return {
        "in_flight": in_flight,
        "pending": pending_total,
        "this_node_position": this_position,
        "last_attempt": last_attempt,
    }


def set_node_manifest_metadata(db, *, node_definition_id: int, body, current_user) -> Dict[str, Any]:
    # current_user is the BuildAuthContext.user from the endpoint. A scoped
    # build-task token (the ImageBuilder authenticates this digest callback
    # with the SAME credential it registered the node with) resolves to the
    # platform service user, so it satisfies the service-account gate below —
    # consistent with the register endpoint, which treats a build token as the
    # service identity for node ops.
    if not sh._is_service_account(current_user) and not getattr(current_user, "is_platform_admin", False):
        raise ForbiddenError("service account or platform admin required")
    nd = node_crud.get_node_definition_unscoped(db, node_definition_id)
    if not nd:
        raise NotFoundError("Node definition not found")
    nd.manifest_ref = body.manifest_ref
    nd.image_digest = body.image_digest
    try:
        sh.crud.record_node_version(db, nd)
    except Exception:  # noqa: BLE001
        sh.logger.warning("record_node_version failed for node %s", nd.id, exc_info=True)
    db.commit()

    # GO.8 (23.7) — a re-published variant image (new digest) is a variant
    # change: the on-change gate re-verifies the affected variants. Best-effort.
    if (body.image_digest or "").strip():
        _variant_change_gate(
            db,
            changed_paths=(_VARIANT_MATRIX_PATH,),
            label="set_node_manifest_metadata (variant re-publish)",
        )

    return {"ok": True, "manifest_ref": nd.manifest_ref, "image_digest": nd.image_digest}


def rescan_node_definition(db, *, node_definition_id: int, current_user) -> Dict[str, Any]:
    db_node_def = node_crud.get_node_definition_unscoped(db, node_definition_id)
    if db_node_def is None:
        raise NotFoundError("Node Definition not found")

    if (
        not current_user.is_platform_admin
        and db_node_def.tenant_id is not None
        and db_node_def.tenant_id != current_user.tenant_id
    ):
        raise ForbiddenError("Cannot rescan other tenant's node")

    if not db_node_def.executable_path:
        raise BadRequestError("Node has no executable_path (processing image); nothing to scan.")

    try:
        task_id = sh._dispatch_trivy_scan(
            db,
            db_node_def.id,
            db_node_def.executable_path,
            db_node_def.tenant_id or 1,
        )
    except Exception as e:
        sh.logger.error("Rescan dispatch failed for node %d: %s", db_node_def.id, e)
        raise ServiceError(f"Could not dispatch scan: {e}", code="internal_error")

    return {
        "ok": True,
        "task_id": task_id,
        "node_id": db_node_def.id,
        "scan_status": "pending_scan",
        "message": f"Scan queued (task {task_id}). Status will update when the worker completes the scan.",
    }


def set_node_role(db, *, node_definition_id: int, body, current_user):
    if not getattr(current_user, "is_platform_admin", False):
        raise ForbiddenError("only a platform admin can change a node's role")
    role = (body.role or "").strip().lower()
    if role not in ("user", "system"):
        raise UnprocessableError("role must be 'user' or 'system'")
    nd = node_crud.get_node_definition_unscoped(db, node_definition_id)
    if nd is None:
        raise NotFoundError("Node Definition not found")
    nd.node_role = role
    db.commit()
    db.refresh(nd)
    sh.logger.info("node %s (%s) role set to %s by user %s",
                    nd.id, nd.name, role, current_user.id)
    return nd


def delete_node_definition(db, *, node_definition_id: int, request, current_user) -> None:
    tenant_id = current_user.tenant_id

    db_node_def = sh.crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id, tenant_id=tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if db_node_def is None:
        raise NotFoundError("Node Definition not found")

    if db_node_def.tenant_id is None:
        try:
            import audit
            audit.log_security_event(
                db,
                action=models.AuditAction.SECURITY_INCIDENT_CREATED,
                request=request,
                user_id=current_user.id,
                tenant_id=current_user.tenant_id,
                metadata={
                    "kind": "system_node_delete_refused",
                    "node_definition_id": node_definition_id,
                },
            )
        except Exception as e:
            sh.logger.warning("system node delete refusal audit failed: %s", type(e).__name__)
        raise ForbiddenError("system nodes cannot be deleted")

    size_b = db_node_def.image_size_bytes or 0
    image_ref = db_node_def.executable_path or ""
    node_tenant_id = db_node_def.tenant_id

    sh.crud.soft_delete_node_definition(
        db=db, db_node_def=db_node_def, actor_user_id=current_user.id,
    )

    # Node images are NOT part of the canonical storage total (see the
    # create-path comment above) — do not adjust tenants.storage_used_bytes
    # here either.

    try:
        import audit
        audit.log_soft_delete(
            db=db,
            actor_user_id=current_user.id,
            tenant_id=node_tenant_id or current_user.tenant_id or 0,
            resource_type="node_definition",
            resource_id=node_definition_id,
            metadata={
                "image_ref": image_ref,
                "image_size_bytes": size_b,
                "node_name": db_node_def.name,
            },
            request=request,
        )
    except Exception as e:
        sh.logger.warning("§11b audit emit failed for node %s: %s", node_definition_id, e)

    sh._tag_harbor_for_deletion_best_effort(node_tenant_id, image_ref)

    return None
