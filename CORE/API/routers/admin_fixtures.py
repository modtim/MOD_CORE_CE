"""Admin fixture endpoints — tenant calibration-fixture migration status (GC.PFM.8a).

Three routes (mounted under /v1 by main.py in GC.PFM.8b — no /v1 here):
  GET  /admin/fixtures                    — platform admin: per-tenant status list.
  POST /admin/fixtures/{tenant_id}/migrate — platform admin: dispatch the
                                             fixture-migrator container task.
  POST /admin/fixtures/callback           — run-task node token (PFM.7b migrator)
                                             OR platform admin (PFM.16 browser
                                             test): record the migrator result.

Soft-state only — nothing is ever deleted (ISO 27001).
"""
from __future__ import annotations

import logging
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services import fixture_service
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Admin / Fixtures"],
    prefix="/admin/fixtures",
)


def _require_platform_admin(current_user: models.User = Depends(require_active_and_onboarded_user)):
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return current_user


class FixtureTenantStatus(BaseModel):
    tenant_id: int
    name: Optional[str] = None
    fixture_status: Optional[str] = None
    fixture_version: Optional[str] = None
    fixture_checked_at: Optional[str] = None


class FixtureMigrateResponse(BaseModel):
    tenant_id: int
    task_id: int
    fixture_status: str


class FixtureMigrateBody(BaseModel):
    # PFM §4.1 — optional migrator mode (PFM §2.2: sync | seed-empty | verify).
    mode: Optional[str] = None


def _fixture_bucket_disabled():
    """FIX-FIXTURE-NITS: the one 409 the migrate endpoint answers while the
    PFM.19 cutover flag is off. A helper so the endpoint and the
    `dispatch_migration` `None` guard (the flag can flip between the check
    and the dispatch) raise the exact same status + detail — never a pydantic
    500 on a `None` task_id."""
    raise HTTPException(
        status_code=409,
        detail=(
            "Fixture shared bucket is disabled (PFM.19 cutover flag "
            "fixture_shared_bucket_enabled off): the platform has no "
            "shared fixture bucket and no fixture-migrator image to "
            "dispatch; the daily drift audit and calibration gate are "
            "inert too."
        ),
    )


class FixtureMigrateAllResponse(BaseModel):
    # PFM §4.1 fan-out shape: {dispatched: [{tenant_id, task_id}, ...]}.
    dispatched: list[FixtureMigrateResponse]


class FixtureCallbackBody(BaseModel):
    tenant_id: int
    ok: bool
    version: Optional[str] = None
    error: Optional[str] = None


class FixtureCallbackResponse(BaseModel):
    tenant_id: int
    fixture_status: str


@router.get("", response_model=list[FixtureTenantStatus])
def list_fixture_status(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(_require_platform_admin),
):
    """Per-tenant calibration-fixture migration status (platform admin)."""
    tenants = fixture_service.list_all_tenants_ordered(db)
    return [
        FixtureTenantStatus(
            tenant_id=t.id,
            name=t.name,
            fixture_status=t.fixture_status,
            fixture_version=t.fixture_version,
            fixture_checked_at=t.fixture_checked_at.isoformat() if t.fixture_checked_at else None,
        )
        for t in tenants
    ]


@router.post("/callback", response_model=FixtureCallbackResponse)
def record_fixture_result(
    body: FixtureCallbackBody,
    principal: Union[dict, models.User] = Depends(node_or_service_principal),
    db: Session = Depends(get_db),
):
    """Record a fixture-migrator result.

    Accepts the migrator's run-task token (tenant pinned from the verified
    token — a token for another tenant 403s) OR a platform admin, so PFM.16
    can mark a tenant ready from a browser test.
    """
    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=body.tenant_id)
    elif not getattr(principal, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin only")

    # FIX-FIXTURE-GATE-2 (REV-FIXTURE-GATE M1): fail closed like the migrate
    # route. A result is only accepted while the PFM.19 flag is on AND the
    # tenant is 'pending' (the marker `dispatch_migration` sets when a
    # migration is queued). Otherwise any run-task token for the tenant could
    # mark it 'ready' without a migration ever running.
    if not fixture_service.shared_bucket_enabled(db):
        _fixture_bucket_disabled()
    # FIX-FIXTURE-CALLBACK-DELETED-TENANT: same soft-delete skip as the
    # single-tenant migrate route (admin_fixtures.py:221) — a retired tenant
    # 404s here too, and its fixture_status is never flipped to 'ready' by a
    # stray run-task token. Checked BEFORE the pending check so a deleted
    # non-pending tenant answers 404, not 409.
    _t = db.get(models.Tenant, body.tenant_id)
    if _t is None or _t.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"Tenant {body.tenant_id} not found")
    if not fixture_service.is_pending(db, body.tenant_id):
        if fixture_service.list_tenant_exists(db, body.tenant_id):
            raise HTTPException(
                status_code=409,
                detail="No fixture migration is pending for this tenant",
            )
        raise HTTPException(status_code=404, detail=f"Tenant {body.tenant_id} not found")

    updated = fixture_service.record_result(db, body.tenant_id, body.ok, body.version)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Tenant {body.tenant_id} not found")
    if not body.ok:
        # FIX-FIXTURE-GATE-2 (LOW): record the migrator's reason (truncated).
        logger.warning(
            "fixture migration failed for tenant %s: %s",
            body.tenant_id, (body.error or "no error reported")[:200],
        )
    return FixtureCallbackResponse(
        tenant_id=body.tenant_id,
        fixture_status="ready" if body.ok else "failed",
    )


@router.post(
    "/{tenant_id}/migrate",
    response_model=Union[FixtureMigrateResponse, FixtureMigrateAllResponse],
)
def migrate_fixtures(
    tenant_id: str,
    body: Optional[FixtureMigrateBody] = None,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(_require_platform_admin),
):
    """Dispatch the fixture-migrator container task for a tenant, or for
    every tenant when `tenant_id` is `"*"` (PFM §4.1 fan-out: the response
    is `{dispatched: [{tenant_id, task_id}, ...]}`).

    `body.mode` selects the PFM §2.2 migrator mode (sync | seed-empty |
    verify); omitted = sync. Marks each tenant pending and enqueues its
    task; the migrator reports back via /admin/fixtures/callback."""
    mode = (body.mode or "sync").strip() if body else "sync"
    if mode not in ("sync", "seed-empty", "verify"):
        raise HTTPException(
            status_code=422,
            detail="mode must be one of: sync, seed-empty, verify",
        )
    # FIX-FIXTURE-GATE (Gate 6 deviation D9): `fixture_shared_bucket_enabled`
    # (PFM.19 cutover, default off — the Community Edition default, since the
    # fixture-migrator image is private and cannot be pulled from a public
    # Harbor project) is the flag `fixture_service.dispatch_migration` gates
    # on. With it off there is nothing to dispatch: fail the admin request
    # cleanly instead of returning a phantom task id (and instead of the
    # worker being denied the same image on every heartbeat, forever).
    if not fixture_service.shared_bucket_enabled(db):
        _fixture_bucket_disabled()
    if tenant_id == "*":
        dispatched = []
        for t in fixture_service.list_all_tenants_ordered(db):
            if t.deleted_at is not None:
                continue
            # FIX-FIXTURE-NITS: the flag can flip off between the check above
            # and the dispatch — `dispatch_migration` then answers `None`
            # (skipped), which is not a task id. Answer the same 409 the
            # disabled-flag check already uses instead of building a response
            # with a None task_id (pydantic would 500).
            task_id = fixture_service.dispatch_migration(db, t.id, mode)
            if task_id is None:
                _fixture_bucket_disabled()
            dispatched.append(
                FixtureMigrateResponse(
                    tenant_id=t.id,
                    task_id=task_id,
                    fixture_status="pending",
                )
            )
        return FixtureMigrateAllResponse(dispatched=dispatched)
    try:
        tid = int(tenant_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    _t = db.get(models.Tenant, tid)
    # FIX-FIXTURE-GATE-2 (LOW): same soft-delete skip as the "*" fan-out.
    if _t is None or _t.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    task_id = fixture_service.dispatch_migration(db, tid, mode)
    if task_id is None:
        # FIX-FIXTURE-NITS: the PFM.19 flag flipped off between the endpoint's
        # check and the dispatch — same 409 as the disabled-flag check, never a
        # pydantic 500 on a None task_id.
        _fixture_bucket_disabled()
    return FixtureMigrateResponse(
        tenant_id=tid,
        task_id=task_id,
        fixture_status="pending",
    )
