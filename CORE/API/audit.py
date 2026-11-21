"""
Audit Logging Helper Functions

Provides utilities for logging permission-related and security actions for compliance and debugging.

CMMC/ISO 27001 Compliance:
- AU-2 (Audit Events)
- AU-3 (Content of Audit Records)
- AU-9 (Protection of Audit Information) - HMAC integrity verification
"""

from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime
from fastapi import Request
import logging
import hashlib
import hmac
import json
import os

import models

# GSEC R-145 / FX.106 p3a — ONE trusted client-IP resolver for every audit
# site. CE ships utils/client_ip.py (trusted_client_ip); the Paid tree adds
# services/client_ip.py (resolve_client_ip), which layers in CF-Connecting-IP
# support + the network.trusted_proxy_cidrs platform setting on top of the
# same net.trusted_proxy_cidrs trust model. `services/client_ip.py` doesn't
# exist in a CE build (open-core/manifest.yaml denies it), so a direct
# `from services.client_ip import ...` is a hard ImportError at process start
# on every CE deployment — guard it via utils/paid_import.optional_paid and
# fall back to the CE resolver. XFF / X-Real-IP / CF-Connecting-IP are
# honoured only from a trusted peer, right-most untrusted hop wins; the raw
# left-most XFF hop is never trusted.
from utils.client_ip import trusted_client_ip as _ce_trusted_client_ip
from utils.paid_import import optional_paid

_paid_client_ip = optional_paid("services.client_ip")
trusted_client_ip = (
    _paid_client_ip.resolve_client_ip if _paid_client_ip is not None
    else _ce_trusted_client_ip
)

logger = logging.getLogger(__name__)

# GSEC R-144 (SEC-3-005) — audit write failures must be observable + alertable
# (AU-5). Every DB-write audit site used to swallow the exception silently (an
# ERROR log, or a bare `pass`), so a lost row left no trace. This counter + the
# WARNING log in _record_audit_write_failure() make the failure rate visible to
# Prometheus. Same pattern as worker_tasks.py's _StsFallbackCounter. Best-effort
# import — metrics must never break the audit path itself.
try:  # pragma: no cover - import guard; prometheus is a transitive dep
    from prometheus_client import Counter as _AuditWriteFailCounter

    _METRIC_AUDIT_WRITE_FAILURES = _AuditWriteFailCounter(
        "audit_write_failures_total",
        "Audit log write failures by site (the audit row was lost; "
        "request continues, so the gap is invisible without this counter).",
        ["site"],
    )
except Exception:  # pragma: no cover - metrics are best-effort
    _METRIC_AUDIT_WRITE_FAILURES = None


def _record_audit_write_failure(site: str, exc: Exception, context: str = "") -> None:
    """GSEC R-144 — alert on a failed audit-log write. WARNING log (immediate
    visibility) + Prometheus counter (rate, alertable) so an audit gap can be
    paged on. Best-effort: never raises into the request path, so a failing
    counter still degrades to just the log line."""
    logger.warning(
        "GSEC R-144: audit write FAILED site=%s err=%s%s — the audit row was "
        "lost and the request continues normally. Investigate DB availability "
        "(this is a compliance gap, not just a log miss).",
        site, exc, f" context={context}" if context else "",
    )
    if _METRIC_AUDIT_WRITE_FAILURES is None:
        return
    try:
        _METRIC_AUDIT_WRITE_FAILURES.labels(site=site).inc()
    except Exception:  # pragma: no cover - best-effort
        pass


# HMAC key for audit log integrity verification
# CMMC AU-9: Protect audit information from unauthorized modification
AUDIT_HMAC_KEY = os.getenv("AUDIT_HMAC_KEY", "").encode()
if not AUDIT_HMAC_KEY:
    logger.warning(
        "SECURITY WARNING: AUDIT_HMAC_KEY not set. Audit log integrity verification disabled. "
        "Set AUDIT_HMAC_KEY environment variable for CMMC AU-9 compliance."
    )


def _compute_audit_hash(record_data: dict) -> str:
    """
    Compute HMAC-SHA256 hash for audit record integrity verification.

    CMMC AU-9: Protect audit information from unauthorized modification.
    """
    if not AUDIT_HMAC_KEY:
        return ""

    # Create deterministic JSON string (sorted keys)
    data_str = json.dumps(record_data, sort_keys=True, default=str)
    return hmac.new(AUDIT_HMAC_KEY, data_str.encode(), hashlib.sha256).hexdigest()


# Platform tenant (see dependencies.PLATFORM_TENANT_ID); tenant-less admin audits land here.
PLATFORM_AUDIT_TENANT_ID = 1

# BUG-960d / Q12=B (10-12 FINAL): dedicated system user (migration
# 20261013i_seed_system_audit_user) that system-emitted audit rows attribute
# to instead of the literal 0 (which has no `users` row -> FK violation on
# PermissionAuditLog.actor_user_id).
SYSTEM_USER_USERNAME = "mod-system"
SYSTEM_USER_EMAIL = "system@mod.internal"
_system_user_id_cache: Optional[int] = None


def _seed_system_user(db: Session) -> Optional[int]:
    """1900Y2b (CE stranger-boot C7) — self-heal a missing mod-system actor.

    Same shape as migrations/20261013i_seed_system_audit_user.py's INSERT. A
    fresh CE DB ships the migration FILES but the seed script never ran, so
    the lookup used to fall back to the literal 0 -> FK violation on
    PermissionAuditLog.actor_user_id -> a SQLAlchemy flush error cascading
    into the caller's transaction (the calibration tick / a failed task each
    hit it, 3 tracebacks in the CE API log).

    Isolated in its own SAVEPOINT + flush so a seed that cannot be written
    (platform tenant absent, FK blip) unwinds just the seed and leaves the
    caller's session usable. Append-only: this only ever INSERTs.
    """
    try:
        with db.begin_nested():
            user = models.User(
                username=SYSTEM_USER_USERNAME,
                email=SYSTEM_USER_EMAIL,
                first_name="MOD",
                last_name="System",
                tenant_id=PLATFORM_AUDIT_TENANT_ID,
                keycloak_user_id=None,
                status=models.UserStatus.ACTIVE,
                onboarding_complete=True,
            )
            db.add(user)
            db.flush()
        db.refresh(user)
        return user.id
    except Exception as exc:  # noqa: BLE001 — a seed miss must not wedge the audit
        raced = db.query(models.User.id).filter(
            models.User.username == SYSTEM_USER_USERNAME).first()
        if raced:
            return raced[0]
        logger.warning(
            "audit: mod-system actor seed failed (falling back to actor 0): %s", exc,
        )
        return None


def _system_user_id(db: Session) -> int:
    """Id of the system audit actor (migration 20261013i, self-healed if absent).

    BUG-960d / Q12=B part 1 (977a): ``PermissionAuditLog.actor_user_id`` is
    NOT NULL with an FK to ``users.id``, so the literal 0 used by the system-
    emitted audit call sites FK-violates. The migration seeds one dedicated
    ``mod-system`` user; 1900Y2b makes a DB that never ran that seed self-heal
    instead of returning 0, so a fresh CE DB runs the calibration tick and a
    failed task without an audit FK traceback. 0 remains the last-resort value
    only if the seed itself cannot be written.
    """
    global _system_user_id_cache
    if _system_user_id_cache is not None:
        return _system_user_id_cache
    row = db.query(models.User.id).filter(
        models.User.username == SYSTEM_USER_USERNAME).first()
    if row:
        _system_user_id_cache = row[0]
        return row[0]
    seeded = _seed_system_user(db)
    if seeded is not None:
        _system_user_id_cache = seeded
        return seeded
    raise RuntimeError("mod-system actor seed failed: no valid User.id for audit writes. The PermissionAuditLog.actor_user_id FK requires a non-zero users.id. Verify the seed migration (20261013i) or re-run calibration tick.")


def _with_request_app_id(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """GX.A3b — fold the requesting APP into audit metadata when present.

    When the current request was authenticated by an app key
    (``api_keys.app_id`` set, stashed on the request principal by
    ``dependencies._enforce_api_key_policy``), stamp ``app_id`` into the
    metadata so the audit row attributes the action to the APP. A plain
    session / non-app-key request leaves the metadata untouched. Never
    overwrites an ``app_id`` the caller already supplied explicitly.
    """
    out = dict(metadata) if metadata else {}
    try:
        from utils.request_principal import get_app_id
        app_id = get_app_id()
    except Exception:
        app_id = None
    if app_id is not None and out.get("app_id") is None:
        out["app_id"] = app_id
    return out


def _get_previous_hash(db: Session, tenant_id: int) -> str:
    """Get the integrity hash of the most recent audit log for chain verification."""
    if not AUDIT_HMAC_KEY:
        return ""

    last_record = db.query(models.PermissionAuditLog).filter(
        models.PermissionAuditLog.tenant_id == tenant_id
    ).order_by(models.PermissionAuditLog.id.desc()).first()

    return last_record.integrity_hash if last_record and last_record.integrity_hash else ""



def log_permission_audit(
    db: Session,
    action: models.AuditAction,
    actor_user_id: int,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    target_user_id: Optional[int] = None,
    permission_level: Optional[str] = None,
    permission_id: Optional[int] = None,
    permission_request_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> models.PermissionAuditLog:
    """
    Create an audit log entry for a permission-related action.

    CMMC/ISO 27001 Compliance:
    - AU-2: Logs security-relevant events
    - AU-3: Includes comprehensive audit record content
    - AU-9: Includes HMAC integrity hash for tamper detection

    Args:
        db: Database session
        action: The audit action type
        actor_user_id: User who performed the action
        tenant_id: Tenant ID
        resource_type: Type of resource (e.g., "cog", "workflow")
        resource_id: ID of the resource
        target_user_id: User who was affected (optional)
        permission_level: Permission level granted/revoked (optional)
        permission_id: Related permission ID (optional)
        permission_request_id: Related permission request ID (optional)
        metadata: Additional context as JSON (optional)
        request: FastAPI request object for IP/user-agent extraction (optional)

    Returns:
        The created audit log entry
    """
    # Extract IP and user agent from request if provided
    ip_address = None
    user_agent = None
    if request:
        # GSEC R-145 — one trusted client-IP resolver (utils/client_ip.py);
        # the raw left-most XFF hop is attacker-supplied and never trusted.
        ip_address = trusted_client_ip(request)
        user_agent = request.headers.get("User-Agent")

    # GX.A3b: attribute the action to the requesting app (if an app-key request)
    # before the metadata goes into the integrity hash AND the stored row.
    metadata = _with_request_app_id(metadata)

    # Get previous record hash for chain integrity (CMMC AU-9)
    previous_hash = _get_previous_hash(db, tenant_id)

    # Prepare record data for integrity hash
    record_data = {
        "tenant_id": tenant_id,
        "action": action.value if action else None,
        "actor_user_id": actor_user_id,
        "target_user_id": target_user_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "permission_level": permission_level,
        "permission_id": permission_id,
        "permission_request_id": permission_request_id,
        "metadata": metadata,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "previous_hash": previous_hash,
    }

    # Compute integrity hash (CMMC AU-9)
    integrity_hash = _compute_audit_hash(record_data)

    audit_log = models.PermissionAuditLog(
        tenant_id=tenant_id,
        action=action,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_level=permission_level,
        permission_id=permission_id,
        permission_request_id=permission_request_id,
        audit_metadata=metadata,
        ip_address=ip_address,
        user_agent=user_agent,
        integrity_hash=integrity_hash,
        previous_hash=previous_hash,
    )

    # Q12=B part 2 (977b): isolate the audit insert in its own SAVEPOINT so a failed
    # permission-audit write only unwinds the insert — any pending writes the caller
    # already staged in the same transaction survive for its own later commit. The
    # savepoint still ends with a real commit: callers across the codebase (e.g.
    # services/node_egress_approval.py's blocked-egress path) document and depend on
    # this function committing durably on its own, independent of whatever the caller
    # does afterward — dropping that commit would silently lose audit rows whenever a
    # caller logs then raises/returns without itself committing. Contract unchanged:
    # exceptions still propagate (return type stays non-Optional) and, since the
    # savepoint rollback happens before commit() is ever reached, a failed insert
    # never touches the caller's earlier pending writes.
    with db.begin_nested():
        db.add(audit_log)
        db.flush()
    db.commit()
    db.refresh(audit_log)

    return audit_log


def log_permission_granted(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    target_user_id: int,
    resource_type: str,
    resource_id: int,
    permission_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission is granted."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_GRANTED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_id=permission_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


def log_permission_revoked(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    target_user_id: int,
    resource_type: str,
    resource_id: int,
    permission_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission is revoked."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_REVOKED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_id=permission_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


def log_permission_updated(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    target_user_id: int,
    resource_type: str,
    resource_id: int,
    permission_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission is updated."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_UPDATED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_id=permission_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


def log_permission_request_created(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    permission_request_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission request is created."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_REQUESTED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_request_id=permission_request_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


def log_permission_request_approved(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    target_user_id: int,
    resource_type: str,
    resource_id: int,
    permission_request_id: int,
    permission_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission request is approved."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_REQUEST_APPROVED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_request_id=permission_request_id,
        permission_id=permission_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


def log_permission_request_denied(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    target_user_id: int,
    resource_type: str,
    resource_id: int,
    permission_request_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission request is denied."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_REQUEST_DENIED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_request_id=permission_request_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


def log_permission_request_cancelled(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    permission_request_id: int,
    permission_level: str,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
):
    """Log when a permission request is cancelled."""
    return log_permission_audit(
        db=db,
        action=models.AuditAction.PERMISSION_REQUEST_CANCELLED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission_request_id=permission_request_id,
        permission_level=permission_level,
        metadata=metadata,
        request=request,
    )


# =============================================================================
# SECURITY AUDIT FUNCTIONS (CMMC AU-2 Compliance)
# =============================================================================

def _maybe_push_high_severity(db: Session, audit_log: models.PermissionAuditLog) -> None:
    """GY.C15b — fire the high-severity incident push for a security audit row.

    Never raises: audit logging must not depend on the push, and a push failure
    is surfaced as a WARNING. Only runs when the platform setting
    `incident.high_severity_push_enabled` is true. `services.incident_export`
    is imported inside the function — `services.incident_export` imports `audit`,
    so a module-level import here would be a cycle.
    """
    try:
        from services import incident_export  # local import: avoids the cycle

        if incident_export.classify_severity(audit_log.action) != "high":
            return
        if not incident_export._psr.get_setting(
                "incident.high_severity_push_enabled", db=db):
            return
        incident_export.push_high_severity(db, audit_log)
    except Exception as e:  # noqa: BLE001 — push must never break audit logging
        logger.warning("GY.C15b high-severity push failed (audit row is safe): %s", e)


def log_security_event(
    db: Session,
    action: models.AuditAction,
    request: Optional[Request] = None,
    user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[models.PermissionAuditLog]:
    """
    Log a security-related audit event.

    CMMC/ISO 27001 Compliance: AU-2 (Audit Events), AU-3 (Content of Audit Records)

    Args:
        db: Database session
        action: Security audit action type
        request: FastAPI request for IP/user-agent extraction
        user_id: User ID if known (may be None for failed auth)
        tenant_id: Tenant ID if known
        metadata: Additional context (error details, etc.)

    Returns:
        Created audit log entry or None if logging fails
    """
    try:
        # Extract request details
        ip_address = None
        user_agent = None
        if request:
            # GSEC R-145 — one trusted client-IP resolver (utils/client_ip.py).
            ip_address = trusted_client_ip(request)
            user_agent = request.headers.get("User-Agent")

        # GX.A3b: attribute the action to the requesting app (if an app-key request)
        metadata = _with_request_app_id(metadata)

        # Use the seeded system user if no user context (BUG-960d)
        actor_id = user_id or _system_user_id(db)

        audit_log = models.PermissionAuditLog(
            tenant_id=tenant_id or PLATFORM_AUDIT_TENANT_ID,
            action=action,
            actor_user_id=actor_id,
            target_user_id=user_id,
            resource_type="security",
            resource_id=0,
            audit_metadata=metadata or {},
            ip_address=ip_address,
            user_agent=user_agent,
        )

        # Q12=B part 2 (977b): isolate the audit insert in its own SAVEPOINT. A bare
        # db.commit() here used to commit (and, on failure, roll back) the WHOLE
        # session, silently discarding any pending writes the caller already staged
        # in the same transaction (e.g. approval_gate._replay_byok_revoke's
        # psr.set_setting calls). With a savepoint, a failed audit insert unwinds
        # only the insert; the caller's earlier pending writes stay in the session
        # for its own later commit.
        #
        # The insert and the commit are two SEPARATE try blocks on purpose:
        #   - if the SAVEPOINT insert itself fails, we must return without calling
        #     db.commit()/db.rollback() at all — the savepoint has already unwound
        #     just the audit row, and the caller's earlier pending writes are still
        #     live, uncommitted, in the session for the caller to commit or roll
        #     back on its own terms.
        #   - once the insert has succeeded, this function still commits durably
        #     (matching the pre-977b contract that many call sites rely on — e.g. a
        #     denied-auth/permission path that logs the event and then raises
        #     without ever calling db.commit() itself). If that commit fails for an
        #     unrelated reason, a full rollback here is correct (same behavior as
        #     the old unconditional db.commit()).
        try:
            with db.begin_nested():
                db.add(audit_log)
                db.flush()
        except Exception as e:
            # GSEC R-144: still count + log the failure loudly, but never raise and
            # never roll back the outer transaction — the SAVEPOINT already unwound
            # just the audit insert, so the caller's pending work must survive.
            _record_audit_write_failure("log_security_event", e)
            logger.exception(
                "log_security_event: audit insert failed (isolated via SAVEPOINT; "
                "caller's pending writes preserved)"
            )
            return None

        try:
            db.commit()
            db.refresh(audit_log)
        except Exception as e:
            _record_audit_write_failure("log_security_event", e)
            logger.exception("log_security_event: commit after audit insert failed")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None

        # Also log to application logger for immediate visibility
        logger.warning(
            f"SECURITY_AUDIT: action={action.value} ip={ip_address} "
            f"user_id={user_id} tenant_id={tenant_id} metadata={metadata}"
        )

        # GY.C15b — high-severity rows are pushed immediately (SIEM shipper +
        # the tenant's security webhook). Imported inside the function to avoid
        # the audit <-> services.incident_export import cycle, and wrapped so a
        # push failure can never break audit logging (the audit row is already
        # committed above).
        _maybe_push_high_severity(db, audit_log)

        return audit_log

    except Exception as e:
        # GSEC R-144: fail the write loudly (WARNING + counter) but never
        # raise — audit failures must not break the request path.
        _record_audit_write_failure("log_security_event", e)
        # Don't raise - audit failures shouldn't break the application. Roll
        # back so a failed audit flush doesn't poison the caller's session
        # (PendingRollbackError on its next attribute access / query).
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def log_auth_failure(
    db: Session,
    reason: str,
    request: Optional[Request] = None,
    user_identifier: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Log authentication failure.

    CMMC Requirement: AU-2 - Log failed authentication attempts
    """
    event_metadata = {
        "reason": reason,
        "user_identifier": user_identifier,
        **(metadata or {})
    }
    return log_security_event(
        db=db,
        action=models.AuditAction.AUTH_LOGIN_FAILED,
        request=request,
        metadata=event_metadata,
    )


def log_token_validation_failure(
    db: Session,
    reason: str,
    request: Optional[Request] = None,
    token_prefix: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Log token validation failure (invalid or expired).

    CMMC Requirement: AU-2 - Log authentication events
    """
    action = models.AuditAction.AUTH_TOKEN_EXPIRED if "expired" in reason.lower() else models.AuditAction.AUTH_TOKEN_INVALID
    event_metadata = {
        "reason": reason,
        "token_prefix": token_prefix[:8] if token_prefix else None,  # Only first 8 chars
        **(metadata or {})
    }
    return log_security_event(
        db=db,
        action=action,
        request=request,
        metadata=event_metadata,
    )


def log_api_key_failure(
    db: Session,
    reason: str,
    request: Optional[Request] = None,
    key_prefix: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Log API key authentication failure.

    CMMC Requirement: AU-2 - Log authentication events
    """
    event_metadata = {
        "reason": reason,
        "key_prefix": key_prefix,
        **(metadata or {})
    }
    return log_security_event(
        db=db,
        action=models.AuditAction.AUTH_API_KEY_INVALID,
        request=request,
        metadata=event_metadata,
    )


def log_webhook_signature_failure(
    db: Session,
    webhook_type: str,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Log webhook signature verification failure.

    CMMC Requirement: AU-2 - Log security-relevant events
    """
    event_metadata = {
        "webhook_type": webhook_type,
        **(metadata or {})
    }
    return log_security_event(
        db=db,
        action=models.AuditAction.WEBHOOK_SIGNATURE_FAILED,
        request=request,
        metadata=event_metadata,
    )


def log_impersonation(
    db: Session,
    service_user_id: int,
    impersonated_user_id: int,
    tenant_id: int,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Log service account impersonation.

    CMMC Requirement: AU-2 - Log privilege usage
    """
    event_metadata = {
        "service_user_id": service_user_id,
        "impersonated_user_id": impersonated_user_id,
        **(metadata or {})
    }
    return log_security_event(
        db=db,
        action=models.AuditAction.AUTH_IMPERSONATION,
        request=request,
        user_id=service_user_id,
        tenant_id=tenant_id,
        metadata=event_metadata,
    )


# =============================================================================
# PLATFORM ADMIN AUDIT FUNCTIONS (CMMC AC-5 / AU-2 Compliance)
# =============================================================================

_XTENANT_DEBOUNCE_SECONDS = 300  # 5-minute window per (actor, target_tenant)


def log_cross_tenant_data_access_blocked(
    db: Session,
    admin_user_id: int,
    target_tenant_id: int,
    request: Optional[Request] = None,
    resource_type: str = "cog",
    resource_id: Optional[int] = None,
    endpoint_path: Optional[str] = None,
    method: Optional[str] = None,
) -> Optional[models.PermissionAuditLog]:
    """Emit an audit event when a platform admin attempts to access tenant
    data they don't own AND is blocked by the data-isolation contract
    (data-files-launch-bulletproof §0; spec
    `tenant-data-isolation`).

    Distinct from `ADMIN_CROSS_TENANT_ACCESS` (which logs ALLOWED
    platform-admin actions on documented endpoints like /users/tenants).
    This event records the BLOCKED attempt — i.e., the platform admin
    tried to read/modify a cog in another tenant and got 403.

    Per-`(actor_user_id, target_tenant_id)` 5-minute debounce via Redis
    (sliding window with NX-set semantics). The user still receives a
    fresh 403 on every blocked request; only the audit row is debounced
    so polling loops don't flood the audit log. New target tenants reset
    the window (one event per (actor, target_tenant) pair).

    Returns the created audit log (or None if Redis unavailable + fallback
    or write failure). Audit failures never raise — always swallow."""
    # Guard: this audit is platform-admin-specific. Caller is expected to
    # check is_platform_admin before calling, but defensive double-check.
    if not admin_user_id or not target_tenant_id:
        return None

    # Redis debounce — if-not-exists set with TTL = window. Only emit
    # when the SET actually claims the key.
    debounce_key = f"audit_xtenant_blocked:{admin_user_id}:{target_tenant_id}"
    try:
        from utils.rate_limiter import _get_redis  # Re-use the existing client.
        r = _get_redis()
        if r is not None:
            claimed = r.set(debounce_key, "1", nx=True, ex=_XTENANT_DEBOUNCE_SECONDS)
            if not claimed:
                # Existing key in window — debounced. Skip the audit emit
                # but still return None to caller.
                return None
        # If Redis is unavailable, FAIL OPEN: emit the event. Better an
        # over-loud audit log than a silent compliance gap.
    except Exception:
        # Same fail-open posture on any Redis hiccup.
        pass

    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_CROSS_TENANT_DATA_ACCESS_BLOCKED,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata={
            "endpoint_path": endpoint_path,
            "method": method,
            "blocked_reason": "iso_27001_tenant_data_isolation",
        },
    )


def log_admin_action(
    db: Session,
    action: models.AuditAction,
    admin_user_id: int,
    request: Optional[Request] = None,
    target_tenant_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[models.PermissionAuditLog]:
    """
    Log platform admin actions for accountability.

    CMMC/ISO 27001 Compliance:
    - AC-5 (Separation of Duties) - Track admin actions separately
    - AU-2 (Audit Events) - Log all privileged operations
    - AU-6 (Audit Review) - Enable review of admin actions

    All platform admin actions are logged with full context for:
    - Security incident investigation
    - Compliance audits
    - Accountability tracking

    Args:
        db: Database session
        action: Admin audit action type (ADMIN_*)
        admin_user_id: Platform admin performing the action
        request: FastAPI request for IP/user-agent
        target_tenant_id: Tenant being affected (if applicable)
        target_user_id: User being affected (if applicable)
        resource_type: Type of resource being modified
        resource_id: ID of resource being modified
        metadata: Additional context (before/after values, reason, etc.)

    Returns:
        Created audit log entry or None if logging fails
    """
    try:
        # Extract request details
        ip_address = None
        user_agent = None
        if request:
            # GSEC R-145 — one trusted client-IP resolver (utils/client_ip.py).
            ip_address = trusted_client_ip(request)
            user_agent = request.headers.get("User-Agent")

        # Include comprehensive context in metadata
        event_metadata = {
            "admin_user_id": admin_user_id,
            "target_tenant_id": target_tenant_id,
            "target_user_id": target_user_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            **(metadata or {})
        }

        # Get previous hash for chain integrity
        # Platform-level actions are recorded against the Platform tenant (id 1).
        # tenant 0 does not exist: permission_audit_logs.tenant_id is an FK, so a
        # 0 row failed every flush — the audit was lost and the caller's session
        # was left in a failed transaction (POST /catalog/mine/codes 500'd on it).
        audit_tenant_id = target_tenant_id or PLATFORM_AUDIT_TENANT_ID
        previous_hash = _get_previous_hash(db, audit_tenant_id)

        # Prepare record data for integrity hash
        record_data = {
            "tenant_id": audit_tenant_id,
            "action": action.value if action else None,
            "actor_user_id": admin_user_id,
            "target_user_id": target_user_id,
            "resource_type": resource_type or "platform",
            "resource_id": resource_id or 0,
            "permission_level": None,
            "permission_id": None,
            "permission_request_id": None,
            "metadata": event_metadata,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "previous_hash": previous_hash,
        }

        # Compute integrity hash (CMMC AU-9)
        integrity_hash = _compute_audit_hash(record_data)

        audit_log = models.PermissionAuditLog(
            tenant_id=audit_tenant_id,
            action=action,
            actor_user_id=admin_user_id,
            target_user_id=target_user_id,
            resource_type=resource_type or "platform",
            resource_id=resource_id or 0,
            audit_metadata=event_metadata,
            ip_address=ip_address,
            user_agent=user_agent,
            integrity_hash=integrity_hash,
            previous_hash=previous_hash,
        )

        db.add(audit_log)
        db.commit()
        db.refresh(audit_log)

        # Also log to application logger for immediate visibility
        # CRITICAL: Admin actions are always logged at WARNING level for visibility
        logger.warning(
            f"ADMIN_AUDIT: action={action.value} admin_id={admin_user_id} "
            f"target_tenant={target_tenant_id} target_user={target_user_id} "
            f"ip={ip_address} resource={resource_type}:{resource_id}"
        )

        return audit_log

    except Exception as e:
        # GSEC R-144: WARNING + counter instead of a swallowed ERROR.
        _record_audit_write_failure("log_admin_action", e)
        # Don't raise - audit failures shouldn't break the application. Roll
        # back so a failed audit flush doesn't poison the caller's session
        # (PendingRollbackError on its next attribute access / query).
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# Convenience functions for specific admin actions

def log_admin_tenant_view(
    db: Session,
    admin_user_id: int,
    target_tenant_id: int,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin views tenant details."""
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_TENANT_VIEW,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        metadata=metadata,
    )


def log_admin_tenant_modify(
    db: Session,
    admin_user_id: int,
    target_tenant_id: int,
    request: Optional[Request] = None,
    changes: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin modifies tenant settings."""
    event_metadata = {
        "changes": changes,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_TENANT_MODIFY,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        metadata=event_metadata,
    )


def log_admin_tenant_delete(
    db: Session,
    admin_user_id: int,
    target_tenant_id: int,
    tenant_name: str,
    request: Optional[Request] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin deletes a tenant (CRITICAL action)."""
    event_metadata = {
        "tenant_name": tenant_name,
        "reason": reason,
        "severity": "CRITICAL",
        **(metadata or {})
    }
    logger.critical(
        f"ADMIN_CRITICAL: Tenant deletion - admin={admin_user_id} "
        f"tenant={target_tenant_id} ({tenant_name}) reason={reason}"
    )
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_TENANT_DELETE,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        metadata=event_metadata,
    )


def log_admin_user_view(
    db: Session,
    admin_user_id: int,
    target_user_id: int,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin views user details."""
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_USER_VIEW,
        admin_user_id=admin_user_id,
        request=request,
        target_user_id=target_user_id,
        metadata=metadata,
    )


def log_admin_user_modify(
    db: Session,
    admin_user_id: int,
    target_user_id: int,
    request: Optional[Request] = None,
    changes: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin modifies user account."""
    event_metadata = {
        "changes": changes,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_USER_MODIFY,
        admin_user_id=admin_user_id,
        request=request,
        target_user_id=target_user_id,
        metadata=event_metadata,
    )


def log_admin_user_deactivate(
    db: Session,
    admin_user_id: int,
    target_user_id: int,
    request: Optional[Request] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin deactivates user account."""
    event_metadata = {
        "reason": reason,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_USER_DEACTIVATE,
        admin_user_id=admin_user_id,
        request=request,
        target_user_id=target_user_id,
        metadata=event_metadata,
    )


def log_admin_user_delete(
    db: Session,
    admin_user_id: int,
    target_user_id: int,
    username: str,
    request: Optional[Request] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin deletes user (CRITICAL action)."""
    event_metadata = {
        "username": username,
        "reason": reason,
        "severity": "CRITICAL",
        **(metadata or {})
    }
    logger.critical(
        f"ADMIN_CRITICAL: User deletion - admin={admin_user_id} "
        f"user={target_user_id} ({username}) reason={reason}"
    )
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_USER_DELETE,
        admin_user_id=admin_user_id,
        request=request,
        target_user_id=target_user_id,
        metadata=event_metadata,
    )


def log_admin_config_change(
    db: Session,
    admin_user_id: int,
    config_key: str,
    old_value: Any,
    new_value: Any,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin changes system configuration."""
    event_metadata = {
        "config_key": config_key,
        "old_value": str(old_value)[:200],  # Truncate for safety
        "new_value": str(new_value)[:200],
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_CONFIG_CHANGE,
        admin_user_id=admin_user_id,
        request=request,
        resource_type="config",
        metadata=event_metadata,
    )


def log_admin_billing_modify(
    db: Session,
    admin_user_id: int,
    target_tenant_id: int,
    request: Optional[Request] = None,
    changes: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin modifies billing settings."""
    event_metadata = {
        "changes": changes,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_BILLING_MODIFY,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        resource_type="billing",
        metadata=event_metadata,
    )


def log_admin_credits_adjust(
    db: Session,
    admin_user_id: int,
    target_tenant_id: int,
    amount: int,
    request: Optional[Request] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin adjusts tenant credits."""
    event_metadata = {
        "amount": amount,
        "reason": reason,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_CREDITS_ADJUST,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        resource_type="credits",
        metadata=event_metadata,
    )


def log_admin_data_export(
    db: Session,
    admin_user_id: int,
    target_tenant_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
    export_type: str = "unknown",
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin exports data (potential data exfiltration tracking)."""
    event_metadata = {
        "export_type": export_type,
        **(metadata or {})
    }
    logger.warning(
        f"ADMIN_DATA_EXPORT: admin={admin_user_id} export_type={export_type} "
        f"tenant={target_tenant_id} user={target_user_id}"
    )
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_DATA_EXPORT,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        target_user_id=target_user_id,
        resource_type="data_export",
        metadata=event_metadata,
    )


def log_admin_security_override(
    db: Session,
    admin_user_id: int,
    override_type: str,
    request: Optional[Request] = None,
    target_tenant_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin overrides security controls (CRITICAL action)."""
    event_metadata = {
        "override_type": override_type,
        "reason": reason,
        "severity": "CRITICAL",
        **(metadata or {})
    }
    logger.critical(
        f"ADMIN_SECURITY_OVERRIDE: admin={admin_user_id} type={override_type} "
        f"tenant={target_tenant_id} user={target_user_id} reason={reason}"
    )
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_SECURITY_OVERRIDE,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        target_user_id=target_user_id,
        resource_type="security_override",
        metadata=event_metadata,
    )


def log_admin_audit_log_view(
    db: Session,
    admin_user_id: int,
    request: Optional[Request] = None,
    target_tenant_id: Optional[int] = None,
    query_params: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log when admin views audit logs (audit the auditor)."""
    event_metadata = {
        "query_params": query_params,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_AUDIT_LOG_VIEW,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        resource_type="audit_logs",
        metadata=event_metadata,
    )


def log_admin_system_maintenance(
    db: Session,
    admin_user_id: int,
    maintenance_type: str,
    request: Optional[Request] = None,
    affected_services: Optional[list] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log system maintenance actions."""
    event_metadata = {
        "maintenance_type": maintenance_type,
        "affected_services": affected_services,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_SYSTEM_MAINTENANCE,
        admin_user_id=admin_user_id,
        request=request,
        resource_type="system",
        metadata=event_metadata,
    )


def log_admin_feature_flag_change(
    db: Session,
    admin_user_id: int,
    flag_name: str,
    old_value: Any,
    new_value: Any,
    request: Optional[Request] = None,
    target_tenant_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Log feature flag changes."""
    event_metadata = {
        "flag_name": flag_name,
        "old_value": old_value,
        "new_value": new_value,
        **(metadata or {})
    }
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_FEATURE_FLAG_CHANGE,
        admin_user_id=admin_user_id,
        request=request,
        target_tenant_id=target_tenant_id,
        resource_type="feature_flag",
        metadata=event_metadata,
    )


# =============================================================================
# TPN (Trusted Partner Network) CONTENT SECURITY AUDIT FUNCTIONS
# =============================================================================
# MPA Content Security Best Practices v4.0+ Compliance
#
# These functions provide audit logging for film/TV content handling
# required for TPN certification. Key control areas:
# - CS-1: Asset Management & Content Tracking
# - CS-5: Logging & Monitoring
# - CS-6: Incident Response
# - CS-7: Content Transfer Security
# - CS-8: Content Destruction
# =============================================================================


def log_content_share_link_created(
    db: Session,
    user_id: int,
    tenant_id: int,
    cog_id: int,
    share_link_id: str,
    expiry_datetime: Optional[datetime] = None,
    password_protected: bool = False,
    max_downloads: Optional[int] = None,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[models.PermissionAuditLog]:
    """
    Log share link creation.

    TPN CS-7: Content Transfer Security - Track external sharing mechanisms
    """
    event_metadata = {
        "share_link_id": share_link_id,
        "expiry_datetime": expiry_datetime.isoformat() if expiry_datetime else None,
        "password_protected": password_protected,
        "max_downloads": max_downloads,
        **(metadata or {})
    }

    logger.warning(
        f"TPN_AUDIT: Share link created for cog={cog_id} by user={user_id} "
        f"link_id={share_link_id} tenant={tenant_id}"
    )

    return log_permission_audit(
        db=db,
        action=models.AuditAction.CONTENT_SHARE_LINK_CREATED,
        actor_user_id=user_id,
        tenant_id=tenant_id,
        resource_type="cog",
        resource_id=cog_id,
        metadata=event_metadata,
        request=request,
    )


def log_content_share_link_accessed(
    db: Session,
    tenant_id: int,
    cog_id: int,
    share_link_id: str,
    accessor_user_id: Optional[int] = None,
    request: Optional[Request] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[models.PermissionAuditLog]:
    """
    Log when a share link is used to access content.

    TPN CS-7: Content Transfer Security - Track external access
    """
    event_metadata = {
        "share_link_id": share_link_id,
        "accessor_type": "authenticated" if accessor_user_id else "anonymous",
        **(metadata or {})
    }

    return log_permission_audit(
        db=db,
        action=models.AuditAction.CONTENT_SHARE_LINK_ACCESSED,
        actor_user_id=accessor_user_id or _system_user_id(db),
        tenant_id=tenant_id,
        resource_type="cog",
        resource_id=cog_id,
        metadata=event_metadata,
        request=request,
    )


# =============================================================================
# Resource monitor — tenant-resource-monitor
# =============================================================================

def log_resource_leak_detected(
    db: Session,
    tenant_id: int,
    user_id: Optional[int],
    ratios: Dict[str, float],
    thresholds: Dict[str, float],
    resources_above_threshold: tuple,
    native_totals: Dict[str, float],
    credits_used: int,
    top_tasks: list,
    window_seconds: int = 86400,
) -> Optional[models.PermissionAuditLog]:
    """Emit RESOURCE_LEAK_DETECTED with per-resource native breakdown.

    System-emitted (no human actor). Caller is responsible for the
    24h debounce per (tenant_id, user_id) — the audit table is the
    source of truth for "has this offender been flagged recently?".
    See ``has_recent_leak_event`` for the check.

    Audit metadata captures *which* resource(s) crossed their
    threshold so forensic queries can answer "is this user abusing
    GPU specifically?" rather than the older single-ratio rollup.
    """
    event_metadata = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "ratios": ratios,
        "thresholds": thresholds,
        "resources_above_threshold": list(resources_above_threshold),
        "native_totals": native_totals,
        "credits_used": credits_used,
        "window_seconds": window_seconds,
        "top_tasks": top_tasks[:5],
    }
    logger.warning(
        "RESOURCE_LEAK_DETECTED: tenant=%s user=%s above=%s credits=%s ratios=%s",
        tenant_id, user_id,
        ",".join(resources_above_threshold) or "(none)",
        credits_used, ratios,
    )
    return log_permission_audit(
        db=db,
        action=models.AuditAction.RESOURCE_LEAK_DETECTED,
        actor_user_id=_system_user_id(db),  # System-emitted (mod-system)
        tenant_id=tenant_id,
        resource_type="resource_monitor",
        resource_id=user_id or 0,
        metadata=event_metadata,
    )


def log_admin_resource_monitor_settings_updated(
    db: Session,
    admin_user_id: int,
    changes: Dict[str, Any],
    request: Optional[Request] = None,
) -> Optional[models.PermissionAuditLog]:
    """Emit ADMIN_RESOURCE_MONITOR_SETTINGS_UPDATED for a rate/threshold change."""
    return log_admin_action(
        db=db,
        action=models.AuditAction.ADMIN_RESOURCE_MONITOR_SETTINGS_UPDATED,
        admin_user_id=admin_user_id,
        request=request,
        resource_type="resource_monitor_config",
        metadata={"changes": changes},
    )


def log_harbor_scan_retriggered(
    db: Session,
    tenant_id: int,
    repository: str,
    digest: str,
    previous_status: Optional[str],
    push_time: Optional[str],
    workflow_run_id: Optional[int],
) -> Optional[models.PermissionAuditLog]:
    """Emit HARBOR_SCAN_RETRIGGERED. System-emitted (no human actor).

    Called by the harbor-followup retrigger endpoint when the
    HarborScanFollowup container reports a successful manual /scan
    POST against a stuck artifact. The audit row carries enough
    context that an operator can answer "was this image ever
    retriggered?" with a single audit-log query.
    """
    event_metadata = {
        "tenant_id": tenant_id,
        "repository": repository,
        "digest": digest,
        "previous_status": previous_status,
        "push_time": push_time,
        "workflow_run_id": workflow_run_id,
    }
    logger.info(
        "HARBOR_SCAN_RETRIGGERED: tenant=%s repo=%s digest=%s prev_status=%s",
        tenant_id, repository, digest[:23] if digest else None, previous_status,
    )
    return log_permission_audit(
        db=db,
        action=models.AuditAction.HARBOR_SCAN_RETRIGGERED,
        actor_user_id=_system_user_id(db),  # System-emitted (mod-system)
        tenant_id=tenant_id,
        resource_type="harbor_artifact",
        resource_id=0,
        metadata=event_metadata,
    )


def has_recent_leak_event(
    db: Session,
    tenant_id: int,
    user_id: Optional[int],
    window_seconds: int = 86400,
) -> bool:
    """Debounce check — has RESOURCE_LEAK_DETECTED fired for this
    (tenant, user) within ``window_seconds``? Reads the audit row's
    resource_id (set to user_id by the helper above, 0 for
    standalone tasks).
    """
    from datetime import timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    q = db.query(models.PermissionAuditLog).filter(
        models.PermissionAuditLog.tenant_id == tenant_id,
        models.PermissionAuditLog.action == models.AuditAction.RESOURCE_LEAK_DETECTED,
        models.PermissionAuditLog.timestamp >= cutoff,
    )
    if user_id is None:
        q = q.filter(models.PermissionAuditLog.resource_id == 0)
    else:
        q = q.filter(models.PermissionAuditLog.resource_id == user_id)
    return db.query(q.exists()).scalar() is True


# =============================================================================
# DATA LIFECYCLE — wizard-rebuild §11b
# =============================================================================

def log_resource_lifecycle_event(
    db: Session,
    action: models.AuditAction,
    actor_user_id: int,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> Optional[models.PermissionAuditLog]:
    """Audit-log a resource lifecycle transition (soft-delete, hard-delete,
    retention-expiry). Distinguished from ``log_admin_action`` in that it
    doesn't require platform-admin privileges or tenant-0 attribution —
    a regular tenant user soft-deleting their own node hits this path.

    Returns the created row, or None if the audit insert failed
    (audit failures must never break the calling code path —
    matches the convention used by every other helper in this file).
    """
    try:
        ip_address = None
        user_agent = None
        if request:
            # GSEC R-145 — one trusted client-IP resolver (utils/client_ip.py).
            ip_address = trusted_client_ip(request)
            user_agent = request.headers.get("User-Agent")

        event_metadata = dict(metadata or {})

        previous_hash = _get_previous_hash(db, tenant_id)
        record_data = {
            "tenant_id": tenant_id,
            "action": action.value if action else None,
            "actor_user_id": actor_user_id,
            "target_user_id": None,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "permission_level": None,
            "permission_id": None,
            "permission_request_id": None,
            "metadata": event_metadata,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "previous_hash": previous_hash,
        }
        integrity_hash = _compute_audit_hash(record_data)

        audit_log = models.PermissionAuditLog(
            tenant_id=tenant_id,
            action=action,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            audit_metadata=event_metadata,
            ip_address=ip_address,
            user_agent=user_agent,
            integrity_hash=integrity_hash,
            previous_hash=previous_hash,
        )
        db.add(audit_log)
        db.commit()
        db.refresh(audit_log)
        return audit_log
    except Exception as e:
        # GSEC R-144: WARNING + counter (site label carries the action so a
        # specific lifecycle event's audit gap is attributable in the metric).
        _record_audit_write_failure(
            f"log_resource_lifecycle:{getattr(action, 'value', action)}", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def log_soft_delete(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    metadata: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> Optional[models.PermissionAuditLog]:
    """Thin wrapper over ``log_resource_lifecycle_event`` for the
    ``CONTENT_DELETE_INITIATED`` event emitted by every user-initiated
    soft-delete on the five wizard-managed tables (§11b)."""
    return log_resource_lifecycle_event(
        db=db,
        action=models.AuditAction.CONTENT_DELETE_INITIATED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
        request=request,
    )


def log_hard_delete_retention(
    db: Session,
    actor_user_id: int,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[models.PermissionAuditLog]:
    """Thin wrapper over ``log_resource_lifecycle_event`` for the
    ``CONTENT_RETENTION_EXPIRED`` event emitted by the §11b GC sweep
    every time it hard-deletes a row whose ``deleted_at`` is older
    than the tenant's ``retention_days``.

    ``actor_user_id`` is the principal under whose authority the GC
    is recorded as having acted — typically the tenant's first
    platform admin. The scheduler caller resolves this; if no admin
    exists for the tenant the GC still proceeds but the audit row
    isn't written (logged as an error)."""
    return log_resource_lifecycle_event(
        db=db,
        action=models.AuditAction.CONTENT_RETENTION_EXPIRED,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
    )
