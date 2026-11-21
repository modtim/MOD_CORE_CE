from __future__ import annotations
"""persistent_stacks.py — thin per-domain aggregator (behavior-preserving split).

The endpoints moved into per-domain sub-modules (identity_tenancy, llm_governance, persistent_stacks, task_queue_scheduler, workers_fleet);
this package re-exports the same top-level router objects, includes each
sub-router into them (identical prefix/tags/deps), and re-exports every
symbol the pre-split module exposed so external importers keep working.
"""
from ._shared import *  # noqa: F401,F403  (re-export helpers/models/constants)
# 2400GX-c3 J2b — the key-aware variant of the router-wide stack:author gate
# (the bearer-only `require_platform_capability` still comes from _shared).
from dependencies import require_stack_author_capability_or_deploy_key

def _require_platform_admin(current_user: models.User = Depends(get_current_user)) -> models.User:
    """Platform-admin-only gate, re-applied per-endpoint to the placement /
    managed-AI / rebalancer / reconciler / observation routes below so a
    delegated author (holding STACK_AUTHOR but not is_platform_admin) reaches
    only their own service lifecycle, never platform controls
    (delegated-stack-authoring §2.3)."""
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin required")
    return current_user


def _require_stack_author(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_stack_author_key),
) -> models.User:
    if not permissions.check_user_platform_capability(db, user.id, permissions.STACK_AUTHOR):
        raise HTTPException(
            status_code=403,
            detail="User does not hold the required 'stack:author' platform capability.",
        )
    return user


def _require_paid(module_name: str, feature: str):
    """GT.D5/DS-2 — the CE gate for a wholly-Paid endpoint.

    Delegates to `utils.paid_import.require_paid`: when the module the
    endpoint's logic lives in is absent from the CE tree, that raises
    HTTP 402 ("Pro feature: …") so the client sees the upgrade path instead
    of an ImportError 500. The module is returned so a call site that only
    needs the module (not the whole-endpoint 402) can use it; an endpoint
    that is wholly Paid ignores the value — its CE body IS the guard.

    Defined here (not in `_shared.py`) so the per-domain sub-routers can
    import it the same way they import `_require_platform_admin`.
    """
    from utils.paid_import import require_paid
    return require_paid(module_name, feature)


# The prefixes the pre-split module baked into each router's constructor are
# applied here on include_router instead, so the sub-routers stay prefix-free
# and empty-path routes (e.g. POST "") still resolve to the same final path
# (FastAPI rejects an empty include-prefix AND an empty route path together).
# main.py includes these three routers at prefix "", so the final paths are
# identical to the pre-split module.
# 2400GX-c3 J2b (30.6.3) — key-aware variant of the router-wide stack:author
# gate: the bearer leg runs the SAME chain (active + onboarding + rate limits +
# the same capability verdict), and ONLY a stack-pinned API key (an ordinary
# ApiKey row with deploy_token_stack_id set + stack:* capability_scope, own-stack
# + per-scope confined by services/app_key_scope.check_stack_key_scope over
# STACK_SCOPE_ALLOWLIST) on one of the SIX operational routes (GET /{stack_id},
# deploy, revert, logs, backup, backups — services/app_key_scope.stack_key_route_rules)
# may answer it. Every other key/route fails closed with the bearer 401 — see
# dependencies.py:require_stack_author_capability_or_deploy_key.
# NOTE: the seed routes below are in STACK_SCOPE_ALLOWLIST (stack:seed) but NOT
# in those six, so a seed-only key still gets the gate's bearer 401 (finding,
# not widened) — see dependencies.py:require_stack_author_capability_or_deploy_key.
router = APIRouter(tags=["Persistent Stacks"],
                   dependencies=[Depends(
                       require_stack_author_capability_or_deploy_key(
                           permissions.STACK_AUTHOR))])
mesh_internal_router = APIRouter(tags=["Persistent Stacks (internal)"])
ingress_router = APIRouter(tags=["Persistent Stacks (ingress)"])
stack_router = APIRouter(tags=["Stack (MCP)"])

from . import (
    workers_fleet as _d_workers_fleet,
    task_queue_scheduler as _d_task_queue_scheduler,
    identity_tenancy as _d_identity_tenancy,
    stack_variables as _d_stack_variables,
    stack_seed as _d_stack_seed,
    persistent_stacks as _d_persistent_stacks,
    llm_governance as _d_llm_governance,
)

# 1634c (CE boot) — routers/persistent_stacks/bench_profile.py is manifest-denied
# (its logic lives in the Paid services/stack_bench_profile.py), so importing it
# module-level crashes a CE build at startup. Guard it: on CE the two bench
# routes are simply never mounted (404 = feature off), which is the same
# feature-off path every other Paid stack route takes.
try:
    from . import bench_profile as _d_bench_profile
except (ImportError, ModuleNotFoundError):
    _d_bench_profile = None

# include order preserves original route-registration order so the bare
# GET /stacks/{stack_id} still comes after the single-segment
# static routes (templates, accelerator-capacity, managed-ai, ...).
# clean-v2-api (infra wave): renamed /persistent-stacks -> /stacks (design.md
# "Infrastructure" resource model). Behavior/auth/services untouched — only
# the mount prefix changed.
router.include_router(_d_workers_fleet.router, prefix="/stacks")
router.include_router(_d_task_queue_scheduler.router, prefix="/stacks")
router.include_router(_d_identity_tenancy.router, prefix="/stacks")
# GX.S10a — the variables + secrets routes. Included BEFORE the
# `persistent_stacks` sub-router so its single-segment static routes
# (`/stacks/compose/variables/detect`, `/stacks/secrets/catalog`) are not
# shadowed by `/stacks/{stack_id}` (FastAPI matches in include order).
router.include_router(_d_stack_variables.router, prefix="/stacks")
# 2400GX-c1 J1 — the external-seed routes. Included BEFORE the
# `persistent_stacks` sub-router so `/{stack_id}/seed...` tails are not
# shadowed by anything that sub-router registers at the same depth.
router.include_router(_d_stack_seed.router, prefix="/stacks")
# 27.6.s3 — the admin bench trigger + profile read-back. Included BEFORE the
# `persistent_stacks` sub-router so its `/{stack_id}/bench(-profile)` tails are
# matched ahead of anything that sub-router registers at the same depth
# (FastAPI matches in include order).
if _d_bench_profile is not None:
    router.include_router(_d_bench_profile.router, prefix="/stacks")
router.include_router(_d_persistent_stacks.router, prefix="/stacks")
mesh_internal_router.include_router(_d_persistent_stacks.mesh_internal_router, prefix="/stacks/internal")
ingress_router.include_router(_d_persistent_stacks.ingress_router, prefix="/stacks")
stack_router.include_router(_d_persistent_stacks.stack_router, prefix="/stack")
stack_router.include_router(_d_llm_governance.stack_router, prefix="/stack")

from .workers_fleet import *  # noqa: F401,F403
from .task_queue_scheduler import *  # noqa: F401,F403
from .identity_tenancy import *  # noqa: F401,F403
from .stack_variables import *  # noqa: F401,F403
try:
    from .bench_profile import *  # noqa: F401,F403
except (ImportError, ModuleNotFoundError):
    pass  # 1634c: CE has no bench sub-router, so there is nothing to re-export
from .persistent_stacks import *  # noqa: F401,F403
from .llm_governance import *  # noqa: F401,F403
