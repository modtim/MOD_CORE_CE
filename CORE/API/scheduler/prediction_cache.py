"""Qdrant-backed prediction cache for the AI scheduler.

(redis-oss-to-valkey Gate 3) — this module previously ran on a dedicated
`redis-stack-server` instance (RediSearch HNSW). redis-stack-server is not
OSS-clean, so the vector ANN + exact-match cache now runs on the platform
Qdrant (`mod_qdrant`), which the codebase already operates for RAG. The
external API (`get`/`put`/`clear`/`fingerprint`/`encode_contention_vector`/
`reset_for_tests`) is unchanged, so callers (`overlap_scheduler`) need no edits.

Two-layer lookup:

  1. **Exact match** (the hot path) — fingerprint is a SHA256 of
     `(node_kind, hw_profile_id, contention_summary_hash)` mapped to a
     deterministic point ID, so identical placements (same task type, same
     worker class, same concurrent load) reuse the prior prediction via a
     point `retrieve` (sub-millisecond). The 90%+ steady-state hit rate is
     what keeps the unified packer inside its 5s replan budget.

  2. **Vector ANN fallback** — when no exact match exists, KNN-search the
     Qdrant collection over a 64-dimensional contention-fingerprint embedding
     (resource utilisation per axis × a few summary stats). A near-miss
     (cosine similarity >= MIN_VECTOR_SIM, default 0.97) returns the nearest
     neighbour's prediction with `confidence` decayed by similarity. Cuts
     inference calls dramatically when production traffic drifts within a
     tight resource neighbourhood.

Qdrant has no native per-key TTL, so entries carry an `expires_at` epoch in
their payload; expired points are filtered out of both layers at query time
and best-effort deleted. `clear()` drops the whole collection.

Cache failures (Qdrant down, malformed entries, missing client lib) NEVER
block placement — `get()` returns None on any exception, `put()` returns
False. Deployments without Qdrant reachable degrade to inference cleanly.
"""
from __future__ import annotations

import hashlib
import json
from product_names import themed_logger
import os
import struct
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional

logger = themed_logger(__name__, 'scheduler')

VECTOR_DIM = 64
COLLECTION_NAME = os.getenv("PREDICTION_CACHE_QDRANT_COLLECTION", "prediction_cache")
KEY_PREFIX = "prediction:"  # retained for back-compat / logging; not a Qdrant key
DEFAULT_TTL_SECONDS = int(os.getenv("PREDICTION_CACHE_TTL_SECONDS", "300"))
MIN_VECTOR_SIM = float(os.getenv("PREDICTION_CACHE_MIN_VECTOR_SIM", "0.97"))

# Stable UUID namespace so the same fingerprint always maps to the same point ID.
_PC_NAMESPACE = uuid.UUID("6f9b1e2c-3a4d-5e6f-7a8b-9c0d1e2f3a4b")

_qdrant_client = None
_collection_ready: Optional[bool] = None  # tri-state: None=unknown, True/False


def _resolve_url() -> Optional[str]:
    """Resolve the Qdrant URL. Prefer the cache-specific var, fall back to the
    shared platform QDRANT_URL. None (cache disabled) only when neither is set."""
    return os.getenv("PREDICTION_CACHE_QDRANT_URL") or os.getenv("QDRANT_URL")


def _resolve_api_key() -> str:
    """API key, verbatim (Qdrant rejects a dropped header vs an empty one)."""
    key = os.getenv("PREDICTION_CACHE_QDRANT_API_KEY")
    if key is None:
        key = os.getenv("QDRANT_API_KEY", "")
    return key


def _get_client():
    """Return a Qdrant client; cached on the module. None if unreachable/unconfigured."""
    global _qdrant_client
    if _qdrant_client is not None:
        return _qdrant_client
    url = _resolve_url()
    if not url:
        logger.debug("prediction_cache: no Qdrant URL configured — cache disabled")
        return None
    try:
        from qdrant_client import QdrantClient
        # `:memory:` runs an in-process Qdrant (used by tests); a real URL hits
        # the platform mod_qdrant.
        if url == ":memory:":
            client = QdrantClient(location=":memory:")
        else:
            client = QdrantClient(url=url, api_key=_resolve_api_key(), timeout=5)
        # Cheap reachability probe — surfaces a dead endpoint at construction.
        client.get_collections()
        _qdrant_client = client
        return _qdrant_client
    except Exception as e:
        logger.debug("prediction_cache: Qdrant unreachable, cache disabled (%s)", e)
        _qdrant_client = None
        return None


def _ensure_collection(client) -> bool:
    """Idempotently create the vector collection. Cached in module state so we
    don't re-check on every call. False on any failure → callers no-op cleanly."""
    global _collection_ready
    if _collection_ready is not None:
        return _collection_ready
    try:
        from qdrant_client.models import Distance, VectorParams
        existing = {c.name for c in client.get_collections().collections}
        if COLLECTION_NAME not in existing:
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )
            logger.info("prediction_cache: created Qdrant collection %s", COLLECTION_NAME)
        _collection_ready = True
    except Exception as e:
        msg = str(e).lower()
        if "already exists" in msg:
            _collection_ready = True
        else:
            logger.warning("prediction_cache: collection init failed: %s", e)
            _collection_ready = False
    return _collection_ready


def fingerprint(node_kind: str, hw_profile_id: int, contention_summary: Dict[str, Any]) -> str:
    """Stable SHA256 over the cache key triple. Sorted JSON ensures the same
    contention dict hashes the same regardless of key insertion order."""
    payload = json.dumps(
        {
            "dag": node_kind,
            "hw": int(hw_profile_id) if hw_profile_id is not None else 0,
            "ctx": contention_summary,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _point_id(fp: str) -> str:
    """Deterministic UUID point ID for a fingerprint (Qdrant needs UUID/int IDs)."""
    return str(uuid.uuid5(_PC_NAMESPACE, fp))


def _contention_vector_list(contention_summary: Dict[str, Any]) -> List[float]:
    """Build the 64-float contention embedding.

    The first slots map to the well-known resource axes (cpu/ram/vram/gpu/
    disk/net at start, peak, end + their deltas) so the embedding has
    meaningful neighbours for similar concurrent loads. Trailing slots are
    zero-padded reserved space — keeps the dim stable across feature schema
    evolutions without invalidating existing entries.
    """
    keys = (
        "cpu_pct", "ram_pct", "vram_pct", "gpu_pct", "disk_pct", "net_pct",
        "cpu_pct_peak", "ram_pct_peak", "vram_pct_peak", "gpu_pct_peak",
        "disk_pct_peak", "net_pct_peak",
        "concurrent_task_count",
        "duration_estimate_s",
        "input_size_log_mb",
        "image_cached",
        "model_warm",
    )
    vec = [0.0] * VECTOR_DIM
    for i, k in enumerate(keys):
        if i >= VECTOR_DIM:
            break
        v = contention_summary.get(k)
        if v is None:
            continue
        try:
            vec[i] = float(v)
        except (TypeError, ValueError):
            vec[i] = 0.0
    return vec


def encode_contention_vector(contention_summary: Dict[str, Any]) -> bytes:
    """Pack the 64-float contention vector into FLOAT32 little-endian bytes.

    Retained for API/back-compat (callers/tests that inspect the raw encoding);
    the Qdrant path itself upserts the float list from `_contention_vector_list`.
    """
    return struct.pack(f"{VECTOR_DIM}f", *_contention_vector_list(contention_summary))


def _is_expired(payload: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    if not payload:
        return True
    exp = payload.get("expires_at")
    if exp is None:
        return False
    try:
        return float(exp) < (now if now is not None else time.time())
    except (TypeError, ValueError):
        return False


def get(node_kind: str, hw_profile_id: int, contention_summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Two-layer lookup. Returns the cached PredictionResult-shaped dict or
    None on miss. Never raises — failures fall through to inference."""
    client = _get_client()
    if client is None:
        return None
    if not _ensure_collection(client):
        return None

    now = time.time()
    fp = fingerprint(node_kind, hw_profile_id, contention_summary)
    pid = _point_id(fp)

    # Layer 1: exact-match retrieve by deterministic point ID.
    try:
        points = client.retrieve(
            collection_name=COLLECTION_NAME, ids=[pid], with_payload=True
        )
        if points:
            payload = points[0].payload or {}
            if not _is_expired(payload, now):
                raw = payload.get("result")
                if raw:
                    data = json.loads(raw)
                    data["_cache_layer"] = "exact"
                    return data
            else:
                # Best-effort reap of the stale point.
                try:
                    client.delete(collection_name=COLLECTION_NAME, points_selector=[pid])
                except Exception:
                    pass
    except Exception as e:
        logger.debug("prediction_cache.get exact lookup failed: %s", e)

    # Layer 2: vector ANN fallback (only if the query vector is non-degenerate).
    vec = _contention_vector_list(contention_summary)
    if not any(vec):
        return None
    try:
        from qdrant_client.models import (
            FieldCondition, Filter, Range,
        )
        # Exclude expired points at query time.
        query_filter = Filter(
            must=[FieldCondition(key="expires_at", range=Range(gte=now))]
        )
        hits = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vec,
            limit=1,
            score_threshold=MIN_VECTOR_SIM,
            query_filter=query_filter,
            with_payload=True,
        )
        if not hits:
            return None
        hit = hits[0]
        # Qdrant COSINE returns similarity directly (1.0 = identical).
        import math
        similarity = float(hit.score)
        if math.isnan(similarity) or math.isinf(similarity) or similarity < MIN_VECTOR_SIM:
            return None
        payload = hit.payload or {}
        raw = payload.get("result")
        if not raw:
            return None
        data = json.loads(raw)
        # Decay confidence by similarity so callers can de-weight ANN hits.
        if "confidence" in data and isinstance(data["confidence"], (int, float)):
            data["confidence"] = float(data["confidence"]) * similarity
        data["_cache_layer"] = "vector"
        data["_cache_similarity"] = similarity
        return data
    except Exception as e:
        logger.debug("prediction_cache.get vector lookup failed: %s", e)
        return None


def put(
    node_kind: str,
    hw_profile_id: int,
    contention_summary: Dict[str, Any],
    prediction: Any,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """Store a prediction under a deterministic point ID with its contention
    vector so both the exact-match and ANN layers can find it. Accepts a dict,
    a `to_dict()`-able, a plain object, or a dataclass. Returns True on success,
    False on any failure (never raises)."""
    client = _get_client()
    if client is None:
        return False
    if not _ensure_collection(client):
        return False

    if hasattr(prediction, "to_dict"):
        pred_dict = prediction.to_dict()
    elif hasattr(prediction, "__dict__"):
        pred_dict = {k: v for k, v in prediction.__dict__.items() if not k.startswith("_")}
    elif isinstance(prediction, dict):
        pred_dict = prediction
    else:
        try:
            pred_dict = asdict(prediction)
        except TypeError:
            logger.debug("prediction_cache.put got non-serialisable prediction: %s", type(prediction))
            return False

    fp = fingerprint(node_kind, hw_profile_id, contention_summary)
    pid = _point_id(fp)
    vec = _contention_vector_list(contention_summary)
    # Qdrant COSINE can't normalise an all-zero vector; nudge a reserved slot so
    # the point is storable (negligible effect on similarity vs real vectors).
    if not any(vec):
        vec[VECTOR_DIM - 1] = 1e-6

    try:
        from qdrant_client.models import PointStruct
        payload = {
            "result": json.dumps(pred_dict, default=str),
            "expires_at": time.time() + max(int(ttl), 1),
            "fingerprint": fp,
        }
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(id=pid, vector=vec, payload=payload)],
        )
        return True
    except Exception as e:
        logger.debug("prediction_cache.put failed: %s", e)
        return False


def clear() -> int:
    """Drop every cached prediction. Returns the approximate count removed. For
    ops use (e.g. wiping the cache after a bad model promotion)."""
    client = _get_client()
    if client is None:
        return 0
    deleted = 0
    try:
        try:
            deleted = int(client.count(collection_name=COLLECTION_NAME, exact=True).count)
        except Exception:
            deleted = 0
        client.delete_collection(collection_name=COLLECTION_NAME)
    except Exception as e:
        logger.warning("prediction_cache.clear failed: %s", e)
    finally:
        global _collection_ready
        _collection_ready = None
    return deleted


def reset_for_tests():
    """Test-only: drop the module-level client + collection-ready flags so a
    fresh fixture / mocked Qdrant takes effect."""
    global _qdrant_client, _collection_ready
    _qdrant_client = None
    _collection_ready = None
