"""Worker discovery, licensing, and management endpoints"""

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from dependencies import get_db, get_current_user
from database import SessionLocal
from models import WorkerLicense, WorkerStatus, User, WorkerOwnerType, WorkerEnrollment, WorkerEnrollmentStatus, WorkerEnrollmentToken, ApiTaskQueue, ApiTaskStatus, WorkerResourceTimeline
from utils import residency as _residency
from utils.worker_arch import normalize_arch as _wm_normalize_arch
from utils.registries import platform_registry, platform_image, configured_platform_registry
from utils.http_errors import internal_http_error
import os
import logging
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
import secrets as py_secrets
from utils.paid_import import optional_paid, require_paid

_license_client = optional_paid("utils.license_client")

# CE fallback for the worker-management license endpoints (owner 2A, 1645e).
# Call sites at lines ~4421, ~4494, ~4931 all use license_status.get(...) so
# the shape here must match the paid module's return dict. workers_max must
# stay an int (not None): assign_license() does `licensed_count >= max_workers`
# and get_license_summary_view() does `workers_max - licensed_count` — a None
# there raises TypeError (verified), always 500ing CE's assign-license and
# license-summary endpoints. Use the codebase's existing "unlimited" sentinel
# (utils.entitlements.UNLIMITED) instead of a raw None.
def get_license_status():
    """Return license status dict; CE returns unlimited valid status."""
    if _license_client is not None:
        return _license_client.get_license_status()
    from utils.entitlements import UNLIMITED
    return {
        "valid": True,
        "edition": "ce",
        "workers_max": UNLIMITED,  # unlimited, int (not None) — see note above
        "development_mode": False,
    }


from security import hash_api_key, verify_api_key
from utils.vault_client import (
    VaultError,
    approle_login,
    read_kv_v2,
    read_database_creds,
)

import crud.worker_management as worker_management_crud
import services.worker_management_service as worker_management_service
import services.worker_host_exec_service as worker_host_exec_service
# Shared worker-enrollment auth gate (X-Worker-Name / X-Bootstrap-Token), the
# same one every /workers/tasks/... endpoint uses — see routers/worker_tasks.py.
# Imported lazily-safe: worker_tasks only references worker_management inside
# function bodies, so this top-level import doesn't create a load cycle.
from routers.worker_tasks import require_enrolled_worker

# NOTE: Do not call logging.basicConfig() in routers.
# The app configures logging + secret redaction centrally in API/main.py.
from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

# Check if debug mode is enabled (may expose detailed worker info)
from services.debug_mode import is_debug_mode

router = APIRouter()


# Workers are Go-based and report via heartbeat API.


# Response models. Every field is Optional with a None default and extra keys
# are kept, so these document the wire shape without being able to reject or
# strip a response. Nested payloads stay plain Dict/List (never sub-models) so
# no key inside them can be dropped. The live Go worker decodes the heartbeat
# response (CORE/Worker/internal/heartbeat/heartbeat.go), so its shape must not
# change; routes using these set response_model_exclude_unset=True so keys a
# handler does not return are not added as nulls.


class EnrollmentTokenValidateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    valid: Optional[bool] = None
    auto_approve: Optional[bool] = None
    auto_license: Optional[bool] = None
    uses_remaining: Optional[int] = None


class WorkerConfigResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    api_url: Optional[str] = None
    minio_url: Optional[str] = None
    keycloak_url: Optional[str] = None
    timestamp: Optional[str] = None


class WorkerHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    # Only on the not-enrolled early return.
    message: Optional[str] = None
    worker_name: Optional[str] = None
    licensed: Optional[bool] = None
    stack_fence_lease_s: Optional[int] = None
    update_available: Optional[bool] = None
    latest_version: Optional[str] = None
    server_updating: Optional[bool] = None
    paused: Optional[bool] = None
    update_requested: Optional[bool] = None
    drain: Optional[bool] = None
    # worker-host-exec — the authenticated worker's numeric WorkerLicense id.
    # Set ONLY on the enrolled success path (never on the not-enrolled early
    # return, never echoed from a request field), so a worker can key its own
    # per-worker state (DB-side task rows, audit, claims) on a stable id instead
    # of re-parsing its name.
    worker_id: Optional[int] = None
    tasks_to_cancel: Optional[List[Any]] = None
    images_to_prepull: Optional[List[Any]] = None
    active_task_count: Optional[int] = None
    max_concurrent_tasks: Optional[int] = None
    docker_registry: Optional[str] = None
    mesh_managers: Optional[List[Any]] = None
    mesh_rehome: Optional[Dict[str, Any]] = None
    mesh_site: Optional[Dict[str, Any]] = None
    noah: Optional[Dict[str, Any]] = None
    # GS.PSR.7 — worker tunables sourced from the platform_settings registry.
    # Only present when at least one tunable has a live DB value; see
    # _resolve_worker_tunables. Absent means "unconfigured", not "cleared" —
    # the worker keeps its own local env/default, so an untouched fleet sees
    # no behavior change.
    tunables: Optional[Dict[str, Any]] = None
    # GSEC Q21.4 — per-worker Vault AppRole status (approle_status output:
    # masked secret fingerprints only, NEVER the secret values). Present
    # only while worker.approle_per_worker_enforced != "off"; absent in the
    # default (off) posture so an untouched fleet's heartbeat is unchanged.
    # Delivery of the actual per-worker secret_id is Q21.8's design and is
    # intentionally NOT part of this response.
    vault: Optional[Dict[str, Any]] = None


class WorkerRehomeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    site_login_server: Optional[str] = None
    epoch: Optional[str] = None
    reason: Optional[str] = None


class WorkerConcurrencyResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    max_concurrent_tasks: Optional[int] = None


class NoahPlatformSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: Optional[bool] = None
    sustained_quiet_s: Optional[int] = None
    pause_grace_s: Optional[int] = None
    plugged_in_only: Optional[bool] = None
    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    batch_appetite: Optional[int] = None
    stale_release_s: Optional[int] = None
    # Pinnable starvation visibility (2026-09-29) — how many currently-online
    # workers satisfy `pinnable` right now (services/noah.py:
    # count_pinnable_workers_online), and a human-readable warning when that
    # count is 0 (every online worker is a NOAH idle-harvest host or an
    # explicit `not-pinnable` opt-out) — the same condition that makes
    # add_pinnable_requirement's starvation guard skip the requirement
    # instead of stranding pin-prone tasks.
    pinnable_workers_online: Optional[int] = None
    pinnable_starvation_warning: Optional[str] = None


class NoahStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None


class NoahFleetResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    platform_enabled: Optional[bool] = None
    totals: Optional[Dict[str, Any]] = None
    workers: Optional[List[Any]] = None


class WorkerNoahSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    # get only; set returns worker_name + effective.
    overrides: Optional[Dict[str, Any]] = None
    effective: Optional[Dict[str, Any]] = None
    available: Optional[bool] = None
    reason: Optional[str] = None


class WorkerInfo(BaseModel):
    """Worker information response"""
    id: int
    worker_name: str
    hostname: str
    hardware_specs: Optional[dict]
    queues: Optional[List[str]]
    status: str
    owner_type: Optional[str] = None
    owner_tenant_id: Optional[int] = None
    assigned_tenant_ids: Optional[List[int]] = None
    licensed_at: Optional[datetime]
    licensed_by_username: Optional[str]
    first_seen_at: datetime
    last_heartbeat: Optional[datetime]
    is_online: bool
    notes: Optional[str]
    benchmark_status: Optional[str] = None
    benchmarked_at: Optional[datetime] = None
    # Drain / pause — per-worker flag flipped by Dashboard Pause button.
    paused: bool = False
    # One-shot update request — set by Dashboard Update button, auto-cleared
    # when a heartbeat arrives with a new agent_version.
    update_requested: bool = False
    # Current vs. latest agent binary — enables the Dashboard to show
    # "Up to date" vs "Update available" on the Update button.
    agent_version: Optional[str] = None
    latest_agent_version: Optional[str] = None
    update_available: bool = False
    # Is it safe to Pause / Update this worker right now? False when doing so
    # would leave the system with zero active workers AND this isn't the
    # single-node embedded exception. The Dashboard uses this to disable the
    # buttons with an explanatory tooltip instead of waiting for a 409.
    can_drain: bool = True
    # GY.V3b — data-residency zone, COMPUTED from the worker's hardware tag
    # ('region:us' / 'region:eu' in hardware_specs['hardware_tags']).
    # "unknown" = no residency tag, which the claim gate counts as outside
    # any residency pin (fail closed). No column — computed per response.
    residency_zone: str = "unknown"
    # Gate 7 — what this machine costs its operator to run, so its work can be
    # priced on reality rather than a hand-set integer. NULL means "not priced
    # yet", which the UI must show as unknown rather than free.
    cost_per_hour_cents: Optional[int] = None
    hardware_cost_cents: Optional[int] = None
    amortisation_months: Optional[int] = None
    power_watts: Optional[int] = None
    electricity_cents_per_kwh: Optional[int] = None
    # What to charge on top. NULL takes the platform default, so the UI shows
    # the EFFECTIVE billed rate rather than implying an unset margin is zero.
    margin_percent: Optional[int] = None
    billed_per_hour_cents: Optional[int] = None
    can_drain_reason: Optional[str] = None
    # Enrollment status — separate lifecycle from license status. If the
    # enrollment row is REVOKED the worker cannot re-enrol without an admin
    # clicking Reinstate (POST /workers/enrollments/{name}/reinstate).
    enrollment_status: Optional[str] = None
    # 993c2 — per-worker host-exec availability for the Dashboard (993d) to
    # show "why isn't this working" instead of a silent no-op. Shape mirrors
    # services/worker_host_exec_service.py:resolve_availability()'s return:
    # {code, available, mode, message, fix}. None on a pre-resolver error
    # (never a 500 on the worker list) or if resolve_availability itself
    # raises.
    host_exec_status: Optional[Dict[str, Any]] = None
    # Tim B3 item 4 — is this the embedded (manager-node) worker? Platform-level
    # identity derived from main's trusted seed-name set ONLY (never tenant-
    # derived, never tenant-scoped-widened, never request-supplied). Default
    # False = fail-closed for every non-embedded row and every older caller.
    # The Dashboard shows this row FIRST, as the internal system.
    is_embedded: bool = False


class LicenseAssignmentRequest(BaseModel):
    """Request to assign or revoke license"""
    worker_name: str
    owner_type: str = "platform_pool"  # platform_only, platform_pool, platform_assigned, tenant_owned
    owner_tenant_id: Optional[int] = None  # Required for tenant_owned
    assigned_tenant_ids: List[int] = []  # Required for platform_assigned


class WorkerEnrollStartRequest(BaseModel):
    worker_name: str
    hostname: Optional[str] = None
    fingerprint: Optional[dict] = None
    enrollment_token: Optional[str] = None


class WorkerEnrollStartResponse(BaseModel):
    enrollment_id: int
    approval_code: str
    bootstrap_token: str
    status: str
    # dual-anchor-mesh-federation §6.1 — the worker pins its api_url to this
    # stable mesh/magic name so a manager move needs no re-enrollment. None on a
    # single-anchor install (the worker keeps its FQDN api_url).
    manager_mesh_name: Optional[str] = None
    home_anchor: str = "local"
    # §6.2 — the mesh coordinates setup-worker.sh reads to join the tailnet in the
    # SAME enrollment step (so the operator never mints a raw headscale key). The
    # key is minted server-side per worker against the token's home anchor and
    # returned in the matching slot; the other slot stays null (single-home).
    mesh_hostname: Optional[str] = None
    site_login_server: Optional[str] = None
    site_authkey: Optional[str] = None
    cloud_login_server: Optional[str] = None
    cloud_authkey: Optional[str] = None


class WorkerEnrollConfirmRequest(BaseModel):
    worker_name: str
    approval_code: str
    bootstrap_token: str


class WorkerEnrollStatusResponse(BaseModel):
    worker_name: str
    status: str
    approved: bool
    last_seen_at: Optional[datetime] = None


# The Go worker uses bootstrap tokens for direct API auth — no
# credential-bundle handoff endpoint.


def _normalize_worker_name(name: str) -> str:
    return (name or "").strip()


def _new_approval_code() -> str:
    # Human-friendly code, not a secret by itself (still treat as sensitive).
    return f"{py_secrets.randbelow(1_000_000):06d}"


def _new_bootstrap_token() -> str:
    # High-entropy one-time secret
    return py_secrets.token_urlsafe(32)


def _validate_enrollment_token(db: Session, token_plain: str, worker_name: str) -> WorkerEnrollmentToken:
    """
    Validate an enrollment token. Returns the token record or raises HTTPException.
    """
    return worker_management_service.validate_enrollment_token(db, token_plain, worker_name)


class WorkerCostResponse(BaseModel):
    """Response for worker cost endpoint."""
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    cost_per_hour_cents: Optional[int] = None
    hardware_cost_cents: Optional[int] = None
    amortisation_months: Optional[int] = None
    power_watts: Optional[int] = None
    electricity_cents_per_kwh: Optional[int] = None
    margin_percent: Optional[int] = None
    effective_margin_percent: Optional[int] = None
    billed_per_hour_cents: Optional[int] = None


class NetworkZoneResponse(BaseModel):
    """Response for network zone list."""
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    name: Optional[str] = None


class WorkerNetworkZoneResponse(BaseModel):
    """Response for setting worker network zone."""
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    network_zone: Optional[str] = None


class LicenseAssignmentResponse(BaseModel):
    """Response for license assignment."""
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    message: Optional[str] = None
    licensed_count: Optional[Any] = None
    max_workers: Optional[Any] = None


class WorkerDeleteResponse(BaseModel):
    """Response for worker deletion."""
    model_config = ConfigDict(extra="allow")
    detail: Optional[str] = None
    worker_name: Optional[str] = None


class WorkerAppRoleRotateResponse(BaseModel):
    """GSEC Q21.4 — masked result of a per-worker AppRole rotation.

    NEVER carries the secret_id / role_id values: the fresh secret is
    delivered to the worker out-of-band (Q21.5 install render / Q21.8
    two-secret window), this endpoint only reports that the rotation
    happened and when the previous secret lapses."""
    model_config = ConfigDict(extra="allow")
    worker_id: Optional[int] = None
    worker_name: Optional[str] = None
    role: Optional[str] = None
    rotated: Optional[bool] = None
    prev_secret_id_valid_until: Optional[str] = None


class WorkerPauseResumeResponse(BaseModel):
    """Response for worker pause/resume."""
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    worker_name: Optional[str] = None
    paused: Optional[bool] = None
    message: Optional[str] = None


class WorkerReleaseInfo(BaseModel):
    """Response for latest worker release info."""
    model_config = ConfigDict(extra="allow")
    version: Optional[str] = None
    released_at: Optional[str] = None
    changelog_url: Optional[str] = None
    amd64_sha256: Optional[str] = None
    arm64_sha256: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None


class WorkerUpdateResponse(BaseModel):
    """Response for worker update trigger."""
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    worker_name: Optional[str] = None
    update_requested: Optional[bool] = None
    current_version: Optional[str] = None
    latest_version: Optional[str] = None
    message: Optional[str] = None


class TokenValidateRequest(BaseModel):
    enrollment_token: str
    worker_name: Optional[str] = None


# Response models for the admin queue/capacity/enrollment/benchmark routes.
# Same contract as above: extra keys kept, exclude_unset so no null keys are
# added (get_worker_hardware_profile omits `arch` when there is no profile).


class ReinstateEnrollmentResponse(BaseModel):
    """Response for reinstating an enrollment."""
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    worker_name: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None


class FlushTenantAdapterCacheResponse(BaseModel):
    """Response for flushing a tenant adapter cache."""
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    tenant_id: Optional[int] = None
    new_salt: Optional[int] = None
    message: Optional[str] = None


class WorkerCleanupZombieResponse(BaseModel):
    """Response for cleaning up zombie workers."""
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    cleaned_count: Optional[int] = None
    cleaned_workers: Optional[List[Any]] = None
    cutoff_date: Optional[str] = None


class LicenseSummaryResponse(BaseModel):
    """Response for license summary."""
    model_config = ConfigDict(extra="allow")
    license: Optional[Dict[str, Any]] = None
    workers: Optional[Dict[str, Any]] = None


class PlatformQueueResponse(BaseModel):
    """Response for platform queue view."""
    model_config = ConfigDict(extra="allow")
    in_flight: Optional[List[Any]] = None
    pending: Optional[List[Any]] = None
    by_queue: Optional[Dict[str, Any]] = None
    counts: Optional[Dict[str, Any]] = None


class WorkerQueueResponse(BaseModel):
    """Response for worker queue view."""
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    in_flight: Optional[List[Any]] = None
    assigned: Optional[List[Any]] = None


class PlatformCapacityResponse(BaseModel):
    """Response for platform capacity view."""
    model_config = ConfigDict(extra="allow")
    total_capacity: Optional[Dict[str, Any]] = None
    licensed_capacity: Optional[Dict[str, Any]] = None
    gpu_by_backend: Optional[Dict[str, Any]] = None
    utilization: Optional[Dict[str, Any]] = None
    performance_metrics: Optional[Dict[str, Any]] = None
    queue_health: Optional[List[Any]] = None
    workers_detail: Optional[List[Any]] = None
    workers: Optional[List[Any]] = None
    running_tasks: Optional[int] = None
    active_workers: Optional[int] = None
    total_pool: Optional[int] = None
    failed_tasks_24h: Optional[int] = None
    unrunnable_tasks: Optional[Dict[str, Any]] = None


class RevokeEnrollmentTokenResponse(BaseModel):
    """Response for revoking an enrollment token."""
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None
    token_id: Optional[int] = None


class WorkerHardwareProfileResponse(BaseModel):
    """Response for worker hardware profile."""
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    agent_version: Optional[str] = None
    hardware_tags: Optional[List[Any]] = None
    arch: Optional[str] = None
    hardware_profile: Optional[Dict[str, Any]] = None
    # 25.5.s5 — read-only view of the worker's last self-cleanup pass RESULT
    # (hardware_specs['last_cleanup'], written by the heartbeat above). The
    # profile describes how fast the machine is; this says what its own GC
    # last got back. None until the worker has run a pass.
    last_cleanup: Optional[Dict[str, Any]] = None
    # MAC.1.s13b — read-only view of the worker's pre-claim free-disk gate
    # reading (hardware_specs['disk_floor'], written by the heartbeat above):
    # {state, free_gb, needed_gb, work_root}. The profile says how fast the
    # machine is; this answers "why does this green worker claim nothing?" —
    # disk pressure, not a bug. Absent key = the worker never probed.
    disk_floor: Optional[Dict[str, Any]] = None


class TriggerBenchmarkResponse(BaseModel):
    """Response for triggering a benchmark."""
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    worker_name: Optional[str] = None
    only: Optional[str] = None


@router.post("/workers/enroll/validate-token", response_model=EnrollmentTokenValidateResponse, response_model_exclude_unset=True)
async def validate_enrollment_token(
    request: TokenValidateRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """
    Check if an enrollment token is valid WITHOUT consuming a use.
    Returns 200 if valid, 401 if expired/used/invalid.
    Used by the setup script to fail fast before installing anything.
    """
    from utils.rate_limiter import check_rate_limit
    # 1788d CE carve: services/client_ip.py is paid (manifest deny 979) while
    # utils/client_ip.py is api_allow. The IP is only a rate-limit KEY here
    # (auth is the enrollment/bootstrap token), so CE falls back to the CE-shipped
    # resolver and, if that is unusable, to "unknown" - never a 500, and an
    # unresolvable IP never refuses the request.
    from utils.paid_import import optional_paid
    _ip_mod = optional_paid("services.client_ip")
    if _ip_mod is None:
        try:
            from utils.client_ip import trusted_client_ip as _trusted_client_ip
            client_ip = _trusted_client_ip(http_request) or "unknown"
        except Exception:  # noqa: BLE001 - an unresolvable IP must not refuse
            client_ip = "unknown"
    else:
        client_ip = _ip_mod.resolve_client_ip(http_request) or "unknown"
    check_rate_limit(f"enroll:{client_ip}", max_requests=5, window_seconds=60)

    worker_name = _normalize_worker_name(request.worker_name or "check")
    try:
        token = _validate_enrollment_token(db, request.enrollment_token, worker_name)
        return {
            "valid": True,
            "auto_approve": token.auto_approve,
            "auto_license": token.auto_license,
            "uses_remaining": (token.max_uses - (token.use_count or 0)) if token.max_uses else None,
        }
    except HTTPException as e:
        raise e


def _enforce_worker_node_limit(db: Session) -> None:
    """Node-count enforcement chokepoint (mod-license-enforcement / open-core
    guard §2.6): a NEW worker adds a node, so gate it against the deployment's
    licensed ``max_worker_nodes``. PAID (a license client exists): the
    entitlement resolver below, unchanged. CE (no license client): unlimited,
    no gate. Resolves via the
    shared resolver; SaaS deployments have unlimited node counts, so this is a
    no-op there. Fail-OPEN on any gate error — a malfunctioning counter/resolver
    must never wedge enrollment (matches the warn-default, non-breaking rollout).
    """
    try:
        from utils.entitlements import get_entitlements, evaluate_limit, Limit, is_saas
        # SaaS: nodes are MOD-managed multi-tenant infra, not a licensed quota.
        # Skip entirely — the resolver here has no tenant and would otherwise fall
        # to the baseline entitlements, which SaaS hard-denies regardless of rollout mode.
        if is_saas():
            return
        # CE: no paid license client (utils/license_client.py is manifest-denied
        # in CE) -> unlimited worker systems; no cap lives in the open code.
        if _license_client is None:
            return
        es = get_entitlements(db=db)
        used = _license_client.count_workers()
        decision = evaluate_limit(Limit.WORKER_NODES, used=used, es=es, requesting=1)
    except Exception:  # noqa: BLE001
        logger.exception("worker node-count gate errored; allowing enrollment (fail-open)")
        return
    if not decision.allowed:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "worker_node_limit_reached",
                "limit": Limit.WORKER_NODES,
                "reason": decision.reason,
                "tier": es.tier_name,
            },
        )


@router.post("/workers/enroll/start", response_model=WorkerEnrollStartResponse)
async def worker_enroll_start(
    request: WorkerEnrollStartRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """
    Start worker enrollment.

    Requires a valid enrollment token (created by admin or system).
    Rate-limited to 5 attempts per minute per IP.

    If the token has auto_approve=True, the worker is instantly APPROVED.
    If the token has auto_license=True, a WorkerLicense is also created.
    """
    from utils.rate_limiter import check_rate_limit
    # 1788d CE carve: services/client_ip.py is paid (manifest deny 979) while
    # utils/client_ip.py is api_allow. The IP is only a rate-limit KEY here
    # (auth is the enrollment/bootstrap token), so CE falls back to the CE-shipped
    # resolver and, if that is unusable, to "unknown" - never a 500, and an
    # unresolvable IP never refuses the request.
    from utils.paid_import import optional_paid
    _ip_mod = optional_paid("services.client_ip")
    if _ip_mod is None:
        try:
            from utils.client_ip import trusted_client_ip as _trusted_client_ip
            client_ip = _trusted_client_ip(http_request) or "unknown"
        except Exception:  # noqa: BLE001 - an unresolvable IP must not refuse
            client_ip = "unknown"
    else:
        client_ip = _ip_mod.resolve_client_ip(http_request) or "unknown"
    check_rate_limit(f"enroll:{client_ip}", max_requests=5, window_seconds=60)

    worker_name = _normalize_worker_name(request.worker_name)
    if not worker_name:
        raise HTTPException(status_code=400, detail="worker_name is required")

    if not request.enrollment_token:
        raise HTTPException(status_code=401, detail="enrollment_token is required")

    # FX.126 (Q113=A) — an enrollment attempt that fails token validation
    # (unknown device, expired token, max uses, name-pattern mismatch) is
    # an unknown/unapproved device trying to join the fleet: emit one
    # admin alert via the existing audit/security path, rate-limited per
    # worker_name (services.asset_inventory.emit_enrollment_alert). The
    # original error is always re-raised unchanged.
    def _enroll_alert_reason(exc: HTTPException) -> str:
        d = str(getattr(exc, "detail", "") or "")
        if "expired" in d.lower():
            return "token_expired"
        if "max uses" in d.lower():
            return "token_max_uses"
        if "reserved" in d.lower():
            return "reserved_name"
        if exc.status_code == 403:
            return "name_pattern_mismatch"
        if exc.status_code == 409:
            return "revoked_worker"
        return "unknown_device"

    try:
        enrollment_token = _validate_enrollment_token(
            db, request.enrollment_token, worker_name)
    except HTTPException as exc:
        # 1788d CE carve: services/asset_inventory.py is paid (manifest deny
        # 959). The enrollment alert is an audit side effect only; with it
        # absent the refusal itself (raise below) is unchanged.
        from utils.paid_import import optional_paid
        _asset_inv = optional_paid("services.asset_inventory")
        if _asset_inv is not None:
            _asset_inv.emit_enrollment_alert(
                db, worker_name=worker_name,
                reason=_enroll_alert_reason(exc), request=http_request,
            )
        raise

    now = datetime.utcnow()

    approval_code = _new_approval_code()
    bootstrap_token = _new_bootstrap_token()
    bootstrap_hash = hash_api_key(bootstrap_token)

    # Re-enrolment of an existing worker_name is a legitimate flow — operators
    # re-run setup-worker.sh to upgrade binaries or rotate the bootstrap token.
    # Previously this path unconditionally reset status/approved_at/approved_by,
    # silently wiping any admin approval. That's the bug that kept putting
    # worker-example-host back to PENDING after every re-run. Per-state policy now:
    #
    #   APPROVED  — preserve status + approved_at + approved_by_user_id.
    #               Rotate bootstrap token + approval code (intended side
    #               effect of re-running setup). Admin approval persists.
    #   PENDING   — remain PENDING unless the enrolment token auto-approves.
    #               Rotate bootstrap token. Operator still needs to approve.
    #   REVOKED   — refuse. 409 Conflict. An operator must explicitly
    #               reinstate or delete the row before re-enrolling, so
    #               possession of the enrolment token alone cannot silently
    #               un-revoke a worker.
    #
    # NEW worker → it adds a node, so a node-limit gate runs before creating the
    # enrollment; re-enrollment is exempt (it replaces an existing node, it
    # doesn't add one). `_enforce_worker_node_limit` stays defined in THIS module
    # (tests call it as `worker_management._enforce_worker_node_limit`), so it's
    # passed into the service rather than imported there.
    enrollment = worker_management_service.enroll_worker_start(
        db,
        worker_name=worker_name,
        hostname=request.hostname,
        fingerprint=request.fingerprint,
        enrollment_token=enrollment_token,
        approval_code=approval_code,
        bootstrap_token_hash=bootstrap_hash,
        now=now,
        enforce_node_limit=_enforce_worker_node_limit,
    )

    # FX.126 (Q113=A) — a device that landed PENDING (valid token, but the
    # token does not auto-approve) is unapproved until an admin acts: one
    # rate-limited admin alert so the queue doesn't silently grow.
    if enrollment.status == WorkerEnrollmentStatus.PENDING:
        # 1788d CE carve: asset_inventory is paid - the admin alert is an audit
        # side effect; the PENDING enrollment row itself is unchanged.
        from utils.paid_import import optional_paid
        _asset_inv = optional_paid("services.asset_inventory")
        if _asset_inv is not None:
            _asset_inv.emit_enrollment_alert(
                db, worker_name=worker_name,
                reason="pending_enrollment", request=http_request,
                tenant_id=getattr(enrollment_token, "owner_tenant_id", None),
            )

    # §6.2 — mint the worker's mesh preauth keys HERE (per worker) and hand back the
    # coordinates setup-worker.sh reads, so the tailnet join happens in this one
    # enrollment step. The operator never sees a raw headscale key.
    #
    # DUAL-HOME EVERY worker on BOTH planes when both are configured: a "local"
    # worker also joins the CLOUD anchor so a ROAMING manager can still reach it
    # after leaving the site, and a "remote" worker also joins the SITE anchor when
    # co-located. `home_anchor` stays the PREFERRED plane; both keys are minted.
    # An unwired plane ⇒ null coordinates for it ⇒ the worker just single-homes on
    # the other (single-anchor installs unchanged).
    _home = (getattr(enrollment_token, "home_anchor", "local") or "local").strip().lower()

    def _mint_anchor(anchor: str):
        login = _anchor_login_server_with_db(anchor, db)
        key = _mint_worker_mesh_key(anchor, reusable=False, db=db) if login else None
        return (login, key) if key else (None, None)

    _site_login, _site_key = _mint_anchor("local")
    _cloud_login, _cloud_key = _mint_anchor("remote")

    return WorkerEnrollStartResponse(
        enrollment_id=enrollment.id,
        approval_code=enrollment.approval_code,
        bootstrap_token=bootstrap_token,
        status=enrollment.status.value,
        manager_mesh_name=_manager_mesh_name(),
        home_anchor=_home,
        # mesh_hostname left null ⇒ setup-worker.sh uses the worker's own name as
        # its tailnet hostname (this field is the WORKER's hostname, not the manager's).
        site_login_server=_site_login,
        site_authkey=_site_key,
        cloud_login_server=_cloud_login,
        cloud_authkey=_cloud_key,
    )


@router.post("/workers/enroll/confirm", response_model=WorkerEnrollStatusResponse)
async def worker_enroll_confirm(
    request: WorkerEnrollConfirmRequest,
    db: Session = Depends(get_db),
):
    """
    Worker confirms enrollment by presenting approval_code + bootstrap_token.

    This does NOT approve the worker; it only proves the worker has the bootstrap secret
    and the operator has the approval_code to share out-of-band.
    """
    worker_name = _normalize_worker_name(request.worker_name)
    enrollment = worker_management_service.confirm_enrollment(
        db, worker_name, request.approval_code, request.bootstrap_token
    )

    return WorkerEnrollStatusResponse(
        worker_name=enrollment.worker_name,
        status=enrollment.status.value,
        approved=enrollment.status == WorkerEnrollmentStatus.APPROVED,
        last_seen_at=enrollment.last_seen_at,
    )


@router.get("/workers/enroll/status/{worker_name}", response_model=WorkerEnrollStatusResponse)
async def worker_enroll_status(
    worker_name: str,
    http_request: Request,
    enrollment_token: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Poll a worker's enrollment status (status + approval state).

    GSEC R-143 (SEC-2d-005) — this was previously unauthenticated and
    unthrottled, so anyone who knew a worker name could enumerate the whole
    fleet and observe its approval state. It now FAILS CLOSED: a caller must
    prove possession of one of the enrollment secrets,

      * the per-enrollment **bootstrap token** the worker already holds (as the
        `X-Bootstrap-Token` header, or the `?bootstrap_token=` query — mirrors
        the heartbeat's bootstrap-token gate), OR
      * the **enrollment token** that started this worker's enrollment
        (`?enrollment_token=` — the same credential the enroll call uses; this
        covers an operator's terminal, which only has the enrollment token).

    ...and is rate-limited per IP with the existing limiter. A request that
    presents neither/ a wrong credential is refused (401) — it no longer leaks
    the worker name, status, or approval state. The token is checked
    in-handler (like the mesh/ingress seams), so the static Depends scanner
    still sees it as a pre-auth route (see test_authz_endpoint_coverage).
    """
    from utils.rate_limiter import check_rate_limit
    # 1788d CE carve: services/client_ip.py is paid (manifest deny 979) while
    # utils/client_ip.py is api_allow. The IP is only a rate-limit KEY here
    # (auth is the enrollment/bootstrap token), so CE falls back to the CE-shipped
    # resolver and, if that is unusable, to "unknown" - never a 500, and an
    # unresolvable IP never refuses the request.
    from utils.paid_import import optional_paid
    _ip_mod = optional_paid("services.client_ip")
    if _ip_mod is None:
        try:
            from utils.client_ip import trusted_client_ip as _trusted_client_ip
            client_ip = _trusted_client_ip(http_request) or "unknown"
        except Exception:  # noqa: BLE001 - an unresolvable IP must not refuse
            client_ip = "unknown"
    else:
        client_ip = _ip_mod.resolve_client_ip(http_request) or "unknown"
    check_rate_limit(f"enroll:{client_ip}", max_requests=5, window_seconds=60)

    norm_name = _normalize_worker_name(worker_name)

    # Bootstrap-token gate (primary): the worker's own per-enrollment secret.
    bootstrap = http_request.headers.get("X-Bootstrap-Token") or None
    if not bootstrap:
        # query fallback so a shell / curl poll can pass it without a header
        bootstrap = http_request.query_params.get("bootstrap_token") or None
    if bootstrap:
        rec = worker_management_crud.get_enrollment_by_name(db, norm_name)
        if rec is not None and rec.bootstrap_token_hash \
                and verify_api_key(bootstrap, rec.bootstrap_token_hash):
            enrollment = rec
            return WorkerEnrollStatusResponse(
                worker_name=enrollment.worker_name,
                status=enrollment.status.value,
                approved=enrollment.status == WorkerEnrollmentStatus.APPROVED,
                last_seen_at=enrollment.last_seen_at,
            )
        # Wrong/unknown bootstrap token ⇒ refuse (do not fall through to the
        # enrollment-token path, which would let a leaked bootstrap token be
        # probed as an oracle).
        logger.warning(
            "enroll/status: rejected for '%s' from %s (invalid bootstrap token)",
            norm_name, client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid bootstrap token")

    # Enrollment-token gate (operator flow): the same token the enroll call
    # uses. validate_enrollment_token is side-effect-free (it does NOT consume
    # a use), so polling cannot burn a single-use token.
    if enrollment_token:
        try:
            tok = worker_management_service.validate_enrollment_token(
                db, enrollment_token, norm_name)
        except HTTPException:
            logger.warning(
                "enroll/status: rejected for '%s' from %s (invalid enrollment token)",
                norm_name, client_ip,
            )
            raise HTTPException(status_code=401, detail="Invalid enrollment token")
        # Scope to THIS worker's enrollment: the token must be the one the
        # worker enrolled with (or a NULL-legacy token, pre-token-system
        # rows, which any valid token may query).
        rec = worker_management_crud.get_enrollment_by_name(db, norm_name)
        if rec is None:
            raise HTTPException(status_code=404, detail="Enrollment not found")
        if rec.enrollment_token_id is not None \
                and rec.enrollment_token_id != tok.id:
            logger.warning(
                "enroll/status: rejected for '%s' from %s (token not used by this enrollment)",
                norm_name, client_ip,
            )
            raise HTTPException(status_code=403, detail="Token does not match this enrollment")
        enrollment = rec
    else:
        raise HTTPException(
            status_code=401,
            detail="An enrollment credential (bootstrap token or enrollment "
                   "token) is required",
        )

    return WorkerEnrollStatusResponse(
        worker_name=enrollment.worker_name,
        status=enrollment.status.value,
        approved=enrollment.status == WorkerEnrollmentStatus.APPROVED,
        last_seen_at=enrollment.last_seen_at,
    )


@router.post("/workers/enroll/approve/{worker_name}", response_model=WorkerEnrollStatusResponse)
async def approve_worker_enrollment(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Platform admin approves a worker enrollment.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can approve workers")

    enrollment = worker_management_service.approve_enrollment(
        db, _normalize_worker_name(worker_name), current_user.id
    )

    return WorkerEnrollStatusResponse(
        worker_name=enrollment.worker_name,
        status=enrollment.status.value,
        approved=True,
        last_seen_at=enrollment.last_seen_at,
    )


def discover_workers(db: Session) -> List[str]:
    """
    Discover workers from the database (registered via heartbeat API).
    Go workers self-register on first heartbeat.

    Returns list of discovered worker names.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)

    # Workers with a recent heartbeat are considered alive
    active = worker_management_crud.list_active_workers_since(db, cutoff)

    return [w.worker_name for w in active]


def mark_offline_workers(db: Session, discovered_workers: List[str]):
    """Mark workers as offline if they haven't been seen recently"""

    # Get all licensed workers
    licensed_workers = worker_management_crud.list_licensed_workers(db)

    for worker in licensed_workers:
        # If worker wasn't in discovery list (or heartbeat is stale) and hasn't been seen in 5 minutes
        if worker.last_heartbeat:
            # Ensure both datetimes are timezone-aware (UTC)
            now = datetime.now(timezone.utc)
            # Silence is measured only across the window in which THIS manager
            # was listening. After a manager outage every last_heartbeat looks
            # ancient, and a plain subtraction would mark the entire fleet
            # OFFLINE on the first tick after recovery — which then requeues
            # every in-flight task out from under workers that never stopped
            # working. See services/manager_lifecycle.
            _ml = optional_paid("services.manager_lifecycle")
            if _ml is None:
                time_since_heartbeat = now - (worker.last_heartbeat or now)
            else:
                time_since_heartbeat = _ml.silent_for(worker.last_heartbeat, now)

            # NOAH short fallback: an opportunistic worker that vanishes (lid
            # closed, power pulled) may hold never-started pre-placed tasks that
            # the standard reaper does not rescue. Release them well before the
            # 5-minute offline threshold so they are merely delayed. Long enough
            # to ride out a transient blip; keyed off the platform default.
            # 1665r2 — CE carve: services/noah.py is denied, so the old
            # `from services import noah` raised ImportError for EVERY
            # worker on EVERY reaper pass (~222 tracebacks per boot) and
            # its `db.rollback()` could discard changes made earlier in
            # the same pass. No module = no NOAH branch at all: no import
            # attempt, no rollback, no log.
            _noah = optional_paid("services.noah")
            if _noah is not None:
                try:
                    if _noah.is_noah_worker(db, worker):
                        stale_s = _noah.get_platform_noah(db).stale_release_s or 75
                        if time_since_heartbeat > timedelta(seconds=stale_s):
                            released = _noah.release_never_started_assignments(db, worker.worker_name)
                            if released:
                                worker.noah_harvested_total = (worker.noah_harvested_total or 0) + released
                                db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("NOAH stale-release failed for %s", worker.worker_name)

            if time_since_heartbeat > timedelta(minutes=5):
                if worker.status != WorkerStatus.OFFLINE:
                    worker.status = WorkerStatus.OFFLINE
                    logger.info(f"Marking worker {worker.worker_name} as OFFLINE (last heartbeat: {worker.last_heartbeat})")
                    db.commit()

    db.commit()


def _is_worker_active(worker: WorkerLicense) -> bool:
    """
    Is this worker currently in the task-claiming pool?
    Used by the last-active-worker guard to decide whether pausing or
    updating this worker would actually reduce the pool size.
    """
    return (
        worker.status == WorkerStatus.LICENSED
        and not bool(getattr(worker, "paused", False))
        and not bool(getattr(worker, "update_requested", False))
    )


def _count_other_active_workers(db: Session, worker: WorkerLicense) -> int:
    """Count LICENSED workers other than `worker` that are neither paused nor mid-update."""
    return worker_management_crud.count_other_active_workers(db, worker)


def _embedded_worker_seed_names() -> Tuple[str, ...]:
    """
    The trusted embedded/manager worker names — read from main's seed set,
    never from a request-supplied name. Lazy import because main imports this
    module (same pattern as the heartbeat's `from main import
    _EMBEDDED_WORKER_SEED_NAMES` at ~:3794); the fallback keeps the exemption
    fail-closed (SaaS name only) rather than crashing the request if the
    constant is ever renamed.
    """
    try:
        from main import _EMBEDDED_WORKER_SEED_NAMES as _names
    except Exception:  # noqa: BLE001 — never let an import hiccup 500 a request
        _names = ("mod-worker-embedded",)
    return _names


def _is_embedded_worker(worker_name: str) -> bool:
    """
    Tim B3 item 4 — is this worker the embedded (manager-node) one?

    Embeddedness is a PLATFORM-level identity derived from main's trusted seed-
    name set ONLY (SaaS/dev and CE/on-prem `mod-worker-embedded`; CE installs
    that predate migration 20261109a still carry the legacy `mod-worker-ce`):
    never tenant-derived, never tenant-scoped-widened, never request-supplied.
    Exact match only — NEVER a substring/prefix test, so `mod-worker-ce-2`,
    `mod-worker-ce-x`, `mod-worker-embedded2` and `Mod-Worker-CE` are NOT
    embedded. Fail closed: None/empty/whitespace-only -> False. The only
    normalization is `.strip()` of surrounding whitespace (same name, just
    padded), so `"mod-worker-ce "` -> True.
    """
    return (worker_name or "").strip() in _embedded_worker_seed_names()


def _is_single_node_embedded(db: Session, worker: WorkerLicense) -> bool:
    """
    True when the deployment has exactly one licensed worker AND that worker
    is the embedded/manager one. Embeddedness is keyed to main's seed-name set
    OR the legacy is_remote=False posture: the CE on-manager worker is
    `mod-worker-ce`, and on a WSL/NAT CE install its heartbeat arrives from a
    LAN IP (not 172./10./127.) so the auto-create stamps is_remote=True —
    keyed to is_remote alone the single-node CE operator would hit the 409
    last-active-worker guard on their only worker and could never patch the
    system. A seed name is trusted by NAME (the fixed set main seeds the Vault
    bootstrap token onto), never by a caller-supplied name: any other remote
    worker keeps the old behaviour and gets no exemption. This is the single-node
    exception to the last-active-worker guard — a single-node operator MUST be
    able to update/pause their only worker. Remote-only single-node deployments
    don't get the exemption: they should add a second worker or temporarily
    license the embedded worker before maintenance.
    """
    if worker.worker_name not in _embedded_worker_seed_names() and worker.is_remote:
        return False
    total_licensed = worker_management_crud.count_workers_by_status(db, WorkerStatus.LICENSED)
    return total_licensed == 1


def _guard_not_last_active_worker(db: Session, worker: WorkerLicense, action: str) -> None:
    """
    Raise HTTP 409 if pausing or updating this worker would leave the system
    with zero workers able to claim tasks, unless this is a single-node
    embedded deployment (see _is_single_node_embedded).
    """
    if not _is_worker_active(worker):
        # Already paused / already mid-update / not licensed — the action
        # doesn't change the active-pool size, so nothing to guard against.
        return
    if _count_other_active_workers(db, worker) > 0:
        return  # safe — at least one other worker can pick up the slack
    if _is_single_node_embedded(db, worker):
        return  # single-node embedded deploy — explicit exception per spec
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot {action} {worker.worker_name}: it is the only worker "
            "actively able to claim tasks. Add or license another worker, "
            "or resume a paused one, first. (Single-node embedded deployments "
            "are exempt from this check.)"
        ),
    )


def pause_worker(worker_name: str, db: Optional[Session] = None) -> bool:
    """
    Pause a worker (stop receiving new tasks).
    Persists `paused=true` on worker_licenses; the next heartbeat response
    carries drain=true and the Go worker stops polling. Safe to call from
    request handlers (pass the request's db session) or background code
    (pass None; a fresh session is opened and closed).
    """
    import traceback
    caller_stack = "".join(traceback.format_stack()[-5:-1])
    logger.warning(
        "pause_worker(%s) CALLED — stack trace:\n%s",
        worker_name, caller_stack,
    )
    if db is None:
        db = SessionLocal()
        own_session = True
    else:
        own_session = False
    try:
        updated = worker_management_crud.bulk_set_worker_paused(db, worker_name, True)
        db.commit()
        if updated:
            logger.warning("pause_worker: paused=%s set on %s", updated, worker_name)
        else:
            logger.warning("Pause requested for unknown worker %s", worker_name)
        return bool(updated)
    finally:
        if own_session:
            db.close()


def resume_worker(worker_name: str, queues: Optional[List[str]] = None, db: Optional[Session] = None) -> bool:
    """
    Resume a worker (start receiving tasks again).
    Clears `paused` on worker_licenses; queue argument is accepted for
    backward-compat with older call sites but no longer rewrites the
    worker's queue list (workers manage their own queues via heartbeat).
    """
    if db is None:
        db = SessionLocal()
        own_session = True
    else:
        own_session = False
    try:
        updated = worker_management_crud.bulk_set_worker_paused(db, worker_name, False)
        db.commit()
        if updated:
            logger.info("Pause cleared on worker %s", worker_name)
        else:
            logger.warning("Resume requested for unknown worker %s", worker_name)
        return bool(updated)
    finally:
        if own_session:
            db.close()


@router.get("/workers/config", response_model=WorkerConfigResponse, response_model_exclude_unset=True)
async def get_worker_config(
    current_user: User = Depends(get_current_user)
):
    """
    Get service manifest for worker configuration.
    
    Returns FQDNs/URLs for essential services (API, MinIO, Keycloak).
    """
    # Base configuration based on environment. Use the CONFIGURED subdomains —
    # hardcoding "api"/"s3"/"auth" handed every non-default deployment (dev /
    # staging, where API_SUBDOMAIN=dev-api etc.) the WRONG service URLs, so a
    # worker would enroll fine against the correct address, then fetch this
    # manifest, switch to api.<base_domain>, and silently stop heartbeating.
    base_domain = os.getenv("BASE_DOMAIN", "example.com")
    protocol = os.getenv("PROTOCOL", "https")
    api_subdomain = os.getenv("API_SUBDOMAIN", "api")
    s3_subdomain = os.getenv("S3_SUBDOMAIN", "s3")
    keycloak_subdomain = os.getenv("KEYCLOAK_SUBDOMAIN", "auth")

    def _fqdn(sub: str) -> str:
        sub = (sub or "").strip()
        # An empty subdomain (e.g. Keycloak on the bare domain) → base domain.
        return f"{sub}.{base_domain}" if sub else base_domain

    return {
        "api_url": f"{protocol}://{_fqdn(api_subdomain)}",
        "minio_url": f"{protocol}://{_fqdn(s3_subdomain)}",
        "keycloak_url": f"{protocol}://{_fqdn(keycloak_subdomain)}",
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/workers", response_model=List[WorkerInfo])
async def list_workers(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    List all discovered workers with their license status.

    Platform admin only endpoint.
    """

    # Only platform admin can view workers (tenant_id may vary; membership is the operator flag)
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can manage workers"
        )

    # Discover current workers
    discovered = discover_workers(db)
    mark_offline_workers(db, discovered)

    # Bulk-load workers + hardware profiles + enrolment statuses, and compute
    # the last-active-worker guard (can_drain / can_drain_reason) for each
    # worker — same logic the pause/update endpoints enforce, surfaced
    # up-front so the Dashboard can render disabled buttons with a tooltip
    # instead of popping a 409 after the click.
    rows, enrol_status_map, current_server_version, pending_enrolments = worker_management_service.list_workers_view(
        db, discovered, _is_worker_active,
    )

    result = []
    for worker, profile, is_online, can_drain, can_drain_reason in rows:
        # 993c2 — resolve_availability's own platform/tenant settings reads
        # go through platform_settings._lookup_row's 30s TTL cache (services/
        # platform_settings.py:324-343), and the per-worker grant-policy
        # queries (_worker_access_policy/_grant_allows) are skipped entirely
        # here because this endpoint already requires current_user.is_platform
        # _admin above, so resolve_availability's `_is_tenant_admin(user)`
        # short-circuits the gate -- no per-worker DB query multiplication
        # from calling this once per worker in the loop.
        try:
            host_exec_status = worker_host_exec_service.resolve_availability(
                db, worker, current_user,
            )
        except Exception:
            logger.debug(
                "993c2: resolve_availability failed for worker %s",
                getattr(worker, "worker_name", None), exc_info=True,
            )
            host_exec_status = None
        result.append(WorkerInfo(
            id=worker.id,
            worker_name=worker.worker_name,
            hostname=worker.hostname,
            hardware_specs=worker.hardware_specs,
            queues=worker.queues,
            status=worker.status.value,
            owner_type=worker.owner_type.value if worker.owner_type else None,
            owner_tenant_id=worker.owner_tenant_id,
            assigned_tenant_ids=worker.assigned_tenant_ids,
            licensed_at=worker.licensed_at,
            licensed_by_username=worker.licensed_by.username if worker.licensed_by else None,
            first_seen_at=worker.first_seen_at,
            last_heartbeat=worker.last_heartbeat,
            is_online=is_online,
            notes=worker.notes,
            benchmark_status=profile.benchmark_status if profile else None,
            benchmarked_at=profile.benchmarked_at if profile else None,
            paused=bool(getattr(worker, "paused", False)),
            update_requested=bool(getattr(worker, "update_requested", False)),
            agent_version=worker.agent_version,
            latest_agent_version=current_server_version,
            # NEVER signal an update when the latest version is unknown.
            # get_latest_version() returns None on a transient manifest read
            # (MinIO blip / API-startup warmup); without the `current_server_version`
            # guard, "v1.0.18" != None evaluates True and EVERY worker is told to
            # drain + self-update spuriously, dropping the remote fleet on any API
            # restart. (Same guard is_update_available() already enforces.)
            update_available=bool(
                worker.agent_version and current_server_version
                and worker.agent_version != current_server_version
            ),
            can_drain=can_drain,
            residency_zone=_residency.zone_from_specs(
                worker.hardware_specs),
            cost_per_hour_cents=worker.cost_per_hour_cents,
            hardware_cost_cents=worker.hardware_cost_cents,
            amortisation_months=worker.amortisation_months,
            power_watts=worker.power_watts,
            electricity_cents_per_kwh=worker.electricity_cents_per_kwh,
            margin_percent=worker.margin_percent,
            billed_per_hour_cents=billed_per_hour_cents(
                worker.cost_per_hour_cents, worker.margin_percent),
            can_drain_reason=can_drain_reason,
            enrollment_status=enrol_status_map.get(worker.worker_name),
            host_exec_status=host_exec_status,
            is_embedded=_is_embedded_worker(worker.worker_name),
        ))

    # Manual enrolment (auto_approve=false / auto_license unset) creates a PENDING
    # WorkerEnrollment with NO WorkerLicense row, so the loop above never selects
    # it and the Dashboard's Approve branch (workers.js:2110 reads
    # `worker.enrollment_status === 'pending'`) has nothing to render. Surface
    # those enrolments as synthetic items: id is NEGATIVE (-enrolment id) because
    # data-worker-id feeds the id-keyed routes (/workers/{id}/benchmark,
    # /hardware-profile, /approle/rotate) and a bare enrolment id would collide
    # with a WorkerLicense id. Tenant-scoped routes (/workers/available-for-tenant)
    # read license rows only and stay unchanged — an enrolment row has no tenant
    # column, so pending extras are platform-pool-scoped (this endpoint is
    # platform-admin-only anyway).
    for enrollment in pending_enrolments:
        result.append(WorkerInfo(
            id=-enrollment.id,
            worker_name=enrollment.worker_name,
            hostname=enrollment.hostname or enrollment.worker_name,
            hardware_specs=None,
            queues=None,
            status=WorkerStatus.UNLICENSED.value,
            licensed_at=None,
            licensed_by_username=None,
            first_seen_at=enrollment.created_at,
            last_heartbeat=enrollment.last_seen_at,
            is_online=False,
            notes=None,
            enrollment_status=enrollment.status.value,
        ))

    return result


def _native_lane_worker(worker) -> bool:
    """MAC.1.s11 — true for a worker with NO container runtime.

    A native lane runs the Go binary directly on a desktop OS: darwin (a Mac,
    MAC.1.s7/s8b), a `metal`-tagged box (the tag the darwin probe emits — the
    tag, not the OS, is what says "no docker"), or windows (GP.3's native
    lane). Detection matches routers/worker_tasks.py's `:worker_metal_lane`
    plus the windows half of `_is_native_worker`, so the claim fence and the
    profiling decision can never drift apart.
    """
    hw = worker.hardware_specs or {}
    os_name = str(hw.get("os") or "linux").strip().lower()
    if os_name in ("darwin", "windows"):
        return True
    tags = hw.get("hardware_tags") or []
    return any(str(t).strip().lower() == "metal" for t in tags if isinstance(t, str))


def _compute_hardware_fingerprint(specs: dict) -> str:
    """Compute a stable fingerprint from hardware specs for deduplication."""
    import hashlib
    parts = [
        str(specs.get("gpu_name", "") or specs.get("gpu_type", "none")),
        str(specs.get("cpu_cores", 0)),
        str(specs.get("ram_gb", 0)),
    ]
    raw = "|".join(parts).lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _generate_benchmark_hmac_key(hardware_profile_id: int, db=None, profile=None) -> str:
    """Generate a one-time HMAC key, store in Vault (primary) or the
    hardware_profiles row (fallback), return it for the container.

    Vault is preferred — the api-benchmark-hmac policy (added in
    vault-init.sh) grants the API CRUD on `secret/data/benchmark-hmac/*`.
    On older deployments where that policy isn't installed yet, Vault
    write fails with 403 and we fall back to storing the key on the
    HardwareProfile row (cleared on verification).

    When `db` and `profile` are passed (from _auto_dispatch_profiling),
    the DB fallback writes directly on the caller's session and ORM
    object — no separate SessionLocal. This avoids the bug where a
    separate session's commit was overwritten by the caller's later
    commit that had a stale cached version of the profile row.
    """
    import secrets
    hmac_key = secrets.token_hex(32)

    stored_in_vault = False
    try:
        from utils.vault_client import write_kv_v2
        vault_token_path = os.getenv("VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
        with open(vault_token_path) as f:
            vault_token = f.read().strip()
        write_kv_v2(f"benchmark-hmac/{hardware_profile_id}", vault_token, {"key": hmac_key})
        stored_in_vault = True
    except Exception as e:
        logger.warning(
            "Could not store HMAC key in Vault for profile %d (will use DB fallback): %s",
            hardware_profile_id, e,
        )

    if not stored_in_vault:
        # Prefer the caller's session + ORM object to avoid cross-session
        # overwrites. Fall back to a new session only when called from
        # contexts that don't pass one (e.g. trigger_benchmark endpoint).
        if profile is not None:
            profile.benchmark_hmac_key = hmac_key
            # Don't commit here — caller commits both key + status together.
        elif db is not None:
            prof = worker_management_crud.get_hardware_profile_by_id(db, hardware_profile_id)
            if prof:
                prof.benchmark_hmac_key = hmac_key
        else:
            try:
                from database import SessionLocal
                _db = SessionLocal()
                try:
                    prof = worker_management_crud.get_hardware_profile_by_id(_db, hardware_profile_id)
                    if prof:
                        prof.benchmark_hmac_key = hmac_key
                        _db.commit()
                finally:
                    _db.close()
            except Exception as e:
                logger.error(
                    "Both Vault AND DB HMAC key storage failed for profile %d: %s",
                    hardware_profile_id, e,
                )

    return hmac_key


def _verify_benchmark_signature(hardware_profile_id: int, scores_json: str, signature: str) -> bool:
    """Verify benchmark results were signed by the container using the
    one-time HMAC key. Tries Vault first, falls back to the row's
    benchmark_hmac_key column. Key is destroyed on success regardless of
    which store held it.
    """
    import hmac as hmac_mod, hashlib

    hmac_key = ""
    vault_held = False
    vault_token = None

    # Try Vault first.
    try:
        from utils.vault_client import read_kv_v2
        vault_token_path = os.getenv("VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
        with open(vault_token_path) as f:
            vault_token = f.read().strip()
        secret = read_kv_v2(f"benchmark-hmac/{hardware_profile_id}", vault_token)
        hmac_key = secret.get("key", "") if secret else ""
        vault_held = bool(hmac_key)
    except Exception as e:
        logger.debug(
            "Vault HMAC read failed for profile %d (trying DB fallback): %s",
            hardware_profile_id, e,
        )

    # Fall back to DB column.
    if not hmac_key:
        try:
            from database import SessionLocal
            _db = SessionLocal()
            try:
                prof = worker_management_crud.get_hardware_profile_by_id(_db, hardware_profile_id)
                hmac_key = (prof.benchmark_hmac_key if prof else "") or ""
            finally:
                _db.close()
        except Exception as e:
            logger.warning("DB HMAC read failed for profile %d: %s", hardware_profile_id, e)

    if not hmac_key:
        logger.warning("No HMAC key found anywhere for profile %d", hardware_profile_id)
        return False

    expected = hmac_mod.new(hmac_key.encode(), scores_json.encode(), hashlib.sha256).hexdigest()
    valid = hmac_mod.compare_digest(expected, signature)

    # One-shot: destroy the key regardless of where it lived, so a replay
    # of the benchmark task with the same key can't succeed.
    if vault_held and vault_token:
        try:
            from utils.vault_client import delete_kv_v2
            delete_kv_v2(f"benchmark-hmac/{hardware_profile_id}", vault_token)
        except Exception as e:
            logger.debug("Vault HMAC delete failed for profile %d: %s", hardware_profile_id, e)
    try:
        from database import SessionLocal
        _db = SessionLocal()
        try:
            prof = worker_management_crud.get_hardware_profile_by_id(_db, hardware_profile_id)
            if prof and prof.benchmark_hmac_key:
                prof.benchmark_hmac_key = None
                _db.commit()
        finally:
            _db.close()
    except Exception:
        pass

    return valid


def _auto_calibrate_for_profile_hook(db, worker, profile, *, trigger_source: str) -> None:
    """Shared gate for the worker-side calibration auto-queue.

    Called from two paths today:
      - heartbeat → `_match_hardware_profile` when `is_new_profile=True`
        (`trigger_source='worker_heartbeat'`)
      - admin re-benchmark → `trigger_benchmark` after the profiling
        workflow is dispatched (`trigger_source='manual_benchmark'`)

    Gates:
      - worker.status == LICENSED
      - worker is NOT the embedded worker (its image catalog is too thin
        to run the calibration matrix)
      - `AUTO_CALIBRATE_NEW_PROFILES` env var is not `"false"`

    Capacity-floor deferral and the actual matrix dispatch live in
    `services.calibration_dispatcher.auto_queue_for_profile`. Either
    way an `AuditEvent` records the attempt with the requested
    `trigger_source` and `outcome` (`queued` / `deferred`).

    Best-effort: any exception is logged at DEBUG and swallowed so the
    caller's main flow (heartbeat response, benchmark dispatch) is
    never blocked.
    """
    is_embedded = worker.worker_name in _embedded_worker_seed_names()
    if not (worker.status == WorkerStatus.LICENSED and not is_embedded):
        return
    if os.getenv("AUTO_CALIBRATE_NEW_PROFILES", "true").lower() == "false":
        return
    try:
        from services.calibration_dispatcher import auto_queue_for_profile
        tenant_id = worker.owner_tenant_id
        if tenant_id is None and worker.assigned_tenant_ids:
            try:
                tenant_id = int(worker.assigned_tenant_ids[0])
            except (TypeError, ValueError, IndexError):
                tenant_id = None
        if tenant_id is None:
            fallback = worker_management_crud.get_first_tenant_id(db)
            tenant_id = fallback[0] if fallback else None
        if tenant_id is None:
            logger.info(
                "auto_queue_for_profile=%s skipped (no tenant resolvable)",
                profile.id,
            )
            return
        dispatch_result = auto_queue_for_profile(
            db, profile_id=profile.id, tenant_id=tenant_id,
        )
        try:
            from models import AuditEvent
            db.add(AuditEvent(
                tenant_id=tenant_id,
                user_id=None,
                event_name="CALIBRATION_AUTO_QUEUED",
                details={
                    "trigger_source": trigger_source,
                    "worker_name": worker.worker_name,
                    "hardware_profile_id": profile.id,
                    "fingerprint": profile.fingerprint,
                    "outcome": "queued" if dispatch_result else "deferred",
                },
            ))
            db.commit()
        except Exception as audit_err:
            logger.debug(
                "AuditEvent emit failed for auto-queue %d: %s",
                profile.id, audit_err,
            )
            db.rollback()
    except Exception as e:
        logger.debug("auto_queue_for_profile failed (non-critical): %s", e)


def _match_hardware_profile(db, worker, specs: dict, accelerators: list = None):
    """Match or create a HardwareProfile for this worker's hardware.

    When a new profile is created (first heartbeat from a new hardware
    fingerprint), auto-dispatch the general system benchmark + one
    per-device accelerator benchmark for every detected device. The worker
    enters a 'profiling' state and cannot claim user tasks until all
    benchmarks complete.

    Uses enqueue_task_internal() for all dispatch — no hand-built
    ApiTaskQueue rows (the old _dispatch_benchmark pattern is deleted).
    """
    try:
        from models import HardwareProfile
        fp = _compute_hardware_fingerprint(specs)
        profile = worker_management_crud.get_hardware_profile_by_fingerprint(db, fp)
        if not profile:
            profile = HardwareProfile(
                fingerprint=fp,
                cpu_model=None,
                cpu_cores=specs.get("cpu_cores"),
                gpu_model=specs.get("gpu_name"),
                gpu_vram_gb=specs.get("vram_gb"),
                ram_gb=specs.get("ram_gb"),
                cloud_provider=specs.get("cloud_provider"),
                instance_type=specs.get("instance_type"),
                benchmark_status="pending",
            )
            db.add(profile)
            db.flush()
            is_new_profile = True
        else:
            is_new_profile = False
        if worker.hardware_profile_id != profile.id:
            worker.hardware_profile_id = profile.id
        db.commit()

        # Upsert per-device accelerator rows from the heartbeat payload.
        if accelerators:
            try:
                from crud.accelerator_cud import upsert_accelerators
                upsert_accelerators(db, profile.id, accelerators)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning("Accelerator upsert failed for profile %d: %s", profile.id, e)

        # Auto-dispatch benchmarks for new profiles on licensed workers.
        # Uses enqueue_task_internal() so the full DAG pipeline handles
        # queue routing, GPU variant resolution, phase planning, etc.
        is_embedded = worker.worker_name in _embedded_worker_seed_names()
        if is_embedded and profile.benchmark_status == "pending":
            profile.benchmark_status = "skipped"
            db.commit()
        # MAC.1.s11 — the native lanes (darwin/Metal/windows) have no container
        # runtime, so the benchmark image can never pull and the container
        # profiling/calibration path can never finish. Park them on a distinct
        # status instead: both profiling gates key on pending/running (the
        # claim-path `profiling_guard` SQL and the scheduler's eligibility
        # filter), so a native profile that never runs a Docker benchmark still
        # reads as calibrated rather than stalling the worker at pending.
        if is_new_profile and _native_lane_worker(worker) and profile.benchmark_status == "pending":
            profile.benchmark_status = "native"
            db.commit()
        should_profile = (
            profile.benchmark_status == "pending"
            and worker.status == WorkerStatus.LICENSED
            and not is_embedded
            and not _native_lane_worker(worker)
        )
        if should_profile:
            active_run = worker_management_crud.get_active_profiling_run(db, profile.id)
            should_profile = active_run is None
        if should_profile:
            try:
                _auto_dispatch_profiling(db, worker, profile, accelerators or [])
                logger.info(
                    "Auto-dispatched profiling for new profile %d on worker %s",
                    profile.id, worker.worker_name,
                )
            except HTTPException as e:
                # Most common case: Harbor creds missing. Log at WARNING so
                # operators see exactly why profiling won't start.
                logger.warning(
                    "Auto-profiling skipped for profile %d on worker %s: %s",
                    profile.id, worker.worker_name, e.detail,
                )
            except Exception as e:
                logger.debug("Auto-profiling dispatch failed (non-critical): %s", e)

        # AI scheduler — auto-queue calibration on a NEW HardwareProfile.
        # Only fires for newly-created profiles on licensed non-embedded
        # workers. The shared helper also runs from trigger_benchmark()
        # with trigger_source='manual_benchmark' so admins re-running the
        # profile get calibration too.
        if is_new_profile:
            _auto_calibrate_for_profile_hook(
                db, worker, profile, trigger_source="worker_heartbeat",
            )

    except Exception as e:
        logger.debug("Hardware profile match failed: %s", e)


def _harbor_creds_missing(db, tenant_id: int) -> list[str]:
    """Return the list of Harbor credential names that are unset or still at
    the 'PLEASE_CONFIGURE' placeholder for *tenant_id*.

    Profiling pulls `hardwarebenchmark:latest` from the platform Harbor
    registry, so this has to be configured before any benchmark can run —
    otherwise the worker cycles forever on a 401 image-pull error.
    """
    import crud
    required = ["harbor_registry", "harbor_username", "harbor_password"]
    missing = []
    for name in required:
        val = crud.get_decrypted_secret_by_name(db, name, tenant_id)
        if not val or val.strip() == "" or val == "PLEASE_CONFIGURE":
            missing.append(name)
    return missing


def _auto_dispatch_profiling(db, worker, profile, accelerators: list, only: Optional[str] = None):
    """Build a profiling WorkflowRun with sequential benchmark nodes.

    Creates a synthetic WorkflowRun (no stored template) with one node per
    benchmark, wired in a sequential chain. The existing DAG engine
    (_check_and_dispatch_downstream) handles ordering, retries, and
    completion detection.

    `only` (optional) narrows the run to a single node:
    - `"system"` → just the CPU/RAM/I/O/MinIO/Docker benchmark.
    - `"<kind>:<index>"` (e.g. `"cuda:0"`) → just that accelerator.
    - `None` → full chain (system → accel 0 → accel 1 → …).
    """
    import uuid
    from models import WorkflowRun, TaskStatus, ApiTaskQueue, ApiTaskStatus
    from services.workflow_orchestrator import build_execution_plan
    from routers.worker_tasks import enqueue_task_internal, TaskEnqueueRequest

    registry = platform_registry()
    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    tenant_id = worker.owner_tenant_id or 1

    # Refuse to dispatch if the benchmark image can't be pulled. Profiling
    # needs ghcr.io/modtim/hardwarebenchmark:latest; without
    # real Harbor credentials the worker will loop on 401 Unauthorized.
    # Platform creds live under tenant_id=1; that's also what worker_tasks.py
    # hands to the worker at claim time.
    missing = _harbor_creds_missing(db, 1)
    # 1665m Gap V — CE ships no Harbor registry at all, so the benchmark image
    # can never pull and the profiling DAG can never finish. The refusal below
    # left the fresh profile at `pending` forever, and `profiling_guard`
    # (routers/worker_tasks.py:2258) withholds EVERY non-system task from that
    # worker's claim SQL while the profile is pending/running — so a CE boot
    # never claimed a single node task (task 69 sat PENDING ~20 min; calgen
    # 70/71/72 FAILED on the Harbor 503). Park it on the same `'skipped'` value
    # the embedded worker already uses: both profiling gates key only on
    # pending/running, so nothing else has to learn about editions. "Cannot run"
    # is exactly 1665l's no-registry condition (routers/worker_tasks.py:4981) —
    # a deployment that configures a registry (paid/SaaS) keeps the profile
    # `pending` and the guard byte-for-byte unchanged.
    from utils.registries import platform_registry_configured
    if missing and not platform_registry_configured():
        profile.benchmark_status = "skipped"
        db.commit()
        logger.warning(
            "Benchmark/calibration SKIPPED for profile %d on worker %s: "
            "DOCKER_REGISTRY is unset or a placeholder (CE ships no Harbor registry), so the "
            "benchmark image cannot pull and the profiling DAG can never finish. "
            "Profile parked at 'skipped' so the profiling claim guard does not "
            "withhold every task; set DOCKER_REGISTRY to enable profiling.",
            profile.id, worker.worker_name,
        )
        return
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Harbor credentials are not configured. Profiling cannot "
                "run until the platform Harbor credentials are set. "
                f"Missing or placeholder: {', '.join(missing)}. "
                "Configure them in Settings → Secrets (harbor_registry, "
                "harbor_username, harbor_password) and try again."
            ),
        )

    # Parse `only` into a simple flag + optional (kind, index) selector.
    only_system = only == "system"
    only_accel: Optional[tuple[str, int]] = None
    if only and ":" in only:
        k, _, i = only.partition(":")
        only_accel = (k, int(i))

    nodes = []
    edges = []

    # Durable pin (utils.task_pinning.benchmark_target_worker): every benchmark
    # node in this run is stamped with the worker the profile belongs to. The
    # requeue/unpin/reassign sweeps read this flag and refuse to move the task
    # to another machine — a score measured on a different box would corrupt
    # this profile. If the pinned worker can't run it, it fails, it does not
    # migrate.
    _pin_worker = worker.worker_name

    system_node_id = f"benchmark-system-{profile.id}"
    # Include the system node unless the caller asked for a specific accelerator.
    include_system = only_accel is None
    if include_system:
        nodes.append({
            "id": system_node_id,
            "data": {
                "node_kind": "hardware_benchmark",
                "processing_image": f"{registry}/modtim/hardwarebenchmark:latest",
                "process_type": "CPU",
                "container_env": {
                    "API_KEY": service_api_key,
                    "HARDWARE_PROFILE_ID": str(profile.id),
                },
                "gpu_required": False,
                "container_timeout": 300,
                "skip_download": True,
                "skip_upload": True,
                # The System benchmark needs MinIO creds for its own
                # upload/download throughput measurements even though
                # the download/upload sidecars are skipped. Without
                # this flag, get_task_credentials() returned creds.minio
                # = None (based on skip_download AND skip_upload), the
                # worker never injected MINIO_ENDPOINT, and the test
                # reported minio_*_mbps=0 with "skipped (no creds)".
                "needs_minio_creds": True,
                # System benchmark measures `docker pull alpine:3.20`
                # time + `docker run alpine:3.20 true` startup. Both
                # need the container to reach a Docker daemon, which
                # means bind-mounting /var/run/docker.sock in. The
                # worker only honours this for explicitly-flagged
                # tasks (scoped to the hardwarebenchmark image); not
                # a general docker-in-docker enablement. Absent this
                # flag, both tests emit null.
                "needs_docker_socket": True,
            },
        })
        nodes[-1]["data"]["benchmark_target_worker"] = _pin_worker

    prev_node_id = system_node_id if include_system else None
    from crud.accelerator_cud import resolve_strategy
    # Skip accelerator nodes entirely when only=system was requested.
    for dev in (accelerators if not only_system else []):
        kind = (dev.get("kind") or "").strip().lower()
        index = int(dev.get("index", 0))
        if not kind:
            continue
        # When the caller wants a specific accelerator, skip the others.
        if only_accel is not None and (kind, index) != only_accel:
            continue

        # Terminal quarantine — don't auto-re-benchmark a device already marked
        # 'unsupported' (the runtime can't see it in this environment) or one an
        # operator disabled. Re-running just re-learns the same fact and burns a
        # profiling slot. An explicit only=<kind>:<index> is an operator override
        # and DOES re-run — that's how a quarantine gets cleared after a fix.
        if only_accel is None:
            _existing = worker_management_crud.get_accelerator_profile_by_kind_index(
                db, profile.id, kind, index,
            )
            if _existing and (
                _existing.benchmark_status == "unsupported"
                or getattr(_existing, "admin_disabled", False)
            ):
                logger.info(
                    "Skipping benchmark for quarantined device %s[%s] on profile %d "
                    "(status=%s admin_disabled=%s)",
                    kind, index, profile.id, _existing.benchmark_status,
                    getattr(_existing, "admin_disabled", False),
                )
                continue

        strategy = resolve_strategy(db, kind, dev.get("vendor_id"), dev.get("device_id"))
        # Fallback must be per-kind, not generic: database.py seeds exactly
        # platform_image(f"accelerator-benchmark-{kind}:latest"), so a missing/stale
        # strategy row still dispatches an image that exists and matches TARGET_KIND.
        # A generic ref can never pull — no accelerator-benchmark-generic image exists.
        image = strategy.image_ref if strategy else platform_image(f"accelerator-benchmark-{kind}:latest")
        strategy_id = strategy.id if strategy else None
        # Any non-CPU kind a worker advertises gets its own queue name
        # (uppercase, matches the heartbeat convention). New accelerator
        # tags (habana, tpu, inferentia, etc.) self-route this way
        # without needing the closed list this branch used to enforce.
        queue = kind.upper() if kind and kind.lower() != "cpu" else "CPU"
        node_id = f"benchmark-{profile.id}-{kind}-{index}"

        nodes.append({
            "id": node_id,
            "data": {
                "node_kind": "accelerator_benchmark",
                "processing_image": image,
                "process_type": queue,
                "container_env": {
                    "API_KEY": service_api_key,
                    "HARDWARE_PROFILE_ID": str(profile.id),
                    "TARGET_KIND": kind,
                    "TARGET_INDEX": str(index),
                    "STRATEGY_ID": str(strategy_id or ""),
                },
                # Accelerator benchmarks HAVE to touch the device — without
                # gpu_required=True the worker's executor skips NVIDIA
                # DeviceRequests (executor.go:789) and cuBLAS/ROCm/etc calls
                # fail silently inside the container, returning zero scores.
                "gpu_required": True,
                "container_timeout": 300,
                "skip_download": True,
                "skip_upload": True,
            },
        })
        nodes[-1]["data"]["benchmark_target_worker"] = _pin_worker
        if prev_node_id is not None:
            edges.append({"source": prev_node_id, "target": node_id})
        prev_node_id = node_id

    if not nodes:
        raise HTTPException(
            status_code=400,
            detail=f"No benchmark node matched only={only!r} (device may not be enumerated yet)",
        )

    # Fail fast on an architecture mismatch instead of burning the profiling
    # deadline. Each benchmark node's image is pinned to THIS worker
    # (assigned_to_worker below), and the claim guard's arch rule treats an
    # image with no confirmed arm64 manifest as amd64-only — so an arm64 worker
    # can never claim an amd64-only image and the run sits RUNNING until the
    # 10-minute expirer force-fails it with a Harbor hint that misleads. That
    # was the exact symptom on a GX10 (arm64) box: the CUDA benchmark image is
    # amd64-only, the pinned task was never claimed, and the dashboard showed
    # "Profiling deadline exceeded ... Check Harbor credentials". Refusing here
    # names the real reason up front.
    try:
        from utils.image_arch import image_supports_arch
        from utils.worker_arch import normalize_arch as _norm_arch
        _worker_arch = _norm_arch((worker.hardware_specs or {}).get("arch")) or "amd64"
        _incompatible = []
        for node in nodes:
            _img = node["data"].get("processing_image")
            if _img and not image_supports_arch(_img, _worker_arch):
                _incompatible.append(node["data"].get("node_kind") or node["id"])
        if _incompatible:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Profiling cannot run on worker {worker.worker_name} "
                    f"({_worker_arch}): the benchmark image(s) for "
                    f"{', '.join(sorted(set(_incompatible)))} have no confirmed "
                    f"{_worker_arch} manifest (they are amd64-only until built "
                    f"multi-arch), so the worker's claim guard will never pick "
                    f"the task up and the run would time out. Build/push the "
                    f"benchmark image(s) for {_worker_arch} and re-trigger."
                ),
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — arch check must not block profiling
        logger.warning("Profiling arch pre-check failed; proceeding", exc_info=True)

    plan = build_execution_plan(nodes, edges)

    run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=None,
        workflow_version_id=None,
        status=TaskStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
        orchestrator_run_id=f"profiling_{profile.id}_{uuid.uuid4().hex[:8]}",
        parameters={
            "execution_plan": plan,
            "profiling_worker": worker.worker_name,
            # Durable per-task pin (see utils.task_pinning). Every benchmark
            # task of this run stays bound to this worker; the sweeps must
            # never reassign it elsewhere.
            "benchmark_target_worker": worker.worker_name,
            "is_profiling": True,
            "hardware_profile_id": profile.id,
        },
    )
    db.add(run)
    db.flush()

    root_node = nodes[0]
    root_data = root_node["data"]
    hmac_key = _generate_benchmark_hmac_key(profile.id, db=db, profile=profile)
    root_env = dict(root_data["container_env"])
    root_env["BENCHMARK_HMAC_KEY"] = hmac_key

    from utils.minio_client import get_tenant_bucket_name
    root_payload = {
        "node_kind": root_data["node_kind"],
        "processing_image": root_data["processing_image"],
        "container_env": root_env,
        "source_file_location": "",
        "minio_bucket": get_tenant_bucket_name(tenant_id, db),
        # Honour the per-node value so accelerator benchmarks get NVIDIA
        # DeviceRequests at container start (see executor.go:789).
        "gpu_required": bool(root_data.get("gpu_required", False)),
        "container_timeout": root_data.get("container_timeout", 300),
        "skip_download": True,
        "skip_upload": True,
        # Promote opt-in flags from node_data → top-level task_payload
        # where the API credentials endpoint and the Go worker both
        # look for them. Nesting them under node_data meant every
        # consumer got False even though the node asked for True.
        "needs_minio_creds": bool(root_data.get("needs_minio_creds", False)),
        "needs_docker_socket": bool(root_data.get("needs_docker_socket", False)),
        "benchmark_target_worker": _pin_worker,
        "node_data": root_data,
    }
    task = ApiTaskQueue(
        workflow_run_id=run.id,
        node_run_id=None,
        node_id=root_node["id"],
        tenant_id=tenant_id,
        queue_name=root_data.get("process_type", "CPU"),
        priority=9,
        task_type="system",
        task_payload=root_payload,
        status=ApiTaskStatus.ASSIGNED,
        assigned_to_worker=worker.worker_name,
        max_attempts=2,
        output_namespace="attempt_0",
    )
    db.add(task)

    profile.benchmark_status = "running"
    db.commit()

    # FX.165 p1: the row is born on the worker's OWN queue (root_data["process_type"]
    # is e.g. "ROCM" for an accelerator-only re-run), so the wake has to name that
    # queue, never a literal. Publish AFTER the commit (never inside the open
    # transaction) via the same lazy-imported helper every other enqueue site uses.
    try:
        from routers.worker_tasks import _publish_event

        _publish_event(f"task:available:{task.queue_name}")
    except Exception:  # noqa: BLE001 — wake-up must never fail the enqueue
        logger.debug("profiling wake publish on %s failed", task.queue_name)

    logger.info(
        "Dispatched profiling workflow (run %d) for profile %d on worker %s (%d nodes)",
        run.id, profile.id, worker.worker_name, len(nodes),
    )


class WorkerHeartbeatRequest(BaseModel):
    """Hardware info reported by workers"""
    worker_name: str
    hostname: str
    ram_gb: int
    cpu_cores: int
    has_gpu: bool = False
    gpu_name: Optional[str] = None
    gpu_vram_mb: Optional[int] = None
    gpu_type: Optional[str] = None  # CUDA, ROCm, Intel, none
    # F14b — canonical vendor of the primary GPU + total accelerator count.
    # Sent in the heartbeat payload so the manager can bucket workers by
    # vendor and report aggregate GPU counts without re-enumerating.
    gpu_vendor: Optional[str] = None  # "NVIDIA", "AMD", "Intel", "Apple"
    gpu_count: Optional[int] = None   # total physical accelerator devices
    hardware_tags: Optional[List[str]] = None  # open-ended: ["cuda","tensorrt"], ["rocm"], etc.
    cloud_provider: Optional[str] = None  # aws, gcp, azure
    instance_type: Optional[str] = None  # e.g. "g5.xlarge"
    queues: Optional[str] = None
    timestamp: Optional[str] = None
    # Health monitoring fields (remote workers)
    disk_free_gb: Optional[float] = None
    # megaplan Q.5a "Show storage" — worker-local disk breakdown, additive.
    # Older worker binaries don't send these; None stored/returned, never an
    # error (matches disk_free_gb's own optionality above).
    disk_total_gb: Optional[float] = None
    images_bytes: Optional[int] = None
    model_cache_bytes: Optional[int] = None
    workdirs_bytes: Optional[int] = None
    reclaimable_bytes: Optional[int] = None
    docker_healthy: Optional[bool] = None
    gpu_temperature_c: Optional[int] = None
    gpu_memory_used_pct: Optional[float] = None
    active_containers: Optional[int] = None
    uptime_hours: Optional[float] = None
    agent_version: Optional[str] = None
    # Boot time + network profiling
    boot_duration_seconds: Optional[float] = None
    minio_latency_ms: Optional[float] = None
    minio_throughput_mbps: Optional[float] = None
    api_latency_ms: Optional[float] = None
    cached_images: Optional[List[str]] = None
    # Real-time resource snapshot (from monitor package)
    resource_snapshot: Optional[Dict[str, Any]] = None
    # Network zone for routing (e.g. "office", "dc-east", "cloud")
    network_zone: Optional[str] = None
    # NOAH opportunistic-harvest availability, reported only by NOAH-enabled
    # workers. Absent = the worker is not opportunistic (never released/parked).
    noah_available: Optional[bool] = None
    noah_reason: Optional[str] = None
    noah_next_free_at: Optional[datetime] = None
    # 24.5.s2 — the server-side lend/pause pin, pushed back down so the
    # Dashboard control and the machine's own tray switch drive ONE verdict.
    # Absent = the server has never pinned this machine, so it keeps whatever
    # its own tray switch decided (POST /noah/mode). An explicit "none" = the
    # server pin was cleared — drop any pin you're holding. See _resolve_noah_mode.
    noah_mode: Optional[str] = None
    # 24.5.s3 — the drain/hand-back report: {at, tasks_requeued}. Omitted by the
    # worker (heartbeat.go) until it actually hands harvest work back to a human,
    # so None = "has not said" and must never be read as "handed nothing back".
    # `at` is RFC3339 text, stamped on the FIRST hand-back of a drain and repeated
    # verbatim on every beat until the drain clears (preempt.go countHandback /
    # handbackReport), so it doubles as the event's identity — see the dedupe in
    # the heartbeat handler.
    noah_handback: Optional[Dict[str, Any]] = None
    # Per-device accelerator inventory (GPUs, NPUs, etc.). Each entry
    # is {kind, index, name, uuid, vendor_id, device_id, pci_bus_id,
    # driver_version, vram_gb}. Populated by capabilities.EnumerateAccelerators().
    accelerators: Optional[List[Dict[str, Any]]] = None
    # AI scheduler — host OS / package telemetry for calibration staleness
    # detection. Populated by capabilities.DetectSystemState() on the worker.
    # Shape: {kernel_version, os_distro, os_version, package_count, air_gapped}.
    # All fields optional: missing system_state in heartbeat just means no
    # update to WorkerSystemState (e.g. older worker binaries pre-Phase-2).
    system_state: Optional[Dict[str, Any]] = None
    # stack-ha-reconciliation §3.2 — this worker's self-fencing state, if it
    # has ever fenced. {fenced, fenced_at, lease_s, stopped_projects,
    # stop_failed?}. Absent on a worker that has never lost contact.
    fence: Optional[Dict[str, Any]] = None
    # stack-ha-reconciliation §1 — the compose projects this worker observes
    # itself running, sampled from the Docker daemon rather than from any
    # record of what it was told to deploy. Each entry is
    # {project, running, total, services}.
    #
    # None and [] mean different things and must stay distinguishable: None is
    # "this worker did not answer" (an older binary, or a failed Docker query),
    # [] is "this worker hosts nothing". Treating the first as the second would
    # read a rolling worker upgrade as a fleet-wide stack outage.
    hosted_stacks: Optional[List[Dict[str, Any]]] = None
    # Review2 L6: True only from a worker that runs the worker-controlled stack
    # start loop (MOD_STACK_WORKER_START); absent from any other worker.
    stack_loop: Optional[bool] = None
    # CPU architecture this worker runs. The Go worker has always SENT this
    # (capabilities/detect.go sets Profile.Arch, heartbeat.go puts it on the
    # payload) and this model had no field for it, so Pydantic dropped it and
    # every worker stored null. Nothing downstream could gate on architecture
    # because nothing was ever told what the architecture was — which is fine
    # while the fleet is uniformly amd64 and breaks the moment it is not.
    arch: Optional[str] = None
    # native-workers GP.1/GP.2 — the OS this worker runs (linux/windows/darwin),
    # from capabilities.Profile.OS (runtime.GOOS), already sent by the Go
    # worker's heartbeat payload (heartbeat.go: "os": h.profile.OS) but with no
    # field here to receive it, exactly like `arch` above before it was added.
    # GP.3's claim-guard platform filter reads this via hardware_specs['os'].
    os: Optional[str] = None
    # native-workers GP.4 / D-P2 — apps this worker detected at heartbeat via
    # the Go nativeapps package (registry App Paths + PATH on windows, mdfind
    # + Info.plist + PATH on darwin). Each entry is {name, version, path}.
    # Stored verbatim on hardware_specs['native_apps']; the claim guard in
    # worker_tasks._try_claim_task reads it to satisfy task_payload.requires_app.
    native_apps: Optional[List[Dict[str, Any]]] = None
    # GX.S10b — worker can materialise stack file secrets (MOD_SECRETS_DIR);
    # sent by heartbeat.go but dropped here until this field existed, so the
    # placement gate in persistent_stacks._shared never saw it.
    file_secrets: Optional[bool] = None
    # c8-W1/W2; c8-A gates in-place identity refresh on it; absent = old
    # binary = redeploy.
    secret_refresh: Optional[bool] = None
    # 993a — host-exec capability self-report (available/code/mode) from the
    # worker's own host-exec helper probe. Optional and may be absent on
    # workers running a pre-993a binary; services/worker_host_exec_service.py
    # 's resolve_availability() treats an absent value as "unknown,
    # fail-closed" rather than assuming availability.
    host_exec: Optional[Dict[str, Any]] = None
    # 25.5 — latest self-cleanup pass summary (mode, reclaimable/reclaimed
    # bytes, per-kind counts). Omitted by the worker until its first pass.
    cleanup: Optional[Dict[str, Any]] = None
    # 25.5.s5 — the sibling `last_cleanup` key: what the LAST pass actually DID
    # (cleanupPassSummary() in cleanup_bridge.go), as opposed to `cleanup`'s
    # "reclaimable → what could go". Shape: {mode, started, finished, counts,
    # bytes_freed, bytes_total, failures:[{kind, ref, reason}]}. Sanitized at
    # ingest (_sanitize_last_cleanup below: mode allow-list, non-negative ints,
    # ISO timestamps, failures capped at the Go resultFailureLimit=25 with
    # reason strings truncated to 200 chars) and stored on hardware_specs with
    # the same absent-carries-forward rule as `cleanup` above, so an older
    # binary or a pass-less beat leaves the previous reading alone.
    last_cleanup: Optional[Dict[str, Any]] = None
    # MAC.1.s13b — the worker's pre-claim free-disk gate reading
    # (diskFloorGate.Report() in cmd/worker/diskfloor.go): {state:
    # "above_floor"|"below_floor"|"unknown", free_gb, needed_gb, work_root}.
    # The manager reads it to explain a worker that heartbeats green but
    # refuses claims — disk pressure, not a bug. The worker OMITS the key
    # until its first probe (heartbeat.go: nil DiskFloorProvider), so None
    # here means "absent", never "the volume is fine". Sanitized at ingest
    # (_sanitize_disk_floor below: state allow-list, non-negative clamped
    # GB figures, work_root truncated) and stored on hardware_specs with the
    # same absent-carries-forward rule as `last_cleanup` above.
    disk_floor: Optional[Dict[str, Any]] = None
    # wslc — WSL-container backend capability (GP.17). Reported ONLY by Windows
    # workers: {available: bool, wsl_version: str, reason: str}. The key is
    # omitted by non-Windows workers, so None must mean "absent" (the key is
    # NOT stored) rather than "unavailable". Sanitized at ingest: only the
    # three keys above survive, `available` is coerced to bool, wsl_version is
    # truncated to 32 chars, reason to 256 chars (untrusted worker input).
    wslc: Optional[Dict[str, Any]] = None
    # The address this worker believes the MANAGER can reach it on, derived
    # from the route to the manager (heartbeat/advertise.go).
    #
    # A CANDIDATE, never a promise. Routing is not always symmetric and NAT
    # rewrites addresses, so a worker on another site may report one the
    # manager cannot dial. It is stored as offered and verified at the point
    # of use — the alternative is the manager guessing, which it has no basis
    # to do: `hostname` is whatever the host calls itself ("example-host") and
    # resolves nowhere else.
    advertise_address: Optional[str] = None
    # dual-anchor-mesh-federation §2 — per-anchor advertise addresses. A dual-
    # homed worker runs two tailscaled instances and reaches the manager on a
    # site-tailnet IP and a cloud-tailnet IP; it offers BOTH so the manager's
    # heartbeat registry can drive site-first / cloud-fallback resolution. Both
    # optional: a single-anchor (legacy) worker sends neither and its
    # `advertise_address` above is treated as the site address by the resolver.
    advertise_address_site: Optional[str] = None
    advertise_address_cloud: Optional[str] = None
    # Which anchors THIS heartbeat confirms the worker currently reaches (a route
    # to that anchor's control plane exists right now). {"site": true, "cloud":
    # false} means the site path is healthy and the cloud path is not, so
    # resolution should prefer site. Absent ⇒ older binary; the manager falls
    # back to `last_heartbeat` freshness on the single `advertise_address`.
    anchor_reachable: Optional[Dict[str, bool]] = None
    # WS4.Q21 — "green but silent" worker detection. Set by
    # heartbeat.MarkFetchLoopIteration (internal/heartbeat/heartbeat.go),
    # called at every pass of cmd/worker's main task fetch/claim loop (and
    # every spin of its inner capacity/NOAH gate loops, so a worker that is
    # legitimately busy or withheld is never mistaken for one that is stuck).
    # The heartbeat sender is a SEPARATE goroutine, so a deadlocked/wedged
    # fetch loop does not stop heartbeats — only this field, rising instead of
    # resetting every ~30s, reveals it. Absent ⇒ older worker binary; treated
    # as "cannot say", never as "not stalled". Consumed by
    # services.scheduler's worker-stall watchdog job (see
    # routers.worker_tasks.detect_and_recover_stalled_fetch_loops).
    last_fetch_at: Optional[str] = None
    fetch_loop_age_s: Optional[float] = None
    # Site-tailnet client status (worker internal/mesh): {mode, state, ip,
    # login_server, error}. Drives the `mesh_site` directive (a fresh key only
    # when the worker needs one) and records the worker's site address.
    site_mesh: Optional[Dict[str, Any]] = None
    # Task IDs this worker is ACTUALLY executing.
    #
    # None and [] mean different things and must stay distinguishable, for the
    # same reason as hosted_stacks: None is "this worker cannot say" (an older
    # binary), and the manager must keep renewing everything or a rolling
    # upgrade would expire the claims of every worker still on the old build.
    # [] is "I am executing nothing", which is exactly when a stale claim
    # should be allowed to die.
    active_task_ids: Optional[List[int]] = None
    # Weight downloads in flight on this host: [{repo, bytes}]. Same None/[]
    # distinction as hosted_stacks — None is "did not answer", [] is "nothing
    # is downloading", and only the second should clear a progress bar.
    #
    # This exists because a managed-AI stack pulls gigabytes before it binds a
    # port and reports no progress while doing so, which made a correct first
    # deploy indistinguishable from a hung one for anybody looking at the UI.
    model_downloads: Optional[List[Dict[str, Any]]] = None
    # Repos whose weights are already fully on this host. Lets the UI say
    # "starting in seconds" instead of leaving a blank panel while it waits to
    # find out whether a download is coming.
    cached_models: Optional[List[str]] = None
    # gw-stack-networking GW.2c — the host ports this worker is ACTUALLY
    # holding open, per protocol: {"tcp": [8080, 20000], "udp": [53]}. The
    # Go worker (internal/executor/stackhostports.go provider) reports docker
    # port bindings plus anything else it can observe cheaply; placement
    # (services/stack_port_placement.observed_bound_ports) reads the sanitized
    # copy stored on hardware_specs["host_bound_ports"] to treat a requested
    # fixed host port as a resource.
    #
    # Omitted (absent) when the worker cannot observe them — an older binary,
    # or a failed daemon query. Absent = "UNKNOWN", NOT "nothing bound": a
    # daemon hiccup must not clear the manager's last reading, so an absent
    # beat carries the previous stored value forward (see the ingest below),
    # unlike wslc where the rebuilt dict is authoritative.
    #
    # Declared `Dict[str, Any]` (like wslc / native_apps above), NOT
    # `Dict[str, List[int]]`: this is untrusted worker input, and a strict
    # `int` type would make a malformed report (a port as the string "8080",
    # or a non-numeric junk entry) fail the ENTIRE heartbeat's validation and
    # drop every other field with it. The loose type lets the beat through and
    # _sanitize_host_bound_ports is the single place that enforces
    # tcp/udp-only, int-in-1..65535, dedupe, and the per-protocol cap.
    host_bound_ports: Optional[Dict[str, Any]] = None
    # gw-stack-networking GW.2 — the SAME host ports attributed to their
    # compose project: {project: {"tcp": [8080]}} (non-stack containers live
    # under the "" project key). Lets placement exclude a redeploying
    # stack's OWN running containers from its fixed-port conflict check
    # (compose stops them before the new ones bind, so the port is not
    # really in conflict). Same absent-vs-empty distinction as
    # host_bound_ports: absent (older binary / failed daemon query) keeps
    # the previous stored value; an explicit empty {} clears it. Loose Dict
    # type (untrusted worker input) — _sanitize_host_bound_ports_by_project
    # is the single place that enforces the nested shape.
    host_bound_ports_by_project: Optional[Dict[str, Any]] = None
    # GY.C2b — the FIPS 140-3 crypto-mode block the Go worker reports on its
    # heartbeat (internal/heartbeat/crypto_report.go): {go_version,
    # fips140_mode ("on"|"off"|"only"), fips140_enabled
    # (crypto/fips140.Enabled()), boringcrypto (bool)}. PRESENCE of the key
    # is the "this binary reports its crypto mode" signal; older binaries
    # omit the block and crypto_status reports "unknown" for them. Loose
    # Dict type (like wslc / host_bound_ports above): this is untrusted
    # worker input, and a strict shape would fail the ENTIRE heartbeat's
    # validation over a malformed report.
    crypto: Optional[Dict[str, Any]] = None
    # GSEC FX.123 s3 — the worker's full-disk-encryption (FDE) status block,
    # reported by the Go worker's heartbeat (FX.123.s2, internal/heartbeat/
    # disk_encryption.go): {"status": "encrypted"|"unencrypted"|"unknown",
    # "method": "bitlocker"|"luks"|"filevault"|"..."}. PRESENCE of the key is
    # the "this binary reports FDE" signal; older binaries omit it and the
    # claim guard treats the worker as not-saying (see the ingest note below).
    # Loose Dict type like crypto/wslc above: untrusted worker input, and a
    # strict shape would fail the ENTIRE heartbeat over a malformed report.
    disk_encryption: Optional[Dict[str, Any]] = None
    # MAC.1.s12 — the native backend's honest confinement posture, reported by
    # the Go worker's heartbeat (CORE/Worker/internal/heartbeat/native_confinement.go):
    # {"os": <worker OS>, "layer": "pdeathsig"|"unsupported"}. Report-only and
    # additive: an older binary omits the block and the manager reads "unknown",
    # never "confined". Loose Dict type like crypto/wslc/disk_encryption above:
    # untrusted worker input, and a strict shape would fail the ENTIRE heartbeat
    # over a malformed report.
    native_confinement: Optional[dict] = None
    # GSEC FX.80 p3 — the worker's recent cross-tenant GPU VRAM scrubs
    # (CORE/Worker/internal/executor/gpu_tenant_scrub.go, FX.80.p2, via
    # heartbeat.OnGpuScrubs). Each entry is {card, from_tenant, to_tenant,
    # bytes_zeroed, duration_ms, ok, at}, newest last, capped by the worker's
    # ring (32). The key is OMITTED until the worker's first scrub — the
    # gate-off on-prem fleet sends nothing, so absent must stay absent
    # (there is nothing to carry forward: a scrub is a fact already recorded
    # when it happened, not a live sample like host_bound_ports above).
    # Loose List[Dict] like every other worker report: a malformed entry is
    # dropped by services/gpu_scrub_evidence._sanitize_scrub_records, it must
    # never fail the heartbeat.
    gpu_scrubs: Optional[List[Dict[str, Any]]] = None
    # GSEC FX.62 p3a1 — the Go worker's rolling self-update report
    # (CORE/Worker/internal/heartbeat/update_report.go). TRI-STATE, same nil-vs-
    # empty discipline as host_bound_ports above:
    #   key OMITTED   -> this binary never attempted a self-update cycle
    #                    (older worker, or a cycle-less process) -> carry forward
    #   "<reason>"    -> the last cycle FAILED; reason is selfUpdate's own error
    #                    (download HTTP / SHA-256 mismatch / signature refusal /
    #                    failed --version exec / atomic swap), capped by the
    #                    worker at 300 chars
    #   ""            -> the last cycle COMPLETED -> explicit CLEAR of any stale
    #                    prior failure. `is not None`, NOT truthiness: the empty
    #                    string is the success signal and must survive.
    last_update_error: Optional[str] = None
    # 1141 — the beat's own warning strings (heartbeat.go appendWarning). The
    # worker already sends them; until this field existed Pydantic dropped them
    # at ingest, so a broken-NVML worker's "GPU telemetry returned 0" warning
    # never reached the API and the card stayed placeable. Loose List[str] like
    # every other worker report: absent = the worker did not answer.
    warnings: Optional[List[str]] = None


# FX.165 p4b — importing this service is what puts worker.direct_wake_* in the
# platform-settings registry (_register_settings runs on import), which every
# key below has to be in by the time _resolve_worker_tunables reads it. No
# cycle: the service only touches crud.worker_management + platform_settings.
try:
    import services.worker_direct_wake  # noqa: F401
except (ImportError, ModuleNotFoundError):
    pass

# hx.2b — same reason as above: importing this settings module is what puts the
# four worker.host_exec_* keys in the platform-settings registry (_register_
# settings runs on import), which every key in the map below has to be in by
# the time _resolve_worker_tunables reads it. No cycle: the module only touches
# platform_settings. hx.5's router/service is not mounted yet, so this router is
# currently the only eager importer — without it an unregistered key would make
# resolve_source raise and drop the WHOLE tunables map from the heartbeat.
import services.worker_host_exec_settings  # noqa: F401

# GS.PSR.7 — platform_settings registry key -> heartbeat `tunables` wire key.
# The Go worker (internal/tunables.Store) looks up entries by the registry
# key verbatim, so this dict is the ONLY place the two spellings are allowed
# to differ; keep it that way rather than inventing a second mapping.
# worker.model_mirror_endpoint is NOT here: its value is derived (GF.C0b.9),
# see _resolve_worker_tunables.
_WORKER_TUNABLE_KEYS = {
    # KEEP-WARM-SLICE-1 — the platform kill switch rides the heartbeat under
    # the key the Go worker reads for its own kill switch
    # (worker.warm_pool_enabled in internal/executor/warm_pool.go). The
    # registry key of that name is the TENANT switch, which is API-side only.
    "worker.warm_pool_allowed": "worker.warm_pool_enabled",
    "worker.egress_proxy_mode": "worker.egress_proxy_mode",
    # Q20 — Docker socket proxy mode (off | report | enforce); consumed
    # by the worker's executor when bind-mounting a docker socket into
    # task containers.
    "worker.docker_proxy_mode": "worker.docker_proxy_mode",
    "worker.prefer_local_images": "worker.prefer_local_images",
    "worker.harbor_proxy_cache": "worker.harbor_proxy_cache",
    "worker.cleanup_reclaim": "worker.cleanup_reclaim",
    "worker.cleanup_image_high_water_pct": "worker.cleanup_image_high_water_pct",
    "worker.cleanup_image_low_water_pct": "worker.cleanup_image_low_water_pct",
    "worker.cleanup_model_cache_max_gb": "worker.cleanup_model_cache_max_gb",
    "worker.cleanup_grace_hours": "worker.cleanup_grace_hours",
    # 25.5.s2 — the two extra GC caps (image-count LRU cap + model-cache GB
    # cap), consumed by cleanup_bridge.go (cleanupImageCountCapTunableKey /
    # cleanupModelCacheCapGbTunableKey) and normalised in planner.go.
    "worker.cleanup_image_count_cap": "worker.cleanup_image_count_cap",
    "worker.cleanup_model_cache_cap_gb": "worker.cleanup_model_cache_cap_gb",
    # 24.5.s3 — NOAH drain/hand-back grace: how long the worker keeps a paused
    # (handed-back) machine's harvest slots before requeuing (noah.go
    # handbackGraceTunableKey). Delivered via the heartbeat `tunables` map.
    "worker.noah_handback_grace_s": "worker.noah_handback_grace_s",
    # MAC.1.s13b — free-disk guard: the worker refuses to CLAIM new tasks while
    # free disk on its work root is below this many GB (Go claimMinFreeGbTunableKey
    # default 10). 0 = guard off. Tasks already running are unaffected. Delivered
    # via the heartbeat `tunables` map, so an admin override needs no restart.
    "worker.claim_min_free_gb": "worker.claim_min_free_gb",
    "worker.stack_host_port_range": "worker.stack_host_port_range",
    "worker.native_allow_unsandboxed_tenant_code": "worker.native_allow_unsandboxed_tenant_code",
    "worker.native_drop_uid": "worker.native_drop_uid",
    # MAC.1.s12 — the darwin twin of native_drop_uid: the dedicated less-privileged
    # uid a macOS native worker runs its curated-recipe processes as
    # (CORE/Worker/internal/executor/native_confine_policy.go, darwinDropUIDKey).
    # 0 / unset = "no dedicated user configured" -> the recipe runs as the worker's
    # own user, i.e. the worker's built-in behaviour is unchanged.
    "worker.native_darwin_uid": "worker.native_darwin_uid",
    # GSEC R-059 — internal-TLS enforcement gate (off | report | require),
    # consumed by the worker's node API_URL override (executor).
    "gsec.transport.internal_tls_enforce": "gsec.transport.internal_tls_enforce",
    # GY.C8 — egress gates for the worker's model cache: sealed installs must
    # never probe or select the public HF upstream (executor modelcache).
    "egress.sealed_mode": "egress.sealed_mode",
    "egress.model_download_enabled": "egress.model_download_enabled",
    # GSEC Q21.4 — per-task credential refresh cadence for the worker's
    # creds-refresher loop (services/worker_approle broker lifecycle).
    "worker.creds_refresh_interval": "worker.creds_refresh_interval",
    # GSEC FX.80 p2/p3 — the worker's cross-tenant GPU VRAM scrub gate + its
    # helper image + timeout (gpu_tenant_scrub.go reads all three off this
    # same map). Registered in services/platform_settings.py; the gate
    # defaults OFF so an untouched fleet never scrubs and never starts a
    # helper container.
    "worker.gpu_tenant_scrub": "worker.gpu_tenant_scrub",
    "worker.gpu_tenant_scrub_image": "worker.gpu_tenant_scrub_image",
    "worker.gpu_tenant_scrub_timeout_s": "worker.gpu_tenant_scrub_timeout_s",
    "worker.gpu_tenant_scrub_reserve_mib": "worker.gpu_tenant_scrub_reserve_mib",
    # GSEC FX.115 p1a — node container lockdown flags (each off|report|enforce,
    # default report = log what would change, change nothing) + the interim
    # node-kind exception allow-list. Consumed by the worker's
    # internal/executor/node_lockdown.go at container-create.
    "worker.node_lockdown_nonroot": "worker.node_lockdown_nonroot",
    "worker.node_lockdown_cap_drop_all": "worker.node_lockdown_cap_drop_all",
    "worker.node_lockdown_no_new_privs": "worker.node_lockdown_no_new_privs",
    "worker.node_lockdown_exempt_nodes": "worker.node_lockdown_exempt_nodes",
    # GSEC FX.115 p1b — the second half of the SAME lockdown layer (seccomp
    # profile + its name, read-only rootfs + scratch tmpfs, and the pids/
    # memory/cpu limit FLOORS with their _cap values). Same off|report|enforce
    # contract, default report, same consumer (node_lockdown.go applyNodeLockdown)
    # and the same exempt_nodes allow-list applies to these knobs too.
    "worker.node_lockdown_seccomp": "worker.node_lockdown_seccomp",
    "worker.node_lockdown_seccomp_profile": "worker.node_lockdown_seccomp_profile",
    "worker.node_lockdown_readonly_rootfs": "worker.node_lockdown_readonly_rootfs",
    "worker.node_lockdown_limits_pids": "worker.node_lockdown_limits_pids",
    "worker.node_lockdown_limits_pids_cap": "worker.node_lockdown_limits_pids_cap",
    "worker.node_lockdown_limits_memory": "worker.node_lockdown_limits_memory",
    "worker.node_lockdown_limits_memory_cap_mb": "worker.node_lockdown_limits_memory_cap_mb",
    "worker.node_lockdown_limits_cpu": "worker.node_lockdown_limits_cpu",
    "worker.node_lockdown_limits_cpu_cap_cores": "worker.node_lockdown_limits_cpu_cap_cores",
    # GSEC FX.115 p4 — hosted gVisor (runsc) as the runtime for node
    # containers (off|report|enforce, default off) + the GPU sub-flag (gVisor
    # nvproxy is limited, so a GPU-requesting container stays on runc unless
    # this is explicitly on). Same consumer (node_lockdown.go applyNodeLockdown)
    # and the same p3 exempt list (a `kind:gvisor` entry keeps that node on
    # runc). Nothing here installs runsc or touches the docker daemon.
    "worker.node_runtime_gvisor": "worker.node_runtime_gvisor",
    "worker.node_runtime_gvisor_gpu": "worker.node_runtime_gvisor_gpu",
    # FX.165 p4b — the Go wake listener (CORE/Worker/internal/wake/listener.go)
    # reads these two off the SAME tunables map: enabled gates whether the API
    # fires a direct wake at all, port is where the worker's listener answers.
    # Registered in services/worker_direct_wake.py (imported above this map);
    # with no
    # DB row both are omitted, so an untouched fleet sends nothing new.
    "worker.direct_wake_enabled": "worker.direct_wake_enabled",
    "worker.direct_wake_port": "worker.direct_wake_port",
    # hx.2b — the host-exec knobs (services/worker_host_exec_settings.py, hx.2).
    # The worker runs host-exec commands on its own host filesystem/process
    # space, so all four have to reach it the same admin-override-without-a-
    # restart way every other knob does: enabled gates the behaviour at all
    # (opt-in, default False), max_output_bytes caps captured stdout+stderr per
    # invocation, default_timeout_s is the wall-clock default when the caller
    # doesn't pass one, workspace_root is the filesystem root those commands
    # run under. Spelled identically on the wire (the Go tunables store looks
    # entries up by the registry key verbatim).
    "worker.host_exec_enabled": "worker.host_exec_enabled",
    "worker.host_exec_max_output_bytes": "worker.host_exec_max_output_bytes",
    "worker.host_exec_default_timeout_s": "worker.host_exec_default_timeout_s",
    "worker.host_exec_workspace_root": "worker.host_exec_workspace_root",
    # 921 — Fortress 3.5 output-scan mode: off|shadow|enforce. The worker's
    # executor (executor.go:outputScanModeTunableKey) reads the SAME key from
    # the heartbeat tunables map. Default shadow (fail-open).
    "fortress.task_output_scan_mode": "fortress.task_output_scan_mode",
}


# MAC.1.s12 — registry-side declaration for the darwin uid knob. The registry
# lives in services/platform_settings.py, which owns every other worker.* row;
# this key is declared HERE because the worker-management router is its only
# consumer and the module must import cleanly on a CE fleet that never touches
# that file. register() is a no-op when the key is already present, so a later
# platform_settings row for the same key (or a double import) never clobbers a
# spec and this block never fights the platform registry.
#
# Default 0 = "no dedicated user configured" (the Go getter's own sentinel):
# with no DB row, _resolve_worker_tunables omits the key entirely, so the
# worker keeps its built-in behaviour — an untouched fleet is unaffected.
try:
    from services import platform_settings as _ps_reg
    if "worker.native_darwin_uid" not in _ps_reg._REGISTRY:  # noqa: SLF001
        _ps_reg.register(
            "worker.native_darwin_uid",
            int,
            0,
            group="worker",
            description="MAC.1.s12 — uid a darwin (macOS) native worker runs its "
                        "curated-recipe processes as, when this worker is root. "
                        "0 (default) = no dedicated user configured: the recipe "
                        "runs as the worker's own user. The darwin twin of "
                        "worker.native_drop_uid; darwin has no Pdeathsig, so this "
                        "uid drop is NOT a confinement claim.",
        )
except Exception:  # noqa: BLE001
    logger.exception("worker.native_darwin_uid registration failed")


# 25.5.s2 — the two extra self-cleanup GC caps. Declared HERE (not in
# services/platform_settings.py) for the same reason as the darwin uid knob
# above: this router is their only consumer and the module must import cleanly
# without touching that file. Both default 0 = OFF, so an untouched fleet keeps
# the worker's built-in policy byte-for-byte; a DB row is the only thing that
# turns either cap on, and it travels to the worker in the heartbeat `tunables`
# map (see _WORKER_TUNABLE_KEYS / _resolve_worker_tunables).
def _register_cleanup_caps() -> None:
    """Register the 25.5.s2 cleanup caps in the platform-settings registry."""
    try:
        from services import platform_settings as _ps_reg
        if "worker.cleanup_image_count_cap" not in _ps_reg._REGISTRY:  # noqa: SLF001
            _ps_reg.register(
                "worker.cleanup_image_count_cap",
                int,
                0,
                validator=_validate_cleanup_cap_nonneg,
                restart_required=False,
                group="worker",
                description="25.5.s2 — maximum number of UNUSED images the worker's "
                            "self-cleanup LRU image GC keeps on the box. 0 (default) = "
                            "off: the watermarks alone decide, exactly as before. "
                            "Pinned/protected images are never counted or evicted. "
                            "Read by cleanup_bridge.go (cleanupImageCountCapTunableKey); "
                            "delivered via the heartbeat `tunables` map — no worker "
                            "restart.",
            )
        if "worker.cleanup_model_cache_cap_gb" not in _ps_reg._REGISTRY:  # noqa: SLF001
            _ps_reg.register(
                "worker.cleanup_model_cache_cap_gb",
                int,
                0,
                validator=_validate_cleanup_cap_nonneg,
                restart_required=False,
                group="worker",
                description="25.5.s2 — GiB ceiling on the worker's model cache; above it "
                            "the self-cleanup GC evicts least-recently-used model dirs "
                            "down to the cap. 0 (default) = off: the worker's built-in "
                            "500 GiB ceiling stays the only size arm, so this knob only "
                            "ever TIGHTENS that cap (planner.go normalise() takes the "
                            "tighter of the two). A model in use by a running task/stack "
                            "is never a victim, and the grace window is the floor. "
                            "Read by cleanup_bridge.go "
                            "(cleanupModelCacheCapGbTunableKey); delivered via the "
                            "heartbeat `tunables` map — no worker restart.",
            )
    except Exception:  # noqa: BLE001
        logger.exception("worker.cleanup_* cap registration failed")


def _validate_cleanup_cap_nonneg(value: int) -> None:
    """0 (off) up to a generous ceiling for the 25.5.s2 cleanup caps."""
    if not 0 <= value <= 100000:
        raise ValueError(
            "worker.cleanup_image_count_cap / worker.cleanup_model_cache_cap_gb "
            "must be 0-100000 (0 = off), got "
            f"{value!r}"
        )


_register_cleanup_caps()


# 24.5.s3 — the NOAH drain/hand-back grace, declared HERE (not in
# services/platform_settings.py) for the same reason as the knobs above: this
# router is its only consumer and the module must import cleanly on a CE fleet
# that never touches that file. The Go worker (noah.go handbackGraceTunableKey)
# reads it off the heartbeat `tunables` map, so a DB row needs no worker
# restart. Default 30s matches the worker's own built-in grace, so an
# untouched fleet is byte-for-byte unchanged.
def _register_noah_handback_grace() -> None:
    """Register the 24.5.s3 hand-back grace in the platform-settings registry."""
    try:
        from services import platform_settings as _ps_reg
        if "worker.noah_handback_grace_s" not in _ps_reg._REGISTRY:  # noqa: SLF001
            _ps_reg.register(
                "worker.noah_handback_grace_s",
                int,
                30,
                validator=_validate_noah_handback_grace_s,
                restart_required=False,
                group="worker",
                description="24.5.s3 — seconds a NOAH worker holds its hand-back "
                            "(drain) grace before requeuing harvest work it can no "
                            "longer run. The worker keeps its own built-in default "
                            "when no DB row exists; a DB row travels to the worker "
                            "via the heartbeat `tunables` map, so changing it needs "
                            "no worker restart. Read by noah.go "
                            "(handbackGraceTunableKey).",
            )
    except Exception:  # noqa: BLE001
        logger.exception("worker.noah_handback_grace_s registration failed")


def _validate_noah_handback_grace_s(value: int) -> None:
    """0 (drain immediately) up to one hour for the 24.5.s3 hand-back grace."""
    if not 0 <= value <= 3600:
        raise ValueError(
            "worker.noah_handback_grace_s must be 0-3600 seconds, got "
            f"{value!r}"
        )


_register_noah_handback_grace()


# MAC.1.s13b — free-disk guard tunable. Declared IN-MODULE (never in
# services/platform_settings.py) exactly like the hand-back grace above: the
# Go worker (claimMinFreeGbTunableKey) reads it off the heartbeat `tunables`
# map, so a DB row needs no worker restart. Default 10 matches the worker's
# own built-in guard, so an untouched fleet is byte-for-byte unchanged; 0
# turns the guard off.
def _register_claim_min_free_gb() -> None:
    """Register the MAC.1.s13b claim free-disk guard in the registry."""
    try:
        from services import platform_settings as _ps_reg
        if "worker.claim_min_free_gb" not in _ps_reg._REGISTRY:  # noqa: SLF001
            _ps_reg.register(
                "worker.claim_min_free_gb",
                int,
                10,
                validator=_validate_claim_min_free_gb,
                restart_required=False,
                group="worker",
                description="MAC.1.s13b — Worker skips claiming new tasks while free "
                            "disk on its work root is below this many GB (0 disables). "
                            "Running tasks are unaffected. The worker keeps its own "
                            "built-in default (10 GB) when no DB row exists; a DB row "
                            "travels to the worker via the heartbeat `tunables` map, so "
                            "changing it needs no worker restart. Read by the Go worker "
                            "(claimMinFreeGbTunableKey).",
            )
    except Exception:  # noqa: BLE001
        logger.exception("worker.claim_min_free_gb registration failed")


def _validate_claim_min_free_gb(value: int) -> None:
    """0 (guard off) up to 100000 GB for the MAC.1.s13b claim guard."""
    if not 0 <= value <= 100000:
        raise ValueError(
            "worker.claim_min_free_gb must be 0-100000 GB, got "
            f"{value!r}"
        )


_register_claim_min_free_gb()


# FIX-CE-WORKER-TUNABLE-CLEANUP-MODEL-CACHE-DELIVERY: a CE-unlimited registry
# default (worker.cleanup_model_cache_max_gb = 0, "no model-cache eviction")
# has no DB row, so the 'db'-only rule below would omit it and the worker
# would keep its own 500 GiB cap. These keys ALSO ship when their source is
# 'default', but only on the CE edition (call-time probe). Paid, and a probe
# error (fail-to-PAID), keep the historical omission, so the paid payload is
# byte-identical to before.
_CE_DEFAULT_SHIPPED_KEYS = frozenset({"worker.cleanup_model_cache_max_gb"})

# KEEP-WARM-SLICE-1 — shipped on EVERY edition even at its default: the
# platform allowance defaults True but the Go worker's own default is off, so
# an omitted key would silently disable warm pools. Harmless on its own: a task
# only runs warm when the node flag AND the tenant switch are also on.
_DEFAULT_SHIPPED_KEYS = frozenset({"worker.warm_pool_allowed"})


def _ce_default_ships() -> bool:
    """True only when the CE probe says CE; any probe failure is PAID (omit)."""
    try:
        from utils.paid_import import is_ce_edition
        return bool(is_ce_edition())
    except Exception:  # noqa: BLE001 — fail to PAID: the default is omitted
        return False


def _resolve_worker_tunables(db: Session) -> Optional[Dict[str, Any]]:
    """Worker tunables sourced from the platform_settings registry
    (GS.PSR.7). Only keys with a live DB value are included — a key with no
    row (source in {'env', 'default'}) is omitted entirely so the worker's
    own local env/default keeps applying, and an untouched fleet is
    unaffected by this landing. Never raises: a resolution problem just
    drops tunables from this one heartbeat rather than failing it."""
    try:
        from services import platform_settings as _ps
        from services.platform_settings import SettingNotRegisteredError  # noqa: F401
        out: Dict[str, Any] = {}
        for reg_key, wire_key in _WORKER_TUNABLE_KEYS.items():
            try:
                value, source = _ps.resolve_source(reg_key, db)
            except SettingNotRegisteredError:
                # Not yet registered (parallel task): skip so the heartbeat
                # stays live.  The doc guarantees we never fail the heartbeat.
                continue
            if source == "db":
                out[wire_key] = value
            elif (source == "default"
                  and reg_key in _CE_DEFAULT_SHIPPED_KEYS
                  and _ce_default_ships()):
                out[wire_key] = value
            elif source == "default" and reg_key in _DEFAULT_SHIPPED_KEYS:
                out[wire_key] = value
        # GF.C0b.9 — model-cache GC interlock. Unlike the keys above, this
        # value is DERIVED, not a DB row: the API points managed-AI stacks at
        # the mirror via model_cache_endpoint() exactly while
        # model_cache_enabled is on, and hands the worker the SAME endpoint
        # so its on-box GC evicts mirror-sourced weights first. It is
        # omitted (never sent as "") when the mirror is off or BASE_DOMAIN is
        # unset, so an untouched deployment is byte-for-byte unaffected and
        # the worker falls back to each stack's own HF_ENDPOINT.
        # 1665r2 — services/managed_ai_stacks.py is denied in the CE carve, so
        # the raw import used to raise here and land in the outer except
        # (ERROR + traceback on every heartbeat). The mirror endpoint is a
        # Paid derivation: absent module = omit the key, quietly (same
        # contract as "mirror off" — never sent as "").
        _mai = optional_paid("services.managed_ai_stacks")
        if _mai is not None:
            endpoint = _mai.model_cache_endpoint()
            if endpoint:
                out["worker.model_mirror_endpoint"] = endpoint
        # GSEC FX.115 p3 — the DERIVED lockdown exempt list. Like the mirror
        # endpoint above, this is not a registry row: it is the live,
        # un-expired models.NodeLockdownException rows UNION the legacy
        # worker.node_lockdown_exempt_nodes setting (which keeps working as
        # a fallback/union for one release), spelled `kind` for a whole-node
        # exemption and `kind:flag` for one relaxed flag. Expiry is resolved
        # HERE, so the worker never parses a date. Omitted (never sent as
        # "") when nothing is exempted, so an untouched fleet is
        # byte-for-byte unaffected.
        try:
            from services import node_lockdown_report as _nlr
            exempt_list = _nlr.exempt_list_value(db)
            if exempt_list:
                out[_nlr.EXEMPT_LIST_KEY] = exempt_list
        except Exception:
            logger.exception("lockdown exempt-list derivation failed")
        return out or None
    except Exception:
        logger.exception("worker tunables resolution failed")
        return None


def _approle_mode(db: Session) -> str:
    """GSEC Q21.4 — per-worker AppRole enforcement mode (off|report|enforce).

    Resolves to 'off' on ANY failure (unknown key, registry error, bad
    value): the lifecycle must stay inert unless an admin explicitly
    flipped the platform setting to report/enforce."""
    try:
        from services import platform_settings as _ps
        mode = _ps.get_setting("worker.approle_per_worker_enforced", db=db)
        return mode if mode in ("off", "report", "enforce") else "off"
    except Exception:
        return "off"


def _resolve_heartbeat_client_ip(http_request: Request) -> str:
    """1675b — client-IP resolution that never 402s on CE.

    The heartbeat used to import services.client_ip.resolve_client_ip bare.
    That module is DENIED in open-core/manifest.yaml (950), so on CE the import
    raises ModuleNotFoundError and main.py's central edition handler
    (_handle_edition_missing_import, main.py:831) answers HTTP 402 — the exact
    "heartbeat API: HTTP 402" the stranger-boot CE run logged
    (decomp/decisions/ce-stranger-boot-1015e.md). The client IP is only a rate-
    limit key + the internal-network heuristic below, so a CE build falls back
    to utils/client_ip.py (api_allow 573, no paid deps) and, if that is
    unusable, to the raw peer address. Fail-open on the fallback: an
    unresolvable IP must never refuse the beat — auth is the bootstrap token,
    not network position.
    """
    try:
        from services.client_ip import resolve_client_ip  # FX.106 p3a — real client IP
        return resolve_client_ip(http_request) or "unknown"
    except ImportError:  # CE: services/client_ip.py is not in this edition
        try:
            from utils.client_ip import trusted_client_ip as _trusted_client_ip
            return _trusted_client_ip(http_request) or "unknown"
        except Exception:  # noqa: BLE001 — never fail the beat over an IP
            return "unknown"
    except Exception:  # noqa: BLE001 — resolver bug must not refuse a worker
        return "unknown"


def _ensure_worker_approle_on_enrollment(worker, db: Session) -> None:
    """GSEC Q21.4 — bring a newly-enrolled worker's Vault AppRole up
    (mode != off). Called AFTER the enrollment commit so the Vault calls
    never ride the heartbeat transaction. A failure is logged and NEVER
    blocks the worker from coming online: until the Q21.12 cutover the
    worker keeps its shared-role fallback."""
    if _approle_mode(db) == "off":
        return
    try:
        from services.worker_approle import ensure_approle
        ensure_approle(worker.id, db=db)
    except Exception:
        logger.exception(
            "worker AppRole ensure failed at enrollment for worker %s "
            "(id=%s); shared-role fallback remains in effect",
            worker.worker_name, worker.id,
        )


def _revoke_worker_approle(worker_id, db: Session,
                           *, user_id=None, tenant_id=None,
                           worker_name=None) -> None:
    """GSEC Q21.4 — drop a deactivated/removed worker's Vault AppRole
    (mode != off). Failures are logged and NEVER block the deactivation
    or removal: the DB-side state change is authoritative and the worker
    is already out of the task pool.

    FX.123 p3 — when `worker_name` is given, the credential destruction
    this helper actually performed is recorded as the retire wipe record
    (services.asset_inventory.record_retire_wipe: what went, when, by
    whom, method) on the asset row's hardware_specs JSONB + one append-
    only audit row. Nothing extra is destroyed here — the record only
    describes what the retire flow already destroyed.

    FX.123 p3a — the DB-side per-worker credentials (the WorkerEnrollment
    row's bootstrap token + approval code, and the single-use enrollment
    token that minted them) are soft-revoked here too, INDEPENDENT of the
    Vault AppRole mode: they exist whether or not Vault is on. The record
    carries them as `key_destruction: [{kind, id, revoked_at}]` — row ids
    only, never key material — linked to the same wipe record. Idempotent:
    an already-revoked credential contributes nothing, so retiring twice
    never double-revokes nor duplicates the record."""
    key_destruction: list[dict] = []
    if worker_name is not None:
        try:
            from utils.paid_import import optional_paid
            _asset_inv = optional_paid("services.asset_inventory")
            if _asset_inv is None:
                # 1788d CE carve: the key-destruction record is a paid audit
                # record; the credential revoke below still runs unchanged.
                pass
            else:
                key_destruction = _asset_inv.revoke_worker_keys(
                    db, worker_name,
                    actor_user_id=user_id, tenant_id=tenant_id,
                ) or []
        except Exception:
            logger.exception(
                "FX.123 p3a worker key revoke failed for worker %s "
                "(deactivation/removal continues)", worker_name,
            )

    def _record(destroyed: list[str]) -> None:
        if worker_name is None or (not destroyed and not key_destruction):
            return
        try:
            from utils.paid_import import optional_paid
            _asset_inv = optional_paid("services.asset_inventory")
            if _asset_inv is not None:
                _asset_inv.record_retire_wipe(
                    db, worker_name, destroyed=destroyed,
                    actor_user_id=user_id, tenant_id=tenant_id,
                    key_destruction=key_destruction,
                )
        except Exception:
            logger.exception(
                "FX.123 retire wipe record failed for worker %s "
                "(retire continues)", worker_name or worker_id,
            )

    if _approle_mode(db) == "off":
        # Vault off: nothing Vault-side existed, but the DB-side
        # credentials above were still destroyed — record those.
        _record([])
        return
    # CARVE-U7 — services/worker_approle.py is DENIED by open-core/manifest.yaml
    # and `_ensure_worker_approle_on_enrollment` never CREATES a per-worker
    # AppRole on CE either, so on CE nothing Vault-side exists to revoke: absent
    # => the same shape as mode=="off" (record the DB-side destruction, return).
    # The import used to sit INSIDE the handler below, which also swallowed a
    # PRESENT module's import failure — reporting a clean retire while the
    # Vault role and its issued tokens survived (fail-open on a security
    # teardown). optional_paid answers None only for a genuinely absent module;
    # any other ImportError propagates: no silent clean retire. A revoke that
    # RUNS and fails (Vault down) stays logged and non-fatal, as documented and
    # as test_gsec_fx126_asset_inventory pins.
    from utils.paid_import import optional_paid
    _approle_mod = optional_paid("services.worker_approle")
    if _approle_mod is None:
        _record([])
        return
    destroyed: list[str] = []
    try:
        outcome = _approle_mod.revoke_approle(
            worker_id, db=db, user_id=user_id, tenant_id=tenant_id,
        )
        if outcome.get("role_deleted"):
            destroyed.append("vault_approle_role")
        if outcome.get("tokens_revoked"):
            destroyed.append("approle_issued_tokens_and_secret_ids")
        if outcome.get("state_removed"):
            destroyed.append("approle_state_kv")
    except Exception:
        logger.exception(
            "worker AppRole revoke failed for worker id=%s "
            "(deactivation/removal continues)", worker_id,
        )
    _record(destroyed)


def _resolve_noah_settings(db: Session, worker) -> dict:
    """Resolved NOAH settings for the heartbeat response; disabled on any error
    so a resolution problem can never accidentally start harvesting a machine."""
    # 1665r2 — CE carve: services/noah.py is denied. The raw import used to
    # raise on every heartbeat (traceback flood) and its except swallowed it
    # with a log; skip quietly instead — the documented "disabled" shape.
    _noah = optional_paid("services.noah")
    if _noah is None:
        return {"enabled": False}
    try:
        return _noah.resolve_settings(db, worker)
    except Exception:
        logger.exception("NOAH settings resolution failed for %s", getattr(worker, "worker_name", "?"))
        return {"enabled": False}


def _resolve_noah_mode(db: Session, worker) -> Optional[str]:
    """24.5.s2 — the worker's server-side lend/pause pin for the heartbeat.

    Returns a word (lend|pause|none) when the worker HAS a server pin row, and
    None — i.e. the key is omitted — when it never has been pinned from the
    server at all. The distinction matters exactly as it does for `tunables`:
    an absent key means "the server has no opinion, keep whatever this machine
    decided locally" (its own tray switch, POST /noah/mode), while an explicit
    "none" means "the server pin was cleared, drop any pin you're holding".
    A pause pin whose `until` has passed resolves to "none" (self-release), so
    the worker is handed the clear on the next beat.
    """
    # 1665r2 — CE carve: no NOAH module = no server pin to hand back, so the
    # key is simply omitted (None), quietly.
    _noah = optional_paid("services.noah")
    if _noah is None:
        return None
    try:
        if _noah.get_mode_row(db, worker) is None:
            return None
        mode, _, _ = _noah.resolve_mode(db, worker)
        return mode
    except Exception:
        logger.exception("NOAH mode resolution failed for %s", getattr(worker, "worker_name", "?"))
        return None


def _sanitize_noah_handback(raw: Any) -> Optional[Dict[str, Any]]:
    """24.5.s3 — normalise the worker's hand-back report {at, tasks_requeued}.

    Untrusted worker input: `at` must parse as a timestamp (stored as ISO-8601
    text so the GET side can hand it back verbatim), `tasks_requeued` must be a
    non-negative int. Anything unusable returns None — the heartbeat never fails
    because of a bad report, it just does not record one.
    """
    if not isinstance(raw, dict):
        return None
    _at = _iso_noah_handback_at(raw.get("at"))
    if _at is None:
        return None
    _n = raw.get("tasks_requeued")
    if isinstance(_n, bool):
        _n = int(_n)
    if not isinstance(_n, int) or _n < 0 or _n > 1000000:
        _n = 0
    return {"at": _at, "tasks_requeued": _n}


def _iso_noah_handback_at(value: Any) -> Optional[str]:
    """Parse the worker's RFC3339 `at` into ISO-8601 text, or None."""
    if value is None:
        return None
    try:
        from datetime import datetime, timezone
        _txt = str(value).strip().replace("Z", "+00:00")
        _dt = datetime.fromisoformat(_txt)
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        return _dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


# 25.5.s5 — the Go side's bounded vocabulary, mirrored so a spoofed heartbeat
# can never smuggle arbitrary keys/strings into the worker's JSONB.
# _LAST_CLEANUP_MODES: cleanupPassSummary() copies Result.Mode, which
# newResult() in internal/cleanup/executor.go sets to exactly one of these two.
# _LAST_CLEANUP_KINDS: the kind literals passed to noteFailure()/note() by
# executor.go's reclaim pass.
# _LAST_CLEANUP_FAILURE_CAP: executor.go's `const resultFailureLimit = 25` —
# the Go side already caps its own list, the API re-caps defensively.
_LAST_CLEANUP_MODES = ("dry_run", "reclaim")
_LAST_CLEANUP_KINDS = ("containers", "images", "model_dirs", "volumes", "networks", "task_dirs")
_LAST_CLEANUP_FAILURE_CAP = 25
_LAST_CLEANUP_REF_CHARS = 200
_LAST_CLEANUP_REASON_CHARS = 200
_LAST_CLEANUP_MAX_BYTES = 1 << 60  # 1 TiB: no real pass frees more than this

# MAC.1.s13b — the Go side's bounded disk-floor vocabulary, mirrored so a
# spoofed heartbeat can never smuggle arbitrary keys/strings into the worker's
# JSONB. _DISK_FLOOR_STATES: diskfloor.go's `stateName` map (0 above_floor /
# 1 below_floor / 2 unknown). _DISK_FLOOR_MAX_GB: the ceiling the platform
# settings registry already enforces on worker.claim_min_free_gb (0-100000),
# so a reported figure can never exceed the tunable's own domain.
# _DISK_FLOOR_ROOT_CHARS: work_root is diskWorkRoot() — /var/lib/mod-worker or
# os.TempDir(), both short — the cap only exists for the spoofed case.
_DISK_FLOOR_STATES = ("above_floor", "below_floor", "unknown")
_DISK_FLOOR_MAX_GB = 100000
_DISK_FLOOR_ROOT_CHARS = 512


def _nonneg_int(value: Any, ceiling: int) -> int:
    """Coerce untrusted worker input to a non-negative int, clamped both ways."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and float(int(value)) == value:
        n = int(value)
    else:
        n = 0
    if n < 0:
        return 0
    return n if n <= ceiling else ceiling


def _sanitize_last_cleanup(raw: Any) -> Optional[Dict[str, Any]]:
    """25.5.s5 — normalise the worker's `last_cleanup` pass RESULT summary.

    cleanupPassSummary() in cleanup_bridge.go sends {mode, started, finished,
    counts, bytes_freed, bytes_total, failures:[{kind, ref, reason}]}. This is
    untrusted worker input, so only the known fields survive, with bounds:
    mode against the Go allow-list, every count/byte figure a non-negative
    clamped int, both timestamps re-emitted as ISO-8601 text (None when
    unparseable — the pass still counts), and `failures` capped at the Go
    resultFailureLimit with kind/ref/reason truncated. Garbage returns None:
    the heartbeat never fails because of a bad summary, it just records none.
    """
    if not isinstance(raw, dict):
        return None
    # Nothing recognisable at all = no pass to record (an empty/garbage-only
    # payload must not mint a phantom "reclaim" pass on the worker row).
    if not any(k in raw for k in ("mode", "started", "finished", "counts",
                                  "bytes_freed", "bytes_total", "failures")):
        return None
    _mode = raw.get("mode")
    _mode = str(_mode).strip().lower() if _mode is not None else ""
    if _mode not in _LAST_CLEANUP_MODES:
        # A worker that invents a mode is still reporting a pass; keep the
        # summary but never store the unrecognised word.
        _mode = "dry_run" if "dry" in _mode else "reclaim"
    out: Dict[str, Any] = {"mode": _mode}
    for _key in ("started", "finished"):
        _iso = _iso_noah_handback_at(raw.get(_key))
        if _iso is not None:
            out[_key] = _iso
    _counts = raw.get("counts")
    if isinstance(_counts, dict):
        _clean_counts = {}
        for _k, _v in list(_counts.items())[: len(_LAST_CLEANUP_KINDS)]:
            _k = str(_k).strip().lower()
            if _k in _LAST_CLEANUP_KINDS:
                _clean_counts[_k] = _nonneg_int(_v, 1000000)
        if _clean_counts:
            out["counts"] = _clean_counts
    _freed = raw.get("bytes_freed")
    if isinstance(_freed, dict):
        _clean_freed = {}
        for _k, _v in list(_freed.items())[: len(_LAST_CLEANUP_KINDS)]:
            _k = str(_k).strip().lower()
            if _k in _LAST_CLEANUP_KINDS:
                _clean_freed[_k] = _nonneg_int(_v, _LAST_CLEANUP_MAX_BYTES)
        if _clean_freed:
            out["bytes_freed"] = _clean_freed
    out["bytes_total"] = _nonneg_int(raw.get("bytes_total"), _LAST_CLEANUP_MAX_BYTES)
    _fails = raw.get("failures")
    _clean_fails = []
    if isinstance(_fails, list):
        for _f in _fails[: _LAST_CLEANUP_FAILURE_CAP]:
            if not isinstance(_f, dict):
                continue
            _kind = str(_f.get("kind") or "").strip().lower()
            if _kind not in _LAST_CLEANUP_KINDS:
                continue
            _entry = {"kind": _kind}
            for _fk, _cap in (("ref", _LAST_CLEANUP_REF_CHARS), ("reason", _LAST_CLEANUP_REASON_CHARS)):
                _fv = _f.get(_fk)
                if _fv is not None:
                    _entry[_fk] = str(_fv)[:_cap]
            _clean_fails.append(_entry)
    out["failures"] = _clean_fails
    return out


def _disk_floor_gb(value: Any) -> float:
    """Coerce an untrusted GB figure to a non-negative float, clamped both ways.

    Unlike `_nonneg_int` above this KEEPS the fraction: diskfloor.go's Report()
    sends free_gb as float64(free)/diskGiB, so 12.34 GB is a meaningful reading
    and truncating it would make a worker sitting just above the floor look
    like it is below it. Values that are not numbers at all (or bools) record
    0.0 — "no usable measurement" — which the manager reads as pressure, never
    as permission.
    """
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        n = 0.0
    if not n >= 0:  # NaN and negatives both land here
        return 0.0
    return round(n if n <= _DISK_FLOOR_MAX_GB else float(_DISK_FLOOR_MAX_GB), 3)


def _sanitize_disk_floor(raw: Any) -> Optional[Dict[str, Any]]:
    """MAC.1.s13b — normalise the worker's pre-claim free-disk gate reading.

    diskFloorGate.Report() in cmd/worker/diskfloor.go sends exactly
    {state, free_gb, needed_gb, work_root} once its first poll round has run
    (heartbeat.go omits the key until then, so the manager never reads "the
    volume is fine" for a worker that has not measured). This is untrusted
    worker input, so only those four keys survive: `state` against the Go
    stateName allow-list (an invented spelling is recorded as "unknown", the
    same fail-open state the gate itself uses when statfs fails), both GB
    figures non-negative floats clamped to the worker.claim_min_free_gb
    registry domain (0-100000), and work_root truncated — it is a host path,
    so a spoofed worker cannot stuff arbitrary text into the worker JSONB.
    Garbage returns None: the heartbeat never fails because of a bad reading,
    it just records none, and the previous good reading carries forward.
    """
    if not isinstance(raw, dict):
        return None
    # Nothing recognisable at all = no probe to record (an empty payload must
    # not mint a phantom "above_floor" that green-lights a dead worker).
    if not any(k in raw for k in ("state", "free_gb", "needed_gb", "work_root")):
        return None
    _state = raw.get("state")
    _state = str(_state).strip().lower() if _state is not None else ""
    if _state not in _DISK_FLOOR_STATES:
        # A worker that invents a state is still reporting a gate; keep the
        # reading but never store the unrecognised word.
        _state = "unknown"
    out: Dict[str, Any] = {"state": _state}
    for _key in ("free_gb", "needed_gb"):
        _v = raw.get(_key)
        if _v is not None:
            out[_key] = _disk_floor_gb(_v)
    _root = raw.get("work_root")
    if _root is not None:
        _root = str(_root).strip()
        if _root:
            out["work_root"] = _root[:_DISK_FLOOR_ROOT_CHARS]
    return out


@router.post("/workers/heartbeat", response_model=WorkerHeartbeatResponse, response_model_exclude_unset=True)
async def worker_heartbeat(
    request: WorkerHeartbeatRequest,
    http_request: Request,
    db: Session = Depends(get_db)
):
    """
    Receive hardware heartbeat from workers.

    S3: Rate-limited to 30/min per IP to prevent abuse.
    """
    # B87 (GSEC Q111 false positive) — the heartbeat's server-side receipt time
    # is captured FIRST, before any per-request work (client-IP resolution, the
    # rate limiter, the worker lookup, bootstrap-token verify), so the pair
    # persisted for clock-skew measurement is (worker send stamp, API receipt
    # instant) and never includes this handler's own processing latency.
    # Kept naive-UTC (datetime.utcnow) to match every other use of `now` in
    # this handler; only the capture POINT moves, not the representation.
    _hb_received_at = datetime.utcnow()

    # S3: Rate limit heartbeat
    from utils.rate_limiter import check_rate_limit
    # 1675b — CE-safe resolver (services/client_ip.py is denied in CE; a bare
    # import there 402'd the whole heartbeat via main.py's edition handler).
    client_ip = _resolve_heartbeat_client_ip(http_request)
    check_rate_limit(f"heartbeat:{client_ip}", max_requests=30, window_seconds=60)

    now = _hb_received_at

    # Find worker record
    worker = worker_management_crud.get_worker_by_name(db, request.worker_name)

    # Authenticate the heartbeat. Every enrolled worker carries a bootstrap
    # token (remote workers via the enrollment flow; the embedded worker via the
    # API startup seed, keyed on the shared BOOTSTRAP_TOKEN); the worker sends it
    # as `X-Bootstrap-Token` and the server verifies it against
    # WorkerEnrollment.bootstrap_token_hash. Enforced regardless of source IP:
    # the API is only reachable through nginx, whose proxy IP (172.x) would read
    # as "internal", so network position is NOT a credential. Without this,
    # anyone who could reach the API could spoof any worker's heartbeat (report
    # fake hardware, keep a dead worker "alive").
    enroll_rec = worker_management_crud.get_enrollment_by_name(db, request.worker_name)
    if enroll_rec is not None and enroll_rec.bootstrap_token_hash:
        presented = http_request.headers.get("X-Bootstrap-Token", "")
        # rc20d: bcrypt.checkpw is ~0.3 s of CPU; inline it blocks the whole
        # uvicorn event loop on every beat, so hand the verify to a thread.
        if not presented or not await asyncio.to_thread(
                verify_api_key, presented, enroll_rec.bootstrap_token_hash):
            logger.warning(
                "Rejected heartbeat for worker '%s' from %s: invalid or missing "
                "bootstrap token", request.worker_name, client_ip,
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing worker bootstrap token",
            )

    # Build hardware specs from reported data
    hardware_specs = {
        "ram_gb": request.ram_gb,
        "cpu_cores": request.cpu_cores,
        "gpu_type": request.gpu_type or ("CUDA" if request.has_gpu else "none"),
        "has_gpu": request.has_gpu,
        "hardware_tags": request.hardware_tags or [],
    }

    if request.has_gpu and request.gpu_vram_mb:
        hardware_specs["vram_gb"] = request.gpu_vram_mb // 1024
        hardware_specs["gpu_name"] = request.gpu_name
    # F14b — canonical vendor and total accelerator count, written on every
    # heartbeat so the manager can bucket workers by vendor and report
    # aggregate GPU counts without re-enumerating. Older workers omit these
    # keys; the API merge leaves the previous reading alone (same as cleanup
    # / last_cleanup contract).
    if request.gpu_vendor is not None:
        hardware_specs["gpu_vendor"] = request.gpu_vendor
    if request.gpu_count is not None:
        hardware_specs["gpu_count"] = request.gpu_count

    # Per-device accelerator list — the legacy single-GPU summary above
    # (gpu_type / vram_gb / gpu_name) comes from the worker's detectGPU
    # "primary" path and collapses multi-GPU or multi-backend boxes into
    # a single row. The `accelerators` list (populated by
    # EnumerateAccelerators on the worker) keeps per-device detail:
    # kind (cuda/rocm/intel/npu), index, name, VRAM GB. Downstream
    # views (Platform Capacity Overview, billing) read this instead of
    # the legacy fields so they can sum VRAM across every device and
    # break totals out per backend.
    if request.accelerators:
        hardware_specs["accelerators"] = request.accelerators
    # Stored on the worker row rather than a table of its own: it is a live
    # sample that is worthless the moment it is stale, exactly like disk_free_gb
    # and gpu_temperature_c beside it. Written only when the worker answered,
    # so an older binary leaves the previous value alone instead of erasing it.
    if request.model_downloads is not None:
        hardware_specs["model_downloads"] = request.model_downloads
    if request.cached_models is not None:
        hardware_specs["cached_models"] = request.cached_models
    if request.file_secrets is not None:
        hardware_specs["file_secrets"] = request.file_secrets
    if request.secret_refresh is not None:
        hardware_specs["secret_refresh"] = request.secret_refresh
    if request.host_exec is not None:
        hardware_specs["host_exec"] = request.host_exec
    if request.cleanup is not None:
        hardware_specs["cleanup"] = request.cleanup
    # 25.5.s5 — the sibling `last_cleanup` key: what the LAST pass DID
    # (cleanupPassSummary() in cleanup_bridge.go), vs. `cleanup`'s "what could
    # go". Sanitized rather than stored verbatim like `cleanup` above because
    # its failures list carries free-text docker error strings; the bounds
    # mirror the Go side (resultFailureLimit=25, kind allow-list from
    # noteFailure's call sites in executor.go). Absent (older binary / a
    # pass-less beat) carries the previous reading forward EXACTLY like
    # `cleanup`, and garbage returns None so the heartbeat never fails.
    _lc_new = _sanitize_last_cleanup(request.last_cleanup) if request.last_cleanup is not None else None
    if _lc_new is not None:
        hardware_specs["last_cleanup"] = _lc_new
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "last_cleanup" in worker.hardware_specs:
        # Absent (older binary / pass-less beat) OR unusable garbage: keep the
        # previous reading, same as noah_handback's `_hb is None` arm above —
        # a bad summary must never erase a good one, and must never fail the
        # beat.
        hardware_specs["last_cleanup"] = worker.hardware_specs["last_cleanup"]
    # MAC.1.s13b — the sibling `disk_floor` key: the worker's pre-claim free-disk
    # gate reading (diskFloorGate.Report() in cmd/worker/diskfloor.go), {state,
    # free_gb, needed_gb, work_root}. Sanitized rather than stored verbatim like
    # `cleanup` above because state/work_root are free-form worker strings and
    # the GB figures are untrusted numbers; the bounds mirror the Go side
    # (stateName's three spellings, the registry's 0-100000 GB domain for
    # worker.claim_min_free_gb). Absent (older binary / the first poll round
    # before any probe) carries the previous reading forward EXACTLY like
    # `last_cleanup`, and garbage returns None so the heartbeat never fails.
    _df_new = _sanitize_disk_floor(request.disk_floor) if request.disk_floor is not None else None
    if _df_new is not None:
        hardware_specs["disk_floor"] = _df_new
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "disk_floor" in worker.hardware_specs:
        # Absent OR unusable garbage: keep the previous reading — a bad reading
        # must never erase a good one, and must never fail the beat.
        hardware_specs["disk_floor"] = worker.hardware_specs["disk_floor"]
    # 24.5.s3 — NOAH drain/hand-back report {at, tasks_requeued}: the moment
    # lending stopped and how many harvest tasks the worker had to give back.
    # Stored verbatim on hardware_specs like `cleanup` above: a live sample,
    # worthless when stale, and the worker omits the key until it actually
    # hands work back (heartbeat.go), so an absent report leaves the previous
    # reading alone rather than erasing it.
    #
    # Deduped by `at`: preempt.go stamps drainAt on the FIRST hand-back of a
    # drain and keeps re-reporting the SAME `at` (with a growing count) on
    # every beat until the drain finishes, so "same at" = the same event, not
    # a new one. The first beat for a given `at` is what gets an audit row;
    # a later beat for the same `at` only refreshes the count in place.
    _noah_handback_new = False
    if request.noah_handback is not None:
        _hb = _sanitize_noah_handback(request.noah_handback)
        if _hb is not None:
            hardware_specs["noah_handback"] = _hb
            _prev_hb = (worker.hardware_specs or {}).get("noah_handback") \
                if (worker and isinstance(worker.hardware_specs, dict)) else None
            _noah_handback_new = not (
                isinstance(_prev_hb, dict)
                and _prev_hb.get("at") == _hb["at"]
            )
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "noah_handback" in worker.hardware_specs:
        hardware_specs["noah_handback"] = worker.hardware_specs["noah_handback"]
    # GY.C2b — the worker's FIPS 140-3 crypto mode ({go_version,
    # fips140_mode, fips140_enabled, boringcrypto}). Stored verbatim on
    # hardware_specs exactly like agent_version: reported data, no second
    # table for it. crypto_status.worker_crypto_rows reads
    # hardware_specs["crypto"]. None (older binary) leaves the key out of
    # the rebuilt dict, so such a worker falls back to "unknown" — the
    # binary that was reporting it is gone, so "cannot say" is right.
    if request.crypto is not None:
        hardware_specs["crypto"] = request.crypto
    # GSEC FX.123 s3 — the worker's full-disk-encryption (FDE) status. Stored
    # verbatim on hardware_specs like every other reported field; the claim
    # guard in worker_tasks.py reads hardware_specs['disk_encryption']['status']
    # to refuse tenant tasks on MOD-hosted workers with unencrypted disks
    # (gated by the security.require_fde_for_customer_tasks platform setting).
    # Untrusted input: whitelist the three known statuses, coerce, bound lengths.
    # ABSENT carries FORWARD the previous stored value (like host_bound_ports
    # above, NOT like crypto): absent = "this beat could not say" (older binary
    # / probe failed), and clearing it would flap the claim gate on a worker
    # that really does have an encrypted disk. An explicit report always replaces.
    if request.disk_encryption is not None:
        _de_in = request.disk_encryption
        _de_status = str(_de_in.get("status") or "").strip().lower()
        if _de_status not in ("encrypted", "unencrypted", "unknown"):
            _de_status = "unknown"
        _de: Dict[str, Any] = {"status": _de_status}
        _de_method = _de_in.get("method")
        if isinstance(_de_method, str) and _de_method:
            _de["method"] = _de_method.strip().lower()[:32]
        hardware_specs["disk_encryption"] = _de
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "disk_encryption" in worker.hardware_specs:
        hardware_specs["disk_encryption"] = worker.hardware_specs["disk_encryption"]
    # MAC.1.s12 — the native backend's honest confinement posture
    # (CORE/Worker/internal/heartbeat/native_confinement.go):
    # {"os": <worker OS>, "layer": "pdeathsig"|"unsupported"}. Stored on
    # hardware_specs exactly like disk_encryption above: report-only, and the
    # `layer` value is the ONLY field a manager may gate on — "runs as another
    # uid" (worker.native_darwin_uid / worker.native_drop_uid) is NOT the claim
    # "confined". Untrusted input: whitelist the two known layers, coerce, bound
    # lengths. ABSENT carries FORWARD the previous stored value, same rule as
    # disk_encryption: absent = "this beat could not say" (older binary), and
    # clearing it would flap a posture gate on a worker that really is confined.
    if request.native_confinement is not None:
        _nc_in = request.native_confinement
        _nc_layer = str(_nc_in.get("layer") or "").strip().lower()
        if _nc_layer not in ("pdeathsig", "unsupported"):
            _nc_layer = "unsupported"
        _nc: Dict[str, Any] = {"layer": _nc_layer}
        _nc_os = _nc_in.get("os")
        if isinstance(_nc_os, str) and _nc_os:
            _nc["os"] = _nc_os.strip().lower()[:32]
        hardware_specs["native_confinement"] = _nc
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "native_confinement" in worker.hardware_specs:
        hardware_specs["native_confinement"] = worker.hardware_specs["native_confinement"]
    # gw-stack-networking GW.2c — host ports the worker is holding open.
    # hardware_specs is REBUILT every beat and the assignment below replaces
    # the dict wholesale, so an ABSENT field must CARRY FORWARD the previous
    # stored value: absent = "this beat could not say" (older binary, or the
    # daemon query failed), NOT "nothing bound". A one-off daemon hiccup must
    # not clear the manager's last reading of every port on the host —
    # placement would otherwise free ports that are actually taken. (This is
    # the OPPOSITE of wslc, where absent authoritatively removes the key.)
    # {} (an empty report) still replaces the stored value — the worker
    # answered and said it binds nothing.
    if request.host_bound_ports is not None:
        hardware_specs["host_bound_ports"] = _sanitize_host_bound_ports(
            request.host_bound_ports)
        # GW.2 fix (stale reading) — stamp the capture time of this answer.
        # An explicit {} still REPLACES the stored value (see the sanitiser
        # below); `host_bound_ports_at` exists so placement can IGNORE a
        # reading that has gone stale — the last-carry-forward below keeps
        # an absent field alive indefinitely, which is right for a daemon
        # hiccup but wrong for a worker that stopped answering the field
        # altogether (older binary, provider gone). Older API rows that
        # never carried the stamp are grandfathered (never stale).
        hardware_specs["host_bound_ports_at"] = now.isoformat()
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "host_bound_ports" in worker.hardware_specs:
        _prev_hbp = worker.hardware_specs["host_bound_ports"]
        hardware_specs["host_bound_ports"] = {
            proto: list(ports) for proto, ports in _prev_hbp.items()
            if isinstance(ports, list)
        } if isinstance(_prev_hbp, dict) else _prev_hbp
        # Carry the capture stamp forward WITH the reading — the reading has
        # not changed, so its age has not been reset.
        if "host_bound_ports_at" in worker.hardware_specs:
            hardware_specs["host_bound_ports_at"] = \
                worker.hardware_specs["host_bound_ports_at"]
    if request.host_bound_ports_by_project is not None:
        hardware_specs["host_bound_ports_by_project"] = \
            _sanitize_host_bound_ports_by_project(
                request.host_bound_ports_by_project)
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "host_bound_ports_by_project" in worker.hardware_specs:
        _prev_hbpbp = worker.hardware_specs["host_bound_ports_by_project"]
        hardware_specs["host_bound_ports_by_project"] = \
            _sanitize_host_bound_ports_by_project(_prev_hbpbp)
    if request.arch:
        # Normalise ONCE at ingest so every downstream read (claim guard, worker
        # detail, capacity views) sees the canonical GOARCH label. Previously the
        # raw `uname -m` spelling was stored, so an "x86_64"/"aarch64" worker was
        # gated right (the claim guard re-aliased) but DISPLAYED unnormalised.
        hardware_specs["arch"] = _wm_normalize_arch(request.arch)
    if request.os:
        hardware_specs["os"] = request.os.strip().lower()
    if request.native_apps is not None:
        hardware_specs["native_apps"] = request.native_apps
    # wslc — Windows-only capability probe. Absent (None) => key NOT stored:
    # the rebuilt hardware_specs below is authoritative, so a worker that stops
    # sending it (or never sent it) has the key removed on its next beat.
    # Untrusted input: whitelist the three known keys, coerce, bound lengths.
    if request.wslc is not None:
        _wslc: Dict[str, Any] = {"available": bool(request.wslc.get("available"))}
        _wsl_ver = request.wslc.get("wsl_version")
        if isinstance(_wsl_ver, str) and _wsl_ver:
            _wslc["wsl_version"] = _wsl_ver[:32]
        _wsl_reason = request.wslc.get("reason")
        if isinstance(_wsl_reason, str) and _wsl_reason:
            _wslc["reason"] = _wsl_reason[:256]
        hardware_specs["wslc"] = _wslc
    # Stored on hardware_specs rather than as a column: it is reported data
    # like every other field here, and it changes whenever the host moves
    # network — which for a laptop manager and its workers is often.
    # Stored on hardware_specs (a live sample, worthless when stale) exactly like
    # the single-address form. `advertise_address` is kept for back-compat.
    if request.advertise_address:
        hardware_specs["advertise_address"] = request.advertise_address.strip()
    if request.advertise_address_site:
        hardware_specs["advertise_address_site"] = request.advertise_address_site.strip()
    if request.advertise_address_cloud:
        hardware_specs["advertise_address_cloud"] = request.advertise_address_cloud.strip()
    # dual-anchor-mesh-federation §2.3 — heartbeat -> resolution hook. Stamp the
    # freshness of each anchor this heartbeat CONFIRMED reachable; an unconfirmed
    # anchor is deliberately left untouched so it ages out of preference. Stored
    # as naive-UTC isoformat to match `now` / `last_heartbeat` handling here.
    _prev_seen = hardware_specs.get("anchor_seen")
    anchor_seen = dict(_prev_seen) if isinstance(_prev_seen, dict) else {}
    _reach = request.anchor_reachable or {}
    for _anchor in ("site", "cloud"):
        if _reach.get(_anchor):
            anchor_seen[_anchor] = now.isoformat()
    if anchor_seen:
        hardware_specs["anchor_seen"] = anchor_seen
    # Site tailnet: record the worker's site address and decide whether its
    # site client needs a fresh key (see _mesh_site_directive).
    _site_report = _sanitize_site_mesh(request.site_mesh)
    if _site_report is not None:
        hardware_specs["site_mesh"] = _site_report
        if _site_report.get("state") == "Running" and _site_report.get("ip"):
            hardware_specs["advertise_address_site"] = _site_report["ip"]
    _prev_mint = (worker.hardware_specs or {}).get("site_mesh_key") if worker else None
    mesh_site, _mint_state = _mesh_site_directive(_site_report, _prev_mint, now, db=db)
    if _mint_state:
        hardware_specs["site_mesh_key"] = _mint_state
    if request.cloud_provider:
        hardware_specs["cloud_provider"] = request.cloud_provider
        hardware_specs["instance_type"] = request.instance_type

    # WS4.Q21 — fetch-loop-liveness. Stored on hardware_specs like every other
    # live sample here: worthless once stale, refreshed every heartbeat.
    # `fetch_loop_stalled` / `fetch_loop_stalled_at` are NOT set here — they
    # are written by the scheduler's worker-stall watchdog job
    # (detect_and_recover_stalled_fetch_loops), which is the only thing that
    # decides "stalled" (fresh age alone is not evidence of harm; see its
    # docstring). This block only records what the worker reported.
    if request.fetch_loop_age_s is not None:
        hardware_specs["fetch_loop_age_s"] = request.fetch_loop_age_s
    if request.last_fetch_at:
        hardware_specs["last_fetch_at"] = request.last_fetch_at
    # Preserve the watchdog's own verdict across this rebuild — hardware_specs
    # is fully replaced below (`worker.hardware_specs = hardware_specs`), and
    # this handler runs far more often (~15s) than the watchdog job that sets
    # these (services.scheduler's worker-stall watchdog), so without this a
    # stalled worker would flicker back to "not stalled" on every heartbeat in
    # between watchdog ticks — exactly the misleading green status this exists
    # to fix. Same pattern as site_mesh_key's _prev_mint above.
    if worker and worker.hardware_specs:
        for _k in ("fetch_loop_stalled", "fetch_loop_stalled_at", "fetch_loop_stalled_recovered_at"):
            if _k in worker.hardware_specs:
                hardware_specs[_k] = worker.hardware_specs[_k]

    # GF.C1b — "cache as a worker role": whether this worker is enabled as
    # its site's model cache is admin-set (services/model_cache_registry.py,
    # via the platform-admin enable/disable endpoint), not reported by the
    # worker itself. Preserve it across this hardware_specs rebuild exactly
    # like fetch_loop_stalled above, or the very next heartbeat (~15s later)
    # would silently revert the admin's choice.
    if worker and worker.hardware_specs:
        for _k in ("site_cache_enabled", "site_cache_enabled_at", "site_cache_disabled_at"):
            if _k in worker.hardware_specs:
                hardware_specs[_k] = worker.hardware_specs[_k]

    # GSEC FX.62 p3a1 — the worker's rolling self-update report
    # (CORE/Worker/internal/heartbeat/update_report.go). Same absent-carries-
    # forward discipline as disk_encryption / native_confinement above: an
    # OLDER binary (or a process that never ran a cycle) omits the key, and
    # clearing the stored failure on such a beat would make the Dashboard
    # toast lie — "no error reported" is not "the update worked".
    # An explicit report ALWAYS replaces, and the empty string "" is the
    # worker's success signal: it must survive as an explicit CLEAR, so the
    # test is `is not None`, NOT truthiness. Bound the reason (untrusted
    # worker input) to the same 500-char ceiling every other free-text
    # heartbeat field uses.
    if request.last_update_error is not None:
        hardware_specs["last_update_error"] = request.last_update_error[:500]
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "last_update_error" in worker.hardware_specs:
        hardware_specs["last_update_error"] = worker.hardware_specs["last_update_error"]

    # BUGFIX (worker-289ba8a0, LANE-RESTORE 10-12): the loop-detection counter
    # lives in the hardware_specs JSONB, but this beat REPLACES the whole
    # column below (`worker.hardware_specs = hardware_specs` at :3268). The
    # rebuild never carried _hb_since_update_req over, so the loop-detection
    # block read `dict(worker.hardware_specs or {})` AFTER the replace — the
    # counter was already gone — and recomputed 0+1 = 1 on every beat. It
    # never reached 3, so update_requested on a worker that can never
    # self-update (e.g. "no release key compiled in: self-update disabled")
    # stayed stuck forever. Carry it over exactly like last_update_error
    # above: reported data owns the key when present, else preserve the
    # stored one so the counter advances across beats.
    if worker and isinstance(worker.hardware_specs, dict) \
            and "_hb_since_update_req" in worker.hardware_specs:
        hardware_specs["_hb_since_update_req"] = worker.hardware_specs["_hb_since_update_req"]

    # 1141 — the beat's own warning strings (heartbeat.go appendWarning). The
    # worker has always sent them; with no field on the request model Pydantic
    # dropped them at ingest, so a broken-NVML worker's "GPU telemetry returned
    # 0" warning never reached the API and the card stayed placeable. Absent
    # (older binary / a beat that never answered) carries the previous reading
    # forward EXACTLY like last_cleanup above; an explicit [] is the worker
    # saying "no warnings" and clears them. Free-text worker input: bounded
    # like every other string the heartbeat stores.
    if request.warnings is not None:
        hardware_specs["worker_warnings"] = [
            str(w)[:300] for w in request.warnings if isinstance(w, str)
        ][:25]
    elif worker and isinstance(worker.hardware_specs, dict) \
            and "worker_warnings" in worker.hardware_specs:
        hardware_specs["worker_warnings"] = worker.hardware_specs["worker_warnings"]

    # 1141 — a card that was benchmark-sized but now reports 0 VRAM is a
    # broken-NVML worker (driver/library mismatch, persistenced socket gone),
    # not a smaller card: crud/accelerator_cud.upsert_accelerators refuses to
    # overwrite the stored vram_gb with a 0, so the DB row kept a good size
    # while live telemetry says 0 and placement kept re-placing stacks onto
    # the dead card (the placement half of the stack-110 hot loop). Record the
    # disagreement on hardware_specs JSONB — the benchmark vram_gb stays the
    # source of truth and is NEVER overwritten from a zero reading. A later
    # healthy beat simply omits the key (hardware_specs is rebuilt from
    # scratch every beat), so the flag self-clears when NVML comes back.
    _zero_vram = bool(request.has_gpu) and (
        request.gpu_vram_mb == 0
        or ((request.resource_snapshot or {}).get("vram_total_mb") == 0)
    )
    if _zero_vram and worker and worker.hardware_profile_id:
        try:
            _rows = worker_management_crud.list_accelerator_profiles(
                db, worker.hardware_profile_id
            )
            # gpu_vram_mb/resource_snapshot describe the PRIMARY GPU only
            # (capabilities/detect.go: "report the primary, first found") —
            # on a mixed-kind box (e.g. one CUDA + one ROCm card) a dead
            # primary must not drag the other kind's healthy card down with
            # it. Scope "good" (still-sized) rows to the kind this beat is
            # actually reporting on; hardware_specs["gpu_type"] already
            # carries the same has_gpu-aware default used to STORE gpu_type
            # above, so the two stay in lockstep.
            _tele_kind = str(hardware_specs.get("gpu_type") or "").strip().lower()
            _rows_k = [(str(r.kind).strip().lower(), float(r.vram_gb or 0),
                        bool(getattr(r, "is_integrated", False)),
                        (r.benchmark_results
                         if isinstance(getattr(r, "benchmark_results", None), dict) else {}),
                        str(getattr(r, "name", None) or ""))
                       for r in _rows]
            # A 0 reading is only evidence against the sized card when nothing
            # else of that kind could have produced it. An integrated GPU (the
            # Tiger Lake iGPU on the Arc B60 NUC lane) or any never-sized row
            # reports 0 device memory by design — shared-memory devices have no
            # private VRAM, and the benchmark binary measures 0 on them even
            # when the worker's own level-zero read invents a shared-heap
            # number for vram_gb — so such a row explains the zero and must
            # never veto the sized DISCRETE card of the same kind sitting
            # beside it. The guarded case (the only/sized card itself going
            # dark) has no explaining row, so it still flags.
            # A unified-memory NVIDIA device (DGX Spark / GX10, Grace) has no
            # private framebuffer at all: NVML answers "Not Supported" for
            # memory.total, so the worker reads 0 VRAM every beat while the
            # card works and the benchmark row still says 122 GB. That zero is
            # by design, not a dead driver — the row explains it, exactly like
            # an integrated GPU does, and must never quarantine the card.
            from utils.accelerator_fit import is_unified_memory_device
            _zero_explained = any(k == _tele_kind and (integ or v <= 0
                                                       or is_unified_memory_device(nm, k)
                                                       or (bench.get("vram_gb") is not None
                                                           and float(bench["vram_gb"] or 0) <= 0))
                                  for k, v, integ, bench, nm in _rows_k)
            _good = [(k, v) for k, v, integ, bench, nm in _rows_k
                     if v > 0 and k == _tele_kind and not is_unified_memory_device(nm, k)]
            if _good and not _zero_explained:
                hardware_specs["gpu_telemetry_unhealthy"] = True
                hardware_specs["gpu_telemetry_unhealthy_kinds"] = sorted({k for k, _ in _good})
                hardware_specs["gpu_telemetry_unhealthy_reason"] = (
                    "GPU telemetry reported 0 VRAM while the benchmark-sized card "
                    f"says {max(v for _, v in _good):.0f} GB"
                )
        except Exception as e:
            logger.warning("GPU-telemetry health check failed for %s: %s", request.worker_name, e)

    # Health monitoring fields (remote workers)
    if request.disk_free_gb is not None:
        hardware_specs["disk_free_gb"] = request.disk_free_gb
    if request.disk_total_gb is not None:
        hardware_specs["disk_total_gb"] = request.disk_total_gb
    if request.images_bytes is not None:
        hardware_specs["images_bytes"] = request.images_bytes
    if request.model_cache_bytes is not None:
        hardware_specs["model_cache_bytes"] = request.model_cache_bytes
    if request.workdirs_bytes is not None:
        hardware_specs["workdirs_bytes"] = request.workdirs_bytes
    if request.reclaimable_bytes is not None:
        hardware_specs["reclaimable_bytes"] = request.reclaimable_bytes
    if request.docker_healthy is not None:
        hardware_specs["docker_healthy"] = request.docker_healthy
    if request.gpu_temperature_c is not None:
        hardware_specs["gpu_temperature_c"] = request.gpu_temperature_c
    if request.gpu_memory_used_pct is not None:
        hardware_specs["gpu_memory_used_pct"] = request.gpu_memory_used_pct
    if request.active_containers is not None:
        hardware_specs["active_containers"] = request.active_containers
    if request.uptime_hours is not None:
        hardware_specs["uptime_hours"] = request.uptime_hours
    if request.agent_version:
        hardware_specs["agent_version"] = request.agent_version
    if request.boot_duration_seconds is not None:
        hardware_specs["boot_duration_seconds"] = request.boot_duration_seconds
    if request.cached_images is not None:
        hardware_specs["cached_images"] = request.cached_images
    if request.minio_latency_ms is not None or request.minio_throughput_mbps is not None or request.api_latency_ms is not None:
        hardware_specs["network"] = {
            **(hardware_specs.get("network", {})),
            **({"minio_latency_ms": request.minio_latency_ms} if request.minio_latency_ms is not None else {}),
            **({"minio_throughput_mbps": request.minio_throughput_mbps} if request.minio_throughput_mbps is not None else {}),
            **({"api_latency_ms": request.api_latency_ms} if request.api_latency_ms is not None else {}),
        }

    # GY.C5 time-sync follow-up — persist the worker's OWN heartbeat stamp so
    # services/time_sync_status.py:worker_skew_rows() can compute
    # skew = server_received_at (last_heartbeat) - worker_sent_at (hb_sent_at).
    # The Go worker already sends RFC3339 `timestamp`; the API discarded it.
    # Parse defensively: absent/garbage => key NOT written (skew stays
    # "not reported"), and the heartbeat NEVER fails because of a bad stamp.
    # Stored tz-aware (UTC offset kept) because worker_skew_rows subtracts
    # `received - sent_dt` and `received` comes back tz-aware from the
    # DateTime(timezone=True) last_heartbeat column.
    if request.timestamp:
        try:
            _hb_sent = datetime.fromisoformat(
                str(request.timestamp).strip().replace("Z", "+00:00"))
            if _hb_sent.tzinfo is None:
                _hb_sent = _hb_sent.replace(tzinfo=timezone.utc)
            hardware_specs["hb_sent_at"] = _hb_sent.astimezone(
                timezone.utc).isoformat()
        except Exception:
            logger.warning(
                "heartbeat timestamp %r unparseable for worker %s — skew stays "
                "not reported", request.timestamp, request.worker_name)
        except Exception:
            logger.warning(
                "heartbeat timestamp %r unparseable for worker %s — skew stays "
                "not reported", request.timestamp, request.worker_name)

    # Parse queues if provided
    queues = None
    if request.queues:
        queues = [q.strip() for q in request.queues.split(",") if q.strip()]

    _newly_registered = worker is None

    if worker:
        # Update existing worker
        worker.last_heartbeat = now
        worker.last_seen_at = now
        worker.hardware_specs = hardware_specs

        # Item #4 (fullproof extension) — extend claim_expires_at for
        # any RUNNING task owned by this worker. Without this, long
        # test_and_repair stages (15+ min for CUDA + nerfstudio-class
        # libraries) lose their claim mid-build and get re-claimed by
        # another instance — which triggers the test_and_repair stage
        # restart cycle that wipes repair_history. The active-
        # continuation guard in reset-repair-budget handled the
        # symptom; this prevents the cause.
        try:
            from datetime import timedelta
            # Column is `claimed_by_worker` (worker_name string),
            # not `assigned_worker_id`. Match against this worker's
            # name to find its in-flight claims.
            running_tasks = worker_management_crud.list_worker_claimed_tasks(
                db, worker.worker_name,
                (ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED, ApiTaskStatus.ASSIGNED),
            )
            # Renewal keys on EXECUTION, not ownership.
            #
            # It used to renew every task this worker had claimed, which made a
            # lost claim immortal: a task handed out and never received was
            # renewed for ever by the claimant's own heartbeat, so
            # claim_expires_at was always in the future, the reaper could never
            # fire, and the work sat wedged until a human cancelled it. Forcing
            # the column into the past just moved it 15 minutes forward again.
            # (Roadmap: "A claim lost in transit is immortal", 2026-08-07.)
            #
            # None means the worker cannot say — renew everything, exactly as
            # before, so a rolling upgrade does not expire the fleet's claims.
            reported = request.active_task_ids
            executing = set(reported) if reported is not None else None

            # A task claimed moments ago may not be in the report yet. Without
            # this grace a claim could die in the window between claiming and
            # starting, which would turn a fix for stuck work into a source of
            # duplicated work.
            claim_grace = now - timedelta(seconds=120)

            for t in running_tasks:
                if executing is not None and t.id not in executing:
                    # `now`/`claim_grace` are naive-UTC by design here (see
                    # anchor_seen / last_heartbeat above), but claimed_at comes
                    # back tz-aware from the DateTime(timezone=True) column.
                    # Normalize to naive-UTC before comparing, else the whole
                    # renewal loop aborts with a naive/aware TypeError.
                    _claimed = t.claimed_at
                    if _claimed is not None and _claimed.tzinfo is not None:
                        _claimed = _claimed.replace(tzinfo=None)
                    if _claimed and _claimed < claim_grace:
                        # Not being executed and not newly claimed: let the
                        # existing expiry run its course and the reaper requeue
                        # it. Deliberately not expired here — one mechanism for
                        # reclaiming work, not two.
                        continue
                # Renew to NOW + 15 min on each heartbeat. Heartbeats
                # come every ~5s, so renewal happens continuously;
                # a task only loses its claim if heartbeats stop for
                # > 15 minutes, matching the stale-claim-sweep's
                # threshold.
                t.claim_expires_at = now + timedelta(minutes=15)
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat claim renewal failed — non-fatal")
        if queues:
            worker.queues = queues
        # BUGFIX (worker-289ba8a0, LANE-RESTORE 10-12): a worker with no
        # release key compiled in can NEVER self-update, so waiting for a
        # version change (or burning 3 heartbeats in loop detection) is
        # pointless — the operator's Update flag would sit stuck. If the
        # heartbeat itself says self-update is impossible, clear the flag
        # immediately and tell the operator why.
        _lue = request.last_update_error
        if (
            getattr(worker, "update_requested", False)
            and _lue
            and "self-update disabled" in _lue
        ):
            worker.update_requested = False
            _specs = dict(worker.hardware_specs or {})
            _specs.pop("_hb_since_update_req", None)
            worker.hardware_specs = _specs
            logger.warning(
                "Cleared stuck update_requested on %s: heartbeat reports "
                "self-update is impossible (%s). The worker needs a binary "
                "with a release key compiled in — cut one via "
                "build the worker with your own release key (see the worker README).",
                worker.worker_name,
                _lue[:200],
            )
        if request.agent_version:
            # Auto-clear update_requested when the worker comes back on a new
            # version. This completes the one-shot Update cycle: operator
            # clicked Update → worker drained + self-updated + restarted →
            # heartbeat here with the new version → flag clears → worker
            # resumes claiming on next poll. No operator action needed.
            if (
                getattr(worker, "update_requested", False)
                and worker.agent_version
                and worker.agent_version != request.agent_version
            ):
                worker.update_requested = False
                # Clear loop-detection counter and any prior error on success.
                if worker.hardware_specs:
                    specs = dict(worker.hardware_specs)
                    specs.pop("_hb_since_update_req", None)
                    specs.pop("last_update_error", None)
                    worker.hardware_specs = specs
                logger.info(
                    "Cleared update_requested on %s (version %s -> %s)",
                    worker.worker_name, worker.agent_version, request.agent_version,
                )
            # Loop detection: if the worker is mid-update but heartbeats back
            # with the SAME version, it downloaded and restarted but nothing
            # changed. Without this guard the drain flag would never clear
            # and the worker would download the binary on every cycle
            # forever. This happens when the MinIO binary has the same
            # baked-in version as what's already running — typically
            # because no new release.sh has been cut.
            elif (
                getattr(worker, "update_requested", False)
                and worker.agent_version
                and worker.agent_version == request.agent_version
            ):
                # Counter lives in hardware_specs JSONB so we don't need a
                # migration for loop detection. Resets implicitly when
                # update_requested flips off.
                existing_specs = dict(worker.hardware_specs or {})
                hb_since_req = int(existing_specs.get("_hb_since_update_req") or 0) + 1
                # 3 heartbeats = ~45s — enough for a legitimate self-update
                # restart to complete and show a new version.
                if hb_since_req >= 3:
                    worker.update_requested = False
                    logger.error(
                        "Update loop detected on %s: version %s unchanged after "
                        "%d heartbeats. Clearing update_requested. Likely cause: "
                        "the binary in MinIO worker-releases/ has the same baked-in "
                        "version as what the worker is already running — cut a new "
                        "release with your own release key (see the worker README) before retrying.",
                        worker.worker_name, worker.agent_version, hb_since_req,
                    )
                    # Stash a sentinel on hardware_specs so the Dashboard can
                    # surface a human-readable error in the row tooltip and the
                    # completion toast already wired up picks it up. JSONB
                    # merge — don't overwrite existing hardware_specs fields.
                    existing_specs["last_update_error"] = (
                        f"Update cycle ended with no version change ({worker.agent_version}). "
                        "The binary in MinIO has the same version as the running worker."
                    )
                    # Reset the counter since we gave up on this cycle.
                    existing_specs.pop("_hb_since_update_req", None)
                else:
                    existing_specs["_hb_since_update_req"] = hb_since_req
                worker.hardware_specs = existing_specs
            worker.agent_version = request.agent_version
        if request.network_zone:
            worker.network_zone = request.network_zone

        # If worker was offline, restore it to its last known licensed state
        if worker.status == WorkerStatus.OFFLINE:
            if worker.licensed_at and not worker.revoked_at:
                worker.status = WorkerStatus.LICENSED
                resume_worker(worker.worker_name, queues)
            else:
                worker.status = WorkerStatus.UNLICENSED

        # Health alerts for remote workers
        if request.disk_free_gb is not None and request.disk_free_gb < 10:
            logger.warning(f"ALERT: Worker {request.worker_name} low disk space: {request.disk_free_gb:.1f} GB free")
        if request.docker_healthy is False:
            logger.warning(f"ALERT: Worker {request.worker_name} reports Docker daemon unhealthy")
        if request.gpu_temperature_c is not None and request.gpu_temperature_c > 90:
            logger.warning(f"ALERT: Worker {request.worker_name} GPU thermal throttling: {request.gpu_temperature_c}°C")

        logger.info(f"Heartbeat received from worker: {request.worker_name} (Status: {worker.status.value})")
    else:
        # Only auto-create for enrolled workers or embedded workers (on Docker network)
        # Remote workers must go through enrollment first
        enrollment = worker_management_crud.get_enrollment_by_name(db, request.worker_name)

        is_internal = client_ip.startswith("172.") or client_ip.startswith("10.") or client_ip == "127.0.0.1"

        if not enrollment and not is_internal:
            logger.warning(f"Heartbeat from unknown remote worker '{request.worker_name}' — ignoring (not enrolled)")
            # 1675b — the 1675 fail-open hole was this branch AUTO-CREATING an
            # APPROVED enrollment + license row for any caller. It never does:
            # the only rows that ever exist are the ones the startup seed
            # provisioned from the Vault token (main.py) or the enrollment
            # flow. The refusal stays a 200-with-message (not a 402 paywall)
            # because existing heartbeat tests pin that shape; nothing is
            # written, so the caller is refused in effect.
            return {"status": "ok", "message": "Worker not enrolled. Use Add Worker to create an enrollment token."}

        # 1675b — trust the embedded worker AND its CE equivalent, nothing else.
        # Both names come from main._EMBEDDED_WORKER_SEED_NAMES: the ONLY two
        # worker_name values the startup seed ever hashes the shared Vault
        # bootstrap token onto. Before this fix, is_embedded was keyed to the
        # literal "mod-worker-embedded" only, so a CE worker's first beat
        # landed its WorkerLicense row as UNLICENSED and poll_for_task
        # (worker_tasks.py:947, `worker.status != WorkerStatus.LICENSED`)
        # permanently 403'd it — enrolled, but unable to ever claim a task.
        # Lazy import avoids a main<->worker_management circular import at
        # module load (same pattern as worker_tasks.py's `from main import
        # get_overlap_scheduler`); the except fallback keeps this fail-closed
        # (embedded-only) rather than crashing the heartbeat if main.py's
        # constant is ever renamed.
        try:
            from main import _EMBEDDED_WORKER_SEED_NAMES as _trusted_seed_names
        except Exception:  # noqa: BLE001 — never let an import hiccup 500 a heartbeat
            _trusted_seed_names = ("mod-worker-embedded",)

        # Only auto-create the embedded/CE worker, not any other internal IP
        if not enrollment and is_internal and request.worker_name not in _trusted_seed_names:
            logger.warning(f"Heartbeat from unknown internal worker '{request.worker_name}' — ignoring (not embedded worker)")
            return {"status": "ok", "message": "Worker not enrolled."}

        # Embedded/CE worker is always auto-licensed — system needs at least one worker
        is_embedded = is_internal and request.worker_name in _trusted_seed_names
        # BUG-1029-B1 — `worker_licenses.worker_name` is PLAIN unique (FK target),
        # so a decommissioned worker's tombstone still OWNS the name: a plain
        # INSERT here 500s on IntegrityError. Reactivate the tombstone instead
        # (clears deleted_at/deleted_by_user_id + stamps the fields below), and
        # only fall through to the INSERT when no tombstone exists. LIVE rows
        # never reach this branch (the lookup above already resolved them), so
        # existing duplicate-name handling is untouched.
        worker = worker_management_crud.reactivate_worker_license(
            db,
            request.worker_name,
            hostname=request.hostname,
            status=WorkerStatus.LICENSED if is_embedded else WorkerStatus.UNLICENSED,
            licensed_at=now if is_embedded else None,
            first_seen_at=now,
            last_seen_at=now,
            last_heartbeat=now,
            agent_version=request.agent_version,
            network_zone=request.network_zone or "local",
            is_remote=not is_internal,
            owner_type=WorkerOwnerType.PLATFORM_POOL if is_embedded else None,
            hardware_specs=hardware_specs,
            queues=queues,
        )
        if worker is None:
            worker = WorkerLicense(
                worker_name=request.worker_name,
                hostname=request.hostname,
                hardware_specs=hardware_specs,
                queues=queues,
                status=WorkerStatus.LICENSED if is_embedded else WorkerStatus.UNLICENSED,
                licensed_at=now if is_embedded else None,
                first_seen_at=now,
                last_seen_at=now,
                last_heartbeat=now,
                agent_version=request.agent_version,
                network_zone=request.network_zone or "local",
                is_remote=not is_internal,
                owner_type=WorkerOwnerType.PLATFORM_POOL if is_embedded else None,
            )
            db.add(worker)
        logger.info(f"New worker registered: {request.worker_name} ({'embedded/auto-licensed' if is_embedded else 'enrolled remote'})")

    db.commit()

    # GSEC Q21.4 — bring the new worker's per-worker Vault AppRole up at
    # enrollment (mode != off). Runs AFTER the commit so the Vault calls
    # never ride the heartbeat transaction; a failure is logged, never
    # fatal (shared-role fallback until the Q21.12 cutover).
    if _newly_registered and worker is not None:
        _ensure_worker_approle_on_enrollment(worker, db)

    # NOAH opportunistic-harvest: persist reported availability (for the
    # scheduler timeline + admin visibility) and, when the worker reports itself
    # unavailable, release its never-started pre-placed tasks back to the pool
    # so they are merely delayed, not stranded on a machine now in use.
    if worker is not None and request.noah_available is not None:
        _noah = optional_paid("services.noah")
        if _noah is not None:
            # Decoupled into two independent transactions (noah-observability).
            # Previously a single try/commit wrapped BOTH the availability write and
            # the release call; a release error (e.g. the enum-case bug) rolled the
            # whole block back, discarding noah_available and freezing the UI on
            # "available/idle". The availability write is what the admin UI reads, so
            # it commits FIRST and on its own.
            prev_available = worker.noah_available
            transitioned = (prev_available is None
                            or bool(prev_available) != bool(request.noah_available))
            try:
                worker.noah_available = request.noah_available
                worker.noah_reason = (request.noah_reason or "")[:64]
                worker.noah_next_free_at = request.noah_next_free_at
                if transitioned:
                    worker.noah_available_since = datetime.now(timezone.utc)
                db.commit()
                if transitioned:
                    _noah.record_availability_transition(
                        worker.worker_name, bool(request.noah_available))
            except Exception:
                db.rollback()
                logger.exception("NOAH availability persist failed for %s", request.worker_name)

            # Release never-started pre-placed tasks in a SEPARATE guarded block so
            # a release failure can no longer roll back the availability write above.
            if not request.noah_available:
                try:
                    if _noah.is_noah_worker(db, worker):
                        released = _noah.release_never_started_assignments(db, worker.worker_name)
                        if released:
                            worker.noah_harvested_total = (worker.noah_harvested_total or 0) + released
                        db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("NOAH release failed for %s", request.worker_name)

    # 24.5.s3 — one audit row per hand-back EVENT, deduped by `at` (see the
    # ingest block above): the worker repeats the same `at` on every beat until
    # its drain clears, so without the dedupe the audit trail would grow ~1 row
    # per heartbeat for the life of a single drain. The audit tenant is the
    # worker's owner when it has one, else the platform tenant (1) — the
    # heartbeat has no user, exactly like the auto-queue emit above.
    if _noah_handback_new and worker is not None:
        try:
            from models import AuditEvent
            db.add(AuditEvent(
                tenant_id=(worker.owner_tenant_id
                           or (worker.assigned_tenant_ids or [None])[0]
                           or 1),
                user_id=None,
                event_name="NOAH_HANDBACK",
                details={
                    "worker_name": worker.worker_name,
                    "at": (hardware_specs.get("noah_handback") or {}).get("at"),
                    "tasks_requeued": (hardware_specs.get("noah_handback") or
                                       {}).get("tasks_requeued"),
                },
            ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("NOAH hand-back audit emit failed for %s",
                             request.worker_name)

    _match_hardware_profile(db, worker, hardware_specs, accelerators=request.accelerators)

    # wizard-rebuild §9 — drain matching pending variant-tests.
    # When a worker reports accelerator capabilities, any queued
    # sample-test rows whose variant matches one of those kinds get
    # claimed and surfaced for dispatch. The actual sample-test
    # task is enqueued via api_task_queue downstream of this hook
    # (kept out of the heartbeat critical path to avoid blowing
    # the heartbeat HTTP timeout when the queue is large).
    try:
        from node_builder.variant_test.queue import drain_for_worker
        kinds = ["cpu"]  # every worker has a CPU
        for a in (request.accelerators or []):
            k = (a.get("kind") or "").strip().lower() if isinstance(a, dict) else ""
            if k:
                kinds.append(k)
        if worker and worker.id:
            claimed = drain_for_worker(db, worker.id, kinds)
            if claimed:
                logger.info(
                    "heartbeat: drained %d pending variant-tests for "
                    "worker=%s kinds=%s",
                    len(claimed), worker.worker_name, kinds,
                )
    except Exception as _drain_exc:
        logger.warning(
            "heartbeat: variant-test queue drain failed (non-fatal): %s",
            _drain_exc,
        )

    # stack-ha-reconciliation §1 — record what this worker says it is running.
    # Non-fatal by design: observation is a diagnostic input, and a worker whose
    # heartbeat is rejected over it would stop reporting anything at all, which
    # is strictly worse than a stale observation.
    if worker:
        _so = optional_paid("services.stack_observation")
        if _so is not None:
            try:
                _so.record_observations(db, worker.worker_name, request.hosted_stacks)
                _so.record_fence(db, worker.worker_name, request.fence)
                # Commit our OWN work rather than relying on a later block's.
                # This originally left the session dirty for whatever committed
                # next, which happened to be the resource-snapshot block — so
                # observations persisted only for workers that also sent a
                # snapshot, and were silently discarded by that block's
                # `db.rollback()` whenever it failed. Fence reports, which arrive
                # exactly when a worker is in trouble, were the visible casualty.
                db.commit()
            except Exception as _obs_exc:  # noqa: BLE001
                # BUG-1028f — LOUD now, and only the exception CLASS name: the
                # live dev DB that lacked `persistent_stacks.keep_running`
                # logged this at warning once per heartbeat and nobody noticed
                # for 45 minutes, during which the reconciler demoted and
                # redeployed three stacks (killing every in-flight lane agent
                # when the gateway recreated). The class is enough to route the
                # fix (UndefinedColumn = migrations lag the code;
                # SQLAlchemyError/DatabaseCode = perms/DB state) and never
                # carries the SQL statement or its parameters.
                logger.error("heartbeat: stack observation ingest failed "
                             "(non-fatal): %s", type(_obs_exc).__name__)
        elif request.hosted_stacks is not None:
            # Worker-controlled stack start (MOD_STACK_WORKER_START=1, the CE
            # default). The WORKER owns restart policy and observation: only a
            # worker running the loop reports `down` per project (it has set its
            # stack containers to restart `no` itself). The API only decides
            # which restore to queue: one at a time per worker (files, then the
            # down services), a full redeploy when no containers exist.
            try:
                from routers.persistent_stacks._shared import stack_worker_start_enabled
                if stack_worker_start_enabled():
                    from services.stack_secret_recovery import (
                        redeploy_missing_stacks, restore_down_stacks)
                    restore_down_stacks(db, worker.worker_name, request.hosted_stacks)
                    redeploy_missing_stacks(db, worker.worker_name, request.hosted_stacks,
                                            stack_loop=request.stack_loop is True)
                    db.commit()
            except Exception as _loop_exc:  # noqa: BLE001
                db.rollback()
                logger.error("heartbeat: stack restore loop failed "
                             "(non-fatal): %s", type(_loop_exc).__name__)
            # Community Edition: the observation module is not shipped, so nothing
            # would act on a worker's `secrets_missing` flag (stack secret files
            # lost with /run tmpfs on a host reboot) and the stack would stay down.
            # This edition-neutral step queues the stack's own deploy, which carries
            # fresh secrets. A no-op unless the worker flagged a project.
            try:
                from services.stack_secret_recovery import recover_lost_secrets
                if recover_lost_secrets(db, worker.worker_name, request.hosted_stacks):
                    db.commit()
            except Exception as _rec_exc:  # noqa: BLE001
                db.rollback()
                logger.error("heartbeat: lost-secret recovery failed "
                             "(non-fatal): %s", type(_rec_exc).__name__)
                db.rollback()

    # GSEC FX.80 p3 — ingest the worker's cross-tenant GPU VRAM scrub records
    # (gpu_tenant_scrub.go, FX.80.p2) into the append-only audit_events trail.
    # One row per scrub, metadata-only, never mutated and never deleted (ISO
    # 27001 AU-2/AU-3): this is the EVIDENCE that a card was cleared before a
    # different tenant's job landed on it, so it has to survive in the trail
    # the compliance evidence bundle and the SIEM shipper already read — a
    # live hardware_specs sample could not answer "was this card ever handed
    # over without a scrub". Absent key = nothing scrubbed yet (gate off, or
    # no tenant switch), and nothing is written: "no data" is never read as
    # "scrubbing is on and found nothing", the same contract the worker ships.
    # Own commit + own rollback, exactly like the observation block above, so
    # a failed evidence write can never discard another block's work.
    if request.gpu_scrubs and worker:
        try:
            # optional_paid: the module is not in the CE manifest, so a
            # Community-Edition tree has no scrub evidence to record and the
            # beat must stay byte-identical there (same guard as the NOAH
            # block above).
            _scrub_ev = optional_paid("services.gpu_scrub_evidence")
            if _scrub_ev is not None:
                _scrub_recs = _scrub_ev._sanitize_scrub_records(request.gpu_scrubs)
                if _scrub_recs:
                    _scrub_written = _scrub_ev.record_gpu_scrubs(
                        db,
                        worker_name=worker.worker_name,
                        records=_scrub_recs,
                        tenant_id=getattr(worker, "owner_tenant_id", None))
                    db.commit()
                    if _scrub_written:
                        logger.info(
                            "heartbeat: recorded %d GPU VRAM scrub(s) for %s",
                            _scrub_written, worker.worker_name,
                        )
        except Exception as _scrub_exc:  # noqa: BLE001
            logger.warning("heartbeat: gpu scrub evidence ingest failed "
                           "(non-fatal): %s", _scrub_exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    # Store real-time resource snapshot for the overlap scheduler
    if request.resource_snapshot and worker:
        try:
            snap = request.resource_snapshot
            timeline = worker_management_crud.get_resource_timeline_by_worker(db, worker.worker_name)
            if not timeline:
                timeline = WorkerResourceTimeline(worker_name=worker.worker_name)
                db.add(timeline)

            timeline.timestamp = now
            timeline.cpu_percent = snap.get("cpu_percent")
            timeline.ram_used_mb = snap.get("ram_used_mb")
            timeline.ram_total_mb = snap.get("ram_total_mb")
            timeline.ram_available_mb = snap.get("ram_available_mb")
            timeline.vram_used_mb = snap.get("vram_used_mb")
            timeline.vram_total_mb = snap.get("vram_total_mb")
            timeline.vram_fragmentation_pct = snap.get("vram_fragmentation_pct")
            timeline.gpu_utilization_pct = snap.get("gpu_utilization_pct")
            timeline.gpu_temperature_c = snap.get("gpu_temperature_c")
            timeline.gpu_power_state = snap.get("gpu_power_state")
            timeline.nic_rx_mbps = snap.get("nic_rx_mbps")
            timeline.nic_tx_mbps = snap.get("nic_tx_mbps")
            timeline.nic_capacity_mbps = snap.get("nic_capacity_mbps")
            timeline.disk_read_iops = snap.get("disk_read_iops")
            timeline.disk_write_iops = snap.get("disk_write_iops")
            timeline.disk_free_gb = snap.get("disk_free_gb")
            timeline.active_containers = snap.get("active_containers")
            timeline.containers_starting = snap.get("containers_starting")
            timeline.cpu_temperature_c = snap.get("cpu_temperature_c")
            timeline.thermal_throttled = snap.get("thermal_throttled")
            timeline.task_snapshots = snap.get("per_task_resources")

            db.commit()
        except Exception as e:
            logger.debug("Failed to store resource snapshot for %s: %s", request.worker_name, e)
            db.rollback()

    # AI scheduler — persist system_state telemetry (kernel/distro/package_count/
    # air_gapped) for calibration staleness detection. Older worker binaries
    # don't send this block; skip silently in that case.
    if request.system_state and worker:
        try:
            from crud.worker_system_state_cud import upsert_worker_system_state
            upsert_worker_system_state(db, worker.id, request.system_state)
            db.commit()
        except Exception as e:
            logger.debug("Failed to upsert worker_system_state for %s: %s", request.worker_name, e)
            db.rollback()

    # Latest worker version comes from the release manifest at
    # worker-releases/latest.json (uploaded by release.sh). If there's no
    # manifest and no WORKER_AGENT_VERSION env var, get_latest_version()
    # returns None and `update_available` stays False — this prevents the
    # infinite self-update loop where the server compared the worker's
    # git-describe string against a hardcoded "1.0.0" placeholder, pushed
    # the worker into drain, and the worker downloaded a binary with the
    # exact same version baked in, heartbeated back unchanged, and looped
    # forever.
    from utils.worker_releases import get_latest_version, is_update_available
    # GY.C9 remainder — update-check kill switch. When egress.update_check_enabled
    # is False (or egress.sealed_mode is True) the heartbeat never advertises a
    # newer worker version: `update_available` stays False, so the worker's
    # drain-then-self-update flow (transport.go ServerStateReceiver) is never
    # triggered and no release binary is ever fetched.
    from utils import egress_policy as _egress_policy
    _uc_reason = _egress_policy.refuse_reason(_egress_policy.CATEGORY_UPDATE_CHECK, db=db)
    if _uc_reason:
        # Audit-safe: category + stable site code + switch name only — no
        # worker name, version or tenant id in the refusal ledger.
        _egress_policy.record_refusal(
            _egress_policy.CATEGORY_UPDATE_CHECK,
            "worker_heartbeat.update_advertise", _uc_reason)
        logger.info("worker heartbeat update-check refused: %s", _uc_reason)
        current_server_version = None
        update_available = False
    else:
        current_server_version = get_latest_version()
        update_available = is_update_available(request.agent_version)

    # Find tasks the server has expired/failed/cancelled for this worker so the
    # worker can HARD-kill the in-flight container (hard-cancel with cascade).
    # NOT gated on is_remote: the embedded worker (mod-worker-embedded) runs
    # containers exactly like a remote one, so gating this on is_remote meant a
    # user Cancel on the box's own GPU worker set the task to CANCELLED in the
    # DB but never reached the worker — the container ran to completion and the
    # slot was never freed. Every worker that claimed the task must be told to
    # stop executing it.
    tasks_to_cancel = worker_management_crud.list_worker_task_ids_by_status(
        db, worker.worker_name,
        [ApiTaskStatus.EXPIRED, ApiTaskStatus.FAILED, ApiTaskStatus.CANCELLED],
    )
    # Scope to what the worker is STILL executing. These rows are terminal and
    # keep claimed_by_worker set, so without this the same dead ids would be
    # re-sent on every beat forever. Once the worker kills the container it
    # drops the id from active_task_ids and this stops naming it. A worker too
    # old to report active_task_ids (None) gets the unfiltered list, as before.
    if request.active_task_ids is not None:
        _still_running = set(request.active_task_ids)
        tasks_to_cancel = [tid for tid in tasks_to_cancel if tid in _still_running]

    # Active task count for remote workers
    active_task_count = 0
    images_to_prepull = []
    # Default empty below the pending-image block: a remote worker with no
    # authorized tenants (e.g. a freshly-enrolled unlicensed worker) never
    # enters that block, and the sidecar check further down still needs
    # `cached` bound (pre-Q21.4 this 500'd such workers' first heartbeats).
    cached = set()
    if worker.is_remote:
        active_task_count = worker_management_crud.count_worker_claimed_tasks(
            db, worker.worker_name, [ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING],
        )

        # Determine images this worker should pre-pull based on pending tasks
        if worker.queues:
            from routers.worker_tasks import _get_authorized_tenants
            authorized = _get_authorized_tenants(worker)
            if authorized:
                from sqlalchemy import text as sa_text
                pending_images = db.execute(sa_text("""
                    SELECT DISTINCT task_payload->>'processing_image' AS img
                    FROM api_task_queue
                    WHERE status = :pending
                      AND queue_name = ANY(:queues)
                      AND tenant_id = ANY(:tenants)
                      AND task_payload->>'processing_image' IS NOT NULL
                    LIMIT 10
                """), {
                    "pending": ApiTaskStatus.PENDING.name,
                    "queues": worker.queues,
                    "tenants": authorized,
                }).fetchall()
                cached = set(hardware_specs.get("cached_images", []))
                images_to_prepull = [row.img for row in pending_images if row.img and row.img not in cached]


    # Server update coordination — checked via flag file set by update-system.sh
    server_updating = os.path.exists("/tmp/mod-server-updating")

    # Per-worker drain: operator flips `paused` (sticky) or `update_requested`
    # (one-shot) via Dashboard. `drain` is the OR of all three signals: global
    # server update, operator pause, update cycle. When drain is true, the Go
    # worker stops polling for new tasks, lets in-flight tasks finish, and (if
    # update_available) self-updates.
    worker_paused = bool(getattr(worker, "paused", False))
    worker_update_requested = bool(getattr(worker, "update_requested", False))

    # add-multi-region-mesh — failover roster: the URLs of every APPROVED manager
    # in the mesh. The worker keeps this list and, if its current manager goes
    # unreachable, rotates to the next one (so a dead manager doesn't strand its
    # workers). Empty on a single-manager deployment (nothing changes). Best-effort:
    # never let a registry hiccup break the heartbeat.
    mesh_managers = []
    try:
        # BOTH addresses, not just `url`. A manager has two of them because no
        # single one reaches every worker: `url` is the in-compose address
        # (http://api:80) that an embedded worker uses and a remote worker
        # cannot resolve, while `external_url` is the routable one — a tailnet
        # FQDN here — that a remote worker needs and that does not resolve
        # inside the compose network.
        #
        # Sending only `url` is why a worker that failed over got stranded: it
        # rotated to an address it could not reach, and the roster that would
        # have corrected it only arrives ON a heartbeat, which it could no
        # longer complete. With both present it rotates until one answers.
        #
        # External first: a worker that can reach both is better off on the
        # routable address, because that one keeps working if it moves.
        mesh_managers = []
        for m in worker_management_crud.list_approved_active_managers(db):
            for candidate in (m.external_url, m.url):
                c = (candidate or "").strip()
                if c and c not in mesh_managers:
                    mesh_managers.append(c)
    except Exception:  # noqa: BLE001
        mesh_managers = []

    # stack-ha-reconciliation §3.4 — the fence lease, derived from THIS
    # manager's observation grace period so the two cannot drift apart. The
    # worker adopts whatever it is told; it never configures its own, because a
    # lease longer than the manager's grace would reopen the window where a
    # stack gets re-placed while its original copy is still running.
    try:
        from services.stack_observation import fence_lease_seconds
        _fence_lease = fence_lease_seconds()
    except Exception:  # noqa: BLE001
        _fence_lease = 0

    # BUG-1028h J3b — the per-worker lease spread the manager may actually push.
    # One manager-derived lease expires on every worker at the same second, so
    # a site-wide loss fences the fleet in lockstep; the registry asks for a
    # spread, but the MANAGER owns the bound: fence_jitter_push_pct clamps it
    # so lease*(1+pct/100) still lands at or before grace - margin, i.e. the
    # chain `lease_effective < grace < gap` the split-brain argument rests on
    # cannot be defeated by a pushed jitter. Fail-closed: any failure ⇒ 0 =
    # no spread, the pre-J3 behaviour.
    try:
        from services.stack_observation import fence_jitter_push_pct
        _fence_jitter_push = fence_jitter_push_pct(db)
    except Exception:  # noqa: BLE001
        _fence_jitter_push = 0

    # BUG-1028h J2 — the fast-path resume hint. A fenced worker that just
    # reconnected has its stacks still stopped, and the reconciler has not run
    # yet (2-min cadence, 240s gap grace), so the manager answers THIS
    # heartbeat with the compose projects the worker may restart IN PLACE.
    # Guards are inherited verbatim from plan_for_resume (see
    # services/stack_reconciler.resume_set_for_worker); nothing is enqueued
    # here — durable state still comes from resume_fenced_stacks next tick.
    # Fail-closed: any failure ⇒ [] and the heartbeat still returns.
    _resume_projects = []
    if worker:
        _sr = optional_paid("services.stack_reconciler")
        if _sr is not None:
            try:
                _resume_projects = list(_sr.resume_set_for_worker(
                    db, request.worker_name))
            except Exception as _resume_exc:  # noqa: BLE001
                _resume_projects = []
                logger.warning(
                    "heartbeat: stack resume-set computation failed (non-fatal): %s",
                    type(_resume_exc).__name__,
                )

    response = {
        "status": worker.status.value,
        "worker_name": request.worker_name,
        "licensed": worker.status == WorkerStatus.LICENSED,
        "stack_fence_lease_s": _fence_lease,
        # BUG-1028h J2 — compose project names this worker may restart IN PLACE
        # on the host that just fenced it (see the block above). Empty list =
        # nothing qualifies; the worker treats absent/empty as "no resume".
        "stack_resume_projects": _resume_projects,
        # BUG-1028h J3b — the CLAMPED per-worker lease spread (see the block
        # above). Additive + optional: an older worker ignores the key and
        # keeps its un-spread lease, which is still safe.
        "stack_fence_jitter_pct": _fence_jitter_push,
        "update_available": update_available,
        "latest_version": current_server_version if update_available else "",
        "server_updating": server_updating,
        "paused": worker_paused,
        "update_requested": worker_update_requested,
        "drain": server_updating or worker_paused or worker_update_requested,
        "tasks_to_cancel": tasks_to_cancel,
        "images_to_prepull": images_to_prepull,
        "active_task_count": active_task_count,
        "max_concurrent_tasks": worker.max_concurrent_tasks,
        "docker_registry": configured_platform_registry(),
        "mesh_managers": mesh_managers,
        # worker-host-exec — the numeric WorkerLicense id of the worker this
        # heartbeat authenticated (resolved from the DB row, never from the
        # request), so the worker can key per-worker state on a stable id.
        # Only ever set on this enrolled success path; the not-enrolled early
        # returns above never carry the key.
        "worker_id": worker.id if worker is not None else None,
        # mdns-name-discovery §3 — re-home directive. None (JSON null, a no-op for
        # the worker) unless MOD_WORKER_REHOME_ENABLE is set; when set, carries the
        # current site login server + epoch so a worker whose manager IP slid (or
        # whose manager took over DHCP) re-homes itself without a hand-run
        # `tailscale up`. INERT on this box (flag default off).
        "mesh_rehome": _mesh_rehome_directive(worker, db=db),
        # Site-tailnet membership: login server + epoch every beat, plus a
        # single-use key only when the worker's site client reported it needs
        # one. None when disabled or no site anchor is configured.
        "mesh_site": mesh_site,
        # NOAH opportunistic-harvest settings (resolved override -> platform
        # default). enabled=false makes the worker a normal dedicated worker.
        "noah": _resolve_noah_settings(db, worker),
        # 24.5.s2 — the lend/pause pin (lend|pause|none), pushed every beat so
        # the Dashboard control and the machine's own tray switch drive ONE
        # verdict. The key itself is added below, only when the server has a
        # pin row for this worker (see _resolve_noah_mode).
    }
    # GS.PSR.7 pattern — the key is PRESENT ONLY when the server has an opinion.
    # An absent key = "the server never pinned this machine", which is what
    # tells a worker to keep whatever its own tray switch decided (POST
    # /noah/mode); an explicit "none" = "pin cleared, drop any pin you hold".
    _noah_mode = _resolve_noah_mode(db, worker)
    if _noah_mode is not None:
        response["noah_mode"] = _noah_mode
    # GS.PSR.7 — only add the key when there's something to send (see
    # _resolve_worker_tunables); an absent key, not a null/empty one, is what
    # tells an unconfigured worker fleet "nothing changed".
    tunables = _resolve_worker_tunables(db)
    if tunables:
        response["tunables"] = tunables
    # GSEC Q21.4 — per-worker Vault AppRole status, additive and MASKED
    # (approle_status carries secret fingerprints only, never the values;
    # delivery of the actual secret_id is Q21.8's design). Present only
    # while the enforcement mode is != off — in the default off posture
    # this block is skipped entirely, so the response is unchanged.
    # db=None keeps the (short) Vault read out of the DB session.
    if worker is not None and _approle_mode(db) != "off":
        try:
            from services.worker_approle import approle_status
            response["vault"] = approle_status(worker.id, db=None)
        except Exception:
            logger.exception(
                "worker AppRole status probe failed on heartbeat for %s "
                "(heartbeat continues without the vault object)",
                request.worker_name,
            )
    return response


@router.get("/workers/mesh/rehome/{worker_name}", response_model=WorkerRehomeResponse, response_model_exclude_unset=True)
async def get_worker_rehome(
    worker_name: str,
    db: Session = Depends(get_db),
    auth_worker: WorkerLicense = Depends(require_enrolled_worker),
):
    """Out-of-band re-home poll (mdns-name-discovery §3) — the same directive the
    heartbeat carries, for a worker that wants to check between beats or that lost
    contact and needs the current site login server to get back.

    Auth: ``require_enrolled_worker`` (the same X-Worker-Name / X-Bootstrap-Token
    gate every other ``/workers/...`` and ``/workers/tasks/...`` endpoint uses —
    see ``routers/worker_tasks.py``), covering both enrolled remote workers
    (bootstrap-token verified against ``WorkerEnrollment``) and the embedded
    worker (license-only path). This REPLACES an earlier in-handler check that
    only ran ``if enroll_rec is not None and enroll_rec.bootstrap_token_hash``
    — i.e. it silently skipped the token check entirely for any worker_name
    with no enrollment row (or an enrollment row with no token hash yet),
    leaving the directive readable with no credential at all for those names.
    We additionally pin the credential to the worker_name in the path (a
    worker may only poll its OWN re-home directive), which the heartbeat's
    equivalent check doesn't need to because there the worker name is the
    payload, not an independently-choosable path segment.

    INERT by default: 404 unless MOD_WORKER_REHOME_ENABLE is set, so the endpoint
    surfaces nothing on the live box until the flag is flipped."""
    if (os.getenv("MOD_WORKER_REHOME_ENABLE") or "false").strip().lower() \
            not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=404, detail="Worker re-home is not enabled")
    name = _normalize_worker_name(worker_name)
    if _normalize_worker_name(auth_worker.worker_name) != name:
        raise HTTPException(status_code=403,
                            detail="Worker credential does not match requested worker_name")
    worker = worker_management_service.get_worker_for_rehome(db, name)
    directive = _mesh_rehome_directive(worker, db=db)
    if directive is None:
        raise HTTPException(status_code=404, detail="No re-home directive")
    return directive


class WorkerConcurrencyRequest(BaseModel):
    max_concurrent_tasks: int


@router.patch("/workers/{worker_name}/concurrency", response_model=WorkerConcurrencyResponse, response_model_exclude_unset=True)
async def set_worker_concurrency(
    worker_name: str,
    request: WorkerConcurrencyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set max concurrent tasks for a worker. Platform admin only."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    if request.max_concurrent_tasks < 0:
        raise HTTPException(status_code=400, detail="max_concurrent_tasks must be 0 (unlimited) or a positive integer")

    worker = worker_management_service.set_concurrency(db, worker_name, request.max_concurrent_tasks)

    return {"worker_name": worker_name, "max_concurrent_tasks": worker.max_concurrent_tasks}


class NoahPlatformSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    sustained_quiet_s: Optional[int] = None
    pause_grace_s: Optional[int] = None
    plugged_in_only: Optional[bool] = None
    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    batch_appetite: Optional[int] = None
    stale_release_s: Optional[int] = None


class NoahWorkerSettingsRequest(BaseModel):
    # None = leave as-is; to reset a worker to inherit the platform default,
    # send inherit=true (clears all per-worker overrides).
    inherit: bool = False
    enabled: Optional[bool] = None
    sustained_quiet_s: Optional[int] = None
    pause_grace_s: Optional[int] = None
    plugged_in_only: Optional[bool] = None
    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    batch_appetite: Optional[int] = None


@router.get("/platform/noah/settings", response_model=NoahPlatformSettingsResponse, response_model_exclude_unset=True)
async def get_noah_platform_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read the platform-default NOAH settings. Platform admin only."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    _noah = require_paid("services.noah", "NOAH idle-worker harvest")
    d = _noah.get_platform_noah(db)
    online = _noah.count_pinnable_workers_online(db)
    db.commit()
    return {
        "enabled": d.enabled,
        "sustained_quiet_s": d.sustained_quiet_s,
        "pause_grace_s": d.pause_grace_s,
        "plugged_in_only": d.plugged_in_only,
        "schedule_start": d.schedule_start,
        "schedule_end": d.schedule_end,
        "batch_appetite": d.batch_appetite,
        "stale_release_s": d.stale_release_s,
        "pinnable_workers_online": online,
        "pinnable_starvation_warning": (
            None if online > 0 else
            "No online worker currently satisfies `pinnable` — pin-prone "
            "tasks (stack ops, profiling, volume ops) will dispatch WITHOUT "
            "the requirement (starvation guard) if noah.require_pinnable_enabled "
            "is on. Add/re-enable a dedicated worker, disable NOAH "
            "platform-wide, or clear a worker's `not-pinnable` tag."
        ),
    }


@router.patch("/platform/noah/settings", response_model=NoahStatusResponse, response_model_exclude_unset=True)
async def set_noah_platform_settings(
    request: NoahPlatformSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the platform-default NOAH settings. Platform admin only."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    _noah = require_paid("services.noah", "NOAH idle-worker harvest")
    d = _noah.get_platform_noah(db)
    for field in ("enabled", "sustained_quiet_s", "pause_grace_s", "plugged_in_only",
                  "schedule_start", "schedule_end", "batch_appetite", "stale_release_s"):
        val = getattr(request, field)
        if val is not None:
            setattr(d, field, val)
    d.updated_by_user_id = current_user.id
    db.commit()
    return {"status": "ok"}


@router.get("/platform/noah/fleet", response_model=NoahFleetResponse, response_model_exclude_unset=True)
async def get_noah_fleet(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fleet-wide NOAH observability (noah-observability).

    One place to see, across every NOAH-enabled worker: whether NOAH is enabled
    (resolved), the last reported availability + reason, when that availability
    last flipped, and how many never-started tasks have been harvested off it.
    Replaces having to open each worker's modal one at a time.

    Platform admin only. Any Dashboard surface for this is a separate FE task —
    flagged for the Dashboard stream.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return worker_management_service.get_noah_fleet_view(db)


@router.get("/workers/{worker_name}/noah", response_model=WorkerNoahSettingsResponse, response_model_exclude_unset=True)
async def get_worker_noah_settings(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Effective + raw per-worker NOAH settings for the Dashboard form."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return worker_management_service.get_worker_noah_settings_view(db, worker_name)


@router.patch("/workers/{worker_name}/noah", response_model=WorkerNoahSettingsResponse, response_model_exclude_unset=True)
async def set_worker_noah_settings(
    worker_name: str,
    request: NoahWorkerSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set (or reset to inherit) a worker's NOAH overrides. Platform admin only."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    fields = ("enabled", "sustained_quiet_s", "pause_grace_s", "plugged_in_only",
              "schedule_start", "schedule_end", "batch_appetite")
    values = {f: getattr(request, f) for f in fields}
    return worker_management_service.set_worker_noah_settings_view(
        db, worker_name, inherit=request.inherit, values=values,
    )


class WorkerCostRequest(BaseModel):
    """What this machine costs its operator to run.

    Either state `cost_per_hour_cents` outright, or supply the workings and let
    the server derive it. Supplying both keeps the explicit rate — an operator
    who typed a number meant it.
    """
    cost_per_hour_cents: Optional[int] = None
    hardware_cost_cents: Optional[int] = None
    amortisation_months: Optional[int] = None
    power_watts: Optional[int] = None
    electricity_cents_per_kwh: Optional[int] = None
    margin_percent: Optional[int] = None


DEFAULT_MARGIN_PERCENT = int(os.getenv("DEFAULT_MARGIN_PERCENT", "20"))


def billed_per_hour_cents(cost_cents: Optional[int],
                          margin_percent: Optional[int]) -> Optional[int]:
    """What to CHARGE per hour: cost plus margin.

    Kept separate from cost so both numbers survive. Storing only the billed
    rate loses the ability to answer "are we actually making money on this
    machine", which is the question the whole exercise exists to answer.

    A NULL margin takes the platform default rather than zero — an operator who
    never opened the margin field should not silently be selling at cost.
    """
    if cost_cents is None:
        return None
    pct = margin_percent if margin_percent is not None else DEFAULT_MARGIN_PERCENT
    return int(round(cost_cents * (1.0 + pct / 100.0)))


def derive_cost_per_hour_cents(
    hardware_cost_cents: Optional[int],
    amortisation_months: Optional[int],
    power_watts: Optional[int],
    electricity_cents_per_kwh: Optional[int],
) -> Optional[int]:
    """COGS per hour = amortised capital + electricity. Cold arithmetic.

        capital/hr    = hardware_cost / (amortisation_months × 730)
        electricity/hr = (watts / 1000) × cents_per_kWh

    730 is the mean hours in a month. Amortising against WALL-CLOCK hours
    rather than busy hours is deliberate: an idle machine still depreciates and
    still has to be paid for, so pricing only the busy hours would quietly
    under-recover exactly when utilisation is poor.

    Either half may be supplied alone — capital-only for a machine whose power
    is someone else's problem (a colo bundle), electricity-only for hardware
    already written off. Returns None when neither is answerable, which means
    "not priced" rather than "free".
    """
    total = 0.0
    answered = False
    if hardware_cost_cents and amortisation_months and amortisation_months > 0:
        total += hardware_cost_cents / (amortisation_months * 730.0)
        answered = True
    if power_watts and electricity_cents_per_kwh:
        total += (power_watts / 1000.0) * electricity_cents_per_kwh
        answered = True
    return int(round(total)) if answered else None


@router.patch("/workers/{worker_name}/cost", response_model=WorkerCostResponse, response_model_exclude_unset=True)
async def set_worker_cost(
    worker_name: str,
    request: WorkerCostRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set what a worker costs to run, so its work can be priced on reality.

    Per worker because the same card in two buildings has different
    electricity behind it, and an on-prem customer's costs are theirs.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    # CE carve: run-cost estimation (services/run_cost_estimate.py) is denied
    # (open-core/manifest.yaml:1099; the services/cost_*.py glob is denied at
    # :1188) while this router ships (api_allow :95). A CE platform admin must
    # not be able to price a worker: the UI form is hidden (CORE/Dashboard/js/
    # workers.js:2051 _paidDetail) but the route is reachable, so gate it like
    # the AppRole rotate at :4971 — 402 "Pro feature", nothing written, never a
    # 500.
    require_paid("services.run_cost_estimate", "worker cost basis")

    for field in ("cost_per_hour_cents", "hardware_cost_cents",
                  "amortisation_months", "power_watts",
                  "electricity_cents_per_kwh", "margin_percent"):
        value = getattr(request, field)
        if value is not None and value < 0:
            raise HTTPException(status_code=400, detail=f"{field} cannot be negative")

    worker = worker_management_service.set_cost(
        db,
        worker_name,
        cost_per_hour_cents=request.cost_per_hour_cents,
        hardware_cost_cents=request.hardware_cost_cents,
        amortisation_months=request.amortisation_months,
        power_watts=request.power_watts,
        electricity_cents_per_kwh=request.electricity_cents_per_kwh,
        margin_percent=request.margin_percent,
        derive_cost_per_hour_cents_fn=derive_cost_per_hour_cents,
    )

    return {
        "worker_name": worker_name,
        "cost_per_hour_cents": worker.cost_per_hour_cents,
        "hardware_cost_cents": worker.hardware_cost_cents,
        "amortisation_months": worker.amortisation_months,
        "power_watts": worker.power_watts,
        "electricity_cents_per_kwh": worker.electricity_cents_per_kwh,
        "margin_percent": worker.margin_percent,
        "effective_margin_percent": (worker.margin_percent
                                     if worker.margin_percent is not None
                                     else DEFAULT_MARGIN_PERCENT),
        "billed_per_hour_cents": billed_per_hour_cents(
            worker.cost_per_hour_cents, worker.margin_percent),
    }


@router.get("/workers/network-zones", response_model=List[NetworkZoneResponse], response_model_exclude_unset=True)
async def list_network_zones(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all known network zones from registered workers, plus 'public'.

    Used by the workflow builder to populate the network zone dropdown
    on NIM Bridge and other external-service nodes.
    """
    zones = worker_management_service.list_network_zones_view(db)
    return [{"id": z, "name": z} for z in zones]


@router.patch("/workers/{worker_name}/network-zone", response_model=WorkerNetworkZoneResponse, response_model_exclude_unset=True)
async def set_worker_network_zone(
    worker_name: str,
    zone: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set the network zone for a worker. Platform admin only."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    worker = worker_management_service.set_network_zone(db, worker_name, zone)

    return {"worker_name": worker_name, "network_zone": worker.network_zone}


class WorkerExportControlResponse(BaseModel):
    worker_name: str
    us_person_operated: bool


@router.patch("/workers/{worker_name}/export-control", response_model=WorkerExportControlResponse)
async def set_worker_export_control(
    worker_name: str,
    us_person_operated: bool,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """GY.V2b — flag a worker as US-located/US-person-operated (platform
    admin only). Flagged workers are the ONLY ones allowed to claim tasks
    whose inputs carry EAR/ITAR (and CUI when
    compliance.cui_requires_us_person is on); unflagged workers never
    receive such work. Audit: EXPORT_CONTROL_WORKER_FLAG_SET."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    # 1788h CE carve: data_classification is paid/denied. The flag itself lives on
    # the CE-kept WorkerLicense model, so the write stays; only the paid module's
    # audit helper is skipped (audit-only side effect — the flag write is unchanged).
    from utils.paid_import import optional_paid
    _dc2b = optional_paid("services.data_classification")
    worker = worker_management_service.get_worker_or_404(db, worker_name)

    worker.us_person_operated = bool(us_person_operated)
    if _dc2b is not None:
        _dc2b.record_audit(
            db,
            tenant_id=worker.owner_tenant_id,
            user=current_user,
            event_name="EXPORT_CONTROL_WORKER_FLAG_SET",
            details={
                "worker_name": worker_name,
                "us_person_operated": bool(us_person_operated),
                "actor_user_id": getattr(current_user, "id", None),
                "actor_username": getattr(current_user, "username", None),
            },
        )
    db.commit()
    return {"worker_name": worker_name,
            "us_person_operated": bool(us_person_operated)}


@router.post("/workers/assign-license", response_model=LicenseAssignmentResponse, response_model_exclude_unset=True)
async def assign_license(
    request: LicenseAssignmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Assign a license to a worker.

    Platform admin only endpoint.
    """

    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can approve workers"
        )

    # Get license status
    license_status = get_license_status()

    if not license_status.get("valid") and not license_status.get("development_mode"):
        raise HTTPException(
            status_code=403,
            detail=f"Worker not approved yet: {license_status.get('message')}"
        )

    max_workers = license_status.get("workers_max", 0)

    # Row-level locking (`FOR UPDATE`, in the CRUD fetch) prevents a race when
    # multiple requests try to assign licenses simultaneously.
    worker, licensed_count, max_workers = worker_management_service.assign_license(
        db,
        worker_name=request.worker_name,
        owner_type=request.owner_type,
        owner_tenant_id=request.owner_tenant_id,
        assigned_tenant_ids=request.assigned_tenant_ids,
        license_status=license_status,
        licensed_by_user_id=current_user.id,
    )

    # Go workers read their license status from the heartbeat response.
    # On next heartbeat, the worker sees LICENSED status and starts accepting tasks.
    resume_worker(worker.worker_name, worker.queues)

    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="WORKER_LICENSE_ASSIGNED",
            details={
                "worker_name": worker.worker_name,
                "owner_type": request.owner_type,
            },
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for license assign")

    return {
        "success": True,
        "message": f"Worker approved: {worker.worker_name}",
        "licensed_count": licensed_count + 1,
        "max_workers": max_workers
    }


@router.post("/workers/revoke-license", response_model=LicenseAssignmentResponse, response_model_exclude_unset=True)
async def revoke_license(
    request: LicenseAssignmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Revoke a license from a worker.

    Platform admin only endpoint.
    """

    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can un-approve workers"
        )

    worker, licensed_count = worker_management_service.revoke_license(
        db, request.worker_name, current_user.id,
    )

    # Pause worker (stop receiving new tasks)
    pause_worker(worker.worker_name)

    license_status = get_license_status()
    max_workers = license_status.get("workers_max", 0)

    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="WORKER_LICENSE_REVOKED",
            details={"worker_name": worker.worker_name},
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for license revoke")

    # GSEC Q21.4 — a revoked license deactivates the worker (UNLICENSED +
    # paused); drop its per-worker Vault AppRole (mode != off; failures are
    # logged and never block the deactivation).
    _revoke_worker_approle(worker.id, db, user_id=current_user.id,
                           tenant_id=current_user.tenant_id,
                           worker_name=worker.worker_name)

    return {
        "success": True,
        "message": f"Worker approval removed from: {worker.worker_name}",
        "licensed_count": licensed_count,
        "max_workers": max_workers
    }


@router.delete("/workers/{worker_name}", response_model=WorkerDeleteResponse, response_model_exclude_unset=True)
async def delete_worker(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Permanently delete a worker and its enrollment record.
    Platform admin only. Worker must be offline or revoked.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can delete workers")

    # Capture the id BEFORE the cascade: the deleted worker row comes back
    # detached (soft-delete, BUG-1029) with its .id expired after the commit.
    _pre_delete = worker_management_crud.get_worker_by_name(db, worker_name)
    _deleted_worker_id = _pre_delete.id if _pre_delete is not None else None
    _deleted, _enrollment = worker_management_service.delete_worker_row(
        db, worker_name, deleted_by_user_id=current_user.id
    )

    # Audit (SOC 2 CC6, ISO 27001 §A.12.1.2): platform infrastructure
    # changes are recorded in audit_events for the launch compliance
    # evidence chain.
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="WORKER_DELETED",
            details={"worker_name": worker_name},
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for worker delete")

    # GSEC Q21.4 — worker removal revokes its per-worker Vault AppRole
    # (mode != off; failures are logged and never block the removal).
    if _deleted_worker_id is not None:
        _revoke_worker_approle(_deleted_worker_id, db, user_id=current_user.id,
                               tenant_id=current_user.tenant_id,
                               worker_name=worker_name)

    return {"detail": f"Worker '{worker_name}' deleted", "worker_name": worker_name}


@router.post("/workers/{worker_id}/approle/rotate", response_model=WorkerAppRoleRotateResponse, response_model_exclude_unset=True)
async def rotate_worker_approle(
    worker_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Rotate a worker's per-worker Vault AppRole secret_id.

    Platform admin only. Mints a new secret_id for the worker's role
    (the previous one stays valid until its TTL so the worker can
    switch over); the response is MASKED — secret values are delivered
    to the worker out-of-band (install render / two-secret window),
    never through this endpoint.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can rotate worker AppRoles")

    worker = worker_management_crud.get_worker_by_id(db, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"Worker {worker_id} not found")

    # 1788h CE carve: per-worker Vault AppRole rotation is a PAID hardening
    # (services/worker_approle.py is denied). CE must NOT skip it silently — a
    # 200 here would claim a credential was rotated when none exists. CE worker
    # auth keeps its CE worker-token path (unaffected); this endpoint 402s as a
    # Pro feature, so nothing is granted, relaxed, or implied.
    from utils.paid_import import require_paid
    _worker_approle = require_paid(
        "services.worker_approle", "per-worker Vault AppRole rotation"
    )
    WorkerAppRoleError = _worker_approle.WorkerAppRoleError
    WorkerAppRoleNotFound = _worker_approle.WorkerAppRoleNotFound
    try:
        payload = _worker_approle.rotate_approle(
            worker_id,
            db=db,
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
        )
    except WorkerAppRoleNotFound:
        raise HTTPException(
            status_code=404,
            detail=f"No per-worker AppRole state for worker {worker_id} "
                   f"(enrollment not run with approle mode != off)",
        )
    except WorkerAppRoleError:
        logger.exception("worker AppRole rotate failed for worker %s (id=%s)",
                         worker.worker_name, worker_id)
        raise HTTPException(
            status_code=502,
            detail="Worker AppRole rotate failed (Vault unavailable or misconfigured); "
                   "see API logs for details",
        )

    return {
        "worker_id": worker_id,
        "worker_name": worker.worker_name,
        "role": payload.get("role"),
        "rotated": True,
        "prev_secret_id_valid_until": payload.get("prev_secret_id_valid_until"),
    }


@router.post("/workers/{worker_name}/pause", response_model=WorkerPauseResumeResponse, response_model_exclude_unset=True)
async def pause_worker_endpoint(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Drain a worker — it finishes in-flight tasks and stops claiming new ones.
    When combined with a pending self-update, the worker swaps its binary
    once the queue is empty and restarts automatically. Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can pause workers")
    worker = worker_management_service.get_worker_or_404(db, worker_name)
    _guard_not_last_active_worker(db, worker, action="pause")
    pause_worker(worker_name, db=db)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="WORKER_PAUSED", details={"worker_name": worker_name},
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for worker pause")
    return {
        "success": True,
        "worker_name": worker_name,
        "paused": True,
        "message": f"Drain signal sent to {worker_name}. In-flight tasks will complete; new tasks will not be claimed.",
    }


@router.post("/workers/{worker_name}/resume", response_model=WorkerPauseResumeResponse, response_model_exclude_unset=True)
async def resume_worker_endpoint(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear the drain flag — worker resumes claiming new tasks."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can resume workers")
    worker = worker_management_service.get_worker_or_404(db, worker_name)
    resume_worker(worker_name, db=db)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="WORKER_RESUMED", details={"worker_name": worker_name},
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for worker resume")
    return {
        "success": True,
        "worker_name": worker_name,
        "paused": False,
        "message": f"{worker_name} resumed — will claim new tasks on next poll.",
    }


@router.get("/workers/latest-release", response_model=WorkerReleaseInfo, response_model_exclude_unset=True)
async def get_latest_worker_release(
    current_user: User = Depends(get_current_user),
):
    """
    Return metadata about the latest worker release.

    Reads `worker-releases/latest.json` from MinIO, falling back to the
    WORKER_AGENT_VERSION env var if no manifest exists yet (keeps legacy
    deployments working). Useful for the Dashboard's worker-detail modal
    to show version, release date, SHA-256 hashes, and a changelog link.

    Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    from utils.worker_releases import get_latest_release_info
    return get_latest_release_info()


@router.post("/workers/{worker_name}/update", response_model=WorkerUpdateResponse, response_model_exclude_unset=True)
async def update_worker_endpoint(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Trigger a one-shot update cycle for a worker: drain -> self-update -> auto-resume.

    Distinct from Pause:
      - Pause is sticky; the operator must click Resume to bring a worker back.
      - Update is one-shot; the server auto-clears update_requested when it
        sees a heartbeat from the new binary version, so the worker resumes
        claiming tasks on its own.

    Platform admin only. 400 if the worker is already on the latest version.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can trigger updates")

    worker = worker_management_service.get_worker_or_404(db, worker_name)
    _guard_not_last_active_worker(db, worker, action="update")

    from utils.worker_releases import get_latest_version
    current_server_version = get_latest_version()
    if worker.agent_version and worker.agent_version == current_server_version:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Worker '{worker_name}' is already on version {current_server_version}. "
                "Upload a newer binary to MinIO worker-releases first."
            ),
        )
    if worker.update_requested:
        return {
            "success": True,
            "worker_name": worker_name,
            "update_requested": True,
            "message": f"{worker_name} is already draining for an update.",
        }

    worker.update_requested = True
    db.commit()
    logger.info(
        "Update requested for worker %s by user %s (current=%s, latest=%s)",
        worker_name, current_user.id, worker.agent_version, current_server_version,
    )
    return {
        "success": True,
        "worker_name": worker_name,
        "update_requested": True,
        "current_version": worker.agent_version,
        "latest_version": current_server_version,
        "message": (
            f"Update requested for {worker_name}. "
            "Worker will finish in-flight tasks, self-update to "
            f"{current_server_version}, and auto-resume."
        ),
    }


@router.post("/workers/enrollments/{worker_name}/reinstate", response_model=ReinstateEnrollmentResponse, response_model_exclude_unset=True)
async def reinstate_enrollment(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Flip a REVOKED enrolment back to PENDING so the operator can re-run
    setup-worker.sh on the remote host and approve it fresh.

    This is the explicit operator action that counteracts the fail-closed
    REVOKED path in /workers/enroll/start — possession of an enrolment
    token alone cannot silently un-revoke a worker, but an admin clicking
    Reinstate here is unambiguous intent. Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform administrators can reinstate workers")

    # Flip to PENDING. Keep revoked_at/revoked_by_user_id for audit — they
    # document the prior revocation. approved_at/approved_by_user_id MUST
    # be cleared because we're starting a fresh approval cycle; operator
    # must explicitly approve again after the worker re-enrols. We do NOT
    # clear revoked_at — the historical revocation is still true and visible
    # in the audit view.
    enrollment = worker_management_service.reinstate_enrollment_row(db, worker_name)

    logger.info(
        "Enrolment reinstated for worker %s by user %s (was REVOKED at %s)",
        worker_name, current_user.id, enrollment.revoked_at,
    )
    return {
        "success": True,
        "worker_name": worker_name,
        "status": "pending",
        "message": (
            f"{worker_name} reinstated to PENDING. Re-run setup-worker.sh on "
            "the remote host, then approve the worker in the Dashboard."
        ),
    }


@router.post("/platform/tenants/{tenant_id}/adapter-cache/flush", response_model=FlushTenantAdapterCacheResponse, response_model_exclude_unset=True)
async def flush_tenant_adapter_cache(
    tenant_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Invalidate a single tenant's variant_adapter cache.

    Bumps the per-tenant salt at `mod:adapter:tenant:{tenant_id}:salt`
    in Redis. The variant_adapter reads that salt into every cache
    key, so INCRing it orphans every prior cached AdaptResult for
    that tenant in a single atomic operation. Old entries stale out
    via the 7-day TTL; no scan+delete needed.

    Platform admin only. The per-tenant lever (distinct from bumping
    the global PROMPT_TEMPLATE_VERSION constant) lets support flush
    one tenant's cache when debugging an adapter quirk without
    invalidating everyone else's work.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can flush adapter caches",
        )

    # Look up the tenant to 404 early on bogus ids rather than just
    # silently INCRing an orphan Redis key.
    worker_management_service.get_tenant_or_404(db, tenant_id)

    try:
        import redis as _redis
        url = os.getenv("REDIS_URL", "").strip()
        if not url:
            raise HTTPException(
                status_code=503,
                detail="REDIS_URL not configured; adapter cache isn't active",
            )
        client = _redis.Redis.from_url(
            url, socket_timeout=2.0, socket_connect_timeout=2.0,
            decode_responses=True,
        )
        new_salt = client.incr(f"mod:adapter:tenant:{tenant_id}:salt")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("adapter-cache flush failed for tenant=%s: %s", tenant_id, e)
        raise internal_http_error(500, "Adapter cache flush failed", e) from e

    logger.info(
        "adapter-cache flushed for tenant=%s by user=%s (new salt=%s)",
        tenant_id, current_user.id, new_salt,
    )
    return {
        "success": True,
        "tenant_id": tenant_id,
        "new_salt": new_salt,
        "message": (
            f"Adapter cache for tenant {tenant_id} flushed. Next "
            f"wizard generation will bypass cached results."
        ),
    }


class ZombieCleanupRequest(BaseModel):
    max_offline_days: int = 7


@router.post("/workers/cleanup-zombies", response_model=WorkerCleanupZombieResponse, response_model_exclude_unset=True)
async def cleanup_zombie_workers(
    request: ZombieCleanupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Clean up zombie workers (OFFLINE for extended periods).

    Workers that have been OFFLINE for more than max_offline_days
    are considered abandoned and will be removed from the database.

    Platform admin only endpoint.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can cleanup zombie workers"
        )

    cleaned_count, cleaned_workers, cutoff_date = worker_management_service.cleanup_zombie_workers(
        db, request.max_offline_days,
    )

    for w in cleaned_workers:
        if is_debug_mode():
            logger.info(f"Cleaned up zombie worker: {w['worker_name']} (last seen: {w['last_seen']})")
        else:
            logger.info(f"Cleaned up zombie worker: {w['worker_name']}")

    return {
        "success": True,
        "cleaned_count": cleaned_count,
        "cleaned_workers": cleaned_workers,
        "cutoff_date": cutoff_date.isoformat()
    }


@router.get("/workers/license-summary", response_model=LicenseSummaryResponse, response_model_exclude_unset=True)
async def get_license_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get summary of worker license usage.

    Platform admin only endpoint.
    """

    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can view the worker summary"
        )

    try:
        license_status = get_license_status()
    except Exception as e:
        import traceback
        if is_debug_mode():
            logger.error(f"ERROR in get_license_status: {e}")
            traceback.print_exc()
        else:
            logger.error("ERROR in get_license_status (set DEBUG_MODE=true for details)")
        raise HTTPException(
            status_code=500,
            detail="Worker status check failed"
        )

    return worker_management_service.get_license_summary_view(db, license_status)


@router.get("/platform/queue", response_model=PlatformQueueResponse, response_model_exclude_unset=True)
async def get_platform_queue(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    """
    Return the platform-wide 'pre queue' — all tasks not yet in-flight,
    plus an in-flight snapshot. Used by the Workers page's "System Queue"
    button to show operators what's about to run without needing to
    click into each worker.

    Response:
      {
        "in_flight": [...tasks currently RUNNING / CLAIMED, any worker],
        "pending":   [...tasks PENDING or ASSIGNED, not yet claimed],
        "by_queue":  {"CPU": N, "CUDA": N, ...}  # pending counts per queue,
        "counts":    {"running": N, "claimed": N, "pending": N, "assigned": N},
      }

    Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    return worker_management_service.get_platform_queue_view(db, limit)


@router.get("/workers/{worker_name}/queue", response_model=WorkerQueueResponse, response_model_exclude_unset=True)
async def get_worker_queue(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return what a specific worker is doing + what's targeted at it.
    Powers the per-worker "Queue" button on the Workers page.

    Response:
      {
        "worker_name": "...",
        "in_flight": [...tasks this worker is RUNNING or has CLAIMED],
        "assigned":  [...tasks assigned_to_worker=this worker, still PENDING/ASSIGNED],
      }

    Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    return worker_management_service.get_worker_queue_view(db, worker_name)


def _summarise_unrunnable_tasks(db: Session) -> Dict[str, Any]:
    """Aggregate the unrunnable-task flags for the System Health
    banner. Returns {count, required_tags, sample_ids}.

    required_tags is the union of all gpu_variants keys across the
    unrunnable set — what the Dashboard says you're missing ("tasks
    waiting for rocm, intel"). sample_ids gives the first few task
    ids so the operator can drill in without pulling the whole list.
    """
    try:
        unrun = worker_management_crud.list_unrunnable_pending_tasks(db, 200)
    except Exception:
        return {"count": 0, "required_tags": [], "sample_ids": []}

    required: set[str] = set()
    for t in unrun:
        payload = t.task_payload or {}
        gv = payload.get("gpu_variants") if isinstance(payload, dict) else None
        if isinstance(gv, dict):
            required.update(str(k).strip().lower() for k in gv.keys())
    return {
        "count": len(unrun),
        "required_tags": sorted(required),
        "sample_ids": [t.id for t in unrun[:10]],
    }


@router.get("/workers/capacity", response_model=PlatformCapacityResponse, response_model_exclude_unset=True)
async def get_platform_capacity(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive platform capacity information including hardware specs,
    utilization, and queue health.

    Platform admin only endpoint.
    """

    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators can view capacity information"
        )

    # Discover current workers from DB (Go workers register via heartbeat)
    discovered = discover_workers(db)
    mark_offline_workers(db, discovered)

    try:
        # All worker data comes from the database (populated by heartbeat API)
        all_workers = worker_management_crud.list_all_workers(db)

        # Count active tasks from task queue
        active_task_counts = {}
        running_tasks = worker_management_crud.list_tasks_in_statuses(
            db, [ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED],
        )
        for t in running_tasks:
            w = t.claimed_by_worker or ""
            active_task_counts[w] = active_task_counts.get(w, 0) + 1

        # Build capacity from database (populated by heartbeat API)
        total_cpu = 0
        total_memory_gb = 0.0
        total_gpu = 0
        total_vram_gb = 0.0

        licensed_cpu = 0
        licensed_memory_gb = 0.0
        licensed_gpu = 0
        licensed_vram_gb = 0.0

        cpu_cores_used = 0
        memory_gb_used = 0.0
        gpu_count_used = 0
        vram_gb_used = 0.0

        snapshots = {}
        try:
            for snap in worker_management_crud.list_all_resource_timelines(db):
                snapshots[snap.worker_name] = snap
        except Exception:
            pass

        workers_detail = []

        # Per-backend rollups keyed by kind (cuda / rocm / intel / npu).
        # Populated from each worker's hardware_specs["accelerators"] list
        # (the per-device output of EnumerateAccelerators on the worker),
        # falling back to the legacy single-GPU fields when a worker on an
        # older agent hasn't sent accelerators yet. Surfaced at the end of
        # this function under "gpu_by_backend" so the Dashboard's System
        # Health panel can render one tile per backend instead of a single
        # undifferentiated "GPUs" number that silently under-counts
        # multi-backend boxes.
        def _new_bucket():
            return {"total_count": 0, "total_vram_gb": 0.0,
                    "licensed_count": 0, "licensed_vram_gb": 0.0,
                    # used_vram_gb = sum of each licensed worker's most
                    # recent `vram_used_mb` snapshot, attributed to that
                    # worker's primary backend. Per-worker, not per-device
                    # (WorkerResourceTimeline aggregates VRAM over the
                    # whole worker; a multi-backend box would attribute
                    # all used VRAM to whichever backend we bucket it
                    # under, which is acceptable because it's rare in
                    # practice and the tile is coarse-grained).
                    "used_vram_gb": 0.0,
                    "devices": []}
        gpu_by_backend: Dict[str, Dict[str, Any]] = {}

        for worker in all_workers:
                hw = worker.hardware_specs or {}
                queues = worker.queues or []
                accelerators = hw.get("accelerators") or []

                cpu_count = hw.get('cpu_cores', 0)
                memory_gb = hw.get('ram_gb', 0)

                # Prefer the per-device list (accurate, multi-GPU-aware)
                # and fall back to the legacy single-GPU summary when the
                # agent hasn't sent accelerators yet (pre-v0.3.27 workers
                # or workers still waiting on a hardware re-enumeration).
                if accelerators:
                    gpu_count = len(accelerators)
                    vram_gb = sum(float(d.get("vram_gb") or 0) for d in accelerators)
                else:
                    gpu_count = 1 if hw.get('has_gpu') else 0
                    vram_gb = hw.get('vram_gb', hw.get('gpu_vram_mb', 0) / 1024.0)
                gpu_name = hw.get('gpu_name', '')

                active_count = active_task_counts.get(worker.worker_name, 0)

                total_cpu += cpu_count
                total_memory_gb += memory_gb
                total_gpu += gpu_count
                total_vram_gb += vram_gb

                is_licensed = worker.status == WorkerStatus.LICENSED
                if is_licensed:
                    licensed_cpu += cpu_count
                    licensed_memory_gb += memory_gb
                    licensed_gpu += gpu_count
                    licensed_vram_gb += vram_gb

                    snap = snapshots.get(worker.worker_name)
                    if snap:
                        if snap.cpu_percent is not None and cpu_count > 0:
                            cpu_cores_used += round(cpu_count * snap.cpu_percent / 100.0, 1)
                        if snap.ram_used_mb is not None:
                            memory_gb_used += snap.ram_used_mb / 1024.0
                        if snap.vram_used_mb is not None and snap.vram_used_mb > 0:
                            vram_gb_used += snap.vram_used_mb / 1024.0
                        if snap.gpu_utilization_pct is not None and snap.gpu_utilization_pct > 0:
                            gpu_count_used += 1
                    else:
                        cpu_cores_used += active_count
                        memory_gb_used += active_count * 4.0

                # Populate per-backend rollup. If the worker reports no
                # accelerators but has_gpu is true, fall back to a single
                # synthetic device bucketed under the legacy gpu_type so
                # pre-v0.3.27 CUDA workers still render correctly.
                # Per-worker VRAM usage (whole-worker aggregate). We
                # attribute all of it to this worker's *primary* backend
                # below, which is ambiguous on a multi-backend box but
                # correct for the overwhelmingly common single-backend
                # case and produces a sensible tile in either one.
                snap_for_backend = snapshots.get(worker.worker_name) if is_licensed else None
                worker_used_vram_gb = 0.0
                if snap_for_backend and snap_for_backend.vram_used_mb and snap_for_backend.vram_used_mb > 0:
                    worker_used_vram_gb = snap_for_backend.vram_used_mb / 1024.0

                if accelerators:
                    # Primary backend = kind of the first accelerator; used-
                    # VRAM attributes to that bucket so the tile reflects
                    # real usage without double-counting across kinds.
                    primary_kind = (accelerators[0].get("kind") or "").strip().lower() or "unknown"
                    for d in accelerators:
                        kind = (d.get("kind") or "").strip().lower() or "unknown"
                        bucket = gpu_by_backend.setdefault(kind, _new_bucket())
                        dev_vram = float(d.get("vram_gb") or 0)
                        bucket["total_count"] += 1
                        bucket["total_vram_gb"] += dev_vram
                        if is_licensed:
                            bucket["licensed_count"] += 1
                            bucket["licensed_vram_gb"] += dev_vram
                        bucket["devices"].append({
                            "worker_name": worker.worker_name,
                            "kind": kind,
                            "index": d.get("index"),
                            "name": d.get("name"),
                            "vram_gb": dev_vram,
                            "licensed": is_licensed,
                        })
                    if worker_used_vram_gb > 0:
                        gpu_by_backend[primary_kind]["used_vram_gb"] += worker_used_vram_gb
                elif gpu_count > 0:
                    kind = (hw.get("gpu_type") or "unknown").strip().lower()
                    bucket = gpu_by_backend.setdefault(kind, _new_bucket())
                    bucket["total_count"] += gpu_count
                    bucket["total_vram_gb"] += vram_gb
                    if is_licensed:
                        bucket["licensed_count"] += gpu_count
                        bucket["licensed_vram_gb"] += vram_gb
                    bucket["devices"].append({
                        "worker_name": worker.worker_name,
                        "kind": kind,
                        "index": 0,
                        "name": gpu_name,
                        "vram_gb": vram_gb,
                        "licensed": is_licensed,
                    })
                    if worker_used_vram_gb > 0:
                        bucket["used_vram_gb"] += worker_used_vram_gb

                workers_detail.append({
                    "name": worker.worker_name,
                    "worker_name": worker.worker_name,
                    "hostname": worker.hostname or worker.worker_name,
                    "cpu_cores": cpu_count,
                    "memory_gb": memory_gb,
                    "available_cpu": max(0, cpu_count - active_count),
                    "available_memory_gb": max(0, memory_gb - active_count * 4.0),
                    "gpu_count": gpu_count,
                    "gpu_name": gpu_name,
                    "vram_gb": round(vram_gb, 1),
                    "accelerators": accelerators,
                    "queues": queues,
                    "licensed": is_licensed,
                    "active_tasks": active_count,
                    "reserved_tasks": 0,
                    "owner_type": worker.owner_type.value if worker.owner_type else None,
                    "status": worker.status.value,
                    "agent_version": hw.get("agent_version", ""),
                })

        # Queue health from ApiTaskQueue table
        queue_health = []
        all_queues = set()
        for w in workers_detail:
            all_queues.update(w['queues'])

        pending_by_queue = {}
        pending_tasks = worker_management_crud.list_tasks_by_status(db, ApiTaskStatus.PENDING)
        for t in pending_tasks:
            q = t.queue_name or "CPU"
            pending_by_queue[q] = pending_by_queue.get(q, 0) + 1

        for queue in sorted(all_queues):
            queue_health.append({
                "queue_name": queue,
                "tasks_waiting": pending_by_queue.get(queue, 0),
                "workers_watching": sum(1 for w in workers_detail if queue in w['queues']),
                "licensed_workers": sum(1 for w in workers_detail if queue in w['queues'] and w['licensed']),
            })

        # Performance metrics from ApiTaskQueue
        performance_metrics = {
            "avg_completion_time_seconds": 0,
            "avg_wait_time_seconds": 0,
            "success_rate_percent": 100.0,
            "tasks_analyzed": 0,
            "successful_tasks": 0,
            "failed_tasks": 0,
        }
        try:
            cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

            recent_completed = worker_management_crud.list_completed_tasks_since(db, cutoff_24h, 100)

            recent_failed = worker_management_crud.list_failed_tasks_since(db, cutoff_24h, 100)

            successful_count = len(recent_completed)
            failed_count = len(recent_failed)
            total_finished = successful_count + failed_count

            # Avg completion time
            durations = []
            wait_times = []
            for t in recent_completed:
                start = t.started_at or t.claimed_at or t.created_at
                if start and t.completed_at:
                    dur = (t.completed_at - start).total_seconds()
                    if dur > 0:
                        durations.append(dur)
                if t.created_at and start:
                    wait = (start - t.created_at).total_seconds()
                    if wait >= 0:
                        wait_times.append(wait)

            performance_metrics = {
                "avg_completion_time_seconds": int(sum(durations) / len(durations)) if durations else 0,
                "avg_wait_time_seconds": int(sum(wait_times) / len(wait_times)) if wait_times else 0,
                "success_rate_percent": round((successful_count / total_finished) * 100, 1) if total_finished > 0 else 100.0,
                "tasks_analyzed": total_finished,
                "successful_tasks": successful_count,
                "failed_tasks": failed_count,
            }
        except Exception:
            pass

        # Round per-backend totals once, and strip empty backends (a
        # worker reporting has_gpu=false shouldn't cause an empty "cuda"
        # bucket to render on the Dashboard).
        gpu_by_backend_rounded = {
            kind: {
                "total_count": b["total_count"],
                "total_vram_gb": round(b["total_vram_gb"], 1),
                "licensed_count": b["licensed_count"],
                "licensed_vram_gb": round(b["licensed_vram_gb"], 1),
                # Actual VRAM usage (aggregated from worker snapshots),
                # bounded above by licensed VRAM so a late/duplicated
                # sample can't drive the tile past 100%.
                "used_vram_gb": round(min(b["used_vram_gb"], b["licensed_vram_gb"]), 1),
                "utilization_pct": round(
                    (min(b["used_vram_gb"], b["licensed_vram_gb"]) / b["licensed_vram_gb"]) * 100.0
                    if b["licensed_vram_gb"] > 0 else 0.0,
                    1,
                ),
                "devices": b["devices"],
            }
            for kind, b in gpu_by_backend.items()
            if b["total_count"] > 0
        }

        return {
            "total_capacity": {
                "cpu_cores": total_cpu,
                "memory_gb": round(total_memory_gb, 1),
                "gpu_count": total_gpu,
                "vram_gb": round(total_vram_gb, 1)
            },
            "licensed_capacity": {
                "cpu_cores": licensed_cpu,
                "memory_gb": round(licensed_memory_gb, 1),
                "gpu_count": licensed_gpu,
                "vram_gb": round(licensed_vram_gb, 1)
            },
            "gpu_by_backend": gpu_by_backend_rounded,
            "utilization": {
                "cpu_cores_used": round(cpu_cores_used, 1),
                "memory_gb_used": round(memory_gb_used, 1),
                "gpu_count_used": gpu_count_used,
                "gpu_utilization_pct": round(sum(
                    s.gpu_utilization_pct for s in snapshots.values()
                    if s.gpu_utilization_pct is not None
                ) / max(1, licensed_gpu), 1) if licensed_gpu > 0 else 0,
                "vram_gb_used": round(vram_gb_used, 1)
            },
            "performance_metrics": performance_metrics,
            "queue_health": queue_health,
            "workers_detail": workers_detail,
            "workers": workers_detail,
            "running_tasks": sum(active_task_counts.values()),
            "active_workers": sum(1 for w in all_workers if w.status == WorkerStatus.LICENSED),
            "total_pool": len(all_workers),
            "failed_tasks_24h": performance_metrics.get("failed_tasks", 0),
            # Unrunnable tasks summary — populated by the scheduler's
            # unrunnable_tasks_sweep job (every 5 min). Used by the
            # System Health banner to surface "add a worker or skip
            # fewer variants" situations.
            "unrunnable_tasks": _summarise_unrunnable_tasks(db),
        }

    except Exception as e:
        logger.error("Failed to get platform capacity: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Failed to get platform capacity"
        )


# =========================================================================
# Enrollment Tokens — pre-shared tokens that gate /workers/enroll/start
# =========================================================================

class CreateEnrollmentTokenRequest(BaseModel):
    label: str
    max_uses: Optional[int] = 1
    auto_approve: bool = False
    auto_license: bool = False
    allowed_name_pattern: Optional[str] = None
    owner_type: str = "platform_pool"
    assigned_tenant_ids: Optional[List[int]] = None
    owner_tenant_id: Optional[int] = None
    expires_in_hours: Optional[int] = 24
    # dual-anchor-mesh-federation §6.2 — "local" (site anchor) | "remote" (cloud)
    home_anchor: str = "local"


class EnrollmentTokenResponse(BaseModel):
    id: int
    label: str
    token: Optional[str] = None
    max_uses: Optional[int]
    use_count: int
    auto_approve: bool
    auto_license: bool
    allowed_name_pattern: Optional[str]
    owner_type: str
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]
    created_at: datetime
    created_by_system: bool
    # dual-anchor mesh enrollment (§6.1/6.2). mesh_authkey is returned ONCE, on
    # create, alongside the plaintext token; None when no mesh plane is wired
    # (Dashboard then emits the legacy FQDN command).
    home_anchor: str = "local"
    manager_mesh_name: Optional[str] = None
    mesh_login_server: Optional[str] = None
    mesh_authkey: Optional[str] = None


def _manager_mesh_name() -> Optional[str]:
    """The manager's stable mesh/magic name a worker dials instead of the public
    FQDN (§6.1). Explicit override wins; else <tailnet-hostname>.<magicdns base>."""
    explicit = (os.getenv("MOD_MANAGER_MESH_NAME") or "").strip()
    if explicit:
        return explicit
    host = (os.getenv("MOD_TAILNET_HOSTNAME") or "").strip() or "mod-node"
    base = (os.getenv("MOD_MESH_BASE_DOMAIN") or "mod-connect.internal").strip()
    return f"{host}.{base}"


def _mdns_discovery_enabled() -> bool:
    """ip-slide resilience (mdns-name-discovery): is the stable-name path on?
    When true, the site control plane is addressed by a STABLE mDNS name
    (mod-manager.local) instead of a DHCP IP that slides whenever the flat
    switch's northward network changes. OFF by default — the actual mDNS
    broadcast onto the LAN is stood up by the platform enabler (avahi bound to
    the physical NIC); this flag only makes MOD *emit the name* into
    worker-facing config once that broadcast is live."""
    return (os.getenv("MOD_SITE_ANCHOR_MDNS_ENABLE") or "false").strip().lower() \
        in ("1", "true", "yes", "on")


def _site_anchor_name() -> str:
    return (os.getenv("MOD_SITE_ANCHOR_NAME") or "mod-manager.local").strip()


def _site_login_server_name_based() -> str:
    """The site login server expressed as the stable NAME (never an IP), or ""
    when mDNS discovery is off. A worker that dials this resolves the manager
    link-locally via mDNS regardless of the manager's current DHCP IP, and the
    headscale-tls cert carries this name as a SAN (see mesh-headscale-tls.sh) so
    TLS still validates across IP changes."""
    if not _mdns_discovery_enabled():
        return ""
    name = _site_anchor_name()
    if not name:
        return ""
    port = (os.getenv("HEADSCALE_TLS_PORT") or "8443").strip()
    return f"https://{name}:{port}"


def _site_login_server_dns_based() -> str:
    """The site login server expressed as a STABLE, ordinary-DNS-resolved
    hostname (e.g. ``mod-headscale.<site-domain>``), or "" when unset (default —
    no behaviour change). "" until the coordinator/mesh self-heal actually keeps
    that hostname's A record pointed at the current LAN IP (a LAN split-horizon
    dnsmasq answer via lan-dns.sh, or an authoritative record the reconcile step
    updates) — see graceful-worker-readd-design.md.

    Distinct from BOTH other stable-address paths:
      - `_site_login_server_name_based` (mDNS `.local`): needs an mDNS/avahi
        broadcaster bound to the physical LAN NIC, which this stack does not yet
        stand up (see that function's docstring). This one needs nothing beyond
        the DNS resolution every worker already does.
      - MagicDNS / headscale split-DNS (`oa_headscale_split_dns`): that only
        resolves names for nodes ALREADY joined to a tailnet, so it cannot
        bootstrap the very login-server address a worker dials to join in the
        first place. This hostname is resolved via CONVENTIONAL DNS, before any
        tailscale join happens.

    Ranked ahead of the raw `MOD_SITE_LOGIN_SERVER_EXTERNAL` IP in
    `_anchor_login_server`: a name a worker re-resolves is strictly more stable
    than an IP a worker must re-authenticate against on every change, even
    though both are kept current by the same LAN-IP self-heal today."""
    name = (os.getenv("MOD_SITE_ANCHOR_DNS_NAME") or "").strip()
    if not name:
        return ""
    if name.startswith("http://") or name.startswith("https://"):
        return name
    port = (os.getenv("HEADSCALE_TLS_PORT") or "8443").strip()
    return f"https://{name}:{port}"


def _public_control_plane_login_server(db=None) -> str:
    """FX.31 s1b — thin delegate to the ONE shared decision
    (`services.mesh_public_vhost_nginx.public_control_plane_login_server`): the
    public control-plane URL when `mesh.public_control_plane` is ON, "" when it
    is OFF (the vhost is not rendered, so a worker dialing it would fail).
    Kept here only so both join resolvers (this module's and
    `routers/persistent_stacks/_shared`'s) read the SAME helper — the setting
    itself is never re-implemented in either."""
    # 1788d CE carve: services/mesh_public_vhost_nginx.py is paid (manifest deny
    # `CORE/API/**/mesh_*.py`). No public control-plane vhost in CE, so the
    # resolver answers "" - the same value the setting's OFF default gives.
    from utils.paid_import import optional_paid
    _vhost = optional_paid("services.mesh_public_vhost_nginx")
    if _vhost is None:
        return ""
    return _vhost.public_control_plane_login_server(db=db) or ""


def _anchor_login_server_with_db(home_anchor: str, db=None) -> str:
    """Arity-tolerant call of `_anchor_login_server` that passes the caller's
    `db` when the live resolver accepts it (FX.31 s1b added the optional second
    parameter so the resolver can read `mesh.public_control_plane` in the
    caller's transaction). Tests monkeypatch the resolver with its original 1-arg
    shape (test_worker_site_mesh), so a 1-arg resolver is called unchanged —
    same arity-detection pattern `_shared._call_minter` uses."""
    import inspect
    try:
        nparams = len(inspect.signature(_anchor_login_server).parameters)
    except (TypeError, ValueError):
        nparams = 1
    return _anchor_login_server(home_anchor, db) if nparams >= 2 \
        else _anchor_login_server(home_anchor)


def _anchor_login_server(home_anchor: str, db=None) -> str:
    """Headscale login server for the home anchor. local → the site control
    plane on the manager; remote → the fixed cloud rendezvous.
    NOTE: on a dual-anchor box MOD_TAILNET_LOGIN_SERVER is the CLOUD url, so the
    local branch prefers the site-specific vars first (MOD_TAILNET_SITE_LOGIN_SERVER
    is the name start-main-server actually writes, e.g. https://headscale-tls:8443).

    GSEC FX.31 s1b — the site branch asks the ONE shared public-control-plane
    decision (`services.mesh_public_vhost_nginx.public_control_plane_login_server`
    via `_public_control_plane_login_server`, FX.31 s1 found NO join path read
    `mesh.public_control_plane` at all) when the joining worker is OFF-SITE
    (the install advertises an externally-reachable site address,
    `MOD_SITE_LOGIN_SERVER_EXTERNAL` — the shared helper checks it). Then the
    LAN address it would otherwise hand out
    can never be resolved (FX.31 p1a keeps every headscale/headscale-tls publish
    127.0.0.1-or-LAN), so the worker is handed the public vhost URL
    `https://mesh-control.<BASE_DOMAIN>:443` instead and joins through nginx.
    Option OFF (the default) → the helper answers "" and the chain below is
    byte-for-byte the pre-FX.31 behaviour; a co-located/LAN install (no
    off-site address advertised) is untouched even with the option ON."""
    if (home_anchor or "local").strip().lower() == "remote":
        return (os.getenv("MOD_HOSTED_LOGIN_SERVER")
                or os.getenv("MOD_CLOUD_LOGIN_SERVER")
                or os.getenv("MOD_TAILNET_LOGIN_SERVER") or "").strip()
    # local: an OFF-HOST worker must dial an externally-reachable address, NOT the
    # in-network name (headscale-tls:8443) the manager's own sidecars use.
    # Preference order (most stable first — see graceful-worker-readd-design.md):
    #   0. mesh.public_control_plane ON + an off-site address advertised → the
    #      public vhost (FX.31 s1b; the LAN address is unreachable off-site).
    #   1. mDNS name-discovery (_site_login_server_name_based), when enabled —
    #      needs an avahi broadcaster this stack does not yet stand up.
    #   2. MOD_SITE_ANCHOR_DNS_NAME (_site_login_server_dns_based) — an
    #      ordinary-DNS hostname, when an operator/self-heal has one that
    #      actually resolves to the current LAN IP. Off (empty) by default, so
    #      this is a pure opt-in with no behaviour change until wired.
    #   3. MOD_SITE_LOGIN_SERVER_EXTERNAL (e.g. https://<manager-LAN-IP>:8443) —
    #      the raw-IP fallback the LAN-IP self-heal keeps current today; a
    #      worker must re-authenticate (mesh_rehome) whenever this rotates.
    #   4/5. legacy in-network name / HEADSCALE_LOGIN_SERVER for co-located /
    #      single-box use. On WSL the external address needs a Windows
    #      port-forward to :8443.
    public = _public_control_plane_login_server(db)
    if public:
        return public
    return (_site_login_server_name_based()
            or _site_login_server_dns_based()
            or os.getenv("MOD_SITE_LOGIN_SERVER_EXTERNAL")
            or os.getenv("MOD_SITE_LOGIN_SERVER")
            or os.getenv("MOD_TAILNET_SITE_LOGIN_SERVER")
            or os.getenv("HEADSCALE_LOGIN_SERVER") or "").strip()


def _mesh_rehome_directive(worker, db=None) -> Optional[dict]:
    """Worker re-home automation (mdns-name-discovery §3): hand a known worker the
    CURRENT site login server so it re-homes AUTOMATICALLY when the site-anchor
    address slides (every box gets DHCP and its IP can change on a flat switch) or
    when the manager takes over DHCP. Registry-driven + heartbeat-delivered — the
    same pull model as `paused`/`drain`: every worker that heartbeats receives it,
    so no separate push channel to each worker is needed.

    The worker compares `epoch` to its last-applied value and, on a change, re-runs
    `tailscale up --login-server=<site_login_server> --force-reauth`; an unchanged
    epoch is a no-op. `epoch` is a stable short hash of the effective address, so
    it changes EXACTLY when the address the worker must dial changes (IP slide, or
    IP→name switch) — no DB column or migration required.

    INERT by default: returns None unless MOD_WORKER_REHOME_ENABLE is truthy, so
    nothing is emitted on the live box until the flag is flipped. Best-effort:
    never let a lookup error break the heartbeat."""
    if (os.getenv("MOD_WORKER_REHOME_ENABLE") or "false").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return None
    try:
        # Only re-home workers whose home plane is the (movable) site anchor; a
        # cloud-homed worker dials a fixed public name that never slides.
        home = (getattr(worker, "network_zone", "local") or "local").strip().lower()
        if home == "remote":
            return None
        site = _anchor_login_server_with_db("local", db)
        if not site:
            return None
        import hashlib
        epoch = hashlib.sha256(site.encode("utf-8")).hexdigest()[:16]
        return {"site_login_server": site, "epoch": epoch,
                "reason": "site-anchor-address"}
    except Exception:  # noqa: BLE001
        return None


_SITE_MESH_NEEDS_KEY = {"absent", "moved", "NeedsLogin", "NoState", "NeedsMachineAuth"}
_SITE_MESH_MINT_BASE_S = 300
_SITE_MESH_MINT_MAX_S = 6 * 3600


def _sanitize_site_mesh(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Keep only the known string fields of a worker's `site_mesh` report,
    trimmed and length-capped (it is stored on hardware_specs)."""
    if not isinstance(report, dict):
        return None
    out = {}
    for k in ("mode", "state", "ip", "login_server", "error", "last_error", "last_error_at"):
        v = report.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:300]
    return out or None


# gw-stack-networking GW.2c — cap per protocol for host_bound_ports. A worker
# that reports more distinct ports than this is either broken or hostile; the
# cap keeps hardware_specs bounded while a legitimate stack-heavy host (a few
# hundred published ports at most) is far below it.
_HOST_BOUND_PORTS_CAP = 4096


def _sanitize_host_bound_ports(raw: Any) -> Dict[str, List[int]]:
    """Sanitize a worker's `host_bound_ports` report for hardware_specs.

    Untrusted worker input: protocols are limited to the tcp/udp the placement
    checker understands, each value must be an int in 1..65535 (bools excluded
    — a JSON true/false would otherwise pass `isinstance(x, int)`), duplicates
    are collapsed, and each protocol list is capped at
    `_HOST_BOUND_PORTS_CAP` in input order. Empty result ({}) is a legitimate
    "nothing bound" report and is distinct from an ABSENT field (see
    WorkerHeartbeatRequest.host_bound_ports).
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[int]] = {}
    for proto in ("tcp", "udp"):
        vals = raw.get(proto)
        if not isinstance(vals, list):
            continue
        seen: set = set()
        ports: List[int] = []
        for v in vals:
            if isinstance(v, bool) or not isinstance(v, int):
                continue
            if not (1 <= v <= 65535) or v in seen:
                continue
            seen.add(v)
            ports.append(v)
            if len(ports) >= _HOST_BOUND_PORTS_CAP:
                break
        if ports:
            out[proto] = ports
    return out


def _sanitize_host_bound_ports_by_project(raw: Any) -> Dict[str, Dict[str, List[int]]]:
    """Sanitize a worker's `host_bound_ports_by_project` report for
    hardware_specs (the per-project attribution of `host_bound_ports`).

    Shape: {project: {"tcp": [8080], "udp": [53]}}. The inner per-protocol
    value reuses `_sanitize_host_bound_ports` verbatim (same tcp/udp-only,
    int-in-1..65535, dedupe, cap rules); the outer project key is a trimmed
    string of bounded length, and the number of projects is capped (a
    broken/hostile worker must not grow hardware_specs without bound).
    Non-stack containers report under the "" project key, which is kept —
    the manager only special-cases a redeploying stack's OWN project. An
    empty result ({}) is a legitimate "nothing bound" report, distinct from
    an ABSENT field (absent is the carry-forward signal, see the ingest).
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, List[int]]] = {}
    for project, by_proto in raw.items():
        name = str(project).strip()[:64]
        inner = _sanitize_host_bound_ports(by_proto)
        if inner:
            out[name] = inner
        if len(out) >= _HOST_BOUND_PORTS_CAP:
            break
    return out


def _site_mesh_enabled() -> bool:
    return (os.getenv("MOD_WORKER_SITE_MESH_ENABLE") or "true").strip().lower() \
        in ("1", "true", "yes", "on")


def _mesh_site_directive(report: Optional[Dict[str, str]], mint_state: Optional[dict],
                         now: datetime, db: Optional[Session] = None
                         ) -> Tuple[Optional[dict], Optional[dict]]:
    """The `mesh_site` heartbeat directive and the updated mint bookkeeping.

    Every beat carries the CURRENT site login server + an epoch hash of it, so a
    worker whose site client points elsewhere re-points itself (keeping its
    identity). A single-use, short-lived preauth key is added ONLY when the
    worker's last report says its client cannot proceed without one (never
    joined, logged out — e.g. by the pre-v1.0.70 keyless --force-reauth — or its
    node was deleted). Mints back off exponentially per worker (5 min doubling to
    6 h) while the worker keeps failing to use them, e.g. a worker that is off
    the site LAN; the backoff resets once it reports Running.

    Returns (directive|None, mint_state|None). Best-effort: never raises."""
    if not _site_mesh_enabled():
        return None, mint_state
    try:
        login = _anchor_login_server_with_db("local", db)
        if not login:
            return None, mint_state
        import hashlib
        directive = {"login_server": login,
                     "epoch": hashlib.sha256(login.encode("utf-8")).hexdigest()[:16]}
        if not report:
            return directive, mint_state
        state = report.get("state") or ""
        if state == "Running":
            return directive, None
        if state not in _SITE_MESH_NEEDS_KEY or report.get("mode") not in ("host", "managed"):
            return directive, mint_state
        n, last = 0, None
        if isinstance(mint_state, dict):
            try:
                n = int(mint_state.get("n") or 0)
                last = datetime.fromisoformat(str(mint_state.get("at")))
            except (TypeError, ValueError):
                n, last = 0, None
        wait = min(_SITE_MESH_MINT_BASE_S * (2 ** max(n - 1, 0)), _SITE_MESH_MINT_MAX_S)
        if last is not None and n > 0 and (now - last).total_seconds() < wait:
            return directive, mint_state
        key = _mint_worker_mesh_key("local", reusable=False, db=db)
        if not key:
            return directive, mint_state
        directive["authkey"] = key
        return directive, {"at": now.isoformat(), "n": n + 1}
    except Exception:  # noqa: BLE001
        logger.exception("mesh_site directive failed")
        return None, mint_state


def _mint_worker_mesh_key(home_anchor: str, *, reusable: bool,
                          ttl_hours: Optional[float] = None,
                          db: Optional[Session] = None) -> Optional[str]:
    """Mint a worker preauth key against the home anchor's Headscale plane, or
    None when the plane is unconfigured (→ FQDN fallback). Tags drive the ACLs.

    `ttl_hours=None` (the default for every single-use caller) resolves to the
    GSEC FX.31 p2a `mesh.preauth_key_ttl_minutes` platform setting (~1h) via
    `headscale_client.preauth_key_ttl_minutes()`. The cloud-burst enrollment
    token caller passes an explicit `ttl_hours` alongside `reusable=True`
    (documented there) because that key intentionally outlives one join."""
    _hs = optional_paid("utils.headscale_client")
    if _hs is None:
        return None
    client = _hs.get_headscale_client_for_anchor(home_anchor)
    if client is None:
        return None
    if ttl_hours is None:
        ttl_hours = _hs.preauth_key_ttl_minutes(db) / 60.0
    try:
        client.ensure_user("workers")
        return client.mint_preauth_key(
            "workers", ["tag:worker", f"tag:home-{home_anchor}"],
            reusable=reusable, ttl_hours=ttl_hours)
    except _hs.HeadscaleError as e:
        logger.warning("worker mesh key mint failed (anchor=%s): %s",
                       home_anchor, e)
        return None


@router.post("/workers/enrollment-tokens", response_model=EnrollmentTokenResponse)
async def create_enrollment_token(
    request: CreateEnrollmentTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a pre-shared enrollment token. Platform admin only.

    Manual flow:  max_uses=1, auto_approve=False
    Cloud burst:  max_uses=N, auto_approve=True, auto_license=True, allowed_name_pattern="burst-.*"
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform admins can create enrollment tokens")

    home_anchor = (request.home_anchor or "local").strip().lower()
    if home_anchor not in ("local", "remote"):
        raise HTTPException(status_code=400,
                            detail="home_anchor must be 'local' or 'remote'")

    token_plain = py_secrets.token_urlsafe(32)
    token_hash = hash_api_key(token_plain)

    expires_at = None
    if request.expires_in_hours:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=request.expires_in_hours)

    enrollment_token = worker_management_service.create_enrollment_token_row(
        db,
        label=request.label,
        created_by_user_id=current_user.id,
        max_uses=request.max_uses,
        auto_approve=request.auto_approve,
        auto_license=request.auto_license,
        allowed_name_pattern=request.allowed_name_pattern,
        owner_type=request.owner_type,
        assigned_tenant_ids=request.assigned_tenant_ids,
        owner_tenant_id=request.owner_tenant_id,
        token_hash=token_hash,
        expires_at=expires_at,
        home_anchor=home_anchor,
    )

    # Mint the worker's mesh preauth key against the chosen anchor's Headscale
    # plane and hand back the login server + magic name so the Dashboard can bake
    # them into the setup command. When no plane is wired the fields stay None and
    # the Dashboard emits the legacy FQDN command.
    manager_mesh_name = _manager_mesh_name()
    mesh_login_server = _anchor_login_server_with_db(home_anchor, db) or None
    mesh_authkey = None
    if mesh_login_server:
        # GSEC FX.31 p2a — INTENTIONALLY reusable (left unchanged) when
        # max_uses > 1: cloud-burst enrollment tokens share one preauth key
        # across N workers by design (see the docstring above), so this one
        # mint site does not go through default_mint_kwargs()/the ~1h
        # setting-driven TTL; its TTL stays admin-supplied (expires_in_hours)
        # since the key must outlive more than a single join.
        mesh_authkey = _mint_worker_mesh_key(
            home_anchor,
            reusable=bool(request.max_uses and request.max_uses > 1),
            ttl_hours=(request.expires_in_hours or 24))

    return EnrollmentTokenResponse(
        id=enrollment_token.id,
        label=enrollment_token.label,
        token=token_plain,
        max_uses=enrollment_token.max_uses,
        use_count=0,
        auto_approve=enrollment_token.auto_approve,
        auto_license=enrollment_token.auto_license,
        allowed_name_pattern=enrollment_token.allowed_name_pattern,
        owner_type=request.owner_type,
        expires_at=enrollment_token.expires_at,
        revoked_at=None,
        created_at=enrollment_token.created_at,
        created_by_system=False,
        home_anchor=home_anchor,
        manager_mesh_name=manager_mesh_name if mesh_authkey else None,
        mesh_login_server=mesh_login_server if mesh_authkey else None,
        mesh_authkey=mesh_authkey,
    )


@router.get("/workers/enrollment-tokens", response_model=List[EnrollmentTokenResponse])
async def list_enrollment_tokens(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all enrollment tokens. Platform admin only. Never returns the raw token."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform admins can list enrollment tokens")

    tokens = worker_management_service.list_enrollment_tokens_view(db)

    return [
        EnrollmentTokenResponse(
            id=t.id,
            label=t.label,
            token=None,
            max_uses=t.max_uses,
            use_count=t.use_count,
            auto_approve=t.auto_approve,
            auto_license=t.auto_license,
            allowed_name_pattern=t.allowed_name_pattern,
            owner_type=t.owner_type.value if isinstance(t.owner_type, WorkerOwnerType) else str(t.owner_type),
            expires_at=t.expires_at,
            revoked_at=t.revoked_at,
            created_at=t.created_at,
            created_by_system=t.created_by_system,
            # mesh_authkey is a create-time one-shot; a list never re-exposes it.
            home_anchor=getattr(t, "home_anchor", "local") or "local",
        )
        for t in tokens
    ]


@router.delete("/workers/enrollment-tokens/{token_id}", response_model=RevokeEnrollmentTokenResponse, response_model_exclude_unset=True)
async def revoke_enrollment_token(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke an enrollment token. Platform admin only."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Only platform admins can revoke enrollment tokens")

    worker_management_service.revoke_enrollment_token_row(db, token_id)

    return {"message": "Enrollment token revoked", "token_id": token_id}


# Internal helper for system-created tokens (cloud burst)
def create_system_enrollment_token(
    db: Session,
    label: str,
    max_uses: int,
    auto_approve: bool = True,
    auto_license: bool = True,
    allowed_name_pattern: Optional[str] = None,
    owner_type: WorkerOwnerType = WorkerOwnerType.PLATFORM_POOL,
    expires_in_hours: int = 2,
    home_anchor: str = "local",
) -> str:
    """
    Create an enrollment token programmatically (no user auth needed).

    Used by cloud burst orchestrators, provisioning DAGs, etc. Cloud-burst
    workers pass home_anchor="remote" so they home to the fixed cloud plane.
    Returns the plaintext token.
    """
    home_anchor = (home_anchor or "local").strip().lower()
    if home_anchor not in ("local", "remote"):
        home_anchor = "local"

    token_plain = py_secrets.token_urlsafe(32)
    token_hash = hash_api_key(token_plain)

    enrollment_token = WorkerEnrollmentToken(
        token_hash=token_hash,
        label=label,
        created_by_user_id=None,
        created_by_system=True,
        max_uses=max_uses,
        use_count=0,
        auto_approve=auto_approve,
        auto_license=auto_license,
        allowed_name_pattern=allowed_name_pattern,
        owner_type=owner_type,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
        home_anchor=home_anchor,
    )
    db.add(enrollment_token)
    db.commit()
    db.refresh(enrollment_token)

    return token_plain


@router.get("/workers/{worker_id}/hardware-profile", response_model=WorkerHardwareProfileResponse, response_model_exclude_unset=True)
async def get_worker_hardware_profile(
    worker_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get hardware profile + benchmark scores for a worker."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    # CPU architecture is surfaced from the heartbeat rather than the
    # benchmark profile — the profile describes how FAST this machine is, and
    # architecture decides what it can run at all. The scheduler now
    # withholds work a worker's arch cannot execute, so the detail panel has
    # to be able to say which arch it is; without it the only symptom of a
    # correctly-gated worker is one that never picks anything up. Normalised
    # defensively for rows written before ingest-time normalisation existed
    # (older heartbeats stored the raw spelling). hardware_tags is surfaced
    # separately so the Dashboard can show the worker's actual detected
    # accelerators even when the per-GPU marketing name / VRAM number isn't
    # available (nvidia-smi/rocm-smi not installed, DRI fallback only
    # yielded vendor-level tags, operator-provided WORKER_HARDWARE_TAGS...).
    _profile_view = worker_management_service.get_worker_hardware_profile_view(
        db, worker_id, _wm_normalize_arch,
    )
    # 25.5.s5 — read-only: the worker's last self-cleanup pass RESULT
    # (hardware_specs['last_cleanup'], written by the heartbeat ingest above).
    # Merged here rather than in the service so this stays the only place the
    # heartbeat's cleanup vocabulary is understood; the profile panel already
    # mixes heartbeat data (arch, hardware_tags) in beside the benchmark
    # profile, and "what the box's own GC last got back" belongs to that same
    # "what this machine is/has" answer. Absent key = never reported.
    _lc = (worker_management_service.get_worker_by_id_or_404(db, worker_id).hardware_specs
           or {}).get("last_cleanup")
    if _lc:
        _profile_view["last_cleanup"] = _lc
    # MAC.1.s13b — read-only beside the cleanup reading: the worker's pre-claim
    # free-disk gate ({state, free_gb, needed_gb, work_root}). This is the
    # answer to "why is this green worker claiming nothing?" — the panel already
    # mixes heartbeat data (arch, hardware_tags, last_cleanup) in beside the
    # benchmark profile, and "what the box's own GC last got back" belongs to
    # that same "what this machine is/has" answer. Absent key = never probed, so
    # the manager never reads "the volume is fine" for a worker that has not
    # measured yet (same omission rule the Go side uses).
    _df = (worker_management_service.get_worker_by_id_or_404(db, worker_id).hardware_specs
           or {}).get("disk_floor")
    if _df:
        _profile_view["disk_floor"] = _df
    return _profile_view


@router.get("/workers/{worker_id}/gpu-usage")
async def get_worker_gpu_usage(
    worker_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """megaplan 14.8 — per-card reserved-vs-typical VRAM for the Workers
    page's per-card bar. Platform admin only (same inline gate as the
    sibling `/workers/{worker_id}/hardware-profile` endpoint above).

    Returns ``{worker_id, worker_name, cards: [{card_id, kind, total_mb,
    live_used_mb, tasks: [{task_id, node_kind, mode, reserved_mb,
    typical_peak_mb, over_budget}]}]}``. ``typical_peak_mb`` is a *learned*
    figure (last COMPLETED runs' peak VRAM for that node_kind), not live
    telemetry — the worker only reports peak_vram_mb at task completion.
    ``live_used_mb`` is only non-null for single-GPU workers, where the
    heartbeat's worker-aggregate VRAM usage unambiguously belongs to that
    one card.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return worker_management_service.get_worker_gpu_usage_view(db, worker_id)


@router.post("/workers/{worker_id}/benchmark", response_model=TriggerBenchmarkResponse, response_model_exclude_unset=True)
async def trigger_benchmark(
    worker_id: int,
    only: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger a hardware benchmark for a worker. Platform admin only.

    Query params:
    - `only`: run just one benchmark node instead of the full chain.
      - `"system"` → system benchmark only (CPU/RAM/I/O/MinIO/Docker).
      - `"<kind>:<index>"` (e.g. `"cuda:0"`, `"rocm:1"`) → one accelerator.
      - Omitted → full benchmark (system + all accelerators, sequential).
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    # Parse/validate the `only` selector up front so the dispatcher gets a
    # normalized form. None means "run everything".
    only_selector: Optional[str] = None
    only_kind: Optional[str] = None
    only_index: Optional[int] = None
    if only:
        o = only.strip().lower()
        if o == "system":
            only_selector = "system"
        elif ":" in o:
            k, _, i = o.partition(":")
            # Accept any accelerator-kind string the operator typed, so
            # selecting `only=habana:0` or `only=tpu:1` works without a
            # closed list. Shape check is the same as wizard_metrics:
            # lowercase alnum/underscore/dash, max 24 chars.
            import re as _accel_only_re
            if not (k and _accel_only_re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", k)) or not i.isdigit():
                raise HTTPException(status_code=400, detail=f"Invalid 'only' value: {only!r}")
            only_selector, only_kind, only_index = f"{k}:{i}", k, int(i)
        else:
            raise HTTPException(status_code=400, detail=f"Invalid 'only' value: {only!r}")

    # Fetches the worker + hardware profile, resets a stale "running" status
    # back to "pending" when no active profiling run backs it (409s instead if
    # one does), and resets each targeted/non-quarantined accelerator's status
    # to "pending" — see `services.worker_management_service.
    # prepare_benchmark_trigger` for the full behaviour (moved verbatim).
    worker, profile, accel_devs = worker_management_service.prepare_benchmark_trigger(
        db, worker_id,
        only_selector=only_selector, only_kind=only_kind, only_index=only_index,
    )

    _auto_dispatch_profiling(db, worker, profile, accel_devs, only=only_selector)
    # AI scheduler — manual re-benchmark also re-establishes a hardware
    # profile, so refire the calibration auto-queue. Same gate as the
    # heartbeat path (LICENSED non-embedded + AUTO_CALIBRATE_NEW_PROFILES
    # + capacity floor); audit event records trigger_source='manual_benchmark'
    # so the two paths are distinguishable in the log.
    _auto_calibrate_for_profile_hook(
        db, worker, profile, trigger_source="manual_benchmark",
    )
    return {"status": "dispatched", "worker_name": worker.worker_name, "only": only_selector}
