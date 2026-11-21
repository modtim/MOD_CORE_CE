"""Embed + upsert code chunks to Qdrant.

Every codebase-analysis task creates its own ephemeral Qdrant
collection (``codebase:{task_id}``) so map/reduce/generate stages
can retrieve the exact chunks they need without scanning the whole
repo for each LLM call.

The embedding model is intentionally small and CPU-runnable — vLLM is
already pinned to the GPU and we don't want to compete for VRAM.
`Snowflake/snowflake-arctic-embed-s` (US-aligned, Apache 2.0) at
~110 MB gets loaded once per worker process and then stays hot.
Comparable MTEB quality to bge-small-en-v1.5; switched from BAAI to
satisfy the MOD Core aligned-provider models-only policy.

Qdrant client + sentence-transformers must be present in the
Containerizer image:

    pip install qdrant-client==1.12.1 sentence-transformers==3.3.1

If either isn't available, `index()` raises IndexUnavailable — the
caller is expected to skip the index stage gracefully.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import Iterable, Optional

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qdrant_models
    _QDRANT_OK = True
except ImportError:   # pragma: no cover
    _QDRANT_OK = False

try:
    from sentence_transformers import SentenceTransformer
    _ST_OK = True
except ImportError:   # pragma: no cover
    _ST_OK = False

from tools.chunk import Chunk


logger = logging.getLogger(__name__)


EMBED_MODEL_NAME = os.getenv("CODEBASE_EMBED_MODEL", "Snowflake/snowflake-arctic-embed-s")
EMBED_BATCH_SIZE = int(os.getenv("CODEBASE_EMBED_BATCH", "32"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
# qdrant-hardening: optional API key (None = no auth = today's behavior).
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")  # verbatim: qdrant empty-key auth rejects a dropped header
EMBED_DEVICE = os.getenv("CODEBASE_EMBED_DEVICE", "cpu")


class IndexUnavailable(RuntimeError):
    """Raised when Qdrant or the embedding model can't be loaded."""


# Lazy singleton — the model load is ~2 s and weights stay in RAM.
_MODEL: Optional["SentenceTransformer"] = None
_MODEL_ERROR: Optional[str] = None
_CLIENT: Optional["QdrantClient"] = None

# The generate container normally runs behind an egress allow-list that reaches
# the LLM host only, so the Hugging Face Hub is NOT reachable at run time. The
# image therefore ships the embedding model on disk (see the Dockerfile) and we
# load it from there; a network fetch is only the fallback when nothing is baked
# in AND the operator has not declared the container offline.
EMBED_BAKED_DIR = os.getenv("CODEBASE_EMBED_BAKED_DIR", "/opt/embed-models")


def _baked_model_path() -> Optional[str]:
    cand = os.path.join(EMBED_BAKED_DIR, EMBED_MODEL_NAME.split("/")[-1])
    return cand if os.path.isdir(cand) else None


def _embed_offline() -> bool:
    for name in ("CODEBASE_EMBED_OFFLINE", "HF_HUB_OFFLINE"):
        if os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def _get_model() -> "SentenceTransformer":
    """Load the embedding model, preferring the baked-in copy.

    A load failure raises IndexUnavailable (and is remembered, so later
    stages fail fast instead of re-attempting a download) — callers treat
    that as "no retrieval index": the pipeline carries on without RAG.
    """
    global _MODEL, _MODEL_ERROR
    if _MODEL is None:
        if _MODEL_ERROR:
            raise IndexUnavailable(_MODEL_ERROR)
        if not _ST_OK:
            raise IndexUnavailable(
                "sentence-transformers not installed — embed stage disabled"
            )
        baked = _baked_model_path()
        try:
            if baked:
                _MODEL = SentenceTransformer(baked, device=EMBED_DEVICE)
            elif _embed_offline():
                raise IndexUnavailable(
                    f"embedding model {EMBED_MODEL_NAME!r} is not baked into "
                    f"this image and the container is offline — retrieval "
                    f"index skipped"
                )
            else:
                _MODEL = SentenceTransformer(EMBED_MODEL_NAME, device=EMBED_DEVICE)
        except IndexUnavailable as exc:
            _MODEL_ERROR = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 — offline / hub error / bad cache
            _MODEL_ERROR = (
                f"embedding model {EMBED_MODEL_NAME!r} could not be loaded "
                f"({type(exc).__name__}) — retrieval index skipped"
            )
            raise IndexUnavailable(_MODEL_ERROR) from exc
    return _MODEL


def _get_client() -> "QdrantClient":
    global _CLIENT
    if _CLIENT is None:
        if not _QDRANT_OK:
            raise IndexUnavailable(
                "qdrant-client not installed — embed stage disabled"
            )
        _CLIENT = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=60.0)
    return _CLIENT


def index(
    task_id: str,
    chunks: Iterable[Chunk],
    *,
    collection_prefix: str = "codebase",
    recreate: bool = True,
) -> dict:
    """Embed every chunk and upsert into Qdrant.

    Returns `{"collection": name, "points": N, "dim": D}` so callers
    can log / persist the collection reference for subsequent
    retrievals.
    """
    chunks = list(chunks)
    if not chunks:
        return {"collection": None, "points": 0, "dim": 0, "skipped": "no chunks"}

    model = _get_model()
    client = _get_client()
    collection = f"{collection_prefix}:{task_id}"

    # Sample embedding to learn the vector dim — arctic-embed-s is 384
    # (same as the legacy bge-small it replaced). Treat as dynamic so
    # future model swaps don't break.
    sample = model.encode(["probe"], convert_to_numpy=True, show_progress_bar=False)
    dim = int(sample.shape[1])

    if recreate:
        _ensure_collection(client, collection, dim, recreate=True)
    else:
        _ensure_collection(client, collection, dim, recreate=False)

    # Encode in batches to keep memory bounded on huge repos.
    total = 0
    for batch in _batched(chunks, EMBED_BATCH_SIZE):
        texts = [c.text for c in batch]
        vectors = model.encode(
            texts, convert_to_numpy=True, show_progress_bar=False,
            batch_size=EMBED_BATCH_SIZE,
        )
        points = [
            qdrant_models.PointStruct(
                id=_point_id(c.id),
                vector=vec.tolist(),
                payload=_chunk_payload(c),
            )
            for c, vec in zip(batch, vectors)
        ]
        client.upsert(collection_name=collection, points=points, wait=False)
        total += len(points)

    logger.info("Indexed %d chunks into %s (dim=%d)", total, collection, dim)
    return {"collection": collection, "points": total, "dim": dim}


def search(
    collection: str,
    query: str,
    *,
    top_k: int = 12,
    role_filter: Optional[str] = None,
    language_filter: Optional[str] = None,
) -> list[dict]:
    """Retrieve top-K chunks for an LLM retrieval call."""
    client = _get_client()
    model = _get_model()
    qvec = model.encode([query], convert_to_numpy=True, show_progress_bar=False)[0]

    must = []
    if role_filter:
        must.append(qdrant_models.FieldCondition(
            key="role", match=qdrant_models.MatchValue(value=role_filter),
        ))
    if language_filter:
        must.append(qdrant_models.FieldCondition(
            key="language", match=qdrant_models.MatchValue(value=language_filter),
        ))
    flt = qdrant_models.Filter(must=must) if must else None

    res = client.search(
        collection_name=collection, query_vector=qvec.tolist(),
        limit=top_k, query_filter=flt,
    )
    return [
        {
            "score": r.score,
            "path": r.payload.get("path"),
            "symbol": r.payload.get("symbol"),
            "language": r.payload.get("language"),
            "kind": r.payload.get("kind"),
            "start_line": r.payload.get("start_line"),
            "end_line": r.payload.get("end_line"),
            "text": r.payload.get("text"),
            "framework_hints": r.payload.get("framework_hints") or [],
            "uses_gpu": bool(r.payload.get("uses_gpu")),
        }
        for r in res
    ]


def drop(collection: str) -> None:
    """Delete the ephemeral collection. Call at pipeline teardown."""
    client = _get_client()
    try:
        client.delete_collection(collection_name=collection)
    except Exception as e:
        logger.warning("Qdrant drop failed for %s: %s", collection, e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_collection(
    client: "QdrantClient", name: str, dim: int, *, recreate: bool,
) -> None:
    if recreate:
        client.recreate_collection(
            collection_name=name,
            vectors_config=qdrant_models.VectorParams(
                size=dim, distance=qdrant_models.Distance.COSINE,
            ),
        )
        return
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            collection_name=name,
            vectors_config=qdrant_models.VectorParams(
                size=dim, distance=qdrant_models.Distance.COSINE,
            ),
        )


def _chunk_payload(c: Chunk) -> dict:
    d = asdict(c)
    # Don't store the id as a payload field — it IS the point id.
    d.pop("id", None)
    # Cap text size on the payload to keep Qdrant fast on recall; the
    # full file can always be reconstructed from the repo if needed.
    d["text"] = d.get("text", "")[:8000]
    return d


def _point_id(chunk_id: str) -> int:
    """Qdrant accepts UUIDs or integers as point IDs. We derive a 63-bit
    integer from the chunk's sha1, which is cheaper to store than a UUID
    and deterministic across ingests of the same codebase."""
    return int(chunk_id[:16], 16) & ((1 << 63) - 1)


def _batched(seq, n):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf; buf = []
    if buf:
        yield buf
