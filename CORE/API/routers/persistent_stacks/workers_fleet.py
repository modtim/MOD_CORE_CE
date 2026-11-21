from __future__ import annotations
from ._shared import *  # noqa: F401,F403
from routers.persistent_stacks import _require_platform_admin, _require_stack_author  # noqa: F401  (defined in __init__; keeps __module__ stable)

# Gate 2b: query-heavy handlers below delegate to crud/services (module-level
# import is safe here — neither module imports back into this package at
# import time; see services/persistent_stacks_workers_fleet_service.py docstring).
from crud import persistent_stacks_workers_fleet as _psw_crud  # noqa: F401
from services import persistent_stacks_workers_fleet_service as _psw_svc

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
router = APIRouter()

@router.post("", response_model=schemas.PersistentStack)
def create_stack(body: schemas.PersistentStackCreate,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    return _psw_svc.create_stack(db, body, user)

@router.post("/{stack_id}/acknowledge-host-access", response_model=schemas.PersistentStack,
             dependencies=[Depends(_require_platform_admin)])
def acknowledge_host_access(stack_id: int, body: _AcknowledgeHostAccessRequest,
                            db: Session = Depends(get_db),
                            user: models.User = Depends(get_current_user)):
    """delegated-stack-authoring §3.5 — the platform-admin acknowledgement.

    The ONLY path an admin-only compose construct (device passthrough, a
    seccomp override, `group_add`, ...) can reach a delegated (non-admin)
    author's stack. Explicit (names one service), per-service (every other
    service on the stack is unaffected), stored with the stack (written into
    that service's `x-mod.admin_ack` inside the STORED `compose_spec`, so it
    travels with the stack and is visible on read), and audited (below).

    Never overrides the §3.1/§3.2 hard refusals (privileged, cap_add, a host
    namespace, the Docker socket) — `_validate_service_security` checks those
    before is_admin/ack are even consulted, for every author, admin included.
    A non-admin author can never set this themselves: at create/redeploy time
    the validator is checking CALLER-supplied input and refuses the marker's
    mere presence outright (§3.7); it is honored only when re-validating
    already-STORED data, which only this admin-gated endpoint ever writes.
    """
    return _psw_svc.acknowledge_host_access(db, stack_id, body, user)

@router.get("", response_model=List[schemas.PersistentStack])
def list_stacks(db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    return _psw_svc.list_stacks(db, user)

@router.get("/templates")
def list_templates(user: models.User = Depends(get_current_user)):
    """The template catalog (id + metadata + params)."""
    return _psw_svc.list_templates()

@router.put("/rebalancer-config", dependencies=[Depends(_require_platform_admin)])
def set_rebalancer_config(body: _RebalancerThreshold,
                          db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """Set the rebalancer threshold and/or master enable (platform-admin only).

    Both fields are optional so the UI can toggle the enable without having to
    round-trip the threshold, and vice versa."""
    return _psw_svc.set_rebalancer_config(db, body, user)

__all__ = [
    'create_stack',
    'acknowledge_host_access',
    'list_stacks',
    'list_templates',
    'set_rebalancer_config',
]
