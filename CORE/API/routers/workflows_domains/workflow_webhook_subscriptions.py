# GX.R3 — run-completion webhook subscription management.
#
# Tenant-scoped, per-workflow subscriptions that get an HMAC-signed POST when
# a run of the workflow reaches a terminal state. RBAC: the same "edit"
# workflow capability required to modify the workflow itself (see
# _user_can_access_workflow). Create/delete + secret rotation emit audit
# events via audit.py (secret values never reach the audit row).
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
import schemas
import audit
from dependencies import get_db, require_active_and_onboarded_user
from routers.workflows_domains._shared import _user_can_access_workflow
from services import run_completion_webhooks as rcw

router = APIRouter()


class _RunWebhookSubscriptionTenantOut(schemas.RunWebhookSubscriptionOut):
    """B36a row: the per-workflow row fields + the workflow's name. Inherits a
    model that has no secret field, so the signing secret can never leak."""
    workflow_name: Optional[str] = None


def _require_workflow_edit(db, current_user, workflow_id: int) -> models.Workflow:
    """404 (non-leak) unless the workflow exists in the caller's tenant AND
    the caller holds the "edit" capability on it — the same gate editing the
    workflow itself uses."""
    wf = (
        db.query(models.Workflow)
        .filter(models.Workflow.id == workflow_id, models.Workflow.tenant_id == current_user.tenant_id)
        .first()
    )
    if wf is None or wf.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, wf, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


def _audit(db, action, current_user, workflow_id: int, extra: Optional[dict] = None) -> None:
    try:
        audit.log_permission_audit(
            db=db,
            action=action,
            actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            resource_type="workflow",
            resource_id=workflow_id,
            metadata=extra or {},
        )
    except Exception:
        # Audit failure must not break the route.
        pass


@router.post(
    "/{workflow_id:int}/webhook-subscriptions",
    response_model=schemas.RunWebhookSubscriptionCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_run_webhook_subscription(
    workflow_id: int,
    body: schemas.RunWebhookSubscriptionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Create a run-completion webhook subscription for a workflow.

    Returns the signing secret ONCE (`secret` field); it is stored only in
    encrypted form and can be recovered via rotate, never re-read.
    """
    wf = _require_workflow_edit(db, current_user, workflow_id)
    refusal, _exempt = rcw.check_target(
        db, current_user.tenant_id, body.url, phase="create",
    )
    if refusal:
        raise HTTPException(status_code=422, detail={"error": "invalid_callback_url", "message": refusal})
    # GSEC FX.161.p1b — refuse (403) a destination outside the tenant's
    # output allowlist (no-op when the tenant hasn't enforced one).
    # FX.162.p5 — CE carve: no DLP output allowlist -> no restriction.
    from utils.paid_import import optional_paid
    _dlp = optional_paid("services.dlp_output_allowlist")
    if _dlp is not None:
        _dlp.check_destination(
            db, current_user.tenant_id, body.url,
            kind="run_completion_webhook", user_id=current_user.id,
        )
    sub, raw_secret = rcw.create_subscription(
        db, tenant_id=current_user.tenant_id, workflow_id=wf.id,
        url=body.url, user_id=current_user.id,
    )
    _audit(db, models.AuditAction.RUN_WEBHOOK_SUBSCRIPTION_CREATED, current_user, wf.id,
           {"subscription_id": sub.id})
    return schemas.RunWebhookSubscriptionCreated(
        id=sub.id, tenant_id=sub.tenant_id, workflow_id=sub.workflow_id,
        url=sub.url, is_active=sub.is_active, description=sub.description,
        created_at=sub.created_at, secret=raw_secret,
    )


@router.get("/{workflow_id:int}/webhook-subscriptions",
            response_model=List[schemas.RunWebhookSubscriptionOut])
def list_run_webhook_subscriptions(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List the workflow's active subscriptions (no secrets)."""
    wf = _require_workflow_edit(db, current_user, workflow_id)
    subs = rcw.list_subscriptions(db, tenant_id=current_user.tenant_id, workflow_id=wf.id)
    return [
        schemas.RunWebhookSubscriptionOut(
            id=s.id, tenant_id=s.tenant_id, workflow_id=s.workflow_id,
            url=s.url, is_active=s.is_active, description=s.description,
            created_at=s.created_at,
        )
        for s in subs
    ]


@router.get("/webhook-subscriptions",
            response_model=List[_RunWebhookSubscriptionTenantOut])
def list_all_run_webhook_subscriptions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 100,
    skip: int = 0,
):
    """B36a — tenant-wide list of run-completion webhook subscriptions ("one
    workflow" scope rows for the merged Webhooks UI), so the UI can show every
    subscription without iterating workflows. Same gate as the per-workflow
    list: a row appears only for workflows the caller may edit. Soft-deleted
    subscriptions/workflows are excluded. The signing secret is NEVER returned.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if skip < 0:
        raise HTTPException(status_code=422, detail="skip must be >= 0")
    rows = rcw.list_tenant_subscriptions(db, tenant_id=current_user.tenant_id,
                                         skip=skip, limit=limit)
    out = []
    for sub, wf in rows:
        if wf is None or not _user_can_access_workflow(db, current_user, wf, "edit"):
            continue
        out.append(
            _RunWebhookSubscriptionTenantOut(
                id=sub.id, tenant_id=sub.tenant_id, workflow_id=sub.workflow_id,
                workflow_name=(wf.name if wf else None), url=sub.url,
                is_active=sub.is_active, description=sub.description,
                created_at=sub.created_at,
            )
        )
    return out


@router.delete("/{workflow_id:int}/webhook-subscriptions/{sub_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_run_webhook_subscription(
    workflow_id: int,
    sub_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete a subscription (ISO 27001: no hard deletes)."""
    wf = _require_workflow_edit(db, current_user, workflow_id)
    deleted = rcw.soft_delete_subscription(
        db, tenant_id=current_user.tenant_id, sub_id=sub_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    _audit(db, models.AuditAction.RUN_WEBHOOK_SUBSCRIPTION_DELETED, current_user, wf.id,
           {"subscription_id": sub_id})
    return None


@router.post("/{workflow_id:int}/webhook-subscriptions/{sub_id}/rotate-secret",
             response_model=schemas.RunWebhookSecretRotated)
def rotate_run_webhook_subscription_secret(
    workflow_id: int,
    sub_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Rotate the signing secret. The new secret is returned ONCE; the old
    secret stops being valid immediately (pending deliveries made with the
    old secret will fail signature verification at the subscriber)."""
    wf = _require_workflow_edit(db, current_user, workflow_id)
    sub, raw_secret = rcw.rotate_subscription_secret(
        db, tenant_id=current_user.tenant_id, sub_id=sub_id
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    _audit(db, models.AuditAction.RUN_WEBHOOK_SECRET_ROTATED, current_user, wf.id,
           {"subscription_id": sub_id})
    return schemas.RunWebhookSecretRotated(id=sub.id, secret=raw_secret)


@router.get("/{workflow_id:int}/webhook-subscriptions/deliveries",
            response_model=List[schemas.RunWebhookDeliveryOut])
def list_run_webhook_deliveries(
    workflow_id: int,
    sub_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Delivery log for this workflow (append-only attempt detail lives on
    each row's last_* fields; full per-attempt history via the delivery id)."""
    wf = _require_workflow_edit(db, current_user, workflow_id)
    rows = rcw.list_deliveries(
        db, tenant_id=current_user.tenant_id, workflow_id=wf.id,
        sub_id=sub_id, skip=skip, limit=limit,
    )
    return [
        schemas.RunWebhookDeliveryOut(
            id=d.id, tenant_id=d.tenant_id, workflow_id=d.workflow_id,
            subscription_id=d.subscription_id, run_id=d.run_id, url=d.url,
            event=d.event, delivery_uuid=d.delivery_uuid, status=d.status,
            attempts=d.attempts, last_attempted_at=d.last_attempted_at,
            last_http_status=d.last_http_status, last_error=d.last_error,
            next_attempt_at=d.next_attempt_at, created_at=d.created_at,
        )
        for d in rows
    ]


@router.get("/{workflow_id:int}/webhook-subscriptions/deliveries/{delivery_id}/attempts",
            response_model=List[schemas.RunWebhookDeliveryAttemptOut])
def list_run_webhook_delivery_attempts(
    workflow_id: int,
    delivery_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Append-only per-attempt history for one delivery."""
    wf = _require_workflow_edit(db, current_user, workflow_id)
    delivery = rcw.get_delivery(db, tenant_id=current_user.tenant_id, delivery_id=delivery_id)
    if delivery is None or delivery.workflow_id != wf.id:
        raise HTTPException(status_code=404, detail="Delivery not found")
    rows = (
        db.query(models.RunWebhookDeliveryAttempt)
        .filter(models.RunWebhookDeliveryAttempt.delivery_id == delivery.id)
        .order_by(models.RunWebhookDeliveryAttempt.attempt_number)
        .all()
    )
    return [
        schemas.RunWebhookDeliveryAttemptOut(
            id=a.id, delivery_id=a.delivery_id, attempt_number=a.attempt_number,
            outcome=a.outcome, http_status=a.http_status, error=a.error,
            attempted_at=a.attempted_at,
        )
        for a in rows
    ]
