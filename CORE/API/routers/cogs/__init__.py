"""cogs.py — thin per-domain aggregator (behavior-preserving split).

The endpoints moved into per-domain sub-modules (assets_cogs_files, task_queue_scheduler, usage_metering, workflows);
this package re-exports the same top-level router objects, includes each
sub-router into them (identical prefix/tags/deps), and re-exports every
symbol the pre-split module exposed so external importers keep working.
"""
from ._shared import *  # noqa: F401,F403  (re-export helpers/models/constants)

router = APIRouter(
    tags=["Cogs"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)
service_router = APIRouter(
    tags=["Cogs - Service"],
    responses={404: {"description": "Not found"}},
)
public_router = APIRouter(
    tags=["Cogs - Public"],
    responses={404: {"description": "Not found"}},
)

from . import (
    assets_cogs_files as _d_assets_cogs_files,
    workflows as _d_workflows,
    task_queue_scheduler as _d_task_queue_scheduler,
)

# ---------------------------------------------------------------------------
# GY.V2a (GSEC R-040) — data-classification write gate.
#
# The PATCH /{cog_id}/classification handler (update_classification, in
# assets_cogs_files.py) is tenant-writable today. Unknown labels are
# rejected 422 by the pydantic validator on schemas.CogClassificationUpdate
# (services/data_classification.normalize); the ADMIN gate — CUI/EAR/ITAR
# set/lower requires a tenant admin (parent user) or platform admin — is
# attached HERE as an extra dependency on that single route so the handler
# file itself stays untouched (owned by another workstream).
#
# Ordering matters: include_router() COPIES each source route's
# `route.dependencies` at include time, so the gate must be appended to the
# sub-router's route BEFORE the include below.
# ---------------------------------------------------------------------------

async def _classification_admin_gate(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
    request: Request = None,
):
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    if _dc is None:
        # 1788 CE carve: the taxonomy is paid and absent; schemas.py's label
        # validator already 402s any classification change in CE, so there is
        # no CUI/EAR/ITAR admin gate to enforce here.
        return

    # Request-scoped deps run before the endpoint's body params, so the
    # body is not yet validated — read it from the (cached) request. MUST be
    # async: in a sync dependency `request.body()` is a coroutine, not bytes.
    requested = None
    if request is not None:
        try:
            import json
            body = await request.body()
            if body:
                requested = json.loads(body).get("data_classification")
        except Exception:
            requested = None
    if not requested:
        return  # body validator will 422 an unknown/missing label first

    old_level = None
    cog_id = None
    if request is not None:
        try:
            # starlette 0.35 exposes request.url.path (no .url_path attr).
            path = request.url.path.rstrip("/")
            cog_id = int(path.split("/")[-2])
            _cog = crud.get_cog_by_id_and_tenant(
                db, cog_id=cog_id,
                tenant_id=getattr(current_user, "tenant_id", None),
            )
            old_level = getattr(_cog, "data_classification", None)
        except Exception:
            old_level = None

    verdict = _dc.evaluate_write(current_user, old_level, requested)
    if verdict == "unknown-label":
        return  # body validator 422s first — don't audit a request that 422s
    try:
        canonical = _dc.normalize(requested)
    except Exception:
        canonical = requested
    details = {
        "cog_id": cog_id,
        "current": old_level,
        "requested": canonical,
        "actor_user_id": getattr(current_user, "id", None),
        "actor_username": getattr(current_user, "username", None),
    }
    if verdict == "admin-required":
        _dc.record_audit(
            db,
            tenant_id=getattr(current_user, "tenant_id", None),
            user=current_user,
            event_name="DATA_CLASSIFICATION_SET_DENIED",
            details=details,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Changing CUI/EAR/ITAR classification requires a tenant "
                   "admin or platform admin",
        )
    _dc.record_audit(
        db,
        tenant_id=getattr(current_user, "tenant_id", None),
        user=current_user,
        event_name="DATA_CLASSIFICATION_SET_REQUESTED",
        details=details,
    )


def _install_classification_gates(r) -> None:
    """Attach the admin gate to the classification route on sub-router `r`
    (no-op if the route is absent)."""
    try:
        for route in r.routes:
            if getattr(route, "path", "") == "/{cog_id}/classification" and \
               "PATCH" in (getattr(route, "methods", None) or set()):
                route.dependencies.append(Depends(_classification_admin_gate))
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "GY.V2a: failed to attach classification admin gate")


_install_classification_gates(_d_assets_cogs_files.router)

# ---------------------------------------------------------------------------
# GY.V2b (GSEC R-040/R-133) — US-person read/download gate on
# export-controlled assets.
#
# When compliance.export_control_enforced is on, a user who is NOT flagged
# as a US person cannot read or download cogs classified EAR/ITAR (CUI
# joins the gated set when compliance.cui_requires_us_person is on). The
# decision is centralized in
# services.data_classification.can_access_classification; a denial is 403
# + an EXPORT_CONTROL_ACCESS_DENIED audit row. Same attach pattern as the
# GY.V2a write gate: the dependency is appended to the sub-router's route
# BEFORE include_router (include_router copies route.dependencies).
# ---------------------------------------------------------------------------

_EXPORT_CONTROL_READ_ROUTES = {
    "/{cog_id:int}",
    "/{cog_id}/download",
    "/{cog_id}/stream",
    "/{cog_id}/stream/{filename:path}",
    "/{cog_id}/download-folder",
}
_EXPORT_CONTROL_MULTI_ROUTE = "/download-multiple"


async def _export_control_access_gate(
    request: Request = None,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    if _dc is None:
        # 1788 CE carve: export-control access rules live in the paid taxonomy;
        # CE ships no export-control policy, so the gate is a no-op.
        return

    cog_ids = []
    if request is not None:
        cid = request.path_params.get("cog_id")
        if cid:
            try:
                cog_ids.append(int(cid))
            except (TypeError, ValueError):
                cog_ids = []
        if not cog_ids:
            # POST /download-multiple declares `cog_ids: List[int]` as a
            # repeated QUERY param (FastAPI binds non-Pydantic list params
            # from the query string, not a JSON body).
            try:
                cog_ids = [int(x) for x in request.query_params.getlist("cog_ids")]
            except (TypeError, ValueError):
                cog_ids = []
    if not cog_ids:
        return
    levels = [
        row[0] for row in db.query(models.Cog.data_classification)
        .filter(models.Cog.id.in_(cog_ids)).all()
    ]
    for lvl in levels:
        allowed, reason = _dc.can_access_classification(current_user, lvl, db)
        if not allowed:
            _dc.record_audit(
                db,
                tenant_id=getattr(current_user, "tenant_id", None),
                user=current_user,
                event_name="EXPORT_CONTROL_ACCESS_DENIED",
                details={
                    "cog_ids": cog_ids,
                    "classification": lvl,
                    "reason": reason,
                    "path": getattr(request, "url", None) and request.url.path,
                    "actor_user_id": getattr(current_user, "id", None),
                    "actor_username": getattr(current_user, "username", None),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=reason,
            )


def _install_export_control_gates(r) -> None:
    """Attach the US-person read/download gate to the single-cog GET and
    download routes on sub-router `r` (no-op if the routes are absent)."""
    try:
        for route in r.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            if (path in _EXPORT_CONTROL_READ_ROUTES and "GET" in methods) or \
               (path == _EXPORT_CONTROL_MULTI_ROUTE and "POST" in methods):
                route.dependencies.append(Depends(_export_control_access_gate))
    except Exception:
        logging.getLogger(__name__).exception(
            "GY.V2b: failed to attach export-control read gate")


_install_export_control_gates(_d_assets_cogs_files.router)

# usage_metering is a Paid (credit-metering) sub-module. It ships in the full
# build and is absent from the CE build; import + mount it only if present so
# the cogs package still boots in CE. No-op when the module IS present.
try:
    from . import usage_metering as _d_usage_metering
except ImportError:  # CE build: Paid sub-module absent
    _d_usage_metering = None

router.include_router(_d_assets_cogs_files.router)
router.include_router(_d_workflows.router)
if _d_usage_metering is not None:
    router.include_router(_d_usage_metering.router)
service_router.include_router(_d_assets_cogs_files.service_router)
service_router.include_router(_d_workflows.service_router)
public_router.include_router(_d_task_queue_scheduler.public_router)
# FX.102 — public share-link visit/download surface (token is the
# credential; the tenant rides on the shareable_links row, never the
# request). Same /v1/assets prefix as the guarded router; the path
# shapes (/shareable-links/{token}…) do not collide with the guarded
# /{cog_id}/shareable-links routes.
public_router.include_router(_d_assets_cogs_files.public_router)

from .assets_cogs_files import *  # noqa: F401,F403
from .workflows import *  # noqa: F401,F403
if _d_usage_metering is not None:
    from .usage_metering import *  # noqa: F401,F403
from .task_queue_scheduler import *  # noqa: F401,F403
