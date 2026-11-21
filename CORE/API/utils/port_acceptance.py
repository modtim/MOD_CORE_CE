"""Phase 11 — perf-gate acceptance evaluator.

Gate logic that runs against a smoke_probe report BEFORE flipping a
variant's `variant_verification_status` to `"verified"`. The probe
captures K forward passes per variant + CPU reference (Phase 11.1);
this module reads those numbers and decides whether the variant is
"fast enough" relative to the operator's threshold.

Shape of the input `smoke_report`:

    {
      "ok": True,
      "variant": "rocm",
      "forward": {
        "forward_ok": True,
        "perf": {
          "k": 20, "warmup": 3,
          "variant":       {"p50_ms": 12.3, "p99_ms": 15.1, ...},
          "cpu_reference": {"p50_ms": 85.2, "p99_ms": 92.5, ...},
          "ratio_p50": 6.93,   # cpu / variant — larger = faster
          "ratio_p99": 6.12
        },
        "reference_comparison": {"passed": True, ...}
      }
    }

Shape of the output:

    {
      "accepted":      True | False,
      "gate_result":   "accuracy_failed" | "perf_too_slow" |
                       "perf_skipped" | "accepted",
      "ratio_p50":     float | None,
      "ratio_p99":     float | None,
      "variant_p50_ms": float | None,
      "variant_p99_ms": float | None,
      "threshold":     float,
      "reason":        str (human readable)
    }

Acceptance rule (Phase 11 plan):
  Both p50 AND p99 ratios must be ≥ threshold for perf acceptance.
  Accuracy must pass independently (reference_comparison.passed).
  When K=0 (operator disabled the perf pass), perf_skipped returns
  accepted=True as long as accuracy passed — we don't punish nodes
  where perf isn't meaningful.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default ratio threshold when Plan.acceptance_perf_ratio is null. 0.25
# means: the variant must be at least 25% as fast as CPU on this node.
# Pick conservatively — nodes with modest GPU speedups (small tensor
# ops, python overhead bound) shouldn't be unduly punished. Tune
# upward once real-world distributions are known (Phase 10 dashboard).
DEFAULT_ACCEPTANCE_PERF_RATIO = 0.25


def _resolve_threshold(plan_threshold: Optional[float]) -> float:
    """Caller may pass Plan.acceptance_perf_ratio directly. Env var
    MOD_ACCEPTANCE_PERF_RATIO_DEFAULT overrides the module default."""
    if plan_threshold is not None:
        try:
            v = float(plan_threshold)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    try:
        env_val = float(os.getenv(
            "MOD_ACCEPTANCE_PERF_RATIO_DEFAULT",
            str(DEFAULT_ACCEPTANCE_PERF_RATIO),
        ))
        if env_val > 0:
            return env_val
    except (TypeError, ValueError):
        pass
    return DEFAULT_ACCEPTANCE_PERF_RATIO


def evaluate_acceptance(
    smoke_report: Optional[Dict[str, Any]],
    *,
    plan_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """Decide whether a variant should be accepted as verified.

    Returns an `accepted` bool + diagnostics. Never raises — a
    malformed smoke_report yields a conservative "no" with
    gate_result="malformed_report".
    """
    threshold = _resolve_threshold(plan_threshold)
    if not isinstance(smoke_report, dict):
        return {
            "accepted": False,
            "gate_result": "malformed_report",
            "ratio_p50": None,
            "ratio_p99": None,
            "variant_p50_ms": None,
            "variant_p99_ms": None,
            "threshold": threshold,
            "reason": "smoke_report is not a dict",
        }

    # Accuracy gate first — a drifted variant is never accepted, even
    # if perf would pass.
    forward = smoke_report.get("forward") or {}
    if not isinstance(forward, dict) or not forward.get("forward_ok"):
        return {
            "accepted": False,
            "gate_result": "accuracy_failed",
            "ratio_p50": None,
            "ratio_p99": None,
            "variant_p50_ms": None,
            "variant_p99_ms": None,
            "threshold": threshold,
            "reason": "forward stage did not report forward_ok=True",
        }
    ref = forward.get("reference_comparison")
    if isinstance(ref, dict) and ref.get("passed") is False:
        return {
            "accepted": False,
            "gate_result": "accuracy_failed",
            "ratio_p50": None,
            "ratio_p99": None,
            "variant_p50_ms": None,
            "variant_p99_ms": None,
            "threshold": threshold,
            "reason": (
                f"reference comparison failed: "
                f"{ref.get('reason', 'unknown drift')}"
            ),
        }

    perf = forward.get("perf") or {}
    if not isinstance(perf, dict) or perf.get("skipped") or perf.get("k", 0) == 0:
        # Perf pass disabled by operator — accept on accuracy alone.
        return {
            "accepted": True,
            "gate_result": "perf_skipped",
            "ratio_p50": None,
            "ratio_p99": None,
            "variant_p50_ms": None,
            "variant_p99_ms": None,
            "threshold": threshold,
            "reason": "perf capture skipped (K=0 or unavailable)",
        }

    variant_stats = perf.get("variant") or {}
    cpu_stats = perf.get("cpu_reference") or {}
    ratio_p50 = perf.get("ratio_p50")
    ratio_p99 = perf.get("ratio_p99")
    variant_p50 = variant_stats.get("p50_ms") if isinstance(variant_stats, dict) else None
    variant_p99 = variant_stats.get("p99_ms") if isinstance(variant_stats, dict) else None

    # If ratios are null (CPU reference was skipped) but variant samples
    # exist, we still accept — no comparison available. This covers the
    # "CPU reference too slow on this node" escape-hatch path.
    if ratio_p50 is None or ratio_p99 is None:
        cpu_skipped = (
            isinstance(cpu_stats, dict) and (
                cpu_stats.get("skipped") or cpu_stats.get("error")
            )
        )
        if cpu_skipped:
            return {
                "accepted": True,
                "gate_result": "perf_cpu_skipped",
                "ratio_p50": None,
                "ratio_p99": None,
                "variant_p50_ms": variant_p50,
                "variant_p99_ms": variant_p99,
                "threshold": threshold,
                "reason": (
                    "CPU reference skipped — accepting on accuracy + "
                    "variant-only perf sample"
                ),
            }
        return {
            "accepted": False,
            "gate_result": "malformed_report",
            "ratio_p50": ratio_p50,
            "ratio_p99": ratio_p99,
            "variant_p50_ms": variant_p50,
            "variant_p99_ms": variant_p99,
            "threshold": threshold,
            "reason": "perf ratios null but CPU reference did not report skipped/error",
        }

    try:
        r50 = float(ratio_p50)
        r99 = float(ratio_p99)
    except (TypeError, ValueError):
        return {
            "accepted": False,
            "gate_result": "malformed_report",
            "ratio_p50": ratio_p50,
            "ratio_p99": ratio_p99,
            "variant_p50_ms": variant_p50,
            "variant_p99_ms": variant_p99,
            "threshold": threshold,
            "reason": "perf ratios not numeric",
        }

    # Phase 11 plan §gating risk — fail only when BOTH p50 and p99
    # exceed threshold. Guards against a single noisy p99 sample (shared
    # infrastructure, cold cache) falsely rejecting a healthy variant.
    if r50 < threshold and r99 < threshold:
        return {
            "accepted": False,
            "gate_result": "perf_too_slow",
            "ratio_p50": r50,
            "ratio_p99": r99,
            "variant_p50_ms": variant_p50,
            "variant_p99_ms": variant_p99,
            "threshold": threshold,
            "reason": (
                f"perf ratio below threshold: p50={r50:.2f} p99={r99:.2f} "
                f"(threshold {threshold:.2f} = variant must be at least "
                f"{int(threshold*100)}% as fast as CPU)"
            ),
        }

    return {
        "accepted": True,
        "gate_result": "accepted",
        "ratio_p50": r50,
        "ratio_p99": r99,
        "variant_p50_ms": variant_p50,
        "variant_p99_ms": variant_p99,
        "threshold": threshold,
        "reason": (
            f"accuracy passed + perf p50={r50:.2f} p99={r99:.2f} "
            f"both ≥ threshold {threshold:.2f}"
        ),
    }


def extract_ref_cuda_ratio(smoke_report: Optional[Dict[str, Any]]) -> Optional[float]:
    """Pull the variant/CPU p50 ratio out of a smoke report for
    persistence onto PortOutcome.ref_cuda_ratio. The column is named
    `ref_cuda_ratio` (historical) but in Phase 11 it's actually the
    ratio vs CPU reference — fine because CPU is the universal floor
    and the dashboard labels it clearly."""
    if not isinstance(smoke_report, dict):
        return None
    forward = smoke_report.get("forward") or {}
    perf = forward.get("perf") if isinstance(forward, dict) else None
    if not isinstance(perf, dict):
        return None
    r = perf.get("ratio_p50")
    try:
        return float(r) if r is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "evaluate_acceptance",
    "extract_ref_cuda_ratio",
    "DEFAULT_ACCEPTANCE_PERF_RATIO",
]
