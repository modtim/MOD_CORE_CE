"""Sub-workflow reference graph: extraction, cycle detection, depth capping.

Operates on raw ``graph_json`` dicts — no DB, no imports from routers/services/models.

RefKey types returned by ``extract_refs``:
  - ``int`` – when the node carries ``workflow_id`` (brief/example format).
  - ``tuple[str, str]`` – when the node carries both ``tenant_slug`` and
    ``workflow_slug`` as *non-empty strings* (spec format).
  version_constraint is *not* part of the key.
"""

from __future__ import annotations

import typing
from typing import Callable, Iterable, Optional, Union

T = typing.TypeVar("T")

RefKey = Union[int, tuple[str, str]]

DEFAULT_MAX_DEPTH: int = 10


def _key_to_short(k: RefKey) -> str:
    """Return a short, id/slug-only string for error messages."""
    if isinstance(k, int):
        return str(k)
    return f"{k[0]}/{k[1]}"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def extract_refs(graph_json: dict) -> list[RefKey]:
    """Return every ref key from sub_workflow nodes.

    * Order is stable (insertion order, first occurrence).
    * Duplicates are de-duplicated.
    * Malformed / non-sub_workflow nodes are silently ignored.
    """
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return []

    seen: set[RefKey] = set()
    out: list[RefKey] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        ref = data.get("workflow_ref")
        if not isinstance(ref, dict):
            continue
        wid = ref.get("workflow_id")
        if isinstance(wid, int) and wid not in seen:
            seen.add(wid)
            out.append(wid)
            continue
        # spec-format slug key
        ts = ref.get("tenant_slug")
        ws = ref.get("workflow_slug")
        if isinstance(ts, str) and isinstance(ws, str) and ts and ws:
            key: RefKey = (ts, ws)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CycleError(ValueError):
    """Raised when a cycle is detected in the sub-workflow ref graph."""

    path: list[RefKey]

    def __init__(self, path: list[RefKey]) -> None:
        super().__init__(
            "cycle: " + " -> ".join(_key_to_short(k) for k in path),
        )
        self.path = path


class DepthExceededError(ValueError):
    """Raised when the ref chain exceeds the configured depth cap."""

    depth: int
    cap: int

    def __init__(self, depth: int, cap: int) -> None:
        super().__init__(f"depth {depth} exceeds cap {cap}")
        self.depth = depth
        self.cap = cap


# ---------------------------------------------------------------------------
# Graph algorithms (generic over RefKey)
# ---------------------------------------------------------------------------


def find_cycle(
    root_id: RefKey,
    load_refs: Callable[[RefKey], Iterable[RefKey]],
) -> Optional[list[RefKey]]:
    """Iterative DFS to find *any* cycle reachable from *root_id*.

    Returns the cycle path as ``[a, b, c, a]`` (closed loop) or ``None``.
    Each ref key is looked up at most once.

    Raises ``LookupError`` if a referenced key does not exist.
    """
    visited: set[RefKey] = set()
    stack: list[tuple[RefKey, list[RefKey]]] = [(root_id, [root_id])]

    while stack:
        node, path = stack.pop()
        if node in visited:
            continue
        visited.add(node)

        children = list(load_refs(node))
        for child in children:
            if child in path:
                idx = path.index(child)
                return path[idx:] + [child]
            if child not in visited:
                stack.append((child, path + [child]))

    return None


def max_depth(
    root_id: RefKey,
    load_refs: Callable[[RefKey], Iterable[RefKey]],
) -> int:
    """Longest ref-chain length from *root_id* (root alone = 0).

    Raises ``CycleError`` if a cycle exists.
    Raises ``LookupError`` on missing workflow.
    """
    cycle = find_cycle(root_id, load_refs)
    if cycle is not None:
        raise CycleError(cycle)

    # Iterative longest-path on a DAG using post-order traversal.
    memo: dict[RefKey, int] = {}
    work: list[RefKey] = [root_id]
    order: list[RefKey] = []
    seen: set[RefKey] = set()
    head = 0
    while head < len(work):
        n = work[head]
        head += 1
        if n in seen:
            continue
        seen.add(n)
        order.append(n)
        for c in load_refs(n):
            if c not in seen:
                work.append(c)
    for n in reversed(order):
        best = 0
        for c in load_refs(n):
            d = memo.get(c, 0) + 1
            if d > best:
                best = d
        memo[n] = best
    return memo.get(root_id, 0)


def check_composition(
    root_id: RefKey,
    load_refs: Callable[[RefKey], Iterable[RefKey]],
    *,
    max_depth_cap: int = DEFAULT_MAX_DEPTH,
) -> None:
    """Validate that the sub-workflow graph from *root_id* is acyclic and
    within the depth cap.

    Raises ``CycleError(path)`` on cycle.
    Raises ``DepthExceededError(depth, cap)`` on depth overflow.
    Raises ``LookupError`` on missing workflow.
    """
    cycle = find_cycle(root_id, load_refs)
    if cycle is not None:
        raise CycleError(cycle)

    d = max_depth(root_id, load_refs)
    if d > max_depth_cap:
        raise DepthExceededError(d, max_depth_cap)
