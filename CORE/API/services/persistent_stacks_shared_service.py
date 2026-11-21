"""Persistent-stacks `_shared` module service layer (Gate 2b).

Holds the policy extracted out of the query-bearing helpers in
`routers/persistent_stacks/_shared.py`. Each function below is a verbatim
extraction of logic that used to live inline in the `_shared.py` helper of the
same name; control flow, error types and log messages are preserved exactly
so runtime behavior stays byte-identical. `_shared.py` itself keeps each
helper defined (same name/signature/docstring) as a thin delegation into this
module, per the migration's rule that its module-level identity must survive
(`_shared` is imported from — by many sibling sub-modules AND the package
`__init__.py` via `from ._shared import *` — never edited by this file).

Data access goes through `crud.persistent_stacks_shared` (the only place
`db.query(models.X)` lives for this slice).

EXCLUDED BY DESIGN: the managed-AI placement chain (`_managed_ai_placement`,
`apply_managed_ai_core`, `_fleet_capacity_workers`, `_fleet_worker_devices`,
`_largest_fitting_tier`, `_host_device_summary`, `_redeploy_host`,
`_locked_host_rejection`, `_relocatable_from_locked_host`, `_best_fleet_preset`,
`reconcile_managed_ai`, `_dispatch_stack_task`) stays physically defined in
`_shared.py` — those functions call each other BY BARE NAME from within
`_shared.py`'s own module globals, and `_managed_ai_placement` /
`_dispatch_stack_task` are directly `monkeypatch.setattr(ps, ...)`-targeted by
tests (`ps` = the `routers.persistent_stacks` package, re-exporting via
`from ._shared import *`). Moving their bodies out of `_shared.py` would not
change whether such a patch is visible to the internal bare-name call (it
already resolves via `_shared.py`'s own `__globals__`, not the package's,
independent of where the callee is *defined* — verified: patching `ps` does
NOT intercept the internal call today, a pre-existing gap, out of this
migration's scope per SHARED RULES "do not alter stack lifecycle / wake /
placement logic semantics"), but it WOULD add a second place the object could
diverge from. Their `db.query` sites are still migrated in place (swapped for
calls into `crud.persistent_stacks_shared`) without relocating the functions
themselves. See MOD_CORE/CORE/API/routers/persistent_stacks/_shared.py for
those bodies.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from crud import persistent_stacks_shared as pss_crud


# =============================================================================
# _pick_worker
# =============================================================================

def pick_worker(db: Session, home_zone: Optional[str]) -> Optional[models.WorkerLicense]:
    if home_zone:
        zoned = pss_crud.get_licensed_worker_in_zone(db, home_zone)
        if zoned:
            return zoned
    return pss_crud.get_first_licensed_worker(db)


# =============================================================================
# _get_owned
# =============================================================================

def get_owned_stack(db: Session, stack_id: int, user: "models.User") -> models.PersistentStack:
    stack = pss_crud.get_owned_stack(db, stack_id, user.tenant_id)
    if not stack:
        raise HTTPException(status_code=404, detail="Persistent stack not found")
    return stack


# =============================================================================
# per-stack egress enforcement (paid layer)
# =============================================================================
# The decision of when a stack's egress posture is enforced lives in the paid
# `services.stack_egress_enforcement`, absent from the Community Edition tree
# (CE stacks keep their default network behaviour). When present it installs
# its decision helpers into this module's namespace.
from utils.paid_import import optional_paid as _optional_paid  # noqa: E402
_stack_egress_layer = _optional_paid("services.stack_egress_enforcement")
if _stack_egress_layer is not None:
    _stack_egress_layer.install_gate(globals())


# =============================================================================
# _unrunnable_reason_for
# =============================================================================

def unrunnable_reason_for(db: Session, stack) -> Optional[str]:
    from product_names import themed_logger
    logger = themed_logger("routers.persistent_stacks._shared", 'stacks')

    if stack is None:
        return None
    if getattr(stack, "status_reason", None):
        return stack.status_reason
    try:
        stuck = pss_crud.get_stuck_unrunnable_task(db, stack.id)
        return stuck.unrunnable_reason if stuck is not None else None
    except Exception as e:  # noqa: BLE001 — columns not migrated / transient
        logger.debug("unrunnable lookup failed for stack %s: %s", stack.id, e)
        return None


# =============================================================================
# _ingress_authorized_domain
# =============================================================================

def ingress_authorized_domain(db: Session, domain: str, *, is_good_standing,
                              active_statuses) -> bool:
    d = domain.strip().lower()
    if not d:
        return False

    # (1) opt-in public exposure — the source of truth for what may go public.
    exposed_rows = pss_crud.get_exposed_stacks(db)
    for stack in exposed_rows:
        if not getattr(stack, "exposed", False):
            continue
        if not is_good_standing(stack):
            continue
        names = {str(getattr(stack, "public_hostname", None) or "").strip().lower(),
                 str(getattr(stack, "custom_domain", None) or "").strip().lower(),
                 str(getattr(stack, "friendly_fqdn", None) or "").strip().lower()}
        names.discard("")
        if d in names:
            return True
    # (2) legacy per-service x-mod.tls 'auto' declaration.
    rows = pss_crud.get_stacks_with_tls_config(db)
    for stack in rows:
        conf = stack.tls_config if isinstance(stack.tls_config, dict) else {}
        for entry in conf.values():
            if (isinstance(entry, dict) and entry.get("mode") == "auto"
                    and str(entry.get("fqdn") or "").strip().lower() == d
                    and getattr(stack, "desired_state", None) == "running"
                    and stack.status in active_statuses):
                return True
    return False


# =============================================================================
# _load_tenant_tls_setting / effective_tenant_tls_mode
# =============================================================================

_TENANT_TLS_SETTING_KEY = "stack_tls"
_TENANT_TLS_MODES = ("letsencrypt", "byo")


def load_tenant_tls_setting(db: Session, tenant_id: int):
    row = pss_crud.get_tenant_setting(db, tenant_id, _TENANT_TLS_SETTING_KEY)
    val = row.value if row is not None and isinstance(row.value, dict) else {}
    return row, dict(val)


def effective_tenant_tls_mode(db: Session, tenant_id: int) -> str:
    _, val = load_tenant_tls_setting(db, tenant_id)
    mode = val.get("mode")
    return mode if mode in _TENANT_TLS_MODES else "letsencrypt"


# =============================================================================
# _idle_settings_row
# =============================================================================

def idle_settings_row(db: Session) -> models.PlatformLLMIdleSettings:
    """Fetch the singleton, creating it if the row is somehow missing."""
    row = pss_crud.get_idle_settings_by_id(db, 1)
    if row is None:
        row = models.PlatformLLMIdleSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# =============================================================================
# host_pin_status
# =============================================================================

def host_pin_status(db: Session, stack, *, aggregate_accelerator_demand) -> Optional[dict]:
    from product_names import themed_logger
    logger = themed_logger("routers.persistent_stacks._shared", 'stacks')

    pinned = getattr(stack, "pinned_worker", None)
    if not pinned:
        return None
    out = {"worker": pinned, "reason": stack.pinned_reason,
           "at": stack.pinned_at.isoformat() if stack.pinned_at else None,
           "satisfied": True, "detail": None}

    worker = pss_crud.get_worker_by_name(db, pinned)
    if worker is None:
        out["satisfied"] = False
        out["detail"] = (f"Pinned to {pinned}, which is no longer an enrolled "
                         f"worker. This stack will not be placed elsewhere — "
                         f"clear the pin or re-enrol that host.")
        return out

    demand = aggregate_accelerator_demand(stack.accelerator_requirements or {})
    if demand:
        try:
            from utils.accelerator_fit import worker_can_host, resolve_stack_demand
            # CANDIDATE demand for the named stack — resolve to measured serving
            # VRAM (behind FIT_GATE_MEASURED_VRAM; no-op when off / unprofiled).
            demand = resolve_stack_demand(db, stack.name, demand)
            if not worker_can_host(db, worker, demand, exclude_stack_id=stack.id):
                out["satisfied"] = False
                out["detail"] = (
                    f"Pinned to {pinned}, which cannot currently satisfy this "
                    f"stack's accelerator demand. It will NOT be moved to "
                    f"another worker — the pin is the constraint. Free "
                    f"capacity on {pinned}, or clear the pin.")
        except Exception:  # noqa: BLE001
            logger.debug("host-pin status check failed for stack %s", stack.id,
                         exc_info=True)
    return out


# =============================================================================
# report_host_port_and_subnet_usage — gw-stack-networking GW.1, read-only
# =============================================================================
# Existing stacks predate MOD owning host ports/subnets: their stored
# `compose_spec` may still publish a host port verbatim (`ports:
# ["8110:8000"]`, no `x-mod.ports`) or pin a network's `ipam.config` subnet —
# neither of which a NEW-format import (has `x-mod.ports`; see
# `services.stack_compose_import.normalize_compose`) can ever carry again.
# This makes NO row changes; it exists so an operator can see, before a
# re-import/redeploy, which stacks would collide. Queries `PersistentStack`
# directly rather than through `crud.persistent_stacks_shared` — this is new
# diagnostic code, not part of the Gate 2b extraction the module docstring
# describes, and a one-shot report over an admin-only table scan doesn't
# warrant a new crud helper for a single caller.
def report_host_port_and_subnet_usage(db: Session) -> list:
    rows = db.query(models.PersistentStack).all()
    out = []
    for stack in rows:
        spec = stack.compose_spec if isinstance(stack.compose_spec, dict) else {}
        services = spec.get("services") if isinstance(spec.get("services"), dict) else {}
        host_ports = []
        for svc_name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            for p in (svc.get("ports") or []):
                host_ports.append({"service": svc_name, "port": p})
        subnets = []
        networks = spec.get("networks") if isinstance(spec.get("networks"), dict) else {}
        for net_name, net_cfg in networks.items():
            if not isinstance(net_cfg, dict):
                continue
            ipam = net_cfg.get("ipam")
            if not isinstance(ipam, dict):
                continue
            for cfg in (ipam.get("config") or []):
                if isinstance(cfg, dict) and cfg.get("subnet"):
                    subnets.append({"network": net_name, "subnet": cfg["subnet"]})
        if not host_ports and not subnets:
            continue
        out.append({
            "id": stack.id,
            "name": stack.name,
            "services": sorted(services.keys()),
            "host_ports": host_ports,
            "subnets": subnets,
        })
    return out
