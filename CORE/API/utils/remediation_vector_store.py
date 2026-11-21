"""Semantic-similarity backing store for the build-remediation cache.

Tier 4 follow-up A — Tim's request to make the system "even more
intelligent" by matching paraphrased / similar errors, not just
identical hashes. Companion to ``BuildRemediation`` (postgres
authoritative store) and the existing exact-match path.

How the two layers cooperate:

  1. Imagebuilder hits a build failure and posts the (raw) error
     text + signature to ``GET /node_definitions/remediation/lookup``.
  2. The endpoint first tries an EXACT match by signature (sha256 of
     the normalised error). Cheap; ~5ms; covers re-runs of the same
     error verbatim.
  3. On exact-match miss, embed the normalised error and ask qdrant
     for the closest stored error. If cosine similarity is above
     SIMILARITY_THRESHOLD (default 0.85), return the diff associated
     with that match — typically a paraphrased version of an error
     we've already learned a fix for.
  4. On record, embed the new error and upsert into qdrant alongside
     the postgres row, so future semantic lookups see it.

What this DOESN'T do:
* Generate embeddings of diffs themselves (only errors).
* Anything cross-tenant — the table is platform-wide platform
  knowledge, same as the exact-match cache.
* Cluster / group similar errors (Tier 4-B's job: trust scoring +
  promotion to known_failures).

Failure modes (all fail-open, never raise to caller):
* sentence-transformers not installed → disable
* qdrant unreachable → disable; the postgres exact-match path still
  works.
* Collection doesn't exist yet → create on first upsert.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
# qdrant-hardening: pass the key VERBATIM (incl "" when unset). qdrant enables
# auth on an empty key too, so a dropped header → 401; "" matches an empty-key
# instance and the real value matches a provisioned one.
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")  # verbatim: qdrant empty-key auth rejects a dropped header
COLLECTION = "build_remediations_v1"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 native dim
# Default cosine cut-off for "this is the same error". 0.85 was the
# sweet spot in early calibration: 0.7 admits false positives (e.g.
# "no space left on device" matches "permission denied"); 0.95 is so
# tight it barely improves over exact-match.
SIMILARITY_THRESHOLD = float(os.getenv("MOD_REMEDIATION_SIM_THRESHOLD", "0.85"))


_model = None
_collection_ready = False


def _get_embedding_model():
    """Return the cached SentenceTransformer or None if unavailable."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            from utils.embedding_model_store import ensure_model
            # Local-only offline load. This runs on the PERIODIC remediation
            # canary; the old bareword load resolved against HuggingFace and,
            # with no network, blocked ~40s and left _model=None so it retried
            # on every tick — pinning a DB connection each time. allow_hf=False
            # + local_files_only make it a fast local load with no network.
            path = ensure_model(
                hf_id="sentence-transformers/all-MiniLM-L6-v2",
                local_dir="/app/models/embeddings/all-MiniLM-L6-v2",
                name="all-MiniLM-L6-v2",
                allow_hf=False,
            )
            _model = SentenceTransformer(path, local_files_only=True)
            logger.info("Loaded embedding model for remediation cache: all-MiniLM-L6-v2 (offline)")
        except Exception as e:  # ImportError or model-load fail
            logger.info("Embedding model unavailable for remediation cache: %s", e)
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
            url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=5, **hop_kwargs
        )
    except internal_tls.InternalTLSConfigError:
        # Fail-closed: a `require` refusal is NOT a qdrant outage. The bare
        # handler below swallows everything and returns None, which would
        # silently degrade the semantic cache (fail-open) — so the refusal
        # re-raises and the caller's own fail-open path stays the outage.
        raise
    except Exception as e:
        logger.info("qdrant unavailable for remediation cache: %s", e)
        return None


def _ensure_collection(client) -> bool:
    """Create the qdrant collection if it doesn't exist. Cached on a
    module-level flag so the first upsert pays the round-trip and
    subsequent calls are free."""
    global _collection_ready
    if _collection_ready:
        return True
    try:
        from qdrant_client.models import Distance, VectorParams
        existing = {c.name for c in client.get_collections().collections}
        if COLLECTION not in existing:
            client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIM, distance=Distance.COSINE,
                ),
            )
            logger.info("Created qdrant collection: %s", COLLECTION)
        _collection_ready = True
        return True
    except Exception as e:
        logger.info("could not ensure remediation collection: %s", e)
        return False


def embed_error(text: str) -> Optional[List[float]]:
    """Embed the (already-normalised) error tail. Returns ``None``
    when the model is unavailable so callers can skip the semantic
    lookup gracefully."""
    if not text:
        return None
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        # SentenceTransformer.encode returns a numpy array; tolist()
        # for serialisation across the qdrant wire / pytest
        # comparisons.
        vec = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
        return vec.tolist()
    except Exception as e:
        logger.info("embed_error failed: %s", e)
        return None


def upsert_remediation_vector(remediation_id: int, embedding: List[float]) -> bool:
    """Store the embedding for a build_remediations row. Idempotent
    via point id == remediation_id, so subsequent recordings of the
    same fix overwrite cleanly. Returns True on success."""
    if not embedding:
        return False
    client = _get_qdrant_client()
    if client is None:
        return False
    if not _ensure_collection(client):
        return False
    try:
        from qdrant_client.models import PointStruct
        client.upsert(
            collection_name=COLLECTION,
            points=[
                PointStruct(
                    id=int(remediation_id),
                    vector=embedding,
                    payload={"remediation_id": int(remediation_id)},
                ),
            ],
            wait=False,
        )
        return True
    except Exception as e:
        logger.info("qdrant upsert failed: %s", e)
        return False


def find_similar_remediation_id(
    embedding: List[float],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
    top_k: int = 5,
) -> Optional[tuple[int, float]]:
    """Cosine search qdrant for the most-similar stored error.
    Returns ``(remediation_id, score)`` if the best match clears
    threshold, ``None`` otherwise.

    The caller is expected to fetch the actual diff from postgres by
    remediation_id — this module deliberately doesn't store diffs in
    qdrant (postgres remains the source of truth for diff bodies).
    """
    if not embedding:
        return None
    client = _get_qdrant_client()
    if client is None:
        return None
    if not _ensure_collection(client):
        return None
    try:
        results = client.search(
            collection_name=COLLECTION,
            query_vector=embedding,
            limit=max(1, int(top_k)),
            with_payload=False,
        )
    except Exception as e:
        logger.info("qdrant search failed: %s", e)
        return None
    if not results:
        return None
    top = results[0]
    if top.score < threshold:
        return None
    return int(top.id), float(top.score)
