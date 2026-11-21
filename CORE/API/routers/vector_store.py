"""User vector store endpoints — API-mediated RAG (add-rag-vector-store 3.3).

The trust boundary for the user vector store. Nodes (Vector Upsert / Vector
Query) and the Dashboard call these endpoints; the API is the ONLY thing that
talks to qdrant-user. Every operation is scoped to the tenant resolved from the
authenticated credential (a scoped build/run token or a user bearer) — NEVER
from the request body — so cross-tenant access is impossible by construction.

Auth: `require_user_or_build_token` accepts either
  - a scoped token (via="build_token") → tenant_id from the token (node path), or
  - a user bearer/api-key (via="user") → tenant_id from the user (Dashboard path).
Missing/invalid credentials → 401 before any qdrant op.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dependencies import (
    BuildAuthContext,
    get_db,
    require_user_or_build_token,
)
from utils import embeddings
from utils import user_vector_store as uvs
from utils.build_task_token import verify_vector_run_token, verify_run_task_token
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vector", tags=["vector-store"])

_MAX_POINTS = 1000
_MAX_TOP_K = 100


def _tenant_of(ctx: BuildAuthContext) -> int:
    if ctx.tenant_id is not None:
        return int(ctx.tenant_id)
    if ctx.user is not None and getattr(ctx.user, "tenant_id", None) is not None:
        return int(ctx.user.tenant_id)
    raise HTTPException(status_code=403, detail="Credential carries no tenant scope.")


def require_vector_tenant(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> int:
    """Resolve the tenant for a vector op from the verified credential — NEVER
    from the request body. Accepts, in order:
      1. a run-scoped vector token (RAG node → tenant from the token),
      2. a run_task node token (the worker-vended per-task credential a RAG node
         actually presents once the worker swaps its injected API_KEY for a
         MOD_TASK_TOKEN_URL token — tenant from the token),
      3. a build-task token or user bearer (Dashboard / build task).
    Missing/invalid → 401/403 before any qdrant op."""
    claims = verify_vector_run_token(x_api_key) if x_api_key else None
    if claims and claims.get("tenant_id") is not None:
        return int(claims["tenant_id"])
    # A RAG node dispatched as a downstream task carries the vector token as
    # API_KEY, but the worker (worker-mediated-node-credentials) swaps that for a
    # short-lived, tenant+run-scoped run_task token whenever API_KEY is present
    # and MOD_STATIC_API_KEY is not — so the node presents a run_task token here,
    # not the vector token. It is the platform's standard node-callback credential
    # (already accepted by resolve-cog-hash, batch-update-processing-history, …)
    # and is tenant-scoped, so vector ops stay confined to the token's own tenant.
    rt_claims = verify_run_task_token(x_api_key) if x_api_key else None
    if rt_claims and rt_claims.get("tenant_id") is not None:
        return int(rt_claims["tenant_id"])
    ctx = require_user_or_build_token(
        authorization=authorization, x_api_key=x_api_key, db=db,
    )
    return _tenant_of(ctx)


class _VectorPoint(BaseModel):
    id: Union[int, str]
    # Supply `text` (the API embeds it with the platform model — the common case
    # for RAG) OR a precomputed `vector`. At least one is required.
    text: Optional[str] = None
    vector: Optional[List[float]] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class _UpsertRequest(BaseModel):
    points: List[_VectorPoint]
    dim: Optional[int] = None
    # Optional intra-tenant collection tag (RAG "collection" / namespace). When
    # set, every point is stamped with payload.namespace so a later query can
    # retrieve ONLY that namespace's chunks — this is how two independent RAGs
    # (e.g. character KB vs brand KB) live in one tenant collection without
    # mixing. None (the default) preserves the historical behavior exactly.
    namespace: Optional[str] = None


class _UpsertResponse(BaseModel):
    upserted: int
    collection: str


class _QueryRequest(BaseModel):
    # Supply `text` (embedded server-side, same model as upsert) OR a `vector`.
    text: Optional[str] = None
    vector: Optional[List[float]] = None
    top_k: int = 8
    # Optional intra-tenant collection tag. When set, only points stamped with
    # this payload.namespace at upsert are considered — server-side filtering, so
    # recall is unaffected by other namespaces sharing the tenant collection.
    # None (the default) preserves the historical behavior exactly.
    namespace: Optional[str] = None


class _QueryMatch(BaseModel):
    id: Union[int, str]
    score: float
    payload: Dict[str, Any] = Field(default_factory=dict)


class _QueryResponse(BaseModel):
    matches: List[_QueryMatch]


class _VectorPurgeResponse(BaseModel):
    dropped: bool
    collection: str


class _VectorClearNamespaceResponse(BaseModel):
    cleared: bool
    namespace: str
    collection: str


@router.post("/upsert", response_model=_UpsertResponse)
def vector_upsert(
    request: _UpsertRequest,
    tenant_id: int = Depends(require_vector_tenant),
):
    if not request.points:
        return _UpsertResponse(upserted=0, collection=uvs.collection_name(tenant_id))
    if len(request.points) > _MAX_POINTS:
        raise HTTPException(status_code=400, detail=f"at most {_MAX_POINTS} points per request")
    # Server-side embedding: points that supply `text` (no `vector`) are embedded
    # with the platform model, so documents + queries always share one model.
    to_embed = [i for i, p in enumerate(request.points) if p.vector is None]
    if to_embed:
        texts = []
        for i in to_embed:
            if not (request.points[i].text or "").strip():
                raise HTTPException(status_code=400, detail=f"point {request.points[i].id}: 'text' or 'vector' required")
            texts.append(request.points[i].text)
        try:
            vecs = embeddings.embed_texts(texts)
        except embeddings.EmbeddingUnavailable as e:
            raise internal_http_error(503, "Embedding unavailable", e) from e
        for j, i in enumerate(to_embed):
            request.points[i].vector = vecs[j]
    _ns = (request.namespace or "").strip()
    points = [
        {
            "id": p.id, "vector": p.vector,
            # Keep the source text in the payload so query results can return it.
            # Stamp the namespace (when given) so a namespaced query can filter to
            # exactly this collection. A per-point payload.namespace always wins,
            # so a caller can still override per point if it supplies one.
            "payload": {
                **({"namespace": _ns} if _ns else {}),
                **p.payload,
                **({"text": p.text} if p.text else {}),
            },
        }
        for p in request.points
    ]
    try:
        n = uvs.upsert(tenant_id, points, dim=request.dim)
    except uvs.VectorStoreUnavailable as e:
        raise internal_http_error(503, "Vector store unavailable", e) from e
    return _UpsertResponse(upserted=n, collection=uvs.collection_name(tenant_id))


@router.post("/query", response_model=_QueryResponse)
def vector_query(
    request: _QueryRequest,
    tenant_id: int = Depends(require_vector_tenant),
):
    vector = request.vector
    if not vector:
        if not (request.text or "").strip():
            raise HTTPException(status_code=400, detail="'text' or 'vector' is required")
        try:
            vector = embeddings.embed_query(request.text)
        except embeddings.EmbeddingUnavailable as e:
            raise internal_http_error(503, "Embedding unavailable", e) from e
    top_k = max(1, min(int(request.top_k), _MAX_TOP_K))
    _ns = (request.namespace or "").strip()
    _where = {"namespace": _ns} if _ns else None
    try:
        matches = uvs.query(tenant_id, vector, top_k=top_k, where=_where)
    except uvs.VectorStoreUnavailable as e:
        raise internal_http_error(503, "Vector store unavailable", e) from e
    return _QueryResponse(matches=[_QueryMatch(**m) for m in matches])


@router.delete("/collection", response_model=_VectorPurgeResponse)
def vector_purge(
    tenant_id: int = Depends(require_vector_tenant),
    db: Session = Depends(get_db),
):
    """Delete the caller's tenant collection (per-tenant / GDPR deletion).

    GY.C15 — legal hold: a held tenant's vector collection is not dropped
    (supports preservation requirements, e.g. DFARS 252.204-7012 90-day).
    Fail closed: a hold-lookup error blocks the drop."""
    try:
        from utils.paid_import import optional_paid
        _lhold = optional_paid("services.legal_hold")
        # FX.162.p5 — CE carve: no legal holds exist -> nothing held
        if _lhold is not None and _lhold.is_held(db, tenant_id=tenant_id):
            raise HTTPException(
                status_code=409,
                detail="Legal hold active on this tenant; vector "
                       "collection cannot be purged",
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — fail closed
        raise HTTPException(
            status_code=409,
            detail="Legal-hold check failed; vector collection drop "
                   "refused (fail closed)",
        ) from e
    try:
        dropped = uvs.drop_tenant(tenant_id)
    except uvs.VectorStoreUnavailable as e:
        raise internal_http_error(503, "Vector store unavailable", e) from e
    return {"dropped": dropped, "collection": uvs.collection_name(tenant_id)}


@router.delete("/namespace", response_model=_VectorClearNamespaceResponse)
def vector_clear_namespace(
    namespace: str,
    tenant_id: int = Depends(require_vector_tenant),
):
    """Clear a single namespace (RAG collection) from the caller's tenant store,
    leaving every other namespace untouched. Used by the RAG Builder node's
    MODE=replace to reset a named corpus before re-ingesting it."""
    ns = (namespace or "").strip()
    if not ns:
        raise HTTPException(status_code=400, detail="namespace is required")
    try:
        cleared = uvs.delete_by_namespace(tenant_id, ns)
    except uvs.VectorStoreUnavailable as e:
        raise internal_http_error(503, "Vector store unavailable", e) from e
    return {"cleared": cleared, "namespace": ns,
            "collection": uvs.collection_name(tenant_id)}
