"""External-executor load — MOD's view of the work it has dispatched to external
orchestrators (Kubernetes / Qube / Deadline) via the executor-submit nodes.

This is the data source for the Dashboard "System → External" page. v1 reports
what MOD itself knows from the task queue — per farm: how many external jobs are
active / queued / succeeded / failed, throughput, and durations — which is the
load MOD is putting on each farm and how the farm is responding. It needs no
credentials and works offline.

Live farm-side hardware metrics (K8s node CPU/GPU utilization, Qube slot usage,
Deadline worker pools) are a v2 add: a best-effort probe per configured farm
using its stored connection secret. The response reserves a `live` field per
executor for that; v1 leaves it null.

Uses raw SQL `->>` for the node_kind / config lookups so it does not depend on
the ApiTaskQueue.task_payload TypeDecorator's comparator (whose `.astext` throws
— see the nim-sleepback fix).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

# node_kind -> display label. Add a row here when a new executor node ships.
EXECUTOR_KINDS = {
    "kubernetes_submit": "Kubernetes",
    "qube_submit": "Qube",
    "deadline_submit": "Deadline",
}

# Lowercase ApiTaskStatus buckets (values are lowercase — matched sets must be).
_ACTIVE = {"running", "pending_action"}
_QUEUED = {"pending", "queued", "claimed", "assigned"}
_SUCCEEDED = {"success", "completed"}
_FAILED = {"failure", "failed", "cancelled"}


def _target_label(kind: str, cenv: dict) -> str:
    """A stable, human label for the specific farm a task targeted, from its
    configured endpoint. Falls back to the kind's label when unset."""
    cenv = cenv or {}
    if kind == "kubernetes_submit":
        ns = cenv.get("K8S_NAMESPACE") or "default"
        host = (cenv.get("EXECUTOR_EGRESS_HOSTS") or "").split(",")[0].strip()
        return f"{host or 'cluster'} / {ns}"
    if kind == "qube_submit":
        return cenv.get("QUBE_SUPERVISOR") or "supervisor"
    if kind == "deadline_submit":
        return cenv.get("DEADLINE_WEBSERVICE_URL") or "web-service"
    return EXECUTOR_KINDS.get(kind, kind)


def _duration_s(started, completed):
    if not started or not completed:
        return None
    try:
        return max(0.0, (completed - started).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def _registered_farm_kinds(db: Session, tenant_id: int) -> set:
    """Executor kinds this tenant has REGISTERED a farm for (EXTFARM-1/2).

    A farm registered via services/external_farms.py means the tenant has wired
    that orchestrator up, so the page reports it `configured` even before MOD
    has dispatched a single task to it. `models` is imported inside the function
    so this module's top level stays stdlib + sqlalchemy (CE-safe).
    """
    import models
    rows = db.query(models.ExternalFarmConnection).filter(
        models.ExternalFarmConnection.tenant_id == tenant_id,
        models.ExternalFarmConnection.is_active == True,  # noqa: E712
    ).all()
    return {f"{r.kind}_submit" for r in rows}


def external_overview(db: Session, tenant_id: int, window_hours: int = 24) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    kinds = list(EXECUTOR_KINDS.keys())
    registered = _registered_farm_kinds(db, tenant_id)
    # Rows finished within the window, PLUS anything still in flight (any age).
    rows = db.execute(
        text(
            """
            SELECT id, status, workflow_run_id, node_id, started_at, completed_at,
                   created_at, error_message,
                   task_payload->>'node_kind'                        AS kind,
                   task_payload->'container_env'->>'K8S_NAMESPACE'   AS k8s_ns,
                   task_payload->'container_env'->>'EXECUTOR_EGRESS_HOSTS' AS egress,
                   task_payload->'container_env'->>'QUBE_SUPERVISOR'  AS qube_sup,
                   task_payload->'container_env'->>'DEADLINE_WEBSERVICE_URL' AS dl_url
            FROM api_task_queue
            WHERE tenant_id = :tid
              AND task_payload->>'node_kind' = ANY(:kinds)
              AND (created_at >= :since
                   OR lower(status::text) IN ('running','pending_action','pending','queued','claimed','assigned'))
            ORDER BY created_at DESC
            """
        ),
        {"tid": tenant_id, "kinds": kinds, "since": since},
    ).mappings().all()

    # kind -> target -> accumulator
    agg: dict = {k: {} for k in kinds}
    for r in rows:
        kind = r["kind"]
        if kind not in agg:
            continue
        cenv = {
            "K8S_NAMESPACE": r["k8s_ns"],
            "EXECUTOR_EGRESS_HOSTS": r["egress"],
            "QUBE_SUPERVISOR": r["qube_sup"],
            "DEADLINE_WEBSERVICE_URL": r["dl_url"],
        }
        target = _target_label(kind, cenv)
        t = agg[kind].setdefault(
            target,
            {"target": target, "active": 0, "queued": 0, "succeeded": 0,
             "failed": 0, "total": 0, "_durations": [], "recent": []},
        )
        st = (r["status"] or "").lower()
        t["total"] += 1
        if st in _ACTIVE:
            t["active"] += 1
        elif st in _QUEUED:
            t["queued"] += 1
        elif st in _SUCCEEDED:
            t["succeeded"] += 1
            d = _duration_s(r["started_at"], r["completed_at"])
            if d is not None:
                t["_durations"].append(d)
        elif st in _FAILED:
            t["failed"] += 1
        if len(t["recent"]) < 10:
            t["recent"].append({
                "task_id": r["id"],
                "run_id": r["workflow_run_id"],
                "node_id": r["node_id"],
                "status": st,
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "duration_s": _duration_s(r["started_at"], r["completed_at"]),
                "error": (r["error_message"] or "")[:200] or None,
            })

    executors = []
    for kind, label in EXECUTOR_KINDS.items():
        targets = []
        tot = {"active": 0, "queued": 0, "succeeded": 0, "failed": 0, "total": 0}
        for tgt in agg[kind].values():
            durs = tgt.pop("_durations")
            tgt["avg_duration_s"] = round(sum(durs) / len(durs), 1) if durs else None
            targets.append(tgt)
            for key in tot:
                tot[key] += tgt[key]
        targets.sort(key=lambda x: (-x["active"], -x["total"], x["target"]))
        executors.append({
            "kind": kind,
            "label": label,
            "configured": len(targets) > 0 or kind in registered,
            "targets": targets,
            "totals": tot,
            "live": None,  # reserved for v2 farm-side probe
        })

    return {
        "as_of": now.isoformat(),
        "window_hours": window_hours,
        "executors": executors,
    }
