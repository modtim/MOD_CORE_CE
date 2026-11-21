"""Shared text embedding — the platform standard (all-MiniLM-L6-v2, 384-dim).

The API already embeds with this model in nodes_ingester / docs_ingester /
remediation_vector_store (each loads its own copy). This centralizes it for the
user vector store so that documents (on upsert) and queries embed through the
EXACT same model — a correctness requirement for RAG retrieval. The existing
modules can adopt this helper later; kept behavior-identical (same model name)
so results match.

Raises EmbeddingUnavailable if sentence-transformers isn't installed / the model
can't load — the vector endpoints map that to 503 (never a silent wrong-dim
write).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("MOD_EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = int(os.getenv("MOD_EMBED_DIM", "384"))  # all-MiniLM-L6-v2 native

# Cap concurrent in-API embeds so RAG ingestion can't monopolize CPU / the
# request threadpool and starve the rest of the platform. Endpoints run in a
# threadpool (sync defs), so this bounds how many can encode() at once; excess
# requests wait briefly. Heavy ingestion should offload to a node/GPU (see the
# RAG plan) — this is the safety valve until then.
_EMBED_MAX_CONCURRENCY = max(1, int(os.getenv("MOD_EMBED_MAX_CONCURRENCY", "2")))
_embed_gate = threading.BoundedSemaphore(_EMBED_MAX_CONCURRENCY)

_model = None
_model_name_loaded = None  # which model the cached _model actually is
# Serialize model (re)loads. torch/sentence-transformers model construction is
# NOT thread-safe: two threads loading at once (e.g. the startup warmup racing
# the first KB ingest, or an ingest racing a RAG query) corrupt the weights onto
# torch's `meta` device, and the broken handle then gets cached in `_model` —
# poisoning EVERY later embed in the process ("Cannot copy out of meta tensor")
# until a restart. This lock makes the load single-flight.
_model_lock = threading.Lock()


class EmbeddingUnavailable(RuntimeError):
    """The embedding model can't be loaded — map to 503, never write bad vectors."""


def _stack_pin():
    """(stack_id, served_model) if the assistant's User-RAG embedding store is
    pinned to a MOD Stack, else None. Fail-safe like `resolve_model_name`."""
    try:
        from utils.tool_config import read_tool_config
        raw = read_tool_config("assistant") or {}
        if raw.get("embedding_provider_user_rag") == "stack":
            stack_id = raw.get("embedding_stack_id_user_rag")
            if stack_id:
                return stack_id, raw.get("embedding_model_user_rag") or None
    except Exception as e:  # noqa: BLE001
        logger.debug("user-rag embedding stack pin lookup failed: %s", e)
    return None


def _embed_via_stack(texts: List[str]) -> Optional[List[List[float]]]:
    """Best-effort: embeds via the pinned stack, or None (caller falls back to
    the in-process model) when no stack is pinned. Raises EmbeddingUnavailable
    (the module's existing error, mapped to 503 by the vector endpoints) when
    a stack IS pinned but can't serve — GE.AIC.1-3's "no silent fallback to
    another model"."""
    pin = _stack_pin()
    if pin is None:
        return None
    stack_id, served_model = pin
    from database import SessionLocal
    from services.ai_stack_provider import resolve_stack_endpoint, embed_via_stack, StackProviderError
    db = SessionLocal()
    try:
        base_url, model_name = resolve_stack_endpoint(db, stack_id, served_model)
    except StackProviderError as e:
        raise EmbeddingUnavailable(str(e))
    finally:
        db.close()
    try:
        return embed_via_stack(base_url, model_name, texts)
    except StackProviderError as e:
        raise EmbeddingUnavailable(str(e))


def resolve_model_name() -> str:
    """The embedding model to use RIGHT NOW for the RAG/user-vector path.

    Platform admins can pin a self-hosted model for the AI Assistant (RAG) via
    Settings → AI Configuration → the Embedding model dropdown; that pin lives in
    the `assistant` tool's config. When set it wins; otherwise the platform
    default (all-MiniLM-L6-v2). Read fresh + fail-safe so a Vault hiccup never
    breaks embedding."""
    try:
        from utils.tool_config import read_tool_config
        pin = (read_tool_config("assistant") or {}).get("embedding_model_user_rag")
        if pin and str(pin).strip():
            return str(pin).strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("embedding pin lookup failed (using default): %s", e)
    return MODEL_NAME


def invalidate_model_cache() -> None:
    """Drop the cached model so the next embed reloads the (possibly newly
    pinned) model. Called after an admin changes the embedding-model pin."""
    global _model, _model_name_loaded
    _model = None
    _model_name_loaded = None


def _load_model(want: str):
    """Construct the SentenceTransformer for `want` and prove it is fully
    materialized before returning. A concurrent or lazy load can leave the
    weights on torch's `meta` device (no data); encoding then throws "Cannot
    copy out of meta tensor". Forcing one tiny encode here makes such a load
    fail NOW — inside the caller's lock — so a broken model is never cached."""
    from sentence_transformers import SentenceTransformer
    from utils.embedding_model_store import ensure_model
    # MinIO-first, strictly OFFLINE at runtime. allow_hf=False means a missing
    # model raises at once rather than doing a ~40s blocking HuggingFace fetch,
    # and local_files_only=True stops SentenceTransformer from doing an online
    # freshness HEAD-check on the (already-local) model dir. Either network call,
    # if made while a caller holds an open DB transaction, pins that connection
    # "idle in transaction" for the whole retry window — a live pool-exhaustion /
    # "API down" cause. The one-time HF SEED stays online via seed_from_hf.
    hf_id = want if "/" in want else f"sentence-transformers/{want}"
    local_dir = os.getenv("MOD_EMBED_MODEL_DIR_BASE", "/app/models/embeddings")
    path = ensure_model(hf_id=hf_id, local_dir=f"{local_dir}/{want}", name=want, allow_hf=False)
    model = SentenceTransformer(path, local_files_only=True)
    model.encode(["_"])  # materialization self-check (raises if on `meta`)
    return model


def _get_model():
    global _model, _model_name_loaded
    want = resolve_model_name()
    # Fast path — already loaded and current (no lock in the common case).
    if _model is not None and _model_name_loaded == want:
        return _model
    # Single-flight the (re)load: torch model init is not thread-safe, and a
    # concurrent load corrupts the model onto the `meta` device (see _model_lock).
    with _model_lock:
        if _model is not None and _model_name_loaded == want:  # re-check under lock
            return _model
        try:
            model = _load_model(want)
        except Exception as e:  # ImportError or model-load/materialization failure
            # NEVER leave a broken model cached — reset so the next call reloads
            # cleanly instead of reusing a poisoned handle forever.
            _model = None
            _model_name_loaded = None
            raise EmbeddingUnavailable(f"embedding model unavailable: {e}")
        _model = model
        _model_name_loaded = want
        logger.info("Loaded shared embedding model: %s", want)
        return _model


def current_dim() -> int:
    """The output dimension of the model in effect right now (loads it if
    needed). Used by the vector-store migration to size the new collection."""
    stack_vec = _embed_via_stack(["_"])
    if stack_vec is not None:
        return len(stack_vec[0])
    dim = _get_model().get_sentence_embedding_dimension()
    return int(dim)


def current_model_name() -> str:
    """Name of the model currently in effect (the pin, or the default)."""
    return resolve_model_name()


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of strings → list of vectors (each length EMBED_DIM).
    Serialized to _EMBED_MAX_CONCURRENCY concurrent callers to protect the API
    from a large ingestion pinning every CPU."""
    stack_vecs = _embed_via_stack(list(texts))
    if stack_vecs is not None:
        return stack_vecs
    model = _get_model()
    try:
        with _embed_gate:
            out = model.encode(list(texts))
    except Exception as e:  # noqa: BLE001
        # Belt-and-suspenders: if a cached model is on torch's `meta` device
        # (poisoned by any path), drop it and reload ONCE rather than failing
        # every embed until a restart. Re-raise anything unrelated unchanged.
        msg = str(e).lower()
        if "meta tensor" not in msg and "meta device" not in msg:
            raise
        logger.warning("embedding model was on meta device — reloading once: %s", e)
        invalidate_model_cache()
        model = _get_model()
        with _embed_gate:
            out = model.encode(list(texts))
    return [v.tolist() if hasattr(v, "tolist") else list(v) for v in out]


def embed_query(text: str) -> List[float]:
    """Embed a single query string → one vector."""
    return embed_texts([text])[0]
