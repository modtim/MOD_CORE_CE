"""expr 10.4/10.5 — services/workflow_parameter_versions.py (store service).

Writes/reads `workflow_parameter_versions` (migrations
20261010d_workflow_parameter_versions + 20261012n_workflow_parameter_versions):
ONE row per parameter SET a differentiable workflow's training loop produced,
rolled back, or hand-edited. Append-only — rollback APPENDS a copy, it never
updates or removes an older row (ISO 27001: soft deletes only, and this module
never even soft-deletes).

Posture (services/autonomous_proposal_store.py + services/causal_graph_persist.py
style): raw `sqlalchemy.text` SQL — the ORM class for this table is being
replaced by the lead, so importing models.py here would bind us to a shape that
is about to change. Every write runs inside ONE savepoint (`db.begin_nested()`)
so a bad row can never poison the caller's transaction; this module NEVER
commits — the caller commits. DB errors are swallowed: the exception CLASS name
is logged and None/[] returned, the outer session stays usable. A bad `source`
word raises ValueError BEFORE any SQL (a caller bug, not a DB condition).

`version_number` is the next per-workflow_version number, computed UNDER
`pg_advisory_xact_lock(hashtext('wpv'), workflow_version_id)` so two concurrent
training steps can never mint the same number (the migration's UNIQUE
uq_workflow_parameter_versions_version_number is the backstop).

Legacy columns from 20261010d stay: `version_id` UUID NOT NULL has no DB
default, so every INSERT generates a uuid4 for it; `training_step_id` UUID
stays NULL (the legacy training-loop ids are not this store's to invent).
"""
from __future__ import annotations

import json
from product_names import themed_logger
import uuid
from typing import Any, Mapping, Optional

from sqlalchemy import text as satext

from services import platform_settings as psr

logger = themed_logger(__name__, 'workflows')

_TABLE = "workflow_parameter_versions"
_LOCK_NS = "wpv"
_SOURCES = frozenset({"training_step", "rollback", "manual"})

_MAX_BYTES_KEY = "expr.parameter_version_max_bytes"
_MAX_BYTES_DEFAULT = 1_048_576
_MAX_BYTES_MIN = 1_024
_MAX_BYTES_MAX = 16_777_216
_LIMIT_DEFAULT = 50
_LIMIT_MIN, _LIMIT_MAX = 1, 200

_COLS = ("id", "tenant_id", "workflow_id", "workflow_version_id",
         "training_step_run_id", "version_number", "parameters",
         "parent_version_id", "source", "created_by", "created_at",
         "deleted_at", "version_id", "training_step_id")


def _validate_max_bytes(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < _MAX_BYTES_MIN or value > _MAX_BYTES_MAX:
        raise ValueError(f"must be {_MAX_BYTES_MIN}..{_MAX_BYTES_MAX}, "
                         f"got {value!r}")


psr.register(
    _MAX_BYTES_KEY, int, _MAX_BYTES_DEFAULT, validator=_validate_max_bytes,
    description="expr 10.4 — hard cap (bytes) on the serialized `parameters` "
                "JSONB of ONE workflow_parameter_versions row. A snapshot "
                "bigger than this is refused (no row): a half-parameter-set "
                "is worse than no history at all. Default 1 MiB, range "
                "1 KiB..16 MiB.",
    group="expr",
)


def _max_bytes(db) -> int:
    """Resolve the size cap, fail-closed to the registered default."""
    try:
        value = psr.get_setting(_MAX_BYTES_KEY, db=db)
        _validate_max_bytes(value)
        return int(value)
    except Exception:  # noqa: BLE001 — a broken settings layer keeps the default
        return _MAX_BYTES_DEFAULT


def _params_json(parameters: Any, cap: int) -> Optional[str]:
    """JSON string for the JSONB column, or None when oversize/unserializable.

    Oversize is REFUSED (None), never truncated: a half parameter set would
    silently corrupt a training snapshot's rollback lineage.
    """
    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be a mapping")
    try:
        s = json.dumps(dict(parameters))
    except (TypeError, ValueError):
        return None
    if len(s.encode("utf-8")) > cap:
        return None
    return s


_LOCK_SQL = satext(
    f"SELECT pg_advisory_xact_lock(hashtext(:ns), :workflow_version_id)"
)

_STEP_DUP_SQL = satext(
    f"SELECT id FROM {_TABLE}"
    f"   WHERE workflow_version_id = :wv AND training_step_run_id = :step"
    f"   LIMIT 1"
)

# tenant join: the graph must belong to (tenant, workflow). FKs are enforced
# replica-side in tests, so the join is the real ownership check.
_GRAPH_SQL = satext(
    f"SELECT COUNT(*) FROM workflow_versions AS wvv"
    f"   JOIN workflows AS wf ON wvv.workflow_id = wf.id"
    f"   WHERE wvv.id = :workflow_version_id AND wf.tenant_id = :tenant_id"
)

_PARENT_SQL = satext(
    f"SELECT COUNT(*) FROM {_TABLE}"
    f"   WHERE id = :parent_version_id AND workflow_version_id = "
    f":workflow_version_id AND deleted_at IS NULL"
)

_NEXT_NUMBER_SQL = satext(
    f"SELECT COALESCE(MAX(version_number), 0) + 1 FROM {_TABLE}"
    f"   WHERE workflow_version_id = :workflow_version_id"
)

_INSERT_SQL = satext(
    f"INSERT INTO {_TABLE}"
    f"   (tenant_id, workflow_id, workflow_version_id, training_step_run_id,"
    f"    version_number, parameters, parent_version_id, source, created_by,"
    f"    version_id)"
    f" VALUES (:tenant_id, :workflow_id, :workflow_version_id,"
    f"        :training_step_run_id, :version_number,"
    f"        CAST(:parameters AS JSONB), :parent_version_id, :source,"
    f"        :created_by, :version_id)"
    f" RETURNING id"
)

_GET_SQL = satext(
    f"SELECT {', '.join(_COLS)} FROM {_TABLE}"
    f"   WHERE id = :version_id AND tenant_id = :tenant_id"
    f"   LIMIT 1"
)

_LIST_SQL = satext(
    f"SELECT {', '.join(_COLS)} FROM {_TABLE}"
    f"   WHERE tenant_id = :tenant_id AND workflow_id = :workflow_id"
    f"   AND deleted_at IS NULL"
    f"   ORDER BY created_at DESC, id DESC"
    f" LIMIT :limit"
)

_LIST_SQL_GRAPH = satext(
    f"SELECT {', '.join(_COLS)} FROM {_TABLE}"
    f"   WHERE tenant_id = :tenant_id AND workflow_id = :workflow_id"
    f"   AND workflow_version_id = :workflow_version_id"
    f"   AND deleted_at IS NULL"
    f"   ORDER BY created_at DESC, id DESC"
    f" LIMIT :limit"
)


def _row_to_dict(row) -> dict:
    return {k: (str(v) if k == "version_id" else v)
            for k, v in zip(_COLS, row)}


def record_version(db, *, tenant_id: int, workflow_id: int,
                   workflow_version_id: int, parameters: Any,
                   source: str, training_step_run_id: Optional[int] = None,
                   parent_version_id: Optional[int] = None,
                   created_by: Optional[int] = None) -> Optional[int]:
    """Append one parameter-version row; return its id (None on refusal).

    Validates `source` against the migration's CHECK vocab BEFORE any SQL
    (ValueError — a caller bug, documented). Ownership: `workflow_version_id`
    must belong to (tenant, workflow) via the workflow_versions -> workflows
    join, and a given `parent_version_id` must be a live row on the SAME
    graph; either mismatch refuses (None, logged). `parameters` is size-capped
    by the `expr.parameter_version_max_bytes` platform setting BEFORE any SQL.

    Idempotent on retry: an existing live row for the same
    (workflow_version_id, training_step_run_id) returns THAT id — the
    migration's partial UNIQUE uq_workflow_parameter_versions_step makes a
    second row impossible, so a re-driven training step must not blow up.

    version_number = next per graph, minted under the advisory lock. DB error
    -> savepoint rollback, exception CLASS name logged, None, session usable.
    NEVER commits.
    """
    if source not in _SOURCES:
        raise ValueError(
            f"workflow_parameter_versions: source must be one of "
            f"training_step|rollback|manual, got {source!r}"
        )
    cap = _max_bytes(db)
    params_json = _params_json(parameters, cap)  # non-mapping -> ValueError
    if params_json is None:
        logger.warning(
            "workflow_parameter_versions: record refused (parameters oversize"
            " or unserializable) for graph %s",
            workflow_version_id,
        )
        return None

    try:
        with db.begin_nested():
            if int(db.execute(_GRAPH_SQL, {
                "workflow_version_id": workflow_version_id,
                "tenant_id": tenant_id,
            }).first()[0]) < 1:
                logger.warning(
                    "workflow_parameter_versions: record refused "
                    "(workflow_version_id %s not owned by tenant %s / "
                    "workflow %s)",
                    workflow_version_id, tenant_id, workflow_id,
                )
                return None

            if training_step_run_id is not None:
                dup = db.execute(_STEP_DUP_SQL, {
                    "wv": workflow_version_id, "step": training_step_run_id,
                }).first()
                if dup is not None:
                    return int(dup[0])

            if parent_version_id is not None:
                if int(db.execute(_PARENT_SQL, {
                    "parent_version_id": parent_version_id,
                    "workflow_version_id": workflow_version_id,
                }).first()[0]) < 1:
                    logger.warning(
                        "workflow_parameter_versions: record refused "
                        "(parent_version_id %s not a live row on graph %s)",
                        parent_version_id, workflow_version_id,
                    )
                    return None

            db.execute(_LOCK_SQL, {"ns": _LOCK_NS,
                                   "workflow_version_id": workflow_version_id})
            number = int(db.execute(
                _NEXT_NUMBER_SQL,
                {"workflow_version_id": workflow_version_id}).first()[0])

            row = db.execute(_INSERT_SQL, {
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "workflow_version_id": workflow_version_id,
                "training_step_run_id": training_step_run_id,
                "version_number": number,
                "parameters": params_json,
                "parent_version_id": parent_version_id,
                "source": source,
                "created_by": created_by,
                "version_id": str(uuid.uuid4()),
            }).first()
        return int(row[0]) if row is not None else None
    except Exception as exc:  # noqa: BLE001 — the savepoint already rolled back
        logger.warning(
            "workflow_parameter_versions: record skipped for graph %s: %s",
            workflow_version_id, type(exc).__name__,
        )
        return None


def rollback_to(db, *, tenant_id: int, workflow_id: int,
                target_version_id: int,
                created_by: Optional[int] = None) -> Optional[int]:
    """Append a NEW row copying `target_version_id`'s parameters, source='rollback'.

    Append-only: the target and every older row stay untouched — rollback is
    history, not an edit. The new row's parent_version_id is the target (the
    lineage edge), and it inherits the target's (workflow_id,
    workflow_version_id) so the caller's workflow_id is only a consistency
    anchor: a mismatch refuses. Refuses on a missing/foreign/deleted target.
    DB error -> CLASS name logged, None, session usable. NEVER commits.
    """
    try:
        with db.begin_nested():
            target = db.execute(
                satext(
                    f"SELECT workflow_id, workflow_version_id, parameters,"
                    f" tenant_id FROM {_TABLE}"
                    f"   WHERE id = :target_version_id AND deleted_at IS NULL"
                    f"   LIMIT 1"
                ),
                {"target_version_id": target_version_id},
            ).first()
            if target is None:
                logger.warning(
                    "workflow_parameter_versions: rollback refused "
                    "(target_version_id %s missing or soft-deleted)",
                    target_version_id,
                )
                return None
            t_workflow_id, t_graph_id, t_params, t_tenant_id = (
                target[0], target[1], target[2], target[3])
            if (int(t_tenant_id) != int(tenant_id)
                    or int(t_workflow_id) != int(workflow_id)):
                logger.warning(
                    "workflow_parameter_versions: rollback refused "
                    "(target_version_id %s belongs to workflow %s / tenant "
                    "%s, not %s / %s)",
                    target_version_id, t_workflow_id, t_tenant_id,
                    workflow_id, tenant_id,
                )
                return None
            params_json = (t_params if isinstance(t_params, str)
                           else json.dumps(t_params))
            cap = _max_bytes(db)
            if len(params_json.encode("utf-8")) > cap:
                logger.warning(
                    "workflow_parameter_versions: rollback refused "
                    "(target parameters oversize) for target %s",
                    target_version_id,
                )
                return None

            db.execute(_LOCK_SQL, {"ns": _LOCK_NS,
                                   "workflow_version_id": t_graph_id})
            number = int(db.execute(
                _NEXT_NUMBER_SQL, {"workflow_version_id": t_graph_id}
            ).first()[0])
            row = db.execute(_INSERT_SQL, {
                "tenant_id": tenant_id,
                "workflow_id": int(t_workflow_id),
                "workflow_version_id": int(t_graph_id),
                "training_step_run_id": None,
                "version_number": number,
                "parameters": params_json,
                "parent_version_id": int(target_version_id),
                "source": "rollback",
                "created_by": created_by,
                "version_id": str(uuid.uuid4()),
            }).first()
        return int(row[0]) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workflow_parameter_versions: rollback skipped for target %s: %s",
            target_version_id, type(exc).__name__,
        )
        return None


def list_versions(db, *, tenant_id: int, workflow_id: int,
                  workflow_version_id: Optional[int] = None,
                  limit: int = _LIMIT_DEFAULT) -> list[dict]:
    """Newest-first parameter history for a workflow (soft-deleted excluded).

    `workflow_version_id` narrows to ONE graph. `limit` clamped to 1..200.
    Never raises: DB error -> CLASS name logged, [].
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _LIMIT_DEFAULT
    n = max(_LIMIT_MIN, min(_LIMIT_MAX, n))
    try:
        if workflow_version_id is None:
            rows = db.execute(_LIST_SQL, {
                "tenant_id": tenant_id, "workflow_id": workflow_id,
                "limit": n,
            }).fetchall()
        else:
            rows = db.execute(_LIST_SQL_GRAPH, {
                "tenant_id": tenant_id, "workflow_id": workflow_id,
                "workflow_version_id": workflow_version_id, "limit": n,
            }).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workflow_parameter_versions: list skipped for workflow %s: %s",
            workflow_id, type(exc).__name__,
        )
        return []


def get_version(db, *, tenant_id: int, version_id: int) -> Optional[dict]:
    """One row by id, tenant-scoped (soft-deleted rows are still readable).

    None when absent or another tenant's. DB error -> CLASS name logged, None.
    """
    try:
        row = db.execute(_GET_SQL, {
            "version_id": version_id, "tenant_id": tenant_id,
        }).first()
        return _row_to_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workflow_parameter_versions: get skipped for id %s: %s",
            version_id, type(exc).__name__,
        )
        return None
