"""
Workflow Orchestrator.

Builds execution plans from workflow graphs and enqueues root-level tasks
on ApiTaskQueue. Downstream dispatch is event-driven via
_check_and_dispatch_downstream (called on each task completion in
worker_tasks.py).

Pure Python + SQLAlchemy.
"""

import importlib
import json
from product_names import themed_logger
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session

# expr 9.2 (tasks.md 9.2) — long_running pre-dispatch checkpoint gate.
# Module-level like the other service import this module carries
# (platform_settings, below): long_running_dispatch imports only models +
# the quota helper, never this module, so there is no cycle.
# CE lazy import (1634o): long_running_dispatch is Paid-only (stripped from
# the CE carve). Absent on CE -> both sentinels None; the dispatch call site
# below treats a None pre_dispatch_gate as "feature not present" and skips
# the gate entirely, so non-long_running runs (the only kind CE has) are
# never blocked.
try:
    from services.long_running_dispatch import is_long_running, pre_dispatch_gate
except (ImportError, ModuleNotFoundError):
    is_long_running = None
    pre_dispatch_gate = None

logger = themed_logger(__name__, 'workflows')


def _module_available(module_name: str) -> bool:
    """CE build check — the CE build strips whole service modules out of the
    tree (manifest.yaml `deny`).  Guard lazy `from services.x import y`
    inside a broad `except` so we never log a traceback at boot."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


SYSTEM_BUILD_TASK_DAG_IDS = frozenset({
    "system_smoke_one_variant",
    "system_push_one_variant",
    "system_sample_test",
    # wizard-rebuild §5 — per-variant sample-test runs the user image
    # via `docker run` (just like smoke does) inside the build-task
    # container, so it needs the host docker.sock bind-mount too.
    "system_sample_test_one_variant",
    "system_register_node",
    # Hero variant uses an auto-iterating test-and-repair node in
    # place of the legacy build→smoke pair. It runs `docker build`
    # inside the build-task container, so it MUST receive /var/run/
    # docker.sock the same way system_build_one_variant does.
    # Without this entry the LLM correctly reports the Dockerfile is
    # fine and the failure is at the docker daemon — and then thrashes
    # through outer-repair attempts trying to fix what isn't broken.
    "system_test_and_repair_one_variant",
})


HUMAN_APPROVAL_NODE_KIND = "human_approval"

# FX 4.1 — the graph node kind services/agent_node_runtime.py runs API-side.
AGENT_NODE_KIND = "agent"

# FIX-CE-DP-NOISE-FAIL-CLOSED (owner decision 11-10 10:50 PM CT) — the FIXED text
# a CE node/task carries when it asked for differential privacy but services.dp_noise
# is absent (paid). Fixed text only: never an exception class name, never a module
# name (a CE user must not learn the name of a paid module).
DP_NOISE_UNAVAILABLE = "differential privacy is not available in Community Edition"


def _compute_input_bytes(node_data: dict) -> Optional[int]:
    """Sum total input bytes from node_data.

    Resolution order (first match wins):

    1. **Explicit ``input_size_bytes``** – set by the graph builder when
       upstream file sizes are known (single pre-computed value).

    2. **``file_sizes`` list** – sum of per-file sizes set by the graph
       builder for multi-file inputs.  Each element is an integer byte
       count (e.g. ``[5242880]`` for five × 1 MB files).

    3. **``file_sizes`` dict** – values mapped from identifiers to per-file
       byte counts (e.g. ``{"a.jpg": 5242880, "b.jpg": 5242880}``).

    4. **Inline string byte-sum** – sums ``utf-8`` byte-lengths of all
       string values in node_data (for inline text payloads), excluding
       ``input_size_bytes`` / ``file_sizes`` keys.

    Returns ``None`` when no signal is available.

    **Scaling assumption.**  The size tier is decided on the **total bytes
    across all files of a fan-in / multi-file input** (path 2 or 3 above).
    For fan-out runs, each per-file dispatch carries only that file's own
    size (the graph builder sets ``file_sizes`` to a one-element list for
    the single file being processed).
    """
    # ── 1. explicit input_size_bytes (pre-computed total) ────────────────
    explicit = node_data.get("input_size_bytes")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass

    # ── 2. file_sizes list or dict (per-file sizes from graph builder) ───
    file_sizes = node_data.get("file_sizes")
    if isinstance(file_sizes, (list, tuple)) and file_sizes:
        total = 0
        for item in file_sizes:
            # bool is subclass of int in Python — skip explicitly.
            if isinstance(item, bool):
                continue
            try:
                v = int(item)
            except (TypeError, ValueError):
                continue
            if v < 0:
                continue
            total += v
        if total > 0:
            return total
    elif isinstance(file_sizes, dict) and file_sizes:
        total = 0
        for val in file_sizes.values():
            # bool is subclass of int in Python — skip explicitly.
            if isinstance(val, bool):
                continue
            try:
                v = int(val)
            except (TypeError, ValueError):
                continue
            if v < 0:
                continue
            total += v
        if total > 0:
            return total

    # ── 4. fallback: inline string byte-sum ──────────────────────────────
    total = 0
    for k, v in node_data.items():
        if k in ("input_size_bytes", "file_sizes"):
            continue
        if isinstance(v, str):
            total += len(v.encode("utf-8"))
        elif isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, str):
                    total += len(item.encode("utf-8"))
    return total if total > 0 else None


_PROBE_TIER_RANK = {"small": 0, "medium": 1, "large": 2}


def _classify_size_probe(node_data: dict) -> Optional[tuple]:
    """Classify a node via a size-probe result, if one is present and usable.

    sz-tcp.16/17 — ``node_data["size_probe"]`` is the parsed JSON a
    size-probe container (``CORE/DockerContainers/size-probe/probe.py``)
    writes to stdout:
    ``{"probes": [{"family": "images", "size_unit_value": 12.0, ...}, ...]}``.
    Each entry's ``size_unit_value`` is already in that family's native unit
    per probe.py's own docstring (megapixels for images, duration_sec *
    pixel_class for video, seconds for audio, triangle count for 3d, bytes
    for documents) — it is compared directly against that family's
    ``{small_max, medium_max}`` pair from
    ``credit_tier_thresholds.get_credit_tier_thresholds()`` (canonical keys;
    per-family values are each in that family's own native unit, not
    necessarily bytes — only ``documents`` is bytes).

    An entry is skipped (not just its family defaulted) when it carries an
    ``"error"`` key (probe.py's per-file failure shape), an unrecognized
    family (anything outside images/video/audio/3d/documents — e.g. probe.py's
    "other" bucket), or a missing/non-numeric ``size_unit_value``. For a
    multi-file input, the file whose OWN family thresholds put it in the
    worst (largest) tier wins — one oversized file in a batch is enough to
    price the whole dispatch at that tier.

    Returns ``(size_tier, "probe")``, or ``None`` when ``size_probe`` is
    absent, malformed, or has no usable entries — callers must fall back to
    the byte-counting path in that case.
    """
    probe = node_data.get("size_probe")
    if not isinstance(probe, dict):
        return None
    entries = probe.get("probes")
    if not isinstance(entries, list) or not entries:
        return None

    # CE lazy import (1634o): credit_tier_thresholds is Paid-only (stripped
    # from the CE carve). Absent on CE -> treat exactly like the existing
    # "size_probe absent/malformed" case just above and fall through to the
    # byte-counting path in _determine_size_tier (never dereference a None
    # sentinel: FAMILIES/get_credit_tier_thresholds are used unconditionally
    # right below, so this function must bail out first).
    try:
        from services.credit_tier_thresholds import FAMILIES, get_credit_tier_thresholds
    except (ImportError, ModuleNotFoundError):
        FAMILIES = None
        get_credit_tier_thresholds = None

    if FAMILIES is None or get_credit_tier_thresholds is None:
        return None

    thresholds = get_credit_tier_thresholds(db=None)
    best_tier = None
    for entry in entries:
        if not isinstance(entry, dict) or "error" in entry:
            continue
        family = entry.get("family")
        if family not in FAMILIES:
            continue
        unit_value = entry.get("size_unit_value")
        if isinstance(unit_value, bool) or not isinstance(unit_value, (int, float)):
            continue
        pair = thresholds.get(family) or {}
        small_max = pair.get("small_max")
        medium_max = pair.get("medium_max")
        if small_max is None or medium_max is None:
            continue
        if unit_value <= small_max:
            tier = "small"
        elif unit_value <= medium_max:
            tier = "medium"
        else:
            tier = "large"
        if best_tier is None or _PROBE_TIER_RANK[tier] > _PROBE_TIER_RANK[best_tier]:
            best_tier = tier

    if best_tier is None:
        return None
    return best_tier, "probe"


def _determine_size_tier(node_data: dict) -> tuple:
    """Return (size_tier, size_tier_source).

    * When ``node_data["size_probe"]`` carries a usable size-probe result
      (see ``_classify_size_probe``), classify by the probed family's
      small/medium thresholds — source ``"probe"``.
    * Otherwise, small/medium/large based on total input bytes against the
      thresholds from ``cost_estimator.get_tier_thresholds()`` (unchanged
      byte-counting behaviour — source ``"measured"``/``"default"``).
    * When byte count is unknown, returns ("medium", "default").
    * Malformed/unknown-family probe data never raises — it silently falls
      through to the byte-counting path.
    """
    probe_result = _classify_size_probe(node_data)
    if probe_result is not None:
        return probe_result

    # CE lazy import (1634o): cost_estimator is Paid-only (stripped from the
    # CE carve). Absent on CE -> empty thresholds dict, which falls through
    # to the exact same hard-coded defaults (2MB/30MB) the .get() calls below
    # already use for a missing key — unconditional dereference right after
    # import, so get_tier_thresholds() must never be called on a None sentinel.
    try:
        from services.cost_estimator import get_tier_thresholds
    except (ImportError, ModuleNotFoundError):
        get_tier_thresholds = None

    thresholds = get_tier_thresholds() if get_tier_thresholds is not None else {}
    small_max = thresholds.get("small_max_bytes", 2 * 1024 * 1024)
    medium_max = thresholds.get("medium_max_bytes", 30 * 1024 * 1024)

    byte_size = _compute_input_bytes(node_data)
    if byte_size is None:
        return "medium", "default"

    if byte_size <= small_max:
        return "small", "measured"
    if byte_size <= medium_max:
        return "medium", "measured"
    return "large", "measured"


def _declared_var_names(node_data: dict) -> set:
    """The variable names a node accepts for run-time override.

    A node "declares" a variable if it names it either in its
    ``configuration_schema.extra_variables`` (the knobs the node definition
    exposes) or in its already-configured ``extraVariables``/``extra_variables``
    runtime bag (a default the user set via the builder). Used to match
    run-level ``extra_variables`` to nodes BY VARIABLE NAME — a run-level var is
    only applied to a node that declares it, so one Text Input's ``text_content``
    is never sprayed into an unrelated image node's env.
    """
    names = set()
    if not isinstance(node_data, dict):
        return names
    for bag_key in ("extraVariables", "extra_variables"):
        bag = node_data.get(bag_key)
        if isinstance(bag, dict):
            names.update(bag.keys())
    cfg = node_data.get("configuration_schema")
    if isinstance(cfg, dict):
        ev = cfg.get("extra_variables")
        if isinstance(ev, dict):  # skip the "NONE" string sentinel
            names.update(ev.keys())
    return names


def _apply_run_extra_variables(node_data: dict, run_extra: dict, per_node_inputs: dict,
                               node_id: Optional[str] = None) -> dict:
    """Merge run-level ``extra_variables`` / per-node ``inputs`` into a node's
    configured ``extraVariables`` for a single dispatch (root or downstream).

    Precedence (highest wins) for a colliding variable name:
      1. ``per_node_inputs[<node_id or label>][var]`` — explicit per-node override
      2. ``run_extra[var]``                           — global run-level, matched by name
      3. the node's own ``data.extraVariables`` (configured default)

    Run-level (level 2) vars are applied only to a node that DECLARES that
    variable name (see ``_declared_var_names``) — that is the "matched to nodes
    by variable name" contract from the request schema. Per-node (level 1) vars
    are applied unconditionally: the caller named this exact node, so it is an
    explicit intent, not a name match.

    Returns a NEW dict (``node_data`` is never mutated). Only the
    ``extraVariables`` (camelCase, the runtime bag the worker's extra-vars→env
    step reads) and ``extra_variables`` (snake_case, read by the output-cache
    key) keys are touched; every other node field is preserved. Values are never
    logged here.
    """
    if not isinstance(node_data, dict):
        return node_data
    run_extra = run_extra if isinstance(run_extra, dict) else {}
    per_node_inputs = per_node_inputs if isinstance(per_node_inputs, dict) else {}

    label = node_data.get("label") or node_data.get("name")

    per_node: dict = {}
    for key, val in per_node_inputs.items():
        if not isinstance(val, dict):
            continue
        if key == node_id or (label and key == label):
            per_node = val
            break

    declared = _declared_var_names(node_data)
    effective = dict(node_data.get("extraVariables") or node_data.get("extra_variables") or {})
    # Global run-level values: matched to THIS node by variable name only.
    for k, v in run_extra.items():
        if v is None:
            continue
        if k in declared:
            effective[k] = v
    # Explicit per-node values win over the global run-level ones (unconditional).
    for k, v in per_node.items():
        if v is not None:
            effective[k] = v

    merged = dict(node_data)
    merged["extraVariables"] = effective
    # Keep the snake_case mirror in sync so the output-cache key (which reads
    # node_data.extra_variables / extraVariables) reflects the effective values.
    merged["extra_variables"] = dict(effective)
    return merged


def _run_extra_values(project_info: dict) -> tuple:
    """Pull (run_extra, per_node_inputs) out of a run's ``project_info``.

    The run router stores ``project_info.extra_variables`` (global, run-level)
    and ``project_info.inputs`` (per-node) — see routers/workflows_domains/
    workflows.py. Trigger-fired runs (services/workflow_orchestrator.py
    ``_fire_trigger_run``) carry ``extra_variables`` too (the forwarded trigger
    inputs); they have no per-node ``inputs``.
    """
    pi = project_info if isinstance(project_info, dict) else {}
    run_extra = pi.get("extra_variables") or {}
    if not isinstance(run_extra, dict):
        run_extra = {}
    per_node_inputs = pi.get("inputs") or {}
    if not isinstance(per_node_inputs, dict):
        per_node_inputs = {}
    return run_extra, per_node_inputs


def park_for_human_approval(
    db: "Session",
    *,
    workflow_run_id: int,
    node_run_id: "Optional[int]",
    node_id: str,
    node_kind: str,
    node_data: dict,
) -> bool:
    """Gate 5. If this node waits on a person, park it and return True.

    A True return means the caller must NOT enqueue a task. Both dispatch
    paths call this — root dispatch here and `_check_and_dispatch_downstream`
    in worker_tasks — because an approval gate is usually mid-graph but is
    legal as a root.

    NOTHING IS DISPATCHED TO A WORKER. A container that blocks until a human
    answers would hold a worker slot for the hours or days that takes; the
    wait is a database row, not a running process.

    Reuses `manual_intervention_states`, which was already live with the right
    shape (unique on node_run_id, indexed on status and resume_hook_id) and had
    no writer. The run resumes through the ordinary DAG advance — see
    `routers/interventions.py` — so there is no second orchestration path.
    """
    if node_kind != HUMAN_APPROVAL_NODE_KIND:
        return False

    from models import (ManualInterventionState, InterventionStatus, NodeRun,
                        Notification, TaskStatus, WorkflowRun)

    run = db.get(WorkflowRun, workflow_run_id)

    # The workflow author names the approver on the node. Falling back to
    # whoever triggered the run keeps a misconfigured node from stranding the
    # graph with the request assigned to nobody.
    assignee_id = node_data.get("assigned_to_user_id") or node_data.get("assignedToUserId")
    try:
        assignee_id = int(assignee_id) if assignee_id else None
    except (TypeError, ValueError):
        assignee_id = None
    if not assignee_id and run is not None:
        assignee_id = run.triggered_by_user_id

    instructions = (node_data.get("instructions")
                    or node_data.get("label")
                    or f"Approval required for node '{node_id}'")

    if node_run_id is not None:
        existing = db.query(ManualInterventionState).filter(
            ManualInterventionState.node_run_id == node_run_id,
        ).first()
        if existing is not None:
            # Re-dispatch of an already-parked node. The unique index would
            # reject a second row anyway; returning True keeps the caller from
            # enqueuing a task the first pass deliberately did not.
            return True

    db.add(ManualInterventionState(
        node_run_id=node_run_id,
        assigned_to_user_id=assignee_id,
        status=InterventionStatus.PENDING_ACTION,
        instructions=instructions,
        resume_hook_id=f"wf:{workflow_run_id}:node:{node_id}",
    ))

    if node_run_id is not None:
        node_run = db.get(NodeRun, node_run_id)
        if node_run is not None:
            node_run.status = TaskStatus.PENDING_ACTION

    if assignee_id:
        db.add(Notification(
            recipient_user_id=assignee_id,
            message=f"Approval needed\n\n{instructions}\n\nWorkflow run #{workflow_run_id}",
            is_read=False,
        ))

    # Before the commit, deliberately: _emit_run_event does not commit, and a
    # timeline entry written after one is discarded. That exact mistake cost a
    # Gate 3 evening (a6dd71f4).
    _emit_run_event(
        db, workflow_run_id, "orchestrator", "info",
        f"Waiting for human approval on '{node_id}'",
        {"node_id": node_id, "assigned_to_user_id": assignee_id},
    )
    db.commit()

    if assignee_id:
        try:
            from routers.notifications import publish_notification_event
            publish_notification_event(assignee_id)
        except Exception:  # noqa: BLE001
            logger.debug("approval notification publish failed", exc_info=True)

    logger.info(
        "Workflow %d: node %s parked for human approval (assignee=%s)",
        workflow_run_id, node_id, assignee_id,
    )
    return True


def _agent_node_session() -> "Session":
    """The background thread's OWN session (separate from the request's).

    Split out so tests can hand the thread the fixture session instead of a
    real SessionLocal — the same reason services/adversary_dispatch.py:272
    imports SessionLocal at its thread boundary.
    """
    from database import SessionLocal
    return SessionLocal()


def _agent_node_advance(bg: "Session", *, workflow_run_id: int,
                        node_run_id: "Optional[int]", node_id: str,
                        tenant_id: Optional[int], completed: bool,
                        outputs: Optional[dict],
                        error_message: Optional[str],
                        dp_spec: Optional[dict] = None) -> None:
    """ONE orchestration path for an API-side agent node's terminal state.

    Writes the terminal NodeRun (mirroring routers/worker_tasks.py
    report_task_complete/report_task_failed) plus the synthetic ApiTaskQueue
    row, then advances the DAG through routers/worker_tasks._check_and_dispatch_downstream
    — the same helper a worker completion calls, fed a synthetic row, exactly
    the re-entry services/sub_workflow_runtime.py:416 uses for a completed
    child run. Both `_bg_agent_node` branches call this: the normal-result
    path AND the outer except path (run_agent_node raised), so a raised
    runtime still advances the DAG and the WorkflowRun can complete.

    Idempotent: a node already in a terminal state is left alone and nothing
    is advanced twice. The advance itself is best-effort — if it raises we log
    the exception class name and return; the node is already stamped terminal
    on disk, so the thread never dies silently.
    """
    from models import (NodeRun, TaskStatus, ApiTaskQueue, ApiTaskStatus)
    from routers.worker_tasks import _check_and_dispatch_downstream
    from datetime import datetime, timezone

    nr = bg.get(NodeRun, node_run_id) if node_run_id is not None else None
    if nr is None:
        return
    if nr.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                     TaskStatus.SUCCESS, TaskStatus.FAILURE):
        return

    if nr.status == TaskStatus.PENDING_ACTION:
        # EXPR 4.4 slice C — a PARKED node is not terminal. Slice A's tool
        # dispatch flipped it PENDING_ACTION and stamped agent_state (+ CHILD_MARKER)
        # while the tool CHILD run works; the resume hook owns the re-entry, so
        # an advance call that reaches this node must write nothing. A PENDING_ACTION
        # node WITHOUT agent_state is a plain sub_workflow / human-approval park,
        # which the pre-existing terminal-write path already owns — only the
        # agent-state shape abstains here.
        _outs = nr.outputs if isinstance(nr.outputs, dict) else {}
        if "agent_state" in _outs:
            return

    # FORTRESS.6.2 (gap) — an API-side agent node never goes through
    # routers/worker_tasks.report_task_complete (the worker path that applies
    # calibrated DP noise at :3199-3236), so the node's `dp` modifier is applied
    # HERE, before ANY terminal write: both the NodeRun and the synthetic
    # ApiTaskQueue row must carry the NOISED value, never the raw one
    # (spec:4 "add calibrated noise to the node's numerical output" + spec:10
    # "the downstream node SHALL receive the noised value"). Fail-closed mirrors
    # the worker posture at worker_tasks.py:3206-3238: a broken spec fails the
    # node — raw output is never passed through.
    # FORTRESS.6.2 (910b) — reserve DP epsilon budget for an API-side agent node.
    # The worker path reserves at dispatch inside routers/worker_tasks.py
    # enqueue_task_internal:7920-7955; an agent node never goes through that
    # function (start_agent_node runs the loop API-side), so without this block
    # the tenant's privacy-budget consumption is UNDERCOUNTED for agent nodes
    # carrying a `dp` spec. Same lookup the noise block below uses (`dp_spec`,
    # flattened from node_data["dp"] by the call sites), the same settings gate
    # worker_tasks.py reads (`dp.budget_enabled`, db=bg), the same workflow_id
    # resolution `_fortress63_run_workflow_id` does at worker_tasks.py:7817-7824
    # (the run row already in hand), and the same reserve() argument shape.
    # reserve() flushes only — the ledger row lands in the SAME transaction as
    # the synthetic ApiTaskQueue insert's bg.commit() below, so a refusal leaves
    # no ledger row and no terminal write.
    # Idempotency: reserve() has no idempotency key, so the node-run IS the key
    # — this block runs only while the NodeRun is still non-terminal (the early
    # return above abstains for COMPLETED/FAILED and for an agent-state park),
    # and the explicit ledger probe below makes a crash-then-re-advance (reserve
    # flushed but the terminal write lost) reserve at most once per
    # (tenant, workflow, run, node) instead of double-counting.
    if completed and dp_spec is not None and tenant_id is not None:
        from models import (WorkflowRun as _WrRun, DpBudgetLedger as _Ledger)
        _wf_id = None
        _wr = bg.query(_WrRun.workflow_id).filter(
            _WrRun.id == workflow_run_id
        ).first()
        if _wr is not None:
            _wf_id = _wr[0]
        _already_reserved = bg.query(
            _Ledger.id
        ).filter(
            _Ledger.tenant_id == tenant_id,
            _Ledger.workflow_id == _wf_id,
            _Ledger.run_id == workflow_run_id,
            _Ledger.node_id == node_id,
        ).first() if _wf_id is not None else None
        if _wf_id is not None and _already_reserved is None:
            from services import platform_settings as _dp_psr
            if _dp_psr.get_setting("dp.budget_enabled", db=bg):
                # CE lazy import (1634o): dp_budget is Paid-only (stripped
                # from the CE carve). dp.budget_enabled is itself a paid
                # setting a CE install should never have set, but if it
                # somehow is, treat the module's absence as "budget
                # enforcement unavailable" and skip reservation rather than
                # dereferencing a None sentinel (names used unconditionally
                # right after import below) — the node still runs normally,
                # same as any other disabled-feature path in this file.
                try:
                    from services.dp_budget import (
                        DpBudgetExhausted as _DpBudgetExhausted,
                        reserve as _dp_reserve,
                        validate_dp as _dp_validate,
                    )
                except (ImportError, ModuleNotFoundError):
                    _DpBudgetExhausted = None
                    _dp_reserve = None
                    _dp_validate = None

                if _dp_reserve is not None and _dp_validate is not None:
                    try:
                        _dp_epsilon, _dp_delta = _dp_validate(dp_spec)
                        _dp_reserve(
                            bg,
                            tenant_id=tenant_id,
                            workflow_id=_wf_id,
                            run_id=workflow_run_id,
                            node_id=node_id,
                            epsilon=_dp_epsilon,
                            delta=_dp_delta,
                        )
                    except _DpBudgetExhausted:
                        # Fail closed on the SAME path the dp_noise_failed branch
                        # below uses: no outputs, no downstream advance. The node
                        # never spends beyond its cap.
                        completed = False
                        outputs = None
                        error_message = "dp_budget_exhausted"
                    except ValueError:
                        # Malformed dp spec (923's dispatch sibling raises a fixed
                        # 422 code): fixed text only, never the exception text.
                        completed = False
                        outputs = None
                        error_message = "dp_spec_invalid"

    dp_noise_applied = None
    from utils.paid_import import optional_paid as _optional_paid
    _dpn = _optional_paid("services.dp_noise") if (completed and dp_spec is not None) else None
    if completed and dp_spec is not None and _dpn is None:
        # FIX-CE-DP-NOISE-FAIL-CLOSED: a dp spec with the module absent (CE) FAILS
        # the node on the SAME path the dp-noise-failure branch below uses — no
        # outputs, no dp_noise_applied stamp, the output is never released. Silent
        # skip would hand raw, un-noised data downstream (FORTRESS.6.2's contract).
        completed = False
        outputs = None
        error_message = DP_NOISE_UNAVAILABLE
    elif completed and dp_spec is not None:
        try:
            _dp_noised, _dp_applied = _dpn.apply_dp_noise(outputs, dp_spec, db=bg)
            outputs = _dp_noised
            dp_noise_applied = bool(_dp_applied)
        except Exception as _dp_exc:  # noqa: BLE001 — handled here, fails closed
            logger.warning(
                "agent node %s (run %s): dp noise failed: %s",
                node_id, workflow_run_id, type(_dp_exc).__name__,
            )
            completed = False
            outputs = None
            error_message = f"dp_noise_failed: {type(_dp_exc).__name__}"

    if completed:
        nr.status = TaskStatus.COMPLETED
        nr.outputs = outputs or {}
    else:
        nr.status = TaskStatus.FAILED
        nr.error_message = str(error_message or "agent node failed")
    nr.completed_at = datetime.now(timezone.utc)

    _synth_task_payload = {"node_kind": AGENT_NODE_KIND, "node_id": node_id}
    if dp_spec is not None:
        # traceability — mirrors worker_tasks.py:7769 flattening `dp` onto
        # ApiTaskQueue.task_payload (cache isolation keys off the same field).
        _synth_task_payload["dp"] = dp_spec
    _synth_result_payload = None
    if completed:
        _synth_result_payload = {"outputs": outputs}
        if dp_noise_applied is not None:
            _synth_result_payload["dp_noise_applied"] = dp_noise_applied

    synth = ApiTaskQueue(
        workflow_run_id=workflow_run_id,
        node_run_id=node_run_id,
        node_id=node_id,
        tenant_id=tenant_id,
        queue_name="CPU",
        status=(ApiTaskStatus.COMPLETED if completed else ApiTaskStatus.FAILED),
        task_payload=_synth_task_payload,
        result_payload=_synth_result_payload,
        error_message=(None if completed
                       else str(error_message or "agent node failed")),
    )
    bg.add(synth)
    bg.commit()
    bg.refresh(synth)
    try:
        _check_and_dispatch_downstream(bg, synth)
    except Exception as exc:  # noqa: BLE001 — the advance is best-effort
        logger.warning(
            "agent node %s (run %s): downstream advance failed: %s",
            node_id, workflow_run_id, type(exc).__name__,
        )


def _start_agent_node_thread(target, name: str) -> None:
    """Start the API-side agent-node loop on a daemon thread.

    Split out of `start_agent_node` so tests can run the body synchronously
    (a real thread racing the fixture's transaction proves nothing).
    """
    import threading
    threading.Thread(target=target, name=name, daemon=True).start()


def start_agent_node(
    db: "Session",
    *,
    tenant_id: Optional[int],
    workflow_run_id: int,
    node_run_id: "Optional[int]",
    node_id: str,
    node_kind: str,
    node_data: dict,
    graph_json: Dict[str, Any],
    agent_input: Any = None,
) -> bool:
    """FX 4.1 wiring. If this node is an `agent` node, run it API-side and return True.

    A True return means the caller must NOT enqueue a worker task — an agent
    node has no container to run: services/agent_node_runtime.run_agent_node
    drives the LLM+tool loop in the API process (its module docstring names
    this call site).

    NOTHING IS ENQUEUED TO A WORKER, and the loop never runs on the request
    thread: it can take seconds to minutes, so it runs on a daemon
    ``threading.Thread`` with its OWN ``SessionLocal`` — the same shape
    services/adversary_dispatch.py:272 uses for a background workflow
    dispatch (commit the rows first, then start the thread, so the thread
    sees them on disk).

    The completion write mirrors the worker-reported path
    (routers/worker_tasks.py report_task_complete:3143 sets
    ``node_run.status = TaskStatus.COMPLETED`` + ``completed_at`` +
    ``outputs``; report_task_failed:3728 sets FAILED + ``error_message``),
    and the DAG then advances through the SAME helper a worker completion
    calls — routers/worker_tasks._check_and_dispatch_downstream — fed a
    synthetic COMPLETED/FAILED ApiTaskQueue row, exactly the re-entry
    services/sub_workflow_runtime.py:416 uses for a completed child run.
    There is no second orchestration path.

    Fail-closed on the existing phase-3 flag (expr.phase3_agent_nodes_enabled):
    off -> return False, the node goes down the normal worker path unchanged.
    """
    if node_kind != AGENT_NODE_KIND:
        return False

    from models import (NodeRun, TaskStatus, WorkflowNode, WorkflowRun)
    # CE lazy import (1634o): expr_feature_flags is Paid-only (stripped from
    # the CE carve). Absent on CE -> treat the phase-3 agent-node flag as
    # permanently off, the SAME fail-closed outcome the docstring above
    # already documents for the flag being off — never call a None sentinel.
    try:
        from services.expr_feature_flags import phase3_agent_nodes_enabled
    except (ImportError, ModuleNotFoundError):
        phase3_agent_nodes_enabled = None
    from services.agent_node_runtime import run_agent_node

    if phase3_agent_nodes_enabled is None or not phase3_agent_nodes_enabled(db, tenant_id):
        return False

    run = db.get(WorkflowRun, workflow_run_id)
    if run is None:
        return False

    node_run = db.get(NodeRun, node_run_id) if node_run_id is not None else None
    if node_run is None:
        # Same lookup the root dispatch (line ~1685) and the orphan guard
        # (worker_tasks:12105) use.
        wn = db.query(WorkflowNode).filter(
            WorkflowNode.workflow_id == run.workflow_id,
            WorkflowNode.graph_node_id == node_id,
        ).first()
        if wn is not None:
            node_run = db.query(NodeRun).filter(
                NodeRun.workflow_run_id == workflow_run_id,
                NodeRun.workflow_node_id == wn.id,
            ).first()
    if node_run is None:
        return False

    if node_run.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                           TaskStatus.SUCCESS, TaskStatus.FAILURE):
        # Idempotent: already terminal, so a re-dispatch must not run the loop
        # twice.
        return True

    node_run.status = TaskStatus.RUNNING
    node_run.started_at = datetime.now(timezone.utc)

    # Capture every scalar the bg closure needs from `run` BEFORE commit: a
    # request-scoped session expires its ORM objects on commit by default, so
    # any attribute access from the closure (a different thread) after that
    # point would lazy-load through `db` and race the request thread's
    # `db.close()` in start_workflow_execution's finally (IllegalStateChangeError).
    run_purpose = getattr(run, "purpose", None)

    # Commit BEFORE the thread starts: the thread runs with its OWN session and
    # must see the RUNNING row (and the run's NodeRuns) already on disk.
    db.commit()

    def _bg_agent_node():
        """Background body: own session, never raises, writes the terminal state."""
        bg = _agent_node_session()
        try:
            result = run_agent_node(
                bg,
                tenant_id=tenant_id,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
                node_config=node_data,
                agent_input=agent_input,
                graph_json=graph_json,
                node_run_id=node_run_id,
                purpose=run_purpose,
            )
            if not isinstance(result, dict):
                result = {"status": "failed",
                          "failure_reason": "agent runtime returned no envelope"}
            status = result.get("status")
            if status == "parked":
                # EXPR 4.4 slice C — the node handed itself off to a tool CHILD
                # run (slice A stamped PENDING_ACTION + agent_state + CHILD_MARKER
                # on THIS session). Nothing is terminal: no synthetic ApiTaskQueue
                # row, no DAG advance, no completed_at. Commit so the park stamp
                # persists on disk before the thread returns, then wait —
                # services/agent_tool_resume.resume_agent_for_child re-enters the
                # loop when the child run reaches a terminal status.
                try:
                    bg.commit()
                except Exception as exc:  # noqa: BLE001 — park is best-effort
                    logger.warning(
                        "agent node %s (run %s) parked but commit failed: %s",
                        node_id, workflow_run_id, type(exc).__name__,
                    )
                return
            completed = status == "completed"
            _agent_node_advance(
                bg,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                node_id=node_id,
                tenant_id=tenant_id,
                completed=completed,
                outputs=result.get("outputs"),
                error_message=(None if completed
                               else result.get("failure_reason") or status),
                dp_spec=(node_data or {}).get("dp"),
            )
        except Exception as exc:  # noqa: BLE001 — nothing escapes the thread
            logger.warning(
                "agent node %s (run %s) bg failed: %s",
                node_id, workflow_run_id, type(exc).__name__,
            )
            try:
                # No rollback: run_agent_node never commits, so the only
                # pending writes in this session are the ones we are about to
                # write. A raised runtime still lands the SAME failure-advance
                # path the FAILED envelope uses, so the run can complete.
                _agent_node_advance(
                    bg,
                    workflow_run_id=workflow_run_id,
                    node_run_id=node_run_id,
                    node_id=node_id,
                    tenant_id=tenant_id,
                    completed=False,
                    outputs=None,
                    error_message=("agent node background failure: "
                                   + f"{type(exc).__name__}: {str(exc)[:500]}"),
                    dp_spec=(node_data or {}).get("dp"),
                )
            except Exception as exc2:  # noqa: BLE001 — advance is best-effort
                logger.warning(
                    "agent node %s (run %s): failure advance failed: %s",
                    node_id, workflow_run_id, type(exc2).__name__,
                )

    _start_agent_node_thread(
        _bg_agent_node, f"agent-node-{workflow_run_id}-{node_id}")

    logger.info(
        "Workflow %d: agent node %s started API-side (bg thread), no worker task",
        workflow_run_id, node_id,
    )
    return True


def should_bind_docker_socket(db: "Session", node_data: dict) -> bool:
    """Authoritative gate for /var/run/docker.sock bind-mount on a task.

    A node receives the host docker socket iff its referenced
    NodeDefinition row is system-shared (tenant_id IS NULL) AND its
    DB-stored node_kind is in the system-build allowlist. The lookup is by
    nodeDefinitionId so user-authored node_data.node_kind is ignored —
    the orchestrator's _enrich_from_def uses setdefault, which would
    otherwise let a tenant-supplied node_kind="system_build_one_variant"
    leak through unchanged when the user pre-populated it.
    """
    if not isinstance(node_data, dict):
        return False
    nd_id = node_data.get("nodeDefinitionId")
    if not nd_id:
        return False
    from models import NodeDefinition
    nd = db.query(NodeDefinition).filter(NodeDefinition.id == nd_id).first()
    if nd is None or nd.tenant_id is not None:
        return False
    node_kind = (nd.node_kind or "")
    return node_kind.startswith("system_build_") or node_kind in SYSTEM_BUILD_TASK_DAG_IDS


# add-rag-vector-store: RAG nodes (Vector Upsert / Vector Query) that call the
# vector API. Identified DB-authoritatively (platform-owned NodeDefinition +
# seeded node_kind) so a user can't spoof node_data to get a token injected.
_VECTOR_NODE_KINDS = frozenset({"vector_upsert", "vector_query"})


def should_inject_vector_token(db: "Session", node_data: dict) -> bool:
    """True iff this node's referenced NodeDefinition is a platform-owned
    (tenant_id IS NULL) RAG vector node. Same DB-authoritative discipline as
    should_bind_docker_socket — the lookup is by nodeDefinitionId, so a
    user-authored node_data.node_kind can't earn a token."""
    if not isinstance(node_data, dict):
        return False
    nd_id = node_data.get("nodeDefinitionId")
    if not nd_id:
        return False
    from models import NodeDefinition
    nd = db.query(NodeDefinition).filter(NodeDefinition.id == nd_id).first()
    if nd is None or nd.tenant_id is not None:
        return False
    return (nd.node_kind or "") in _VECTOR_NODE_KINDS


# System maintenance nodes that call BACK into the platform API with the
# long-lived platform-service key, rather than a per-run scoped credential.
#
# The scoped tokens above are better and these nodes cannot use them: a
# build-task token is scoped to one (tenant_id, build_spec_id) and a vector
# token to one tenant, while a maintenance sweep's whole job is to act ACROSS
# tenants. There is nothing to scope it to.
#
# Keep this set small and keep it maintenance-only. Anything that touches
# user-supplied content should be getting a scoped credential instead — see the
# rationale in utils/build_task_token.
_PLATFORM_SERVICE_KEY_NODE_KINDS = frozenset({"system_harbor_scan_followup"})


def should_inject_platform_service_key(db: "Session", node_data: dict) -> bool:
    """True iff this node is a platform-owned maintenance node cleared to hold
    the platform-service key.

    Same DB-authoritative discipline as should_bind_docker_socket: resolved by
    nodeDefinitionId against a row with tenant_id IS NULL, so a user-authored
    node_data.node_kind cannot earn the platform's own credential. That matters
    more here than for the scoped tokens — this key is long-lived and
    cross-tenant, so spoofing it would be worth doing.
    """
    if not isinstance(node_data, dict):
        return False
    nd_id = node_data.get("nodeDefinitionId")
    if not nd_id:
        return False
    from models import NodeDefinition
    nd = db.query(NodeDefinition).filter(NodeDefinition.id == nd_id).first()
    if nd is None or nd.tenant_id is not None:
        return False
    return (nd.node_kind or "") in _PLATFORM_SERVICE_KEY_NODE_KINDS


# GSEC R-008 — container_env keys the worker's netpol.go / executor.go treat
# as PLATFORM markers: PLATFORM_TASK → main (platform) network + platform
# Harbor push creds + Vault AppRole bootstrap; API_KEY / NIM_API_KEY /
# MOD_TASK_TOKEN_URL → main network; MOD_STACK_NETWORK → stack API network;
# NEEDS_INTERNET → external egress network. Only the platform may set them.
# Mirrors routers/worker_tasks.TENANT_RESERVED_CONTAINER_ENV (FX.1), which
# already strips these from the PUBLIC tenant enqueue endpoint; the
# workflow-node path (tenant-authored graph_json container_env flowing
# through _start_workflow / _check_and_dispatch_downstream) was the
# remaining vector.
RESERVED_CONTAINER_ENV_KEYS = frozenset({
    "PLATFORM_TASK",
    "API_KEY",
    "MOD_TASK_TOKEN_URL",
    "NIM_API_KEY",
    "MOD_STACK_NETWORK",
    "NEEDS_INTERNET",
    # FX.91 p5 (owner 10-07 Q5=A): the internal-only posture stamp the enqueue
    # gate writes AFTER this scrub — a tenant-authored graph_json must not be
    # able to claim it (and it can only ever RESTRICT a container).
    "MOD_GOV_UNDECLARED_INTERNAL_ONLY",
    # B108: MOD_NEEDS_API_TOKEN promotes the container to the main platform
    # network; MOD_TASK_NONCE is the worker-vended nonce. Tenants must not
    # self-promote or forge platform tokens.
    "MOD_NEEDS_API_TOKEN",
    "MOD_TASK_NONCE",
})


# B114 (20.6.s4) — node_kinds whose shipped code calls ONLY the platform's own
# managed-LLM generator/reasoner endpoint (http://ollama:11434 / http://vllm-big:
# 8000 — the same fixed aliases routers/ai_config.py resolves, served on
# mod_modtex-network with no auth; see seed_task_extract_node.py). These are
# MOD-authored, tenant-shared node kinds with NO user-configurable BASE_URL, so
# granting the SCOPED worker-mediated-token marker (MOD_NEEDS_API_TOKEN) is safe
# regardless of whether the node lands at the root of a graph or downstream of
# other nodes — unlike the general is_root_node-gated promotion in
# routers/worker_tasks.enqueue_task_internal, which deliberately stays root-only
# for untrusted compute nodes.
#
# Root cause: task_extract's seed docstring assumed "every GENERAL processing
# node is already granted platform-network reachability", but that promotion is
# actually root-only (B108) — a downstream Task Extract node (e.g. extract-1 in
# the 20.6.s4 Plane e2e, after input-1/asr-1) landed on the isolated
# default-deny network and could never resolve `ollama` (DNS failure), even
# though node_definitions.egress_domains has no way to express a bare internal
# service name (FX.91 p2a's grammar is FQDN-only). This allowlist is the
# narrowest fix: an explicit per-node-kind grant, not a blanket network opening
# for downstream nodes in general.
LLM_GENERATOR_DIRECT_CLIENT_NODE_KINDS = frozenset({"task_extract"})

# 1587 (B114 part 2, owner decision owner 10-14 option A) — kind -> the bare
# repo:tag the platform seeds that kind's image under (seed_task_extract_node.py
# etc). Fed through utils.registries.platform_image() on every check (not
# cached) so an on-prem DOCKER_REGISTRY override is honored. Add future kinds
# here, never by trusting a tenant row's own node_kind string.
LLM_GENERATOR_DIRECT_CLIENT_TRUSTED_COPY_IMAGES = {
    "task_extract": "taskextractnode:latest",
}

# 2400LR-ceGP-F5 (BUG-1027d) — the SAME grant shape as B114, for the MOD-authored
# export node kinds whose shipped code calls the platform's own run-task-scoped
# node-callback endpoints (POST /v1/assets/service/workflows/{id}/create-output-
# folder, /v1/cogs/service/workflows/upload-processed-file, upload-text-file,
# create-preview, GET /v1/runs/service/workflow/{id}[/resources]) via
# CORE/DockerContainers/<dir>/mod_node_auth.py, which already prefers the
# worker-vended short-lived run_task token (MOD_TASK_TOKEN_URL) over the
# standing API_KEY. Those endpoints' auth dep is utils/run_task_auth.node_or_
# service_principal (accepts a run_task token, tenant+run pinned by
# pin_run_task_tenant) — no auth dependency was widened.
#
# Root cause (F3): an export node is DOWNSTREAM, so the root-only B108 general
# promotion never fires, and _scrub_reserved_container_env strips the router's
# API_KEY from a tenant-owned row (the starter package copies the palette into
# the tenant), leaving the container on the default-deny network -> exit 1.
#
# Kinds included (each container dir has the SAME mod_node_auth.py MOD_TASK_
# TOKEN_URL support and calls ONLY the run_task-accepted endpoint set above):
#   return_assets_export (ReturnExport), unity_export (unity-export),
#   unreal_export (unreal-export) — identical export.py/mod_node_auth.py trio.
# Deliberately EXCLUDED: platform_export (PlatformExport has NO mod_node_auth.
# py — it reads only the standing API_KEY, so a scoped marker would break it
# and it must keep the platform-key path reserved to platform-owned rows);
# rag_export / lora_export (their own mod_node_auth.py, but their only API
# call is POST /named-artifacts on the brand pipeline, seeded tenant-owned by
# the CE-denied seed_export_pipeline_nodes.py — out of scope for this fix).
EXPORT_CLIENT_NODE_KINDS = frozenset({
    "return_assets_export", "unity_export", "unreal_export",
})

# 2400LR-ceGP-F6 (BUG-1027r) — the platform's OWN export dispatch env.
# Source of these exact keys: routers/worker_tasks.py:8885-8893, where the
# router builds `_export_env` for the export node kinds and (for every kind
# except rag_export) REPLACES the node's container_env with it at :8911-8912.
# Those keys are platform-authored, NOT tenant-supplied, so on a trusted MOD
# copy they must count as "declared" in _trusted_copy_allowed_env_keys —
# otherwise _trusted_copy_env_is_safe reads them as undeclared tenant env and
# the ceGP-F5 grant never fires on a REAL dispatch (the run lands on the
# default-deny network). API_KEY is deliberately NOT in this set: it is
# reserved (RESERVED_CONTAINER_ENV_KEYS) and scrubbed from tenant copies
# before this check ever runs. None of these keys is a hijack vector
# (no *_URL/*_HOST/PATH/LD_*/PYTHON*), and the danger regex still filters
# this set below.
EXPORT_PLATFORM_DISPATCH_ENV_KEYS = frozenset({
    "WORKFLOW_RUN_ID", "TENANT_ID", "INPUT_COG_ID", "WORKFLOW_NAME",
    "ORIGINAL_FILENAME", "NODE_OUTPUTS",
})

# kind -> bare repo:tag the platform seeds that kind's image under
# (CORE/Metadata/system/{Return,Unity,Unreal}_EXPORT_Node.py `executable_path`
# — bare refs get qualified by utils.registries.platform_image() on every
# check, so an on-prem DOCKER_REGISTRY override is honored).
EXPORT_CLIENT_TRUSTED_COPY_IMAGES = {
    "return_assets_export": "return-export:latest",
    "unity_export": "unity-export:latest",
    "unreal_export": "unreal-export:latest",
}

# The only places this codebase lets a NodeDefinition or a per-node dispatch
# payload declare a container command/entrypoint override:
#   - NodeDefinition.custom_metadata, via node_builder/service_from_node.py's
#     _COMMAND_KEYS ("service_command", "command", "long_lived_command") —
#     the node-as-persistent-service de-specialization path.
#   - node_data (the saved graph node, read at dispatch) could in principle
#     carry the same keys, or "entrypoint"/"args" — the Go worker's
#     TaskPayload.Cmd is the only field that actually reaches `docker run`
#     (internal/transport/transport.go), and today NOTHING in the Python
#     dispatch path ever sets it from node_data for a normal workflow node
#     (it is wired only for platform system task types). Checked anyway,
#     belt-and-suspenders: this gate must stay correct even if a future
#     change wires one of these into dispatch.
_TRUSTED_COPY_COMMAND_OVERRIDE_KEYS = (
    "service_command", "command", "long_lived_command", "entrypoint", "args",
)


def _image_repository(image_ref: str) -> str:
    """Strip a `:tag` or `@sha256:digest` suffix off an image reference,
    leaving only `registry/project/name`. Exact-match comparison only — no
    prefix/substring matching, so `taskextractnode-evil` or a platform repo
    used as a path prefix never passes.

    A trailing `:tag` is only a tag when its `:` comes after the LAST `/` —
    otherwise it's a registry host:port (e.g. `localhost:5000/project/name`).
    """
    ref = (image_ref or "").strip()
    if not ref:
        return ""
    # Strip a digest suffix FIRST, then fall through to the tag-stripping
    # logic below on what's left — a combined `repo:tag@sha256:digest` ref
    # (worker/executor tooling can emit this form) must normalise to `repo`,
    # not `repo:tag`. An early `return` here was the 1587-escalation bug.
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        return ref[:last_colon]
    return ref


def _has_command_override(mapping: Optional[dict]) -> bool:
    if not isinstance(mapping, dict):
        return False
    return any(mapping.get(_k) for _k in _TRUSTED_COPY_COMMAND_OVERRIDE_KEYS)


# 1587 escalation (round 1) — a tenant-controlled env VALUE, not just a
# command/entrypoint override, is enough to hijack the real platform image
# (LD_PRELOAD a malicious .so, repoint PYTHONPATH/PYTHONHOME/PYTHONSTARTUP at
# tenant-supplied code the real interpreter imports on start, BASH_ENV into a
# shell-invoked entrypoint, NODE_OPTIONS into a node one).
#
# 1587 escalation (round 2) — widened for token-harvest / MITM vectors: PATH
# (swap in a malicious binary ahead of the real one), HOME (relocate config
# files an interpreter reads on start), *PROXY (HTTP(S)_PROXY/ALL_PROXY —
# routes the "unauthenticated" managed-LLM call through a tenant-controlled
# MITM), SSL_CERT_*/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE (trust a forged CA so a
# MITM's TLS cert validates), *_BASE_URL/*_HOST (repoint the call away from
# the real ollama/vllm-big alias entirely), and the Perl/Ruby/Java/glibc
# equivalents of PYTHONSTARTUP (PERL5OPT, RUBYOPT, JAVA_TOOL_OPTIONS,
# GCONV_PATH). Matched case-INSENSITIVE against a stripped key — these are
# refused UNCONDITIONALLY, never allow-listable by any configuration_schema,
# tenant-row or otherwise, regardless of kind.
_TRUSTED_COPY_DANGEROUS_ENV_KEY_RE = re.compile(
    r"^(LD_.*|PYTHON.*|BASH_ENV|NODE_OPTIONS|.*_PRELOAD"
    r"|PATH|HOME|.*PROXY|SSL_CERT.*|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE"
    r"|.*_BASE_URL|.*_HOST|PERL5OPT|RUBYOPT|JAVA_TOOL_OPTIONS|GCONV_PATH)$",
    re.IGNORECASE,
)


def _effective_tenant_env(node_data: dict, node_conf: Optional[dict]) -> dict:
    """Every tenant-reachable source of this dispatch's container_env,
    merged and KEY-UPPERCASED (mirrors routers/worker_tasks.py's
    `extraVariables` -> `container_env[key.upper()]` transform), so the
    trusted-copy pass sees exactly what a tenant could set. `node_conf`, at
    the one real call site (apply_node_task_injections), already carries
    the merged payload container_env (extraVariables included — the router
    merges those in BEFORE calling apply_node_task_injections) with
    RESERVED_CONTAINER_ENV_KEYS already scrubbed; `node_data.container_env`
    and `node_data.extraVariables` are read directly too, so a direct
    should_inject_llm_generator_direct_client_grant(db, node_data) call
    (tests, or any future caller without node_conf) still sees them."""
    merged: dict = {}
    for _source in (node_data, node_conf):
        if not isinstance(_source, dict):
            continue
        _cenv = _source.get("container_env")
        if isinstance(_cenv, dict):
            for _k, _v in _cenv.items():
                merged[str(_k).strip().upper()] = _v
    _extra = (node_data or {}).get("extraVariables")
    if isinstance(_extra, dict):
        for _k, _v in _extra.items():
            merged.setdefault(str(_k).strip().upper(), _v)
    return merged


# 1587 escalation (round 3) — the allowlist of "declared, harmless" extra_
# variables keys MUST be a CODE-SIDE constant per node_kind, resolved from the
# PLATFORM's OWN seed module, NEVER from nd.configuration_schema. That column
# lives on the TENANT's copy of the row and is PATCHable via
# NodeDefinitionUpdate (routers/node_definitions_domains/node_authoring.py,
# ~line 2919) — reading it back as the allowlist let a tenant simply declare
# OLLAMA_HOST/HTTP_PROXY/etc on their own row's configuration_schema and have
# it rubber-stamped as "declared". Lazily imported (not at module load) so
# there is no import-cycle risk either direction, and cached per node_kind
# since each seed module's dict is a static literal.
_TRUSTED_COPY_ALLOWED_ENV_KEYS_CACHE: Dict[str, frozenset] = {}


def _trusted_copy_allowed_env_keys(node_kind: str) -> frozenset:
    if node_kind in _TRUSTED_COPY_ALLOWED_ENV_KEYS_CACHE:
        return _TRUSTED_COPY_ALLOWED_ENV_KEYS_CACHE[node_kind]
    keys: frozenset = frozenset()
    if node_kind in EXPORT_CLIENT_NODE_KINDS:
        # 2400LR-ceGP-F6 (BUG-1027r) — the platform's OWN export dispatch env
        # (routers/worker_tasks.py:8885-8893 `_export_env`, which REPLACES the
        # node's container_env at :8911-8912 for every export kind except
        # rag_export). Those keys are platform-authored, not tenant-supplied,
        # so they must count as "declared" — otherwise the ceGP-F5 grant never
        # fires on a real dispatch. Code-side constant (never the tenant row's
        # patchable configuration_schema), still filtered through the danger
        # regex below like the task_extract branch.
        keys = EXPORT_PLATFORM_DISPATCH_ENV_KEYS
    elif node_kind == "task_extract":
        # 2400CR1zl — seed_task_extract_node.py is CE-denied, so this import used
        # to raise ImportError on CE. Absent module => no extra allowed env keys
        # (an empty allowlist is the safe default; never widen it).
        from utils.paid_import import optional_paid
        _seed_mod = optional_paid("seed_task_extract_node")
        if _seed_mod is None:
            logger.info(
                "task_extract trusted-copy allowlist: seed_task_extract_node absent "
                "in this edition (CE); no extra allowed env keys"
            )
        else:
            _extra = ((_seed_mod.TASK_EXTRACT_NODE.get("configuration_schema")
                       or {}).get("extra_variables") or {})
            keys = frozenset(
                _k for _k in (str(_name).strip().upper() for _name in _extra.keys())
                if not _TRUSTED_COPY_DANGEROUS_ENV_KEY_RE.match(_k)
            )
    _TRUSTED_COPY_ALLOWED_ENV_KEYS_CACHE[node_kind] = keys
    return keys


def _trusted_copy_env_is_safe(
    node_kind: str, node_data: dict, node_conf: Optional[dict],
) -> bool:
    """The effective tenant-supplied env for this dispatch must be EMPTY, or
    contain ONLY keys the platform's OWN seed module declares for this
    node_kind (_trusted_copy_allowed_env_keys — a code-side constant, never
    the tenant row's configuration_schema), with simple scalar values, and
    NEVER a key matching the danger class even if "declared" there
    (belt-and-suspenders — the seed-derived allowlist is already filtered,
    but this keeps the two checks independently correct). Task Extract's own
    seed declares ROLE, MODEL, SYSTEM_PROMPT, TEMPERATURE, MAX_TOKENS,
    MAX_TASKS, TIMEOUT — plain strings/numbers consumed as LLM-call
    parameters, not interpreter/process hijack or MITM vectors.
    """
    env = _effective_tenant_env(node_data, node_conf)
    if not env:
        return True
    declared = _trusted_copy_allowed_env_keys(node_kind)
    for key, value in env.items():
        if value is None or not str(value).strip():
            continue  # empty/blank — harmless, matches extraVariables' own skip
        if _TRUSTED_COPY_DANGEROUS_ENV_KEY_RE.match(key):
            return False
        if key not in declared:
            return False
        if not isinstance(value, (str, int, float, bool)):
            return False
    return True


def should_inject_llm_generator_direct_client_grant(
    db: "Session", node_data: dict, *, node_conf: Optional[dict] = None,
) -> bool:
    """True iff this node's referenced NodeDefinition earns the
    LLM-generator-direct-client network grant, via one of two
    DB-authoritative passes:

    1. Platform-owned: `tenant_id IS NULL` and the row's OWN node_kind is in
       LLM_GENERATOR_DIRECT_CLIENT_NODE_KINDS.
    2. Trusted MOD copy (1587, B114 part 2): the row is tenant-owned but is
       provably an unmodified copy of the platform's own seeded image —
       ALL of: (a) the row's own node_kind is in the allowlist; (b) the
       row's executable_path has the EXACT repository (registry+project+
       name, tag/digest ignored) of that kind's platform image; (c) neither
       the row's custom_metadata nor this dispatch's node_data carries a
       command/entrypoint override; (d) the EFFECTIVE tenant-supplied env
       for this dispatch (node_data/node_conf container_env + extraVariables
       — see _effective_tenant_env) is empty, or every non-blank value is
       under a key the PLATFORM's OWN seed module declares for this kind
       (_trusted_copy_allowed_env_keys — a CODE-SIDE constant; NEVER the
       row's own configuration_schema, which is tenant-PATCHable) AND
       outside the LD_*/PYTHON*/PATH/HOME/*PROXY/SSL_CERT_*/*_BASE_URL/
       *_HOST/... danger class (see _TRUSTED_COPY_DANGEROUS_ENV_KEY_RE)
       (1587 escalation round 2: an image-repo match alone is not enough —
       a tenant copy with no command override could still set LD_PRELOAD or
       PYTHONPATH via extraVariables and hijack the real MOD image's own
       entrypoint; round 3: the allowlist itself must not be read from the
       tenant's own, PATCHable configuration_schema, or a tenant could just
       declare OLLAMA_HOST/HTTP_PROXY there and have it rubber-stamped).
       `user_id IS NULL` is deliberately NOT a
       condition here — the normal node-authoring create path
       (services/node_authoring_service.py) sets user_id=None for every
       FRESH tenant node regardless of who created it (only forks/edits via
       node_code_edit.py set user_id=current_user.id), so a NULL user_id is
       not a "seeded by MOD" signal; the image-repo + no-override + no-
       dangerous-env checks carry the whole trust decision.

    `node_conf` is the in-flight dispatch payload (apply_node_task_injections'
    own parameter) — passed so this check sees container_env AFTER the
    router's extraVariables merge (routers/worker_tasks.py, which runs
    BEFORE apply_node_task_injections) and AFTER _scrub_reserved_container_env,
    but BEFORE any platform-injected key this same function call might add.
    Optional (defaults to None, i.e. node_data-only) so direct/test callers
    that only have node_data keep working — the platform-owned pass never
    needs it, and node_data alone already covers node_data.container_env /
    node_data.extraVariables.

    Same DB-authoritative discipline as should_bind_docker_socket /
    should_inject_vector_token: resolved by nodeDefinitionId, never by the
    caller-supplied node_kind string. A tenant can label their own node_data
    (or an arbitrary-image node's node_kind) "task_extract" — that string
    alone must NEVER earn the main-network + worker-token grant, or any
    tenant could bypass B108's root-only default-deny boundary with a
    relabeled downstream node, a look-alike image, an overridden command, or
    a hijacking env var. Everything that doesn't match BOTH passes exactly
    fails closed to False. db=None (no session available at the call site)
    fails closed to False, same as a missing/unresolvable row.
    """
    return _client_grant_allowed(
        db, node_data, node_conf=node_conf,
        node_kinds=LLM_GENERATOR_DIRECT_CLIENT_NODE_KINDS,
        trusted_copy_images=LLM_GENERATOR_DIRECT_CLIENT_TRUSTED_COPY_IMAGES,
    )


def should_inject_export_client_grant(
    db: "Session", node_data: dict, *, node_conf: Optional[dict] = None,
) -> bool:
    """2400LR-ceGP-F5 (BUG-1027d) — the SAME grant as B114, for the MOD-authored
    export node kinds (EXPORT_CLIENT_NODE_KINDS). Thin wrapper over the ONE
    shared DB-authoritative implementation below (_client_grant_allowed) — no
    duplicated trust logic (S5). See should_inject_llm_generator_direct_client_
    grant's docstring for the two passes and the escalation history; the
    export kinds' trusted-copy images come from EXPORT_CLIENT_TRUSTED_COPY_
    IMAGES and their tenant-supplied env allowlist is EMPTY (their platform
    seed, CORE/Metadata/system/{Return,Unity,Unreal}_EXPORT_Node.py, declares
    `configuration_schema = {"extra_variables": "NONE"}` — so any non-blank
    tenant env key on such a copy is refused by _trusted_copy_env_is_safe).
    """
    return _client_grant_allowed(
        db, node_data, node_conf=node_conf,
        node_kinds=EXPORT_CLIENT_NODE_KINDS,
        trusted_copy_images=EXPORT_CLIENT_TRUSTED_COPY_IMAGES,
    )


def _client_grant_allowed(
    db: "Session",
    node_data: dict,
    *,
    node_conf: Optional[dict] = None,
    node_kinds: frozenset,
    trusted_copy_images: dict,
) -> bool:
    """THE single DB-authoritative network-grant gate (B114 + ceGP-F5).

    Resolves the referenced NodeDefinition by nodeDefinitionId — NEVER by the
    caller-supplied node_kind string — and grants only via one of two passes:
    (1) platform-owned row (tenant_id IS NULL) whose OWN node_kind is in
    `node_kinds`; (2) trusted MOD copy — tenant-owned, exact platform image
    repository (registry+project+name, tag/digest ignored), no command/
    entrypoint override, and an effective tenant-supplied env that is empty
    or only keys the platform's own seed declares for that kind
    (_trusted_copy_allowed_env_keys, a CODE-side constant) and outside the
    interpreter-hijack / MITM danger class. Everything else fails closed.
    See should_inject_llm_generator_direct_client_grant for the full
    rationale + 1587 escalation rounds.
    """
    if db is None:
        return False
    if not isinstance(node_data, dict):
        return False
    nd_id = node_data.get("nodeDefinitionId")
    if not nd_id:
        return False
    from models import NodeDefinition
    nd = db.query(NodeDefinition).filter(NodeDefinition.id == nd_id).first()
    if nd is None:
        return False
    node_kind = nd.node_kind or ""
    if node_kind not in node_kinds:
        return False
    if nd.tenant_id is None:
        return True

    # Trusted MOD copy pass — tenant-owned rows only reach here.
    expected_bare = trusted_copy_images.get(node_kind)
    if not expected_bare:
        return False
    from utils.registries import platform_image
    expected_repo = _image_repository(platform_image(expected_bare))
    actual_repo = _image_repository(nd.executable_path or "")
    if not actual_repo:
        return False
    if actual_repo != expected_repo:
        # 2400LR-ceGP-F5R — CE publishes MOD's OWN images to the public CE
        # registry (`ghcr.io/modtim`, utils/registries.CE_SHIPPED_REGISTRY) and
        # the CE seed path writes those full refs straight into
        # node_definitions.executable_path, while platform_registry()/platform_
        # project() resolve to the install's Harbor mirror (CE_IMAGE_REGISTRY /
        # DOCKER_REGISTRY) — so on a CE install the trusted copy's row NEVER
        # matches the Harbor-qualified ref and the gate failed closed (the fix
        # was a no-op: live cr10 def 156 repo != platform_image() repo). Accept
        # the CE-shipped namespace for the SAME kind's image too. Still an
        # EXACT repository match (registry+project+name, tag/digest ignored, no
        # prefix/substring), and ghcr.io/modtim is MOD's own publish target —
        # the same namespace ce_mirror_ref() already treats as MOD's shipped
        # images — not a tenant-writable registry.
        from utils.registries import CE_SHIPPED_REGISTRY
        if actual_repo != _image_repository(
                f"{CE_SHIPPED_REGISTRY}/{expected_bare}"):
            return False
    if _has_command_override(nd.custom_metadata) or _has_command_override(node_data):
        return False
    if not _trusted_copy_env_is_safe(node_kind, node_data, node_conf):
        return False
    return True


def is_platform_owned_node(db: "Session", node_data: dict) -> bool:
    """DB-authoritative: the referenced NodeDefinition row exists and is
    platform-owned (tenant_id IS NULL). Same discipline as
    should_inject_platform_service_key — resolved by nodeDefinitionId, so a
    user-authored node_data.node_kind cannot claim platform status."""
    if not isinstance(node_data, dict):
        return False
    nd_id = node_data.get("nodeDefinitionId")
    if not nd_id:
        return False
    from models import NodeDefinition
    nd = db.query(NodeDefinition).filter(NodeDefinition.id == nd_id).first()
    return nd is not None and nd.tenant_id is None


def _scrub_reserved_container_env(
    db: "Session",
    node_data: dict,
    node_conf: dict,
    *,
    tenant_id: int,
    workflow_run_id: Optional[int],
    node_id: str,
) -> None:
    """GSEC R-008: strip platform-reserved container_env keys from
    tenant-controlled sources before the payload reaches the worker.

    Applied at the single dispatch choke point (apply_node_task_injections)
    so EVERY path — root dispatch, downstream dispatch, reruns, and any
    router that enqueues a task — is covered. The scrub runs BEFORE the
    gated injections below mint their own API_KEY (vector token,
    platform-service key), which stay DB-authoritatively gated, and BEFORE
    the extraVariables merge in enqueue_task_internal, so nothing
    tenant-controlled re-introduces a marker afterwards.

    Kept for platform-owned nodes only (is_platform_owned_node): system
    nodes whose env is baked into their NodeDefinition / graph_json
    (PLATFORM_TASK bootstrap tasks, NIM/NEEDS_INTERNET system nodes) must
    keep working. Each strip is logged + audited.
    """
    if is_platform_owned_node(db, node_data):
        return

    _node_kind = (node_data or {}).get("node_kind") or ""
    for _env in (
        node_conf.get("container_env"),
        (node_data.get("container_env")
         if isinstance(node_data, dict) else None),
    ):
        if not isinstance(_env, dict):
            continue
        for _key in RESERVED_CONTAINER_ENV_KEYS:
            if _key in _env:
                _env.pop(_key, None)
                logger.warning(
                    "GSEC R-008: stripped reserved container_env key %r from "
                    "tenant-authored node %s (kind=%s, tenant_id=%s, "
                    "workflow_run_id=%s) — platform markers are granted "
                    "only to platform-owned nodes",
                    _key, node_id, _node_kind, tenant_id, workflow_run_id,
                )
                if workflow_run_id is not None:
                    try:
                        _emit_run_event(
                            db, workflow_run_id, "gsec_r008", "warning",
                            f"reserved container_env key '{_key}' stripped "
                            f"from tenant node '{node_id}'",
                            {
                                "tenant_id": tenant_id,
                                "node_kind": _node_kind,
                            },
                        )
                    except Exception as _ev_exc:  # noqa: BLE001 — audit is best-effort
                        logger.debug(
                            "GSEC R-008 audit event failed (non-fatal): %s",
                            _ev_exc,
                        )


def apply_node_task_injections(
    db: "Session",
    node_data: dict,
    node_conf: dict,
    *,
    tenant_id: int,
    workflow_run_id: Optional[int],
    node_id: str,
    node_kind: str,
) -> None:
    """Per-node credential + stack-reachability injection, applied at the single
    choke point every dispatch path funnels through — `enqueue_task_internal`
    (root dispatch, downstream dispatch, and any other router that enqueues a
    task all call it).

    Historically the RAG vector token + platform-service key lived ONLY in
    `_start_workflow`, so ONLY root nodes got them. Every downstream node (RAG
    Builder, Image to Text, …) was enqueued with an empty container_env — no
    vector token (RAG failed downstream — verified live: task 6217), no VLM
    endpoint (Image to Text unreachable). Applying these here fixes both for
    every path.

    Scope is deliberately narrow and safe:
      * vector token / platform-service key — DB-authoritative gates
        (nodeDefinitionId + tenant_id IS NULL + a fixed node_kind set), so a
        user-authored node_data cannot spoof a credential.
      * VLM endpoint + host pin + main-network promotion — scoped to the VLM
        client node kinds only.
      * B114 — managed-LLM-generator DIRECT client main-network promotion
        (no stack/URL to resolve) — scoped to
        LLM_GENERATOR_DIRECT_CLIENT_NODE_KINDS only (currently task_extract).
    The GENERAL `MOD_NEEDS_API_TOKEN` main-network promotion is intentionally
    NOT applied here — it stays root-only in `_start_workflow` so untrusted
    downstream compute nodes are not broadly lifted off the isolated
    default-deny network.

    Mutates `node_conf` in place (container_env, and pin/socket/assign markers).
    """
    # GSEC R-008: strip platform-reserved container_env keys from
    # tenant-authored sources FIRST, so the gated injections below (which
    # mint their own DB-authoritatively-gated API_KEY) and the
    # extraVariables merge in enqueue_task_internal run against a clean
    # baseline and cannot re-introduce a marker on a tenant node.
    _scrub_reserved_container_env(
        db, node_data, node_conf,
        tenant_id=tenant_id,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )

    _cenv = dict(node_conf.get("container_env") or {})

    # add-rag-vector-store: a RAG node gets a short-lived, tenant-scoped vector
    # token injected as API_KEY so it can call the vector API (the worker sets
    # API_URL). Gated DB-authoritatively; best-effort — never block a run.
    if should_inject_vector_token(db, node_data):
        try:
            from utils.build_task_token import (
                is_configured,
                mint_vector_run_token,
            )
            if is_configured():
                _cenv["API_KEY"] = mint_vector_run_token(
                    tenant_id=tenant_id, workflow_run_id=workflow_run_id,
                )
        except Exception as _vt_exc:  # noqa: BLE001
            logger.warning(
                "vector token injection skipped for node %s: %s",
                node_id, _vt_exc,
            )

    # Platform maintenance nodes get the STATIC platform-service key as API_KEY.
    # NOT best-effort: dispatching these without the credential only reproduces
    # the hourly `{"errors": ["API_KEY is required"]}` failure.
    if should_inject_platform_service_key(db, node_data):
        _psk = os.getenv("PLATFORM_SERVICE_API_KEY", "").strip()
        if not _psk:
            raise RuntimeError(
                f"node {node_id} ({node_kind}) requires the platform-service "
                f"key but PLATFORM_SERVICE_API_KEY is not set on the API; "
                f"refusing to dispatch a task that can only fail authentication"
            )
        _cenv["API_KEY"] = _psk
        # Keep the static key (do not let the worker swap it for a scoped
        # run_task token — the platform-service-only endpoints 401 that).
        _cenv["MOD_STATIC_API_KEY"] = "1"
        # These nodes reach the API via the INTERNAL URL, which only resolves on
        # the manager; setdefault so an explicit value is never overridden.
        _cenv.setdefault("API_URL", os.getenv("API_BASE_URL", "http://api:80"))
        # Pin to the on-manager embedded worker (internal API URL resolves only
        # there); an ASSIGNED task is claimable only by its assigned worker.
        node_conf["assign_to_embedded_worker"] = True

    # VLM-consuming nodes (Image to Text / Brand VLM Critique) ATTACH to the
    # managed brand-vlm stack — they don't host a model. Resolve the running
    # stack, inject its reachable endpoint, and pin the task to the stack's host
    # so `host.docker.internal:<published port>` reaches the co-located VLM
    # wherever the reconciler placed it. Best-effort: a missing/unresolvable
    # stack leaves the node's baked VLM_URL default in force. Only fills VLM_URL
    # when the user did not set one, so an owner pointing the node at their own
    # endpoint is respected.
    if "VLM_URL" not in _cenv:
        try:
            from services.brand_image_stacks import (
                VLM_CLIENT_NODE_KINDS,
                StackHeld,
                resolve_vlm_client,
            )
            try:
                resolved = (resolve_vlm_client(db, tenant_id)
                            if (node_kind or "") in VLM_CLIENT_NODE_KINDS else None)
            except StackHeld as _vlm_held:
                # D7: the VLM stack won't fit right now — record what this node
                # is waiting on rather than falling back to a VLM_URL default
                # that would just fail to connect (GA.GSW.6).
                node_conf["awaiting_stack"] = _vlm_held.ref.stack_name
                resolved = None
            if resolved is not None:
                vlm_host, candidates = resolved
                if candidates:
                    _cenv["VLM_URL"] = ",".join(candidates)
                if vlm_host:
                    # Co-locate the consumer with its VLM stack (redeploy- and
                    # host-independent). Honoured in enqueue_task_internal only
                    # when the worker is live + LICENSED.
                    node_conf["pin_to_worker"] = vlm_host
                # A VLM node must land on the MAIN network so host.docker.internal
                # resolves to the (pinned) stack host's published port. This
                # marker makes the worker issue a per-task nonce + MOD_TASK_TOKEN_URL,
                # which netpol promotes to the main network + adds the host-gateway
                # extra host. SCOPED to the VLM node — it does not broadly promote
                # other downstream compute nodes off the isolated default-deny net.
                if "API_KEY" not in _cenv:
                    _cenv["MOD_NEEDS_API_TOKEN"] = "1"
        except Exception as _vlm_exc:  # noqa: BLE001
            logger.warning(
                "VLM endpoint injection skipped for node %s: %s",
                node_id, _vlm_exc,
            )

    # SDXL generate nodes (Brand SDXL Generate / SDXL-Turbo Generate) ATTACH to
    # their serving stack the same way VLM nodes attach to brand-vlm: they host no
    # model. Resolve the running stack FOR THIS node_kind, inject its reachable
    # host.docker.internal:<published port>/generate endpoint as SDXL_URL, and pin
    # the task to the stack's host. This is the "manager provides the stack API"
    # contract — the node's baked SDXL_URL default is only a last-resort fallback.
    # Fires only when SDXL_URL is empty/unset, so an owner who points the node at
    # their own endpoint is respected. Best-effort: an unresolvable stack leaves
    # the baked default in force.
    if not _cenv.get("SDXL_URL"):
        try:
            from services.brand_image_stacks import (
                SDXL_CLIENT_STACK_BY_KIND,
                StackHeld,
                resolve_sdxl_client,
            )
            try:
                resolved = (resolve_sdxl_client(db, tenant_id, node_kind)
                            if (node_kind or "") in SDXL_CLIENT_STACK_BY_KIND else None)
            except StackHeld as _sdxl_held:
                # D7: the SDXL stack won't fit right now — record what this
                # node is waiting on rather than falling back to a SDXL_URL
                # default that would just fail to connect (GA.GSW.6).
                node_conf["awaiting_stack"] = _sdxl_held.ref.stack_name
                resolved = None
            if resolved is not None:
                sdxl_host, candidates = resolved
                if candidates:
                    _cenv["SDXL_URL"] = ",".join(candidates)
                if sdxl_host:
                    # Co-locate the consumer with its SDXL stack so
                    # host.docker.internal reaches the co-located server's
                    # published port. Honoured in enqueue_task_internal only when
                    # the worker is live + LICENSED.
                    node_conf["pin_to_worker"] = sdxl_host
                # Land on the MAIN network + host-gateway so host.docker.internal
                # resolves (same mechanism the VLM node uses). Scoped to this node.
                if "API_KEY" not in _cenv:
                    _cenv["MOD_NEEDS_API_TOKEN"] = "1"
        except Exception as _sdxl_exc:  # noqa: BLE001
            logger.warning(
                "SDXL endpoint injection skipped for node %s: %s",
                node_id, _sdxl_exc,
            )

    # Brand Asset Generate nodes ATTACH to the serving stack NAMED in their
    # GENERATOR_STACK config (brand-sdxl / brand-sdxl-turbo for images, any
    # model-catalog video stack for clips) — the stack-name twin of the SDXL
    # block above, which is why ONE node covers both asset kinds and "swap the
    # model" is a config edit on the canvas. resolve_generator_client wakes the
    # named stack and returns its reachable /generate endpoint(s) + host; the
    # node's baked GENERATOR_URL default is only a last-resort fallback and fires
    # only when GENERATOR_URL is empty/unset, so an owner who points the node at
    # their own endpoint is respected. Best-effort: an unresolvable stack leaves
    # the baked default in force.
    if not _cenv.get("GENERATOR_URL"):
        try:
            from services.brand_image_stacks import (
                GENERATOR_NODE_KINDS,
                StackHeld,
                resolve_generator_client,
            )
            try:
                resolved = (resolve_generator_client(db, tenant_id, _cenv.get("GENERATOR_STACK"))
                            if (node_kind or "") in GENERATOR_NODE_KINDS else None)
            except StackHeld as _gen_held:
                # D7: the generator stack won't fit right now — record what this
                # node is waiting on rather than falling back to a GENERATOR_URL
                # default that would just fail to connect (GA.GSW.6).
                node_conf["awaiting_stack"] = _gen_held.ref.stack_name
                resolved = None
            except ValueError as _gen_cfg:
                # An empty/unknown GENERATOR_STACK is a node config error, not
                # "no stack available" — say so and leave the node unresolved.
                logger.warning(
                    "Brand generator endpoint injection skipped for node %s: %s",
                    node_id, _gen_cfg,
                )
                resolved = None
            if resolved is not None:
                gen_host, candidates = resolved
                if candidates:
                    _cenv["GENERATOR_URL"] = ",".join(candidates)
                if gen_host:
                    # Co-locate the consumer with its generator stack so
                    # host.docker.internal reaches the co-located server's
                    # published port. Honoured in enqueue_task_internal only when
                    # the worker is live + LICENSED.
                    node_conf["pin_to_worker"] = gen_host
                # Land on the MAIN network + host-gateway so host.docker.internal
                # resolves (same mechanism the VLM/SDXL nodes use). Scoped here.
                _cenv.setdefault("MOD_NEEDS_API_TOKEN", "1")
        except Exception as _gen_exc:  # noqa: BLE001
            logger.warning(
                "Brand generator endpoint injection skipped for node %s: %s",
                node_id, _gen_exc,
            )

    # B114 (20.6.s4) — managed-LLM-generator DIRECT client nodes (Task Extract):
    # see LLM_GENERATOR_DIRECT_CLIENT_NODE_KINDS / the gate function above for
    # why this scoped grant is safe at any graph position. DB-authoritative —
    # resolved by nodeDefinitionId, never by the caller-supplied node_kind
    # string (should_inject_llm_generator_direct_client_grant), so a tenant
    # cannot earn it by relabeling their own node "task_extract". No stack to
    # resolve (unlike VLM/SDXL/generator above) — the node's own code already
    # defaults to the right managed endpoint; it only needs the network marker.
    if should_inject_llm_generator_direct_client_grant(
            db, node_data, node_conf=node_conf) \
            and "API_KEY" not in _cenv:
        _cenv.setdefault("MOD_NEEDS_API_TOKEN", "1")

    # 2400LR-ceGP-F5 (BUG-1027d) — MOD-authored export nodes (return_assets_
    # export / unity_export / unreal_export) downstream of anything: the root-
    # only B108 promotion never fires and _scrub_reserved_container_env above
    # stripped the router's standing API_KEY from the tenant-owned copy, so
    # without this grant the container lands on the default-deny network and
    # export.py's create-output-folder call never connects (exit 1 x3). Same
    # DB-authoritative gate shape as B114 (should_inject_export_client_grant
    # -> the single _client_grant_allowed implementation), same SCOPED marker:
    # the worker vends the node a short-lived run_task token (mod_node_auth.py
    # prefers MOD_TASK_TOKEN_URL); the standing platform key is NEVER handed
    # to a tenant-owned copy. `"API_KEY" not in _cenv` keeps platform-owned
    # rows (which keep their key, scrub no-op) unchanged — the marker is only
    # needed on the scrubbed tenant copies.
    if should_inject_export_client_grant(
            db, node_data, node_conf=node_conf) \
            and "API_KEY" not in _cenv:
        _cenv.setdefault("MOD_NEEDS_API_TOKEN", "1")

    node_conf["container_env"] = _cenv

    # Build-task containers run docker build/push and need /var/run/docker.sock
    # bind-mounted. DB-authoritative gate (never trusts node_data.node_kind).
    if should_bind_docker_socket(db, node_data):
        node_conf["needs_docker_socket"] = True


def _emit_run_event(
    db: "Session",
    workflow_run_id: int,
    source: str,
    level: str,
    message: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert a row into workflow_run_events (#12, launch-day-followup-2).
    Surfaces orchestrator / traffic-cop / worker activity in the
    run-detail Logs panel — the gap between two container outputs
    that previously left users guessing what the system was doing.

    Best-effort: any DB error is logged but never propagated, so a
    missing/full event log can't break the run itself.
    """
    try:
        from models import WorkflowRunEvent
        ev = WorkflowRunEvent(
            workflow_run_id=workflow_run_id,
            source=source,
            level=level,
            message=message,
            event_metadata=metadata,
        )
        db.add(ev)
        # Don't commit — let the caller's transaction commit it. If
        # the caller rolls back, the event row rolls back too (which
        # is correct — no run state means no events about it).
    except Exception as e:
        logger.debug("WorkflowRunEvent write failed (non-fatal): %s", e)


def _node_port_names(node: dict):
    """Return (input_ports, output_ports) declared by a node, or None for a
    side when the node declares no schema for it (then that side's handles are
    NOT validated — absent schema means "unknown", never "invalid").

    Tolerant of the shapes node data takes across wizard versions:
      * JSON-schema style: input_schema/output_schema = {"properties": {...}}
        or a bare {port_name: spec} mapping
      * port-list style: inputs/outputs = [{"name": ...}, ...] or ["name", ...]
    """
    data = (node or {}).get("data") or {}

    def _collect(*keys):
        names = set()
        found = False
        for k in keys:
            v = data.get(k)
            if not v:
                continue
            found = True
            if isinstance(v, dict):
                props = v.get("properties") if "properties" in v else v
                if isinstance(props, dict):
                    names.update(str(x) for x in props.keys())
            elif isinstance(v, list):
                for p in v:
                    if isinstance(p, dict):
                        nm = p.get("name") or p.get("id") or p.get("handle")
                        if nm:
                            names.add(str(nm))
                    elif isinstance(p, str):
                        names.add(p)
        return (names if found else None)

    in_ports = _collect("input_schema", "inputs", "input_ports")
    out_ports = _collect("output_schema", "outputs", "output_ports")
    return in_ports, out_ports


def build_execution_plan(nodes_list: list, edges_list: list) -> dict:
    """
    Build a dependency-aware execution plan from workflow graph.
    Pure Python (networkx-style topo sort, in-tree).

    Robust against malformed graphs (runaway-dispatch hardening):
      * edges referencing a non-existent node are dropped,
      * self-loops are dropped,
      * an edge whose sourceHandle/targetHandle names a port the node does
        NOT declare is dropped (a phantom handle must not create a
        dependency — this is the invalid `sourceHandle=input,
        targetHandle=output` edge from workflow_version 81),
      * DUPLICATE edges are collapsed: a node depends on a given parent
        exactly ONCE no matter how many edges connect them, so a duplicate
        edge can never inflate in-degree and leave a node permanently
        un-satisfiable (the root cause of the 126-task enqueue runaway).

    Args:
        nodes_list: List of node dicts from graph_json["nodes"]
        edges_list: List of edge dicts from graph_json["edges"]

    Returns:
        dict with nodes, edges, dependencies, execution_order, root_nodes.
        `edges` holds the VALIDATED edges (bad ones removed) so downstream
        port-mapping never rebuilds a phantom dependency.

    Raises:
        ValueError: If a circular dependency is detected (the run is then
            failed cleanly by the caller rather than spinning forever).
    """
    # Build node lookup
    nodes = {node["id"]: node for node in nodes_list}

    # Build dependency graph (node_id -> list of parent node_ids).
    # `seen_pairs` dedupes by (source, target): the scheduling in-degree must
    # count each parent once. The per-port fan-out that legitimately needs
    # multiple edges between the same two nodes is preserved in `valid_edges`
    # (returned as plan["edges"]) for downstream port recovery.
    dependencies = defaultdict(list)
    dependents = defaultdict(list)
    seen_pairs = set()
    dropped_edges = 0

    # DEPENDENCIES are derived purely from node PAIRS, never from handles: an
    # edge source->target means target depends on source regardless of handle
    # names. (Validating handles here in a prior loop-guard change dropped
    # legitimate edges from legacy-form nodes, leaving a downstream node with
    # zero dependencies so it ran BEFORE its inputs finished.) So the pair de-dup
    # below is what prevents the runaway (a parent counted once). Ordering logic
    # is untouched.
    #
    # valid_edges (used ONLY for data staging / port recovery) is chosen in a
    # second step per pair: prefer edges whose targetHandle is a REAL input port
    # of the target so a MALFORMED/backwards duplicate (the incident: input-2->
    # process-1 with targetHandle=output) can't win port recovery. A node's SOLE
    # edge to a parent is ALWAYS kept (even if its handle looks off), so this
    # never removes a dependency's staging edge — it only drops redundant
    # malformed duplicates.
    from collections import OrderedDict as _OD

    def _target_input_handles(node_id):
        d = (nodes.get(node_id) or {}).get("data", {}) or {}
        names = set()
        for it in (d.get("inputs") or []):
            n = it.get("name") if isinstance(it, dict) else it
            if n:
                names.add(n)
        isch = d.get("input_schema") or {}
        props = isch.get("properties") if isinstance(isch, dict) else None
        if isinstance(props, dict):
            names.update(props.keys())
        return names

    pair_edges = _OD()  # (source, target) -> [edges in order]
    for edge in (edges_list or []):
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target:
            dropped_edges += 1
            continue
        if source not in nodes or target not in nodes:
            dropped_edges += 1
            continue
        if source == target:  # self-loop is never a real dependency
            dropped_edges += 1
            continue
        pair = (source, target)
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            dependencies[target].append(source)
            dependents[source].append(target)
        pair_edges.setdefault(pair, []).append(edge)

    valid_edges = []
    for (source, target), elist in pair_edges.items():
        valid_handles = _target_input_handles(target)

        def _handle_ok(e):
            th = e.get("targetHandle")
            return (not valid_handles) or th is None or th in valid_handles

        goods = [e for e in elist if _handle_ok(e)]
        chosen = goods if goods else elist[:1]  # sole legit edge is always kept
        seen_th = set()
        for e in chosen:
            th = e.get("targetHandle")
            if th in seen_th:  # collapse exact-duplicate handle rows
                continue
            seen_th.add(th)
            valid_edges.append(e)
        dropped_edges += len(elist) - len(seen_th)

    if dropped_edges:
        logger.warning(
            "build_execution_plan: dropped %d invalid/orphan edge(s) of %d",
            dropped_edges, len(edges_list or []),
        )

    # Find root nodes (no dependencies)
    root_nodes = [node_id for node_id in nodes.keys() if node_id not in dependencies]

    # Topological sort to determine execution order (level-based)
    execution_order = []
    in_degree = {node_id: len(deps) for node_id, deps in dependencies.items()}
    for node_id in nodes.keys():
        if node_id not in in_degree:
            in_degree[node_id] = 0

    queue = deque(root_nodes)

    while queue:
        current_level = list(queue)
        execution_order.append(current_level)
        queue.clear()

        for node_id in current_level:
            for dependent in dependents.get(node_id, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

    # Validate all nodes were scheduled (detect cycles)
    scheduled_nodes = set()
    for level in execution_order:
        scheduled_nodes.update(level)

    missing_nodes = set(nodes.keys()) - scheduled_nodes
    if missing_nodes:
        raise ValueError(
            "Circular dependency detected (invalid workflow graph): these "
            f"nodes can never become ready: {sorted(missing_nodes)}"
        )

    return {
        "nodes": nodes,
        "edges": valid_edges,
        "dependencies": dict(dependencies),
        "dependents": dict(dependents),
        "execution_order": execution_order,
        "root_nodes": root_nodes,
    }


def dispatch_released_root_node(db: Session, run, node_id: str) -> bool:
    """2400LR-bp1: resume of a held ENTRY node (no upstream completion exists to
    re-kick it). Re-enters _start_workflow for that one root; the spent
    breakpoint_hit event means the gate no longer holds it. True if dispatched."""
    from models import ApiTaskQueue
    params = run.parameters or {}
    plan = params.get("execution_plan") or {}
    if node_id not in (plan.get("root_nodes") or []):
        return False
    if db.query(ApiTaskQueue.id).filter(
        ApiTaskQueue.workflow_run_id == run.id,
        ApiTaskQueue.node_id == node_id,
    ).first():
        return False
    conf = {"nodes": list((plan.get("nodes") or {}).values()),
            "edges": plan.get("edges") or [],
            "project_info": params.get("project_info") or {},
            "tenant_id": run.tenant_id}
    _start_workflow(db, conf, run.id, only_nodes={node_id})
    return True


def start_workflow_execution(db_factory, conf: dict, workflow_run_id: int):
    """
    Start a workflow by building the execution plan and enqueuing root-level tasks.

    Called as a background task from the workflow execution endpoint.

    Args:
        db_factory: Callable that returns a new SQLAlchemy Session
        conf: The full workflow conf (carries the run's input data through to nodes)
        workflow_run_id: The WorkflowRun ID
    """
    db = db_factory()
    try:
        _start_workflow(db, conf, workflow_run_id)
    except Exception as e:
        # Keep the exception text short-but-specific so operators see
        # the real cause in the dashboard's run-detail view instead
        # of a generic "failed" badge. (Historically this catch
        # surfaced scan-block errors too — that gate is gone now, but
        # other validation failures and infra blowups still flow
        # through here.)
        err_text = f"{type(e).__name__}: {e}"[:2000]
        logger.error("Workflow orchestration failed for run %d: %s", workflow_run_id, e)
        try:
            from models import WorkflowRun, TaskStatus
            run = db.query(WorkflowRun).filter(WorkflowRun.id == workflow_run_id).first()
            if run and run.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                run.status = TaskStatus.FAILED
                run.completed_at = datetime.now(timezone.utc)
                # Persist so the dashboard's run-detail view can surface
                # the real cause (column added by the migration in
                # database.py — see workflow_runs.error_message).
                run.error_message = err_text
                db.commit()
                # fortress-2.1e — this early-abort path terminalizes the run
                # outside the worker finalize block, so the 2.1c hook in
                # fire_completion_triggers never sees it. Seal its signed
                # proof bundle here (post-commit; gated by
                # compliance.run_proof_bundle_enabled, background thread +
                # own session, best-effort — can never roll back the FAILED
                # status we just committed).
                # CARVE-U9a: services/run_proof_bundle.py is carve-denied.
                # optional_paid binding so absence is an explicit CE skip —
                # the old _module_available + log-only shape let the bare
                # import's ImportError fall into the seal-failure handler
                # (witness FAIL-OPEN).
                from utils.paid_import import optional_paid
                _rpb = optional_paid("services.run_proof_bundle")
                if _rpb is None:
                    logger.debug("SKIPPED (not in this edition): "
                                 "run proof bundle seal")
                else:
                    try:
                        _rpb.seal_run_proof_background(workflow_run_id)
                    except Exception:  # noqa: BLE001 — sealing is best-effort
                        logger.debug(
                            "run proof bundle: seal scheduling failed for run %d",
                            workflow_run_id,
                        )
                # Outbound webhooks (WS4.Q20b) — this early-abort path
                # terminalizes the run outside the worker finalize block, so
                # dispatch the completion fan-out here too. Background thread
                # + own session, exactly like the finalize path in
                # routers/worker_tasks.py.
                try:
                    from services.outbound_webhooks import (
                        dispatch_workflow_completion_background,
                    )
                    dispatch_workflow_completion_background(
                        tenant_id=run.tenant_id,
                        workflow_run_id=workflow_run_id,
                        status_str=run.status.value,
                    )
                    # GX.R3 — enqueue run-completion webhook deliveries
                    # (post-commit; a failure here must never affect the run).
                    try:
                        from services.run_completion_webhooks import (
                            enqueue_run_completion_background as _rcw_enqueue,
                        )
                        _rcw_enqueue(
                            tenant_id=run.tenant_id,
                            workflow_run_id=workflow_run_id,
                            status_str=run.status.value,
                        )
                    except Exception:
                        logger.debug(
                            "run_webhooks: enqueue scheduling failed for run %d",
                            workflow_run_id, exc_info=True,
                        )
                except Exception:
                    logger.debug(
                        "outbound webhook: scheduling failed for run %d",
                        workflow_run_id, exc_info=True,
                    )
        except Exception:
            # Last-ditch: don't lose the original failure just because
            # the DB write failed. The logger.error above already has it.
            pass
    finally:
        db.close()


def _resolve_node_def_for_routing(db, node_data, node_kind, tenant_id):
    """NodeDefinition that decides a root node's queue (gpu_variants -> queue).

    A node_kind is shared by the system row (tenant_id IS NULL) and tenant
    copies, so a bare ``node_kind == X .first()`` could pick the tenant-NULL row
    whose ``gpu_variants`` is NULL and send a CUDA-coded task to the CPU queue.
    Order: (1) the row the graph node references by ``nodeDefinitionId`` when it
    is live and visible to this tenant (system row or the tenant's own);
    (2) else by node_kind among live rows visible to the tenant, the tenant's own
    row before the system row, and among equals a row with non-NULL
    ``gpu_variants`` first (then lowest id). A kind with one row is unchanged.
    """
    from models import NodeDefinition as _ND
    nd_id = node_data.get("nodeDefinitionId") if isinstance(node_data, dict) else None
    if nd_id:
        try:
            nd = db.query(_ND).filter(_ND.id == int(nd_id)).first()
        except (TypeError, ValueError):
            nd = None
        if (nd is not None and getattr(nd, "deleted_at", None) is None
                and (nd.tenant_id is None or nd.tenant_id == tenant_id)):
            return nd
    rows = db.query(_ND).filter(
        _ND.node_kind == node_kind,
        _ND.deleted_at.is_(None),
        _ND.tenant_id.is_(None) | (_ND.tenant_id == tenant_id),
    ).all()
    if not rows:
        # Preserve the previous behaviour when nothing visible matched.
        return db.query(_ND).filter(_ND.node_kind == node_kind).first()
    rows.sort(key=lambda r: (r.tenant_id is None, not r.gpu_variants, r.id or 0))
    return rows[0]


def _check_removed_node_definitions(db, nodes_list: list) -> None:
    """Q15 (2026-10-03) — fail LOUDLY if a node in the plan was soft-deleted.

    The ``with_loader_criteria`` filter in ``database.py`` makes a soft-deleted
    ``NodeDefinition`` invisible to the lookups that follow, so without this a
    run referencing a removed node degrades into an obscure "no root nodes had
    a node_kind" (or a silently-skipped root) instead of a clear error. Resolve
    each ``nodeDefinitionId`` against the FULL (include_deleted) table and name
    any removed node in an operator-readable message. FaceFusion gets the
    specific Q15 reason; any other soft-deleted node gets the generic wording.

    Raises ValueError when at least one referenced node definition is
    soft-deleted. No-op otherwise (unknown / still-live nodes pass through).
    """
    from models import NodeDefinition

    removed = set()
    for node in nodes_list:
        nd_id = (node.get("data") or {}).get("nodeDefinitionId")
        if not nd_id:
            continue
        row = (
            db.query(NodeDefinition)
            .execution_options(include_deleted=True)
            .filter(NodeDefinition.id == nd_id)
            .first()
        )
        if row is not None and row.deleted_at is not None:
            removed.add(row.name or str(nd_id))

    if not removed:
        return

    first = sorted(removed)[0]
    extra = (f" (also removed: {', '.join(sorted(removed)[1:])})"
             if len(removed) > 1 else "")
    if first.strip().lower() == "facefusion":
        reason = (
            f"'{first}' was removed from MOD on 2026-10-03 (Q15: non-commercial "
            f"InsightFace inswapper licence + deepfake liability)"
        )
    else:
        reason = (
            f"node '{first}' was removed from MOD on 2026-10-03 "
            f"(its node definition is no longer available)"
        )
    raise ValueError(
        f"Workflow cannot run: {reason}. "
        f"Remove or replace this node in the workflow and re-run.{extra}"
    )


def _start_workflow(db: Session, conf: dict, workflow_run_id: int, only_nodes=None):
    """Internal: build plan + enqueue root tasks. Handles batch mode."""
    from models import WorkflowRun, NodeRun, TaskStatus, ApiTaskQueue, ApiTaskStatus

    nodes_list = conf.get("nodes", [])
    edges_list = conf.get("edges", [])
    project_info = conf.get("project_info") or {}
    tenant_id = conf.get("tenant_id")
    cog_ids = project_info.get("cog_ids", [])

    if not nodes_list:
        raise ValueError("No nodes in workflow conf")

    # Detect execution mode from node data (batch = one run per file)
    execution_mode = "batch"
    for node in nodes_list:
        node_data = node.get("data", {})
        batch_mode = node_data.get("batch_mode")
        if batch_mode and batch_mode != "batch":
            execution_mode = batch_mode
            break

    # Build execution plan
    plan = build_execution_plan(nodes_list, edges_list)

    # Q15 (2026-10-03) — fail LOUDLY on a soft-deleted node definition.
    _check_removed_node_definitions(db, nodes_list)

    # Container-security policy decision: warn, don't block.
    #
    # Earlier policy ran scan_status as a hard runtime gate for SaaS
    # deployments — anything that wasn't 'approved'/'clean'/'high_vulns'
    # raised ValueError and the workflow refused to start. That was
    # appropriate for full multi-tenant SaaS where vulnerable nodes
    # could impact other customers. Product direction has since
    # changed: this is operator-managed deployments, the tenant owns
    # their own risk, and an unrunnable node is worse UX than a
    # warned-but-runnable one.
    #
    # Scan status is preserved on the row (Harbor still scans, the API
    # still polls, the row still reflects reality). The UI surfaces
    # color-coded badges so users see WHAT was found. We just don't
    # block execution. Anyone who wants to re-add a gate later can
    # restore the check from git history (this file pre-commit
    # b2044002).
    from models import NodeDefinition
    _NOISY_STATUSES = (
        "critical_vulns", "scan_failed", "rejected", "failed",
        "pending_scan", "scanning",
    )
    for node in nodes_list:
        node_data = node.get("data", {})
        node_def_name = node_data.get("label") or node_data.get("name")
        if not node_def_name:
            continue
        node_def = db.query(NodeDefinition).filter(
            NodeDefinition.name == node_def_name,
        ).first()
        if not node_def or not node_def.scan_status:
            continue
        if node_def.scan_status in _NOISY_STATUSES:
            # Log the warning so admins watching the run feed can see
            # which nodes are at risk. The workflow still proceeds.
            logger.warning(
                "Workflow run %s admits node '%s' with scan_status=%s "
                "(warn-only policy; check Tenant Registry for CVE detail)",
                workflow_run_id, node_def_name, node_def.scan_status,
            )

    # Store execution plan on the WorkflowRun for downstream dispatch
    run = db.query(WorkflowRun).filter(WorkflowRun.id == workflow_run_id).first()
    if not run:
        raise ValueError(f"WorkflowRun {workflow_run_id} not found")

    run.status = TaskStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    _emit_run_event(db, workflow_run_id, "orchestrator", "info",
                     "Workflow run started")

    # Stamp a stable run identifier for the UI
    import uuid
    if not run.orchestrator_run_id:
        run.orchestrator_run_id = f"api_run_{workflow_run_id}_{uuid.uuid4().hex[:8]}"

    # Defensive backfill of node.data fields from NodeDefinition.
    # Legacy workflow versions (saved before LDF-4 §2.7 fixed the
    # save-path stripping bug) only persist {label, nodeDefinitionId}
    # in data — node_kind / executable_path / output_schema /
    # cost_in_credits / process_type were dropped. Without this:
    #   - root dispatch fails with "no root nodes had a node_kind"
    #   - downstream dispatch (worker_tasks._check_and_dispatch_downstream)
    #     reads the stored plan and silently skips downstream nodes,
    #     marking the run COMPLETED with only root nodes executed
    # Enriching the WHOLE plan before persisting fixes both surfaces
    # and ensures the run-detail UI reflects the resolved fields too.
    from models import NodeDefinition as _NodeDef
    def _enrich_from_def(node_data):
        nd_id = (node_data or {}).get("nodeDefinitionId")
        if not nd_id:
            return node_data
        nd = db.query(_NodeDef).filter(_NodeDef.id == nd_id).first()
        if not nd:
            return node_data
        enriched = dict(node_data)
        # Backfill from the NodeDefinition when the stored graph_json
        # has an empty value (NULL, missing key, or empty string).
        # `setdefault` only fills MISSING keys — it doesn't replace an
        # explicit null, which was the bug behind workflow_run 15:
        # the saved graph_json had `node_kind: null` baked in from a
        # version of the wizard that didn't populate it, the DB row
        # has been backfilled, but enrichment couldn't migrate the
        # plan forward. Use truthy-aware fallback so the canonical
        # NodeDefinition wins whenever the stored value is empty.
        def _fallback(key, db_value):
            cur = enriched.get(key)
            if cur is None or cur == "" or cur == {} or cur == 0:
                enriched[key] = db_value
        _fallback("node_kind", nd.node_kind)
        _fallback("executable_path", nd.executable_path or "")
        _fallback("output_schema", nd.output_schema or {})
        # GX.R1 — the declared run-time knobs (configuration_schema.extra_variables)
        # are authoritative on the NodeDefinition row, NOT on the saved graph node.
        # The WB persists graph node.data as {label, nodeDefinitionId, ...} — it
        # never bakes configuration_schema into node.data — so without this
        # backfill `_declared_var_names` sees NOTHING for a normal node and the
        # run-level `extra_variables` name-match silently drops every inline
        # run input (the "apps built on MOD: run-level inputs never reach nodes"
        # bug). Hydrating it here is also what the downstream-dispatch path reads
        # (this enriched plan is persisted into run.parameters.execution_plan),
        # so root + downstream dispatch both match by name from the same source.
        # The "NONE" string sentinel some definitions carry is tolerated by
        # _declared_var_names' isinstance(dict) guard.
        _fallback("configuration_schema", nd.configuration_schema or {})
        # cost_in_credits = 0 is a legitimate value (free nodes), so
        # we only backfill when the key is MISSING, not when it's
        # explicitly 0.
        enriched.setdefault("cost_in_credits", nd.cost_in_credits or 0)
        _fallback("process_type", nd.process_type or "CPU")
        return enriched

    # Apply enrichment to every node in the plan (in-place).
    for _node_id, _node in plan.get("nodes", {}).items():
        _node["data"] = _enrich_from_def(_node.get("data", {}))

    # Store the (enriched) plan in parameters (execution_plan key) so
    # downstream-dispatch reads backfilled values.
    if run.parameters is None:
        run.parameters = {}
    run.parameters["execution_plan"] = plan
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(run, "parameters")
    db.commit()

    # BUG.B85a — shared terminalization for the fail-fast guards below.
    # Same path the existing "no root nodes had a node_kind" fail-fast uses
    # (status + error_message + commit + run event + completion fan-out),
    # plus completed_at, so a refused/orphaned run never sits RUNNING waiting
    # for the zombie reaper to mark it COMPLETED.
    def _terminalize_run_failed(msg: str, meta: dict) -> None:
        logger.error("Workflow %d: %s", workflow_run_id, msg)
        run.status = TaskStatus.FAILED
        run.error_message = msg
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        # fortress-2.1e — shared fail-fast terminalization: seal the run's
        # signed proof bundle post-commit (gated + background + best-effort,
        # same as the 2.1c hook in fire_completion_triggers).
        # CARVE-U9a: optional_paid binding — absent module = explicit CE skip
        # (witness FAIL-OPEN on the old log-only shape).
        from utils.paid_import import optional_paid
        _rpb = optional_paid("services.run_proof_bundle")
        if _rpb is None:
            logger.debug("SKIPPED (not in this edition): "
                         "run proof bundle seal")
        else:
            try:
                _rpb.seal_run_proof_background(workflow_run_id)
            except Exception:  # noqa: BLE001 — sealing is best-effort
                logger.debug(
                    "run proof bundle: seal scheduling failed for run %d",
                    workflow_run_id,
                )
        _emit_run_event(db, workflow_run_id, "orchestrator", "error", msg, meta)
        try:
            from services.outbound_webhooks import (
                dispatch_workflow_completion_background,
            )
            dispatch_workflow_completion_background(
                tenant_id=run.tenant_id,
                workflow_run_id=workflow_run_id,
                status_str=run.status.value,
            )
            try:
                from services.run_completion_webhooks import (
                    enqueue_run_completion_background as _rcw_enqueue,
                )
                _rcw_enqueue(
                    tenant_id=run.tenant_id,
                    workflow_run_id=workflow_run_id,
                    status_str=run.status.value,
                )
            except Exception:
                logger.debug(
                    "run_webhooks: enqueue scheduling failed for run %d",
                    workflow_run_id, exc_info=True,
                )
        except Exception:
            logger.debug(
                "outbound webhook: scheduling failed for run %d",
                workflow_run_id, exc_info=True,
            )

    # Create NodeRun records for ALL nodes (status=queued)
    from models import WorkflowNode
    _node_runs_created = 0
    for node_id_key in plan["nodes"]:
        try:
            wn = db.query(WorkflowNode).filter(
                WorkflowNode.workflow_id == run.workflow_id,
                WorkflowNode.graph_node_id == node_id_key,
            ).first()
            if wn:
                existing_nr = db.query(NodeRun).filter(
                    NodeRun.workflow_run_id == workflow_run_id,
                    NodeRun.workflow_node_id == wn.id,
                ).first()
                if not existing_nr:
                    nr = NodeRun(
                        workflow_run_id=workflow_run_id,
                        workflow_node_id=wn.id,
                        status=TaskStatus.QUEUED,
                    )
                    db.add(nr)
                # BUG.B85a — count NodeRuns PRESENT for this run (created now
                # or already there on a resumed run) so the orphan guard below
                # only fires when the graph truly has no backing rows.
                _node_runs_created += 1
        except Exception as e:
            logger.debug("Could not create NodeRun for %s: %s", node_id_key, e)
    db.commit()

    # BUG.B85a — orphaned run: the plan has nodes but no WorkflowNode row
    # matched any graph node, so nothing can ever be dispatched or finalized
    # through the NodeRun bookkeeping. Terminalize now instead of leaving the
    # run RUNNING with 0 NodeRuns (the zombie reaper used to flip it COMPLETED).
    # The workflow DOES carry WorkflowNode rows but none of them match the
    # plan's graph ids (graph_node_id drift / re-saved graph) — the real
    # "workflow_nodes missing for graph nodes" shape. A workflow with no
    # WorkflowNode rows at all keeps its historical behaviour untouched.
    _wn_row_count = (
        db.query(WorkflowNode)
        .filter(WorkflowNode.workflow_id == run.workflow_id)
        .count()
    )
    # BUG.B85 (owner ghost run 2202) — the `_wn_row_count > 0` carve-out was
    # the stranding window. Workflow 1 carried NO WorkflowNode rows at all
    # (its row 160 was created ~15:00 on 2026-10-09, which is exactly when
    # runs 2205+ started getting NodeRuns), so runs 2200-2204 — every one of
    # them zero NodeRuns / zero ApiTaskQueue rows — fell through this guard
    # and sat RUNNING. The zombie reconcile then finalized 2202 as COMPLETED
    # (audit 1505160, verdict 'completed', 12:38:57) — a false success for a
    # run that executed nothing, and one that poisons every restart drain
    # "is the system busy" check while it is still RUNNING. A plan that has
    # nodes and no backing WorkflowNode row can never dispatch, whether the
    # workflow has zero rows or only mismatched ones, so the shape is the
    # same and the verdict is the same: FAILED, honestly labelled.
    if plan["nodes"] and _node_runs_created == 0:
        _terminalize_run_failed(
            ("orphaned: no node runs (workflow_nodes missing for graph nodes)"
             if _wn_row_count > 0 else
             "orphaned: no node runs (workflow has no workflow_nodes rows)"),
            {"plan_node_count": len(plan["nodes"]),
             "node_runs_created": 0,
             "workflow_node_rows": _wn_row_count},
        )
        return

    logger.info(
        "Workflow %d: built plan with %d nodes, %d levels, root=%s",
        workflow_run_id, len(plan["nodes"]), len(plan["execution_order"]), plan["root_nodes"],
    )

    # expr 9.2 — long_running pre-dispatch checkpoint gate.  NodeRuns for the
    # whole plan exist; no worker task has been queued yet.  The graph-level
    # declaration lives on the conf the run endpoint threads through
    # (graph_json["settings"].long_running); `conf` IS that graph dict here, so
    # the non-long_running path costs zero extra queries (is_long_running is a
    # pure dict check and the gate short-circuits on it).
    # CE lazy import (1634o): pre_dispatch_gate is None when
    # services.long_running_dispatch is absent (CE carve) — CE has no
    # long_running feature at all, so skip the gate outright rather than
    # calling a None sentinel.
    if pre_dispatch_gate is not None:
        try:
            gate = pre_dispatch_gate(db, tenant_id=tenant_id, workflow_run=run,
                                     graph_json=conf)
        except Exception as gate_exc:
            # Fail closed for long_running runs: an unmarked long-running run
            # silently drops the guarantee, so refuse.  Non-long_running runs
            # must NEVER be blocked by this hook — log the class name only (never
            # free text: operators see the exact failure shape) and continue.
            if is_long_running(conf):
                _terminalize_run_failed(
                    "long_running pre-dispatch refused: checkpoint_unavailable",
                    {"reason": "checkpoint_unavailable",
                     "gate_exception": type(gate_exc).__name__},
                )
                return
            logger.warning(
                "Workflow %d: long_running pre-dispatch gate raised %s "
                "(non-long_running run continues unblocked)",
                workflow_run_id, type(gate_exc).__name__,
            )
        else:
            if not gate.get("allowed", False):
                # gate["reason"] is a fixed code from the gate module
                # (checkpoint_unavailable / checkpoint_quota_exceeded), never free text.
                _terminalize_run_failed(
                    f"long_running pre-dispatch refused: {gate['reason']}",
                    {"reason": gate["reason"]},
                )
                return
            if gate.get("checkpoint_id") is not None:
                # The marker must be durable BEFORE any root task is queued —
                # the gate only ever flushes; the caller owns this commit.
                db.commit()

    # Enqueue root-level tasks
    # Batch mode: one task per file. Grouped mode: one task with all files.
    root_nodes = plan["root_nodes"]
    if only_nodes is not None:  # 2400LR-bp1: re-dispatch of a released entry node
        root_nodes = [n for n in root_nodes if n in only_nodes]

    # Determine file batches
    if execution_mode == "batch" and len(cog_ids) > 1:
        file_batches = [[cid] for cid in cog_ids]
        logger.info("Workflow %d: batch mode — %d file(s), %d iteration(s)", workflow_run_id, len(cog_ids), len(file_batches))
    else:
        file_batches = [cog_ids]

    enqueued_count = 0
    skipped_count = 0
    # BUG.B85a — refusals ({"task_id": None, "status": "egress_gov_blocked",
    # "reason": ...} from routers/worker_tasks.py) must NOT count as
    # enqueued; they get collected so the post-loop guard can terminalize
    # the run instead of leaving it RUNNING with 0 NodeRuns.
    refusals: list = []
    # Plan was enriched in-place above before persist; no per-iteration
    # backfill needed here.
    for batch_cog_ids in file_batches:
        for node_id in root_nodes:
            node = plan["nodes"][node_id]
            node_data = node.get("data", {})
            node_kind = node_data.get("node_kind")

            if not node_kind:
                logger.info("Skipping node %s (no node_kind)", node_id)
                skipped_count += 1
                continue

            # 2400LR-bp1: a persisted canvas breakpoint on an ENTRY node holds it
            # before its first dispatch, exactly like the downstream gate.
            from services.breakpoint_gate import check_and_hold as _bp_hold
            if _bp_hold(db, run, node_id):
                db.commit()
                logger.info("root dispatch held at node %s — breakpoint hit on run %s",
                            node_id, workflow_run_id)
                continue

            # Build node conf — flatten key fields to top level (Go worker expects them at root)
            batch_project_info = dict(project_info)
            batch_project_info["cog_ids"] = batch_cog_ids
            # PERRUN-BINDINGS-SLICE-1: neither the request key nor the names
            # record rides in the durable task payload; the chosen ref is
            # already inside node_data.secret_refs for the resolver.
            batch_project_info.pop("secret_bindings", None)
            batch_project_info.pop("secret_binding_names", None)

            # workflow-agentic-execution-loop §wizard-test-run-dispatch —
            # sandbox-mode runs route output writes to a per-session
            # isolated MinIO prefix WITHIN the same tenant bucket. Pure
            # routing logic lives in services.wizard_sandbox_payload so
            # it's testable without invoking the full orchestrator path.
            from services.wizard_sandbox_payload import compute_sandbox_payload_fields
            from utils.minio_client import get_tenant_bucket_name
            _sandbox_fields = compute_sandbox_payload_fields(
                getattr(run, "purpose", "production"),
                conf,
            )

            # GX.R1 — thread run-level extra_variables + per-node inputs into
            # this root node's effective extraVariables (per-node > run-level >
            # configured default). No-op when the run carries neither.
            _run_extra, _per_node_inputs = _run_extra_values(project_info)
            if _run_extra or _per_node_inputs:
                node_data = _apply_run_extra_variables(
                    node_data, _run_extra, _per_node_inputs, node_id)

            # PERRUN-BINDINGS-SLICE-1 — write the run's chosen secret NAME into
            # a bindable ref so the claim-time resolver (which trusts the task
            # payload) resolves the chosen secret; unbound runs are untouched.
            from services.run_secret_bindings import (
                apply_to_node_data as _rsb_apply, bindings_from_project_info as _rsb_from_pi)
            _run_bindings = _rsb_from_pi(project_info)
            if _run_bindings:
                node_data = _rsb_apply(node_data, _run_bindings)

            node_conf = {
                "tenant_id": tenant_id,
                # GC.MWL.7c: no keycloak_token in the task payload — the Go
                # worker never read this key (credentials are minted
                # on-demand via GET /workers/tasks/{id}/credentials), so
                # re-propagating it here just meant every downstream task
                # row (ApiTaskQueue.task_payload) durably carried the live
                # secret too. Dropped at the source.
                "workflow_run_id": workflow_run_id,
                "node_id": node_id,
                "node_data": node_data,
                "ProjectInfo": batch_project_info,
                "parent_outputs": {},
                "node_kind": node_kind,
                "executable_path": node_data.get("executable_path", ""),
                "output_schema": node_data.get("output_schema", {}),
                "cost_in_credits": node_data.get("cost_in_credits", 0),
                "minio_bucket": get_tenant_bucket_name(tenant_id, db),
                "cog_ids": batch_cog_ids,
                # Sandbox-mode flags — empty/None in production, populated
                # for wizard_test_run purpose. Worker reads these to
                # enforce sandbox semantics (timeout / size cap / prefix).
                **_sandbox_fields,
            }
            # GC.MWL.12 — borrower-side loaned run: stamp the work-loan id so
            # the worker tags this task's S3 outputs with Mod-Loan-Status /
            # Mod-Loan-Id. Ordinary runs resolve to None and the payload is
            # left untouched (byte-identical to before).
            from utils.paid_import import optional_paid
            _mwl_payload = optional_paid("services.mesh_loan_payload")
            _loan_id = (
                _mwl_payload.loan_id_for_run(db, workflow_run_id)
                if _mwl_payload is not None else None
            )
            if _loan_id is not None:
                node_conf["loan_id"] = str(_loan_id)
            # GC.MWL.C7 — owner-side: this node run may be the OTHER end of a
            # borrower's checkpoint hand-back (a duplicate-execution
            # "handback" decision, GC.MWL.C2). If GC.MWL.8's reaper tick has
            # already registered an import for it, hand the worker a
            # presigned GET so it hydrates instead of a cold restart.
            # Ordinary runs (no hand-back staged) resolve to None and the
            # payload is left untouched.
            from utils.paid_import import optional_paid
            _mwl_handback = optional_paid("services.mesh_loan_handback")
            _hydrate_url = (
                _mwl_handback.hydrate_url_for_node(db, workflow_run_id, node_id)
                if _mwl_handback is not None else None
            )
            if _hydrate_url:
                node_conf["checkpoint_hydrate_url"] = _hydrate_url
            # fortress-35 — loaned-run checkpoint EXPORT side of GC.MWL.C7:
            # this node run may have a pending export (mesh_loans.py
            # request_checkpoint_export) waiting for the worker to PUT its
            # checkpoint bytes. Ordinary runs (no pending export) resolve to
            # None and the payload is left untouched.
            _export_url = (
                _mwl_handback.export_url_for_node(db, workflow_run_id, node_id)
                if _mwl_handback is not None else None
            )
            if _export_url:
                node_conf["checkpoint_export_url"] = _export_url
                _export_transfer_id = _mwl_handback.export_transfer_id_for_node(
                    db, workflow_run_id, node_id)
                if _export_transfer_id is not None:
                    node_conf["checkpoint_export_transfer_id"] = _export_transfer_id
            # node-trial-runs — carry the trialed node's kind so the worker
            # paywall (enqueue + credential mint) can exempt exactly that node in
            # a node_trial run. Harmless on non-trial runs (read only when
            # purpose == 'node_trial'). `purpose` already flows via _sandbox_fields.
            _trial_kind = conf.get("trial_node_kind")
            if _trial_kind:
                node_conf["trial_node_kind"] = _trial_kind
            # δ DAG-wizard B5b — pass per-node container_env through
            # to the worker. Set by build_dag.submit when generating
            # the graph_json so each per-task container gets the
            # right STAGE / BUILD_SPEC_ID / VARIANT / Harbor creds.
            if isinstance(node_data, dict) and node_data.get("container_env"):
                node_conf["container_env"] = node_data["container_env"]
            # GX.sz-tcp.12 — stamp size_tier on every dispatch so the Go
            # worker / credit engine can branch on small/medium/large at
            # cost-calculation time.
            _tier, _source = _determine_size_tier(node_data)
            node_conf["size_tier"] = _tier
            node_conf["size_tier_source"] = _source
            # Scoped/gated per-node injections (vector token, platform-service
            # key, VLM endpoint + host pin, docker socket) are applied centrally
            # in enqueue_task_internal, which this dispatch funnels through.
            #
            # B108: the general worker-mediated token marker for a ROOT
            # processing node that carries no scoped credential (it lands the
            # node on the main network so it can fetch short-lived run_task
            # tokens — resolve cog, report status, upload outputs) is
            # deliberately NOT set here. Setting it here, before
            # enqueue_task_internal runs _scrub_reserved_container_env,
            # would be wiped right back out for any tenant-owned (non
            # platform-owned) root NodeDefinition, since the scrub cannot
            # distinguish "the orchestrator just set this" from "a tenant
            # payload set this". Instead we pass is_root_node=True on the
            # TaskEnqueueRequest below; enqueue_task_internal applies this
            # marker AFTER the scrub (same ordering the VLM/SDXL/generator
            # injections already use). is_root_node is platform-internal-only
            # — _enforce_tenant_enqueue_scope 403s it on the public HTTP
            # enqueue route — so a tenant cannot self-declare "root" to earn
            # the promotion.

            # Get node_run_id
            from models import WorkflowNode
            workflow_node = db.query(WorkflowNode).filter(
                WorkflowNode.workflow_id == run.workflow_id,
                WorkflowNode.graph_node_id == node_id,
            ).first()

            node_run = None
            if workflow_node:
                node_run = db.query(NodeRun).filter(
                    NodeRun.workflow_run_id == workflow_run_id,
                    NodeRun.workflow_node_id == workflow_node.id,
                ).first()

            node_run_id = node_run.id if node_run else None

            # Queue routing is authoritative on NodeDefinition (not the metadata
            # file). select_queue_for_node derives from gpu_variants:
            # 0 → CPU, 1 → that variant, 2+ → ACCELERATOR (meta-queue;
            # variants_guard filters at claim time). See
            # CORE/API/utils/queue_selector.py.
            from utils.queue_selector import select_queue_for_node_data
            from models import NodeDefinition
            node_def = _resolve_node_def_for_routing(
                db, node_data, node_kind, tenant_id)
            # Route by NodeDefinition.gpu_variants, BUT for build-DAG variant
            # nodes (generic system build-task, no gpu_variants) honor the
            # node's data.variant so a cuda/rocm/intel variant's build + smoke +
            # sample-test land on matching-hardware workers, not CPU.
            node_queue = select_queue_for_node_data(node_def, node_data)

            # Gate 5 — an approval gate as a root node. Parks and dispatches
            # nothing; the graph advances when a person answers.
            if park_for_human_approval(
                db,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                node_id=node_id,
                node_kind=node_kind,
                node_data=node_data,
            ):
                continue

            # FX 4.1 — an `agent` node as a root node. Runs API-side on a bg
            # thread (no container, no worker slot); True means enqueue nothing.
            # Fail-closed on expr.phase3_agent_nodes_enabled inside the helper.
            if start_agent_node(
                db,
                tenant_id=tenant_id,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                node_id=node_id,
                node_kind=node_kind,
                node_data=node_data,
                graph_json={"nodes": nodes_list, "edges": edges_list},
                agent_input=node_conf.get("parent_outputs") or {},
            ):
                continue

            # Gate 2.1 (wiring) — a sub_workflow node as a root node. The child
            # run IS the wait, so nothing is enqueued here: the runtime parks
            # this NodeRun (PENDING_ACTION + CHILD_MARKER) and dispatches the
            # pinned workflow version as an ordinary child run. Same tenant
            # gate as the other control-flow node kinds — sub_workflow_runtime
            # does not gate itself, so the call site does.
            if node_kind == "sub_workflow":
                # LAZY import: sub_workflow_runtime lazily imports this module
                # (complete_parent_for_child -> _check_and_dispatch_downstream),
                # so a top-level import here would be circular (FX.106 p3a shape).
                from services.sub_workflow_runtime import park_and_dispatch_sub_workflow
                from services.control_flow import is_enforcement_enabled_for_tenant
                if not is_enforcement_enabled_for_tenant(db, tenant_id):
                    _emit_run_event(
                        db, workflow_run_id, "orchestrator", "error",
                        f"Control-flow node {node_id} requires "
                        f"control_flow_primitives_enabled flag (off for this tenant)",
                        {"node_id": node_id, "kind": node_kind},
                    )
                    continue
                if park_and_dispatch_sub_workflow(
                    db,
                    workflow_run_id=workflow_run_id,
                    node_run_id=node_run_id,
                    node_id=node_id,
                    node_kind=node_kind,
                    node_data=node_data,
                ):
                    continue

            # Direct function call (no HTTP round-trip — same process)
            from routers.worker_tasks import enqueue_task_internal, TaskEnqueueRequest
            try:
                enqueue_req = TaskEnqueueRequest(
                    workflow_run_id=workflow_run_id,
                    node_run_id=node_run_id,
                    node_id=node_id,
                    tenant_id=tenant_id,
                    queue_name=node_queue,
                    priority=0,
                    task_payload=node_conf,
                    # B108 — see the comment above this block: the root
                    # MOD_NEEDS_API_TOKEN marker is applied by
                    # enqueue_task_internal AFTER its reserved-env scrub, not
                    # here, so it survives for tenant-owned root nodes too.
                    is_root_node=True,
                )
                result = enqueue_task_internal(enqueue_req, db)
                task_id = (result or {}).get("task_id")
                if isinstance(result, dict) and task_id:
                    enqueued_count += 1
                    logger.info(
                        "Workflow %d: enqueued root task %s for node %s (node_kind=%s, queue=%s)",
                        workflow_run_id, task_id, node_id, node_kind, node_queue,
                    )
                else:
                    # BUG.B85a — enqueue refused the dispatch (e.g. egress_gov_blocked
                    # under a gov/DoD compliance profile). Counting it as enqueued
                    # left the run RUNNING with 0 tasks until the zombie reaper
                    # marked it COMPLETED. Collect the refusal instead so the
                    # post-loop guard below can terminalize the run FAILED.
                    _refusal_status = (
                        (result or {}).get("status") or "enqueue_refused"
                    )
                    refusals.append({
                        "node_id": node_id,
                        "status": _refusal_status,
                        "reason": (result or {}).get("reason"),
                    })
                    logger.warning(
                        "Workflow %d: root dispatch REFUSED for node %s "
                        "(status=%s, reason=%s)",
                        workflow_run_id, node_id, _refusal_status,
                        (result or {}).get("reason"),
                    )
            except Exception as e:
                logger.error("Failed to enqueue task for node %s: %s", node_id, e)
                raise

    # If every root node was skipped (no node_kind on any of them), the
    # run will hang forever in queued state — fail fast with a clear
    # error instead. This is a real launch-blocker case for workflows
    # the wizard built without populating node.data.node_kind (e.g. all-
    # passthrough graphs from a malformed apply).
    if enqueued_count == 0 and skipped_count > 0:
        msg = (
            f"No root nodes had a node_kind ({skipped_count} skipped). "
            "The workflow's root nodes are passthrough or missing node_kind "
            "metadata. Re-save the workflow or rebuild the missing "
            "node definitions."
        )
        logger.error("Workflow %d: %s", workflow_run_id, msg)
        run.status = TaskStatus.FAILED
        run.error_message = msg
        db.commit()
        # fortress-2.1e — fail-fast terminalization: seal the run's signed
        # proof bundle post-commit (gated + background + best-effort, same
        # as the 2.1c hook in fire_completion_triggers).
        # CARVE-U9a: optional_paid binding — absent module = explicit CE skip
        # (witness FAIL-OPEN on the old log-only shape).
        from utils.paid_import import optional_paid
        _rpb = optional_paid("services.run_proof_bundle")
        if _rpb is None:
            logger.debug("SKIPPED (not in this edition): "
                         "run proof bundle seal")
        else:
            try:
                _rpb.seal_run_proof_background(workflow_run_id)
            except Exception:  # noqa: BLE001 — sealing is best-effort
                logger.debug(
                    "run proof bundle: seal scheduling failed for run %d",
                    workflow_run_id,
                )
        _emit_run_event(db, workflow_run_id, "orchestrator", "error",
                         msg, {"skipped_count": skipped_count})
        # Outbound webhooks (WS4.Q20b) — this early-fail path terminalizes the
        # run before any worker task exists, so the finalize block never runs.
        # Dispatch the completion fan-out here (background thread + own
        # session, same pattern as routers/worker_tasks.py).
        try:
            from services.outbound_webhooks import (
                dispatch_workflow_completion_background,
            )
            dispatch_workflow_completion_background(
                tenant_id=run.tenant_id,
                workflow_run_id=workflow_run_id,
                status_str=run.status.value,
            )
            # GX.R3 — enqueue run-completion webhook deliveries
            # (post-commit; a failure here must never affect the run).
            try:
                from services.run_completion_webhooks import (
                    enqueue_run_completion_background as _rcw_enqueue,
                )
                _rcw_enqueue(
                    tenant_id=run.tenant_id,
                    workflow_run_id=workflow_run_id,
                    status_str=run.status.value,
                )
            except Exception:
                logger.debug(
                    "run_webhooks: enqueue scheduling failed for run %d",
                    workflow_run_id, exc_info=True,
                )
        except Exception:
            logger.debug(
                "outbound webhook: scheduling failed for run %d",
                workflow_run_id, exc_info=True,
            )
        return

    # BUG.B85a — every root dispatch was REFUSED (e.g. egress_gov_blocked
    # under an active gov/DoD compliance profile). Nothing is queued, so the
    # run would otherwise sit RUNNING with 0 tasks until the zombie reaper
    # marked it COMPLETED. Terminalize FAILED with the refusal statuses.
    if enqueued_count == 0 and refusals:
        _status_list = ", ".join(
            sorted({str(r["status"]) for r in refusals}))
        _terminalize_run_failed(
            f"root dispatch refused: {_status_list}",
            {"refusals": refusals},
        )
        return

    logger.info("Workflow %d: %d root task(s) enqueued (%d skipped)",
                 workflow_run_id, enqueued_count, skipped_count)
    _emit_run_event(db, workflow_run_id, "orchestrator", "info",
                     f"{enqueued_count} root task(s) enqueued",
                     {"enqueued_count": enqueued_count,
                      "skipped_count": skipped_count})


# ---------------------------------------------------------------------------
# Trigger-driven dispatch
# ---------------------------------------------------------------------------
#
# Used by webhook / file-event / log-pattern / queue-empty / completion
# triggers. Each trigger creates a server-side WorkflowRun (no Keycloak
# token, no client cog_ids) and hands off to start_workflow_execution.
# ---------------------------------------------------------------------------

# Per-process tracker of fan-out attempts that exceed the chain-depth cap
# (depth > MAX_CHAIN_DEPTH). Used in tests; not load-bearing in prod.
MAX_CHAIN_DEPTH = 5
CHAIN_DEPTH_EXCEEDED_MESSAGE = "Trigger chain depth exceeded"


def _try_relay_trigger(db, home_zone, trigger_id, extra_inputs,
                       triggered_by_user_id, chain_depth):
    """add-multi-region-mesh — relay a trigger fire to the manager that owns the
    workflow's home region, so it runs where the workflow's data lives. Returns a
    lightweight marker on success, or None if there's no approved owner for that
    region / the peer is unreachable / the peer doesn't have the trigger — in which
    case the caller falls back to running locally (a fire is never silently dropped)."""
    import os
    from types import SimpleNamespace
    from utils.paid_import import optional_paid
    mesh_trust = optional_paid("utils.mesh_trust")
    if mesh_trust is None:
        return None
    import models as _m
    peer = (db.query(_m.Manager)
            .filter(_m.Manager.region == home_zone,
                    _m.Manager.admin_state == "approved",
                    _m.Manager.status == "active",
                    _m.Manager.manager_id != os.getenv("MANAGER_ID", "01m"))
            .first())
    if peer is None or not peer.url:
        return None
    try:
        resp = mesh_trust.mesh_post(
            f"{peer.url.rstrip('/')}/managers/trigger-relay",
            json={"trigger_id": trigger_id, "extra_inputs": extra_inputs,
                  "triggered_by_user_id": triggered_by_user_id, "chain_depth": chain_depth},
            timeout=15)
        if resp.status_code == 404:
            logger.info("trigger %s not present on owner %s — running locally",
                        trigger_id, peer.manager_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        logger.info("trigger %s relayed to owner %s (region %s) run=%s",
                    trigger_id, peer.manager_id, home_zone, data.get("run_id"))
        return SimpleNamespace(id=data.get("run_id"), relayed=True,
                               relayed_to=peer.manager_id)
    except Exception as e:  # noqa: BLE001 — fall back to local, never drop the fire
        logger.warning("trigger relay to %s failed (%s) — running locally", home_zone, e)
        return None


def dispatch_trigger_run(
    db: "Session",
    *,
    workflow,
    triggered_by_user_id: Optional[int] = None,
    triggered_by_trigger_id: Optional[int] = None,
    extra_inputs: Optional[Dict[str, Any]] = None,
    chain_depth: int = 0,
    encrypted_refresh_token: Optional[bytes] = None,
    skip_region_routing: bool = False,
) -> "object":
    """Create a `WorkflowRun` for a trigger-fired execution and enqueue its
    root tasks. Returns the persisted run.

    `chain_depth` is stored on the run when the column exists (after the
    Group A migration lands). When `chain_depth > MAX_CHAIN_DEPTH`, a
    synthetic FAILED run is persisted and orchestration is skipped.

    Caller is responsible for trigger-row updates (`last_triggered_at`,
    `trigger_count`) and try/except containment.
    """
    from models import WorkflowRun, WorkflowVersion, TaskStatus
    from utils.minio_client import get_tenant_bucket_name

    tenant_id = workflow.tenant_id

    # node-run-controls — DAG schedule pause: a paused workflow starts NO new
    # trigger/schedule-fired runs. In-progress runs are unaffected. This is the
    # single choke point for both temporal schedules (via _fire_schedule_trigger)
    # and event/completion triggers, which all funnel through here.
    if getattr(workflow, "schedule_paused", False):
        logger.info(
            "Workflow %s schedule is paused — skipping trigger-fired run (trigger %s)",
            workflow.id, triggered_by_trigger_id,
        )
        return None

    # Find the active version (the one users see in the builder).
    version = (
        db.query(WorkflowVersion)
        .filter(WorkflowVersion.workflow_id == workflow.id)
        .order_by(WorkflowVersion.version_number.desc())
        .first()
    )
    if not version or not version.graph_json:
        raise ValueError(
            f"Workflow {workflow.id} has no active version with graph_json"
        )

    # add-multi-region-mesh — region routing. If this workflow's home region is
    # owned by an approved peer manager (and we're not already a relay target),
    # relay the fire there so it runs where the data lives. Falls back to local
    # if the owner can't take it.
    if not skip_region_routing and triggered_by_trigger_id is not None:
        import os as _os
        home = getattr(workflow, "home_zone", None)
        my_region = _os.getenv("MANAGER_REGION", "")
        if home and my_region and home != my_region:
            relayed = _try_relay_trigger(
                db, home, triggered_by_trigger_id, extra_inputs,
                triggered_by_user_id, chain_depth)
            if relayed is not None:
                return relayed

    if chain_depth > MAX_CHAIN_DEPTH:
        # Persist a synthetic FAILED row so the chain-cap is auditable.
        synthetic = WorkflowRun(
            tenant_id=tenant_id,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            triggered_by_user_id=triggered_by_user_id,
            triggered_by_trigger_id=triggered_by_trigger_id,
            status=TaskStatus.FAILED,
            error_message=CHAIN_DEPTH_EXCEEDED_MESSAGE,
            completed_at=datetime.now(timezone.utc),
            run_name=f"{workflow.name} (chain-depth cap)",
        )
        if hasattr(WorkflowRun, "chain_depth"):
            try:
                synthetic.chain_depth = chain_depth
            except Exception:
                pass
        db.add(synthetic)
        db.commit()
        db.refresh(synthetic)
        logger.warning(
            "Trigger chain depth %d exceeded for workflow %d (tenant %d) — "
            "persisted synthetic FAILED run %d",
            chain_depth, workflow.id, tenant_id, synthetic.id,
        )
        return synthetic

    # GG.8c sealed-storage: a sealed buyer copy stores no graph in its own rows;
    # expand the publisher's listing snapshot server-side (buyer's input-param
    # overrides applied) so trigger-fired runs dispatch the real pipeline.
    # In-memory only — never written back to the buyer's version/nodes.
    from services import sealed_workflow as _sealed_wf
    graph = (
        _sealed_wf.expand_for_run(db, version)
        if _sealed_wf.is_sealed(db, workflow)
        else (version.graph_json or {})
    )
    tenant_bucket = get_tenant_bucket_name(tenant_id, db)

    full_conf = {
        "tenant_id": tenant_id,
        "tenant_bucket": tenant_bucket,
        "nodes": graph.get("nodes", []),
        "edges": graph.get("edges", []),
        "project_info": {
            "tenant_id": tenant_id,
            "tenant_bucket": tenant_bucket,
            "user_id": triggered_by_user_id,
            "cog_ids": [],
            "metadata_cog_ids": [],
            "parent_folder_id": None,
            "workflow_name": workflow.name,
            "extra_variables": dict(extra_inputs or {}),
        },
    }

    db_run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        triggered_by_user_id=triggered_by_user_id,
        triggered_by_trigger_id=triggered_by_trigger_id,
        status=TaskStatus.QUEUED,
        parameters=full_conf,
        encrypted_refresh_token=encrypted_refresh_token,
        run_name=f"{workflow.name} (trigger)",
        # GO.6c: stamp a provisional started_at at enqueue time (before the
        # background dispatch thread below even runs). _start_workflow()
        # overwrites this with the real dispatch timestamp once the thread
        # actually starts, so normal runs are unaffected. But if the thread
        # never runs or dies before reaching that line (e.g. its uvicorn
        # worker process is killed/recreated mid-dispatch), this run would
        # otherwise sit QUEUED forever with started_at=NULL — and
        # services/scheduler.py's `_run_zombie_run_reconcile` filters on
        # `WorkflowRun.started_at.isnot(None)`, so a NULL started_at is
        # structurally invisible to that sweep no matter how long it's been
        # stuck. Pre-stamping closes that gap (workflow_runs 1943/1944/1946
        # were orphaned exactly this way — QUEUED, started_at NULL, zero
        # NodeRun/ApiTaskQueue rows, never reconciled).
        started_at=datetime.now(timezone.utc),
    )
    if hasattr(WorkflowRun, "chain_depth"):
        try:
            db_run.chain_depth = chain_depth
        except Exception:
            pass
    db.add(db_run)
    db.commit()
    db.refresh(db_run)

    full_conf["workflow_run_id"] = db_run.id

    # GC.STW.15b — temporal SCHEDULE fires (services/scheduler.py `_fire_schedule_trigger`)
    # and every other trigger caller funnel through here, and until now they BYPASSED
    # the orchestration queue: the in-process Thread handoff below runs whether or not
    # the `orchestration_via_queue_enabled` platform-settings flag is on. Route the
    # flag-ON case through the SAME helper the Execute route uses
    # (routers/workflows_domains/workflows.py) so the queue row, its tenant scoping and
    # its `mod_orchestration` pg_notify are identical, and let the background role build
    # the plan. The flag read is NOT wrapped in try/except: an unreadable setting must
    # never fail open onto both paths, so a raise propagates to the caller's existing
    # containment instead of silently double-dispatching.
    from services.orchestration_consumer import (
        orchestration_via_queue_enabled,
        enqueue_orchestration_task,
    )
    if orchestration_via_queue_enabled():
        enqueue_orchestration_task(
            db,
            tenant_id=tenant_id,
            workflow_run_id=db_run.id,
            full_conf=full_conf,
        )
        logger.info(
            "Trigger dispatch: workflow %d run %d (tenant %d, depth %d, trigger %s) "
            "enqueued via orchestration queue",
            workflow.id, db_run.id, tenant_id, chain_depth, triggered_by_trigger_id,
        )
        return db_run

    # Hand off orchestration to a background thread so the caller's HTTP
    # response doesn't block on plan-build + task enqueue.
    import threading
    from database import SessionLocal
    threading.Thread(
        target=start_workflow_execution,
        args=(SessionLocal, full_conf, db_run.id),
        daemon=True,
    ).start()

    logger.info(
        "Trigger dispatch: workflow %d run %d (tenant %d, depth %d, trigger %s)",
        workflow.id, db_run.id, tenant_id, chain_depth, triggered_by_trigger_id,
    )
    return db_run


def fire_completion_triggers(db: "Session", source_run) -> None:
    """Fan-out workflow_completion triggers when `source_run` reaches a
    terminal status. MUST NOT raise: a failure here cannot roll back the
    source run's commit.
    """
    from models import (
        WorkflowTrigger,
        WorkflowTriggerType,
        Workflow,
        TaskStatus,
    )

    try:
        terminal = source_run.status
        if terminal not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return

        # fortress-2.1c — the run just terminalized (this is the shared
        # terminal fan-out, called by routers/worker_tasks.py right AFTER
        # the status commit). Seal its signed proof bundle: gated by
        # `compliance.run_proof_bundle_enabled` (default OFF => no-op),
        # fire-and-forget on a daemon thread with its OWN session, so the
        # Vault read + Ed25519 sign never blocks this request path and a
        # seal failure can never roll back the run's committed status.
        # CARVE-U9a: optional_paid binding — absent module = explicit CE skip
        # (witness FAIL-OPEN on the old log-only shape).
        from utils.paid_import import optional_paid
        _rpb = optional_paid("services.run_proof_bundle")
        if _rpb is None:
            logger.debug("SKIPPED (not in this edition): "
                         "run proof bundle seal")
        else:
            try:
                _rpb.seal_run_proof_background(source_run.id)
            except Exception:  # noqa: BLE001 — sealing is best-effort
                logger.debug(
                    "run proof bundle: seal scheduling failed for run %s",
                    source_run.id,
                )

        if _module_available("services.drift_run_hook"):
            try:
                from services.drift_run_hook import schedule_run_fingerprint
                schedule_run_fingerprint(source_run.id)
            except Exception:  # noqa: BLE001 — drift fingerprinting is best-effort
                logger.debug(
                    "drift run hook: fingerprint scheduling failed for run %s",
                    source_run.id,
                )

        # Gate 2.1 (wiring) — a sub_workflow CHILD run just reached a terminal
        # status: complete the parked parent NodeRun and kick its downstream.
        # `terminal` here is already narrowed to COMPLETED/FAILED, which is
        # exactly complete_parent_for_child's own guard. No-op for runs that
        # are not children (no marker). LAZY import: sub_workflow_runtime
        # imports this module lazily, so a top-level import would be circular.
        # EXPR 4.4 slice B — an AGENT-node tool child run just reached terminal:
        # the parked agent node is RE-ENTERED (its loop continues with the child's
        # output as one more tool result), not completed once. resume_agent_for_child
        # returns False for a plain sub_workflow park (no agent_state in outputs),
        # so the completion below still owns those. Wrapped so a resume failure can
        # never break the completion triggers.
        resumed_agent = False
        try:
            from services.agent_tool_resume import resume_agent_for_child
            resumed_agent = resume_agent_for_child(db, source_run)
        except Exception:  # noqa: BLE001 — resume is best-effort
            logger.debug(
                "agent tool resume: re-entry scheduling failed for run %s",
                source_run.id, exc_info=True,
            )

        try:
            if not resumed_agent:
                from services.sub_workflow_runtime import complete_parent_for_child
                complete_parent_for_child(db, source_run)
        except Exception:  # noqa: BLE001 — fan-out is best-effort
            logger.debug(
                "sub_workflow completion: parent completion failed for run %s",
                source_run.id, exc_info=True,
            )

        # Fortress 6.8 (944b) — MPC session completion: mark the session
        # running on this run as completed or failed.  Best-effort: the audit
        # write and status flip must never roll back the run's committed
        # terminal status.  LAZY import: mpc_session has no import path back
        # to this module, but lazy avoids any future circular-import risk.
        try:
            from services.mpc_session import complete_for_run
            complete_for_run(db, source_run.id)
        except Exception:  # noqa: BLE001 — MPC session update is best-effort
            logger.debug(
                "mpc session completion: transition failed for run %s",
                source_run.id, exc_info=True,
            )

        on_status_match = "succeeded" if terminal == TaskStatus.COMPLETED else "failed"

        triggers = (
            db.query(WorkflowTrigger)
            .filter(
                WorkflowTrigger.trigger_type == WorkflowTriggerType.WORKFLOW_COMPLETION,
                WorkflowTrigger.is_active.is_(True),
                WorkflowTrigger.deleted_at.is_(None),
                WorkflowTrigger.tenant_id == source_run.tenant_id,
            )
            .all()
        )

        # Filter in Python: JSONB indexed fetch + Python check on
        # source_workflow_id and on_status. The partial expression index
        # (added by the migration) makes the SQL filter cheap once the
        # WORKFLOW_COMPLETION enum value is queryable.
        depth = getattr(source_run, "chain_depth", 0) or 0

        for trigger in triggers:
            try:
                cfg = trigger.configuration or {}
                src_id = cfg.get("source_workflow_id")
                if int(src_id) != int(source_run.workflow_id):
                    continue
                on_status = cfg.get("on_status", "any")
                if on_status not in ("any", on_status_match):
                    continue

                downstream_workflow = (
                    db.query(Workflow)
                    .filter(
                        Workflow.id == trigger.workflow_id,
                        Workflow.tenant_id == source_run.tenant_id,
                    )
                    .first()
                )
                if not downstream_workflow:
                    continue

                extra_inputs: Dict[str, Any] = {}
                if cfg.get("forward_outputs"):
                    extra_inputs = _collect_forwarded_outputs(
                        db, source_run, downstream_workflow,
                    )
                # §4.3 failure→remediation: give the downstream the failure context.
                if on_status_match == "failed":
                    extra_inputs = dict(extra_inputs)
                    extra_inputs["failure"] = {
                        "source_run_id": source_run.id,
                        "source_workflow_id": source_run.workflow_id,
                        "error_message": getattr(source_run, "error_message", None),
                    }

                dispatch_trigger_run(
                    db,
                    workflow=downstream_workflow,
                    triggered_by_user_id=source_run.triggered_by_user_id,
                    triggered_by_trigger_id=trigger.id,
                    extra_inputs=extra_inputs,
                    chain_depth=depth + 1,
                )

                trigger.last_triggered_at = datetime.now(timezone.utc)
                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                db.commit()

            except Exception as fire_exc:
                logger.exception(
                    "Completion-trigger %d failed for source-run %d: %s",
                    trigger.id, source_run.id, fire_exc,
                )
                # Don't roll back the source-run commit; continue with the
                # next trigger.
                try:
                    db.rollback()
                except Exception:
                    pass

    except Exception as outer_exc:
        logger.exception(
            "fire_completion_triggers failed for source-run %s: %s",
            getattr(source_run, "id", "?"), outer_exc,
        )


def fire_node_completion_triggers(db: "Session", node_run) -> None:
    """add-workflow-triggers §4.2 — fire NODE_COMPLETION triggers when a named node
    in a source workflow run reaches a terminal status ("one process finishes →
    another starts", cross-workflow). MUST NOT raise (can't roll back the commit)."""
    from models import (WorkflowTrigger, WorkflowTriggerType, WorkflowRun, TaskStatus)
    try:
        status = node_run.status
        if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return
        on_status_match = "succeeded" if status == TaskStatus.COMPLETED else "failed"
        run = db.query(WorkflowRun).filter(WorkflowRun.id == node_run.workflow_run_id).first()
        if not run:
            return
        depth = getattr(run, "chain_depth", 0) or 0
        triggers = (db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.NODE_COMPLETION,
            WorkflowTrigger.is_active.is_(True),
            WorkflowTrigger.deleted_at.is_(None),
            WorkflowTrigger.tenant_id == run.tenant_id).all())
        from services.triggers import pipeline
        for trig in triggers:
            cfg = trig.configuration or {}
            if int(cfg.get("source_workflow_id", -1)) != int(run.workflow_id):
                continue
            if int(cfg.get("workflow_node_id", -1)) != int(node_run.workflow_node_id or -1):
                continue
            if cfg.get("on_status", "any") not in ("any", on_status_match):
                continue
            event_ctx = {
                "event_key": f"node:{node_run.id}:{trig.id}",
                "node": {"workflow_node_id": node_run.workflow_node_id,
                         "status": on_status_match, "outputs": node_run.outputs},
                "inputs": {"node_outputs": node_run.outputs},
            }
            try:
                pipeline.evaluate_and_dispatch(
                    db, trig, event_ctx, chain_depth=depth + 1,
                    triggered_by_user_id=run.triggered_by_user_id)
            except Exception:  # noqa: BLE001
                logger.exception("node-completion trigger %s fire failed", trig.id)
                try:
                    db.rollback()
                except Exception:
                    pass

        # brand-retry-to-bar (21.3.s3) — the retry loop rides the SAME terminal-node
        # seam the trigger hook uses, so the Generate + Critique containers stay
        # byte-unchanged and no new worker path is involved. Lazy import: the loop
        # must not appear in this module's import graph (it reaches routers/worker_tasks,
        # which reaches back here). Best-effort by contract, same as triggers above.
        if _module_available("services.brand_retry_to_bar"):
            try:
                from services.brand_retry_to_bar import on_node_completion as _brand_retry_hook
                _brand_retry_hook(db, node_run)
            except Exception:  # noqa: BLE001
                logger.exception("brand retry-to-bar hook failed for node_run %s",
                                 getattr(node_run, "id", "?"))
                try:
                    db.rollback()
                except Exception:
                    pass
    except Exception:  # noqa: BLE001
        logger.exception("fire_node_completion_triggers failed for node_run %s",
                         getattr(node_run, "id", "?"))


# capture-preflight §3.1 — persist the node's report against its run.
_PREFLIGHT_REPORT_BEGIN = "---MOD-PREFLIGHT-REPORT-BEGIN---"
_PREFLIGHT_REPORT_END = "---MOD-PREFLIGHT-REPORT-END---"


def _extract_preflight_report(log_tail: str):
    """Return the parsed report dict from the sentinel-fenced JSON in a task's
    log tail, or None if this task carried no preflight report. The node emits
    the report between _PREFLIGHT_REPORT_BEGIN/END; the tail is both streams
    merged, so we take the LAST fenced block (a retry appends a fresher one)."""
    import json as _json
    if not log_tail or _PREFLIGHT_REPORT_BEGIN not in log_tail:
        return None
    start = log_tail.rfind(_PREFLIGHT_REPORT_BEGIN) + len(_PREFLIGHT_REPORT_BEGIN)
    end = log_tail.find(_PREFLIGHT_REPORT_END, start)
    blob = (log_tail[start:end] if end != -1 else log_tail[start:]).strip()
    if not blob:
        return None
    try:
        obj = _json.loads(blob)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _preflight_output_key(outputs, *suffixes):
    """Best-effort locate a node output whose filename ends with one of
    `suffixes` (e.g. 'database.db', 'sparse.ply') and return its storage key.
    Tolerant of the two shapes node outputs take: a list of {filename, key/
    object_key/path} dicts, or a {filename: key} mapping."""
    if not outputs:
        return None
    def _match(name):
        return isinstance(name, str) and any(name.endswith(s) for s in suffixes)
    try:
        if isinstance(outputs, dict):
            for name, val in outputs.items():
                if _match(name):
                    if isinstance(val, str):
                        return val
                    if isinstance(val, dict):
                        return val.get("object_key") or val.get("key") or val.get("path")
        elif isinstance(outputs, list):
            for entry in outputs:
                if isinstance(entry, dict) and _match(entry.get("filename") or entry.get("name")):
                    return entry.get("object_key") or entry.get("key") or entry.get("path")
    except Exception:  # noqa: BLE001
        return None
    return None


def persist_capture_preflight_report(db: "Session", node_run, result_payload) -> None:
    """capture-preflight §3.1 — on completion of a capture-preflight node, parse
    its report from the task's log tail and upsert a CapturePreflightReport for
    the run so it is queryable (§3.2) without re-parsing the fenced tail, and so
    the preserved `database.db` is discoverable for reuse (§3.3).

    Self-identifying: only tasks whose tail carries the preflight sentinels are
    persisted, so this is a no-op for every other node. MUST NOT raise (runs in
    the commit path)."""
    from models import CapturePreflightReport, WorkflowRun
    try:
        log_tail = (result_payload or {}).get("log_tail") if isinstance(result_payload, dict) else None
        report = _extract_preflight_report(log_tail or "")
        if report is None:
            return  # not a preflight node (or no parseable report)

        run = db.query(WorkflowRun).filter(
            WorkflowRun.id == node_run.workflow_run_id).first()
        if run is None:
            return

        outputs = getattr(node_run, "outputs", None)
        db_key = _preflight_output_key(outputs, "database.db")
        ply_key = _preflight_output_key(outputs, "sparse.ply")

        verdict = str(report.get("verdict") or "error")[:20]
        row = db.query(CapturePreflightReport).filter(
            CapturePreflightReport.workflow_run_id == run.id).first()
        if row is None:
            row = CapturePreflightReport(tenant_id=run.tenant_id, workflow_run_id=run.id)
            db.add(row)
        row.node_run_id = getattr(node_run, "id", None)
        row.verdict = verdict
        row.advisory = bool(report.get("advisory", True))
        row.summary = report.get("summary")
        row.findings = report.get("findings")
        # weak_regions may be nested in a finding or top-level depending on node
        # version; accept either so the highlighted-render points always persist.
        row.weak_regions = report.get("weak_regions") or report.get("weak_points")
        row.thresholds = report.get("thresholds")
        if db_key:
            row.database_object_key = db_key
        if ply_key:
            row.sparse_ply_object_key = ply_key
        db.commit()
        logger.info("capture-preflight: persisted report run=%s verdict=%s db_key=%s",
                    run.id, verdict, bool(db_key))
    except Exception:  # noqa: BLE001
        logger.exception("persist_capture_preflight_report failed for node_run %s",
                         getattr(node_run, "id", "?"))
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def fire_fanin_triggers(db: "Session", source_run) -> None:
    """add-workflow-triggers §4.4 — fan-in/barrier: record this source's arrival;
    dispatch the downstream only once ALL configured sources have arrived within the
    window. Arrivals are stored in `trigger_event_log` (event_key = source id)."""
    from models import (WorkflowTrigger, WorkflowTriggerType, TriggerEventLog, TaskStatus)
    from datetime import datetime, timezone, timedelta
    try:
        terminal = source_run.status
        if terminal not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return
        on_status_match = "succeeded" if terminal == TaskStatus.COMPLETED else "failed"
        triggers = (db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.FAN_IN,
            WorkflowTrigger.is_active.is_(True),
            WorkflowTrigger.deleted_at.is_(None),
            WorkflowTrigger.tenant_id == source_run.tenant_id).all())
        now = datetime.now(timezone.utc)
        for trig in triggers:
            cfg = trig.configuration or {}
            sources = [int(s) for s in (cfg.get("sources") or [])]
            if int(source_run.workflow_id) not in sources:
                continue
            if cfg.get("on_status", "succeeded") not in ("any", on_status_match):
                continue
            window_s = int(cfg.get("window_seconds", 3600))
            cutoff = now - timedelta(seconds=window_s)
            # Record this arrival (one row per source).
            db.add(TriggerEventLog(trigger_id=trig.id, tenant_id=trig.tenant_id,
                                   event_key=f"src:{source_run.workflow_id}",
                                   state={"run_id": source_run.id}))
            db.commit()
            # Which distinct sources have arrived within the window?
            rows = (db.query(TriggerEventLog.event_key).filter(
                TriggerEventLog.trigger_id == trig.id,
                TriggerEventLog.fired_at >= cutoff,
                TriggerEventLog.event_key.like("src:%")).all())
            arrived = {r[0] for r in rows}
            needed = {f"src:{s}" for s in sources}
            if not needed.issubset(arrived):
                continue  # barrier not satisfied yet
            # All arrived → dispatch once, then clear the arrivals so it re-arms.
            depth = getattr(source_run, "chain_depth", 0) or 0
            from services.triggers import pipeline
            try:
                res = pipeline.evaluate_and_dispatch(
                    db, trig, {"event_key": f"fanin:{trig.id}:{int(now.timestamp())}",
                               "inputs": {"fan_in_sources": sources}},
                    chain_depth=depth + 1,
                    triggered_by_user_id=source_run.triggered_by_user_id)
                if res.get("dispatched"):
                    db.query(TriggerEventLog).filter(
                        TriggerEventLog.trigger_id == trig.id,
                        TriggerEventLog.event_key.like("src:%")).delete(
                        synchronize_session=False)
                    db.commit()
            except Exception:  # noqa: BLE001
                logger.exception("fan-in trigger %s dispatch failed", trig.id)
                try:
                    db.rollback()
                except Exception:
                    pass
    except Exception:  # noqa: BLE001
        logger.exception("fire_fanin_triggers failed for source-run %s",
                         getattr(source_run, "id", "?"))


def _collect_forwarded_outputs(
    db: "Session", source_run, downstream_workflow,
) -> Dict[str, Any]:
    """Collect terminal-node outputs from `source_run` and intersect them
    with `downstream_workflow`'s current input port names. Mismatched keys
    are logged at INFO and dropped.
    """
    from models import NodeRun, WorkflowNode, WorkflowVersion, TaskStatus

    try:
        # Inputs of the downstream workflow's active version.
        version = (
            db.query(WorkflowVersion)
            .filter(WorkflowVersion.workflow_id == downstream_workflow.id)
            .order_by(WorkflowVersion.version_number.desc())
            .first()
        )
        if not version or not version.graph_json:
            return {}

        inputs = set()
        for node in (version.graph_json or {}).get("nodes", []):
            for port in (node.get("data", {}).get("inputs") or []):
                name = port.get("name") if isinstance(port, dict) else None
                if name:
                    inputs.add(name)

        # Outputs of the source run's terminal nodes.
        terminal_runs = (
            db.query(NodeRun)
            .filter(
                NodeRun.workflow_run_id == source_run.id,
                NodeRun.status == TaskStatus.COMPLETED,
            )
            .all()
        )

        flattened: Dict[str, Any] = {}
        for nr in terminal_runs:
            outputs = getattr(nr, "outputs", None) or getattr(nr, "result", None)
            if isinstance(outputs, dict):
                flattened.update(outputs)

        forwarded: Dict[str, Any] = {}
        dropped = []
        for key, value in flattened.items():
            if key in inputs:
                forwarded[key] = value
            else:
                dropped.append(key)
        if dropped:
            logger.info(
                "Completion forwarding dropped %d unmatched output key(s) for "
                "downstream workflow %d: %s",
                len(dropped), downstream_workflow.id, dropped,
            )
        return forwarded

    except Exception as e:
        logger.warning("Output forwarding failed: %s — falling back to defaults", e)
        return {}


# ---------------------------------------------------------------------------
# GO.6e — durable trigger dispatch (stranded trigger-run re-dispatch)
# ---------------------------------------------------------------------------
#
# dispatch_trigger_run commits a QUEUED WorkflowRun, then hands the root-task
# dispatch to a daemon thread (start_workflow_execution). If the uvicorn
# worker recycles before that thread runs, the run is orphaned: QUEUED, zero
# NodeRuns, zero ApiTaskQueue rows. GO.6c's provisional started_at makes it
# visible to _run_zombie_run_reconcile (which FINALIZES it ~30 min later),
# but the run never actually runs. This sweep re-drives the stranded runs
# with the SAME dispatch the thread would have done, so they run instead of
# being reaped.

REDISPATCH_MARKER_KEY = "_trigger_redispatch"


def _claim_stranded_run(db: "Session", run_id: int,
                        expected_started_at: "datetime") -> bool:
    """Atomically claim a stranded run by compare-and-swap.

    UPDATE ... WHERE id=run_id AND status='queued' AND
    started_at=<expected_started_at> — the value the CALLER read during its
    candidate scan. The winner re-stamps started_at (so the loser's stale
    read can never match) and returns True; a lost claim returns False.

    Status alone is not a sufficient token: two overlapping sweeps (the
    boot sweep runs in every uvicorn worker, and the scheduler job can
    tick at the same time) can both scan the same QUEUED run before either
    commits. The loser's UPDATE then blocks on the row lock, re-evaluates
    the WHERE against the winner's committed row — whose status is still
    QUEUED because the winner's dispatch thread hasn't flipped it yet —
    and would otherwise claim the run a second time. Matching on the
    started_at the caller read closes that gap (no new column:
    started_at is the CAS value and the re-stamp doubles as the
    re-queued grace window).
    """
    now = datetime.now(timezone.utc)
    import models
    claimed = (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == run_id,
                models.WorkflowRun.status == models.TaskStatus.QUEUED,
                models.WorkflowRun.started_at == expected_started_at)
        .update({models.WorkflowRun.started_at: now},
                synchronize_session=False)
    )
    return bool(claimed)


def sweep_stranded_trigger_runs(db: "Session") -> Dict[str, int]:
    """Re-dispatch trigger-fired runs whose dispatch thread died before
    enqueuing anything.

    A run is stranded when it is QUEUED, has zero NodeRuns, has zero
    ApiTaskQueue rows, is NOT capacity-queued (LDF-4 §7 — those are waiting
    on plan.max_concurrent_processes, not on a dead thread), and whose
    started_at is older than the grace window. The dispatch conf is rebuilt
    from the run's `parameters` (the same full_conf dispatch_trigger_run
    built — `workflow_run_id` is restored here), and the SAME
    start_workflow_execution entry point runs in a fresh daemon thread, so
    normal-run semantics (status flip, NodeRun/task creation, failure
    handling) are untouched.

    Double-dispatch guard: each claim is a compare-and-swap
    (_claim_stranded_run) — UPDATE ... WHERE id=... AND status=QUEUED AND
    started_at=<the value this pass read>. Status alone is NOT a
    sufficient token: two overlapping sweeps (the boot sweep runs in
    every uvicorn worker; the scheduler job can tick at the same time)
    can both scan the same QUEUED run before either commits, the loser's
    UPDATE blocks on the row lock and then re-evaluates the WHERE against
    the winner's committed row — whose status is still QUEUED because the
    winner's dispatch thread hasn't flipped it yet. The winner's claim
    re-stamps started_at, so the loser's stale read can never match
    (the re-stamp also re-queues the grace window). The attempt counter
    lives in the run's `parameters` (`_trigger_redispatch` marker, no new
    column) and is capped; after the cap the run is left for the zombie
    reconcile to finalize as FAILED.

    Returns {"re_dispatched": n, "capped": n}.
    """
    from datetime import timedelta
    import threading
    import models
    from database import SessionLocal
    from services import platform_settings as _psr

    try:
        enabled = bool(_psr.get_setting(
            "orchestration.trigger_run_redispatch_enabled", db=db))
    except Exception:  # noqa: BLE001 — registry miss must not kill the sweep
        enabled = True
    if not enabled:
        return {"re_dispatched": 0, "capped": 0}

    def _as_int(key: str, fallback: int) -> int:
        try:
            return max(1, int(_psr.get_setting(key, db=db)))
        except Exception:  # noqa: BLE001
            return fallback

    grace_min = _as_int(
        "orchestration.trigger_run_redispatch_grace_minutes", 3)
    max_attempts = _as_int(
        "orchestration.trigger_run_redispatch_max_attempts", 2)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_min)

    try:
        candidates = (
            db.query(models.WorkflowRun)
            .filter(models.WorkflowRun.status == models.TaskStatus.QUEUED,
                    models.WorkflowRun.started_at.isnot(None),
                    models.WorkflowRun.started_at <= cutoff,
                    models.WorkflowRun.deleted_at.is_(None),
                    ~models.WorkflowRun.is_capacity_queued.is_(True))
            .limit(200)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("trigger_run_redispatch: candidate scan failed: %s", e)
        return {"re_dispatched": 0, "capped": 0}

    re_dispatched = 0
    capped = 0
    for run in candidates:
        try:
            if (db.query(models.NodeRun.id)
                    .filter(models.NodeRun.workflow_run_id == run.id)
                    .first()) is not None:
                continue
            if (db.query(models.ApiTaskQueue.id)
                    .filter(models.ApiTaskQueue.workflow_run_id == run.id)
                    .first()) is not None:
                continue

            marker = {}
            params = run.parameters
            if isinstance(params, dict):
                m = params.get(REDISPATCH_MARKER_KEY)
                if isinstance(m, dict):
                    marker = m
            attempts = marker.get("attempts", 0)
            try:
                attempts = int(attempts)
            except (TypeError, ValueError):
                attempts = 0
            if attempts >= max_attempts:
                if capped == 0:
                    logger.info(
                        "trigger_run_redispatch: run %s hit the %d-attempt "
                        "cap — leaving it for the zombie-run reconcile",
                        run.id, max_attempts,
                    )
                capped += 1
                continue

            # Claim: compare-and-swap on the started_at this pass read.
            # rowcount 0 means someone else (the dead thread's successor,
            # a user action, or an overlapping sweep pass) already moved
            # the run — skip. The CAS (see _claim_stranded_run) is what
            # makes this safe when two sweeps overlap.
            now = datetime.now(timezone.utc)
            if not _claim_stranded_run(db, run.id, run.started_at):
                continue
            # Rebuild the marker on a FRESH copy of parameters: the ORM
            # dict attribute may already hold an `execution_plan` entry if a
            # previous re-drive's thread ran part-way.
            params = dict(run.parameters) if isinstance(run.parameters, dict) \
                else {}
            params[REDISPATCH_MARKER_KEY] = {
                "attempts": attempts + 1,
                "last_attempt_at": now.isoformat(),
                "source": "sweep_stranded_trigger_runs",
            }
            run.parameters = params

            conf = dict(params)
            conf.pop(REDISPATCH_MARKER_KEY, None)
            conf["workflow_run_id"] = run.id

            threading.Thread(
                target=start_workflow_execution,
                args=(SessionLocal, conf, run.id),
                daemon=True,
            ).start()

            try:
                db.add(models.AuditEvent(
                    tenant_id=getattr(run, "tenant_id", None),
                    user_id=None,
                    event_name="WORKFLOW_RUN_TRIGGER_REDISPATCHED",
                    details={"workflow_run_id": run.id,
                             "attempt": attempts + 1,
                             "max_attempts": max_attempts,
                             "grace_minutes": grace_min},
                ))
            except Exception:  # noqa: BLE001 — audit is best-effort
                logger.debug("trigger_run_redispatch: audit append failed "
                             "for run %s", run.id, exc_info=True)
            db.commit()
            re_dispatched += 1
            logger.warning(
                "trigger_run_redispatch: re-driven stranded trigger run %s "
                "(workflow %s, tenant %s, attempt %d/%d, queued %s)",
                run.id, run.workflow_id, run.tenant_id, attempts + 1,
                max_attempts, run.started_at,
            )
        except Exception as e:  # noqa: BLE001 — one bad run never blocks the sweep
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("trigger_run_redispatch: run %s raised: %s",
                           getattr(run, "id", "?"), e)

    # EXPR 4.4 slice C — zombie guard for PARKED AGENT nodes. A parked agent
    # NodeRun (PENDING_ACTION + outputs.agent_state + CHILD_MARKER) waits on its
    # tool CHILD run; the child's terminal fan-out (fire_completion_triggers)
    # re-enters the loop through services/agent_tool_resume.resume_agent_for_child.
    # If that thread died (uvicorn recycle, same shape GO.6c protects against),
    # the node sits PENDING_ACTION forever and the zombie-run reconcile can never
    # reach it: its `live_node` check counts PENDING_ACTION as "may still
    # progress", so the run is left RUNNING with a node that will never move.
    # Re-drive the SAME resume entry here — its atomic PENDING_ACTION -> RUNNING
    # claim makes a double resume impossible, so a second sweep pass (or a
    # pass racing the live fan-out) is a no-op.
    try:
        resumed = _resume_zombie_parked_agent_nodes(
            db, grace_min=grace_min, cutoff=cutoff)
        if resumed:
            logger.warning("trigger_run_redispatch: zombie-resumed %d parked "
                           "agent node(s) whose tool child is terminal", resumed)
    except Exception as e:  # noqa: BLE001 — the guard never breaks the sweep
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("trigger_run_redispatch: parked-agent zombie guard "
                       "raised: %s", type(e).__name__)

    if re_dispatched:
        logger.warning("trigger_run_redispatch: re-dispatched %d stranded "
                       "trigger run(s) (%d at cap)", re_dispatched, capped)
    return {"re_dispatched": re_dispatched, "capped": capped}


# ---------------------------------------------------------------------------
# EXPR 4.4 slice C — zombie guard for PARKED AGENT nodes
# ---------------------------------------------------------------------------
#
# A parked agent NodeRun (PENDING_ACTION + outputs.agent_state + CHILD_MARKER)
# waits on its tool CHILD run, and the child's terminal fan-out re-enters the
# loop through services/agent_tool_resume.resume_agent_for_child. When that
# resume thread dies (uvicorn worker recycle mid-dispatch — the same shape GO.6c
# pre-stamps started_at for), the node sits PENDING_ACTION forever: the zombie-
# run reconcile's `live_node` check counts PENDING_ACTION as "may still
# progress", so it never finalizes the run either. This branch re-drives the
# SAME resume entry, whose atomic PENDING_ACTION -> RUNNING claim makes a double
# resume impossible (a second sweep pass, or one racing the live fan-out, is a
# no-op False). A parked node WITHOUT agent_state is a plain sub_workflow park
# and stays owned by complete_parent_for_child — never touched here.
def _resume_zombie_parked_agent_nodes(db: "Session", *, grace_min: int,
                                      cutoff: "datetime") -> int:
    """Re-resume parked AGENT nodes whose tool child is already terminal.

    Returns the number of nodes the resume entry actually re-claimed (its own
    atomic claim decides; a lost race counts zero). Never raises per-candidate:
    one bad row never breaks the sweep.
    """
    from models import NodeRun, TaskStatus, WorkflowRun
    from services.agent_tool_resume import (
        CHILD_MARKER, AGENT_STATE_KEY, resume_agent_for_child,
    )

    rows = (
        db.query(NodeRun)
        .filter(NodeRun.status == TaskStatus.PENDING_ACTION,
                NodeRun.started_at.isnot(None),
                NodeRun.started_at <= cutoff)
        .limit(200).all()
    )
    resumed = 0
    for nr in rows:
        try:
            outputs = nr.outputs
            if not isinstance(outputs, dict):
                continue
            if AGENT_STATE_KEY not in outputs:
                continue  # plain sub_workflow park — not ours
            child_id = outputs.get(CHILD_MARKER)
            if not isinstance(child_id, int):
                continue
            child = db.get(WorkflowRun, child_id)
            if child is None:
                continue
            if child.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                continue  # the child is still working — not a zombie
            if resume_agent_for_child(db, child):
                resumed += 1
        except Exception:  # noqa: BLE001 — one row never breaks the sweep
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return resumed


# ---------------------------------------------------------------------------
# BUG.B85 — ghost-run reconcile (the self-heal half of the fix)
# ---------------------------------------------------------------------------
#
# A GHOST run is a WorkflowRun sitting RUNNING with ZERO NodeRuns and ZERO
# ApiTaskQueue rows: it dispatched nothing, so every started_at-keyed rescuer
# is blind to it and it blocks restart drains ("is the system busy") forever.
# Owner evidence (2026-10-09): run 2202 (workflow 1, hourly trigger) started
# 12:00:19 with 0 node_runs / 0 api_task_queue rows; the zombie reconcile
# finalized it at 12:38:57 with verdict 'completed' (audit 1505160) — a FALSE
# success for a run that executed nothing. Runs 2200/2201/2203 were the same
# shape; workflow 1 had NO WorkflowNode rows at all until row 160 was created
# ~15:00, which is why the `_wn_row_count > 0` carve-out in the BUG.B85a
# orphan guard let them through (that carve-out is closed at the root above).
#
# This sweep is the belt-and-braces half: it catches ghosts from ANY path
# (not just the orchestrator's), so a shape we have not yet root-caused still
# self-heals. It finalizes FAILED with an honest error_message + audit row,
# never COMPLETED (a ghost ran nothing).
#
# The flip goes through crud/workflows_domain.conditional_cancel_run — an
# atomic UPDATE ... WHERE status in (queued, running) — so a run that just
# gained NodeRuns (or already terminalized in another uvicorn worker / the
# scheduler's own reconcile) loses the race and is left untouched: `.rowcount`
# tells us whether our flip actually applied, and we only audit when it did.
GHOST_RUN_ENABLED_SETTING = "orchestrator.ghost_run_reconcile_enabled"
GHOST_RUN_MINUTES_SETTING = "orchestrator.ghost_run_reconcile_minutes"
GHOST_RUN_MINUTES_DEFAULT = 15
GHOST_RUN_MINUTES_FLOOR = 5
GHOST_RUN_MINUTES_CEIL = 1440
GHOST_RUN_ERROR_MESSAGE = "orphaned: RUNNING with no node runs"
GHOST_RUN_AUDIT_EVENT = "WORKFLOW_RUN_GHOST_RECONCILED"


def _validate_ghost_run_minutes(value: int) -> None:
    """Registry validator for the grace window (5..1440 minutes)."""
    if not isinstance(value, int) or isinstance(value, bool) \
            or not GHOST_RUN_MINUTES_FLOOR <= value <= GHOST_RUN_MINUTES_CEIL:
        raise ValueError(
            f"{GHOST_RUN_MINUTES_SETTING} must be an int in "
            f"{GHOST_RUN_MINUTES_FLOOR}..{GHOST_RUN_MINUTES_CEIL}"
        )


# Registered at import (same shape as services/egress_denial_metrics.py).
# No env fallback: behaviour switches belong in the platform-settings
# registry, not in the environment.
from services import platform_settings as _psr

_psr.register(
    GHOST_RUN_ENABLED_SETTING,
    bool,
    True,
    group="orchestrator",
    description="BUG.B85 — self-heal for ghost workflow runs: finalize a run "
                "that sits RUNNING with ZERO NodeRuns and ZERO ApiTaskQueue "
                "rows as FAILED (error_message 'orphaned: RUNNING with no "
                "node runs') plus an audit row, instead of leaving it "
                "stranded forever (default ON). A run that has NodeRuns, or "
                "that already terminalized, is never touched.",
)
_psr.register(
    GHOST_RUN_MINUTES_SETTING,
    int,
    GHOST_RUN_MINUTES_DEFAULT,
    group="orchestrator",
    validator=_validate_ghost_run_minutes,
    description="BUG.B85 — age window (minutes) past which a ghost run "
                "(RUNNING, zero NodeRuns, zero ApiTaskQueue rows) is "
                "finalized FAILED by reconcile_ghost_runs. "
                f"{GHOST_RUN_MINUTES_FLOOR}..{GHOST_RUN_MINUTES_CEIL}, "
                f"default {GHOST_RUN_MINUTES_DEFAULT}.",
)


def reconcile_ghost_runs(db: "Session") -> Dict[str, int]:
    """BUG.B85 self-heal — finalize ghost runs (RUNNING, nothing dispatched).

    A run is a GHOST when it is RUNNING, older than the grace window, has
    ZERO NodeRun rows and ZERO ApiTaskQueue rows. Nothing was ever
    dispatchable, so no rescuer keyed on NodeRuns/tasks can ever reach it and
    it poisons every restart-drain busy-check for the rest of its life.

    Deliberately narrower than services/scheduler._run_zombie_run_reconcile:
    that sweep finalizes runs whose NodeRuns are ALL terminal (a real run that
    finished late) and may mark them COMPLETED. A ghost ran NOTHING, so it is
    always FAILED — marking it COMPLETED is the false success the owner saw on
    run 2202 (audit 1505160, verdict 'completed').

    The flip is crud/workflows_domain.conditional_cancel_run: an atomic
    UPDATE ... WHERE status in (queued, running). We re-scan the child rows
    immediately before it, and only audit when the UPDATE actually applied
    (`.rowcount`), so a run that terminalized in another uvicorn worker — or
    that just gained its NodeRuns — loses the race and stays untouched.

    Returns {"scanned": n, "reconciled": n}.
    """
    from datetime import timedelta
    import models
    from crud.workflows_domain import conditional_cancel_run

    try:
        enabled = bool(_psr.get_setting(GHOST_RUN_ENABLED_SETTING, db=db))
    except Exception:  # noqa: BLE001 — registry miss must not kill the sweep
        enabled = True
    if not enabled:
        return {"scanned": 0, "reconciled": 0}

    try:
        grace_min = int(_psr.get_setting(GHOST_RUN_MINUTES_SETTING, db=db))
    except Exception:  # noqa: BLE001
        grace_min = GHOST_RUN_MINUTES_DEFAULT
    grace_min = max(GHOST_RUN_MINUTES_FLOOR,
                    min(GHOST_RUN_MINUTES_CEIL, grace_min))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_min)

    candidates = (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.status == models.TaskStatus.RUNNING,
                models.WorkflowRun.deleted_at.is_(None),
                models.WorkflowRun.started_at.isnot(None),
                # LDF-4 §7 — capacity-queued runs wait on
                # plan.max_concurrent_processes; they are not ghosts (same
                # guard as sweep_stranded_trigger_runs and the scheduler's
                # zombie reconcile orphan branch).
                ~models.WorkflowRun.is_capacity_queued.is_(True),
                models.WorkflowRun.started_at <= cutoff)
        .limit(200).all()
    )
    reconciled = 0
    for run in candidates:
        if (db.query(models.NodeRun.id)
            .filter(models.NodeRun.workflow_run_id == run.id)
            .first()) is not None:
            continue  # not a ghost — it dispatched work
        if (db.query(models.ApiTaskQueue.id)
            .filter(models.ApiTaskQueue.workflow_run_id == run.id)
            .first()) is not None:
            continue  # not a ghost — it has (or had) tasks
        flipped = conditional_cancel_run(
            db, run.id,
            completed_at=datetime.now(timezone.utc),
            error_message=GHOST_RUN_ERROR_MESSAGE,
        )
        if not getattr(flipped, "rowcount", 0):
            continue  # lost the race: another path already terminalized it
        logger.warning(
            "ghost_run_reconcile: run %s RUNNING with no node runs -> FAILED",
            run.id,
        )
        try:
            db.add(models.AuditEvent(
                tenant_id=getattr(run, "tenant_id", None),
                user_id=None,
                event_name=GHOST_RUN_AUDIT_EVENT,
                details={"workflow_run_id": run.id,
                         "verdict": models.TaskStatus.FAILED.value,
                         "grace_minutes": grace_min,
                         "node_runs": 0, "api_tasks": 0},
            ))
        except Exception:  # noqa: BLE001 — audit is best-effort
            logger.debug("ghost_run_reconcile: audit append failed for run %s",
                         run.id, exc_info=True)
        reconciled += 1
    if reconciled:
        db.commit()
        logger.warning("ghost_run_reconcile: finalized %d ghost run(s) FAILED",
                       reconciled)
    return {"scanned": len(candidates), "reconciled": reconciled}
