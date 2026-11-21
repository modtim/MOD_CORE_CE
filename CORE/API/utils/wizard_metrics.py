"""Wizard pipeline telemetry — Prometheus counters + Postgres event log.

Dual-write: every wizard event increments an in-process Prometheus
counter (for real-time ops / Grafana) AND inserts a row into the
wizard_event_log table (for durable, compliance-grade audit data
that survives container restarts).

The Dashboard reads from the DB via /platform/wizard-stats. Prometheus
stays for ops alerting but is no longer the dashboard's source of truth.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy Prometheus client singleton
# ---------------------------------------------------------------------------

_prom_module = None
_metrics_disabled_reason: Optional[str] = None


def _prom():
    global _prom_module, _metrics_disabled_reason
    if _prom_module is not None:
        return _prom_module
    if _metrics_disabled_reason is not None:
        return None
    try:
        import prometheus_client as _pc
        _prom_module = _pc
        return _prom_module
    except Exception as exc:
        _metrics_disabled_reason = f"prometheus_client unavailable: {exc!r}"
        logger.warning(
            "wizard_metrics: %s — telemetry calls will no-op",
            _metrics_disabled_reason,
        )
        return None


_registered: dict[str, object] = {}


def _get_counter(name: str, description: str, labelnames: tuple[str, ...]):
    if name in _registered:
        return _registered[name]
    pc = _prom()
    if pc is None:
        return None
    try:
        c = pc.Counter(name, description, labelnames)
        _registered[name] = c
        return c
    except Exception as exc:
        logger.warning("wizard_metrics: failed to register %s: %s", name, exc)
        return None


def _get_histogram(
    name: str, description: str, labelnames: tuple[str, ...],
    buckets: Optional[tuple[float, ...]] = None,
):
    if name in _registered:
        return _registered[name]
    pc = _prom()
    if pc is None:
        return None
    try:
        if buckets:
            h = pc.Histogram(name, description, labelnames, buckets=buckets)
        else:
            h = pc.Histogram(name, description, labelnames)
        _registered[name] = h
        return h
    except Exception as exc:
        logger.warning("wizard_metrics: failed to register %s: %s", name, exc)
        return None


def _get_gauge(name: str, description: str, labelnames: tuple[str, ...]):
    if name in _registered:
        return _registered[name]
    pc = _prom()
    if pc is None:
        return None
    try:
        g = pc.Gauge(name, description, labelnames)
        _registered[name] = g
        return g
    except Exception as exc:
        logger.warning("wizard_metrics: failed to register %s: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Postgres persistence — durable event log
# ---------------------------------------------------------------------------

def _persist_event(
    event_type: str,
    metadata: Dict[str, Any],
    *,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    try:
        from database import SessionLocal
        import models
        db = SessionLocal()
        try:
            row = models.WizardEventLog(
                event_type=event_type,
                tenant_id=tenant_id,
                build_spec_id=build_spec_id,
                workflow_run_id=workflow_run_id,
                metadata_=metadata,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("wizard_metrics: _persist_event(%s) failed", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# Allowed label value sets
# ---------------------------------------------------------------------------

ALLOWED_MODES = frozenset({
    "image", "git", "dockerfile", "script", "describe-codegen",
    "legacy",
})

ALLOWED_EXTRACTION_RESULTS = frozenset({"success", "partial", "failure"})

ALLOWED_REPAIR_OUTCOMES = frozenset({
    "reverted",
    "delta-applied",
    "exhausted",
    "thrash-bail",
    "infra-retry",
    "from-lock-reject",
    "rejected",
})

ALLOWED_VERDICTS = frozenset({"match", "drift", "incompatible", "infra-fail"})

# Accelerator label is open-ended — workers can advertise new
# accelerator tags (habana, tpu, inferentia, trainium, etc.) and the
# metric label should reflect what was actually used. Cardinality is
# bounded by the worker pool's distinct hardware tags + the lookup
# allowlist below. _validate_label uses both signals: in-frozenset OR
# safe-string-shape passes; anything else coerces to "unknown".
ALLOWED_ACCELERATORS = frozenset({
    "cuda", "rocm", "intel", "xpu", "npu", "cpu", "unknown",
})

# Permissive shape check for accelerator strings the platform hasn't
# pre-listed but a worker advertised. Lowercase alnum/underscore/dash,
# max 24 chars — catches realistic accelerator names (habana,
# inferentia, tpu_v4, etc.) while rejecting label-explosion attempts.
import re as _accel_re
_ACCEL_SHAPE_RE = _accel_re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

ALLOWED_LLM_PURPOSES = frozenset({
    "tag", "extract", "delta", "cascade-rank",
    "suggest", "synthesize", "rewrite", "analyze",
})

ALLOWED_CACHE_RESULTS = frozenset({"hit", "miss", "expired"})

ALLOWED_BUILD_OUTCOMES = frozenset({
    "success", "failed", "cancelled", "abandoned",
})


def _validate_label(value: str, allowed: frozenset[str], label_name: str) -> str:
    if value in allowed:
        return value
    # Accelerator label is open-ended: any worker-advertised tag with
    # a safe alphanumeric shape passes through. Bounds cardinality at
    # the shape level so a malformed input can't blow up Prometheus.
    if label_name == "accelerator" and isinstance(value, str) and _ACCEL_SHAPE_RE.match(value):
        return value
    logger.warning(
        "wizard_metrics: %s label %r not in allowed set; coercing to 'unknown'",
        label_name, value,
    )
    return "unknown"


# ---------------------------------------------------------------------------
# Public recorder API
# ---------------------------------------------------------------------------

def record_extraction(
    *,
    result: str,
    mode: str,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    r = _validate_label(result, ALLOWED_EXTRACTION_RESULTS, "result")
    m = _validate_label(mode, ALLOWED_MODES, "mode")
    c = _get_counter(
        "wizard_extraction_total",
        "BuildPlan extractions, partitioned by outcome + wizard mode.",
        ("result", "mode"),
    )
    if c is not None:
        try:
            c.labels(result=r, mode=m).inc()
        except Exception:
            logger.debug("wizard_metrics: record_extraction prom failed", exc_info=True)
    _persist_event("extraction", {"result": r, "mode": m},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


def record_buildplan_confidence(
    *,
    mode: str,
    confidence: float,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    m = _validate_label(mode, ALLOWED_MODES, "mode")
    conf = max(0.0, min(1.0, float(confidence)))
    h = _get_histogram(
        "wizard_buildplan_confidence",
        "BuildPlan confidence score distribution by mode.",
        ("mode",),
        buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0),
    )
    if h is not None:
        try:
            h.labels(mode=m).observe(conf)
        except Exception:
            logger.debug("wizard_metrics: record_buildplan_confidence prom failed", exc_info=True)
    _persist_event("buildplan_confidence", {"mode": m, "confidence": conf},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


def record_repair(
    *,
    outcome: str,
    mode: str,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    o = _validate_label(outcome, ALLOWED_REPAIR_OUTCOMES, "outcome")
    m = _validate_label(mode, ALLOWED_MODES, "mode")
    c = _get_counter(
        "wizard_repair_total",
        "Wizard repair attempts, partitioned by outcome + mode.",
        ("outcome", "mode"),
    )
    if c is not None:
        try:
            c.labels(outcome=o, mode=m).inc()
        except Exception:
            logger.debug("wizard_metrics: record_repair prom failed", exc_info=True)
    _persist_event("repair", {"outcome": o, "mode": m},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


def record_variant_test(
    *,
    verdict: str,
    accelerator: str,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    v = _validate_label(verdict, ALLOWED_VERDICTS, "verdict")
    a = _validate_label(accelerator, ALLOWED_ACCELERATORS, "accelerator")
    c = _get_counter(
        "wizard_variant_test_total",
        "Per-variant sample-test verdicts, partitioned by accelerator.",
        ("verdict", "accelerator"),
    )
    if c is not None:
        try:
            c.labels(verdict=v, accelerator=a).inc()
        except Exception:
            logger.debug("wizard_metrics: record_variant_test prom failed", exc_info=True)
    _persist_event("variant_test", {"verdict": v, "accelerator": a},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


def record_build_duration(
    *,
    mode: str,
    outcome: str,
    seconds: float,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    m = _validate_label(mode, ALLOWED_MODES, "mode")
    o = _validate_label(outcome, ALLOWED_BUILD_OUTCOMES, "outcome")
    s = max(0.0, float(seconds))
    h = _get_histogram(
        "wizard_build_duration_seconds",
        "End-to-end wizard build duration, partitioned by mode + final outcome.",
        ("mode", "outcome"),
        buckets=(30, 60, 120, 300, 600, 1200, 1800, 2700, 3600, 5400, 7200),
    )
    if h is not None:
        try:
            h.labels(mode=m, outcome=o).observe(s)
        except Exception:
            logger.debug("wizard_metrics: record_build_duration prom failed", exc_info=True)
    _persist_event("build_duration", {"mode": m, "outcome": o, "seconds": s},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


def record_llm_call(
    *,
    purpose: str,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    p = _validate_label(purpose, ALLOWED_LLM_PURPOSES, "purpose")
    c = _get_counter(
        "wizard_llm_calls_total",
        "Wizard LLM call count, partitioned by purpose.",
        ("purpose",),
    )
    if c is not None:
        try:
            c.labels(purpose=p).inc()
        except Exception:
            logger.debug("wizard_metrics: record_llm_call prom failed", exc_info=True)
    _persist_event("llm_call", {"purpose": p},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


def set_speculative_queue_depth(
    *,
    accelerator: str,
    depth: int,
    tenant_id: Optional[int] = None,
) -> None:
    a = _validate_label(accelerator, ALLOWED_ACCELERATORS, "accelerator")
    d = max(0, int(depth))
    g = _get_gauge(
        "wizard_speculative_queue_depth",
        "Pending speculative variant-test queue depth, per accelerator.",
        ("accelerator",),
    )
    if g is not None:
        try:
            g.labels(accelerator=a).set(d)
        except Exception:
            logger.debug("wizard_metrics: set_speculative_queue_depth prom failed", exc_info=True)
    _persist_event("speculative_queue", {"accelerator": a, "depth": d},
                   tenant_id=tenant_id)


def record_repo_docs_cache(
    *,
    result: str,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    r = _validate_label(result, ALLOWED_CACHE_RESULTS, "result")
    c = _get_counter(
        "wizard_repo_docs_cache_total",
        "Repo-docs extract cache outcomes (hit/miss/expired).",
        ("result",),
    )
    if c is not None:
        try:
            c.labels(result=r).inc()
        except Exception:
            logger.debug("wizard_metrics: record_repo_docs_cache prom failed", exc_info=True)
    _persist_event("repo_docs_cache", {"result": r},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)


ALLOWED_PREFLIGHT_RESULTS = frozenset({
    "exists", "not-found", "malformed", "not-a-registry",
    "wrong-mediatype", "auth-required", "unknown",
})


def record_image_preflight(
    *,
    result: str,
    registry: str,
    tenant_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
) -> None:
    r = _validate_label(result, ALLOWED_PREFLIGHT_RESULTS, "result")
    registry_label = (registry or "unknown")[:64]
    c = _get_counter(
        "wizard_image_preflight_total",
        "Image-ref preflight outcomes, partitioned by result + registry.",
        ("result", "registry"),
    )
    if c is not None:
        try:
            c.labels(result=r, registry=registry_label).inc()
        except Exception:
            logger.debug("wizard_metrics: record_image_preflight prom failed", exc_info=True)
    _persist_event("image_preflight", {"result": r, "registry": registry_label},
                   tenant_id=tenant_id, build_spec_id=build_spec_id,
                   workflow_run_id=workflow_run_id)
