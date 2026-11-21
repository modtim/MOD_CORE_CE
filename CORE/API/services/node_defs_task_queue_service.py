"""Business logic for the `task_queue_scheduler` node_definitions domain
(openspec: stack-training-and-prewarm, Gate 2b / D9).

Companion to `crud/node_defs_task_queue.py` (the ONLY place this slice's
`db.query` lives). The router
(`routers/node_definitions_domains/task_queue_scheduler.py`) stays
HTTP-only: parse input, call a function here, map a `ServiceError` to
`HTTPException`, shape the response.

Extracted verbatim from the router during the Gate-2b migration —
behaviour-preserving MOVE. Every function below is the same logic that
used to live inline in the router handler of the same name; only the
data access now goes through `crud.node_defs_task_queue` and error
raising goes through `ServiceError` subclasses instead of bare
`HTTPException`.

404-vs-403: fetches stay tenant-UN-scoped in `crud.node_defs_task_queue`;
tenant/ownership policy is applied HERE (SHARED RULES #5). Note that two
of these endpoints (`get_generation_artifacts`, `get_port_diff`) never
distinguished 403 from 404 in the original inline code either — a
cross-tenant task always read as a plain "Task not found" 404 — so this
migration preserves that exact (single-404) behaviour rather than
introducing a new 403 path the original response contract never had.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified as _flag_modified

from crud import node_defs_task_queue as node_crud
from routers.node_definitions_domains import _shared as sh
from services._base import NotFoundError, ServiceError


# =============================================================================
# Local error types (services/_base.py has not_found/conflict but no
# forbidden/bad_request — SHARED RULES: declare what's needed here).
# =============================================================================


class ForbiddenError(ServiceError):
    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# Router-side code -> HTTP status map. Mirrors node_authoring_service.py.
STATUS_BY_CODE = {
    "not_found": 404,
    "conflict": 409,
    "forbidden": 403,
    "bad_request": 400,
}


# =============================================================================
# Wizard artifact ingest / repair-budget reset
# =============================================================================


def ingest_wizard_artifacts(db, *, request, auth) -> "sh.IngestArtifactsResponse":
    sh.assert_build_scope(auth, build_spec_id=request.spec_id)

    from node_builder.build_dag.artifact_ingest import (
        admit_clone_bounds,
        merge_artifacts_into_spec_json,
        normalize_artifact_bundle,
        validate_artifacts,
    )
    from node_builder.critic_artifacts import audit_artifacts

    # Flatten the nested-wrapper shape weak Containerizer models sometimes emit
    # (all three artifacts as a JSON string inside `dockerfile`, sibling keys
    # empty) BEFORE validating — otherwise a present-but-nested entrypoint_sh
    # reads as missing and 400s the whole build. No-op for a well-formed flat
    # bundle. Everything downstream (audit, hallucinated-base, merge) then sees
    # the flattened artifacts.
    artifacts = normalize_artifact_bundle(request.artifacts)

    # GSEC R-100 — deterministic clone-bounds admission. Rewrites every
    # unbounded `git clone` the LLM emitted in the generated Dockerfile(s) to
    # a shallow partial clone with LFS smudge skipped, and rejects
    # history-unshallowing constructs (`git fetch --unshallow` / `--no-depth`)
    # with a clear 400 reason. Runs BEFORE validate/audit so the rest of the
    # pipeline only ever sees bounded artifacts.
    try:
        artifacts, clone_changes = admit_clone_bounds(dict(artifacts or {}))
        if clone_changes:
            sh.logger.info(
                "/wizard/ingest-artifacts: spec=%d clone-bounds rewrites: %s",
                request.spec_id, clone_changes,
            )
    except Exception as exc:  # noqa: BLE001 — CloneBoundsRejection is the expected type
        reason = getattr(exc, "reason", None) or str(exc)
        sh.logger.warning(
            "/wizard/ingest-artifacts: spec=%d rejecting artifacts — %s",
            request.spec_id, reason,
        )
        raise BadRequestError(
            "unbounded git clone in generated Dockerfile",
            details={"reason": reason},
        )

    ok, reason = validate_artifacts(artifacts)
    if not ok:
        raise BadRequestError(reason or "invalid artifacts", details={"reason": reason})

    spec_row = node_crud.get_build_spec_record(db, request.spec_id)
    if spec_row is None:
        detail = f"build_spec {request.spec_id} not found"
        raise NotFoundError(detail, details={"reason": detail})

    # Critic C: Artifact Auditor. Runs deterministic structural
    # checks (image_name shape, TODO placeholders, required Dockerfile
    # directives) then an LLM critic for source/dependency/IO
    # consistency. Patches inline on failure up to 5 iterations. On
    # critic exhaustion we proceed with whatever's left — downstream
    # test_and_repair catches anything the critic missed and the
    # operator sees a clearer failure later in the timeline rather
    # than a black-box build crash.
    audited_artifacts = audit_artifacts(dict(artifacts or {}))

    # Deterministic hallucinated-base check on the INITIAL Containerizer
    # output. wfr 65 hit this: Containerizer wrote `FROM nvidia/cuda:
    # 12.1.1-base` which is a hallucinated tag — the build looped
    # through 2 attempts before bailing because the test-and-repair
    # stage's outer-repair only runs the denylist on REPAIR outputs,
    # not on the initial Dockerfile. Catching here saves 20-60s per
    # affected build and gives the operator a clean failure reason
    # instead of "docker build returned non-zero".
    try:
        from node_builder.known_bases import (
            extract_from_images, is_known_hallucinated,
        )
        hero_df = audited_artifacts.get("hero_dockerfile") or ""
        if hero_df:
            hallucinated = [
                ref for ref in extract_from_images(hero_df)
                if is_known_hallucinated(ref)
            ]
            if hallucinated:
                sh.logger.warning(
                    "/wizard/ingest-artifacts: spec=%d rejecting initial "
                    "Dockerfile — hallucinated base(s): %s",
                    request.spec_id, hallucinated,
                )
                raise BadRequestError(
                    "hallucinated base image",
                    details={
                        "reason": (
                            f"Containerizer wrote a hallucinated base "
                            f"image: {hallucinated}. These tags don't "
                            f"exist on any registry — the build would "
                            f"have failed with 'manifest unknown'. "
                            f"Re-run the wizard so the Containerizer "
                            f"picks a verified base from the curated "
                            f"allowlist."
                        ),
                        "hallucinated_bases": hallucinated,
                    },
                )
    except BadRequestError:
        raise
    except Exception:  # noqa: BLE001
        sh.logger.exception(
            "ingest-artifacts hallucinated-base check failed — "
            "letting artifact through; test-and-repair will catch it later"
        )

    # Snapshot the BEFORE state so we can report which fields actually
    # changed value (not just which fields exist on the merged dict).
    # The merge function returns a new dict, so the before-state is
    # exactly what's currently in the row.
    before = dict(spec_row.spec_json or {})
    merged = merge_artifacts_into_spec_json(before, audited_artifacts)
    fields_merged = sorted(
        k for k in set(merged.keys()) | set(before.keys())
        if before.get(k) != merged.get(k)
    )
    spec_row.spec_json = merged
    _flag_modified(spec_row, "spec_json")
    db.commit()

    return sh.IngestArtifactsResponse(
        ok=True,
        fields_merged=fields_merged,
    )


def reset_repair_budget(db, *, request, auth) -> "sh.ResetRepairBudgetResponse":
    sh.assert_build_scope(auth, build_spec_id=request.spec_id)
    spec_row = node_crud.get_build_spec_record(db, request.spec_id)
    if spec_row is None:
        raise NotFoundError(f"build_spec {request.spec_id} not found")

    spec_data = dict(spec_row.spec_json or {})
    prev_inner = int(spec_data.get("repair_attempts_inner_used", 0))
    prev_outer = int(spec_data.get("repair_attempts_outer_used", 0))
    prior_history = spec_data.get("repair_history") or []

    # Active-continuation guard: if the prior history's most recent
    # entry is < 15 minutes old, the test_and_repair stage is
    # resuming after a claim re-acquisition (worker restart, API
    # restart, claim_expires_at lapse mid-build) — NOT a fresh
    # user-triggered retry. Wiping the history in that case loses
    # all the cross-attempt memory move A relies on. Skip the reset
    # entirely; the stage will see the prior counters and continue
    # against the same budget.
    from datetime import datetime, timedelta, timezone
    if prior_history:
        try:
            last_at = prior_history[-1].get("at")
            if last_at:
                last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last_dt < timedelta(minutes=15):
                    sh.logger.info(
                        "/wizard/reset-repair-budget: spec=%d ACTIVE CONTINUATION "
                        "(last attempt %s ago) — preserving %d-entry history",
                        request.spec_id,
                        datetime.now(timezone.utc) - last_dt,
                        len(prior_history),
                    )
                    return sh.ResetRepairBudgetResponse(
                        ok=True,
                        previous_inner_used=prev_inner,
                        previous_outer_used=prev_outer,
                    )
        except Exception:  # noqa: BLE001
            sh.logger.exception("active-continuation check failed — falling through to reset")

    spec_data["repair_attempts_inner_used"] = 0
    spec_data["repair_attempts_outer_used"] = 0
    spec_data["repair_history"] = []
    if prior_history:
        archived = list(spec_data.get("repair_history_archived") or [])
        archived.extend(prior_history)
        spec_data["repair_history_archived"] = archived[-30:]

    spec_row.spec_json = spec_data
    _flag_modified(spec_row, "spec_json")
    db.commit()
    return sh.ResetRepairBudgetResponse(
        ok=True,
        previous_inner_used=prev_inner,
        previous_outer_used=prev_outer,
    )


# =============================================================================
# Generation-artifacts polling
# =============================================================================


def get_generation_artifacts(db, *, task_id: int, current_user) -> Dict[str, Any]:
    """
    Fetch the three Albedo-pattern files generated by a /generate-artifacts task.
    Parses the Containerizer's stdout JSON from the task's result_payload.log_tail.

    Returns {status: "pending"|"running"|"completed"|"failed", ...}. When completed,
    also returns {dockerfile, entrypoint_sh, build_and_push_sh, image_name, ...}.
    """
    task = node_crud.get_api_task_queue_by_id(db, task_id)
    if task is None or task.tenant_id != current_user.tenant_id:
        raise NotFoundError("Task not found")

    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)
    response: Dict[str, Any] = {"task_id": task.id, "status": status_value}

    # Pull log_tail early so both in-flight AND completed states can
    # surface the codebase pipeline's streamed stage events.
    rp = task.result_payload or {}
    log_tail = (rp.get("log_tail") or "") if isinstance(rp, dict) else ""

    # Codebase pipeline streams `{"stage":...,"status":...}` JSON lines
    # as it runs. Return them on every poll so the wizard UI can render
    # a live timeline while the task is still running. Legacy albedo
    # runs never emit these, so an empty list is the "nothing to show
    # yet" state.
    stages = sh._extract_codebase_stages(log_tail)
    if stages:
        response["stages"] = stages

    # P9e.4 — attach per-variant verification state so the wizard's
    # build-queue row can render `building → verifying → verified`
    # badges after the main build completes. We look up the
    # NodeDefinition via NODE_NAME in the task's container_env
    # (ImageBuilder + Containerizer both set it). Attached on EVERY
    # poll (including mid-build) so the UI can show the first smoke
    # task flipping a variant to `verified` the instant the build
    # finishes + workers claim smoke.
    _task_payload = task.task_payload or {}
    _container_env = (
        _task_payload.get("container_env", {})
        if isinstance(_task_payload, dict) else {}
    )
    _node_name = _container_env.get("NODE_NAME", "").strip() if isinstance(_container_env, dict) else ""
    if _node_name:
        try:
            _node = node_crud.get_node_definition_by_name_for_tenant(
                db, _node_name, current_user.tenant_id,
            )
            if _node is not None:
                response["variant_verification_status"] = _node.variant_verification_status or {}
                response["gpu_variants_registered"] = _node.gpu_variants or {}
                response["incompatible_reasons"] = _node.incompatible_reasons or {}
                # P9o — per-variant structured failure explanations
                # (LLM-written / pattern-matched). Keyed by variant,
                # shape: {reason, manual_fix, log_excerpt, source,
                # original_reason}. Empty dict when no variant has
                # failed. Wizard's failure modal reads this.
                _cm = _node.custom_metadata or {}
                response["failure_explanations"] = (
                    _cm.get("failure_explanations") or {}
                    if isinstance(_cm, dict) else {}
                )
        except Exception:
            # Not finding the node is a normal state during a build
            # (node is registered by ImageBuilder post-push, so a poll
            # during the build phase legitimately won't find it). Log
            # at debug and carry on.
            sh.logger.debug("generation-artifacts NodeDefinition lookup for %r failed",
                             _node_name, exc_info=True)

    if status_value == "failed":
        response["error"] = task.error_message or "Task failed"
        # Attach the last chunk of worker stdout/stderr so the wizard
        # can surface more than "container exited with code 1". Bounded
        # to 1 KB so the client doesn't blow up on runaway traces.
        if log_tail:
            response["log_tail_tail"] = log_tail[-1000:]
        return response

    if status_value != "completed":
        return response

    payload = task.result_payload or {}
    log_tail = payload.get("log_tail", "") or ""

    # llm-ping tasks emit a single JSON with `llm_ping` key instead of
    # the three Albedo keys. Recognise + return early — no DAG render,
    # no artifacts, just the diagnostic payload.
    ping = sh._extract_llm_ping(log_tail)
    if ping is not None:
        response.update(ping)
        return response

    artifacts = sh._extract_albedo_artifacts(log_tail)
    if not artifacts:
        response["error"] = "Task completed but no Albedo artifacts found in output"
        response["log_tail_tail"] = log_tail[-500:] if log_tail else ""
        return response

    response.update(artifacts)

    # Synthesize the 4th Albedo artifact — the node metadata `.py` file.
    # Pulled from the wizard metadata we stashed on the task at dispatch
    # time + the image name the Containerizer actually settled on. No
    # LLM: deterministic render via the shared metadata_template module so
    # the wizard's client-side preview matches byte-for-byte on commit.
    try:
        from utils.metadata_template import render, coerce_and_validate
        task_payload = task.task_payload or {}
        wiz_meta = (task_payload.get("wizard_metadata") or {}) if isinstance(task_payload, dict) else {}
        if wiz_meta:
            # Prefer the image name the Containerizer resolved (it may have
            # normalised it); fall back to wizard's expectation.
            image_name = artifacts.get("image_name") or ""
            if image_name:
                wiz_meta = {
                    **wiz_meta,
                    "executable_path": image_name if ":" in image_name else f"{image_name}:latest",
                }
            rendered = render(coerce_and_validate(wiz_meta))
            response["metadata_file"] = {
                "filename": rendered["filename"],
                "content": rendered["content"],
                "node_kind": rendered["node_kind"],
                "category": rendered["category"],
            }
    except Exception as e:
        # Never fail the artifact poll because the metadata sidecar couldn't
        # render — the wizard can still render a client-side preview
        # from its own metadata. Log for ops but return the other three.
        sh.logger.warning("generation-artifacts DAG render failed (task %s): %s", task_id, e)

    return response


def get_port_diff(db, *, task_id: int, current_user) -> Dict[str, Any]:
    """P9n — return the kernel_porter's per-variant source transforms for
    a completed /generate-artifacts task, paired with each file's BEFORE
    body so the wizard can render a side-by-side diff viewer.

    Source of data:
      - `port_plans` in the Containerizer's result JSON: variant →
        PortPlan.to_dict() which carries `source_transforms` (filename,
        new_filename, new_body) + `port_report` (tool, unsupported_apis,
        manual_cleanup_needed) + dockerfile_env + requirements_patches.
      - `source_files` in the same result: the ORIGINAL file bodies the
        Containerizer received. Paired by filename to produce the
        before/after columns.

    404 if the task doesn't exist or isn't this tenant's. 200 with empty
    variants dict when the task is still running, failed, or never
    emitted port_plans (e.g., non-deep-CUDA repo).
    """
    task = node_crud.get_api_task_queue_by_id(db, task_id)
    if task is None or task.tenant_id != current_user.tenant_id:
        raise NotFoundError("Task not found")

    rp = task.result_payload or {}
    log_tail = rp.get("log_tail", "") if isinstance(rp, dict) else ""
    artifacts = sh._extract_albedo_artifacts(log_tail) or {}

    port_plans = artifacts.get("port_plans") or {}
    source_files = artifacts.get("source_files") or []

    # Fast path: no port data yet (task running / failed / non-deep
    # repo). Return empty-but-well-formed response so the client can
    # render "nothing to diff" without a null check.
    if not isinstance(port_plans, dict) or not port_plans:
        return {"task_id": task.id, "variants": {}}

    before_by_filename: Dict[str, str] = {}
    if isinstance(source_files, list):
        for f in source_files:
            if isinstance(f, dict):
                name = f.get("filename")
                body = f.get("body")
                if isinstance(name, str) and isinstance(body, str):
                    before_by_filename[name] = body

    variants_out: Dict[str, Any] = {}
    for variant, plan in port_plans.items():
        if not isinstance(plan, dict):
            continue
        transforms_out = []
        for xform in (plan.get("source_transforms") or []):
            if not isinstance(xform, dict):
                continue
            fname = xform.get("filename")
            if not isinstance(fname, str):
                continue
            transforms_out.append({
                "filename": fname,
                "new_filename": xform.get("new_filename"),
                "before": before_by_filename.get(fname, ""),
                "after": xform.get("new_body") if xform.get("new_body") is not None
                         else before_by_filename.get(fname, ""),
                "delete": bool(xform.get("delete", False)),
            })
        port_report = plan.get("port_report") or {}
        variants_out[variant] = {
            "tool": port_report.get("tool") if isinstance(port_report, dict) else None,
            "compatibility": plan.get("compatibility"),
            "incompat_reason": plan.get("incompat_reason"),
            "transforms": transforms_out,
            "manual_cleanup_needed": (
                port_report.get("manual_cleanup_needed") or []
                if isinstance(port_report, dict) else []
            ),
            "unsupported_apis": (
                port_report.get("unsupported_apis") or []
                if isinstance(port_report, dict) else []
            ),
            "dockerfile_env": plan.get("dockerfile_env") or {},
            "requirements_patches": plan.get("requirements_patches") or {"add": [], "remove": []},
        }

    return {"task_id": task.id, "variants": variants_out}


# =============================================================================
# Tenant sharing
# =============================================================================


def share_node_with_tenant(db, *, node_definition_id: int, current_user):
    """
    Promote a user-private node to tenant-shared.

    Permission rule: the caller must own the private row OR be a
    tenant admin in the same tenant. Cross-tenant calls 403.
    """
    nd = node_crud.get_node_definition_by_id(db, node_definition_id)
    if nd is None:
        raise NotFoundError("Node not found")
    if nd.user_id is None:
        # Already tenant-shared (or system) — idempotent return.
        return nd
    if nd.tenant_id != current_user.tenant_id:
        raise ForbiddenError("Cross-tenant promotion forbidden")
    is_owner = (nd.user_id == current_user.id)
    is_tenant_admin = bool(getattr(current_user, "is_tenant_admin", False))
    is_platform_admin = bool(getattr(current_user, "is_platform_admin", False))
    if not (is_owner or is_tenant_admin or is_platform_admin):
        raise ForbiddenError("Only the node owner or a tenant admin can share this node")
    promoted = sh.crud.promote_node_definition_to_tenant(db, nd)
    return promoted
