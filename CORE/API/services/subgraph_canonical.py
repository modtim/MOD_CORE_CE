"""Subgraph canonicalisation + canonical hashing + equivalence grouping.

Pure analysis core for the workflow-semantic-deduplication background analyzer
(tasks.md 3.5). The daily APScheduler job + `subgraph_equivalence_index`
persistence are LEAD follow-ups; this module only computes canonical forms,
hashes, and equivalence groupings. No DB, no I/O, no env.

graph_json shape (confirmed against services/workflow_orchestrator.py):
  nodes[]: {"id", "type", "position", "data": {... node-definition ref + config ...}}
  edges[]: {"id", "source", "target", "sourceHandle", "targetHandle", ...}

Spec: the internal design notes
 - byte-equivalent = identical canonicalised JSON
 - cross-tenant equivalence only visible when tenants opt in (privacy setting)
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator

# ---------------------------------------------------------------------------
# Volatile fields stripped before canonicalisation (explicit, per brief).
# Node top-level keys: ids/positions/UI-only.
VOLATILE_NODE_KEYS = frozenset({
    "id", "position", "x", "y", "z", "zIndex", "width", "height",
    "selected", "highlighted", "hovered", "ui", "style", "icon", "color",
})
# Keys inside node["data"]: display labels/names are UI-only; the node-definition
# refs (nodeDefinitionId/nodeDefinitionName/node_kind) and config are KEPT.
VOLATILE_DATA_KEYS = frozenset({
    "label", "name", "displayName", "title", "description", "notes",
    "icon", "color", "position", "x", "y", "selected", "locked",
})
# Edge keys: ids and pure-rendering attributes. Handles (sourceHandle/targetHandle)
# are KEPT: port wiring is semantic.
VOLATILE_EDGE_KEYS = frozenset({
    "id", "label", "style", "color", "animated", "selected",
    "markerStart", "markerEnd", "strokeColor", "type", "width",
})

# Upper bound on connected subgraphs enumerate_subgraphs yields per workflow.
# The analyzer processes one workflow at a time; anything past the cap is
# simply not emitted (documented truncation, not an error).
MAX_SUBGRAPHS_PER_WORKFLOW = 5000

# Upper bound on the number of label-permutation candidates
# canonicalize_subgraph will brute-force when WL refinement leaves tied
# classes (see canonicalize_subgraph docstring). Subgraphs produced by
# enumerate_subgraphs are tiny (<= max_nodes, default 6), so this is never hit
# in the analyzer's own pipeline; it only guards direct callers passing
# pathologically symmetric, oversized node_ids sets.
MAX_TIEBREAK_PERMUTATIONS = 5000


def _strip(obj: dict, drop: frozenset[str]) -> dict:
    return {k: v for k, v in obj.items() if k not in drop}


def canonicalize_subgraph(graph_json: dict, node_ids: set[str]) -> dict:
    """Canonical form of the induced subgraph on `node_ids`.

    Keeps only the chosen nodes and edges whose endpoints are both chosen;
    strips volatile fields; relabels nodes as "0".."k-1" via deterministic
    Weisfeiler-Lehman-style refinement (topological + content ordering) so two
    isomorphic subgraphs with different node ids canonicalise identically.
    Edge direction is preserved (source->target). Output keys are sorted by
    json.dumps(sort_keys=True) at hash time.
    """
    nodes = [n for n in (graph_json.get("nodes") or []) if n.get("id") in node_ids]
    edges = [
        e for e in (graph_json.get("edges") or [])
        if e.get("source") in node_ids and e.get("target") in node_ids
    ]

    def node_content(n: dict) -> dict:
        data = _strip(dict(n.get("data") or {}), VOLATILE_DATA_KEYS)
        return {
            "type": n.get("type"),
            "data": data,
            "inputs": sorted(_norm_handles((data.get("inputs") or []))),
            "outputs": sorted(_norm_handles((data.get("outputs") or []))),
        }

    def _norm_handles(items: list) -> list[str]:
        out = []
        for it in items:
            out.append(it.get("name") if isinstance(it, dict) else str(it))
        return [str(h) for h in out if h]

    # --- deterministic relabelling (WL refinement) -------------------------
    # Initial signature = node content. Iteratively refine with the multiset of
    # (direction, neighbour signature).
    ids = sorted(n.get("id") for n in nodes)
    content = {n["id"]: node_content(n) for n in nodes}
    succ = defaultdict(list)
    pred = defaultdict(list)
    for e in edges:
        succ[e["source"]].append((e.get("sourceHandle"), e.get("targetHandle"), e["target"]))
        pred[e["target"]].append((e.get("sourceHandle"), e.get("targetHandle"), e["source"]))

    sig = {nid: json.dumps(content[nid], sort_keys=True) for nid in ids}
    for _ in range(len(ids)):
        new = {}
        for nid in ids:
            down = sorted(sig[t] for (_, _, t) in succ[nid])
            up = sorted(sig[t] for (_, _, t) in pred[nid])
            edge_marks = sorted(f"{sh}->{th}" for (sh, th, _) in succ[nid] + pred[nid])
            new[nid] = json.dumps([sig[nid], down, up, edge_marks], sort_keys=True)
        if new == sig:
            break
        sig = new

    # WL refinement alone is NOT a canonical labelling: when a tied class has
    # edges *within* it (e.g. two interchangeable nodes wired crosswise into
    # two other interchangeable nodes), breaking ties by the arbitrary
    # original id is not automorphism-safe and can yield different canonical
    # forms for the same graph under different id relabellings. Subgraphs here
    # are small and bounded (enumerate_subgraphs caps node count), so
    # brute-force every permutation within each WL-tied class and keep the
    # lexicographically smallest resulting form. This IS a true canonical
    # labelling: an isomorphism between two equivalent graphs maps tied
    # classes onto tied classes one-to-one, so both graphs explore the same
    # set of candidate integer-labelled forms and their minima coincide.
    classes: dict[str, list[str]] = defaultdict(list)
    for nid in ids:
        classes[sig[nid]].append(nid)
    class_groups = [sorted(v) for _, v in sorted(classes.items())]

    combo_count = 1
    for grp in class_groups:
        combo_count *= math.factorial(len(grp))
        if combo_count > MAX_TIEBREAK_PERMUTATIONS:
            break

    def _build(order: list[str]) -> dict:
        # Note: the WL-refinement `sig` strings are internal bookkeeping only
        # (used above to find tied classes) -- they are NOT included in the
        # canonical output. They grow with each refinement round (each round
        # nests the previous round's JSON-encoded strings), so embedding them
        # would bloat/slow hashing for no benefit; `content` already captures
        # everything semantically distinguishing about a node.
        label = {nid: str(i) for i, nid in enumerate(order)}
        canon_nodes = [
            {"id": label[nid], "content": content[nid]}
            for nid in order
        ]
        canon_edges = []
        for e in edges:
            keep = _strip({k: v for k, v in e.items() if k not in {"source", "target"}}, VOLATILE_EDGE_KEYS)
            canon_edges.append({
                "source": label[e["source"]],
                "target": label[e["target"]],
                **keep,
            })
        canon_edges.sort(key=lambda ce: json.dumps(ce, sort_keys=True))
        return {"nodes": canon_nodes, "edges": canon_edges, "node_count": len(order)}

    if combo_count <= MAX_TIEBREAK_PERMUTATIONS:
        best: dict | None = None
        best_key: str | None = None
        for perms in itertools.product(*(itertools.permutations(grp) for grp in class_groups)):
            order = [nid for grp_perm in perms for nid in grp_perm]
            candidate = _build(order)
            key = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
            if best_key is None or key < best_key:
                best_key, best = key, candidate
        return best

    # Pathologically symmetric / oversized input beyond MAX_TIEBREAK_PERMUTATIONS:
    # fall back to id-stable ordering (documented limitation -- not guaranteed
    # canonical across relabelling for such inputs; never hit via
    # enumerate_subgraphs, whose outputs are always tiny).
    order = sorted(ids, key=lambda nid: (sig[nid], nid))
    return _build(order)


def canonical_hash(canon: dict) -> str:
    """sha256 over the canonical JSON (byte-equivalence key, spec scenario 1)."""
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def enumerate_subgraphs(
    graph_json: dict,
    *,
    min_nodes: int = 2,
    max_nodes: int = 6,
) -> Iterator[set[str]]:
    """Yield connected node-id sets of size [min_nodes, max_nodes].

    Connected = connected in the UNDIRECTED view (an induced subgraph is emitted
    once; direction does not affect connectivity). Bounded: at most
    MAX_SUBGRAPHS_PER_WORKFLOW sets are yielded per workflow — after the cap the
    iterator simply stops (the analyzer logs truncation).
    """
    ids = [n.get("id") for n in (graph_json.get("nodes") or []) if n.get("id") is not None]
    id_set = set(ids)
    nbr = defaultdict(set)
    for e in graph_json.get("edges") or []:
        s, t = e.get("source"), e.get("target")
        if s in id_set and t in id_set and s != t:
            nbr[s].add(t)
            nbr[t].add(s)

    # Trotter-Weisbrod reverse-search enumeration (RSC): every connected
    # induced subgraph is output exactly once, seeded at its smallest vertex.
    # Bounded: at most MAX_SUBGRAPHS_PER_WORKFLOW sets per workflow.
    sorted_ids = sorted(ids)
    rank = {nid: i for i, nid in enumerate(sorted_ids)}
    yielded = 0
    _out: list[set[str]] = []
    seen: set[frozenset[str]] = set()

    def extend(current: tuple[str, ...], cand: list[str]) -> None:
        nonlocal yielded
        if yielded >= MAX_SUBGRAPHS_PER_WORKFLOW:
            return
        if len(current) >= min_nodes:
            key = frozenset(current)
            if key not in seen:
                seen.add(key)
                yielded += 1
                _out.append(set(current))
        if len(current) >= max_nodes or yielded >= MAX_SUBGRAPHS_PER_WORKFLOW:
            return
        for v in cand:
            if yielded >= MAX_SUBGRAPHS_PER_WORKFLOW:
                return
            nxt = sorted(
                {u for u in nbr[v] if rank[u] > rank[current[0]]}
                | {u for u in cand if u != v}
            )
            nxt = [u for u in nxt if u not in current]
            extend(current + (v,), nxt)

    for seed in sorted_ids:
        if yielded >= MAX_SUBGRAPHS_PER_WORKFLOW:
            break
        extend((seed,), [u for u in sorted_ids if rank[u] > rank[seed] and u in nbr[seed]])
        while _out:
            yield _out.pop(0)
            if yielded >= MAX_SUBGRAPHS_PER_WORKFLOW:
                return


def group_equivalent(
    items: Iterable[tuple[object, object, str]],
    *,
    cross_tenant_opt_in: set[int] | None = None,
) -> dict[str, list[tuple[object, object]]]:
    """Group (tenant_id, workflow_id) occurrences by canonical hash.

    Cross-tenant rule (spec "Cross-tenant deduplication is opt-in"): a group
    spans tenants ONLY when every tenant represented in that group has opted
    in. Otherwise the hash key is tenant-scoped, so each tenant sees its own
    group and never another tenant's workflows.

    Returns {hash_key: [(tenant_id, workflow_id), ...]} where hash_key is the
    bare canonical hash when a group may span tenants, and
    f"{tenant_id}:{hash}" when it must stay tenant-scoped.
    """
    opted = set(cross_tenant_opt_in or ())
    buckets: dict[str, list[tuple[object, object]]] = defaultdict(list)
    for tenant_id, workflow_id, h in items:
        if int(tenant_id) in opted:
            key = h
        else:
            key = f"{tenant_id}:{h}"
        buckets[key].append((tenant_id, workflow_id))

    # A bare-hash bucket is cross-tenant only if ALL its tenants opted in; opted
    # tenants are the only ones that produced bare keys, so this holds by
    # construction. Non-opted tenants can never join it (their key is scoped).
    return dict(buckets)
