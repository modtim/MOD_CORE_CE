"""Calibration as a pinned two-node system workflow.

    [calibration_input_generator]  ->  [node under test]    <- only this is timed

Replaces the seeded-fixture model. Calibration used to resolve its input from a
pre-seeded object at `tenant-{id}-files/calibration-data/{tier}/sample.{ext}`.
That fixture set had to be created by hand, kept in step with the catalog's
formats, and survive every environment reset — it failed all three. The fixtures
were seeded manually in May 2026, lost on a reset, and never recreated, after
which every file-consuming calibration task failed on download.

Generating the input as node 1 of the run deletes the whole problem class: no
bucket, no path contract, no `{input_type}` mismatch, no seeding step, nothing
that can silently vanish.

Dispatch copies the hardware/accelerator benchmark pattern, which already does
exactly this shape in production:

  * a WorkflowRun carrying `execution_plan` and `pinned_worker`
  * a root ApiTaskQueue row created ASSIGNED to a specific worker
  * downstream nodes dispatched ASSIGNED to the same worker by
    `routers/worker_tasks.py`, which reads the pin off the run

Co-location is the point. Node 1 and node 2 MUST run on the same worker or the
measurement is meaningless — the input would be produced on one box and consumed
on another, and the timing would include a transfer that a real run never pays.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from utils.minio_client import get_tenant_bucket_name
from utils.registries import platform_registry

logger = logging.getLogger(__name__)


# Must stay in step with GENERATORS in
# CORE/DockerContainers/CalibrationInputGenerator/generate.py. The preflight
# below is the only thing standing between a node whose format we cannot
# produce and a task that is certain to fail, so drift here is expensive.
SUPPORTED_FORMATS = frozenset({
    "png", "jpg", "jpeg",
    "obj", "ply",
    "wav", "mp3",
    "json", "xlsx",
    "ifc", "step", "stp", "usd",
    "exr", "mp4",
})

SIZE_TIERS = ("small", "medium", "large")

GENERATOR_NODE_KIND = "calibration_input_generator"


def generator_image() -> str:
    registry = platform_registry()
    return os.getenv(
        "CALIBRATION_GENERATOR_IMAGE",
        f"{registry}/modtim/calibrationinputgenerator:latest",
    )


def node_input_format(node_def) -> Optional[str]:
    """The format a node's calibration input must be, or None when the node
    consumes no file.

    Mirrors the old `_calibration_input_for_node()` rule — first entry of
    `accepted_formats` — because that is what the node actually receives at
    runtime. `accepted_formats` may be a scalar/null on nodes that take no
    input, which is exactly the 5 nodes that ever calibrated successfully
    under the old model."""
    formats = getattr(node_def, "accepted_formats", None)
    if not isinstance(formats, list) or not formats:
        return None
    first = formats[0]
    if not isinstance(first, str) or not first.strip():
        return None
    return first.strip().lstrip(".").lower()


def preflight(node_def) -> dict:
    """Can this node be calibrated at all?

    Returns {"ok": bool, "reason": str|None, "format": str|None}. A node needing
    no input passes with format None — it simply gets a single-node run."""
    fmt = node_input_format(node_def)
    if fmt is None:
        return {"ok": True, "reason": None, "format": None}
    if fmt not in SUPPORTED_FORMATS:
        return {
            "ok": False,
            "format": fmt,
            "reason": (
                f"no calibration input generator for format '{fmt}' "
                f"(node {node_def.id} '{node_def.name}')"
            ),
        }
    return {"ok": True, "reason": None, "format": fmt}


def run_suffix(run_kind: str, tier: str, run_index: int,
               stressor_kinds: Optional[List[str]] = None,
               k8s_node: Optional[str] = None) -> str:
    """Stable, unique identity for one run within a node's 84-run matrix.

    Order-independent in the stressor set so a multi pair is the same run
    however it is spelled. ``k8s_node`` (18.4 / GI.4) keeps a per-k8s-node
    pinned run from colliding with the docker matrix's same-kind/same-tier
    run — the k8s runs are auxiliary measurements and must never look like
    the matrix run of the same suffix (or the matrix resume logic skips
    them / re-dispatches them)."""
    parts = [run_kind, tier, str(run_index)]
    if stressor_kinds:
        parts.append("+".join(sorted(stressor_kinds)))
    if k8s_node:
        parts.append(f"k8s-{k8s_node}")
    return "-".join(parts)


def build_graph(node_def, tier: str, fmt: Optional[str],
                container_env: Optional[dict] = None,
                run_kind: str = "isolation",
                stressor_kinds: Optional[List[str]] = None,
                calibration_pair_id: Optional[str] = None,
                run_index: int = 0,
                k8s_node: Optional[str] = None) -> tuple:
    """(nodes, edges) for one calibration run.

    Two nodes when the node consumes a file, one when it doesn't. The generator
    uploads its output so the worker's normal parent-output resolution feeds it
    to the node under test as `source_file_location`.

    ``k8s_node`` (18.4 / GI.4) pins the run to one k8s node: the node name
    rides on the FOCAL node's data as ``k8s_calibration_node`` so it ends up
    on the focal task's payload (the downstream dispatcher promotes
    calibration markers from node_data), where both the claim path
    (``k8s_cluster_registry.build_k8s_gpu`` — hostname nodeSelector) and the
    results path (``scheduler.pattern_updater`` — the per-node duration) read
    it.
    """
    from routers.node_definitions import _qualify_image_ref

    queue = node_def.process_type or "CPU"
    nodes: List[dict] = []
    edges: List[dict] = []

    # Ids must be unique per RUN, not per tier: a node has 84 runs and
    # `calibrate-{id}-{tier}` collides across all of them, making per-task
    # tracing impossible and resume-detection unusable. Same collision class
    # that made 10 multi-stressor tasks share one id in the old dispatcher.
    # 18.4: k8s-pinned runs also vary by node so a per-node run is never
    # mistaken for the docker matrix run of the same (kind, tier, index).
    suffix = run_suffix(run_kind, tier, run_index, stressor_kinds,
                        k8s_node=k8s_node)

    gen_id = None
    if fmt is not None:
        gen_id = f"calgen-{node_def.id}-{suffix}"
        nodes.append({
            "id": gen_id,
            "data": {
                "node_kind": GENERATOR_NODE_KIND,
                "processing_image": generator_image(),
                # The generator is pure CPU work regardless of what the node
                # under test needs — never hold a GPU to write a sample file.
                "process_type": "CPU",
                "container_env": {
                    "CALIBRATION_FORMAT": fmt,
                    "CALIBRATION_TIER": tier,
                },
                "gpu_required": False,
                "container_timeout": int(
                    os.getenv("CALIBRATION_GENERATOR_TIMEOUT", "900")),
                # Nothing to fetch; its output must be uploaded so the node
                # under test can consume it.
                "skip_download": True,
                "skip_upload": False,
            },
        })

    focal_id = f"calibrate-{node_def.id}-{suffix}"
    focal_env = dict(container_env or {})
    focal_env.setdefault("CALIBRATION_RUN", "true")
    focal_env.setdefault("SIZE_TIER", tier)

    nodes.append({
        "id": focal_id,
        "data": {
            "node_kind": node_def.node_kind,
            "processing_image": _qualify_image_ref(
                node_def.executable_path, node_def),
            "process_type": queue,
            "container_env": focal_env,
            "gpu_required": bool(queue and queue.upper() not in ("CPU", "DEFAULT")),
            "container_timeout": node_def.container_timeout or 900,
            # Input arrives from the generator when there is one.
            "skip_download": fmt is None,
            # We measure the node; its output is not the product.
            "skip_upload": True,
            # Calibration markers MUST ride on the focal task, not just the
            # root. `_sweep_completed_nodes` and the failure brake both select
            # on task_payload->>'calibration_run', and `_determine_size_tier`
            # reads size_tier off the payload. Without these the ONLY task
            # counted for a node is the generator — so a node's matrix would be
            # judged complete on generator outcomes while the node under test
            # went unmeasured, and every measurement would be filed under
            # size_tier 'any' with all three tiers collapsed into one bucket.
            "calibration_run": True,
            "size_tier": tier,
            "node_definition_id": node_def.id,
            # run_kind and pair id must ride on the FOCAL task too. Landing
            # them only on the generator means a contention measurement is
            # indistinguishable from an isolation one on the task actually
            # being measured — the exact mislabelling this design exists to
            # prevent, inverted.
            "calibration_run_kind": run_kind,
            "stressor_kinds": list(stressor_kinds or []),
            "calibration_pair_id": calibration_pair_id,
            # 18.4 (GI.4): the target k8s node for a pinned run — promoted to
            # the focal task payload by the downstream dispatcher, read by
            # build_k8s_gpu (hostname nodeSelector) and the pattern updater
            # (per-node duration). Absent on the docker matrix runs.
            **({"k8s_calibration_node": k8s_node} if k8s_node else {}),
        },
    })

    if gen_id is not None:
        edges.append({"source": gen_id, "target": focal_id})

    return nodes, edges


def dispatch_calibration_run(
    db: Session,
    node_def,
    tenant_id: int,
    worker_name: str,
    tier: str,
    run: int = 0,
    run_kind: str = "isolation",
    stressor_kinds: Optional[List[str]] = None,
    calibration_pair_id: Optional[str] = None,
    k8s_node: Optional[str] = None,
):
    """Create and dispatch one pinned calibration workflow run.

    Returns the WorkflowRun, or None when preflight refuses the node.

    ``k8s_node`` (18.4 / GI.4) — pin this run to one k8s node: the name is
    stamped on the run parameters, the root task payload and the focal
    node's data so the k8s Job the backend submits lands on that node
    (``kubernetes.io/hostname`` nodeSelector) and the measured duration is
    filed under it.
    """
    from models import ApiTaskQueue, ApiTaskStatus, TaskStatus, WorkflowRun
    from services.workflow_orchestrator import build_execution_plan

    if tier not in SIZE_TIERS:
        raise ValueError(f"tier must be one of {SIZE_TIERS}, got {tier!r}")

    check = preflight(node_def)
    if not check["ok"]:
        logger.warning("calibration preflight refused: %s", check["reason"])
        return None

    fmt = check["format"]
    stressors = list(stressor_kinds or [])

    nodes, edges = build_graph(
        node_def, tier, fmt,
        container_env={
            "CALIBRATION_RUN_KIND": run_kind,
            "STRESSOR_KINDS": ",".join(stressors),
        },
        run_kind=run_kind,
        stressor_kinds=stressors,
        calibration_pair_id=calibration_pair_id,
        run_index=run,
        k8s_node=k8s_node,
    )
    plan = build_execution_plan(nodes, edges)

    wf_run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=None,
        workflow_version_id=None,
        status=TaskStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
        orchestrator_run_id=f"calibration_{node_def.id}_{uuid.uuid4().hex[:8]}",
        parameters={
            "execution_plan": plan,
            # General pin name; worker_tasks.py reads this first and falls back
            # to the benchmark-era "profiling_worker".
            "pinned_worker": worker_name,
            "is_calibration": True,
            "node_definition_id": node_def.id,
            "size_tier": tier,
            "run_index": run,
            "calibration_run_kind": run_kind,
            "stressor_kinds": stressors,
            # Correlates this run with the stressor run that accompanies it.
            # Verification, not labelling: a stressor that fails to stress
            # produces a result identical to isolation, and without this link
            # "stressor ran hard, node unaffected" and "stressor never started"
            # are indistinguishable.
            "calibration_pair_id": calibration_pair_id,
            # 18.4 (GI.4): target k8s node for a pinned run — source of truth
            # the claim path and the results path fall back to when a task's
            # own payload does not carry the pin.
            **({"k8s_calibration_node": k8s_node} if k8s_node else {}),
        },
    )
    db.add(wf_run)
    db.flush()

    root = nodes[0]
    root_data = root["data"]
    root_payload = {
        "node_kind": root_data["node_kind"],
        "processing_image": root_data["processing_image"],
        "container_env": root_data["container_env"],
        "source_file_location": "",
        # WS4.Q17.12 — tenant-scoped task: must match the worker
        # credentials path (worker_tasks resolve_backend), i.e. the
        # tenant's effective bucket, not the house format.
        "minio_bucket": get_tenant_bucket_name(tenant_id, db),
        "gpu_required": bool(root_data.get("gpu_required", False)),
        "container_timeout": root_data.get("container_timeout", 900),
        "skip_download": bool(root_data.get("skip_download", False)),
        "skip_upload": bool(root_data.get("skip_upload", False)),
        "calibration_run": True,
        "calibration_run_kind": run_kind,
        "node_definition_id": node_def.id,
        "size_tier": tier,
        "stressor_kinds": stressors,
        "calibration_pair_id": calibration_pair_id,
        "node_data": root_data,
        # 18.4 (GI.4): also stamp the root task (the generator, or the focal
        # node itself when there is no generator) so the k8s run is
        # identifiable on EVERY one of its tasks — the progress counter and
        # the coordinator sweep use this to keep k8s runs out of the 84-task
        # matrix bookkeeping.
        **({"k8s_calibration_node": k8s_node} if k8s_node else {}),
    }

    task = ApiTaskQueue(
        workflow_run_id=wf_run.id,
        node_run_id=None,
        node_id=root["id"],
        tenant_id=tenant_id,
        queue_name=root_data.get("process_type", "CPU"),
        priority=2,
        priority_class="low",
        task_type="system",
        task_payload=root_payload,
        status=ApiTaskStatus.ASSIGNED,
        assigned_to_worker=worker_name,
        max_attempts=2,
        output_namespace="attempt_0",
    )
    db.add(task)
    db.commit()

    _wake_workers(root_data.get("process_type", "CPU"))

    logger.info(
        "calibration: dispatched run %s for node %d (%s) tier=%s kind=%s "
        "on worker %s (%d node%s, format=%s)",
        wf_run.orchestrator_run_id, node_def.id, node_def.name, tier, run_kind,
        worker_name, len(nodes), "" if len(nodes) == 1 else "s", fmt or "none",
    )
    return wf_run


# Stressor axes. cpu-multi and io are accelerator-agnostic (one image each);
# the GPU axes have one image per accelerator family; npu is its own image
# because torch has no NPU device.
ACCEL_AGNOSTIC_STRESSORS = ("cpu-multi", "io")
GPU_STRESSORS = ("gpu-compute", "vram-bandwidth", "mixed")
STRESSOR_NODE_KIND = "calibration_stressor"


def stressor_image(kind: str, accel_family: str = "cuda") -> str:
    """Image ref for one stressor axis on one accelerator family.

    cuda is the unsuffixed build; rocm and intel carry a suffix. Getting this
    wrong is not a cosmetic problem — running a CUDA-built stressor on an AMD
    box produces a container that starts, finds no device, quietly degrades to
    CPU, and reports a contention run in which nothing was contended."""
    registry = platform_registry()
    family = (accel_family or "cuda").lower()

    if kind in ACCEL_AGNOSTIC_STRESSORS or kind == "npu":
        suffix = ""
    elif family in ("rocm", "intel"):
        suffix = f"-{family}"
    else:
        suffix = ""

    return f"{registry}/modtim/stressor-{kind}{suffix}:latest"


def worker_accel_family(db: Session, worker_name: str) -> str:
    """Best-guess accelerator family for a worker, from its advertised queues.

    Falls back to 'cuda' only when nothing more specific is known — and the
    stressor verdict is what catches a wrong guess, since a stressor that finds
    no device reports zero saturation."""
    from models import WorkerLicense

    w = db.query(WorkerLicense).filter(
        WorkerLicense.worker_name == worker_name).first()
    if w is None:
        return "cuda"

    queues = {str(q).upper() for q in (w.queues or [])}
    specs = w.hardware_specs or {}
    gpu_type = str(specs.get("gpu_type") or "").lower()

    if "rocm" in gpu_type or "ROCM" in queues:
        return "rocm"
    if "intel" in gpu_type or "XPU" in queues or "INTEL" in queues:
        return "intel"
    if "npu" in gpu_type or "NPU" in queues:
        return "npu"
    return "cuda"


def dispatch_stressor_run(
    db: Session,
    tenant_id: int,
    worker_name: str,
    kind: str,
    calibration_pair_id: str,
    duration_s: int = 60,
    accel_family: str = "cuda",
):
    """Dispatch one stressor as its own pinned single-node workflow.

    Deliberately NOT part of the calibration workflow's graph. Keeping it a
    separate run means nothing in the worker needs to know what a stressor is,
    and the two runs need no relationship beyond the shared pair id.

    The payload does NOT carry `calibration_run`. If it did, the failure brake
    and the node sweep would both count stressor runs as calibration tasks and
    their outcomes would pollute a node's success rate.
    """
    from models import ApiTaskQueue, ApiTaskStatus, TaskStatus, WorkflowRun
    from services.workflow_orchestrator import build_execution_plan

    image = stressor_image(kind, accel_family)
    node_id = f"stressor-{kind}-{calibration_pair_id[:8]}"

    node = {
        "id": node_id,
        "data": {
            "node_kind": STRESSOR_NODE_KIND,
            "processing_image": image,
            "process_type": "CPU" if kind in ACCEL_AGNOSTIC_STRESSORS else "CUDA",
            "container_env": {
                "STRESSOR_KIND": kind,
                "STRESSOR_DURATION_S": str(duration_s),
                "STRESSOR_ACCEL": accel_family,
            },
            "gpu_required": kind not in ACCEL_AGNOSTIC_STRESSORS,
            "container_timeout": duration_s + 120,
            "skip_download": True,
            "skip_upload": True,
            # Uncapped CPU. The executor caps every container at 2 cores by
            # default, which is sane for user work and fatal for a stressor:
            # measured on a 22-core host, cpu-multi spawned 22 processes, got
            # 2 cores, and produced ~200% instead of ~2200%. Both it and the
            # node under test were capped at 2, so on a 22-core box they never
            # competed and "contention" calibration measured no contention.
            # A negative cpu_limit means uncapped (worker maps it to
            # NanoCPUs=0). Only the platform dispatcher can set this.
            "cpu_limit": float(os.getenv("CALIBRATION_STRESSOR_CPU_LIMIT", "-1")),
        },
    }
    plan = build_execution_plan([node], [])

    run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=None,
        workflow_version_id=None,
        status=TaskStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
        orchestrator_run_id=f"stressor_{kind}_{uuid.uuid4().hex[:8]}",
        parameters={
            "execution_plan": plan,
            "pinned_worker": worker_name,
            "is_stressor": True,
            "stressor_kind": kind,
            "accel_family": accel_family,
            # The link that makes the calibration measurement verifiable.
            "calibration_pair_id": calibration_pair_id,
        },
    )
    db.add(run)
    db.flush()

    task = ApiTaskQueue(
        workflow_run_id=run.id,
        node_run_id=None,
        node_id=node_id,
        tenant_id=tenant_id,
        queue_name=node["data"]["process_type"],
        priority=2,
        priority_class="low",
        task_type="system",
        task_payload={
            "node_kind": STRESSOR_NODE_KIND,
            "processing_image": image,
            "container_env": node["data"]["container_env"],
            "source_file_location": "",
            # WS4.Q17.12 — tenant-scoped task: must match the worker
            # credentials path (worker_tasks resolve_backend), i.e. the
            # tenant's effective bucket, not the house format.
            "minio_bucket": get_tenant_bucket_name(tenant_id, db),
            "gpu_required": node["data"]["gpu_required"],
            "container_timeout": node["data"]["container_timeout"],
            "skip_download": True,
            "skip_upload": True,
            "cpu_limit": node["data"]["cpu_limit"],
            # NOT calibration_run — see docstring.
            "is_stressor": True,
            "stressor_kind": kind,
            "calibration_pair_id": calibration_pair_id,
            "node_data": node["data"],
        },
        status=ApiTaskStatus.ASSIGNED,
        assigned_to_worker=worker_name,
        max_attempts=1,
        output_namespace="attempt_0",
    )
    db.add(task)
    db.commit()

    _wake_workers(node["data"]["process_type"])
    logger.info(
        "calibration: dispatched stressor '%s' (%s) on %s pair=%s",
        kind, image, worker_name, calibration_pair_id,
    )
    return run


def stressor_duration_for(node_def, db: Session = None,
                          size_tier: str = None) -> int:
    """How long a stressor must run to cover the node it contends with.

    Sized from what the node ACTUALLY takes, not from what it is allowed to
    take. `container_timeout` is a worst-case kill switch — on a typical node
    it is 1800s guarding a 23s run. Sizing from it burned the box for thirty
    minutes per pairing, and because there is no cancel channel, every
    subsequent measurement on that worker had to wait the stressor out. The
    isolation runs for a tier always complete before its contention runs
    dispatch, so by this point a measured duration exists.

    The multiplier buys headroom: contention is expected to SLOW the focal run
    (that is the entire point of measuring it), so a stressor sized to the
    isolated duration would end mid-run and drag the result back toward
    isolation. `container_timeout` remains the fallback when nothing has been
    measured yet, and the cap still bounds both paths.
    """
    margin = int(os.getenv("CALIBRATION_STRESSOR_MARGIN_S", "60"))
    cap = int(os.getenv("CALIBRATION_STRESSOR_MAX_S", "1800"))
    factor = float(os.getenv("CALIBRATION_STRESSOR_FACTOR", "3.0"))

    measured = measured_duration_for(db, node_def, size_tier)
    if measured:
        base = int(measured * factor)
    else:
        base = int(getattr(node_def, "container_timeout", None) or 900)

    return max(60, min(base + margin, cap))


def measured_duration_for(db: Session, node_def, size_tier: str):
    """Isolated duration already measured for this node at this tier, or None.

    Reads the same ResourcePattern rows the isolation phase writes. Returns
    None rather than a guess when nothing has been measured — the caller must
    be able to tell "fast node" from "never measured".
    """
    if db is None or node_def is None:
        return None
    node_kind = getattr(node_def, "node_kind", None)
    if not node_kind:
        return None

    from models import ResourcePattern

    q = db.query(ResourcePattern).filter(ResourcePattern.node_kind == node_kind)
    if size_tier:
        q = q.filter(ResourcePattern.size_tier == size_tier)
    rows = [r for r in q.all()
            if r.avg_duration_seconds and r.avg_duration_seconds > 0]
    if not rows:
        return None
    # The slowest tier measured is the safe one to cover.
    return max(r.avg_duration_seconds for r in rows)


def worker_has_live_stressor(db: Session, worker_name: str,
                             exclude_pair_id: Optional[str] = None) -> bool:
    """Is a stressor still running on this worker for some other pairing?

    This is a correctness guard, not tidiness. Because a running stressor
    cannot be cancelled, one dispatched for run N can still be loading the box
    when run N+1 starts. If N+1 is an isolation run, its baseline would be
    measured under contention — poisoning the very number every contention
    result is compared against.
    """
    from models import ApiTaskQueue, ApiTaskStatus

    q = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.assigned_to_worker == worker_name,
        ApiTaskQueue.task_payload["is_stressor"].as_string() == "true",
        ApiTaskQueue.status.in_((
            ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED,
            ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING,
        )),
    )
    if exclude_pair_id:
        q = q.filter(
            ApiTaskQueue.task_payload["calibration_pair_id"].as_string()
            != exclude_pair_id
        )
    return db.query(q.exists()).scalar()


def evaluate_contention_measurement(db: Session, calibration_pair_id: str,
                                    expected_family: str = "cuda") -> dict:
    """Decide whether a finished contention run counts as a measurement.

    Joins the calibration run to the stressor run it was paired with — the
    reason `calibration_pair_id` exists — reads the stressor's verdict off its
    container log, and returns {"valid": bool, "reason": str, "verdict": dict}.

    A run whose stressor cannot be confirmed is NOT a contention measurement.
    Keeping it would file an isolation-shaped result under a `pairwise` label,
    and downstream nothing could tell the difference between "this node is
    unaffected by contention" (a real, useful finding) and "the stressor never
    ran" (garbage). Those are opposite conclusions from identical data.
    """
    from models import ApiTaskQueue

    stressor_tasks = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.task_payload["is_stressor"].as_string() == "true",
        ApiTaskQueue.task_payload["calibration_pair_id"].as_string() == calibration_pair_id,
    ).all()

    if not stressor_tasks:
        return {"valid": False, "reason": "no stressor run found for this pairing",
                "verdict": None}

    # Every stressor in the pairing must check out — a multi-stressor run with
    # one dead side measured less contention than it claims.
    worst = None
    for t in stressor_tasks:
        log_tail = (t.result_payload or {}).get("log_tail")
        verdict = parse_stressor_verdict(log_tail)
        ok, reason = verdict_is_valid(verdict, expected_family)
        if not ok:
            kind = (t.task_payload or {}).get("stressor_kind", "?")
            return {"valid": False, "reason": f"stressor '{kind}': {reason}",
                    "verdict": verdict}
        worst = verdict

    return {"valid": True, "reason": "", "verdict": worst}


def release_paired_stressors(db: Session, calibration_pair_id: str) -> int:
    """Mark a pairing's stressor tasks terminal once the measurement is done.

    Housekeeping and accounting: it stops a queued stressor from starting after
    its calibration run already finished, and keeps `worker_has_live_stressor`
    honest.

    It does NOT stop a container that is already running — there is no cancel
    channel to the worker. That one runs out its own duration, which is why
    `stressor_duration_for` caps it.
    """
    from models import ApiTaskQueue, ApiTaskStatus

    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.task_payload["is_stressor"].as_string() == "true",
        ApiTaskQueue.task_payload["calibration_pair_id"].as_string() == calibration_pair_id,
        ApiTaskQueue.status.in_((ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED)),
    ).update({"status": ApiTaskStatus.CANCELLED}, synchronize_session=False)


def dispatch_contention_run(
    db: Session,
    node_def,
    tenant_id: int,
    worker_name: str,
    tier: str,
    stressor_kinds: List[str],
    run: int = 0,
    duration_s: Optional[int] = None,
):
    """A calibration run plus the stressor run(s) that contend with it.

    Both sides are pinned to the same worker and share a `calibration_pair_id`.
    The stressors are started FIRST so they are already loading the box when
    the node under test begins — a stressor that starts late would leave part
    of the focal run uncontended and drag the measurement toward isolation.

    Returns (calibration_run, [stressor_runs]) or (None, []) if preflight
    refuses the node.
    """
    check = preflight(node_def)
    if not check["ok"]:
        logger.warning("contention run refused: %s", check["reason"])
        return None, []

    # A stressor left over from an earlier pairing would contend with this run
    # too, and nothing can cancel it mid-flight. Wait rather than measure
    # against an unknown amount of foreign load.
    if worker_has_live_stressor(db, worker_name):
        logger.info(
            "contention run deferred: worker %s still has a live stressor from "
            "another pairing", worker_name,
        )
        return None, []

    if duration_s is None:
        duration_s = stressor_duration_for(node_def, db, tier)

    pair_id = str(uuid.uuid4())
    family = worker_accel_family(db, worker_name)

    stressor_runs = [
        dispatch_stressor_run(
            db, tenant_id, worker_name, kind, pair_id,
            duration_s=duration_s, accel_family=family,
        )
        for kind in stressor_kinds
    ]

    run_kind = "pairwise" if len(stressor_kinds) == 1 else "multi"
    cal_run = dispatch_calibration_run(
        db, node_def, tenant_id, worker_name, tier=tier, run=run,
        run_kind=run_kind, stressor_kinds=stressor_kinds,
        calibration_pair_id=pair_id,
    )
    return cal_run, stressor_runs


STRESSOR_RESULT_MARKER = "STRESSOR_RESULT_JSON:"


def parse_stressor_verdict(log_tail: Optional[str]) -> Optional[dict]:
    """Pull the verdict a stressor prints on exit out of its container log.

    Mirrors the BUILD_RESULT_JSON convention already used for the image
    builder. Returns None when no verdict line is present — which is itself
    meaningful: a stressor that died before finishing never printed one.
    """
    import json

    if not log_tail:
        return None
    for line in reversed(log_tail.splitlines()):
        idx = line.find(STRESSOR_RESULT_MARKER)
        if idx < 0:
            continue
        try:
            return json.loads(line[idx + len(STRESSOR_RESULT_MARKER):].strip())
        except Exception:
            continue
    return None


def verdict_is_valid(verdict: Optional[dict], expected_family: str = "cuda") -> tuple:
    """Did the stressor actually stress? Returns (ok, reason).

    Deliberately NOT threshold-based on absolute saturation. Those numbers are
    hardware-dependent and we have no baseline to calibrate them against, so
    inventing one would just be a guess wearing a number. The two checks here
    are binary and need no tuning:

      Did it get the device it was asked for? A stressor told `cuda` that
      reports `cpu` silently degraded — it started, ran, and contended for
      nothing.

      Did it cover the run? A stressor that exits at 20% of the focal task's
      duration leaves 80% of the measurement uncontended, dragging the result
      toward isolation.

    Absolute saturation floors can be added later, once real runs have
    produced numbers worth comparing against.
    """
    if verdict is None:
        return False, "no stressor verdict recorded (it never reported completion)"
    if not verdict.get("started"):
        return False, "stressor never started"

    fam = str(verdict.get("device_family") or "").lower()
    want = (expected_family or "cuda").lower()
    # cpu-multi and io are accelerator-agnostic and legitimately report 'cpu'.
    if verdict.get("kind") not in ACCEL_AGNOSTIC_STRESSORS:
        if fam and want and fam != want:
            return False, (
                f"stressor fell back to '{fam}' when '{want}' was required — "
                f"it contended for nothing"
            )

    covered = float(verdict.get("covered_fraction") or 0.0)
    floor = float(os.getenv("CALIBRATION_STRESSOR_MIN_COVERAGE", "0.9"))
    if covered < floor:
        return False, (
            f"stressor covered only {covered:.0%} of its requested duration "
            f"(floor {floor:.0%}) — part of the run was uncontended"
        )

    if float(verdict.get("saturation") or 0.0) <= 0:
        return False, "stressor reported zero saturation"

    return True, ""


def pick_worker(db: Session, queue: str) -> Optional[str]:
    """A licensed, heartbeating worker that serves `queue`.

    Calibration pins a whole run to one worker, so this choice decides where
    both the generator and the node under test execute. Prefers a worker whose
    advertised queues contain the node's queue; falls back to any licensed
    worker only for CPU work, because a GPU node on a CPU-only box would
    measure the wrong thing entirely."""
    from datetime import timedelta
    from models import WorkerLicense, WorkerStatus

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=int(os.getenv("CALIBRATION_WORKER_HEARTBEAT_S", "120")))

    candidates = db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.LICENSED,
        WorkerLicense.last_heartbeat.isnot(None),
        WorkerLicense.last_heartbeat >= cutoff,
        WorkerLicense.deleted_at.is_(None),
    ).order_by(WorkerLicense.worker_name.asc()).all()

    want = (queue or "CPU").upper()
    for w in candidates:
        queues = [str(q).upper() for q in (w.queues or [])]
        if want in queues:
            return w.worker_name

    if want in ("CPU", "DEFAULT"):
        for w in candidates:
            return w.worker_name

    return None


def dispatch_matrix(db: Session, node_def, tenant_id: int,
                    worker_name: str, max_runs: Optional[int] = None) -> list:
    """Dispatch the calibration matrix for one node.

    The full matrix is 84 runs:

        9  isolation       3 tiers x 3 runs
        45 pairwise        5 stressors x 3 tiers x 3 runs
        30 multi-stressor  10 stressor-pairs x 3 tiers x 1 run

    Contention runs pair a calibration run with stressor run(s) sharing a
    `calibration_pair_id`, and the measurement is gated afterwards on the
    stressor's verdict (see evaluate_contention_measurement). Without that gate
    a stressor that silently failed would file an isolation-shaped result under
    a `pairwise` label — indistinguishable downstream from a node genuinely
    unaffected by contention.

    Isolation runs are dispatched FIRST on purpose: they are the baseline every
    contention result is compared against, and they remain useful even if the
    stressor half is unavailable. Set CALIBRATION_INCLUDE_CONTENTION=0 to
    dispatch isolation only.

    `max_runs` bounds a single tick so one node cannot flood the queue; the
    coordinator passes its remaining headroom.
    """
    from routers.node_definitions import (
        CALIBRATION_STRESSORS, MULTI_STRESSOR_PAIRS, PAIRWISE_RUNS_PER_TIER,
    )

    runs_per_tier = int(os.getenv("CALIBRATION_RUNS_PER_TIER", "3"))
    include_contention = os.getenv("CALIBRATION_INCLUDE_CONTENTION", "1") != "0"

    # The catalog constant spells these "stressor-cpu-multi"; the image
    # resolver and payload want the bare axis.
    def _axis(name):
        return name[len("stressor-"):] if name.startswith("stressor-") else name

    planned = [("isolation", tier, i, [])
               for tier in SIZE_TIERS for i in range(runs_per_tier)]

    if include_contention:
        planned += [("pairwise", tier, i, [_axis(s)])
                    for s in CALIBRATION_STRESSORS
                    for tier in SIZE_TIERS
                    for i in range(PAIRWISE_RUNS_PER_TIER)]
        planned += [("multi", tier, 0, [_axis(a), _axis(b)])
                    for (a, b) in MULTI_STRESSOR_PAIRS
                    for tier in SIZE_TIERS]

    # Skip what this node already has. The matrix is dispatched across many
    # coordinator ticks, not in one shot, so every call must resume rather
    # than restart. Identity is the run suffix, which is unique per
    # (kind, tier, run, stressor-set).
    already = already_dispatched_suffixes(db, node_def.id)
    planned = [pl for pl in planned
               if run_suffix(pl[0], pl[1], pl[2], pl[3]) not in already]

    if max_runs is not None:
        planned = planned[:max(0, max_runs)]

    out = []
    contention_dispatched = 0
    for kind, tier, i, stressors in planned:
        if kind == "isolation":
            run = dispatch_calibration_run(
                db, node_def, tenant_id, worker_name,
                tier=tier, run=i, run_kind="isolation",
            )
        else:
            # Contention runs are serialised: each needs the worker's load to
            # be ITS stressor and nothing else. Dispatching two at once means
            # the second measures both stressors and reports it as one.
            if contention_dispatched >= 1 or worker_has_live_stressor(db, worker_name):
                break
            run, _ = dispatch_contention_run(
                db, node_def, tenant_id, worker_name,
                tier=tier, stressor_kinds=stressors, run=i,
            )
            contention_dispatched += 1
        if run is None:
            break
        out.append(run)
    return out


def already_dispatched_suffixes(db: Session, node_definition_id: int) -> set:
    """Run suffixes already dispatched for this node, from the task queue.

    Stateless resume: the queue is the record of what has been attempted, so
    the coordinator can be restarted, the plan cancelled and re-created, or a
    tick can fail, without re-running measurements that already exist.
    """
    from models import ApiTaskQueue

    # Both node ids in a run carry the same suffix. At DISPATCH time only the
    # generator task exists — the focal task is created later by the downstream
    # dispatcher — so matching only `calibrate-` would see nothing and
    # re-dispatch the whole matrix on every tick.
    prefixes = (f"calgen-{node_definition_id}-", f"calibrate-{node_definition_id}-")
    rows = db.query(ApiTaskQueue.node_id).filter(
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node_definition_id),
    ).all()

    out = set()
    for (nid,) in rows:
        if not nid:
            continue
        for pre in prefixes:
            if nid.startswith(pre):
                out.add(nid[len(pre):])
                break
    return out


def _wake_workers(queue: str) -> None:
    """Nudge the queue so a worker picks the task up without waiting for its
    next poll. Best-effort — the worker polls anyway."""
    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish(f"task:available:{queue}", "1")
    except Exception:
        pass
