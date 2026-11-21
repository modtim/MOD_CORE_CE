"""workflow-agentic-execution-loop §wizard-test-run-dispatch — pure
helper that computes the sandbox payload routing the orchestrator
injects into per-node task_payloads when purpose='wizard_test_run'.

Pulled out of services.workflow_orchestrator so the contract is testable
without spinning up the full orchestrator's dependency surface (redis,
worker_tasks router, ApiTaskQueue, etc.).

The output of compute_sandbox_payload_fields() is a small dict the
orchestrator merges into every node_conf before enqueuing. Production
runs get empty/None values; sandbox runs get the isolated prefix +
timeout + size cap.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# A path segment is safe when it is a bounded run of id-like characters —
# no `/`, no `..`, no NUL: a candidate/pairing id can never traverse out of
# its own prefix.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _safe_int(value: Any) -> Optional[int]:
    """`value` as a non-negative int path segment, or None (bools excluded —
    `isinstance(True, int)` is True in Python but a bool is never a valid id)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _safe_segment(value: Any) -> Optional[str]:
    """`value` as a safe path segment string, or None."""
    if not isinstance(value, str):
        return None
    return value if _SAFE_SEGMENT_RE.match(value) else None


def compute_sandbox_payload_fields(
    run_purpose: Optional[str],
    conf: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute the sandbox-mode routing fields for a task payload given
    the WorkflowRun's purpose + the run's `parameters` conf dict.

    Returns a dict with four keys that get merged into the per-node
    task_payload. Production runs get a default payload with `purpose='production'`
    and empty/None for the rest. Sandbox runs get the per-tenant isolated
    prefix + the timeout + size cap supplied by the route handler.

    All inputs are tolerated as None / partial dicts — this is called on
    every dispatch, so it must be safe against legacy WorkflowRun rows
    that pre-date the purpose column.
    """
    purpose = (run_purpose or "production")
    safe_conf = conf or {}

    output_prefix: str = ""
    sandbox_timeout_s: Optional[int] = None
    sandbox_max_output_mb: Optional[int] = None

    if purpose == "wizard_test_run":
        sess_id = safe_conf.get("wizard_session_id")
        iter_n = safe_conf.get("wizard_test_run_iteration")
        if sess_id is not None and iter_n is not None:
            output_prefix = f"_wizard_test_runs/{sess_id}/{iter_n}/"
        # Optional — when not supplied, worker uses defaults
        # (60s timeout, 100MB cap).
        sandbox_timeout_s = safe_conf.get("sandbox_timeout_s")
        sandbox_max_output_mb = safe_conf.get("sandbox_max_output_mb")
    elif purpose == "adversary_dispatch":
        # WSIP 6.4 slice 1 (services/adversary_dispatch.py) — adversary
        # candidates get the SAME sandbox semantics but their own isolated
        # output prefix. The SPEC's literal path (the internal design notes
        # workflow-self-improving-production/specs/workflow-adversarial-
        # coevolution/spec.md:30) is keyed by the TARGET workflow (not the
        # adversary pairing row id) + GA generation + candidate id, all
        # carried in conf["adversary"] (ids only — the candidate value is
        # never in the routing fields). Each component is validated as a
        # safe path segment (int for workflow_id/generation, id-like string
        # for candidate_id — no `/`, no `..`); any missing/unsafe component
        # -> no prefix (nothing is written to a shared/traversed path).
        adv = safe_conf.get("adversary")
        adv = adv if isinstance(adv, dict) else {}
        wf_id = _safe_int(adv.get("workflow_id"))
        generation = _safe_int(adv.get("generation"))
        cand_id = _safe_segment(adv.get("candidate_id"))
        if wf_id is not None and generation is not None and cand_id is not None:
            output_prefix = f"_adversary_dispatches/{wf_id}/{generation}/{cand_id}/"
        sandbox_timeout_s = safe_conf.get("sandbox_timeout_s")
        sandbox_max_output_mb = safe_conf.get("sandbox_max_output_mb")

    return {
        "purpose": purpose,
        "output_prefix": output_prefix,
        "sandbox_timeout_s": sandbox_timeout_s,
        "sandbox_max_output_mb": sandbox_max_output_mb,
    }
