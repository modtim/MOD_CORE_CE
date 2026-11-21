"""User vector store — API-mediated, per-tenant RAG vectors.

add-rag-vector-store group 3. This module is the ONLY client of the dedicated
`qdrant-user` instance. Every operation is scoped to a tenant via a per-tenant
collection `t{tenant_id}_vectors`; the tenant_id is derived from the
authenticated request (never client-supplied), so cross-tenant access is
impossible by construction — the collection name is computed here, not passed in.

Isolation model (mirrors MinIO):
  - qdrant-user is network-isolated (only the API is on `vectorstore-net`) and
    auth-gated (`QDRANT_USER_API_KEY`). Nodes never talk to it directly.
  - This module + the vector endpoints are the trust boundary. A caller supplies
    vectors/queries and a token; the API resolves the tenant and pins the
    collection. There is no code path that lets a request name another tenant's
    collection.

Fail-open on infrastructure: if qdrant-user is unreachable or the client library
is missing, helpers return safe empties / raise a clear error the endpoint maps
to 503 — never a silent cross-tenant leak.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QDRANT_USER_URL = os.getenv("QDRANT_USER_URL", "http://qdrant-user:6333")
# qdrant enables auth whenever QDRANT__SERVICE__API_KEY is present — INCLUDING
# an empty string (an empty key still rejects keyless requests). Compose always
# sets it (empty until provisioned, then the real value), so the client must
# always send the MATCHING key — pass the env value verbatim ("" when unset),
# never coerce "" → None (that drops the header → 401).
QDRANT_USER_API_KEY = os.getenv("QDRANT_USER_API_KEY", "")
# Default embedding dimension (all-MiniLM-L6-v2). Overridable per deployment;
# a collection is created with the dim of its first upsert if different.
DEFAULT_DIM = int(os.getenv("USER_VECTOR_DIM", "384"))

# Pooled qdrant client — created once and reused (avoids a connection setup per
# request under load). Reset via _reset_client() after a connection failure so a
# qdrant-user restart doesn't leave every request failing against a dead handle.
_CLIENT = None


class VectorStoreUnavailable(RuntimeError):
    """qdrant-user (or the client lib) is unreachable — map to 503, never leak."""


def collection_name(tenant_id: int) -> str:
    """The one place a tenant maps to a collection. `t{tenant_id}_vectors`.
    Computed from the authenticated tenant_id — never from client input."""
    return f"t{int(tenant_id)}_vectors"


def _client():
    try:
        from qdrant_client import QdrantClient
    except Exception as e:  # pragma: no cover - import guard
        raise VectorStoreUnavailable(f"qdrant-client not installed: {e}")
    global _CLIENT
    if _CLIENT is None:
        try:
            _CLIENT = QdrantClient(
                url=QDRANT_USER_URL, api_key=QDRANT_USER_API_KEY, timeout=15,
            )
        except Exception as e:
            raise VectorStoreUnavailable(f"qdrant-user unreachable: {e}")
    return _CLIENT


def _reset_client() -> None:
    """Drop the pooled client so the next call reconnects (used after a
    connection-level failure)."""
    global _CLIENT
    _CLIENT = None


def _ensure_collection(client, tenant_id: int, dim: int) -> str:
    """Return the tenant collection, creating it (cosine, `dim`) on first use."""
    name = collection_name(tenant_id)
    try:
        client.get_collection(name)
        return name
    except Exception:
        pass
    from qdrant_client.models import Distance, VectorParams
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=int(dim), distance=Distance.COSINE),
    )
    logger.info("user vector store: created collection %s (dim=%d)", name, dim)
    return name


def upsert(
    tenant_id: int, points: List[Dict[str, Any]], *, dim: Optional[int] = None,
) -> int:
    """Upsert points into the tenant's collection. Each point:
    {"id": <int|str>, "vector": [float, ...], "payload": {...}}. Returns count.
    The collection is created on first use from the vector dim."""
    if not points:
        return 0
    vec0 = points[0].get("vector") or []
    use_dim = int(dim or (len(vec0) if vec0 else DEFAULT_DIM))
    client = _client()
    from qdrant_client.models import PointStruct
    structs = [
        PointStruct(
            id=p["id"], vector=list(p["vector"]),
            payload=dict(p.get("payload") or {}),
        )
        for p in points
    ]
    try:
        name = _ensure_collection(client, tenant_id, use_dim)
        client.upsert(collection_name=name, points=structs)
    except Exception as e:
        # Reset the pooled client so a dead connection (qdrant-user restart)
        # doesn't wedge every subsequent request.
        _reset_client()
        raise VectorStoreUnavailable(f"upsert failed: {e}")
    return len(structs)


def _payload_filter(where: Optional[Dict[str, Any]]):
    """Build a qdrant `must`-all-match filter from a flat {key: value} map.
    Returns None for an empty/absent map (no filtering — back-compat)."""
    if not where:
        return None
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    return Filter(must=[
        FieldCondition(key=k, match=MatchValue(value=v))
        for k, v in where.items()
    ])


def query(
    tenant_id: int, vector: List[float], *, top_k: int = 8,
    where: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Top-k nearest neighbors from the tenant's collection. Returns
    [{"id", "score", "payload"}]. Empty list if the collection doesn't exist.
    `where` is an optional flat payload filter (e.g. {"source": "kb"}) that
    every hit must match; None (the default) filters nothing — the shared
    collection mixes KB and workflow vectors, so KB callers pass source=="kb"."""
    client = _client()
    name = collection_name(tenant_id)
    try:
        hits = client.search(
            collection_name=name, query_vector=list(vector), limit=int(top_k),
            query_filter=_payload_filter(where),
        )
    except Exception as e:
        msg = str(e).lower()
        # A missing collection (no data yet) is not an error — return empty.
        if "not found" in msg or "404" in msg or "doesn't exist" in msg or "not exist" in msg:
            logger.info("user vector store: query on %s — no collection yet", name)
            return []
        # Dimension mismatch — happens transiently DURING a dimension migration
        # when the pinned model already changed but this tenant's collection
        # hasn't been re-embedded yet. Degrade to empty (no results) rather than
        # 503, so RAG returns nothing instead of erroring while the migration
        # catches up.
        if "dimension" in msg or "wrong input" in msg:
            logger.warning("user vector store: query on %s dim-mismatch (migration in flight?) — empty", name)
            return []
        # A real failure (connection dead, etc.) — reset the pool + surface 503.
        _reset_client()
        raise VectorStoreUnavailable(f"query failed: {e}")
    return [
        {"id": h.id, "score": h.score, "payload": h.payload or {}}
        for h in hits
    ]


def drop_tenant(tenant_id: int) -> bool:
    """Delete the tenant's collection entirely (per-tenant / GDPR deletion)."""
    client = _client()
    name = collection_name(tenant_id)
    try:
        client.delete_collection(collection_name=name)
        logger.info("user vector store: dropped collection %s", name)
        return True
    except Exception as e:
        logger.info("user vector store: drop %s no-op (%s)", name, e)
        return False


def delete_by_document(tenant_id: int, kb_document_id: Any) -> bool:
    """Delete exactly one KB document's chunks from the tenant collection by
    payload filter (`kb_document_id == …`), leaving every other point — other
    KB documents and workflow vectors — untouched. Used by per-document delete
    and reindex (reindex = delete_by_document then re-upsert).

    No-op (returns False) if the collection doesn't exist yet — deleting from an
    empty KB is not an error. Fail-closed on a real infrastructure failure
    (raises VectorStoreUnavailable) so a caller never marks chunks removed while
    they are still retrievable."""
    client = _client()
    name = collection_name(tenant_id)
    from qdrant_client.models import (
        Filter, FieldCondition, MatchValue, FilterSelector,
    )
    flt = Filter(must=[
        FieldCondition(key="kb_document_id", match=MatchValue(value=kb_document_id)),
    ])
    try:
        client.delete(
            collection_name=name, points_selector=FilterSelector(filter=flt),
        )
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "404" in msg or "doesn't exist" in msg or "not exist" in msg:
            logger.info("user vector store: delete_by_document on %s — no collection yet", name)
            return False
        _reset_client()
        raise VectorStoreUnavailable(f"delete_by_document failed: {e}")
    logger.info("user vector store: deleted kb_document_id=%s from %s", kb_document_id, name)
    return True


def delete_by_namespace(tenant_id: int, namespace: str) -> bool:
    """Delete every point stamped with ``payload.namespace == namespace`` from the
    tenant collection, leaving all other namespaces (other RAGs, KB, workflow
    vectors) untouched. Used by the RAG Builder's MODE=replace to clear a named
    collection before re-ingesting it.

    No-op (returns False) if the collection doesn't exist yet — clearing an empty
    namespace is not an error. Fail-closed on a real infrastructure failure
    (raises VectorStoreUnavailable)."""
    if not namespace:
        return False
    client = _client()
    name = collection_name(tenant_id)
    from qdrant_client.models import (
        Filter, FieldCondition, MatchValue, FilterSelector,
    )
    flt = Filter(must=[
        FieldCondition(key="namespace", match=MatchValue(value=namespace)),
    ])
    try:
        client.delete(
            collection_name=name, points_selector=FilterSelector(filter=flt),
        )
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "404" in msg or "doesn't exist" in msg or "not exist" in msg:
            logger.info("user vector store: delete_by_namespace on %s — no collection yet", name)
            return False
        _reset_client()
        raise VectorStoreUnavailable(f"delete_by_namespace failed: {e}")
    logger.info("user vector store: cleared namespace=%s from %s", namespace, name)
    return True


# =============================================================================
# Dimension migration — re-embed a tenant's corpus to the currently-pinned
# embedding model (a new model usually means a new vector width, and qdrant can't
# mix widths in one collection). Rebuild-in-place: read payload.text out of the
# live collection, re-embed with the new model into a temp collection, then swap.
# The source keeps serving old vectors until the final copy; during the copy the
# collection is briefly empty (queries return [], never an error).
# =============================================================================

def _collection_exists(client, name: str) -> bool:
    try:
        client.get_collection(name)
        return True
    except Exception:
        return False


def _recreate(client, name: str, dim: int) -> None:
    from qdrant_client.models import Distance, VectorParams
    try:
        client.delete_collection(collection_name=name)
    except Exception:
        pass
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=int(dim), distance=Distance.COSINE),
    )


def list_tenant_collections() -> List[int]:
    """Tenant ids that currently have a RAG collection (`t{id}_vectors`)."""
    import re
    client = _client()
    out: List[int] = []
    try:
        for c in client.get_collections().collections:
            m = re.match(r"^t(\d+)_vectors$", c.name)
            if m:
                out.append(int(m.group(1)))
    except Exception as e:
        _reset_client()
        raise VectorStoreUnavailable(f"list collections failed: {e}")
    return sorted(out)


def migrate_tenant_collection(tenant_id: int, *, batch: int = 256, on_progress=None) -> Dict[str, Any]:
    """Rebuild-in-place re-embed of one tenant's RAG collection to the CURRENT
    (pinned) embedding model. Returns a summary dict. Points whose payload has no
    recoverable `text` (e.g. upserted as precomputed vectors) can't be re-embedded
    and are counted as skipped."""
    from qdrant_client.models import PointStruct
    from utils.embeddings import embed_texts, current_dim, current_model_name

    client = _client()
    src = collection_name(tenant_id)
    tmp = f"{src}__mig"
    result: Dict[str, Any] = {
        "tenant_id": int(tenant_id), "model": current_model_name(),
        "migrated": 0, "skipped_no_text": 0, "new_dim": None, "status": "ok",
    }
    if not _collection_exists(client, src):
        result["status"] = "no_collection"
        return result

    try:
        new_dim = current_dim()
        result["new_dim"] = new_dim

        # 1) Build temp at the new dim from the source's text. Source keeps
        #    serving its old vectors throughout this pass.
        _recreate(client, tmp, new_dim)
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=src, limit=batch, offset=offset,
                with_payload=True, with_vectors=False,
            )
            if points:
                ids, texts, payloads = [], [], []
                for p in points:
                    txt = (p.payload or {}).get("text")
                    if not txt:
                        result["skipped_no_text"] += 1
                        continue
                    ids.append(p.id)
                    texts.append(txt)
                    payloads.append(p.payload or {})
                if texts:
                    vecs = embed_texts(texts)
                    client.upsert(collection_name=tmp, points=[
                        PointStruct(id=i, vector=v, payload=pl)
                        for i, v, pl in zip(ids, vecs, payloads)
                    ])
                    result["migrated"] += len(texts)
                    if on_progress:
                        on_progress(result["migrated"])
            if offset is None:
                break

        # Safety gate: NEVER destroy src unless tmp holds exactly the points we
        # migrated. If re-embedding fell short (partial failure), abort with src
        # untouched (old vectors keep serving) — no data loss.
        tmp_count = client.get_collection(tmp).points_count or 0
        if tmp_count != result["migrated"]:
            result["status"] = "aborted_incomplete_temp"
            logger.error("vector migration ABORT %s: temp has %d, expected %d — src left intact",
                         src, tmp_count, result["migrated"])
            try:
                client.delete_collection(collection_name=tmp)
            except Exception:
                pass
            return result

        # 2) Swap: recreate src at the new dim, copy temp → src (moving vectors,
        #    no re-embed). Brief window where src is empty; queries degrade to
        #    empty (see query()). tmp is the ONLY full copy during this window,
        #    so it is NOT deleted until src is verified below.
        _recreate(client, src, new_dim)
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=tmp, limit=batch, offset=offset,
                with_payload=True, with_vectors=True,
            )
            if points:
                client.upsert(collection_name=src, points=[
                    PointStruct(id=p.id, vector=p.vector, payload=p.payload or {})
                    for p in points
                ])
            if offset is None:
                break

        # Verify the copy landed before dropping the only backup (tmp). If it's
        # short, KEEP tmp for recovery and flag it.
        src_count = client.get_collection(src).points_count or 0
        if src_count != tmp_count:
            result["status"] = "copy_incomplete_temp_kept"
            logger.error("vector migration %s: src has %d after copy, expected %d — KEEPING %s for recovery",
                         src, src_count, tmp_count, tmp)
            return result
        try:
            client.delete_collection(collection_name=tmp)
        except Exception:
            pass
    except VectorStoreUnavailable:
        raise
    except Exception as e:
        _reset_client()
        raise VectorStoreUnavailable(f"migration failed for {src}: {e}")

    logger.info(
        "vector migration: %s → dim=%s model=%s (%d migrated, %d skipped)",
        src, result["new_dim"], result["model"], result["migrated"], result["skipped_no_text"],
    )
    return result
