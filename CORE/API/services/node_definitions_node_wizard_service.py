"""Business logic for `routers/node_definitions_domains/node_wizard.py`
(Gate 2b migration, openspec: stack-training-and-prewarm).

Data access -> `crud/node_definitions_node_wizard.py` (the ONLY place
`db.query` lives for this sub-module). Wizard/build helper primitives
(`_assert_node_registration_affordable`, `_node_image_slug`,
`_docs_driven_builds_enabled`, `_universal_variant_fanout_enabled`,
`_run_admission`, `_BuildSpecCreateResponse`, `node_admission`, `logger`)
are NOT owned by this migration — they stay defined in `_shared.py` and are
imported directly from there, exactly as the pre-migration router did (via
`from ._shared import *`).

`get_build_run_progress` has no 404-vs-403 trap: BOTH "run doesn't exist"
and "run exists but the caller can't view it" (co-tenant's private build)
return 404 in the pre-migration handler — there is no distinct 403 branch
to preserve here (unlike the sibling `iam_permissions` domain).
`build_and_register_node`'s tenant/name/slug guards similarly only ever
raised 400/409 in the original — no 403 in this sub-module at all.

`services/_base.py` has no `bad_request` error (only `not_found` and
`conflict`) — SHARED RULES: declare what's needed here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
import crud.node_definitions_node_wizard as crud
from routers.node_definitions_domains._shared import (
    _BuildSpecCreateResponse,
    _assert_node_registration_affordable,
    _docs_driven_builds_enabled,
    _node_image_slug,
    _run_admission,
    _universal_variant_fanout_enabled,
    HTTPException,
    logger,
    node_admission,
)
from services._base import ConflictError, NotFoundError, ServiceError


class BadRequestError(ServiceError):
    """Malformed/invalid request the pre-migration handler rejected with a
    plain 400 (router -> 400)."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


def _validate_cost_tiers(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """sz-tcp.5b — validate the wizard's optional `cost_tiers` body key.

    The WorkflowBuilder node wizard now sends `cost_tiers: {small, medium,
    large}` (credits) next to the legacy `cost_in_credits` (which stays the
    medium tier). The endpoint body is a raw `Dict[str, Any]` — there is no
    Pydantic request model to validate against — so the shape rules live
    here and surface as the 422 a malformed body deserves (same code the
    sibling node-authoring endpoints use for unparseable payloads):

      - must be an object;
      - must carry ALL THREE tier keys (small/medium/large);
      - every value a non-negative number (bool rejected: it is an int
        subclass and would silently price a node at 0/1).

    Extra tier keys are ignored. Returns the normalised dict, or None when
    the caller didn't send the key (legacy behaviour, unchanged).
    """
    raw = request.get("cost_tiers")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail="cost_tiers must be an object with small/medium/large credit values",
        )
    tiers: Dict[str, Any] = {}
    for key in ("small", "medium", "large"):
        if key not in raw:
            raise HTTPException(
                status_code=422,
                detail=f"cost_tiers is missing the required tier key '{key}'",
            )
        val = raw[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise HTTPException(
                status_code=422,
                detail=f"cost_tiers['{key}'] must be a number of credits",
            )
        if val < 0:
            raise HTTPException(
                status_code=422,
                detail=f"cost_tiers['{key}'] must be non-negative",
            )
        tiers[key] = val
    return tiers


def build_and_register_node(
    db: Session, *, request: Dict[str, Any], current_user: "models.User",  # noqa: F821
) -> "_BuildSpecCreateResponse":
    """Unified entry point for the wizard's Create Node button. Verbatim MOVE
    of the pre-migration handler body — see
    `routers/node_definitions_domains/node_wizard.py` for the endpoint
    docstring (unchanged, not duplicated here).

    Raises `BadRequestError` (400) or `ConflictError` (409) — the router maps
    each back to the exact `HTTPException` shape (status + detail body,
    string or dict) the pre-migration inline code raised.
    """
    from node_builder.build_dag.adapter import (
        from_build_local_request,
        from_wizard_inputs_request,
    )
    from node_builder.build_dag.submit import (
        assert_build_farm_available,
        assert_wizard_llm_configured,
        create_build_workflow,
    )

    # Fail fast (409 build_farm_unavailable) before any BuildSpec row / queue
    # work when this install has no build-task image (2400LR-wiz1). The
    # Containerizer image is checked below, once we know the build needs it.
    assert_build_farm_available()

    if current_user.tenant_id is None:
        raise BadRequestError("tenant required")

    # §22.5 — pre-flight credit gate. The register step at the END of
    # the workflow run charges NODE_CREATION_CREDIT_COST; users who
    # passed /wizard-preflight at wizard-mount can drop below the
    # threshold during a mid-wizard generate. Without this check we'd
    # dispatch the whole workflow, run all the work, and only fail at
    # the final register callback — wasting time and Harbor bandwidth.
    _assert_node_registration_affordable(db, current_user)

    # sz-tcp.5b — validate the optional tiered-pricing key up front, before
    # any credits/spec work, so a malformed body never dispatches a build.
    _cost_tiers = _validate_cost_tiers(request)

    # Mode detection. Three paths now:
    #   - Pre-generated artifacts (legacy, dockerfile in body) → legacy path
    #   - Image mode (wrap_base_image only, no inline/repo/extra) → §2
    #     synthesizes artifacts directly without the Containerizer LLM
    #   - Other inputs (script/repo/dockerfile-with-extras) → generate-phase
    #     workflow (Containerizer runs as the first DAG node)
    has_artifacts = bool(request.get("dockerfile"))
    inputs_only = not has_artifacts

    from node_builder.io_adapter.image_mode import (
        is_image_mode_submission,
        build_image_mode_artifacts,
    )
    image_mode = inputs_only and is_image_mode_submission(request)
    include_generate_phase = inputs_only and not image_mode

    if inputs_only:
        # Must have at least one input source the LLM can work from.
        if not any(request.get(k) for k in (
            "source_repo_url", "inline_source_body", "wrap_base_image",
        )):
            # Log the keys we received so the rejection's cause is
            # obvious in the API logs without revealing payload secrets.
            logger.warning(
                "build-and-register-node 400: no input source supplied. "
                "request keys=%s, node_name=%r, source_repo_url=%r, "
                "inline_source_body_len=%d, wrap_base_image=%r",
                sorted(request.keys()),
                request.get("node_name"),
                request.get("source_repo_url"),
                len(request.get("inline_source_body") or ""),
                request.get("wrap_base_image"),
            )
            raise BadRequestError(
                "Inputs-only submission requires one of source_repo_url, "
                "inline_source_body, or wrap_base_image.",
                details={
                    "error": "Inputs-only submission requires one of "
                             "source_repo_url, inline_source_body, or "
                             "wrap_base_image.",
                    "received_keys": sorted(request.keys()),
                },
            )
        if not request.get("node_name"):
            logger.warning(
                "build-and-register-node 400: node_name missing. "
                "request keys=%s",
                sorted(request.keys()),
            )
            raise BadRequestError(
                "node_name required",
                details={
                    "error": "node_name required",
                    "received_keys": sorted(request.keys()),
                },
            )

        # Name-uniqueness gate. POST /node_definitions/ has create-or-
        # update semantics (the register_node stage + deferred-approval
        # both rely on it). For a FRESH wizard submission that's almost
        # always wrong — silently overwriting a working Node's metadata
        # because the user reused a name is the surprise failure mode
        # Tim hit manually 2026-05-21. Reject early with 409 so the
        # wizard surfaces a clear "pick a different name" error
        # before the workflow_run dispatches.
        #
        # The check also catches SLUG collisions (Tim's 2026-05-22
        # follow-up): two distinct node names whose harbor image-name
        # slug is identical — e.g. "Python" / "python" / "PYTHON" all
        # → `python`, or "My Node" / "My-Node" / "My_Node" all →
        # `my-node`. Both would push to the same harbor tag and
        # deleting one would clobber the other.
        candidate_name = request["node_name"].strip()
        candidate_slug = _node_image_slug(candidate_name)
        candidate_lower = candidate_name.lower()
        tenant_nodes = crud.list_tenant_node_definitions(db, current_user.tenant_id)
        for node in tenant_nodes:
            node_name_norm = (node.name or "").strip().lower()
            node_slug = _node_image_slug(node.name)
            if node_name_norm == candidate_lower:
                raise ConflictError(
                    f"A node named {request['node_name']!r} already exists",
                    details={
                        "error": (
                            f"A node named {request['node_name']!r} already "
                            f"exists in this tenant (case-insensitive). "
                            f"Pick a different name or delete the existing "
                            f"node before re-submitting."
                        ),
                        "existing_node_definition_id": node.id,
                        "collision": "name",
                    },
                )
            if node_slug == candidate_slug:
                raise ConflictError(
                    f"Node name {request['node_name']!r} would collide on "
                    f"harbor slug {candidate_slug!r}",
                    details={
                        "error": (
                            f"Node name {request['node_name']!r} would build "
                            f"to the same harbor image as the existing node "
                            f"{node.name!r} (both slug to {candidate_slug!r}). "
                            f"Pick a name that differs by more than "
                            f"case/punctuation."
                        ),
                        "existing_node_definition_id": node.id,
                        "existing_node_name": node.name,
                        "candidate_slug": candidate_slug,
                        "collision": "image_slug",
                    },
                )

        # Smart-category canonicalization. If the wizard passed
        # node_type, fold it against existing tenant categories so
        # "BEST" → "Best" when "Best" already exists (manual-test
        # ask from Tim 2026-05-22 — "best and BEST should go to the
        # same place"). Skipped on empty / Custom; "Custom" remains
        # the legacy fallback.
        if (request.get("node_type") or "").strip():
            from node_builder.category_suggester import (
                _list_existing_categories,
                canonicalize_against_existing,
            )
            existing = _list_existing_categories(
                db, current_user.tenant_id,
            )
            request["node_type"] = canonicalize_against_existing(
                request["node_type"], existing,
            )

        # Admission gate on repo URLs — same allowlist the legacy
        # /generate-artifacts applied before burning credits.
        repo_url = request.get("source_repo_url")
        if repo_url and isinstance(repo_url, str) and repo_url.startswith((
            "http://", "https://", "git@",
        )):
            _run_admission("repo", {"repo_url": repo_url}, current_user)

        spec = from_wizard_inputs_request(request, tenant_id=current_user.tenant_id)

        # wizard-rebuild §4 chunk 3 — universal mode dispatcher. Image
        # mode always routes through it (chunk 2 baseline). Git /
        # Script / Dockerfile route through it when
        # WIZARD_DOCS_DRIVEN_BUILDS_ENABLED is set — the deterministic
        # path handles those shapes (proven by
        # test_buildplan_script_hardening.py: apt inference, variant gate,
        # I/O wrap; Git's extractors/git.py uses the repo-docs cache +
        # an EXTRACTIVE-only LLM call, not the Containerizer generate
        # phase). sz-wizard-rebuild.4 retired the earlier `and not
        # _is_git` guard that pinned Git to the legacy Containerizer-LLM
        # path unconditionally — the deterministic git extractor is now
        # a safe replacement, so Git gets the same flag-gated treatment
        # as Dockerfile/Script: flag off → legacy Containerizer path,
        # flag on → BuildPlan dispatcher.
        use_buildplan_dispatcher = image_mode or _docs_driven_builds_enabled()
        if use_buildplan_dispatcher:
            from node_builder.build_plan.dispatcher import (
                dispatch_extraction,
            )
            from node_builder.build_plan.renderer import render_artifacts
            from node_builder.build_dag.adapter import apply_build_plan
            try:
                result = dispatch_extraction(
                    request, db=db, tenant_id=current_user.tenant_id,
                )
            except ValueError as exc:
                # Unparseable input shape — surface as 400 with the
                # error message the dispatcher provided.
                raise BadRequestError(str(exc), details={"error": str(exc)})
            plan = result.plan
            spec.image_name = result.image_name
            # Variant fan-out OR hero-only render. Both paths write the
            # same hero_dockerfile/entrypoint/build_and_push fields;
            # fan-out additionally populates spec.variants + .hero_variant
            # + .incompatible_variants.
            if _universal_variant_fanout_enabled():
                from node_builder.build_dag.variant_strategy import (
                    apply_variant_fanout,
                )
                apply_variant_fanout(spec, plan)
            else:
                artifacts = render_artifacts(plan)
                spec.hero_dockerfile = artifacts["dockerfile"]
                spec.entrypoint_sh = artifacts["entrypoint_sh"]
                spec.build_and_push_sh = artifacts["build_and_push_sh"]
            apply_build_plan(
                spec, plan,
                repo_docs_extract_id=result.repo_docs_extract_id,
            )
            # No more Containerizer-LLM generate phase — the
            # deterministic extractor already populated all three
            # artifacts on the spec.
            include_generate_phase = False
            logger.info(
                "build-and-register-node: %s-mode BuildPlan extracted "
                "(confidence=%.2f tier=%s repo_docs=%s)",
                result.mode, plan.confidence, plan.tier(),
                result.repo_docs_extract_id or "—",
            )
            from utils import wizard_metrics as _wm
            _wm.record_extraction(result="success", mode=result.mode)
    else:
        spec = from_build_local_request(request, tenant_id=current_user.tenant_id)

        # §22.3 — sanitize + lint every Dockerfile in the spec before
        # persisting. Cheapest possible failure mode for a wizard build
        # is a deterministically-broken Dockerfile (empty `pip install`,
        # missing FROM) that burns through the worker's 3-attempt budget
        # in seconds. Sanitize first (auto-fix the empty package-install
        # pattern the wizard LLM keeps emitting), then lint (block on
        # anything still broken).
        from utils.dockerfile_lint import sanitize_dockerfile, lint_dockerfile
        _sanitize_log: List[Dict[str, Any]] = []
        cleaned_hero, removed_hero = sanitize_dockerfile(spec.hero_dockerfile or "")
        if removed_hero:
            _sanitize_log.append({"target": "hero_dockerfile", "removed": removed_hero})
            spec.hero_dockerfile = cleaned_hero
        for v in (spec.variants or []):
            cv, rv = sanitize_dockerfile(v.dockerfile or "")
            if rv:
                _sanitize_log.append({"target": f"variant[{v.variant}]", "removed": rv})
                v.dockerfile = cv
        if _sanitize_log:
            logger.info(
                "build-and-register-node: sanitized %d Dockerfile line(s): %s",
                sum(len(s["removed"]) for s in _sanitize_log), _sanitize_log,
            )

        # Lint AFTER sanitize. Anything reported here is something the
        # auto-fix couldn't safely repair — refuse the spec rather than
        # let it eat credits and worker attempts.
        _all_issues: List[Dict[str, Any]] = []
        for label, df in (
            ("hero_dockerfile", spec.hero_dockerfile),
            *((f"variant[{v.variant}]", v.dockerfile) for v in (spec.variants or [])),
        ):
            for issue in lint_dockerfile(df or ""):
                if issue.severity == "error":
                    _all_issues.append({
                        "target": label,
                        "code": issue.code,
                        "message": issue.message,
                        "line": issue.line,
                    })
        if _all_issues:
            raise BadRequestError(
                "Generated Dockerfile failed validation. Re-run the "
                "wizard's describe step or edit the Dockerfile before "
                "submitting.",
                details={
                    "error": "Generated Dockerfile failed validation. "
                             "Re-run the wizard's describe step or edit "
                             "the Dockerfile before submitting.",
                    "issues": _all_issues,
                },
            )

        # Licence gate for SYSTEM deps. The SPDX allowlist only vets the
        # wrapped repo; it's blind to apt packages a Dockerfile pulls in
        # (e.g. poppler=GPL for PDF). Warn-don't-block: surface a notice
        # so the operator sees we're shipping a copyleft binary in an
        # image the platform advertises as permissive-only.
        from utils.license_allowlist import apt_license_warnings
        _lic_warnings: List[str] = []
        for df in (spec.hero_dockerfile, *(v.dockerfile for v in (spec.variants or []))):
            _lic_warnings.extend(apt_license_warnings(df or ""))
        if _lic_warnings:
            logger.warning(
                "build-and-register-node: copyleft system deps in spec: %s",
                "; ".join(dict.fromkeys(_lic_warnings)),
            )

    # Generate-phase (Containerizer) builds need the Containerizer image AND a
    # model for the ``wizard`` AI tool; refuse BEFORE any row is written, with
    # a message the operator can act on (409 build_farm_unavailable /
    # wizard_llm_not_configured). Image-source builds (and the deterministic
    # BuildPlan path) never reach the Containerizer, so they skip both.
    if include_generate_phase:
        assert_build_farm_available(needs_containerizer=True)
        assert_wizard_llm_configured()

    # sz-tcp.5b — carry the wizard's explicit tiered pricing onto the spec so
    # it persists in build_specs.spec_json and the register stage can write
    # node_definitions.cost_credits_by_tier + cost_basis='manual' verbatim
    # instead of letting utils/node_cost re-derive the 0.5x/1x/2.5x ratios.
    # models.py keeps a CHECK that a non-empty tier map must agree with
    # cost_in_credits on 'medium', so when the caller priced the tiers
    # explicitly the medium tier becomes authoritative for cost_in_credits.
    if _cost_tiers is not None:
        spec.cost_tiers = _cost_tiers
        spec.cost_basis = "manual"
        _medium = _cost_tiers["medium"]
        if int(spec.cost_in_credits) != int(_medium):
            spec.cost_in_credits = int(_medium)

    rec = models.BuildSpecRecord(
        tenant_id=current_user.tenant_id,
        spec_json=spec.model_dump(),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    logger.info(
        "build-and-register-node: persisted spec id=%d mode=%s",
        rec.id, "inputs" if inputs_only else "artifacts",
    )

    # Stash the build_spec_id on the spec object so the submit helper
    # can embed it into each node's data.container_env.
    spec._build_spec_id = rec.id  # type: ignore[attr-defined]

    workflow = create_build_workflow(
        db, spec, user_id=current_user.id,
        include_generate_phase=include_generate_phase,
    )

    # Trigger the workflow run so the orchestrator dispatches the
    # build-task containers (one per DAG node, fanned out across
    # workers). Without this, the workflow exists but no work happens.
    workflow_run_id = None
    try:
        from services.workflow_orchestrator import dispatch_trigger_run
        wf = crud.get_workflow_by_id(db, workflow["workflow_id"])
        run = dispatch_trigger_run(
            db, workflow=wf, triggered_by_user_id=current_user.id,
            extra_inputs={"build_spec_id": rec.id},
        )
        workflow_run_id = getattr(run, "id", None)
        # Wire build_spec back to the run for retention + audit.
        rec.workflow_run_id = workflow_run_id
        db.commit()
    except Exception as e:
        logger.warning("build-and-register-node: trigger failed: %s", e)

    # Surface this build in the Wizard Queue (observational only — never
    # let surfacing break the build). No backfill; only new runs.
    if workflow_run_id is not None:
        try:
            from services import wizard_queue_emitters as _wqe
            _target = (
                getattr(spec, "node_name", None)
                or getattr(spec, "name", None)
                or getattr(rec, "node_name", None)
            )
            # C5 follow-up — stamp the scope/variant THIS build dispatched
            # with so Wizard "Retry" re-dispatches the SAME scope instead of
            # always falling back to full_fanout (wizard_queue_emitters._retry_build
            # reads extra["rebuild_scope"] / extra["rebuild_variant"]). The DAG
            # this submit created builds hero + spec.variants, so a spec with
            # no extra variants is a single-variant build.
            _extra_variants = list(getattr(spec, "variants", None) or [])
            if not _extra_variants:
                _rebuild_extra = {
                    "rebuild_scope": _wqe.SCOPE_SINGLE_VARIANT,
                    "rebuild_variant": getattr(spec, "hero_variant", None) or "hero",
                }
            else:
                _rebuild_extra = {"rebuild_scope": _wqe.SCOPE_FULL_FANOUT}
            _run = _wqe.emit_build_run(
                db,
                wizard_type=models.WizardRunType.NODE,
                workflow_run_id=workflow_run_id,
                target_name=_target,
                target_ref_type="node",
                target_ref_id=str(getattr(rec, "id", "")),
                creator=current_user,
                log_source="container_build",
            )
            if _run is not None:
                _run.extra = {**(_run.extra or {}), **_rebuild_extra}
                db.commit()
        except Exception as _e:  # noqa: BLE001
            logger.debug("wizard-queue emit (node build) failed: %s", _e)

    return _BuildSpecCreateResponse(
        build_spec_id=rec.id,
        workflow_id=workflow["workflow_id"],
        workflow_run_id=workflow_run_id,
    )


def get_build_run_progress(
    db: Session, *, workflow_run_id: int, current_user: "models.User",  # noqa: F821
) -> Dict[str, Any]:
    """δ — progress poll for a build dispatched via /build-and-register-node.
    Verbatim MOVE of the pre-migration handler body — see
    `routers/node_definitions_domains/node_wizard.py` for the endpoint
    docstring (unchanged, not duplicated here).

    Raises `NotFoundError` (404) for both "no such run" and "caller can't
    view this run" — there is no 403 branch on this endpoint (see module
    docstring).
    """
    run = crud.get_workflow_run_for_tenant(db, workflow_run_id, current_user.tenant_id)
    if not run:
        raise NotFoundError("Workflow run not found")
    # Per-workflow gate: the progress poll exposes run status + aggregated
    # log_tail. Tenant-scoping alone let any co-tenant user read the logs of a
    # private workflow run by run id. A build the caller launched is owned by
    # them (derived ownership on the per-tenant build workflow), so the wizard's
    # own BuildProgressCard poll still passes; a co-tenant's private run is hidden.
    from resource_permissions import can_view_workflow_run
    if not can_view_workflow_run(db, current_user, run):
        raise NotFoundError("Workflow run not found")

    raw_status = run.status.value if hasattr(run.status, "value") else str(run.status)
    raw_status = (raw_status or "").lower()
    status_map = {
        "queued": "pending",
        "pending": "pending",
        "pending_action": "running",
        "running": "running",
        "blocked": "running",
        "success": "completed",
        "completed": "completed",
        "failure": "failed",
        "failed": "failed",
    }
    status_value = status_map.get(raw_status, raw_status or "pending")

    child_tasks = crud.list_child_tasks_for_run(db, workflow_run_id)

    # Defensive cross-check — if WorkflowRun.status got marked
    # completed/success while ANY child task is FAILED/EXPIRED, the
    # orchestrator has lied (race during status reconciliation, or
    # downstream task failed after the parent task completion hook
    # already set the run to COMPLETED). The wizard's progress bar
    # transitions to a green "Done" the moment we return "completed"
    # and stops polling, so a lying "completed" persists in the UI
    # forever. Demote to "failed" whenever there's any child task
    # whose terminal status is FAILED — the actual user-visible
    # outcome is failed regardless of how the workflow_run row
    # was marked.
    if status_value == "completed":
        _failed_statuses = (
            models.ApiTaskStatus.FAILED,
            models.ApiTaskStatus.EXPIRED,
            models.ApiTaskStatus.CANCELLED,
        )
        failed_children = [t for t in child_tasks if t.status in _failed_statuses]
        if failed_children:
            status_value = "failed"

    log_parts: List[str] = []
    for t in child_tasks:
        rp = t.result_payload or {}
        body = (rp.get("log_tail") or "") if isinstance(rp, dict) else ""
        # task_payload sometimes encodes the variant name in its
        # container_env; surface it so a multi-variant build's logs are
        # easy to scan ("hero" vs "cuda" vs "rocm").
        tp = t.task_payload or {}
        env = tp.get("container_env", {}) if isinstance(tp, dict) else {}
        variant = env.get("VARIANT") or env.get("variant") or ""
        tstatus = t.status.value if hasattr(t.status, "value") else str(t.status)
        header = f"[task {t.id}" + (f" ({variant})" if variant else "") + f" status={tstatus}]"
        if body:
            log_parts.append(f"{header}\n{body}".strip())
        else:
            log_parts.append(header)

    log_tail = "\n\n".join(log_parts)

    response: Dict[str, Any] = {
        "task_id": workflow_run_id,
        "workflow_run_id": workflow_run_id,
        "status": status_value,
        "log_tail": log_tail,
        "child_task_count": len(child_tasks),
    }
    if run.error_message:
        response["error"] = run.error_message
    if run.started_at:
        response["started_at"] = run.started_at.isoformat()
    if run.completed_at:
        response["completed_at"] = run.completed_at.isoformat()

    # Bridge the native build status/log tail onto its Wizard Queue row
    # (idempotent; no-op if this build isn't surfaced). Never let the
    # queue mirror break the poll.
    try:
        from services import wizard_queue_emitters as _wqe
        _wqe.reconcile_workflow_run_status(
            db, workflow_run_id, status_value, log_tail)
    except Exception as _e:  # noqa: BLE001
        logger.debug("wizard-queue reconcile failed for run %s: %s",
                     workflow_run_id, _e)

    return response


def list_external_base_images() -> Dict[str, Any]:
    """The base-image allowlist an external author must build against.
    Verbatim MOVE of the pre-migration handler body — see
    `routers/node_definitions_domains/node_wizard.py` for the endpoint
    docstring (unchanged, not duplicated here). No DB access; no
    `ServiceError` cases."""
    al = node_admission.load_allowlist()
    return {
        "allowed_from_prefixes": al.get("allowed_from_prefixes") or [],
        "allowed_image_prefixes_for_image_mode": al.get("allowed_image_prefixes_for_image_mode") or [],
        "allowed_git_host_patterns": al.get("allowed_git_host_patterns") or [],
        "mode": al.get("mode"),
    }
