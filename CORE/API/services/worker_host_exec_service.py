"""services/worker_host_exec_service.py -- hx.5 (opsx worker-host-exec).

Authorization + create/cancel for host-exec jobs (the internal design notes
worker-host-exec/tasks.md D1-D4, hx.15/16/17 compliance-access model).

Access model (see tasks.md for the full matrix):
  * `worker.host_exec_enabled` off -> 403 for everyone.
  * B102 (SECURITY P1) gate that runs BEFORE anything else, for every caller
    including platform admins: the worker must be `LICENSED` and not revoked
    (`worker_not_licensed`), and a non-platform-admin caller must be able to
    SEE the worker at all (`not_visible`) -- see `_worker_visible_to`, the
    single visibility rule shared by evaluate_exec and list_exec_targets:
    owner tenant == caller tenant. A PLATFORM_ONLY / PLATFORM_POOL /
    other-tenant / assigned-not-owner box is denied outright instead of
    silently falling through to "sandboxed" (HX7B: pool boxes are
    platform-admin-only).
  * Platform admin -> mode "direct" on any (licensed, non-revoked) worker.
  * DEDICATED worker (owner_tenant_id set, assigned_tenant_ids empty or
    == [owner_tenant_id]) -> same-tenant caller only -> mode "direct",
    widened by the worker's live `WorkerHostExecPolicy.access` row (hx.15):
    'admins' (default when no live row) = owner-tenant admins + platform
    admin; 'grants' = those plus any user with an active
    `WorkerHostExecGrant`; 'all_members' = any member of the owning tenant.
    Other-tenant callers -> 403.
  * SHARED worker owned by the caller's tenant -> that tenant's users ->
    mode "sandboxed". A no-owner PLATFORM_POOL box is platform-admin-only
    (`shared_pool_admin_only`, Q-HX7B-SUBMIT-RULE answered); an assigned-
    but-not-owner tenant is denied (assigned case still an open question).
  * `run_as == "root"` requires mode "direct" (dedicated-admin or
    platform admin) AND a fresh step-up MFA proof (hx.16 stub below);
    anything else -> 403.

Nothing here ever hard-deletes a job row (ISO 27001): cancel is a status
transition, never a DELETE.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from jose import jwt, JWTError

import audit
import models
from services import platform_settings
from services import worker_host_exec_settings as settings

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

# --- argv redaction ---------------------------------------------------------

_SECRET_FLAG_RE = re.compile(
    r"^--?[\w-]*(password|passwd|token|secret|api[-_]?key|auth)[\w-]*$",
    re.IGNORECASE,
)
_SECRET_KV_RE = re.compile(
    r"^(?P<key>[\w.-]*(password|passwd|token|secret|api[-_]?key)[\w.-]*)"
    r"=(?P<val>.*)$",
    re.IGNORECASE,
)


def redact_argv(argv: List[str]) -> List[str]:
    """Redact the value that follows a secret-looking flag (--password x)
    and the value half of a `key=secret` token. Never mutates `argv`."""
    out: List[str] = []
    redact_next = False
    for tok in argv:
        tok = str(tok)
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        kv = _SECRET_KV_RE.match(tok)
        if kv:
            out.append(f"{kv.group('key')}=<redacted>")
            continue
        if _SECRET_FLAG_RE.match(tok):
            out.append(tok)
            redact_next = True
            continue
        out.append(tok)
    return out


def argv_sha256(argv: List[str]) -> str:
    blob = json.dumps(list(argv), separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- access model ------------------------------------------------------------

def _is_tenant_admin(user: "models.User") -> bool:
    """Same tenant-root-admin idiom as routers/assistant.py:_is_tenant_admin
    (parent_user_id IS NULL identifies the tenant root admin); platform
    admins always pass."""
    if getattr(user, "is_platform_admin", False):
        return True
    return getattr(user, "parent_user_id", None) is None


def _worker_visible_to(user: "models.User",
                       worker: "models.WorkerLicense") -> bool:
    """B102 / HX7B -- the ONE visibility rule shared by evaluate_exec and
    list_exec_targets (they cannot drift): a caller may act on / see a
    worker only when it is a licensed, non-revoked box AND the caller is a
    platform admin or the worker's OWNER tenant is the caller's tenant.

    Everything else is invisible, because evaluate_exec denies it:
      * a NO-OWNER-TENANT box (platform pool, PLATFORM_ONLY, any missing/odd
        owner_type) is admin-only -- fail closed, never "sandboxed";
      * an ASSIGNED-but-not-owner tenant is denied (Q-HX7B-SUBMIT-RULE for
        the assigned case is still an OPEN owner question; its verdict is
        unchanged from HEAD, so the list must not offer what exec refuses).
    Tenant ids are compared as str on both sides."""
    if (getattr(worker, "status", None) != models.WorkerStatus.LICENSED
            or getattr(worker, "revoked_at", None) is not None
            or getattr(worker, "deleted_at", None) is not None):
        return False
    if bool(getattr(user, "is_platform_admin", False)):
        return True
    owner_tenant_id = getattr(worker, "owner_tenant_id", None)
    if owner_tenant_id is None:
        return False
    return str(owner_tenant_id) == str(getattr(user, "tenant_id", None))


def _is_assigned_not_owner(user: "models.User",
                           worker: "models.WorkerLicense") -> bool:
    """True when the caller's tenant is in `assigned_tenant_ids` of a worker
    it does not own (the Q-HX7B-SUBMIT-RULE assigned case)."""
    tenant_id = getattr(user, "tenant_id", None)
    owner_tenant_id = getattr(worker, "owner_tenant_id", None)
    if tenant_id is None or str(owner_tenant_id) == str(tenant_id):
        return False
    assigned = getattr(worker, "assigned_tenant_ids", None) or []
    return str(tenant_id) in {str(a) for a in assigned if a is not None}


def _is_dedicated(worker: "models.WorkerLicense") -> bool:
    owner_tenant_id = getattr(worker, "owner_tenant_id", None)
    if not owner_tenant_id:
        return False
    assigned = getattr(worker, "assigned_tenant_ids", None) or []
    if not assigned:
        return True
    return list(assigned) == [owner_tenant_id]


def _worker_access_policy(db: Session, worker: "models.WorkerLicense") -> str:
    """hx.15 (D2): the worker's live access policy -- 'admins' (default),
    'grants' or 'all_members'. Soft-deleted rows (deleted_at set) don't
    count; a worker with no live row falls back to 'admins'."""
    row = (
        db.query(models.WorkerHostExecPolicy)
        .filter(
            models.WorkerHostExecPolicy.worker_id == worker.id,
            models.WorkerHostExecPolicy.deleted_at.is_(None),
        )
        .order_by(models.WorkerHostExecPolicy.updated_at.desc())
        .first()
    )
    if row is None:
        return "admins"
    return (row.access or "admins").strip()


def _grant_allows(db: Session, worker: "models.WorkerLicense",
                  user: "models.User", *, allow_root: bool) -> bool:
    """Active-grant lookup for `worker`/`user`; when `allow_root` is True the
    grant must also carry allow_root=true (hx.15 D4)."""
    if getattr(user, "id", None) is None:
        return False
    rows = (
        db.query(models.WorkerHostExecGrant)
        .filter(
            models.WorkerHostExecGrant.worker_id == worker.id,
            models.WorkerHostExecGrant.grantee_user_id == user.id,
            models.WorkerHostExecGrant.revoked_at.is_(None),
        )
        .all()
    )
    if not allow_root:
        return bool(rows)
    return any(bool(getattr(r, "allow_root", False)) for r in rows)


def _require_step_up(
    user: "models.User",
    request: Optional[Request],
    db: Optional[Session] = None,
) -> None:
    """hx.16 — step-up MFA freshness gate for run_as=root host exec.

    Stricter than the GY.C4b `require_fresh_auth` dependency: that one is
    fail-open (no auth_time = machine channel, pass). Root host-exec has no
    machine channel at all -- an API-key / service token can NEVER run as
    root -- so everything here fails closed:

    1. Claims come from `request.state.token_claims` when an upstream
       dependency already stashed them; otherwise we UNVERIFIED-decode the
       `Authorization: Bearer <jwt>` header (the signature/aud/issuer was
       already validated upstream by get_current_user, same approach as
       dependencies.py:require_fresh_auth -- no second JWKS fetch). No
       request / no bearer / undecodable -> 403.
    2. `token_has_strong_auth(claims)` (amr strong-auth value or a
       federated-SSO `idp` claim) must be True, else 403.
    3. `auth_time` must be present and its age within
       `worker.host_exec_root_reauth_max_age_s` (default 300s). Stale ->
       401 whose detail carries `error: "reauth_required"` + the max-age
       hint, the same shape require_fresh_auth raises so the Dashboard's
       re-auth bounce (prompt=login&max_age=N) works.

    Never logs the token or claim values.
    """
    claims: Optional[Dict[str, Any]] = None
    if request is not None:
        claims = getattr(getattr(request, "state", None), "token_claims", None)
    if not claims:
        claims = _claims_from_bearer(request)
    if not claims:
        raise HTTPException(
            status_code=403,
            detail="run_as=root requires a human MFA session; API-key and "
                   "service channels cannot exec as root",
        )

    from services.outside_collaborators import token_has_strong_auth
    if not token_has_strong_auth(claims):
        raise HTTPException(
            status_code=403,
            detail="run_as=root requires MFA (step-up)",
        )

    max_age = settings.root_reauth_max_age_s(db, tenant_id=None)
    auth_time = claims.get("auth_time")
    if auth_time is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "reauth_required",
                "reauth_max_age_s": max_age,
                "detail": "Root host exec requires a recent MFA login.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        age = time.time() - int(auth_time)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "reauth_required",
                "reauth_max_age_s": max_age,
                "detail": "Root host exec requires a recent MFA login.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    if age > max_age:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "reauth_required",
                "reauth_max_age_s": max_age,
                "detail": "Root host exec requires a recent MFA login.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


def _claims_from_bearer(request: Optional[Request]) -> Optional[Dict[str, Any]]:
    """Unverified-decode the Bearer JWT off `request.headers`.

    Safe because the token's signature/aud/issuer were already validated
    upstream before the user was resolved; we only read auth_time/amr here.
    Returns None on any missing/undecodable header (the caller fails closed).
    Never logs the token.
    """
    if request is None:
        return None
    try:
        header = request.headers.get("Authorization") or ""
    except Exception:  # noqa: BLE001 -- odd request object: fail closed
        return None
    if not isinstance(header, str):
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    try:
        claims = jwt.get_unverified_claims(parts[1])
    except JWTError:
        return None
    except Exception:  # noqa: BLE001 -- undecodable: fail closed
        return None
    return claims if isinstance(claims, dict) else None


def _audit_denied(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    request: Optional[Request],
    reason: str,
) -> None:
    """hx.11 — one PermissionAuditLog row per refused host-exec action.

    ISO 27001 / CMMC AU-2: a denied privileged action must be auditable.
    Same AuditAction as the create path (ADMIN_WORKER_MODIFY) so denials
    and grants of the same resource sit in one action space; the
    `host_exec_action=denied` + `reason` code carries the difference.
    NEVER argv/env/command text here -- reason codes only.
    `log_security_event` commits the row itself (audit.py), so the row
    survives the request-session rollback that FastAPI does on HTTPException.
    Best-effort: an audit-write failure must never turn the 403 into a 500
    (same seam as services/lockbox.py:_audit).
    """
    try:
        audit.log_security_event(
            db,
            models.AuditAction.ADMIN_WORKER_MODIFY,
            request=request,
            user_id=getattr(user, "id", None),
            tenant_id=getattr(worker, "owner_tenant_id", None),
            metadata={
                "host_exec_action": "denied",
                "reason": reason,
                "worker_id": worker.id,
                "result": "denied",
            },
        )
    except Exception as e:  # noqa: BLE001 -- denial wins over audit
        logger.warning("hx.11 host-exec denial audit write failed: %s", e)


# hx.14 — reason code -> the EXACT 403 detail authorize_exec used to raise
# for it (the wrapper must stay byte-for-byte identical to the pre-split
# implementation; tests/test_worker_host_exec_deny_audit.py pins these).
_DENY_DETAIL: Dict[str, str] = {
    "disabled": "host-exec is disabled",
    "cross_tenant": "this worker is dedicated to another tenant",
    "no_grant": "host-exec on this dedicated worker requires an "
                 "active grant",
    "not_tenant_admin": "host-exec on a dedicated worker requires a "
                        "tenant admin",
    "run_as_forbidden": "run_as=root requires a dedicated-admin or "
                        "platform-admin session",
    "root_not_granted": "run_as=root requires a platform admin, the owning "
                        "tenant's admin, or an active grant with allow_root",
    "worker_not_licensed": "worker is not licensed or has been revoked",
    "not_visible": "this worker is not available to your tenant",
    "shared_pool_admin_only": "host-exec on a shared platform-pool worker requires "
                              "a platform-admin session",
}

# 993c — short, user-facing "what to do about it" text for evaluate_exec's
# policy-axis reason codes that are NOT also a resolve_availability (993b)
# code. `no_grant` is intentionally absent here: it IS also a 993b
# availability code (`_AVAILABILITY_MESSAGES["no_grant"]`), and authorize_exec
# below prefers that resolver's message/fix text when the two axes share a
# reason code, so the UI gives one consistent story for "no grant" whether it
# came from a pre-flight availability check or a denied create_job call.
_DENY_FIX: Dict[str, str] = {
    "disabled": "Ask a platform admin to enable host-exec.",
    "cross_tenant": "Use a worker your tenant owns.",
    "not_tenant_admin": "Ask your tenant admin to run this, or request a "
                         "grant on this worker.",
    "run_as_forbidden": "Request a dedicated-admin or platform-admin "
                         "session to run as root.",
    "root_not_granted": "Ask a platform admin, your tenant's admin, or "
                         "request a grant with root access.",
    "worker_not_licensed": "Choose another worker; this one is not "
                            "licensed or has been revoked.",
    "not_visible": "Ask an admin to share this worker with your tenant.",
    "shared_pool_admin_only": "Ask a platform admin to run host-exec on this shared "
                              "platform-pool worker.",
}


def evaluate_exec(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    run_as: Optional[str],
) -> "tuple[Optional[str], Optional[str]]":
    """Pure access decision: `(mode, None)` on allow, `(None, reason_code)`
    on deny. NO audit row, NO HTTPException, NO step-up gate (hx.14 — the
    listing path needs the verdict without side effects; `authorize_exec`
    is now a thin wrapper over this, same check order, same reason codes).
    """
    tenant_id_for_setting = getattr(worker, "owner_tenant_id", None)
    if not settings.host_exec_enabled(db, tenant_id=tenant_id_for_setting):
        return (None, "disabled")

    # B102 (SECURITY P1): a worker is exec-able ONLY if it is licensed and
    # not revoked (admins included), and a non-platform-admin caller can
    # actually see it (`_worker_visible_to` — the same rule list_exec_targets
    # uses). Both checks run BEFORE the admin/dedicated/shared mode logic, so
    # a PLATFORM_ONLY or other-tenant box can no longer fall through to
    # "sandboxed".
    if (getattr(worker, "status", None) != models.WorkerStatus.LICENSED
            or getattr(worker, "revoked_at", None) is not None
            or getattr(worker, "deleted_at", None) is not None):
        return (None, "worker_not_licensed")

    is_admin = bool(getattr(user, "is_platform_admin", False))
    if not is_admin and (
            (getattr(worker, "owner_tenant_id", None) is None
             and getattr(worker, "owner_type", None)
             == models.WorkerOwnerType.PLATFORM_POOL)
            or _is_assigned_not_owner(user, worker)):
        # HX7B: a NO-OWNER-TENANT platform-pool box is admin-only, and an
        # ASSIGNED-but-not-owner tenant keeps the HEAD verdict (denied with
        # shared_pool_admin_only; Q-HX7B-SUBMIT-RULE for that case is still
        # OPEN). Runs BEFORE the visibility gate -- which also hides these
        # boxes from tenant callers -- so the reason code is not shadowed by
        # not_visible (the committed 403-shape test pins code, detail, audit).
        return (None, "shared_pool_admin_only")
    if not is_admin and not _worker_visible_to(user, worker):
        return (None, "not_visible")

    dedicated = _is_dedicated(worker)

    if is_admin:
        mode = "direct"
    elif dedicated:
        if getattr(user, "tenant_id", None) != worker.owner_tenant_id:
            return (None, "cross_tenant")
        policy = _worker_access_policy(db, worker)
        if policy == "all_members":
            pass
        elif policy == "grants":
            if not _is_tenant_admin(user) and not _grant_allows(
                db, worker, user, allow_root=False
            ):
                return (None, "no_grant")
        else:
            if not _is_tenant_admin(user):
                return (None, "not_tenant_admin")
        mode = "direct"
    else:
        # Shared box. Reaching here means the caller is a platform admin or
        # the box's OWNER tenant (pool and assigned-not-owner callers were
        # denied above); a tenant-owned shared box accepts that tenant's
        # users (owner decision Q-HX7B-SUBMIT-RULE: "a tenant-owned worker
        # accepts that tenant members"). Fail closed on anything else.
        if not is_admin and (getattr(user, "tenant_id", None)
                             != worker.owner_tenant_id):
            return (None, "shared_pool_admin_only")
        mode = "sandboxed"

    run_as_norm = (run_as or "").strip()
    if run_as_norm.lower() == "root":
        if mode != "direct":
            return (None, "run_as_forbidden")
        root_authorized = (
            bool(getattr(user, "is_platform_admin", False))
            or _is_tenant_admin(user)
            or _grant_allows(db, worker, user, allow_root=True)
        )
        if not root_authorized:
            return (None, "root_not_granted")

    return (mode, None)


def authorize_exec(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    run_as: Optional[str],
    request: Optional[Request] = None,
) -> str:
    """Return "direct" or "sandboxed", or raise HTTPException(403).

    Thin wrapper over evaluate_exec (hx.14): every denial still leaves
    exactly one audit row (hx.11, see _audit_denied) with the same reason
    code and the same 403 detail as before the split.
    """
    mode, reason = evaluate_exec(db, user, worker, run_as)
    if reason is not None:
        _audit_denied(db, user, worker, request, reason)
        # 993c — `detail` becomes a dict (additive: status stays 403, `error`
        # keeps the exact hx.14 string so the audit log / anything reading
        # the raw string elsewhere is unaffected). `reason` is one of
        # evaluate_exec's policy-axis codes (disabled, cross_tenant,
        # no_grant, not_tenant_admin, run_as_forbidden, root_not_granted,
        # worker_not_licensed, not_visible) -- a DIFFERENT axis from 993b's
        # resolve_availability codes. `no_grant` happens to be a name both
        # axes use; when it is, prefer the 993b message/fix text so the
        # "no grant" story is identical whether it comes from a pre-flight
        # availability check or a denied create_job call.
        avail_msg = _AVAILABILITY_MESSAGES.get(reason)
        if avail_msg is not None:
            message, fix = avail_msg
        else:
            message, fix = _DENY_DETAIL[reason], _DENY_FIX.get(reason, "")
        raise HTTPException(status_code=403, detail={
            "error": _DENY_DETAIL[reason],
            "code": reason,
            "message": message,
            "fix": fix,
        })

    if (run_as or "").strip().lower() == "root":
        _require_step_up(user, request, db)

    return mode


# --- create / get / cancel ----------------------------------------------------

def create_job(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    argv: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    run_as: Optional[str] = None,
    timeout_s: Optional[int] = None,
    request: Optional[Request] = None,
) -> "models.WorkerHostExecJob":
    mode = authorize_exec(db, user, worker, run_as, request=request)

    run_as_norm = (run_as or "").strip() or "service"
    privileged = run_as_norm.lower() == "root"
    tenant_id = getattr(worker, "owner_tenant_id", None)
    sha = argv_sha256(argv)

    job = models.WorkerHostExecJob(
        tenant_id=tenant_id,
        worker_id=worker.id,
        actor_user_id=getattr(user, "id", None),
        argv_sha256=sha,
        argv_redacted={"argv": redact_argv(argv)},
        cwd=cwd,
        run_as=run_as_norm,
        timeout_s=timeout_s or settings.host_exec_default_timeout_s(
            db, tenant_id=tenant_id),
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"host-exec-{job.id}",
        tenant_id=tenant_id or 1,
        queue_name="CPU",
        priority=5,
        task_type="host-exec",
        task_payload={
            "job_id": job.id,
            "argv": list(argv),
            "cwd": cwd,
            "env": env or {},
            "run_as": run_as_norm,
            "timeout_s": job.timeout_s,
            "mode": mode,
        },
        status=models.ApiTaskStatus.ASSIGNED,
        assigned_to_worker=worker.worker_name,
        max_attempts=1,
    )
    db.add(task)
    db.commit()

    try:  # best-effort wake-up, never fails the enqueue
        import redis
        r = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish(f"task:available:{task.queue_name}", "1")
    except Exception:  # noqa: BLE001
        pass

    audit.log_security_event(
        db,
        models.AuditAction.ADMIN_WORKER_MODIFY,
        request=request,
        user_id=getattr(user, "id", None),
        tenant_id=tenant_id,
        metadata={
            "host_exec_action": "create",
            "job_id": job.id,
            "worker_id": worker.id,
            "argv_sha256": sha,
            "mode": mode,
            "privileged": privileged,
        },
    )
    return job


def get_job(db: Session, job_id: int) -> Optional["models.WorkerHostExecJob"]:
    return db.get(models.WorkerHostExecJob, job_id)


def authorize_job_access(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    job: "models.WorkerHostExecJob",
    request: Optional[Request] = None,
) -> None:
    """Job-level visibility on top of worker-level `authorize_exec`.

    `authorize_exec` alone is not enough to gate reads: on a SHARED
    worker it returns "sandboxed" for *any* active tenant user, which
    would let one tenant read another tenant's job (argv, exit code,
    status) if we stopped there. A sandboxed job is visible only to the
    actor who created it or a platform admin; a dedicated-worker job
    follows the normal tenant-admin `authorize_exec` check.
    """
    if bool(getattr(user, "is_platform_admin", False)):
        return
    if _is_dedicated(worker):
        authorize_exec(db, user, worker, None, request=request)
        return
    if job.actor_user_id != getattr(user, "id", None):
        raise HTTPException(
            status_code=403,
            detail="not authorized to view this host-exec job",
        )


def cancel_job(
    db: Session,
    user: "models.User",
    job: "models.WorkerHostExecJob",
    request: Optional[Request] = None,
) -> "models.WorkerHostExecJob":
    is_admin = bool(getattr(user, "is_platform_admin", False))
    if not is_admin and job.actor_user_id != getattr(user, "id", None):
        raise HTTPException(
            status_code=403,
            detail="only the requesting actor or a platform admin may "
                   "cancel this job",
        )
    if job.status not in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot cancel a job in status {job.status!r}",
        )

    job.status = "cancelled"
    db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.node_id == f"host-exec-{job.id}",
    ).update({"status": models.ApiTaskStatus.CANCELLED},
             synchronize_session=False)
    db.commit()
    db.refresh(job)

    audit.log_security_event(
        db,
        models.AuditAction.ADMIN_WORKER_MODIFY,
        request=request,
        user_id=getattr(user, "id", None),
        tenant_id=job.tenant_id,
        metadata={
            "host_exec_action": "cancel",
            "job_id": job.id,
            "worker_id": job.worker_id,
            "argv_sha256": job.argv_sha256,
        },
    )
    return job


# --- hx.14: "which workers can I run tests on?" ------------------------------

_ONLINE_WINDOW_S = 180
_LIST_CAP = 200


def list_exec_targets(
    db: Session,
    user: "models.User",
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Candidates the caller could actually exec on, for GET /host-exec/targets.

    Read-only: writes NO audit row (listing is not a privileged action).
    Scope is deliberately NARROWER than authorize_exec -- a platform admin
    may authorize another tenant's dedicated box, but the listing never
    shows another tenant's hardware: candidates are licensed, non-revoked
    workers where the caller is a platform admin or the worker's owner
    tenant is the caller's tenant (pool boxes are admin-only, assigned-not-
    owner tenants are denied) --
    i.e. exactly `_worker_visible_to`, the ONE rule evaluate_exec also uses
    (B102: the two can no longer drift).
    Ordered by id, capped at 200. Never carries hardware_specs, queues,
    costs or secrets.
    """
    rows = (
        db.query(models.WorkerLicense)
        .filter(
            models.WorkerLicense.status == models.WorkerStatus.LICENSED,
            models.WorkerLicense.revoked_at.is_(None),
            models.WorkerLicense.deleted_at.is_(None),
        )
        .order_by(models.WorkerLicense.id)
        .all()
    )

    candidates = [worker for worker in rows if _worker_visible_to(user, worker)]
    if len(candidates) >= _LIST_CAP:
        candidates = candidates[:_LIST_CAP]

    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        # Defensive: a naive `now` would otherwise make every comparison
        # below a naive-vs-aware TypeError risk; treat naive as UTC.
        ref = ref.replace(tzinfo=timezone.utc)
    targets: List[Dict[str, Any]] = []
    for worker in candidates:
        mode, _reason = evaluate_exec(db, user, worker, None)
        if mode is None:
            continue
        heartbeat = getattr(worker, "last_heartbeat", None)
        online = False
        if heartbeat is not None:
            hb = heartbeat
            if hb.tzinfo is None:
                # last_heartbeat is DateTime(timezone=True); a naive value
                # here means legacy/un-coerced data -- treat as UTC rather
                # than crash the listing.
                hb = hb.replace(tzinfo=timezone.utc)
            online = abs((ref - hb).total_seconds()) <= _ONLINE_WINDOW_S
        targets.append({
            "worker_id": worker.id,
            "worker_name": getattr(worker, "worker_name", None),
            "hostname": getattr(worker, "hostname", None),
            "mode": mode,
            "online": online,
            "last_heartbeat": heartbeat.isoformat() if heartbeat else None,
        })
    return targets


# --- 993b: per-worker host-exec availability status --------------------------

# hx.7e's hostexec_wire.go first landed in commit f82f9b283 (2026-10-10,
# "feat(worker): wire host-exec executor seams (hx.7e)") and is already an
# ancestor of the v1.0.82 VERSION bump -- but that code had not yet shipped
# in a published worker release. 988b is the release that actually publishes
# it, as v1.0.83 (CORE/Worker/VERSION). Workers below this floor cannot be
# trusted to report host-exec capability at all.
MIN_HOSTEXEC_AGENT_VERSION = "v1.0.83"

_KNOWN_HOST_EXEC_CODES = frozenset({
    "disabled_platform", "disabled_tenant", "worker_too_old", "not_root",
    "os_unsupported_yet", "helper_missing", "no_grant", "available",
})

_AVAILABILITY_MESSAGES: Dict[str, "tuple[str, str]"] = {
    "disabled_platform": (
        "Host-exec is turned off for this platform.",
        "Ask a platform admin to enable it in Platform Settings → Worker → Host Exec.",
    ),
    "disabled_tenant": (
        "Host-exec is turned off for your organization.",
        "Ask your tenant admin to enable it in Tenant Settings → Worker → Host Exec.",
    ),
    "worker_too_old": (
        "This worker's version is too old for host-exec.",
        "Update the worker (it self-updates, or trigger an update from the Workers page).",
    ),
    "not_root": (
        "This worker isn't running with the privileges host-exec needs.",
        "Reinstall the worker as a system service.",
    ),
    "os_unsupported_yet": (
        "Host-exec isn't available on this worker's OS yet.",
        "Windows/macOS support is on the roadmap.",
    ),
    "helper_missing": (
        "This worker hasn't installed the host-exec helper.",
        "Update or restart the worker.",
    ),
    "no_grant": (
        "You don't have access to run host-exec on this worker.",
        "Ask an admin for a grant on this worker.",
    ),
    "available": ("", ""),
}


def _host_exec_version_tuple(v: Optional[str]) -> "tuple[int, int, int]":
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)


_HOST_EXEC_MESSAGE_MAX_LEN = 300


def _sanitize_worker_message(raw: Optional[str]) -> Optional[str]:
    """993f: `payload["message"]` is a worker-controlled string (any worker
    that can reach the heartbeat endpoint can set it) rendered to users on
    the pre-flight availability check. Strip control/formatting characters
    (keep plain space) and cap length so a malicious/buggy worker can't push
    oversized or HTML/script-bearing text at callers. The Dashboard (993d)
    renders this as plain text, never HTML -- this is defense in depth.
    """
    if not isinstance(raw, str):
        return None
    cleaned = "".join(ch for ch in raw if ch == " " or (ch.isprintable() and ch not in "\t\n\r"))
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return cleaned[:_HOST_EXEC_MESSAGE_MAX_LEN]


def _availability_result(
    code: str,
    *,
    available: bool,
    mode: Optional[str] = None,
    message: Optional[str] = None,
    fix: Optional[str] = None,
) -> Dict[str, Any]:
    default_message, default_fix = _AVAILABILITY_MESSAGES.get(code, ("", ""))
    return {
        "code": code,
        "available": available,
        "mode": mode,
        "message": message if message is not None else default_message,
        "fix": fix if fix is not None else default_fix,
    }


def resolve_availability(
    db: Session,
    worker: "models.WorkerLicense",
    user: Optional["models.User"] = None,
    *,
    prefetched_access_policy: Optional[str] = None,
    prefetched_grant_allows: Optional[bool] = None,
) -> Dict[str, Any]:
    """993b: the per-worker host-exec availability status shown to users
    (the create-job pre-flight UI lands in 993c/993d; this ships the pure
    resolver + its own unit test only -- NOT wired into create_job's 403 or
    any router response yet).

    Returns `{"code", "available", "mode", "message", "fix"}`. NO audit row,
    NO HTTPException -- mirrors evaluate_exec's "decision, no side effects"
    shape but answers a different question ("can this worker ever run
    host-exec, and why not") rather than evaluate_exec's "may THIS caller
    run THIS command right now".

    First-match-wins order: platform/tenant setting off, worker agent too
    old, worker OS not supported yet, worker-reported host-exec state
    (993a's heartbeat payload, when present) or a fail-closed-but-honest
    `helper_missing` when the worker hasn't reported at all (older binary,
    pre-993a), then (only once the worker itself says `available`) the
    grant-policy check, else `available`.
    """
    owner_tenant_id = getattr(worker, "owner_tenant_id", None)

    # (a) platform/tenant setting off -- distinguish the two by reading the
    # tenant-row override directly rather than through resolve_source (whose
    # "source" values are only db/env/default, not tenant-vs-platform).
    tenant_override = None
    if owner_tenant_id is not None:
        tenant_override = platform_settings._lookup_row(
            db, settings.ENABLED_KEY, owner_tenant_id, "tenant"
        )
    if tenant_override is False:
        return _availability_result("disabled_tenant", available=False)
    if not settings.host_exec_enabled(db, tenant_id=owner_tenant_id):
        return _availability_result("disabled_platform", available=False)

    # (b) worker binary too old for host-exec at all.
    if _host_exec_version_tuple(getattr(worker, "agent_version", None)) < \
            _host_exec_version_tuple(MIN_HOSTEXEC_AGENT_VERSION):
        return _availability_result("worker_too_old", available=False)

    specs = worker.hardware_specs if isinstance(worker.hardware_specs, dict) else {}

    payload = specs.get("host_exec")
    # A malformed payload (not a dict, or missing the required `available`
    # key) is treated the same as no payload at all.
    payload_present = isinstance(payload, dict) and "available" in payload

    # (c) Windows/macOS host-exec (992b/992f, worker >= MIN_HOSTEXEC_AGENT_VERSION)
    # now ships and self-reports via the heartbeat `host_exec` payload (993a).
    # Only fall back to the hardcoded "unsupported" answer when an older
    # worker binary hasn't reported the payload yet; once it's present we
    # trust it and fall through to the same payload handling as linux.
    if specs.get("os") in ("windows", "darwin") and not payload_present:
        return _availability_result(
            "os_unsupported_yet",
            available=False,
            # hx.9s: this hardcoded a stale "v1.0.84+" that had drifted from
            # MIN_HOSTEXEC_AGENT_VERSION (bumped to v1.0.83 by 988b) --
            # reference the constant so the two can never diverge again.
            fix=f"Update the worker to {MIN_HOSTEXEC_AGENT_VERSION}+ to "
                "enable host-exec on Windows/macOS.",
        )

    # (e) absent/malformed payload: older worker binary (pre-993a), or one
    # that never reported this heartbeat field at all. Never claim
    # `available` without worker-reported evidence -- fail closed but
    # honest about why.
    if not payload_present:
        return _availability_result(
            "helper_missing",
            available=False,
            message="this worker's version does not yet report host-exec "
                    "capability",
        )

    # (d) payload present: trust its reported code verbatim when it's one
    # of this function's own known codes; otherwise fall back to a generic
    # `helper_missing` rather than inventing a new taxonomy.
    reported_code = payload.get("code")
    if reported_code not in _KNOWN_HOST_EXEC_CODES:
        reported_code = "helper_missing"
    if reported_code != "available":
        return _availability_result(
            reported_code,
            available=False,
            message=_sanitize_worker_message(payload.get("message")),
        )
    reported_mode = payload.get("mode") or "direct"

    # (f) grant-policy gate -- reuse _worker_access_policy/_grant_allows so
    # this never drifts from the enforcement logic elsewhere in this file.
    # 993c3: a caller iterating many workers for a non-admin user (e.g. the
    # tenant worker list) can pass `prefetched_access_policy`/
    # `prefetched_grant_allows` -- computed from ONE bulk query each across
    # all worker ids -- to skip these two per-worker queries. Default (None)
    # behaviour is unchanged: compute them here, same as before 993c3.
    if user is not None and not _is_tenant_admin(user):
        policy = (
            prefetched_access_policy if prefetched_access_policy is not None
            else _worker_access_policy(db, worker)
        )
        if policy == "grants":
            grant_ok = (
                prefetched_grant_allows if prefetched_grant_allows is not None
                else _grant_allows(db, worker, user, allow_root=False)
            )
            if not grant_ok:
                return _availability_result("no_grant", available=False)

    # (g) worker reports it, platform/tenant allow it, caller is cleared.
    return _availability_result("available", available=True, mode=reported_mode)
