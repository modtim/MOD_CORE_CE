"""Business logic for `routers/persistent_stacks/workers_fleet.py` (Gate 2b).

Companion to `crud/persistent_stacks_workers_fleet.py` (the only place this
slice's `db.query` lives). Extracted verbatim from the router during the
Gate-2b migration — a behaviour-preserving MOVE: every function below is the
same logic that used to live inline in the router handler of the same name,
with only the query call now routed through the crud module.

This router package (`routers/persistent_stacks/`) has never used the
`services._base.ServiceError` scaffold — its handlers raise plain
`HTTPException` inline (see `services/persistent_stacks_domain_service.py`'s
docstring for the same note). Keeping that convention here rather than
switching to `ServiceError` changes nothing observable but avoids an
unrequested behavior change.

`_shared` (compose validation, mesh/audit/response-shaping helpers,
`STACK_TEMPLATES`, etc.) is imported from, never edited — function-local
imports, matching the existing convention in
`services/persistent_stacks_domain_service.py`, to avoid forcing the whole
`routers.persistent_stacks` package to initialize at this module's import
time.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

import models
from crud import persistent_stacks_workers_fleet as psw_crud


# =============================================================================
# create_stack — POST ""
# =============================================================================

def create_stack(db: Session, body: "schemas.PersistentStackCreate", user: "models.User"):  # noqa: F821
    from routers.persistent_stacks._shared import (
        _validate_compose, _reject_mesh_name_collision, _require_backup_ack,
        _parse_accelerator_requirements, _parse_egress_policy,
        _parse_secret_refs, _parse_tls_config, _resolve_home_zone,
        _audit_stack, _out,
    )
    from utils.paid_import import optional_paid
    # GT.D6 DS-4 — `stack_logical_backup` is Paid: CE degrades to "no backup-ack
    # services surfaced" (the CE-side `_require_backup_ack` gate still runs).
    _backup_mod = optional_paid("services.stack_logical_backup")

    _validate_compose(body.compose_spec, is_admin=bool(getattr(user, "is_platform_admin", False)),
                       secret_prefix=f"tenants/{user.tenant_id}/stacks/{body.name}/",
                       tenant_id=user.tenant_id, db=db)
    _reject_mesh_name_collision(db, user.tenant_id, body.name, body.compose_spec)
    # GA.ASW.6 — a known-database image with no backup posture declared
    # requires an explicit acknowledge_no_backup before the row is created.
    _require_backup_ack(body.compose_spec, body.acknowledge_no_backup)
    _backup_ack_services = (_backup_mod.backup_ack_required(body.compose_spec)
                            if _backup_mod else [])
    # GR.FEN.3a — stop silently defaulting to `external`: an explicit ha_class
    # wins (validated → 422 on an unknown value); otherwise infer it from the
    # compose's mounts (writable named volume / non-cache host bind →
    # external; read-only or model-cache mounts → cache_backed; no volumes →
    # stateless). The reason travels on the create response only.
    from fastapi import HTTPException
    # GT.D6 DS-4 — `stack_ha_class` is Paid: CE keeps the conservative model
    # default (`external`, never auto-re-placed) and honours an explicit
    # request value as-is (the request schema already rejects unknown classes).
    _ha_mod = optional_paid("services.stack_ha_class")
    if _ha_mod:
        try:
            ha_class, ha_class_reason = _ha_mod.resolve_create_ha_class(
                body.compose_spec, body.ha_class)
        except _ha_mod.InvalidHaClass as e:
            raise HTTPException(status_code=422, detail=e.args[0] if e.args else str(e))
    else:
        ha_class = body.ha_class or "external"
        ha_class_reason = ("explicitly set at create" if body.ha_class
                           else "not inferred (CE)")
    stack = models.PersistentStack(
        tenant_id=user.tenant_id,
        created_by_user_id=user.id,
        name=body.name,
        compose_spec=body.compose_spec,
        accelerator_requirements=_parse_accelerator_requirements(body.compose_spec) or None,
        egress_policy=_parse_egress_policy(body.compose_spec) or None,
        secret_refs=_parse_secret_refs(body.compose_spec) or None,
        tls_config=_parse_tls_config(body.compose_spec) or None,
        storage_class=body.storage_class,
        home_zone=_resolve_home_zone(body.home_zone),
        quota_bytes=body.quota_bytes,
        status="created",
        # Undeclared means `external` — never auto-re-placed. The platform
        # cannot know what someone else's stack keeps, and the cost of guessing
        # wrong is asymmetric: failing to re-place a stateless service costs
        # downtime, wrongly re-placing a stateful one can lose data or run two
        # primaries.
        ha_class=ha_class,
        # A stack starts life meant to run; `deploy` is what acts on that.
        desired_state="running",
    )
    db.add(stack)
    db.commit()
    db.refresh(stack)
    _audit_extra = ({"backup_ack": True, "backup_ack_services": _backup_ack_services}
                    if _backup_ack_services else None)
    _audit_extra = _audit_extra or {}
    _audit_extra["ha_class"] = ha_class
    if ha_class_reason != "explicitly set at create":
        _audit_extra["ha_class_inferred"] = True
        _audit_extra["ha_class_reason"] = ha_class_reason
    _audit_stack(db, "STACK_CREATED", stack, user, _audit_extra or None)
    db.commit()
    # stack-training-and-prewarm §2.1 — enqueue an async characterization run when
    # no passing StackProfile exists. Best-effort so it can never fail a create.
    # GT.D6 DS-4 — `stack_calibration` is Paid: CE skips the characterization
    # enqueue (it was best-effort anyway).
    _sc = optional_paid("services.stack_calibration")
    if _sc is not None:
        _sc.maybe_enqueue_characterization(db, stack)
    out = _out(stack)
    out.ha_class_reason = ha_class_reason
    return out


# =============================================================================
# acknowledge_host_access — POST /{stack_id}/acknowledge-host-access
# =============================================================================

def acknowledge_host_access(db: Session, stack_id: int, body: "_AcknowledgeHostAccessRequest",  # noqa: F821
                             user: "models.User"):
    from fastapi import HTTPException
    from routers.persistent_stacks._shared import (
        _get_owned, _validate_service_security, _COMPOSE_ADMIN_ONLY_KEYS,
        _ADMIN_ACK_KEY, _audit_stack, _out,
    )

    stack = _get_owned(db, stack_id, user)
    services = (stack.compose_spec or {}).get("services") or {}
    svc = services.get(body.service)
    if not isinstance(svc, dict):
        raise HTTPException(status_code=404,
                             detail=f"service {body.service!r} not found on this stack")
    # Defense in depth: the hard refusals apply here too, so an admin cannot
    # acknowledge a service that should never have been stored at all.
    _validate_service_security(body.service, svc, is_admin=True,
                                allowed_host_mount_prefixes=())
    if not any(k in svc for k in _COMPOSE_ADMIN_ONLY_KEYS):
        raise HTTPException(
            status_code=400,
            detail=f"service {body.service!r} uses no admin-only construct — nothing to acknowledge")

    spec = copy.deepcopy(stack.compose_spec)
    target = spec["services"][body.service]
    xmod = target.get("x-mod")
    if not isinstance(xmod, dict):
        xmod = {}
        target["x-mod"] = xmod
    xmod[_ADMIN_ACK_KEY] = {
        "by_user_id": user.id,
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": body.reason or None,
    }
    stack.compose_spec = spec
    db.add(stack)
    _audit_stack(db, "STACK_HOST_ACCESS_ACKNOWLEDGED", stack, user,
                 {"service": body.service, "reason": body.reason})
    db.commit()
    db.refresh(stack)
    return _out(stack)


# =============================================================================
# list_stacks — GET ""
# =============================================================================

def list_stacks(db: Session, user: "models.User"):
    from routers.persistent_stacks._shared import _out_with_cost

    rows = psw_crud.list_stacks_for_tenant(db, user.tenant_id)
    out = [_out_with_cost(db, s) for s in rows]
    _attach_hardware_ineligible(db, rows, out)
    return out


def _attach_hardware_ineligible(db: Session, rows, dtos) -> None:
    """Mark, per stack, the workers whose hardware can never host it.

    Best-effort: a failure here must not take the stack list down with it —
    the pin endpoint re-checks and refuses on its own.
    """
    from routers.persistent_stacks._shared import _fleet_capacity_workers, logger
    from utils.accelerator_fit import hardware_ineligible
    try:
        workers = _fleet_capacity_workers(db)
        cache: dict = {}
        for st, dto in zip(rows, dtos):
            dto.hardware_ineligible = hardware_ineligible(db, st, workers, cache)
    except Exception:  # noqa: BLE001
        logger.debug("hardware eligibility unavailable for stack list", exc_info=True)


# =============================================================================
# list_templates — GET /templates
# =============================================================================

def list_templates() -> list:
    from routers.persistent_stacks._shared import STACK_TEMPLATES

    return [{"id": tid, **meta} for tid, meta in STACK_TEMPLATES.items()]


# =============================================================================
# set_rebalancer_config — PUT /rebalancer-config
# =============================================================================

def set_rebalancer_config(db: Session, body: "_RebalancerThreshold", user: "models.User") -> Dict[str, Any]:  # noqa: F821
    from fastapi import HTTPException
    from routers.persistent_stacks._shared import logger

    if not getattr(user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin required")
    if body.threshold is not None and not (0 <= body.threshold <= 10):
        raise HTTPException(status_code=422, detail="threshold must be between 0 and 10")
    row = psw_crud.get_rebalancer_config_singleton(db)
    if row is None:
        row = models.StackRebalancerConfig(singleton_id=1)
        db.add(row)
    if body.threshold is not None:
        row.threshold = body.threshold
    if body.enabled is not None:
        row.enabled = body.enabled
        logger.warning("stack rebalancer %s by user %s — automatic stack relocation "
                        "is now %s", "ENABLED" if body.enabled else "disabled", user.id,
                        "live" if body.enabled else "off")
    row.updated_by_user_id = user.id
    db.commit()
    db.refresh(row)
    logger.info("stack rebalancer config set (threshold=%.3f enabled=%s) by user %s",
                row.threshold, row.enabled, user.id)
    return {"threshold": row.threshold, "enabled": row.enabled, "source": "db"}
