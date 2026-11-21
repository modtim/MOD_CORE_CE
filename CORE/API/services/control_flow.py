"""workflow-agentic-execution-loop §control-flow-primitives — pure
helpers for the three control-flow node types the wizard can propose:

  - conditional         — predicate gate on upstream output
  - fan_out_over_list   — N parallel copies of one downstream branch
  - retry_with_params   — re-run a wrapped node with mutated params

Surfaces:

  - `node_kind_of(node_data) -> Optional[str]`
        Returns 'conditional' | 'fan_out_over_list' | 'retry_with_params'
        if the node carries a recognised control-flow marker, None
        otherwise. Used by the orchestrator to route dispatch.

  - `extract_config(node_data, kind) -> ConditionalConfig | FanOutConfig
        | RetryConfig`
        Validates the node's `data.control_flow` payload against the
        Pydantic schema. Raises `ValueError` on malformed config so the
        caller can surface a useful error to the wizard / run-detail UI.

  - `evaluate_predicate(predicate: dict, parent_output: Any) -> bool`
        Deterministic predicate eval — no LLM, no eval(). Supported
        ops: eq, ne, lt, le, gt, ge, contains, in, exists.

  - `resolve_path(path: str, root: Any) -> Any`
        Minimal JSON-path subset: `$.foo.bar`, `$[0]`, `$.items[2].name`.
        No wildcards / filters / recursive descent — explicit on
        purpose so the agent loop's predicates are easy to reason about.

  - `is_enforcement_enabled_for_tenant(db, tenant_id) -> bool`
        Per-tenant gate. Default off so legacy workflows keep their
        existing dispatch semantics unchanged.

  - `synthesise_fanout_dispatches(parent_outputs, fanout_cfg,
        downstream_node_id, *, list_cap) -> list[dict]`
        Builds N dispatch descriptors from the parent output list.
        Returns a list of {output_namespace, scoped_input} tuples the
        caller enqueues as separate ApiTaskQueue rows.

  - `should_take_branch(edge, predicate_result) -> bool`
        Decides whether an edge departing a conditional node should
        carry the dispatch. Edges may set `data.branch` to "true" /
        "false"; unset edges default to "true" so unmarked graphs
        still work like a single-edge fall-through.

Retry execution is deliberately schema-only in this pass — the
orchestrator surfaces a clear "retry_with_params dispatch not yet
implemented" error when one is reached. Per-node retry is the
lowest-leverage of the three for the agent loop (the
builder+critic loop already retries above the graph), and shipping
it half-done risks regressions in the live worker_tasks dispatch
path.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Pydantic schemas — node `data.control_flow` payload shapes
# ---------------------------------------------------------------------

_ALLOWED_OPS = {
    "eq", "ne", "lt", "le", "gt", "ge",
    "contains", "in", "exists",
}


class PredicateSpec(BaseModel):
    """Deterministic predicate evaluated against the parent output.

    Shape: {"op": "eq", "path": "$.status", "value": "ok"}
    Or:    {"op": "exists", "path": "$.error"}  (value omitted)
    """
    op: str
    path: str
    value: Any = None

    @model_validator(mode="after")
    def _check_op(self):
        if self.op not in _ALLOWED_OPS:
            raise ValueError(
                f"predicate.op must be one of {sorted(_ALLOWED_OPS)}, "
                f"got {self.op!r}"
            )
        return self


class ConditionalConfig(BaseModel):
    predicate: PredicateSpec


class FanOutConfig(BaseModel):
    list_path: str
    # Safety cap — the wizard could propose a workflow that fans out
    # over an unbounded list and DOS the queue. Default 100 is sane
    # for image-batch and bullet-point fan-outs; tenants can opt-in
    # to larger via tenant-level config (future).
    list_cap: int = Field(default=100, ge=1, le=10000)


class RetryConfig(BaseModel):
    """Schema for retry_with_params nodes. Execution semantics land
    in a follow-up — the orchestrator currently surfaces a clear
    "not yet implemented" error when one of these is dispatched."""
    wraps_node_id: str
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Either a list of explicit param-overrides per attempt, OR a
    # strategy name the orchestrator interprets. Validation only
    # checks one OR the other is present.
    param_overrides: Optional[List[Dict[str, Any]]] = None
    param_strategy: Optional[str] = None

    @model_validator(mode="after")
    def _check_one_strategy(self):
        if not self.param_overrides and not self.param_strategy:
            raise ValueError(
                "retry_with_params requires either `param_overrides` "
                "(list of dicts) or `param_strategy` (name)"
            )
        return self


# Recognised control-flow node kinds.
_CONTROL_FLOW_KINDS = {
    "conditional", "fan_out_over_list", "retry_with_params",
}


def node_kind_of(node_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the control-flow kind if this node is one, else None.

    The wizard / FE marks a control-flow node by setting `node_kind`
    to one of {conditional, fan_out_over_list, retry_with_params}
    AND populating `data.control_flow` with the kind's config. We
    require BOTH to be safe against malformed graphs.
    """
    if not isinstance(node_data, dict):
        return None
    kind = (node_data.get("node_kind") or "").strip()
    if kind not in _CONTROL_FLOW_KINDS:
        return None
    cf = node_data.get("control_flow")
    if not isinstance(cf, dict):
        return None
    return kind


def extract_config(
    node_data: Dict[str, Any], kind: str,
) -> Union[ConditionalConfig, FanOutConfig, RetryConfig]:
    """Parse the `data.control_flow` blob into the right Pydantic
    model. Raises ValueError with a humane message on malformed input
    so the orchestrator can surface it on the run-detail UI."""
    cf = (node_data or {}).get("control_flow") or {}
    try:
        if kind == "conditional":
            return ConditionalConfig(**cf)
        if kind == "fan_out_over_list":
            return FanOutConfig(**cf)
        if kind == "retry_with_params":
            return RetryConfig(**cf)
    except ValidationError as e:
        raise ValueError(f"malformed control_flow.{kind} config: {e}") from e
    raise ValueError(f"unknown control-flow kind {kind!r}")


# ---------------------------------------------------------------------
# JSON-path resolution
# ---------------------------------------------------------------------

_PATH_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]")


def resolve_path(path: str, root: Any) -> Any:
    """Resolve a `$.foo.bar[2].baz` style path against `root`. Returns
    None if any step is missing — explicit on purpose so predicates
    over absent keys evaluate as you'd intuit (missing != value, etc).

    Supported: dot-property + integer-index. NOT supported: filters,
    wildcards, recursive descent — keep it explicit + auditable for
    the agent loop.
    """
    if not path:
        return None
    p = path.strip()
    if p == "$":
        return root
    if not p.startswith("$"):
        return None
    rest = p[1:]
    cur = root
    pos = 0
    while pos < len(rest):
        m = _PATH_TOKEN_RE.match(rest, pos)
        if not m:
            return None
        prop, idx = m.group(1), m.group(2)
        if prop is not None:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(prop)
        else:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
        pos = m.end()
        if cur is None:
            return None
    return cur


def evaluate_predicate(predicate: Dict[str, Any], parent_output: Any) -> bool:
    """Evaluate `predicate` over `parent_output`. Returns the bool
    result. Re-raises ValueError on schema problems (caller should
    validate via PredicateSpec first; this is the runtime eval)."""
    spec = PredicateSpec(**predicate) if not isinstance(predicate, PredicateSpec) else predicate
    actual = resolve_path(spec.path, parent_output)
    val = spec.value

    if spec.op == "exists":
        return actual is not None
    if actual is None and spec.op not in ("ne",):
        # null on the path can't be == / < / contains / in anything.
        # `ne` flips: missing IS not-equal-to a concrete value.
        return False

    try:
        if spec.op == "eq":
            return actual == val
        if spec.op == "ne":
            return actual != val
        if spec.op == "lt":
            return actual < val
        if spec.op == "le":
            return actual <= val
        if spec.op == "gt":
            return actual > val
        if spec.op == "ge":
            return actual >= val
        if spec.op == "contains":
            # works for strings and lists
            return val in actual
        if spec.op == "in":
            return actual in val
    except TypeError:
        # Comparing incompatible types (e.g. lt across str / int) →
        # false rather than raising. Matches "fall through to else
        # branch" intuition.
        return False
    return False


# ---------------------------------------------------------------------
# Per-tenant gate
# ---------------------------------------------------------------------

def is_enforcement_enabled_for_tenant(db: Session, tenant_id: int) -> bool:
    """Per-tenant `control_flow_primitives_enabled` gate. When OFF,
    the orchestrator should reject any workflow containing control-
    flow node kinds (the wizard won't propose them if it knows the
    tenant flag is off). When ON, primitives dispatch normally."""
    if not tenant_id:
        return False
    import models
    t = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if t is None:
        return False
    return bool(getattr(t, "control_flow_primitives_enabled", False))


# ---------------------------------------------------------------------
# Edge / branch routing
# ---------------------------------------------------------------------

def should_take_branch(edge: Dict[str, Any], predicate_result: bool) -> bool:
    """Decide whether `edge` departing a conditional node carries this
    dispatch. Looks at `edge.data.branch`:
      - "true"   → only when predicate is True
      - "false"  → only when predicate is False
      - missing / anything else → take when predicate is True
        (single-edge fall-through; unmarked graphs behave like
        "execute downstream when predicate matched")
    """
    branch = ((edge or {}).get("data") or {}).get("branch")
    if branch == "true":
        return predicate_result
    if branch == "false":
        return not predicate_result
    return predicate_result


# ---------------------------------------------------------------------
# Fan-out expansion
# ---------------------------------------------------------------------

def synthesise_fanout_dispatches(
    *,
    parent_outputs: Dict[str, Any],
    fanout_cfg: FanOutConfig,
    list_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Expand the fan-out node's parent list into N per-item dispatch
    descriptors.

    Returns: `[{"item_index": i, "item": <list_elem>,
                "output_namespace": "fanout_<i>"}]`

    The orchestrator turns each descriptor into an ApiTaskQueue row
    against the wrapped downstream node, with `output_namespace`
    distinguishing each iteration's outputs in storage.

    `parent_outputs` is the orchestrator's standard parent-outputs
    dict (parent_node_id -> outputs list). The list source path
    points at one specific parent's output — convention: the fan-out
    node has exactly one upstream parent, so we resolve against the
    single parent's first output blob.
    """
    cap = list_cap if list_cap is not None else fanout_cfg.list_cap
    list_root: Any = None
    for _pid, outputs in (parent_outputs or {}).items():
        if isinstance(outputs, list) and outputs:
            list_root = outputs[0]
            break
    items = resolve_path(fanout_cfg.list_path, list_root)
    if not isinstance(items, list):
        # No list at the configured path — produce zero dispatches
        # rather than guessing. The orchestrator surfaces this as
        # "fan_out_over_list: no list at <path>".
        return []
    descriptors: List[Dict[str, Any]] = []
    for i, item in enumerate(items[:cap]):
        descriptors.append({
            "item_index": i,
            "item": item,
            "output_namespace": f"fanout_{i}",
        })
    return descriptors
