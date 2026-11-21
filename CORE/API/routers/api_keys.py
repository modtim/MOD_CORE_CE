# In API/routers/api_keys.py
"""
API Keys Router with Keycloak Service Account Integration.

CMMC/ISO 27001 Compliance:
- API keys are mapped to Keycloak service accounts (OAuth 2.0 Client Credentials)
- Access tokens are short-lived JWTs (default 1 hour)
- Full audit trail via Keycloak events
- Automatic expiration enforcement
- Encrypted storage of service account secrets
"""

import secrets
import logging
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session

import crud, schemas, models
from dependencies import get_db
from security import hash_api_key, verify_api_key
from dependencies import require_active_and_onboarded_user, require_fresh_auth
from keycloak_auth import keycloak_admin_client
from utils.encryption import encrypt_value, decrypt_value, ENCRYPTION_KEY
# GSEC FX.152 (Q139=B): scannable key format (part 1) + rotation grace window
# (part 2). Both are services modules, so the router stays thin. Both are
# Paid-only (manifest-denied), so they come in through the CE guard (1634d): on
# the CE carve they are None and every use site below falls back to the legacy
# rule instead of raising.
from utils.paid_import import optional_paid
api_key_format = optional_paid("services.api_key_format")
api_key_grace = optional_paid("services.api_key_grace")
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["API Keys"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)

# Public router for token exchange (no auth required - uses API key)
public_router = APIRouter(
    tags=["API Keys"],
    responses={404: {"description": "Not found"}},
)


def generate_keycloak_client_id(user_id: int, key_prefix: str) -> str:
    """Generate a unique Keycloak client ID for the API key."""
    return f"api-key-{user_id}-{key_prefix}"


def mint_key_pair() -> tuple:
    """Return (key value, stored key_prefix) for a freshly minted key.

    GSEC FX.152 (Q139=B) scannable format when the Paid module is present; on
    the CE carve (1634d) `services.api_key_format` is absent, so this falls
    back to the pre-FX.152 legacy rule — `sk_<token>` and the first 8
    characters — which is what `crud.get_api_key_by_key_prefix` matches there.
    """
    if api_key_format is None:
        value = f"sk_{secrets.token_urlsafe(32)}"
        return value, value[:8]
    value = api_key_format.new_api_key()
    return value, api_key_format.stored_key_prefix(value)


def _stack_key_error(exc) -> HTTPException:
    """30.6.3 tok-merge (J2) — the code-carrying 422 for a bad stack-key
    request shape (same shape as the old route's `_err`)."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"detail": exc.message, "code": exc.code})


def _mint_stack_pinned_key(db: Session, current_user: models.User,
                           api_key_in, scopes) -> dict:
    """30.6.3 tok-merge (J2) — mint a stack-pinned key (formerly deploy token).

    Reached ONLY from POST /api-keys/ with a `stack_id` + `stack:*` scopes
    (the request shape was already checked by
    `services/app_key_scope.validate_stack_key_request`). The old per-stack
    guards/limits from `services/stack_deploy_token.py:mint` are preserved
    verbatim: the four `stacks.deploy_token_*` settings (read through
    `services.platform_settings.get_setting`, never env), the tenant-scoped
    404 stack lookup, the STACK_AUTHOR capability, the S15 principal check,
    the ttl default/max, the per-stack limit and the 1-255 label rule.

    NO Keycloak service account is created (MUST): the row keeps
    keycloak_client_id / keycloak_client_uuid / encrypted_keycloak_secret NULL
    — identical to the rows the old route minted — which is what makes
    POST /api-keys/token refuse the key. A Keycloak client would let the key
    trade up to a JWT that is not pin-confined.
    """
    from services import platform_settings as psr
    from services.persistent_stacks_shared_service import get_owned_stack
    from services.app_key_scope import is_stack_pinned_key
    from utils.request_principal import get_api_key
    import permissions
    from models import AuditEvent

    def _err(code_status: int, msg: str, code: str):
        """The project's code-carrying error shape (services/stack_deploy_token.py:49)."""
        return HTTPException(status_code=code_status,
                             detail={"detail": msg, "code": code})

    if not psr.get_setting("stacks.deploy_token_enabled", db=db):
        raise _err(status.HTTP_403_FORBIDDEN,
                   "deploy tokens are disabled", "deploy_tokens_disabled")

    # Tenant isolation: the SAME guard the old route used — unknown OR
    # cross-tenant stack is 404 (existence is never leaked).
    stack = get_owned_stack(db, api_key_in.stack_id, current_user)
    if getattr(stack, "status", None) == "torn_down":
        raise _err(status.HTTP_404_NOT_FOUND,
                   "Persistent stack not found", "stack_not_found")

    # NEW and MANDATORY: the general key route has no router-wide STACK_AUTHOR
    # gate (the old /stacks/{id}/deploy-tokens router did), so the minting
    # caller must hold the stack-author capability — otherwise any user could
    # mint a deploy credential for a stack they cannot deploy.
    if not permissions.check_user_platform_capability(
            db, current_user.id, permissions.STACK_AUTHOR):
        raise _err(status.HTTP_403_FORBIDDEN,
                   "user does not hold the stack:author capability",
                   "stack_author_required")

    # S15: a request authorised by an API key may not mint a stack-pinned key
    # — human/bearer only. A stack-pinned principal gets the old code
    # `deploy_token_scope_denied`; ANY api-key principal (app-bound included)
    # is refused too, so a key can never trade up a sibling deploy credential.
    principal_key = get_api_key()
    if principal_key is not None:
        if is_stack_pinned_key(principal_key) or getattr(
                principal_key, "app_id", None) is not None:
            raise _err(status.HTTP_403_FORBIDDEN,
                       "deploy tokens cannot manage deploy tokens",
                       "deploy_token_scope_denied")
        raise _err(status.HTTP_403_FORBIDDEN,
                   "an API key cannot mint a stack-pinned key",
                   "api_key_principal_denied")

    # Expiry: the stack-key rule REPLACES the generic credential_lifetime
    # default for stack keys only (non-stack keys keep today's path below).
    now = datetime.now(timezone.utc)
    ttl_default = int(psr.get_setting(
        "stacks.deploy_token_ttl_days_default", db=db))
    ttl_max = int(psr.get_setting("stacks.deploy_token_ttl_days_max", db=db))
    if api_key_in.expires_at is None:
        expires_at = now + timedelta(days=ttl_default)
    else:
        expires_at = api_key_in.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now or expires_at > now + timedelta(days=ttl_max):
            raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY,
                       f"expires_at must be within {ttl_max} days from now",
                       "invalid_ttl")

    # Per-stack limit: live (not soft-deleted) pinned rows only.
    # The exact filter services/stack_deploy_token.py:_live_tokens used (no
    # crud helper exists for a per-stack count).
    # r5-lock-ok: count-only probe of the caller's own pin column.
    live = db.query(models.ApiKey).filter(
        models.ApiKey.deploy_token_stack_id == stack.id,
        models.ApiKey.deleted_at.is_(None)).count()
    # FIX-CE-UNLIMITED-A: the CE default for stacks.deploy_token_max_per_stack
    # is the sentinel 0 = unlimited (CE has no quantity caps).
    # `max_per_stack > 0` is the smallest guard: a no-op for any finite limit,
    # so Paid (5) and any explicit operator row enforce exactly as before.
    max_per_stack = int(psr.get_setting(
        "stacks.deploy_token_max_per_stack", db=db))
    if max_per_stack > 0 and live >= max_per_stack:
        raise _err(status.HTTP_409_CONFLICT,
                   "deploy token limit reached for this stack",
                   "deploy_token_limit")

    label = api_key_in.description
    if not isinstance(label, str) or not (1 <= len(label) <= 255):
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "label must be 1-255 characters", "invalid_label")

    # Generation + hashing EXACTLY as the ordinary mint: mint_key_pair() (the
    # CE fallback inside it included) + security.hash_api_key. No new format.
    new_key_value, key_prefix = mint_key_pair()
    hashed_key = hash_api_key(new_key_value)

    db_api_key = models.ApiKey()
    db_api_key.user_id = current_user.id      # tenant = the minting human
    db_api_key.key_prefix = key_prefix
    db_api_key.hashed_key = hashed_key
    db_api_key.description = label
    db_api_key.expires_at = expires_at
    db_api_key.app_id = None                  # a key is never both
    db_api_key.deploy_token_stack_id = stack.id   # the pin (validated above)
    db_api_key.capability_scope = list(scopes)    # the exact stack:* strings
    db_api_key.is_read_only = False               # a stack key mutates: forced
    db.add(db_api_key)
    db.commit()
    db.refresh(db_api_key)

    # Audits: key material NEVER appears (prefix only) — the ordinary create
    # event plus the stack-key event the old mint wrote.
    try:
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="AUTH_API_KEY_CREATED",
            details={"key_prefix": key_prefix,
                     "description": label},
        ))
        db.commit()
    except Exception:
        pass
    try:
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="STACK_DEPLOY_TOKEN_CREATED",
            details={"api_key_id": db_api_key.id, "stack_id": stack.id,
                     "key_prefix": key_prefix, "scopes": list(scopes),
                     "expires_at": expires_at.isoformat()}))
        db.commit()
    except Exception:  # audit never blocks the mint
        pass

    logger.info(
        f"Created stack-pinned API key {key_prefix}... for user "
        f"{current_user.id} pinned to stack {stack.id}")

    return {
        "id": db_api_key.id,
        "description": db_api_key.description,
        "key_prefix": db_api_key.key_prefix,
        "created_at": db_api_key.created_at,
        "last_used_at": db_api_key.last_used_at,
        "access_key": new_key_value,
        "stack_id": stack.id,
        "stack_name": stack.name,
    }


@router.post("/", response_model=schemas.NewApiKey, status_code=status.HTTP_201_CREATED)
def create_api_key_endpoint(
    api_key_in: schemas.ApiKeyCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    fresh: models.User = Depends(require_fresh_auth),  # GY.C4b re-auth
):
    """
    Generate a new API key with Keycloak service account integration.

    The API key can be exchanged for short-lived OAuth tokens via POST /api-keys/token.

    CMMC/ISO 27001 Compliance:
    - Creates a Keycloak service account for OAuth 2.0 Client Credentials flow
    - Access tokens are short-lived (1 hour default)
    - Full audit trail via Keycloak events
    """
    try:
        # 30.6.3 tok-merge (J2) — a stack-pinned key (formerly deploy token) is
        # minted through the ORDINARY key route: the body gains the optional
        # `stack_id` and the scopes carry `stack:*`. The request shape is
        # checked FIRST (before any Keycloak work) by the CONTRACT validator in
        # services/app_key_scope; a bad shape is the code-carrying 422. A
        # request with neither stack_id nor a stack:* scope keeps today's path
        # byte-for-byte below.
        from services.app_key_scope import (
            validate_stack_key_request, StackKeyRequestError)
        try:
            scopes = validate_stack_key_request(
                api_key_in.capability_scope, api_key_in.stack_id)
        except StackKeyRequestError as e:
            raise _stack_key_error(e)
        if api_key_in.stack_id is not None:
            # `validate_stack_key_request` returns None when no scopes were
            # given at all; a stack_id with no stack:* scope is the same
            # refusal the CONTRACT names (a pinned key carries ONLY stack:*).
            if not scopes:
                raise _stack_key_error(
                    StackKeyRequestError("stack_id_requires_stack_scope"))
            return _mint_stack_pinned_key(db, current_user, api_key_in, scopes)

        # GY.C3a — platform TTL ceiling (auth.api_key_max_ttl_days > 0).
        from utils.paid_import import optional_paid; _sa_inventory = optional_paid("services.service_account_inventory")  # 2400LR-g6a CE carve: service_account_inventory is denied in CE; absent -> no platform TTL ceiling, the client's expiry is kept (pre-GY.C3a posture). SaaS/Pro unchanged.
        if _sa_inventory is not None:
            _sa_inventory.enforce_api_key_ttl(db, api_key_in.expires_at)

        # GSEC FX.56 p1 (Q39=A) — finite lifetime: a missing expiry takes
        # credentials.default_lifetime_days (90); over max is refused with
        # 422 CREDENTIAL_LIFETIME_TOO_LONG (gov profiles cap the max at
        # credentials.gov_max_lifetime_days).
        from utils.paid_import import optional_paid; cred_lifetime = optional_paid("services.credential_lifetime")  # 1788b CE carve: credential_lifetime is paid; absent in CE -> the key keeps exactly the client's expiry (None = no expiry), the pre-FX.56 posture.
        api_key_in.expires_at = cred_lifetime.resolve_expiry_from_datetime(
            db, current_user.tenant_id, api_key_in.expires_at) if cred_lifetime else api_key_in.expires_at

        # GSEC FX.152 (Q139=B) — scannable format + CRC32; CE falls back to the
        # legacy `sk_` rule inside mint_key_pair().
        new_key_value, key_prefix = mint_key_pair()
        hashed_key = hash_api_key(new_key_value)

        keycloak_client_id = generate_keycloak_client_id(current_user.id, key_prefix)

        try:
            keycloak_result = keycloak_admin_client.create_service_account_client(
                client_id=keycloak_client_id,
                description=api_key_in.description or "API Key",
                user_id=current_user.id,
                tenant_id=current_user.tenant_id,
            )
        except Exception as e:
            logger.error(f"Failed to create Keycloak service account: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create service account"
            )

        encrypted_secret = encrypt_value(
            keycloak_result["keycloak_client_secret"],
            ENCRYPTION_KEY
        )

        db_api_key = models.ApiKey()
        db_api_key.user_id = current_user.id
        db_api_key.key_prefix = key_prefix
        db_api_key.hashed_key = hashed_key
        db_api_key.description = api_key_in.description
        db_api_key.expires_at = api_key_in.expires_at
        db_api_key.keycloak_client_id = keycloak_result["keycloak_client_id"]
        db_api_key.keycloak_client_uuid = keycloak_result["keycloak_client_uuid"]
        db_api_key.encrypted_keycloak_secret = encrypted_secret
        # Least-privilege scoping (Gate 34/35): persist the capability
        # allowlist / read-only flag so a key can be minted limited.
        if api_key_in.capability_scope is not None:
            db_api_key.capability_scope = api_key_in.capability_scope
        if api_key_in.is_read_only is not None:
            db_api_key.is_read_only = api_key_in.is_read_only
        db.add(db_api_key)
        db.commit()
        db.refresh(db_api_key)

        # Audit: API key creation (CMMC AU-2)
        try:
            from models import AuditEvent
            db.add(AuditEvent(
                tenant_id=current_user.tenant_id, user_id=current_user.id,
                event_name="AUTH_API_KEY_CREATED",
                details={"key_prefix": key_prefix, "description": api_key_in.description},
            ))
            db.commit()
        except Exception:
            pass

        try:
            from resource_permissions import grant_owner_permissions
            grant_owner_permissions(db, current_user.tenant_id, current_user.id, models.ResourceType.API_KEY, db_api_key.id)
            db.commit()
        except Exception:
            pass

        # FX.112 (Q99=A) security alert — prefix only, never the key value.
        # FX.162.p5e — the CE carve strips services/account_security_alerts, so
        # the alert is skipped there (the mint itself is unchanged).
        try:
            from utils.paid_import import optional_paid
            asa = optional_paid("services.account_security_alerts")
            if asa is not None:
                asa.notify_api_key_created(
                    db, current_user, key_prefix=key_prefix,
                    description=api_key_in.description)
        except Exception as e:  # noqa: BLE001 — alert never blocks the mint
            logger.warning("FX.112 api-key-created alert failed: %s", e)

        logger.info(
            f"Created API key {key_prefix}... for user {current_user.id} "
            f"with Keycloak client {keycloak_client_id}"
        )

        return {
            "id": db_api_key.id,
            "description": db_api_key.description,
            "key_prefix": db_api_key.key_prefix,
            "created_at": db_api_key.created_at,
            "last_used_at": db_api_key.last_used_at,
            "access_key": new_key_value
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating API key: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


class ApiKeyRotateResponse(schemas.NewApiKey):
    """GSEC FX.152 part 2 (Q139=B) — the create response shape plus the
    rotation bookkeeping (the new key is shown exactly once)."""
    old_key_id: int
    grace_until: Optional[datetime] = None


def _rotate_authorize(db: Session, current_user: models.User,
                      old_key: models.ApiKey) -> None:
    """GSEC FX.152 part 2 (Q139=B) — who may rotate a key.

    The key owner always may (the existing GET/DELETE routes are owner-scoped
    through `crud.get_user_api_key`), and a tenant admin may rotate any key in
    their own tenant (the shared `_is_tenant_admin` idiom from
    routers/assistant.py). A key in another tenant is 404, not 403 — existence
    is not leaked across tenants.
    """
    owner = db.get(models.User, old_key.user_id)
    if owner is None or owner.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="API Key not found")
    if current_user.id == old_key.user_id:
        return
    from utils.paid_import import optional_paid; _assistant = optional_paid("routers.assistant")  # 1788b CE carve: routers/assistant is paid; the tenant-root-admin test is the same `parent_user_id IS NULL` idiom routers/tenant.py uses, so rotation authority is identical in CE.
    _is_tenant_admin = _assistant._is_tenant_admin if _assistant else (lambda u: bool(getattr(u, "is_platform_admin", False) or getattr(u, "parent_user_id", None) is None))
    if _is_tenant_admin(current_user): return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the key owner or a tenant admin can rotate this API key.",
    )


@router.post("/{api_key_id}/rotate", response_model=ApiKeyRotateResponse,
            status_code=status.HTTP_201_CREATED)
def rotate_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    fresh: models.User = Depends(require_fresh_auth),  # GY.C4b re-auth
):
    """GSEC FX.152 part 2 (owner decision Q139=B) — rotate an API key.

    Mints a new key in the FX.152 scannable format, copies the old key's
    description / scopes and expiry policy (the new expiry follows the tenant
    max-lifetime policy when the old key has no expiry), and swaps the OLD key
    for the new one in ONE transaction (BUG.B44): the old row is soft-revoked
    (`deleted_at` — never hard-deleted, ISO 27001) so exactly ONE active key
    remains for that description, and it is hidden from GET /api-keys/.

    BUG.B44: before this fix rotate left the old row live in the active list
    (only `rotated_at`/`grace_until` were stamped), so the Dashboard showed the
    same description twice and the UI's token button kept the pre-rotate
    plaintext. The rotation bookkeeping (`rotated_at`, `grace_until`,
    `rotates_to_key_id`) is still stamped so the successor chain and the
    grace-window machinery (`services/api_key_grace.py`, still used by the
    service-account rotation path) stay intact.

    ISO 27001: the old row is never deleted. Both events are audited —
    `API_KEY_ROTATED` here (old key id + new key id), and
    `API_KEY_GRACE_EXPIRED` once at the post-grace rejection.

    The new key value is returned exactly once, in the same shape as POST /.
    """
    old_key = db.get(models.ApiKey, api_key_id)
    if old_key is None or old_key.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="API Key not found")
    _rotate_authorize(db, current_user, old_key)

    if old_key.disabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="API key is disabled; rotate only live keys.")

    # 30.6.3 tok-merge (J2) — a stack-pinned key (formerly deploy token) is
    # NOT rotatable: rotate copies is_read_only/capability_scope/app_id but
    # never the pin, so silently rotating would DROP the pin and mint a
    # Keycloak client for a deploy credential (both forbidden). Refuse instead
    # — revoke and create a new stack key.
    from services.app_key_scope import is_stack_pinned_key
    if is_stack_pinned_key(old_key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": "revoke and create a new stack key",
                    "code": "stack_key_not_rotatable"})

    now = datetime.now(timezone.utc)

    # Expiry policy: keep the old key's expiry when it is still in the future,
    # otherwise resolve it through the tenant max-lifetime policy (the same
    # rule FX.114's rotate path uses).
    # GSEC FX.56 p1b (Q39=A) — finite lifetime: a missing expiry takes the
    # platform default (credentials.default_lifetime_days, 90); an expiry
    # beyond the effective max is refused with 422
    # CREDENTIAL_LIFETIME_TOO_LONG.
    from utils.paid_import import optional_paid; cred_lifetime = optional_paid("services.credential_lifetime")  # 1788b CE carve: credential_lifetime is paid; absent in CE -> the rotated key keeps the old key's expiry as-is (None = no expiry), no platform default/max-lifetime policy.
    new_expires = cred_lifetime.resolve_expiry_from_datetime(
        db, current_user.tenant_id, old_key.expires_at, now=now) if cred_lifetime else old_key.expires_at

    new_key_value, key_prefix = mint_key_pair()
    hashed_key = hash_api_key(new_key_value)
    keycloak_client_id = generate_keycloak_client_id(old_key.user_id, key_prefix)

    try:
        keycloak_result = keycloak_admin_client.create_service_account_client(
            client_id=keycloak_client_id,
            description=old_key.description or "API Key",
            user_id=old_key.user_id,
            tenant_id=current_user.tenant_id,
        )
    except Exception:
        logger.error("Failed to create Keycloak service account for rotation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create service account")

    encrypted_secret = encrypt_value(keycloak_result["keycloak_client_secret"],
                                     ENCRYPTION_KEY)

    db_api_key = models.ApiKey()
    db_api_key.user_id = old_key.user_id
    db_api_key.key_prefix = key_prefix
    db_api_key.hashed_key = hashed_key
    db_api_key.description = old_key.description
    db_api_key.expires_at = new_expires
    db_api_key.keycloak_client_id = keycloak_result["keycloak_client_id"]
    db_api_key.keycloak_client_uuid = keycloak_result["keycloak_client_uuid"]
    db_api_key.encrypted_keycloak_secret = encrypted_secret
    # Scopes are the key's, not the rotation's — copying them is what keeps the
    # caller's least-privilege grant (Gate 34/35) after the swap.
    db_api_key.is_read_only = bool(getattr(old_key, "is_read_only", False))
    db_api_key.capability_scope = getattr(old_key, "capability_scope", None)
    db_api_key.app_id = getattr(old_key, "app_id", None)
    db_api_key.service_account_id = getattr(old_key, "service_account_id", None)

    # 1634d: `services.api_key_grace` is Paid-only, so on the CE carve there is
    # no grace machinery to consult — nothing is stamped (the old row is still
    # soft-revoked below); the paid path keeps the tenant window.
    grace_until = None if api_key_grace is None else now + timedelta(
        hours=api_key_grace.grace_hours(db, current_user.tenant_id))

    # BUG.B44 — the swap is ONE transaction. `flush()` assigns the new row's
    # id without committing, so the old row can be pointed at its successor
    # and soft-revoked (`deleted_at` — never hard-deleted, ISO 27001) in the
    # same commit. Before this, rotate left the old row live in the active
    # list, so the Dashboard showed the same description twice.
    db.add(db_api_key)
    db.flush()
    old_key.rotated_at = now
    old_key.grace_until = grace_until
    old_key.rotates_to_key_id = db_api_key.id
    old_key.deleted_at = now
    db.commit()
    db.refresh(db_api_key)

    # Audit: the rotation itself (CMMC AU-2). Old + new key ids only — never
    # key material.
    try:
        import audit
        audit.log_admin_action(
            db, models.AuditAction.API_KEY_ROTATED,
            admin_user_id=current_user.id,
            target_tenant_id=current_user.tenant_id,
            target_user_id=old_key.user_id,
            resource_type="api_key", resource_id=db_api_key.id,
            metadata={"event": "api_key_rotated",
                      "old_key_id": old_key.id, "new_key_id": db_api_key.id,
                      "rotated_at": str(now),
                      "grace_until": str(grace_until)})
    except Exception as e:  # noqa: BLE001 — audit failure never blocks rotation
        logger.warning("FX.152 rotate audit row failed: %s", e)

    try:
        from resource_permissions import grant_owner_permissions
        grant_owner_permissions(db, current_user.tenant_id, old_key.user_id,
                                models.ResourceType.API_KEY, db_api_key.id)
        db.commit()
    except Exception:
        pass

    logger.info(
        f"Rotated API key {old_key.key_prefix}... -> {key_prefix}... for user "
        f"{old_key.user_id}; old key valid until {grace_until}")

    return {
        "id": db_api_key.id,
        "description": db_api_key.description,
        "key_prefix": db_api_key.key_prefix,
        "created_at": db_api_key.created_at,
        "last_used_at": db_api_key.last_used_at,
        "access_key": new_key_value,
        "old_key_id": old_key.id,
        "grace_until": grace_until,
    }


@router.get("/", response_model=List[schemas.ApiKey])
def read_api_keys(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100)
):
    """
    Retrieve active API keys for the current user with pagination.

    30.6.3 tok-merge (J2): a stack-pinned key (formerly deploy token) row also
    carries `stack_id` (the row column `deploy_token_stack_id`) and `stack_name`.
    The names resolve in ONE `IN` query on `PersistentStack.id` restricted to
    the caller's tenant — no N+1, and a name outside the tenant is never
    returned (the id is still shown).
    """
    rows = crud.get_api_keys_for_user(
        db=db, user_id=current_user.id, skip=skip, limit=limit
    )

    def _pin_of(row):
        pin = getattr(row, "deploy_token_stack_id", None)
        if isinstance(pin, bool) or not isinstance(pin, int):
            return None
        return pin

    pins = sorted({_pin_of(row) for row in rows if _pin_of(row) is not None})
    names: dict = {}
    if pins:
        # ONE tenant-scoped IN query for the stack names (the brief's no-N+1
        # requirement; no crud helper takes an id list).
        # r5-lock-ok: tenant-scoped IN query for the stack names; no N+1.
        for st in db.query(models.PersistentStack).filter(
                models.PersistentStack.id.in_(pins),
                models.PersistentStack.tenant_id == current_user.tenant_id
        ).all():
            names[st.id] = st.name
    for row in rows:
        pin = _pin_of(row)
        row.stack_id = pin
        row.stack_name = names.get(pin)
    return rows


@router.delete("/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Revoke an active API key for the current user.

    This also deletes the associated Keycloak service account,
    immediately invalidating any tokens that were issued.
    """
    db_api_key = crud.get_user_api_key(db=db, api_key_id=api_key_id, user_id=current_user.id)

    if not db_api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Key not found")

    # Delete the Keycloak service account
    if db_api_key.keycloak_client_uuid:
        try:
            keycloak_admin_client.delete_service_account_client(db_api_key.keycloak_client_uuid)
            logger.info(f"Deleted Keycloak client {db_api_key.keycloak_client_id}")
        except Exception as e:
            # Log but don't fail - the key should still be revoked
            logger.warning(f"Failed to delete Keycloak client: {e}")

    # Soft delete the API key
    crud.revoke_api_key(db=db, db_api_key=db_api_key)
    # FX.112 PART 1 (Q99=A) — security alert on key revocation (prefix
    # only; the key is already unusable by the time this runs).
    try:
        # FX.162.p5e — CE carve: no alert module -> no alert (revoke unchanged).
        from utils.paid_import import optional_paid
        asa = optional_paid("services.account_security_alerts")
        if asa is not None:
            asa.notify_api_key_revoked(
                db, current_user, key_prefix=db_api_key.key_prefix)
    except Exception as e:  # noqa: BLE001 — alert never blocks the revoke
        logger.warning("FX.112 api-key-revoked alert failed: %s", e)
    logger.info(f"Revoked API key {db_api_key.key_prefix}... for user {current_user.id}")

    return None


# =============================================================================
# Public Token Exchange Endpoint (No Auth Required)
# =============================================================================

def _api_key_token_rate_limit(http_request: Request, db: Session) -> None:
    """GSEC R-020 — throttle the public token exchange per client IP.

    The limit is a platform setting (``auth.api_key_token_rate_per_minute``,
    default 30), read through the shared settings registry. Uses the SAME
    shared Redis-backed rate limiter as the rest of the API
    (``utils.rate_limiter.check_rate_limit``) — no new mechanism. Raises 429
    when the per-minute budget is exhausted. Fails OPEN (never blocks) if
    Redis/the limiter is unavailable, matching the rest of the limiter.
    """
    from utils.rate_limiter import check_rate_limit
    limit = 30
    try:
        from services import platform_settings
        limit = platform_settings.get_setting(
            "auth.api_key_token_rate_per_minute", db=db)
        limit = int(limit)
        if limit < 1:
            limit = 1
    except Exception:
        # A broken setting read (or an unexpected db/session shape) must never
        # block the endpoint — fall back to the registered default of 30/min.
        limit = 30
    # 1788b CE carve: services/client_ip is paid; the KEPT utils/client_ip
    # resolver is the same trust model, so the per-IP token rate limit still
    # keys on the real client in CE (never a single shared "unknown" bucket).
    from utils.rate_limiter import _rate_limit_client_ip; client_ip = _rate_limit_client_ip(http_request)
    check_rate_limit(f"apikeytoken:{client_ip}", limit, 60)


@public_router.post("/token", response_model=schemas.ApiKeyTokenResponse)
def exchange_api_key_for_token(
    request: Request,
    body: schemas.ApiKeyTokenRequest,
    db: Session = Depends(get_db),
):
    """
    Exchange an API key for a short-lived OAuth 2.0 access token.

    This is the CMMC/ISO 27001 compliant way to use API keys:
    1. Client sends API key to this endpoint
    2. Server validates API key and returns a short-lived JWT
    3. Client uses JWT for subsequent API calls
    4. JWT expires after 1 hour, client must request new token

    Security Benefits:
    - Access tokens are short-lived (1 hour default)
    - Tokens are standard OAuth 2.0 JWTs validated by Keycloak
    - Full audit trail via Keycloak events
    - API key never sent to other endpoints
    - GSEC R-020: throttled per IP; key expiry/status enforced via
      dependencies._check_api_key_policy (same rule as the auth funnels); the
      minted response carries the source key's read_only + capability scope.
    """
    # GSEC R-020: throttle BEFORE doing any key work (per client IP).
    _api_key_token_rate_limit(request, db)

    api_key = body.api_key

    # Validate API key format
    if not api_key or len(api_key) < 8:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format"
        )

    key_prefix = api_key[:8]
    db_api_key = crud.get_api_key_by_key_prefix(db, api_key)  # FX.152: full key, both formats

    if not db_api_key:
        logger.warning(f"API key not found: {key_prefix}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # Verify the full key hash
    if not verify_api_key(api_key, db_api_key.hashed_key):
        logger.warning(f"API key hash mismatch: {key_prefix}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # GSEC R-020: enforce key expiry/status through the SAME post-resolution
    # rule the auth funnels use (_enforce_api_key_policy's expiry half,
    # _check_api_key_policy). Raises 401 (+ AUTH_API_KEY_EXPIRED audit row)
    # for an expired key. This supersedes the previous inline expiry check.
    from dependencies import _check_api_key_expiry
    _check_api_key_expiry(db, db_api_key)

    # Defense in depth (ISO 27001 A.9.2.6): never mint a token for a key whose
    # owner is deactivated. Deactivation already revokes the key rows, but this
    # guard also blocks the mint if a key survived by any path. Checks for an
    # explicit non-active status only (so a resolvable-but-active owner is
    # unaffected).
    owner = crud.get_user(db, user_id=db_api_key.user_id)
    if owner is not None and getattr(owner, "status", None) in (
        models.UserStatus.DEACTIVATED,
        models.UserStatus.INACTIVE,
    ):
        logger.warning(f"API key owner not active (user={db_api_key.user_id}): {key_prefix}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # Check if the key has Keycloak integration
    if not db_api_key.keycloak_client_id or not db_api_key.encrypted_keycloak_secret:
        logger.error(f"API key missing Keycloak integration: {key_prefix}...")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key is not configured for token exchange. Please create a new API key."
        )

    # Decrypt the Keycloak client secret
    try:
        client_secret = decrypt_value(db_api_key.encrypted_keycloak_secret, ENCRYPTION_KEY)
    except Exception as e:
        logger.error("Failed to decrypt Keycloak secret (%s)", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process API key"
        )

    # Exchange for OAuth token via Keycloak
    try:
        token_response = keycloak_admin_client.get_service_account_token(
            client_id=db_api_key.keycloak_client_id,
            client_secret=client_secret,
        )
    except Exception as e:
        logger.error("Keycloak token exchange failed (%s: %s)",
                     type(e).__name__,
                     getattr(getattr(e, "response", None), "status_code", None))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate access token"
        )

    # Update last used timestamp
    db_api_key.last_used_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(f"Token issued for API key {key_prefix}... (user {db_api_key.user_id})")

    # GSEC R-020: carry the source key's read_only flag + capability scope into
    # the minted token's claims. Keycloak's client-credentials token does not
    # itself embed these, so they are surfaced on the response for the caller
    # (and any token consumer) to enforce. The X-API-Key consumer path
    # (resource_permissions.authorize → _api_key_scope_allows, and the
    # require_*_key dependencies) already honours is_read_only/capability_scope
    # — see the consumer-gap note in the FX.2 report.
    return schemas.ApiKeyTokenResponse(
        access_token=token_response["access_token"],
        token_type=token_response.get("token_type", "Bearer"),
        expires_in=token_response.get("expires_in", 3600),
        scope=token_response.get("scope"),
        is_read_only=bool(getattr(db_api_key, "is_read_only", False)),
        capability_scope=getattr(db_api_key, "capability_scope", None),
    )
