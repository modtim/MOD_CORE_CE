"""workflow-agentic-execution-loop §10 — Prometheus metrics for the
wizard agentic loop + node-output cache + control-flow flags.

Metric design (low cardinality on purpose — no per-tenant labels on
gauges that the sweep can recompute, no proposal_id / session_id
labels anywhere):

  - wizard_agent_iteration_count          (histogram, buckets 1..3)
  - wizard_agent_test_run_duration_seconds (histogram, exponential)
  - wizard_output_critic_verdict_total{verdict}  (counter)
  - wizard_agent_result_total{result}     (counter — SUCCESS / BEST_EFFORT / etc)
  - node_output_cache_hit_rate            (gauge — recomputed by sweep)
  - node_output_cache_size_rows           (gauge)
  - node_output_cache_size_bytes          (gauge)
  - wizard_test_run_rate_limit_hits_total (counter — fires on 429)
  - wizard_auto_bridge_inserted_total{src_type,tgt_type}  (counter)
  - control_flow_dispatched_total{kind}   (counter)

prometheus_client is imported lazily so module import in tests
(without prometheus_client installed) doesn't crash. The metric
helpers no-op when the dep is missing.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Module-level handles — populated lazily on first emit. None means
# either prometheus_client isn't installed OR the metric registration
# raised (e.g. duplicate name from a previous import). All emit
# helpers tolerate None and no-op.
_M_ITERATION_COUNT: Any = None
_M_TEST_RUN_DURATION: Any = None
_M_CRITIC_VERDICT: Any = None
_M_AGENT_RESULT: Any = None
_M_CACHE_HIT_RATE: Any = None
_M_CACHE_SIZE_ROWS: Any = None
_M_CACHE_SIZE_BYTES: Any = None
_M_RATE_LIMIT_HITS: Any = None
_M_AUTO_BRIDGE: Any = None
_M_CONTROL_FLOW: Any = None
_M_NODE_HANDOFF: Any = None
_INITIALISED = False


def _init() -> None:
    """Lazy register. Idempotent: re-registering an existing metric
    name raises ValueError in prometheus_client; we catch + tolerate
    to keep tests + reloads from blowing up."""
    global _INITIALISED
    global _M_ITERATION_COUNT, _M_TEST_RUN_DURATION, _M_CRITIC_VERDICT
    global _M_AGENT_RESULT, _M_CACHE_HIT_RATE, _M_CACHE_SIZE_ROWS
    global _M_CACHE_SIZE_BYTES, _M_RATE_LIMIT_HITS, _M_AUTO_BRIDGE
    global _M_CONTROL_FLOW, _M_NODE_HANDOFF
    if _INITIALISED:
        return
    try:
        from prometheus_client import Counter, Histogram, Gauge
    except Exception:
        _INITIALISED = True
        return

    def _safe_register(builder, *args, **kw):
        try:
            return builder(*args, **kw)
        except Exception as e:
            logger.debug("wizard_metrics: register failed for %s: %s", args, e)
            return None

    _M_ITERATION_COUNT = _safe_register(
        Histogram, "wizard_agent_iteration_count",
        "How many builder+critic iterations the agent loop ran "
        "per wizard turn (final iteration_count emitted at "
        "agent_finished).",
        buckets=(1, 2, 3, 4, 5),
    )
    _M_TEST_RUN_DURATION = _safe_register(
        Histogram, "wizard_agent_test_run_duration_seconds",
        "Wall-clock seconds spent from test_run_dispatched to "
        "test_run_succeeded / test_run_failed.",
        buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
    )
    _M_CRITIC_VERDICT = _safe_register(
        Counter, "wizard_output_critic_verdict_total",
        "Count of output-critic verdicts emitted by the agent loop.",
        ["verdict"],
    )
    _M_AGENT_RESULT = _safe_register(
        Counter, "wizard_agent_result_total",
        "Terminal result of an agent loop (SUCCESS / BEST_EFFORT / "
        "USER_CLARIFICATION / NO_INPUT).",
        ["result"],
    )
    _M_CACHE_HIT_RATE = _safe_register(
        Gauge, "node_output_cache_hit_rate",
        "Cache hit rate over the last sweep window (0..1).",
    )
    _M_CACHE_SIZE_ROWS = _safe_register(
        Gauge, "node_output_cache_size_rows",
        "Total rows in node_output_cache.",
    )
    _M_CACHE_SIZE_BYTES = _safe_register(
        Gauge, "node_output_cache_size_bytes",
        "Approximate stored bytes referenced by node_output_cache.",
    )
    _M_RATE_LIMIT_HITS = _safe_register(
        Counter, "wizard_test_run_rate_limit_hits_total",
        "Wizard test-run rate-limit rejections (per-tenant).",
        ["tenant_id"],
    )
    _M_AUTO_BRIDGE = _safe_register(
        Counter, "wizard_auto_bridge_inserted_total",
        "Adapter inserts the wizard auto-bridge produced.",
        ["src_type", "tgt_type"],
    )
    _M_CONTROL_FLOW = _safe_register(
        Counter, "control_flow_dispatched_total",
        "Control-flow node dispatches by kind.",
        ["kind"],
    )
    _M_NODE_HANDOFF = _safe_register(
        Counter, "workflow_node_handoff_emitted_total",
        "Workflow-wizard → node-wizard handoff emissions. result "
        "labels: emitted (prefill spec produced). has_input / "
        "has_output flag whether the wizard could infer the IO "
        "types from the surrounding graph.",
        ["result", "has_input", "has_output"],
    )
    _INITIALISED = True


# ---------------------------------------------------------------------
# Emit helpers
# ---------------------------------------------------------------------

def record_iteration_count(n: int) -> None:
    _init()
    if _M_ITERATION_COUNT is None:
        return
    try:
        _M_ITERATION_COUNT.observe(n)
    except Exception:
        pass


def record_test_run_duration(seconds: float) -> None:
    _init()
    if _M_TEST_RUN_DURATION is None:
        return
    try:
        _M_TEST_RUN_DURATION.observe(seconds)
    except Exception:
        pass


def record_critic_verdict(verdict: str) -> None:
    _init()
    if _M_CRITIC_VERDICT is None:
        return
    try:
        _M_CRITIC_VERDICT.labels(verdict=verdict or "unknown").inc()
    except Exception:
        pass


def record_agent_result(result: str) -> None:
    _init()
    if _M_AGENT_RESULT is None:
        return
    try:
        _M_AGENT_RESULT.labels(result=result or "unknown").inc()
    except Exception:
        pass


def set_cache_hit_rate(rate: float) -> None:
    _init()
    if _M_CACHE_HIT_RATE is None:
        return
    try:
        _M_CACHE_HIT_RATE.set(max(0.0, min(1.0, float(rate))))
    except Exception:
        pass


def set_cache_size(rows: Optional[int] = None, bytes_total: Optional[int] = None) -> None:
    _init()
    try:
        if rows is not None and _M_CACHE_SIZE_ROWS is not None:
            _M_CACHE_SIZE_ROWS.set(rows)
        if bytes_total is not None and _M_CACHE_SIZE_BYTES is not None:
            _M_CACHE_SIZE_BYTES.set(bytes_total)
    except Exception:
        pass


def record_rate_limit_hit(tenant_id: int) -> None:
    _init()
    if _M_RATE_LIMIT_HITS is None:
        return
    try:
        _M_RATE_LIMIT_HITS.labels(tenant_id=str(tenant_id)).inc()
    except Exception:
        pass


def record_auto_bridge(src_type: str, tgt_type: str) -> None:
    _init()
    if _M_AUTO_BRIDGE is None:
        return
    try:
        _M_AUTO_BRIDGE.labels(
            src_type=src_type or "unknown",
            tgt_type=tgt_type or "unknown",
        ).inc()
    except Exception:
        pass


def record_control_flow_dispatch(kind: str) -> None:
    _init()
    if _M_CONTROL_FLOW is None:
        return
    try:
        _M_CONTROL_FLOW.labels(kind=kind or "unknown").inc()
    except Exception:
        pass


def record_node_handoff(
    *, result: str = "emitted",
    has_input: bool = False, has_output: bool = False,
) -> None:
    _init()
    if _M_NODE_HANDOFF is None:
        return
    try:
        _M_NODE_HANDOFF.labels(
            result=result or "emitted",
            has_input=str(bool(has_input)).lower(),
            has_output=str(bool(has_output)).lower(),
        ).inc()
    except Exception:
        pass


# ---------------------------------------------------------------------
# Test-only access
# ---------------------------------------------------------------------

def _reset_for_tests() -> None:
    """Clear metric state between tests. Uses prometheus_client's
    internal _value resets; tolerant when metrics aren't registered."""
    _init()
    for m in (_M_ITERATION_COUNT, _M_TEST_RUN_DURATION,
              _M_CACHE_HIT_RATE, _M_CACHE_SIZE_ROWS, _M_CACHE_SIZE_BYTES):
        if m is None:
            continue
        try:
            if hasattr(m, "_metrics"):
                m._metrics.clear()
            if hasattr(m, "_sum"):
                m._sum.set(0)
            if hasattr(m, "_count"):
                m._count.set(0)
        except Exception:
            pass
    for m in (_M_CRITIC_VERDICT, _M_AGENT_RESULT, _M_RATE_LIMIT_HITS,
              _M_AUTO_BRIDGE, _M_CONTROL_FLOW):
        if m is None:
            continue
        try:
            if hasattr(m, "_metrics"):
                m._metrics.clear()
        except Exception:
            pass


# Register at import time so a fresh uvicorn worker's /metrics
# endpoint surfaces all of these BEFORE the first emit. `_init()` is
# idempotent (duplicate registration is caught + tolerated), so
# importing this module multiple times across forked workers is safe.
_init()


def get_metric_value(name: str, **labels) -> Optional[float]:
    """Test-only: read a metric's current value. Returns None when the
    metric isn't registered. Sum-of-samples for histograms; current
    value for gauges; running total for counters."""
    _init()
    by_name = {
        "wizard_agent_iteration_count": _M_ITERATION_COUNT,
        "wizard_agent_test_run_duration_seconds": _M_TEST_RUN_DURATION,
        "wizard_output_critic_verdict_total": _M_CRITIC_VERDICT,
        "wizard_agent_result_total": _M_AGENT_RESULT,
        "node_output_cache_hit_rate": _M_CACHE_HIT_RATE,
        "node_output_cache_size_rows": _M_CACHE_SIZE_ROWS,
        "node_output_cache_size_bytes": _M_CACHE_SIZE_BYTES,
        "wizard_test_run_rate_limit_hits_total": _M_RATE_LIMIT_HITS,
        "wizard_auto_bridge_inserted_total": _M_AUTO_BRIDGE,
        "control_flow_dispatched_total": _M_CONTROL_FLOW,
        "workflow_node_handoff_emitted_total": _M_NODE_HANDOFF,
    }
    m = by_name.get(name)
    if m is None:
        return None
    try:
        if labels:
            sample = m.labels(**labels)
        else:
            sample = m
        # Counter / Gauge: _value._value
        if hasattr(sample, "_value"):
            return sample._value.get()
        # Histogram: _sum
        if hasattr(sample, "_sum"):
            return sample._sum.get()
    except Exception:
        return None
    return None
