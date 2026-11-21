# API/routers/nodes_builds.py
"""Nodes/builds — the clean `/v1/woodsman` resource (OZ-F7 mount rename).

clean-v2-api / node-definitions-collapse wave (openspec: clean-v2-api,
design.md "Domain 2", collapse rule 5: "internal worker<->API callbacks ->
`/v1/woodsman/*`"). This router groups the wizard BUILD PIPELINE — the
node-container/worker-facing side of node authoring — under one explicit
internal namespace, replacing the equivalent `/v1/node-definitions/wizard/*`,
`/build-*`, `/check-*`, `/analyze-*`, `/describe-*`, `built-image-cache/*`
etc. paths.

REUSE, NOT REWRITE — see routers/nodes.py's module docstring for the
reuse pattern (same function objects, re-registered at new paths via
`router.<verb>(path, **same kwargs)(function)`); the corresponding
`@router.*` decorators in `routers/node_definitions_domains/*.py` have
been removed.

CRITICAL: several of these endpoints are called directly (hardcoded
`/v1/node-definitions/...` URLs) by Python scripts baked into node-container
images — `CORE/DockerContainers/build-task/stages/*.py` and
`CORE/DockerContainers/ImageBuilder/*.py` — which this wave's charter
explicitly forbids editing. See the wave report for the full file:line
list of callers that must be repointed to `/v1/woodsman/...` before
(or in the same deploy as) the old `/node-definitions` paths are retired.

Note: the SEPARATE `/v1/node-authoring/*` external contract (this
package's `external_router`s in node_marketplace.py/node_wizard.py/
task_queue_scheduler.py — `/contract`, `/builds/{task_id}`, `/base-images`,
`POST /nodes`, `POST /nodes/validate`) is NOT migrated here: a repo-wide
grep found zero callers in Worker/DockerContainers/WorkflowBuilder/Dashboard
for `node-authoring`, so it's left mounted exactly as before pending a
follow-up audit (dead code vs. an as-yet-undiscovered external client).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
# GY.C16 remaining routes: the wizard SSE synthesis stream can echo customer
# description content + learned exemplars — same data-content gate.
from dependencies import require_auditor_data_content
from node_builder.repo_docs import store as repo_docs_store

from routers.node_definitions import (
    # node_authoring.py
    get_build_spec,
    get_build_spec_context,
    post_stage_result,
    sync_variants_from_hero,
    variant_sample_test_result,
    variant_verified,
    build_spec_eta,
    enqueue_variant_test_pending,
    variant_test_pending_status,
    variant_change_gate_weekly_report,
    _ChangeGateWeeklyReportResponse,
    describe_cascade,
    retry_with_elevated_budget,
    finalize_partial_publish,
    submit_wizard_support_ticket,
    finalize_variant_verification,
    extract_repo_docs,
    describe_search,
    describe_synthesize,
    describe_synthesize_stream,
    describe_rewrite_prompt,
    check_source_license,
    check_repo_safety,
    build_task_status,
    finalize_registration,
    check_image_ref_endpoint,
    generate_dockerfile,
    build_and_register,
    built_image_cache_lookup,
    built_image_cache_record,
    # task_queue_scheduler.py
    ingest_wizard_artifacts,
    reset_repair_budget,
    get_generation_artifacts,
    get_port_diff,
    upload_container_image,
    adapt_container_to_node,
    # workers_fleet.py
    get_wizard_cluster_capabilities,
    repair_artifacts,
    # workflows.py
    analyze_repo,
    rebuild_variant_with_edits,
    build_and_register_local,
    # assets_cogs_files.py
    check_syntax_endpoint,
    analyze_script,
    # node_wizard.py
    build_and_register_node,
    get_build_run_progress,
    # iam_permissions.py
    predict_variants,
    retry_with_corrections,
    # response models
    _SyncVariantsFromHeroResponse,
    _VariantSampleTestResultResponse,
    _VariantVerifiedResponse,
    _BuildSpecEtaResponse,
    _EnqueueVariantTestPendingResponse,
    _VariantTestPendingStatusResponse,
    _DescribeCascadeResponse,
    _RetryElevatedBudgetResponse,
    _FinalizePartialPublishResponse,
    _SupportTicketResponse,
    _FinalizeVerificationResponse,
    _RepoDocsExtractResponse,
    _DescribeSearchResponse,
    _DescribeSynthesizeResponse,
    _DescribeRewriteResponse,
    _CheckSourceLicenseResponse,
    _CheckRepoSafetyResponse,
    _BuiltImageCacheLookupResponse,
    _BuildSpecCreateResponse,
    CheckImageRefResponse,
    IngestArtifactsResponse,
    ResetRepairBudgetResponse,
    ClusterCapabilitiesResponse,
    RepairArtifactsResponse,
    AnalyzeScriptResponse,
    CheckSyntaxResponse,
)

router = APIRouter(
    prefix="/woodsman",
    tags=["Woodsman Converter"],
    responses={404: {"description": "Not found"}},
    # OZ-F7 (11-15, Tim): the /v1/nodes/builds family is a HARD mount-prefix
    # rename IN PLACE to /v1/woodsman (tag "Woodsman Converter"), NO aliases,
    # old path 404. The router module and every identifier inside it stay
    # `nodes_builds` (internals stay); only the MOUNT PREFIX changed. The
    # renamed surface is a named public product (the Woodsman Converter), so
    # it is back in the OpenAPI schema (the old include_in_schema=False hid the
    # pre-rename internal name; the golden contract regenerates in OZ-F7 P6).
)

# ---------------------------------------------------------------------
# build-specs
# ---------------------------------------------------------------------
router.get("/build-specs/{spec_id}")(get_build_spec)
router.get("/build-specs/{spec_id}/context")(get_build_spec_context)
router.get("/build-specs/{spec_id}/eta", response_model=_BuildSpecEtaResponse)(build_spec_eta)
router.post("/build-specs/{spec_id}/stage-result")(post_stage_result)
router.get("/build-task-status/{task_id}")(build_task_status)

# ---------------------------------------------------------------------
# wizard/*
# ---------------------------------------------------------------------
router.post("/wizard/ingest-artifacts", response_model=IngestArtifactsResponse)(ingest_wizard_artifacts)
router.post("/wizard/reset-repair-budget", response_model=ResetRepairBudgetResponse)(reset_repair_budget)
router.post(
    "/wizard/sync-variants-from-hero",
    response_model=_SyncVariantsFromHeroResponse,
)(sync_variants_from_hero)
router.post(
    "/wizard/variant-sample-test-result",
    response_model=_VariantSampleTestResultResponse,
)(variant_sample_test_result)
router.post("/wizard/variant-verified", response_model=_VariantVerifiedResponse)(variant_verified)
router.post(
    "/wizard/enqueue-variant-test-pending",
    response_model=_EnqueueVariantTestPendingResponse,
)(enqueue_variant_test_pending)
router.get(
    "/wizard/variant-test-pending-status",
    response_model=_VariantTestPendingStatusResponse,
)(variant_test_pending_status)
# GO.8 (23.7.s3) — read-out of the change-gate weekly report (the persisted
# audit event VARIANT_CHANGE_GATE_WEEKLY_REPORT). Platform-admin only; the
# handler answers {"report": null} when no sweep has run yet.
router.get(
    "/wizard/variant-change-gate-weekly-report",
    response_model=_ChangeGateWeeklyReportResponse,
)(variant_change_gate_weekly_report)
router.post(
    "/wizard/retry-with-elevated-budget",
    response_model=_RetryElevatedBudgetResponse,
)(retry_with_elevated_budget)
router.post(
    "/wizard/finalize-partial-publish",
    response_model=_FinalizePartialPublishResponse,
)(finalize_partial_publish)
router.post("/wizard/support-tickets", response_model=_SupportTicketResponse)(submit_wizard_support_ticket)
router.post(
    "/wizard/finalize-verification",
    response_model=_FinalizeVerificationResponse,
)(finalize_variant_verification)
router.get(
    "/wizard/cluster-capabilities",
    response_model=ClusterCapabilitiesResponse,
)(get_wizard_cluster_capabilities)
router.post("/wizard/repair-artifacts", response_model=RepairArtifactsResponse)(repair_artifacts)
router.post("/wizard/rebuild-variant", status_code=status.HTTP_202_ACCEPTED)(rebuild_variant_with_edits)
router.post(
    "/build-and-register-node",
    response_model=_BuildSpecCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)(build_and_register_node)
router.get("/build-run-progress/{workflow_run_id}")(get_build_run_progress)

# ---------------------------------------------------------------------
# build-* / from-container / generate-dockerfile
# ---------------------------------------------------------------------
router.post("/from-container", status_code=status.HTTP_202_ACCEPTED)(adapt_container_to_node)
router.post("/generate-dockerfile", status_code=status.HTTP_202_ACCEPTED)(generate_dockerfile)
router.post("/build-and-register", status_code=status.HTTP_202_ACCEPTED)(build_and_register)
router.post("/build-and-register-local", status_code=status.HTTP_202_ACCEPTED)(build_and_register_local)
router.post("/upload-image", status_code=status.HTTP_201_CREATED)(upload_container_image)
router.post("/finalize-registration")(finalize_registration)

# ---------------------------------------------------------------------
# check-* / analyze-* / describe-* / repo-docs
# ---------------------------------------------------------------------
router.post("/check-source-license", response_model=_CheckSourceLicenseResponse)(check_source_license)
router.post("/check-repo-safety", response_model=_CheckRepoSafetyResponse)(check_repo_safety)
router.post("/check-image-ref", response_model=CheckImageRefResponse)(check_image_ref_endpoint)
router.post("/check-syntax", response_model=CheckSyntaxResponse)(check_syntax_endpoint)
router.post("/analyze-script", response_model=AnalyzeScriptResponse)(analyze_script)
router.post("/analyze-repo", response_model=AnalyzeScriptResponse)(analyze_repo)
router.post("/repo-docs/extract", response_model=_RepoDocsExtractResponse)(extract_repo_docs)


def get_repo_docs(
    extract_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    row = db.execute(
        select(models.RepoDocsExtract).where(
            models.RepoDocsExtract.id == extract_id,
            models.RepoDocsExtract.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="repo-docs extract not found")
    return {
        "id": row.id,
        "chunks": [
            repo_docs_store._chunk_to_dict(c)
            for c in repo_docs_store.chunks_from_row(row)
        ],
    }


router.get("/repo-docs/{extract_id}")(get_repo_docs)
router.post("/describe-cascade", response_model=_DescribeCascadeResponse)(describe_cascade)
router.post("/describe-search", response_model=_DescribeSearchResponse)(describe_search)
router.post("/describe-synthesize", response_model=_DescribeSynthesizeResponse)(describe_synthesize)
router.post("/describe-synthesize-stream",
            dependencies=[Depends(require_auditor_data_content)])(
    describe_synthesize_stream)
router.post("/describe-rewrite-prompt", response_model=_DescribeRewriteResponse)(describe_rewrite_prompt)

# ---------------------------------------------------------------------
# predict-variants / retry-with-corrections
# ---------------------------------------------------------------------
router.post("/predict-variants")(predict_variants)
router.post("/retry-with-corrections", status_code=status.HTTP_202_ACCEPTED)(retry_with_corrections)

# ---------------------------------------------------------------------
# built-image-cache/* / generation-artifacts/*
# ---------------------------------------------------------------------
router.get(
    "/built-image-cache/lookup",
    response_model=_BuiltImageCacheLookupResponse,
)(built_image_cache_lookup)
router.post("/built-image-cache/record")(built_image_cache_record)
router.get("/generation-artifacts/{task_id}")(get_generation_artifacts)
router.get("/generation-artifacts/{task_id}/port-diff")(get_port_diff)
