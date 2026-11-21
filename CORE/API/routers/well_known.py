"""30.6.5 / GX.S1c c5a — the MOD JWKS endpoint (public key material only).

Two public, unauthenticated routes serve the SAME body:

    GET /v1/.well-known/jwks.json      (the Collective's path)
    GET /.well-known/jwks.json         (the spec-canonical alias)

The body is `{"keys":[{kty,kid,alg,use,n,e}, …]}` — the manager keypair MOD
mints its RS256 tokens with (`utils/build_task_token.py`), plus the previous
key while it is in-window. Every member set is exactly {kty,kid,alg,use,n,e}:
no `d`/`p`/`q`/`dp`/`dq`/`qi`, no PEM, ever (S1).

Served from MEMORY only — no outbound fetch, no httpx, no JWKS-URI
resolution (S9: the `keycloak_auth.py` `httpx.get(to_internal_url(jwks_uri))`
pattern is deliberately NOT reused here). Cacheable like a static file:
`Cache-Control: public, max-age=3600` plus an `ETag` over the canonical JSON,
so a receiver can revalidate cheaply.

Unknown `kid` is NOT a JWKS error: the endpoint just publishes what it has;
the verifier that cannot resolve a kid rejects with its own 401.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from utils.build_task_token import jwks_document

from dependencies import get_db

router = APIRouter(tags=["well-known"])

_CACHE_CONTROL = "public, max-age=3600"


def _canonical_jwks_json(document: dict) -> str:
    """Canonical serialization the ETag is computed over (sorted keys, no
    insignificant whitespace) so the hash is stable across renders."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _render(document: dict) -> JSONResponse:
    body = _canonical_jwks_json(document)
    response = JSONResponse(content=document, media_type="application/json")
    response.headers["Cache-Control"] = _CACHE_CONTROL
    response.headers["ETag"] = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest() + '"'
    return response


def _jwks_response(db=None):
    """Build the JWKS response (memory only, never a fetch)."""
    return _render(jwks_document(db=db))


# api-lint: public-by-design: JWKS — public key material only, unauthenticated by design (class B)
@router.get("/v1/.well-known/jwks.json", response_model=dict, include_in_schema=False)
def get_jwks_v1(db=Depends(get_db)):
    """JWKS for MOD-minted RS256 tokens (versioned path)."""
    return _jwks_response(db=db)


# api-lint: public-by-design: JWKS alias — same public body, unauthenticated by design (class B)
@router.get("/.well-known/jwks.json", response_model=dict, include_in_schema=False)
def get_jwks_alias(db=Depends(get_db)):
    """JWKS alias at the spec-canonical, unversioned path."""
    return _jwks_response(db=db)
