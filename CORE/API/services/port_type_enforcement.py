"""workflow-agentic-execution-loop §port-type-enforcement — port
type compatibility + adapter auto-bridging for wizard-proposed graphs.

Surfaces:

- `are_types_compatible(src_type, tgt_type)` — pure check. Treats
  `any` and `NONE` as wildcards (matches the wizard catalog's
  conventional "no type information" sentinels). Empty strings on
  either side are tolerant — type info is best-effort in the catalog.

- `AdapterRegistry` — in-memory lookup of system-shared NodeDefinitions
  with `is_adapter=True`, indexed by (input_type, output_type). Reload
  on demand (cheap: hits a single indexed SELECT).

- `find_adapter(registry, src_type, tgt_type)` — single-hop lookup.
  Returns the NodeDefinition row if one exists for (src_type ->
  tgt_type), None otherwise. Multi-hop chains are deliberately NOT
  supported — `image -> tensor -> embedding` requires two adapters,
  too speculative to auto-insert without user intent.

- `is_enforcement_enabled_for_tenant(db, tenant_id)` — per-tenant flag
  gate. The validator hard-rejects type mismatches unconditionally;
  the auto-insert path only fires when this flag is on.

- `synthesise_adapter_insertion(src_node_id, tgt_node_id, edge_id,
  adapter_def)` — returns the (new adapter node entry, replacement
  edge_a, replacement edge_b) to splice into a wizard proposal. The
  inserted node is tagged `auto_inserted_adapter: True` in its data
  block so downstream code + the UI can surface that this was a
  system insertion rather than the user's intent.

The validator in `routers/support_chat.py` is the only caller; this
module is intentionally cheap to import (no DB at import time).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Sentinels in NodeDefinition.input_schema / output_schema that mean
# "no type info" — treated as wildcards so legacy / loosely-typed nodes
# don't trigger spurious rejects.
_WILDCARD_TYPES = {"", "any", "NONE", "none", None}

# Generic file-container types. A node that produces one of these emits an
# arbitrary uploaded file (e.g. "Resource Input" → 'resource'), and a node
# that accepts one consumes any file. Either way the *concrete* format lives
# in the bytes, not the port label — a 'resource'/'file' CAN carry a 'pdf',
# so it's compatible with any concrete type on the other side. Without this,
# the wizard treats 'resource' vs 'pdf' as a mismatch and invents nonsense
# bridge nodes ("PDF to PDF Converter") to cross the gap.
_GENERIC_FILE_TYPES = {"resource", "file", "binary", "blob"}


def _norm(t: Any) -> str:
    """Normalise a type string for comparison. lower() so 'JSON' ==
    'json'; trim whitespace; wildcards become a single sentinel."""
    if t in _WILDCARD_TYPES:
        return ""
    s = str(t).strip().lower()
    return "" if s in {"any", "none"} else s


def are_types_compatible(src_type: Any, tgt_type: Any) -> bool:
    """Return True if a port of `src_type` can feed a port of `tgt_type`.

    Rules (deliberately permissive — false-positives degrade gracefully
    by surfacing as runtime errors, false-negatives prevent valid graphs
    from being proposed):

      - Wildcards on either side match anything.
      - Exact match (case-insensitive) compatible.
      - Otherwise incompatible.

    The wizard catalog occasionally encodes comma-separated input types
    (e.g. "audio,text") for multi-input nodes — split on comma and
    accept if the src type is in the set.
    """
    s = _norm(src_type)
    t = _norm(tgt_type)
    if not s or not t:
        return True
    if s == t:
        return True
    # A generic file container on either side carries an arbitrary file, so
    # the concrete format on the other side is compatible (resource/file ->
    # pdf, image -> file, …). The runtime validates the actual bytes.
    if s in _GENERIC_FILE_TYPES or t in _GENERIC_FILE_TYPES:
        return True
    # multi-accept target: "audio,text" accepts "audio" or "text".
    if "," in t:
        accept = {_norm(x) for x in t.split(",")}
        return s in accept
    if "," in s:
        produce = {_norm(x) for x in s.split(",")}
        return t in produce
    return False


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

class AdapterRegistry:
    """In-memory index of system-shared adapter NodeDefinitions.

    Keyed by (input_type, output_type). When multiple adapters cover
    the same hop (rare — implies a duplicate node definition), the
    one with the lowest cost_in_credits wins; ties broken by lowest id.

    Built lazily on first use, refreshed via `reload(db)`.
    """

    def __init__(self) -> None:
        # (in_type, out_type) -> NodeDefinition
        self._by_hop: Dict[tuple[str, str], Any] = {}
        self._loaded: bool = False

    def reload(self, db: Session) -> int:
        """Rebuild the index from the DB. Returns row count loaded."""
        import models
        rows = (
            db.query(models.NodeDefinition)
            .filter(
                models.NodeDefinition.is_adapter == True,  # noqa: E712
                models.NodeDefinition.tenant_id.is_(None),
            )
            .all()
        )
        new_index: Dict[tuple[str, str], Any] = {}
        for r in rows:
            in_t = _norm(((r.input_schema or {}) or {}).get("input_type"))
            out_t = _norm(((r.output_schema or {}) or {}).get("output_type"))
            if not in_t or not out_t:
                # No type info → not usable as a typed bridge. Skip.
                continue
            key = (in_t, out_t)
            existing = new_index.get(key)
            if existing is None:
                new_index[key] = r
            else:
                # Prefer cheaper, then lower id for stability.
                e_cost = getattr(existing, "cost_in_credits", 0) or 0
                r_cost = getattr(r, "cost_in_credits", 0) or 0
                if (r_cost, r.id) < (e_cost, existing.id):
                    new_index[key] = r
        self._by_hop = new_index
        self._loaded = True
        logger.info(
            "port_type_enforcement: adapter registry reloaded (%d hops)",
            len(new_index),
        )
        return len(new_index)

    def find(self, src_type: Any, tgt_type: Any):
        """Single-hop lookup: returns the NodeDefinition that produces
        `tgt_type` from `src_type`, or None."""
        s = _norm(src_type)
        t = _norm(tgt_type)
        if not s or not t or s == t:
            return None
        return self._by_hop.get((s, t))

    def size(self) -> int:
        return len(self._by_hop)


# Module-level singleton — the validator and tests share it.
_registry: AdapterRegistry = AdapterRegistry()


def get_registry(db: Optional[Session] = None) -> AdapterRegistry:
    """Return the process-wide adapter registry. If `db` is provided
    and the registry hasn't been loaded yet, load it. Callers that
    need fresh data should call `_registry.reload(db)` explicitly."""
    if db is not None and not _registry._loaded:
        try:
            _registry.reload(db)
        except Exception as e:
            logger.warning(
                "port_type_enforcement: initial registry load failed: %s", e,
            )
    return _registry


def find_adapter(
    registry: AdapterRegistry,
    src_type: Any,
    tgt_type: Any,
):
    """Convenience pass-through to `registry.find()` so the validator
    doesn't need to know the registry's internal API."""
    return registry.find(src_type, tgt_type)


# ---------------------------------------------------------------------
# Per-tenant flag gate
# ---------------------------------------------------------------------

def is_enforcement_enabled_for_tenant(db: Session, tenant_id: int) -> bool:
    """Read the per-tenant `port_type_enforcement_enabled` flag. When
    OFF (the default), the validator still rejects type mismatches but
    does NOT auto-insert adapters — the existing hard-fail behaviour."""
    if not tenant_id:
        return False
    import models
    t = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if t is None:
        return False
    return bool(getattr(t, "port_type_enforcement_enabled", False))


# ---------------------------------------------------------------------
# Proposal mutation: synthesise the insertion
# ---------------------------------------------------------------------

def synthesise_adapter_insertion(
    *,
    src_node_id: str,
    tgt_node_id: str,
    edge: Dict[str, Any],
    adapter_def: Any,
    new_node_id_prefix: str = "auto_adapter_",
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Build the splice payload for inserting `adapter_def` between
    `src_node_id` and `tgt_node_id`. Returns:

      (adapter_node_entry, edge_src_to_adapter, edge_adapter_to_tgt)

    The caller drops the original `edge`, appends the two replacement
    edges, and appends `adapter_node_entry` to the proposal's nodes.
    The adapter node is tagged `auto_inserted_adapter=True` so the
    Apply path can persist that flag into the workflow_nodes table
    (and the UI can highlight it).

    `edge.id` is preserved on edge_src_to_adapter; edge_adapter_to_tgt
    gets a derived id (`{edge.id}__adapter`) so both replacements are
    deterministic + introspectable.
    """
    # Generate a deterministic-ish id; collision with proposal-local
    # ids is the caller's responsibility to avoid (the validator does
    # this by checking node_index).
    new_node_id = f"{new_node_id_prefix}{edge.get('id', src_node_id + '_to_' + tgt_node_id)}"

    adapter_node_entry: Dict[str, Any] = {
        "id": new_node_id,
        "type": "default",  # process node, not input/output
        "data": {
            "nodeDefinitionName": adapter_def.name,
            "node_kind": adapter_def.node_kind,
            "auto_inserted_adapter": True,
            "adapter_reason": (
                f"bridge {_norm((adapter_def.input_schema or {}).get('input_type'))} "
                f"-> {_norm((adapter_def.output_schema or {}).get('output_type'))}"
            ),
        },
    }

    base_edge_id = edge.get("id") or f"{src_node_id}__{tgt_node_id}"
    edge_src_to_adapter: Dict[str, Any] = {
        "id": base_edge_id,
        "source": src_node_id,
        "target": new_node_id,
    }
    edge_adapter_to_tgt: Dict[str, Any] = {
        "id": f"{base_edge_id}__adapter",
        "source": new_node_id,
        "target": tgt_node_id,
    }
    # Preserve anchor_source on the first half (it still points at the
    # original src) and anchor_target on the second half (still points
    # at the original tgt).
    if edge.get("anchor_source"):
        edge_src_to_adapter["anchor_source"] = True
    if edge.get("anchor_target"):
        edge_adapter_to_tgt["anchor_target"] = True

    return adapter_node_entry, edge_src_to_adapter, edge_adapter_to_tgt
