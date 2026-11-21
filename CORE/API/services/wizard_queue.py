"""Wizard Queue service (wizard-queue-redesign).

The single write path + scope/query logic for the unified ``WizardRun``
table. Every wizard (node / stack / agent-project / build-repair /
container-adapter) surfaces its run through the helpers here; each keeps
its own execution/dispatch path untouched (design D12 — observational,
never throttling).

Responsibilities:
  * classify a run platform-level vs tenant-scoped at creation (D17);
  * record status transitions on the canonical lifecycle (D13) and
    stamp start/end/duration;
  * persist secret-redacted log lines (D19) and fan them out for the
    live SSE tail;
  * resolve role-scoped visibility + action authorization (D5/D15);
  * HARD cancel with cascade to child runs (D14/D21) and retry as a
    NEW linked run (D14);
  * emit bell + toast notifications on completion/failure — NO email
    (D18).

Per-wizard native kill/retry behaviour is provided by hooks the
emitters register (``register_cancel_hook`` / ``register_retry_hook``)
so this module never imports the individual wizard backends.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

import models
from models import (
    WizardRun,
    WizardRunLog,
    WizardRunStatus,
    WizardRunType,
    WizardRunOrigin,
    WIZARD_TERMINAL_STATUSES,
    WIZARD_RETRYABLE_STATUSES,
)
from utils.secret_redaction import redact

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# Per-wizard hook registries
# =====================================================================
# A cancel hook receives the WizardRun and must HARD-kill the underlying
# build/container/task (idempotent, best-effort). A retry hook receives
# (original_run, new_run) and must freshly dispatch the new run. Both are
# keyed by native_source / wizard_type respectively so the service never
# imports the wizard backends directly.

_CANCEL_HOOKS: Dict[str, Callable[[Session, WizardRun], None]] = {}
_RETRY_HOOKS: Dict[WizardRunType, Callable[[Session, WizardRun, WizardRun], None]] = {}


def register_cancel_hook(native_source: str,
                         fn: Callable[[Session, WizardRun], None]) -> None:
    _CANCEL_HOOKS[native_source] = fn


def register_retry_hook(wizard_type: WizardRunType,
                        fn: Callable[[Session, WizardRun, WizardRun], None]) -> None:
    _RETRY_HOOKS[wizard_type] = fn


def _hard_kill_native(db: Session, run: WizardRun) -> None:
    """Best-effort HARD kill of a run's underlying execution. Never
    raises — cancel must always be able to mark the row canceled even if
    the native kill fails."""
    hook = _CANCEL_HOOKS.get(run.native_source or "")
    if hook is None:
        return
    try:
        hook(db, run)
    except Exception as e:  # noqa: BLE001 — cancel must not be blocked
        logger.warning("wizard_queue: native kill hook for %s failed on run "
                       "%s: %s", run.native_source, run.id, e)


# =====================================================================
# Classification (D17)
# =====================================================================
def classify(creator: Optional[models.User] = None, *, system: bool = False):
    """Return (origin, is_platform_level). Platform-level if a SYSTEM
    process OR created by a PLATFORM ADMIN; else tenant-scoped."""
    if system or creator is None:
        return WizardRunOrigin.SYSTEM, True
    if getattr(creator, "is_platform_admin", False):
        return WizardRunOrigin.PLATFORM_ADMIN, True
    return WizardRunOrigin.TENANT_USER, False


# =====================================================================
# Create + status transitions
# =====================================================================
def create_run(
    db: Session,
    *,
    wizard_type: WizardRunType,
    target_name: Optional[str] = None,
    target_ref_type: Optional[str] = None,
    target_ref_id: Optional[str] = None,
    creator: Optional[models.User] = None,
    system: bool = False,
    owner_user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    log_source: Optional[str] = None,
    native_source: Optional[str] = None,
    native_ref: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    retry_of_id: Optional[str] = None,
    status: WizardRunStatus = WizardRunStatus.QUEUED,
    origin: Optional[WizardRunOrigin] = None,
    is_platform_level: Optional[bool] = None,
    extra: Optional[dict] = None,
    commit: bool = True,
) -> WizardRun:
    """Create + persist a WizardRun. Emitters call this once when a
    wizard run begins."""
    if origin is None or is_platform_level is None:
        c_origin, c_platform = classify(creator, system=system)
        origin = origin or c_origin
        is_platform_level = c_platform if is_platform_level is None else is_platform_level

    if creator is not None:
        if owner_user_id is None:
            owner_user_id = creator.id
        if tenant_id is None and not is_platform_level:
            tenant_id = getattr(creator, "tenant_id", None)

    run = WizardRun(
        wizard_type=wizard_type,
        target_name=target_name,
        target_ref_type=target_ref_type,
        target_ref_id=target_ref_id,
        status=status,
        progress=0,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        is_platform_level=bool(is_platform_level),
        origin=origin,
        log_source=log_source,
        native_source=native_source,
        native_ref=native_ref,
        parent_run_id=parent_run_id,
        retry_of_id=retry_of_id,
        extra=extra,
    )
    if status == WizardRunStatus.RUNNING:
        run.started_at = _now()
    db.add(run)
    db.flush()
    if commit:
        db.commit()
    return run


def set_status(
    db: Session,
    run: WizardRun,
    status: WizardRunStatus,
    *,
    progress: Optional[int] = None,
    error_message: Optional[str] = None,
    commit: bool = True,
) -> WizardRun:
    """Transition a run onto the canonical lifecycle. Stamps started_at
    on first RUNNING and ended_at/duration on a terminal state, and
    fires completion/failure notifications (bell + toast, NO email)."""
    prev = run.status
    run.status = status
    if progress is not None:
        run.progress = max(0, min(100, int(progress)))
    if error_message is not None:
        run.error_message = error_message

    if status in (WizardRunStatus.RUNNING, WizardRunStatus.VERIFYING) \
            and run.started_at is None:
        run.started_at = _now()

    became_terminal = (
        status in WIZARD_TERMINAL_STATUSES and prev not in WIZARD_TERMINAL_STATUSES
    )
    if became_terminal:
        run.ended_at = _now()
        if run.progress < 100 and status == WizardRunStatus.SUCCEEDED:
            run.progress = 100
        if run.started_at is not None:
            run.duration_seconds = max(
                0.0, (run.ended_at - run.started_at).total_seconds())

    db.flush()

    notify_recipients: List[int] = []
    if became_terminal and status in (
            WizardRunStatus.SUCCEEDED, WizardRunStatus.FAILED):
        notify_recipients = _create_terminal_notifications(db, run)

    if commit:
        db.commit()

    for uid in notify_recipients:
        _publish_notification(uid)
    return run


def append_log(
    db: Session,
    run: WizardRun,
    message: str,
    *,
    level: str = "info",
    commit: bool = True,
) -> WizardRunLog:
    """Persist ONE secret-redacted log line and fan it out for the live
    SSE tail. Redaction happens here so the persisted store, the live
    stream, and exports can never carry a secret (D19)."""
    redacted = redact(str(message))
    # next seq for this run
    last = (
        db.query(WizardRunLog.seq)
        .filter(WizardRunLog.run_id == run.id)
        .order_by(WizardRunLog.seq.desc())
        .first()
    )
    seq = (last[0] + 1) if last else 0
    log = WizardRunLog(
        run_id=run.id, seq=seq, level=level, message=redacted,
    )
    db.add(log)
    if commit:
        db.commit()
    _publish_log_event(run.id, seq, level, redacted)
    return log


# =====================================================================
# Visibility + action authorization (D5 / D15 / D17)
# =====================================================================
def _role(user: models.User) -> str:
    if getattr(user, "is_platform_admin", False):
        return "platform_admin"
    if getattr(user, "parent_user_id", None) is None:
        return "tenant_admin"
    return "user"


def scope_query(query, user: models.User):
    """Restrict a WizardRun query to what ``user`` may see."""
    role = _role(user)
    if role == "platform_admin":
        return query.filter(WizardRun.is_platform_level.is_(True))
    if role == "tenant_admin":
        return query.filter(
            WizardRun.is_platform_level.is_(False),
            WizardRun.tenant_id == user.tenant_id,
        )
    return query.filter(
        WizardRun.is_platform_level.is_(False),
        WizardRun.owner_user_id == user.id,
    )


def can_access(user: models.User, run: WizardRun) -> bool:
    """True if ``user`` may view AND act on ``run`` (visibility and
    action scope are identical — D15 mirrors D5)."""
    role = _role(user)
    if role == "platform_admin":
        return bool(run.is_platform_level)
    if run.is_platform_level:
        return False
    if role == "tenant_admin":
        return run.tenant_id == user.tenant_id
    return run.owner_user_id == user.id


# =====================================================================
# Cancel (hard + cascade) and retry (new linked run)  — D14 / D21
# =====================================================================
def cancel_run(db: Session, run: WizardRun, *, actor: models.User,
               commit: bool = True) -> WizardRun:
    """HARD cancel: immediately kill the underlying task/container and
    mark the run canceled. Cascades to every non-terminal child run a
    composite run spawned."""
    children = (
        db.query(WizardRun)
        .filter(
            WizardRun.parent_run_id == run.id,
            WizardRun.status.notin_(list(WIZARD_TERMINAL_STATUSES)),
            WizardRun.deleted_at.is_(None),
        )
        .all()
    )
    for child in children:
        _hard_kill_native(db, child)
        append_log(db, child,
                   f"Canceled via cascade from parent run {run.id}",
                   level="warn", commit=False)
        set_status(db, child, WizardRunStatus.CANCELED, commit=False)

    _hard_kill_native(db, run)
    append_log(db, run,
               f"Run hard-canceled by user {getattr(actor, 'id', '?')}",
               level="warn", commit=False)
    set_status(db, run, WizardRunStatus.CANCELED, commit=False)

    if commit:
        db.commit()
    return run


def retry_run(db: Session, run: WizardRun, *, actor: models.User,
              commit: bool = True) -> WizardRun:
    """Create a NEW WizardRun linked to ``run`` via retry_of and freshly
    dispatch it. The original keeps its terminal state (append-only
    history)."""
    new = create_run(
        db,
        wizard_type=run.wizard_type,
        target_name=run.target_name,
        target_ref_type=run.target_ref_type,
        target_ref_id=run.target_ref_id,
        owner_user_id=run.owner_user_id,
        tenant_id=run.tenant_id,
        log_source=run.log_source,
        native_source=run.native_source,
        retry_of_id=run.id,
        status=WizardRunStatus.QUEUED,
        # Retry keeps the ORIGINAL run's scope/classification — a retry
        # of a tenant run stays tenant-scoped regardless of who clicks
        # retry.
        origin=run.origin,
        is_platform_level=run.is_platform_level,
        extra=run.extra,
        commit=False,
    )
    append_log(db, new,
               f"Fresh retry of run {run.id} (started by user "
               f"{getattr(actor, 'id', '?')})",
               level="info", commit=False)

    hook = _RETRY_HOOKS.get(run.wizard_type)
    if hook is not None:
        try:
            hook(db, run, new)
        except Exception as e:  # noqa: BLE001
            logger.warning("wizard_queue: retry hook for %s failed on run "
                           "%s: %s", run.wizard_type, run.id, e)
            append_log(db, new,
                       f"retry dispatch hook error: {e}",
                       level="error", commit=False)

    if commit:
        db.commit()
    return new


# =====================================================================
# Soft-delete (dismiss / clear-completed)  — ISO no-hard-delete
# =====================================================================
def soft_delete(db: Session, run: WizardRun, *, actor: models.User,
                commit: bool = True) -> WizardRun:
    run.deleted_at = _now()
    run.deleted_by_user_id = getattr(actor, "id", None)
    db.flush()
    if commit:
        db.commit()
    return run


def clear_completed(db: Session, user: models.User) -> int:
    """Soft-delete every terminal, in-scope, not-already-deleted run."""
    q = scope_query(
        db.query(WizardRun).filter(
            WizardRun.deleted_at.is_(None),
            WizardRun.status.in_(list(WIZARD_TERMINAL_STATUSES)),
        ),
        user,
    )
    n = 0
    for run in q.all():
        soft_delete(db, run, actor=user, commit=False)
        n += 1
    db.commit()
    return n


# =====================================================================
# Log retention — delegated to the FX.57 system-log archive (Q40=A)
# =====================================================================
# wizard_run_logs are NO LONGER rotated (hard-deleted) here. The FX.57
# part-1 nightly archive job (services/system_log_archive.
# run_log_archive_tick, 03:55 UTC) owns system-log retention: it exports
# rows older than the ``logs.searchable_days`` platform setting off-box
# (gzip JSONL + SHA-256 manifest) and, only after verifying the upload,
# soft-marks the rows ``archived_at``. Rows are NEVER hard-deleted
# (ISO 27001 — no hard deletes, soft-delete only). The env-var retention
# path (WIZARD_LOG_RETENTION_DAYS) is removed: tunables live in the
# DB-backed platform-settings registry (group "logging"), not the env.
#
# ``rotate_logs`` is retained as a NO-OP so any residual caller stays
# harmless (it removes nothing) and is no longer scheduled.

def rotate_logs(db: Session, *, older_than_days: Optional[int] = None) -> int:
    """No-op retained for back-compat: returns 0 and removes NO rows.

    WizardRunLog retention is handled by the FX.57 system-log archive job
    (``services/system_log_archive.run_log_archive_tick``), which reads
    ``logs.searchable_days`` from the platform-settings registry and
    soft-marks (never deletes) rows past the window. ``older_than_days``
    is accepted but ignored — the env-var retention knob it mapped to
    (WIZARD_LOG_RETENTION_DAYS) has been removed. The WizardRun record is
    untouched, as before."""
    logger.info("wizard_queue.rotate_logs is a no-op — wizard_run_logs are "
                "archived (never deleted) by the FX.57 system-log archive "
                "job; nothing removed")
    return 0


# =====================================================================
# Notifications (bell + toast only, NO email)  — D18
# =====================================================================
def _create_terminal_notifications(db: Session, run: WizardRun) -> List[int]:
    """Insert Notification rows for the owner + relevant admin. Returns
    the recipient ids so the caller can publish the realtime signal
    AFTER commit. Never sends email."""
    recipients: set[int] = set()
    if run.owner_user_id:
        recipients.add(run.owner_user_id)

    try:
        if run.is_platform_level:
            admins = (
                db.query(models.User.id)
                .filter(
                    models.User.is_platform_admin.is_(True),
                    models.User.status == models.UserStatus.ACTIVE,
                )
                .all()
            )
        elif run.tenant_id is not None:
            admins = (
                db.query(models.User.id)
                .filter(
                    models.User.tenant_id == run.tenant_id,
                    models.User.parent_user_id.is_(None),
                    models.User.status == models.UserStatus.ACTIVE,
                )
                .all()
            )
        else:
            admins = []
        recipients.update(a[0] for a in admins)
    except Exception as e:  # noqa: BLE001 — never block the transition
        logger.warning("wizard_queue: admin lookup for notifications "
                       "failed on run %s: %s", run.id, e)

    succeeded = run.status == WizardRunStatus.SUCCEEDED
    ntype = "wizard_run_succeeded" if succeeded else "wizard_run_failed"
    verb = "completed" if succeeded else "failed"
    name = run.target_name or run.wizard_type.value
    msg = f"{run.wizard_type.value} build '{name}' {verb}"

    for uid in recipients:
        db.add(models.Notification(
            recipient_user_id=uid,
            message=msg,
            notification_type=ntype,
            link_url=f"#wizard-queue/{run.id}",
        ))
    db.flush()
    return list(recipients)


def _publish_notification(recipient_user_id: int) -> None:
    try:
        from routers.notifications import publish_notification_event
        publish_notification_event(recipient_user_id)
    except Exception:  # noqa: BLE001
        pass


# =====================================================================
# Live log fan-out (Redis pub/sub — SSE tail)
# =====================================================================
def _redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://redis:6379/2")


def log_channel(run_id: str) -> str:
    return f"wizard:logs:{run_id}"


def _publish_log_event(run_id: str, seq: int, level: Optional[str],
                       message: str) -> None:
    """Fire-and-forget push of a (already-redacted) log line so live SSE
    subscribers wake up. The poll fallback covers any missed wakeups."""
    try:
        import json
        import redis as redis_lib
        r = redis_lib.Redis.from_url(_redis_url())
        r.publish(log_channel(run_id), json.dumps(
            {"seq": seq, "level": level, "message": message}))
    except Exception as e:  # noqa: BLE001
        logger.debug("wizard_queue: log publish failed for run %s: %s",
                     run_id, e)
