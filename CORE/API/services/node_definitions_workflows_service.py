"""Business logic for `routers/node_definitions_domains/workflows.py`
(Gate 2b migration, openspec: stack-training-and-prewarm).

Data access -> `crud/node_definitions_workflows.py` (the ONLY place `db.query`
lives for this sub-module). The wizard/build helper primitives
(`_run_admission`, `_get_or_provision_tenant_harbor_creds`, `tenant_registry`,
`_enqueue_container_task`, `logger`) are NOT owned by this migration — they
stay defined in `_shared.py` and are imported directly from there, exactly as
the pre-migration router did (via `from ._shared import *`).

404-vs-403 trap (shared migration rule #5): `crud.get_workflow_run_for_tenant`
IS tenant-scoped (a run belonging to another tenant 404s at the DB layer,
matching the pre-migration inline query, which combined id + tenant_id in one
filter). `rebuild_variant_with_edits` below then checks
`run.triggered_by_user_id` against the caller on the already-fetched row,
raising `ForbiddenError` (403) when it belongs to someone else but exists in
the caller's own tenant — that ownership check is a *separate* dimension from
tenant membership, so scoping the initial fetch to the tenant does not
collapse it.

`_run_admission` (called from `build_and_register_local`) raises
`HTTPException(422, ...)` directly on admission rejection — it is an
un-migrated shared helper, not a service-layer error, and is left to
propagate through this service and the router unchanged (flagged, not
"fixed" — out of scope for this migration).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import crud.node_definitions_workflows as crud
import models
from routers.node_definitions_domains._shared import (
    _enqueue_container_task,
    _get_or_provision_tenant_harbor_creds,
    _run_admission,
    logger,
    tenant_registry,
)
from services._base import ServiceError


class ForbiddenError(ServiceError):
    """Authenticated caller lacks authorization for the action (router -> 403).

    `services._base` has no `ForbiddenError`, so it is declared here per the
    Gate 2b migration rules (shared rule #5) — same pattern as
    `services/node_definitions_iam_permissions_service.py`.
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class NotFoundError(ServiceError):
    """Requested entity does not exist (router maps -> 404)."""

    def __init__(self, message: str = "Not found", *, details: Any = None):
        super().__init__(message, code="not_found", details=details)


def analyze_repo(*, request: "AnalyzeRepoRequest", current_user: "models.User") -> Dict[str, Any]:  # noqa: F821
    """Same response shape as /analyze-script. Returns a low-confidence
    fallback (with a clear reason) on every failure mode rather than raising.
    Verbatim MOVE of the pre-migration handler body — see
    `routers/node_definitions_domains/workflows.py` for the endpoint
    docstring (unchanged, not duplicated here). No DB access — matches the
    pre-migration handler, which took no `db` dependency.
    """
    from node_builder.analyze_prompt import (
        SYSTEM_PROMPT, build_user_prompt, coerce_and_validate, fallback_response,
    )
    from utils.llm_client import call_wizard_llm, LLMClientError
    from utils.repo_fetcher import fetch_repo_snippet

    if os.getenv("DISABLE_WIZARD_AUTOFILL", "false").lower() == "true":
        return fallback_response(
            "Wizard auto-fill is disabled (DISABLE_WIZARD_AUTOFILL=true)."
        )

    if not (request.repo_url or "").strip():
        return fallback_response("No repo URL provided.")

    try:
        sample, included = fetch_repo_snippet(
            request.repo_url, auth_token=request.auth_token,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("analyze-repo: fetch failed for %s: %s",
                        request.repo_url, e)
        return fallback_response(f"Repo sample failed: {e}")

    if not sample:
        return fallback_response(
            "Repo mode: couldn't fetch sentinel files (private repo without "
            "credentials, unsupported host, or the repo is empty). "
            "Fill in the fields manually — the Containerizer reads the repo "
            "at build time."
        )

    user_prompt = build_user_prompt("repo", sample)

    try:
        timeout_s = int(os.getenv("ANALYZE_SCRIPT_TIMEOUT_SECONDS", "180"))
        raw = call_wizard_llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=timeout_s,
        )
    except LLMClientError as e:
        logger.info("analyze-repo LLM unavailable, returning fallback: %s", e)
        return fallback_response(f"Analysis unavailable: {e}")

    if not isinstance(raw, dict):
        return fallback_response("Analysis returned unexpected shape.")

    out = coerce_and_validate(raw)
    # Same GPU-library knowledge override as /analyze-script — the
    # sample sniffed from the repo may include requirements.txt /
    # pyproject.toml / a hero entrypoint with nerfstudio etc. that
    # the analyzer LLM missed.
    from node_builder.gpu_library_index import apply_override, confirm_accelerator_need
    out = apply_override(out, sample)
    out = confirm_accelerator_need(out, sample)  # "are you sure it needs a GPU?"
    # Add provenance to the reasoning so the user knows what the
    # analyzer actually saw — meaningful when the heuristic differs
    # from what the user expected.
    sampled = ", ".join(included) if included else "(no files sampled)"
    out["reasoning"] = (
        f"Sampled: {sampled}. " + (out.get("reasoning") or "").strip()
    ).strip()
    return out


def rebuild_variant_with_edits(
    db: Session,
    *,
    request: "RebuildVariantRequest",  # noqa: F821
    current_user: "models.User",  # noqa: F821
) -> Dict[str, Any]:
    """Rebuild one already-built variant with the operator's edited source.
    Verbatim MOVE of the pre-migration handler body — see
    `routers/node_definitions_domains/workflows.py` for the endpoint
    docstring (unchanged, not duplicated here).

    Raises `ServiceError` (code="bad_request" -> 400), `NotFoundError` (404),
    `ForbiddenError` (403) or `ServiceError` (code="bad_gateway" -> 502) — the
    router maps each back to the exact `HTTPException` shape (status code +
    detail body) the pre-migration inline code raised.
    """
    from node_builder.build_dag.types import BuildSpec
    from node_builder.build_dag.submit import (
        assert_build_farm_available,
        create_build_workflow,
    )
    from services.workflow_orchestrator import dispatch_trigger_run

    # 409 build_farm_unavailable before the spec row is mutated (2400LR-wiz1).
    assert_build_farm_available()

    edited = [
        {"filename": f["filename"], "content": f.get("content", "")}
        for f in (request.files or [])
        if isinstance(f, dict) and f.get("filename")
    ]
    if not edited:
        raise ServiceError("no edited files supplied", code="bad_request")

    # Tenant-scoped fetch (404-vs-403 trap — see module docstring): a run
    # belonging to another tenant 404s here, exactly as the pre-migration
    # inline query.
    run = crud.get_workflow_run_for_tenant(db, request.workflow_run_id, current_user.tenant_id)
    if run is None:
        raise NotFoundError("build run not found")
    if run.triggered_by_user_id not in (None, current_user.id):
        raise ForbiddenError("not your build")

    spec_row = crud.get_latest_build_spec_for_run(db, run.id, current_user.tenant_id)
    if spec_row is None:
        raise NotFoundError("no build spec for this run")

    spec_data = dict(spec_row.spec_json or {})
    # The variant must be one this build actually produced. hero is always
    # valid; non-hero variants live in the typed `variants` list after ingest
    # translated them from dockerfile_variants.
    known = {"hero"} | {
        v.get("variant") for v in (spec_data.get("variants") or [])
        if isinstance(v, dict)
    }
    if request.variant not in known:
        raise ServiceError(
            f"unknown variant {request.variant!r} for this build",
            code="bad_request",
        )

    # Record the explicit override (replaces any prior override for this
    # variant — a re-edit-and-rebuild is idempotent).
    overrides = dict(spec_data.get("source_overrides_by_variant") or {})
    overrides[request.variant] = edited
    spec_data["source_overrides_by_variant"] = overrides
    # If the variant was marked incompatible by the adapter, the operator's
    # manual fix overrides that verdict — clear it so the rebuild proceeds.
    adaptations = dict(spec_data.get("variant_adaptations") or {})
    if isinstance(adaptations.get(request.variant), dict) and adaptations[request.variant].get("incompatible"):
        adaptations[request.variant] = {**adaptations[request.variant], "incompatible": False}
        spec_data["variant_adaptations"] = adaptations
    spec_row.spec_json = spec_data
    flag_modified(spec_row, "spec_json")
    db.commit()

    # Re-dispatch the single-variant rebuild against the SAME spec row (so the
    # build container reads the override by build_spec_id). Do NOT re-point
    # spec_row.workflow_run_id — the original run stays the audit anchor.
    spec = BuildSpec.model_validate(spec_data)
    spec._build_spec_id = spec_row.id  # type: ignore[attr-defined]
    workflow = create_build_workflow(
        db, spec, user_id=current_user.id, rebuild_variant=request.variant,
    )
    rebuild_run_id = None
    try:
        wf = crud.get_workflow_by_id(db, workflow["workflow_id"])
        rebuild_run = dispatch_trigger_run(
            db, workflow=wf, triggered_by_user_id=current_user.id,
            extra_inputs={"build_spec_id": spec_row.id, "rebuild_variant": request.variant},
        )
        rebuild_run_id = getattr(rebuild_run, "id", None)
    except Exception as e:  # noqa: BLE001
        logger.warning("rebuild-variant: trigger failed: %s", e)
        raise ServiceError("failed to dispatch rebuild", code="bad_gateway")

    logger.info(
        "rebuild-variant: spec=%d variant=%s files=%d origin_run=%s rebuild_run=%s user=%d",
        spec_row.id, request.variant, len(edited), run.id, rebuild_run_id, current_user.id,
    )
    return {
        "build_spec_id": spec_row.id,
        "variant": request.variant,
        "workflow_run_id": rebuild_run_id,
        "files_overridden": len(edited),
    }


def build_and_register_local(
    db: Session,
    *,
    request: "BuildLocalRequest",  # noqa: F821
    current_user: "models.User",  # noqa: F821
) -> Dict[str, Any]:
    """
    Build the three user-approved Albedo-pattern files and push to the LOCAL
    registry (registry:5000), then register the resulting image as a
    NodeDefinition. Verbatim MOVE of the pre-migration handler body — see
    `routers/node_definitions_domains/workflows.py` for the endpoint
    docstring (unchanged, not duplicated here).

    Raises `ServiceError` (code="bad_request" -> 400, "payment_required" ->
    402, or "payload_too_large" -> 413) — the router maps each back to the
    exact `HTTPException` shape (status code + detail body) the
    pre-migration inline code raised. `_run_admission` may also raise
    `HTTPException(422, ...)` directly — an un-migrated shared helper, left
    to propagate unchanged (see module docstring).
    """
    import os
    if not request.image_name.strip():
        raise ServiceError("image_name is required", code="bad_request")
    if not request.dockerfile.strip():
        raise ServiceError("dockerfile is required", code="bad_request")

    # Layer-1 admission on the user-authored Dockerfile. Fires before
    # the credit charge and before imagebuilder dispatch — a rejected
    # submission costs nothing.
    _run_admission("dockerfile", {"dockerfile": request.dockerfile}, current_user)

    # Quota gate: refuse new builds if check_harbor_quotas has flipped
    # this tenant's quota_push_blocked flag (>=95% of Harbor quota).
    # Pulls still work; old images can be deleted from the Tenant
    # Registry tab to free space.
    if current_user.tenant_id:
        _tenant = crud.get_tenant_by_id(db, current_user.tenant_id)
        if _tenant and getattr(_tenant, "quota_push_blocked", False):
            raise ServiceError(
                "Your container registry is at its storage limit. "
                "Delete unused images from the Tenant Registry tab or "
                "free up space to continue building nodes.",
                code="payment_required",
            )

    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")

    # Resolve tenant Harbor push creds from Vault. Written during tenant
    # provisioning (crud/tenant_cud.py) for tenants created after Phase 4
    # shipped. For existing tenants (pre-Phase-4), we auto-provision the
    # Harbor project + robot on the first build attempt — self-healing,
    # no manual backfill script required.
    tenant_harbor_url = ""
    tenant_harbor_user = ""
    tenant_harbor_pass = ""
    # GS.PSR.8 — harbor.tenant_harbor_enabled via the platform-settings
    # registry (DB > env > True), read per build attempt. tenant_harbor_client
    # is a Paid module: on CE skip the per-tenant Harbor push creds and build
    # against the LOCAL registry only.
    from utils.paid_import import optional_paid
    _harbor_mod = optional_paid("utils.tenant_harbor_client")
    if _harbor_mod is not None:
        if current_user.tenant_id and _harbor_mod.tenant_harbor_enabled(db):
            tenant_harbor_url, tenant_harbor_user, tenant_harbor_pass = _get_or_provision_tenant_harbor_creds(
                current_user.tenant_id
            )

    env = {
        "REGISTRY_TARGET": "local",
        "LOCAL_REGISTRY": tenant_registry(),
        # Per-tenant Harbor push target. ImageBuilder prefers these over
        # LOCAL_REGISTRY; only falls back when TENANT_HARBOR_URL is empty.
        "TENANT_HARBOR_URL": tenant_harbor_url,
        "TENANT_HARBOR_USERNAME": tenant_harbor_user,
        "TENANT_HARBOR_PASSWORD": tenant_harbor_pass,
        "IMAGE_NAME": request.image_name,
        "NODE_NAME": request.node_name,
        "API_URL": os.getenv("API_BASE_URL", "http://api:80"),
        "API_KEY": service_api_key,
        "INPUT_TYPE": request.input_type,
        "OUTPUT_TYPE": request.output_type,
        "PROCESS_TYPE": request.process_type,
        "COST_IN_CREDITS": str(request.cost_in_credits),
        "TENANT_ID": str(current_user.tenant_id) if current_user.tenant_id else "",
        # Inline artifacts — ImageBuilder will materialize these into its build
        # context before running `docker build`.
        "INLINE_DOCKERFILE": request.dockerfile,
        "INLINE_ENTRYPOINT_SH": request.entrypoint_sh,
        "INLINE_BUILD_AND_PUSH_SH": request.build_and_push_sh,
    }

    # FX.149 (Q136=A) — external (Anthropic) LLM remediation gate. This
    # direct image-builder path runs the same builder as the build-DAG,
    # so it must honour the same platform external-LLM switch + tenant
    # opt-in. The builder container cannot read settings; the decision
    # rides container_env ("1" only when allowed, absent == OFF).
    try:
        from node_builder.build_dag.submit import _external_llm_remediation_allowed
        if _external_llm_remediation_allowed(
                db, getattr(current_user, "tenant_id", None)):
            env["EXTERNAL_LLM_REMEDIATION_ALLOWED"] = "1"
    except Exception:  # noqa: BLE001 — fail closed on gate-read errors
        pass

    # Per-GPU Dockerfile variants — if present, ImageBuilder will build
    # and push `<image>:latest-<variant>` for each and populate the
    # registered node's gpu_variants dict. JSON-encoded because env
    # vars are string-only.
    if request.dockerfile_variants:
        import json as _json
        env["INLINE_DOCKERFILE_VARIANTS"] = _json.dumps(request.dockerfile_variants)
    if request.gpu_variants:
        import json as _json
        env["GPU_VARIANTS"] = _json.dumps(request.gpu_variants)
    if request.description:
        env["DESCRIPTION"] = request.description
    # γ — real-data sample loop wiring. Cap at 8 MB on the env so a
    # huge upload doesn't get lost in the worker's task-payload size
    # limit. Bigger samples need an upload-to-MinIO flow, deferred.
    if request.sample_input_filename and request.sample_input_body_b64:
        if len(request.sample_input_body_b64) > 8 * 1024 * 1024:
            raise ServiceError(
                "sample input exceeds 8 MB cap; use the MinIO sample-upload endpoint",
                code="payload_too_large",
            )
        env["INLINE_SAMPLE_INPUT_FILENAME"] = request.sample_input_filename
        env["INLINE_SAMPLE_INPUT_B64"] = request.sample_input_body_b64
    if request.defer_registration:
        env["DEFER_REGISTRATION"] = "1"
    if request.container_env:
        import json as _json
        env["CONTAINER_ENV"] = _json.dumps(request.container_env)

    # Glue artifact (issue #18). When the wizard's per-step LLM Q&A
    # produced a wrapper script (entrypoint.sh / Dockerfile.glue /
    # glue.sh / Containerfile.glue), ship it to ImageBuilder as a
    # named INLINE_GLUE env var so the build script can drop it into
    # the build context as <artifact_type>. Schema is curated +
    # already validated by analyze_prompt.coerce_and_validate, but
    # we double-check the type belongs to the recognised slot list
    # so a malformed payload from a stale FE bundle can't ship a
    # surprise filename into the build context.
    if request.glue_artifact and isinstance(request.glue_artifact, dict):
        _glue_type = str(request.glue_artifact.get("artifact_type") or "").strip()
        _glue_content = str(request.glue_artifact.get("content") or "")
        _valid_glue_types = {
            "entrypoint.sh", "Dockerfile.glue", "glue.sh", "Containerfile.glue",
        }
        if _glue_type in _valid_glue_types and _glue_content:
            # When the glue is named "entrypoint.sh" it overrides
            # the LLM-generated entrypoint that came from the same
            # analysis pass — picking the user-confirmed glue is
            # what they expect. Other types (Dockerfile.glue /
            # glue.sh / Containerfile.glue) ride alongside via
            # their own slot.
            if _glue_type == "entrypoint.sh":
                env["INLINE_ENTRYPOINT_SH"] = _glue_content
            else:
                # Custom slot name — ImageBuilder writes it as
                # <artifact_type> in the build context. Each
                # slot has a dedicated env var so the build
                # script doesn't have to parse a JSON envelope.
                _slot_var = f"INLINE_GLUE_{_glue_type.replace('.', '_').upper()}"
                env[_slot_var] = _glue_content

    # Render the 4th artifact (DAG .py) server-side from the wizard
    # metadata. This is the "server double-checks before commit" step:
    # the client's live preview and this render MUST match byte-for-byte
    # because both call through utils.metadata_template. If they don't match,
    # the server wins — malicious/buggy clients can't slip arbitrary
    # Python into the METADATA BLOCK.
    try:
        from utils.metadata_template import render as _render_metadata, coerce_and_validate as _coerce_metadata
        md = dict(request.node_metadata or {})
        md.setdefault("node_name", request.node_name)
        md.setdefault("executable_path", f"{request.image_name}:latest")
        md.setdefault("description", request.description or "")
        md.setdefault("input_type", request.input_type)
        md.setdefault("output_type", request.output_type)
        md.setdefault("process_type", request.process_type)
        md.setdefault("cost_in_credits", request.cost_in_credits)
        md.setdefault("tenant_id", current_user.tenant_id)
        if request.container_env and "container_env_extras" not in md:
            md["container_env_extras"] = request.container_env
        # gpu_variants — drops into the METADATA BLOCK as a
        # `gpu_variants = {...}` line so node_scanner / the scheduler
        # can route each task to the matching per-hardware image tag.
        if request.gpu_variants and "gpu_variants" not in md:
            md["gpu_variants"] = request.gpu_variants
        rendered_metadata = _render_metadata(_coerce_metadata(md))
        env["INLINE_METADATA_FILE_SOURCE"] = rendered_metadata["content"]
        env["METADATA_FILE_NAME"] = rendered_metadata["filename"]
    except Exception as e:
        # Never block the build because the sidecar failed — the three
        # primary files still go through. ops can backfill later.
        logger.warning("build-and-register-local: DAG render skipped: %s", e)

    # No charge here — the wizard already paid NODE_CREATION_CREDIT_COST
    # at /generate-artifacts. Charging a second time at Build would
    # double-bill the same logical node creation. Direct-API clients
    # that bypass /generate-artifacts still get the per-endpoint
    # charges at /node_definitions/ (POST) and /build-and-register.

    task_id = _enqueue_container_task(
        db, current_user.tenant_id, "imagebuilder:latest", env, "image_builder_local"
    )
    return {
        "task_id": task_id,
        "step": "build-and-register-local",
        "status": "pending",
    }
