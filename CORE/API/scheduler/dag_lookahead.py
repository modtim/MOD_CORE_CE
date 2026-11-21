"""DAG-aware lookahead — surface pending-but-blocked tasks so the packer
reserves future timeline slots for them when placing currently-runnable
tasks today.

Without lookahead, the packer is greedy on the current-round PENDING set
and can pack a worker's near-future timeline tightly with batch tasks,
leaving no room for an interactive task that's about to be unblocked
when its parent finishes 5 seconds from now. The reservations this
module emits are conservative: each blocked NodeRun gets a placeholder
slot equal to its `estimated_duration_seconds` at its predicted
earliest-runnable time on the lowest-makespan eligible worker.

This is observational — reservations don't write to ApiTaskQueue or
NodeRun. They only modify in-memory `worker_timelines` so the round's
greedy + 2-opt placement reasons about future work.
"""
from __future__ import annotations

from product_names import themed_logger
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = themed_logger(__name__, 'scheduler')


@dataclass
class LookaheadReservation:
    """One slot to reserve in a worker's timeline before greedy placement.

    Carries the predicted earliest-runnable offset (relative to round
    start) + predicted duration so the packer can place a no-op
    PlannedTask in that window.
    """
    workflow_run_id: int
    node_run_id: int
    workflow_node_graph_id: str
    earliest_runnable_offset_s: float
    predicted_duration_s: float
    queue_name: str


def collect_lookahead_reservations(
    db: Session,
    max_runs: int = 50,
) -> List[LookaheadReservation]:
    """Walk active workflow runs and emit reservations for blocked NodeRuns.

    A NodeRun is "blocked" when its workflow's DAG has it depending on
    parent NodeRuns that haven't completed yet. The earliest-runnable
    offset is `max(parent_completion_time) - now` so reservations land
    in worker_timelines at the right point relative to the current
    moment.

    Caps at `max_runs` active runs to bound query cost; the daily 5s
    packer tick wakes again if there are more.
    """
    import models

    now = datetime.now(timezone.utc)

    active_runs = (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.status == models.TaskStatus.RUNNING)
        .order_by(models.WorkflowRun.id.asc())
        .limit(max_runs)
        .all()
    )

    reservations: List[LookaheadReservation] = []
    for run in active_runs:
        if not run.workflow_version:
            continue
        graph = run.workflow_version.graph_json or {}
        edges = graph.get("edges") or []

        # Build dependencies: target_graph_node_id -> [parent_graph_node_id, ...].
        dependencies: Dict[str, List[str]] = {}
        for edge in edges:
            src = edge.get("source") or edge.get("from")
            tgt = edge.get("target") or edge.get("to")
            if src and tgt:
                dependencies.setdefault(tgt, []).append(src)

        # Pull all NodeRuns for this workflow run, joined with their
        # WorkflowNode to get graph_node_id (which the dependency graph
        # uses as its key).
        node_runs = (
            db.query(models.NodeRun, models.WorkflowNode.graph_node_id)
            .join(models.WorkflowNode, models.NodeRun.workflow_node_id == models.WorkflowNode.id)
            .filter(models.NodeRun.workflow_run_id == run.id)
            .all()
        )
        if not node_runs:
            continue

        # Index by graph_node_id for fast parent lookups.
        nrs_by_graph_id: Dict[str, models.NodeRun] = {gid: nr for nr, gid in node_runs}

        for nr, graph_id in node_runs:
            if nr.status != models.TaskStatus.QUEUED:
                continue
            parent_graph_ids = dependencies.get(graph_id, [])
            if not parent_graph_ids:
                # Root node (no dependencies). It's runnable now or already
                # claimed; not a lookahead candidate.
                continue

            # Compute earliest-runnable: max parent predicted completion.
            # If any parent is still QUEUED with no started_at, we conservatively
            # treat its earliest completion as (now + parent.estimated_duration).
            # Skip emitting a reservation when ANY parent has no estimate at all
            # (we can't predict its completion meaningfully).
            parent_completions: List[datetime] = []
            skip = False
            for pid in parent_graph_ids:
                p = nrs_by_graph_id.get(pid)
                if p is None:
                    skip = True
                    break
                if p.status in (models.TaskStatus.SUCCESS,):
                    if p.completed_at:
                        parent_completions.append(p.completed_at)
                    continue
                if p.status == models.TaskStatus.RUNNING:
                    if p.started_at and p.estimated_duration_seconds:
                        from datetime import timedelta
                        parent_completions.append(
                            p.started_at + timedelta(seconds=p.estimated_duration_seconds)
                        )
                        continue
                    skip = True
                    break
                if p.status == models.TaskStatus.QUEUED:
                    if p.estimated_duration_seconds:
                        from datetime import timedelta
                        parent_completions.append(
                            now + timedelta(seconds=p.estimated_duration_seconds)
                        )
                        continue
                    skip = True
                    break
                # FAILED / CANCELLED parent → child won't run; skip.
                skip = True
                break

            if skip or not parent_completions:
                continue

            earliest_completion = max(parent_completions)
            earliest_offset_s = max(0.0, (earliest_completion - now).total_seconds())

            duration_s = float(nr.estimated_duration_seconds or 120.0)

            # The queue_name routing requires the NodeDefinition's process_type;
            # fall back to "CPU" if we can't resolve it.
            queue_name = "CPU"
            if nr.workflow_node and nr.workflow_node.node_definition_id:
                node_def = db.query(models.NodeDefinition).filter(
                    models.NodeDefinition.id == nr.workflow_node.node_definition_id
                ).first()
                if node_def and node_def.process_type:
                    queue_name = node_def.process_type

            reservations.append(LookaheadReservation(
                workflow_run_id=run.id,
                node_run_id=nr.id,
                workflow_node_graph_id=graph_id,
                earliest_runnable_offset_s=earliest_offset_s,
                predicted_duration_s=duration_s,
                queue_name=queue_name,
            ))

    return reservations


def apply_reservations_to_timelines(
    reservations: List[LookaheadReservation],
    worker_timelines,
) -> int:
    """Mutate worker_timelines in place: for each reservation, simulate a
    placeholder task on the worker with the lowest current makespan that's
    eligible for the reservation's queue_name. Returns the count of
    reservations actually applied (some may have no eligible worker).

    The placeholder task uses minimal resources (1 MB RAM, 0 VRAM, 1% CPU)
    so it doesn't generate spurious contention signal — its only purpose
    is to bump the worker's makespan past the reservation window.
    """
    from scheduler.timeline_simulator import PlannedTask, TimelineSimulator

    if not reservations or not worker_timelines:
        return 0

    applied = 0
    for r in reservations:
        if not worker_timelines:
            break

        # Pick the worker with the lowest current makespan. Production
        # filtering by queue_name happens in _get_eligible_workers; the
        # caller has already pruned the timeline set.
        candidate = min(worker_timelines.items(), key=lambda kv: kv[1].makespan())
        worker_name, sim = candidate

        placeholder = PlannedTask(
            task_id=-r.node_run_id,  # negative to make it visually distinct in logs
            node_kind=f"lookahead-{r.workflow_run_id}",
            start_offset_s=r.earliest_runnable_offset_s,
            processing_duration_s=r.predicted_duration_s,
            upload_duration_s=0.0,
            download_duration_s=0.0,
            peak_ram_mb=1.0,
            peak_vram_mb=0.0,
            peak_cpu_pct=1.0,
            peak_net_mbps=0.0,
            pattern_type="plateau",
            is_gpu_bound=False,
        )
        worker_timelines[worker_name] = sim.simulate_task(placeholder)
        applied += 1

    return applied
