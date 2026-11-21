"""Fortress telemetry — Prometheus duration histograms for the compliance
hot paths (safety scanning + proof-bundle signing).

Same lazy / idempotent registration pattern as utils/wizard_metrics.py:
histograms are created once and cached in a module-level registry, so a
re-import (or a second `import main`) never raises
"Duplicated timeseries in CollectorRegistry".  If prometheus_client is
missing, or registration fails, every observe_* call is a silent no-op —
telemetry must never break the scan/sign path it wraps.

Label values here are fixed outcome enums chosen in code, never tenant
data (no unbounded cardinality).
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

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
            "fortress_metrics: %s — telemetry calls will no-op",
            _metrics_disabled_reason,
        )
        return None


_registered: dict[str, object] = {}

# Duration buckets: sub-ms scans up to multi-second signs.
_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


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
        # A re-import (or a second `import main`) hits "Duplicated timeseries":
        # reuse the collector the first import left in the registry.
        existing = _reuse(pc, name, labelnames)
        if existing is not None:
            _registered[name] = existing
            return existing
        logger.warning("fortress_metrics: failed to register %s: %s", name, exc)
        return None


def _reuse(pc, name: str, labelnames: tuple[str, ...], kind=None):
    """The collector already in the default registry under `name`, if any.

    Reaches into ``REGISTRY._names_to_collectors`` (private API) because
    prometheus_client has no public "get collector by name" lookup. Only
    returns a hit that is actually the expected collector kind with the same
    labelnames — a name collision with an unrelated/mismatched collector
    returns None rather than handing back something the caller can't use.
    Never raises.
    """
    try:
        collector = pc.REGISTRY._names_to_collectors.get(name)
        if collector is None or not isinstance(collector, kind or pc.Histogram):
            return None
        if tuple(getattr(collector, "_labelnames", ())) != tuple(labelnames):
            return None
        return collector
    except Exception:  # noqa: BLE001 — telemetry must never break callers
        return None


def _observe(name: str, seconds: float, outcome: str) -> None:
    """Feed one sample to a registered histogram; never raises."""
    h = _registered.get(name)
    if h is None:
        h = _get_histogram(
            name, _DESCRIPTIONS[name], ("outcome",), _BUCKETS)
    if h is None:
        return
    try:
        h.labels(outcome=str(outcome)).observe(float(seconds))
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: observe %s failed: %s", name, exc)


_DESCRIPTIONS = {
    "fortress_safety_scan_duration_seconds":
        "Duration of one output_safety_scanners scan_output call, by outcome",
    "fortress_proof_bundle_sign_duration_seconds":
        "Duration of one run_proof_bundle sign_bundle call, by outcome",
}


# ---------------------------------------------------------------------------
# Lazy Prometheus counters (fortress 9.1a)
# ---------------------------------------------------------------------------

_COUNTER_DESCRIPTIONS = {
    "proof_bundle_signed_total":
        "Total proof bundles signed, by status (ok|failed)",
    "output_safety_scan_results_total":
        "Output-safety scan verdicts, by scanner and verdict "
        "(clean|findings|error)",
}

# Bounded scanner vocabulary (services/output_safety_scanners.py registry).
# Anything else collapses to "other" so a tenant-registered scanner name can
# never grow the cardinality of the {scanner} label.
#
# This static frozenset is only the FALLBACK. The live vocabulary is
# recomputed at call time (_allowed_scanners, below) because BUG-933 made
# services/output_scan_config.known_scanners() read the live scanner
# registry: a hard-coded copy here goes stale (it has no `hive_ai`, so
# commercial-scan verdicts were labelled "other").
_KNOWN_SCANNERS = frozenset({
    "regex_pii", "secret_leak", "presidio", "heuristic_text",
    "clip", "heuristic_image", "nsfw_clip", "presidio_pii",
})
_OTHER_SCANNER = "other"


def _allowed_scanners() -> frozenset[str]:
    """Live scanner vocabulary: registry ∪ commercial ∪ static fallback.

    BUG-933 made the scanner registry live (``services.output_scan_config``
    exposes ``known_scanners()`` / ``KNOWN_SCANNERS`` resolved at call time),
    so the label set here must be resolved at call time too — otherwise
    commercial scanners (``hive_ai``) collapse to "other".

    Imports are lazy on purpose: utils must not import services at module
    import time (import cycle).  If either import or the registry read fails
    we fall back to the static frozenset, which is always safe.  Only names
    from the registries are ever emitted, so cardinality stays bounded —
    raw caller input never reaches the {scanner} label.
    """
    allowed = set(_KNOWN_SCANNERS)
    try:
        from services.output_scan_config import known_scanners
        allowed |= set(known_scanners())
        from services import commercial_scanners as _cs
        allowed |= set(_cs.COMMERCIAL_SCANNERS)
    except (ImportError, AttributeError, LookupError, TypeError, ValueError):
        logger.debug("fortress_metrics: scanner registry unavailable, "
                     "using static fallback vocabulary")
        return _KNOWN_SCANNERS
    return frozenset(allowed)


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
        existing = _reuse(pc, name, labelnames, kind=pc.Counter)
        if existing is not None:
            _registered[name] = existing
            return existing
        logger.warning("fortress_metrics: failed to register %s: %s", name, exc)
        return None


def _inc(name: str, labels: dict) -> None:
    """Feed one sample to a registered counter; never raises."""
    c = _registered.get(name)
    if c is None:
        spec = _COUNTER_LABELNAMES[name]
        c = _get_counter(name, _COUNTER_DESCRIPTIONS[name], spec)
    if c is None:
        return
    try:
        c.labels(**labels).inc()
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: inc %s failed: %s", name, exc)


_COUNTER_LABELNAMES = {
    "proof_bundle_signed_total": ("status",),
    "output_safety_scan_results_total": ("scanner", "verdict"),
    "compliance_policy_violations_total": ("framework", "rule"),
    "regulatory_submission_packages_generated_total": ("framework", "outcome"),
    "dp_budget_decisions_total": ("phase", "outcome"),
}


_COUNTER_DESCRIPTIONS["compliance_policy_violations_total"] = (
    "Total compliance policy violations, by framework and rule"
)
_COUNTER_DESCRIPTIONS["regulatory_submission_packages_generated_total"] = (
    "Total regulatory submission packages generated, by framework "
    "(hipaa|soc2|other) and outcome (completed|failed|other)"
)
_COUNTER_DESCRIPTIONS["dp_budget_decisions_total"] = (
    "Differential-privacy budget decisions, by phase "
    "(preflight|enqueue|settle|other) and outcome (ok|refused|error)"
)

# Bounded framework vocabulary. "privacy" is included alongside "gdpr"
# because services/compliance_environments.ALLOWED_ENVIRONMENTS uses
# "privacy" as the declared-environment value for GDPR in this codebase;
# it maps to itself here rather than collapsing to "other".
_KNOWN_FRAMEWORKS = frozenset({
    "hipaa", "gdpr", "privacy", "soc2", "pci_dss", "cmmc", "fedramp",
    "custom",
})
_OTHER_FRAMEWORK = "other"

# Rule sanitisation: keep only [a-z0-9_.-], truncate to 64 chars.
_RULE_RE = re.compile(r'[^a-z0-9_.\-]')


def _sanitise_rule(rule: str) -> str:
    """Normalise a rule string to the bounded label alphabet."""
    cleaned = str(rule).lower()
    cleaned = _RULE_RE.sub('_', cleaned)[:64]
    if not cleaned:
        cleaned = "empty"
    return cleaned


# Bounded distinct-rule set: max 200 before falling through to "other".
# Guarded by a lock since this is mutated from request-handling threads.
_rule_vocab: set[str] = set()
_rule_vocab_lock = threading.Lock()
_RULE_CAP = 200
_RULE_OTHER = "other"


def _lookup_rule(rule: str) -> str:
    """Map a rule through sanitisation + cardinality cap (thread-safe)."""
    key = _sanitise_rule(rule)
    with _rule_vocab_lock:
        if key in _rule_vocab:
            return key
        if len(_rule_vocab) >= _RULE_CAP:
            return _RULE_OTHER
        _rule_vocab.add(key)
    return key


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def inc_policy_violation(framework: str, rule: str) -> None:
    """Count one compliance-policy violation.

    Framework is bounded to the known set; rule is sanitized and capped at
    200 distinct values, after which extra rules collapse to "other".
    """
    fw = str(framework) if str(framework) in _KNOWN_FRAMEWORKS else _OTHER_FRAMEWORK
    rl = _lookup_rule(rule)
    _inc("compliance_policy_violations_total", {"framework": fw, "rule": rl})


# ---------------------------------------------------------------------------
# 721 — compliance policy-eval duration histogram
# ---------------------------------------------------------------------------

_POLICY_EVAL_HISTOGRAM_NAME = "compliance_policy_eval_duration_seconds"
_POLICY_EVAL_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_KNOWN_PHASES = frozenset({"compose", "dispatch"})
_OTHER_PHASE = "other"
_KNOWN_OUTCOMES = frozenset({"allow", "deny", "error"})
_POLICY_EVAL_LABELS = ("phase", "outcome")


def _observe_policy_eval(phase: str, outcome: str, seconds: float) -> None:
    """Feed one sample to the policy-eval histogram; never raises."""
    h = _registered.get(_POLICY_EVAL_HISTOGRAM_NAME)
    if h is None:
        h = _get_histogram(
            _POLICY_EVAL_HISTOGRAM_NAME,
            "Duration of one compliance policy-eval, by phase and outcome",
            _POLICY_EVAL_LABELS,
            _POLICY_EVAL_BUCKETS,
        )
    if h is None:
        return
    try:
        p = str(phase) if str(phase) in _KNOWN_PHASES else _OTHER_PHASE
        o = str(outcome) if str(outcome) in _KNOWN_OUTCOMES else "error"
        h.labels(phase=p, outcome=o).observe(float(seconds))
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: observe_policy_eval failed: %s", exc)


# ---------------------------------------------------------------------------
# Public observers
# ---------------------------------------------------------------------------

def observe_scan(seconds: float, outcome: str) -> None:
    """Record a scan_output duration (outcome: clean|findings|error)."""
    _observe("fortress_safety_scan_duration_seconds", seconds, outcome)


def observe_sign(seconds: float, outcome: str) -> None:
    """Record a sign_bundle duration (outcome: ok|error)."""
    _observe("fortress_proof_bundle_sign_duration_seconds", seconds, outcome)


def inc_sign(status: str) -> None:
    """Count one proof-bundle signing attempt (status: ok|failed)."""
    _inc("proof_bundle_signed_total",
         {"status": "failed" if str(status) != "ok" else "ok"})


def inc_scan_result(scanner: str, verdict: str) -> None:
    """Count one scanner verdict (verdict: clean|findings|error).

    `scanner` is bounded to the platform vocabulary — the live scanner
    registry (services/output_scan_config.known_scanners()) plus the
    commercial list, with the static frozenset as fallback — so a
    tenant-registered scanner name collapses to "other" and the {scanner}
    label cardinality stays capped.  Commercial names (e.g. "hive_ai") are
    resolved at call time, so their verdicts are labelled with their own
    name, not "other".
    """
    name = (str(scanner) if str(scanner) in _allowed_scanners()
            else _OTHER_SCANNER)
    _inc("output_safety_scan_results_total", {"scanner": name,
                                              "verdict": str(verdict)})


# ---------------------------------------------------------------------------
# 721 — compliance policy-eval public API
# ---------------------------------------------------------------------------


def observe_policy_eval(phase: str, outcome: str, seconds: float) -> None:
    """Record one compliance policy-eval duration.

    `phase` ∈ {compose, dispatch} else "other".
    `outcome` ∈ {allow, deny, error} else "error".
    """
    _observe_policy_eval(phase, outcome, seconds)


# ---------------------------------------------------------------------------
# 775 — fortress 9.1/9.2: submission packages, certifications, DP
#
# NOTE: dp_budget_consumed_ratio{tenant_id} is the deliberate exception to
# the fixed-enum rule: tenant ids are bounded by the tenant count and are
# always str(int), added per FORTRESS.9.1 (tracker 9.1 requires it).
# ---------------------------------------------------------------------------

_SUBMISSION_PKG_COUNTER = "regulatory_submission_packages_generated_total"
_SUBMISSION_PKG_HISTOGRAM = "submission_package_generation_duration_seconds"
_CERT_ACTIVE_GAUGE = "workflow_certifications_active_count"
_DP_BUDGET_COUNTER = "dp_budget_decisions_total"
_DP_NOISE_HISTOGRAM = "dp_noise_calibration_duration_seconds"
_DP_BUDGET_RATIO_GAUGE = "dp_budget_consumed_ratio"

_KNOWN_PKG_FRAMEWORKS = frozenset({"hipaa", "soc2"})
_OTHER_PKG_FRAMEWORK = "other"
_KNOWN_PKG_OUTCOMES = frozenset({"completed", "failed"})
_OTHER_PKG_OUTCOME = "other"

_KNOWN_CERT_KINDS = frozenset({"platform", "third_party"})
_OTHER_CERT_KIND = "other"
_CERT_EXPIRING_GAUGE = "fortress_certifications_expiring"
_KNOWN_CERT_WINDOWS = frozenset({"7d", "30d", "90d"})
_OTHER_CERT_WINDOW = "other"

_KNOWN_DP_PHASES = frozenset({"preflight", "enqueue", "settle"})
_OTHER_DP_PHASE = "other"
_KNOWN_DP_OUTCOMES = frozenset({"ok", "refused", "error"})
_OTHER_DP_OUTCOME = "error"

_KNOWN_DP_MECHANISMS = frozenset({"laplace", "gaussian"})
_OTHER_DP_MECHANISM = "other"

# Submission packages take seconds to tens of minutes: buckets 1s .. 30min.
_PKG_DURATION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800)
# DP-noise calibration runs sub-second to a few minutes.
_DP_NOISE_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 5, 15, 60, 300)


def _get_gauge(name: str, description: str,
               labelnames: tuple[str, ...]) -> Optional[object]:
    """Register a Gauge lazily/idempotently; never raises."""
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
        existing = _reuse(pc, name, labelnames, kind=pc.Gauge)
        if existing is not None:
            _registered[name] = existing
            return existing
        logger.warning("fortress_metrics: failed to register %s: %s", name, exc)
        return None


def inc_submission_package(framework: str, outcome: str) -> None:
    """Count one regulatory submission package generated.

    `framework` ∈ {hipaa, soc2} else "other".
    `outcome` ∈ {completed, failed} else "other".
    """
    fw = str(framework) if str(framework) in _KNOWN_PKG_FRAMEWORKS \
        else _OTHER_PKG_FRAMEWORK
    oc = str(outcome) if str(outcome) in _KNOWN_PKG_OUTCOMES else _OTHER_PKG_OUTCOME
    _inc(_SUBMISSION_PKG_COUNTER, {"framework": fw, "outcome": oc})


def observe_submission_package(seconds: float, outcome: str) -> None:
    """Record one submission-package generation duration (1s..30min buckets).

    `outcome` ∈ {completed, failed} else "other".
    """
    h = _registered.get(_SUBMISSION_PKG_HISTOGRAM)
    if h is None:
        h = _get_histogram(
            _SUBMISSION_PKG_HISTOGRAM,
            "Duration of one regulatory submission-package generation, by outcome",
            ("outcome",),
            _PKG_DURATION_BUCKETS,
        )
    if h is None:
        return
    try:
        oc = str(outcome) if str(outcome) in _KNOWN_PKG_OUTCOMES \
            else _OTHER_PKG_OUTCOME
        h.labels(outcome=oc).observe(float(seconds))
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: observe_submission_package failed: %s", exc)


def set_certifications_active(kind: str, count: int) -> None:
    """Set the active-certification count gauge.

    `kind` ∈ {platform, third_party} else "other".
    Non-integer `count` collapses to 0.
    """
    g = _registered.get(_CERT_ACTIVE_GAUGE)
    if g is None:
        g = _get_gauge(_CERT_ACTIVE_GAUGE,
                       "Active workflow certifications, by kind "
                       "(platform|third_party)", ("kind",))
    if g is None:
        return
    try:
        k = str(kind) if str(kind) in _KNOWN_CERT_KINDS else _OTHER_CERT_KIND
        try:
            c = int(count)
        except Exception:
            c = 0
        g.labels(kind=k).set(c)
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: set_certifications_active failed: %s", exc)


def set_certifications_expiring(window: str, kind: str, count: int) -> None:
    """Set the expiring-certification count gauge (fortress 9.3).

    `window` ∈ {7d, 30d, 90d} else "other" — cumulative buckets, i.e. a
    certification expiring in 5 days counts in 7d, 30d AND 90d.
    `kind` ∈ {platform, third_party} else "other" (same allow-list as
    set_certifications_active). Non-integer `count` collapses to 0.
    """
    g = _registered.get(_CERT_EXPIRING_GAUGE)
    if g is None:
        g = _get_gauge(_CERT_EXPIRING_GAUGE,
                       "Workflow certifications expiring within the window "
                       "(7d|30d|90d), by kind (platform|third_party)",
                       ("window", "kind"))
    if g is None:
        return
    try:
        w = str(window) if str(window) in _KNOWN_CERT_WINDOWS \
            else _OTHER_CERT_WINDOW
        k = str(kind) if str(kind) in _KNOWN_CERT_KINDS else _OTHER_CERT_KIND
        try:
            c = int(count)
        except Exception:
            c = 0
        g.labels(window=w, kind=k).set(c)
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: set_certifications_expiring failed: %s",
                     exc)


def inc_dp_budget(phase: str, outcome: str) -> None:
    """Count one differential-privacy budget decision.

    `phase` ∈ {preflight, enqueue, settle} else "other".
    `outcome` ∈ {ok, refused, error} else "error".
    """
    p = str(phase) if str(phase) in _KNOWN_DP_PHASES else _OTHER_DP_PHASE
    o = str(outcome) if str(outcome) in _KNOWN_DP_OUTCOMES else _OTHER_DP_OUTCOME
    _inc(_DP_BUDGET_COUNTER, {"phase": p, "outcome": o})


def observe_dp_noise(seconds: float, mechanism: str) -> None:
    """Record one DP-noise calibration duration.

    `mechanism` ∈ {laplace, gaussian} else "other".
    """
    h = _registered.get(_DP_NOISE_HISTOGRAM)
    if h is None:
        h = _get_histogram(
            _DP_NOISE_HISTOGRAM,
            "Duration of one differential-privacy noise calibration, by mechanism",
            ("mechanism",),
            _DP_NOISE_BUCKETS,
        )
    if h is None:
        return
    try:
        m = str(mechanism) if str(mechanism) in _KNOWN_DP_MECHANISMS \
            else _OTHER_DP_MECHANISM
        h.labels(mechanism=m).observe(float(seconds))
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: observe_dp_noise failed: %s", exc)


def set_dp_budget_ratio(tenant_id: int, ratio: float) -> None:
    """Set the tenant-level DP-budget consumed ratio gauge (FORTRESS.9.1).

    Gauge name ``dp_budget_consumed_ratio``, single label ``tenant_id``
    (always ``str(int(tenant_id))``).  ``ratio`` is clamped below to 0 —
    over-cap values (> 1) are reported as-is, never hidden.  Silent no-op
    when prometheus_client is missing or registration fails.
    """
    g = _registered.get(_DP_BUDGET_RATIO_GAUGE)
    if g is None:
        g = _get_gauge(_DP_BUDGET_RATIO_GAUGE,
                       "Differential-privacy budget consumed ratio "
                       "(spent/cap), by tenant_id", ("tenant_id",))
    if g is None:
        return
    try:
        try:
            tid = str(int(tenant_id))
            r = float(ratio)
        except Exception:
            return
        if r < 0:
            r = 0.0
        g.labels(tenant_id=tid).set(r)
    except Exception as exc:  # noqa: BLE001 — telemetry must not break callers
        logger.debug("fortress_metrics: set_dp_budget_ratio failed: %s", exc)
