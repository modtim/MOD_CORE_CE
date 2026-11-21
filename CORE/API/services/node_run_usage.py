"""2400GX-c11-J24 (30.6.11) — stack-model-call token metering on NodeRun.

The Go worker (c11-J3, CORE/Worker/internal/transport/transport.go) attaches
``usage_tokens`` to its completion body:

    {"models": [{"model": str, "prompt_tokens"?: int,
                 "completion_tokens"?: int, "total_tokens"?: int,
                 "images"?: int}, ...]}

It is worker-supplied data and ANY tenant-built node can write
``/data/output/result.json`` (S7 trust boundary), so the API stores it ONLY
when the NodeRun's node is the platform-owned Stack Model Call node, and only
when it validates strictly (S8). Everything else is dropped.

Node resolution is SERVER-stored, never from the completion body: the run's
WorkflowVersion.graph_json (written by the save path, read by the orchestrator
at dispatch) matched on the NodeRun's own graph node id
(``NodeRun.workflow_node.graph_node_id``), with the orchestrator's
server-written ``task_payload["node_data"]`` as the fallback — the worker never
echoes node_data into /complete, and neither source is worker-writable. The
platform check is the same DB-authoritative one the orchestrator uses
(``workflow_orchestrator.is_platform_owned_node``: NodeDefinition row by
``nodeDefinitionId`` with ``tenant_id IS NULL``), plus the seeded Stack Model
Call identity (``seed_stack_model_call_node.py``: name "Stack Model Call",
``executable_path = platform_image("stack-model-call:latest")``).

``node_id`` is added by the API from the NodeRun, never taken from the worker.

Nothing here raises on bad input and nothing mutates the transaction in a way
that can poison the commit: the caller owns the transaction, this only sets one
JSONB attribute. Logs never carry the payload (S3) — counts and model strings
stay out of every log line.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    conlist,
    constr,
    ValidationError,
)

from sqlalchemy.orm import Session

from models import (  # noqa: F401 — models imported for its side effects too
    NodeDefinition, NodeRun, WorkflowRun, WorkflowVersion,
)
from services.workflow_orchestrator import (
    _image_repository, is_platform_owned_node,
)
from utils.registries import platform_image

logger = logging.getLogger(__name__)

MAX_USAGE_JSON_BYTES = 2048
MAX_MODELS = 4
STACK_MODEL_CALL_IMAGE = platform_image("stack-model-call:latest")
STACK_MODEL_CALL_NAME = "Stack Model Call"


class UsageModelEntry(BaseModel):
    """One model-call metering entry. Strict ints: ``StrictInt`` refuses bool
    (bool is not an int for pydantic strict mode), float and the numeric string
    ``"5"`` — lax mode would coerce all three."""
    model_config = ConfigDict(extra="forbid")

    model: constr(pattern=r"^[A-Za-z0-9._:/-]{1,128}$")
    prompt_tokens: Optional[StrictInt] = None
    completion_tokens: Optional[StrictInt] = None
    total_tokens: Optional[StrictInt] = None
    images: Optional[StrictInt] = None


class NodeRunUsageTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: conlist(UsageModelEntry, min_length=1, max_length=MAX_MODELS)


def _bounds_ok(entry: UsageModelEntry) -> bool:
    """0..10^9 on every count (S8). StrictInt handles the TYPE; the range is
    checked here so an out-of-range value drops through the same debug path."""
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "images"):
        val = getattr(entry, key)
        if val is not None and not (0 <= val <= 10**9):
            return False
    return True


def _resolve_node_data(db: Session, node_run, task) -> Optional[dict]:
    """Server-stored node_data for this NodeRun — never the completion body."""
    if node_run is None:
        return None
    wn = getattr(node_run, "workflow_node", None)
    graph_gid = (wn.graph_node_id if wn else None)

    run = (db.query(WorkflowRun)
           .filter(WorkflowRun.id == node_run.workflow_run_id).first()
           if node_run.workflow_run_id else None)
    if run and graph_gid is not None:
        version = (db.get(WorkflowVersion, run.workflow_version_id)
                   if getattr(run, "workflow_version_id", None) else None)
        for node in ((version.graph_json or {}).get("nodes") or []) if version else []:
            if not isinstance(node, dict) or node.get("id") != graph_gid:
                continue
            data = node.get("data")
            if isinstance(data, dict) and data.get("nodeDefinitionId"):
                return data

    payload = (task.task_payload or {}) if task is not None else {}
    fallback = payload.get("node_data")
    if isinstance(fallback, dict) and fallback.get("nodeDefinitionId"):
        return fallback
    return None


def record_usage_tokens(db: Session, *, task, node_run, raw) -> bool:
    """Validate + store worker-supplied usage on the NodeRun. Returns True only
    when it was written. Never raises on bad input; a DB-level failure is left
    to the caller's try (the router wraps this call)."""
    if raw is None or node_run is None:
        return False
    if not isinstance(raw, dict):
        logger.debug("usage_tokens dropped for task %s: not a dict", getattr(task, "id", "?"))
        return False
    try:
        if len(json.dumps(raw)) > MAX_USAGE_JSON_BYTES:
            logger.debug("usage_tokens dropped for task %s: over %d bytes",
                         getattr(task, "id", "?"), MAX_USAGE_JSON_BYTES)
            return False
    except (TypeError, ValueError):
        return False

    try:
        parsed = NodeRunUsageTokens(**raw)
    except ValidationError as exc:
        # S3: the field ERRORS name the offending model string / counts — log
        # the exception TYPE only, never the payload.
        logger.debug("usage_tokens dropped for task %s: schema invalid (%s)",
                     getattr(task, "id", "?"), type(exc).__name__)
        return False

    if not all(_bounds_ok(e) for e in parsed.models):
        logger.debug("usage_tokens dropped for task %s: counts out of bounds",
                     getattr(task, "id", "?"))
        return False

    node_data = _resolve_node_data(db, node_run, task)
    if node_data is None:
        logger.debug("usage_tokens dropped for task %s: no server node_data for node run %s",
                     getattr(task, "id", "?"), getattr(node_run, "id", "?"))
        return False
    if not is_platform_owned_node(db, node_data):
        logger.debug("usage_tokens dropped for task %s: node not platform-owned",
                     getattr(task, "id", "?"))
        return False

    nd = db.query(NodeDefinition).filter(
        NodeDefinition.id == node_data.get("nodeDefinitionId")).first()
    if nd is None or nd.deleted_at is not None:
        return False
    if not _is_stack_model_call(nd):
        logger.debug("usage_tokens dropped for task %s: node is not the Stack Model Call node",
                     getattr(task, "id", "?"))
        return False

    node_id = node_run.workflow_node.graph_node_id if getattr(node_run, "workflow_node", None) else None
    node_run.usage_tokens = {
        "models": [dict(e.model_dump(exclude_none=True), node_id=node_id)
                   for e in parsed.models],
    }
    return True


def _is_stack_model_call(nd: NodeDefinition) -> bool:
    """The seeded Stack Model Call identity (seed_stack_model_call_node.py:43
    name, :62 executable_path = platform_image("stack-model-call:latest")).
    Match on the IMAGE (repository-qualified, tag/digest-insensitive) or the
    seeded platform name — both only ever set by MOD's own seed, and the row
    already passed the tenant_id IS NULL platform check."""
    return (
        _image_repository(nd.executable_path or "")
        == _image_repository(STACK_MODEL_CALL_IMAGE)
    ) or (nd.name or "") == STACK_MODEL_CALL_NAME
