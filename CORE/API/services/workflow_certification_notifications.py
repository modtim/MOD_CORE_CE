"""FORTRESS 5.10 — certification expiry notifications (service only).

A WorkflowCertification expires on ``expires_at``. This service tells the people
who care (the user who created the certification, plus the workflow's creator)
when a threshold — 30 / 7 / 1 days out — is crossed, so they can re-certify
before the claim stops holding.

The hourly scheduler tick (task 763) calls ``notify_expiring`` once an hour.
Because there is no "notified-thresholds" column on WorkflowCertification, the
window test is what makes the sweep idempotent: a threshold counts as crossed
only when the boundary sits inside the last hour
(``T days - 1 hour < expires_at - now <= T days``). A defensive stateless dedup
(a prior Notification carrying the deterministic ``[cert:<id>:<T>d]`` message
prefix for the same recipient) backs that up, so a re-run in the same window —
or a scheduler that fires twice — never fans out duplicates.

Never commits: callers own the transaction; every mutating helper flushes only.
Callers publish the in-app events AFTER the commit (see
``routers.notifications.publish_notification_event``) using the recipient ids
returned alongside the count.

No signature/key material ever reaches a notification: the message names the
workflow, the countdown and the expiry date, nothing else.
"""
from __future__ import annotations

from product_names import themed_logger
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session  # noqa: F401 — session type for callers

from models import Notification, User, Workflow, WorkflowCertification

logger = themed_logger(__name__, 'workflows')

THRESHOLD_DAYS = (30, 7, 1)

# The window a threshold is considered "crossed" in: the hourly scheduler tick
# gives us one hour of slack, so a boundary up to an hour in the past still
# counts. Anything older already got its notification on a previous tick.
WINDOW = timedelta(hours=1)


def _utc(value):
    """Coerce a possibly-naive DB timestamp to UTC so comparisons are safe."""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _prefix(certification_id: int, threshold_days: int) -> str:
    """Deterministic dedup marker embedded at the head of every message."""
    return f"[cert:{certification_id}:{threshold_days}d]"


def _recipients(db, cert) -> list[int]:
    """Who hears about this certification: its creator, plus the workflow's
    creator when that column exists. A recipient is only eligible while they
    still belong to the certification's tenant and are not soft-deleted — a
    transferred or erased user must never be notified (tenant safety)."""
    ids: list[int] = []
    wf = db.query(Workflow).filter(Workflow.id == cert.workflow_id).first()
    candidates = [cert.created_by_user_id]
    if wf is not None:
        candidates.append(getattr(wf, "created_by_user_id", None))
    for uid in candidates:
        if uid is None or uid in ids:
            continue
        u = db.query(User).filter(User.id == uid).first()
        if u is None:
            continue
        if u.tenant_id != cert.tenant_id:
            continue
        if getattr(u, "deleted_at", None) is not None:
            continue
        ids.append(int(uid))
    return ids


def notify_expiring(db: Session, now: Optional[datetime] = None) -> tuple[int, list[int]]:
    """Sweep live active certifications; notify each recipient whose expiry
    crossed a THRESHOLD_DAYS boundary inside the last hourly window.

    Returns (notifications_created, recipient_user_ids). Flushes only — the
    caller commits, then publishes an event per recipient id.
    """
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    created = 0
    recipients: list[int] = []
    certs = (
        db.query(WorkflowCertification)
        .filter(WorkflowCertification.deleted_at.is_(None),
                WorkflowCertification.status == "active")
        .all()
    )
    for cert in certs:
        expires_at = _utc(cert.expires_at)
        if expires_at is None:
            continue
        for threshold in THRESHOLD_DAYS:
            boundary = expires_at - timedelta(days=threshold)
            if not (now - WINDOW < boundary <= now):
                continue
            prefix = _prefix(cert.id, threshold)
            days_left = "1 day" if threshold == 1 else f"{threshold} days"
            for uid in _recipients(db, cert):
                prior = (
                    db.query(Notification)
                    .filter(Notification.recipient_user_id == uid,
                            Notification.message.like(f"{prefix}%"))
                    .first()
                )
                if prior is not None:
                    continue
                wf = db.query(Workflow).filter(Workflow.id == cert.workflow_id).first()
                wf_name = wf.name if wf is not None else f"workflow {cert.workflow_id}"
                db.add(Notification(
                    recipient_user_id=uid,
                    message=(f"{prefix} Workflow certification for "
                             f"'{wf_name}' expires in {days_left} on "
                             f"{expires_at.strftime('%Y-%m-%d %H:%M UTC')}"),
                    is_read=False,
                    notification_type="certification_expiry",
                    severity="warning",
                ))
                db.flush()
                created += 1
                if uid not in recipients:
                    recipients.append(uid)
    return created, recipients


def expired_warning(db: Session, tenant_id: int, workflow_id: int,
                    now: Optional[datetime] = None) -> Optional[dict]:
    """The 'expired-certification warning surface': if this workflow has NO
    currently valid certification (live, active, not yet expired) but does
    have an expired one, describe the most recent expired row. Tenant-scoped;
    returns None cross-tenant and None while a valid cert still covers it.
    """
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    live = (
        db.query(WorkflowCertification)
        .filter(WorkflowCertification.tenant_id == tenant_id,
                WorkflowCertification.workflow_id == workflow_id,
                WorkflowCertification.deleted_at.is_(None),
                WorkflowCertification.status == "active",
                WorkflowCertification.expires_at > now)
        .first()
    )
    if live is not None:
        return None
    expired = (
        db.query(WorkflowCertification)
        .filter(WorkflowCertification.tenant_id == tenant_id,
                WorkflowCertification.workflow_id == workflow_id,
                WorkflowCertification.deleted_at.is_(None))
        .filter(
            (WorkflowCertification.status == "expired")
            | ((WorkflowCertification.status == "active")
               & (WorkflowCertification.expires_at <= now))
        )
        .order_by(WorkflowCertification.expires_at.desc(),
                   WorkflowCertification.id.desc())
        .first()
    )
    if expired is None:
        return None
    return {"certification_id": expired.id,
            "expired_at": _utc(expired.expires_at)}
