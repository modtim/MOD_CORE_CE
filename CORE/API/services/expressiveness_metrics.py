"""Prometheus metrics for the
expressiveness subsystem (sub-workflows, agent nodes, auto-builds, event triggers,
human approvals, workflow memory quotas).

The expressiveness features were previously invisible to /metrics. This module
registers the gauges/counters and the emit helpers the call-site wiring task
(the follow-up wiring task) uses:

  - sub_workflow_dispatch_count                     (gauge)
  - agent_node_tool_calls_total{decision_outcome}   (counter)
  - auto_build_dispatch_total{outcome}              (counter)
  - event_trigger_dispatch_total{trigger_kind,status}(counter)
  - human_approval_pending_count                    (gauge)
  - workflow_memory_quota_used_bytes{tenant_id}     (gauge)

Design notes:

  - `register_expressiveness_metrics()` is the single entry point and is
    idempotent: re-registering an existing metric name raises ValueError in
    prometheus_client, so registration is check-then-register + tolerate
    (same pattern as services/wizard_metrics.py and services/mesh_metrics.py).
    Safe to call twice in one process and on every uvicorn worker.
  - prometheus_client is imported lazily so importing this module in a test
    process without the dependency does not crash; the helpers no-op when the
    dep is missing or a registration failed.
  - Low cardinality on purpose: no run_id / node_id / proposal_id labels, and
    unknown label values are coerced to "unknown". `tenant_id` on the memory
    gauge is the one per-tenant label; it is bounded by
    MAX_TENANT_LABEL_SERIES (overflow tenants roll up to "other") so the gauge
    cannot grow an unbounded number of series.
  - This module has no config surface and no sweep job — recomputing the
    gauges from the DB is the follow-up wiring task.
"""
from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Bounded cardinality for the per-tenant memory gauge: after this many distinct
# tenant series, further tenants roll up into the "other" label value.
MAX_TENANT_LABEL_SERIES = 200

# WSIP §12.1 closed label vocabularies: every label value is normalised into
# one of these sets, unknown values roll up to "other", so the number of series
# per metric is a compile-time constant and can never grow at runtime.
SEVERITY_LABELS = frozenset({"critical", "high", "medium", "low", "info"})
OPERATOR_TRIGGERS = frozenset({"drift", "failure_rate", "adversary", "manual"})
OPERATOR_ACTIONS = frozenset({"canary_started", "promoted", "rolled_back"})

_M_SUB_WORKFLOW_DISPATCH: Any = None      # Gauge
_M_AGENT_TOOL_CALLS: Any = None           # Counter[decision_outcome]
_M_AUTO_BUILD_DISPATCH: Any = None        # Counter[outcome]
_M_EVENT_TRIGGER_DISPATCH: Any = None     # Counter[trigger_kind, status]
_M_APPROVAL_PENDING: Any = None           # Gauge
_M_MEMORY_QUOTA_BYTES: Any = None         # Gauge[tenant_id]
_TENANT_SERIES: Dict[str, str] = {}
_TENANT_SERIES_LOCK = threading.Lock()

# §14.2 + §12.2 histograms (expressiveness + wsip timing)
_M_GOAL_DIRECTED_PLANNER: Any = None      # Histogram (seconds)
_M_REFLEXION_RETRIEVAL: Any = None        # Histogram (seconds)
_M_EVENT_TRIGGER_LATENCY: Any = None      # Histogram (seconds)
_M_DRIFT_DETECTION: Any = None            # Histogram (seconds)
_M_FAILURE_PREDICTION_INFERENCE: Any = None  # Histogram (seconds)
_M_CAUSAL_GRAPH_REFRESH: Any = None       # Histogram (seconds)
_M_ADVERSARY_DISPATCH: Any = None         # Histogram (seconds)
_M_AUTONOMOUS_OPERATOR: Any = None        # Histogram (seconds)

# WSIP §12.1 — outcome counters/gauge (definitions + helpers only in this slice;
# the call-site wiring lands in a later slice).
#
# DELIBERATE DEVIATION from the tracker: the spec puts `workflow_id` on
# production_drift_events_total and failure_prediction_calibration_brier_score.
# We do NOT add it — workflow_id is unbounded cardinality (every workflow ever
# created) and would expose cross-tenant workflow ids on the public /metrics
# scrape. The drift counter is therefore platform-wide and the Brier gauge
# carries the platform-wide latest value.
_M_PRODUCTION_DRIFT_EVENTS: Any = None       # Counter (no labels)
_M_FAILURE_PRED_BRIER: Any = None            # Gauge  (no labels, latest)
_M_ADVERSARY_FINDINGS: Any = None            # Counter[severity]
_M_OPERATOR_PROPOSALS: Any = None            # Counter[trigger]
_M_OPERATOR_DEPLOYMENTS: Any = None          # Counter[action]

_INITIALISED = False


def _safe_register(builder, *args, **kw):
    """Check-then-register. A duplicate name (reload, second import, another
    uvicorn worker in the same process) raises ValueError — tolerate it so
    tests and reloads don't blow up."""
    try:
        return builder(*args, **kw)
    except Exception as e:  # noqa: BLE001 — duplicate registration on reload
        logger.debug("expressiveness_metrics register failed for %s: %s", args, e)
        return None


def register_expressiveness_metrics() -> bool:
    """Register the gauges/counters on the default REGISTRY (what
    prometheus-fastapi-instrumentator exposes at /metrics). Idempotent:
    returns True when the metrics are available, False when prometheus_client
    is unavailable or every registration was already present."""
    global _INITIALISED, _M_SUB_WORKFLOW_DISPATCH, _M_AGENT_TOOL_CALLS
    global _M_AUTO_BUILD_DISPATCH, _M_EVENT_TRIGGER_DISPATCH
    global _M_APPROVAL_PENDING, _M_MEMORY_QUOTA_BYTES
    global _M_GOAL_DIRECTED_PLANNER, _M_REFLEXION_RETRIEVAL
    global _M_EVENT_TRIGGER_LATENCY, _M_DRIFT_DETECTION
    global _M_FAILURE_PREDICTION_INFERENCE, _M_CAUSAL_GRAPH_REFRESH
    global _M_ADVERSARY_DISPATCH, _M_AUTONOMOUS_OPERATOR
    global _M_PRODUCTION_DRIFT_EVENTS, _M_FAILURE_PRED_BRIER
    global _M_ADVERSARY_FINDINGS, _M_OPERATOR_PROPOSALS, _M_OPERATOR_DEPLOYMENTS
    if _INITIALISED:
        return True
    try:
        from prometheus_client import Counter, Gauge, Histogram
    except Exception:
        _INITIALISED = True
        return False

    _M_SUB_WORKFLOW_DISPATCH = _safe_register(
        Gauge, "sub_workflow_dispatch_count",
        "Sub-workflow dispatches currently in flight (parent run -> child run).",
    )
    _M_AGENT_TOOL_CALLS = _safe_register(
        Counter, "agent_node_tool_calls_total",
        "Agent-node tool calls by decision outcome.", ["decision_outcome"],
    )
    _M_AUTO_BUILD_DISPATCH = _safe_register(
        Counter, "auto_build_dispatch_total",
        "Auto-build dispatches by outcome.", ["outcome"],
    )
    _M_EVENT_TRIGGER_DISPATCH = _safe_register(
        Counter, "event_trigger_dispatch_total",
        "Event-trigger dispatches by trigger kind and status.",
        ["trigger_kind", "status"],
    )
    _M_APPROVAL_PENDING = _safe_register(
        Gauge, "human_approval_pending_count",
        "Human approvals waiting on a decision across all tenants.",
    )
    _M_MEMORY_QUOTA_BYTES = _safe_register(
        Gauge, "workflow_memory_quota_used_bytes",
        "Workflow memory quota used, in bytes, per tenant.", ["tenant_id"],
    )

    # ------------------------------------------------------------------
    # §14.2 — Expressiveness subsystem timing histograms
    # ------------------------------------------------------------------

    _M_GOAL_DIRECTED_PLANNER = _safe_register(
        Histogram, "goal_directed_planner_duration_seconds",
        "Wall-clock seconds for the goal-directed planner loop.",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    )
    _M_REFLEXION_RETRIEVAL = _safe_register(
        Histogram, "reflexion_retrieval_duration_seconds",
        "Wall-clock seconds for a single Reflexion retrieve+reason cycle.",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )
    _M_EVENT_TRIGGER_LATENCY = _safe_register(
        Histogram, "event_trigger_to_dispatch_latency_seconds",
        "Seconds from event arrival to node dispatch.",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )

    # ------------------------------------------------------------------
    # §12.2 — Self-improving-production timing histograms
    # ------------------------------------------------------------------

    _M_DRIFT_DETECTION = _safe_register(
        Histogram, "drift_detection_compute_seconds",
        "Seconds spent computing drift fingerprints.",
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    )
    _M_FAILURE_PREDICTION_INFERENCE = _safe_register(
        Histogram, "failure_prediction_inference_seconds",
        "Seconds spent running the failure-prediction model.",
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    )
    _M_CAUSAL_GRAPH_REFRESH = _safe_register(
        Histogram, "causal_graph_refresh_seconds",
        "Seconds spent refreshing the causal graph for a workflow.",
        buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    )
    _M_ADVERSARY_DISPATCH = _safe_register(
        Histogram, "adversary_dispatch_round_trip_seconds",
        "Seconds for a full adversary dispatch round-trip (sandbox→collect).",
        buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    )
    _M_AUTONOMOUS_OPERATOR = _safe_register(
        Histogram, "autonomous_operator_propose_to_deploy_seconds",
        "Seconds from operator propose to deploy decision.",
        buckets=(1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0,
                 1800.0, 3600.0, 21600.0, 86400.0, 259200.0, 604800.0),
    )

    # ------------------------------------------------------------------
    # §12.1 — Self-improving-production outcome metrics (no workflow_id: see
    # the deviation note above the module-level handles)
    # ------------------------------------------------------------------

    _M_PRODUCTION_DRIFT_EVENTS = _safe_register(
        Counter, "production_drift_events_total",
        "Production drift events detected, platform-wide.",
    )
    _M_FAILURE_PRED_BRIER = _safe_register(
        Gauge, "failure_prediction_calibration_brier_score",
        "Latest platform-wide Brier score for failure-prediction calibration.",
    )
    _M_ADVERSARY_FINDINGS = _safe_register(
        Counter, "adversary_findings_total",
        "Adversary findings by severity.", ["severity"],
    )
    _M_OPERATOR_PROPOSALS = _safe_register(
        Counter, "autonomous_operator_proposals_total",
        "Autonomous-operator proposals by trigger.", ["trigger"],
    )
    _M_OPERATOR_DEPLOYMENTS = _safe_register(
        Counter, "autonomous_operator_deployments_total",
        "Autonomous-operator deployments by action.", ["action"],
    )

    _INITIALISED = True
    return any(h is not None for h in (
        _M_SUB_WORKFLOW_DISPATCH, _M_AGENT_TOOL_CALLS, _M_AUTO_BUILD_DISPATCH,
        _M_EVENT_TRIGGER_DISPATCH, _M_APPROVAL_PENDING, _M_MEMORY_QUOTA_BYTES,
        _M_GOAL_DIRECTED_PLANNER, _M_REFLEXION_RETRIEVAL, _M_EVENT_TRIGGER_LATENCY,
        _M_DRIFT_DETECTION, _M_FAILURE_PREDICTION_INFERENCE,
        _M_CAUSAL_GRAPH_REFRESH, _M_ADVERSARY_DISPATCH, _M_AUTONOMOUS_OPERATOR,
        _M_PRODUCTION_DRIFT_EVENTS, _M_FAILURE_PRED_BRIER,
        _M_ADVERSARY_FINDINGS, _M_OPERATOR_PROPOSALS, _M_OPERATOR_DEPLOYMENTS,
    ))


# ---------------------------------------------------------------------
# Emit helpers
# ---------------------------------------------------------------------

def record_sub_workflow_dispatch(in_flight: Optional[int] = None) -> None:
    """Set the in-flight gauge, or increment it by 1 when no value is given."""
    register_expressiveness_metrics()
    if _M_SUB_WORKFLOW_DISPATCH is None:
        return
    try:
        if in_flight is None:
            _M_SUB_WORKFLOW_DISPATCH.inc()
        else:
            _M_SUB_WORKFLOW_DISPATCH.set(max(0.0, float(in_flight)))
    except Exception:  # noqa: BLE001
        pass


def record_agent_tool_call(decision_outcome: str) -> None:
    register_expressiveness_metrics()
    if _M_AGENT_TOOL_CALLS is None:
        return
    try:
        _M_AGENT_TOOL_CALLS.labels(decision_outcome=decision_outcome or "unknown").inc()
    except Exception:  # noqa: BLE001
        pass


def record_auto_build_dispatch(outcome: str) -> None:
    register_expressiveness_metrics()
    if _M_AUTO_BUILD_DISPATCH is None:
        return
    try:
        _M_AUTO_BUILD_DISPATCH.labels(outcome=outcome or "unknown").inc()
    except Exception:  # noqa: BLE001
        pass


def record_event_trigger_dispatch(trigger_kind: str, status: str) -> None:
    register_expressiveness_metrics()
    if _M_EVENT_TRIGGER_DISPATCH is None:
        return
    try:
        _M_EVENT_TRIGGER_DISPATCH.labels(
            trigger_kind=trigger_kind or "unknown",
            status=status or "unknown",
        ).inc()
    except Exception:  # noqa: BLE001
        pass


def set_human_approval_pending(pending: int) -> None:
    register_expressiveness_metrics()
    if _M_APPROVAL_PENDING is None:
        return
    try:
        _M_APPROVAL_PENDING.set(max(0.0, float(pending)))
    except Exception:  # noqa: BLE001
        pass


def set_memory_quota_used_bytes(tenant_id: Any, bytes_used: int) -> None:
    """Per-tenant gauge. Bounded: past MAX_TENANT_LABEL_SERIES distinct tenants,
    values roll up to the "other" label instead of growing new series."""
    register_expressiveness_metrics()
    if _M_MEMORY_QUOTA_BYTES is None or tenant_id is None:
        return
    try:
        key = str(tenant_id)
        with _TENANT_SERIES_LOCK:
            if key not in _TENANT_SERIES:
                if len(_TENANT_SERIES) >= MAX_TENANT_LABEL_SERIES:
                    key = "other"
                else:
                    _TENANT_SERIES[key] = key
        _M_MEMORY_QUOTA_BYTES.labels(tenant_id=key).set(max(0.0, float(bytes_used)))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------
# §12.1 — Self-improving-production emit helpers
# ---------------------------------------------------------------------


def _norm_label(value: Any, allowed: frozenset) -> str:
    """Normalise *value* into a closed vocabulary; anything unknown -> "other"."""
    if value is None:
        return "other"
    key = str(value).strip().lower()
    return key if key in allowed else "other"


def _finite_number(value: Any, *, minimum: Optional[float] = None) -> Optional[float]:
    """Return *value* as a finite float >= *minimum*, else None (never raises)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):  # NaN / infinity
        return None
    if minimum is not None and num < minimum:
        return None
    return num


def inc_production_drift_events(n: int = 1) -> None:
    """Bump the platform-wide drift counter. None/negative/NaN -> no-op."""
    register_expressiveness_metrics()
    if _M_PRODUCTION_DRIFT_EVENTS is None:
        return
    amount = _finite_number(n, minimum=0.0)
    if amount is None or amount == 0.0:
        return
    try:
        _M_PRODUCTION_DRIFT_EVENTS.inc(amount)
    except Exception:  # noqa: BLE001
        pass


def set_failure_prediction_brier(value: Optional[float]) -> None:
    """Set the platform-wide latest Brier score. None/negative/NaN -> no-op."""
    register_expressiveness_metrics()
    if _M_FAILURE_PRED_BRIER is None:
        return
    num = _finite_number(value, minimum=0.0)
    if num is None:
        return
    try:
        _M_FAILURE_PRED_BRIER.set(num)
    except Exception:  # noqa: BLE001
        pass


def inc_adversary_findings(severity: Any) -> None:
    """Bump adversary_findings_total{severity}; unknown severity -> "other"."""
    register_expressiveness_metrics()
    if _M_ADVERSARY_FINDINGS is None:
        return
    try:
        _M_ADVERSARY_FINDINGS.labels(severity=_norm_label(severity, SEVERITY_LABELS)).inc()
    except Exception:  # noqa: BLE001
        pass


def inc_operator_proposals(trigger: Any) -> None:
    """Bump autonomous_operator_proposals_total{trigger}; unknown -> "other"."""
    register_expressiveness_metrics()
    if _M_OPERATOR_PROPOSALS is None:
        return
    try:
        _M_OPERATOR_PROPOSALS.labels(trigger=_norm_label(trigger, OPERATOR_TRIGGERS)).inc()
    except Exception:  # noqa: BLE001
        pass


def inc_operator_deployments(action: Any) -> None:
    """Bump autonomous_operator_deployments_total{action}; unknown -> "other"."""
    register_expressiveness_metrics()
    if _M_OPERATOR_DEPLOYMENTS is None:
        return
    try:
        _M_OPERATOR_DEPLOYMENTS.labels(action=_norm_label(action, OPERATOR_ACTIONS)).inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------
# §14.2 + §12.2 histogram observe helpers
# ---------------------------------------------------------------------

def _observe_histogram(handle: Any, seconds: float, **labels: Any) -> None:
    """Observe *seconds* on a histogram handle, never raising."""
    if handle is None:
        return
    try:
        handle.observe(float(seconds), **labels)
    except Exception:  # noqa: BLE001
        pass


def observe_goal_directed_planner_duration(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_GOAL_DIRECTED_PLANNER, seconds, **labels)


def observe_reflexion_retrieval_duration(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_REFLEXION_RETRIEVAL, seconds, **labels)


def observe_event_trigger_to_dispatch_latency(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_EVENT_TRIGGER_LATENCY, seconds, **labels)


def observe_drift_detection_compute(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_DRIFT_DETECTION, seconds, **labels)


def observe_failure_prediction_inference(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_FAILURE_PREDICTION_INFERENCE, seconds, **labels)


def observe_causal_graph_refresh(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_CAUSAL_GRAPH_REFRESH, seconds, **labels)


def observe_adversary_dispatch_round_trip(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_ADVERSARY_DISPATCH, seconds, **labels)


def observe_autonomous_operator_propose_to_deploy(
    seconds: float, **labels: Any,
) -> None:
    register_expressiveness_metrics()
    _observe_histogram(_M_AUTONOMOUS_OPERATOR, seconds, **labels)


# ---------------------------------------------------------------------
# Context manager: time_metric(name, **labels)
# ---------------------------------------------------------------------

# Mapping from user-facing metric names to the internal variable names.
# Used by time_metric() to look up the handle dynamically after registration.
_HISTOGRAM_NAMES: Dict[str, str] = {
    "goal_directed_planner_duration_seconds": "_M_GOAL_DIRECTED_PLANNER",
    "reflexion_retrieval_duration_seconds": "_M_REFLEXION_RETRIEVAL",
    "event_trigger_to_dispatch_latency_seconds": "_M_EVENT_TRIGGER_LATENCY",
    "drift_detection_compute_seconds": "_M_DRIFT_DETECTION",
    "failure_prediction_inference_seconds": "_M_FAILURE_PREDICTION_INFERENCE",
    "causal_graph_refresh_seconds": "_M_CAUSAL_GRAPH_REFRESH",
    "adversary_dispatch_round_trip_seconds": "_M_ADVERSARY_DISPATCH",
    "autonomous_operator_propose_to_deploy_seconds": "_M_AUTONOMOUS_OPERATOR",
}


@contextlib.contextmanager
def time_metric(name: str, **labels: Any):
    """Measure elapsed seconds and observe on the histogram named *name*.

    Observes elapsed time even if the caller raises an exception, then
    re-raises the original exception.  If *name* is unknown or
    registration failed, the context manager is a no-op.
    """
    register_expressiveness_metrics()
    start = time.monotonic()
    try:
        yield
    except BaseException as exc:
        elapsed = time.monotonic() - start
        var_name = _HISTOGRAM_NAMES.get(name)
        handle = getattr(sys.modules[__name__], var_name, None) if var_name else None
        _observe_histogram(handle, elapsed, **labels)
        raise
    elapsed = time.monotonic() - start
    var_name = _HISTOGRAM_NAMES.get(name)
    handle = getattr(sys.modules[__name__], var_name, None) if var_name else None
    _observe_histogram(handle, elapsed, **labels)
