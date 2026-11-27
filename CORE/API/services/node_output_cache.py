"""workflow-agentic-execution-loop §workflow-node-output-cache — content-
addressable cache of node outputs.

Cache key = `sha256(image_ref || '|' || input_content_hash || '|' ||
canonical_json(config) || '|' || tenant_id)`. The four components together
uniquely identify a deterministic execution: same image + same input bytes
+ same config + same tenant means the same output, and the cached row
short-circuits dispatch.

Per-tenant scoping is part of the unique constraint (so two tenants with
byte-identical (image, input, config) tuples don't collide and don't see
each other's outputs).

Two surfaces:

- `compute_cache_key(image_ref, input_content_hash, config, tenant_id)`
  - pure function, deterministic, returns a 64-hex SHA256 string

- `lookup(db, *, image_ref, input_content_hash, config, tenant_id)`
  - returns the cached NodeOutputCache row if a hit exists, None
    otherwise. Updates `last_accessed_at` on hit.

- `record(db, *, image_ref, input_content_hash, config, tenant_id,
  output_pointer, output_content_hash)`
  - upserts the cache row. Best-effort: a failed write logs a warning
    but never breaks the caller.

The orchestrator + worker call these as a black box; they don't need to
know about the key format.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _canonical_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, NaN/
    Infinity disallowed. The same input dict always produces the same
    bytes so the hash is reproducible.
    """
    try:
        return json.dumps(
            value if value is not None else {},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError):
        # Fall back to repr() for non-JSON-serialisable inputs. Less
        # robust to dict-order changes but produces SOMETHING stable.
        return repr(value)


def compute_cache_key(
    *,
    image_ref: str,
    input_content_hash: str,
    config: Optional[Dict[str, Any]],
    tenant_id: int,
) -> str:
    """Compute the 64-hex cache key. Pure function, side-effect free."""
    payload = (
        (image_ref or "")
        + "|"
        + (input_content_hash or "")
        + "|"
        + _canonical_json(config)
        + "|"
        + str(int(tenant_id))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_components(
    image_ref: str,
    input_content_hash: str,
    config: Optional[Dict[str, Any]],
    tenant_id: int,
) -> tuple[str, str, str, int]:
    """Split the cache key into its four normalised components, which
    is what the table's UNIQUE constraint enforces. We don't store the
    SHA256 composite directly; instead we store each component so a
    debugger can read the row without re-hashing inputs.
    """
    # image_digest column accepts up to 80 chars — truncate but keep
    # enough of the image ref to remain unique in practice.
    image_digest = (image_ref or "")[:80]
    # input_content_hash + config_hash columns are 64-char SHA256.
    cfg_hash = hashlib.sha256(
        _canonical_json(config).encode("utf-8")
    ).hexdigest()
    return (image_digest, input_content_hash or "", cfg_hash, int(tenant_id))


def lookup(
    db: Session,
    *,
    image_ref: str,
    input_content_hash: str,
    config: Optional[Dict[str, Any]],
    tenant_id: int,
):
    """Look up a cache row by its component key. Returns the row on
    hit and bumps last_accessed_at; returns None on miss.

    Per the spec, this is the read-short-circuit path. Callers gate
    invocation behind the per-tenant `output_cache_enabled` flag.
    """
    if not input_content_hash:
        # Without an input hash, there's nothing to key on. Bail
        # rather than insert garbage rows / generate false hits.
        return None

    import models
    image_digest, input_hash, cfg_hash, t_id = _key_components(
        image_ref, input_content_hash, config, tenant_id,
    )

    row = (
        db.query(models.NodeOutputCache)
        .filter(
            models.NodeOutputCache.image_digest == image_digest,
            models.NodeOutputCache.input_content_hash == input_hash,
            models.NodeOutputCache.config_hash == cfg_hash,
            models.NodeOutputCache.tenant_id == t_id,
        )
        .first()
    )
    if row is None:
        return None

    # Bump last_accessed_at so the daily sweep doesn't drop hot rows.
    try:
        from sqlalchemy import func as sa_func
        row.last_accessed_at = sa_func.now()
        db.flush()
    except Exception:
        # Read still succeeded; the timestamp update is best-effort.
        pass
    return row


def record(
    db: Session,
    *,
    image_ref: str,
    input_content_hash: str,
    config: Optional[Dict[str, Any]],
    tenant_id: int,
    output_pointer: str,
    output_content_hash: str,
) -> Optional[int]:
    """Upsert a cache row capturing the node's completed output. Returns
    the row id on success, None on failure.

    Always-on per the spec — the cache accumulates whether or not the
    read flag is enabled, so flipping the flag on later picks up
    immediate benefit from prior runs.

    Idempotent against the UNIQUE(image_digest, input_content_hash,
    config_hash, tenant_id) constraint: a duplicate insert is caught
    and treated as success (the existing row is the cache hit). We
    DON'T overwrite an existing pointer because the existing row is
    by definition the same output (same key components).
    """
    if not input_content_hash or not output_content_hash:
        return None

    import models
    image_digest, input_hash, cfg_hash, t_id = _key_components(
        image_ref, input_content_hash, config, tenant_id,
    )

    _savepoint_failed = False
    try:
        # First try the fast path: insert. If the unique key already
        # exists, we get a constraint error and fall through to the
        # "find existing" path.
        row = models.NodeOutputCache(
            image_digest=image_digest,
            input_content_hash=input_hash,
            config_hash=cfg_hash,
            tenant_id=t_id,
            output_pointer=output_pointer,
            output_content_hash=output_content_hash,
        )
        # The insert runs inside a SAVEPOINT so a duplicate-key failure rolls
        # back ONLY this cache row. A bare db.rollback() here used to discard
        # the caller's still-uncommitted rows in the same session (the
        # WorkflowNodeOutput + OutputSignature rows _record_task_outputs had
        # just added), so every run whose cache key already existed completed
        # with its output in result_payload but no workflow_node_outputs row.
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except Exception:
            _savepoint_failed = True
            raise
        db.commit()
        return row.id
    except Exception as e:
        # Idempotent — duplicate key means we already have an entry for
        # this exact (image, input, config, tenant). The savepoint already
        # undid the failed insert (caller's pending rows stay); only a
        # failure of the final commit needs a full rollback.
        if not _savepoint_failed:
            try:
                db.rollback()
            except Exception:
                pass
        try:
            existing = (
                db.query(models.NodeOutputCache)
                .filter(
                    models.NodeOutputCache.image_digest == image_digest,
                    models.NodeOutputCache.input_content_hash == input_hash,
                    models.NodeOutputCache.config_hash == cfg_hash,
                    models.NodeOutputCache.tenant_id == t_id,
                )
                .first()
            )
            if existing is not None:
                return existing.id
        except Exception:
            pass
        logger.warning(
            "node_output_cache: record failed for tenant=%s image=%s input=%s: %s",
            tenant_id, image_digest, input_hash, e,
        )
        return None


def is_cache_enabled_for_tenant(db: Session, tenant_id: int) -> bool:
    """Per-tenant gate for cache-read short-circuit. Defaults to False
    (the column's server_default). Reading the cache requires the flag
    to be on; WRITING the cache is always-on regardless of the flag.
    """
    if not tenant_id:
        return False
    import models
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if tenant is None:
        return False
    return bool(getattr(tenant, "output_cache_enabled", False))


# ---------------------------------------------------------------------
# Daily sweep — TTL + orphaned pointer cleanup
# ---------------------------------------------------------------------

# Default 90 days per spec.
_DEFAULT_TTL_DAYS = 90


def sweep_stale_cache(db: Session, ttl_days: int = _DEFAULT_TTL_DAYS) -> int:
    """Remove cache rows whose last_accessed_at is older than the TTL.
    Returns the number of rows deleted. Idempotent — re-running on the
    same set is a no-op.

    Orphaned-pointer cleanup (rows whose output_pointer references a
    missing MinIO object) is a separate concern handled by the storage
    reconciler; this sweep is TTL-only.
    """
    from sqlalchemy import text as _sa_text
    result = db.execute(_sa_text("""
        DELETE FROM node_output_cache
        WHERE last_accessed_at < now() - (:ttl || ' days')::interval
    """), {"ttl": str(ttl_days)})
    deleted = result.rowcount or 0
    try:
        db.commit()
    except Exception:
        pass
    if deleted:
        logger.info(
            "node_output_cache sweep: deleted %d stale rows (ttl=%d days)",
            deleted, ttl_days,
        )

    # Section F §10.2 — gauge the cache size after the sweep so
    # observability dashboards see the post-eviction footprint.
    try:
        from sqlalchemy import text as _t
        row_count = db.execute(_t(
            "SELECT count(*) FROM node_output_cache"
        )).scalar() or 0
        from services import wizard_metrics
        wizard_metrics.set_cache_size(rows=int(row_count))
    except Exception:
        pass

    return deleted
