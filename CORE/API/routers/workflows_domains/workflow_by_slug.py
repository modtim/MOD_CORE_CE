"""30.6.9 (2400GX-c9-J3) — PUT|GET /v1/road/by-slug/{slug}.

The HTTP surface for the c9-J2 service `services/workflow_upsert.py`
(`upsert()` :166, `get_by_slug()` :76, `validate_slug()` :62). The service
commits ONCE and does NO authz; every permission decision lives HERE.

Mount: the aggregator `routers/workflows.py` includes this router BEFORE
`workflows.router`, so `/by-slug/{slug}` is matched before the
`/{workflow_id}` catch-all (design c9 vet "Routing (MUST)" :118-121).

Auth funnel: `require_bearer_or_api_key` (dependencies.py:1416), the same one
`routers/billing_credit_rate.py:35` uses. The tenant ALWAYS comes from the
principal (`caller.tenant_id`) — the body schema is extra="forbid" and has no
tenant field (schemas.py:861), and no query/header picks a tenant.

Bearer (a tenant user): the SAME permission `POST /workflows/import`
(routers/workflows_domains/workflows.py:4831-4836) and `POST /workflows/`
(routers/workflows_domains/node_authoring.py:35-41) require today —
`require_active_and_onboarded_user` (dependencies.py:504). There is NO
further capability check: `_create_imported_workflow`
(services/workflows_domains_shared_service.py:104) runs only the
differentiable-declaration SAVE gate (`_enforce_dp_gate`,
crud/workflow_cud.py:979 — a graph-shape 422, not an identity check), and
`create_workflow` adds only `require_tenant_writable` (utils/tenant_lifecycle,
a tenant-state 4xx, not a role). So the codebase has no "can create
workflows" role beyond active+onboarded; a user who cannot create today is
exactly one whose status is not ACTIVE or whose onboarding is incomplete, and
that dependency 403s here (T8). No new role is invented.

App key for App X (`api_key.app_id == X`, read from
`utils/request_principal.get_api_key()` — the contextvar set by
dependencies.py:1433 `_mark`, the same handle `routers/ephemeral_run_inputs.py:78`
and the stack-pinned key path (`api_keys.deploy_token_stack_id` set;
`check_stack_key_scope`, services/app_key_scope.py:666) read):
  (a) `req.add_to_app_id` must be set and equal X, else 403
      `app_key_scope_denied` (T2 cross-app; also when missing).
  (b) hijack guard (lead MUST, not in the design): if the slug already names a
      workflow in this tenant, that workflow's id must already be in X's
      `allowed_workflow_ids`, else 403 + NO write. A brand-new slug is allowed
      because the App creates it and the service adds it to its own scope.
  (c) the stack-pinned API key `workflows:apply` path is DEFERRED (design :113).
      Default deny already covers it: `check_stack_key_scope`
      (services/app_key_scope.py:666-748) runs inside
      `_enforce_api_key_policy` (dependencies.py:1589) BEFORE the handler and
      403s `deploy_token_scope_denied` with reason `route_not_allowed` (:725)
      because no stack-pinned-key rule matches this route. The router re-checks so
      the 403 holds even if that funnel is bypassed.

Allowlist kind is `none`, not the design's `own-app` (:112): `own-app`
compares an `{app_id}` PATH param (services/app_key_scope.py:40,
_scope_violation :337-340) and this route has no `{app_id}` segment, so the
App check is enforced HERE — (a) plus (b), which is strictly stronger than
own-app anyway.

Errors use the c14 shape (`utils/http_errors.ApiError`, lower snake_case).
No `slug_taken_by_tenant_other`, no cross-tenant oracle (design :92-93):
`get_by_slug` is filtered on (tenant_id, slug) only.

Audit: emitted ONLY by the service (WORKFLOW_UPSERT_CREATED /
WORKFLOW_UPSERT_VERSION_CREATED / WORKFLOW_APP_SCOPE_ADDED). The router adds
none. A 403 from (a)/(b) writes no audit row — the neighbouring app-scope
denials DO audit (services/app_key_scope.py:250 `_deny` ->
models.AuditAction.AUTH_APP_SCOPE_DENIED, throttled by
_AUDIT_COOLDOWN_SECONDS :109), so a router-level 403 is the one that stays
silent; the deploy-token 403 of (c) is audited by `_deny` itself.
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Request
from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from dependencies import get_db, require_bearer_or_api_key
from utils.http_errors import ApiError
from utils.request_principal import get_api_key
from services import workflow_upsert

from product_names import themed_logger
logger = themed_logger('routers.workflows_domains.workflow_by_slug', 'workflows')

router = APIRouter(prefix="/by-slug", tags=["Workflows - By Slug"])


def _authorising_key(db: Session, request) -> "models.ApiKey | None":
    """The ApiKey row that authorised this request, or None (Bearer session).

    Primary source is the contextvar `utils/request_principal.get_api_key()`
    (:157, set by `set_api_key` in dependencies.py:1355 and in
    `require_bearer_or_api_key._mark` :1433 — the handle
    routers/ephemeral_run_inputs.py:78 and the stack-pinned key path
    (`api_keys.deploy_token_stack_id` set; `check_stack_key_scope`,
    services/app_key_scope.py:666) read). It is not visible from a sync handler body that FastAPI runs in the
    threadpool, so the request-state marker the SAME funnel writes
    (dependencies.py:1360 `_api_key_policy_enforced_id`) is the fallback."""
    key = get_api_key()
    if key is not None:
        return key
    key_id = getattr(getattr(request, "state", None),
                     "_api_key_policy_enforced_id", None)
    if key_id is None:
        return None
    return (db.query(models.ApiKey)
            .filter(models.ApiKey.id == int(key_id)).first())


def _deny_app_scope(reason: str) -> None:
    """403 app_key_scope_denied — the code app_key_scope.py:252 `_deny` raises.

    `reason` is logged, never sent: the body must not tell a caller whether
    some other App's scope holds the slug."""
    logger.info("by-slug app-key denial: reason=%s app=%s",
                reason, getattr(get_api_key(), "app_id", None))
    raise ApiError(403, "app_key_scope_denied",
                   "This App's API key is not allowed to write this workflow.")


def _app_scope_workflow_ids(db: Session, app_id: int) -> list:
    """The owning App's `allowed_workflow_ids` (hijack guard, brief MUST (b))."""
    app = (db.query(models.App)
           .filter(models.App.id == int(app_id))
           .first())
    if app is None:
        return []
    return [int(v) for v in (app.allowed_workflow_ids or [])]


def require_writer(
        db: Session = Depends(get_db),
        caller: models.User = Depends(require_bearer_or_api_key),
) -> models.User:
    """Bearer-or-app-key, plus the WRITE permission a Bearer needs.

    A tenant user must pass the SAME gate creating a workflow requires today:
    `require_active_and_onboarded_user` (dependencies.py:504), which
    `POST /workflows/import` (routers/workflows_domains/workflows.py:4835) and
    `POST /workflows/` (routers/workflows_domains/node_authoring.py:40) both
    use. That dependency is just `require_active_user` (dependencies.py:483,
    403 "Inactive user") + `require_onboarding_complete` (dependencies.py:492,
    403 "Onboarding must be completed...") plus per-plan rate limiting, so
    calling the two legs on the funnel's principal is the same permission, not
    a new one. There is NO further capability check on the create path:
    `_create_imported_workflow` (services/workflows_domains_shared_service.py:104)
    only runs the differentiable-declaration SAVE gate (`_enforce_dp_gate`,
    crud/workflow_cud.py:979 — a graph-shape 422, not an identity check), and
    `create_workflow` adds only `require_tenant_writable`
    (utils/tenant_lifecycle — tenant STATE, not a role). So the codebase has no
    "can create workflows" role beyond active+onboarded, and a user who cannot
    create today is exactly one whose status is not ACTIVE or whose onboarding
    is incomplete: 403 here (T8).

    An App key is NOT run through it — the funnel already requires its owning
    user to be ACTIVE (dependencies.py:1453) and app keys are programmatic by
    design; the App branch is guarded by (a)/(b)/(c) below.
    """
    key = get_api_key()
    if key is None or getattr(key, "app_id", None) is None:
        from dependencies import require_active_user, require_onboarding_complete
        require_active_user(current_user=caller)
        require_onboarding_complete(current_user=caller)
    return caller


@router.put("/{slug}", response_model=schemas.WorkflowUpsertResult)
def upsert_workflow_by_slug(
        slug: str,
        req: schemas.WorkflowUpsertRequest,
        request: Request,
        db: Session = Depends(get_db),
        caller: models.User = Depends(require_writer),
):
    """Idempotent per-tenant UPSERT of the workflow named by `slug`."""
    key = _authorising_key(db, request)
    if key is not None and getattr(key, "deploy_token_stack_id", None) is not None:
        # (c) DEFERRED scope: default deny, same code as app_key_scope.py:530.
        raise ApiError(403, "deploy_token_scope_denied",
                       "This deploy token may not write workflows by slug.")

    tenant_id = caller.tenant_id
    workflow_upsert.validate_slug(slug)

    if key is not None and getattr(key, "app_id", None) is not None:
        # (a) the key's App must be exactly the App the body asks for.
        if req.add_to_app_id is None or int(req.add_to_app_id) != int(key.app_id):
            _deny_app_scope("add_to_app_id_not_own_app")
        # (b) hijack guard, BEFORE the service writes anything.
        existing = workflow_upsert.get_by_slug(db, tenant_id, slug)
        if existing is not None and int(existing.id) not in _app_scope_workflow_ids(
                db, key.app_id):
            _deny_app_scope("workflow_not_in_app_scope")

    result = workflow_upsert.upsert(
        db, user=caller, tenant_id=tenant_id, slug=slug, req=req)
    return result




@router.get("/{slug}", response_model=schemas.SlugLookupResponse)
def lookup_workflow_by_slug(
        slug: str,
        request: Request,
        db: Session = Depends(get_db),
        caller: models.User = Depends(require_bearer_or_api_key),
):
    """Resolve `slug` to the tenant's workflow row.

    404 `workflow_not_found` when the tenant has no such slug. An App key
    gets the SAME 404 for a workflow outside its `allowed_workflow_ids`, so
    the response never reveals whether the slug exists (R7: bodies are
    byte-identical). Archived rows ARE returned — the lookup is a lookup;
    the 409 `workflow_archived` guard belongs to the PUT (the vet's table is
    about writing to an archived slug: never resurrect, never twin).
    """
    key = _authorising_key(db, request)
    if key is not None and getattr(key, "deploy_token_stack_id", None) is not None:
        raise ApiError(403, "deploy_token_scope_denied",
                       "This deploy token may not look up workflows by slug.")

    tenant_id = caller.tenant_id
    workflow_upsert.validate_slug(slug)
    wf = workflow_upsert.get_by_slug(db, tenant_id, slug)

    if key is not None and getattr(key, "app_id", None) is not None:
        if wf is None or int(wf.id) not in _app_scope_workflow_ids(db, key.app_id):
            # Same error for "no such slug" and "not yours" — no existence leak.
            raise ApiError(404, "workflow_not_found",
                           "No workflow matches this slug.")

    if wf is None:
        raise ApiError(404, "workflow_not_found", "No workflow matches this slug.")

    return schemas.SlugLookupResponse(
        workflow_id=wf.id,
        slug=wf.slug,
        latest_version_id=wf.latest_version_id,
        graph_hash=workflow_upsert._latest_graph_hash(db, wf),
    )
