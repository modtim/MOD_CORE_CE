"""node_definitions endpoints for the `node_authoring` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)
# GS.PSR.8 — the credits gate helper (underscore-prefixed, so NOT carried
# by the star import above).
from ._shared import _credits_enabled  # noqa: F401

from services import node_authoring_service as _node_authoring_service
from services._base import ServiceError as _ServiceError
from utils import node_cost as _node_cost
from utils.http_errors import internal_http_error

# =============================================================================
# PAID modules (CE-launch Gate 4: "CE fanout; marketplace/team-sharing business logic is the non-CE slice") — optional
# imports. CE does not ship `services/node_marketplace_paid.py` (marketplace
# publish/curate/browse-gate, tenant-team sharing, packages, try-before-you-
# buy) or `services/node_build_budget_paid.py` (node-creation credit charge
# + P9v build-failure refund). Every endpoint below that depends on one of
# these modules guards on `is None` and answers 404 ("feature not available
# in this edition") instead of calling through — the endpoint PATHS stay
# physically in place either way (contract-snapshot stable). When the paid
# module IS present (every build today, including this repo's live
# container), behaviour is byte-identical to before the split.
# =============================================================================
try:
    from services import node_marketplace_paid as _marketplace_paid
except ImportError:  # pragma: no cover — exercised by the CE-absence smoke test
    _marketplace_paid = None

try:
    from services import node_build_budget_paid as _budget_paid
except ImportError:  # pragma: no cover — exercised by the CE-absence smoke test
    _budget_paid = None

router = APIRouter(
    responses={404: {"description": "Not found"}},
)


def _raise_http(exc: _ServiceError) -> None:
    """Map a node_authoring service-layer error to the HTTP response the
    original inline handler used to raise directly (SHARED RULES: router
    maps ServiceError -> HTTPException)."""
    status_code = _node_authoring_service.STATUS_BY_CODE.get(exc.code, 400)
    detail = exc.details if exc.details is not None else exc.message
    raise HTTPException(status_code=status_code, detail=detail)


def _paid_feature_unavailable() -> None:
    """Raised by a marketplace/build-budget endpoint when its paid module
    isn't shipped (CE). Distinct 404 detail so this is diagnosable from the
    client side, vs. a plain "not found" on a missing resource."""
    raise HTTPException(
        status_code=404,
        detail="This feature is not available in this edition.",
    )


def get_build_spec(
    spec_id: int,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """B4 — fetch a BuildSpec by id. Called by per-task containers
    via the scoped build-task token. A token may only read its own build.
    """
    try:
        return _node_authoring_service.get_build_spec(db, spec_id=spec_id, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def get_build_spec_context(
    spec_id: int,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """Stream the raw build CONTEXT tarball for a raw_context build. Same scoped
    gate as the spec fetch: a build-task token may only read its own build, and
    only for its own tenant. The tarball is content-addressed in MinIO; the
    build-task extracts it into the docker build context."""
    try:
        return _node_authoring_service.get_build_spec_context(db, spec_id=spec_id, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

async def build_image_from_context(
    image_name: str = Form(...),
    node_name: str = Form(...),
    dockerfile: str = Form(..., description="Dockerfile TEXT to build against the context"),
    context: UploadFile = File(..., description="gzipped tar of the build context"),
    process_type: str = Form("CPU"),
    current_user=Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Platform-admin: build ANY container image from a Dockerfile + a context
    tarball, through the fully-gated build pipeline. The general 'bring your own
    Dockerfile' primitive (the mobilebuilder is its first user)."""
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="platform admin only")
    if current_user.tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant required")
    data = await context.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty context")
    # Same Dockerfile admission gate as wizard builds (FROM allowlist, etc.).
    _run_admission("dockerfile", {"dockerfile": dockerfile}, current_user)
    return submit_raw_context_build(
        db, image_name=image_name, node_name=node_name, dockerfile=dockerfile,
        context_bytes=data, tenant_id=current_user.tenant_id, user_id=current_user.id,
        process_type=process_type,
    )

def post_stage_result(
    spec_id: int,
    request: _StageResultRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """B4 — per-task container reports its outcome here. Logged
    for now; future builds can aggregate to a per-build
    progress/results table."""
    try:
        return _node_authoring_service.post_stage_result(db, spec_id=spec_id, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def sync_variants_from_hero(
    request: _SyncVariantsFromHeroRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """Called by `test_and_repair_one_variant.py` after hero PASSes.

    Propagates the (possibly LLM-repaired) hero_dockerfile +
    entrypoint_sh + build_and_push_sh to non-hero variants on
    Image-mode specs, where variants are byte-identical to hero
    by design (the Image-mode renderer doesn't vary on
    cuda_version). Without this, the gated non-hero variant
    builds would still use the pre-repair Dockerfiles even
    though they wait for hero PASS via the DAG edge.

    Non-Image modes are skipped — their variants legitimately
    differ from hero (per-accelerator install_step rewrites).
    Plan-aware repair (§7) will handle those properly.
    """
    try:
        return _node_authoring_service.sync_variants_from_hero(db, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def variant_sample_test_result(
    request: _VariantSampleTestResultRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """§5 — receive a per-variant sample-test result from the
    build-task and score it.

    The build-task's `sample_test_one_variant.py` runs the variant
    image with /data I/O, captures the largest produced output, and
    POSTs the bytes here (base64-encoded). This endpoint persists
    the output to MinIO so a later reference-variant swap can re-
    score in place, runs `variant_test.dispatcher.record_variant_test`
    to compute score + verdict via the comparer, and returns the
    verdict so the build-task can exit 0 (pass) or 1 (fail).

    Hero `publish_ok=False` causes the workflow's register step to
    abort; non-hero results never block register but populate the
    Node's per-variant chip state (verified / drift / incompatible).
    """
    # A build-task token may only post sample-test results for ITS OWN build.
    assert_build_scope(_auth, build_spec_id=request.build_spec_id)

    import base64 as _base64
    from node_builder.variant_test.dispatcher import record_variant_test

    try:
        output_bytes = (
            _base64.b64decode(request.output_body_b64)
            if request.output_body_b64 else b""
        )
    except Exception as exc:
        raise internal_http_error(422, "output_body_b64 is not valid base64", exc) from exc

    try:
        result = record_variant_test(
            db,
            build_spec_id=request.build_spec_id,
            variant=request.variant,
            exit_code=request.exit_code,
            produced_output=request.produced_output,
            output_bytes=output_bytes,
            output_type=request.output_type,
            image_digest=request.image_digest,
        )
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))

    return _VariantSampleTestResultResponse(
        verdict=result["verdict"],
        score=float(result["score"]),
        publish_ok=bool(result["publish_ok"]),
        scored_against=result.get("scored_against"),
        is_marginal=bool(result.get("is_marginal")),
        attempt=int(result.get("attempt", 1)),
        best_score=result.get("best_score"),
        regressed=bool(result.get("regressed")),
        published_digest=result.get("published_digest"),
    )

def variant_verified(
    request: _VariantVerifiedRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """§6 — non-hero sample-test callback. Called by the build-task
    ``sample_test_one_variant.py`` stage AFTER the sample-test has
    scored a non-hero variant. Adds (or removes) the variant from the
    Node's ``gpu_variants`` map retroactively — register may have
    already fired and the Node is in the library; this lets the card
    flip from "verifying X" to "Available on X" without a rebuild.

    Verdicts:
      - ``verified`` / ``marginal`` / ``drift``: variant is usable;
        ensure it's present in ``gpu_variants``.
      - ``failed`` / ``incompatible``: drop the variant from
        ``gpu_variants`` (scheduler won't route real workloads to a
        broken variant).

    Idempotent: re-running with the same verdict is a no-op.

    No-op if the spec hasn't been promoted to a NodeDefinition yet
    (register hasn't fired). The §5 build-time verification row is
    still written by the sample-test result endpoint; this endpoint
    only mutates ``gpu_variants`` on the registered Node.
    """
    try:
        return _node_authoring_service.variant_verified(db, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def build_spec_eta(
    spec_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§10c — return the ETA + (optional) stall reason for a
    BuildSpec. The WorkflowBuilder's BuildQueueRow polls this to
    render "~12 min" and the stall warning banner.

    Tenant-scoped: cross-tenant access → 403.
    """
    try:
        return _node_authoring_service.build_spec_eta(db, spec_id=spec_id, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def enqueue_variant_test_pending(
    request: _EnqueueVariantTestPendingRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),  # noqa: ARG001
):
    """§9 — enqueue a speculative sample-test for a variant whose
    matching accelerator hardware isn't currently online. The
    worker-heartbeat hook drains the row when a matching worker
    joins; the hourly APScheduler sweep expires rows that hit TTL
    without ever finding hardware.

    Idempotent: if a pending row already exists for the same
    (build_spec_id or node_definition_id, variant), the existing
    row's id is returned with `enqueued=False`.
    """
    from node_builder.variant_test.queue import enqueue_pending
    if request.build_spec_id is None and request.node_definition_id is None:
        raise HTTPException(
            status_code=422,
            detail="at least one of build_spec_id, node_definition_id required",
        )
    if request.ttl_days < 1 or request.ttl_days > 365:
        raise HTTPException(
            status_code=422,
            detail="ttl_days must be in [1, 365]",
        )
    try:
        row_id = enqueue_pending(
            db,
            build_spec_id=request.build_spec_id,
            node_definition_id=request.node_definition_id,
            variant=request.variant,
            image_ref=request.image_ref,
            ttl_days=request.ttl_days,
        )
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    # `enqueued` reflects new-vs-existing: if the queue module
    # short-circuited an existing row, the timestamp won't change.
    # Simpler check: see if the row's status is "pending" and the
    # ID matches what we expected. For v1 we always return True;
    # the idempotency is internal to enqueue_pending.
    return _EnqueueVariantTestPendingResponse(id=row_id, enqueued=True)

def variant_test_pending_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§9 — admin dashboard helper. Returns pending-test queue
    depth per accelerator across all tenants. Surfaces in the
    Platform admin → Workers panel so operators see what hardware
    is most-needed."""
    from node_builder.variant_test.queue import queue_depth_by_accelerator
    depth = queue_depth_by_accelerator(db)
    return _VariantTestPendingStatusResponse(
        queue_depth_by_accelerator=depth,
        total_pending=sum(depth.values()),
    )

class _ChangeGateWeeklyReportResponse(PydanticBaseModel):
    report: Optional[Dict[str, Any]]


def variant_change_gate_weekly_report(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """GO.8 (23.7.s3) — admin read-out of the change-gate WEEKLY REPORT.

    The report itself is produced by the weekly sweep (services/scheduler.py)
    and persisted as the append-only audit event
    ``VARIANT_CHANGE_GATE_WEEKLY_REPORT``; this endpoint reads that audit row
    through the service (`change_gate.latest_weekly_report`) — no query here
    (api_lint R5). Answers `{"report": null}` when no sweep has run yet.
    Platform-admin only — same gate helper (`_require_platform_admin`) the
    other node-authoring admin routes use.
    """
    _require_platform_admin(current_user)
    from node_builder.variant_test import change_gate as _change_gate
    return _ChangeGateWeeklyReportResponse(
        report=_change_gate.latest_weekly_report(db))


def describe_cascade(
    request: _DescribeCascadeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§8 — Describe-mode cascade. Walk 4 steps (container search →
    repo search → library search → codegen) and return ranked
    candidates plus per-backend rate-limit + error status.

    Picked candidate's ``submission`` dict can be POSTed verbatim
    to ``/build-and-register-node`` — the UI Confirm screen just
    forwards it.
    """
    description = (request.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if len(description) > 2000:
        raise HTTPException(
            status_code=400,
            detail="description must be 2000 characters or fewer",
        )
    floor = (
        float(request.confidence_floor)
        if request.confidence_floor is not None else 0.7
    )
    if not 0.0 <= floor <= 1.0:
        raise HTTPException(
            status_code=422,
            detail="confidence_floor must be in [0, 1]",
        )

    from node_builder.describe_cascade.cascade import run_cascade
    result = run_cascade(
        db,
        description=description,
        tenant_id=current_user.tenant_id,
        confidence_floor=floor,
    )
    return _DescribeCascadeResponse(
        description=result.description,
        confidence_floor=result.confidence_floor,
        candidates=[c.model_dump() for c in result.candidates],
        per_step=[s.model_dump() for s in result.per_step],
        rate_limited=result.rate_limited,
        errors=result.errors,
        stopped_at_step=result.stopped_at_step,
        reason=result.reason,
    )

def retry_with_elevated_budget(
    request: _RetryElevatedBudgetRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§7d — "Retry with elevated budget" recovery action.

    When auto-repair gave up (thrash-bail or outer budget exhausted),
    operators can grant a one-shot 15-attempt extension. Bumps
    ``repair_attempts_outer_max`` by 15 over the tier-default, resets
    the used counter to 0, and zeroes infra-transient retries so the
    next loop starts clean. Caller is expected to re-trigger the
    build workflow.

    Only available to specs whose plan tier was high or medium —
    low-tier plans already get 15 attempts and shouldn't get more
    without operator review (per §7d task list).
    """
    try:
        return _node_authoring_service.retry_with_elevated_budget(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def finalize_partial_publish(
    request: _FinalizePartialPublishRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§7d — "Ship hero only, skip failing variants" recovery action.

    Available iff the hero variant's sample-test passed (verdict in
    verified/marginal). Drops failed/incompatible variants from
    gpu_variants on the (already-registered, per §6) NodeDefinition.
    Failing variants are marked with verdict=incompatible and reason
    "user-skipped" so the audit trail records the operator decision.

    If register hasn't fired yet (hero somehow not verified), returns
    400 — the user should investigate the hero failure first.
    """
    try:
        return _node_authoring_service.finalize_partial_publish(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def submit_wizard_support_ticket(
    request: _SupportTicketRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§7d — "Send to MOD support" recovery action.

    Packages the BuildSpec context (BuildPlan + accumulated deltas +
    last 200 log lines from repair_history + repo URL / source body
    excerpt) into a single audit-event row that operators can review
    under System → Support. Returns a ticket id (the audit_event id)
    surface-able in the UI.

    No external delivery in v1 — the ticket lives in the audit-event
    stream. Email / Slack delivery can wire up later.
    """
    try:
        return _node_authoring_service.submit_wizard_support_ticket(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def finalize_variant_verification(
    request: _FinalizeVerificationRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),
):
    """§5 — after register creates the NodeDefinition row, backfill
    ``node_definition_id`` on every build-time verification row so the
    UI + reference-variant PATCH endpoint can look them up by Node id
    rather than the transient ``build_spec_id``.

    Idempotent: rows that already have ``node_definition_id`` set are
    untouched. Build-task ``register_node.py`` calls this right after
    `POST /node_definitions/` succeeds.
    """
    try:
        return _node_authoring_service.finalize_variant_verification(db, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def get_variant_verification(
    node_def_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§5 — list the per-variant verification rows for a Node. Used
    by the WorkflowBuilder UI to render the drift-reference dropdown
    + per-variant verified/drift/marginal/failed chips on the Node
    settings panel."""
    try:
        return _node_authoring_service.get_variant_verification(db, node_def_id=node_def_id, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def rule_on_drifted_variant(
    node_def_id: int,
    variant: str,
    request: _DriftRulingRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_active_and_onboarded_user),
):
    """project-agent §1.4 — decide what happens to a variant whose output
    diverged from the reference.

    Until this is called (or a tenant default applies) the variant is
    withheld from selection by §1.5. It previously published with a
    warning chip, which only works if a human is looking — an agent
    picking a variant by placement never sees one.
    """
    try:
        return _node_authoring_service.rule_on_drifted_variant(
            db, node_def_id=node_def_id, variant=variant, request=request, user=user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def patch_reference_variant(
    node_def_id: int,
    request: _PatchReferenceVariantRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§5c — change the drift reference for a Node, recomputing every
    variant's score in place.

    Reads each variant's stored sample-test output from MinIO
    (``node_variant_verification.raw_output_ref``) and compares it
    to the new reference's stored output via the same comparer the
    build-task uses. Verdicts are recomputed; the reference variant
    itself becomes verified-vs-itself (score 1.0).

    Constraints:
      - The new reference variant must exist in the Node's
        verification rows AND have verdict in {verified, marginal,
        drift} (i.e. it produced usable output). Picking a failed /
        incompatible variant is rejected.
      - No rebuilds — this is purely a re-comparison against
        already-stored outputs.
    """
    try:
        return _node_authoring_service.patch_reference_variant(
            db, node_def_id=node_def_id, request=request, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def suggest_node_category(
    request: _SuggestCategoryRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """LLM-suggested category for a wizard-built node.

    Replaces the "every wizard node lands under Custom" default with
    a smart pick from the tenant's existing taxonomy (preferred) or a
    fresh proposal. Tenant's existing categories ride along in the
    response so the wizard UI can offer them as a dropdown.

    Called from the wizard's Review step. Returns quickly (single
    short LLM call, no chunk pipeline). Falls back gracefully when
    the LLM is unreachable.
    """
    if not (request.node_name or "").strip():
        raise HTTPException(
            status_code=400, detail="node_name is required",
        )
    from node_builder.category_suggester import suggest_category
    return suggest_category(
        db,
        tenant_id=current_user.tenant_id,
        node_name=request.node_name.strip(),
        description=request.description or "",
        base_image=request.base_image,
        source_body_excerpt=request.source_body,
        repo_url=request.repo_url,
    )

def extract_repo_docs(
    request: _RepoDocsExtractRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Wizard-rebuild §3c — chunk + tag a repo's docs, with caching.

    Front-door for the Git-mode BuildPlan extractor. First call for
    a (tenant, repo_url, ref) tuple fetches sentinels and runs the
    §3a chunker + §3b intent tagger; subsequent calls for the same
    tuple inside the 7-day TTL window return the cached row. When
    `BuildPlan.EXTRACTOR_VERSION` bumps, old rows coexist with new
    ones until the hourly sweep purges them — no race window.

    Returns the row id plus the chunks the caller would want anyway;
    the Git extractor (later §3 chunk) reads chunks directly off
    the row via `store.chunks_from_row()`.
    """
    if not (request.repo_url or "").strip():
        raise HTTPException(
            status_code=400, detail="repo_url is required",
        )

    from node_builder.repo_docs.store import (
        chunks_from_row,
        get_or_extract,
    )

    try:
        row, cache_hit = get_or_extract(
            db,
            tenant_id=current_user.tenant_id,
            repo_url=request.repo_url.strip(),
            ref=request.ref.strip(),
            prefer_llm=request.prefer_llm,
        )
    except Exception as exc:
        logger.warning(
            "repo-docs/extract failed for tenant=%s repo=%r: %s",
            current_user.tenant_id, request.repo_url, exc,
        )
        raise internal_http_error(502, "Repo-docs extraction failed", exc) from exc

    chunks = chunks_from_row(row)
    return _RepoDocsExtractResponse(
        extract_id=row.id,
        cache_hit=cache_hit,
        repo_url=row.repo_url,
        ref=row.ref,
        extractor_version=row.extractor_version,
        chunks=[
            _RepoDocsChunkOut(
                source_file=c.source_file,
                start_line=c.start_line,
                end_line=c.end_line,
                heading=c.heading,
                content=c.content,
                intents=c.intents,
            )
            for c in chunks
        ],
        files_missing=list(row.files_missing or []),
        fetched_at=row.fetched_at.isoformat() if row.fetched_at else "",
        expires_at=row.expires_at.isoformat() if row.expires_at else "",
    )

def marketplace_toggle(
    node_definition_id: int,
    request: _MarketplaceToggleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Marketplace M2 — flip a system NodeDefinition's marketplace flag.

    Platform-admin gate: only platform admins can publish/unpublish
    marketplace nodes. Tenants can't reach this endpoint and can't
    flip the flag on their own nodes (it'd have no effect anyway —
    only system-shared rows tenant_id IS NULL participate in the
    marketplace surface).

    Idempotent: re-toggling to the same state is a no-op (returns the
    current state without writing). Audit-logged either way.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.marketplace_toggle(
            db, node_definition_id=node_definition_id, request=request, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def admin_curate_list(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Platform-admin curate feed — returns user-curateable system-
    shared NodeDefinitions regardless of marketplace flag, so the
    admin can see uncurated candidates and flip them on. Bypasses
    the tenant visibility filter (which gates on entitlement).

    `system_only=TRUE` rows are excluded: those are internal
    operational utilities (benchmarks, LLM inference, trivy scans,
    scheduler bootstrap, etc.) that the API dispatches automatically
    and that aren't meant for human composition. Surfacing them in
    the curate table only invites a misclick that puts an unusable
    node on the public marketplace.

    `type = 'System'` is excluded too as belt-and-suspenders against
    a row that drifted out of sync (system_only=False on disk because
    a stale rescan never re-applied the metadata file's updated flag).
    The two conditions OR together — a misconfigured row gets caught
    by whichever fires first. We hit this once in stage where the
    benchmark NodeDefinitions seeded before `system_only` was added
    to their metadata files were stuck with system_only=False and
    showed up on Curate as toggleable "System" rows; fixed by
    backfilling the DB + adding this guard so it can't recur.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.admin_curate_list(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def tenant_curate_list(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """My Nodes (Curate) — the tenant's own nodes (tenant_id = current
    tenant). Where an admin tests built nodes and decides which to add to
    the marketplace. Each item carries is_in_marketplace so the UI shows
    whether it's already in the marketplace. Admin-only (parent user).
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.tenant_curate_list(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def tenant_add_to_marketplace(
    node_definition_id: int,
    request: _AddToMarketplaceRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Add a node from My Nodes to the marketplace (Browse).

    target:
      - this_tenant     — flag it into the marketplace for the current
                          tenant (node stays tenant-owned). Tenant admin.
      - all_tenants     — publish as a platform node visible in EVERY
                          tenant's Browse. Platform admin only.
      - specific_tenants — publish to the listed tenants only (entitles
                          each so it lands in their palette). Platform
                          admin only; requires tenant_ids.

    404 for a node that isn't the caller's tenant node (no cross-tenant
    leak); 403 when a non-platform-admin asks for cross-tenancy targets.
    """
    # GSEC FX.79 p2a — a live data-marking whose label sets
    # blocks_external_share stops the node from reaching the marketplace.
    from utils.paid_import import optional_paid
    _dm_p2a = optional_paid("services.data_markings")
    if _dm_p2a is not None:  # FX.162.p5 — CE carve: no markings -> publishable
        _dm_p2a.assert_shareable(
            db, current_user.tenant_id, "node_definition", node_definition_id,
            action="marketplace_publish", user_id=current_user.id)
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.tenant_add_to_marketplace(
            db, node_definition_id=node_definition_id, request=request, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def tenant_marketplace_toggle(
    node_definition_id: int,
    request: _MarketplaceToggleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Tenant-admin flips `is_in_marketplace` on a tenant-shared node.

    Refuses on:
    - System rows (`tenant_id IS NULL`) — those are platform-admin scope.
    - User-private rows (`user_id IS NOT NULL`) — admin must promote via
      `share-with-tenant` first.
    - Nodes owned by a different tenant — returned as 404 to avoid
      leaking row existence across tenants.

    Idempotent: re-toggling to the same state is a no-op (returns the
    current state without writing). Audit-logged either way as
    NODE_MARKETPLACE_TOGGLED.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.tenant_marketplace_toggle(
            db, node_definition_id=node_definition_id, request=request, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def tenant_team_grant(
    node_definition_id: int,
    request: _TenantTeamGrantRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Tenant-admin grants a Team `use` access to a tenant-shared NodeDef.

    Idempotent on `(resource_type=NODE_DEFINITION, resource_id, team_id)`:
    if an active non-revoked grant already exists, returns it without
    creating a duplicate. Soft-revoked grants for the same triple do
    NOT block; a fresh active row is created and the revoked row stays
    as audit history.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.tenant_team_grant(
            db, node_definition_id=node_definition_id, request=request, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def tenant_team_revoke(
    node_definition_id: int,
    permission_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Tenant-admin revokes a team grant (soft-delete).

    Sets `is_active=False, revoked_at=now(), revoked_by_user_id=admin`.
    The visibility filter already treats this as no-grant, so the node
    flips back to default-visible (or to whatever other team grants
    remain). Hard-delete is avoided so the audit trail of who-had-
    access-when survives.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.tenant_team_revoke(
            db, node_definition_id=node_definition_id, permission_id=permission_id, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def set_node_price(
    node_definition_id: int,
    body: schemas.NodePriceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """node-ownership-purchases — platform admin sets/clears a maintained node's
    one-time ownership price. Only system nodes may be priced."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.set_node_price(
        db, node_definition_id=node_definition_id, body=body, current_user=current_user,
    )

def admin_list_system_nodes(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Platform admin: every live system node (tenant_id NULL), ordered by name."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.admin_list_system_nodes(db, current_user=current_user)

def admin_export_paid_node_listings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """MKT.M1a — platform admin: every priced + marketplace-listed system
    node, shaped for the Marketplace's `migrate_core_listings.py` importer
    (id/name/description/price_cents/currency/version/is_in_marketplace).

    Read-only export, no Core purchase-path behaviour touched — feeds the
    one-time migration of these listings into the Marketplace service.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.admin_export_paid_node_listings(db, current_user=current_user)

def public_node_detail(node_definition_id: int, db: Session = Depends(get_db)):
    """Public (no-auth) node detail for the standalone store page — display-only,
    no secrets, only a published marketplace node."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.public_node_detail(db, node_definition_id=node_definition_id)

def set_node_sample(
    node_definition_id: int,
    body: _NodeSampleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Admin — set a node's curated demo (sample input/output + optional demo workflow)."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.set_node_sample(
        db, node_definition_id=node_definition_id, body=body, current_user=current_user,
    )

def mark_marketplace_seen(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Stamp 'tenant opened the store' — clears the new-nodes badge."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.mark_marketplace_seen(db, current_user=current_user)

def marketplace_new_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Count of buyable nodes the tenant doesn't own, published since they last
    opened the store — the 'N new nodes to buy' badge."""
    if _marketplace_paid is None:
        # 2400UI93 CE carve: the Dashboard marketplace badge (CORE/Dashboard/js/ui.js)
        # calls this on EVERY page load; the generic 404 read as a broken route (E2E
        # net guard failed active-sessions). With no paid marketplace there is nothing
        # to buy — answer the same shape the badge consumes instead.
        return {"new_count": 0}
    try:
        return _marketplace_paid.marketplace_new_count(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def try_node(
    node_definition_id: int,
    body: _NodeTryRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """node-trial-runs — run the node's trivial demo workflow into the tenant's
    real folders (try-before-buy). Demo data run locally is free; own data is
    charged compute normally. Paywall-exempts ONLY this node (purpose=node_trial).
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.try_node(
            db, node_definition_id=node_definition_id, body=body, request=request,
            background_tasks=background_tasks, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def list_node_packages(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Store — official node packages with this tenant's ownership state."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.list_node_packages(db, current_user=current_user)

def buy_node_package(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Buy a package — Stripe one-time checkout. Ownership of every node in the
    package is granted by the webhook. Already-owned → status='granted'."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.buy_node_package(db, package_id=package_id, current_user=current_user)

def create_node_package(
    body: schemas.NodePackageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Admin — create an official node package and (optionally) seed its nodes."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.create_node_package(db, body=body, current_user=current_user)

def add_node_package_item(
    package_id: int,
    body: schemas.NodePackageItemAdd,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Admin — add a node to a package."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.add_node_package_item(db, package_id=package_id, body=body, current_user=current_user)

def bind_package_to_plan(
    package_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Admin — include a package free in a plan (grants via the reconciler)."""
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    return _marketplace_paid.bind_package_to_plan(db, package_id=package_id, plan_id=plan_id, current_user=current_user)

def marketplace_list(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Marketplace M4 — list all nodes MOD has flagged for the
    marketplace, with a flag for each indicating whether the
    current tenant has already downloaded (entitled) it.

    Returns system-shared rows (tenant_id IS NULL) where
    is_in_marketplace=TRUE. Authenticated tenants of any role can
    see this list (it's public-within-the-platform, like a catalogue).

    system_only=TRUE rows are excluded as belt-and-suspenders: those
    are internal utilities (benchmarks, LLM inference, trivy scans)
    and should never have is_in_marketplace=TRUE in the first place
    — but if one ever does, we still don't want it browsable.

    `type='System'` is also excluded so a stale-rescan row that has
    system_only=False but is clearly internal (benchmarks, etc.) stays
    hidden from tenant Browse.
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.marketplace_list(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def marketplace_download(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Marketplace M4 — tenant clicks "Download" on a marketplace node.

    Inserts a TenantNodeEntitlement row, making the node visible in
    the tenant's WorkflowBuilder palette via the M3 visibility filter.

    Idempotent: if the tenant already has an entitlement, returns
    the existing record with ``already_owned=True``. No second row
    created (the unique constraint would block it anyway).

    Refuses (404) when:
      - The node doesn't exist
      - The node isn't currently flagged for the marketplace
        (tenants can only download from the published catalogue)
      - The node isn't system-shared (tenant_id IS NOT NULL)
    """
    if _marketplace_paid is None:
        _paid_feature_unavailable()
    try:
        return _marketplace_paid.marketplace_download(
            db, node_definition_id=node_definition_id, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def describe_search(
    request: _DescribeSearchRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """δ — first wizard call for the Describe-and-build source type.

    Takes the user's free-text description, classifies intent
    (Phase 4), if wrap_repo searches GitHub (Phase 2) and scores
    the candidates (Phase 5). Caches the whole response for 24h
    deployment-wide (Phase 3) so iterating users hit the cache.
    Per-tenant rate-limited to 5 fresh searches / 15 min.

    Returns 503 (``not_configured=True``) when the platform admin
    hasn't set ``github_token``. Returns 200 with
    ``rate_limited=True`` and an empty candidate list when the
    tenant is over their per-window quota — wizard renders a
    cooldown banner and offers "Build from scratch" only.
    """
    from utils import describe_cache, github_search, repo_safety
    from node_builder import describe_intent, describe_score

    description = (request.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if len(description) > 2000:
        raise HTTPException(status_code=400, detail="description must be 2000 characters or fewer")

    # Cache check first. Cache hits don't consume the rate-limit budget.
    # A re-fire carrying the user's clarifying answer must NOT be served from
    # cache — the cached response predates the answer (and is likely the weak /
    # needs_clarification result that prompted it). Bypass the cache so the
    # answer is folded into the pool and recorded as a learned crossover.
    _has_tool_answer = bool((request.user_tool_answer or "").strip())
    cached = None if _has_tool_answer else describe_cache.lookup(description)
    if cached:
        return _DescribeSearchResponse(
            intent=cached.get("intent", "from_scratch"),
            interpretation=cached.get("interpretation", ""),
            search_keywords=cached.get("search_keywords", []),
            candidates=cached.get("candidates", []),
            recommend_build_fresh=cached.get("recommend_build_fresh", False),
            surface=cached.get("surface", "node"),
            surface_reason=cached.get("surface_reason", ""),
            needs_clarification=cached.get("needs_clarification", False),
            clarifying_question=cached.get("clarifying_question", ""),
            cached=True,
        )

    # Rate-limit gate.
    rate = _check_and_inc_describe_rate(current_user.tenant_id)
    if not rate["allowed"]:
        return _DescribeSearchResponse(
            intent="from_scratch",
            interpretation="",
            search_keywords=[],
            candidates=[],
            rate_limited=True,
            rate_limit_remaining=0,
            rate_limit_reset_seconds=rate["reset_seconds"],
        )

    # Intent classification + Critic B (Search-Intent Auditor).
    # The bare classifier has been observed producing keyword sets
    # that were tangential to the user's actual concept or too
    # narrow to surface real repos. The auditor catches both before
    # we burn GitHub search quota.
    from node_builder.critic_search_intent import classify_intent_with_critic
    intent_result = classify_intent_with_critic(description)

    candidates_payload: List[Dict[str, Any]] = []
    recommend_build_fresh = False
    needs_clarification = False
    clarifying_question = ""
    # ALWAYS search — the intent classifier (a small local model) systematically
    # under-calls `wrap_repo`: it labelled "text to image", "transcribe audio to
    # text", "remove background", etc. as `from_scratch`, so GitHub was NEVER
    # searched and the wizard fell back to writing an (often hallucinated) script
    # from zero. Intent must NOT gate discovery — a weak model's triage guess
    # can't be the reason real solutions are never looked for. Both intents are
    # in `_VALID_INTENTS`, so this runs for every classified request; the
    # relevance scorer + `should_recommend_build_fresh` (score-driven, below)
    # still route genuinely trivial tasks straight to build-fresh even when a
    # search ran. Intent now only informs the UI recommendation, never the gate.
    if intent_result.intent in _INTENTS_THAT_SEARCH:
        token = _load_platform_github_token(db)
        if not token:
            # Platform admin hasn't configured the token. Refund the
            # rate-limit slot since we didn't actually search.
            _decrement_describe_rate(current_user.tenant_id)
            return _DescribeSearchResponse(
                intent=intent_result.intent,
                interpretation=intent_result.interpretation,
                search_keywords=intent_result.search_keywords,
                candidates=[],
                not_configured=True,
                surface=intent_result.surface,
                surface_reason=intent_result.surface_reason,
            )

        # add-web-search-node §6 — web query-expansion. Run the hidden
        # "System — Web Search" workflow (as a real, logged run) to mine
        # jargon + candidate repos/packages from the live web. Its jargon
        # is folded into the keyword pool below and its repos/packages are
        # seeded into the candidate list BEFORE the reranker. Best-effort:
        # None on any failure/timeout → we proceed on the intent
        # classifier's keywords alone.
        web_jargon: List[str] = []
        web_github_repos: List[str] = []
        web_pypi_packages: List[str] = []
        web_artifact = _run_web_query_expansion(
            db, description, user_id=current_user.id,
        )
        if web_artifact:
            web_jargon = [
                j.strip() for j in (web_artifact.get("jargon") or [])
                if isinstance(j, str) and j.strip()
            ]
            web_github_repos = [
                r.strip() for r in (web_artifact.get("github_repos") or [])
                if isinstance(r, str) and r.strip()
            ]
            web_pypi_packages = [
                p.strip() for p in (web_artifact.get("pypi_packages") or [])
                if isinstance(p, str) and p.strip()
            ]

        # Curated CROSSOVER table — colloquial task -> canonical domain jargon +
        # tool names (photogrammetry->COLMAP, "transcribe"->whisper, "remove
        # background"->rembg). Highest-signal bridge for common task families;
        # layered ON TOP of the live web/Wikipedia extraction (which handles the
        # long tail) and fires even when the web is unreachable, so common tasks
        # never miss. Its jargon is searched on GitHub, its repos are seeded, and
        # its pypi names become GitHub search terms — same paths as the web hits.
        try:
            from node_builder.jargon_map import crossover_for
            _xover = crossover_for(description)
        except Exception as _xo_err:  # noqa: BLE001 — never break discovery
            logger.info("describe-search: crossover lookup skipped: %s", _xo_err)
            _xover = {"jargon": [], "github": [], "pypi": []}
        _jl = {x.lower() for x in web_jargon}
        for _j in _xover.get("jargon", []):
            if _j and _j.lower() not in _jl:
                web_jargon.append(_j); _jl.add(_j.lower())
        _rl = {x.lower() for x in web_github_repos}
        for _r in _xover.get("github", []):
            if _r and _r.lower() not in _rl:
                web_github_repos.append(_r); _rl.add(_r.lower())
        _pl = {x.lower() for x in web_pypi_packages}
        for _p in _xover.get("pypi", []):
            if _p and _p.lower() not in _pl:
                web_pypi_packages.append(_p); _pl.add(_p.lower())
        if _xover.get("github") or _xover.get("pypi"):
            logger.info("describe-search: crossover matched — +%d jargon, +%d repos, "
                        "+%d pypi", len(_xover.get("jargon", [])),
                        len(_xover.get("github", [])), len(_xover.get("pypi", [])))

        # LEARNED crossover — the human-assisted-discovery sibling of the
        # curated table. Rows recorded from prior "ask the user" answers,
        # retrieved by arctic-embed cosine on the description. Unioned into the
        # SAME web_* pools as the curated crossover (learned jargon searched,
        # learned repos seeded, learned pypi names become GitHub search terms).
        # Best-effort — {} on any failure.
        try:
            from node_builder.learned_crossover import learned_crossover_for
            _learned = learned_crossover_for(
                db, description, tenant_id=current_user.tenant_id,
            )
        except Exception as _lx_err:  # noqa: BLE001 — never break discovery
            logger.info("describe-search: learned crossover lookup skipped: %s", _lx_err)
            _learned = {}
        if not isinstance(_learned, dict):
            _learned = {}
        _jl = {x.lower() for x in web_jargon}
        for _j in _learned.get("jargon", []):
            if _j and _j.lower() not in _jl:
                web_jargon.append(_j); _jl.add(_j.lower())
        _rl = {x.lower() for x in web_github_repos}
        for _r in _learned.get("github", []):
            if _r and _r.lower() not in _rl:
                web_github_repos.append(_r); _rl.add(_r.lower())
        _pl = {x.lower() for x in web_pypi_packages}
        for _p in _learned.get("pypi", []):
            if _p and _p.lower() not in _pl:
                web_pypi_packages.append(_p); _pl.add(_p.lower())
        if _learned.get("github") or _learned.get("pypi") or _learned.get("jargon"):
            logger.info("describe-search: learned crossover matched — +%d jargon, "
                        "+%d repos, +%d pypi", len(_learned.get("jargon", [])),
                        len(_learned.get("github", [])), len(_learned.get("pypi", [])))

        # human-assisted-discovery re-fire — the user answered a prior clarifying
        # question with a library / GitHub repo / URL / technical term. Parse it,
        # fold it into the same pools (jargon + pypi become search terms, a repo
        # is seeded via fetch_repo_metadata below), and record it as a learned
        # crossover keyed to this description so a near-identical future query
        # retrieves it without re-asking. Recording is best-effort and runs BEFORE
        # the GitHub search so a rate-limit / not-configured early-return can't
        # lose the learning.
        if _has_tool_answer:
            _ans_repo, _ans_kw, _ans_pkg = _parse_user_tool_answer(
                request.user_tool_answer,
            )
            if _ans_repo and _ans_repo.lower() not in {r.lower() for r in web_github_repos}:
                web_github_repos.append(_ans_repo)
            if _ans_kw and _ans_kw.lower() not in {j.lower() for j in web_jargon}:
                web_jargon.append(_ans_kw)
            if _ans_pkg and _ans_pkg.lower() not in {p.lower() for p in web_pypi_packages}:
                web_pypi_packages.append(_ans_pkg)
            try:
                from node_builder.learned_crossover import record_learned_crossover
                record_learned_crossover(
                    db,
                    tenant_id=current_user.tenant_id,
                    description=description,
                    jargon=[_ans_kw] if _ans_kw else [],
                    github=[_ans_repo] if _ans_repo else [],
                    pypi=[_ans_pkg] if _ans_pkg else [],
                    created_by=getattr(current_user, "id", None),
                )
            except Exception as _rec_err:  # noqa: BLE001 — learning never breaks search
                logger.info("describe-search: record learned crossover skipped: %s", _rec_err)

        try:
            # GitHub search treats whitespace as AND, so simply joining
            # all keywords reads as "every term must appear" — image-
            # to-3D repos like TRELLIS, Hunyuan3D, OpenLRM all
            # returned 0 candidates because no single repo description
            # contained EVERY keyword the intent classifier produced.
            #
            # Strategy:
            #   1. One per-keyword search (each phrase-quoted) so each
            #      semantic facet gets its own top-N repos returned by
            #      GitHub. This surfaces niche repos that match a
            #      specific keyword strongly even when GitHub's stars-
            #      desc order would have buried them on a single
            #      broader query.
            #   2. Plus one OR'd umbrella query as a safety net so
            #      cross-keyword stars-desc hits aren't lost.
            #   3. Merge, dedupe by full_name, cap to ~30 raw candidates
            #      (the relevance scorer then ranks against the user's
            #      original description).
            #
            # Stage repro: 2026-05-26 "image to 3d model" wizard call;
            # AND-joined query returned 0 candidates against a real
            # field of TRELLIS / Hunyuan3D / Stability-AI / OpenLRM.
            kws = [k.strip() for k in (intent_result.search_keywords or []) if k.strip()]
            # Fold web-mined jargon + pypi package names into the keyword
            # pool (deduped, order-preserving, case-insensitive). PyPI
            # package names make good GitHub search terms — they usually
            # match the hosting repo's name/description.
            _seen_kw = {k.lower() for k in kws}
            for _extra in list(web_jargon) + list(web_pypi_packages):
                if _extra and _extra.lower() not in _seen_kw:
                    kws.append(_extra)
                    _seen_kw.add(_extra.lower())
            seen: dict = {}
            raw_candidates_list: list = []
            queries: list = []
            if kws:
                for k in kws:
                    queries.append(f'"{k}"' if " " in k else k)
                quoted = [f'"{k}"' if " " in k else k for k in kws]
                queries.append(" OR ".join(quoted))
            else:
                queries.append((description or "")[:200])
            for q in queries:
                if not q.strip():
                    continue
                try:
                    batch = github_search.search_repos(q, token=token, max_n=10)
                except (github_search.RateLimited, github_search.NotConfigured):
                    raise
                except github_search.GitHubError as ge:
                    logger.warning("describe-search per-keyword query failed: %s", ge)
                    continue
                for c in batch:
                    if c.full_name in seen:
                        continue
                    seen[c.full_name] = True
                    raw_candidates_list.append(c)
                if len(raw_candidates_list) >= 30:
                    break
            # Seed web-discovered repos ("owner/repo") as candidates,
            # deduped against the keyword-search hits, so they enter the
            # pool the reranker scores. fetch_repo_metadata resolves each to
            # a RepoCandidate; a rate-limit / not-configured error re-raises
            # to the handlers below (same as the per-keyword loop), other
            # per-repo failures are skipped (best-effort seeds).
            for _repo_full in web_github_repos:
                if _repo_full in seen:
                    continue
                try:
                    _cand = github_search.fetch_repo_metadata(_repo_full, token=token)
                except (github_search.RateLimited, github_search.NotConfigured):
                    raise
                except github_search.GitHubError as ge:
                    logger.debug(
                        "describe-search: web-seed repo %s failed: %s",
                        _repo_full, ge,
                    )
                    continue
                if _cand is None:
                    continue
                if _cand.full_name in seen:
                    continue
                seen[_cand.full_name] = True
                raw_candidates_list.append(_cand)
            raw_candidates = raw_candidates_list
        except github_search.NotConfigured:
            _decrement_describe_rate(current_user.tenant_id)
            return _DescribeSearchResponse(
                intent=intent_result.intent,
                interpretation=intent_result.interpretation,
                search_keywords=intent_result.search_keywords,
                candidates=[],
                not_configured=True,
                surface=intent_result.surface,
                surface_reason=intent_result.surface_reason,
            )
        except github_search.RateLimited:
            return _DescribeSearchResponse(
                intent=intent_result.intent,
                interpretation=intent_result.interpretation,
                search_keywords=intent_result.search_keywords,
                candidates=[],
                rate_limited=True,
                rate_limit_remaining=rate["remaining"],
                rate_limit_reset_seconds=rate["reset_seconds"],
            )
        except github_search.GitHubError as e:
            logger.warning("describe-search GitHub error: %s", e)
            raw_candidates = []

        # Repo-safety filter — drop hard-blocks; pass warnings through.
        from node_builder.jargon_map import known_license_for
        safe_candidate_dicts: List[Dict[str, Any]] = []
        for cand in raw_candidates:
            # Backfill a verified-permissive licence for a curated repo that
            # GitHub reports as NOASSERTION/null (COLMAP=BSD-3, Meshroom=MPL-2.0):
            # GitHub's licence auto-detection misses their non-standard LICENSE
            # files, and the safety gate hard-blocks a null licence — silently
            # deleting the canonical photogrammetry tools the crossover table is
            # trying to promote. Only fills a null; never overrides a licence
            # GitHub actually reported, and only for the hand-verified allowlist
            # in jargon_map (a genuinely copyleft repo like openMVS stays blocked).
            if not cand.license_spdx:
                _known = known_license_for(cand.full_name)
                if _known:
                    cand.license_spdx = _known
            safety = repo_safety.evaluate_repo(cand.metadata())
            if not safety.ok:
                # Hard-blocked candidate (typically licence) — never
                # surface to the user.
                continue
            d = cand.as_dict()
            d["safety_ok"] = True
            d["safety_warnings"] = safety.warnings
            safe_candidate_dicts.append(d)

        # Install-strategy probe — detect whether each candidate is
        # pip-installable / clone-and-PYTHONPATH-able / dockerfile-as-base
        # / unusable. The wizard's synthesize step previously assumed
        # every repo could be `pip install git+<url>`-ed; the result was
        # a build failure on repos like AlexBuzaev75/cad-to-pdf-converter
        # (just .py files at root, no setup.py). Drop unusable repos
        # outright (user can't pick them); pass the strategy through to
        # the synthesize prompt for the rest.
        ranked_candidate_dicts: List[Dict[str, Any]] = []
        for d in safe_candidate_dicts:
            strategy = github_search.detect_install_strategy(
                d["full_name"], token=token,
            )
            if strategy == "unusable":
                continue
            d["install_strategy"] = strategy
            ranked_candidate_dicts.append(d)

        # Score what's left, then apply the composite (relevance ×
        # log-scaled stars) and flag the top candidate as
        # `is_recommended` if it has a meaningful lead AND decent
        # absolute quality. The wizard's Describe chat renders the
        # recommended candidate as a prominent "best match" card
        # above the regular cards.
        scored = describe_score.score_candidates(description, ranked_candidate_dicts)
        # Crossover AUTHORITY floor — a repo named by the curated table, a
        # learned crossover, or the user's own tool answer IS the canonical
        # answer for this task family (human-vetted, not a keyword guess). Give
        # it a relevance floor so a sparse GitHub description can't rank the
        # canonical tool below a generic keyword hit. Applied BEFORE the
        # composite so both the star-weighted ordering and the is_recommended
        # pick see the floor.
        _trusted_repos = {
            r.lower() for r in
            (list(_xover.get("github") or []) + list(_learned.get("github") or []))
            if isinstance(r, str) and r
        }
        if _has_tool_answer and _ans_repo:
            _trusted_repos.add(_ans_repo.lower())
        if _trusted_repos:
            for sc in scored:
                if (sc.full_name or "").lower() in _trusted_repos:
                    sc.relevance = max(
                        sc.relevance, describe_score._CROSSOVER_RELEVANCE_FLOOR,
                    )
        scored = describe_score.apply_composite_and_recommendation(scored)
        candidates_payload = [s.as_dict() for s in scored]
        # Trivial-task gate: a search ran but nothing scored well enough
        # to be worth showing → tell the wizard to skip the candidate
        # wall and go straight to build-fresh. (Don't suppress the
        # candidates themselves — the flag drives the routing.)
        recommend_build_fresh = describe_score.should_recommend_build_fresh(scored)

        # human-assisted-discovery WEAK verdict. When NO curated crossover and
        # NO learned crossover matched AND the search pool is weak (build-fresh
        # recommended, or the best candidate is below the worth-showing floor —
        # an empty pool counts as weak), the wizard doesn't have enough to go on:
        # ask the user for a library / GitHub repo / URL / technical term rather
        # than silently building fresh. Suppressed on a re-fire (the user has
        # already answered) so we don't loop the same question.
        _curated_matched = bool(
            _xover.get("jargon") or _xover.get("github") or _xover.get("pypi")
        )
        _learned_matched = bool(
            _learned.get("jargon") or _learned.get("github") or _learned.get("pypi")
        )
        _max_relevance = max(
            (int(c.get("relevance") or 0) for c in candidates_payload),
            default=0,
        )
        _pool_weak = (
            recommend_build_fresh
            or _max_relevance < describe_score._WORTH_SHOWING_RELEVANCE
        )
        if (not _curated_matched and not _learned_matched
                and _pool_weak and not _has_tool_answer):
            needs_clarification = True
            clarifying_question = (
                "I couldn't find a strong match for that. Do you know a specific "
                "library, a GitHub repo or URL, or a technical term for what "
                "you're trying to do? Share one and I'll search again."
            )

    response_dict = {
        "intent": intent_result.intent,
        "interpretation": intent_result.interpretation,
        "search_keywords": intent_result.search_keywords,
        "candidates": candidates_payload,
        "recommend_build_fresh": recommend_build_fresh,
        "surface": intent_result.surface,
        "surface_reason": intent_result.surface_reason,
        "needs_clarification": needs_clarification,
        "clarifying_question": clarifying_question,
    }

    # Cache for 24h. Don't cache rate-limited / not-configured
    # responses — those are state-of-the-deployment, not state-of-
    # the-query. Also don't cache score-LLM-timeout responses where
    # every candidate has relevance=0 (the LLM is currently down /
    # slow; results will be much better on the next call when it
    # comes back). Stage repro 2026-05-27: score-LLM hit 30s timeout
    # on a 22-candidate response, all relevance=0, got cached for
    # 24h — every subsequent "image to 3d" query hit the zero-score
    # cache, the wizard's "ready_to_build" fall-through kicked in,
    # users never saw repo cards (TRELLIS / Hunyuan3D / Wonder3D
    # never surfaced despite being IN the candidate list).
    score_llm_failed = (
        bool(candidates_payload)
        and all((c.get("relevance") or 0) == 0 for c in candidates_payload)
    )
    # Don't cache a needs_clarification verdict: once the user answers, a learned
    # crossover is recorded, and the NEXT plain call (which hits the cache) must
    # re-run learned_crossover_for to pick it up instead of replaying the stale
    # "I couldn't find a match" response forever.
    if needs_clarification:
        logger.info(
            "describe-search: skipping cache write — needs_clarification "
            "(weak pool, no crossover); next call re-runs learned lookup"
        )
    elif not score_llm_failed:
        describe_cache.store(description, response_dict)
    else:
        logger.info(
            "describe-search: skipping cache write — all %d candidates "
            "have relevance=0 (score LLM likely timed out, not a real "
            "state-of-the-query)",
            len(candidates_payload),
        )

    return _DescribeSearchResponse(
        cached=False,
        rate_limit_remaining=rate["remaining"],
        rate_limit_reset_seconds=rate["reset_seconds"],
        **response_dict,
    )

def describe_synthesize(
    request: _DescribeSynthesizeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """δ Phase 8 — synthesize a wrapper script.

    Called after the user picks a repo (or "from scratch") on the
    Describe-and-build tab. Returns a script + requirements + base
    image hint + I/O type suggestions for the wizard to drop into
    its Script-source state.

    ``previous_attempt`` lets the wizard retry after a γ sample-test
    failure or a build-time error, threading the prior script + error
    into the next prompt so the LLM corrects it instead of starting
    from scratch.

    Per-tenant rate-limited to 10 calls per 15 min.
    """
    description = (request.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if len(description) > 2000:
        raise HTTPException(status_code=400, detail="description must be 2000 characters or fewer")

    rate = _check_and_inc_synth_rate(current_user.tenant_id)
    if not rate["allowed"]:
        return _DescribeSynthesizeResponse(
            ok=False,
            rejection_reasons=[
                f"Rate-limited; try again in {rate['reset_seconds']} seconds."
            ],
            rate_limited=True,
            rate_limit_reset_seconds=rate["reset_seconds"],
        )

    from node_builder import describe_synthesize as ds

    # Exemplar seeding — retrieve a prior WORKING node similar to this
    # request and hand it to the generator as a reference (adapt, don't
    # invent). Fail-open: retrieval errors just mean no exemplar.
    exemplar = None
    try:
        from node_builder.exemplar_retrieval import fetch_exemplar
        exemplar = fetch_exemplar(
            db, description,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
        )
        # No curated-recipe fallback: the wizard stands on its own via
        # docs-driven synthesis. A learned exemplar (the user's own smoke-passed
        # node) still seeds when one exists; otherwise synthesis works from the
        # description + the chosen repo's docs alone.
    except Exception as _ex_exc:
        logger.info("exemplar seeding skipped: %s", _ex_exc)

    # Read the chosen repo's OWN docs (README / install / usage) and feed them
    # to the synthesizer so it wraps a repo it has actually read, instead of
    # guessing the tool's usage from the repo name (the cause of hallucinated
    # `pip install git+…colmap` / `import colmap` wrappers). Best-effort.
    repo_context = None
    if request.repo_url:
        repo_context = ds.fetch_repo_context(
            db, repo_url=request.repo_url,
            tenant_id=current_user.tenant_id,
            auth_token=_load_platform_github_token(db),
        )

    result = ds.synthesize_wrapper(
        description,
        repo_url=request.repo_url,
        repo_full_name=request.repo_full_name,
        install_strategy=request.install_strategy,
        previous_attempt=request.previous_attempt,
        user_input_type=request.user_input_type,
        user_output_type=request.user_output_type,
        exemplar=exemplar,
        repo_context=repo_context,
        already_clarified=bool(getattr(request, "already_clarified", False)),
    )
    return _DescribeSynthesizeResponse(
        ok=result.ok,
        script_content=result.script_content,
        requirements=result.requirements,
        base_image_hint=result.base_image_hint,
        system_packages=result.system_packages,
        dockerfile_extras=result.dockerfile_extras,
        suggested_input_type=result.suggested_input_type,
        suggested_output_type=result.suggested_output_type,
        reasoning=result.reasoning,
        rejection_reasons=result.rejection_reasons,
        rejection_category=result.rejection_category,
        attempts=result.attempts,
        warnings=list(getattr(result, "warnings", []) or []),
        needs_clarification=bool(getattr(result, "needs_clarification", False)),
        clarifying_question=str(getattr(result, "clarifying_question", "") or ""),
        exemplar_used=((exemplar or {}).get("name") if result.ok else None),
        exemplar_source=((exemplar or {}).get("source") if result.ok else None),
    )

async def describe_synthesize_stream(
    request: _DescribeSynthesizeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Streaming variant of /describe-synthesize (piece 3 — in-the-loop UX).

    synthesize_wrapper loops internally (generate → review → revise →
    escalate) inside one long call; the non-streaming endpoint hides all
    of it behind a single response, so the wizard looks frozen. This
    endpoint emits SSE progress events as those stages happen, then a
    final `result` event carrying the same payload as the JSON endpoint.

    Wire format (one JSON object per SSE `data:` line):
      {"t":"progress","event":"generating|reviewing|revising|escalating|seeded","data":{...}}
      {"t":"result", ...same fields as _DescribeSynthesizeResponse...}
    """
    import asyncio
    import json as _json
    import threading

    from fastapi.responses import StreamingResponse

    description = (request.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if len(description) > 2000:
        raise HTTPException(status_code=400, detail="description must be 2000 characters or fewer")

    rate = _check_and_inc_synth_rate(current_user.tenant_id)

    from node_builder import describe_synthesize as ds

    # Exemplar retrieval needs the request-scoped db session — do it here on
    # the main thread, then hand the plain dict to the worker thread.
    exemplar = None
    if rate["allowed"]:
        try:
            from node_builder.exemplar_retrieval import fetch_exemplar
            exemplar = fetch_exemplar(
                db, description,
                tenant_id=current_user.tenant_id, user_id=current_user.id,
            )
            # No curated-recipe fallback — docs-driven synthesis stands alone;
            # a learned exemplar still seeds when the user has one.
        except Exception as _ex_exc:
            logger.info("exemplar seeding skipped: %s", _ex_exc)

    # Read the chosen repo's OWN docs on the main thread (needs the request-
    # scoped db) so the worker thread's synthesis wraps a repo it has actually
    # read rather than guessing from the name. Best-effort. See fetch_repo_context.
    repo_context = None
    if rate["allowed"] and request.repo_url:
        repo_context = ds.fetch_repo_context(
            db, repo_url=request.repo_url,
            tenant_id=current_user.tenant_id,
            auth_token=_load_platform_github_token(db),
        )

    loop = asyncio.get_running_loop()
    q: "asyncio.Queue" = asyncio.Queue()
    holder: dict = {}

    def _cb(event, data):
        # Called from the worker thread — hop back onto the event loop.
        try:
            loop.call_soon_threadsafe(
                q.put_nowait, {"t": "progress", "event": event, "data": data or {}}
            )
        except Exception:
            pass

    def _run():
        # mega-qa Billing #4 — this runs in a bare worker thread that does NOT
        # inherit the request's ContextVars, so the request principal set by the
        # auth dependency is invisible here and every wizard node-gen LLM call
        # (utils.llm_client reads utils.request_principal at the metering
        # boundary) attributes to no tenant → the calls are dropped from
        # per-tenant metering. Re-establish the principal in THIS thread from the
        # captured request user so those calls are metered to the tenant.
        from utils.request_principal import set_principal, reset_principal
        _ptok = set_principal(current_user)
        try:
            if not rate["allowed"]:
                holder["result"] = ds.SynthesisResult(
                    ok=False,
                    rejection_reasons=[
                        f"Rate-limited; try again in {rate['reset_seconds']} seconds."
                    ],
                )
                holder["rate_limited"] = True
            else:
                holder["result"] = ds.synthesize_wrapper(
                    description,
                    repo_url=request.repo_url,
                    repo_full_name=request.repo_full_name,
                    install_strategy=request.install_strategy,
                    previous_attempt=request.previous_attempt,
                    user_input_type=request.user_input_type,
                    user_output_type=request.user_output_type,
                    exemplar=exemplar,
                    repo_context=repo_context,
                    progress_cb=_cb,
                    already_clarified=bool(getattr(request, "already_clarified", False)),
                )
        except Exception as e:  # noqa: BLE001
            holder["error"] = str(e)
        finally:
            reset_principal(_ptok)
            try:
                loop.call_soon_threadsafe(q.put_nowait, {"t": "__done__"})
            except Exception:
                pass

    def _sse(obj) -> str:
        return f"data: {_json.dumps(obj)}\n\n"

    # mega-qa Billing #4 — run the synthesis inside a COPY of the request context
    # so the thread also carries the channel / api-key ContextVars an in-request
    # call would have (the principal itself is re-established inside _run from the
    # captured request user, which is bulletproof regardless of whether a sync
    # dependency's ContextVar mutation propagated into this async endpoint).
    import contextvars as _contextvars
    _ctx = _contextvars.copy_context()

    async def _stream():
        # Immediate ack so the client shows life before the first real stage.
        yield _sse({"t": "progress", "event": "starting", "data": {}})
        threading.Thread(target=_ctx.run, args=(_run,), daemon=True).start()
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                # Heartbeat during a long SILENT LLM generation. A small local
                # model on modest hardware can take minutes to emit the wrapper
                # JSON (scaled synth timeout), during which no stage event
                # fires; a periodic SSE comment keeps the connection from being
                # idle-dropped by a proxy or the browser.
                yield ": keepalive\n\n"
                continue
            if ev.get("t") == "__done__":
                break
            yield _sse(ev)
        res = holder.get("result")
        if res is not None:
            yield _sse({
                "t": "result",
                "ok": res.ok,
                "script_content": res.script_content,
                "requirements": res.requirements,
                "base_image_hint": res.base_image_hint,
                "system_packages": res.system_packages,
                "dockerfile_extras": res.dockerfile_extras,
                "suggested_input_type": res.suggested_input_type,
                "suggested_output_type": res.suggested_output_type,
                "reasoning": res.reasoning,
                "rejection_reasons": res.rejection_reasons,
                "rejection_category": res.rejection_category,
                "attempts": res.attempts,
                "warnings": list(getattr(res, "warnings", []) or []),
                "needs_clarification": bool(getattr(res, "needs_clarification", False)),
                "clarifying_question": str(getattr(res, "clarifying_question", "") or ""),
                "exemplar_used": ((exemplar or {}).get("name") if res.ok else None),
                "exemplar_source": ((exemplar or {}).get("source") if res.ok else None),
                "rate_limited": bool(holder.get("rate_limited")),
                "rate_limit_reset_seconds": (
                    rate.get("reset_seconds") if holder.get("rate_limited") else None
                ),
            })
        else:
            yield _sse({
                "t": "result", "ok": False,
                "rejection_reasons": [holder.get("error") or "synthesis failed"],
            })

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )

def describe_rewrite_prompt(
    request: _DescribeRewriteRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """δ §21.5 — personalise one scripted chat prompt.

    Wizard fires this fire-and-forget for the high-value prompts in
    the Describe chat (pick_repo / ask_input / ask_output /
    ready_repo / ready_scratch). On success the chat replaces the
    scripted text with the rewritten one; on failure (LLM down,
    timeout, bad response) the wizard leaves the canonical text in
    place — feature is best-effort polish, never blocking.
    """
    description = (request.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if len(description) > 2000:
        raise HTTPException(status_code=400, detail="description must be 2000 characters or fewer")
    canonical_text = (request.canonical_text or "").strip()
    if not canonical_text:
        raise HTTPException(status_code=400, detail="canonical_text is required")
    if len(canonical_text) > 600:
        raise HTTPException(status_code=400, detail="canonical_text must be 600 characters or fewer")

    from node_builder.describe_rewrite import ALLOWED_PROMPT_KEYS
    from node_builder.critic_chat_question import rewrite_prompt_with_critic
    if request.prompt_key not in ALLOWED_PROMPT_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"prompt_key must be one of: {sorted(ALLOWED_PROMPT_KEYS)}",
        )
    # Critic A: Chat-Question Auditor. The bare rewriter has been
    # observed inventing examples ("or videos") and asking about
    # info the user already provided. The auditor catches both.
    # On critic exhaustion (or critic outage), the result is the
    # last proposer attempt — caller's existing ok/text contract
    # is preserved.
    result = rewrite_prompt_with_critic(
        prompt_key=request.prompt_key,
        canonical_text=canonical_text,
        description=description,
        chosen_repo=request.chosen_repo,
        input_answer=request.input_answer,
        output_answer=request.output_answer,
    )
    if result.get("ok"):
        return _DescribeRewriteResponse(ok=True, text=result.get("text"))
    return _DescribeRewriteResponse(ok=False)

def admin_describe_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """δ Phase 13 — operator dashboard for the describe-and-build
    feature.

    Reports top picked repos (by build success count), 24h call
    volumes, cache hit ratio, and rate-limit incidents so the
    platform admin can see how the feature is being used and
    whether the limits are too tight or too loose.

    Reads from Redis counters (best-effort) + the BuildRemediation /
    NodeDefinition tables (authoritative). Restricted to platform
    admins via in-handler check (we don't have a clean dependency
    for it — mirrors the admin/remediation/stats pattern).
    """
    try:
        return _node_authoring_service.admin_describe_stats(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def check_source_license(
    request: _CheckSourceLicenseRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """δ Phase 12 — heuristic licence-header scan on user-uploaded
    source files (Script source-type, multi-file bundle).

    Calls source_file_license_scanner.scan_bundle. Returns ``ok=False``
    with per-file rejection reasons if any file's first 8 KB declares
    a hard-blocked licence (GPL/AGPL/SSPL/CC-BY-NC). Acts as a soft
    pre-flight; the wizard surfaces the result inline so the user
    can swap files before walking through the rest of the wizard.
    """
    from utils import source_file_license_scanner as scanner

    if not isinstance(request.files, list) or len(request.files) == 0:
        return _CheckSourceLicenseResponse(ok=True)
    result = scanner.scan_bundle(request.files)
    return _CheckSourceLicenseResponse(
        ok=result.ok,
        blocking_files=[
            {
                "filename": f.filename,
                "detected_license": f.detected_license,
                "reasons": f.reasons,
            }
            for f in result.blocking_files
        ],
        detected_licenses={
            f.filename: f.detected_license for f in result.files
        },
    )

def check_repo_safety(
    request: _CheckRepoSafetyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """δ Phase 11 — apply the same safety gate to user-pasted git
    URLs as we apply to describe-source candidates.

    Wizard's existing Repo source-type calls this when the user
    enters a URL. Refuses GPL/AGPL, archived/disabled, abuse-keyword
    repos before the Containerizer is even dispatched.

    For non-GitHub URLs we report ``not_github=True`` with ``ok=False``
    — defence in depth for the launch (we only have safety telemetry
    on GitHub-hosted repos right now). The platform admin may extend
    this surface to other forges later.
    """
    from utils import github_search, repo_safety

    raw_url = (request.repo_url or "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="repo_url is required")

    # Only GitHub-hosted repos go through the metadata fetch. Non-
    # GitHub URLs (gitlab, bitbucket, self-hosted gitea, etc.) get
    # a hard refuse with a clear message.
    parsed = github_search._parse_owner_repo(raw_url)
    if parsed is None:
        return _CheckRepoSafetyResponse(
            ok=False,
            reasons=[
                "We only build from GitHub-hosted repositories. Other "
                "git forges (GitLab, Bitbucket, self-hosted Gitea, etc.) "
                "are not supported in this launch — fork the repo to "
                "GitHub first if needed."
            ],
            not_github=True,
        )

    token = _load_platform_github_token(db)
    if not token:
        return _CheckRepoSafetyResponse(
            ok=False,
            reasons=[
                "Platform admin must configure github_token in Settings "
                "→ Secrets before we can check repository safety."
            ],
            not_configured=True,
        )

    try:
        candidate = github_search.fetch_repo_metadata(raw_url, token=token)
    except github_search.NotConfigured:
        return _CheckRepoSafetyResponse(
            ok=False,
            reasons=["Platform GitHub access is rate-limited; ask your platform admin."],
            not_configured=True,
        )
    except github_search.RateLimited:
        return _CheckRepoSafetyResponse(
            ok=False,
            reasons=["GitHub rate limit hit; please retry in a few minutes."],
        )
    except github_search.GitHubError as e:
        return _CheckRepoSafetyResponse(
            ok=False,
            reasons=[str(e)],
        )

    if candidate is None:
        return _CheckRepoSafetyResponse(
            ok=False,
            reasons=["Could not resolve this URL to a GitHub repository."],
            not_github=True,
        )

    safety = repo_safety.evaluate_repo(candidate.metadata())
    return _CheckRepoSafetyResponse(
        ok=safety.ok,
        reasons=list(safety.reasons),
        warnings=list(safety.warnings),
        license_spdx=safety.license_spdx,
        full_name=candidate.full_name,
        description=candidate.description,
    )

def wizard_preflight(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Cheap pre-flight check for the wizard's Source step.

    Tier 2 #1 — surface the credit gate BEFORE the user spends 30
    minutes of build time only to be rejected at registration with
    ``Insufficient credits`` (the failure mode that bit the TRELLIS.2
    driver run). The wizard fetches this on mount and shows a banner
    if ``can_create_node`` is False, so users top up first.

    §22.5 — wizard builds charge NODE_CREATION_CREDIT_COST twice
    (once at /generate-artifacts for the LLM, once at the register
    callback at end-of-build). Surface BOTH values so the banner
    can render the honest total. ``node_creation_cost`` keeps the
    original single-step semantics for back-compat with any caller
    that already reads it; ``wizard_total_cost`` is the new field
    callers should prefer when sizing the gate.

    Free, no side effects, no credit charge. SAAS-only; on-prem
    deployments report unlimited credits.
    """
    cost = NODE_CREATION_CREDIT_COST
    wizard_total = cost * 2  # /generate-artifacts + register callback
    # Bypass / unlimited in non-SaaS or when CREDITS_ENABLED is off.
    if (
        not _is_saas()
        # GS.PSR.8 — billing.credits_enabled via the platform-settings
        # registry (DB > env > True), imported from the shared module
        # (star-imported above).
        or not _credits_enabled(db)
        or getattr(current_user, "is_platform_admin", False)
    ):
        return {
            "node_creation_cost": cost,
            "wizard_total_cost": wizard_total,
            "credits_remaining": None,
            "can_create_node": True,
            "can_complete_wizard_build": True,
            "reason": None,
        }
    if current_user.tenant_id is None:
        return {
            "node_creation_cost": cost,
            "wizard_total_cost": wizard_total,
            "credits_remaining": 0,
            "can_create_node": False,
            "can_complete_wizard_build": False,
            "reason": "no tenant",
        }
    try:
        from utils.credit_manager import CreditManager
        balance = CreditManager.get_credit_balance(db, current_user.tenant_id)
    except Exception:
        # Fail-open on the preflight (the actual charge step still
        # gates correctly); we'd rather show "no banner" than crash
        # the wizard on a transient credit-manager error.
        return {
            "node_creation_cost": cost,
            "wizard_total_cost": wizard_total,
            "credits_remaining": None,
            "can_create_node": True,
            "can_complete_wizard_build": True,
            "reason": None,
        }
    # OWNER-DECISIONS 09-29 — plan + purchased pools combined for the
    # affordability check (purchased top-up credits pay for builds too).
    remaining = int(balance.get("credits_remaining") or 0) + int(
        balance.get("purchased_credits_remaining") or 0
    )
    can_create = remaining >= cost
    can_complete_wizard = remaining >= wizard_total
    if can_complete_wizard:
        reason = None
    elif can_create:
        reason = (
            f"have {remaining}, wizard build needs {wizard_total} "
            f"({cost} for AI generation + {cost} for node registration). "
            f"You can afford the AI step but not the registration that "
            f"follows — top up to avoid losing the build at the last step."
        )
    else:
        reason = f"need {cost} credits to even start, have {remaining}"

    # Disk-space pre-flight. Stage repro: TRELLIS.2 spec 12 ran 5
    # outer LLM repair attempts thrashing on bogus "docker socket"
    # explanations because the host's docker daemon was at 92% disk
    # usage and BuildKit was cancelling mid-build with errors that
    # contained the word "docker". The user spent ~5 minutes waiting
    # before seeing a useless failure message. Surface the disk
    # warning here so the wizard UI can show it before the user
    # commits to a multi-GB build.
    disk_warning = _check_host_docker_disk()
    return {
        "node_creation_cost": cost,
        "wizard_total_cost": wizard_total,
        "credits_remaining": remaining,
        "credits_total": int(balance.get("credits_total") or 0),
        "can_create_node": can_create,
        "can_complete_wizard_build": can_complete_wizard,
        "can_advance": bool(balance.get("can_advance")),
        "reason": reason,
        "disk_warning": disk_warning,
    }

def remediation_lookup(
    error_signature: str,
    error_text: Optional[str] = None,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),  # global cache: valid build token only, no spec scope
):
    """Return the cached diff for this error.

    Two-tier lookup:
      1. Exact match by signature hash (cheap, ~5ms).
      2. On miss, if ``error_text`` is provided, embed it and search
         qdrant for the most-similar prior error. Above the cosine
         threshold → return that diff with match_kind="semantic".

    Tier-A semantic match means a paraphrased error (different
    wording, same root cause) gets the same fix as if it were the
    exact signature.
    """
    try:
        return _node_authoring_service.remediation_lookup(
            db, error_signature=error_signature, error_text=error_text, auth=_auth,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def remediation_record(
    request: _RemediationRecordRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),  # global cache: valid build token only, no spec scope
):
    """Record a successful LLM remediation (or increment its
    success_count if the same (signature, diff) pair was already seen).

    Atomic on (error_signature, diff_hash) — the unique constraint
    in models.BuildRemediation prevents duplicate rows; on conflict we
    update success_count + last_used_at.
    """
    try:
        return _node_authoring_service.remediation_record(db, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def remediation_record_failure(
    request: _RemediationFailureRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),  # noqa: ARG001
):
    """Tier-B trust signal: a cached fix was tried (applied + retry, or
    failed-to-apply) and didn't resolve the build. Increment the
    entry's failure_count so the next lookup ranks healthier
    alternatives higher.

    Idempotent on (error_signature, diff_hash) like /record. No-op
    when the pair isn't in the table — a failure for a row we never
    recorded carries no learning signal. ``promoted`` rows are NOT
    auto-demoted by a single failure; only the trust ranking
    changes."""
    try:
        return _node_authoring_service.remediation_record_failure(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def remediation_record_failed_attempt(
    request: _RemediationFailedAttemptRequest,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(require_build_task_token),  # global cache: valid build token only, no spec scope
):
    """Negative cache: remember a fix that was tried for this error and
    failed, even if it was never a recorded success.

    Differs from ``/remediation/record-failure`` (which only bumps an
    existing row's failure_count and no-ops on a miss): this UPSERTS a
    row with ``success_count=0`` so the failed fix is persisted and
    surfaced in future ``known_bad`` lookups. Idempotent on
    (error_signature, diff_hash) — a repeat just bumps failure_count.

    A row that later turns out to work is recorded via /remediation/
    record, which bumps its success_count and lifts it out of
    known_bad automatically (known_bad filters success_count==0)."""
    try:
        return _node_authoring_service.remediation_record_failed_attempt(db, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

def federation_remediation_share(
    request: _FederationShareRequest,
    authorization: Optional[str] = Header(None),
    origin: Optional[str] = Header(None, alias="X-MOD-Federation-Origin"),
    db: Session = Depends(get_db),
):
    """Tier-E inbound: a peer deployment shares one of its
    remediation entries. We persist into ``federated_remediations``
    (separate from the local table) so foreign trust signals don't
    contaminate local accounting.

    Idempotent on (error_signature, diff_hash, source_peer): a
    re-share refreshes the counters + last_seen_at instead of
    inserting a duplicate.
    """
    try:
        return _node_authoring_service.federation_remediation_share(
            db, request=request, authorization=authorization, origin=origin,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def built_image_cache_lookup(
    source_hash: str,
    image_name: str,
    variant: str = "hero",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),  # noqa: ARG001
):
    """Foolproof #3 — has this exact (source bytes, image name,
    variant) been successfully built before? Return the digest so the
    imagebuilder can ``docker pull`` + retag + push instead of
    rebuilding from scratch.

    Lookup is on (source_hash, image_name, variant) so the same
    bytes legitimately rebuilt as two different nodes don't collide.
    """
    try:
        return _node_authoring_service.built_image_cache_lookup(
            db, source_hash=source_hash, image_name=image_name, variant=variant,
            current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def built_image_cache_record(
    request: _BuiltImageCacheRecordRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),  # noqa: ARG001
):
    """Record a fresh successful build so the next rebuild of the
    same source hits the fast path. Idempotent on the unique key
    (source_hash, image_name, variant) — re-recording bumps
    last_used_at + image_digest if it changed.
    """
    try:
        return _node_authoring_service.built_image_cache_record(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def build_task_status(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """γ — wizard polls this when it submitted a build with
    ``defer_registration=True``. Returns the task status and, when the
    imagebuilder finished, the captured sample output + node_config so
    the wizard can render the thumbs-up/down review.
    """
    try:
        return _node_authoring_service.build_task_status(db, task_id=task_id, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def finalize_registration(
    request: _FinalizeRegistrationRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """γ — final approval gate after the wizard runs the user's
    sample input through the freshly-built image.

    Build task ran with ``defer_registration=True``, did everything
    EXCEPT the register_node call, and emitted the captured sample
    output as a BUILD_RESULT_JSON log line. The wizard rendered that
    for the user; user reviewed; clicked thumbs-up or thumbs-down.
    Thumbs-up here pulls the deferred node_config out of the build's
    log_tail and registers it. Thumbs-down records the rejection (so
    we can learn from it via the existing thumbs-down feedback path)
    and leaves the image in Harbor without a NodeDefinition row.
    """
    try:
        return _node_authoring_service.finalize_registration(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def admin_remediation_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Operator dashboard data — cache hit rate, LLM call volume,
    promotion velocity, federation peer health, top promoted fixes.
    Platform admin only.

    Foolproof #4: without these numbers, you can't tell if the
    self-healing layers are actually saving wall-time / LLM calls
    or just adding latency.
    """
    try:
        return _node_authoring_service.admin_remediation_stats(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def feedback_node_broken(
    request: _NodeFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Foolproof #6 — user thumbs-down on a built node.

    A node can build successfully + register cleanly + still produce
    wrong output at RUNTIME. Today nothing learns from runtime
    failures — only build outcomes feed the trust signal. This
    endpoint accepts a "this node is broken" signal from the wizard
    UI and converts it into a failure_count bump on whichever
    remediation produced the underlying image.

    The link from a NodeDefinition back to its remediations isn't
    1:1 (multiple builds may have used cached / promoted fixes). For
    now, we record the negative signal against the most-recently-
    used remediation that matches the node's tenant context. More
    sophisticated provenance tracking is a future improvement; the
    core value here is closing the runtime feedback loop.
    """
    try:
        return _node_authoring_service.feedback_node_broken(db, request=request, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def admin_demote_remediation(
    remediation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Foolproof #9 — clear ``promoted_at`` on a specific remediation,
    typically when a previously-trusted fix turned out to be bad.
    Audit-logged. Idempotent — re-demoting a non-promoted entry is
    a no-op (returns ``demoted: false``)."""
    try:
        return _node_authoring_service.admin_demote_remediation(
            db, remediation_id=remediation_id, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def remediation_promoted(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),  # noqa: ARG001
):
    """List all auto-promoted remediations. Surfaces the catalog
    that's been distilled from real-world build outcomes — useful
    for dashboards, debugging, and tooling that wants to copy
    high-trust entries into the static known_failures.py.
    Ordered by promotion time (most recent first) so reviewers see
    fresh promotions at the top.
    """
    try:
        return _node_authoring_service.remediation_promoted(db, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def create_or_update_node_definition(
        node_def: schemas.NodeDefinitionCreate,
        response: Response,  # Dependency to control the response status
        db: Session = Depends(get_db),
        _auth: BuildAuthContext = Depends(require_user_or_build_token),
):
    """
    Create a new Node Definition or update an existing one based on the name.
    - If a definition with the same name exists for the tenant, it's updated (200 OK).
    - If not, a new definition is created (201 Created).

    For service accounts: tenant_id can be provided in payload (NULL for system nodes).
    For regular users: tenant_id is forced to current_user.tenant_id.

    Shared between normal users and the build-task register stage. When called
    with a scoped build-task token, it may only register the node for that
    token's own (tenant_id, build_spec_id) — enforced below.
    """
    try:
        return _node_authoring_service.create_or_update_node_definition(
            db, node_def=node_def, response=response, auth=_auth,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def _ce_palette_flags(db, ids):
    """CE-inclusion flags for the palette badge when services/ce_inclusion.py is
    absent (open-core/manifest.yaml:431 denies it on the CE carve).

    services/ce_inclusion.py is the ONE source of truth on the paid tree, so on
    Pro/SaaS this function is never reached (byte-identical behaviour there). On
    CE it replicates its two layers, nothing else:

    * the stored `node_definitions.ce_included` stamp — written CREATE-only by
      services/node_scanner.py:1165 (TRUE for a keep-listed node, NULL
      otherwise; migration 20261007zu_fx73_p4c_ce_included.py);
    * when the column is not applied yet, derive from the CE node-seed keep-list
      (open-core/node-seed-keeplist.yaml) by node name — the same list the
      scanner stamps from, and api_allow (manifest.yaml:81).

    Nodes with no stamp and no keep-list match are ABSENT (the badge renders
    nothing; never a guessed "CE"). Best-effort: any failure answers {}.
    """
    from sqlalchemy import inspect as sa_inspect
    from services import node_scanner

    try:
        bind = db.get_bind() if hasattr(db, "get_bind") else db
        columns = {c["name"] for c in sa_inspect(bind).get_columns("node_definitions")}
    except Exception:  # noqa: BLE001
        columns = set()

    try:
        if "ce_included" in columns:
            rows = db.query(
                models.NodeDefinition.id, models.NodeDefinition.ce_included
            ).filter(models.NodeDefinition.id.in_(ids)).all()
            stored = {int(row[0]): True for row in rows if row[1]}
            # A stored FALSE is NOT "not CE": on a fresh DB models.py:2726
            # server-defaults the column to false, so rows written by the
            # startup SEEDERS (seed_human_approval_node, the domain registry)
            # — which pre-exist the scanner's CREATE stamp — read FALSE.
            # UNION in the keep-list-by-name derivation for ids absent from the
            # truthy set, so the badge and the dedupe arm agree.
            names = node_scanner._load_ce_keeplist_names()
            if names:
                for row in db.query(
                    models.NodeDefinition.id, models.NodeDefinition.name
                ).filter(models.NodeDefinition.id.in_(ids)).all():
                    if int(row[0]) not in stored and row[1] in names:
                        stored[int(row[0])] = True
            return stored

        names = node_scanner._load_ce_keeplist_names()
        if not names:
            return {}
        rows = db.query(
            models.NodeDefinition.id, models.NodeDefinition.name
        ).filter(models.NodeDefinition.id.in_(ids)).all()
        return {int(row[0]): True for row in rows if row[1] in names}
    except Exception:  # noqa: BLE001
        logger.debug("ce palette badge fallback skipped", exc_info=True)
        return {}


def list_node_definitions_for_tenant(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
    limit: int = 100,
    offset: int = 0,
):
    """
    Retrieve a paginated list of Node Definitions available for the current user.
    Returns: system nodes + tenant-shared nodes + this user's private nodes.
    Service accounts (no user_id) see only system + tenant rows.

    Tenant admins (parent_user_id IS NULL) see all tenant-shared nodes
    regardless of team-grant restrictions; non-admin users only see
    tenant-shared rows that are either ungated or grant-visible to one
    of their teams (δ §17.1 filter in get_node_definitions).
    """
    # FX.110 (owner decision Q97=A) — outside collaborators: external
    # users see NO node palette. They only view resources explicitly
    # shared with them (their grants cover the shared cogs/workflows);
    # node authoring and tenant-wide node browsing are internal-only.
    if getattr(current_user, "is_external", False):
        return []
    user_id = getattr(current_user, "id", None)
    is_tenant_admin = getattr(current_user, "parent_user_id", None) is None
    node_defs = crud.get_node_definitions(
        db=db, tenant_id=current_user.tenant_id,
        user_id=user_id, limit=limit, skip=offset,
        is_tenant_admin=is_tenant_admin,
    )
    # FX.73 p3 — hide non-commercial-licence nodes from the palette on
    # paid/hosted tenants until a tenant admin accepts the non-commercial
    # terms (services/commercial_safe.py::hide_noncommercial_for). Unknown
    # licence stays visible — non_commercial_node_ids only returns ids
    # whose licence flatly classifies non_commercial.
    # 1784 CE carve: commercial_safe is paid; with it absent the FX.73 licence-hiding
    # step is a no-op, i.e. every node is allowed and the palette stays unfiltered.
    from utils.paid_import import optional_paid
    _csafe = optional_paid("services.commercial_safe")
    if _csafe is not None and _csafe.hide_noncommercial_for(db, current_user.tenant_id):
        _hidden_nc_ids = _csafe.non_commercial_node_ids(
            db, [nd.id for nd in node_defs])
        if _hidden_nc_ids:
            node_defs = [nd for nd in node_defs
                         if nd.id not in _hidden_nc_ids]
    # WB14-PRODFIX (RED :187) — read-time collapse of CE keep-list twins. The
    # scanner now reuses its own row instead of inserting one per pass, but a
    # room whose catalogue already holds duplicates (the pinned image's DB)
    # still reads 3x "Image Converter": crud.get_node_definitions' name-dedup
    # only hides LESS-specific twins, never same-scope rows. Collapse here, at
    # read time, ONLY when every row in the group is a scanner row (user_id
    # NULL) already stamped ce_included=True and they share name AND node_kind
    # (name alone is never the key) — keep the lowest id. A group with any
    # unstamped or user-authored row is left whole, so a user's same-name node
    # stays visible and no user data is ever hidden. Best-effort: a DB without
    # the ce_included column (migration 20261007zu not applied) skips this.
    try:
        _groups = {}
        for nd in node_defs:
            if getattr(nd, "user_id", None) is not None:
                continue
            if getattr(nd, "ce_included", None) is not True:
                continue
            _key = (getattr(nd, "name", None), getattr(nd, "node_kind", None))
            if not _key[0] or not _key[1]:
                continue
            _groups.setdefault(_key, []).append(nd)
        _drop_ids = set()
        for _rows in _groups.values():
            if len(_rows) < 2:
                continue
            _keep = sorted(_rows, key=lambda r: int(r.id))[0]
            for _r in _rows:
                if _r is not _keep:
                    _drop_ids.add(int(_r.id))
        if _drop_ids:
            node_defs = [nd for nd in node_defs if nd.id not in _drop_ids]
    except Exception:  # noqa: BLE001 — collapse is cosmetic; never break the palette
        logger.debug("ce keep-list read-time collapse skipped", exc_info=True)
    # node-version-pinning — attach each node's current version number (one
    # grouped query) so the builder can badge "vN" and detect updates. Transient
    # attribute; from_attributes serialization picks it up. Best-effort.
    ids = [nd.id for nd in node_defs]
    # node-version-pinning — attach each node's current version number (one
    # grouped query) so the builder can badge "vN" and detect updates. Transient
    # attribute; from_attributes serialization picks it up. Best-effort.
    # 10-31 rc20 fix: this enrichment used to share ONE try with the paid-only
    # palette badges below. services/licence_detect.py and services/ce_inclusion.py
    # are DENIED on CE (open-core/manifest.yaml:1135 / :431), so on the CE carve
    # their function-level imports raise ImportError INSIDE that try and the whole
    # block — including current_version/current_node_version_id — was skipped. The
    # canvas badge (process-node.tsx:368 `typeof data.currentNodeVersion === 'number'`)
    # then rendered nothing for ANY node on CE, while the single-node GET
    # (node_authoring.py:2695) still enriched. The CE-core facts (version,
    # ownership, lifecycle, egress — every module they need is api_allow:
    # crud/node_definition_cud.py:347, crud/node_ownership_cud.py:361,
    # services/node_egress_approval.py) now get their OWN try, so a missing paid
    # module can never blank them again.
    try:
        latest = crud.latest_versions_for_nodes(db, ids)
        # node-ownership-purchases — per-tenant ownership state for the store/palette.
        owned_src = crud.node_ownership_cud.entitlement_sources_for_nodes(db, current_user.tenant_id, ids)
        # FX.91 p2b — per-tenant egress-approval state for the palette badge
        # (node.egress_approval, read by node-library.tsx + node-config-panel
        # via the p2c GET endpoints). ONE batched query for the whole page
        # (services/node_egress_approval.palette_egress_states), never one
        # per node; nodes with no live approval row read 'required'/'not_required'
        # from their own declaration, so nothing is ever guessed.
        # Badge-only, so it gets its OWN nested guard: a missing table/module must
        # never blank the version/ownership facts above it.
        egress_states = {}
        try:
            from services.node_egress_approval import palette_egress_states
            egress_states = palette_egress_states(
                db, current_user.tenant_id, node_defs)
        except Exception:  # noqa: BLE001
            logger.debug("node palette egress enrichment skipped", exc_info=True)
        # NCA.4.2 — unsupported-kinds set (live system rows with
        # `custom_metadata.catalogue_status == 'unsupported'`)
        try:
            _unsupported_kinds_set = set()
            for r in db.query(
                models.NodeDefinition.node_kind
            ).filter(
                models.NodeDefinition.tenant_id.is_(None),
                models.NodeDefinition.deleted_at.is_(None),
                models.NodeDefinition.custom_metadata["catalogue_status"] == "unsupported",
            ):
                if r[0]:
                    _unsupported_kinds_set.add(r[0])
        except Exception:
            _unsupported_kinds_set = set()

        for nd in node_defs:
            cur = latest.get(nd.id)
            if cur:
                nd.current_version = cur["version"]
                nd.current_node_version_id = cur["id"]
            src = owned_src.get(nd.id)
            nd.owned = src is not None
            nd.ownership_source = src
            # FX.103 part 2 — deprecation badge inputs for the palette
            # (lifecycle_state / retire_at alias status / retire_after).
            nd.lifecycle_state = getattr(nd, "status", None) or "active"
            nd.retire_at = getattr(nd, "retire_after", None)
            # FX.91 p2b — this tenant's egress-approval state for the node
            # (declared domains + approved set + gov-block flag). Absent =>
            # the UI shows no approval claim (the node's own egress_domains
            # still drives the "No network"/"Network: N domains" half).
            nd.egress_approval = egress_states.get(nd.id)
            # NCA.4.2 — is_unsupported flag for the palette
            if nd.node_kind:
                nd.is_unsupported = nd.node_kind in _unsupported_kinds_set
    except Exception:  # noqa: BLE001
        logger.debug("node list enrichment skipped", exc_info=True)
    # FX.73 p4b/p4c — the PAID-only palette badges. Both modules are DENIED on
    # CE, so they go through the file's existing edition helper
    # (utils/paid_import.optional_paid, same shape as the commercial_safe gate
    # above at :2437): on CE the badge is simply absent (no badge, never a
    # guessed one), and the CE-core enrichment above is untouched.
    try:
        from utils.paid_import import optional_paid
        _licence_detect = optional_paid("services.licence_detect")
        _ce_inclusion = optional_paid("services.ce_inclusion")
        # FX.73 p4b — licence badges for the palette badge in node-library.tsx
        # (reads node.licence?.spdx / node.licence?.commercial_use). ONE query
        # for all returned ids; nodes without a live LicenceRecord are absent
        # (no badge, never a guessed one).
        badges = _licence_detect.licence_badges_for_nodes(db, ids) if _licence_detect else {}
        # FX.73 p4c — CE-inclusion flags for the palette CE badge
        # (node.ce_included, read by node-library.tsx). ONE query; nodes the CE
        # edition does not ship are absent (no badge, never a guessed one).
        # set_committed_value keeps the rows clean — the palette is a read path
        # and must not mark a node dirty for a later flush.
        from sqlalchemy.orm.attributes import set_committed_value
        from services import platform_settings as psr
        # Tunable (platform-settings registry, no env): OFF stops the badge
        # enrichment only — stored ce_included flags are untouched.
        # 10-31 rc20 fix (wb-ce-batch14 FAIL 1): on the CE carve _ce_inclusion is
        # None, which used to mean ce_flags={} and BLANK the stored ce_included
        # stamp (node_scanner.py:1165 stamps TRUE on CREATE for keep-listed nodes)
        # — so the palette row of a seeded node carried no flag and the WB
        # CeBadge (node-chips.tsx:278 `if (!ceIncluded) return null`) rendered
        # nothing. On CE, fall back to the STORED stamp / keep-list derivation
        # (_ce_palette_flags above — same two layers as services/ce_inclusion.py,
        # built only from api_allow modules: models.py, node_scanner.py +
        # node-seed-keeplist.yaml, manifest.yaml:81). On the paid tree
        # _ce_inclusion is present, so behaviour is byte-identical to before.
        ce_flags = {}
        if psr.get_setting("ce.palette_badge_enabled", db=db):
            ce_flags = (
                _ce_inclusion.ce_included_for_nodes(db, ids)
                if _ce_inclusion
                else _ce_palette_flags(db, ids)
            )
        for nd in node_defs:
            nd.licence = badges.get(nd.id)
            set_committed_value(nd, "ce_included", ce_flags.get(nd.id))
    except Exception:  # noqa: BLE001
        logger.debug("node badge enrichment skipped", exc_info=True)
    # GO.9 — honest per-accelerator labels for the palette. Best-effort and
    # isolated per-row so one malformed variant column degrades that node
    # only, never blanks the whole palette.
    from utils.queue_selector import compute_variant_status
    for nd in node_defs:
        try:
            nd.variant_status = compute_variant_status(nd) or None
        except Exception:  # noqa: BLE001
            logger.debug("variant_status enrichment skipped for node %s", getattr(nd, "id", "?"), exc_info=True)
    return node_defs

def patch_variant_status(
    payload: VariantStatusRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Best-effort update of a node's per-variant outcomes. Called by
    ImageBuilder after build-all-then-push-all completes (Phase 9b).

    Semantics:
      - `succeeded` variants STAY in .gpu_variants (if already present)
        and are REMOVED from .incompatible_reasons.
      - `failed` variants are REMOVED from .gpu_variants and ADDED to
        .incompatible_reasons with their reason string.

    Idempotent: re-PATCHing with the same payload converges to the
    same state.
    """
    try:
        return _node_authoring_service.patch_variant_status(db, payload=payload, current_user=current_user)
    except _ServiceError as exc:
        _raise_http(exc)

def list_curated_base_images(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Curated base images the wizard's source step offers in its
    dropdown. Every tenant node is built FROM one of these so Trivy
    scans are bounded to the user's delta rather than the whole image.

    Source of truth: `utils.curated_base_images.CURATED_BASE_IMAGES`.
    The nightly rebuild (`utils.base_image_rebuild`) rebuilds these
    against upstream tags so `apt-get upgrade` / `apk upgrade` CVE
    fixes land without the tenant doing anything.
    """
    from utils.curated_base_images import CURATED_BASE_IMAGES
    return [BaseImageCatalogEntry(**b.to_dict()) for b in CURATED_BASE_IMAGES]

def wizard_llm_status(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Is the wizard's LLM loaded and ready to serve a fast analysis,
    or is it still cold-loading from disk? Used by the wizard UI so the
    user sees an accurate status chip ("Ready" vs "Loading model…")
    instead of a generic spinner.

    Works against Ollama's `/api/ps` endpoint (returns currently-loaded
    models + VRAM footprint + keep-alive expiry). For non-Ollama
    providers we return loaded=True optimistically — OpenAI, Anthropic,
    etc. are always "loaded" server-side from the client's perspective.
    """
    # CE shim: delegates to the paid resolver on Pro; on CE it resolves BYO /
    # stack-slot wizard config and returns {} ("not configured") otherwise,
    # so this endpoint stays 200 on the CE build (GT.P2.6b, edge E28).
    from utils.tool_config import resolved_effective_env
    env = resolved_effective_env("wizard")
    provider = env.get("LLM_PROVIDER", "unknown")
    model = env.get("LLM_MODEL", "")
    base_url = (env.get("LLM_BASE_URL") or "").rstrip("/")

    if not model or not base_url:
        # CE (owner 11-10 item 46 + 11-11 follow-up): the CE unbound state must
        # TELL the user what is missing and how to fix it, in the real UI words
        # (Settings > AI Configuration tab, the slot's label is 'Node Wizard',
        # CORE/Dashboard/js/settings.js:1858). Paid keeps its original wording
        # unchanged. The leading phrase 'Wizard LLM not configured' stays so the
        # e2e specs keep matching (wizard-build-preflight.spec.ts:79,
        # wb-ce-batch15.spec.ts:100 -> /Wizard LLM not configured|Can.t reach LLM/).
        detail = "Wizard LLM not configured."
        from utils.paid_import import is_ce_edition
        if is_ce_edition():
            detail = (
                "Wizard LLM not configured. No model is bound to the Node "
                "Wizard yet. Open Settings > AI Configuration (the Node Wizard row in Easy view, or the Node Wizard tab in Advanced view), and choose a "
                "model for the Node Wizard (a model served by one of your "
                "own stacks, or your own OpenAI-compatible endpoint), then "
                "save."
            )
        return WizardLLMStatusResponse(
            provider=provider, model=model, base_url=base_url,
            loaded=False, detail=detail,
        )

    if provider != "ollama":
        if provider == "openai_compatible":
            # BYO / Persistent-Stack OpenAI-compatible endpoints are on THIS
            # box: a dead one must not light a green chip (the chat stream
            # already says the endpoint is unreachable). Any HTTP reply at all
            # (200/401/403/404...) proves the endpoint answers; only a
            # connection error / timeout is a lie. No auth header is sent, so
            # the exception text carries no secret. Public cloud providers
            # (openai/anthropic/azure...) stay optimistic: no cloud call.
            probe = f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
            try:
                import requests
                requests.get(probe, timeout=3)
            except Exception as e:
                # FIX-WIZARD-LLM-STATUS (11-12): the raw exception text must NOT
                # reach a tenant caller (it echoed the internal host/port - an
                # oracle for host scans). Generic words only; the real error
                # stays server-side. Prefix kept for the e2e regexes.
                logger.debug("wizard LLM probe failed (%s): %s", probe, e)
                return WizardLLMStatusResponse(
                    provider=provider, model=model, base_url=base_url,
                    loaded=False, detail="Can't reach LLM: the configured endpoint is unreachable.",
                )
            return WizardLLMStatusResponse(
                provider=provider, model=model, base_url=base_url,
                loaded=True, detail="Remote provider — reachable.",
            )
        return WizardLLMStatusResponse(
            provider=provider, model=model, base_url=base_url,
            loaded=True, detail="Remote provider — always available.",
        )

    try:
        import requests
        resp = requests.get(f"{base_url}/api/ps", timeout=5)
    except Exception as e:
        logger.debug("wizard LLM ollama probe failed: %s", e)
        return WizardLLMStatusResponse(
            provider=provider, model=model, base_url=base_url,
            loaded=False, detail="Can't reach LLM: the configured endpoint is unreachable.",
        )

    if not resp.ok:
        return WizardLLMStatusResponse(
            provider=provider, model=model, base_url=base_url,
            loaded=False, detail=f"LLM returned HTTP {resp.status_code}",
        )

    try:
        payload = resp.json()
    except ValueError:
        return WizardLLMStatusResponse(
            provider=provider, model=model, base_url=base_url,
            loaded=False, detail="LLM returned non-JSON status.",
        )

    for entry in payload.get("models") or []:
        if entry.get("name") == model or entry.get("model") == model:
            return WizardLLMStatusResponse(
                provider=provider, model=model, base_url=base_url,
                loaded=True,
                size_bytes=int(entry.get("size_vram") or entry.get("size") or 0),
                expires_at=entry.get("expires_at"),
                detail="Model resident in memory.",
            )

    return WizardLLMStatusResponse(
        provider=provider, model=model, base_url=base_url,
        loaded=False,
        detail=f"Model {model} not currently loaded — first analysis will cold-load it (can take 60-120s for a 14GB model).",
    )

def get_node_definitions_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Checks node definition refresh status.
    Returns completed since node scanning is synchronous.
    """
    return {
        "status": "completed",
        "node_kind": "node_scanner",
        "tenant_id": current_user.tenant_id,
        "message": "Node definitions are up to date",
    }

def worker_llm_ping(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Dispatch a trivial Containerizer task whose only job is to curl
    the configured LLM's /api/tags endpoint and emit a JSON diagnostic.

    Purpose: isolate *network* from *LLM behaviour* when debugging
    wizard failures. If a wizard Confirm hits "Temporary failure in
    name resolution", the user can hit this endpoint instead and get
    back "yes, the worker can reach Ollama" (meaning the real problem
    is LLM-side — model fetch, malformed JSON, etc.) vs. "no, DNS
    failed" (meaning the PLATFORM_TASK network routing isn't working
    and the task never should have been dispatched).

    Client polls /generation-artifacts/{task_id} for the result —
    reuses the existing albedo-artifact poll which also recognises
    the `llm_ping` JSON shape this task prints.
    """
    import os as _os
    env = {
        "OUTPUT_MODE": "llm-ping",
        "TENANT_ID": str(current_user.tenant_id) if current_user.tenant_id else "",
        **_build_llm_env("wizard"),
    }
    task_id = _enqueue_container_task(
        db, current_user.tenant_id, "containerizer:latest", env,
        "llm_ping",
    )
    return WorkerLLMPingResponse(
        task_id=task_id,
        expected_network=_os.getenv("DOCKER_NETWORK", "mod_modtex-network"),
        llm_base_url=env.get("LLM_BASE_URL", ""),
        message=(
            "Diagnostic ping dispatched. Poll /generation-artifacts/"
            f"{task_id} for the llm_ping result."
        ),
    )

def get_node_definition(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieve a specific Node Definition by its ID, ensuring it belongs to the user's tenant.
    """
    # The CRUD function now also filters by tenant_id for security
    db_node_def = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if db_node_def is None:
        raise HTTPException(status_code=404, detail="Node Definition not found")
    # node-version-pinning — attach current version so the builder's node panel
    # can refresh "update available" state from the live def.
    try:
        cur = crud.latest_versions_for_nodes(db, [db_node_def.id]).get(db_node_def.id)
        if cur:
            db_node_def.current_version = cur["version"]
            db_node_def.current_node_version_id = cur["id"]
    except Exception:  # noqa: BLE001
        logger.debug("current_version enrichment skipped (single get)", exc_info=True)
    # GO.9 — honest per-accelerator labels for the node detail panel.
    try:
        from utils.queue_selector import compute_variant_status
        db_node_def.variant_status = compute_variant_status(db_node_def) or None
    except Exception:  # noqa: BLE001
        logger.debug("variant_status enrichment skipped (single get)", exc_info=True)
    # FX.91 p2b — this tenant's egress-approval state so the node config
    # panel's "Network access" section reads the SAME state the palette
    # badge shows (one row lookup for one node; the panel already fetches
    # this def live).
    try:
        from services.node_egress_approval import palette_egress_states
        db_node_def.egress_approval = palette_egress_states(
            db, current_user.tenant_id, [db_node_def]
        ).get(db_node_def.id)
    except Exception:  # noqa: BLE001
        logger.debug("egress_approval enrichment skipped (single get)", exc_info=True)
    # NCA.4.2 — is_unsupported flag for the node detail panel
    try:
        ndk = db.query(models.NodeDefinition.node_kind).filter(
            models.NodeDefinition.tenant_id.is_(None),
            models.NodeDefinition.deleted_at.is_(None),
            models.NodeDefinition.custom_metadata["catalogue_status"] == "unsupported",
        )
        db_node_def.is_unsupported = db_node_def.node_kind in {r[0] for r in ndk}
    except Exception:
        db_node_def.is_unsupported = None
    return db_node_def

def get_scan_queue_context(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Queue context for a node that's currently scan_status=pending_scan
    or scanning. Powers the "Your position #3 in line" display in the
    Scan Details popup so users aren't staring at an empty 'Waiting' panel.

    Returns:
      - in_flight: total trivy_scan tasks currently RUNNING/CLAIMED across the worker pool
      - pending:   total trivy_scan tasks PENDING in queue
      - this_node_position: 1-based queue position of THIS node's most-recent
                            pending task, or null if no pending task exists for it
      - last_attempt: shape {task_id, status, completed_at, error_message}
                      from the most-recent FAILED/COMPLETED scan task for this node,
                      or null if it's never been scanned
    """
    try:
        return _node_authoring_service.get_scan_queue_context(
            db, node_definition_id=node_definition_id, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def get_node_manifest(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Return the signed manifest JSON for a node, fetched from MinIO.

    Access: own-tenant, platform admin, or api_service. System nodes
    (tenant_id=None) are readable by any authenticated user."""
    nd = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if not nd:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if not nd.manifest_ref:
        raise HTTPException(
            status_code=404,
            detail="No manifest recorded for this node — likely pre-dates the manifest pipeline",
        )
    try:
        from utils.manifest import manifest_bucket
        from minio_client import get_minio_client  # existing helper
        client = get_minio_client()
        bucket = manifest_bucket(nd.tenant_id, db)
        obj = client.get_object(bucket, nd.manifest_ref)
        data = obj.read()
        return json.loads(data)
    except Exception as e:
        logger.exception("manifest fetch failed")
        raise internal_http_error(500, "Manifest fetch failed", e) from e

def _build_or_bearer_auth(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> BuildAuthContext:
    """Auth for the imagebuilder manifest-metadata callback.

    The ImageBuilder sends its per-task scoped build token in the
    Authorization header as ``Bearer <token>`` (manifest_emit.py), while the
    register call uses ``X-API-Key: <token>``. The standard
    ``require_user_or_build_token`` only tries build-token / master-key on the
    X-API-Key header, so the Bearer form 401'd and every build's digest was
    silently dropped. Try build-token / master-key on BOTH header forms, then
    fall through to ordinary user auth (Keycloak bearer / api-key) exactly as
    before."""
    from dependencies import _try_build_token, _try_master_key
    ctx = _try_build_token(x_api_key, db) or _try_master_key(x_api_key, db)
    if ctx is not None:
        return ctx
    if authorization and authorization.startswith("Bearer "):
        bearer_token = authorization.split(" ", 1)[1]
        ctx = _try_build_token(bearer_token, db) or _try_master_key(bearer_token, db)
        if ctx is not None:
            return ctx
    user = require_bearer_or_api_key(authorization=authorization, x_api_key=x_api_key, db=db)
    return BuildAuthContext(user=user, via="user")


def set_node_manifest_metadata(
    node_definition_id: int,
    body: ManifestMetadata,
    db: Session = Depends(get_db),
    _auth: BuildAuthContext = Depends(_build_or_bearer_auth),
):
    """Set manifest_ref + image_digest on a registered node. Called by
    imagebuilder after it uploads the signed manifest to MinIO. Service
    accounts (api_service), platform admins, and the build-task itself
    (scoped build token — the ImageBuilder authenticates with the SAME
    credential it used to register the node, which resolves to the platform
    service user) may write this field; tenants don't write it directly;
    it's an attestation the imagebuilder produces on their behalf.

    ``_auth`` is a BuildAuthContext when the request comes through FastAPI
    (via _build_or_bearer_auth) but a bare User when a test invokes this
    function directly — normalize both."""
    user = getattr(_auth, "user", None) or _auth
    try:
        return _node_authoring_service.set_node_manifest_metadata(
            db, node_definition_id=node_definition_id, body=body,
            current_user=user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def verify_node_manifest(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Re-fetch the manifest and verify its HMAC signature. Returns
    {ok: bool, reason: str, manifest_digest: str, image_digest: str}.
    ok=false on any tampering/key-mismatch/retag — callers should treat
    that as a compliance incident and alert the owner."""
    nd = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if not nd:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if not nd.manifest_ref:
        return {"ok": False, "reason": "no manifest recorded", "manifest_digest": None,
                "image_digest": nd.image_digest}
    try:
        from utils.manifest import manifest_bucket, verify_manifest_signature
        from utils.minio_client import get_minio_client
        client = get_minio_client()
        bucket = manifest_bucket(nd.tenant_id, db)
        raw = client.get_object(bucket, nd.manifest_ref).read()
        doc = json.loads(raw)
        ok, reason = verify_manifest_signature(doc)
        image_digest_in_manifest = (doc.get("image") or {}).get("digest")
        digest_drift = (
            nd.image_digest
            and image_digest_in_manifest
            and nd.image_digest != image_digest_in_manifest
        )
        if digest_drift:
            return {
                "ok": False,
                "reason": (
                    f"image digest drift: db={nd.image_digest} vs "
                    f"manifest={image_digest_in_manifest}"
                ),
                "manifest_digest": image_digest_in_manifest,
                "image_digest": nd.image_digest,
            }
        return {
            "ok": bool(ok),
            "reason": reason,
            "manifest_digest": image_digest_in_manifest,
            "image_digest": nd.image_digest,
        }
    except Exception as e:
        logger.exception("manifest verify failed")
        raise internal_http_error(500, "Manifest verify failed", e) from e

def rescan_node_definition(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Trigger a fresh Trivy security scan for a node definition.
    Used by the workflow builder's "Scan Now" button so operators can
    skip the wait when scans are backed up or a node is stuck in
    pending_scan after a dispatcher hiccup (e.g. the trivyscanner
    image being missing from Harbor earlier today).

    Auth: platform admins can rescan anything; other users can only
    rescan nodes in their own tenant (including NULL-tenant system
    nodes, since those are shared and rescanning is a low-risk op).
    """
    try:
        return _node_authoring_service.rescan_node_definition(
            db, node_definition_id=node_definition_id, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def set_node_role(
    node_definition_id: int,
    body: _NodeRoleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """node-role §1 — the one-flip promote/demote. Set a node's authoritative
    role: 'user' (in the palette + wizard) or 'system' (orchestrator-only,
    hidden from users). Platform-admin only, since system-shared node
    classification is a platform decision. `system_only` is synced automatically
    by the model event."""
    try:
        return _node_authoring_service.set_node_role(
            db, node_definition_id=node_definition_id, body=body, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

def update_node_definition(
    node_definition_id: int,
    node_def_update: schemas.NodeDefinitionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Update a Node Definition, ensuring the user has permission (is in the same tenant).
    """
    # First, fetch the definition securely to ensure it belongs to the user's tenant
    db_node_def = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if db_node_def is None:
        raise HTTPException(status_code=404, detail="Node Definition not found")

    # R-048 (GSEC.2) — mass-assignment guard: node_role / system_only are
    # platform-only fields (system-shared node classification is a platform
    # decision — the dedicated PATCH /{id}/role endpoint enforces the same
    # is_platform_admin gate in set_node_role). A non-platform-admin tenant
    # user must not be able to flip them through the generic node PATCH;
    # reject with 403 rather than silently dropping the field.
    _privileged_fields = {"node_role", "system_only"}
    if (
        _privileged_fields & node_def_update.model_fields_set
        and not getattr(current_user, "is_platform_admin", False)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a platform admin can update the privileged fields "
                   "node_role/system_only.",
        )

    # FX.103 (owner Q89=A) — deprecation lifecycle fields ride the same PATCH
    # but go through the validated transition (services/node_deprecation),
    # not the generic setattr: deprecated/retired requires deprecated_at and
    # retire_after must be >= deprecated_at + deprecation.min_runnable_months
    # (>= 12 months after deprecation). The generic update below strips them
    # out of its payload; the transition is applied to the SAME row first.
    _lifecycle_fields = {"status", "deprecated_at", "retire_after"}
    _lifecycle = {k: v for k, v in node_def_update.model_dump().items()
                  if k in _lifecycle_fields and k in node_def_update.model_fields_set}
    for _k in _lifecycle_fields:
        node_def_update.model_fields_set.discard(_k)
    # 1788i CE carve: node_deprecation is paid, so the deprecation lifecycle is
    # OFF in CE — the validated transition is skipped (nothing is ever
    # deprecated/retired here), and the lifecycle fields stay out of the
    # generic setattr below, so the PATCH is a no-op for them rather than a 500.
    try:
        from services import node_deprecation as _dep
    except ImportError:
        _dep = None
    if _lifecycle and _dep is None:
        _lifecycle = {}
    if _lifecycle:
        _target_status = _lifecycle.get("status")
        if _target_status is None:
            # dates without an explicit status: setting them on an active
            # node deprecates it; otherwise keep the current status.
            _target_status = (_dep.NODE_STATUS_DEPRECATED
                              if db_node_def.status == _dep.NODE_STATUS_ACTIVE
                              else db_node_def.status)
        try:
            _dep.apply_node_status(
                db, db_node_def, status=_target_status,
                deprecated_at=_lifecycle.get("deprecated_at"),
                retire_after=_lifecycle.get("retire_after"),
                actor_user=current_user,
            )
            db.commit()
            db.refresh(db_node_def)
        except _dep.DeprecationError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=exc.message)

    # Canonicalize the `type` field against existing tenant categories so
    # "BEST" / "best" / "Best" collapse onto the canonical existing form
    # (manual-test fix 2026-05-22). Empty / None left as-is so callers
    # can clear the field explicitly.
    if node_def_update.type and node_def_update.type.strip():
        from node_builder.category_suggester import (
            _list_existing_categories,
            canonicalize_against_existing,
        )
        existing = _list_existing_categories(db, current_user.tenant_id)
        node_def_update.type = canonicalize_against_existing(
            node_def_update.type, existing,
        )

    # GB.TIER.0.4 (a): an operator-set price is basis='manual' and re-derives
    # the tiers. Pre-migration DBs lack the tier columns, so there the field
    # stays in the update model (plain cost_in_credits assignment).
    if (
        "cost_in_credits" in node_def_update.model_fields_set
        and node_def_update.cost_in_credits is not None
        and _node_cost.tier_columns_available(db)
    ):
        _node_cost.apply_node_cost(
            db_node_def, node_def_update.cost_in_credits, basis="manual",
        )
        node_def_update.model_fields_set.discard("cost_in_credits")

    try:
        updated_node_def = crud.update_node_definition(
            db=db, db_node_def=db_node_def, node_def_update=node_def_update
        )
        return updated_node_def
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Failed to update node definition: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update node definition",
        )

def delete_node_definition(
    node_definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Soft-delete a Node Definition.

    Wizard-rebuild §11b — stamps ``deleted_at`` + ``deleted_by_user_id``
    so the row remains in the audit chain for the tenant's retention
    period (default 90 days, configurable 30–2555 per
    ``tenant.retention_days``). The daily ``hard_delete_after_retention``
    APScheduler job reclaims Harbor + DB once retention has elapsed.

    The tenant's ``storage_used_bytes`` counter is decremented
    immediately so the quota bar reflects the user's intent (Harbor
    bytes are reclaimed at GC time; quota drift during retention is
    accepted — see §11b decision log).
    """
    try:
        return _node_authoring_service.delete_node_definition(
            db, node_definition_id=node_definition_id, request=request, current_user=current_user,
        )
    except _ServiceError as exc:
        _raise_http(exc)

class _RecomputeCostRequest(PydanticBaseModel):
    force: bool = False


@router.post("/{node_definition_id}/recompute-cost")
def recompute_cost(
    node_definition_id: int,
    request: Optional[_RecomputeCostRequest] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """tiered-credit-pricing 7.1 — manual cost recompute (platform admin only).

    Recomputes the node's credit cost with the EXISTING auto logic
    (``scheduler.pattern_updater._auto_calculate_credit_cost`` →
    ``utils.node_cost.apply_node_cost``), so the tier values follow the
    recomputed medium cost and ``cost_basis`` lands on 'calibrated'.

    Freshness rule (no ``force``): the call is a no-op when the current cost
    is already authoritative — either the manual-basis lock (``cost_basis``
    'manual', the one 7.1 names) or a completed calibration
    (``calibration_status == 'calibrated'``, i.e. the cost already IS the
    calibration-derived number). ``force=true`` overrides both and recomputes
    for this one call. ``cost_basis == 'free'`` is never auto-overwritten
    without force either (mirrors the auto path's own guard).

    Tenant-scoped lookup: system rows + this tenant's rows + the caller's own
    private rows; soft-deleted rows are 404 (ISO 27001: nothing hard-deleted,
    the row just stops being addressable). Audit-logged either way (skipped or
    written) with before/after costs + per-tier maps.
    """
    _require_platform_admin(current_user)

    db_node_def = crud.get_node_definition_by_id(
        db=db,
        node_definition_id=node_definition_id,
        tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
    )
    if db_node_def is None or getattr(db_node_def, "deleted_at", None) is not None:
        raise HTTPException(status_code=404, detail="Node Definition not found")

    force = bool(getattr(request, "force", False)) if request is not None else False

    # The tier columns are deferred ORM columns that may be missing from a
    # live DB pre-migration — reading them there emits SQL naming a missing
    # column. Gate every tier read on the same probe the auto path uses.
    _tiers_ok = _node_cost.tier_columns_available(db)

    def _read_tiers(nd):
        if not _tiers_ok:
            return None
        return dict(getattr(nd, "cost_credits_by_tier", None) or {})

    before_cost = db_node_def.cost_in_credits
    before_tiers = _read_tiers(db_node_def)
    before_basis = getattr(db_node_def, "cost_basis", None) if _tiers_ok else None
    before_cal = getattr(db_node_def, "calibration_status", None)

    _locked = (
        before_basis in ("manual", "free")
        or before_cal == "calibrated"
    )
    skipped = _locked and not force

    after_cost, after_tiers, after_basis = before_cost, before_tiers, before_basis
    if not skipped:
        from scheduler.pattern_updater import _auto_calculate_credit_cost
        _auto_calculate_credit_cost(db_node_def, db)
        db.commit()
        db.refresh(db_node_def)
        after_cost = db_node_def.cost_in_credits
        after_tiers = _read_tiers(db_node_def)
        after_basis = getattr(db_node_def, "cost_basis", None) if _tiers_ok else None

    db.add(models.AuditEvent(
        tenant_id=current_user.tenant_id,
        user_id=getattr(current_user, "id", None),
        event_name="NODE_COST_RECOMPUTE",
        details={
            "node_definition_id": node_definition_id,
            "force": force,
            "skipped": skipped,
            "cost_before": before_cost,
            "cost_after": after_cost,
            "cost_credits_by_tier_before": before_tiers,
            "cost_credits_by_tier_after": after_tiers,
            "cost_basis_before": before_basis,
            "cost_basis_after": after_basis,
            "calibration_status_before": before_cal,
            "actor_user_id": getattr(current_user, "id", None),
            "actor_is_platform_admin": bool(
                getattr(current_user, "is_platform_admin", False)
            ),
        },
    ))
    db.commit()

    return {
        "node_definition_id": node_definition_id,
        "skipped": skipped,
        "force": force,
        "cost_before": before_cost,
        "cost_after": after_cost,
        "cost_credits_by_tier_before": before_tiers,
        "cost_credits_by_tier_after": after_tiers,
        "cost_basis_before": before_basis,
        "cost_basis_after": after_basis,
        "calibration_status": before_cal,
    }


def calibrate_node(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Dispatch calibration tasks (9 runs: 3 per size tier) for a node definition.
    Requires ML scheduling feature (Pro+ SaaS or licensed on-prem).
    """
    node_def = crud.get_node_definition_by_id(db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id, user_id=getattr(current_user, 'id', None))
    if not node_def:
        raise HTTPException(status_code=404, detail="Node Definition not found")

    if node_def.calibration_status == "calibrating":
        raise HTTPException(status_code=409, detail="Calibration already in progress")

    total = len(SIZE_TIERS) * CALIBRATION_RUNS_PER_TIER
    node_def.calibration_status = "calibrating"
    node_def.calibration_progress = {"completed": 0, "total": total}
    db.commit()

    task_ids = _dispatch_calibration_tasks(db, node_def, current_user.tenant_id)

    return {
        "status": "calibrating",
        "node_definition_id": node_definition_id,
        "tasks_dispatched": len(task_ids),
        "total_runs": total,
    }

def build_variant(
    node_definition_id: int,
    hardware_tag: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Dispatch a gpu-variant-builder task to create a container variant for a specific hardware tag."""
    node_def = crud.get_node_definition_by_id(db=db, node_definition_id=node_definition_id, tenant_id=current_user.tenant_id, user_id=getattr(current_user, 'id', None))
    if not node_def:
        raise HTTPException(status_code=404, detail="Node Definition not found")

    if not node_def.executable_path:
        raise HTTPException(status_code=400, detail="Node has no container image to build from")

    existing_variants = node_def.gpu_variants or {}
    if hardware_tag in existing_variants:
        raise HTTPException(status_code=409, detail=f"Variant for '{hardware_tag}' already exists")

    import os
    harbor_url = os.getenv("HARBOR_URL", "")
    api_url = os.getenv("API_INTERNAL_URL", "http://api:80")
    builder_image = os.getenv(
        "VARIANT_BUILDER_IMAGE",
        platform_image("gpu-variant-builder:latest")
    )

    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"build-variant-{node_def.id}-{hardware_tag}",
        tenant_id=current_user.tenant_id,
        queue_name="CPU",
        priority=3,
        task_payload={
            "node_kind": "gpu_variant_build",
            "processing_image": builder_image,
            "node_definition_id": node_def.id,
            "hardware_tag": hardware_tag,
            "gpu_required": False,
            "container_env": {
                "SOURCE_IMAGE": node_def.executable_path,
                "TARGET_BACKEND": hardware_tag,
                "HARBOR_URL": harbor_url,
                "API_URL": api_url,
                "NODE_DEF_ID": str(node_def.id),
            },
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace=f"variant_build_{hardware_tag}",
    )
    db.add(task)
    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    return {
        "status": "building",
        "node_definition_id": node_definition_id,
        "hardware_tag": hardware_tag,
        "task_id": task.id,
    }

def _heal_tenant_copies_of_system_nodes(db) -> int:
    """NULL-only fill of tenant clones of system nodes (Q-CR14-TENANT-CLONE-HEAL).

    A tenant copy of a system node (tenant_cud.py provisioning, node_code_edit.py
    fork) can carry NULLs the system row already has (node_kind / executable_path
    on the cr14 room's rows 351/416), and the palette view then fails the CE
    node_kind/executable_path check. This pass walks ONLY rows with a resolvable
    system source — ``forked_from_node_definition_id`` pointing at a
    ``tenant_id IS NULL`` row, else ``source_origin == "forked"`` matched by name
    to the ONE live system row of that name — and copies a RECONCILED_FIELDS value
    from that system row when (and only when) the tenant field is NULL/empty, so a
    tenant row can never be written from another tenant's row. Non-NULL
    tenant values are never overwritten; user-authored rows (node_definition_cud
    shape, no system source) are never touched; a row whose source is unresolvable
    (deleted system row, FK to a non-system row, name collision) is skipped.
    Returns the number of tenant rows written.
    """
    from node_definitions_domains.image_ops import RECONCILED_FIELDS

    # The global soft-delete loader filter (database.py:453) already hides
    # tombstoned rows on a plain query, so this walk is live rows only.
    rows = db.query(models.NodeDefinition).all()
    system_by_id = {r.id: r for r in rows if r.tenant_id is None}

    healed = 0
    for row in rows:
        if row.tenant_id is None:
            continue
        source = None
        if row.forked_from_node_definition_id is not None:
            source = system_by_id.get(row.forked_from_node_definition_id)
            if source is None:
                logging.warning(
                    "refresh heal skipped: tenant node %s FK %s is not a live system row",
                    row.id, row.forked_from_node_definition_id,
                )
                continue
        elif row.source_origin == "forked":
            twins = [s for s in system_by_id.values() if s.name == row.name]
            if len(twins) != 1:
                logging.warning(
                    "refresh heal skipped: tenant node %s has no unique system "
                    "name twin (%s)",
                    row.id, row.name,
                )
                continue
            source = twins[0]
        if source is None:
            continue
        filled = []
        for field in RECONCILED_FIELDS:
            if getattr(row, field, None) in (None, "", {}, []):
                value = getattr(source, field, None)
                if value not in (None, "", {}, []):
                    setattr(row, field, value)
                    filled.append(field)
        if filled:
            healed += 1
            logging.info(
                "refresh heal filled %s on tenant node %s from system node %s",
                ",".join(filled), row.id, source.id,
            )
    if healed:
        db.commit()
    return healed


def reseed_seeded_nodes(db, keeplist_names=None) -> dict:
    """Re-run the four startup seeders in boot order (idempotent check-then-insert).

    Repairs seeder-made rows (converters, domain nodes, human approval, text llm)
    that the node scan cannot recreate. A failing step never blocks the next one.
    """
    from services import node_scanner

    if keeplist_names is None and node_scanner._is_ce_edition():
        keeplist_names = node_scanner._load_ce_keeplist_names()

    results: dict = {}

    def _converters():
        from seed_converters import seed_converter_nodes
        return seed_converter_nodes(db)

    def _domain_nodes():
        from node_definitions_domains._registry import register_all_domain_nodes
        out = register_all_domain_nodes(db, keeplist_names=keeplist_names)
        db.commit()
        return len(out)

    def _human_approval():
        from seed_human_approval_node import seed_human_approval_node
        return seed_human_approval_node(db)

    def _text_llm():
        from seed_text_llm_node import seed_text_llm_node
        return seed_text_llm_node(db)

    for step_name, step in (
        ("seed_converters", _converters),
        ("seed_domain_nodes", _domain_nodes),
        ("seed_human_approval_node", _human_approval),
        ("seed_text_llm_node", _text_llm),
    ):
        try:
            results[step_name] = step()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logging.warning("refresh reseed step %s failed: %s", step_name, e.__class__.__name__)
            results[step_name] = f"error: {e.__class__.__name__}"

    # Q-CR14-TENANT-CLONE-HEAL (owner: "A now AND C") — after the four seed
    # steps, NULL-only heal of tenant clones of system nodes. Same fail-closed
    # shape as the steps above: a raising heal never aborts the reseed, and the
    # returned dict shape is unchanged (no new keys).
    try:
        _heal_tenant_copies_of_system_nodes(db)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logging.warning("refresh tenant-clone heal failed: %s", e.__class__.__name__)
    return results


def _refresh_worker(session_factory) -> None:
    """Thread target for POST /nodes/refresh: re-seed seeder-made nodes, then scan."""
    try:
        db = session_factory()
        try:
            reseed_seeded_nodes(db)
        finally:
            db.close()
    except Exception as e:
        logging.warning("refresh reseed failed: %s", e.__class__.__name__)
    from services.node_scanner import scan_and_register_nodes
    scan_and_register_nodes(session_factory, force_digest_sweep=True)


def refresh_node_definitions(
    # This dependency protects the endpoint and provides the user object
    current_user: models.User = Depends(require_active_and_onboarded_user),
    # ✅ We need to capture the raw Authorization header to get the token
    authorization: str = Header(...),
):
    """
    Triggers a background process to refresh the Node Definitions for the user's tenant.
    """
    try:
        # Extract the token from the 'Bearer <token>' format
        bearer_token = authorization.split(" ")[1]
    except IndexError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format."
        )

    node_kind_to_trigger = "Node_Dag_Gen_Dag"

    logging.info(f"Triggering DAG '{node_kind_to_trigger}' for tenant_id: {current_user.tenant.id}")

    try:
        from database import SessionLocal
        import threading
        threading.Thread(
            target=_refresh_worker,
            args=(SessionLocal,),
            daemon=True,
        ).start()
        return {
            "detail": "Node definition refresh process has been successfully initiated."
        }
    except Exception as e:
        logging.warning(f"Node scan failed (non-critical): {e}")
        return {
            "detail": "Node refresh request received but scan encountered an error.",
            "warning": str(e)
        }

def check_image_ref_endpoint(
    request: CheckImageRefRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Verify a Docker image reference resolves against its public
    registry. Returns one of three outcomes:

      - exists=True: registry confirmed the namespace/repo (we don't
        always validate the tag — but the build will).
      - exists=False: registry returned 404 OR the host isn't a Docker
        registry at all (HuggingFace model paths land here). Response
        includes a `reason` string and, optionally, LLM-suggested real
        images that match the user's intent.
      - exists=None: couldn't verify (private image, unsupported
        registry, network blip). Caller MAY proceed; the build will
        catch a bad ref at FROM resolution.

    Per Tim 2026-05-21: typo-protect Image mode so the LLM doesn't
    thrash on a bogus FROM line for 14 minutes.
    """
    from utils.image_ref_preflight import check_image_ref, suggest_alternatives_llm
    from utils import wizard_metrics
    result = check_image_ref(request.image_ref)

    # Telemetry: §0c — track preflight outcomes so the dashboard
    # can break down where tenants are typing bad refs.
    _preflight_result = (
        "exists" if result.exists is True else
        "not-a-registry" if "model hub" in result.reason.lower()
                            or "not a docker registry" in result.reason.lower() else
        "wrong-mediatype" if "mediatype" in result.reason.lower()
                              or "model artifact" in result.reason.lower() else
        "auth-required" if "private" in result.reason.lower()
                            or "401" in result.reason else
        "malformed" if "malformed" in result.reason.lower()
                        or "empty" in result.reason.lower() else
        "not-found" if result.exists is False else
        "unknown"
    )
    _registry_host = result.parsed.registry_host if result.parsed else "unknown"
    wizard_metrics.record_image_preflight(
        result=_preflight_result, registry=_registry_host,
    )

    suggestions: List[Dict[str, Any]] = []
    if result.exists is False and not request.skip_suggestions:
        try:
            suggestions = suggest_alternatives_llm(
                ref=request.image_ref,
                node_name=request.node_name or "",
                input_type=request.input_type or "image",
                output_type=request.output_type or "image",
                description=request.description or "",
            )
            if suggestions:
                wizard_metrics.record_llm_call(purpose="suggest")
        except Exception as e:
            logger.warning("check-image-ref: suggester crashed: %s", e)
    return CheckImageRefResponse(
        exists=result.exists,
        reason=result.reason,
        registry_source=result.registry_source,
        suggestions=suggestions,
    )

def generate_dockerfile(
    request: ContainerAdaptRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """
    Step 1: Analyze a Docker image and generate a MOD_CORE-compatible wrapper.

    Dispatches the ContainerAdapter container which uses an LLM to analyze
    the source image and generate a Dockerfile + entrypoint that conforms
    to /data/input/ -> /data/output/ convention.

    Output files can be reviewed and edited before building.
    """
    import os
    if not request.source_image:
        raise HTTPException(status_code=400, detail="source_image is required")

    # Git-mode admission: when source_image looks like a git URL, check
    # the host allowlist. When it looks like a docker image ref (the
    # adapter also handles image→dockerfile for existing containers),
    # defer admission until the LLM-generated Dockerfile comes back
    # and routes through /build-and-register-local.
    if request.source_image.startswith(("http://", "https://", "git@")):
        _run_admission("repo", {"repo_url": request.source_image}, current_user)

    _adapter_slot_guard(db, current_user.tenant_id)

    env = {
        "SOURCE_IMAGE": request.source_image,
        "NODE_NAME": request.node_name,
        "INPUT_TYPE": request.input_type,
        "OUTPUT_TYPE": request.output_type,
        "PROCESS_TYPE": request.process_type,
        "COST_IN_CREDITS": str(request.cost_in_credits),
        "TENANT_ID": str(current_user.tenant_id) if current_user.tenant_id else "",
        **_build_llm_env("adapter"),
    }
    if request.additional_instructions:
        env["ADDITIONAL_INSTRUCTIONS"] = request.additional_instructions

    task_id = _enqueue_container_task(
        db, current_user.tenant_id, "containeradapter:latest", env,
        "container_adapter",
    )

    # Surface this dispatch in the Wizard Queue (observational only —
    # never let surfacing break the dispatch). Linked via
    # target_ref_type="task"/target_ref_id since a container-adapter
    # dispatch has no workflow_run_id to bridge on.
    try:
        from services import wizard_queue_emitters as _wqe
        _wqe.emit_container_task_run(
            db,
            wizard_type=models.WizardRunType.CONTAINER_ADAPTER,
            task_id=task_id,
            target_name=request.node_name or request.source_image,
            creator=current_user,
            tenant_id=current_user.tenant_id,
        )
    except Exception as _e:  # noqa: BLE001
        logger.debug("wizard-queue emit (container-adapter) failed: %s", _e)

    return {"task_id": task_id, "step": "generate-dockerfile", "status": "pending"}

def build_and_register(
    request: BuildRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """
    Step 3: Build a Docker image, push to Harbor, and register as a workflow node.

    Expects the Dockerfile to already exist (from step 1 or manually written).
    The build context must be available to the worker.
    """
    import os
    from utils.registries import platform_registry

    registry = platform_registry()
    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")

    env = {
        "IMAGE_NAME": request.image_name,
        "HARBOR_REGISTRY": registry,
        "HARBOR_USERNAME": os.getenv("HARBOR_USERNAME", ""),
        "HARBOR_PASSWORD": os.getenv("HARBOR_PASSWORD", ""),
    }
    if request.node_name:
        env.update({
            "NODE_NAME": request.node_name,
            "API_URL": os.getenv("API_BASE_URL", "http://api:80"),
            "API_KEY": service_api_key,
            "INPUT_TYPE": request.input_type,
            "OUTPUT_TYPE": request.output_type,
            "PROCESS_TYPE": request.process_type,
            "COST_IN_CREDITS": str(request.cost_in_credits),
            "TENANT_ID": str(current_user.tenant_id) if current_user.tenant_id else "",
        })
        if request.description:
            env["DESCRIPTION"] = request.description

    # Charge 100 credits at imagebuilder dispatch — the single billable
    # moment for a Dockerfile/Git/Script-mode node creation. Free LLM
    # analysis steps (/generate-dockerfile, /from-container) are upstream;
    # the internal service-account registration at /node_definitions/
    # downstream is exempt. 402 raised here pre-empts task enqueue so a
    # user who can't afford it isn't charged.
    # PAID (CE-launch Gate 4): CE ships no billing module for this call —
    # node creation proceeds unmetered when `_budget_paid` is absent.
    if _budget_paid is not None:
        _budget_paid.charge_node_creation(db, current_user, request.node_name or request.image_name)

    task_id = _enqueue_container_task(
        db, current_user.tenant_id, "imagebuilder:latest", env, "image_builder"
    )
    return {"task_id": task_id, "step": "build-and-register", "status": "pending"}

__all__ = [
    'get_build_spec',
    'get_build_spec_context',
    'build_image_from_context',
    'post_stage_result',
    'sync_variants_from_hero',
    'variant_sample_test_result',
    'variant_verified',
    'build_spec_eta',
    'enqueue_variant_test_pending',
    'variant_test_pending_status',
    'variant_change_gate_weekly_report',
    '_ChangeGateWeeklyReportResponse',
    'describe_cascade',
    'retry_with_elevated_budget',
    'finalize_partial_publish',
    'submit_wizard_support_ticket',
    'finalize_variant_verification',
    'get_variant_verification',
    'rule_on_drifted_variant',
    'patch_reference_variant',
    'suggest_node_category',
    'extract_repo_docs',
    'marketplace_toggle',
    'admin_curate_list',
    'tenant_curate_list',
    'tenant_add_to_marketplace',
    'tenant_marketplace_toggle',
    'tenant_team_grant',
    'tenant_team_revoke',
    'set_node_price',
    'admin_list_system_nodes',
    'admin_export_paid_node_listings',
    'public_node_detail',
    'set_node_sample',
    'mark_marketplace_seen',
    'marketplace_new_count',
    'try_node',
    'list_node_packages',
    'buy_node_package',
    'create_node_package',
    'add_node_package_item',
    'bind_package_to_plan',
    'marketplace_list',
    'marketplace_download',
    'describe_search',
    'describe_synthesize',
    'describe_synthesize_stream',
    'describe_rewrite_prompt',
    'admin_describe_stats',
    'check_source_license',
    'check_repo_safety',
    'wizard_preflight',
    'remediation_lookup',
    'remediation_record',
    'remediation_record_failure',
    'remediation_record_failed_attempt',
    'federation_remediation_share',
    'built_image_cache_lookup',
    'built_image_cache_record',
    'build_task_status',
    'finalize_registration',
    'admin_remediation_stats',
    'feedback_node_broken',
    'admin_demote_remediation',
    'remediation_promoted',
    'create_or_update_node_definition',
    'list_node_definitions_for_tenant',
    'patch_variant_status',
    'list_curated_base_images',
    'wizard_llm_status',
    'get_node_definitions_status',
    'worker_llm_ping',
    'get_node_definition',
    'get_scan_queue_context',
    'get_node_manifest',
    'set_node_manifest_metadata',
    'verify_node_manifest',
    'rescan_node_definition',
    'set_node_role',
    'update_node_definition',
    'delete_node_definition',
    'calibrate_node',
    'recompute_cost',
    'build_variant',
    'refresh_node_definitions',
    'check_image_ref_endpoint',
    'generate_dockerfile',
    'build_and_register',
]
