"""Shared imports, helpers, Pydantic models and module constants for the
node_definitions router, extracted verbatim during the Gate 2a domain split.

This module is the sibling `_shared.py` for the `node_definitions_domains`
package: every per-domain sub-module does `from ._shared import *` so the
endpoint bodies keep referencing these names unchanged. Behaviour-preserving
MOVE only (openspec: stack-training-and-prewarm, Workstream B, D5/D10).
"""

__all__ = [
    'json',
    'os',
    're',
    'datetime',
    'timezone',
    'List',
    'Optional',
    'Dict',
    'Any',
    'APIRouter',
    'Depends',
    'HTTPException',
    'status',
    'Header',
    'Request',
    'Response',
    'UploadFile',
    'File',
    'Form',
    'BackgroundTasks',
    'PydanticBaseModel',
    'Field',
    'Session',
    'flag_modified',
    'schemas',
    'models',
    'crud',
    'get_db',
    'require_active_and_onboarded_user',
    'require_active_onboarded_parent_user',
    'require_bearer_or_api_key',
    'require_service_api_key',
    'require_build_task_token',
    'require_user_or_build_token',
    'require_node_authoring_key',
    'assert_build_scope',
    'BuildAuthContext',
    'logging',
    'logger',
    'node_admission',
    'platform_image',
    'tenant_registry',
    'Cap',
    'evaluate_feature',
    'get_entitlements',
    '_is_service_account',
    '_node_image_slug',
    '_docs_driven_builds_enabled',
    '_universal_variant_fanout_enabled',
    '_run_admission',
    '_admission_dry_run',
    'NODE_CREATION_CREDIT_COST',
    '_PLATFORM_TENANT_ID_DESCRIBE',
    '_DescribeSearchRequest',
    '_DescribeSearchResponse',
    '_DESCRIBE_SEARCH_LIMIT_KEY',
    '_DESCRIBE_SEARCH_LIMIT_COUNT',
    '_DESCRIBE_SEARCH_LIMIT_WINDOW_S',
    '_redis_for_describe',
    '_check_and_inc_describe_rate',
    '_decrement_describe_rate',
    '_load_platform_github_token',
    '_parse_user_tool_answer',
    '_WEB_SEARCH_WORKFLOW_NAME',
    '_web_search_expand_budget_s',
    '_web_search_expand_poll_s',
    '_looks_like_web_search_artifact',
    '_run_web_query_expansion',
    '_BuildSpecCreateResponse',
    'submit_raw_context_build',
    '_StageResultRequest',
    '_SyncVariantsFromHeroRequest',
    '_SyncVariantsFromHeroResponse',
    '_VariantSampleTestResultRequest',
    '_VariantSampleTestResultResponse',
    '_VariantVerifiedRequest',
    '_VariantVerifiedResponse',
    '_BuildSpecEtaResponse',
    '_EnqueueVariantTestPendingRequest',
    '_EnqueueVariantTestPendingResponse',
    '_VariantTestPendingStatusResponse',
    '_DescribeCascadeRequest',
    '_DescribeCascadeResponse',
    '_RetryElevatedBudgetRequest',
    '_RetryElevatedBudgetResponse',
    '_FinalizePartialPublishRequest',
    '_FinalizePartialPublishResponse',
    '_SupportTicketRequest',
    '_SupportTicketResponse',
    '_FinalizeVerificationRequest',
    '_FinalizeVerificationResponse',
    '_VariantVerificationRow',
    '_GetVariantVerificationResponse',
    '_DriftRulingRequest',
    '_DriftRulingResponse',
    '_PatchReferenceVariantRequest',
    '_PatchReferenceVariantResponse',
    '_SuggestCategoryRequest',
    '_SuggestCategoryResponse',
    '_RepoDocsExtractRequest',
    '_RepoDocsChunkOut',
    '_RepoDocsExtractResponse',
    '_MarketplaceToggleRequest',
    '_MarketplaceToggleResponse',
    '_CurateListItem',
    '_CurateListResponse',
    '_derive_compute_backends',
    '_require_private_marketplace',
    '_require_marketplace_publish',
    '_TenantCurateTeamGrant',
    '_TenantCurateListItem',
    '_TenantCurateListResponse',
    '_AddToMarketplaceRequest',
    '_AddToMarketplaceResponse',
    '_TenantTeamGrantRequest',
    '_TenantTeamGrantResponse',
    '_TenantTeamRevokeResponse',
    '_resolve_tenant_node_for_admin',
    '_node_definition_use_capability',
    '_MarketplaceListItem',
    '_MarketplaceListResponse',
    '_MarketplaceDownloadResponse',
    'NodePaidListingExport',
    '_NodeSampleUpdate',
    '_NodeTryRequest',
    '_require_platform_admin',
    '_package_to_response',
    '_INTENTS_THAT_SEARCH',
    '_DescribeSynthesizeRequest',
    '_DescribeSynthesizeResponse',
    '_SYNTH_LIMIT_KEY',
    '_SYNTH_LIMIT_WINDOW_S',
    '_SYNTH_RATE_BASE',
    '_SYNTH_RATE_FLOOR',
    '_SYNTH_RATE_CEIL',
    '_synth_call_budget',
    '_check_and_inc_synth_rate',
    '_DescribeRewriteRequest',
    '_DescribeRewriteResponse',
    '_DescribeStatsResponse',
    '_CheckSourceLicenseRequest',
    '_CheckSourceLicenseResponse',
    '_CheckRepoSafetyRequest',
    '_CheckRepoSafetyResponse',
    '_check_host_docker_disk',
    '_is_saas',
    '_RemediationLookupResponse',
    '_RemediationRecordRequest',
    '_RemediationFailureRequest',
    '_RemediationFailedAttemptRequest',
    'PROMOTION_MIN_SUCCESSES',
    'PROMOTION_MAX_FAILURES',
    '_trust_score',
    '_bad_federation_peers',
    '_maybe_promote',
    '_FederationShareRequest',
    '_BuiltImageCacheLookupResponse',
    '_BuiltImageCacheRecordRequest',
    '_FinalizeRegistrationRequest',
    '_extract_deferred_registration',
    '_extract_sample_test_failure',
    '_NodeFeedbackRequest',
    '_index_remediation_embedding',
    '_assert_node_registration_affordable',
    '_charge_node_creation_credits',
    '_effective_billing_user',
    '_effective_creator_user_id',
    '_image_is_in_tenant_harbor',
    '_parse_tenant_harbor_ref',
    '_decide_harbor_scan_outcome',
    '_persist_scan_outcome',
    '_poll_harbor_scan_async',
    '_clear_soft_deletes_for_node_refs',
    '_dispatch_trivy_scan',
    'SIZE_TIERS',
    'CALIBRATION_RUNS_PER_TIER',
    'CALIBRATION_STRESSORS',
    'PAIRWISE_RUNS_PER_TIER',
    'MULTI_STRESSOR_PAIRS',
    'TOTAL_CALIBRATION_TASKS',
    '_calibration_node_id',
    '_enqueue_calibration_task',
    '_calibration_input_for_node',
    '_qualify_image_ref',
    '_auto_calibrate_new_node_hook',
    '_dispatch_calibration_tasks',
    '_derive_node_kind_from_image',
    'VariantStatusRequest',
    '_VARIANT_SMOKE_QUEUE',
    '_enqueue_variant_smoke_task',
    'BaseImageCatalogEntry',
    'WizardLLMStatusResponse',
    'WorkerLLMPingResponse',
    'ManifestMetadata',
    '_NodeRoleUpdate',
    '_tag_harbor_for_deletion_best_effort',
    '_parse_harbor_image_ref',
    '_try_registry_delete_image',
    'CheckSyntaxRequest',
    'CheckSyntaxResponse',
    'CheckImageRefRequest',
    'CheckImageRefResponse',
    'ClarifyingQuestion',
    'GlueArtifact',
    'AnalyzeScriptRequest',
    'AnalyzeScriptResponse',
    'AnalyzeRepoRequest',
    'ContainerAdaptRequest',
    '_PLATFORM_IMAGE_CACHE',
    '_PLATFORM_IMAGE_CACHE_TTL_S',
    '_probe_image_manifest',
    '_resolve_platform_image',
    '_enqueue_container_task',
    '_build_llm_env',
    '_adapter_slot_failure_cause',
    '_adapter_slot_guard',
    'BuildRequest',
    'PredictVariantsRequest',
    '_DEEP_CUDA_PATTERNS',
    '_DEEP_CUDA_EXTENSIONS',
    '_detect_deep_cuda',
    '_DEFAULT_TORCH_CUDA_ARCH_LIST',
    '_detect_deep_cuda_with_hints',
    'RetryWithCorrectionsRequest',
    '_retry_parse_build_results',
    '_retry_build_corrections_prompt',
    'RebuildVariantRequest',
    'RepairArtifactsRequest',
    'RepairArtifactsResponse',
    'IngestArtifactsRequest',
    'IngestArtifactsResponse',
    'ResetRepairBudgetRequest',
    'ResetRepairBudgetResponse',
    'ClusterCapabilitiesResponse',
    '_extract_llm_ping',
    '_ARTIFACT_BEGIN_SENTINEL',
    '_ARTIFACT_END_SENTINEL',
    '_extract_albedo_artifacts',
    '_extract_codebase_stages',
    'BuildLocalRequest',
    '_get_or_provision_tenant_harbor_creds',
    '_load_node_contract',
]

import json
import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request, Response, UploadFile, File, Form, BackgroundTasks
from pydantic import BaseModel as PydanticBaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import schemas
import models
import crud
from dependencies import get_db, require_active_and_onboarded_user, require_active_onboarded_parent_user, require_bearer_or_api_key, require_service_api_key, require_build_task_token, require_user_or_build_token, require_node_authoring_key, assert_build_scope, BuildAuthContext
import logging

logger = logging.getLogger(__name__)


from utils import node_admission
# GS.PSR.8 — credits kill-switch via the platform-settings registry
# (billing.credits_enabled: DB > env > True), registered on the module
# that owns credit behaviour.
try:
    from utils.credit_manager import credits_enabled as _credits_enabled
except ImportError:
    def _credits_enabled():
        return False
from utils.minio_client import get_tenant_bucket_name
from utils.registries import platform_image
from utils.registries import tenant_registry
from utils.registries import platform_registry
from utils.registries import ce_mirror_ref
from utils.entitlements import Cap, evaluate_feature, get_entitlements


def _is_service_account(user) -> bool:
    """True for the internal API service user (build-task containers, the
    Containerizer dispatch path, anything authenticating with the seeded
    platform API key).

    Reads ``users.is_service_account`` (added 2026-05-13 to replace the
    legacy ``username == "api_service"`` literal check that was always
    false in production — the real service user is ``api@service.local``,
    Keycloak-imported). Falls back to the literal username comparison so
    legacy fixtures + on-prem deployments that haven't run the
    20260513_users_is_service_account migration still work.

    ``is True`` (rather than ``bool(...)``) so MagicMock fixtures in the
    test suite — where every attribute auto-vivifies to a truthy Mock —
    correctly fall through to the username check instead of being
    misidentified as service accounts.
    """
    if getattr(user, "username", None) == "api_service":
        return True
    return getattr(user, "is_service_account", False) is True


# Router is defined up-front so module-level @router.<method> decorators
# (wizard_preflight, remediation/*, etc.) can reference it during import.
# It's mutated by the decorators below; the original location lower in
# the file used to break import when those endpoints were added above
# their original site (NameError: 'router' is not defined).

# Public, versioned external node-authoring surface (open-core wizard
# replacement). Colocated here so it reuses the same admission / build / scan
# helpers, but mounted under its own stable /v1/node-authoring namespace so the
# contract is independent of the internal /node_definitions router's shape.


def _node_image_slug(text: str) -> str:
    """The slug used as the harbor image_name for a wizard-built Node.

    Lowercased; runs of non-alphanumeric chars collapsed to `-`;
    leading/trailing `-` stripped. Empty input yields `node` so the
    push always has SOMETHING to publish under.

    Used both by:
      - the inputs-only build-and-register-node endpoint to derive
        the harbor tag from node_name (so each Node gets its own
        image; deleting one doesn't affect siblings)
      - the slug-collision guard above the derivation, so two
        names that slug to the same value can't both register
    """
    import re as _re
    s = (text or "").lower().strip()
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "node"


def _docs_driven_builds_enabled() -> bool:
    """Feature flag for the §4 chunk 3 universal mode dispatcher.

    When True: non-Image modes (Git / Script / Dockerfile) extract
    their BuildPlan via the new dispatcher at submission time and
    skip the legacy Containerizer-LLM generate phase. When False
    (default): non-Image modes fall through to the legacy
    LLM-driven path unchanged. Image mode always uses BuildPlan
    (chunk 2 wired that path months ago + the manual-test pass
    locked it down).
    """
    from services import platform_settings as _psr
    return bool(_psr.get_setting("wizard_docs_driven_builds_enabled"))


def _universal_variant_fanout_enabled() -> bool:
    """Feature flag for §4 chunk 4 universal variant fan-out.

    When True: the BuildPlan dispatcher path also fans the plan out
    across the universal accelerator set (cpu / cuda / rocm /
    intel / npu) via `apply_variant_fanout`. The hero variant gets
    `spec.hero_dockerfile`; non-hero compatible variants get a
    `BuildVariantSpec` in `spec.variants` with their per-accelerator
    adapted Dockerfile (torch wheels rewritten); architecturally-
    incompatible variants get recorded in `spec.incompatible_variants`
    so the Node card can explain why they weren't built.

    When False (default): only the hero variant builds — same
    behavior as before chunk 4 shipped. Independent of the
    docs-driven flag so the two rollouts can stagger.
    """
    from services import platform_settings as _psr
    return bool(_psr.get_setting("wizard_variant_fanout_universal"))


def _run_admission(
    mode: str,
    payload: dict,
    current_user: "models.User",
    effective_tenant_id: Optional[int] = None,
    strict: bool = False,
) -> None:
    """Run Layer-1 admission and raise 422 on failure. No-op on on-prem,
    for system nodes (tenant_id=None), and for the api_service account.
    Kill switch: DISABLE_NODE_ADMISSION=true short-circuits to pass for
    emergency override (audit-logged). Platform admins can alternatively
    pass `force_admission=True` in the payload for per-request bypass.

    `strict=True` enforces the base-image allowlist even under
    WIZARD_ALLOW_ANY_IMAGE relaxation (external node-authoring path)."""
    if os.getenv("DISABLE_NODE_ADMISSION", "false").lower() == "true":
        logger.warning("DISABLE_NODE_ADMISSION=true: skipping admission for mode=%s", mode)
        return
    tenant_id = effective_tenant_id if effective_tenant_id is not None else current_user.tenant_id
    result = node_admission.validate_for_mode(
        mode,
        payload,
        tenant_id=tenant_id,
        is_platform_admin=bool(getattr(current_user, "is_platform_admin", False)),
        is_service_account=_is_service_account(current_user),
        force_admission=bool(payload.get("force_admission")),
        strict=strict,
    )
    if not result.passed:
        logger.info(
            "Admission reject mode=%s rule=%s tenant_id=%s user=%s reason=%s",
            mode, result.rule, tenant_id, current_user.username, result.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "admission_rule": result.rule,
                "reason": result.reason,
            },
        )


def _admission_dry_run(
    mode: str,
    payload: dict,
    current_user: "models.User",
    effective_tenant_id: Optional[int] = None,
    strict: bool = True,
) -> "node_admission.AdmissionResult":
    """Read-only admission check for the MCP `validate_submission` tool.

    Runs the same `validate_for_mode` gate as `_run_admission` but RETURNS the
    structured `AdmissionResult` (pass/fail + rule + reason) instead of raising
    422 and registers/creates nothing. Defaults to strict (external path)."""
    tenant_id = effective_tenant_id if effective_tenant_id is not None else current_user.tenant_id
    return node_admission.validate_for_mode(
        mode,
        payload,
        tenant_id=tenant_id,
        is_platform_admin=bool(getattr(current_user, "is_platform_admin", False)),
        is_service_account=_is_service_account(current_user),
        force_admission=False,
        strict=strict,
    )


# Credits per new-node creation. Applied uniformly across all four wizard
# modes (Image / Dockerfile / Git Repo / Script). Charged at the imagebuilder
# dispatch point (or at pre-built-image registration for Image mode) — never
# on LLM-only analysis steps like /generate-artifacts or /generate-dockerfile
# which are free so users can iterate without burning credits. Service
# accounts and platform admins are exempt; tenants with CREDITS_ENABLED=false
# are exempt (on-prem / self-hosted paths).
NODE_CREATION_CREDIT_COST = 100


# δ Phase 6 — describe-and-build endpoint. Tenant id used to load
# the platform-managed github_token (mirrors harbor_* convention).
_PLATFORM_TENANT_ID_DESCRIBE = 1


class _DescribeSearchRequest(PydanticBaseModel):
    description: str
    # human-assisted-discovery re-fire — the user's answer to a prior
    # clarifying question (a library name / GitHub repo or URL / technical
    # term). When present the call bypasses the cache, folds the answer into
    # the search pool, and records it as a learned crossover.
    user_tool_answer: Optional[str] = None


class _DescribeSearchResponse(PydanticBaseModel):
    intent: str
    interpretation: str
    search_keywords: List[str]
    candidates: List[Dict[str, Any]]
    cached: bool = False
    rate_limited: bool = False
    not_configured: bool = False
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset_seconds: Optional[int] = None
    # human-assisted-discovery — set when the search pool is weak and no
    # curated/learned crossover matched: the wizard asks the user for a
    # library / GitHub repo / URL / technical term instead of building blind.
    needs_clarification: bool = False
    clarifying_question: str = ""
    # True when a repo search ran but no candidate is worth showing
    # (best relevance below the floor) — the task is trivial / standard-
    # library-tier and the results are keyword noise. The wizard skips
    # the candidate list and goes straight to "build fresh".
    recommend_build_fresh: bool = False
    # Node-vs-workflow surface hint. "workflow" means the request is a
    # multi-step pipeline better built in the Workflow Wizard; the node
    # wizard shows a non-blocking "Switch to Workflow Wizard" nudge.
    # Conservative default "node" — only an explicit classification flips it.
    surface: str = "node"
    surface_reason: str = ""


# Per-tenant rate-limit window for /describe-search. 5 fresh
# searches per 15 min (cache hits don't count). Guards the
# deployment-wide GitHub API quota — without this one tenant could
# burn through 5000/h on their own.
_DESCRIBE_SEARCH_LIMIT_KEY = "mod:describe:rate:tenant:{tenant_id}"
_DESCRIBE_SEARCH_LIMIT_COUNT = 5
_DESCRIBE_SEARCH_LIMIT_WINDOW_S = 15 * 60


def _redis_for_describe():
    """Lazy Redis import + client. Mirrors describe_cache pattern
    so tests can monkeypatch one factory."""
    import redis  # local import — keep route module light
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def _check_and_inc_describe_rate(tenant_id: int) -> Dict[str, Any]:
    """Returns ``{allowed: bool, remaining: int, reset_seconds: int}``.

    Allows the call when the per-tenant counter is below the limit;
    otherwise reports remaining=0 and how many seconds until the
    window resets. Redis errors fail open (allowed=True) — better to
    serve a few extra requests than block the wizard on a Redis
    blip.
    """
    try:
        client = _redis_for_describe()
        key = _DESCRIBE_SEARCH_LIMIT_KEY.format(tenant_id=tenant_id)
        # Atomic inc-and-set-ttl-if-new dance. Pipeline so concurrent
        # tenants don't race the TTL set.
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        if int(count) == 1 or int(ttl) < 0:
            client.expire(key, _DESCRIBE_SEARCH_LIMIT_WINDOW_S)
            ttl = _DESCRIBE_SEARCH_LIMIT_WINDOW_S
        if int(count) > _DESCRIBE_SEARCH_LIMIT_COUNT:
            return {
                "allowed": False,
                "remaining": 0,
                "reset_seconds": int(ttl),
            }
        return {
            "allowed": True,
            "remaining": _DESCRIBE_SEARCH_LIMIT_COUNT - int(count),
            "reset_seconds": int(ttl),
        }
    except Exception as e:
        logger.warning("describe-search rate-limit check failed (fail-open): %s", e)
        return {"allowed": True, "remaining": -1, "reset_seconds": -1}


def _decrement_describe_rate(tenant_id: int) -> None:
    """Roll back a counter increment when we end up serving from
    cache (cache hits shouldn't count against the tenant's
    quota)."""
    try:
        client = _redis_for_describe()
        key = _DESCRIBE_SEARCH_LIMIT_KEY.format(tenant_id=tenant_id)
        client.decr(key)
    except Exception:
        pass


def _load_platform_github_token(db: Session) -> Optional[str]:
    """Resolve the platform GitHub token via the single shared resolver
    (utils.github_token) so the describe cascade, repo-fetch, and github_search
    all use ONE token source/name: the canonical ``github_token`` secret, with
    ``GITHUB_PLATFORM_TOKEN`` / ``GITHUB_TOKEN`` env as fallback. The
    PLEASE_CONFIGURE placeholder counts as unset."""
    from utils.github_token import get_platform_github_token
    return get_platform_github_token(db)


def _parse_user_tool_answer(answer: Optional[str]):
    """Parse a human-assisted-discovery answer into ``(repo, keyword, pypi)``.

    Recognises a GitHub URL / ``owner/repo`` shorthand / ``git@`` SSH form as a
    repo (which gets seeded as a candidate), a non-GitHub URL or a multi-word
    phrase as a free-text search keyword, and a bare single token as BOTH a
    search keyword and a candidate PyPI package name (bare tokens are usually
    importable package names that also match the hosting repo). Any of the three
    may be None. Never raises."""
    text = (answer or "").strip()
    if not text:
        return None, None, None
    try:
        from utils import github_search
        repo = github_search._parse_owner_repo(text)
    except Exception:  # noqa: BLE001 — parsing must never break the search
        repo = None
    if repo:
        return repo, None, None
    if "://" in text or " " in text:
        # A URL we couldn't resolve to a repo, or a multi-word phrase — keyword.
        return None, text, None
    # Bare token — good as both a search keyword and a PyPI package name.
    return None, text, text


# ---------------------------------------------------------------------
# add-web-search-node §6 — web query-expansion for the describe-search
# discovery path. Runs the hidden "System — Web Search" workflow as a
# REAL, logged workflow run (create the run + hand off to the orchestrator's
# start_workflow_execution — the same machinery the build wizard uses),
# passing the user's description as the QUERY input, then polls the
# NodeRun for the JSON artifact the container POSTs back via
# /admin/web-search/result. This is deliberately NOT an in-process
# web_expand call: the requirement is that every expansion runs (and is
# audited) as a normal workflow.
#
# Best-effort throughout: on a missing/unseeded workflow, dispatch error,
# timeout, or an ok=False/empty artifact it returns None and discovery
# proceeds on the intent-classifier keyword pool alone (never errors the
# request).
# ---------------------------------------------------------------------
_WEB_SEARCH_WORKFLOW_NAME = "System — Web Search"


def _web_search_expand_budget_s() -> float:
    """Total seconds to wait for the web-search run's artifact before
    falling back. Tunable via WEB_SEARCH_EXPAND_BUDGET_S; a few seconds by
    default (0 disables the expansion entirely)."""
    try:
        return max(0.0, float(os.getenv("WEB_SEARCH_EXPAND_BUDGET_S", "6")))
    except (TypeError, ValueError):
        return 6.0


def _web_search_expand_poll_s() -> float:
    """Poll interval while waiting for the artifact."""
    try:
        return max(0.1, float(os.getenv("WEB_SEARCH_EXPAND_POLL_S", "0.5")))
    except (TypeError, ValueError):
        return 0.5


def _looks_like_web_search_artifact(value: Any) -> bool:
    """True when `value` has the node's OUTPUT artifact shape (any of the
    expected list keys present) — distinguishes a callback-stored artifact
    dict from an empty/None NodeRun.outputs or a worker-written list."""
    return isinstance(value, dict) and any(
        k in value for k in ("results", "github_repos", "pypi_packages", "jargon")
    )


def _run_web_query_expansion(
    db: Session, description: str, *, user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run the hidden web-search workflow with `description` as QUERY and
    return its OUTPUT artifact dict, or None on any failure/timeout.

    The artifact shape is
    ``{results, github_repos, pypi_packages, jargon, ok}``.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.run_web_query_expansion(db, description, user_id=user_id)


# ---------------------------------------------------------------------
# δ DAG-wizard B4 — BuildSpec storage + dispatch endpoints
# ---------------------------------------------------------------------
class _BuildSpecCreateResponse(PydanticBaseModel):
    build_spec_id: int
    workflow_run_id: Optional[int] = None
    workflow_id: Optional[int] = None








def submit_raw_context_build(
    db, *, image_name: str, node_name: str, dockerfile: str, context_bytes: bytes,
    tenant_id: int, user_id: int, process_type: str = "CPU",
    input_type: str = "image", output_type: str = "image",
    cost_in_credits: int = 1, description: Optional[str] = None,
    node_metadata: Optional[Dict[str, Any]] = None, filename: str = "context.tar.gz",
) -> Dict[str, Any]:
    """Submit a raw-context build (bring-your-own Dockerfile + context) through
    the gated build pipeline: upload the context tarball to MinIO (content-
    addressed), persist a raw_context BuildSpec, create the build workflow, and
    dispatch it. Every gate (auth, tenancy, audit, scoped token, Harbor creds,
    back-pressure) applies exactly as for a wizard build — the only difference
    is the context comes from a tarball, not inline text."""
    from services import node_definitions_shared_service as _svc
    return _svc.submit_raw_context_build(
        db, image_name=image_name, node_name=node_name, dockerfile=dockerfile,
        context_bytes=context_bytes, tenant_id=tenant_id, user_id=user_id,
        process_type=process_type, input_type=input_type, output_type=output_type,
        cost_in_credits=cost_in_credits, description=description,
        node_metadata=node_metadata, filename=filename,
    )




class _StageResultRequest(PydanticBaseModel):
    spec_id: int
    stage: str
    variant: Optional[str] = None
    success: bool
    payload: Dict[str, Any]




# ---------------------------------------------------------------------
# §4 chunk 4 follow-up — variant-sync-from-hero (Issue 2 fix)
# ---------------------------------------------------------------------

class _SyncVariantsFromHeroRequest(PydanticBaseModel):
    build_spec_id: int


class _SyncVariantsFromHeroResponse(PydanticBaseModel):
    propagated: int          # how many variant Dockerfiles were updated
    skipped_non_image: int   # variants left alone (mode != image)




# ---------------------------------------------------------------------
# wizard-rebuild §5 — per-variant sample-test result + reference-variant
# ---------------------------------------------------------------------

class _VariantSampleTestResultRequest(PydanticBaseModel):
    build_spec_id: int
    variant: str
    exit_code: int
    produced_output: bool
    output_type: str
    output_filename: str = ""
    output_body_b64: str = ""  # empty when no output
    # project-agent §1.3 — the manifest digest this attempt pushed.
    # Optional so an older build-task keeps working, but without it a
    # regression can be detected and not reverted: moving :latest leaves
    # the earlier manifest in the registry, and the digest is the handle
    # on it. ImageBuilder already captures this via
    # `docker inspect --format "{{index .RepoDigests 0}}"`.
    image_digest: Optional[str] = None


class _VariantSampleTestResultResponse(PydanticBaseModel):
    verdict: str
    score: float
    publish_ok: bool
    scored_against: Optional[str] = None
    is_marginal: bool = False
    # §1.3 — the row describes the BEST attempt, so these describe how
    # THIS attempt compared to it.
    attempt: int = 1
    best_score: Optional[float] = None
    regressed: bool = False
    published_digest: Optional[str] = None




class _VariantVerifiedRequest(PydanticBaseModel):
    build_spec_id: int
    variant: str
    verdict: str  # verified / drift / marginal / failed / incompatible
    image_ref: Optional[str] = None  # full harbor tag, e.g.
                                     # "harbor.../tenant-X/foo:latest-cuda"


class _VariantVerifiedResponse(PydanticBaseModel):
    node_definition_id: Optional[int]
    variant: str
    verdict: str
    gpu_variants_after: Dict[str, str]




class _BuildSpecEtaResponse(PydanticBaseModel):
    spec_id: int
    eta_seconds: int
    eta_minutes: int
    variant_count: int
    stall_reason: Optional[str] = None  # set when the spec's
                                         # workflow is RUNNING and
                                         # >5 min since last event




class _EnqueueVariantTestPendingRequest(PydanticBaseModel):
    build_spec_id: Optional[int] = None
    node_definition_id: Optional[int] = None
    variant: str
    image_ref: str
    ttl_days: int = 30


class _EnqueueVariantTestPendingResponse(PydanticBaseModel):
    id: int
    enqueued: bool       # True = new row; False = existing pending row returned




class _VariantTestPendingStatusResponse(PydanticBaseModel):
    queue_depth_by_accelerator: Dict[str, int]
    total_pending: int




class _DescribeCascadeRequest(PydanticBaseModel):
    description: str
    confidence_floor: Optional[float] = None


class _DescribeCascadeResponse(PydanticBaseModel):
    description: str
    confidence_floor: float
    candidates: List[Dict[str, Any]]
    per_step: List[Dict[str, Any]]
    rate_limited: Dict[str, Dict[str, Any]]
    errors: Dict[str, str]
    stopped_at_step: Optional[str] = None
    reason: str




class _RetryElevatedBudgetRequest(PydanticBaseModel):
    build_spec_id: int


class _RetryElevatedBudgetResponse(PydanticBaseModel):
    build_spec_id: int
    new_outer_max: int
    outer_used_reset_to: int




class _FinalizePartialPublishRequest(PydanticBaseModel):
    build_spec_id: int


class _FinalizePartialPublishResponse(PydanticBaseModel):
    node_definition_id: Optional[int]
    verified_variants: List[str]
    skipped_variants: List[str]




class _SupportTicketRequest(PydanticBaseModel):
    build_spec_id: int
    user_message: Optional[str] = ""


class _SupportTicketResponse(PydanticBaseModel):
    ticket_id: int
    build_spec_id: int




class _FinalizeVerificationRequest(PydanticBaseModel):
    build_spec_id: int
    node_definition_id: int


class _FinalizeVerificationResponse(PydanticBaseModel):
    promoted: int




class _VariantVerificationRow(PydanticBaseModel):
    variant: str
    score: Optional[float]
    verdict: str
    scored_against_variant: Optional[str]
    output_type_at_test: Optional[str]
    scored_at: Optional[str]
    # project-agent §1.3 — the row describes the best attempt, so a
    # reader needs to know how many there were and whether any were
    # discarded. attempt_history carries the discarded ones.
    attempt_count: int = 1
    best_score: Optional[float] = None
    image_digest: Optional[str] = None
    attempt_history: List[Dict[str, Any]] = []
    # §1.4/§1.5 — a drift verdict with no ruling is withheld from
    # selection. The UI needs to tell "diverged, waiting on you" apart
    # from "diverged, you said ship it".
    drift_ruling: Optional[str] = None
    awaiting_drift_ruling: bool = False


class _GetVariantVerificationResponse(PydanticBaseModel):
    node_definition_id: int
    reference_variant: str
    variants: List[_VariantVerificationRow]
    pending_variants: List[str] = []  # §6 — variants still verifying
                                      # in the background after Node
                                      # registration. Drives the
                                      # "verifying X, Y" UI chip.




class _DriftRulingRequest(PydanticBaseModel):
    ruling: str  # publish | drop | keep_fixing


class _DriftRulingResponse(PydanticBaseModel):
    node_definition_id: int
    variant: str
    ruling: str
    in_service: bool




class _PatchReferenceVariantRequest(PydanticBaseModel):
    reference_variant: str


class _PatchReferenceVariantResponse(PydanticBaseModel):
    node_definition_id: int
    reference_variant: str
    updated: List[Dict[str, Any]]
    skipped: List[Dict[str, Any]]




# ---------------------------------------------------------------------
# Smart node-category suggestion (manual-test-driven 2026-05-22)
# ---------------------------------------------------------------------

class _SuggestCategoryRequest(PydanticBaseModel):
    """Body for `POST /node-definitions/suggest-category`."""
    node_name: str
    description: Optional[str] = ""
    base_image: Optional[str] = None
    source_body: Optional[str] = None
    repo_url: Optional[str] = None


class _SuggestCategoryResponse(PydanticBaseModel):
    suggestion: str
    existing_categories: List[str]
    rationale: str
    source: str   # "llm" | "user-existing-match" | "fallback"




# ---------------------------------------------------------------------
# Wizard-rebuild §3c — repo-docs extract endpoint
# ---------------------------------------------------------------------

class _RepoDocsExtractRequest(PydanticBaseModel):
    """Body for `POST /node_definitions/repo-docs/extract`.

    `repo_url` is required; `ref` defaults to empty so the fetcher
    resolves the repo's default branch. `prefer_llm=False` is
    intended for tests / air-gapped deployments.
    """
    repo_url: str
    ref: str = ""
    prefer_llm: bool = True


class _RepoDocsChunkOut(PydanticBaseModel):
    """One chunk in the response — same shape as DocChunk."""
    source_file: str
    start_line: int
    end_line: int
    heading: Optional[str]
    content: str
    intents: List[str]


class _RepoDocsExtractResponse(PydanticBaseModel):
    extract_id: int
    cache_hit: bool
    repo_url: str
    ref: str
    extractor_version: int
    chunks: List[_RepoDocsChunkOut]
    files_missing: List[str]
    fetched_at: str
    expires_at: str




# ---------------------------------------------------------------------
# Marketplace M2 — admin toggle for surfacing a node in the marketplace
# ---------------------------------------------------------------------
class _MarketplaceToggleRequest(PydanticBaseModel):
    enabled: bool


class _MarketplaceToggleResponse(PydanticBaseModel):
    node_definition_id: int
    is_in_marketplace: bool




class _CurateListItem(PydanticBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    type: Optional[str] = None
    process_type: Optional[str] = None
    # Compute backends derived from `NodeDefinition.gpu_variants`. The
    # Phase-6 rework deprecated `process_type` (it now defaults to
    # "CPU" for every row even when the node has GPU variants) — the
    # scheduler picks the actual queue at enqueue time from
    # gpu_variants. This list mirrors that source-of-truth so the
    # Curate UI can show real backends instead of a stale "CPU" badge
    # for every node.
    compute_backends: List[str] = []
    cost_in_credits: Optional[int] = None
    is_in_marketplace: bool
    # node-role §1 — current classification, so the Curate → Platform Nodes
    # role toggle can show + flip it. 'user' == in the palette/wizard; 'system'
    # == hidden from users but still run by the orchestrator.
    node_role: str = "user"


class _CurateListResponse(PydanticBaseModel):
    items: List[_CurateListItem]


def _derive_compute_backends(nd: "models.NodeDefinition") -> List[str]:
    """Normalize gpu_variants keys into UI-friendly backend labels.
    Returns a stable, deduplicated, ordered list. Falls back to
    ['CPU'] when the node has no variants (which is the historical
    convention for single-image system nodes)."""
    variants = nd.gpu_variants or {}
    if not isinstance(variants, dict) or not variants:
        return ["CPU"]
    label_for = {
        "cpu": "CPU", "cuda": "CUDA", "rocm": "ROCm",
        "intel": "Intel", "npu": "NPU",
        "accelerator": "Accelerator", "generic": "CPU",
    }
    ordered = ["CUDA", "ROCm", "Intel", "NPU", "Accelerator", "CPU"]
    seen: set[str] = set()
    for k in variants.keys():
        seen.add(label_for.get(str(k).lower(), str(k).upper()))
    return [b for b in ordered if b in seen] + [b for b in sorted(seen) if b not in ordered]




# ---------------------------------------------------------------------
# store-node-distribution §4.6 — hosting a tenant's OWN node store (a
# self-branded catalog a tenant publishes to its own org/teams via
# is_in_marketplace on a tenant-owned row) requires Cap.PRIVATE_MARKETPLACE.
# Gates the create/publish-to-own-store surface only — the
# platform-admin MOD-catalogue actions (`all_tenants` / `specific_tenants`
# targets on tenant_add_to_marketplace, and /admin/marketplace-toggle) are a
# different surface (MOD's own public marketplace) and stay ungated here.
def _require_private_marketplace(db: Session, current_user: models.User) -> None:
    es = get_entitlements(db, getattr(current_user, "tenant", None))
    if not evaluate_feature(Cap.PRIVATE_MARKETPLACE, es).allowed:
        raise HTTPException(
            status_code=402,
            detail={"code": "private_marketplace_required"},
        )


# clean-v2-api Wave 10 "CE POLICY RESOLVED: Reconciled" — Cap.MARKETPLACE_PUBLISH
# gates the OTHER surface the comment above calls out: distributing
# a node into the platform-wide catalogue (tenant_add_to_marketplace's
# `all_tenants`/`specific_tenants` targets) — "publish as a seller into the
# shared marketplace", per the tiering-capability-audit's identified gap
# (previously RBAC-only via `is_platform_admin`, no license/tier cap of any
# kind). Distinct from Cap.PRIVATE_MARKETPLACE above (hosting your OWN branded
# store) — this is publishing INTO the platform's shared one.
def _require_marketplace_publish(db: Session, current_user: models.User) -> None:
    es = get_entitlements(db, getattr(current_user, "tenant", None))
    if not evaluate_feature(Cap.MARKETPLACE_PUBLISH, es).allowed:
        raise HTTPException(
            status_code=402,
            detail={"code": "marketplace_publish_required"},
        )


# δ §17 — tenant marketplace curate (per-team visibility governance)
# ---------------------------------------------------------------------
class _TenantCurateTeamGrant(PydanticBaseModel):
    permission_id: int
    team_id: int
    team_name: str


class _TenantCurateListItem(PydanticBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    type: Optional[str] = None
    process_type: Optional[str] = None
    compute_backends: List[str] = []
    cost_in_credits: Optional[int] = None
    is_in_marketplace: bool
    # Tenant-internal marketplace lifecycle state for the caller's own node.
    review_requested: bool = False
    # Derived: "draft" (private, not submitted), "submitted" (awaiting
    # tenant-admin review), or "published" (tenant-shared + in Browse).
    status: str = "draft"
    team_grants: List[_TenantCurateTeamGrant] = []


class _TenantCurateListResponse(PydanticBaseModel):
    items: List[_TenantCurateListItem]




class _AddToMarketplaceRequest(PydanticBaseModel):
    # "this_tenant" | "all_tenants" | "specific_tenants"
    target: str = "this_tenant"
    tenant_ids: List[int] = []


class _AddToMarketplaceResponse(PydanticBaseModel):
    id: int
    target: str
    is_in_marketplace: bool
    tenant_ids: List[int] = []






class _TenantTeamGrantRequest(PydanticBaseModel):
    team_id: int


class _TenantTeamGrantResponse(PydanticBaseModel):
    permission_id: int
    team_id: int
    team_name: str
    capability_id: int
    granted_at: str


class _TenantTeamRevokeResponse(PydanticBaseModel):
    permission_id: int
    revoked_at: str


def _resolve_tenant_node_for_admin(db, *, node_definition_id, tenant_id):
    """Look up a tenant-shared NodeDef the caller's admin scope owns.

    Returns the NodeDef on success. Raises HTTPException with the same
    error semantics as `tenant_marketplace_toggle`:
    - 404 when nonexistent OR owned by another tenant (no existence leak)
    - 400 when system-shared (tenant_id IS NULL)
    - 400 when user-private (user_id IS NOT NULL) → share-with-tenant first
    """
    from services import node_definitions_shared_service as _svc
    return _svc.resolve_tenant_node_for_admin(
        db, node_definition_id=node_definition_id, tenant_id=tenant_id,
    )


def _node_definition_use_capability(db):
    from services import node_definitions_shared_service as _svc
    return _svc.node_definition_use_capability(db)






# ---------------------------------------------------------------------
# Marketplace M4 — tenant browses + downloads
# ---------------------------------------------------------------------
class _MarketplaceListItem(PydanticBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    type: str
    cost_in_credits: int
    process_type: Optional[str] = None
    is_downloaded: bool
    # True when this is the tenant's OWN published node (promoted from a
    # creator draft). It's already tenant-shared, so the UI shows
    # "Available" instead of a Download button.
    is_own_tenant_node: bool = False
    # node-monetization-finish (GA.NMF.4) — one-time price + display tier
    # (mirrors schemas.NodeDefinition), and this tenant's ownership state so
    # The store dialog can show price/owned chips without a per-id fan-out.
    price_cents: Optional[int] = None
    price_tier: Optional[str] = None
    owned: bool = False
    # node-trial-runs — True when the node carries a curated demo (sample
    # input or a curated demo workflow), so the UI can offer "Try it".
    has_demo: bool = False


class _MarketplaceListResponse(PydanticBaseModel):
    items: List[_MarketplaceListItem]


class _MarketplaceDownloadResponse(PydanticBaseModel):
    node_definition_id: int
    tenant_id: int
    granted_at: str
    already_owned: bool
    # node-ownership-purchases — set when the node is priced and unowned: the
    # caller must complete Stripe checkout; ownership is granted by the webhook.
    status: str = "granted"          # 'granted' | 'checkout'
    checkout_url: Optional[str] = None


class NodePaidListingExport(PydanticBaseModel):
    """MKT.M1a — one row of Core's platform-admin export of PRICED,
    marketplace-listed system nodes, consumed by the Marketplace's
    ``migrate_core_listings.import_core_node_listings`` to seed/refresh
    the corresponding ``Listing(kind='node', ref=id)`` rows over there.

    Only system (tenant_id IS NULL), non-deleted rows with a non-NULL
    ``price_cents`` are included — free/unpriced or tenant-owned nodes
    are out of scope for this export (see `admin_export_paid_node_listings`).

    ``currency`` is always ``"usd"``: `models.NodeDefinition` has no
    currency column (Core's one-time node-ownership price is USD-only,
    same as `models.NodePurchase.currency`'s default) — this field exists
    so the Marketplace importer's `skipped_currency` check has something
    to compare against `checkout.LAUNCH_CURRENCY` without hardcoding an
    assumption on its own side.
    """
    id: int
    name: str
    description: Optional[str] = None
    price_cents: int
    currency: str = "usd"
    version: Optional[int] = None
    is_in_marketplace: bool


# ---------------------------------------------------------------------------
# node-trial-runs — public detail, admin demo sample, discovery. Literal routes
# MUST precede the /{node_definition_id} catch-all (declared far below).
# ---------------------------------------------------------------------------

class _NodeSampleUpdate(PydanticBaseModel):
    sample_input_ref: Optional[str] = None
    sample_output_ref: Optional[str] = None
    demo_workflow_ref: Optional[str] = None










class _NodeTryRequest(PydanticBaseModel):
    data_source: str = "demo"          # 'demo' | 'own'
    input_ref: Optional[str] = None    # cog id/ref for 'own' data




# ---------------------------------------------------------------------------
# node-ownership-purchases Phase 2 — node packages (bundles). Literal /packages
# routes MUST precede the /{node_definition_id} catch-all (declared far below).
# ---------------------------------------------------------------------------

def _require_platform_admin(current_user: models.User):
    if not getattr(current_user, "is_platform_admin", False) and not _is_service_account(current_user):
        raise HTTPException(status_code=403, detail="platform admin required")


def _package_to_response(db, pkg, tenant_id) -> schemas.NodePackageResponse:
    ids = crud.node_ownership_cud.package_node_ids(db, pkg.id)
    owned = crud.node_ownership_cud.tenant_owns_all_package_nodes(db, tenant_id, pkg.id) if tenant_id else False
    return schemas.NodePackageResponse(
        id=pkg.id, name=pkg.name, description=pkg.description,
        price_cents=pkg.price_cents, price_tier=pkg.price_tier,
        node_definition_ids=ids, owned=owned,
        visibility=getattr(pkg, "visibility", "public") or "public",
        allowed_tenant_ids=list(getattr(pkg, "allowed_tenant_ids", None) or []),
    )












# NOTE: the open-core-tier-structure store-agent stub `_is_paid_marketplace_node`
# (a `custom_metadata["marketplace_paid"]` placeholder) was removed on merge with
# node-monetization — the real per-node pricing schema (NodeDefinition.price_cents
# + crud.node_ownership_cud.is_priced_marketplace_node) now owns the paid decision.






# Which classified intents trigger a source search. Deliberately BOTH — the
# intent classifier is a weak local model that errs toward `from_scratch`, and a
# wrong guess must never be the reason the wizard skips looking for a real repo.
# (Kept as a named set so the always-search decision is explicit and greppable.)
_INTENTS_THAT_SEARCH = {"wrap_repo", "from_scratch"}




class _DescribeSynthesizeRequest(PydanticBaseModel):
    description: str
    repo_url: Optional[str] = None
    repo_full_name: Optional[str] = None
    # How to install the chosen repo's code into the generated
    # Dockerfile. One of "pip" / "requirements" / "dockerfile" / "clone".
    # Populated by the client from the candidate's install_strategy that
    # /describe-search returned; falls back to legacy "try pip git+..."
    # if absent (e.g. user pasted a URL directly without going through
    # the search step).
    install_strategy: Optional[str] = None
    previous_attempt: Optional[Dict[str, Any]] = None  # {script, error}
    # Authoritative I/O contract from the wizard chat. When the
    # wizard inferred clean type tokens (via the domain-vocab Pass 0
    # or the X-to-Y regex passes in lib/describe-chat.ts), these
    # carry that decision so the synthesizer can't override it with
    # a different "more common" choice. Free-form Q&A answers stay
    # in the description text — only clean type tokens land here.
    user_input_type: Optional[str] = None
    user_output_type: Optional[str] = None
    # Critic D v2 — set True on the re-synth AFTER the user answered a
    # clarifying question, so the critic degrades a still-ambiguous verdict to
    # a warning instead of asking again (ask at most once).
    already_clarified: bool = False


class _DescribeSynthesizeResponse(PydanticBaseModel):
    ok: bool
    script_content: str = ""
    requirements: List[str] = []
    base_image_hint: str = ""
    # Phase A — the synth LLM now declares which apt packages and
    # raw Dockerfile snippets the script depends on so the binary-
    # presence validator can fail fast at synth time. These ride on
    # the response so the wizard can stash them on the BuildSpec;
    # Phase B (separate commit) wires them into the Containerizer's
    # Dockerfile composer.
    system_packages: List[str] = []
    dockerfile_extras: str = ""
    suggested_input_type: str = "other"
    suggested_output_type: str = "other"
    reasoning: str = ""
    rejection_reasons: List[str] = []
    # One of "license" / "contract" / "binary" / "other" when ok=False.
    # The wizard branches on this — license rejections re-show the
    # candidate list so the user can pick a different repo, since
    # re-running synth on the same repo will produce the same
    # failure. Other categories may benefit from a retry.
    rejection_category: Optional[str] = None
    # Self-heal retry count from the synth loop. 1 = succeeded on the
    # first LLM call; > 1 = the synth loop fed prior failures back to
    # the LLM and got a passing wrapper on a later try. Surfaced so
    # the wizard UI can show "I had to retry 2 times — usually fine,
    # but ping support if you keep seeing this" subtle UX hints.
    attempts: int = 1
    # Critic D advisory notes (non-blocking) — e.g. "may not fully match your
    # request: ...". Surfaced in the wizard's Review step as heads-ups; the
    # script is still accepted.
    warnings: List[str] = []
    # Critic D v2 — when the intent is genuinely ambiguous, the wizard should
    # ask this question in the describe chat (instead of going to Review) and
    # re-synth with the answer + already_clarified=true. ok is still true.
    needs_clarification: bool = False
    clarifying_question: str = ""
    # Provenance: name of the prior working node used to seed generation
    # (exemplar seeding). None when nothing similar was found. Surfaced in
    # Review so the user can see what the wizard learned from.
    exemplar_used: Optional[str] = None
    # "learned" (a node the user's catalog already built) vs "pinned" (a
    # curated built-in reference recipe) — lets Review phrase it honestly.
    exemplar_source: Optional[str] = None
    rate_limited: bool = False
    rate_limit_reset_seconds: Optional[int] = None


# Per-tenant rate limit on synthesis calls. Tighter than search
# because each call is a full LLM round-trip — a tenant retrying
# 50 times in a minute could starve the wizard for everyone else.
_SYNTH_LIMIT_KEY = "mod:describe:synth:rate:tenant:{tenant_id}"
_SYNTH_LIMIT_WINDOW_S = 15 * 60
# A single "Build it" fans out to several LLM calls (analyze + synthesize +
# containerize + critic + repair). The per-window budget SCALES INVERSELY WITH
# MODEL SIZE, never hardcoded: a small model needs more retries and each call is
# cheap, so it gets a big budget; a large model succeeds first-try and costs
# more per call, so it gets a tight one. Derived from the serving model's VRAM
# floor. Operators shift the whole curve with these env vars.
_SYNTH_RATE_BASE = float(os.getenv("NODE_SYNTH_RATE_BASE_GB_CALLS", "200"))
_SYNTH_RATE_FLOOR = int(os.getenv("NODE_SYNTH_RATE_FLOOR", "8"))
_SYNTH_RATE_CEIL = int(os.getenv("NODE_SYNTH_RATE_CEIL", "60"))


def _synth_call_budget() -> int:
    """Per-window synth call budget, scaled inversely with model VRAM (shared
    util) and clamped. Small model → bigger budget (retries); large → tight."""
    from utils.model_size import scaled_by_inverse_vram
    return scaled_by_inverse_vram(
        _SYNTH_RATE_BASE, _SYNTH_RATE_FLOOR, _SYNTH_RATE_CEIL, tool="wizard")


def _check_and_inc_synth_rate(tenant_id: int) -> Dict[str, Any]:
    budget = _synth_call_budget()
    try:
        client = _redis_for_describe()
        key = _SYNTH_LIMIT_KEY.format(tenant_id=tenant_id)
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        if int(count) == 1 or int(ttl) < 0:
            client.expire(key, _SYNTH_LIMIT_WINDOW_S)
            ttl = _SYNTH_LIMIT_WINDOW_S
        if int(count) > budget:
            return {"allowed": False, "remaining": 0, "reset_seconds": int(ttl)}
        return {
            "allowed": True,
            "remaining": budget - int(count),
            "reset_seconds": int(ttl),
        }
    except Exception as e:
        logger.warning("describe-synth rate-limit check failed (fail-open): %s", e)
        return {"allowed": True, "remaining": -1, "reset_seconds": -1}






class _DescribeRewriteRequest(PydanticBaseModel):
    prompt_key: str
    canonical_text: str
    description: str
    chosen_repo: Optional[str] = None
    input_answer: Optional[str] = None
    output_answer: Optional[str] = None


class _DescribeRewriteResponse(PydanticBaseModel):
    ok: bool
    text: Optional[str] = None




class _DescribeStatsResponse(PydanticBaseModel):
    top_repos: List[Dict[str, Any]]
    total_searches_24h: int
    total_synthesis_24h: int
    cache_hit_ratio: Optional[float] = None
    rate_limited_calls_24h: int = 0




class _CheckSourceLicenseRequest(PydanticBaseModel):
    files: List[Dict[str, Any]]  # [{filename, body}, ...]


class _CheckSourceLicenseResponse(PydanticBaseModel):
    ok: bool
    blocking_files: List[Dict[str, Any]] = []
    detected_licenses: Dict[str, Optional[str]] = {}




class _CheckRepoSafetyRequest(PydanticBaseModel):
    repo_url: str


class _CheckRepoSafetyResponse(PydanticBaseModel):
    ok: bool
    reasons: List[str] = []
    warnings: List[str] = []
    license_spdx: Optional[str] = None
    full_name: Optional[str] = None
    description: Optional[str] = None
    not_github: bool = False
    not_configured: bool = False






def _check_host_docker_disk() -> Optional[Dict[str, Any]]:
    """Return a {free_gb, min_gb, message} dict when the docker host
    is below 30 GB free, else None.

    The API container has /var/run/docker.sock mounted, so it can ask
    the docker daemon for its root dir, then statvfs that path. Path
    is inside the API container's view of the filesystem (the docker
    root is bind-mounted into the API for compose deployments), but
    even when it's not, the same partition usually applies.
    """
    MIN_GB = 30
    try:
        import subprocess as _sub
        info = _sub.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True, text=True, timeout=5,
        )
        docker_root = (info.stdout or "/var/lib/docker").strip() or "/var/lib/docker"
        stat = os.statvfs(docker_root)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < MIN_GB:
            return {
                "free_gb": round(free_gb, 1),
                "min_gb": MIN_GB,
                "message": (
                    f"Only {free_gb:.1f} GB free on the docker root "
                    f"({docker_root}); need at least {MIN_GB} GB to "
                    f"build a wizard node reliably. Multi-GB image "
                    f"builds (CUDA, ML stacks) will fail with "
                    f"misleading errors when the daemon runs low on "
                    f"disk. Free space with `docker system prune -a "
                    f"--volumes` before starting."
                ),
            }
    except Exception:
        # Fail-open: rather show "no banner" than crash the wizard on
        # a faulty disk check.
        return None
    return None


def _is_saas():
    import os
    return os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas"


# =============================================================================
# Tier 4 follow-up: persistent remediation memory
# =============================================================================
# The imagebuilder writes here after a successful LLM-suggested fix and
# reads from here BEFORE invoking the LLM. Service-to-service auth
# (require_bearer_or_api_key) so the imagebuilder's API_KEY is enough;
# the table is platform-wide knowledge, not tenant data.

class _RemediationLookupResponse(PydanticBaseModel):
    diff: Optional[str] = None
    diff_hash: Optional[str] = None
    success_count: int = 0
    failure_count: int = 0
    model: Optional[str] = None
    # Tier-A-semantic fields. ``match_kind`` is "exact" when the
    # signature hash matched verbatim, "semantic" when we found a
    # paraphrased prior error via vector similarity. ``similarity``
    # is the cosine score on semantic hits (0.0-1.0); None on exact
    # hits and misses.
    match_kind: Optional[str] = None
    similarity: Optional[float] = None
    # Tier-B promotion state. ``promoted`` is True when this
    # remediation has cleared the trust threshold; the imagebuilder
    # uses this to skip the LLM loop entirely (the cached fix is
    # canonical, no fallback needed). ``trust`` is a 0-1 score
    # (success / (success + failure)) for ranking displays.
    promoted: bool = False
    trust: float = 0.0
    # Negative cache: fixes that have been tried for this EXACT error
    # signature and failed (success_count==0, failure_count>=1). The
    # build-task loop feeds these to the repair LLM as "do not propose
    # these again", and skips re-applying them. Each entry is
    # {diff_hash, excerpt}. Capped + ranked by failure_count desc.
    known_bad: List[Dict[str, str]] = Field(default_factory=list)


class _RemediationRecordRequest(PydanticBaseModel):
    error_signature: str
    error_sample: Optional[str] = None
    # The full normalised error text — needed for semantic embedding
    # so paraphrased future errors collide on similarity. Optional
    # for back-compat with imagebuilders that predate the field.
    error_text: Optional[str] = None
    diff: str
    diff_hash: str
    model: Optional[str] = None


class _RemediationFailureRequest(PydanticBaseModel):
    error_signature: str
    diff_hash: str


class _RemediationFailedAttemptRequest(PydanticBaseModel):
    """Negative-cache record: a fix was tried for this error and failed,
    even though it was never a recorded success. Unlike
    ``_RemediationFailureRequest`` (which only bumps an existing row),
    this CREATES a row (success_count=0) when the (sig, diff) pair is
    new, so the failed fix is remembered and surfaced in ``known_bad``."""
    error_signature: str
    diff: str
    diff_hash: str
    error_sample: Optional[str] = None


# Tier-B promotion thresholds. A fix gets promoted (skip-LLM canonical
# status) when it has clean track record:
#   * success_count >= PROMOTION_MIN_SUCCESSES, AND
#   * failure_count <= PROMOTION_MAX_FAILURES
# Conservative defaults — better to delay promotion than to canonise
# a flaky fix. Override per-deployment via env if needed.
PROMOTION_MIN_SUCCESSES = int(os.getenv("MOD_REMEDIATION_PROMOTION_MIN_SUCCESSES", "10"))
PROMOTION_MAX_FAILURES = int(os.getenv("MOD_REMEDIATION_PROMOTION_MAX_FAILURES", "1"))


def _trust_score(success: int, failure: int) -> float:
    """Simple trust ratio. 0.0 means everything failed; 1.0 means a
    perfect track record. New entries with success=1 / failure=0
    score 1.0 — fine because rank() in lookup also breaks ties on
    success_count, and a single-replay fix isn't promoted yet."""
    total = success + failure
    if total <= 0:
        return 0.0
    return float(success) / float(total)


def _bad_federation_peers(db: Session) -> set:
    """Foolproof #7 — return the set of peer URLs whose aggregate
    failure_count exceeds success_count by a margin. A peer that's
    consistently shipping bad shares gets auto-dropped from lookups
    until an operator vets the situation.

    Threshold: a peer with at least 5 entries AND failure-rate >= 0.5
    is considered bad. Tunable via env if needed; conservative
    defaults so a single transient share-failure doesn't ban a
    legitimate peer.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.bad_federation_peers(db)


def _maybe_promote(row: "models.BuildRemediation", now) -> None:
    """Set ``row.promoted_at`` when the entry crosses the promotion
    thresholds. Idempotent — once promoted_at is set, never clear.
    Caller is responsible for db.commit().

    Tier-E: every fresh promotion fires a federation push (best-
    effort, fail-open). Existing promoted rows don't re-push on
    every replay — only the first transition from "not promoted"
    to "promoted" triggers the share.
    """
    if row.promoted_at is not None:
        return
    if (
        row.success_count >= PROMOTION_MIN_SUCCESSES
        and row.failure_count <= PROMOTION_MAX_FAILURES
    ):
        row.promoted_at = now
        # Best-effort federation share. Errors swallowed so a peer
        # outage doesn't block local promotion.
        try:
            from utils.federation import is_enabled, push_share, share_payload_for
            if is_enabled():
                payload = share_payload_for(
                    error_signature=row.error_signature,
                    diff_hash=row.diff_hash,
                    diff=row.diff,
                    success_count=row.success_count,
                    failure_count=row.failure_count,
                    promoted_at_source=True,
                    error_sample=row.error_sample,
                )
                push_share(payload)
        except Exception as e:
            logger.info("federation push on promotion failed (non-fatal): %s", e)










class _FederationShareRequest(PydanticBaseModel):
    error_signature: str
    diff_hash: str
    success_count: int = 1
    failure_count: int = 0
    promoted_at_source: bool = False
    diff: Optional[str] = None  # Only present when source peer shares full
    error_sample: Optional[str] = None




class _BuiltImageCacheLookupResponse(PydanticBaseModel):
    image_digest: Optional[str] = None
    source_registry: Optional[str] = None
    hit_count: int = 0


class _BuiltImageCacheRecordRequest(PydanticBaseModel):
    source_hash: str
    image_name: str
    variant: str = "hero"
    image_digest: str
    source_registry: str






class _FinalizeRegistrationRequest(PydanticBaseModel):
    build_task_id: int
    approved: bool
    decline_reason: Optional[str] = None


def _extract_deferred_registration(log_tail: str) -> Optional[Dict[str, Any]]:
    """γ — pull the imagebuilder's awaiting_user_approval BUILD_RESULT_JSON
    line out of a build task's log_tail. Returns the parsed dict (with
    sample_test_result + node_config + image) or None when the build
    didn't defer.

    The imagebuilder logs ONE line of shape
    ``BUILD_RESULT_JSON: {... "status": "awaiting_user_approval" ...}``
    when DEFER_REGISTRATION=1 and the sample-test passed. We parse
    every BUILD_RESULT_JSON line in the log and return the first one
    whose status is ``awaiting_user_approval``.
    """
    if not log_tail or "awaiting_user_approval" not in log_tail:
        return None
    for match in re.finditer(
        r"^BUILD_RESULT_JSON:\s*(\{.*?\})\s*$",
        log_tail,
        re.MULTILINE,
    ):
        try:
            parsed = json.loads(match.group(1))
        except Exception:
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("status") == "awaiting_user_approval"
        ):
            return parsed
    return None


def _extract_sample_test_failure(log_tail: str) -> Optional[Dict[str, Any]]:
    """γ — pull the sample-test-failed BUILD_RESULT_JSON line out of a
    build task's log_tail. Returns the parsed dict or None.
    """
    if not log_tail or "sample_test_failed" not in log_tail:
        return None
    for match in re.finditer(
        r"^BUILD_RESULT_JSON:\s*(\{.*?\})\s*$",
        log_tail,
        re.MULTILINE,
    ):
        try:
            parsed = json.loads(match.group(1))
        except Exception:
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("error_class") == "sample_test_failed"
        ):
            return parsed
    return None








class _NodeFeedbackRequest(PydanticBaseModel):
    node_definition_id: int
    is_broken: bool
    reason: Optional[str] = None








def _index_remediation_embedding(remediation_id: int, error_text: str) -> None:
    """Compute embedding + upsert into qdrant. Best-effort — never
    raises. Decoupled into its own helper so the record endpoint
    stays clean and tests can monkeypatch it cheaply."""
    try:
        from utils.remediation_vector_store import (
            embed_error, upsert_remediation_vector,
        )
        vec = embed_error(error_text)
        if vec:
            upsert_remediation_vector(remediation_id, vec)
    except Exception as e:
        logger.info("remediation vector index failed (non-fatal): %s", e)


def _assert_node_registration_affordable(
    db: Session,
    current_user: "models.User",
) -> None:
    """Pre-flight credit check. Raises 402 if the tenant can't afford
    the upcoming registration charge; returns None silently otherwise.

    Wizard flow today charges NODE_CREATION_CREDIT_COST twice:
      1. /generate-artifacts — when the LLM runs (covers AI cost)
      2. POST /node_definitions/ at the end of the build (the
         "register" step calls back via the build-task container)

    The wizard-mount /wizard-preflight endpoint only checks for ONE
    cost. After the user pays the first 100 for the LLM, their
    balance can drop below 100, and the build runs to completion
    before failing at the very last step with 402 — wasting both
    the user's compute time and our Harbor bandwidth.

    This helper is the missing gate. /build-and-register-node calls
    it BEFORE persisting the BuildSpec or dispatching the workflow,
    so the user gets told "you need 100 more credits, you have N"
    at click time, not 5–10 minutes later.

    Mirrors _charge_node_creation_credits's skip logic exactly so
    the gate fires only when the deduction would.
    """
    if not _credits_enabled(db):
        return
    if _is_service_account(current_user):
        return
    if getattr(current_user, "is_platform_admin", False):
        return
    if current_user.tenant_id is None:
        return
    try:
        from utils.credit_manager import CreditManager
        balance = CreditManager.get_credit_balance(db, current_user.tenant_id)
    except Exception as e:
        # Fail-open: the real /charge step still gates, so a transient
        # credit-manager failure here shouldn't block the build. We'd
        # rather over-admit than over-reject on a flaky lookup.
        logger.warning(
            "Pre-flight credit check skipped (lookup failed): %s", e,
        )
        return
    # OWNER-DECISIONS 09-29 — the balance CHECK is plan + purchased pools
    # combined; a tenant can pay the build out of purchased (top-up) credits.
    remaining = int((balance or {}).get("credits_remaining") or 0) + int(
        (balance or {}).get("purchased_credits_remaining") or 0
    )
    cost = NODE_CREATION_CREDIT_COST
    if remaining < cost:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient credits to complete the build. The "
                f"register-node step at the end of the build will "
                f"cost {cost} credits, but the tenant currently has "
                f"{remaining}. Top up credits and re-submit. "
                f"(Note: the wizard's earlier /generate-artifacts step "
                f"has already charged {cost} credits separately for "
                f"the LLM call — wizard builds cost {cost * 2} total.)"
            ),
        )


def _charge_node_creation_credits(
    db: Session,
    current_user: "models.User",
    node_name: str,
) -> None:
    """Deduct NODE_CREATION_CREDIT_COST credits from the tenant or raise
    402 Payment Required. Idempotent is NOT guaranteed by this helper —
    callers must ensure they only invoke it once per logical node creation
    (e.g. at imagebuilder task enqueue, not on repeated retries). Returns
    None on success, raises HTTPException(402) on insufficient balance."""
    if not _credits_enabled(db):
        return
    # System nodes (tenant_id IS NULL on the row being created) — usually
    # dispatched by api_service — are free. Platform admins are also exempt
    # so they can seed tenants without burning credits.
    if _is_service_account(current_user):
        return
    if getattr(current_user, "is_platform_admin", False):
        return
    if current_user.tenant_id is None:
        return
    # CARVE-U4 (CE carve, CRASH class) — utils/credit_manager.py is manifest-
    # denied (open-core/manifest.yaml :1379), so the bare import above raised
    # ModuleNotFoundError -> 500 on the CE image. Load it the absent-tolerant
    # way this repo already uses (utils.paid_import.optional_paid): CE has no
    # credit ledger, so there is nothing to charge -> skip (the same posture as
    # the `_credits_enabled` kill-switch branch above). A non-absent ImportError
    # still re-raises (optional_paid only swallows the module itself).
    # E30 contract holds: this file never names routers.ai_config in a paid-
    # import call literal (tests/test_p2_6b_node_authoring_ce.py:89).
    from utils.paid_import import optional_paid
    credit_manager_mod = optional_paid("utils.credit_manager")
    if credit_manager_mod is None:
        return  # CE: no credit system -> node creation is free
    CreditManager = credit_manager_mod.CreditManager
    InsufficientCreditsError = credit_manager_mod.InsufficientCreditsError
    try:
        success, error = CreditManager.deduct_credits(
            db=db,
            tenant_id=current_user.tenant_id,
            amount=NODE_CREATION_CREDIT_COST,
            user_id=current_user.id,
            notes=f"Custom node creation: {node_name}",
            allow_advance=False,
        )
    except InsufficientCreditsError as e:
        # deduct_credits raises this when tenant has no payment method and
        # advance is disallowed; the other "no balance" branch returns
        # (False, error_msg). Treat both the same way.
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient credits. Creating a custom node costs "
                f"{NODE_CREATION_CREDIT_COST} credits. {e}"
            ),
        )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient credits. Creating a custom node costs "
                f"{NODE_CREATION_CREDIT_COST} credits. {error or ''}"
            ),
        )


def _effective_billing_user(
    db: Session, current_user: "models.User", build_spec_id: Optional[int],
) -> "models.User":
    """The principal to bill for a node creation.

    Build-task register callbacks authenticate as the platform service account
    (so `current_user` is api@service.local, not the person who started the
    build). The real billing principal is the build's TRIGGERING user — so an
    admin-initiated build is free and a regular tenant is charged per §22.5.
    Resolve them from the originating BuildSpec → WorkflowRun.triggered_by_user_id.
    Direct (non-build) creates by a real user bill that user unchanged.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.effective_billing_user(db, current_user, build_spec_id)


def _effective_creator_user_id(
    db: Session, current_user: "models.User", build_spec_id: Optional[int],
) -> Optional[int]:
    """The user_id a freshly-registered node should be private to (its builder).

    New nodes land creator-private (draft) so only the person who built them
    sees them until a tenant admin reviews + publishes. The builder is the
    build's TRIGGERING user (resolved from BuildSpec -> WorkflowRun), not the
    service account that runs the register callback. If we can't attribute the
    node to a real human (no build_spec linkage yet — e.g. before the
    build-task image ships build_spec_id), fall back to None so the node lands
    tenant-shared, preserving today's behavior instead of breaking.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.effective_creator_user_id(db, current_user, build_spec_id)


def _image_is_in_tenant_harbor(image_path: str) -> bool:
    """True when an image_path points at our tenant Harbor (either the
    external subdomain or the compose-internal core alias). Admission
    routes these through Harbor's built-in Trivy instead of dispatching
    MOD's own TrivyScanner — Harbor scans every push automatically.
    """
    import os
    if not image_path:
        return False
    ext_url = (os.getenv("TENANT_HARBOR_EXTERNAL_URL") or "").rstrip("/")
    subdomain = os.getenv("TENANT_HARBOR_SUBDOMAIN") or ""
    base_domain = os.getenv("BASE_DOMAIN") or ""
    candidates = []
    if ext_url:
        # Strip scheme for substring-match against bare image refs.
        candidates.append(ext_url.split("://", 1)[-1])
    if subdomain and base_domain:
        # A LAN install's BASE_DOMAIN is an address (`10.0.0.1:3000`), not a
        # registry name — composing it names a host that resolves nowhere, and
        # a substring match against it could claim a stranger's image is ours.
        # Same rule as utils/registries._registry_base_name, kept inline so this
        # helper stays self-contained (test_tenant_harbor.py execs its source).
        base = base_domain.split("://", 1)[-1].split("/", 1)[0]
        head, sep, tail = base.partition(":")
        if sep and tail.isdigit():
            base = head
        if "." in base and not base[:1].isdigit():
            candidates.append(f"{subdomain}.{base}")
    candidates.append("tenant-harbor-core")  # in-compose alias
    return any(host in image_path for host in candidates if host)


def _parse_tenant_harbor_ref(image_path: str) -> tuple[Optional[str], Optional[str]]:
    """Extract ``(repository, reference)`` from a fully-qualified tenant
    Harbor image path like
    ``tenant-registry.example.com/tenant-7/blip@sha256:abc…`` or
    ``tenant-registry.example.com/tenant-7/blip:latest-cuda``.

    Returns the project/repo (``tenant-7/blip``) and the reference
    (``sha256:abc…`` or ``latest-cuda``). Either may be None when the
    path doesn't follow Harbor's project/repo convention.
    """
    if not image_path or "/" not in image_path:
        return None, None
    # Drop the registry host.
    without_host = image_path.split("/", 1)[1]
    # Harbor wants project/repo for its API; keep the first two segments.
    if "@" in without_host:
        repo, reference = without_host.split("@", 1)
    elif ":" in without_host.rsplit("/", 1)[-1]:
        head, tail = without_host.rsplit(":", 1)
        repo, reference = head, tail
    else:
        repo, reference = without_host, "latest"
    return repo, reference


def _decide_harbor_scan_outcome(scan_overview_raw):
    """Map a Harbor ``scan_overview`` payload to ``(outcome, detail)``.

    Pure function — no DB, no Harbor calls, no logging. Both the eager
    daemon-thread poller and the scheduler sweep call into this so the
    decision rule is single-sourced.

    Returns:
      - ``(outcome, detail)`` when the Harbor scan reached a terminal state.
        ``outcome`` is one of {``clean``, ``high_vulns``, ``critical_vulns``,
        ``scan_failed``}.
      - ``(None, None)`` when Harbor's scan is still pending / running /
        unknown — caller should keep waiting.
    """
    scan = (scan_overview_raw or {}).get("scan_overview", {}) or {}
    # Harbor returns a dict keyed by scanner MIME type — collapse it.
    if scan and isinstance(next(iter(scan.values()), None), dict):
        scan = next(iter(scan.values()))

    status_str = (scan.get("scan_status") or "").lower()
    if status_str == "success":
        summary = scan.get("summary", {}) or {}
        counts = (summary.get("summary") or {}) if isinstance(summary.get("summary"), dict) else summary
        critical = int(counts.get("Critical") or counts.get("CRITICAL") or 0)
        high = int(counts.get("High") or counts.get("HIGH") or 0)
        fixable = int(scan.get("fixable", 0) or 0)
        if critical > 0 and fixable > 0:
            return "critical_vulns", f"Critical vulnerabilities: {critical} (fixable={fixable})"
        if critical > 0:
            # Not fixable → warn only, admit to library.
            return "high_vulns", f"Critical unfixable: {critical}; High: {high}"
        if high > 0:
            return "high_vulns", f"High: {high}"
        return "clean", None
    if status_str == "failed":
        return "scan_failed", str(scan.get("summary", "Harbor scan failed"))
    # Pending / Running / Unknown — keep polling.
    return None, None


def _persist_scan_outcome(node_id: int, outcome: str, detail: Optional[str]) -> None:
    """Write a Harbor scan outcome to the NodeDefinition row.

    Module-level so the scheduler sweep can call it too (not just the
    daemon-thread poller). Idempotent — re-writing the same outcome is
    a cheap no-op committed UPDATE.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.persist_scan_outcome(node_id, outcome, detail)


def _poll_harbor_scan_async(
    node_def_id: int,
    tenant_id: int,
    repo: str,
    reference: str,
    timeout_s: int = 300,
    interval_s: int = 5,
) -> None:
    """Poll tenant Harbor for a scan result in a background thread.

    Eager path — fast resolution when everything works. The thread is a
    `daemon=True` so it won't block process exit. If the thread dies
    silently (e.g. the kicker was a short-lived script that exited
    before the first poll), the scheduler-driven sweep
    `_run_sweep_stuck_harbor_scans` in services/scheduler.py is the
    recovery mechanism — it picks up any row stuck at
    ``scanning``/``pending_scan`` and pushes it to terminal.

    Blocks until scan_status is Success/Failed or the timeout elapses,
    then updates the NodeDefinition row: ``scan_status`` becomes one of
    ``clean`` / ``high_vulns`` / ``critical_vulns`` / ``scan_failed``.

    Decision logic lives in module-level ``_decide_harbor_scan_outcome``
    so the scheduler sweep uses the same rule.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.poll_harbor_scan_async(
        node_def_id, tenant_id, repo, reference,
        timeout_s=timeout_s, interval_s=interval_s,
    )


def _clear_soft_deletes_for_node_refs(
    db: Session,
    tenant_id: Optional[int],
    node_def: "models.NodeDefinition",
) -> int:
    """Delete any active `tenant_image_deletions` rows whose
    (repository, tag) matches an image ref this node uses.

    Called on node (re-)registration. A user who soft-deleted an
    image via Tenant Registry and then rebuilds the corresponding
    node has signaled, by the rebuild itself, that they want the
    image back. Leaving the soft-delete row in place would:

      * keep the image hidden in the Tenant Registry listing
        (`/harbor/tenant-images` filters via `_active_soft_delete_keys`);
      * keep the node hidden in the Workflow Builder palette
        (`crud._hide_nodes_with_soft_deleted_images` matches on
        executable_path / gpu_variants tails);

    even though the image is freshly pushed and the user clearly
    wants both back. The reaper already skips reaped rows, so the
    only viable "auto-restore" is to drop the active soft-delete
    on re-register.

    Returns the count of rows cleared. System nodes (tenant_id IS
    NULL) are a no-op — they don't live in a tenant Harbor project.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.clear_soft_deletes_for_node_refs(db, tenant_id, node_def)


def _dispatch_trivy_scan(db: Session, node_def_id: int, image_path: str, tenant_id: int) -> int:
    """Dispatch a Trivy security scan for a node's container image.

    Tenant Harbor images are scanned automatically on push by Harbor's
    built-in Trivy; for those we spawn a background poll that updates
    ``scan_status`` once Harbor finishes. Platform / system images that
    live in ``ghcr.io/modtim`` (or the legacy local
    registry) still go through MOD's TrivyScanner worker task — that
    code path is retained so the rescan button keeps working and so
    anything built pre-Harbor-migration can still be re-scanned on
    demand.

    Returns 0 for the Harbor-polled path (no task row created) or the
    dispatched task_id for the legacy path.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.dispatch_trivy_scan(db, node_def_id, image_path, tenant_id)


SIZE_TIERS = ["small", "medium", "large"]
CALIBRATION_RUNS_PER_TIER = 3
# Bucket E — synthetic stressor catalog. Five corners of the resource
# consumption space (cpu / io / gpu-compute / vram-bandwidth / mixed).
# The contention training data the model trains on captures generalised
# resource pressure rather than node-pair memorisation.
CALIBRATION_STRESSORS = [
    "stressor-cpu-multi",
    "stressor-io",
    "stressor-gpu-compute",
    "stressor-vram-bandwidth",
    "stressor-mixed",
]
PAIRWISE_RUNS_PER_TIER = 3       # 5 stressors × 3 tiers × 3 runs = 45
MULTI_STRESSOR_PAIRS = [          # 10 unordered pairs of distinct stressors
    (a, b)
    for i, a in enumerate(CALIBRATION_STRESSORS)
    for b in CALIBRATION_STRESSORS[i + 1:]
]
# 10 pairs × 3 tiers × 1 run = 30 tasks
# Total per node = 9 isolation + 45 pairwise + 30 multi-stressor = 84.
TOTAL_CALIBRATION_TASKS = (
    len(SIZE_TIERS) * CALIBRATION_RUNS_PER_TIER
    + len(CALIBRATION_STRESSORS) * len(SIZE_TIERS) * PAIRWISE_RUNS_PER_TIER
    + len(MULTI_STRESSOR_PAIRS) * len(SIZE_TIERS) * 1
)


def _calibration_node_id(node_def_id, run_kind, tier, run, stressor_kinds=None):
    """Stable, unique node_id for one calibration task.

    The stressor set is part of the identity: multi-stressor runs are
    10 pairs x 3 tiers x 1 run, so tier+run alone collides 10 ways.
    Isolation runs name no stressor and keep the original short form.
    """
    base = f"calibrate-{node_def_id}-{run_kind}-{tier}-{run}"
    if not stressor_kinds:
        return base
    return f"{base}-{'+'.join(sorted(stressor_kinds))}"


def _enqueue_calibration_task(db, node_def, tenant_id, *, tier, run, stressor_kinds=None,
                               run_kind="isolation", calibration_pair_id=None):
    """Build one ApiTaskQueue row for a calibration task.

    `run_kind` ∈ {"isolation", "pairwise", "multi"}. `stressor_kinds` is the
    list of concurrently-running stressor image refs (empty for isolation).
    `calibration_pair_id` ties together rows that should be treated as the
    same physical co-located run for pair-aware logging — both the focal
    node's row AND each stressor row carry the same id so the worker logs
    both sides as concurrent samples in TaskResourceSample.
    """
    stressor_kinds = list(stressor_kinds or [])
    # output_namespace column is VARCHAR(50) — abbreviate stressor names
    # to two-letter codes to stay under the limit even for the worst-case
    # multi-stressor run.
    short = {"stressor-cpu-multi": "cm", "stressor-io": "io",
             "stressor-gpu-compute": "gc", "stressor-vram-bandwidth": "vb",
             "stressor-mixed": "mx"}
    namespace = f"cal_{run_kind[:3]}_{tier[:3]}_{run}"
    if stressor_kinds:
        namespace += "_" + "".join(short.get(s, s[:2]) for s in stressor_kinds)
    source_file_location, file_extension = _calibration_input_for_node(node_def, tier)
    # Belt-and-suspenders: when there's no input file for this node
    # (formatless source nodes), set skip_download=true so the worker
    # doesn't even pull the minio-client image. The worker already
    # short-circuits on empty source_file_location, but the
    # `needsMinio := !SkipDownload || !SkipUpload` gate still pulls
    # minio-client when MinioBucket is set, costing a few seconds
    # per task for no reason.
    skip_download = not source_file_location
    task_payload: Dict[str, Any] = {
        "node_kind": node_def.node_kind,
        "processing_image": ce_mirror_ref(_qualify_image_ref(node_def.executable_path, node_def)),
        "calibration_run": True,
        "calibration_run_kind": run_kind,
        "calibration_pair_id": calibration_pair_id,
        "stressor_kinds": stressor_kinds,
        "size_tier": tier,
        "node_definition_id": node_def.id,
        "source_file_location": source_file_location,
        "file_extension": file_extension,
        "minio_bucket": get_tenant_bucket_name(tenant_id, db),
        "gpu_required": (node_def.process_type or "CPU").upper() != "CPU",
        "container_env": {
            "CALIBRATION_RUN": "true",
            "SIZE_TIER": tier,
            "CALIBRATION_RUN_KIND": run_kind,
            "STRESSOR_KINDS": ",".join(stressor_kinds),
        },
    }
    if skip_download:
        task_payload["skip_download"] = True
    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        # Include the stressor set in the id. Without it the 30 multi-stressor
        # runs collide: they are 10 stressor-pairs x 3 tiers x 1 run, and the
        # id was built from tier and run index only — so all 10 pairs at a
        # given tier shared e.g. "calibrate-98-multi-small-0", making per-task
        # tracing ambiguous (10 rows, one id).
        node_id=_calibration_node_id(node_def.id, run_kind, tier, run, stressor_kinds),
        tenant_id=tenant_id,
        queue_name=node_def.process_type or "CPU",
        priority=2,
        priority_class="low",
        task_payload=task_payload,
        status=models.ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace=namespace,
    )
    db.add(task)
    db.flush()
    return task


def _calibration_input_for_node(node_def: models.NodeDefinition, tier: str) -> tuple[str, str]:
    """Derive (source_file_location, file_extension) for a calibration task.

    The minio-client sidecar runs `mc cp myminio/{bucket}/{source_file_location}
    /data/input/{name}.{ext}` (executor.go:490). Two requirements:
      1. `source_file_location` MUST point at a single object (not a
         prefix) — without `--recursive`, `mc cp` rejects directories.
      2. `file_extension` MUST be set so the processing container's
         input file has the right suffix.

    This helper picks the first entry from `node_def.accepted_formats`
    (jsonb list of accepted extensions like `["png", "jpg", ...]`) and
    builds `calibration-data/{tier}/sample.{ext}`. The fixture file at
    that path needs to be seeded in `tenant-{tenant_id}-files` ahead of
    the calibration run.

    Returns:
      - (`calibration-data/{tier}/sample.{ext}`, ext) when accepted_formats
        is non-empty
      - ("", "") when the node lists no accepted_formats. The worker's
        executor short-circuits the download sidecar when
        SourceFileLocation == "", so the calibration task runs without
        attempting an mc download. (Previously this branch returned
        `("calibration-data/{tier}", "")` — a prefix path — and the
        sidecar then crashed with `mc cp ... To copy or move <prefix>
        the --recursive flag is required` because mc can't copy a
        directory without --recursive. 5,131 calibration task failures
        on the running plan all traced to this single line.)
    """
    formats = node_def.accepted_formats or []
    if not formats:
        return "", ""
    ext = str(formats[0]).lstrip(".").lower()
    return f"calibration-data/{tier}/sample.{ext}", ext


def _qualify_image_ref(
    image_ref: Optional[str],
    node_def: Optional[models.NodeDefinition] = None,
    registry: Optional[str] = None,
) -> Optional[str]:
    """Prepend the right registry path to a bare image short name.

    Most `node_definitions.executable_path` values are short tags like
    `image-converter:latest` (no registry, no namespace). The Go worker's
    `resolveProcessingImage` uses payload.processing_image verbatim when
    set, which means a bare short name resolves against `docker.io` and
    fails the pull. Calibration dispatch needs to write a fully-qualified
    ref so the worker can pull from the right registry.

    Three target registries are recognised by the platform admission
    allowlist:
      - `ghcr.io/modtim/`  — system nodes (Mod Core
        catalog). System rows have `user_id IS NULL`. Per-tenant copies
        of system rows (provisioning clones) also have `user_id IS NULL`,
        so the system catalog is the right target for them too.
      - `registry:5000/tenant-{id}/`        — user-built custom nodes on
        on-prem deploys (`DEPLOYMENT_MODE=onprem`).
      - `example.com/tenant-{id}/` — user-built custom
        nodes on SaaS (`DEPLOYMENT_MODE=saas`).

    The user_id discriminator: system rows AND inherited per-tenant
    copies of system rows have `user_id IS NULL`; only the wizard's
    build flow (or a tenant admin's manual create) sets a non-null
    user_id. So `user_id IS NOT NULL` is the right "this is a user-
    built custom node" signal.

    Returns:
      - empty/None → returned unchanged (worker falls through to its
        own `harborReg + "/modtim/" + node_kind` default at
        executor.go:173).
      - already qualified (any `/` in the ref) → returned unchanged.
        Covers Harbor refs, internal `registry:5000/tenant-N/...`,
        and any other registry-prefixed form.
      - bare + node_def is None or node_def.user_id IS NULL → system
        catalog: `{registry}/modtim/{image_ref}`.
      - bare + node_def.user_id IS NOT NULL → user-built tenant node:
        `{tenant_registry}/tenant-{tenant_id}/{image_ref}`. The
        tenant registry comes from `LOCAL_REGISTRY` (default
        `registry:5000`) on on-prem, or `TENANT_HARBOR_REGISTRY`
        (default `example.com`) on SaaS, picked
        from `DEPLOYMENT_MODE`.
    """
    if not image_ref:
        return image_ref
    if "/" in image_ref:
        return image_ref
    if node_def is not None and node_def.user_id is not None:
        deployment_mode = (os.getenv("DEPLOYMENT_MODE") or "").lower()
        if deployment_mode == "saas":
            tenant_reg = os.getenv("TENANT_HARBOR_REGISTRY", "example.com")
        else:
            tenant_reg = tenant_registry("onprem")
        tenant_id = node_def.tenant_id
        if tenant_id is not None and tenant_reg:
            return f"{tenant_reg}/tenant-{tenant_id}/{image_ref}"
        if tenant_id is not None:
            # No tenant registry on this deployment (CE LAN: BASE_DOMAIN is an
            # IP, no registry container). The image the wizard just built sits
            # on the building machine's own daemon, so the honest ref is the
            # bare tag — never `/tenant-7/...`, which is not a valid ref.
            return image_ref
        # User_id set but no tenant_id — unusual; fall through to system
        # catalog rather than emit a malformed `tenant-None/` segment.
    if registry is None:
        registry = platform_registry()
    return f"{registry}/modtim/{image_ref}"


def _auto_calibrate_new_node_hook(db: Session, new_def: models.NodeDefinition,
                                   current_user, effective_tenant_id: Optional[int]) -> None:
    """AI scheduler — auto-queue contention calibration for a freshly-
    created NodeDefinition. Best-effort; never raises into the caller so
    a calibration-dispatch glitch can't fail the node-creation request.

    Gates:
      - Disabled via `AUTO_CALIBRATE_NEW_NODES=false`.
      - Capacity-floor aware (handled by the dispatcher).

    Tenant resolution mirrors the worker-side hook:
    `effective_tenant_id` (the node's owning tenant) → creating user's
    tenant → first active tenant fallback. The chosen tenant is recorded
    on the audit event regardless of dispatch outcome.
    """
    from services import node_definitions_shared_service as _svc
    return _svc.auto_calibrate_new_node_hook(db, new_def, current_user, effective_tenant_id)


def _dispatch_calibration_tasks(db: Session, node_def: models.NodeDefinition, tenant_id: int) -> list:
    """Dispatch the full 84-task calibration matrix for a node definition.

    Matrix:
      - 9  isolation runs   (3 tiers × 3 runs)
      - 45 pairwise runs    (5 stressors × 3 tiers × 3 runs)
      - 30 multi-stressor   (10 stressor-pairs × 3 tiers × 1 run)

    Pair-aware: each pairwise + multi-stressor focal task carries a unique
    calibration_pair_id so the worker can correlate concurrent
    TaskResourceSample rows across the focal + stressor sides. The
    stressor containers themselves are dispatched as sibling rows inline
    (separate ApiTaskQueue entries for the same pair_id) by the worker
    once it picks up the focal task — the worker spins them up as
    sidecars during processing. This file enqueues only the focal entries
    plus the stressor metadata that the worker needs.
    """
    import os
    import uuid

    task_ids = []
    # 9 isolation runs.
    for tier in SIZE_TIERS:
        for run in range(CALIBRATION_RUNS_PER_TIER):
            t = _enqueue_calibration_task(
                db, node_def, tenant_id, tier=tier, run=run, run_kind="isolation",
            )
            task_ids.append(t.id)

    # 45 pairwise runs — one stressor at a time concurrent with focal.
    for stressor in CALIBRATION_STRESSORS:
        for tier in SIZE_TIERS:
            for run in range(PAIRWISE_RUNS_PER_TIER):
                pair_id = str(uuid.uuid4())
                t = _enqueue_calibration_task(
                    db, node_def, tenant_id, tier=tier, run=run,
                    stressor_kinds=[stressor], run_kind="pairwise",
                    calibration_pair_id=pair_id,
                )
                task_ids.append(t.id)

    # 30 multi-stressor runs — 3-way contention: focal + two stressors.
    for s1, s2 in MULTI_STRESSOR_PAIRS:
        for tier in SIZE_TIERS:
            pair_id = str(uuid.uuid4())
            t = _enqueue_calibration_task(
                db, node_def, tenant_id, tier=tier, run=0,
                stressor_kinds=[s1, s2], run_kind="multi",
                calibration_pair_id=pair_id,
            )
            task_ids.append(t.id)

    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        queue = node_def.process_type or "CPU"
        r.publish(f"task:available:{queue}", str(len(task_ids)))
    except Exception:
        pass

    return task_ids



def _derive_node_kind_from_image(executable_path: Optional[str]) -> Optional[str]:
    """Best-effort node_kind from an image ref.

    NodeDefinition.node_kind is the downstream-dispatch routing key
    (`worker_tasks.py:189` silently drops any node missing it).
    Build-task's register_node.py now sends it explicitly, but the
    deferred-registration approve path, the legacy upload-image
    flow, and external CLI callers can still POST without it. This
    helper derives a deterministic value from the image ref so the
    column is NEVER NULL on wizard-shape rows. Lowercase, dashes
    converted to underscores, namespace prefix stripped — matches
    the convention build-task uses for the same image.

    ``example.com/tenant-1/pdf-to-png-converter:latest``
    → ``pdf_to_png_converter``

    Returns None when the path is empty or can't be parsed (caller
    leaves the column NULL — downstream dispatch will still drop
    the row, which is the correct fail-loud signal for malformed
    image refs).
    """
    if not executable_path:
        return None
    tail = executable_path.split("/")[-1]      # "pdf-to-png-converter:latest"
    repo = tail.split(":")[0]                   # "pdf-to-png-converter"
    if not repo:
        return None
    return repo.replace("-", "_").lower()




# ---------------------------------------------------------------------------
# Literal-path GET routes — MUST be declared BEFORE the /{node_definition_id}
# catch-all below. FastAPI matches in decoration order; if a literal path like
# /wizard-llm-status sits AFTER /{node_definition_id:int}, the router hands
# the literal to the int-param route, Pydantic fails int coercion on the
# string, and the response is 422 Unprocessable Entity. Keep new literal
# GET routes in this block.
# ---------------------------------------------------------------------------


class VariantStatusRequest(PydanticBaseModel):
    """P9b — ImageBuilder PATCHes this after per-variant build outcomes
    are known. `succeeded` is the list of variant names that built AND
    pushed; `failed` is {variant: reason} for those that didn't. The
    API writes these into node_definitions.gpu_variants (filtered to
    succeeded) + .incompatible_reasons (failed entries) so the
    scheduler + wizard render the right state.
    """
    node_name: str
    succeeded: List[str] = []
    failed: Dict[str, str] = {}




# ---------------------------------------------------------------------------
# P9e — variant smoke-verification dispatch
# ---------------------------------------------------------------------------
#
# Maps a gpu_variants key to the queue_name a worker of that hardware
# subscribes to. Smoke tasks carry both queue_name (claim-time filter)
# AND gpu_variants (variants_guard hardware_tags intersection) — the
# queue narrows to the right worker pool, the variants_guard narrows
# further to workers that actually have the matching hardware tag.
_VARIANT_SMOKE_QUEUE: Dict[str, str] = {
    "cuda": "CUDA",
    "rocm": "ROCm",
    "intel": "Intel",
    "npu": "NPU",
    "metal": "Metal",
    "cpu": "CPU",
}


def _enqueue_variant_smoke_task(db, node, variant: str) -> Optional[int]:
    """Enqueue a P9e variant_smoke task that runs the baked-in probe
    inside the node's variant image on a matching-hardware worker.

    Returns the task id on success, None when the variant has no image
    ref (pushed-but-not-registered race; caller will re-sweep on the
    next variant-status PATCH).
    """
    import uuid
    from models import ApiTaskQueue, ApiTaskStatus

    gv = node.gpu_variants or {}
    image_ref = gv.get(variant)
    if not image_ref:
        logger.info(
            "variant_smoke skip: no image ref for node=%s variant=%s",
            node.name, variant,
        )
        return None

    queue_name = _VARIANT_SMOKE_QUEUE.get(variant.lower(), "CPU")

    # input_schema carries the shape/dtype hints the probe uses to
    # construct its synthetic forward-pass input. Pass-through — the
    # probe itself (P9e.2) does the zero-tensor fallback when the
    # schema is empty or missing fields.
    input_schema = node.input_schema or {}

    container_env = {
        "PLATFORM_TASK": "1",
        "NODE_NAME": node.name,
        "NODE_ID": str(node.id),
        "VARIANT": variant,
        "MOD_VARIANT_SMOKE": "1",
        "INPUT_SCHEMA_JSON": json.dumps(input_schema),
    }

    # P9m.3 — forward per-node tolerance overrides to the probe. Nodes
    # can set `custom_metadata.port_tolerance: {atol: 1e-5, rtol: 1e-6}`
    # when they need tighter reference comparison than the probe's 1e-3
    # default. Only pass the keys that parse as positive floats — the
    # probe falls back to defaults on bad values anyway, but this keeps
    # the wire shape clean.
    port_tol = (node.custom_metadata or {}).get("port_tolerance") or {}
    if isinstance(port_tol, dict):
        for env_key, tol_key in (("MOD_SMOKE_ATOL", "atol"),
                                 ("MOD_SMOKE_RTOL", "rtol")):
            raw = port_tol.get(tol_key)
            try:
                v = float(raw)
                if v > 0:
                    container_env[env_key] = str(v)
            except (TypeError, ValueError):
                pass

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"variant_smoke:{node.name}:{variant}:{uuid.uuid4().hex[:8]}",
        tenant_id=node.tenant_id,
        queue_name=queue_name,
        priority=0,
        task_type="variant_smoke",
        task_payload={
            "node_kind": "variant_smoke",
            "processing_image": image_ref,
            "container_env": container_env,
            "source_file_location": "",
            "minio_bucket": get_tenant_bucket_name(node.tenant_id, db),
            "gpu_required": variant.lower() != "cpu",
            "container_timeout": 300,
            "skip_download": True,
            "skip_upload": True,
            # variants_guard intersects these keys with the worker's
            # hardware_tags at claim time (see worker_tasks.py:379).
            # Single-key map — only a worker with `variant` in its
            # hardware_tags will pick this up.
            "gpu_variants": {variant: image_ref},
            # Cmd override — runs the probe baked into the variant
            # image (P9e.2). The variant's baked-in ENTRYPOINT is not
            # used here.
            "cmd": ["python", "/opt/mod_smoke/probe.py"],
            # Marker the complete/failed callbacks read to detect
            # a variant_smoke task and apply its result to
            # NodeDefinition.variant_verification_status.
            "variant_smoke": {
                "node_id": node.id,
                "node_name": node.name,
                "variant": variant,
            },
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace=f"variant_smoke_{variant}",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Nudge the claim poller on the matching queue.
    try:
        import redis as _redis
        r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish(f"task:available:{queue_name}", "1")
    except Exception:
        pass

    logger.info(
        "variant_smoke enqueued task_id=%s node=%s variant=%s queue=%s image=%s",
        task.id, node.name, variant, queue_name, image_ref,
    )
    return task.id


# ---------------------------------------------------------------------------


class BaseImageCatalogEntry(PydanticBaseModel):
    """One row in the wizard's base-image dropdown."""
    variant_dir: str
    image_tag: str
    image_ref: str
    display_name: str
    category: str
    default_interpreter: Optional[str] = None
    description: str = ""
    process_type_hint: str = "CPU"




class WizardLLMStatusResponse(PydanticBaseModel):
    provider: str
    model: str
    base_url: str
    loaded: bool
    size_bytes: int = 0
    expires_at: Optional[str] = None
    detail: str = ""






class WorkerLLMPingResponse(PydanticBaseModel):
    task_id: int
    expected_network: str
    llm_base_url: str
    message: str




# ---------------------------------------------------------------------------
# End literal-path block — everything below is parameterised or safe.
# ---------------------------------------------------------------------------







class ManifestMetadata(PydanticBaseModel):
    manifest_ref: str
    image_digest: str








class _NodeRoleUpdate(PydanticBaseModel):
    role: str  # 'user' | 'system'







def _tag_harbor_for_deletion_best_effort(
    tenant_id: Optional[int], image_ref: str,
) -> None:
    """§11b — apply the ``mod-deleted`` tag to a Harbor artifact so the
    image is visually flagged as awaiting retention-cleanup. Best-effort
    (failure is logged + swallowed); the authoritative ``deleted_at``
    lives in the DB row.
    """
    if not tenant_id or not image_ref:
        return
    try:
        repo, reference = _parse_harbor_image_ref(image_ref)
    except ValueError:
        return
    if not repo:
        return
    try:
        from utils.tenant_harbor_client import TenantHarborClient
        TenantHarborClient().tag_artifact_for_deletion(
            tenant_id=tenant_id, repository=repo, reference=reference,
        )
    except Exception as e:
        logger.info(
            "Harbor mod-deleted tag skipped for %s (tenant %s): %s",
            image_ref, tenant_id, e,
        )


def _parse_harbor_image_ref(image_ref: str) -> tuple[str, str]:
    """Split ``host/project/repo:tag`` into ``(repo, tag_or_digest)``
    suitable for the per-tenant Harbor client. The host + project
    prefix is dropped because TenantHarborClient infers the project
    from ``tenant_id``. Returns ``("", "")`` if the ref doesn't look
    like a Harbor URL we manage."""
    if not image_ref or "/" not in image_ref:
        return "", ""
    no_host = image_ref.split("/", 1)[1]
    if "/" not in no_host:
        return "", ""
    after_project = no_host.split("/", 1)[1]
    if "@" in after_project:
        repo, _, ref = after_project.partition("@")
        return repo, ref
    if ":" in after_project:
        repo, _, ref = after_project.rpartition(":")
        return repo, ref
    return after_project, "latest"


def _try_registry_delete_image(image_ref: str) -> None:
    """DELETE /v2/<name>/manifests/<digest> against the local registry.
    Only acts on images whose ref points at our internal registry:5000 —
    Harbor-hosted images (MOD system nodes) are out of scope. Failure is
    logged but not raised; the tenant's storage counter was already
    decremented before this was called."""
    import os as _os, requests as _rq
    if not image_ref:
        return
    local_registry = tenant_registry()
    if not image_ref.startswith(local_registry + "/"):
        return  # not an internal image, leave it alone
    # image_ref looks like "registry:5000/tenant-42/my-node:latest"
    without_prefix = image_ref[len(local_registry) + 1:]
    if ":" in without_prefix:
        name, tag = without_prefix.rsplit(":", 1)
    else:
        name, tag = without_prefix, "latest"
    base = f"http://{local_registry}"
    # Step 1: HEAD the manifest to get its digest.
    head = _rq.head(
        f"{base}/v2/{name}/manifests/{tag}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
        timeout=10,
    )
    if head.status_code != 200:
        logger.info("registry manifest HEAD %s → %s", image_ref, head.status_code)
        return
    digest = head.headers.get("Docker-Content-Digest")
    if not digest:
        return
    # Step 2: DELETE by digest. Requires registry config allow-delete.
    dele = _rq.delete(f"{base}/v2/{name}/manifests/{digest}", timeout=10)
    if dele.status_code == 202:
        logger.info("registry deleted %s@%s", name, digest)
    else:
        logger.info("registry delete %s@%s → %s", name, digest, dele.status_code)







class CheckSyntaxRequest(PydanticBaseModel):
    """Fast sync syntax check — called from the wizard while the user
    is still on the Source step so they get inline feedback before
    hitting Next. Never raises; returns ok=False + error line on bad
    input."""
    script: str
    interpreter: str = "python3"
    source_type: str = "script"


class CheckSyntaxResponse(PydanticBaseModel):
    ok: bool
    error: str = ""
    line: Optional[int] = None


class CheckImageRefRequest(PydanticBaseModel):
    """Pre-flight existence check for an Image-mode docker ref. Used by
    the wizard's Source step to fail-fast on typos / wrong mental models
    (e.g. HuggingFace model paths typed as Docker image refs) BEFORE the
    user submits a build that thrashes through the test-and-repair loop."""
    image_ref: str
    # Context fed to the LLM-suggester when ref doesn't exist. Lets the
    # suggestions match the user's actual intent rather than fishing
    # blindly from the name alone.
    node_name: Optional[str] = None
    input_type: Optional[str] = "image"
    output_type: Optional[str] = "image"
    description: Optional[str] = None
    # Skip the LLM suggestion call (saves ~1-3s when the caller doesn't
    # need alternatives — e.g., admin tools just verifying refs).
    skip_suggestions: bool = False


class CheckImageRefResponse(PydanticBaseModel):
    exists: Optional[bool] = None
    reason: str = ""
    registry_source: str = ""
    suggestions: List[Dict[str, Any]] = Field(default_factory=list)






class ClarifyingQuestion(PydanticBaseModel):
    """One question the LLM has asked the user during the Source-step
    Q&A pass (issue #18). The wizard renders these inline on Source
    so the user can answer before the wizard advances.

    The LLM's prompt populates this list when the analyzer detects
    something it can't disambiguate from the source alone — e.g. a
    script that reads `os.environ.get("TARGET_ENTITY")` could mean
    "TARGET_ENTITY is a node input file" OR "TARGET_ENTITY is an
    internal API key the user already has set". The user picks one,
    the wizard re-fires analyze-script with `user_answers` to
    propagate the resolution into Configure-step pre-fill.

    `id` is a stable, LLM-emitted identifier the wizard reflects
    back in user_answers so the LLM can correlate. `options` (when
    non-empty) renders as a list of preset choices; an empty list
    means free-text answer.
    """
    id: str
    question: str
    options: List[str] = []
    # Optional: location in the user's source the question is about,
    # so the wizard can highlight the offending line next to the
    # question. Format: "{filename}:{line}". Empty if not available.
    source_location: str = ""

    # Set when this question is a library-profiler-driven override
    # (process_type was promoted CPU → CUDA because nerfstudio was
    # imported, etc.). Lets the wizard frontend render the override
    # banner with the right UI affordance — "we changed this for you"
    # vs. "we don't deploy this yet, you'll need to bring your own
    # base image."
    field: Optional[str] = None
    suggested_answer: Optional[str] = None
    detected_accelerator: Optional[str] = None
    deployable: Optional[bool] = None


class GlueArtifact(PydanticBaseModel):
    """A small entrypoint/wrapper script the LLM proposes to bridge
    the user's I/O conventions to MOD's `/data/input/` and
    `/data/output/` paths (issue #18).

    Surfaces on the Preview step as another artifact tab in the
    dropdown row list, alongside the Dockerfile and entrypoint.sh.
    The user can edit or remove it before clicking Confirm and
    Build.

    `artifact_type` is the canonical filename slot the glue should
    occupy in the build context. Recognised values:
      • "entrypoint.sh"      — for script source
      • "Dockerfile.glue"    — wrapper layer for Dockerfile source
      • "glue.sh"            — shim for repo source
      • "Containerfile.glue" — wrapper for raw image source
    """
    artifact_type: str
    content: str
    description: str = ""


class AnalyzeScriptRequest(PydanticBaseModel):
    """Pre-fill the New Node Wizard by analysing the submitted code.
    Called synchronously from the wizard on Source → Next transition.

    `user_answers` is populated on follow-up turns when the previous
    response carried `clarifying_questions` — each answer pairs with
    the question's `id` so the LLM can incorporate the resolution
    into its analysis.
    """
    script: str
    interpreter: str = "python3"
    source_type: str = "script"  # "script" | "dockerfile" | "repo"
    user_answers: Optional[List[Dict[str, str]]] = None  # [{id, answer}]


class AnalyzeScriptResponse(PydanticBaseModel):
    input_type: str
    output_type: str
    process_type: str
    # variant_dir of a curated base image (from /base-images) that the
    # LLM thinks best matches the script. The wizard pre-selects this on
    # the Configure step with an "AI suggested" badge; user can override.
    # Guaranteed to be a valid variant_dir or a safe fallback (python-3.11
    # for CPU, cuda-12.4-python3.11 for CUDA, etc.) even when the LLM
    # hallucinates.
    suggested_base_image: str = "python-3.11"
    suggested_name: str
    suggested_description: str
    env_vars: List[Dict[str, Any]]
    allowed_egress: List[Dict[str, Any]]
    confidence: str
    reasoning: str
    # α — schema-specific confidence. ``"high"`` / ``"medium"`` / ``"low"``.
    # The overall ``confidence`` covers everything (base image,
    # process_type, env vars, etc.); this field is narrower: how
    # confident is the LLM specifically about input_type + output_type?
    # The wizard's hard schema gate blocks Next on the Configure step
    # when schema_confidence is "low" AND the user hasn't manually
    # overridden the AI-filled values. Default "high" preserves
    # back-compat with callers that predate the field.
    schema_confidence: str = "high"
    # New (issue #18): the LLM may emit clarifying questions for the
    # user before it commits to its analysis, AND/OR a glue artifact
    # to bridge non-standard I/O conventions. Both default to empty
    # so existing callers see no shape change. The wizard renders
    # questions inline on Source and the glue artifact as a tab on
    # Preview. Populated only when the LLM prompt is wired to emit
    # them — until then, callers see empty arrays / null and the
    # existing single-pass analysis flow continues unchanged.
    clarifying_questions: List[ClarifyingQuestion] = []
    glue_artifact: Optional[GlueArtifact] = None

    # Item #8 (fullproof extension) — build-time prediction. Estimates
    # the typical number of repair attempts + wall-clock minutes
    # based on library_profiles.successful_build_count_by_accel for
    # the libraries this script imports. None when no signal is
    # available (script imports no libraries we've profiled, or all
    # are first-time captures). The wizard frontend renders this
    # next to the Build button as "Typical build: ~N attempts /
    # ~M min based on past runs" so users know what to expect.
    predicted_attempts: Optional[int] = None
    predicted_minutes: Optional[int] = None
    prediction_basis: Optional[str] = None  # human-readable why
    # sz-tcp.4 (tiered credit pricing) — per input-SIZE-tier credit cost the
    # LLM estimated (Small <= 2 MB, Medium <= 30 MB, Large > 30 MB;
    # thresholds from services/cost_estimator.get_tier_thresholds). None =
    # the model gave no usable number for that tier, so the wizard leaves it
    # blank and admission falls back to the rule floors. Negative / garbage
    # LLM output is normalised to None by coerce_and_validate.
    suggested_credits_small: Optional[int] = None
    suggested_credits_medium: Optional[int] = None
    suggested_credits_large: Optional[int] = None




class AnalyzeRepoRequest(PydanticBaseModel):
    """Analyze a Git repo's shape for the New Node Wizard's repo tab.

    The endpoint samples a small set of sentinel files (README, manifests,
    likely entrypoints) over raw HTTPS — no `git clone`. The concatenated
    sample is then handed to the same LLM analyzer the script flow uses,
    so the wizard's downstream logic doesn't need to know which path
    sourced the content.
    """
    repo_url: str
    auth_token: Optional[str] = None  # opaque PAT/Bearer for private repos




class ContainerAdaptRequest(PydanticBaseModel):
    """Request to convert a Docker image or script into a workflow node."""
    source_image: Optional[str] = None       # Existing Docker image (e.g., "nginx:latest")
    node_name: str                           # Human-readable name for the new node
    input_type: str = "image"                # What it processes: image, video, mesh, text, audio
    output_type: str = "image"               # What it produces
    process_type: str = "CPU"                # CPU or GPU
    cost_in_credits: int = 1
    additional_instructions: Optional[str] = None  # Extra context for the LLM


_PLATFORM_IMAGE_CACHE: Dict[str, tuple[float, str]] = {}
_PLATFORM_IMAGE_CACHE_TTL_S = 60.0


def _probe_image_manifest(registry: str, repo: str, tag: str,
                            *, timeout_s: float = 3.0) -> bool:
    """HEAD the OCI distribution manifest URL. Returns True when the
    registry confirms the image exists. Anything else (404, network
    error, auth required) is treated as 'not available here'.

    We swallow auth errors instead of treating them as 'present' because
    a 401 response can mean either 'image exists, you need creds' or
    'image not found, registry hides existence behind auth'. Without
    creds we can't tell, so the safer behaviour is to fall back to the
    local registry where probes are anonymous.
    """
    import requests
    url = (
        f"https://{registry}/v2/{repo}/manifests/{tag}"
        if not registry.startswith(("http://", "https://"))
        else f"{registry.rstrip('/')}/v2/{repo}/manifests/{tag}"
    )
    headers = {
        "Accept":
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.v2+json",
    }
    try:
        # Try HTTPS first; if the registry is `registry:5000` (the
        # in-compose plain-HTTP one) we get connection-refused on the
        # HTTPS attempt and silently retry HTTP.
        resp = requests.head(url, headers=headers, timeout=timeout_s,
                              allow_redirects=True)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        # 401 / 403 / etc.: don't trust as positive presence.
        return False
    except requests.exceptions.SSLError:
        # Local registry serves plain HTTP.
        try:
            url_http = url.replace("https://", "http://", 1)
            resp = requests.head(url_http, headers=headers, timeout=timeout_s,
                                  allow_redirects=True)
            return resp.status_code == 200
        except requests.RequestException:
            return False
    except requests.RequestException:
        return False


def _resolve_platform_image(image: str) -> str:
    """Return the registry-qualified ref for a platform image, falling back to
    this deployment's own mirror of MOD's catalogue when the configured
    registry does not have it.

    Lookup order:
      1. {DOCKER_REGISTRY}/modtim/{image}    (Harbor — preferred)
      2. {PLATFORM_MIRROR_REGISTRY}/modtim/{image}  (your mirror, if any)
      3. Default: keep the Harbor ref (fail loudly via worker pull logs)

    Result is cached for 60 s per image string so we don't probe Harbor
    on every dispatch — Harbor's pull-through cache is fast but a
    network round-trip per task adds up.
    """
    import time

    # Local short-circuit: tests + fresh-boot envs frequently set
    # DOCKER_REGISTRY=registry:5000 directly. In that case we trust the
    # caller and don't probe.
    registry = platform_registry()
    canonical = f"{registry}/modtim/{image}"

    now = time.time()
    cached = _PLATFORM_IMAGE_CACHE.get(image)
    if cached is not None and cached[0] > now:
        return cached[1]

    # Parse `image` ("containeradapter:latest") into repo + tag.
    if ":" in image:
        repo_short, tag = image.split(":", 1)
    else:
        repo_short, tag = image, "latest"
    full_repo = f"modtim/{repo_short}"

    chosen = canonical  # default to Harbor; only swap on a confirmed miss
    if _probe_image_manifest(registry, full_repo, tag):
        chosen = canonical
    else:
        # PLATFORM_MIRROR_REGISTRY, not LOCAL_REGISTRY. This was the only
        # caller reading that variable as "a second copy of MOD's catalogue";
        # every other reader means "where TENANT images live". Its old default
        # (registry:5000) was retired with the registry:2 container, so this
        # probe went to a host that does not resolve and the mirror feature was
        # off for everyone. Unset now means no mirror, and the probe is skipped
        # rather than spent.
        from utils.registries import platform_mirror_registry
        local_registry = platform_mirror_registry()
        if local_registry and local_registry != registry:
            if _probe_image_manifest(local_registry, full_repo, tag):
                chosen = f"{local_registry}/modtim/{image}"
                logger.info(
                    "_resolve_platform_image: %s not in Harbor; using local "
                    "registry fallback %s", canonical, chosen,
                )
            else:
                logger.warning(
                    "_resolve_platform_image: %s not in Harbor or local "
                    "registry %s/modtim/%s — dispatching with Harbor ref "
                    "anyway; the operator should push the image or fix "
                    "DOCKER_REGISTRY", canonical, local_registry, image,
                )

    _PLATFORM_IMAGE_CACHE[image] = (now + _PLATFORM_IMAGE_CACHE_TTL_S, chosen)
    return chosen


def _enqueue_container_task(
    db,
    tenant_id: int,
    image: str,
    env: dict,
    node_kind_label: str,
    queue_name: str = "CPU",
    gpu_required: bool = False,
    max_attempts: int = 3,
) -> int:
    """Helper to enqueue a container task and notify workers.

    `queue_name` picks the worker pool (CPU / CUDA / ROCm / Intel). For LLM
    tasks pass queue_name="CUDA" + gpu_required=True; the task container
    reads LLM_BASE_URL=http://host.docker.internal:11434 and hits whatever
    Ollama the GPU worker has running on its host.

    `max_attempts` defaults to 3 so transient failures (worker hung, claim
    expired without progress, network-partitioned remote worker) get one
    automatic re-queue before the task is marked FAILED. Long-running
    builds aren't penalised — workers extend their claims via heartbeat
    while actually working, so retries only fire when a worker truly
    went silent. Pass max_attempts=1 explicitly for billing-sensitive
    or expensive one-shots that should never auto-retry.
    """
    import os
    import uuid
    from models import ApiTaskQueue, ApiTaskStatus
    # CE shim: delegates to the paid alias table on Pro; on CE there are no
    # managed AI stacks, so it is always False and the external-URL swap
    # below is skipped (GT.P2.6b, edge E29).
    from utils.tool_config import is_managed_local_llm

    registry = platform_registry()
    # `ApiTaskQueue.node_id` is NOT NULL (it's a logical identifier used by
    # workers to group/stream task output). For bootstrap tasks that
    # AREN'T yet associated with a real NodeDefinition — image builds,
    # DAG generation, container adaptation, base-image rebuilds — we
    # synthesize a stable, descriptive id from the node_kind_label + a short
    # uuid suffix so multiple concurrent bootstraps of the same kind don't
    # collide on the same node_id row when workers write back telemetry.
    synthetic_node_id = f"bootstrap:{node_kind_label}:{uuid.uuid4().hex[:8]}"

    # Every task enqueued via this helper runs MOD-owned code — the
    # Containerizer, ImageBuilder, ContainerAdapter, or
    # the nightly base-image rebuild. They all need the main docker
    # network so they can reach Ollama / Harbor / MinIO / the API.
    # Flag them with PLATFORM_TASK so the worker's SelectNetworkMode
    # routes them to mainNetwork via its priority-0 branch (see
    # CORE/Worker/internal/executor/netpol.go). Tenant-submitted work
    # doesn't go through this helper and therefore doesn't get the
    # flag — the default-deny user-sandbox network applies.
    env = dict(env or {})  # copy so callers don't observe the mutation
    env["PLATFORM_TASK"] = "1"

    # Swap LLM_BASE_URL to the PUBLIC Ollama endpoint when one is
    # configured. In-compose the API uses `http://ollama:11434`, which
    # only resolves on the control host's network. Remote workers
    # (and the containers they spawn) can't reach that name — they
    # need the nginx-exposed FQDN (`LLM_BASE_URL_EXTERNAL`, e.g.
    # `https://example.com`). Set at the compose level by
    # start-main-server.sh. If no external URL is configured (local-
    # only dev), leave whatever the `_build_llm_env` resolver put in —
    # the embedded worker reaches Ollama via the internal name and
    # is the only one claiming platform tasks in that case anyway.
    #
    # Only swap when the resolved target IS the managed-local model
    # (`is_managed_local_llm`, routers.ai_config) — a tool configured
    # against a cloud provider (OpenAI/Anthropic/etc) already has a
    # publicly-reachable LLM_BASE_URL, and clobbering it with
    # LLM_BASE_URL_EXTERNAL would silently redirect that provider's
    # calls at our own Ollama endpoint instead.
    external_llm = os.getenv("LLM_BASE_URL_EXTERNAL", "").strip().rstrip("/")
    current_llm = env.get("LLM_BASE_URL", "")
    if external_llm and current_llm and is_managed_local_llm(current_llm):
        # Worker tasks use the OpenAI Python SDK (via llm_providers.py's
        # OpenAICompatibleProvider). The SDK appends `/chat/completions`
        # to base_url but does NOT add `/v1/` itself — it expects the
        # caller to include the version segment in base_url. Same
        # convention as `https://api.openai.com/v1`.
        #
        # If LLM_BASE_URL_EXTERNAL is configured without `/v1` (e.g.
        # `https://example.com`), the SDK produces
        # `https://stage-llm.../chat/completions` which vLLM 404s on.
        # Append it here when missing so the operator doesn't have to
        # remember the version suffix in every deployment's .env.
        if external_llm.endswith("/v1") or external_llm.endswith("/v1/"):
            env["LLM_BASE_URL"] = external_llm.rstrip("/")
        else:
            env["LLM_BASE_URL"] = external_llm + "/v1"

    # Inject Harbor credentials into the task payload so the container
    # can `docker login` before `docker build` (the Dockerfile's FROM
    # typically references registry.example.com and will 401 without
    # auth). Resolved from the tenant's own secrets — falls back to
    # process env (operator-configured DOCKER_REGISTRY + HARBOR_*) if
    # Vault/DB lookup is empty. Putting the creds in the task_payload
    # itself (not just the worker-side injection) means even stale
    # remote workers that predate the worker-side injection still get
    # a working payload. Observed: task 7088 claimed by a remote
    # `worker-example-host` running an older binary.
    if tenant_id is not None:
        try:
            from crud import resolve_decrypted_secret_for_tenant
            from database import SessionLocal as _DbSession
            # Reuse the existing db session if possible; otherwise
            # open a short-lived one just for the lookup.
            _db = db if db is not None else _DbSession()
            _close_db = db is None
            try:
                hu = resolve_decrypted_secret_for_tenant(_db, "harbor_username", tenant_id)
                hp = resolve_decrypted_secret_for_tenant(_db, "harbor_password", tenant_id)
                hr = resolve_decrypted_secret_for_tenant(_db, "harbor_registry", tenant_id)
                if hu and hp and "HARBOR_USERNAME" not in env:
                    env["HARBOR_USERNAME"] = hu
                    env["HARBOR_PASSWORD"] = hp
                if hr and "HARBOR_REGISTRY" not in env:
                    env["HARBOR_REGISTRY"] = hr
            finally:
                if _close_db:
                    _db.close()
        except Exception:
            logger.exception("harbor creds lookup failed; task may 401 on Harbor pulls")
    # Operator-level fallback — for system dispatches (tenant_id is
    # None, e.g. nightly base-image rebuild) and for tenants whose
    # Vault doesn't hold Harbor creds. The API container is already
    # configured with these env vars for its own Harbor access.
    if "HARBOR_USERNAME" not in env and os.getenv("HARBOR_USERNAME"):
        env["HARBOR_USERNAME"] = os.getenv("HARBOR_USERNAME", "")
        env["HARBOR_PASSWORD"] = os.getenv("HARBOR_PASSWORD", "")
    if "HARBOR_REGISTRY" not in env:
        env["HARBOR_REGISTRY"] = os.getenv("HARBOR_REGISTRY") or platform_registry()

    # ImageBuilder (+ nightly base-image rebuild) runs `docker build`
    # and `docker push` inside the task container — which requires a
    # usable Docker socket. Every other platform image (Containerizer,
    # ContainerAdapter) only talks to Ollama/Harbor over
    # HTTP and does NOT need the socket. Auto-enable based on the
    # image name so callers don't have to remember.
    image_lower = (image or "").lower()
    needs_docker_socket = (
        image_lower.startswith("imagebuilder")
        or image_lower.startswith("modtim/imagebuilder")
    )

    # ImageBuilder's manifest writer (DockerContainers/ImageBuilder/
    # manifest_emit.py::_bucket) must resolve the same bucket the
    # API-side reader (utils.manifest.manifest_bucket) does: a
    # BYO-S3 tenant's signed manifest must land in the tenant's own
    # bucket, not a hardcoded `tenant-{id}-files` guess. Hand the
    # container the already-resolved effective bucket as MINIO_BUCKET
    # (manifest_emit reads it and falls back to the house default when
    # absent — e.g. a stale worker image). `manifest_bucket` covers the
    # system-tenant case (tenant_id=None → mod-core-files) that the
    # plain get_tenant_bucket_name house default does not.
    if needs_docker_socket:
        try:
            from utils.manifest import manifest_bucket
            env.setdefault("MINIO_BUCKET", manifest_bucket(tenant_id, db))
        except Exception:
            logger.warning(
                "could not resolve effective bucket for imagebuilder "
                "dispatch (tenant_id=%s); container falls back to its "
                "house default", tenant_id, exc_info=True,
            )

    # Resolve the canonical image reference with a fallback. Platform
    # images live in two places by convention:
    #   1. Harbor (`{DOCKER_REGISTRY}/modtim/{image}`) — the curated
    #      remote target every shipped platform builds.
    #   2. The local in-compose registry
    #      (`{LOCAL_REGISTRY}/modtim/{image}`) — a fallback for fresh
    #      installs where Harbor hasn't been seeded yet.
    # `_resolve_platform_image` probes Harbor's manifest endpoint and,
    # on 404 / unreachable, swaps to the local-registry copy if it has
    # one. Result is cached for 60 s so we don't re-probe on every
    # task dispatch. If neither location has the image we still
    # enqueue with the Harbor ref so the failure mode is loud + the
    # worker's existing error logging tells the operator exactly what's
    # missing — silently dispatching a task with a 404 image is worse.
    processing_image = _resolve_platform_image(image)

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=synthetic_node_id,
        tenant_id=tenant_id,
        queue_name=queue_name,
        priority=0,
        task_payload={
            "node_kind": node_kind_label,
            "processing_image": processing_image,
            "container_env": env,
            "source_file_location": "",
            "minio_bucket": get_tenant_bucket_name(tenant_id, db),
            "gpu_required": gpu_required,
            "container_timeout": 1800,
            "needs_docker_socket": needs_docker_socket,
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=max_attempts,
        output_namespace=node_kind_label,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish(f"task:available:{queue_name}", "1")
    except Exception:
        pass

    return task.id


def _build_llm_env(tool: str) -> dict:
    """Resolve the tool's AI config (from Vault) into the LLM_* env vars a
    task container needs. Delegates to utils.tool_config, which on Pro
    resolves through routers.ai_config (managed tiers, keep_alive wire-format
    translation, etc.) and on CE covers BYO/stack slots (GT.P2.6b, E30).
    """
    from utils.tool_config import resolved_effective_env
    return resolved_effective_env(tool)


# The paid managed-AI router's module name (GT.P2.6d/E41 guard). A constant so
# this file keeps its GT.P2.6b E30 contract: no `optional_paid("<paid module>")`
# call literal here (tests/test_p2_6b_node_authoring_ce.py locks that).
_PAID_AI_CONFIG_MODULE = "routers.ai_config"


def _adapter_bound_stack_asleep(db: "Session", res: Dict[str, Any]) -> bool:
    """Is the adapter-bound stack ASLEEP (design D4)?

    Mirrors ``assistant_orchestrator._bound_stack_asleep`` but scoped to the
    "adapter" slot.  A stack whose desired_state is ``stopped`` (the platform's
    sleep policy wrote it) is woken; a stack that is merely stopped/not-running
    with desired_state still ``running`` is an operator stop — no wake.
    """
    target = res.get("target") or {}
    stack_id = target.get("stack_id")
    if not stack_id:
        return False
    try:
        from crud.persistent_stacks_domain import get_stack_by_id
        from utils.accelerator_fit import ACTIVE_STACK_STATUSES
        row = get_stack_by_id(db, int(stack_id))
        if row is None or (getattr(row, "status", None) in ACTIVE_STACK_STATUSES):
            return False
        if getattr(row, "desired_state", None) != "stopped":
            return False
        return (getattr(row, "status", None)
                not in ("torn_down", "created", "failed", "provisioning_failed"))
    except Exception:  # noqa: BLE001 — a diagnostic read never breaks the build
        return False


def _adapter_slot_failure_cause(db, tenant_id: Optional[int] = None) -> Optional[str]:
    """ai-config-stack-binding 3.5 — the resolver's sentence for why the
    ADAPTER slot will not serve, for EVERY non-``ready`` outcome.

    ``_build_llm_env`` (via :func:`utils.tool_config.resolved_effective_env`)
    degrades a failed resolution to ``{}`` — that is its contract, so the dict
    can never leak a diagnostic key into a task container. The CAUSE lives in
    :func:`services.ai_slot_resolver.resolve_slot`, which is what every
    consumer that REPORTS to a human calls directly (same shape as 3.2's
    ``assistant_orchestrator._slot_failure_cause`` and 3.3's
    ``support_chat_shared_service._wizard_slot_failure_cause``):
    ``no_binding | stack_stopped | stack_missing | unreachable | waking``.

    Returns ``None`` only when the slot unexpectedly resolves ``ready`` or the
    resolver cannot speak at all (no DB / resolver raised).
    """
    try:
        # GT.P2.6d (E41) carve-out, same shape as `stack_provider_error`'s: the
        # binding table + Settings -> AI surface is the PAID managed-AI state,
        # so in a tree without the paid AI-config router there is nothing to
        # report — "nothing configured" stays the honest CE answer (CE
        # behaviour unchanged: the adapter still dispatches with whatever env
        # it has). Spelled through a constant so this file keeps its GT.P2.6b
        # E30 contract of never naming the paid module in a call literal.
        from utils.paid_import import optional_paid
        if optional_paid(_PAID_AI_CONFIG_MODULE) is None:
            return None
        from services.ai_slot_resolver import REMEDY, resolve_slot
        res = resolve_slot(db, "adapter", tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — a diagnostic must not break the build
        return None
    if (res.get("outcome") or "") == "ready":
        return None
    # D4 — a bound stack that is ASLEEP is not a failure: start it and answer.
    if (res.get("outcome") or "") == "stack_stopped" and _adapter_bound_stack_asleep(
            db, res):
        res = resolve_slot(db, "adapter", tenant_id=tenant_id, wake=True)
        if (res.get("outcome") or "") == "ready":
            return None
    message = (res.get("message") or "").strip()
    attempts = res.get("attempts") or []
    if len(attempts) > 1:
        others = [(a.get("message") or "").replace(REMEDY, "").strip()
                  for a in attempts[1:]]
        others = [o for o in others if o]
        if others and message:
            message = f"{message} Fallbacks: {' '.join(others)}"
    return message or None


def _adapter_slot_guard(db, tenant_id: Optional[int] = None) -> None:
    """ai-config-stack-binding 3.5 — the REQUEST-BOUNDARY check for the
    CONTAINER_ADAPTER dispatch.

    Both adapter call sites (``node_authoring.py`` generate-dockerfile and
    ``task_queue_scheduler.py``) spread ``**_build_llm_env("adapter")`` into the
    task env, so an unbound / stopped / missing slot used to enqueue a doomed
    ``ApiTaskQueue`` row that a worker would run with an empty env and fail
    silently. Call this BEFORE ``_enqueue_container_task`` (and before any
    billing/credit charge) so the client gets the named cause — the same
    sentence the wizard/assistant/support surfaces get — instead of a task
    that can never succeed.

    Raises ``HTTPException(503)`` (service-unavailable: the bound model is
    what is unavailable, the request itself is well-formed — same status the
    3.x family uses for "the slot's model cannot serve", e.g.
    ``utils.llm_client``'s held-wake 503 and ``support_chat``'s disabled 503)
    with the resolver's message; does nothing when the slot resolves ``ready``
    or the resolver cannot speak (CE / no DB), which keeps today's behaviour.
    """
    cause = _adapter_slot_failure_cause(db, tenant_id)
    if cause:
        raise HTTPException(status_code=503, detail=cause)




class BuildRequest(PydanticBaseModel):
    """Request to build a Docker image and register as a node."""
    image_name: str                          # Image name without registry (e.g., "myprocessor")
    node_name: Optional[str] = None          # If set, also registers the node
    input_type: str = "image"
    output_type: str = "image"
    process_type: str = "CPU"
    cost_in_credits: int = 1
    description: Optional[str] = None








class PredictVariantsRequest(PydanticBaseModel):
    """Input for the cheap 'which GPU variants would we build?' probe
    the wizard fires on the Review step. No LLM call — pure import
    sniffing on the source + intersection with the platform's GPU
    compatibility whitelist. Lets the user uncheck variants they
    don't want BEFORE paying the 100-credit generation charge."""
    source_image: Optional[str] = None        # hero FROM hint, e.g. python:3.11
    additional_instructions: Optional[str] = None  # typically the pasted script body
    source_files: Optional[List[Dict[str, Any]]] = None  # multi-file mode
    # Repo mode — when set, the endpoint fetches the same sentinel-file
    # slice /analyze-repo uses and feeds it to the import sniffer, so
    # repo-sourced nodes get the same GPU-variant detection as scripts
    # and Dockerfiles. Otherwise repo nodes used to ship as CPU-only
    # because the sniffer had nothing to read.
    repo_url: Optional[str] = None
    repo_auth_token: Optional[str] = None




# ----------------------------------------------------------------------
# P8d — deep-CUDA fingerprint detector
# ----------------------------------------------------------------------
#
# Source-level rewrites handle framework-only CUDA (`.to("cuda")` etc.)
# For repos with compiled CUDA kernels (4DGaussians-class), the adapter
# would need hipify_python / dpct / OpenVINO ports we haven't built yet.
# Detect the shape here so the wizard can set expectations before the
# user burns 100 credits on a generation that can't cross to AMD/Intel.

_DEEP_CUDA_PATTERNS = [
    # setup.py using torch.utils.cpp_extension's CUDAExtension
    (r"\bCUDAExtension\s*\(",                       "CUDAExtension() in setup.py"),
    # torch.utils.cpp_extension.load / load_inline with .cu source
    (r"torch\.utils\.cpp_extension\.(?:load|load_inline)\s*\(",
                                                    "torch.utils.cpp_extension.load"),
    # Direct nvcc invocation in a build script
    (r"\bnvcc\s+-[A-Za-z]",                         "nvcc compile flags"),
    # cmake targets using CUDA_ARCHITECTURES or enable_language(CUDA)
    (r"enable_language\s*\(\s*CUDA\s*\)",           "CMake enable_language(CUDA)"),
    (r"CUDA_ARCHITECTURES",                         "CMake CUDA_ARCHITECTURES"),
    # pyproject.toml build-requires with torch-cuda-specific pins
    (r"cupy(?:-cuda\d+)?\b",                        "cupy build dep"),
    # Direct cuda runtime imports (pycuda)
    (r"import\s+pycuda",                            "pycuda import"),
    # cuBLAS / cuDNN direct linkage
    (r"#include\s+<(?:cublas|cudnn|cusparse|cusolver)",
                                                    "cuBLAS/cuDNN C header include"),
]

# Filename fingerprints.
_DEEP_CUDA_EXTENSIONS = {".cu", ".cuh"}


def _detect_deep_cuda(source_files) -> tuple[bool, list[str]]:
    """Back-compat: returns (deep_cuda, reasons). Thin wrapper over
    _detect_deep_cuda_with_hints — callers that don't need the hints
    payload stay on the original signature."""
    deep, reasons, _hints = _detect_deep_cuda_with_hints(source_files)
    return deep, reasons


# Default TORCH_CUDA_ARCH_LIST covering modern NVIDIA compute
# capabilities the platform realistically ships workers for:
#   7.5 — Turing (RTX 2000, T4)
#   8.0 — Ampere A100
#   8.6 — Ampere GA10x (RTX 3000, A10, A40, RTX 6000 Ada on some fw)
#   8.9 — Ada Lovelace (RTX 4000, L4, L40)
#   9.0 — Hopper (H100, H200)
# Extension builds target all of these so the resulting image runs
# on anything from an RTX 2060 to an H100 without a rebuild.
_DEFAULT_TORCH_CUDA_ARCH_LIST = "7.5;8.0;8.6;8.9;9.0"


def _detect_deep_cuda_with_hints(source_files) -> tuple[bool, list[str], dict]:
    """Detect compiled-CUDA fingerprints AND return build hints the
    Containerizer uses to patch the LLM-generated Dockerfile
    deterministically (no more "the LLM forgot submodule init" runs).

    Returns: (deep_cuda, reasons, hints) where hints has:
      - needs_submodule_init: True when the source tree includes a
        `.gitmodules` file. The patch pass inserts
        `git submodule update --init --recursive` before the extension
        install step. Fixes the 4DGaussians-class case where the .cu
        kernels live in git submodules the checkout didn't recurse.
      - cuda_arch_list: semicolon-joined compute capability string to
        export as TORCH_CUDA_ARCH_LIST so nvcc compiles for every
        modern GPU the platform realistically hosts. Avoids the
        "no kernel image is available for execution on the device"
        runtime error when the build-time autodetect picked a narrower
        set than the worker GPU uses.
      - kernel_dirs: list of directory names containing .cu/.cuh files.
        Used for human-readable messaging on the Review step.
      - extension_files: list of setup.py / pyproject.toml paths that
        invoke CUDAExtension — the patch pass ensures
        `pip install --verbose --no-build-isolation .` runs in each
        extension's directory.
    """
    import os.path as _osp
    import re as _re
    reasons: list[str] = []
    hints = {
        "needs_submodule_init": False,
        "cuda_arch_list": _DEFAULT_TORCH_CUDA_ARCH_LIST,
        "kernel_dirs": [],
        "extension_files": [],
    }
    if not source_files:
        return False, reasons, hints

    kernel_dirs_set: set[str] = set()

    for f in source_files:
        if not isinstance(f, dict):
            continue
        name = (f.get("filename") or "").lower()
        body = f.get("body") or ""

        # .gitmodules — signals we must recurse submodules at build time.
        # 4DGaussians ships its differentiable-rasterizer kernels via
        # a submodule; without this the kernel .cu sources never land
        # on disk and the extension build skips silently.
        if _osp.basename(name) == ".gitmodules":
            hints["needs_submodule_init"] = True
            reasons.append("has .gitmodules (submodules need init)")
            continue

        # Extension fingerprint — .cu / .cuh
        for ext in _DEEP_CUDA_EXTENSIONS:
            if name.endswith(ext):
                reasons.append(f"ships {ext} kernel file")
                parent = _osp.dirname(name) or "."
                kernel_dirs_set.add(parent)
                break

        # Content fingerprint — CUDAExtension / cpp_extension.load / ...
        if isinstance(body, str) and body:
            for pattern, label in _DEEP_CUDA_PATTERNS:
                if _re.search(pattern, body):
                    reasons.append(label)
                    # Track setup.py-style files that invoke CUDAExtension
                    if "CUDAExtension" in body or "cpp_extension" in body:
                        hints["extension_files"].append(f.get("filename") or "")
                    break

    hints["kernel_dirs"] = sorted(kernel_dirs_set)
    hints["extension_files"] = sorted(set(p for p in hints["extension_files"] if p))

    # Dedupe reasons while preserving order.
    seen = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return (len(out) > 0), out, hints




# ----- Retry with corrections (issue #2 / node-build-reliability) -----

class RetryWithCorrectionsRequest(PydanticBaseModel):
    """Re-run the Containerizer for a task whose Phase 2 build failed.
    Server reuses the original env vars, appends per-variant failure
    context to ADDITIONAL_INSTRUCTIONS so the LLM corrects the
    Dockerfile, and dispatches a new Containerizer task.
    """
    task_id: int


def _retry_parse_build_results(log_tail: str) -> List[Dict[str, Any]]:
    """Extract failed BUILD_RESULT_JSON envelopes from a task's log
    tail. Returns the parsed dicts in order of appearance so the
    corrections prompt mentions failures the way the user saw them.
    """
    out: List[Dict[str, Any]] = []
    if not log_tail:
        return out
    for match in re.finditer(
        r"^BUILD_RESULT_JSON:\s*(\{.*?\})\s*$",
        log_tail,
        re.MULTILINE,
    ):
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict) and parsed.get("status") == "failed":
                out.append(parsed)
        except Exception:
            continue
    return out


def _retry_build_corrections_prompt(failures: List[Dict[str, Any]]) -> str:
    """Build the corrections block we append to ADDITIONAL_INSTRUCTIONS.
    Per-class prompt templates so the LLM gets concrete guidance on
    what to fix instead of a generic "try again".
    """
    if not failures:
        return (
            "\n\nThe previous Containerizer attempt failed but no "
            "structured failure data was captured. Please retry with "
            "extra care toward base-image selection and dependency "
            "version pinning."
        )
    lines = [
        "",
        "",
        "IMPORTANT — your previous Dockerfile failed. Correct these specific issues:",
    ]
    for f in failures:
        variant = f.get("variant", "?")
        cls = f.get("error_class", "unknown")
        cmd = (f.get("command") or "").strip()
        line_no = f.get("line")
        dockerfile = f.get("dockerfile", "Dockerfile")
        loc = f"{dockerfile}:{line_no}" if line_no else dockerfile
        if cls == "from_image_not_found":
            lines.append(
                f"- Variant `{variant}`: base image not found at {loc}. "
                f"Pick a different FROM that exists in the curated allowlist; "
                f"do not invent tags."
            )
        elif cls == "pip_install_failed":
            lines.append(
                f"- Variant `{variant}`: pip install failed at {loc} "
                f"(`{cmd}`). Verify every version exists on PyPI; remove "
                f"any package that hallucinated a version pin (e.g. "
                f"`gradio==6.0.1` doesn't exist; use `gradio==4.44.0`)."
            )
        elif cls == "apt_install_failed":
            lines.append(
                f"- Variant `{variant}`: apt-get install failed at {loc} "
                f"(`{cmd}`). The base image may not have apt repos "
                f"configured, or the package name is wrong. Try a "
                f"different base or drop the apt step entirely."
            )
        elif cls == "vcs_clone_failed":
            lines.append(
                f"- Variant `{variant}`: git clone failed at {loc} "
                f"(`{cmd}`). Verify the URL is correct + publicly "
                f"reachable; consider pinning to a release tarball."
            )
        elif cls == "out_of_disk":
            lines.append(
                f"- Variant `{variant}`: ran out of disk space "
                f"(`{cmd}`). Reduce image layer size — combine RUN "
                f"steps and clean apt caches/pip caches."
            )
        elif cls == "network_unreachable":
            lines.append(
                f"- Variant `{variant}`: network failure at {loc} "
                f"(`{cmd}`). Likely transient; if not, switch to a "
                f"base image that bundles the dependency."
            )
        else:
            lines.append(
                f"- Variant `{variant}`: build failed at {loc} "
                f"(`{cmd}`). error_class={cls}."
            )
    lines.append("")
    return "\n".join(lines)




class RebuildVariantRequest(PydanticBaseModel):
    """Operator "Rebuild {variant} with my changes" from the wizard Preview.

    workflow_run_id identifies the build the operator is looking at; the
    endpoint resolves its BuildSpec, records the edited source as an explicit
    per-variant override, and re-dispatches JUST that variant's build → push →
    sample-test chain.
    """
    workflow_run_id: int
    variant: str
    files: List[Dict[str, str]]  # [{"filename": ..., "content": ...}]




# ====================================================================
# δ wizard auto-repair loop — runtime smoke failures
# ====================================================================
#
# The retry-with-corrections endpoint above handles BUILD failures
# (docker build → apt/pip install errors). The repair-artifacts
# endpoint below handles RUNTIME failures: the image built fine, the
# entrypoint ran, but it produced no output (or crashed). The
# test-and-repair build-task stage calls this in a loop while
# iterating; this endpoint owns the LLM round-trip + BuildSpec mutation
# and nothing else.


class RepairArtifactsRequest(PydanticBaseModel):
    spec_id: int
    mode: str  # "inner" | "outer"
    attempt_number: int
    failure: Dict[str, Any]   # exit_code, stdout_tail, stderr_tail, produced_output, output_files, input_seed
    current_scripts: Dict[str, str] = Field(default_factory=dict)
    # Only sent for outer mode — sending it for inner is allowed but
    # ignored, so the build-task stage can avoid branching on mode.
    current_dockerfile: Optional[str] = None
    # Remediation-cache context, supplied by the build-task loop after
    # it queries /remediation/lookup for this failure's signature.
    #   * cache_hint    — a prior diff that fixed a similar error; the
    #                     LLM gets it as a starting point (not gospel).
    #   * known_bad     — short excerpts of fixes that ALREADY FAILED
    #                     for this exact error; the LLM is told not to
    #                     re-propose them (persistent anti-thrash).
    #   * promoted_diff — a high-trust (promoted) cached fix. When set
    #                     for an outer repair, the endpoint applies it
    #                     directly and skips the LLM entirely.
    cache_hint: Optional[str] = None
    known_bad: List[str] = Field(default_factory=list)
    promoted_diff: Optional[str] = None


class RepairArtifactsResponse(PydanticBaseModel):
    accepted: bool
    mode: str
    attempt_number: int
    explanation: str = ""
    updated_scripts: Dict[str, str] = Field(default_factory=dict)
    updated_dockerfile: Optional[str] = None
    rejection_reasons: List[str] = Field(default_factory=list)
    budget_remaining_inner: int = 0
    budget_remaining_outer: int = 0


class IngestArtifactsRequest(PydanticBaseModel):
    spec_id: int
    artifacts: Dict[str, Any]


class IngestArtifactsResponse(PydanticBaseModel):
    ok: bool
    reason: str = ""
    fields_merged: List[str] = Field(default_factory=list)




class ResetRepairBudgetRequest(PydanticBaseModel):
    spec_id: int


class ResetRepairBudgetResponse(PydanticBaseModel):
    ok: bool
    previous_inner_used: int = 0
    previous_outer_used: int = 0




class ClusterCapabilitiesResponse(PydanticBaseModel):
    available: List[str]
    recommended: str
    description: str








def _extract_llm_ping(log_tail: str) -> Optional[Dict[str, Any]]:
    """Find a JSON object in log_tail carrying `llm_ping`. Emitted by
    Containerizer's OUTPUT_MODE=llm-ping branch as its last stdout
    line. Returns the dict verbatim when found, else None."""
    import json as _json
    if not log_tail or "llm_ping" not in log_tail:
        return None
    text = log_tail
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    for start in reversed(positions):
        depth = 0
        end = None
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        candidate = text[start:end + 1]
        try:
            obj = _json.loads(candidate)
        except _json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "llm_ping" in obj:
            return obj
    return None






_ARTIFACT_BEGIN_SENTINEL = "=====MOD_ALBEDO_ARTIFACTS_BEGIN====="
_ARTIFACT_END_SENTINEL = "=====MOD_ALBEDO_ARTIFACTS_END====="


def _extract_albedo_artifacts(log_tail: str) -> Optional[Dict[str, Any]]:
    """
    Find the last JSON object in log_tail that contains all three Albedo keys.
    Containerizer prints its final result as pretty-printed JSON to stdout; the
    worker captures stdout into log_tail.

    Fast path (sentinel-wrapped): scans for the sentinels in O(n).
    The Containerizer has wrapped its artifact JSON in sentinels since
    the TRELLIS.2 thrash repro — task 11570's 4.4 MB log_tail took
    0.86s per poll in the old brute-force walker, which made the
    wizard UI fall behind and look stuck.

    Fallback (legacy): tasks completed BEFORE the Containerizer
    sentinels shipped don't carry them. Walk brace-balanced
    candidates from the end. Tolerant of nested wrappers: some LLM
    backends return `{"dockerfile": "{...all three keys inside as a
    JSON string...}"}` instead of the flat form.
    """
    import json as _json

    if not log_tail:
        return None

    required_keys = ("dockerfile", "entrypoint_sh", "build_and_push_sh")

    # ---- Fast path: sentinel-wrapped extraction ----
    # rfind/find on a Python string is O(n) via the C-level fast
    # search. For a 4 MB log_tail this is microseconds; the old
    # bracket-walking approach was hundreds of ms.
    end_idx = log_tail.rfind(_ARTIFACT_END_SENTINEL)
    if end_idx != -1:
        # Search for the matching BEGIN before the LAST end (handles
        # the rare case of multiple artifact prints in one task —
        # take the last complete bundle).
        begin_idx = log_tail.rfind(_ARTIFACT_BEGIN_SENTINEL, 0, end_idx)
        if begin_idx != -1:
            payload_start = begin_idx + len(_ARTIFACT_BEGIN_SENTINEL)
            payload = log_tail[payload_start:end_idx].strip()
            try:
                obj = _json.loads(payload)
                # Sentinel-wrapped output is trusted shape — verify
                # required keys present (a malformed Containerizer
                # write would still need to fall back) and return.
                if isinstance(obj, dict) and all(k in obj for k in required_keys):
                    return obj
            except _json.JSONDecodeError:
                # Sentinels present but payload malformed → fall
                # through to brute-force walker.
                pass

    text = log_tail

    def _looks_like_artifacts(obj: Any) -> bool:
        return isinstance(obj, dict) and all(k in obj for k in required_keys)

    def _unwrap_nested(obj: Any) -> Any:
        """If a dict has only one key whose value is a JSON string
        that itself decodes to an artifacts dict, return the inner."""
        if not isinstance(obj, dict):
            return obj
        # Try every string-valued field — the LLM might wrap under
        # "dockerfile", "result", "output", or something else.
        for v in obj.values():
            if not isinstance(v, str):
                continue
            s = v.strip()
            if not (s.startswith("{") and s.endswith("}")):
                continue
            try:
                inner = _json.loads(s)
            except _json.JSONDecodeError:
                continue
            if _looks_like_artifacts(inner):
                return inner
        return obj

    # Scan brace-balanced candidates from the END of log_tail backwards.
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    for start in reversed(positions):
        depth = 0
        end = None
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        candidate = text[start:end + 1]
        try:
            obj = _json.loads(candidate)
        except _json.JSONDecodeError:
            continue

        # Direct hit: flat dict with all three keys.
        if _looks_like_artifacts(obj):
            src = obj
        else:
            # Nested-wrapper fallback: LLM put everything inside a
            # string-valued field. Peel one level.
            unwrapped = _unwrap_nested(obj)
            if not _looks_like_artifacts(unwrapped):
                continue
            src = unwrapped
        return {
            "dockerfile": src.get("dockerfile", ""),
            "entrypoint_sh": src.get("entrypoint_sh", ""),
            "build_and_push_sh": src.get("build_and_push_sh", ""),
            "image_name": src.get("image_name", ""),
            "full_image_reference": src.get("full_image_reference", ""),
            "technology_stack": src.get("technology_stack", ""),
            "base_image_reasoning": src.get("base_image_reasoning", ""),
            "additional_notes": src.get("additional_notes", ""),
            # Optional wizard-analysis side-output (containerizer
            # emits it alongside the Dockerfile when it runs with
            # OUTPUT_MODE=albedo). May be an empty dict if the
            # analysis failed; the wizard UI tolerates that.
            "analysis": src.get("analysis") or {},
            # Codebase pipeline carries its own 4th file (node metadata)
            # plus the build profile + validation report + optional
            # build-verify outcome. These are absent in legacy albedo.
            "node_metadata_py": src.get("node_metadata_py", ""),
            "codebase_build_profile": src.get("codebase_build_profile"),
            "codebase_validation": src.get("codebase_validation"),
            "codebase_build_verify": src.get("codebase_build_verify"),
            "codebase_scan_summary": src.get("codebase_scan_summary"),
            # GPU-variant Dockerfiles + the gpu_variants dict for the
            # DAG. Empty when the wizard picked a CPU-only base or when
            # the Containerizer predates variant synthesis.
            "dockerfile_variants": src.get("dockerfile_variants") or {},
            "gpu_variants": src.get("gpu_variants") or {},
            # P9n — per-variant kernel_porter artifacts. Empty when the
            # repo didn't trigger the deep-CUDA port pass. Passed
            # through verbatim; the port-diff endpoint (P9n.1) pairs
            # port_plans' source_transforms with source_files' bodies
            # to produce before/after diffs.
            "port_plans": src.get("port_plans") or {},
            "source_files": src.get("source_files") or [],
            "port_incompatible_reasons": src.get("port_incompatible_reasons") or {},
            "port_not_recommended": src.get("port_not_recommended") or {},
            # P9f.1 — per-variant adapted source bodies + adaptation
            # diagnostics. Containerizer emits these alongside
            # dockerfile_variants; without them, the Wizard's per-
            # variant tabs stay "pending" forever because the frontend
            # falls back to an empty source map for non-hero variants.
            #   source_variants: {variant: {filename: adapted_body}}
            #   variant_adaptations: {variant: {compat, incompatible,
            #                                   files: {filename: {changes,
            #                                   warnings, language, ...}}}}
            "source_variants": src.get("source_variants") or {},
            "variant_adaptations": src.get("variant_adaptations") or {},
            # Hero variant label (cuda/rocm/...) so the wizard can
            # render the correct tab order.
            "hero_variant": src.get("hero_variant") or "",
        }
    return None


def _extract_codebase_stages(log_tail: str) -> list[Dict[str, Any]]:
    """Parse the `{"stage":..., "status":..., ...}` JSON lines emitted
    by tools/codebase_pipeline.py. Returns them in emission order so
    the wizard UI can render a live timeline without polling the log
    itself."""
    import json as _json
    if not log_tail:
        return []
    events: list[Dict[str, Any]] = []
    for line in log_tail.splitlines():
        s = line.strip()
        if not s.startswith('{"stage"'):
            continue
        try:
            obj = _json.loads(s)
        except _json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "stage" in obj and "status" in obj:
            events.append(obj)
    return events


class BuildLocalRequest(PydanticBaseModel):
    """Request to build user-approved Albedo-pattern artifacts and push
    to the local registry.

    The three LLM-generated files (dockerfile, entrypoint_sh,
    build_and_push_sh) are taken verbatim as the user edited them — we
    hand them to ImageBuilder and admission guards catch anything
    unsafe.

    The 4th artifact (node metadata) is different: the user edits
    METADATA FIELDS (name, description, process_type, cost, ...) in
    the wizard form, not raw Python bytes. The server re-renders the
    `.py` here from those fields via `utils.metadata_template.render` — the
    same module the client used for its live preview. Byte-for-byte
    parity between preview and committed is the "double-check":
    whatever the user saw in the preview IS what ships, and the server
    owns the final bytes so a malicious client can't slip arbitrary
    Python into the METADATA BLOCK.
    """
    image_name: str
    node_name: str
    dockerfile: str
    entrypoint_sh: str
    build_and_push_sh: str
    input_type: str = "image"
    output_type: str = "image"
    process_type: str = "CPU"
    cost_in_credits: int = 1
    description: Optional[str] = None
    container_env: Optional[Dict[str, str]] = None
    # Editable metadata fields for the 4th artifact (DAG .py file).
    # Server re-renders via utils.metadata_template regardless of any raw
    # .py bytes the client might try to include — metadata is the
    # contract; the rendered file is derived.
    node_metadata: Optional[Dict[str, Any]] = None
    # Per-GPU Dockerfile bodies emitted by the wizard (one entry per
    # enabled hardware tag in Settings → GPU Compatibility). Empty /
    # absent means a legacy single-Dockerfile commit. ImageBuilder
    # materialises each into Dockerfile.<variant> and builds + pushes
    # `<image>:latest-<variant>` for every one.
    dockerfile_variants: Optional[Dict[str, str]] = None
    # {variant: registry-qualified-image:tag} dict written into the
    # node metadata so the scheduler can dispatch tasks to the right
    # image per worker hardware. ImageBuilder rederives this from what
    # it actually built, but we pre-populate here from the wizard so
    # the metadata render includes it.
    gpu_variants: Optional[Dict[str, str]] = None
    # Per-variant adapted source files. Shape:
    #   {variant_name: {filename: adapted_body}}
    # Produced by Containerizer's variant_adapter (see
    # CORE/DockerContainers/Containerizer/scripts/tools/variant_adapter.py).
    # The hero source targets one backend — adapting the Python (or R,
    # Julia, shell, ...) per variant means each image ships code that
    # actually works on its hardware. ImageBuilder's build loop swaps
    # the relevant files into the build context before each variant's
    # docker build via _swap_source_for_variant. When absent, hero
    # sources are used for every variant (legacy behaviour).
    source_variants: Optional[Dict[str, Dict[str, str]]] = None
    # γ — real-data sample loop. When the user uploaded a sample
    # input file in the wizard's Source step, ship it through here.
    # The imagebuilder, AFTER smoke passes but BEFORE registration,
    # mounts the sample at /data/input/<filename> and runs the
    # image's entrypoint normally. Captured stdout + /data/output/*
    # files come back in the build-result.json so the wizard can
    # render them for the user's thumbs-up/down review.
    # ``defer_registration=True`` tells the imagebuilder to STOP at
    # the sample-test stage and wait for the user's explicit
    # approval via /finalize-registration. Default False preserves
    # the existing auto-register flow.
    sample_input_filename: Optional[str] = None
    sample_input_body_b64: Optional[str] = None
    # project-agent §1.1 — the output the requester EXPECTS for the
    # sample input above. When supplied, the hero variant's sample
    # test is graded against it and MIN_PUBLISH_CONFIDENCE becomes
    # load-bearing; when absent the hero scores 1.0 for producing any
    # bytes at all, and the build is recorded as unanchored (§1.2)
    # rather than as a pass.
    sample_output_filename: Optional[str] = None
    sample_output_body_b64: Optional[str] = None
    defer_registration: bool = False
    # User-authored source files (multi-file input). Each entry:
    #   {filename, body, language?, is_entrypoint?}
    # Back-compat: if the wizard used the legacy single-paste flow,
    # the Containerizer synthesises a one-element list from the
    # pasted body + default filename.
    source_files: Optional[List[Dict[str, Any]]] = None
    # Per-variant diagnostic metadata, keyed by variant name:
    #   {compat, incompatible, incompatible_reason,
    #    files: {filename: {changes[], warnings[], language,
    #                       incompatible, incompatible_reason, llm_used}}}
    # Rendered in the Wizard Preview (per-variant tile with per-file
    # diff sub-tabs) and persisted so operators can audit what the
    # adapter changed later. Variants flagged incompatible at the
    # whole-node level are already dropped from dockerfile_variants
    # and gpu_variants by the Containerizer, but kept here for
    # visibility.
    variant_adaptations: Optional[Dict[str, Dict[str, Any]]] = None
    # Optional glue artifact attached by the wizard's per-step LLM
    # Q&A flow (issue #18). When present, ImageBuilder writes it
    # alongside the canonical Dockerfile / entrypoint.sh in the
    # build context so the build picks it up. When absent the build
    # proceeds with the original (non-glued) artifacts. Schema:
    #   {"artifact_type": "entrypoint.sh" | "Dockerfile.glue" |
    #                     "glue.sh" | "Containerfile.glue",
    #    "content": str, "description": str}
    glue_artifact: Optional[Dict[str, Any]] = None


def _get_or_provision_tenant_harbor_creds(tenant_id: int) -> tuple[str, str, str]:
    """Return ``(registry_url, username, password)`` for pushing to the
    tenant's Harbor project. Reads from Vault at
    ``secret/data/tenants/{id}/tenant_harbor``; if the key is missing
    (e.g. tenant was created before Phase 4 shipped), auto-provisions
    the project + robot on the fly and writes the creds back to Vault.

    Returns ``("", "", "")`` if anything goes wrong — caller falls back
    to the legacy registry path (dev-only, no remote-worker support).
    """
    import os
    try:
        from utils.vault_client import approle_login, read_kv_v2, write_kv_v2
    except Exception:
        return "", "", ""

    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not role_id or not secret_id:
        logger.warning("Vault AppRole creds missing — cannot resolve tenant Harbor push creds")
        return "", "", ""

    try:
        token = approle_login(role_id=role_id, secret_id=secret_id)
        existing = read_kv_v2(f"tenants/{tenant_id}/tenant_harbor", token=token)
        if existing and existing.get("password") and existing.get("username"):
            return (
                existing.get("registry", "") or "",
                existing.get("username", "") or "",
                existing.get("password", "") or "",
            )
    except Exception as e:
        logger.info("Vault read for tenant %s Harbor creds failed (will try to provision): %s", tenant_id, e)
        token = None

    # No creds in Vault → provision project + robot now.
    try:
        from utils.tenant_harbor_client import TenantHarborClient, project_name_for_tenant
        harbor = TenantHarborClient()
        harbor.create_project(tenant_id)
        # See crud/tenant_cud.py for the rationale on prevent_vul=False
        # (Debian "won't-fix" Critical CVEs → 412 on every pull).
        harbor.set_project_vulnerability_policy(
            tenant_id, severity="Critical", prevent_vul=False,
        )
        robot = harbor.create_robot(
            tenant_id, name=f"mod-push-{tenant_id}", actions=["push", "pull"]
        )
        registry_url = (
            os.getenv("TENANT_HARBOR_EXTERNAL_URL")
            or os.getenv("TENANT_HARBOR_INTERNAL_URL")
            or "http://tenant-harbor-core:8080"
        )
        if token is None:
            token = approle_login(role_id=role_id, secret_id=secret_id)
        write_kv_v2(
            f"tenants/{tenant_id}/tenant_harbor",
            token=token,
            data={
                "username": robot.username,
                "password": robot.password,
                "registry": registry_url,
                "project": project_name_for_tenant(tenant_id),
            },
        )
        logger.info("Auto-provisioned tenant Harbor project + robot for tenant %s", tenant_id)
        return registry_url, robot.username, robot.password
    except Exception as e:
        logger.warning(
            "Auto-provisioning tenant Harbor creds failed for tenant %s: %s",
            tenant_id, e,
        )
        return "", "", ""






# ============================================================================
# External node-authoring API (open-core wizard replacement)
# ----------------------------------------------------------------------------
# A thin, stable facade over the existing registration/build/admission/scan
# internals. No server-side LLM/codegen. Authenticated by an API key carrying
# the `node:author` scope; identity/tenant/role are derived from the key and
# never trusted from the body, so a caller can only create private drafts.
# ============================================================================







def _load_node_contract() -> dict:
    """Load the shipped node-contract reference doc (version + markdown)."""
    import re as _re
    # NOTE: this module was split one directory deeper (routers/ -> routers/
    # node_definitions_domains/) in Gate 2a, so resolving the app root from
    # __file__ needs THREE dirname() calls, not two, to reach /app.
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "docs", "node_contract.md")
    try:
        with open(path, "r") as f:
            content = f.read()
    except Exception:
        return {"version": None, "content": ""}
    m = _re.search(r"node-contract-version:\s*(\d+)", content)
    return {"version": int(m.group(1)) if m else None, "content": content}








# LDF-3 §7 — share-with-tenant: promote a user's private node to
# tenant-shared so every other user in the same tenant can see it.
# Mirrors the `crud.promote_node_definition_to_tenant` helper, with
# the same tenancy guardrails the rest of the router enforces.


# NOTE: `GET /status` + `GET /wizard-llm-status` live at the top of the
# router (above the /{node_definition_id} catch-all) to avoid 422s on
# the int-path coercion. Don't re-add duplicates here.

# --- preserve pre-split contract identity (see module docstring) ---
_CANONICAL_MODULE = "routers.node_definitions"
for _name in __all__:
    _obj = globals().get(_name)
    if getattr(_obj, "__module__", None) == __name__:
        try:
            _obj.__module__ = _CANONICAL_MODULE
        except (AttributeError, TypeError):
            pass
del _name, _obj
