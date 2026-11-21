"""Business logic for the `workers_fleet` node_definitions domain
(routers/node_definitions_domains/workers_fleet.py — Gate 2b migration).

Companion to `crud/node_definitions_workers_fleet.py` (the only place this
slice's `db.query`/`db.get` lives). The router stays HTTP-only: parse input,
call a function here, map a `ServiceError` to `HTTPException`, shape the
response — same convention as the sibling `task_queue_scheduler` domain
migration (`services/node_defs_task_queue_service.py`).

Extracted verbatim from the router during the Gate-2b migration —
behaviour-preserving MOVE. Every function below is the same logic that used
to live inline in the router handler of the same name; only the data access
now goes through `crud.node_definitions_workers_fleet` and the two bare
`HTTPException` raises now go through `ServiceError` subclasses.

`_shared` (logger, response/request models, `assert_build_scope`, etc.) is
imported from, never edited, as `sh` — matching
`services/node_defs_task_queue_service.py`. `os`, `datetime`/`timezone`,
`flag_modified` and `models` are imported directly here exactly as `_shared`
itself does, rather than reached through `sh.*`, to keep the (already very
long) `repair_artifacts` body as close to a byte-identical move as possible.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import models
from crud import node_definitions_workers_fleet as node_crud
from routers.node_definitions_domains import _shared as sh
from services._base import NotFoundError, ServiceError


# =============================================================================
# Local error types (services/_base.py has not_found/conflict but no
# bad_request — SHARED RULES: declare what's needed here).
# =============================================================================


class BadRequestError(ServiceError):
    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# Router-side code -> HTTP status map. Mirrors node_defs_task_queue_service.py.
STATUS_BY_CODE = {
    "not_found": 404,
    "bad_request": 400,
}


# =============================================================================
# get_wizard_cluster_capabilities — GET /wizard/cluster-capabilities
# =============================================================================

def get_wizard_cluster_capabilities(db: Session, current_user: "models.User") -> "sh.ClusterCapabilitiesResponse":
    from services.cluster_capabilities import (
        describe_capabilities_for_llm,
        get_cluster_capabilities,
        pick_best_capability,
        rank_capability,
    )
    caps = get_cluster_capabilities(db, tenant_id=current_user.tenant_id)
    # Sort via rank_capability hint table — known accelerators ordered
    # by perf priority, unknown accelerators alphabetical between
    # known-accelerator-tier and CPU. New worker-advertised
    # accelerators (habana, tpu, etc.) flow through this sort cleanly.
    available_sorted = sorted(caps, key=rank_capability)
    return sh.ClusterCapabilitiesResponse(
        available=available_sorted,
        recommended=pick_best_capability(caps),
        description=describe_capabilities_for_llm(caps),
    )


# =============================================================================
# repair_artifacts — POST /wizard/repair-artifacts
# =============================================================================

def repair_artifacts(db: Session, *, request: "sh.RepairArtifactsRequest",
                      auth: "sh.BuildAuthContext") -> "sh.RepairArtifactsResponse":
    """Thin Wizard Queue wrapper (wizard-queue-redesign GE.WQR.4). Creates a
    BUILD_REPAIR run before the attempt, delegates to _repair_artifacts_impl
    (the unchanged original handler body), then sets a terminal status from
    the outcome and appends the repair summary as one log line. A queue
    failure must never break the underlying repair — every wizard_queue call
    here is wrapped and only logged on failure."""
    from services import wizard_queue as _wq
    from utils.secret_redaction import redact as _redact

    target_name = f"build_spec:{request.spec_id}"
    tenant_id_for_run = None
    try:
        _spec_row_for_name = node_crud.get_build_spec_record(db, request.spec_id)
        if _spec_row_for_name is not None:
            tenant_id_for_run = getattr(_spec_row_for_name, "tenant_id", None)
            _sj = _spec_row_for_name.spec_json or {}
            target_name = (
                (_sj.get("node_name") if isinstance(_sj, dict) else None)
                or target_name
            )
    except Exception:  # noqa: BLE001
        pass

    creator = getattr(auth, "user", None)
    wizard_run = None
    try:
        wizard_run = _wq.create_run(
            db,
            wizard_type=models.WizardRunType.BUILD_REPAIR,
            target_name=str(target_name),
            target_ref_type="build_spec",
            target_ref_id=str(request.spec_id),
            creator=creator,
            system=(creator is None),
            tenant_id=(tenant_id_for_run if creator is None else None),
            is_platform_level=(False if creator is None and tenant_id_for_run is not None else None),
            log_source="llm_step",
        )
    except Exception as _e:  # noqa: BLE001
        sh.logger.debug("wizard-queue emit (build repair) failed: %s", _e)

    try:
        response = _repair_artifacts_impl(db, request=request, auth=auth)
    except Exception as e:
        if wizard_run is not None:
            try:
                _wq.append_log(db, wizard_run, f"repair attempt error: {e}",
                               level="error", commit=False)
                _wq.set_status(db, wizard_run, models.WizardRunStatus.FAILED,
                               error_message=_redact(str(e)))
            except Exception as _e2:  # noqa: BLE001
                sh.logger.debug("wizard-queue fail-mark (build repair) failed: %s", _e2)
        raise

    if wizard_run is not None:
        try:
            summary = (
                response.explanation
                or "; ".join(response.rejection_reasons or [])
                or ("repair attempt accepted" if response.accepted
                    else "repair attempt rejected")
            )
            terminal = (models.WizardRunStatus.SUCCEEDED if response.accepted
                       else models.WizardRunStatus.FAILED)
            _wq.append_log(db, wizard_run, summary, level="info", commit=False)
            _wq.set_status(
                db, wizard_run, terminal,
                error_message=(None if response.accepted else _redact(summary)),
            )
        except Exception as _e:  # noqa: BLE001
            sh.logger.debug("wizard-queue terminal-mark (build repair) failed: %s", _e)

    return response


def _repair_artifacts_impl(db: Session, *, request: "sh.RepairArtifactsRequest",
                           auth: "sh.BuildAuthContext") -> "sh.RepairArtifactsResponse":
    sh.assert_build_scope(auth, build_spec_id=request.spec_id)
    from node_builder.repair_loop import (
        FailureContext,
        build_repair_user_prompt,
        is_image_mode_spec,
        system_prompt_for,
        validate_repair_response,
    )

    if request.mode not in ("inner", "outer"):
        raise BadRequestError(f"mode must be 'inner' or 'outer', got {request.mode!r}")

    spec_row = node_crud.get_build_spec_record(db, request.spec_id)
    if spec_row is None:
        raise NotFoundError(f"build_spec {request.spec_id} not found")

    spec_data = dict(spec_row.spec_json or {})
    inner_used = int(spec_data.get("repair_attempts_inner_used", 0))
    outer_used = int(spec_data.get("repair_attempts_outer_used", 0))
    inner_max = int(spec_data.get("repair_attempts_inner_max", 30))
    # wizard-rebuild §7c — outer budget scales with BuildPlan
    # confidence tier (high=3, medium=8, low=15). Specs without a
    # BuildPlan keep the legacy 10. Per-spec overrides
    # (`repair_attempts_outer_max`) still win — used by the §7d
    # "Retry with elevated budget" recovery action.
    _bp = spec_data.get("build_plan") if isinstance(spec_data.get("build_plan"), dict) else None
    if "repair_attempts_outer_max" in spec_data:
        outer_max = int(spec_data["repair_attempts_outer_max"])
    elif _bp and _bp.get("mode"):
        try:
            from node_builder.build_plan.types import BuildPlan as _BP
            outer_max = _BP(**_bp).repair_budget()
        except Exception:
            outer_max = 10
    else:
        outer_max = 10
    # Hard-library budget bump: instead of a curated _HARD_LIBS set
    # (which only covers libs we hand-curated), bump the outer budget
    # for ANY library the profiler has flagged as accelerator-required
    # (gpu_required in {CUDA, ROCM, INTEL, NPU, TPU, ...} — anything
    # that isn't CPU or UNKNOWN). The library_profiler.profile() call
    # learns this from PyPI metadata + transitive deps for libraries
    # the curated seed doesn't cover, so coverage extends automatically.
    try:
        from node_builder.install_recipes import libraries_referenced_in_spec
        spec_libs = libraries_referenced_in_spec(spec_data)
        hard_libs_found: list[str] = []
        if spec_libs:
            from node_builder.gpu_library_index import _normalise
            for lib in spec_libs:
                norm = _normalise(lib)
                row = node_crud.get_library_profile(db, norm)
                if row and row.gpu_required and row.gpu_required not in ("CPU", "UNKNOWN"):
                    hard_libs_found.append(f"{norm}({row.gpu_required})")
        if hard_libs_found and outer_max < 20:
            outer_max = 20
            sh.logger.info(
                "/wizard/repair-artifacts: spec=%d bumped outer_max to 20 — accelerator-required lib detected (%s)",
                request.spec_id, hard_libs_found,
            )
    except Exception:  # noqa: BLE001
        sh.logger.exception("hard-lib budget-bump check failed — keeping default")

    # Budget check. The stage tracks its own counter (request.attempt_number)
    # but we enforce server-side too — protects against a runaway stage
    # that loses its place and burns through tokens.
    if request.mode == "inner" and inner_used >= inner_max:
        return sh.RepairArtifactsResponse(
            accepted=False, mode=request.mode, attempt_number=request.attempt_number,
            rejection_reasons=[f"inner repair budget exhausted ({inner_used}/{inner_max})"],
            budget_remaining_inner=0,
            budget_remaining_outer=max(outer_max - outer_used, 0),
        )
    if request.mode == "outer" and outer_used >= outer_max:
        return sh.RepairArtifactsResponse(
            accepted=False, mode=request.mode, attempt_number=request.attempt_number,
            rejection_reasons=[f"outer repair budget exhausted ({outer_used}/{outer_max})"],
            budget_remaining_inner=max(inner_max - inner_used, 0),
            budget_remaining_outer=0,
        )

    failure = FailureContext(
        exit_code=int(request.failure.get("exit_code", -1)),
        stdout_tail=str(request.failure.get("stdout_tail", "")),
        stderr_tail=str(request.failure.get("stderr_tail", "")),
        produced_output=bool(request.failure.get("produced_output", False)),
        output_files=list(request.failure.get("output_files", [])),
        input_seed=dict(request.failure.get("input_seed", {})),
        # --help grounding: real command surface the stage probed from the
        # built image (present only on missing/hallucinated-command fails).
        binary_reality=(str(request.failure.get("binary_reality") or "") or None),
    )

    # wizard-rebuild §0b — Infra-vs-plan classifier short-circuit.
    # Before burning an LLM call, check whether this looks like an
    # infrastructure transient (Harbor 502, worker disconnect, mirror
    # flake, etc.). Up to 3 free retries per spec are granted for
    # infra-transient failures; after that, fall through to normal
    # repair so a persistently-stuck spec doesn't loop forever.
    from node_builder.repair.failure_classifier import classify
    from utils import wizard_metrics
    # Mode label derived from spec shape — used by §0c telemetry.
    # Pre-§3 specs don't carry an explicit mode field; infer.
    _spec_mode = (
        "image" if spec_data.get("wrap_base_image") and not spec_data.get("inline_source_body")
                and not spec_data.get("source_repo_url") else
        "git" if spec_data.get("source_repo_url") else
        "script" if spec_data.get("inline_source_body") else
        "legacy"
    )
    classification = classify(failure)
    MAX_FREE_INFRA_RETRIES = 3
    if classification.category == "infra-transient":
        free_used = int(spec_data.get("infra_transient_retries_used", 0))
        if free_used < MAX_FREE_INFRA_RETRIES:
            spec_data["infra_transient_retries_used"] = free_used + 1
            history = list(spec_data.get("repair_history", []))
            history.append({
                "attempt": request.attempt_number,
                "mode": request.mode,
                "failure_reason": (
                    f"exit={failure.exit_code} produced_output={failure.produced_output}"
                ),
                "llm_explanation": (
                    f"infra-transient ({classification.matched_pattern}) — "
                    f"{classification.reason}; free retry "
                    f"{free_used + 1}/{MAX_FREE_INFRA_RETRIES}, no LLM call"
                ),
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": "infra-transient-retry",
            })
            spec_data["repair_history"] = history
            spec_row.spec_json = spec_data
            flag_modified(spec_row, "spec_json")
            db.commit()
            sh.logger.info(
                "/wizard/repair-artifacts: infra-transient short-circuit "
                "for spec=%d pattern=%s free_used=%d/%d",
                request.spec_id, classification.matched_pattern,
                free_used + 1, MAX_FREE_INFRA_RETRIES,
            )
            wizard_metrics.record_repair(outcome="infra-retry", mode=_spec_mode)
            # accepted=True with no updates — caller rebuilds as-is.
            # Budget remainings unchanged: we didn't consume a real
            # repair attempt.
            return sh.RepairArtifactsResponse(
                accepted=True,
                mode=request.mode,
                attempt_number=request.attempt_number,
                explanation=(
                    f"infra-transient retry ({classification.matched_pattern}): "
                    f"{classification.reason}"
                ),
                budget_remaining_inner=max(inner_max - inner_used, 0),
                budget_remaining_outer=max(outer_max - outer_used, 0),
            )
        # Free retries exhausted on persistent infra issue. Log and
        # fall through to normal repair so the LLM gets a chance to
        # propose a workaround (e.g., pin a different mirror), and
        # so a really-stuck spec eventually hits the budget cap and
        # bails with a clear failure rather than looping.
        sh.logger.warning(
            "/wizard/repair-artifacts: spec=%d hit max free infra retries "
            "(%d); falling through to normal repair for pattern=%s",
            request.spec_id, MAX_FREE_INFRA_RETRIES,
            classification.matched_pattern,
        )

    # wizard-rebuild §7a-2 — Plan-divergence revert.
    # Before calling the LLM, check whether hero_dockerfile still
    # matches the canonical render of the spec's BuildPlan. If it
    # diverged (most commonly because an earlier outer-repair returned
    # a free-form Dockerfile that got written verbatim), revert to
    # the canonical render. Consumes a build attempt, NOT an LLM
    # budget attempt — the next iteration starts from a clean state.
    from node_builder.build_plan.diff import diff_against_plan
    canonical_from_plan = diff_against_plan(spec_data)
    if canonical_from_plan is not None:
        spec_data["hero_dockerfile"] = canonical_from_plan
        plan_dict = dict(spec_data.get("build_plan") or {})
        # Treat the revert as an applied delta-of-sorts so the audit
        # trail makes sense ("attempt N was a revert to the canonical
        # render"). Doesn't go into BuildPlan.applied_deltas (those
        # are field-level deltas) — surfaces in repair_history only.
        history = list(spec_data.get("repair_history") or [])
        history.append({
            "attempt": request.attempt_number,
            "mode": request.mode,
            "failure_reason": (
                f"exit={failure.exit_code} produced_output={failure.produced_output}"
            ),
            "llm_explanation": (
                "plan-divergence revert — hero_dockerfile diverged from "
                "render(BuildPlan); reverted to canonical render. No "
                "LLM call; no budget consumed."
            ),
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "plan-revert",
        })
        spec_data["repair_history"] = history
        spec_row.spec_json = spec_data
        flag_modified(spec_row, "spec_json")
        db.commit()
        sh.logger.info(
            "/wizard/repair-artifacts: spec=%d plan-divergence revert "
            "fired — caller rebuilds from canonical render",
            request.spec_id,
        )
        wizard_metrics.record_repair(outcome="reverted", mode=_spec_mode)
        return sh.RepairArtifactsResponse(
            accepted=True,
            mode=request.mode,
            attempt_number=request.attempt_number,
            explanation=(
                "plan-divergence revert: hero_dockerfile was inconsistent "
                "with render(BuildPlan); restored canonical render. "
                "Rebuild and retry."
            ),
            updated_dockerfile=canonical_from_plan,
            budget_remaining_inner=max(inner_max - inner_used, 0),
            budget_remaining_outer=max(outer_max - outer_used, 0),
        )

    history = list(spec_data.get("repair_history", []))

    # Move C — known-working install recipes for libraries this build
    # imports, tenant-scoped (GB.LEAK.5). If a prior build in this
    # tenant successfully installed nerfstudio/torch/etc. on the same
    # accelerator, prepend that working recipe to the repair LLM prompt
    # so it doesn't have to re-derive the install steps from scratch.
    # Another tenant's recipes are never returned.
    # Move D — when no recipe is captured yet, fall back to the
    # project's own README install section (cold-start RAG).
    install_recipes_block = ""
    install_docs_block = ""
    try:
        from node_builder.install_recipes import (
            libraries_referenced_in_spec, render_recipes_for_prompt,
        )
        _libs = libraries_referenced_in_spec(spec_data)
        if _libs:
            install_recipes_block = render_recipes_for_prompt(
                db,
                tenant_id=spec_row.tenant_id,
                libraries=_libs,
                accelerator=(spec_data.get("process_type") or "CPU"),
            )
            # Move D fallback: only fetch install docs when we DON'T
            # have a captured recipe — saves PyPI/GitHub round-trips
            # when we already have ground truth.
            if not install_recipes_block:
                from node_builder.install_docs import render_install_docs_for_prompt
                install_docs_block = render_install_docs_for_prompt(db, libraries=_libs)
    except Exception:  # noqa: BLE001
        sh.logger.exception("install_recipes/docs read failed — proceeding without recipe hints")

    # Inject the cluster's capability set so the LLM picks a base it can
    # actually run on. Without this, the LLM commonly defaults to
    # nvidia/cuda even on AMD-only / Intel-only / CPU-only deployments,
    # burning an outer-repair iteration to converge.
    from services.cluster_capabilities import (
        describe_capabilities_for_llm,
        get_cluster_capabilities,
    )
    cluster_caps_for_prompt = get_cluster_capabilities(db, tenant_id=spec_row.tenant_id)
    # Target the user's process_type, not the cluster's best — same
    # reason as the Containerizer's generate-env path. Repair LLM
    # shouldn't be telling the model to use CUDA when the user picked CPU.
    cluster_hint = describe_capabilities_for_llm(
        cluster_caps_for_prompt,
        user_process_type=spec_data.get("process_type"),
    )
    from node_builder.llm_budget import default_budgets
    user_prompt = build_repair_user_prompt(
        mode=request.mode,
        failure=failure,
        current_dockerfile=request.current_dockerfile or spec_data.get("hero_dockerfile"),
        current_scripts=request.current_scripts,
        attempt_number=request.attempt_number,
        attempt_max=(inner_max if request.mode == "inner" else outer_max),
        prior_attempts=history,
        budgets=default_budgets(),
    )

    # USER CONSTRAINT — process_type. Manual-test repro 2026-05-22
    # spec=38 (Node.js App) + spec=39 (Transformers): user picked CPU
    # but the repair LLM kept proposing `nvidia/cuda:*` bases; the
    # validator caught it each time but the LLM kept proposing the
    # same thing → thrash exit after 3 same-rejections. Surface the
    # user's pick explicitly BEFORE the cluster hint so the LLM
    # anchors on it instead of defaulting to the best available
    # accelerator. Cluster hint stays — it still constrains what's
    # buildable on this deployment.
    process_type = (spec_data.get("process_type") or "CPU").upper().strip()
    if process_type == "CPU":
        process_hint = (
            f"USER CONSTRAINT — process_type=CPU. The user explicitly "
            f"chose a CPU node. DO NOT propose any GPU-flavored base "
            f"image (anything matching nvidia/cuda, nvcr.io/nvidia, "
            f"rocm/, intel/intel-extension, intel/oneapi, openvino/, "
            f"tensorrt). Curated CPU bases that always work: "
            f"`python:3.11-slim`, `debian:bookworm-slim`, `ubuntu:22.04`. "
            f"Adding accelerator libraries to a CPU node bloats the "
            f"image by GBs with no runtime benefit and the build will "
            f"be rejected on submission."
        )
    else:
        process_hint = (
            f"USER CONSTRAINT — process_type={process_type}. "
            f"Match the user's hardware choice."
        )
    # Prepend the user constraint then the cluster-capability hint.
    # User constraint is FIRST so the LLM treats it as the strongest
    # signal — the cluster hint is the runtime fallback, not the
    # primary anchor. Known-working install recipes (move C) come
    # next so the LLM treats them as concrete examples it can copy,
    # NOT as instructions it must follow verbatim.
    recipes_section = ""
    if install_recipes_block:
        recipes_section = (
            "KNOWN-WORKING INSTALL RECIPES — these install steps "
            "succeeded for the same library on past builds. Use as "
            "a starting point; adapt to the specific stderr below if "
            "needed.\n\n"
            f"{install_recipes_block}\n\n---\n\n"
        )
    elif install_docs_block:
        # Move D — fall back to the project's own README install
        # section when we don't have a captured recipe yet. Lower-
        # confidence signal than a recipe (this is what the project
        # SAYS, not what we've SEEN work) but better than nothing.
        recipes_section = (
            "PROJECT-DOCUMENTED INSTALL HINTS — extracts from each "
            "library's README install section. Treat as a starting "
            "point, NOT gospel — adapt to the actual stderr below.\n\n"
            f"{install_docs_block}\n\n---\n\n"
        )

    # Move E — classify the failure stderr and prepend a specialized
    # playbook hint for the category. Categories: compiler_missing,
    # version_pin_invalid, package_not_found, cuda_toolchain,
    # disk_space, network, permission, import_error. Each has a
    # short distilled playbook the LLM treats as advice. Default
    # category → no specialized hint, generic system prompt only.
    category_hint = ""
    try:
        from node_builder.failure_category import classify, hint_for_category
        _cat = classify(failure.stderr_tail or "")
        category_hint = hint_for_category(_cat)
        if category_hint:
            sh.logger.info(
                "/wizard/repair-artifacts: spec=%d failure_category=%s — using specialized hint",
                request.spec_id, _cat,
            )
    except Exception:  # noqa: BLE001
        sh.logger.exception("failure_category classification failed — using default hint")

    category_section = ""
    if category_hint:
        category_section = f"{category_hint}\n\n---\n\n"

    # Remediation-cache context from the build-task loop's lookup.
    #   * known_bad — fixes that ALREADY failed for this exact error.
    #     Telling the LLM not to re-propose them is the persistent,
    #     cross-build version of the in-loop thrash guard.
    #   * cache_hint — a prior diff that fixed a similar error; offered
    #     as a starting point, not gospel.
    cache_section = ""
    if request.known_bad:
        bad_block = "\n".join(f"  - {b}" for b in request.known_bad[:10])
        cache_section += (
            "ALREADY-FAILED FIXES — each of these was tried for THIS "
            "exact error on a previous build and did NOT resolve it. Do "
            "NOT propose them again; choose a structurally different "
            "approach:\n"
            f"{bad_block}\n\n---\n\n"
        )
    if request.cache_hint:
        cache_section += (
            "PRIOR FIX THAT WORKED for a similar error — use as a "
            "starting point, adapt to the stderr below:\n"
            f"{request.cache_hint}\n\n---\n\n"
        )

    user_prompt = (
        f"{process_hint}\n\n{cluster_hint}\n\n{category_section}{cache_section}{recipes_section}---\n\n{user_prompt}"
    )

    # Detect Image-mode specs so the outer repair prompt locks the
    # FROM line. Image-mode adapters are user-pinned to a specific
    # base; without this lock the LLM rewrites the user's chosen
    # image and the build fails for a different reason on every
    # attempt (stage repro 2026-05-21).
    image_mode = is_image_mode_spec(spec_data)
    # §7b — has_build_plan flips outer-mode to the plan-delta prompt
    # so the LLM returns structured field changes instead of a full
    # Dockerfile. Image-mode specs keep the FROM-locked prompt
    # since their flow doesn't go through the plan renderer.
    has_build_plan = (
        not image_mode
        and isinstance(spec_data.get("build_plan"), dict)
        and bool(spec_data["build_plan"].get("mode"))
    )

    from utils.llm_client import call_wizard_llm, call_repair_llm, LLMClientError

    # mega-qa-hardening Billing #4 — this endpoint authenticates with a
    # build-task token (require_build_task_token), so there is NO request
    # principal and the LLM metering would otherwise drop these wizard-build
    # calls with tenant_id=None. Thread the initiating tenant (from the token
    # scope, falling back to the acting user) into both the proposer and the
    # critic LLM so llm_call_usage / the daily rollup attribute them.
    _build_tenant_id = auth.tenant_id or getattr(auth.user, "tenant_id", None)
    import functools as _functools
    _metered_wizard_llm = _functools.partial(call_wizard_llm, _tenant_id=_build_tenant_id)

    # Move B — pre-build critic. Wrap the proposer LLM in a critic
    # loop so a fast (~5s) review LLM catches obvious bad fixes
    # before the caller burns 5-15 minutes on a docker build that
    # was always going to fail. The critic checks: does the fix
    # actually address the cited stderr? Does it pin a non-existent
    # version? Is it a known-broken pattern? Is the LLM cycling on
    # the same fix family?
    from node_builder.critic_repair_fix import audit_repair_fix

    _repair_system_prompt = system_prompt_for(
        request.mode, is_image_mode=image_mode, has_build_plan=has_build_plan,
    )
    _repair_timeout_s = int(os.getenv("WIZARD_REPAIR_TIMEOUT_SECONDS", "180"))

    def _call_repair_llm(prior_issues):
        """Proposer closure for audit_repair_fix. prior_issues comes
        from the critic on retry iterations — splice as an extra
        constraint block onto the user prompt so the next repair
        attempt sees exactly what the critic objected to."""
        prompt = user_prompt
        if prior_issues:
            issue_block = "\n".join(f"  - {i}" for i in prior_issues)
            prompt = (
                user_prompt
                + "\n\n"
                + "CRITIC FEEDBACK from a fast reviewer on your previous attempt:\n"
                + issue_block
                + "\n\nAddress each of those concretely. If the critic flagged "
                + "a same-error cycle, propose a STRUCTURALLY different fix "
                + "(different base image, different install strategy, etc.) "
                + "rather than another surface-level tweak."
            )
        # Proposer uses the REPAIR tool's model (the strong reasoner,
        # e.g. gpt-oss-20b) — falls back to the wizard model when the
        # repair tool isn't configured. The critic below stays on the
        # fast wizard model. Larger token budget: gpt-oss spends output
        # tokens on its reasoning channel before the final Dockerfile.
        return call_repair_llm(
            system_prompt=_repair_system_prompt,
            user_prompt=prompt,
            response_format="json",
            timeout_s=_repair_timeout_s,
            max_tokens=int(os.getenv("WIZARD_REPAIR_MAX_TOKENS", "8192")),
            _tenant_id=_build_tenant_id,  # Billing #4 — attribute to the tenant
        )

    # Promoted-cache short-circuit (choice (b)): when the build-task
    # loop's lookup found a HIGH-TRUST (promoted) fix for this exact
    # error, apply it directly and skip the LLM entirely. Only for
    # outer (Dockerfile) repairs, and only when it actually differs
    # from the current failing Dockerfile (else the no-op guard below
    # would reject it). The synthesised `parsed` flows through the same
    # validation + spec-mutation + return path as an LLM proposal, so
    # persistence is unchanged.
    _from_cache = False
    _cur_df = (request.current_dockerfile or spec_data.get("hero_dockerfile") or "").strip()
    if (
        request.mode == "outer"
        and request.promoted_diff
        and request.promoted_diff.strip()
        and request.promoted_diff.strip() != _cur_df
    ):
        parsed = {
            "updated_dockerfile": request.promoted_diff,
            "explanation": (
                "Applied a promoted cached fix for this error signature "
                "(skipped the LLM — this fix has a proven track record)."
            ),
        }
        _from_cache = True
        sh.logger.info(
            "/wizard/repair-artifacts: spec=%d applied promoted cached "
            "fix (skip-LLM)", request.spec_id,
        )
    else:
        try:
            parsed = audit_repair_fix(
                stderr_signature=(failure.stderr_tail or "")[-1500:],
                prior_dockerfile=(request.current_dockerfile or spec_data.get("hero_dockerfile") or ""),
                repair_history_tail=list(history)[-5:],
                propose_fn=_call_repair_llm,
                llm=_metered_wizard_llm,  # Billing #4 — critic attributed too
            )
        except LLMClientError as e:
            return sh.RepairArtifactsResponse(
                accepted=False, mode=request.mode, attempt_number=request.attempt_number,
                rejection_reasons=[f"LLM unavailable: {e}"],
                budget_remaining_inner=max(inner_max - inner_used, 0),
                budget_remaining_outer=max(outer_max - outer_used, 0),
            )

    rejections = validate_repair_response(request.mode, parsed)
    # Image-mode FROM lock: the user pinned this base; reject any
    # outer repair whose updated_dockerfile changes the FROM line.
    # Belt-and-suspenders alongside the IMAGE_MODE_OUTER system prompt
    # — LLMs sometimes "fix" failures by swapping the base anyway, and
    # the system_prompt's instruction is only advice, not enforcement.
    if request.mode == "outer" and image_mode:
        from node_builder.repair_loop import extract_from_line
        prior_from = extract_from_line(spec_data.get("hero_dockerfile") or "")
        new_from = extract_from_line(parsed.get("updated_dockerfile") or "")
        if prior_from and new_from and prior_from != new_from:
            rejections.append(
                f"image-mode FROM is locked to {prior_from!r}; the user "
                f"explicitly chose this base. The repair changed FROM to "
                f"{new_from!r} — that's not allowed. Keep the existing "
                f"FROM line and fix the failure with RUN directives "
                f"below it, or by editing entrypoint.sh."
            )
            wizard_metrics.record_repair(outcome="from-lock-reject", mode="image")
    # Base-image rail: if the LLM picked a base that can't run on this
    # cluster (e.g. nvidia/cuda on an AMD-only deployment), refuse the
    # response. The repair_history entry from THIS turn surfaces the
    # rejection_reasons in the next prompt, so the LLM knows what's
    # actually available and converges. Skipped in Image mode — the
    # user's base is preserved, the cluster constraint is the user's
    # problem to reconcile.
    if request.mode == "outer" and not image_mode:
        from services.cluster_capabilities import (
            get_cluster_capabilities,
            validate_base_for_cluster,
        )
        cluster_caps = get_cluster_capabilities(db, tenant_id=spec_row.tenant_id)
        process_type = (spec_data.get("process_type") or "")
        new_dockerfile = parsed.get("updated_dockerfile") or ""
        rejections.extend(
            validate_base_for_cluster(
                new_dockerfile, cluster_caps,
                user_preference=process_type,
            )
        )

        # Endpoint-level hallucinated-base guard. The critic_repair_fix
        # loop rejects hallucinated bases internally but after max_iters
        # it ships the last attempt anyway — wfr 66 hit this:
        # LLM kept proposing `harbor.../base-python:3.11-slim`
        # (matches the structural `[^-]+-slim` hallucination pattern),
        # critic rejected each time, but after 2 critic rounds the bad
        # Dockerfile committed to spec. Then the next build attempt
        # tried to pull that hallucinated tag and failed. Rejecting at
        # the endpoint forces a structurally-different proposal on
        # the next iteration.
        if new_dockerfile:
            from node_builder.known_bases import (
                extract_from_images, is_known_hallucinated,
            )
            hallucinated = [
                ref for ref in extract_from_images(new_dockerfile)
                if is_known_hallucinated(ref)
            ]
            if hallucinated:
                rejections.append(
                    f"Proposed base image(s) {hallucinated} match the "
                    f"hallucinated-base denylist (these tags don't exist "
                    f"on any registry — the build would fail with "
                    f"'manifest unknown'). Pick a verified base from the "
                    f"curated allowlist. For CPU work: python:3.11-slim, "
                    f"debian:bookworm-slim, ubuntu:22.04, or harbor.../"
                    f"base-python:3.11 (NO -slim suffix on harbor tags)."
                )

        # Endpoint-level FROM-thrash guard. wfr 67 hit this: LLM swapped
        # FROM through 11 iterations (debian→python:slim→harbor→ubuntu→
        # python:slim→...) trying to debug a PEP-668 install-time error.
        # The critic_repair_fix layer catches this internally too, but
        # after max_iters it ships the last attempt. Reject at the
        # endpoint so the stage's same-rejection bail-out engages on
        # something concrete, rather than letting the budget drain.
        if new_dockerfile and request.mode == "outer":
            import re as _re_thrash_ep
            def _from_line(df: str) -> str:
                m = _re_thrash_ep.search(
                    r"^\s*FROM\s+(\S+)", df or "",
                    _re_thrash_ep.MULTILINE | _re_thrash_ep.IGNORECASE,
                )
                return (m.group(1) if m else "").lower()
            history_tail = list(history)[-3:]
            recent_outer_entries = [h for h in history_tail if h.get("mode") == "outer"]
            install_markers = (
                "externally-managed-environment",
                "pep 668",
                "pip: not found",
                "pip3: not found",
                "subprocess-exited-with-error",
                "could not build wheels",
                "failed building wheel",
                "no matching distribution",
            )
            stderr_lower = (failure.stderr_tail or "").lower()
            is_install_time = any(m in stderr_lower for m in install_markers)
            prior_from = _from_line(spec_data.get("hero_dockerfile") or "")
            new_from = _from_line(new_dockerfile)
            if (
                is_install_time
                and len(recent_outer_entries) >= 3
                and prior_from
                and new_from
                and prior_from != new_from
            ):
                rejections.append(
                    f"FROM-thrash rejected. The build is failing at the "
                    f"INSTALL step (stderr matches a pip / wheel / "
                    f"PEP-668 marker), not the base. The last 3 outer "
                    f"repairs all swapped FROM but the same install-"
                    f"layer error keeps recurring — the base is not the "
                    f"problem. Keep `FROM {prior_from}` as-is and fix "
                    f"the install RUN line instead. Try: switch `pip "
                    f"install` to `pip3 install` (some bases ship only "
                    f"pip3); add `--break-system-packages` for PEP-668; "
                    f"use a venv (`python -m venv /opt/venv && . /opt/"
                    f"venv/bin/activate`); or `apt-get install -y "
                    f"python3-pip` before the pip RUN."
                )

    if rejections:
        return sh.RepairArtifactsResponse(
            accepted=False, mode=request.mode, attempt_number=request.attempt_number,
            rejection_reasons=rejections,
            budget_remaining_inner=max(inner_max - inner_used, 0),
            budget_remaining_outer=max(outer_max - outer_used, 0),
        )

    explanation = str(parsed.get("explanation") or "").strip()
    updated_scripts = dict(parsed.get("updated_scripts") or {})
    updated_dockerfile = parsed.get("updated_dockerfile") if request.mode == "outer" else None

    # Diagnostic logging: capture prior+proposed dockerfile sizes and
    # FROM lines per attempt. Wfr 58 left a mystery where the spec's
    # hero_dockerfile was python:3.11-slim AFTER repairs but every
    # build attempt reported a 462-byte Dockerfile starting with
    # nvidia/cuda — somewhere between "endpoint commits new dockerfile"
    # and "_materialise_build_context reads spec.hero_dockerfile",
    # the new content was being lost. This log makes that flow visible.
    if request.mode == "outer":
        prior_df = spec_data.get("hero_dockerfile") or ""
        new_df = updated_dockerfile or ""
        import re as _df_re
        def _from(df: str) -> str:
            m = _df_re.search(r"^\s*FROM\s+(\S+)", df, _df_re.MULTILINE | _df_re.IGNORECASE)
            return m.group(1) if m else "(none)"
        sh.logger.info(
            "/wizard/repair-artifacts: spec=%d outer repair "
            "prior_df_bytes=%d prior_from=%s "
            "new_df_bytes=%d new_from=%s "
            "explanation=%r",
            request.spec_id, len(prior_df), _from(prior_df),
            len(new_df), _from(new_df),
            (explanation or "")[:120],
        )

    # Endpoint-level no-op repair guard. The critic_repair_fix layer
    # also catches no-ops, but after ``max_iters`` exhausts it ships
    # the last attempt regardless — so a persistently-no-op LLM still
    # commits the same Dockerfile back to the spec, the next build
    # reproduces the same failure, and the budget burns. wfr 58 cycled
    # 6 outer attempts here: LLM kept emitting the prior `python:3.11-
    # slim` Dockerfile and narrating "replaced the non-existent
    # nvidia/cuda base" each time, even though the spec was already
    # python:3.11-slim. Reject the no-op here so the stage thrash
    # detector engages on a different signal (rejection signature)
    # rather than burning more LLM calls on the same hallucination.
    if (
        request.mode == "outer"
        and updated_dockerfile is not None
        and updated_dockerfile.strip() == (spec_data.get("hero_dockerfile") or "").strip()
    ):
        sh.logger.info(
            "/wizard/repair-artifacts: spec=%d outer repair returned a "
            "byte-identical Dockerfile — rejecting as no-op (LLM "
            "hallucinated a change it didn't make)",
            request.spec_id,
        )
        wizard_metrics.record_repair(outcome="rejected", mode=_spec_mode)
        return sh.RepairArtifactsResponse(
            accepted=False, mode=request.mode, attempt_number=request.attempt_number,
            rejection_reasons=[(
                "Proposed Dockerfile is byte-identical to the current "
                "(failing) one — your explanation describes a change "
                "you did not make in updated_dockerfile. The build will "
                "reproduce the same failure if accepted. Re-emit the "
                "Dockerfile with the described change actually applied, "
                "or pick a different fix strategy entirely."
            )],
            budget_remaining_inner=max(inner_max - inner_used, 0),
            budget_remaining_outer=max(outer_max - outer_used, 0),
        )

    # wizard-rebuild §7b — Plan-delta path.
    # When the spec has a BuildPlan AND the LLM response carries a
    # `deltas` list (new §7 shape), prefer that path over the legacy
    # free-form Dockerfile rewrite:
    #   1. Validate + apply 1-3 field deltas to the BuildPlan
    #   2. Re-render the Dockerfile from the updated plan
    #   3. Thrash-bail if the same delta-set was just proposed
    # Falls back to the legacy free-form path when the LLM returns
    # the old shape OR the deltas fail validation (caller retries).
    raw_deltas = parsed.get("deltas")
    if (
        request.mode == "outer"
        and isinstance(raw_deltas, list)
        and isinstance(spec_data.get("build_plan"), dict)
        and spec_data["build_plan"].get("mode")
    ):
        from node_builder.build_plan.deltas import (
            apply_deltas, deltas_signature,
        )
        from node_builder.build_plan.renderer import render_dockerfile
        from node_builder.build_plan.types import BuildPlan

        # Thrash check — same signature as the previous attempt?
        new_sig = deltas_signature(raw_deltas)
        history_now = list(spec_data.get("repair_history") or [])
        prev_sigs = [h.get("delta_signature") for h in history_now]
        if prev_sigs and prev_sigs[-1] == new_sig and new_sig:
            wizard_metrics.record_repair(outcome="thrash-bail", mode=_spec_mode)
            history_now.append({
                "attempt": request.attempt_number,
                "mode": request.mode,
                "llm_explanation": explanation
                    or "plan-delta thrash-bail (repeated delta-set)",
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": "plan-delta-thrash",
                "delta_signature": new_sig,
            })
            spec_data["repair_history"] = history_now
            spec_row.spec_json = spec_data
            flag_modified(spec_row, "spec_json")
            db.commit()
            return sh.RepairArtifactsResponse(
                accepted=False,
                mode=request.mode,
                attempt_number=request.attempt_number,
                explanation=(
                    "plan-delta thrash-bail: LLM proposed the same "
                    "delta-set twice in a row — bailing instead of "
                    "burning the budget."
                ),
                rejection_reasons=["thrash-bail: identical delta-set repeated"],
                budget_remaining_inner=max(inner_max - inner_used, 0),
                budget_remaining_outer=max(outer_max - outer_used, 0),
            )

        new_plan_dict, delta_rejections = apply_deltas(
            spec_data["build_plan"], raw_deltas,
        )
        if delta_rejections:
            wizard_metrics.record_repair(outcome="rejected", mode=_spec_mode)
            return sh.RepairArtifactsResponse(
                accepted=False,
                mode=request.mode,
                attempt_number=request.attempt_number,
                explanation=explanation or "plan-delta validation failed",
                rejection_reasons=delta_rejections,
                budget_remaining_inner=max(inner_max - inner_used, 0),
                budget_remaining_outer=max(outer_max - outer_used, 0),
            )
        # Apply: update plan + re-render Dockerfile + record history.
        spec_data["build_plan"] = new_plan_dict
        try:
            spec_data["hero_dockerfile"] = render_dockerfile(
                BuildPlan(**new_plan_dict),
            )
        except Exception as exc:
            return sh.RepairArtifactsResponse(
                accepted=False,
                mode=request.mode,
                attempt_number=request.attempt_number,
                explanation=f"applied plan failed to render: {exc}",
                rejection_reasons=[f"render after delta-apply: {exc}"],
                budget_remaining_inner=max(inner_max - inner_used, 0),
                budget_remaining_outer=max(outer_max - outer_used, 0),
            )
        spec_data["repair_attempts_outer_used"] = outer_used + 1
        history_now.append({
            "attempt": request.attempt_number,
            "mode": request.mode,
            "failure_reason": (
                f"exit={failure.exit_code} produced_output={failure.produced_output}"
            ),
            "llm_explanation": explanation
                or f"plan-delta applied: {len(raw_deltas)} field(s)",
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "plan-delta",
            "delta_signature": new_sig,
            "deltas": [
                {"field": d.get("field"), "reason": d.get("reason")}
                for d in raw_deltas if isinstance(d, dict)
            ],
        })
        spec_data["repair_history"] = history_now
        spec_row.spec_json = spec_data
        flag_modified(spec_row, "spec_json")
        db.commit()
        wizard_metrics.record_repair(outcome="delta-applied", mode=_spec_mode)
        return sh.RepairArtifactsResponse(
            accepted=True,
            mode=request.mode,
            attempt_number=request.attempt_number,
            explanation=explanation,
            updated_dockerfile=spec_data["hero_dockerfile"],
            budget_remaining_inner=max(inner_max - inner_used, 0),
            budget_remaining_outer=max(
                outer_max - spec_data.get("repair_attempts_outer_used", 0), 0,
            ),
        )

    # Apply to BuildSpec. The wizard's spec has structured fields for
    # entrypoint_sh + build_and_push_sh; other files live in source_files
    # as {filename, content} dicts. Update by name.
    if updated_scripts:
        for fname, content in updated_scripts.items():
            if fname == "entrypoint.sh":
                spec_data["entrypoint_sh"] = content
            elif fname == "build_and_push.sh":
                spec_data["build_and_push_sh"] = content
            else:
                source_files = list(spec_data.get("source_files") or [])
                replaced = False
                for sf in source_files:
                    if sf.get("filename") == fname:
                        sf["content"] = content
                        replaced = True
                        break
                if not replaced:
                    source_files.append({"filename": fname, "content": content})
                spec_data["source_files"] = source_files
                # Script-mode renderer embeds the script body as a
                # base64-encoded `RUN ... | base64 -d > <path>` line
                # in the Dockerfile (not COPY from source_files), so
                # rewriting source_files doesn't actually change what
                # ends up in the image — the next build re-decodes
                # the original base64. Detect that pattern and patch
                # the base64 inline so the LLM's repair takes effect
                # (manual-test repro 2026-05-23 spec=184 Test F:
                # inner repair fixed script.py in source_files but
                # the Dockerfile's base64 still pointed at the bad
                # version, so the next build re-failed identically;
                # LLM thrashed for 5 attempts trying to "fix" the
                # same syntax error that its prior repair already
                # fixed in source_files).
                import base64 as _b64, re as _re
                hero_df = spec_data.get("hero_dockerfile") or ""
                pat = _re.compile(
                    r"(echo )([A-Za-z0-9+/=]+)( \| base64 -d > [^\s]*/"
                    + _re.escape(fname) + r")"
                )
                if pat.search(hero_df):
                    new_b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
                    spec_data["hero_dockerfile"] = pat.sub(
                        r"\1" + new_b64 + r"\3", hero_df,
                    )

    if updated_dockerfile is not None:
        # Script-mode guard: the renderer's base64-decode line is the
        # only thing that puts the user's script into the image. The
        # outer-repair LLM sometimes strips it as "cleanup" because
        # the encoded blob looks like noise; if that happens the
        # next build's ENTRYPOINT exec'es a missing /app/script.py
        # and exits 2 forever (manual-test repro 2026-05-23 spec=379
        # Test F: 4 inner repairs trying to "remove" a script.py
        # that was already missing).
        #
        # If the prior Dockerfile had the base64 line AND the new
        # one doesn't, re-attach the renderer's line at the same
        # position the new one would have COPY entrypoint.sh at.
        import re as _re_guard
        import base64 as _b64_guard
        bp = spec_data.get("build_plan") or {}
        if (
            bp.get("mode") == "script"
            and bp.get("script_body")
            and bp.get("script_filename")
        ):
            script_path = (
                (bp.get("workdir") or "/app").rstrip("/")
                + "/" + bp.get("script_filename")
            )
            # §7e — also AST-validate any base64-encoded script that
            # the LLM embedded in the returned Dockerfile. If broken,
            # rewrite the line to use the canonical plan.script_body
            # instead. Catches the case where outer-repair returned
            # a "fixed" Dockerfile that included a regenerated script
            # with a syntax error (run 462 Test F manual repro: LLM
            # changed FROM line but also rewrote script.py with
            # `output_dir.mkdir exist_ok=True)` — missing paren).
            embedded_pat = _re_guard.compile(
                r"echo\s+([A-Za-z0-9+/=]+)\s*\|\s*base64\s+-d\s*>\s*"
                + _re_guard.escape(script_path)
            )
            embedded_match = embedded_pat.search(updated_dockerfile)
            if embedded_match:
                try:
                    decoded = _b64_guard.b64decode(embedded_match.group(1)).decode("utf-8")
                    import ast as _ast_g
                    _ast_g.parse(decoded)
                except (ValueError, SyntaxError) as exc:
                    # Embedded script is broken — replace with the
                    # canonical plan.script_body.
                    canon = _b64_guard.b64encode(
                        bp["script_body"].encode("utf-8"),
                    ).decode("ascii")
                    updated_dockerfile = embedded_pat.sub(
                        f"echo {canon} | base64 -d > {script_path}",
                        updated_dockerfile,
                    )
                    sh.logger.warning(
                        "wizard/repair-artifacts spec=%d outer mode: LLM "
                        "embedded a broken script (%s) into the Dockerfile; "
                        "rewrote with canonical plan.script_body.",
                        request.spec_id, exc,
                    )
            if not embedded_pat.search(updated_dockerfile):
                # Stripped — splice it back in just after the FIRST
                # COPY / RUN line so the script lands before any
                # downstream RUN that might reference it.
                enc = _b64_guard.b64encode(
                    bp["script_body"].encode("utf-8"),
                ).decode("ascii")
                fixup_line = (
                    f"RUN mkdir -p {bp.get('workdir') or '/app'} && "
                    f"echo {enc} | base64 -d > {script_path}"
                )
                lines = updated_dockerfile.splitlines()
                # Insert after the WORKDIR or USER root line if
                # present; else after FROM.
                insert_at = 1
                for i, ln in enumerate(lines):
                    if ln.startswith(("WORKDIR ", "USER root")):
                        insert_at = i + 1
                lines.insert(insert_at, fixup_line)
                updated_dockerfile = "\n".join(lines) + "\n"
                sh.logger.warning(
                    "wizard/repair-artifacts spec=%d outer mode: LLM "
                    "stripped script-mode base64 line for %s; "
                    "re-injecting from plan.script_body to keep the "
                    "ENTRYPOINT executable.",
                    request.spec_id, script_path,
                )
        spec_data["hero_dockerfile"] = updated_dockerfile

    if request.mode == "inner":
        spec_data["repair_attempts_inner_used"] = inner_used + 1
    else:
        spec_data["repair_attempts_outer_used"] = outer_used + 1

    # Cross-attempt memory (move A). Capture the concrete failure
    # signature alongside the LLM's explanation so the NEXT repair
    # iteration can see "I already tried X — same error reappeared",
    # which prevents the LLM from cycling through the same fix family.
    # Without this, the repair_history shown to the next iteration only
    # carries the LLM's own narration of what it did (which it's biased
    # to describe as "fixed") — not the ground-truth error.
    #
    # wfr 51 verification: 600-char window only captured the outer
    # "process did not complete successfully: exit code 1" wrapper —
    # the ACTUAL python/cmake/pip error preceding that gets pushed off
    # the tail. The LLM cycled 14 attempts blind to the real failure.
    # Bumped to 4000 chars to capture compilation tracebacks too.
    stderr_signature = (failure.stderr_tail or "").strip()[-4000:]
    history.append({
        "attempt": request.attempt_number,
        "mode": request.mode,
        "failure_reason": (
            f"exit={failure.exit_code} produced_output={failure.produced_output}"
        ),
        "stderr_signature": stderr_signature,
        "llm_explanation": explanation,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    spec_data["repair_history"] = history
    spec_row.spec_json = spec_data
    flag_modified(spec_row, "spec_json")
    db.commit()

    return sh.RepairArtifactsResponse(
        accepted=True, mode=request.mode, attempt_number=request.attempt_number,
        explanation=explanation,
        updated_scripts=updated_scripts,
        updated_dockerfile=updated_dockerfile,
        budget_remaining_inner=max(inner_max - spec_data.get("repair_attempts_inner_used", 0), 0),
        budget_remaining_outer=max(outer_max - spec_data.get("repair_attempts_outer_used", 0), 0),
    )
