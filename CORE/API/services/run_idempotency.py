"""2400GX-n8 (tasks.md 30.4.8) -- idempotency keys for workflow-version runs.

An app firing runs from an outbox sends ``idempotency_key`` (body) or the
``Idempotency-Key`` header on ``POST /workflows/workflow-versions/{id}/run``;
a replay returns the original ``workflow_run_id`` instead of a second run.

Store: ``workflow_run_idempotency_keys`` (a NEW table -- the MCP tools/call
key lives in ``api_trigger_events`` dedupe, scoped to a trigger exposure, not
to workflow runs, so it is not reusable). It is created by the raw-SQL
migration ``20261026a_workflow_run_idempotency_keys.py`` and has no ORM model
(like ``wizard_feedback_event``); this module talks to it with plain SQL.

Scope is (tenant_id, workflow_version_id, key) -- a UNIQUE index enforces it,
so two simultaneous first calls cannot both record a key. The key row is
inserted in the SAME DB transaction as the run row (crud.create_workflow_run commits
them together; attach_run then fills the run id), so there is no window where a run exists without its key.

Retention: key rows are dev-infra metadata (not product data / audit), kept
>= 24h; ``purge_expired`` may drop OLD key rows, never runs. No scheduler job
is wired here (PG note: wire ``purge_expired`` into an APScheduler system
task if the table grows).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
MIN_RETENTION_HOURS = 24

# Idempotent DDL. The migration and the test fixture both run exactly this.
ENSURE_DDL = (
    """
CREATE TABLE IF NOT EXISTS workflow_run_idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    workflow_version_id INTEGER NOT NULL,
    idem_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    workflow_run_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_wf_run_idem_scope
    ON workflow_run_idempotency_keys (tenant_id, workflow_version_id, idem_key)
""",
    """
CREATE INDEX IF NOT EXISTS ix_wf_run_idem_created_at
    ON workflow_run_idempotency_keys (created_at)
""",
)


def resolve_key(body_key: Optional[str], header_key: Optional[str]) -> Optional[str]:
    """Effective key from body + header. ValueError (-> 422) on a bad charset /
    length, or when both are present and differ. None when neither is sent."""
    header_key = header_key.strip() if isinstance(header_key, str) else header_key
    if header_key == "":
        header_key = None
    if header_key is not None and not KEY_RE.match(header_key):
        raise ValueError("Idempotency-Key must be 1-255 chars of [A-Za-z0-9._:-]")
    if body_key is not None and not KEY_RE.match(body_key):
        raise ValueError("idempotency_key must be 1-255 chars of [A-Za-z0-9._:-]")
    if body_key is not None and header_key is not None and body_key != header_key:
        raise ValueError("idempotency_key and Idempotency-Key header differ")
    return body_key if body_key is not None else header_key


def request_hash(run_request: Any) -> str:
    """Stable sha256 of the normalized request body. The key itself and the
    (rotating) refresh token are excluded; None/empty fields normalize equal."""
    data = run_request.model_dump(
        mode="json", exclude={"idempotency_key", "refresh_token"}, exclude_none=True)
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# A claim whose run id is still NULL after this long is a crash leftover (the
# claim commits with the run row; attach_run follows within milliseconds), so
# lookup treats it as free and deletes it. Module constant, not behaviour-env.
STALE_CLAIM_SECONDS = 120


def lookup(db: Session, tenant_id: int, version_id: int, key: str):
    """The (request_hash, workflow_run_id) row for this scope, or None.
    A stale NULL-run claim (crash between claim and attach) is reaped first so
    it can never strand the key forever."""
    reaped = db.execute(text(
        "DELETE FROM workflow_run_idempotency_keys "
        "WHERE tenant_id = :t AND workflow_version_id = :v AND idem_key = :k "
        "AND workflow_run_id IS NULL "
        "AND created_at < now() - make_interval(secs => :s)"),
        {"t": tenant_id, "v": version_id, "k": key, "s": STALE_CLAIM_SECONDS})
    if reaped.rowcount:
        db.commit()
    return db.execute(text(
        "SELECT request_hash, workflow_run_id FROM workflow_run_idempotency_keys "
        "WHERE tenant_id = :t AND workflow_version_id = :v AND idem_key = :k"),
        {"t": tenant_id, "v": version_id, "k": key}).first()


def lookup_wait(db: Session, tenant_id: int, version_id: int, key: str,
                timeout: float = 3.0):
    """lookup(), but while the winner is still between its claim and its
    attach_run (run id NULL) poll briefly so a concurrent loser sees the run."""
    import time
    deadline = time.monotonic() + timeout
    row = lookup(db, tenant_id, version_id, key)
    while row is not None and row.workflow_run_id is None \
            and time.monotonic() < deadline:
        time.sleep(0.05)
        db.rollback()
        row = lookup(db, tenant_id, version_id, key)
    return row


def claim(db: Session, tenant_id: int, version_id: int, key: str,
          req_hash: str) -> bool:
    """Claim the key in the CALLER's transaction (no commit), BEFORE the run
    row is created; ``attach_run`` fills in the run id in the same transaction.
    True when this call won the key; False when the unique index says another
    call did (a concurrent in-flight winner makes this INSERT wait for its
    outcome, so a loser never creates a run row at all)."""
    try:
        got = db.execute(text(
            "INSERT INTO workflow_run_idempotency_keys "
            "(tenant_id, workflow_version_id, idem_key, request_hash, created_at) "
            "VALUES (:t, :v, :k, :h, clock_timestamp()) "
            "ON CONFLICT (tenant_id, workflow_version_id, idem_key) DO NOTHING "
            "RETURNING id"),
            {"t": tenant_id, "v": version_id, "k": key, "h": req_hash}
        ).first()
    except IntegrityError:
        return False
    return got is not None


def attach_run_nocommit(db: Session, tenant_id: int, version_id: int, key: str,
                        run_id: int) -> None:
    """Point the claimed key at its run in the CALLER's transaction (no
    commit, no flush). crud.create_workflow_run calls this right after the run
    row is flushed, so the key row never commits with workflow_run_id NULL
    while its run exists (2400GX-n8a)."""
    db.execute(text(
        "UPDATE workflow_run_idempotency_keys SET workflow_run_id = :r "
        "WHERE tenant_id = :t AND workflow_version_id = :v AND idem_key = :k"),
        {"t": tenant_id, "v": version_id, "k": key, "r": run_id})


def attach_run(db: Session, tenant_id: int, version_id: int, key: str,
               run_id: int) -> None:
    """attach_run_nocommit + commit. After n8a the run id is already set
    inside create_workflow_run's transaction, so this is a harmless no-op
    re-UPDATE (same run id) kept for callers that did not pass ``idempotency``."""
    attach_run_nocommit(db, tenant_id, version_id, key, run_id)
    db.commit()


def release(db: Session, tenant_id: int, version_id: int, key: str) -> None:
    """Drop a key row (the run it pointed at failed before dispatch, so a
    retry with the same key must be able to start a fresh attempt)."""
    db.execute(text(
        "DELETE FROM workflow_run_idempotency_keys "
        "WHERE tenant_id = :t AND workflow_version_id = :v AND idem_key = :k"),
        {"t": tenant_id, "v": version_id, "k": key})


def purge_expired(db: Session, older_than_hours: int = MIN_RETENTION_HOURS) -> int:
    """Delete key rows older than the retention window (floor 24h). Runs are
    never touched. Returns the number of rows removed (caller commits)."""
    hours = max(int(older_than_hours), MIN_RETENTION_HOURS)
    res = db.execute(text(
        "DELETE FROM workflow_run_idempotency_keys "
        "WHERE created_at < now() - make_interval(hours => :h)"), {"h": hours})
    return res.rowcount or 0
