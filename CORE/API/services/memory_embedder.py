"""expr 9.8 (slice 2) — embedder factory for memory nodes.

Provides :func:`embed_fn_for` which returns a callable matching the
``embed_fn(text) -> list[float]`` signature expected by
``memory_node_handlers.execute_memory_node``.  The callable is a closure
over a single :class:`wizard_ml.embeddings.EmbeddingClient` instance and
enforces three invariants:

  1. Input text is str()-coerced, then capped at the registered
     ``expr.memory_embed_max_chars`` setting (default 8000).  Empty
     input after stripping raises ``ValueError`` — the node handler maps
     exceptions to ``{"status": "error", "reason": "invalid_node_input"}``.

  2. The returned vector length is validated against
     ``workflow_memory_vector.DIM`` (768) — the dimension the vector
     store persists.  A mismatch raises ``ValueError``.

  3. The text payload is never logged (the model name / stack
     configuration may be, but not the text).

The factory returns ``None`` when the ``expr.memory_nodes_enabled`` flag
is off or when the embedding client cannot be constructed.  No platform
setting is required to turn the embedder on — the flag is the only gate.

This module registers its own platform-setting key at import time via the
``psr.register`` pattern (same as ``workflow_memory_quota.py``).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from sqlalchemy.orm import Session

from services import platform_settings as psr
try:
    from services.wizard_ml.embeddings import get_embedding_client
except (ImportError, ModuleNotFoundError):
    get_embedding_client = None  # type: ignore
from services.workflow_memory_vector import DIM as _VEC_DIM

logger = logging.getLogger(__name__)

MAX_CHARS_KEY = "expr.memory_embed_max_chars"
DEFAULT_MAX_CHARS = 8000


def _validate_max_chars(value) -> None:
    """Range: 1..100000."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 100000:
        raise ValueError(f"must be 1..100000, got {value!r}")


# Register the setting so callers can adjust it without touching env vars.
psr.register(
    MAX_CHARS_KEY,
    int,
    DEFAULT_MAX_CHARS,
    validator=_validate_max_chars,
    group="expr",
    description="expr 9.8 (slice 2) — maximum input character length for "
                "memory embedder calls.  Text longer than this is truncated. "
                "Default 8000; 1..100000. No env var.",
)


def embed_fn_for(db: Optional[Session], tenant_id: Optional[int]) -> Optional[Callable[[Any], List[float]]]:
    """Return an embed closure or ``None``.

    Returns ``None`` when:

      * ``expr.memory_nodes_enabled`` is off for this tenant.
      * ``wizard_ml.embeddings.get_embedding_client()`` cannot construct a
        client (import failure, missing model, stack endpoint unreachable).

    Otherwise returns a callable that matches the ``embed_fn`` signature
    expected by ``memory_node_handlers.execute_memory_node``:
    ``embed_fn(text) -> list[float]``.
    """
    # Gate 1: feature flag.
    try:
        from services.memory_node_handlers import MEMORY_FLAG_KEY
        on = bool(psr.get_setting(MEMORY_FLAG_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001
        return None
    if not on:
        return None

    # Gate 2: build the embedding client.
    if get_embedding_client is None:
        return None
    try:
        client = get_embedding_client()
        expected_dim = client.dim()
    except Exception:  # noqa: BLE001
        logger.warning("memory embedder: client construction failed")
        return None

    # Read the char cap at factory time; the registered default is used
    # when nothing is explicitly set.  Any invalid value falls through to
    # the default via the psr layer (coercion + validator).
    try:
        max_chars = psr.get_setting(MAX_CHARS_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        max_chars = DEFAULT_MAX_CHARS
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = max(1, min(max_chars, 100000))

    # Gate 3: dimension sanity — if the client reports a width different
    # from the vector store's DIM, refuse to produce vectors (caller gets
    # None).  This catches misconfiguration before the node handler.
    if expected_dim != _VEC_DIM:
        logger.warning(
            "memory embedder dim mismatch: client=%d, vector_store=%d",
            expected_dim, _VEC_DIM,
        )
        return None

    max_dim = expected_dim

    def _embed(text_in: Any) -> List[float]:
        """str-coerce, cap, reject empty, embed, validate dim."""
        text = str(text_in)
        if len(text) > max_chars:
            text = text[:max_chars]
        if not text.strip():
            raise ValueError("empty text")
        vec = client.embed(text)
        if len(vec) != max_dim:
            raise ValueError(
                f"embedding dim mismatch: expected {max_dim}, got {len(vec)}"
            )
        return vec

    return _embed
