"""Fortress 1.3 — `secret_access_log` writer (append-only, names only).

`secret_access_log` (migration 20261012q_fortress_13_access_denial_logs.py,
NOT yet applied live) is the audit sink for every read/attempt against a
tenant secret: the NAME of the secret, who/what asked for it (`accessor` =
'worker:<id>' / 'user:<id>'), and the outcome ('granted' | 'denied'). ISO
27001: audit tables are append-only — this module only ever INSERTs, never
commits, and the migration's guard trigger refuses UPDATE/DELETE/TRUNCATE.

NAMES ONLY. Nothing here accepts, stores, or logs a secret VALUE: the
columns are names/ids/outcomes and the writer exposes no `value`
parameter at all (see the test's `inspect.signature` check).

models.py is lead-only, so there is no ORM class for the table yet: the
writer is raw `text()` SQL (same posture as
services/adversary_findings_store.py and
services/failure_prediction_recorder.py). The INSERT runs inside a
SAVEPOINT (`db.begin_nested()`) so a bad row — or a table that does not
exist yet, pre-migration — can never poison the caller's session; the
writer logs the exception CLASS name and returns False.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text as satext

from services import platform_settings as psr

logger = logging.getLogger(__name__)

TABLE = "secret_access_log"
ENABLED_KEY = "fortress.secret_access_log_enabled"
OUTCOMES = frozenset({"granted", "denied"})
_NAME_MAX = 255
_ACCESSOR_MAX = 255
_REASON_MAX = 500
_BATCH_CAP = 100

_INSERT_SQL = satext(f"""
    INSERT INTO {TABLE}
        (tenant_id, workflow_run_id, node_run_id, node_id,
         secret_name, accessor, outcome, reason)
    VALUES (:tenant_id, :workflow_run_id, :node_run_id, :node_id,
            :secret_name, :accessor, :outcome, :reason)
    RETURNING id
""")

psr.register(
    ENABLED_KEY,
    bool,
    True,
    group="compliance",
    description="Fortress 1.3 — write secret_access_log rows at all. "
                "Default ON: every secret read/attempt leaves a "
                "name-only audit row (never the value). A tenant-scope "
                "OFF silences logging for that tenant only; it is a "
                "logging kill switch, not a permission.",
)


def _enabled(db, tenant_id: Optional[int]) -> bool:
    """Kill-switch read: tenant row > platform row > default True.

    A settings-read failure must never break the caller, so any error
    falls back to the default (ON) and is logged by class name only.
    """
    try:
        return bool(psr.get_setting(ENABLED_KEY, db=db, tenant_id=tenant_id))
    except Exception as exc:  # noqa: BLE001 — logging must not break audit
        logger.warning("secret_access_log: setting read skipped: %s",
                       type(exc).__name__)
        return True


def _clean_text(value: Any, limit: int) -> Optional[str]:
    """Non-empty str, truncated to `limit`; None/blank/non-str -> None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def record_secret_access(db, *, tenant_id: int, secret_name: str,
                         accessor: str, outcome: str,
                         workflow_run_id: int = None, node_run_id: int = None,
                         node_id: str = None, reason: str = None) -> bool:
    """Append ONE secret_access_log row; True = a row landed.

    Validation first, SQL second: a non-positive tenant_id, a blank
    secret_name/accessor, or an outcome outside {'granted','denied'}
    returns False WITHOUT touching the DB (the CHECK's own vocabulary, so
    the caller's bug is loud in the return, not in a DB error). Names are
    truncated to 255, reason to 500.

    The INSERT runs in a SAVEPOINT and this function NEVER commits — the
    caller commits. Any DB error (including UndefinedTable, i.e. the
    migration not applied yet) rolls back only the savepoint, logs the
    exception CLASS name, and returns False: the caller's session stays
    usable.
    """
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int):
        return False
    if tenant_id <= 0:
        return False
    name = _clean_text(secret_name, _NAME_MAX)
    who = _clean_text(accessor, _ACCESSOR_MAX)
    if name is None or who is None:
        return False
    if outcome not in OUTCOMES:
        return False
    if not _enabled(db, tenant_id):
        return False

    params = {
        "tenant_id": tenant_id,
        "workflow_run_id": (workflow_run_id
                            if isinstance(workflow_run_id, int)
                            and not isinstance(workflow_run_id, bool) else None),
        "node_run_id": (node_run_id
                        if isinstance(node_run_id, int)
                        and not isinstance(node_run_id, bool) else None),
        "node_id": _clean_text(node_id, _NAME_MAX),
        "secret_name": name,
        "accessor": who,
        "outcome": outcome,
        "reason": _clean_text(reason, _REASON_MAX),
    }
    try:
        with db.begin_nested():
            row = db.execute(_INSERT_SQL, params).first()
        return row is not None
    except Exception as exc:  # a logging failure must never break the caller
        logger.warning("secret_access_log: skipped: %s", type(exc).__name__)
        return False


def record_secret_accesses(db, rows: Iterable[Mapping]) -> int:
    """Batch form: loop record_secret_access over `rows`, cap 100.

    Returns how many rows landed. A row that is not a Mapping, or that
    fails validation/DB, counts 0 and never stops the loop.
    """
    written = 0
    for index, row in enumerate(rows):
        if index >= _BATCH_CAP:
            break
        if not isinstance(row, Mapping):
            continue
        try:
            if record_secret_access(db, **dict(row)):
                written += 1
        except TypeError:
            continue  # a malformed row (bad kwargs) must not kill the batch
    return written
