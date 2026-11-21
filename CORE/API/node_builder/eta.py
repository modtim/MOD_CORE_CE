"""wizard-rebuild §10c — build ETA estimator + stall detection.

Two read-only surfaces consumed by the WorkflowBuilder's BuildQueueRow:

  - ``estimate_eta(plan_dict) → int seconds``
    Heuristic time-to-completion from the BuildPlan's shape. The
    longer the install_steps, the more system_deps, the heavier
    the base image — the higher the ETA. Designed to be a useful
    rough cut, not a contract: users see "~12 min" and set
    expectations; the actual build may be ±50%. Better than
    "indeterminate".

  - ``detect_stall(workflow_run, last_event_age_s) → str | None``
    Returns a stall reason string when the workflow has been
    RUNNING with no log update for >5 minutes. Used by the UI
    to render a yellow warning banner with operator-actionable
    guidance.

Heuristics tuned against stage observation (most wizard builds
take 5–15 min depending on base image pull + install_steps
count). Easy to tune as we gather more data.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# Per-step seconds. Aligned with stage observation: a typical
# `pip install foo` costs ~10s wall-clock (pull + resolve +
# install), apt-get installs ~5s. Buffer accounts for cold-cache
# base-image pull + Docker layer extraction.
_INSTALL_STEP_S = 10
_SYSTEM_DEP_S = 5
_BASE_IMAGE_PULL_S = 60      # generous floor; CUDA bases trigger
                              # multi-minute pulls but stage caches
                              # warm copies for the curated ones.
_OVERHEAD_S = 120             # fixed: spec persist → workflow create
                              # → orchestrator dispatch → register.

# Per-variant cost. Speculative builds in §9 fan out N variants;
# each one needs its own build + push, so total time scales.
_PER_VARIANT_S = 90


# Output type-driven sample-test cost. Image / video sample-tests
# run on real input bytes which takes longer; binary / text are
# fast.
_SAMPLE_TEST_S = {
    "image":     45,
    "video":     90,
    "audio":     45,
    "text":      15,
    "json":      15,
    "embedding": 30,
    "3d_mesh":   60,
    "binary":    15,
}


def estimate_eta(plan_dict: Dict[str, Any], *, variant_count: int = 1) -> int:
    """Return an estimated total build time in seconds for ``plan``.

    Args:
      plan_dict: the BuildSpec's ``build_plan`` dict (Pydantic-
        serialized BuildPlan). Missing / malformed → falls back to
        a generic 5-minute estimate.
      variant_count: total variant count from the BuildPlan's
        fan-out. Defaults to 1 (hero only). Each non-hero variant
        adds `_PER_VARIANT_S` to the total.

    The estimate is monotonic in plan complexity: a plan with 0
    install_steps + 0 system_deps returns the floor; each extra
    step / dep pushes the ETA up linearly.
    """
    if not isinstance(plan_dict, dict):
        return _OVERHEAD_S + 5 * 60
    install_steps = plan_dict.get("install_steps") or []
    system_deps = plan_dict.get("system_deps") or []
    output_type = "binary"
    try:
        out_ports = plan_dict.get("output_ports") or []
        if out_ports and isinstance(out_ports, list):
            output_type = (out_ports[0] or {}).get("type") or "binary"
    except Exception:
        pass

    sample_cost = _SAMPLE_TEST_S.get(output_type, 30)
    install_cost = len(install_steps) * _INSTALL_STEP_S
    syslib_cost = len(system_deps) * _SYSTEM_DEP_S
    variant_extra = max(0, variant_count - 1) * _PER_VARIANT_S

    total = (
        _OVERHEAD_S
        + _BASE_IMAGE_PULL_S
        + install_cost
        + syslib_cost
        + sample_cost
        + variant_extra
    )
    return int(total)


def detect_stall(
    workflow_run_status: str,
    last_event_age_s: Optional[float],
    *,
    threshold_s: int = 5 * 60,
) -> Optional[str]:
    """Return a stall reason string when the run is RUNNING but
    no log update has landed in `threshold_s` seconds. None means
    "no stall detected" — UI renders nothing.

    Args:
      workflow_run_status: ``WorkflowRun.status`` value (string
        form — ``"RUNNING"`` / ``"COMPLETED"`` / etc.).
      last_event_age_s: seconds since the most recent
        ``workflow_run_events`` row for this run. None when no
        events have landed yet — caller is expected to treat
        "no events at all" as "not stalled yet" (the build may
        still be in startup).
      threshold_s: default 5 minutes per the §10 spec.

    Stall detection is intentionally conservative: only RUNNING
    runs with a known last-event age above threshold count.
    """
    if not workflow_run_status:
        return None
    status_norm = workflow_run_status.upper().replace("TASKSTATUS.", "")
    if status_norm != "RUNNING":
        return None
    if last_event_age_s is None:
        return None
    if last_event_age_s <= threshold_s:
        return None
    minutes = int(last_event_age_s // 60)
    return (
        f"build appears stalled — last activity {minutes} min ago. "
        f"Worker may be hung; check the Workers dashboard."
    )
