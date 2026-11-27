"""Business layer for GET /runs/{run_id}/result (Group X / GX.R2 — apps built
on MOD).

One call returns a finished (or in-progress) workflow run's outcome for an
external app: run status + timing, an error summary with the failed node,
per-node execution records with ALL of the node's outputs (fan-out runs have
one NodeRun per input file, each with its own outputs — every one is included,
grouped per graph node), the terminal (sink) node outputs as the run's
"answer", and a presigned download URL for the first final output. Small text
final outputs (<= 64 KB) are also inlined verbatim so an LLM answer needs no
second download.

Layering mirrors services/node_runs_service.py: the router is thin
authentication + delegation; this module owns the view-gate policy and
response shaping; the db reads are one query per collection (run, node runs,
outputs, content-blob cog map) — no per-output queries, no two-collection
joinedload cartesian (see the runs-list cartesian fix).

Reusable by GX.R3 (completion webhook) for the same "what did this run
produce" payload. No FastAPI/HTTPException imports here — the router maps
NotFoundError to 404.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from datetime import datetime, timezone

from pydantic import BaseModel
from decimal import Decimal

import schemas
from schemas import RunUsage
from sqlalchemy import func

import crud
import crud.node_runs as node_runs_crud
import models
from services._base import NotFoundError

# Presigned-URL lifetime, matching the node-run previews and run-surface
# outputs endpoints (15 minutes).
_PRESIGN_TTL = timedelta(seconds=900)

# Small-text inlining cap for final outputs: anything at or under this size
# with a text-ish type is fetched and embedded inline.
_INLINE_TEXT_MAX_BYTES = 64 * 1024
_INLINE_TEXT_EXTS = {"txt", "json", "jsonl", "md", "csv", "log", "xml", "yaml", "yml"}


def _get_viewable_run(db, *, run_id: int, tenant_id: int, user: "models.User") -> "models.WorkflowRun":
    """Fetch a WorkflowRun the caller may view, else raise NotFoundError.

    Tenant-scoped fetch first (a run in another tenant resolves to None ->
    404, never 403, so run ids do not leak across tenants), then the same
    per-workflow view gate the other run-content endpoints use.
    """
    from resource_permissions import can_view_workflow_run

    db_workflow_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_id)
    if not db_workflow_run:
        raise NotFoundError("Workflow run not found")
    if not can_view_workflow_run(db, user, db_workflow_run):
        raise NotFoundError("Workflow run not found")
    return db_workflow_run


def _presign(minio_client, content_hash: str, name: str, mime: str) -> Optional[str]:
    """Short-lived (15 min) presigned GET for one content blob, or None when
    storage is unreachable (the response degrades, never 500s, on a presign
    failure — same behaviour as the previews endpoint)."""
    try:
        return minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=content_hash,
            expires=_PRESIGN_TTL,
            response_headers={
                "response-content-type": mime or "application/octet-stream",
                "response-content-disposition": f'inline; filename="{name}"',
            },
        )
    except Exception:
        return None


def _is_inline_text(mime: Optional[str], ext: Optional[str]) -> bool:
    """True when an output should be fetched and inlined: a text/* or
    JSON/XML content type, or a text-ish file extension (the storage layer
    defaults untyped objects to application/octet-stream, so for those — and
    other non-text application types alike — the extension decides)."""
    m = (mime or "").lower()
    e = (ext or "").lower().lstrip(".")
    if m.startswith("text/") or m in ("application/json", "application/xml"):
        return True
    if m.startswith("application/"):
        return False
    return e in _INLINE_TEXT_EXTS


def _read_inline_text(minio_client, content_hash: str, size_bytes) -> Optional[str]:
    """Fetch a small text blob and return its decoded content, or None on any
    failure (missing object, size overrun, non-UTF-8 bytes)."""
    if size_bytes is not None and size_bytes > _INLINE_TEXT_MAX_BYTES:
        return None
    try:
        r = minio_client.client.get_object(
            bucket_name=minio_client.bucket_name, object_name=content_hash
        )
        data = r.read(_INLINE_TEXT_MAX_BYTES + 1)
        if len(data) > _INLINE_TEXT_MAX_BYTES:
            return None
        return data.decode("utf-8")
    except Exception:
        return None


def _output_entry(
    o: "models.WorkflowNodeOutput",
    cog_id_by_hash: Dict[str, int],
    minio_client,
    *,
    with_url: bool,
    inline_text: bool,
) -> Dict[str, Any]:
    """One output dict in the response: cog_id (resolved via the output's
    version, tenant-scoped), filename, mime/size, and — for final outputs — a
    presigned url and (for small text) the inline content."""
    entry: Dict[str, Any] = {
        "cog_id": cog_id_by_hash.get(o.content_hash),
        "filename": o.logical_name or (f"{o.content_hash[:12]}.{o.file_extension}" if o.file_extension else o.content_hash[:12]),
        "mime_type": o.mime_type,
        "size": o.size_bytes,
        "url": None,
        "text": None,
        "_hash": o.content_hash,
        "_ext": o.file_extension,
    }
    if with_url:
        entry["url"] = _presign(minio_client, o.content_hash, entry["filename"], o.mime_type)
        if inline_text and _is_inline_text(o.mime_type, o.file_extension):
            entry["text"] = _read_inline_text(minio_client, o.content_hash, o.size_bytes)
    return entry


# _compute_usage uses schemas.RunUsage (c6-J4')

# c11-J5 — bounded, whitelisted usage.models (worker-derived JSONB)
_USAGE_MODEL_CAP = 256
_USAGE_MODEL_NAME_MAX = 128
_USAGE_COUNT_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "images")
_USAGE_COUNT_MAX = 10 ** 9


_TERMINAL_RUN_STATUSES = (
    models.TaskStatus.SUCCESS, models.TaskStatus.FAILURE,
    models.TaskStatus.COMPLETED, models.TaskStatus.FAILED,
)


def _compute_usage(db, db_run, tenant_id: int) -> Dict[str, Any]:
    """GX-n5 — per-run compute usage for the run result (additive ``usage``).

    ONE aggregate SELECT over NodeRun joined to WorkflowRun (tenant-filtered):
    COUNT(*) and SUM(coalesce(completed_at, now) - started_at) in seconds for
    node runs that have started. Running nodes count "so far"; ``final`` is
    False until the run is terminal. ``gpu_seconds`` is omitted: NodeRun does
    not record whether a node ran on a GPU. ``credits_charged`` comes from the
    existing billing rows (CreditUsageLog + prepaid ledger debits, the same
    figure the cost cap uses) and is omitted when there are none (CE).
    """
    NR = models.NodeRun
    now = func.now()
    n, compute = (
        db.query(
            func.count(NR.id),
            func.coalesce(func.sum(func.extract(
                "epoch", func.coalesce(NR.completed_at, now) - NR.started_at)), 0),
        )
        .join(models.WorkflowRun, models.WorkflowRun.id == NR.workflow_run_id)
        .filter(NR.workflow_run_id == db_run.id,
                models.WorkflowRun.tenant_id == tenant_id)
        .one()
    )
    wall = None
    if db_run.started_at:
        end = db_run.completed_at or datetime.now(timezone.utc)
        started = db_run.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        wall = max((end - started).total_seconds(), 0.0)
    usage: Dict[str, Any] = {
        "wall_seconds": wall,
        "compute_seconds": round(float(compute or 0), 3),
        "nodes": int(n or 0),
        "final": db_run.status in _TERMINAL_RUN_STATUSES,
    }
    try:
        from services.cost_accumulator import _spent_credits, _spent_usd_cents
        spent = _spent_credits(db, tenant_id, db_run.id)
    except Exception:  # CE / billing seam absent: omit, never fail the result
        spent = 0
    if spent > 0:
        usage["credits_charged"] = spent
    # c10-J2 — customer charge in USD cents, never WorkflowRun.cost_cents
    try:
        cents = _spent_usd_cents(db, tenant_id, db_run.id)
    except Exception:
        cents = None
    if cents is not None:
        usage["cost"] = {"amount": str(
            (Decimal(cents) / Decimal(100)).quantize(Decimal("0.0001"))
        ), "currency": "USD"}
    # c11-J5 — per Stack Model Call node token counts (30.6.11). ONE extra query
    # over the same tenant-pinned join; the column is worker-derived JSONB, so
    # every row is whitelisted and a bad row is skipped, never a 500.
    try:
        rows = db.query(
            NR.id, NR.usage_tokens,
        ).join(models.WorkflowRun, models.WorkflowRun.id == NR.workflow_run_id).filter(
            NR.workflow_run_id == db_run.id,
            models.WorkflowRun.tenant_id == tenant_id,
            NR.usage_tokens.isnot(None),
        ).order_by(NR.id).all()
        entries: List[Dict[str, Any]] = []
        for _row_id, raw in rows:
            if len(entries) >= _USAGE_MODEL_CAP:
                break
            if not isinstance(raw, dict):
                continue
            per_model = raw.get("models")
            if not isinstance(per_model, list):
                continue
            for item in per_model:
                if len(entries) >= _USAGE_MODEL_CAP:
                    break
                if not isinstance(item, dict):
                    continue
                name = item.get("model")
                if not isinstance(name, str) or not name or len(name) > _USAGE_MODEL_NAME_MAX:
                    continue
                entry: Dict[str, Any] = {"model": name}
                node_id = item.get("node_id")
                if node_id is not None:
                    entry["node_id"] = node_id
                for key in _USAGE_COUNT_KEYS:
                    value = item.get(key)
                    if value is None or isinstance(value, bool) or not isinstance(value, int):
                        continue
                    if not 0 <= value <= _USAGE_COUNT_MAX:
                        continue
                    entry[key] = value
                entries.append(entry)
        if entries:
            usage["models"] = entries
    except Exception:  # metering is best-effort: never fail the run result
        pass
    return schemas.RunUsage(**usage).model_dump(exclude_none=True, exclude={"wall_seconds"} if wall is None else set())


def _fallback_outputs_from_task_payload(db, *, node_runs, outputs, tenant_id: int) -> list:
    """Read-only safety net: for every finished node run that has no
    workflow_node_outputs row, build output entries from the newest COMPLETED
    task's result_payload["outputs"]. Only hashes with a ContentBlob in THIS
    tenant are returned (tenant-scoped lookup), so a payload can never surface
    another tenant's bytes. Nothing is written."""
    import mimetypes
    from types import SimpleNamespace

    have = {o.node_run_id for o in outputs}
    done = (models.TaskStatus.COMPLETED, models.TaskStatus.SUCCESS)
    missing = [nr.id for nr in node_runs if nr.id not in have and nr.status in done]
    if not missing:
        return []
    tasks = (
        db.query(models.ApiTaskQueue)
        .filter(models.ApiTaskQueue.node_run_id.in_(missing),
                models.ApiTaskQueue.tenant_id == tenant_id,
                models.ApiTaskQueue.status == models.ApiTaskStatus.COMPLETED)
        .order_by(models.ApiTaskQueue.id.desc())
        .all()
    )
    newest: Dict[int, Any] = {}
    for t in tasks:
        newest.setdefault(t.node_run_id, t)
    wanted: List[Tuple[int, int, dict]] = []
    for nr_id, t in newest.items():
        outs = (t.result_payload or {}).get("outputs") or []
        for i, od in enumerate(outs):
            if isinstance(od, dict) and od.get("content_hash"):
                wanted.append((nr_id, i, od))
    if not wanted:
        return []
    blobs = {
        b[0] for b in db.query(models.ContentBlob.id).filter(
            models.ContentBlob.tenant_id == tenant_id,
            models.ContentBlob.id.in_([w[2]["content_hash"] for w in wanted]),
        ).all()
    }
    result = []
    for nr_id, i, od in sorted(wanted, key=lambda w: (w[0], w[1])):
        if od["content_hash"] not in blobs:
            continue
        ext = od.get("file_extension") or ""
        name = od.get("file_name") or ""
        result.append(SimpleNamespace(
            content_hash=od["content_hash"], node_run_id=nr_id, output_index=i,
            logical_name=name, file_extension=ext,
            size_bytes=od.get("size_bytes") or 0,
            mime_type=od.get("mime_type")
            or mimetypes.guess_type(name or ("x." + ext.lstrip(".")))[0],
        ))
    return result


def get_run_result(
    db,
    *,
    run_id: int,
    tenant_id: int,
    user: "models.User",
    minio_client,
) -> Dict[str, Any]:
    """Assemble the one-call run result (see module docstring)."""
    db_run = _get_viewable_run(db, run_id=run_id, tenant_id=tenant_id, user=user)

    # --- node runs (one query; workflow_node is to-one so joinedload-safe) ---
    node_runs = node_runs_crud.get_node_runs_with_workflow_node(db, workflow_run_id=run_id)

    # --- outputs for this run (one query) ---
    outputs = (
        db.query(models.WorkflowNodeOutput)
        .filter(models.WorkflowNodeOutput.workflow_run_id == run_id)
        .order_by(models.WorkflowNodeOutput.node_run_id.asc(), models.WorkflowNodeOutput.output_index.asc())
        .all()
    )
    # A COMPLETED node run whose workflow_node_outputs row never landed still
    # has its outputs in api_task_queue.result_payload: answer from there.
    outputs = list(outputs) + _fallback_outputs_from_task_payload(
        db, node_runs=node_runs, outputs=outputs, tenant_id=tenant_id)
    outputs_by_nr: Dict[int, List[models.WorkflowNodeOutput]] = {}
    for o in outputs:
        outputs_by_nr.setdefault(o.node_run_id, []).append(o)

    # --- cog_id resolution (two queries: cogs by hash, versions by cogs) ---
    cog_ids = db.query(models.ContentBlob.cog_id).filter(
        models.ContentBlob.tenant_id == tenant_id,
        models.ContentBlob.cog_id.isnot(None),
        models.ContentBlob.id.in_([o.content_hash for o in outputs]),
    ).all()
    cog_ids = [c[0] for c in cog_ids if c[0] is not None]
    cog_id_by_hash: Dict[str, int] = {}
    if cog_ids:
        vers = db.query(models.Version.cog_id, models.ContentBlob.id).join(
            models.ContentBlob, models.Version.content_id == models.ContentBlob.id
        ).filter(
            models.Version.cog_id.in_(cog_ids),
            models.ContentBlob.tenant_id == tenant_id,
        ).all()
        # a cog may hold several versions of the SAME content; any of them
        # identifies the output's folder entry.
        for cog_id, chash in vers:
            cog_id_by_hash.setdefault(chash, cog_id)

    # --- sink (terminal) nodes from the run's pinned version graph: nodes
    # with no outgoing edge. Fall back to "no sinks known" (final_outputs =
    # the outputs of the run's COMPLETED/SUCCESS nodes when the graph is
    # unavailable) so a legacy version row without graph_json still answers.
    graph_nodes: Dict[str, str] = {}
    sinks: Optional[set] = None
    if db_run.workflow_version_id:
        version = crud.get_workflow_version_by_id(
            db, version_id=db_run.workflow_version_id, tenant_id=tenant_id
        )
        g = (version.graph_json or {}) if version else {}
        node_ids = set()
        for gn in g.get("nodes", []) or []:
            gid = gn.get("id") if isinstance(gn, dict) else None
            if not gid:
                continue
            node_ids.add(gid)
            graph_nodes[gid] = (gn.get("data") or {}).get("label") or gid
        downstream = set()
        for e in g.get("edges", []) or []:
            if not isinstance(e, dict):
                continue
            src, dst = e.get("source"), e.get("target")
            if src in node_ids and dst in node_ids:
                downstream.add(src)
        sinks = node_ids - downstream
        if not sinks:  # a graph with no nodes at all: nothing can be a sink
            sinks = set()

    # --- group node runs by graph node (fan-out = many rows per node).
    # Every graph node appears in the response (nodes with no NodeRun yet are
    # stubbed "pending", like /workflow-runs/{id}/node-status); a node's
    # reported status is a STABLE rollup of all its fan-out branches
    # (active-or-partially-done -> running, any done -> success, failure only
    # when every branch failed) so a fanned node never flickers between polls.
    _ACTIVE = {"running", "pending_action"}
    _WAITING = {"queued", "blocked", "pending", "claimed", "assigned"}
    _DONE = {"completed", "success"}
    _FAIL = {"failed", "failure"}

    def _rollup(statuses: set) -> str:
        s = {str(x).lower() for x in statuses if x}
        if (s & _ACTIVE) or (s & _WAITING and (s & _DONE or s & _FAIL)):
            return "running"
        if s & _WAITING:
            return "pending"
        if s & _DONE:
            return "success"
        if s & _FAIL:
            return "failure"
        return "pending"

    nodes_out: "Dict[str, Dict[str, Any]]" = {
        gid: {
            "node_id": gid,
            "label": label,
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "error": None,
            "outputs": [],
        }
        for gid, label in graph_nodes.items()
    }
    # NodeRuns whose graph node isn't in the version's graph (renamed node
    # ids, legacy runs) still show up.
    for nr in node_runs:
        gid = nr.workflow_node.graph_node_id if nr.workflow_node else None
        if not gid:
            continue
        if gid not in nodes_out:
            nodes_out[gid] = {
                "node_id": gid, "label": gid, "status": "pending",
                "started_at": None, "completed_at": None, "error": None,
                "outputs": [],
            }
    _statuses: "Dict[str, set]" = {}
    failed_nodes: List[Tuple[models.NodeRun, str]] = []
    for nr in node_runs:
        gid = nr.workflow_node.graph_node_id if nr.workflow_node else None
        if not gid:
            continue
        st = nr.status.value if nr.status else None
        _statuses.setdefault(gid, set()).add(st)
        bucket = nodes_out[gid]
        if nr.started_at and (bucket["started_at"] is None or nr.started_at > bucket["started_at"]):
            bucket["started_at"] = nr.started_at
        if nr.completed_at and (bucket["completed_at"] is None or nr.completed_at > bucket["completed_at"]):
            bucket["completed_at"] = nr.completed_at
        if nr.error_message and not bucket["error"]:
            bucket["error"] = nr.error_message
        for o in outputs_by_nr.get(nr.id, []):
            bucket["outputs"].append(_output_entry(o, cog_id_by_hash, minio_client,
                                                   with_url=False, inline_text=False))
        if st in _FAIL:
            failed_nodes.append((nr, gid))
    for gid, sts in _statuses.items():
        nodes_out[gid]["status"] = _rollup(sts)

    # --- run error summary ---
    error = None
    run_failed = db_run.status is not None and db_run.status.value in ("failed", "failure")
    if run_failed:
        message = db_run.error_message
        failed_node_id = failed_node_label = None
        if failed_nodes:
            # newest failure first — the one that stopped the run
            failed_nodes.sort(key=lambda p: (p[0].completed_at or p[0].started_at or 0), reverse=True)
            _, failed_node_id = failed_nodes[0]
            failed_node_label = graph_nodes.get(failed_node_id) or failed_node_id
            message = (failed_nodes[0][0].error_message or message)
        # GY.V4b — the run error text is shown to the caller, so it is redacted
        # on display only (stored error_message is never rewritten; off profile
        # returns the same string).
        from utils.pii_redaction_surfaces import safe_redact
        message = safe_redact(message)
        error = {"message": message, "failed_node_id": failed_node_id,
                 "failed_node_label": failed_node_label}

    # --- final outputs (the answer): sink nodes' outputs. When the graph is
    # unknown (sinks is None) every produced output counts as final so a
    # legacy version without graph_json still answers; a run whose graph has
    # no sinks gets none. In-progress runs simply have no sink outputs yet.
    final_outputs: List[Dict[str, Any]] = []
    if sinks is None:
        final_ids = list(nodes_out)
    else:
        final_ids = [gid for gid in nodes_out if gid in sinks]
    for gid in final_ids:
        for out in nodes_out[gid]["outputs"]:
            final_outputs.append(out)

    # url on the first final output; inline text for every small text final.
    if final_outputs:
        final_outputs[0]["url"] = _presign(
            minio_client, final_outputs[0]["_hash"], final_outputs[0]["filename"],
            final_outputs[0]["mime_type"])
        # GY.V1a follow-up (GSEC R-111): the run-result hand-off is a user-
        # facing presigned GET, so it gets its own delivery row. IDs only —
        # the cog id when the blob resolves to one (else the run id), the
        # content hash as the object key, never the URL itself.
        if final_outputs[0]["url"]:
            # 1788 CE carve: delivery_audit is paid; CE records no delivery row.
            from utils.paid_import import optional_paid
            _delivery_audit = optional_paid("services.delivery_audit")

            _cog_id = final_outputs[0].get("cog_id")
            (_delivery_audit.record_delivery if _delivery_audit is not None
             else (lambda *a, **kw: None))(
                db,
                delivery_path="presign",
                actor_user_id=user.id,
                tenant_id=tenant_id,
                resource_id=_cog_id if _cog_id else run_id,
                resource_type="cog" if _cog_id else "workflow_run",
                object_key=final_outputs[0]["_hash"],
                expiry_seconds=int(_PRESIGN_TTL.total_seconds()),
                metadata={"channel": "run_result",
                          "filename": final_outputs[0]["filename"]},
            )
    for out in final_outputs:
        if _is_inline_text(out["mime_type"], out.get("_ext")):
            out["text"] = _read_inline_text(minio_client, out["_hash"], out["size"])

    # strip the internal lookup keys from every emitted output (final entries
    # alias the node-outputs list, so one pass covers both).
    for node in nodes_out.values():
        for out in node["outputs"]:
            out.pop("_hash", None)
            out.pop("_ext", None)

    # --- run timing ---
    duration_s = None
    if db_run.started_at and db_run.completed_at:
        duration_s = (db_run.completed_at - db_run.started_at).total_seconds()

    return {
        "run_id": run_id,
        "workflow_id": db_run.workflow_id,
        "workflow_version_id": db_run.workflow_version_id,
        "status": db_run.status.value if db_run.status else "unknown",
        "started_at": db_run.started_at,
        "completed_at": db_run.completed_at,
        "duration_s": duration_s,
        "error": error,
        "nodes": [nodes_out[gid] for gid in sorted(nodes_out)],
        "final_outputs": final_outputs,
        "url": final_outputs[0]["url"] if final_outputs else None,
        "usage": _compute_usage(db, db_run, tenant_id),
    }
