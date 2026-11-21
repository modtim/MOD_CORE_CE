"""FX.117 p3b — per-secret allowed-workflows scope (the RECORD, the gate, the admin API).

FX.117 (owner Q104=A) ships the customer secret store. p3a added exact-value
masking of the tenant secret VALUES the run injects. p3b lets a tenant admin
restrict a tenant secret so ONLY the listed workflows may resolve it: with no
active scope rows the secret behaves exactly as today (every workflow of the
tenant).

Three halves, one module:

  1. The RECORD — `models.SecretWorkflowScope` (NEW table `secret_workflow_scopes`,
     one row per allowed (secret, workflow) pair). Soft-delete only (ISO 27001):
     replacing a scope soft-deletes the removed rows and inserts the new ones,
     so the compliance trail keeps every scope a secret ever had.
  2. The GATE — `assert_run_scope` at the run's secret-resolution point
     (`routers/worker_tasks.py` POST /workers/tasks/{id}/secrets, the endpoint
     the Go worker calls to have `task_payload.secret_refs` turned into
     container env). A denied pair is NOT injected: a WARNING naming the
     secret only (never the value), an audited SECRET_ACCESS_DENIED row, and
     the worker fails the node with "secret <name> is not allowed for this
     workflow".
  3. The ADMIN API — GET/PUT /secrets/{secret_id}/workflow-scope
     (routers/secrets.py, the same per-secret gate the existing secrets routes
     use; the tenant comes from the token, another tenant's secret is 404).
     PUT replaces the whole list (soft-delete removed + insert added) and
     mirrors the list into the p2 `Secret.allowed_workflow_ids` bag so the
     by-name run-token gate (`services.secrets_service._check_run_context_allowed`)
     and this table can never disagree.

Masking (FX.117 p3a, `services.secret_masking.run_secret_values`) is
deliberately NOT scoped: a secret the run references must still be masked in
persisted node text even when scope refused to inject it — masking protects
the log, it does not authorise the value.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy.orm import Session
from datetime import datetime, timezone

import models
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

# Audit label carried by models.AuditAction (and the live `auditaction` PG enum
# via migrations/20261007zzzd_fx117_p3b_secret_workflow_scopes.py). AuditEvent
# rows use the same UPPERCASE name in `event_name` — the label the enum-sync
# guard requires.
SCOPE_AUDIT_EVENT = "SECRET_SCOPE_UPDATED"
DENIAL_AUDIT_EVENT = "SECRET_ACCESS_DENIED"
DENIAL_REASON = "run_workflow_not_in_scope"


class BadRequestError(ServiceError):
    """Payload shape the caller must fix (router maps -> 400)."""

    def __init__(self, message: str = "Bad request", *, details: None = None):
        super().__init__(message, code="bad_request", details=details)


class ForbiddenError(ServiceError):
    """Caller lacks the permission for this action (router maps -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details: None = None):
        super().__init__(message, code="forbidden", details=details)


def _live_rows(db: Session, secret_id: int) -> list:
    """Every LIVE scope row for one secret (tombstones excluded).

    `SecretWorkflowScope` is not in the global soft-delete filter list in
    database.py, so the `deleted_at` predicate is written here explicitly —
    and every reader of this table goes through this helper, so no callsite
    can accidentally count a tombstone as an allowed workflow.
    """
    return (
        db.query(models.SecretWorkflowScope)
        .filter(
            models.SecretWorkflowScope.secret_id == int(secret_id),
            models.SecretWorkflowScope.deleted_at.is_(None),
        )
        .all()
    )


def allowed_workflow_ids(db: Session, secret_id: int) -> List[int]:
    """Workflow ids the secret may currently be injected into ([] = unrestricted)."""
    return sorted({int(row.workflow_id) for row in _live_rows(db, secret_id)})


def is_unrestricted(db: Session, secret_id: int) -> bool:
    """True when the secret has NO active scope rows = today's behaviour."""
    return not _live_rows(db, secret_id)


def workflow_allowed(db: Session, secret_id: int, workflow_id: Optional[int]) -> bool:
    """May this workflow resolve this secret?

    No active rows -> allowed (unrestricted). With rows, the workflow id must
    be one of them. A missing/None workflow_id is refused when rows exist:
    the scope can only be honoured against a concrete workflow, so an
    unidentified consumer must not get a scoped value (fail closed).
    """
    rows = _live_rows(db, secret_id)
    if not rows:
        return True
    if workflow_id is None:
        return False
    return workflow_id in {int(row.workflow_id) for row in rows}


def _audit_denial(db: Session, db_secret: "models.Secret", secret_name: str,
                  *, workflow_run_id: Optional[int] = None,
                  node_id: Optional[str] = None,
                  tenant_id: Optional[int] = None) -> None:
    """Audit a scope DENIAL. Best-effort: an audit failure never changes the
    decision (the value is withheld either way).

    Mirrors `services.secrets_service._audit_secret_access_denied` — same
    event_name, same `reason` — so the p2 by-name gate and the p3b table gate
    produce one indistinguishable denial shape in the audit trail. Carries the
    secret NAME and id only; the value is never in an audit row.
    """
    details = {
        "secret_id": db_secret.id,
        "secret_name": secret_name,
        "reason": DENIAL_REASON,
    }
    if workflow_run_id is not None:
        details["workflow_run_id"] = workflow_run_id
    if node_id:
        details["node_id"] = node_id
    try:
        db.add(
            models.AuditEvent(
                tenant_id=(tenant_id if tenant_id is not None
                           else getattr(db_secret, "tenant_id", None)),
                user_id=None,
                event_name=DENIAL_AUDIT_EVENT,
                details=details,
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — audit is a nicety, the denial stands
        logger.debug("Scope-denial audit failed for secret %s: %s",
                     db_secret.id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def assert_run_scope(db: Session,
                     db_secret: "models.Secret",
                     secret_name: str,
                     *,
                     workflow_run_id: Optional[int] = None,
                     node_id: Optional[str] = None) -> bool:
    """The p3b run-scope gate. Returns True when the value may be injected.

    Called at the run's secret-resolution point. When the secret has active
    scope rows and the run's workflow is not among them: a WARNING naming the
    secret ONLY (never the value), an audited SECRET_ACCESS_DENIED row, and
    False — the caller withholds the value and fails the node with
    `SecretScopeError.detail` ("secret <name> is not allowed for this
    workflow"). Never raises: the caller is a batch resolver that must keep
    resolving the other refs of the same task.
    """
    workflow_id = None
    if workflow_run_id:
        run = (db.query(models.WorkflowRun)
               .filter(models.WorkflowRun.id == int(workflow_run_id))
               .first())
        workflow_id = run.workflow_id if run is not None else None

    if workflow_allowed(db, db_secret.id, workflow_id):
        return True

    logger.warning(
        "Secret '%s' (id %s) is scoped to workflows %s; run %s (workflow %s) "
        "is not allowed — value NOT injected.",
        secret_name, db_secret.id,
        allowed_workflow_ids(db, db_secret.id),
        workflow_run_id, workflow_id,
    )
    _audit_denial(db, db_secret, secret_name,
                  workflow_run_id=workflow_run_id, node_id=node_id)
    return False


class SecretScopeError(ServiceError):
    """The node-facing refusal (the worker fails the node with `.message`).

    Raised nowhere — the resolver returns it as a value so one denied ref
    cannot abort the resolution of the task's other refs.
    """

    def __init__(self, message: str, *, details: None = None):
        super().__init__(message, code="secret_scope_denied", details=details)


def node_error(secret_name: str) -> str:
    """The exact node-failure message the worker reports (secret NAME only)."""
    return f"secret {secret_name} is not allowed for this workflow"


# ---------------------------------------------------------------------------
# Admin API: replace / read the scope list
# ---------------------------------------------------------------------------


def get_scope(db: Session, secret_id: int) -> List[int]:
    """The current allowed-workflow list for a secret ([] = unrestricted)."""
    return allowed_workflow_ids(db, secret_id)


def _crud_get_secret_by_id(db: Session, secret_id: int, tenant_id: int):
    """Tenant-scoped secret lookup (crud/secret_cud.get_secret_by_id)."""
    from crud.secret_cud import get_secret_by_id
    return get_secret_by_id(db, secret_id=secret_id, tenant_id=tenant_id)


def _load_secret_for_admin(db: Session, secret_id: int,
                           current_user: "models.User") -> "models.Secret":
    """The tenant-scoped lookup + the same per-secret gate the existing
    secrets routes use (services/secrets_service.get_secret / update_secret).

    Another tenant's secret is a 404 (NotFoundError), never a 403 — the
    tenant comes from the token, so a foreign secret is simply not there.
    """
    import services.secrets_service as secrets_service
    db_secret = _crud_get_secret_by_id(db, secret_id, current_user.tenant_id)
    if db_secret is None:
        raise NotFoundError("Secret not found.")
    if not secrets_service.can_access_secret(db, current_user, secret_id, "edit"):
        raise ForbiddenError(
            "You don't have edit access to this secret."
        )
    return db_secret


def get_scope_for_admin(db: Session, secret_id: int,
                        current_user: "models.User") -> List[int]:
    """Admin read: the allowed-workflow list ([] = unrestricted).

    Same per-secret gate the existing GET /secrets/{id} route uses
    ('view'); another tenant's secret is a 404 (tenant from the token).
    """
    import services.secrets_service as secrets_service
    db_secret = _crud_get_secret_by_id(db, secret_id, current_user.tenant_id)
    if db_secret is None:
        raise NotFoundError("Secret not found.")
    if not secrets_service.can_access_secret(db, current_user, secret_id, "view"):
        raise ForbiddenError("You don't have access to this secret.")
    return allowed_workflow_ids(db, db_secret.id)


def replace_scope_for_admin(db: Session, secret_id: int,
                            workflow_ids: List[int],
                            current_user: "models.User"):
    """Admin write: (db_secret, added, removed, new_list). Replaces the list."""
    db_secret = _load_secret_for_admin(db, secret_id, current_user)
    added, removed, new_ids = replace_scope(
        db, db_secret, workflow_ids, current_user)
    return db_secret, added, removed, new_ids


def replace_scope(db: Session,
                  db_secret: "models.Secret",
                  workflow_ids: List[int],
                  current_user: "models.User") -> List[int]:
    """Replace a secret's allowed-workflow list: soft-delete the removed rows,
    insert the added ones, keep the untouched ones.

    `workflow_ids` is the FULL new list; [] clears the scope (back to
    "any workflow of the tenant"). Every workflow must belong to the SAME
    tenant as the secret (BadRequestError otherwise) — a tenant admin cannot
    scope a secret onto someone else's workflow.

    Also mirrors the list into `Secret.allowed_workflow_ids` (the p2 admin
    bag read by the by-name run-token gate) so the table and that gate can
    never disagree. Audits one SECRET_SCOPE_UPDATED row with the added/removed
    ids and the acting user — never a secret value. Returns
    (added, removed, new_list).
    """
    tenant_id = int(db_secret.tenant_id)
    wanted: set = set()
    for raw in (workflow_ids or []):
        try:
            wid = int(raw)
        except (TypeError, ValueError):  # noqa: BLE001 — payload shape
            raise BadRequestError("workflow-scope entries must be workflow ids")
        wf = (db.query(models.Workflow)
              .filter(models.Workflow.id == wid)
              .first())
        if wf is None:
            raise BadRequestError(
                f"Workflow {wid} does not exist."
            )
        if int(wf.tenant_id) != tenant_id:
            raise BadRequestError(
                f"Workflow {wid} belongs to another tenant; a secret's scope "
                "may only name this tenant's workflows."
            )
        wanted.add(wid)

    existing = _live_rows(db, db_secret.id)
    existing_ids = {int(r.workflow_id) for r in existing}
    removed = sorted(existing_ids - wanted)
    added = sorted(wanted - existing_ids)

    now = datetime.now(timezone.utc)
    for row in existing:
        if int(row.workflow_id) in removed:
            row.deleted_at = now
            row.deleted_by_user_id = (current_user.id if current_user else None)
    for wid in added:
        db.add(
            models.SecretWorkflowScope(
                tenant_id=tenant_id,
                secret_id=int(db_secret.id),
                workflow_id=wid,
                created_by_user_id=(current_user.id if current_user else None),
            )
        )
    db_secret.allowed_workflow_ids = sorted(wanted) if wanted else None
    db.commit()

    try:
        db.add(
            models.AuditEvent(
                tenant_id=tenant_id,
                user_id=(current_user.id if current_user else None),
                event_name=SCOPE_AUDIT_EVENT,
                details={
                    "secret_id": db_secret.id,
                    "secret_name": db_secret.name,
                    "added_workflow_ids": added,
                    "removed_workflow_ids": removed,
                    "allowed_workflow_ids": sorted(wanted),
                },
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — audit is a nicety
        logger.warning("Scope audit write failed for secret %s: %s",
                       db_secret.id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return added, removed, sorted(wanted)
