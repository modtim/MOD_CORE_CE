"""Business logic for the worker_management domain (openspec:
stack-training-and-prewarm, Gate 2 per-domain migration, D9).

Owns policy + the transaction boundary for `routers/worker_management.py`;
all data access is delegated to `crud/worker_management.py`. Domain errors are
raised as plain `fastapi.HTTPException` (not `services._base.ServiceError`) —
deliberately, so the exact status codes + detail shapes the router used to
construct inline are preserved byte-for-byte (the contract-snapshot safety
net this migration is verified against, per Gate 0's D3).

Gate 2b: `worker_heartbeat` and `get_platform_capacity` (plus the
`_summarise_unrunnable_tasks` / `_match_hardware_profile` /
`_auto_dispatch_profiling` / `_auto_calibrate_for_profile_hook` /
`_generate_benchmark_hmac_key` / `_verify_benchmark_signature` helpers they
share with the already-migrated `trigger_benchmark`) had their `db.query`
reads moved into `crud.worker_management` in this pass, but are STILL
intentionally NOT backed by this service module — their surrounding policy
stays physically in `routers/worker_management.py`, thinned only at the
data-access layer. Two reasons, both hard blockers on a real service split:

1. `worker_heartbeat` is hot-path with several independent, deliberately
   UN-atomic commit/rollback blocks (claim renewal, NOAH availability,
   resource-snapshot, system_state) — see the extensive inline comments on
   each block in the router for the incidents that shaped that structure.
   Splitting the policy out risks reordering or merging those transaction
   boundaries, which the task explicitly forbids for this handler ("pure
   move" only).
2. Both handlers are pinned by `inspect.getsource(worker_heartbeat)` /
   `inspect.getsource(get_platform_capacity)` regression guards scattered
   across tests/test_worker_heartbeat_guard.py, tests/test_worker_tasks.py,
   tests/test_phase3_comprehensive.py and tests/test_recent_fixes.py, which
   assert on literal substrings of the *router* function's own source
   (e.g. `"mod-worker-embedded" in source`, `"WorkerResourceTimeline" in
   source`, `'"drain": server_updating or worker_paused...' in source`).
   Moving that logic into this service module would silently orphan those
   guards from the code they're meant to pin.

A handful of shared non-route helpers that BOTH those deferred handlers and
the migrated ones call (`discover_workers`, `mark_offline_workers`,
`pause_worker`, `resume_worker`, the last-active-worker guard trio, plus the
Gate 2b hardware-profile/benchmark helpers above) stay physically in the
router too, thinned to delegate their own `db.query` down to
`crud.worker_management` directly — they are not endpoints, so there is
nothing here for them to route through.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import crud.worker_management as crud
from models import (
    WorkerEnrollment,
    WorkerEnrollmentStatus,
    WorkerEnrollmentToken,
    WorkerLicense,
    WorkerOwnerType,
    WorkerStatus,
)

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')


# =============================================================================
# Reserved worker names (the manager-node / embedded worker's identity)
# =============================================================================

# SINGLE source of truth. `mod-worker-embedded` is the embedded worker's name in
# EVERY edition; `mod-worker-ce` is its legacy CE alias (main._EMBEDDED_WORKER_
# SEED_NAMES must equal this; tests pin that and the client scripts'
# `is_reserved_worker_name` list to it). Both rows are seeded at API startup
# with the embedded worker's own Vault bootstrap-token hash, so a client-driven
# enrollment under either name would rotate that hash and lock the embedded
# worker out (or hand its identity to a token holder).
RESERVED_WORKER_NAMES = ("mod-worker-embedded", "mod-worker-ce")


def is_reserved_worker_name(name: Optional[str]) -> bool:
    """True when `name` (trimmed, case-insensitive, exact match) is reserved."""
    return (name or "").strip().casefold() in RESERVED_WORKER_NAMES


def reject_reserved_worker_name(name: Optional[str]) -> None:
    """Raise 400 when a client-chosen name for a NEW remote worker is reserved.

    NOT applied to the embedded worker's own startup path (the API's
    `_startup_seed_embedded_worker_enrollment` + its heartbeat), which never goes
    through enrollment."""
    if is_reserved_worker_name(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Worker name '{(name or '').strip()}' is reserved for the "
                "manager-node embedded worker and cannot be enrolled. "
                "Choose a different worker name."
            ),
        )


# =============================================================================
# Small shared lookups (404-raising) used by many handlers below
# =============================================================================


def get_worker_or_404(db: Session, worker_name: str) -> WorkerLicense:
    worker = crud.get_worker_by_name(db, worker_name)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


def get_worker_by_id_or_404(db: Session, worker_id: int) -> WorkerLicense:
    worker = crud.get_worker_by_id(db, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


def get_enrollment_or_404(
    db: Session, worker_name: str, *, detail: str = "Enrollment not found"
) -> WorkerEnrollment:
    enrollment = crud.get_enrollment_by_name(db, worker_name)
    if not enrollment:
        raise HTTPException(status_code=404, detail=detail)
    return enrollment


# =============================================================================
# Enrollment-token validation (backs `_validate_enrollment_token`)
# =============================================================================


def validate_enrollment_token(
    db: Session, token_plain: str, worker_name: str
) -> WorkerEnrollmentToken:
    """Validate an enrollment token against every un-revoked token's hash.
    Returns the matched token row or raises HTTPException (400/401/403).

    A reserved (embedded-worker) name is refused (400) before the token is even
    checked, so every caller — enroll/start, enroll/validate-token and the
    enroll/status enrollment-token poll — shares the one rule."""
    import re

    from security import verify_api_key

    reject_reserved_worker_name(worker_name)

    all_tokens = crud.list_unrevoked_enrollment_tokens(db)

    matched_token = None
    for t in all_tokens:
        if verify_api_key(token_plain, t.token_hash):
            matched_token = t
            break

    if not matched_token:
        raise HTTPException(status_code=401, detail="Invalid enrollment token")

    if matched_token.expires_at:
        expires = matched_token.expires_at
        now = datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            raise HTTPException(status_code=401, detail="Enrollment token has expired")

    if matched_token.max_uses is not None and matched_token.use_count >= matched_token.max_uses:
        raise HTTPException(status_code=401, detail="Enrollment token has reached max uses")

    if matched_token.allowed_name_pattern:
        if not re.fullmatch(matched_token.allowed_name_pattern, worker_name):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Worker name '{worker_name}' does not match allowed "
                    f"pattern '{matched_token.allowed_name_pattern}'"
                ),
            )

    return matched_token


# =============================================================================
# Enrollment lifecycle
# =============================================================================


def _seed_max_concurrent_tasks() -> int:
    """CE-only seed for a freshly token-enrolled remote worker.

    MOD CE ships no quantity caps (owner rule), so a fresh CE remote worker
    seeds 0 = unlimited (the DB column default; the Go worker's
    `effectiveConcurrency` + `maxConc <= 0` gate and the API claim guard in
    routers/worker_tasks.py already read 0 as unlimited). Paid keeps the
    historical seed of 1 byte-for-byte. The edition probe is asked at
    enrolment time (never frozen at import) and ANY failure returns the PAID
    value, so a broken probe can never silently loosen a paid cap. An
    explicit value set by an admin (PATCH /workers/{name}/concurrency) still
    wins on both editions - this is a seed, not a cap.
    """
    try:
        from utils.paid_import import is_ce_edition
        ce = bool(is_ce_edition())
    except Exception:  # noqa: BLE001 — fail to the PAID seed
        ce = False
    return 0 if ce else 1


def enroll_worker_start(
    db: Session,
    *,
    worker_name: str,
    hostname: Optional[str],
    fingerprint: Optional[dict],
    enrollment_token: WorkerEnrollmentToken,
    approval_code: str,
    bootstrap_token_hash: str,
    now: datetime,
    enforce_node_limit: Callable[[Session], None],
) -> WorkerEnrollment:
    """Create-or-update the `WorkerEnrollment` row for `/workers/enroll/start`,
    auto-license when the token says to, and bump the token's use count.

    `enforce_node_limit` is the router's `_enforce_worker_node_limit` passed
    in rather than imported here — it stays defined in the router (tests
    patch/call it there directly) and the service layer must not import
    upward from `routers.*`.
    """
    # Defence in depth: the router already refuses a reserved name via
    # validate_enrollment_token, but this is the function that would rotate the
    # embedded worker's bootstrap hash, so it re-checks for any direct caller.
    reject_reserved_worker_name(worker_name)

    initial_status = WorkerEnrollmentStatus.PENDING
    approved_at = None
    if enrollment_token.auto_approve:
        initial_status = WorkerEnrollmentStatus.APPROVED
        approved_at = now

    existing = crud.get_enrollment_by_name(db, worker_name)
    if existing:
        # Re-enrolment of an existing worker_name is a legitimate flow — see
        # the per-state policy documented on the original handler:
        #   APPROVED  — preserve status + approved_at + approved_by_user_id.
        #   PENDING   — remain PENDING unless the token auto-approves.
        #   REVOKED   — refuse (409); an operator must reinstate/delete first.
        if existing.status == WorkerEnrollmentStatus.REVOKED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Worker '{worker_name}' is REVOKED. An operator must "
                    "reinstate or delete the enrolment before re-enrolling."
                ),
            )
        existing.hostname = hostname or existing.hostname
        existing.fingerprint = fingerprint or existing.fingerprint
        existing.approval_code = approval_code
        existing.bootstrap_token_hash = bootstrap_token_hash
        existing.last_seen_at = now
        existing.enrollment_token_id = enrollment_token.id
        if existing.status == WorkerEnrollmentStatus.APPROVED:
            # Intentionally DO NOT touch status / approved_at / approved_by_user_id.
            pass
        else:
            existing.status = initial_status
            existing.approved_at = approved_at
            existing.approved_by_user_id = None
        db.commit()
        enrollment = existing
    else:
        # NEW worker -> it adds a node. Gate on the licensed worker-node count
        # before creating the enrollment. Re-enrollment (above) is exempt.
        #
        # BUG-1029 SECURITY MUST: the gate runs BEFORE the revive lookup below
        # on purpose. A soft-deleted (tombstoned) worker is invisible to the
        # LIVE-only getters, so it no longer counts toward the node total;
        # reviving it adds a node exactly like a fresh INSERT does and MUST
        # pass the same `enforce_node_limit` check. Never move this call
        # after (or inside) the revive branch.
        enforce_node_limit(db)
        # BUG-1029-fix2 (SECURITY, cross-tenant leak): a tombstone still OWNS
        # its `worker_name`, and the revive below inherits whatever tenant
        # ownership + retained history that row carries. Revive is therefore
        # allowed ONLY when the tombstone's `owner_tenant_id` equals the
        # token's (NULL == NULL, i.e. platform pool). On a mismatch we neither
        # revive nor INSERT — `worker_name` is PLAIN unique, so the name is
        # already taken by another owner's deleted worker.
        has_tombstone, tomb_owner = crud.get_tombstoned_owner_tenant_id(
            db, worker_name
        )
        # A tombstone with a NULL owner is platform-pool — it carries no
        # tenant's ownership/history to leak, so it stays revivable by any
        # token (the pre-existing BUG-1029-B1 flow). The leak is exactly the
        # case below: the tombstone DOES name an owner and it is not the
        # token's owner.
        if has_tombstone and tomb_owner is not None and tomb_owner != enrollment_token.owner_tenant_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"worker name is held by a deleted worker of a different "
                    "owner; choose another name"
                ),
            )
        # BUG-1029-B1 — `worker_enrollments.worker_name` is PLAIN unique (FK
        # target), so a decommissioned worker's tombstone still OWNS the name:
        # a plain INSERT of the re-enrolled name 500s on IntegrityError.
        # Revive the tombstone first (clears deleted_at/deleted_by_user_id and
        # stamps the same fields the INSERT below would have set); INSERT only
        # when no tombstone exists. Same tenant scoping as the INSERT — the
        # enrollment row carries no tenant column, ownership lives on the
        # enrollment_token/license.
        enrollment = crud.reactivate_worker_enrollment(
            db,
            worker_name,
            hostname=hostname,
            fingerprint=fingerprint,
            approval_code=approval_code,
            bootstrap_token_hash=bootstrap_token_hash,
            status=initial_status,
            approved_at=approved_at,
            last_seen_at=now,
            enrollment_token_id=enrollment_token.id,
        )
        if enrollment is None:
            enrollment = WorkerEnrollment(
                worker_name=worker_name,
                hostname=hostname,
                fingerprint=fingerprint,
                approval_code=approval_code,
                bootstrap_token_hash=bootstrap_token_hash,
                status=initial_status,
                approved_at=approved_at,
                last_seen_at=now,
                enrollment_token_id=enrollment_token.id,
            )
            db.add(enrollment)
            db.commit()
            db.refresh(enrollment)
        else:
            db.commit()

    # Auto-license: create a WorkerLicense if it doesn't exist
    if enrollment_token.auto_license and initial_status == WorkerEnrollmentStatus.APPROVED:
        existing_license = crud.get_worker_by_name(db, worker_name)
        if not existing_license:
            # BUG-1029-fix2: the LIVE-enrollment branch above skipped the owner
            # gate (it never reaches the else-branch), so re-answer it here for
            # the license tombstone before anything is mutated — same 409,
            # nothing revived, nothing INSERTed.
            _has_lic_tomb, _lic_tomb_owner = crud.get_tombstoned_owner_tenant_id(
                db, worker_name
            )
            if _has_lic_tomb and _lic_tomb_owner is not None and _lic_tomb_owner != enrollment_token.owner_tenant_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "worker name is held by a deleted worker of a different "
                        "owner; choose another name"
                    ),
                )
            # BUG-1029-B1 twin: `worker_licenses.worker_name` is PLAIN unique
            # (FK target), so the decommissioned worker's license tombstone
            # still owns the name and a plain INSERT 500s on IntegrityError.
            # Revive it with the same fields the INSERT below would have set
            # (same tenant scoping — owner_tenant_id/assigned_tenant_ids come
            # from the enrollment token either way).
            #
            # BUG-1029-fix2: `expect_owner_tenant_id` re-checks the owner gate
            # that was already answered above (fail-closed net — the 409 above
            # is what the client sees); `reset_activity=True` +
            # `last_heartbeat=now` clear the tombstone's stale heartbeat/pause/
            # agent-version/zone so the revived worker is not re-zombied at
            # once by cleanup_zombie_workers.
            worker_license = crud.reactivate_worker_license(
                db,
                worker_name,
                hostname=hostname,
                status=WorkerStatus.LICENSED,
                licensed_at=now,
                first_seen_at=now,
                last_seen_at=now,
                is_remote=True,
                owner_type=enrollment_token.owner_type,
                owner_tenant_id=enrollment_token.owner_tenant_id,
                assigned_tenant_ids=enrollment_token.assigned_tenant_ids,
                queues=["CPU"],
                hardware_specs={},
                max_concurrent_tasks=_seed_max_concurrent_tasks(),
                last_heartbeat=now,
                expect_owner_tenant_id=enrollment_token.owner_tenant_id,
                reset_activity=True,
            )
            if worker_license is None:
                worker_license = WorkerLicense(
                    worker_name=worker_name,
                    hostname=hostname,
                    status=WorkerStatus.LICENSED,
                    is_remote=True,
                    licensed_at=now,
                    first_seen_at=now,
                    last_seen_at=now,
                    owner_type=enrollment_token.owner_type,
                    owner_tenant_id=enrollment_token.owner_tenant_id,
                    assigned_tenant_ids=enrollment_token.assigned_tenant_ids,
                    queues=["CPU"],
                    hardware_specs={},
                    max_concurrent_tasks=_seed_max_concurrent_tasks(),
                )
                db.add(worker_license)

    # Increment use count
    enrollment_token.use_count = (enrollment_token.use_count or 0) + 1
    db.commit()

    return enrollment


def confirm_enrollment(
    db: Session, worker_name: str, approval_code: str, bootstrap_token: str
) -> WorkerEnrollment:
    from security import verify_api_key

    enrollment = get_enrollment_or_404(db, worker_name)

    if enrollment.approval_code != (approval_code or "").strip():
        raise HTTPException(status_code=403, detail="Invalid approval_code")

    if not verify_api_key(bootstrap_token or "", enrollment.bootstrap_token_hash):
        raise HTTPException(status_code=403, detail="Invalid bootstrap_token")

    enrollment.last_seen_at = datetime.utcnow()
    db.commit()
    return enrollment


def approve_enrollment(db: Session, worker_name: str, approved_by_user_id: int) -> WorkerEnrollment:
    enrollment = get_enrollment_or_404(db, worker_name)
    enrollment.status = WorkerEnrollmentStatus.APPROVED
    enrollment.approved_at = datetime.utcnow()
    enrollment.approved_by_user_id = approved_by_user_id
    enrollment.last_seen_at = datetime.utcnow()
    db.commit()
    return enrollment


def reinstate_enrollment_row(db: Session, worker_name: str) -> WorkerEnrollment:
    enrollment = crud.get_enrollment_by_name(db, worker_name)
    if not enrollment:
        raise HTTPException(
            status_code=404, detail=f"No enrolment row found for worker '{worker_name}'"
        )
    if enrollment.status != WorkerEnrollmentStatus.REVOKED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Worker '{worker_name}' is {enrollment.status.value.upper()} "
                "(not REVOKED). Reinstate only applies to REVOKED enrolments."
            ),
        )
    # Keep revoked_at/revoked_by_user_id for audit — they document the prior
    # revocation. approved_at/approved_by_user_id ARE cleared: fresh cycle.
    enrollment.status = WorkerEnrollmentStatus.PENDING
    enrollment.approved_at = None
    enrollment.approved_by_user_id = None
    db.commit()
    return enrollment


# =============================================================================
# Worker re-home lookup
# =============================================================================


def get_worker_for_rehome(db: Session, worker_name: str) -> WorkerLicense:
    worker = crud.get_worker_by_name(db, worker_name)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


# =============================================================================
# list_workers view assembly
# =============================================================================


def compute_can_drain(db: Session, worker: WorkerLicense, is_worker_active_fn) -> Tuple[bool, Optional[str]]:
    """The last-active-worker guard, computed up-front for display (mirrors
    what pause/update enforce). `is_worker_active_fn` is passed in because
    that predicate has no DB access of its own and stays defined once, in
    the router, as `_is_worker_active`."""
    if not is_worker_active_fn(worker):
        return True, None
    if crud.count_other_active_workers(db, worker) > 0:
        return True, None
    if is_single_node_embedded(db, worker):
        return True, "single-node embedded deployment — exempt from last-active-worker guard"
    return False, (
        "This is the only worker actively able to claim tasks. Add or "
        "license another worker, or resume a paused one, before pausing "
        "or updating this one."
    )


def _embedded_worker_seed_names() -> Tuple[str, ...]:
    """
    The trusted embedded/manager worker names — read from main's seed set,
    never from a request-supplied name. Lazy import because main imports this
    module (same pattern as the router's `from main import
    _EMBEDDED_WORKER_SEED_NAMES` at ~:3794); the fallback keeps the
    exemption fail-closed (SaaS name only) rather than crashing the request
    if the constant is ever renamed.
    """
    try:
        from main import _EMBEDDED_WORKER_SEED_NAMES as _names  # noqa: E402
    except Exception:  # noqa: BLE001 — never let an import hiccup 500 a request
        _names = ("mod-worker-embedded",)
    return _names


def is_single_node_embedded(db: Session, worker: WorkerLicense) -> bool:
    if worker.worker_name in _embedded_worker_seed_names():
        return True
    if worker.is_remote:
        return False
    total_licensed = crud.count_workers_by_status(db, WorkerStatus.LICENSED)
    return total_licensed == 1


def list_workers_view(
    db: Session, discovered: List[str], is_worker_active_fn
) -> Tuple[List[Tuple[Any, Any, bool, bool, Optional[str]]], Dict[str, str], Optional[str], List[Any]]:
    """Assemble the raw ingredients for `list_workers`'s response. Returns
    `(rows, enrol_status_map, current_server_version, pending_enrolments)` where
    each row is `(worker, profile_or_None, is_online, can_drain, can_drain_reason)`
    — the router shapes these into `WorkerInfo`.

    `pending_enrolments` are the LIVE PENDING `WorkerEnrollment` rows that have
    no live `WorkerLicense` row (manual enrolment: `auto_approve=false` /
    `auto_license` unset) — the Dashboard's Approve branch needs them listed,
    and the license-row loop below cannot select them."""
    from utils.worker_releases import get_latest_version

    workers = crud.list_workers_ordered(db)
    profiles = crud.get_hardware_profiles_by_id(db)
    enrol_status_map = crud.list_enrollment_statuses(db)
    current_server_version = get_latest_version()

    rows = []
    for worker in workers:
        is_online = worker.worker_name in discovered
        can_drain, can_drain_reason = compute_can_drain(db, worker, is_worker_active_fn)
        profile = profiles.get(worker.hardware_profile_id)
        rows.append((worker, profile, is_online, can_drain, can_drain_reason))

    pending = crud.list_pending_enrollments_without_license(db)
    return rows, enrol_status_map, current_server_version, pending


# =============================================================================
# Simple per-worker field setters
# =============================================================================


def set_concurrency(db: Session, worker_name: str, max_concurrent_tasks: int) -> WorkerLicense:
    worker = get_worker_or_404(db, worker_name)
    worker.max_concurrent_tasks = max_concurrent_tasks
    db.commit()
    return worker


def set_network_zone(db: Session, worker_name: str, zone: str) -> WorkerLicense:
    worker = get_worker_or_404(db, worker_name)
    worker.network_zone = zone.strip()
    db.commit()
    return worker


def set_cost(
    db: Session,
    worker_name: str,
    *,
    cost_per_hour_cents: Optional[int],
    hardware_cost_cents: Optional[int],
    amortisation_months: Optional[int],
    power_watts: Optional[int],
    electricity_cents_per_kwh: Optional[int],
    margin_percent: Optional[int],
    derive_cost_per_hour_cents_fn,
) -> WorkerLicense:
    worker = get_worker_or_404(db, worker_name)
    worker.hardware_cost_cents = hardware_cost_cents
    worker.amortisation_months = amortisation_months
    worker.power_watts = power_watts
    worker.electricity_cents_per_kwh = electricity_cents_per_kwh
    worker.margin_percent = margin_percent
    worker.cost_per_hour_cents = (
        cost_per_hour_cents
        if cost_per_hour_cents is not None
        else derive_cost_per_hour_cents_fn(
            hardware_cost_cents, amortisation_months, power_watts, electricity_cents_per_kwh,
        )
    )
    db.commit()
    return worker


def list_network_zones_view(db: Session) -> List[str]:
    zones = crud.list_distinct_network_zones(db)
    if "public" not in zones:
        zones.append("public")
    return zones


# =============================================================================
# NOAH settings
# =============================================================================


def get_noah_fleet_view(db: Session) -> Dict[str, Any]:
    from utils.paid_import import require_paid
    _noah = require_paid("services.noah", "NOAH idle-worker harvest")

    platform = _noah.get_platform_noah(db)
    db.commit()

    workers = []
    total_harvested = 0
    enabled_count = 0
    available_count = 0
    for w in crud.list_all_workers(db):
        resolved_enabled = bool(_noah._pick(w.noah_enabled, platform.enabled))
        if not resolved_enabled:
            continue
        enabled_count += 1
        if w.noah_available:
            available_count += 1
        harvested = int(w.noah_harvested_total or 0)
        total_harvested += harvested
        since = getattr(w, "noah_available_since", None)
        next_free = getattr(w, "noah_next_free_at", None)
        workers.append({
            "worker_name": w.worker_name,
            "enabled": resolved_enabled,
            "available": w.noah_available,
            "reason": w.noah_reason,
            "last_transition": since.isoformat() if since else None,
            "noah_next_free_at": next_free.isoformat() if next_free else None,
            "harvested_total": harvested,
            "status": w.status.value if w.status else None,
        })

    return {
        "platform_enabled": bool(platform.enabled),
        "totals": {
            "noah_workers": enabled_count,
            "available_now": available_count,
            "harvested_total": total_harvested,
        },
        "workers": workers,
    }


def get_worker_noah_settings_view(db: Session, worker_name: str) -> Dict[str, Any]:
    from utils.paid_import import require_paid
    _noah = require_paid("services.noah", "NOAH idle-worker harvest")

    worker = get_worker_or_404(db, worker_name)
    fields = ("enabled", "sustained_quiet_s", "pause_grace_s", "plugged_in_only",
              "schedule_start", "schedule_end", "batch_appetite")
    overrides = {f: getattr(worker, f"noah_{f}") for f in fields}
    return {
        "worker_name": worker_name,
        "overrides": overrides,
        "effective": _noah.resolve_settings(db, worker),
        "available": worker.noah_available,
        "reason": worker.noah_reason,
    }


def set_worker_noah_settings_view(
    db: Session, worker_name: str, *, inherit: bool, values: Dict[str, Any]
) -> Dict[str, Any]:
    from utils.paid_import import require_paid
    _noah = require_paid("services.noah", "NOAH idle-worker harvest")

    worker = get_worker_or_404(db, worker_name)
    fields = ("enabled", "sustained_quiet_s", "pause_grace_s", "plugged_in_only",
              "schedule_start", "schedule_end", "batch_appetite")
    if inherit:
        for field in fields:
            setattr(worker, f"noah_{field}", None)
    else:
        for field in fields:
            val = values.get(field)
            if val is not None:
                setattr(worker, f"noah_{field}", val)
    db.commit()
    return {"worker_name": worker_name, "effective": _noah.resolve_settings(db, worker)}


# =============================================================================
# Licensing
# =============================================================================


def assign_license(
    db: Session,
    *,
    worker_name: str,
    owner_type: str,
    owner_tenant_id: Optional[int],
    assigned_tenant_ids: List[int],
    license_status: Dict[str, Any],
    licensed_by_user_id: int,
) -> Tuple[WorkerLicense, int, int]:
    """Returns `(worker, licensed_count_before_increment, max_workers)`."""
    max_workers = license_status.get("workers_max", 0)

    try:
        worker = crud.get_worker_by_name_for_update(db, worker_name)
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not found")

        if worker.status == WorkerStatus.LICENSED:
            raise HTTPException(status_code=400, detail="Worker already approved")

        licensed_count = crud.count_workers_by_status(db, WorkerStatus.LICENSED)

        if not license_status.get("development_mode") and licensed_count >= max_workers:
            db.rollback()
            raise HTTPException(
                status_code=403,
                detail=(
                    f"No worker slots available ({licensed_count}/{max_workers} used). "
                    "Revoke a worker to free a slot."
                ),
            )

        worker.status = WorkerStatus.LICENSED
        worker.licensed_at = datetime.utcnow()
        worker.licensed_by_user_id = licensed_by_user_id
        worker.revoked_at = None
        worker.revoked_by_user_id = None

        enrollment = crud.get_enrollment_by_name(db, worker_name)
        if enrollment and enrollment.status != WorkerEnrollmentStatus.APPROVED:
            enrollment.status = WorkerEnrollmentStatus.APPROVED
            enrollment.approved_at = datetime.utcnow()
            enrollment.approved_by_user_id = licensed_by_user_id

        try:
            worker.owner_type = WorkerOwnerType(owner_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid owner_type: {owner_type}")

        if owner_type == "tenant_owned":
            if not owner_tenant_id:
                raise HTTPException(
                    status_code=400, detail="owner_tenant_id required for tenant_owned workers"
                )
            worker.owner_tenant_id = owner_tenant_id
            worker.assigned_tenant_ids = None
        elif owner_type == "platform_assigned":
            if not assigned_tenant_ids or len(assigned_tenant_ids) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="assigned_tenant_ids required for platform_assigned workers",
                )
            worker.assigned_tenant_ids = assigned_tenant_ids
            worker.owner_tenant_id = None
        else:  # platform_only or platform_pool
            worker.owner_tenant_id = None
            worker.assigned_tenant_ids = None

        db.commit()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("Failed to add worker: %s", e)
        raise HTTPException(status_code=500, detail="Failed to add worker")

    return worker, licensed_count, max_workers


def revoke_license(db: Session, worker_name: str, revoked_by_user_id: int) -> Tuple[WorkerLicense, int]:
    """Returns `(worker, licensed_count_after_revoke)`."""
    licensed_count = crud.count_workers_by_status(db, WorkerStatus.LICENSED)
    if licensed_count <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the last approved worker. The system requires at least one worker.",
        )

    worker = crud.get_worker_by_name(db, worker_name)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if worker.status != WorkerStatus.LICENSED:
        raise HTTPException(status_code=400, detail="Worker has not been approved yet: click Add Worker")

    worker.status = WorkerStatus.UNLICENSED
    worker.owner_type = WorkerOwnerType.PLATFORM_POOL
    worker.assigned_tenant_ids = None
    worker.owner_tenant_id = None
    worker.revoked_at = datetime.utcnow()
    worker.revoked_by_user_id = revoked_by_user_id

    db.commit()

    licensed_count_after = crud.count_workers_by_status(db, WorkerStatus.LICENSED)
    return worker, licensed_count_after


def get_license_summary_view(db: Session, license_status: Dict[str, Any]) -> Dict[str, Any]:
    licensed_count = crud.count_workers_by_status(db, WorkerStatus.LICENSED)
    unlicensed_count = crud.count_workers_by_status(db, WorkerStatus.UNLICENSED)
    offline_count = crud.count_workers_by_status(db, WorkerStatus.OFFLINE)

    return {
        "license": {
            "valid": license_status.get("valid"),
            "development_mode": license_status.get("development_mode", False),
            "max_workers": license_status.get("workers_max", 0),
            "expires_at": license_status.get("expires_at"),
        },
        "workers": {
            "licensed": licensed_count,
            "unlicensed": unlicensed_count,
            "offline": offline_count,
            "total": licensed_count + unlicensed_count + offline_count,
            "available_licenses": max(0, license_status.get("workers_max", 0) - licensed_count),
        },
    }


# =============================================================================
# Delete / zombie cleanup
# =============================================================================


def delete_worker_row(
    db: Session, worker_name: str, deleted_by_user_id: int | None = None
) -> Tuple[Optional[WorkerLicense], Optional[WorkerEnrollment]]:
    """Enforces "not the last licensed worker" / "not currently LICENSED",
    then performs the cascade delete. Returns the (now-detached) worker +
    enrollment for the router's response/audit-log use."""
    worker = crud.get_worker_by_name(db, worker_name)
    enrollment = crud.get_enrollment_by_name(db, worker_name)

    if worker and worker.status == WorkerStatus.LICENSED:
        licensed_count = crud.count_workers_by_status(db, WorkerStatus.LICENSED)
        if licensed_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the last approved worker. The system requires at least one worker.",
            )

    if not worker and not enrollment:
        raise HTTPException(status_code=404, detail="Worker not found")

    if worker and worker.status == WorkerStatus.LICENSED:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an active worker. Remove its approval first or wait for it to go offline.",
        )

    active_stacks = crud.list_active_persistent_stacks_for_worker(db, worker_name)
    if active_stacks:
        stack_names = ", ".join(s.name for s in active_stacks)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete worker '{worker_name}': it is still hosting "
                f"persistent stack(s) ({stack_names}). Tear down or migrate "
                "those stacks first."
            ),
        )

    try:
        crud.delete_worker_cascade(
            db, worker_name, worker, enrollment, deleted_by_user_id=deleted_by_user_id
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.exception("delete_worker_row: unexpected FK conflict deleting %s", worker_name)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete worker '{worker_name}': it still has dependent "
                "records referencing it. Retry once those are cleared, or "
                "contact support if this persists."
            ),
        )
    return worker, enrollment


def cleanup_zombie_workers(db: Session, max_offline_days: int) -> Tuple[int, List[Dict[str, Any]], datetime]:
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_offline_days)
    zombies = crud.list_offline_zombies(db, cutoff_date)

    cleaned_workers = []
    for zombie in zombies:
        worker_name = zombie.worker_name
        last_seen = zombie.last_heartbeat
        # BUG-1029 (ISO 27001 no-hard-delete): zombies are SOFT-decommissioned
        # through the same cascade the user-facing delete uses — tombstone
        # (`deleted_at` + status flip), never reaped. `deleted_by_user_id` stays
        # NULL because this is a system action, not a user's.
        # BUG-1029: commit PER worker. One FK conflict (IntegrityError) on one
        # zombie must not lose the whole batch — rollback + skip that name, the
        # rest still get soft-deleted and committed. `cleaned_workers` only
        # grows after a successful commit so the reported count matches DB
        # state.
        try:
            crud.delete_worker_cascade(
                db, worker_name, zombie, crud.get_enrollment_by_name(db, worker_name),
                deleted_by_user_id=None,
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.exception(
                "cleanup_zombie_workers: FK conflict, skipped %s", worker_name
            )
            continue
        logger.info("cleanup_zombie_workers: decommissioned (soft) %s", worker_name)
        cleaned_workers.append({
            "worker_name": worker_name,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })

    return len(cleaned_workers), cleaned_workers, cutoff_date


# =============================================================================
# Queue views
# =============================================================================


def _serialize_task(t) -> Dict[str, Any]:
    return {
        "id": t.id,
        "status": t.status.value if hasattr(t.status, "value") else str(t.status),
        "node_kind": (t.task_payload or {}).get("node_kind"),
        "queue_name": t.queue_name,
        "priority": t.priority,
        "tenant_id": t.tenant_id,
        "workflow_run_id": t.workflow_run_id,
        "node_id": t.node_id,
        "assigned_to_worker": getattr(t, "assigned_to_worker", None),
        "claimed_by_worker": getattr(t, "claimed_by_worker", None),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "claimed_at": t.claimed_at.isoformat() if t.claimed_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
    }


def get_platform_queue_view(db: Session, limit: int) -> Dict[str, Any]:
    in_flight_rows = crud.list_in_flight_tasks(db, limit)
    pending_rows = crud.list_pending_tasks(db, limit)
    counts_raw = crud.queue_status_counts(db)
    by_queue_raw = crud.queue_pending_counts_by_name(db)

    def _key(s):
        return (s.value if hasattr(s, "value") else str(s)).lower()

    return {
        "in_flight": [_serialize_task(t) for t in in_flight_rows],
        "pending": [_serialize_task(t) for t in pending_rows],
        "by_queue": {(k or "unknown"): v for k, v in by_queue_raw.items()},
        "counts": {_key(k): v for k, v in counts_raw.items()},
    }


def get_worker_queue_view(db: Session, worker_name: str) -> Dict[str, Any]:
    in_flight = crud.list_worker_in_flight_tasks(db, worker_name)
    assigned = crud.list_worker_assigned_tasks(db, worker_name)

    def _ser(t):
        return {
            "id": t.id,
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            "node_kind": (t.task_payload or {}).get("node_kind"),
            "queue_name": t.queue_name,
            "priority": t.priority,
            "tenant_id": t.tenant_id,
            "workflow_run_id": t.workflow_run_id,
            "node_id": t.node_id,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "claimed_at": t.claimed_at.isoformat() if t.claimed_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
        }

    return {
        "worker_name": worker_name,
        "in_flight": [_ser(t) for t in in_flight],
        "assigned": [_ser(t) for t in assigned],
    }


# =============================================================================
# Tenant adapter-cache flush (Tenant lookup only; the Redis INCR stays in the
# router — no DB session should straddle a network call, and this one is
# outside any transaction anyway)
# =============================================================================


def get_tenant_or_404(db: Session, tenant_id: int):
    tenant = crud.get_tenant_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    return tenant


# =============================================================================
# Enrollment tokens
# =============================================================================


def create_enrollment_token_row(
    db: Session,
    *,
    label: Optional[str],
    created_by_user_id: int,
    max_uses: Optional[int],
    auto_approve: bool,
    auto_license: bool,
    allowed_name_pattern: Optional[str],
    owner_type: str,
    assigned_tenant_ids: List[int],
    owner_tenant_id: Optional[int],
    token_hash: str,
    expires_at: Optional[datetime],
    home_anchor: str,
    created_by_system: bool = False,
) -> WorkerEnrollmentToken:
    owner_type_enum = WorkerOwnerType(owner_type)
    enrollment_token = WorkerEnrollmentToken(
        token_hash=token_hash,
        label=label,
        created_by_user_id=created_by_user_id,
        created_by_system=created_by_system,
        max_uses=max_uses,
        use_count=0,
        auto_approve=auto_approve,
        auto_license=auto_license,
        allowed_name_pattern=allowed_name_pattern,
        owner_type=owner_type_enum,
        assigned_tenant_ids=[str(tid) for tid in (assigned_tenant_ids or [])],
        owner_tenant_id=owner_tenant_id,
        expires_at=expires_at,
        home_anchor=home_anchor,
    )
    db.add(enrollment_token)
    db.commit()
    db.refresh(enrollment_token)
    return enrollment_token


def list_enrollment_tokens_view(db: Session) -> List[WorkerEnrollmentToken]:
    return crud.list_enrollment_tokens(db)


def revoke_enrollment_token_row(db: Session, token_id: int) -> WorkerEnrollmentToken:
    token = crud.get_enrollment_token_by_id(db, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Enrollment token not found")
    if token.revoked_at:
        raise HTTPException(status_code=400, detail="Token already revoked")
    token.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return token


# =============================================================================
# Hardware profile / benchmark
# =============================================================================


def get_worker_hardware_profile_view(db: Session, worker_id: int, normalize_arch_fn) -> Dict[str, Any]:
    worker = get_worker_by_id_or_404(db, worker_id)

    profile = None
    if worker.hardware_profile_id:
        profile = crud.get_hardware_profile_by_id(db, worker.hardware_profile_id)

    hw_specs = worker.hardware_specs or {}
    hw_tags = hw_specs.get("hardware_tags") or []

    if not profile:
        return {
            "worker_name": worker.worker_name,
            "agent_version": worker.agent_version,
            "hardware_tags": hw_tags,
            "hardware_profile": None,
        }

    return {
        "worker_name": worker.worker_name,
        "agent_version": worker.agent_version,
        "hardware_tags": hw_tags,
        "arch": (normalize_arch_fn(hw_specs.get("arch")) or None),
        "hardware_profile": {
            "id": profile.id,
            "fingerprint": profile.fingerprint,
            "cpu_model": profile.cpu_model,
            "cpu_cores": profile.cpu_cores,
            "gpu_model": profile.gpu_model,
            "gpu_vram_gb": profile.gpu_vram_gb,
            "ram_gb": profile.ram_gb,
            "cloud_provider": profile.cloud_provider,
            "instance_type": profile.instance_type,
            "benchmark_status": profile.benchmark_status,
            "benchmarked_at": profile.benchmarked_at.isoformat() if profile.benchmarked_at else None,
            "scores": {
                "gpu_compute": profile.gpu_compute_score,
                "cpu_multi": profile.cpu_multi_score,
                "cpu_single": profile.cpu_single_score,
                "io_seq": profile.io_seq_score,
                "io_random": profile.io_random_score,
                "memory_bw": profile.memory_bw_score,
            },
        },
    }


def prepare_benchmark_trigger(
    db: Session,
    worker_id: int,
    *,
    only_selector: Optional[str],
    only_kind: Optional[str],
    only_index: Optional[int],
) -> Tuple[WorkerLicense, Any, List[Dict[str, Any]]]:
    """Validate + prep the worker/profile/accelerator rows for
    `trigger_benchmark`. Returns `(worker, profile, accel_devs)`; the router
    keeps calling `_auto_dispatch_profiling` / `_auto_calibrate_for_profile_hook`
    itself afterwards (those stay in the router — shared with the deferred
    heartbeat path, see module docstring)."""
    from models import AcceleratorProfile

    worker = get_worker_by_id_or_404(db, worker_id)
    if not worker.hardware_profile_id:
        raise HTTPException(
            status_code=400, detail="Worker has no hardware profile yet (needs heartbeat first)"
        )

    profile = crud.get_hardware_profile_by_id(db, worker.hardware_profile_id)
    if not profile:
        raise HTTPException(
            status_code=409,
            detail="Worker's hardware profile is missing (dangling reference); run a heartbeat to re-establish it",
        )

    if profile.benchmark_status == "running":
        active_profiling_run = crud.get_active_profiling_run(db, profile.id)
        if active_profiling_run:
            raise HTTPException(status_code=409, detail="Benchmark already running")
        profile.benchmark_status = "pending"

    accel_devs = []
    for ap in crud.list_accelerator_profiles(db, worker.hardware_profile_id):
        _is_targeted = bool(only_kind and ap.kind.lower() == only_kind and ap.index == only_index)
        _quarantined = ap.benchmark_status == "unsupported" or getattr(ap, "admin_disabled", False)
        if _is_targeted or (only_selector is None and not _quarantined):
            ap.benchmark_status = "pending"
            ap.error_message = None
        accel_devs.append({
            "kind": ap.kind, "index": ap.index, "name": ap.name,
            "vendor_id": ap.vendor_id, "device_id": ap.device_id,
        })

    return (worker, profile, accel_devs)


def get_worker_gpu_usage_view(db: Session, worker_id: int) -> Dict[str, Any]:
    """megaplan 14.8 — per-card reserved-vs-typical VRAM for the Workers
    page's per-card bar.

    * "reserved" comes straight from each running task's
      `task_payload["gpu_assignment"]` (see `services.gpu_reservation`, §14.1
      / §14.2) — the actual soft reservation the scheduler already holds.
    * "typical peak" is NOT live telemetry — the worker only reports
      `peak_vram_mb` at task COMPLETION. It is the learned p95 (falling back
      to the learned average peak) of past COMPLETED runs of the same
      node_kind, i.e. `gpu_reservation._learned_p95_vram_mb` /
      `_learned_peak_vram_mb` with NO margin applied (unlike
      `vram_reservation_mb`, which inflates + clamps for scheduling — we want
      the raw comparison point here, not the padded reservation).
    * "live_used_mb" per card is only populated when the worker has exactly
      one sized GPU card, so the worker-aggregate
      `WorkerResourceTimeline.vram_used_mb` unambiguously belongs to that one
      card. Multi-GPU workers get `live_used_mb: null` — there is no
      per-card breakdown in the heartbeat today (see task doc); this
      function must never fabricate one.
    """
    import services.gpu_reservation as gpu_reservation

    worker = crud.get_worker_by_id(db, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    cards = gpu_reservation.worker_cards(db, worker)
    by_card_id = {c["card_id"]: c for c in cards}

    timeline = crud.get_resource_timeline_by_worker(db, worker.worker_name)
    single_card_live_mb = (
        timeline.vram_used_mb if (timeline and len(cards) == 1) else None
    )

    tasks_by_card: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in by_card_id}
    for task_id, tenant_id, payload in crud.list_gpu_assignment_tasks(db, by_card_id.keys()):
        assignment = (payload or {}).get("gpu_assignment") or {}
        node_kind = (
            payload.get("node_kind")
            or (payload.get("node_config") or {}).get("node_kind")
            or payload.get("stack_name")
        )
        reserved_mb = float(assignment.get("vram_limit_mb") or 0)
        mode = assignment.get("mode") or "shared"

        typical_peak_mb = gpu_reservation._learned_p95_vram_mb(db, node_kind, tenant_id)
        if not typical_peak_mb:
            typical_peak_mb = gpu_reservation._learned_peak_vram_mb(db, node_kind, tenant_id)

        task_view = {
            "task_id": task_id,
            "node_kind": node_kind,
            "mode": mode,
            "reserved_mb": int(reserved_mb),
            "typical_peak_mb": int(typical_peak_mb) if typical_peak_mb else None,
            "over_budget": bool(typical_peak_mb and typical_peak_mb > reserved_mb),
        }
        for card in assignment.get("cards") or ():
            cid = str(card.get("card_id") or "")
            if cid in tasks_by_card:
                tasks_by_card[cid].append(task_view)

    return {
        "worker_id": worker.id,
        "worker_name": worker.worker_name,
        "cards": [
            {
                "card_id": cid,
                "kind": card["kind"],
                "total_mb": int(card["total_vram_mb"]),
                "live_used_mb": (
                    single_card_live_mb if single_card_live_mb is not None else None
                ),
                "tasks": tasks_by_card.get(cid, []),
            }
            for cid, card in by_card_id.items()
        ],
    }

    return worker, profile, accel_devs
