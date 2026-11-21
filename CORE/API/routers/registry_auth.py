"""
Registry Token Auth — Keycloak-backed auth for the local container registry.

registry:2 delegates auth here. When a Docker client (worker or API) tries
to pull/push, the registry redirects to this endpoint. We:
1. Validate the caller's Keycloak token (or API key for service account)
2. Parse the requested scope (repository path + actions)
3. Enforce tenant isolation (tenant-1 can only access tenant-1/*)
4. Return a short-lived JWT signed with the registry auth key

The registry validates the JWT signature against the cert we gave it at
startup (registry-certs/auth.crt).
"""

import os
import time
import json
import base64
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Query
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509 import load_pem_x509_certificate
from jose import jwt as jose_jwt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Registry Auth"])

# Load the signing key at import time
_REGISTRY_KEY_PATH = os.getenv("REGISTRY_AUTH_KEY_PATH", "/run/registry-certs/auth.key")
_REGISTRY_CERT_PATH = os.getenv("REGISTRY_AUTH_CERT_PATH", "/run/registry-certs/auth.crt")
_ISSUER = "mod-registry-auth"
_TOKEN_TTL = 300  # 5 minutes

_signing_key = None
_key_id = None


def _load_signing_key():
    global _signing_key, _key_id
    if _signing_key is not None:
        return

    try:
        with open(_REGISTRY_KEY_PATH, "rb") as f:
            _signing_key = serialization.load_pem_private_key(f.read(), password=None)

        # Key ID = base32 of first 30 bytes of cert fingerprint (Docker convention)
        with open(_REGISTRY_CERT_PATH, "rb") as f:
            cert = load_pem_x509_certificate(f.read())
            fingerprint = cert.fingerprint(hashes.SHA256())
            _key_id = base64.b32encode(fingerprint[:30]).decode("ascii")[:12]

        logger.info("Registry auth key loaded (kid=%s)", _key_id)
    except FileNotFoundError:
        logger.warning("Registry auth key not found at %s — registry auth disabled", _REGISTRY_KEY_PATH)
    except Exception as e:
        logger.warning("Failed to load registry auth key: %s", e)


def _parse_scope(scope: str):
    """
    Parse Docker registry scope string.
    Format: "repository:tenant-1/my-image:pull" or "repository:tenant-1/my-image:push,pull"
    Returns (resource_type, repository, actions) or (None, None, [])
    """
    if not scope:
        return None, None, []

    parts = scope.split(":")
    if len(parts) < 3:
        return None, None, []

    resource_type = parts[0]  # "repository"
    repository = parts[1]  # "tenant-1/my-image"
    actions = parts[2].split(",")  # ["pull"] or ["push", "pull"]

    return resource_type, repository, actions


def _extract_tenant_from_repo(repository: str) -> Optional[int]:
    """Extract tenant ID from repository path like 'tenant-1/my-image'."""
    if not repository:
        return None
    first_segment = repository.split("/")[0]
    if first_segment.startswith("tenant-"):
        try:
            return int(first_segment.split("-", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def _validate_keycloak_token(token: str) -> Optional[dict]:
    """Validate a Keycloak token and return claims. Returns None if invalid."""
    try:
        from keycloak_auth import get_current_user_claims
        return get_current_user_claims(token)
    except Exception:
        return None


def _validate_api_key(key: str, db=None) -> bool:
    """Check if this is a valid service API key.

    GSEC R-075: a key that resolves + verifies but is expired/revoked is
    REJECTED (401) through _enforce_api_key_policy — the same post-resolution
    policy the other auth funnels (require_bearer_or_api_key /
    require_service_api_key) apply — instead of being silently treated as a
    valid service key. _enforce_api_key_policy raises HTTPException(401) for
    an expired key; we let it propagate to the caller rather than swallowing
    it into a bare False.

    ``db`` is an optional session: pass one (tests) to reuse the caller's
    session; when omitted the function opens a short-lived session of its own
    (the registry endpoint is a sync route with no injected DB).
    """
    from fastapi import HTTPException
    from security import verify_api_key
    from dependencies import _enforce_api_key_policy
    import crud

    own_session = db is None
    if own_session:
        from database import SessionLocal
        db = SessionLocal()
    try:
        db_key = crud.get_api_key_by_key_prefix(db, key_prefix=key)
        if db_key and verify_api_key(key, db_key.hashed_key):
            # Enforce expiry/revocation/app-scope BEFORE declaring the key a
            # valid service credential. Raises 401 for an expired key (and
            # self-commits its own AUTH_API_KEY_EXPIRED audit row).
            _enforce_api_key_policy(db, db_key)
            # The policy self-commits its audit row when the session already
            # has a unit of work; on a fresh session (our own short-lived one)
            # there is none yet, so commit it here or it would be dropped.
            try:
                if db.in_transaction():
                    db.commit()
            except Exception:
                pass
            return True
    except HTTPException:
        # The policy's rejection (401 expired, 403 app-scope) must reach the
        # caller — do NOT fold it back into a silent 'not a service key'.
        raise
    except Exception:
        pass
    finally:
        if own_session:
            db.close()
    return False


def _get_user_tenant_id(claims: dict) -> Optional[int]:
    """Get tenant ID from Keycloak claims by looking up the user."""
    try:
        from database import SessionLocal
        import crud

        db = SessionLocal()
        try:
            user = crud.get_user_by_keycloak_id(db, keycloak_user_id=claims.get("sub"))
            if user:
                return user.tenant_id
        finally:
            db.close()
    except Exception:
        pass
    return None


def _make_token(scope: str, actions: list, repository: str) -> str:
    """Create a signed JWT for the registry."""
    now = int(time.time())

    access = []
    if repository:
        access.append({
            "type": "repository",
            "name": repository,
            "actions": actions,
        })

    payload = {
        "iss": _ISSUER,
        "sub": "mod-auth",
        "aud": "container-registry",
        "exp": now + _TOKEN_TTL,
        "nbf": now,
        "iat": now,
        "jti": base64.b64encode(os.urandom(16)).decode(),
        "access": access,
    }

    # python-jose needs the PEM key as bytes
    pem_bytes = _signing_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jose_jwt.encode(
        payload,
        pem_bytes,
        algorithm="RS256",
        headers={"kid": _key_id},
    )


@router.get("/registry/auth")
def registry_token_auth(
    service: str = Query(default="container-registry"),
    scope: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Docker registry token authentication endpoint.

    Called by registry:2 when a client tries to pull/push. The client
    provides credentials (Keycloak token or API key) and the requested
    scope. We validate and return a scoped JWT.
    """
    _load_signing_key()

    if _signing_key is None:
        raise HTTPException(status_code=503, detail="Registry auth not configured")

    # --- Authenticate the caller ---
    is_service_account = False
    caller_tenant_id = None

    # Try API key first (service account — can push to any tenant)
    if x_api_key and _validate_api_key(x_api_key):
        is_service_account = True
    elif authorization:
        # Try Bearer token (Keycloak)
        token = None
        if authorization.startswith("Bearer "):
            token = authorization[7:]
        elif authorization.startswith("Basic "):
            # Docker client sends Basic auth — password field contains the token
            try:
                decoded = base64.b64decode(authorization[6:]).decode()
                _, token = decoded.split(":", 1)
            except Exception:
                pass

        if token:
            claims = _validate_keycloak_token(token)
            if claims:
                caller_tenant_id = _get_user_tenant_id(claims)
            else:
                raise HTTPException(status_code=401, detail="Invalid token")
        else:
            raise HTTPException(status_code=401, detail="Missing credentials")
    else:
        raise HTTPException(status_code=401, detail="Missing credentials")

    # --- Parse and validate scope ---
    resource_type, repository, actions = _parse_scope(scope)

    if repository:
        repo_tenant_id = _extract_tenant_from_repo(repository)

        # Service accounts can access everything
        if not is_service_account:
            # Regular users can only access their own tenant
            if repo_tenant_id is None:
                raise HTTPException(status_code=403, detail="Invalid repository path — must be tenant-{id}/...")
            if caller_tenant_id != repo_tenant_id:
                raise HTTPException(status_code=403, detail="Access denied — wrong tenant")
            # Regular users can only pull, not push
            if "push" in actions:
                raise HTTPException(status_code=403, detail="Push access denied — use the API to upload containers")

    # --- Issue token ---
    token = _make_token(scope, actions, repository)

    return {
        "token": token,
        "expires_in": _TOKEN_TTL,
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
