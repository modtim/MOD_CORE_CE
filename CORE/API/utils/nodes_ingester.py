"""Per-user node-definition RAG.

Indexes a NodeDefinition's name + description + I/O schema into Qdrant
so the Workflow Wizard can do semantic search filtered by who is
allowed to see each node:

  - system rows  (tenant_id NULL, user_id NULL) → visible to everyone
  - tenant rows  (tenant_id T,    user_id NULL) → visible to tenant T
  - private rows (tenant_id T,    user_id U)    → visible only to user U

Every NodeDefinition becomes ONE Qdrant point keyed by node id. The
payload carries `tenant_id` and `user_id` so search-time filtering is a
two-clause `should`:
    should = [tenant_id NULL] OR
             [tenant_id = T AND user_id NULL] OR
             [tenant_id = T AND user_id = U]

Lifecycle:
  - upsert on node create / update
  - delete on node delete
  - bulk reindex via reindex_all_nodes() (called at boot or via the
    /platform/support/reindex-docs admin endpoint, which already
    kicks the docs ingester — we piggyback)

The same `all-MiniLM-L6-v2` embedder used by docs_ingester is reused
to keep memory footprint and latency identical.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
# qdrant-hardening: pass the key VERBATIM (incl "" when unset). qdrant enables
# auth on an empty key too, so a dropped header → 401; "" matches an empty-key
# instance and the real value matches a provisioned one.
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")  # verbatim: qdrant empty-key auth rejects a dropped header
QDRANT_COLLECTION = "node_definitions_index"
EMBEDDING_DIM = 384


_model = None


def _get_embedding_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            from utils.embedding_model_store import ensure_model
            # Load the LOCAL model dir strictly offline — the bareword
            # SentenceTransformer("all-MiniLM-L6-v2") resolved the name against
            # HuggingFace, so with no network it retried for ~40s and (on
            # failure) left _model=None, re-blocking every call. allow_hf=False
            # + local_files_only guarantee a fast local load and no network.
            path = ensure_model(
                hf_id="sentence-transformers/all-MiniLM-L6-v2",
                local_dir="/app/models/embeddings/all-MiniLM-L6-v2",
                name="all-MiniLM-L6-v2",
                allow_hf=False,
            )
            _model = SentenceTransformer(path, local_files_only=True)
        except Exception as e:  # noqa: BLE001 — missing lib or missing model
            logger.warning("node RAG embedding model unavailable: %s", e)
            return None
    return _model


def _get_qdrant_client():
    from utils import internal_tls

    try:
        from qdrant_client import QdrantClient
        # GY.C6.s3 — build the hop through the internal-TLS seam so a flip of
        # gsec.transport.internal_tls_enforce actually enforces this hop:
        # `require` re-verifies against the platform CA bundle and refuses a
        # plain http:// URL. `off` (the default) returns no kwargs at all, so
        # the call below keeps today's shape byte-identically.
        hop_kwargs = internal_tls.internal_client_kwargs(url=QDRANT_URL)
        return QdrantClient(
            url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=10, **hop_kwargs
        )
    except internal_tls.InternalTLSConfigError:
        # Fail-closed: a `require` refusal is NOT a qdrant outage. The bare
        # handler below swallows everything and returns None, which would
        # silently disable node RAG (fail-open) — so the refusal re-raises.
        raise
    except ImportError:
        logger.warning("qdrant-client missing — node RAG disabled")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Qdrant unreachable for node RAG: %s", e)
        return None


def _ensure_collection(client) -> bool:
    """Create the collection on first use. Returns True if usable.

    `qdrant_client.models` is imported lazily so a test environment
    without the package can still exercise the fake-client path — tests
    construct a real Qdrant point/filter shape via simple stubs and
    monkeypatch the helpers that need real qdrant types.
    """
    try:
        client.get_collection(QDRANT_COLLECTION)
        return True
    except Exception:
        pass
    try:
        from qdrant_client.models import Distance, VectorParams
    except ImportError:
        # Fake client in tests doesn't need real Distance/VectorParams —
        # call create_collection with kwargs and hope for the best.
        try:
            client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config={"size": EMBEDDING_DIM, "distance": "Cosine"},
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("could not create %s (no qdrant_client.models): %s",
                            QDRANT_COLLECTION, e)
            return False
    try:
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("could not create %s collection: %s", QDRANT_COLLECTION, e)
        return False


def _node_text(nd) -> str:
    """Flatten the columns the LLM cares about into one embedding string."""
    parts = [
        f"name: {getattr(nd, 'name', '') or ''}",
        f"type: {getattr(nd, 'type', '') or ''}",
        f"description: {getattr(nd, 'description', '') or ''}",
        f"process_type: {getattr(nd, 'process_type', '') or ''}",
    ]
    inp = getattr(nd, "input_schema", None) or {}
    out = getattr(nd, "output_schema", None) or {}
    if isinstance(inp, dict) and inp:
        parts.append(f"input_type: {inp.get('input_type', '')}")
    if isinstance(out, dict) and out:
        parts.append(f"output_type: {out.get('output_type', '')}")
    return " | ".join(p for p in parts if p.strip(": "))


def _node_payload(nd) -> dict:
    """Payload kept on the Qdrant point — used for filtering at search
    time and for rendering hits to the LLM without a follow-up DB query."""
    return {
        "node_id": int(getattr(nd, "id", 0) or 0),
        "tenant_id": getattr(nd, "tenant_id", None),
        "user_id": getattr(nd, "user_id", None),
        "name": getattr(nd, "name", None),
        "type": getattr(nd, "type", None),
        "description": (getattr(nd, "description", None) or "")[:1000],
        "process_type": getattr(nd, "process_type", None),
    }


def upsert_node(nd) -> bool:
    """Embed and upsert a single NodeDefinition into Qdrant.
    Returns False quietly if Qdrant or embeddings are unavailable —
    callers must NOT depend on RAG for correctness."""
    model = _get_embedding_model()
    client = _get_qdrant_client()
    if model is None or client is None:
        return False
    if not _ensure_collection(client):
        return False
    try:
        try:
            from qdrant_client.models import PointStruct  # type: ignore
        except ImportError:
            PointStruct = _StubPoint  # type: ignore[misc]
        raw = model.encode(_node_text(nd))
        vector = raw.tolist() if hasattr(raw, "tolist") else list(raw)
        client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=[PointStruct(
                id=int(nd.id),
                vector=vector,
                payload=_node_payload(nd),
            )],
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("node RAG upsert failed for id=%s: %s", getattr(nd, "id", "?"), e)
        return False


def delete_node(node_id: int) -> bool:
    client = _get_qdrant_client()
    if client is None:
        return False
    try:
        try:
            from qdrant_client.models import PointIdsList  # type: ignore
        except ImportError:
            PointIdsList = _StubPointIds  # type: ignore[misc]
        client.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=PointIdsList(points=[int(node_id)]),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("node RAG delete failed for id=%s: %s", node_id, e)
        return False


class _StubPoint:
    def __init__(self, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload


class _StubPointIds:
    def __init__(self, points):
        self.points = points


class _StubFilter:
    def __init__(self, *, must=None, should=None):
        self.must = must
        self.should = should


class _StubFieldCondition:
    def __init__(self, *, key, match):
        self.key = key
        self.match = match


class _StubMatchValue:
    def __init__(self, *, value):
        self.value = value


class _StubIsNullCondition:
    def __init__(self, *, is_null):
        self.is_null = is_null


def search_user_nodes(
    db, query: str, *,
    tenant_id: Optional[int],
    user_id: Optional[int],
    top_k: int = 8,
) -> List[dict]:
    """Semantic search over the nodes a (tenant, user) pair can see.

    `db` is unused today but kept in the signature so we can fall back
    to a SQL LIKE search when Qdrant is down (caller doesn't care which
    backend served the result).
    """
    model = _get_embedding_model()
    client = _get_qdrant_client()
    if model is None or client is None:
        return _sql_fallback(db, query, tenant_id=tenant_id, user_id=user_id, top_k=top_k)
    if not _ensure_collection(client):
        return []

    try:
        from qdrant_client.models import (  # type: ignore
            Filter, FieldCondition, MatchValue, IsNullCondition,
        )
    except ImportError:
        Filter = _StubFilter  # type: ignore[misc]
        FieldCondition = _StubFieldCondition  # type: ignore[misc]
        MatchValue = _StubMatchValue  # type: ignore[misc]
        IsNullCondition = _StubIsNullCondition  # type: ignore[misc]

    # Three-clause OR: system / tenant-shared / private
    should: list = [
        Filter(must=[
            IsNullCondition(is_null={"key": "tenant_id"}),
            IsNullCondition(is_null={"key": "user_id"}),
        ]),
    ]
    if tenant_id is not None:
        should.append(Filter(must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=int(tenant_id))),
            IsNullCondition(is_null={"key": "user_id"}),
        ]))
        if user_id is not None:
            should.append(Filter(must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=int(tenant_id))),
                FieldCondition(key="user_id", match=MatchValue(value=int(user_id))),
            ]))

    qfilter = Filter(should=should)
    try:
        _q = model.encode(query)
        vec = _q.tolist() if hasattr(_q, "tolist") else list(_q)
        hits = client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vec,
            query_filter=qfilter,
            limit=top_k,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("node RAG search failed: %s", e)
        return _sql_fallback(db, query, tenant_id=tenant_id, user_id=user_id, top_k=top_k)

    return [
        {
            "node_id": h.payload.get("node_id"),
            "name": h.payload.get("name"),
            "description": h.payload.get("description"),
            "type": h.payload.get("type"),
            "process_type": h.payload.get("process_type"),
            "score": h.score,
        }
        for h in hits
    ]


def _sql_fallback(db, query: str, *, tenant_id, user_id, top_k: int) -> List[dict]:
    """Substring search when Qdrant is unavailable. Keeps the wizard
    semi-functional during a vector DB outage."""
    if db is None:
        return []
    try:
        import models
        from crud.node_definition_cud import get_node_definitions
        rows = get_node_definitions(db, tenant_id=tenant_id, user_id=user_id, limit=200)
    except Exception:
        return []
    q = (query or "").lower()
    scored = []
    for nd in rows:
        text = _node_text(nd).lower()
        score = sum(text.count(tok) for tok in q.split() if tok)
        if score:
            scored.append((score, nd))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {
            "node_id": nd.id,
            "name": nd.name,
            "description": (nd.description or "")[:1000],
            "type": nd.type,
            "process_type": nd.process_type,
            "score": float(s),
        }
        for s, nd in scored[:top_k]
    ]


def reindex_all_nodes(db) -> dict:
    """Bulk-embed every NodeDefinition currently in the DB. Used at boot
    and by the admin-only /platform/support/reindex-docs endpoint.

    Tolerant of partial failures — returns a count + an `errors` field.
    """
    model = _get_embedding_model()
    client = _get_qdrant_client()
    if model is None or client is None:
        return {"success": False, "error": "qdrant or embeddings unavailable"}
    if not _ensure_collection(client):
        return {"success": False, "error": "could not ensure collection"}

    import models as _models
    try:
        from qdrant_client.models import PointStruct  # type: ignore
    except ImportError:
        PointStruct = _StubPoint  # type: ignore[misc]

    rows: Iterable = db.query(_models.NodeDefinition).all()
    points = []
    errors = 0
    for nd in rows:
        try:
            _r = model.encode(_node_text(nd))
            vec = _r.tolist() if hasattr(_r, "tolist") else list(_r)
            points.append(PointStruct(id=int(nd.id), vector=vec, payload=_node_payload(nd)))
        except Exception:
            errors += 1

    if not points:
        return {"success": True, "indexed": 0, "errors": errors}
    BATCH = 100
    for i in range(0, len(points), BATCH):
        try:
            client.upsert(collection_name=QDRANT_COLLECTION, points=points[i:i + BATCH])
        except Exception:
            errors += 1
    return {"success": True, "indexed": len(points), "errors": errors,
            "collection": QDRANT_COLLECTION}
