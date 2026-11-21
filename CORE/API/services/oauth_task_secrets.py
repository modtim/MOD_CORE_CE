"""sz-4.6a — an OAuth connection as a task `secret_ref` (token → node container).

sz-4.1 vaulted one external-provider account per row in `oauth_connections`
(ciphertext only); sz-4.2/4.3 gave the broker the connect + refresh flows;
sz-4.5b let a calendar trigger pull with the decrypted token. sz-4.6a closes
the last mile: a node's `task_payload["secret_refs"]` may carry

    {"env_var": "OAUTH_ACCESS_TOKEN", "oauth_connection_id": 12}

so the customer's Drive/Github/Notion token is injected into the node
container through the SAME X25519 + AES-GCM envelope `resolve_task_secrets`
already uses for named secrets and build creds — the plaintext token never
crosses the wire in the clear and never lands in a log.

Fail-closed, in this order (a denial returns a CODE, never a token, and the
node-facing message names the CONNECTION ID only):

  invalid_ref          the ref itself is malformed (non-int / non-positive
                       connection id, or an env_var outside the env-name
                       shape) — nothing is looked up.
  connection_missing   the id is not a live connection of THIS tenant. The
                       query is tenant-scoped, so a foreign tenant's id is
                       indistinguishable from a missing one — no cross-tenant
                       existence, no cross-tenant data.
  not_permitted        the connection is another user's personal connection
                       and the run's initiator is someone else (or nobody:
                       a scheduled/system run may only use tenant-wide
                       connections). Stops user B binding user A's private
                       Drive token into B's container.
  reauth_required      the row is revoked / refresh_failed / reconnect_required
                       — the customer must re-run the connect flow.
  expiring             the access token is within 120s of its expiry and the
                       pre-emptive refresh did not succeed.
  undecryptable        the vaulted ciphertext could not be decrypted (tenant
                       key rotated) — same shape as the refresher's
                       `key_rotated` outcome.

Audit: the same secret-access rows `resolve_task_secrets` writes for NAMED
secrets — `SECRET_ACCESSED` on success, `SECRET_ACCESS_DENIED` with
`reason=<denial code>` on refusal — keyed by `resource =
"oauth_connection:<id>"`. Connection id + provider + denial code only; a
token is never a field in details, a log line, or a message.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models
from services import oauth_broker
from services import oauth_refresh

logger = logging.getLogger(__name__)

ACCESS_AUDIT_EVENT = "SECRET_ACCESSED"
DENIAL_AUDIT_EVENT = "SECRET_ACCESS_DENIED"

ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
DEFAULT_ENV_VAR = "OAUTH_ACCESS_TOKEN"
NEAR_EXPIRY_SECONDS = 120

DENIAL_CODES = frozenset({
    "connection_missing", "not_permitted", "reauth_required",
    "expiring", "undecryptable", "invalid_ref",
})


def is_oauth_secret_ref(ref) -> bool:
    """True iff ``ref`` is an OAuth-connection secret ref.

    Deliberately narrower than "a dict with an env_var": a build ref is
    recognised by its `source` (node_builder.build_dag.build_secrets.is_build_secret_ref)
    and a named ref by `secret_name`, so the OAuth branch keys on its own
    `oauth_connection_id` key and the three never overlap.
    """
    return isinstance(ref, dict) and "oauth_connection_id" in ref


def denial_message(code: str, connection_id) -> str:
    """Node-facing refusal text — connection ID only, never a token.

    The worker reports a non-200 here as `secret resolution failed: HTTP 409:
    <detail>` and marks the node failed, so the text has to tell the customer
    WHICH connection to fix and WHERE to fix it.
    """
    if code == "reauth_required":
        return (f"OAuth connection {connection_id} needs to be reconnected in "
                "Settings → Integrations")
    if code == "expiring":
        return (f"OAuth connection {connection_id} access token is expiring — "
                "reconnect it in Settings → Integrations")
    if code == "undecryptable":
        return (f"OAuth connection {connection_id} token could not be "
                "decrypted (tenant key rotated) — reconnect it in "
                "Settings → Integrations")
    if code == "not_permitted":
        return (f"OAuth connection {connection_id} is not connected to the "
                "user who started this run — connect it in Settings → "
                "Integrations")
    if code == "connection_missing":
        return "OAuth connection referenced by this node no longer exists"
    return "OAuth secret reference is invalid"


def _audit(db: Session, tenant_id: Optional[int], user_id: Optional[int],
           event_name: str, connection_id: int,
           provider: Optional[str], reason: Optional[str]) -> None:
    """One secret-access audit row: connection id + provider + CODE only.

    Mirrors the rows `resolve_task_secrets` writes for named secrets
    (`secrets_service._audit_secret_access_denied`): best-effort, never
    raises, and the details carry no token material of any kind.
    """
    try:
        details: dict = {
            "resource": f"oauth_connection:{connection_id}",
            "connection_id": connection_id,
        }
        if provider:
            details["provider"] = provider
        if reason:
            details["reason"] = reason
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id,
            event_name=event_name, details=details,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — audit must never break resolution
        import audit
        audit._record_audit_write_failure("oauth_task_secret_audit", exc,
                                          context=f"connection_id={connection_id}")
        try:
            db.rollback()
        except Exception:
            pass


def _run_initiator_user_id(db: Session, task) -> Optional[int]:
    """The user who started the run this task belongs to (None = nobody).

    `WorkflowRun.triggered_by_user_id` is the authority sz-4.6a checks
    ownership against; a scheduled / system run has none, which is NOT a
    wildcard — such a run may only use tenant-wide connections.
    """
    if task.workflow_run_id is None:
        return None
    run = (db.query(models.WorkflowRun)
           .filter(models.WorkflowRun.id == task.workflow_run_id)
           .one_or_none())
    if run is None:
        return None
    return run.triggered_by_user_id


def resolve_oauth_secret_ref(db: Session, task, ref) -> tuple:
    """Resolve one OAuth secret ref → (token, None) or (None, denial_code).

    Never raises: the caller is the batch resolver in
    `routers/worker_tasks.py` resolve_task_secrets, which must keep resolving
    the task's other refs. The token it returns is plaintext IN MEMORY ONLY —
    the caller envelope-encrypts it (X25519 + AES-GCM) before it leaves the
    process, exactly as it does for named secrets and build creds.
    """
    env_var = ref.get("env_var") or DEFAULT_ENV_VAR
    if not ENV_VAR_RE.match(str(env_var)):
        _audit(db, task.tenant_id, None, DENIAL_AUDIT_EVENT,
               _safe_connection_id(ref), None, "invalid_ref")
        return None, "invalid_ref"

    cid = ref.get("oauth_connection_id")
    # bool is an int in Python — a ref of `True` must not become connection 1.
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        _audit(db, task.tenant_id, None, DENIAL_AUDIT_EVENT,
               _safe_connection_id(ref), None, "invalid_ref")
        return None, "invalid_ref"

    # Tenant-scoped: a foreign tenant's id is simply not found, so neither its
    # existence nor any of its data is readable (same shape as calendar_poll's
    # _connection). Soft-deleted rows are tombstones — a disconnected
    # connection is `connection_missing`, not a re-connect prompt.
    conn = (db.query(models.OAuthConnection)
            .filter(models.OAuthConnection.id == cid,
                    models.OAuthConnection.tenant_id == task.tenant_id,
                    models.OAuthConnection.deleted_at.is_(None))
            .one_or_none())
    if conn is None:
        _audit(db, task.tenant_id, None, DENIAL_AUDIT_EVENT, cid, None,
               "connection_missing")
        return None, "connection_missing"

    initiator = _run_initiator_user_id(db, task)
    if conn.user_id is not None and conn.user_id != initiator:
        _audit(db, task.tenant_id, initiator, DENIAL_AUDIT_EVENT, cid,
               conn.provider, "not_permitted")
        return None, "not_permitted"

    if conn.status != "active":
        _audit(db, task.tenant_id, initiator, DENIAL_AUDIT_EVENT, cid,
               conn.provider, "reauth_required")
        return None, "reauth_required"

    if _near_expiry(conn):
        # The 60s refresh tick renews well in time; a node that catches a
        # token about to die refreshes it HERE first so the run never sees a
        # dead token. A refresh that cannot happen is a refusal, never a
        # silently-stale token.
        outcome = _try_refresh(db, conn)
        if outcome == "reconnect_required":
            _audit(db, task.tenant_id, initiator, DENIAL_AUDIT_EVENT, cid,
                   conn.provider, "reauth_required")
            return None, "reauth_required"
        if outcome != "refreshed":
            _audit(db, task.tenant_id, initiator, DENIAL_AUDIT_EVENT, cid,
                   conn.provider, "expiring")
            return None, "expiring"

    try:
        token = oauth_broker.get_access_token(db, conn)
    except oauth_broker.OAuthStateError as exc:
        code = ("undecryptable" if "undecryptable" in str(exc)
                else "reauth_required")
        _audit(db, task.tenant_id, initiator, DENIAL_AUDIT_EVENT, cid,
               conn.provider, code)
        return None, code

    _audit(db, task.tenant_id, conn.user_id, ACCESS_AUDIT_EVENT, cid,
           conn.provider, None)
    return token, None


def _safe_connection_id(ref) -> int:
    """Connection id for an audit row on a malformed ref (0 = unusable).

    The raw ref value is never echoed into the audit details — an
    attacker-supplied `oauth_connection_id` is arbitrary JSON.
    """
    cid = ref.get("oauth_connection_id")
    if isinstance(cid, int) and not isinstance(cid, bool) and cid > 0:
        return cid
    return 0


def _near_expiry(conn) -> bool:
    """True when the access token dies within NEAR_EXPIRY_SECONDS."""
    exp = conn.access_token_expires_at
    if exp is None:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= datetime.now(timezone.utc) + timedelta(
        seconds=NEAR_EXPIRY_SECONDS)


def _try_refresh(db: Session, conn) -> str:
    """Pre-emptive refresh of a near-expiry token; outcome string, never a token.

    `services.oauth_refresh.refresh_connection` owns the same frozen provider
    registry + httpx `_TEST_TRANSPORT` seam the broker uses, so tests drive it
    with a MockTransport. A provider we cannot refresh (unknown / raising) is
    reported as a transient failure — the caller refuses the value.
    """
    try:
        return oauth_refresh.refresh_connection(db, conn)
    except oauth_broker.OAuthStateError:
        return "transient_failure"
