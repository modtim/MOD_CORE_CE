"""GX.A2 — Apps built on MOD: one hostname + OIDC client + API key each.

POST /v1/apps is tenant-admin only (an app is a long-lived identity with its
own blast radius). Provisioning order (§2.3): DB row (status 'provisioning')
→ app service user → KC client → key → proxy route. Any failure after the DB
row leaves the app in `provisioning_failed` with the failed step recorded;
"retry provisioning" is idempotent at every step.

GX.S1 — GET /v1/apps/auth/verify is the Caddy `forward_auth` target (see
services/app_forward_auth.py for the design) and PUT /v1/apps/{id}/auth-config
is the tenant-admin config surface for it.

ISO 27001: revoke SOFT-deletes the app (status 'revoked', deleted_at), the
app service user, and the API key — nothing is hard-deleted; the KC OIDC
client is disabled (not deleted).
"""
from __future__ import annotations

import html
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from urllib.parse import quote

import models
from dependencies import get_db, require_capability
from security import hash_api_key
from crud import revoke_api_key as _crud_revoke_api_key
from keycloak_auth import (
    keycloak_admin_client,
    get_current_user_claims,
    KEYCLOAK_AUDIENCE,
)
from services import app_hostname, app_routing
from services import app_forward_auth
SessionHmacError = app_forward_auth.SessionHmacError
from services import stack_forward_auth
from services.app_service_user import (
    ensure_app_service_user,
    revoke_app_service_user,
)
from utils.encryption import encrypt_value, ENCRYPTION_KEY
from utils.http_errors import ApiError, internal_http_error

logger = logging.getLogger(__name__)

# prefix="/apps" + main.py mounts under /v1 → /v1/apps (avoids a bare "/"
# colliding with other routers' collection routes).
router = APIRouter(prefix="/apps", tags=["Apps"])

# §7.7 — tenant admin only (capability grant). The alias carries a "require_"
# hint so the API-lint auth heuristic recognises it.
_require_tenant_admin = require_capability(models.ResourceType.TENANT, "manage_settings")


def _register_settings() -> None:
    """30.6.13 c13-J2 — the auto-redeploy tunable lives in the platform-settings
    registry (never a new env var). Idempotent: register() no-ops on re-import."""
    from services import platform_settings
    platform_settings.register(
        "apps.update_target_auto_redeploy",
        bool,
        True,
        group="apps",
        description="When an app's route target moves to a stack whose recorded "
                    "identity claim no longer matches the live targeting app, "
                    "queue a no-change redeploy so the stack gets a re-minted "
                    "identity token. Off = report `redeploy_required` and leave "
                    "the push to the operator.",
    )


_register_settings()


# ---------------------------------------------------------------------------
# request / response schemas (this router is the only consumer)
# ---------------------------------------------------------------------------

class AppCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=63)
    name: Optional[str] = None
    target_kind: str = Field("stack-service", pattern="^(stack-service|external-url)$")
    stack_id: Optional[int] = None
    stack_service: Optional[str] = None
    stack_port: Optional[int] = Field(None, ge=1, le=65535)
    external_url: Optional[str] = None
    allowed_workflow_ids: List[int] = Field(default_factory=list)
    allowed_folder_ids: List[int] = Field(default_factory=list)


class AppUpdateTarget(BaseModel):
    target_kind: str = Field(..., pattern="^(stack-service|external-url)$")
    stack_id: Optional[int] = None
    stack_service: Optional[str] = None
    stack_port: Optional[int] = Field(None, ge=1, le=65535)
    external_url: Optional[str] = None


class AppUpdateScope(BaseModel):
    allowed_workflow_ids: List[int] = Field(default_factory=list)
    allowed_folder_ids: List[int] = Field(default_factory=list)


class AppOut(BaseModel):
    """App identity: plain `X-Mod-*` headers are untrusted.
    Trust only the RS256-signed `X-Mod-Assertion` verified against the
    JWKS at `GET /v1/.well-known/jwks.json` (see docs:
    `## Trusting the caller's identity (X-Mod-Assertion)`).
    """
    id: int
    slug: Optional[str] = None
    hostname: Optional[str] = None
    name: Optional[str] = None
    status: str
    failure_detail: Optional[dict] = None
    target_kind: Optional[str] = None
    stack_id: Optional[int] = None
    stack_service: Optional[str] = None
    stack_port: Optional[int] = None
    external_url: Optional[str] = None
    edge_upstream: Optional[str] = None
    # 2400GX-c2-J2 (30.6.2): optional so every other _app_public caller / AppOut
    # consumer is unaffected when they are absent.
    auth_config: Optional[dict] = None
    edge_ready: Optional[bool] = None
    # 2400GX-c13-J2 (30.6.13): the stack-identity status of this app's target.
    # Optional so every other _app_public caller / AppOut consumer is unaffected
    # when it is absent. NEVER carries token bytes — only {"status", "app_id"}.
    identity: Optional[dict] = None
    allowed_workflow_ids: List[int] = Field(default_factory=list)
    allowed_folder_ids: List[int] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class AppCreatedOut(AppOut):
    key_prefix: Optional[str] = None
    access_key: Optional[str] = None


class AppKeyOut(BaseModel):
    id: int
    key_prefix: Optional[str] = None
    access_key: Optional[str] = None
    created_at: Optional[datetime] = None


class AppRevokeOut(BaseModel):
    status: str
    id: int


class AppVerifyOut(BaseModel):
    """Body of GET /apps/auth/verify (the Caddy forward_auth target). The body
    is mostly informational — Caddy acts on the STATUS (2xx = allow) and copies
    the X-Mod-* response headers onto the proxied request. See
    services/app_forward_auth.py for the design."""
    authenticated: bool
    public: bool = False
    login_url: Optional[str] = None


class AppAuthConfigUpdate(BaseModel):
    """PUT /apps/{id}/auth-config (GX.S1). require_login=false ⇒ the app is
    fully public (no forward_auth rendered). allowed_groups empty = any
    authenticated user. public_paths are path prefixes that skip forward_auth
    (e.g. OAuth callback, health)."""
    require_login: bool = False
    allowed_groups: List[str] = Field(default_factory=list)
    public_paths: List[str] = Field(default_factory=list)
    # 2400GX-c2-J2 (30.6.2): None/omitted ⇒ derived from require_login
    # (services/app_forward_auth._fail_closed_for). Deliberately Optional[Any]:
    # a non-bool ("true", 1) must reach validate_config so it 400s with the
    # same controlled detail string as the other keys' bad values (S7 strict
    # bool), instead of a pydantic 422 field-error body.
    fail_closed: Optional[Any] = None


# ---------------------------------------------------------------------------
# data access (routed through the services layer — no raw db.query in the
# router, per the R5 convention; session is named db_session throughout)
# ---------------------------------------------------------------------------

def _get_app(db_session: Session, app_id: int, tenant_id: int) -> models.App:
    """Tenant-scoped fetch — a foreign id 404s (no existence leak, §7.4)."""
    app = (
        db_session.query(models.App)
        .filter(models.App.id == app_id, models.App.tenant_id == tenant_id)
        .first()
    )
    if app is None or app.deleted_at is not None:
        raise HTTPException(status_code=404, detail="App not found")
    return app


def _list_apps(db_session: Session, tenant_id: int) -> List[models.App]:
    return (
        db_session.query(models.App)
        .filter(models.App.tenant_id == tenant_id,
                models.App.deleted_at.is_(None))
        .order_by(models.App.id)
        .all()
    )


def _app_conflicts(db_session: Session, tenant_id: int, slug: str,
                   hostname: str) -> Optional[str]:
    """Return the conflicting slug/hostname, or None when both are free."""
    row = (
        db_session.query(models.App.slug)
        .filter(models.App.tenant_id == tenant_id, models.App.slug == slug,
                models.App.deleted_at.is_(None))
        .first()
    )
    if row is not None:
        return f"slug {slug!r} already exists"
    row = (
        db_session.query(models.App.hostname)
        .filter(models.App.hostname == hostname,
                models.App.deleted_at.is_(None))
        .first()
    )
    if row is not None:
        return f"hostname {hostname!r} already taken"
    return None


def _live_app_count(db_session: Session, tenant_id: int) -> int:
    return (
        db_session.query(models.App)
        .filter(models.App.tenant_id == tenant_id,
                models.App.deleted_at.is_(None))
        .count()
    )


def _get_stack(db_session: Session, stack_id: int, tenant_id: int):
    return (
        db_session.query(models.PersistentStack)
        .filter(models.PersistentStack.id == stack_id,
                models.PersistentStack.tenant_id == tenant_id)
        .first()
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _audit(db_session: Session, app: models.App, user_id: Optional[int],
           event_name: str, details: dict) -> None:
    try:
        db_session.add(models.AuditEvent(
            tenant_id=app.tenant_id,
            user_id=user_id,
            event_name=event_name,
            details=details,
        ))
        db_session.commit()
    except Exception:  # noqa: BLE001
        pass


def _check_max_apps(db_session: Session, tenant_id: int) -> None:
    limit = app_routing.max_apps_per_tenant(db=None)
    if limit and limit > 0 and _live_app_count(db_session, tenant_id) >= limit:
        raise HTTPException(status_code=409, detail=f"tenant app limit reached ({limit})")


def _validate_target(db_session: Session, tenant_id: int, payload) -> None:
    """Target-kind field validation (stack ownership / external-url fence).

    external-url validation performs blocking DNS — callers commit any pending
    DB work BEFORE calling this so the fence runs outside an open transaction
    (the "no external fetch inside a DB transaction" rule).
    """
    if payload.target_kind == "stack-service":
        if payload.stack_id is None or not payload.stack_service or not payload.stack_port:
            raise HTTPException(
                status_code=400,
                detail="stack-service target requires stack_id, stack_service and stack_port",
            )
        if _get_stack(db_session, payload.stack_id, tenant_id) is None:
            raise HTTPException(status_code=404, detail="Stack not found")
    else:  # external-url
        if not app_routing.external_targets_enabled(db=None):
            raise HTTPException(
                status_code=403,
                detail="external-url targets are disabled on this deployment "
                       "(platform setting app.external_targets.enabled)",
            )
        if not payload.external_url:
            raise HTTPException(status_code=400, detail="external_url is required")
        app_routing.validate_external_url(payload.external_url)


def _update_target_identity(db_session: Session, app: models.App,
                            current_user: models.User) -> dict:
    """30.6.13 c13-J2 — the stack-identity status of an app's NEW target.

    Returns exactly ``{"status": ..., "app_id": ...}`` and NEVER anything else:
    no token bytes, no ``content_b64``, no JWT, no reason strings (a reason
    would name internal hosts/capabilities to a caller who only asked to move a
    route). ``status`` is one of:

      * ``"none"`` — external-url target (no stack identity involved);
      * ``"pushed"`` — stack target whose recorded identity claim already
        matches the live targeting app (nothing to re-mint), or a no-change
        redeploy queued and re-minted it;
      * ``"redeploy_required"`` — stack target whose claim is stale and could
        NOT be re-minted (no licensed worker, pinned host down, compose
        refused, or the stack is not running). The target change itself still
        succeeded (Q2 = A: the identity push is best-effort, it must never
        500 the update-target the operator asked for).

    The claim is NEVER read from the request body (S5): it comes from
    ``stack.storage_refs["identity"]`` + ``_active_targeting_app`` only.
    ``apps.update_target_auto_redeploy`` (platform-settings registry, default
    true) gates the dispatch; when off, a stale claim reports
    ``redeploy_required`` without touching anything.
    """
    from services import stack_identity
    if app.target_kind != "stack-service":
        return {"status": "none", "app_id": None}
    stack = _get_stack(db_session, app.stack_id, app.tenant_id)
    if stack is None:  # _validate_target already 404s; belt-and-braces
        return {"status": "redeploy_required", "app_id": None}

    state = stack_identity.identity_state(db_session, stack)
    recorded, current = state["app_id"], state["current_app_id"]
    if recorded is not None and recorded == current:
        return {"status": "pushed", "app_id": current}

    from services.platform_settings import get_setting
    auto = bool(get_setting("apps.update_target_auto_redeploy", db=db_session))
    if not auto:
        return {"status": "redeploy_required", "app_id": current}

    # 30.6.13 c13-J2b — the line's own ask: RE-MINT AND PUSH the identity
    # secret (`mint_identity_secret`), not merely queue a redeploy. c8-A2's
    # push_rotated_identity does exactly that in place: one SAVEPOINT holds
    # mint_identity_secret + the refresh_secret_files dispatch, so a failed
    # push unwinds the re-mint and storage_refs never claims an expiry the
    # container did not get. V8 keeps the OLD token valid to its own exp —
    # the re-target itself is what kills it (BUG-1027f: resolve_identity_claims
    # 401s an app-scoped token whose app no longer targets that stack), so the
    # stale secret has no fallback on the routes the App needs.
    # Q2 = A: push_rotated_identity documents "NEVER raises", but a raise must
    # still not 500 the update-target. Catch it (exception TYPE name only — the
    # repr can carry the mint/refresh payload), log the stack id, and take the
    # same fallback as a push that could not apply.
    try:
        push = stack_identity.push_rotated_identity(db_session, stack)
    except Exception as exc:  # noqa: BLE001 — best-effort push (Q2 = A)
        logger.warning("apps: update-target identity push raised for stack=%s: %s",
                       stack.id, type(exc).__name__)
        push = {"status": "redeploy_required", "reason": "push_raised"}
    # The push only ever answers "pushed" | "redeploy_required" | "skipped"
    # (never "unsupported"), so anything that is not "pushed" falls back.
    if push.get("status") == "pushed":
        return {"status": "pushed", "app_id": current}

    # Q1 = A: the deploy is attributed to the tenant admin who moved the
    # target — they are the actor causing it; APP_ROUTE_TARGET_UPDATED +
    # APP_TARGET_REMINTED together carry the trail.
    # Q2 = A: a failed re-mint must not 500 the target update. Catch the
    # exception, keep the response shape, leak nothing from it.
    # The in-place push could not run (legacy file-bind form, a worker without
    # secret_refresh, stack not running, or the lock held elsewhere) — fall
    # back to c3-J4's no-change redeploy, which re-mints at deploy time.
    from services.persistent_stacks_domain_service import deploy_stack
    try:
        deploy_stack(db_session, stack.id, None, current_user)
    except Exception as exc:  # noqa: BLE001 — best-effort push (Q2 = A)
        logger.warning("apps: update-target re-mint failed (app=%s stack=%s): %s",
                       app.id, stack.id, type(exc).__name__)
        return {"status": "redeploy_required", "app_id": current}
    return {"status": "pushed", "app_id": current}


def _ce_resolve_app_key_expiry(expires_at=None) -> Optional[datetime]:
    """1788g CE fallback for credential_lifetime.resolve_expiry_from_datetime.

    Same shape as the paid resolver minus the gov-profile cap (CE ships no
    gov profiles): None -> credentials.default_lifetime_days (90); a past
    value is refused; over credentials.max_lifetime_days is refused with the
    same 422 CREDENTIAL_LIFETIME_TOO_LONG code. services/platform_settings.py
    is api_allow (shipped in CE), so the tunables still come from the
    settings registry."""
    from datetime import timedelta
    from services.platform_settings import get_setting
    now = datetime.now(timezone.utc)
    if expires_at is None:
        default_days = int(get_setting("credentials.default_lifetime_days", db=None)
                           or 90)
        return now + timedelta(days=default_days)
    tz = timezone.utc
    aware = expires_at if expires_at.tzinfo is not None \
        else expires_at.replace(tzinfo=tz)
    if aware <= now:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "CREDENTIAL_LIFETIME_TOO_LONG",
                    "message": "Credential expiry must be in the future."})
    max_days = int(get_setting("credentials.max_lifetime_days", db=None) or 365)
    if aware > now + timedelta(days=max_days):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "CREDENTIAL_LIFETIME_TOO_LONG",
                    "message": (f"Requested credential expiry {aware.isoformat()} "
                                f"exceeds the maximum lifetime of {max_days} days")})
    return aware


def _mint_app_key(db_session: Session, app: models.App, user_id: int,
                  expires_at=None):
    """Mint the app's API key reusing routers/api_keys.py machinery
    (secrets.token_urlsafe + hash_api_key + KC service account) and bind it to
    the app (api_keys.app_id). The key's user is the app's service user, NOT
    the creating admin. GSEC FX.56 p1b (Q39=A): the key's lifetime is
    resolved through the single credential-lifetime resolver — a missing
    expiry takes credentials.default_lifetime_days (90), over-max is refused
    (422 CREDENTIAL_LIFETIME_TOO_LONG), gov profiles cap the max at 90.
    Returns (row, plaintext).

    1788g CE carve: services/credential_lifetime.py is paid/denied (manifest
    deny :986), so the resolver is fetched through utils/paid_import.optional_paid
    and CE falls back to the inline local equivalent below (same default 90d,
    same 422 CREDENTIAL_LIFETIME_TOO_LONG shape, no gov-profile cap since CE
    ships no gov profiles)."""
    from utils.paid_import import optional_paid
    _cred_lifetime = optional_paid("services.credential_lifetime")
    if _cred_lifetime is not None:
        expires_at = _cred_lifetime.resolve_expiry_from_datetime(
            db_session, app.tenant_id, expires_at)
    else:
        expires_at = _ce_resolve_app_key_expiry(expires_at)
    new_key_value = f"sk_{secrets.token_urlsafe(32)}"
    key_prefix = new_key_value[:8]

    kc_client_id = f"mod-app-key-{app.tenant_id}-{app.slug}"
    kc_result = keycloak_admin_client.create_service_account_client(
        client_id=kc_client_id,
        description=f"App key for {app.slug}",
        user_id=user_id,
        tenant_id=app.tenant_id,
    )

    db_key = models.ApiKey()
    db_key.user_id = user_id
    db_key.key_prefix = key_prefix
    db_key.hashed_key = hash_api_key(new_key_value)
    db_key.description = f"App key for {app.name or app.slug}"
    db_key.keycloak_client_id = kc_result["keycloak_client_id"]
    db_key.keycloak_client_uuid = kc_result["keycloak_client_uuid"]
    db_key.encrypted_keycloak_secret = encrypt_value(
        kc_result["keycloak_client_secret"], ENCRYPTION_KEY or ""
    )
    if expires_at is not None:
        db_key.expires_at = expires_at
    db_key.app_id = app.id
    db_session.add(db_key)
    db_session.commit()
    db_session.refresh(db_key)
    return db_key, new_key_value


def _revoke_app_key(db_session: Session, app: models.App) -> None:
    """Soft-delete the app's API key + disable its KC service account (reuse
    the routers/api_keys.py revoke path: delete KC client, soft-delete row)."""
    if app.api_key_id is None:
        return
    key = db_session.get(models.ApiKey, app.api_key_id)
    if key is None or key.deleted_at is not None:
        return
    if key.keycloak_client_uuid:
        try:
            keycloak_admin_client.delete_service_account_client(key.keycloak_client_uuid)
        except Exception as e:  # noqa: BLE001
            # Honest swallow: the row is still soft-revoked below, and the
            # leftover KC client is reclaimed by create_service_account_client's
            # idempotent 409 reuse when the next rotate re-mints the same
            # clientId — so a failed delete must not abort the rotate.
            logger.warning(
                "failed to delete app-key KC client %s: %s", key.keycloak_client_uuid, e
            )
    _crud_revoke_api_key(db=db_session, db_api_key=key)
    # Release the slug-derived Keycloak clientId on the soft-deleted row:
    # keycloak_client_id is unique=True (models.ApiKey) and _mint_app_key
    # deliberately re-mints the SAME clientId (`mod-app-key-{tenant}-{slug}`,
    # the KC 409-reuse path), so a revoked row still holding it makes the
    # rotate's INSERT violate api_keys_keycloak_client_id_key -> unhandled
    # 500 (no CORS header, UI hangs). Column is nullable; every reader of
    # the id (get_api_key_by_keycloak_client_id, service_account_inventory)
    # already filters deleted_at.is_(None), so nothing needs the id of a
    # REVOKED key — the KC client itself is deleted above by uuid.
    key.keycloak_client_id = None
    db_session.commit()


def _app_public(app: models.App, db=None) -> dict:
    return {
        "id": app.id,
        "slug": app.slug,
        "hostname": app.hostname,
        "name": app.name,
        "status": app.status,
        "failure_detail": app.failure_detail,
        "target_kind": app.target_kind,
        "stack_id": app.stack_id,
        "stack_service": app.stack_service,
        "stack_port": app.stack_port,
        "external_url": app.external_url,
        "allowed_workflow_ids": app.allowed_workflow_ids or [],
        "allowed_folder_ids": app.allowed_folder_ids or [],
        "created_at": app.created_at,
        # GX Q-ING=A follow-up: the owner's edge-Caddy upstream for this
        # manager (platform setting apps.edge_upstream, registered in
        # services/app_routing.py). Read-only helper text for the Apps
        # panel; None = unset, so the panel keeps its placeholder.
        "edge_upstream": app_routing.edge_upstream(db=db),
        # 2400GX-c2-J2 (30.6.2): the stored forward-auth config (incl. the
        # derived fail_closed) and whether the edge can VERIFY a fail-closed
        # check. `edge_ready()` never raises (its Vault read is cached and
        # swallows failures into False + a debug line), so no try is needed
        # here and a Vault outage can never 500 the GET (J2 vet). The ask
        # token value itself is never returned or logged — bool only (S5).
        "auth_config": app_forward_auth.config_for(app),
        "edge_ready": app_forward_auth.edge_ready(),
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@router.post("/", response_model=AppCreatedOut, status_code=status.HTTP_201_CREATED)
def create_app(
    payload: AppCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    """Provision a new app: service user + OIDC client + API key + proxy route."""
    tenant_id = current_user.tenant_id
    _check_max_apps(db, tenant_id)

    slug = app_hostname.validate_slug(payload.slug)
    hostname = app_hostname.compute_hostname(slug)
    conflict = _app_conflicts(db, tenant_id, slug, hostname)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    # external-url DNS fence runs OUTSIDE any open txn.
    db.commit()
    _validate_target(db, tenant_id, payload)

    # F16 (EARLY.APPS lifecycle): remove-then-install-again with the
    # SAME slug must work. uq_apps_tenant_slug (models.py) covers SOFT-deleted
    # rows, so a second INSERT of (tenant, slug) would raise IntegrityError
    # -> unhandled 500 (_app_conflicts filters deleted_at, so the pre-check
    # passes). Revive the revoked row IN PLACE instead: keep the primary key,
    # reset the mutable fields from the new request, clear deleted_at, and
    # re-run the exact same provisioning seams below (the KC client was
    # disabled-not-deleted and ensure_app_client updates it in place with
    # enabled=True; ensure_app_service_user heals the soft-deleted service
    # user; _mint_app_key mints a fresh key and re-uses the slug-derived KC
    # clientId through its documented 409-reuse path).
    revived = (
        db.query(models.App)
        .filter(models.App.tenant_id == tenant_id, models.App.slug == slug,
                models.App.deleted_at.isnot(None))
        .first()
    )
    if revived is not None:
        app = revived
        app.hostname = hostname
        app.name = payload.name or slug
        app.status = "provisioning"
        app.failure_detail = None
        app.deleted_at = None
        app.created_at = datetime.now(timezone.utc)
        app.target_kind = payload.target_kind
        app.stack_id = payload.stack_id
        app.stack_service = payload.stack_service
        app.stack_port = payload.stack_port
        app.external_url = payload.external_url
        app.allowed_workflow_ids = list(payload.allowed_workflow_ids)
        app.allowed_folder_ids = list(payload.allowed_folder_ids)
        app.created_by_user_id = current_user.id
        db.add(app)
        db.commit()
        db.refresh(app)
    else:
        app = models.App(
            tenant_id=tenant_id,
            slug=slug,
            hostname=hostname,
            name=payload.name or slug,
            status="provisioning",
            target_kind=payload.target_kind,
            stack_id=payload.stack_id,
            stack_service=payload.stack_service,
            stack_port=payload.stack_port,
            external_url=payload.external_url,
            allowed_workflow_ids=list(payload.allowed_workflow_ids),
            allowed_folder_ids=list(payload.allowed_folder_ids),
            created_by_user_id=current_user.id,
        )
        db.add(app)
        db.commit()
        db.refresh(app)

    _audit(db, app, current_user.id, "APP_CREATED",
           {"slug": slug, "hostname": hostname, "target_kind": payload.target_kind,
            **({"reinstalled": True} if revived is not None else {})})

    # App service user (GX.A3b) — the key authenticates as this non-human user.
    try:
        svc_user = ensure_app_service_user(db, app)
    except Exception as e:  # noqa: BLE001
        # F18: a DB failure here (e.g. the duplicate-email IntegrityError a
        # re-register of a removed slug used to hit) leaves the session in
        # PendingRollbackError; without this rollback the follow-up commit
        # raises and the endpoint 500s instead of the safe 502 below.
        db.rollback()
        app.status = "provisioning_failed"
        app.failure_detail = {"step": "service_user", "error": str(e)}
        db.commit()
        raise internal_http_error(502, "App service user provisioning failed", e) from e

    # KC OIDC client (step 2).
    try:
        kc = keycloak_admin_client.ensure_app_client(app)
        app.kc_client_id = kc["client_id"]
        app.kc_client_uuid = kc.get("uuid")
        db.commit()
        _audit(db, app, current_user.id, "APP_PROVISION_KC", {"action": kc["action"]})
    except Exception as e:  # noqa: BLE001
        app.status = "provisioning_failed"
        app.failure_detail = {"step": "kc_client", "error": str(e)}
        db.commit()
        raise internal_http_error(502, "Keycloak client provisioning failed", e) from e

    # API key (step 3) — owned by the app's service user.
    try:
        key, raw_key = _mint_app_key(db, app, svc_user.id)
        app.api_key_id = key.id
        db.commit()
        _audit(db, app, current_user.id, "APP_PROVISION_KEY", {"key_prefix": key.key_prefix})
    except Exception as e:  # noqa: BLE001
        app.status = "provisioning_failed"
        app.failure_detail = {"step": "api_key", "error": str(e)}
        db.commit()
        raise internal_http_error(502, "API key provisioning failed", e) from e

    # Proxy route (step 4) — fire-and-forget reconcile (own thread/session).
    app.status = "active"
    app.failure_detail = None
    db.commit()
    _audit(db, app, current_user.id, "APP_PROVISION_ROUTE", {"hostname": hostname})
    app_routing.trigger_reconcile_async()

    out = _app_public(app, db=db)
    out["key_prefix"] = key.key_prefix
    out["access_key"] = raw_key  # plaintext, surfaced exactly once (mint time)
    return out


@router.get("/", response_model=List[AppOut])
def list_apps(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    return [_app_public(a, db=db) for a in _list_apps(db, current_user.tenant_id)]


# ---------------------------------------------------------------------------
# GX.S1 — forward-auth verify (Caddy `forward_auth` target) + config
# ---------------------------------------------------------------------------

def _verify_unauthorized(login_url: Optional[str] = None,
                         detail: str = "not authenticated",
                         redirect_uri: Optional[str] = None,
                         wants_html: bool = False) -> JSONResponse:
    """Failure response for the verify endpoint. Caddy's forward_auth copies
    ANY non-2xx response from the auth endpoint (status + body) back to the
    client UNCHANGED — including a 3xx — so:
      * HTML requests (browsers; Accept: text/html) get a 302 to the app's
        own ``/_mod/auth/login`` (the MOD-handled OIDC code flow, GX.S1c);
      * everyone else gets 401 JSON with login_url (API clients / the app's
        own fetches, which own their Bearer-token login UX)."""
    body = {"authenticated": False, "public": False}
    if login_url:
        body["login_url"] = login_url
    if wants_html and redirect_uri:
        return JSONResponse(status_code=302, content=body,
                            headers={"location": redirect_uri})
    return JSONResponse(status_code=401, content={**body, "detail": detail})


# BUG.B86g — rejection reasons that mean the credential is VALID but the
# identity it names is not permitted. These must NEVER 302 back to login:
# the browser is already correctly authenticated with Keycloak, so a 302
# just re-auths the SAME identity and loops forever (login -> callback ->
# verify reject -> login -> ...). Anything NOT in this set is an authN
# problem (missing/invalid/expired/foreign-app credential) and keeps the
# existing 302/401 behaviour — the browser needs to go sign in again, which
# CAN change the outcome.
_AUTHZ_DENIED_REASONS = frozenset({
    "user not in allowed groups",
    "user not in this app's tenant",
    "user not in this stack's tenant",
})


def _verify_authz_denied(request: Request, app: Optional[models.App],
                         stack: Optional[models.PersistentStack],
                         claims: Optional[dict], detail: str,
                         wants_html: bool) -> JSONResponse:
    """BUG.B86g — authZ (not authN) rejection: the caller IS who they say
    they are, they're just not permitted for this app/stack. Always 403,
    NEVER a redirect (a 302 here is the infinite-loop bug)."""
    email = ((claims or {}).get("email")
             or (claims or {}).get("preferred_username")
             or (claims or {}).get("sub") or "unknown user")
    target_name = (app.name if app is not None
                   else (stack.name if stack is not None else "this app")) \
        or "this app"
    if wants_html:
        safe_email = html.escape(str(email))
        safe_name = html.escape(str(target_name))
        page = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Access denied</title></head><body>"
            f"<p>You're signed in as <strong>{safe_email}</strong> but "
            f"don't have access to <strong>{safe_name}</strong>.</p>"
            "<p>Ask the app owner for access, or "
            f"<a href=\"{app_forward_auth.MOD_AUTH_PATH}/logout\">"
            "sign out and switch account</a>.</p>"
            "</body></html>"
        )
        return HTMLResponse(status_code=403, content=page)
    return JSONResponse(
        status_code=403,
        content={"detail": detail, "code": "APP_ACCESS_DENIED"},
    )


def _verify_reject_log(
    request: Request, host: str, app: Optional[models.App],
    stack: Optional[models.PersistentStack], reason: str,
    claims: Optional[dict] = None, via_cookie: bool = False,
    authz: bool = False,
) -> None:
    """BUG.B86/BUG.B86c live-diagnosis: one WARNING line per verify rejection.

    The loop (login 302 → callback 302 → verify 302 → login …) is invisible in
    the field unless the rejection reason is logged, so EVERY reject branch
    emits: ``app_auth_verify reject reason=<code> host=<resolved host>
    has_cookie=<bool> app_id=<id or None>``.

    NEVER logged: the cookie value, the token, the auth code, ``sub``/``email``
    or any other user secret. Only the resolved host, a boolean "did the
    browser send a session cookie at all", and the DB ids the host resolved to
    (ints) plus the cookie's own ``app_id`` pin (an int) when it parsed.
    """
    cookie = request.cookies.get(app_forward_auth.SESSION_COOKIE, "") or \
        next(iter(app_forward_auth.session_cookie_values(request)), "")
    bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    claim_app_id = claims.get("app_id") if (claims and via_cookie) else None
    logger.warning(
        "app_auth_verify reject reason=%s host=%s has_cookie=%s app_id=%s "
        "via=%s app_found=%s stack_found=%s claim_app_id=%s authz=%s",
        reason, host or "-", bool(cookie),
        (app.id if app is not None
         else (stack.id if stack is not None else None)),
        "bearer" if bearer else ("cookie" if cookie else "none"),
        app is not None, stack is not None, claim_app_id, int(authz),
    )


def _wants_html(request: Request) -> bool:
    """True when the ORIGINAL client is a browser page load. Caddy's
    forward_auth forwards the original request's headers to the verify
    endpoint (plus X-Forwarded-Method / X-Forwarded-Uri), so the client's
    Accept is still visible here."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def _mod_login_redirect_uri(request: Request) -> Optional[str]:
    """The app-relative ``/_mod/auth/login?rd=<original uri>`` a browser
    should be sent to. Caddy's forward_auth sets X-Forwarded-Uri to the
    original request URI; fall back to the API path (always safe — it is
    relative on the app host)."""
    uri = request.headers.get("x-forwarded-uri", "") or \
        request.url.path or "/"
    # uri is the ORIGINAL app request URI (e.g. "/dashboard?x=1") — it is
    # already relative to the app host; guard against a malformed/absolute
    # value (open-redirect fence).
    if not uri.startswith("/") or uri.startswith("//") or ":" in uri.split("/", 3)[0]:
        uri = "/"
    return f"{app_forward_auth.MOD_AUTH_PATH}/login?rd={quote(uri)}"


def _callback_access_denied(request: Request,
                            stack: models.PersistentStack) -> HTMLResponse:
    """n3c: the stack callback's authZ deny page. Generic 403 HTML that names
    the stack only: never the caller's email, groups or the stack's
    allowed_groups (the verify leg's page may echo the email; this one must
    not, it is reached straight after a fresh KC login)."""
    safe_name = html.escape(str(getattr(stack, "name", None) or "this app"))
    page = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Access denied</title></head><body>"
        f"<p>Your account doesn't have access to <strong>{safe_name}</strong>."
        "</p><p>Ask the app owner for access, or "
        f"<a href=\"{app_forward_auth.MOD_AUTH_PATH}/logout\">"
        "sign out and switch account</a>.</p>"
        "</body></html>"
    )
    return HTMLResponse(status_code=403, content=page)


def _resolve_stack_identity(db: Session, stack: models.PersistentStack, cfg: dict,
                            claims: dict, via_cookie: bool = False
                            ) -> "tuple[Optional[models.User], dict, str]":
    """Verify checks for the STACK host fallback (GX.S1c-stacks).

    Stacks have no per-stack KC client, so the Bearer path skips the app's
    ``azp`` pin and enforces the platform JWKS audience (fastapi-app — carried
    by every platform client) plus a TENANT pin: the caller must be a MOD user
    of THIS stack's tenant (a platform admin passes for any tenant). A session
    cookie is accepted when it was minted for THIS stack host — the mint also
    carries ``stack_id``, re-checked here so an app cookie (or another stack's
    cookie) can never be replayed against this host.
    """
    if via_cookie:
        if int(claims.get("stack_id") or 0) != int(stack.id):
            return None, [], "session cookie for another host"
    else:
        aud = claims.get("aud")
        aud_ok = (aud == KEYCLOAK_AUDIENCE) if isinstance(aud, str) else \
            (KEYCLOAK_AUDIENCE in (aud or []))
        if not aud_ok:
            return None, [], "wrong audience"
    groups = claims.get("groups") or []
    # Alias-aware intersection: expand both token groups and config allowed_groups
    # through the group-name alias map so minio_tenant_admin / s3_tenant_admin
    # match regardless of which name the KC token carries.
    from utils.keycloak_client import _expand_group_name
    expanded_groups = set()
    for g in groups:
        expanded_groups.update(_expand_group_name(str(g)))
    expanded_cfg = set()
    for g in cfg.get("allowed_groups", []):
        expanded_cfg.update(_expand_group_name(str(g)))
    if cfg["allowed_groups"] and not (expanded_groups & expanded_cfg):
        return None, [], "user not in allowed groups"
    user = app_forward_auth.get_user_by_kc_id(db, claims.get("sub", ""))
    if user is not None and user.status != models.UserStatus.ACTIVE:
        return None, groups, "user disabled"
    # Fail closed: a KC identity with no MOD user (or not a platform admin)
    # cannot satisfy the stack's tenant pin, and stacks have no app_users
    # self-signup path to grant one.
    if user is None or (not getattr(user, "is_platform_admin", False)
                        and user.tenant_id != stack.tenant_id):
        return None, groups, "user not in this stack's tenant"
    return user, groups, ""


def _resolve_identity(db: Session, app: models.App, cfg: dict, claims: dict,
                      via_cookie: bool = False
                      ) -> "tuple[Optional[models.User], dict, str]":
    """Shared verify checks for EITHER credential kind.

    Returns ``(user, groups, error_detail)`` — error_detail is "" on success.

    * **Bearer token** (``via_cookie=False``): the raw KC claims, so the
      platform JWKS check already ran and the ``azp`` == this app's client
      (inbound Host) pin applies — a token minted by any other client fails.
    * **Session cookie** (``via_cookie=True``): MOD-minted at callback AFTER
      that same JWKS + azp check already passed, and pinned to ONE app by
      ``app_id`` (the ``__Host-`` prefix scopes it to that host in the
      browser; the app_id check rejects a crafted/host-spoofed cookie). A
      cookie carries no KC ``aud``/``azp`` — it carries app_id/sub/groups —
      so those two checks are skipped on this path.

    Both paths re-check the DB on EVERY verify: the app must be active and a
    linked user must not be disabled (a stateless cookie can't be revoked
    server-side, so the DB is the kill-switch).
    """
    if getattr(app, "status", None) != "active":
        return None, [], "app not active"

    if not via_cookie:
        aud = claims.get("aud")
        aud_ok = (aud == KEYCLOAK_AUDIENCE) if isinstance(aud, str) else \
            (KEYCLOAK_AUDIENCE in (aud or []))
        if not aud_ok:
            return None, [], "wrong audience"
        if claims.get("azp") != app_forward_auth.kc_client_id_for(app):
            return None, [], "token not issued by this app's client"
    else:
        # Cookie pin: minted for exactly one app.
        claim_app_id = claims.get("app_id")
        if claim_app_id is None or int(claim_app_id) != app.id:
            return None, [], "session cookie for another app"

    groups = claims.get("groups") or []
    # Alias-aware intersection: expand both token groups and config allowed_groups
    # through the group-name alias map so minio_tenant_admin / s3_tenant_admin
    # match regardless of which name the KC token carries.
    from utils.keycloak_client import _expand_group_name
    expanded_groups = set()
    for g in groups:
        expanded_groups.update(_expand_group_name(str(g)))
    expanded_cfg = set()
    for g in cfg.get("allowed_groups", []):
        expanded_cfg.update(_expand_group_name(str(g)))
    if cfg["allowed_groups"] and not (expanded_groups & expanded_cfg):
        return None, [], "user not in allowed groups"
    user = app_forward_auth.get_user_by_kc_id(db, claims.get("sub", ""))
    if user is not None and user.status != models.UserStatus.ACTIVE:
        return None, groups, "user disabled"
    # Tenant pin (mirrors _resolve_stack_identity's tenant check): a KC
    # identity that resolves to an existing MOD platform user must belong to
    # THIS app's tenant (a platform admin passes for any tenant) — otherwise
    # a member of tenant X who can authenticate against tenant Y's app client
    # (one shared KC realm) would leak their real MOD username/email to
    # tenant Y's app via the X-Mod-* headers. Unlike stacks, user is None is
    # NOT rejected here: apps have a dedicated app_users self-signup path
    # (GX.S1b) for KC identities with no MOD account, which this tenant pin
    # must not break.
    if (user is not None and not getattr(user, "is_platform_admin", False)
            and user.tenant_id != app.tenant_id):
        return None, groups, "user not in this app's tenant"
    if user is None and app_forward_auth.app_user_disabled(
            db, app, claims.get("sub", "")):
        return None, groups, "app user disabled"
    return user, groups, ""


# api-lint: public-by-design: Caddy forward_auth verify — does its own Bearer-JWKS or session-cookie HMAC check inline (class B)
@router.get("/auth/verify", response_model=AppVerifyOut)
def app_auth_verify(
    request: Request,
    db: Session = Depends(get_db),
):
    """Caddy `forward_auth` verify endpoint (GX.S1 + GX.S1c browser session,
    see services/app_forward_auth.py for the design).

    Called server-side by caddy-front for every non-public path of a
    require_login app. Two credential kinds are accepted:
      * **Bearer token** (GX.S1, unchanged): validated with the platform's
        standard JWKS check (audience = fastapi-app, realm issuer).
      * **Session cookie** (GX.S1c): the compact HS256 token set by
        ``/auth/callback`` after the MOD-handled OIDC code flow — this is
        what a plain browser page load carries.
    Both then share: ``azp`` == THIS app's KC client (inbound Host),
    allowed_groups intersect, app active, linked user not disabled.

    Success: 200 + X-Mod-User / X-Mod-Email / X-Mod-Groups / X-Mod-User-Type
    (forward_auth's copy_headers whitelists exactly these).
    Failure: Caddy's forward_auth copies ANY non-2xx response (status + body)
    back to the client unchanged — including a 3xx — so an HTML request
    (Accept: text/html) gets 302 → ``/_mod/auth/login?rd=<original uri>`` and
    everything else gets 401 JSON with ``login_url``.
    """
    # 30.6.4 c4b — the OPTIONAL leg rendered for public_paths
    # (`uri /v1/apps/auth/verify?optional=1`, services/app_forward_auth.py).
    # MUST-2: on this leg EVERY non-success outcome is an anonymous 200 with an
    # empty body and no X-Mod-* header — never 302 (login redirect), never 401,
    # never 403. A public path must work for logged-out visitors; Caddy only
    # routes a cookie-carrying request here at all (MUST-1), so a miss is just
    # "no identity", not an error. MUST-3: with `optional` absent the responses
    # below are byte-identical to before.
    optional = request.query_params.get("optional") == "1"

    def _optional_anonymous() -> Response:
        """c4b MUST-2: anonymous 200 — no body, no X-Mod-* header, no redirect."""
        return Response(status_code=200)

    # BUG.B86c — the inbound host is X-Forwarded-Host FIRST, then Host,
    # exactly like `_app_from_host`/`_stack_from_host` (the login + callback
    # legs). A reverse proxy in front of caddy-front (nginx) re-origins the
    # request: it forwards the app Host but ALSO carries its own/other
    # X-Forwarded-Host, and some chains drop Host entirely. Reading only
    # ``Host`` here while login/callback read X-Forwarded-Host first means the
    # cookie the callback mints for host A is verified against host B — the
    # app_id/stack_id pin then rejects it and the browser loops (login →
    # callback 302 → verify 302 → login …). One resolution order everywhere.
    host = request.headers.get("x-forwarded-host") or \
        request.headers.get("host", "")
    app = app_forward_auth.get_app_by_hostname(db, host)
    stack, stack_cfg = (None, None)
    if app is None:
        # GX.S1c-stacks — no App owns this Host; a PUBLICLY EXPOSED persistent
        # stack with an `x-mod.ingress.auth` block may (the ingress renders
        # forward_auth + the `/_mod/auth*` flow on the stack host too).
        stack, stack_cfg = stack_forward_auth.get_stack_by_hostname(db, host)
        if stack is None:
            _verify_reject_log(request, host, None, None, "unknown-host")
            if optional:
                return _optional_anonymous()
            return _verify_unauthorized(detail="unknown app host")
        cfg = stack_cfg
    else:
        cfg = app_forward_auth.config_for(app)
    if not cfg["require_login"]:
        # Not protected — 200 with no identity headers (app is public).
        return {"authenticated": False, "public": True}

    if stack is not None:
        # GX.S1c-stacks — a stack host gets the SAME browser shape as an app
        # host: an HTML request is 302'd to the stack's own /_mod/auth/login
        # (the MOD-handled code flow on the stack host, pinned to the matched
        # serving host) and everyone else gets 401 JSON carrying the KC
        # authorize URL for the platform client.
        authorize, _st, _vr = stack_forward_auth.stack_login_url_for(
            stack, stack_forward_auth.stack_redirect_uri(stack))
        login_url = authorize
    else:
        login_url = app_forward_auth.login_url_for(app)
    redirect_uri = _mod_login_redirect_uri(request)
    wants_html = _wants_html(request)
    cred = {"claims": None, "via_cookie": False}

    def fail(detail: str) -> JSONResponse:
        """BUG.B86c/B86g diagnosis: every rejection logs its reason before
        replying. AuthZ rejections (valid credential, not permitted) ALWAYS
        get 403 — never the 302/401 (that path is for authN failures, and
        302-ing an already-authenticated browser back to login is the
        infinite-loop bug)."""
        is_authz = detail in _AUTHZ_DENIED_REASONS
        _verify_reject_log(request, host, app, stack, detail,
                           cred["claims"], cred["via_cookie"], authz=is_authz)
        if optional:
            # c4b MUST-2: the optional leg never 302/401/403 and never leaks
            # identity — the diagnosis above is logged, then anonymous 200.
            return _optional_anonymous()
        if is_authz:
            return _verify_authz_denied(request, app, stack, cred["claims"],
                                        detail, wants_html)
        return _verify_unauthorized(login_url, detail,
                                    redirect_uri=redirect_uri,
                                    wants_html=wants_html)

    claims: Optional[dict] = None
    via_cookie = False
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            claims = get_current_user_claims(auth[7:].strip())  # JWKS check
        except HTTPException:
            return fail("invalid token")
        cred["claims"], cred["via_cookie"] = claims, False
    else:
        # BUG.B86e: try EVERY value the browser sent for the session-cookie
        # name, not just the one Starlette keeps. A stale same-named cookie
        # (stored under a parent Domain or a different Path) is sent alongside
        # the freshly minted host-only one, and request.cookies keeps only one
        # value per name — so the minted cookie could never be seen here.
        values = app_forward_auth.session_cookie_values(request)
        try:
            claims = None
            for value in values:
                claims = app_forward_auth.parse_session_token(value)
                if claims is not None:
                    break
        except SessionHmacError:
            return fail("session key unavailable")
        if claims is None:
            # The log line's has_cookie=<bool> separates "browser sent nothing"
            # (cookie dropped by the browser: __Host- prefix violation,
            # oversized value, cross-site SameSite drop) from "sent but
            # rejected" (bad signature / expired / forged / foreign key).
            return fail("missing bearer token or session cookie")
        via_cookie = True
        cred["claims"], cred["via_cookie"] = claims, True

    if stack is not None:
        user, groups, err = _resolve_stack_identity(db, stack, cfg, claims,
                                                    via_cookie=via_cookie)
    else:
        user, groups, err = _resolve_identity(db, app, cfg, claims,
                                              via_cookie=via_cookie)
    if err:
        return fail(err)

    user_type = app_forward_auth.user_type_for(user, db, app, claims)
    # 30.6.4 c4a/c4d — mint ONLY after the allow decision above (`if err: return
    # fail(err)`), so an unresolved or unauthorised identity never gets a token.
    # APP branch: aud=app:<id> (c4a). STACK branch: aud=stack:<id> (c4d) with
    # tenant_id from the stack's DB row. A mint failure (no manager keypair on
    # this box, or an AppAssertionError) logs the id + error class ONLY (never
    # the token, S5) and falls back to the response WITHOUT the header: c4a/c4d
    # must not break existing hosts; c4b decides fail_closed behaviour.
    assertion_headers = {}
    if stack is None:
        try:
            assertion_headers[app_forward_auth.ASSERTION_HEADER] = \
                app_forward_auth.mint_app_assertion(
                    app=app, user=user, claims=claims, groups=groups,
                    user_type=user_type)
        except (app_forward_auth.AppAssertionError, RuntimeError) as e:
            logger.warning("app_auth_verify: assertion mint failed app=%s code=%s",
                           app.id, getattr(e, "code", "runtime"))
            if optional:
                # c4b MUST-2: on the optional leg a mint failure is a
                # non-success — anonymous 200, never half the identity set.
                return _optional_anonymous()
    else:
        # 30.6.4 c4d — stack hosts get the same signed identity, bound to
        # aud=stack:<id>. On the optional leg the assertion is included only
        # when the stack identity resolved (we are past `if err` here); an
        # anonymous public path already returned _optional_anonymous() above
        # (routers/apps.py:781 `_optional_anonymous`), so it stays anonymous.
        try:
            assertion_headers[app_forward_auth.ASSERTION_HEADER] = \
                stack_forward_auth.mint_stack_assertion(
                    stack=stack, user=user, claims=claims, groups=groups,
                    user_type=user_type)
        except (app_forward_auth.AppAssertionError, RuntimeError) as e:
            logger.warning("app_auth_verify: assertion mint failed stack=%s code=%s",
                           stack.id, getattr(e, "code", "runtime"))
            if optional:
                # c4b MUST-2 mirrored on the stack leg: mint failure on the
                # optional path is a non-success — anonymous 200.
                return _optional_anonymous()
    headers = {
        "X-Mod-User": ((user.username if user else claims.get("preferred_username")) or claims.get("sub", "")),
        "X-Mod-Email": (user.email if user else claims.get("email")) or "",
        "X-Mod-Groups": ",".join(groups),
        "X-Mod-User-Type": user_type,
    }
    headers.update(assertion_headers)
    return JSONResponse(
        status_code=200,
        content={
            "authenticated": True,
            "public": False,
            "mod_user": (user.username if user else claims.get("preferred_username")) or claims.get("sub"),
            "mod_email": (user.email if user else claims.get("email")) or "",
            "mod_groups": groups,
            "mod_user_type": user_type,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# GX.S1c — MOD-handled OIDC code flow on the app's own host.
#
# Caddy renders (render_app_site) a PUBLIC ``handle_path /_mod/auth*`` that
# reverse_proxies to the API with ``uri /v1/apps/auth`` — so the browser hits
# <app host>/_mod/auth/{login,callback,logout} and Caddy rewrites it to
# /v1/apps/auth/{login,callback,logout} on the API. The host→app mapping is
# the inbound Host header (X-Forwarded-Host / Host — caddy's reverse_proxy
# forwards the original Host to the upstream via header_up Host {host}).
# These routes are PUBLIC on purpose: they are the login itself.
# ---------------------------------------------------------------------------

def _app_from_host(db: Session, request: Request) -> Optional[models.App]:
    """The app owning this request's inbound host (X-Forwarded-Host first —
    that is what Caddy's forward_auth/handle_path pass — then Host)."""
    host = request.headers.get("x-forwarded-host") or \
        request.headers.get("host", "")
    return app_forward_auth.get_app_by_hostname(db, host)


def _stack_from_host(db: Session, request: Request):
    """The exposed persistent stack serving this request's inbound host, plus
    its forward-auth config (GX.S1c-stacks) — the stack sibling of
    `_app_from_host`, tried only when no App owns the Host."""
    host = request.headers.get("x-forwarded-host") or \
        request.headers.get("host", "")
    return stack_forward_auth.get_stack_by_hostname(db, host)


# api-lint: public-by-design: OIDC authorize redirect — no authentication yet required (class B)
@router.get("/auth/login")
def app_auth_login(
    request: Request,
    rd: str = "/",
    db: Session = Depends(get_db),
):
    """Start the MOD-handled OIDC code flow: mint PKCE S256 + state, store
    them in a short-lived signed cookie (NOT the DB), and 302 to the realm's
    authorize endpoint for THIS app's KC client.

    ``rd`` is the post-login destination — it must be a RELATIVE path (the
    open-redirect guard); anything else is silently reset to "/".
    """
    app = _app_from_host(db, request)
    stack = None
    if app is None:
        stack, stack_cfg = _stack_from_host(db, request)
        if stack is None:
            raise HTTPException(status_code=404, detail="unknown app host")
        if not stack_cfg["require_login"]:
            # A public stack has no login to start — the ingress never renders
            # forward_auth for it, so a login request here is a stale link.
            raise HTTPException(status_code=404,
                                detail="stack host is public")

    if stack is not None:
        # GX.S1c-stacks — the same flow on a stack host: the platform KC
        # client (stacks have none of their own) and a redirect_uri pinned to
        # the matched serving host (never an arbitrary request Host).
        authorize, state, verifier = stack_forward_auth.stack_login_url_for(
            stack, stack_forward_auth.stack_redirect_uri(stack))
        if not authorize:
            raise HTTPException(status_code=404,
                                detail="stack host has no serving hostname")
        state_cookie = stack_forward_auth.stack_state_cookie(stack, rd)
        resp = RedirectResponse(authorize, status_code=302)
        resp.set_cookie(app_forward_auth.STATE_COOKIE, state_cookie,
                        **app_forward_auth.state_cookie_attrs())
        return resp

    try:
        state_cookie = app_forward_auth.mint_state_cookie(app.id, rd)
        parsed = app_forward_auth.parse_state_cookie(state_cookie, app.id)
    except SessionHmacError:
        logger.warning(
            "app_auth_login: session key unavailable, "
            "sign-in temporarily unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is temporarily unavailable (session key not loaded). Try again shortly.")
    verifier, state = parsed["verifier"], parsed["state"]
    challenge = app_forward_auth.pkce_challenge(verifier)

    # redirect_uri = this app's own origin + the Caddy-rewritten callback
    # path. It must match the client's registered `{origin}/*` pattern.
    scheme = request.headers.get("x-forwarded-proto", "https").split(",")[0].strip() or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    host = host.split(":")[0]
    redirect_uri = f"{scheme}://{host}{app_forward_auth.MOD_AUTH_PATH}/callback"

    # BUG B86h: self-heal the `basic` default client scope (Keycloak >= 25
    # delivers `sub` from it; without it the session cookie is unreadable and
    # login loops). Memoized per process, and never blocks login.
    try:
        keycloak_admin_client.ensure_app_client_scopes(
            app_forward_auth.kc_client_id_for(app))
    except Exception as e:  # noqa: BLE001
        logger.warning("app_auth_login: ensure 'basic' scope failed for %s: %s",
                       app_forward_auth.kc_client_id_for(app), e)

    authorize = app_forward_auth.kc_authorize_url(
        app_forward_auth.kc_client_id_for(app), redirect_uri, state, challenge)

    resp = RedirectResponse(authorize, status_code=302)
    resp.set_cookie(app_forward_auth.STATE_COOKIE, state_cookie,
                    **app_forward_auth.state_cookie_attrs())
    return resp


# api-lint: public-by-design: Keycloak return leg of MOD-handled OIDC code flow — PKCE code exchange, not a public endpoint (class B)
@router.get("/auth/callback")
def app_auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    db: Session = Depends(get_db),
):
    """Keycloak's return leg: verify state, exchange the code (PKCE),
    validate the token (JWKS + azp == this app's client), set the session
    cookie, 302 to the original (relative) page.

    The post-login destination is X-Forwarded-Uri (the URI Caddy called
    verify with — the page the browser originally asked for), re-guarded to
    be relative-only.
    """
    app = _app_from_host(db, request)
    stack, stack_cfg = (None, None)
    if app is None:
        stack, stack_cfg = _stack_from_host(db, request)
        if stack is None:
            raise HTTPException(status_code=404, detail="unknown app host")

    # Same B86e shadowing class as the session cookie: try EVERY value the
    # browser sent for the state-cookie name (a stale parent-Domain/other-Path
    # duplicate must not shadow the one /auth/login just minted), not just
    # the single value Starlette's request.cookies collapses to.
    owner_id = app.id if app is not None else stack.id
    state_values = app_forward_auth.state_cookie_values(request)
    try:
        parsed = None
        for state_cookie in state_values:
            parsed = app_forward_auth.parse_state_cookie(state_cookie, owner_id)
            if parsed is not None:
                break
    except SessionHmacError:
        logger.warning(
            "app_auth_callback: session key unavailable, "
            "sign-in temporarily unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is temporarily unavailable (session key not loaded). Try again shortly.")
    if parsed is None or parsed["state"] != state or not code:
        # State mismatch = CSRF or a replayed/expired/cross-host flow. Never
        # redirect with attacker-controlled data on this path.
        raise HTTPException(status_code=400, detail="state validation failed")

    if stack is not None:
        # GX.S1c-stacks — the redirect_uri is the stack's PINNED serving host,
        # not whatever Host the callback arrived on (login minted this exact
        # value; a mismatch = the code was minted for another host).
        redirect_uri = stack_forward_auth.stack_redirect_uri(stack)
    else:
        scheme = request.headers.get("x-forwarded-proto", "https").split(",")[0].strip() or "https"
        host = (request.headers.get("x-forwarded-host") or
                request.headers.get("host", "")).split(":")[0]
        redirect_uri = f"{scheme}://{host}{app_forward_auth.MOD_AUTH_PATH}/callback"
    try:
        tokens = app_forward_auth.exchange_auth_code(
            code, redirect_uri, parsed["verifier"],
            app_forward_auth.kc_client_id_for(app) if app is not None
            else stack_forward_auth.stack_platform_client_id())
    except ValueError:
        raise HTTPException(status_code=401,
                             detail="authorization code rejected")

    # Validate the minted access token with the platform's standard JWKS
    # check (aud = fastapi-app, realm issuer) + this app's client (azp).
    access_token = tokens.get("access_token", "")
    try:
        claims = get_current_user_claims(access_token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="token validation failed")
    if app is not None and claims.get("azp") != app_forward_auth.kc_client_id_for(app):
        raise HTTPException(status_code=401,
                             detail="token not issued by this app's client")

    # The post-login destination is the SIGNED rd from the state cookie —
    # already validated to a relative path at login (open-redirect fence).
    # Re-guard defensively (defense in depth).
    rd = parsed.get("rd") or "/"
    if not app_forward_auth._RD_RE.match(rd):
        rd = "/"

    user = app_forward_auth.get_user_by_kc_id(db, claims.get("sub", ""))
    if stack is not None:
        # Re-run the stack's verify checks before minting the cookie: the
        # tenant pin + allowed_groups must hold, or the login never happened.
        _u, _g, deny = _resolve_stack_identity(db, stack, stack_cfg, claims)
        if deny:
            logger.info("app_auth_callback: stack access denied reason=%s "
                        "stack_id=%s", deny, getattr(stack, "id", None))
            return _callback_access_denied(request, stack)
        try:
            session_token = stack_forward_auth.stack_session_token(
                stack, {**claims,
                        "email": (user.email if user else claims.get("email")) or ""})
        except SessionHmacError:
            logger.warning(
                "app_auth_callback: session key unavailable, "
                "sign-in temporarily unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sign-in is temporarily unavailable (session key not loaded). Try again shortly.")
    else:
        try:
            session_token = app_forward_auth.mint_session_token(
                {
                    "app_id": app.id,
                    "tenant_id": app.tenant_id,
                    "sub": claims.get("sub"),
                    "email": (user.email if user else claims.get("email")) or "",
                    "groups": claims.get("groups") or [],
                    "user_type": app_forward_auth.user_type_for(user, db, app, claims),
                },
                app_forward_auth.session_ttl_seconds(),
            )
        except SessionHmacError:
            logger.warning(
                "app_auth_callback: session key unavailable, "
                "sign-in temporarily unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sign-in is temporarily unavailable (session key not loaded). Try again shortly.")
        except ValueError as e:
            # BUG B86h: no `sub` on the KC token = the app client is missing the
            # `basic` default scope. Minting anyway would set a cookie verify
            # can never read, so the browser loops. Fail loud instead.
            logger.warning("app_auth_callback: %s (app slug=%s)", e, app.slug)
            raise internal_http_error(
                502,
                "App sign-in failed: the Keycloak token carried no 'sub' "
                "(app client missing the 'basic' client scope).", e) from e

    resp = RedirectResponse(rd, status_code=302)
    resp.set_cookie(app_forward_auth.SESSION_COOKIE, session_token,
                    **app_forward_auth.session_cookie_attrs(
                        app_forward_auth.session_ttl_seconds()))
    # BUG.B86e: expire the stale same-named session-cookie variant stored under
    # the parent Domain (browsers key a cookie by name+Domain+Path, so a stale
    # Domain=.example.com copy survives next to the host-only cookie just
    # minted above and is sent alongside it — the login loop). The host-only
    # Path=/ variant is NOT cleared: that is the cookie we just minted.
    for clear in app_forward_auth.stale_session_cookie_clears(request):
        resp.delete_cookie(app_forward_auth.SESSION_COOKIE, **clear)
    # P0 consign loop fix: this is itself a __Host- cookie Set-Cookie — it
    # needs Secure (+ matching attrs) or the browser silently drops it and
    # the state cookie is never actually cleared (see host_cookie_clear_attrs).
    resp.delete_cookie(app_forward_auth.STATE_COOKIE,
                       **app_forward_auth.host_cookie_clear_attrs())
    return resp


# api-lint: public-by-design: clears session cookie and 302s to KC end-session — must work with expired/absent session (class B)
@router.get("/auth/logout")
def app_auth_logout(request: Request, db: Session = Depends(get_db)):
    """Clear the session cookie, then 302 to the realm's end-session
    endpoint (single logout).

    GX.S1c-stacks: a stack host gets the same treatment (its own session
    cookie is cleared there too — the ``__Host-`` prefix scopes it), so the
    route resolves app-or-stack and 404s for a host neither owns."""
    app = _app_from_host(db, request)
    if app is None and _stack_from_host(db, request)[0] is None:
        raise HTTPException(status_code=404, detail="unknown app host")
    resp = RedirectResponse(app_forward_auth.kc_end_session_url(),
                            status_code=302)
    # P0 consign loop fix: see host_cookie_clear_attrs — without Secure the
    # browser rejects the deletion of these __Host- cookies outright.
    clear_attrs = app_forward_auth.host_cookie_clear_attrs()
    resp.delete_cookie(app_forward_auth.SESSION_COOKIE, **clear_attrs)
    resp.delete_cookie(app_forward_auth.STATE_COOKIE, **clear_attrs)
    return resp


@router.put("/{app_id}/auth-config", response_model=AppOut)
def update_auth_config(
    app_id: int,
    payload: AppAuthConfigUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    """Tenant admin: set the app's forward-auth config (GX.S1). Reconciles the
    app's caddy-front site so the change is live on the next render pass."""
    app = _get_app(db, app_id, current_user.tenant_id)
    try:
        cfg = app_forward_auth.validate_config(
            payload.require_login, payload.allowed_groups, payload.public_paths,
            payload.fail_closed)
    except ValueError as e:
        # Controlled validation message from app_forward_auth.validate_config
        # (field-shape checks only, never a raw internal exception) — safe to
        # surface directly as the 400 detail string.
        raise HTTPException(status_code=400, detail=(e.args[0] if e.args else "Invalid auth config"))
    # 2400GX-c2-J2 (30.6.2, S9/S6): a fail-closed seam the edge cannot VERIFY is
    # refused BEFORE anything is written, audited or reconciled — the only way
    # to reach fail_closed=true with no ask token is this 409. Tenant pin +
    # admin (S1/S2) and validate (S7) already ran above.
    if cfg["require_login"] and cfg["fail_closed"] and not app_forward_auth.edge_ready():
        raise ApiError(409, "edge_not_ready",
                       "edge is not provisioned for a fail-closed check "
                       "(ask token absent)")
    old = app_forward_auth.config_for(app)
    app.forward_auth_config = cfg
    db.commit()
    _audit(db, app, current_user.id, "APP_AUTH_CONFIG_UPDATED",
           {"old": old, "new": cfg})
    app_routing.trigger_reconcile_async()
    return _app_public(app, db=db)


@router.get("/{app_id}", response_model=AppOut)
def get_app(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    return _app_public(_get_app(db, app_id, current_user.tenant_id), db=db)


@router.post("/{app_id}/update-target", response_model=AppOut)
def update_target(
    app_id: int,
    payload: AppUpdateTarget,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    app = _get_app(db, app_id, current_user.tenant_id)
    old = {
        "target_kind": app.target_kind, "stack_id": app.stack_id,
        "stack_service": app.stack_service, "stack_port": app.stack_port,
        "external_url": app.external_url,
    }
    db.commit()  # close any open txn before the external-url DNS fence
    _validate_target(db, app.tenant_id, payload)

    app.target_kind = payload.target_kind
    app.stack_id = payload.stack_id
    app.stack_service = payload.stack_service
    app.stack_port = payload.stack_port
    app.external_url = payload.external_url
    db.commit()

    _audit(db, app, current_user.id, "APP_ROUTE_TARGET_UPDATED",
           {"old": old, "new": payload.model_dump()})

    # 30.6.13 c13-J2 — the identity status of the NEW target, computed AFTER the
    # target write (never read from the body, S5) and always present (S4: no
    # silent downgrade — an external-url target answers "none", not an absent
    # field).
    identity = _update_target_identity(db, app, current_user)
    if identity["status"] != "none":
        _audit(db, app, current_user.id, "APP_TARGET_REMINTED",
               {"app_id": app.id, "status": identity["status"]})

    app_routing.trigger_reconcile_async()
    return {**_app_public(app, db=db), "identity": identity}


@router.post("/{app_id}/rotate-key", response_model=AppKeyOut)
def rotate_key(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    """Mint a fresh key for the app; the OLD key is retired (soft-deleted).
    Scope lives on the app, so rotation keeps the scope (§3)."""
    app = _get_app(db, app_id, current_user.tenant_id)
    old_prefix = None
    if app.api_key_id is not None:
        old = db.get(models.ApiKey, app.api_key_id)
        if old is not None and old.deleted_at is None:
            old_prefix = old.key_prefix
            _revoke_app_key(db, app)

    svc_user = ensure_app_service_user(db, app)
    key, raw_key = _mint_app_key(db, app, svc_user.id)
    app.api_key_id = key.id
    db.commit()
    _audit(db, app, current_user.id, "APP_KEY_ROTATED",
           {"old_prefix": old_prefix, "new_prefix": key.key_prefix})
    return {
        "id": app.id,
        "key_prefix": key.key_prefix,
        "access_key": raw_key,  # plaintext, surfaced exactly once (mint time)
        "created_at": app.created_at,
    }


@router.post("/{app_id}/retry-provisioning", response_model=AppOut)
def retry_provisioning(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    """Re-run the failed provisioning steps (idempotent at every step, §2.3)."""
    app = _get_app(db, app_id, current_user.tenant_id)
    if app.status not in ("provisioning_failed", "provisioning"):
        raise HTTPException(status_code=409, detail="app is not in a failed state")

    try:
        svc_user = ensure_app_service_user(db, app)
    except Exception as e:  # noqa: BLE001
        app.status = "provisioning_failed"
        app.failure_detail = {"step": "service_user", "error": str(e)}
        db.commit()
        raise internal_http_error(502, "App service user provisioning failed", e) from e

    try:
        kc = keycloak_admin_client.ensure_app_client(app)
        app.kc_client_id = kc["client_id"]
        app.kc_client_uuid = kc.get("uuid")
        db.commit()
    except Exception as e:  # noqa: BLE001
        app.status = "provisioning_failed"
        app.failure_detail = {"step": "kc_client", "error": str(e)}
        db.commit()
        raise internal_http_error(502, "Keycloak client provisioning failed", e) from e

    has_key = False
    if app.api_key_id is not None:
        k = db.get(models.ApiKey, app.api_key_id)
        has_key = k is not None and k.deleted_at is None
    if not has_key:
        try:
            key, _raw = _mint_app_key(db, app, svc_user.id)
            app.api_key_id = key.id
            db.commit()
        except Exception as e:  # noqa: BLE001
            app.status = "provisioning_failed"
            app.failure_detail = {"step": "api_key", "error": str(e)}
            db.commit()
            raise internal_http_error(502, "API key provisioning failed", e) from e

    app.status = "active"
    app.failure_detail = None
    db.commit()
    _audit(db, app, current_user.id, "APP_PROVISION_ROUTE", {"hostname": app.hostname})
    app_routing.trigger_reconcile_async()
    return _app_public(app, db=db)


@router.put("/{app_id}/scope", response_model=AppOut)
def update_scope(
    app_id: int,
    payload: AppUpdateScope,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    app = _get_app(db, app_id, current_user.tenant_id)
    old = {
        "allowed_workflow_ids": list(app.allowed_workflow_ids or []),
        "allowed_folder_ids": list(app.allowed_folder_ids or []),
    }
    app.allowed_workflow_ids = list(payload.allowed_workflow_ids)
    app.allowed_folder_ids = list(payload.allowed_folder_ids)
    db.commit()
    _audit(db, app, current_user.id, "APP_SCOPE_UPDATED",
           {"old": old, "new": payload.model_dump()})
    return _app_public(app, db=db)


@router.delete("/{app_id}", response_model=AppRevokeOut, status_code=status.HTTP_200_OK)
def revoke_app(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_tenant_admin),
):
    """Revoke (§2.2): soft-delete the app + its service user, disable its KC
    client, kill its key, drop the proxy route. ISO 27001 — nothing is
    hard-deleted."""
    app = _get_app(db, app_id, current_user.tenant_id)

    # 1. Soft-delete the app row (kept, auditable).
    app.status = "revoked"
    app.deleted_at = datetime.now(timezone.utc)
    db.commit()

    # 2. Disable (NOT delete) the KC OIDC client.
    if app.kc_client_id:
        try:
            keycloak_admin_client.disable_app_client(app.kc_client_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to disable app KC client: %s", e)

    # 3. Kill the app's API key (soft-delete + KC service account disabled).
    _revoke_app_key(db, app)

    # 4. Soft-delete the app's service user (ISO: row kept).
    try:
        revoke_app_service_user(db, app)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to revoke app service user: %s", e)

    _audit(db, app, current_user.id, "APP_REVOKED",
           {"slug": app.slug, "hostname": app.hostname})

    # 5. Drop the proxy site (reconcile pass, own thread/session).
    app_routing.trigger_reconcile_async()
    return {"status": "revoked", "id": app.id}
