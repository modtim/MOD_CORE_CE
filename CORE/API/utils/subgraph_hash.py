"""EP.3.5a — canonical subgraph hashing (utils/subgraph_hash.py).

Pure-stdlib fingerprints of canvas graphs so the daily analyzer job can later
dedupe workflow versions by subgraph equivalence (the equivalence table itself
lands in a later task).

Contract (locked):
* :func:`node_signature` hashes only the SEMANTIC node content: the bound
  ``nodeDefinitionId`` (falling back to ``node_kind`` / editor ``type``) plus
  the node ``config`` with sorted keys and normalized values (floats via
  ``repr``, NaN/Inf dropped).  Node ids, canvas positions, labels, UI
  bookkeeping keys and secret-looking config keys never reach the hash input.
* :func:`subgraph_hash` fingerprints the induced subgraph over a node set:
  invariant to node ids and list ordering, sensitive to edge direction and to
  ``sourceHandle``/``targetHandle`` names.  Weisfeiler-Lehman refinement
  (3 iterations, separate in/out neighbour multisets labelled with handle
  names), then sha256 of the sorted final node-label multiset plus the sorted
  edge-label multiset.  Deterministic across processes (no Python ``hash()``).
* :func:`connected_subgraphs` enumerates weakly-connected node sets in a size
  window, each exactly once, ESU-style extension, bounded by ``limit``.

Malformed nodes/edges (missing ids, edges to unknown nodes) are ignored,
never raised.  Graphs above :data:`MAX_GRAPH_NODES` raise ValueError.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from typing import Iterable, Iterator

MAX_GRAPH_NODES = 2000
WL_ITERATIONS = 3

_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|secret|token|credential|api[ _-]?key|key)",
    re.IGNORECASE,
)

_UI_ONLY_KEYS = frozenset(
    {
        "id",
        "position",
        "label",
        "type",
        "selected",
        "dragging",
        "width",
        "height",
        "z",
        "zIndex",
        "className",
        "style",
        "editable",
        "draggable",
        "locked",
        "isLocked",
        "hidden",
        "parentNode",
        "selectedFeatures",
        "isModified",
        "offsetX",
        "offsetY",
    }
)


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(value: object) -> object:
    """Recursively normalize a config value into deterministic JSON scalars."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {
            str(key): _normalize(val)
            for key, val in value.items()
            if not _SECRET_KEY_RE.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_normalize(item) for item in sorted(value, key=_canonical_json)]
    return str(value)


def _semantic_payload(node: dict) -> dict:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    definition = data.get("nodeDefinitionId")
    if definition is None:
        definition = data.get("node_kind") or node.get("type")
    config = data.get("config")
    if not isinstance(config, dict):
        config = {}
    semantic_config = {
        str(key): _normalize(val)
        for key, val in config.items()
        if str(key) not in _UI_ONLY_KEYS and not _SECRET_KEY_RE.search(str(key))
    }
    return {"definition": _normalize(definition), "config": semantic_config}


def node_signature(node: dict) -> str:
    """sha256 of the canonical JSON of a node's semantic content only."""
    return _sha256(_canonical_json(_semantic_payload(node)))


def _index_nodes(graph: dict) -> dict:
    nodes: dict = {}
    raw = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            nid = item.get("id")
            if not isinstance(nid, str) or nid in nodes:
                continue
            nodes[nid] = item
    return nodes


def _valid_edges(graph: dict, known: set) -> list:
    """Directed (source, target, sourceHandle, targetHandle) with known ends."""
    edges: list = []
    raw = graph.get("edges") if isinstance(graph, dict) else None
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            if source not in known or target not in known:
                continue
            source_handle = item.get("sourceHandle")
            target_handle = item.get("targetHandle")
            edges.append(
                (
                    source,
                    target,
                    source_handle if isinstance(source_handle, str) else "",
                    target_handle if isinstance(target_handle, str) else "",
                )
            )
    return edges


def subgraph_hash(graph: dict, node_ids: Iterable[str]) -> str:
    """Hash of the induced subgraph over ``node_ids`` (id/order invariant)."""
    nodes = _index_nodes(graph)
    if len(nodes) > MAX_GRAPH_NODES:
        raise ValueError(f"graph too large for hashing: {len(nodes)} nodes (max {MAX_GRAPH_NODES})")
    selected = sorted({nid for nid in node_ids if isinstance(nid, str) and nid in nodes})
    known = set(selected)
    labels = {nid: node_signature(nodes[nid]) for nid in selected}
    edges = _valid_edges(graph, known)
    for _ in range(WL_ITERATIONS):
        outgoing: dict = {nid: [] for nid in selected}
        incoming: dict = {nid: [] for nid in selected}
        for source, target, source_handle, target_handle in edges:
            outgoing[source].append((source_handle, labels[target]))
            incoming[target].append((target_handle, labels[source]))
        labels = {
            nid: _sha256(_canonical_json([labels[nid], sorted(outgoing[nid]), sorted(incoming[nid])]))
            for nid in selected
        }
    edge_labels = sorted(
        f"{labels[source]}|{labels[target]}|{source_handle}|{target_handle}"
        for source, target, source_handle, target_handle in edges
    )
    return _sha256(_canonical_json([sorted(labels.values()), edge_labels]))


def connected_subgraphs(
    graph: dict,
    *,
    min_size: int = 2,
    max_size: int = 6,
    limit: int = 5000,
) -> Iterator:
    """Weakly-connected node sets in the size window, each exactly once."""
    nodes = _index_nodes(graph)
    if len(nodes) > MAX_GRAPH_NODES:
        raise ValueError(f"graph too large for enumeration: {len(nodes)} nodes (max {MAX_GRAPH_NODES})")
    if max_size < min_size or max_size < 1 or limit <= 0:
        return
    adjacency: dict = {nid: set() for nid in nodes}
    for source, target, _sh, _th in _valid_edges(graph, set(nodes)):
        if source != target:
            adjacency[source].add(target)
            adjacency[target].add(source)
    order = {nid: index for index, nid in enumerate(sorted(nodes))}
    emitted = 0
    for root in sorted(nodes):
        root_rank = order[root]
        seen: set = set()
        stack = deque([(frozenset({root}), frozenset(n for n in adjacency[root] if order[n] > root_rank))])
        while stack:
            sub, candidates = stack.pop()
            if min_size <= len(sub) <= max_size and sub not in seen:
                seen.add(sub)
                yield sub
                emitted += 1
                if emitted >= limit:
                    return
            if len(sub) >= max_size:
                continue
            for w in sorted(candidates, key=lambda n: order[n], reverse=True):
                nxt = (candidates | adjacency[w]) - sub - {w}
                nxt = frozenset(n for n in nxt if order[n] > root_rank)
                stack.append((sub | {w}, nxt))
