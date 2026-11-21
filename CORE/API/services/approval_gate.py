"""GSEC FX.163 p2 [B] (owner decision Q150=A) — the approval SERVICE + the
gate: maker-checker enforcement for the four sensitive tenant actions.

FX.163 p1 shipped the DATA half (models.PendingApproval + the tenant
setting + the single enforcement read services/approvals.approvals_required).
This module is the DECISION half: it turns one sensitive call into

    request_approval(db, tenant_id, action, payload, requester)
        -> PendingApproval row ('pending', expires_at stamped)

and lets a DIFFERENT tenant admin decide it:

    approve(db, approval_id, approver)   -> replays the stored action
    reject(db, approval_id, approver)    -> records the refusal

Every gate call site asks ``approvals_required(db, tenant_id)`` first, so
when the switch is OFF the action runs byte-for-byte as before and this
module is never touched. When it is ON the caller returns 202
``{"status": "awaiting_approval", "approval_id": ...}`` instead of
executing.

Gated actions (the stable keys models.PendingApproval documents):

    role_grant            routers/rbac.py PUT /users/{id}/assign-role
    full_export           routers/tenant/export.py POST /tenant/export
    byok_revoke           routers/tenant/byok.py DELETE /tenant/byok
    data_marking_clear    routers/tenant/data_markings.py
                          DELETE /data-markings/{type}/{id}/{label}
    iam_preset_apply      routers/resource_perms/iam_permissions.py
                          POST /iam/presets/apply (the sibling role surface
                          the p2 pass missed — same role-grant semantics:
                          a preset rewrites a user's whole capability set)

Replay: ``approve`` re-runs the stored action through the HANDLER
REGISTRY (_ACTION_HANDLERS) with the row's own ``payload`` — the
requester's identity is the row, not the caller, so the approver never
becomes the acting party. Before replay the service re-hashes the
payload and refuses on a mismatch with the stored
``payload_sha256`` (canonical JSON, the same digest shape
tests/test_fx163_p1_pending_approvals.py computes), so an approval can
never prove one payload while a different one runs.

Maker-checker is enforced twice: the DB CHECK
``ck_pending_approvals_second_approver`` refuses the row outright, and
``approve``/``reject`` raise ApprovalSelfRefusalError (a 403 at the
router) before anything else, audited so a self-approval attempt is
visible in the trail (FX.163 p4's ISMS doc reviews exactly those).

Both actors are audited, append-only, reusing existing AuditAction
values (PERMISSION_REQUESTED for the request, PERMISSION_REQUEST_
APPROVED / PERMISSION_REQUEST_DENIED for the decision, SECURITY_
INCIDENT_CREATED for a refused self-approval or an expiry) — the same
reuse pattern services/export_approval.py uses; no new enum members,
no new table.

Expiry: ``approvals.pending_approval_expiry_hours`` (registered at the
END of services/platform_settings.py, default 72, modelled on
dlp.export_approval_expiry_hours). A pending row past its deadline is
stamped 'expired' LAZILY on the next read/attempt (never by a hard
delete — ISO 27001: everything here is a status stamp or a
``deleted_at`` marker).

No secrets in payloads: the payload carries identifiers only (user ids,
role ids, resource ids/labels, tenant ids). The BYOK payload carries the
provider NAME and the key REF (already a tenant-scoped settings value,
never key material).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from services._base import (ConflictError, NotFoundError, ServiceError)

logger = logging.getLogger(__name__)

EXPIRY_HOURS_KEY = "approvals.pending_approval_expiry_hours"
DEFAULT_EXPIRY_HOURS = 72
REASON_MAX_LEN = 2000
DECISION_NOTE_MAX_LEN = 2000

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

#: audit-metadata kind tags (rows reuse existing AuditAction values).
AUDIT_KIND_CREATED = "pending_approval_created"
AUDIT_KIND_APPROVED = "pending_approval_approved"
AUDIT_KIND_REJECTED = "pending_approval_rejected"
AUDIT_KIND_SELF_REFUSED = "pending_approval_self_refused"
AUDIT_KIND_EXPIRED = "pending_approval_expired"
AUDIT_KIND_REPLAYED = "pending_approval_replayed"

ACTION_ROLE_GRANT = "role_grant"
ACTION_FULL_EXPORT = "full_export"
ACTION_BYOK_REVOKE = "byok_revoke"
ACTION_DATA_MARKING_CLEAR = "data_marking_clear"
ACTION_IAM_PRESET_APPLY = "iam_preset_apply"
ACTION_MOBILE_DEVICE_REVOKE = "mobile_device_revoke"
ACTION_KEYS = (ACTION_ROLE_GRANT, ACTION_FULL_EXPORT,
               ACTION_BYOK_REVOKE, ACTION_DATA_MARKING_CLEAR,
               ACTION_IAM_PRESET_APPLY, ACTION_MOBILE_DEVICE_REVOKE)


class ApprovalSelfRefusalError(ServiceError):
    """The decider IS the requester (segregation of duties). A 403 at
    the router; audited as a security incident so the attempt is
    reviewable (FX.163 p4)."""


class ApprovalExpiredError(ServiceError):
    """The row is past ``expires_at`` (or already decided/cancelled).
    A 409 at the router."""


class ApprovalPayloadMismatchError(ServiceError):
    """The payload the approver is asked to sign no longer hashes to the
    row's ``payload_sha256`` — the stored action was tampered with
    between request and approval. A 409 at the router."""


class ApprovalValidationError(ServiceError):
    """Unknown action key / malformed payload. A 400 at the router."""


# =============================================================================
# Canonical payload digest (the row's payload_sha256)
# =============================================================================


def _canonical(payload: Any) -> str:
    """The canonical JSON text of a payload — the exact shape
    tests/test_fx163_p1_pending_approvals.py hashes (sorted keys, tight
    separators), so a digest written by p1 tests and by this service
    are the same string."""
    return json.dumps(payload or {}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8").decode(
                          "utf-8")


def payload_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    """DB rows hand back naive datetimes on the SQLite test backend;
    comparisons need tz-aware ones (mirrors services/export_approval.py)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expiry_hours(db: Session) -> int:
    """The deadline knob (platform setting). Read failure -> documented
    default, never an exception."""
    from services import platform_settings
    try:
        value = platform_settings.get_setting(EXPIRY_HOURS_KEY, db=db)
    except Exception as exc:  # noqa: BLE001 — degrade, never break a gate
        logger.warning("FX.163 p2: expiry-hours read failed (defaulting): "
                       "%s", exc)
        value = None
    try:
        hours = int(value)
    except (TypeError, ValueError):
        hours = DEFAULT_EXPIRY_HOURS
    return hours if hours > 0 else DEFAULT_EXPIRY_HOURS


def _audit(db, *, action: models.AuditAction, kind: str, tenant_id: int,
           user_id: Optional[int] = None, request: Any = None,
           extra: Optional[dict] = None) -> None:
    """One append-only audit row; a write failure never blocks the
    caller (the FX.161.p2a audit seam, copied so both halves of the
    vertical behave identically)."""
    try:
        from audit import log_security_event
        metadata = {"kind": kind}
        if extra:
            metadata.update(extra)
        log_security_event(db=db, action=action, request=request,
                           user_id=user_id, tenant_id=tenant_id,
                           metadata=metadata)
    except Exception as exc:  # noqa: BLE001 — audit must not block the flow
        logger.warning("FX.163 p2: audit row failed kind=%s: %s", kind, exc)


def _is_expired(row, now=None) -> bool:
    exp = _aware(getattr(row, "expires_at", None))
    return exp is not None and (now or _now()) >= exp


def _expire(db, *, row, request: Any = None) -> None:
    """Stamp a past-deadline pending/approved row 'expired' + audit.
    Idempotent; never deletes."""
    if row.status not in (STATUS_PENDING, STATUS_APPROVED):
        return
    previous = row.status
    row.status = STATUS_EXPIRED
    db.commit()
    _audit(db, action=models.AuditAction.SECURITY_INCIDENT_CREATED,
           kind=AUDIT_KIND_EXPIRED, tenant_id=row.tenant_id,
           user_id=row.requested_by_user_id,
           request=request,
           extra={"approval_id": row.id, "action": row.action,
                  "requested_by_user_id": row.requested_by_user_id,
                  "previous_status": previous})


def get_row(db, tenant_id: int, approval_id: int):
    """Tenant-scoped fetch of one live row. Raises NotFoundError for an
    unknown / foreign-tenant id (the id alone never leaks another
    tenant's row state)."""
    row = (
        db.query(models.PendingApproval)
        .filter(models.PendingApproval.id == approval_id,
                models.PendingApproval.tenant_id == tenant_id,
                models.PendingApproval.deleted_at.is_(None))
        .first()
    )
    if row is None:
        raise NotFoundError("Pending approval not found")
    return row


def list_rows(db, tenant_id: int, status_filter: Optional[str] = None,
              limit: int = 50):
    """The tenant's own pending-approval rows, newest first (never
    another tenant's). Lapsed pending/approved rows are stamped 'expired'
    as they are seen, so the list never shows a dead row as live."""
    q = (db.query(models.PendingApproval)
         .filter(models.PendingApproval.tenant_id == tenant_id,
                 models.PendingApproval.deleted_at.is_(None)))
    if status_filter:
        q = q.filter(models.PendingApproval.status == status_filter)
    rows = (q.order_by(models.PendingApproval.requested_at.desc(),
                       models.PendingApproval.id.desc())
            .limit(max(1, min(int(limit or 50), 200))).all())
    for row in rows:
        if row.status in (STATUS_PENDING, STATUS_APPROVED) and _is_expired(
                row):
            _expire(db, row=row)
    return rows


# =============================================================================
# The request side
# =============================================================================

_PAYLOAD_SHAPES: Dict[str, tuple] = {
    ACTION_ROLE_GRANT: ("user_id", "role_id"),
    ACTION_FULL_EXPORT: (),
    ACTION_BYOK_REVOKE: ("provider", "key_ref"),
    ACTION_DATA_MARKING_CLEAR: ("resource_type", "resource_id", "label"),
    ACTION_IAM_PRESET_APPLY: ("user_id", "preset"),
    ACTION_MOBILE_DEVICE_REVOKE: ("device_id",),
}


def _validate_payload(action: str, payload: Any) -> Dict[str, Any]:
    """Normalise the replay payload to identifiers only, per action key.
    Raises ApprovalValidationError (a 400 at the router) on an unknown
    action or a malformed payload — a gate must never store a row it
    cannot replay."""
    if action not in _PAYLOAD_SHAPES:
        raise ApprovalValidationError(
            f"Unknown approval action {action!r} "
            f"(known: {', '.join(ACTION_KEYS)})")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ApprovalValidationError("payload must be a JSON object")
    required = _PAYLOAD_SHAPES[action]
    out: Dict[str, Any] = {}
    for key in required:
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ApprovalValidationError(
                f"payload for {action} requires a non-empty {key!r}")
        if key in ("user_id", "role_id"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ApprovalValidationError(
                    f"payload for {action} requires {key!r} to be an id")
            if value <= 0:
                raise ApprovalValidationError(
                    f"payload for {action} requires a positive {key!r}")
        elif key == "resource_id":
            value = str(value)
        elif key in ("provider", "label", "resource_type", "preset"):
            value = str(value).strip()
        out[key] = value
    # Anything else the caller smuggled in is dropped: the row stores
    # identifiers only, never secrets.
    return out


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _get_open(db, tenant_id: int, action: str, payload: dict,
              requester_id: Optional[int] = None):
    """The tenant's newest unexpired PENDING row for this exact
    action+payload (and requester, when known); a lapsed one is
    expired in place and the search restarts."""
    digest = payload_digest(payload)
    while True:
        q = (db.query(models.PendingApproval)
             .filter(models.PendingApproval.tenant_id == tenant_id,
                     models.PendingApproval.action == action,
                     models.PendingApproval.status == STATUS_PENDING,
                     models.PendingApproval.payload_sha256 == digest,
                     models.PendingApproval.deleted_at.is_(None)))
        if requester_id is not None:
            q = q.filter(
                models.PendingApproval.requested_by_user_id
                == requester_id)
        row = (q.order_by(models.PendingApproval.requested_at.desc(),
                          models.PendingApproval.id.desc())
               .first())
        if row is None:
            return None
        if _is_expired(row):
            _expire(db, row=row)
            continue
        return row


def request_approval(db, tenant_id: int, action: str, payload: Any,
                     requester, reason: Optional[str] = None,
                     request: Any = None) -> models.PendingApproval:
    """Record one sensitive tenant action as a PENDING second-approver
    row and DO NOT run it.

    Only called when ``approvals_required(db, tenant_id)`` is True — the
    callers check that first, so an install with the switch OFF never
    reaches here and its behaviour is unchanged.

    Idempotent per (tenant, action, payload, requester): a repeat call
    while an unexpired pending row for the SAME action+payload is open
    returns that row instead of stacking duplicates (the FX.161.p2a
    export-request precedent). ``payload_sha256`` is the canonical-JSON
    digest of the stored payload, so the decision later proves THAT
    exact payload was the one approved.
    """
    payload = _validate_payload(action, payload)
    existing = _get_open(db, tenant_id, action, payload,
                         requester_id=getattr(requester, "id", None))
    if existing is not None:
        return existing
    reason = (reason or "").strip() or None
    if reason and len(reason) > REASON_MAX_LEN:
        reason = reason[:REASON_MAX_LEN]
    row = models.PendingApproval(
        tenant_id=tenant_id,
        action=action,
        payload=payload,
        payload_sha256=payload_digest(payload),
        requested_by_user_id=getattr(requester, "id", None),
        reason=reason,
        expires_at=_now() + timedelta(hours=expiry_hours(db)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit(db, action=models.AuditAction.PERMISSION_REQUESTED,
           kind=AUDIT_KIND_CREATED, tenant_id=tenant_id,
           user_id=row.requested_by_user_id, request=request,
           extra={"approval_id": row.id, "action": action,
                  "requested_by_user_id": row.requested_by_user_id,
                  "payload_sha256": row.payload_sha256,
                  "expires_at": _iso(row.expires_at)})
    return row


# =============================================================================
# The decision side
# =============================================================================


def _require_second_approver(db, row, decider, request=None) -> None:
    """The service-level maker-checker (the DB CHECK is the backstop).
    A self-approval is refused AND audited — FX.163 p4's ISMS doc is
    exactly about reviewing those recorded attempts."""
    decider_id = getattr(decider, "id", None)
    if decider_id is None:
        raise ApprovalValidationError("decider has no id")
    if getattr(decider, "tenant_id", None) != row.tenant_id:
        raise NotFoundError("Pending approval not found")
    if decider_id == row.requested_by_user_id:
        _audit(db, action=models.AuditAction.SECURITY_INCIDENT_CREATED,
               kind=AUDIT_KIND_SELF_REFUSED, tenant_id=row.tenant_id,
               user_id=decider_id, request=request,
               extra={"approval_id": row.id, "action": row.action,
                      "requested_by_user_id": row.requested_by_user_id})
        raise ApprovalSelfRefusalError(
            "The requester cannot approve their own request "
            "(a different tenant admin must decide it).")


def _require_decidable(db, row, request=None) -> None:
    """Only a live PENDING row can be decided; a lapsed one is stamped
    'expired' in place first (lazy expiry, never a delete)."""
    if row.status in (STATUS_PENDING, STATUS_APPROVED) and _is_expired(
            row):
        _expire(db, row=row, request=request)
        raise ApprovalExpiredError(
            "Pending approval has expired (its deadline passed before a "
            "second admin decided it).")
    if row.status != STATUS_PENDING:
        raise ApprovalExpiredError(
            f"Pending approval was already decided "
            f"(status={row.status}).")


def _check_payload(db, row) -> None:
    """Re-hash the stored payload against the row's digest. A mismatch
    means the replay detail was tampered with after the request, so the
    approval must not run it."""
    if payload_digest(row.payload or {}) != row.payload_sha256:
        _audit(db, action=models.AuditAction.SECURITY_INCIDENT_CREATED,
               kind="pending_approval_payload_mismatch",
               tenant_id=row.tenant_id,
               extra={"approval_id": row.id, "action": row.action,
                      "stored_sha256": row.payload_sha256})
        raise ApprovalPayloadMismatchError(
            "The stored payload no longer matches its digest "
            "(tampered between request and approval).")


def cancel_approval(db, approval_id: int, requester,
                    request: Any = None) -> models.PendingApproval:
    """Withdraw an open request before any decision (the requester only,
    same tenant). Soft state change: status 'cancelled', never a delete."""
    row = get_row(db, getattr(requester, "tenant_id", None), approval_id)
    if getattr(requester, "id", None) != row.requested_by_user_id:
        raise NotFoundError("Pending approval not found")
    if row.status not in (STATUS_PENDING, STATUS_APPROVED):
        raise ApprovalExpiredError(
            "Only an open pending approval can be cancelled.")
    row.status = STATUS_CANCELLED
    row.decided_at = _now()
    db.commit()
    _audit(db, action=models.AuditAction.PERMISSION_REQUEST_CANCELLED,
           kind="pending_approval_cancelled", tenant_id=row.tenant_id,
           user_id=row.requested_by_user_id, request=request,
           extra={"approval_id": row.id, "action": row.action})
    return row


def approve(db, approval_id: int, approver, decision_note: Optional[str]
            = None, request: Any = None) -> Tuple[models.PendingApproval,
                                                  Dict[str, Any]]:
    """Approve AND run the stored action.

    Returns ``(row, result)``: ``result`` is the handler's replay result
    (JSON-safe) and may carry ``_background`` — a list of
    ``(callable, args)`` the ROUTER must hand to BackgroundTasks (the
    full export's zip build, exactly as POST /tenant/export does today).

    Refusals: 404-shaped NotFoundError for an unknown/foreign row,
    ApprovalSelfRefusalError (403) when the approver is the requester,
    ApprovalExpiredError (409) when the row is lapsed/decided,
    ApprovalPayloadMismatchError (409) on a tampered payload. Both
    actors end up in the audit trail: the request row audited the
    requester, this call audits the approver.
    """
    row = get_row(db, getattr(approver, "tenant_id", None), approval_id)
    _require_second_approver(db, row, approver, request=request)
    _require_decidable(db, row, request=request)
    _check_payload(db, row)
    handler = _ACTION_HANDLERS.get(row.action)
    if handler is None:  # a row whose action key lost its handler
        raise ApprovalValidationError(
            f"No handler registered for action {row.action!r}.")
    result = handler(db, row=row, approver=approver, request=request)
    note = (decision_note or "").strip() or None
    if note and len(note) > DECISION_NOTE_MAX_LEN:
        note = note[:DECISION_NOTE_MAX_LEN]
    row.status = STATUS_APPROVED
    row.approved_by_user_id = getattr(approver, "id", None)
    row.decided_at = _now()
    row.decision_note = note
    db.commit()
    _audit(db, action=models.AuditAction.PERMISSION_REQUEST_APPROVED,
           kind=AUDIT_KIND_APPROVED, tenant_id=row.tenant_id,
           user_id=row.approved_by_user_id, request=request,
           extra={"approval_id": row.id, "action": row.action,
                  "requested_by_user_id": row.requested_by_user_id,
                  "approver_user_id": row.approved_by_user_id,
                  "payload_sha256": row.payload_sha256})
    return row, result


def reject(db, approval_id: int, decider, decision_note: Optional[str]
           = None, request: Any = None) -> models.PendingApproval:
    """Refuse a pending request: the stored action NEVER runs. Same
    refusal rules as approve (self-decision 403, lapsed 409)."""
    row = get_row(db, getattr(decider, "tenant_id", None), approval_id)
    _require_second_approver(db, row, decider, request=request)
    _require_decidable(db, row, request=request)
    note = (decision_note or "").strip() or None
    if note and len(note) > DECISION_NOTE_MAX_LEN:
        note = note[:DECISION_NOTE_MAX_LEN]
    row.status = STATUS_REJECTED
    row.approved_by_user_id = getattr(decider, "id", None)
    row.decided_at = _now()
    row.decision_note = note
    db.commit()
    _audit(db, action=models.AuditAction.PERMISSION_REQUEST_DENIED,
           kind=AUDIT_KIND_REJECTED, tenant_id=row.tenant_id,
           user_id=row.approved_by_user_id, request=request,
           extra={"approval_id": row.id, "action": row.action,
                  "requested_by_user_id": row.requested_by_user_id,
                  "decided_by_user_id": row.approved_by_user_id})
    return row


def awaiting_approval(row) -> Dict[str, Any]:
    """The 202 body every gated call site returns instead of executing."""
    return {
        "status": "awaiting_approval",
        "approval_id": row.id,
        "action": row.action,
        "requested_by_user_id": row.requested_by_user_id,
        "expires_at": _iso(row.expires_at),
        "message": "A different tenant admin must approve this request "
                   "(POST /v1/pending-approvals/{approval_id}/approve) "
                   "before the action runs.",
    }


# =============================================================================
# The replay registry: action key -> handler(db, row=, approver=, request=)
# Each handler re-runs the ORIGINAL action with the row's REQUESTER as the
# acting party (the approver only signs off), through the very same service
# call the ungated route makes. Services are imported lazily so this module
# stays import-cheap and cycle-free.
# =============================================================================


def _user_by_id(db, user_id: int):
    from crud import rbac as rbac_crud
    return rbac_crud.get_user_by_id(db, user_id)


def _replay_role_grant(db, *, row, approver, request=None):
    from services import rbac_service
    payload = row.payload or {}
    requester = _user_by_id(db, row.requested_by_user_id)
    if requester is None:
        raise NotFoundError("Requesting user no longer exists")
    rbac_service.assign_role(
        db, requester, user_id=payload["user_id"],
        role_id=payload["role_id"])
    return {"status": "applied", "user_id": payload["user_id"],
            "role_id": payload["role_id"]}


def _replay_full_export(db, *, row, approver, request=None):
    # 1788d CE carve: services/tenant_export.py is paid (manifest deny 1137).
    # A full tenant export is a paid capability; the replay answers 402 instead
    # of a 500 (the approvals row that queues it is itself a paid feature).
    from utils.paid_import import optional_paid
    from fastapi import HTTPException
    tenant_export = optional_paid("services.tenant_export")
    if tenant_export is None:
        raise HTTPException(
            status_code=402,
            detail="Tenant export is not available in MOD "
                   "Community Edition.",
        )
    requester = _user_by_id(db, row.requested_by_user_id)
    if requester is None:
        raise NotFoundError("Requesting user no longer exists")
    if tenant_export.running_job(db, row.tenant_id):
        raise ConflictError(
            "A tenant export is already in progress for this tenant")
    job = tenant_export.create_job(
        db, user=requester, tenant_id=row.tenant_id, request=request)
    return {"status": "job_started", "job_id": job.id,
            "_background": [(tenant_export.run_job, (job.id,))]}


def _replay_byok_revoke(db, *, row, approver, request=None):
    from services import platform_settings as psr
    import audit
    tenant_id = row.tenant_id
    psr.set_setting("kms.byok_provider", "", db=db,
                    actor_user_id=row.requested_by_user_id,
                    tenant_id=tenant_id)
    psr.set_setting("kms.byok_key_ref", "", db=db,
                    actor_user_id=row.requested_by_user_id,
                    tenant_id=tenant_id)
    audit.log_security_event(
        db, action=models.AuditAction.SECURITY_CONFIG_ERROR,
        user_id=row.requested_by_user_id, tenant_id=tenant_id,
        metadata={"event": "kms_byok_cleared", "approval_id": row.id,
                  "approved_by_user_id": getattr(approver, "id", None)})
    db.commit()
    return {"status": "cleared", "provider": "", "key_ref": "",
            "tenant_id": tenant_id}


def _replay_data_marking_clear(db, *, row, approver, request=None):
    from utils.paid_import import require_paid
    markings = require_paid("services.data_markings", "data markings")  # FX.162.p5 — CE carve: marking op refused
    payload = row.payload or {}
    requester = _user_by_id(db, row.requested_by_user_id)
    if requester is None:
        raise NotFoundError("Requesting user no longer exists")
    markings.clear_marking(
        db, requester,
        resource_type=payload["resource_type"],
        resource_id=payload["resource_id"],
        label_code=payload["label"])
    return {"status": "cleared",
            "resource_type": payload["resource_type"],
            "resource_id": payload["resource_id"],
            "label": payload["label"]}


def _replay_iam_preset_apply(db, *, row, approver, request=None):
    """Replay POST /iam/presets/apply through the SAME service the ungated
    route calls, with the row's REQUESTER as the acting party (the
    approver only signs off). The preset rewrite is idempotent, so a
    replay is safe; the row's payload (user_id + preset name) is the
    only detail it can carry."""
    import services.resource_perms_iam_service as iam_service
    payload = row.payload or {}
    requester = _user_by_id(db, row.requested_by_user_id)
    if requester is None:
        raise NotFoundError("Requesting user no longer exists")
    result = iam_service.apply_role_preset(
        db, preset=payload["preset"], user_id=payload["user_id"],
        current_user=requester)
    return {"status": "applied", "user_id": payload["user_id"],
            "preset": payload["preset"],
            "revoked": result.get("revoked"), "granted": result.get(
                "granted")}


def _replay_mobile_device_revoke(db, *, row, approver, request=None):
    """Replay POST /mobile-devices/{device_id}/revoke through the SAME service
    the ungated route calls, with the row's REQUESTER as the acting party (the
    approver only signs off). The revoke — and the shelf wipe it carries (CUIP.13)
    — are both idempotent, so a replay is safe; the row's payload carries the
    client device_id only. `replayed=True` skips the gate again (the second
    approver already signed this exact action)."""
    # 1788d CE carve: services/mobile_device_service.py is paid (manifest deny
    # `CORE/API/**/mobile*.py`). The mobile-device revoke is a paid capability;
    # the replay answers 402 rather than a 500.
    from utils.paid_import import optional_paid
    from fastapi import HTTPException
    _mobile = optional_paid("services.mobile_device_service")
    if _mobile is None:
        raise HTTPException(
            status_code=402,
            detail="Mobile-device revoke is not available in MOD "
                   "Community Edition.",
        )
    revoke_device = _mobile.revoke_device
    payload = row.payload or {}
    requester = _user_by_id(db, row.requested_by_user_id)
    if requester is None:
        raise NotFoundError("Requesting user no longer exists")
    detail: Dict[str, Any] = {}
    applied = revoke_device(db, row.tenant_id, payload["device_id"],
                            row.requested_by_user_id, detail=detail,
                            replayed=True)
    return {"status": "revoked" if applied else "already_revoked",
            "device_id": payload["device_id"], "tenant_id": row.tenant_id,
            "purge_client": detail.get("purge_client", False),
            "purged_items": detail.get("purged_items", 0)}


_ACTION_HANDLERS = {
    ACTION_ROLE_GRANT: _replay_role_grant,
    ACTION_FULL_EXPORT: _replay_full_export,
    ACTION_BYOK_REVOKE: _replay_byok_revoke,
    ACTION_DATA_MARKING_CLEAR: _replay_data_marking_clear,
    ACTION_IAM_PRESET_APPLY: _replay_iam_preset_apply,
    ACTION_MOBILE_DEVICE_REVOKE: _replay_mobile_device_revoke,
}


def gate(db, tenant_id: int, action: str, payload: Any, requester,
         reason: Optional[str] = None,
         request: Any = None) -> Optional[Dict[str, Any]]:
    """The one-line answer a call site asks: None when the action may
    run now (approvals not required), else the 202 body to return
    instead of running it."""
    # 1788d CE carve: services/approvals.py is paid (manifest deny 958). The
    # approval-request ledger is a paid compliance control; with it absent the
    # platform has no approval policy, so the action may run now (None) - the
    # same answer the setting's own default gives. Not auth: the route's tenant/
    # worker auth above is untouched.
    from utils.paid_import import optional_paid
    approvals = optional_paid("services.approvals")
    if approvals is None:
        return None
    if tenant_id is None:
        # No tenant to scope the switch to (the caller's own path will
        # 404 "Tenant not found"); the gate never invents one.
        return None
    if not approvals.approvals_required(db, tenant_id):
        return None
    row = request_approval(db, tenant_id=tenant_id, action=action,
                           payload=payload, requester=requester,
                           reason=reason, request=request)
    return awaiting_approval(row)


__all__ = [
    "approve", "reject", "request_approval", "cancel_approval", "gate",
    "awaiting_approval", "awaiting_response", "get_row", "list_rows",
    "payload_digest", "ACTION_KEYS", "EXPIRY_HOURS_KEY",
    "gate_role_grant", "gate_full_export", "gate_byok_revoke",
    "gate_data_marking_clear", "gate_iam_preset_apply", "role_grant_tenant",
    "ApprovalSelfRefusalError", "ApprovalExpiredError",
    "ApprovalPayloadMismatchError", "ApprovalValidationError",
]


# =============================================================================
# Gate helpers for the call sites (tenant resolution without a router query)
# =============================================================================


def role_grant_tenant(db, user_id: int,
                      fallback_tenant_id: Optional[int] = None):
    """The tenant a role grant AFFECTS — the TARGET user's tenant (a
    platform admin may assign cross-tenant, and the tenant switch belongs
    to the org being changed, not the admin's own). Falls back to the
    caller's tenant when the target cannot be resolved (the ungated path
    will 404 it anyway, so the gate must not invent a 404 of its own)."""
    from crud import rbac as rbac_crud
    target = rbac_crud.get_user_by_id(db, user_id)
    tenant_id = getattr(target, "tenant_id", None) if target else None
    return tenant_id if tenant_id is not None else fallback_tenant_id


def gate_role_grant(db, *, caller, user_id: int, role_id: int,
                    reason: Optional[str] = None,
                    request: Any = None):
    """The role-grant gate (routers/rbac.py PUT /users/{id}/assign-role).
    None = run now; else the 202 body."""
    tenant_id = role_grant_tenant(db, user_id,
                                 getattr(caller, "tenant_id", None))
    if tenant_id is None:
        return None
    return gate(db, tenant_id, ACTION_ROLE_GRANT,
                {"user_id": user_id, "role_id": role_id}, caller,
                reason=reason, request=request)


def gate_full_export(db, *, tenant_id: int, caller,
                     reason: Optional[str] = None,
                     request: Any = None):
    """The full-export gate (routers/tenant/export.py POST /tenant/export).
    None = run now; else the 202 body."""
    return gate(db, tenant_id, ACTION_FULL_EXPORT, {}, caller,
                reason=reason, request=request)


def gate_byok_revoke(db, *, tenant_id: int, caller,
                     provider: str, key_ref: str,
                     reason: Optional[str] = None,
                     request: Any = None):
    """The BYOK-revoke gate (routers/tenant/byok.py DELETE /tenant/byok).
    The payload carries the provider NAME and the key REF (already a
    tenant-scoped settings value — never key material)."""
    return gate(db, tenant_id, ACTION_BYOK_REVOKE,
                {"provider": provider, "key_ref": key_ref}, caller,
                reason=reason, request=request)


def gate_data_marking_clear(db, *, tenant_id: int, caller,
                            resource_type: str, resource_id: str,
                            label: str, reason: Optional[str] = None,
                            request: Any = None):
    """The data-marking-clear gate (routers/tenant/data_markings.py
    DELETE /data-markings/{type}/{id}/{label})."""
    return gate(db, tenant_id, ACTION_DATA_MARKING_CLEAR,
                {"resource_type": resource_type, "resource_id": str(
                    resource_id), "label": label}, caller,
                reason=reason, request=request)


def gate_mobile_device_revoke(db, *, tenant_id: int, caller,
                              device_id: str, reason: Optional[str] = None,
                              request: Any = None):
    """The mobile-device-revoke gate (services/mobile_device_service.revoke_device,
    reached from routers/mobile_devices.py POST /mobile-devices/{device_id}/revoke).
    Revoking an enrolled phone also remote-wipes its CUI shelf (CUIP.13), so under
    the second-approver switch the wipe is recorded, not applied. The payload
    carries the client device_id only — the row's tenant scopes the lookup, so
    nothing cross-tenant is observable. None = run now; else the 202 body."""
    return gate(db, tenant_id, ACTION_MOBILE_DEVICE_REVOKE,
                {"device_id": str(device_id)}, caller,
                reason=reason, request=request)


def gate_iam_preset_apply(db, *, caller, user_id: int, preset: str,
                          reason: Optional[str] = None,
                          request: Any = None):
    """The IAM role-preset gate (routers/resource_perms/iam_permissions.py
    POST /iam/presets/apply) — the sibling of PUT /users/{id}/assign-role
    that rewrites a user's whole capability set. Same tenant rule as the
    role grant: the switch belongs to the TARGET user's tenant (a platform
    admin may apply a preset cross-tenant via the user-management selector).
    None = run now; else the 202 body."""
    tenant_id = role_grant_tenant(db, user_id,
                                  getattr(caller, "tenant_id", None))
    if tenant_id is None:
        return None
    return gate(db, tenant_id, ACTION_IAM_PRESET_APPLY,
                {"user_id": user_id, "preset": preset}, caller,
                reason=reason, request=request)


def awaiting_response(body: Dict[str, Any],
                      headers: Optional[Dict[str, str]] = None):
    """The JSONResponse every gated route returns instead of executing:
    202 + the awaiting_approval body (a route's own status_code/response_
    model must not reshape the gate's answer)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202, content=body,
                        headers=headers)
