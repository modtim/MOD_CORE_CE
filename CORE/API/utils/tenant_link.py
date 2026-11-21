"""MKT.C2 — tenant link: short-lived Core-signed token binding a
Marketplace purchase sale to a MOD tenant.

The in-app Buy deep link carries this token; the Marketplace embeds it in
its purchase-delivery webhook as ``tenant_link`` so the sale is applied to
the right tenant without the webhook ever trusting a client-supplied id.

Signed the same way the codebase's other short-lived tokens are
(utils/build_task_token.py, utils/relay_token.py — python-jose JWT with
iat/exp claims). HS256 with ``MARKETPLACE_WEBHOOK_SECRET``: the Marketplace
shares that exact secret with us for HMAC request signing, so the same
secret doubles as the tenant-link signing key — one shared secret between
the two systems, no new credential.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from jose import jwt, JWTError

TOKEN_TYPE = "mod-tenant-link-v1"
_ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 3600


def _secret() -> str:
    secret = os.getenv("MARKETPLACE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError(
            "MARKETPLACE_WEBHOOK_SECRET is not set; cannot sign tenant links."
        )
    return secret


def mint_tenant_link(tenant_id: int, ttl_s: int = DEFAULT_TTL_SECONDS) -> str:
    """Sign a tenant-link token binding a sale to ``tenant_id``.

    Raises RuntimeError if the shared secret is unset (fail closed)."""
    now = int(time.time())
    claims = {
        "typ": TOKEN_TYPE,
        "tenant_id": int(tenant_id),
        "iat": now,
        "exp": now + int(ttl_s),
    }
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM)


def verify_tenant_link(token: str) -> int:
    """Verify signature, type and expiry; return the bound ``tenant_id``.

    Raises ``JWTError`` when the token is missing, malformed, wrong-typed,
    not signed with our secret, or expired."""
    if not token:
        raise JWTError("missing tenant link token")
    claims = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    if claims.get("typ") != TOKEN_TYPE:
        raise JWTError("token is not a tenant link")
    tenant_id = claims.get("tenant_id")
    if tenant_id is None:
        raise JWTError("tenant link token has no tenant_id claim")
    return int(tenant_id)
