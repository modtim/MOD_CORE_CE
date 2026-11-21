"""expr 9.8 (slice 1) — memory-operation node handlers (pure dispatcher module).

Intended call site (wiring task, tasks.md 9.8 follow-up): API-side node
execution next to ``_maybe_dispatch_control_flow`` in routers/worker_tasks.py.
A node whose kind is in :data:`MEMORY_NODE_KINDS` is intercepted there; its
``node_data`` config and the parent ``inputs`` are mapped onto the committed
workflow-memory services by :func:`execute_memory_node`, and the returned
envelope is stored as the node output.  This module never dispatches, never
commits (the caller owns the transaction), and never raises: every failure
mode is an envelope.

Envelope: ``{"status": "ok"|"refused"|"error", "outputs": {...}}`` plus
``"reason"`` for refused/error.  Quota refusals use the services' own
vocabulary: ``MemoryQuotaExceeded`` -> reason ``"memory_quota_exceeded"``.

Config/input mapping (node_data config first, inputs value first):
  memory_write_kv          key (nd)            value (in)
  memory_read_kv           key (nd)            -
  memory_embed_and_index   collection, doc_id, content_ref, metadata (nd)
                                              text (in), embedded via embed_fn
  memory_vector_search     collection, top_k (nd)
                                              vector | query|text (in)
  memory_timeseries_append series (nd)         points (in)
  memory_timeseries_query  series, start, end, limit, order,
                           bucket_seconds|minutes|hours, agg (nd)

Clamps (documented): ``top_k`` -> 1..MAX_K (100, services/workflow_memory_vector,
default DEFAULT_K=10); query ``limit`` -> 1..QUERY_LIMIT_MAX (10000,
services/workflow_memory_timeseries, default 1000).  Out-of-range values are
clamped; non-int values fall back to the default.  ``ttl_seconds`` (655):
memory_write_kv reads node config ``ttl_seconds`` and stamps the KV row's
``expires_at`` = now(UTC) + ttl, which the retention sweep then soft-deletes.
Non-int / <=0 / missing ttl means NO expiry; values above TTL_SECONDS_MAX
(31_536_000 = 1 year) are clamped to it.  A later rewrite of the same key
WITHOUT ttl clears the old expiry (write_kv's ON CONFLICT overwrites the
column), so the row's lifetime always follows the most recent write.

Flag: services/expr_feature_flags.py carries NO memory flag today (verified
2026-10-11: expr.phase2..10 + subgraph-analyzer keys only), so this module
registers its own per-tenant bool, ``expr.memory_nodes_enabled`` (default
``False`` for every tenant, same registry pattern as expr_feature_flags.py's
``_register_phase_key``). A tenant with the flag OFF (the default) gets
``{"status": "refused", "reason": "memory_disabled"}`` and no DB writes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy.exc import SQLAlchemyError

from services.workflow_memory_kv import MemoryQuotaExceeded as KvQuota, read_kv, write_kv
from services.workflow_memory_vector import (
    DEFAULT_K, MAX_K,
    MemoryQuotaExceeded as VecQuota, search, upsert_vector,
)
from services.workflow_memory_timeseries import (
    QUERY_LIMIT_MAX, MemoryQuotaExceeded as TsQuota,
    append_points, downsample, query_points,
)
from services import platform_settings as psr

logger = logging.getLogger(__name__)

MEMORY_NODE_KINDS = frozenset({
    "memory_write_kv", "memory_read_kv",
    "memory_embed_and_index", "memory_vector_search",
    "memory_timeseries_append", "memory_timeseries_query",
})

MEMORY_FLAG_KEY = "expr.memory_nodes_enabled"
_QUOTA_ERRORS = (KvQuota, VecQuota, TsQuota)
_MISSING = object()
_DEFAULT_QUERY_LIMIT = 1000

# 655 — memory_write_kv ttl clamp: 1 year in seconds (365 * 86400).
TTL_SECONDS_MAX = 31_536_000


def _validate_bool(value: Any) -> None:
    if value is None:
        raise ValueError("expr.memory_nodes_enabled must not be None")
    if not isinstance(value, (bool, int)):
        raise ValueError(
            f"expr.memory_nodes_enabled must be bool/int, got "
            f"{type(value).__name__}"
        )


psr.register(
    MEMORY_FLAG_KEY,
    bool,
    False,
    validator=_validate_bool,
    group="expr",
    description="expr 9.8 — per-tenant switch for the memory-operation node "
                "handlers (memory_write_kv/read_kv/embed_and_index/"
                "vector_search/timeseries_append/timeseries_query). Default "
                "False = OFF for every tenant until an operator opts in. "
                "Tenant row overrides the platform row. OFF refuses with "
                "reason memory_disabled and performs no DB writes.",
)


def _ok(outputs: dict) -> dict:
    return {"status": "ok", "outputs": outputs}


def _err(reason: str) -> dict:
    return {"status": "error", "outputs": {}, "reason": reason}


def _refused(reason: str) -> dict:
    return {"status": "refused", "outputs": {}, "reason": reason}


def _cfg(nd: Mapping, inp: Mapping, name: str) -> Any:
    """Config field: node_data first, inputs as a convenience fallback."""
    v = nd.get(name, _MISSING)
    return v if v is not _MISSING else inp.get(name, _MISSING)


def _opt(nd: Mapping, inp: Mapping, name: str) -> Any:
    """Optional field: None when absent (never the _MISSING sentinel)."""
    v = _cfg(nd, inp, name)
    return None if v is _MISSING else v


def _val(nd: Mapping, inp: Mapping, name: str) -> Any:
    """Value field: inputs first, node_data as a convenience fallback."""
    v = inp.get(name, _MISSING)
    return v if v is not _MISSING else nd.get(name, _MISSING)


def _clamp(raw: Any, default: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return max(1, min(raw, maximum))


def _ttl_expires_at(raw: Any) -> Optional[datetime]:
    """ttl_seconds -> absolute expires_at (tz-aware UTC), or None for no expiry.

    655: missing / non-int / bool / <=0 ttl means the row never expires.  A
    positive ttl is clamped to TTL_SECONDS_MAX (1 year) and resolved against
    now(UTC), so the node never has to know the server clock.
    """
    if raw is _MISSING or raw is None or isinstance(raw, bool) \
            or not isinstance(raw, int) or raw <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(
        seconds=min(raw, TTL_SECONDS_MAX))


def _coerce_ts(value: Any) -> Optional[datetime]:
    """ISO string or datetime -> tz-aware datetime, else None (naive rejected)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None
    return dt


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _bucket_seconds(nd: Mapping) -> Optional[int]:
    for key, mult in (("bucket_seconds", 1), ("bucket_minutes", 60),
                      ("bucket_hours", 3600)):
        raw = nd.get(key)
        if raw is not None:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                return None
            return raw * mult
    return None


def _embed_text(embed_fn: Optional[Callable[[Any], Any]], text: Any) -> Any:
    """embed_fn(text) -> embedding list; None when no embedder is available.

    No built-in embedder is wired here: services/wizard_pro_client.embed and
    services/ai_stack_provider.embed_via_stack are network/stack calls, which
    this module must not make, so the embedder is injected by the caller.
    The vector store is fixed-dimension (DIM floats), which the injected
    embed_fn must satisfy.
    """
    if embed_fn is None:
        return None
    return embed_fn(text)


def execute_memory_node(db, *, tenant_id: int, workflow_slug: str,
                        node_kind: str, node_data: Mapping,
                        inputs: Mapping, embed_fn=None) -> dict:
    """Run one memory node; returns the node-output envelope (never raises).

    Each service call runs inside ``db.begin_nested()`` so a failed call rolls
    back only itself; nothing here commits — the caller owns the transaction.
    DB failures log the exception CLASS NAME only (never raw values/text) and
    return an error envelope; the session stays usable after the rollback.
    """
    if node_kind not in MEMORY_NODE_KINDS:
        return _err("unknown_memory_node")
    nd = node_data if isinstance(node_data, Mapping) else {}
    inp = inputs if isinstance(inputs, Mapping) else {}
    try:
        with db.begin_nested():
            # Flag read runs inside the same savepoint as the dispatch call
            # so a DB-level failure here (e.g. an out-of-range tenant_id)
            # rolls back cleanly and is reported the same way as a failure
            # inside the service call, instead of poisoning the session.
            on = bool(psr.get_setting(MEMORY_FLAG_KEY, db=db,
                                      tenant_id=tenant_id))
            if not on:
                return _refused("memory_disabled")
            return _dispatch(db, tenant_id, workflow_slug, node_kind, nd, inp,
                             embed_fn)
    except _QUOTA_ERRORS:
        return _refused("memory_quota_exceeded")
    except SQLAlchemyError as exc:
        logger.warning("memory node db failure: %s", exc.__class__.__name__)
        return _err("db_error")
    except Exception as exc:  # noqa: BLE001 — class name only, never the value
        logger.warning("memory node validation failure: %s",
                       exc.__class__.__name__)
        return _err("invalid_node_input")


def _dispatch(db, tenant_id, workflow_slug, node_kind, nd, inp,
              embed_fn) -> dict:
    if node_kind == "memory_write_kv":
        key = _cfg(nd, inp, "key")
        value = _val(nd, inp, "value")
        if key is _MISSING or key is None or value is _MISSING:
            return _err("missing_required_field")
        expires_at = _ttl_expires_at(_cfg(nd, inp, "ttl_seconds"))
        res = write_kv(db, tenant_id=tenant_id, workflow_slug=workflow_slug,
                       key=key, value=value, expires_at=expires_at)
        return _ok({"key": res["key"], "size_bytes": res["size_bytes"],
                    "created": res["created"],
                    "expires_at": expires_at.isoformat()
                    if expires_at is not None else None})

    if node_kind == "memory_read_kv":
        key = _cfg(nd, inp, "key")
        if key is _MISSING or key is None:
            return _err("missing_required_field")
        value = read_kv(db, tenant_id=tenant_id, workflow_slug=workflow_slug,
                        key=key)
        return _ok({"key": key, "value": value, "found": value is not None})

    if node_kind == "memory_embed_and_index":
        collection = _cfg(nd, inp, "collection")
        doc_id = _cfg(nd, inp, "doc_id")
        text = _val(nd, inp, "text")
        if collection is _MISSING or collection is None or doc_id is _MISSING \
                or doc_id is None or text is _MISSING:
            return _err("missing_required_field")
        embedding = _embed_text(embed_fn, text)
        if embedding is None:
            return _err("no_embedder")
        res = upsert_vector(db, tenant_id=tenant_id,
                            workflow_slug=workflow_slug,
                            collection=collection, doc_id=doc_id,
                            embedding=embedding,
                            content_ref=_opt(nd, inp, "content_ref"),
                            metadata=_opt(nd, inp, "metadata"))
        return _ok(res)

    if node_kind == "memory_vector_search":
        collection = _cfg(nd, inp, "collection")
        if collection is _MISSING or collection is None:
            return _err("missing_required_field")
        top_k = _clamp(_cfg(nd, inp, "top_k"), DEFAULT_K, MAX_K)
        query = _val(nd, inp, "vector")
        if query is _MISSING:
            text = _val(nd, inp, "query")
            if text is _MISSING:
                text = _val(nd, inp, "text")
            if text is _MISSING:
                return _err("missing_required_field")
            query = _embed_text(embed_fn, text)
            if query is None:
                return _err("no_embedder")
        rows = search(db, tenant_id=tenant_id, workflow_slug=workflow_slug,
                      collection=collection, query_embedding=query, k=top_k)
        return _ok({"results": rows, "top_k": top_k})

    if node_kind == "memory_timeseries_append":
        series = _cfg(nd, inp, "series")
        points = _val(nd, inp, "points")
        if series is _MISSING or series is None or points is _MISSING:
            return _err("missing_required_field")
        if isinstance(points, list):
            pts = []
            for p in points:
                if isinstance(p, Mapping):
                    pts.append((_coerce_ts(p.get("ts")), p.get("value")))
                else:
                    pts.append(tuple(p))
            points = pts
        res = append_points(db, tenant_id=tenant_id,
                            workflow_slug=workflow_slug, series=series,
                            points=points)
        return _ok(res)

    # memory_timeseries_query
    series = _cfg(nd, inp, "series")
    if series is _MISSING or series is None:
        return _err("missing_required_field")
    start = _coerce_ts(_cfg(nd, inp, "start"))
    end = _coerce_ts(_cfg(nd, inp, "end"))
    if start is None or end is None:
        return _err("missing_required_field")
    limit = _clamp(_cfg(nd, inp, "limit"), _DEFAULT_QUERY_LIMIT,
                   QUERY_LIMIT_MAX)
    order = _cfg(nd, inp, "order")
    if order is _MISSING or order is None:
        order = "asc"
    rows = query_points(db, tenant_id=tenant_id, workflow_slug=workflow_slug,
                        series=series, start=start, end=end, limit=limit,
                        order=order)
    outputs = {"series": series,
               "points": [{"ts": _iso(r["ts"]), "value": r["value"],
                          "tags": r["tags"]} for r in rows],
               "count": len(rows), "limit": limit}
    bucket = _bucket_seconds(nd)
    agg = nd.get("agg")
    if bucket is not None and agg is not None:
        outputs["buckets"] = downsample(rows, timedelta(seconds=bucket), agg)
    return _ok(outputs)
