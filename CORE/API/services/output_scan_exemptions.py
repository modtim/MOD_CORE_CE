"""Fortress 3.5-d — output-safety exemption consult (inline path).

A single lookup helper over ``models.OutputScanExemption``
(migrations/20261012u_fortress_36c_output_scan_exemptions.py). This module
only reads the table; the exemption rows themselves are created/approved
through a separate admin surface (out of scope here).

Consult is inert when no rows exist for a tenant (owner rec, Q2 — no new
platform setting): an empty table always returns ``None``, so wiring this in
changes nothing until an admin actually grants an exemption.

Fortress 3.6c API half (891) — the write side now lives next to the read side:
``list_live_exemptions`` / ``create_exemption`` / ``soft_delete_exemption``,
driven by routers/compliance_output_scanners.py (GET/POST
/platform/compliance/output-scanners/exemptions + /exemptions/{id}/revoke).
Revoke is a SOFT delete only (ISO 27001): the table carries a BEFORE DELETE
trigger (trg_no_delete_output_scan_exemptions) that RAISEs feature_not_supported,
so a hard delete is never even attempted here — ``deleted_at`` +
``deleted_by_user_id`` are stamped instead and the row stays attributable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_

import models

logger = logging.getLogger(__name__)


def exemption_covers(
    db,
    *,
    tenant_id: int,
    workflow_id: Optional[int],
    node_kind: Optional[str],
    scanner: Optional[str],
    finding_code: Optional[str],
    now: Optional[datetime] = None,
) -> Optional["models.OutputScanExemption"]:
    """Return the live exemption row covering this call, or ``None``.

    Matching semantics (migrations/20261012u_fortress_36c_output_scan_exemptions.py,
    read directly off the CREATE TABLE + partial-UNIQUE comments — never
    widened beyond what that migration documents):

      * ``tenant_id`` — exact match, always. There is no cross-tenant
        wildcard; every exemption is scoped to one tenant.
      * ``workflow_id`` — the migration docstring: "NULL = tenant-wide".
        A NULL exemption row matches any workflow (including an unresolved
        one); a non-NULL row only matches that exact workflow id.
      * ``scanner`` — the migration docstring: "NULL = any scanner".
      * ``finding_code`` — the migration docstring: "NULL = any finding".
      * ``node_kind`` — not given its own "NULL = any ..." sentence in the
        migration docstring, but it is declared nullable and keyed into the
        SAME partial UNIQUE index (``uq_output_scan_exemptions_live_key``)
        alongside workflow_id/scanner/finding_code via the identical
        ``COALESCE(col, <sentinel>)`` idiom — i.e. it is the same kind of
        scope dimension, so NULL is treated as "any node kind" too.

    For each of those four columns: the row matches when the column is NULL
    (wildcard) OR equals the call's value. Critically, this is NOT
    symmetric — when the *call's* value is unknown (``None``), only a
    wildcard (NULL) row on that column can match; an unresolved workflow_id/
    node_kind must never accidentally satisfy a narrowly-scoped exemption
    (never widen scope in the other direction).

    Live = ``deleted_at IS NULL`` (ISO 27001 soft-delete only) AND
    ``approved_by_user_id IS NOT NULL`` (dual control — an exemption nobody
    has approved covers nothing) AND
    ``expires_at IS NULL OR expires_at > now``.

    Fails CLOSED: any DB error is caught and logged by exception TYPE only
    (never params/content) and returns ``None`` — a lookup failure must
    never manufacture an exemption, only ever miss one it should have found.
    """
    now = now or datetime.now(timezone.utc)
    try:
        q = db.query(models.OutputScanExemption).filter(
            models.OutputScanExemption.tenant_id == tenant_id,
            models.OutputScanExemption.deleted_at.is_(None),
            models.OutputScanExemption.approved_by_user_id.isnot(None),
            or_(
                models.OutputScanExemption.expires_at.is_(None),
                models.OutputScanExemption.expires_at > now,
            ),
        )
        for col, value in (
            (models.OutputScanExemption.workflow_id, workflow_id),
            (models.OutputScanExemption.node_kind, node_kind),
            (models.OutputScanExemption.scanner, scanner),
            (models.OutputScanExemption.finding_code, finding_code),
        ):
            if value is None:
                q = q.filter(col.is_(None))
            else:
                q = q.filter(or_(col.is_(None), col == value))
        return q.order_by(models.OutputScanExemption.id.asc()).first()
    except Exception as e:  # noqa: BLE001 — fail closed, never raise
        logger.warning("exemption_covers lookup failed: %s", type(e).__name__)
        return None


# ---------------------------------------------------------------------------
# Write side (891) — create / list / soft-delete. The read side above stays
# the only consult; nothing here widens matching semantics.
# ---------------------------------------------------------------------------

class DuplicateExemption(Exception):
    """The live key (tenant, workflow, node_kind, scanner, finding_code) is
    already exempted — raised against uq_output_scan_exemptions_live_key so the
    router can answer 409 instead of minting a second live row."""


class SelfApprovalNotAllowed(Exception):
    """Dual control (891 reviewer fix): the API never lets the creator of an
    exemption also be its approver. The migration's
    ck_output_scan_exemptions_dual_control CHECK still permits
    ``self_approved=True`` for a future out-of-band (PSQL) grant, but every
    row minted through THIS service is held to the strict two-person rule —
    unlike services/privileged_elevation_approval.py's solo-mode escape
    hatch, there is no setting here that relaxes it, so the router always
    raises 403 on approver == creator."""


def _live_key(row) -> tuple:
    """The partial-UNIQUE key, verbatim off the migration's COALESCE idiom."""
    return (
        row.tenant_id,
        row.workflow_id or 0,
        row.node_kind or "",
        row.scanner or "",
        row.finding_code or "",
    )


def list_live_exemptions(db, tenant_id: int, workflow_id: Optional[int] = None):
    """Live (``deleted_at IS NULL``) exemption rows for ONE tenant, optionally
    narrowed to one workflow. Revoked rows are never listed (they stay in the
    table for attribution only). Ordered by id for a stable listing."""
    q = db.query(models.OutputScanExemption).filter(
        models.OutputScanExemption.tenant_id == tenant_id,
        models.OutputScanExemption.deleted_at.is_(None),
    )
    if workflow_id is not None:
        q = q.filter(models.OutputScanExemption.workflow_id == workflow_id)
    return q.order_by(models.OutputScanExemption.id.asc()).all()


def create_exemption(
    db,
    *,
    tenant_id: int,
    reason: str,
    created_by_user_id: Optional[int] = None,
    workflow_id: Optional[int] = None,
    node_kind: Optional[str] = None,
    scanner: Optional[str] = None,
    finding_code: Optional[str] = None,
    expires_at: Optional[datetime] = None,
):
    """Mint one PENDING exemption row (reviewer fix, 891): creation NEVER
    approves. There is no ``approved_by_user_id`` parameter here on purpose —
    a create call cannot pre-stamp its own approver, so a row always starts
    with ``approved_by_user_id``/``approved_at`` NULL and ``self_approved``
    False. ``exemption_covers`` demands ``approved_by_user_id IS NOT NULL``,
    so a freshly-created row covers nothing until a SECOND person calls
    ``approve_exemption`` on it (dual control, enforced at the router: the
    approver must not be the creator — see ``SelfApprovalNotAllowed``).

    ``reason`` is REQUIRED: blank/whitespace raises ValueError (the router
    answers 400 — an exemption nobody can justify is exactly what a compliance
    audit reports, so the API refuses to mint it; the DB's own
    ck_output_scan_exemptions_reason_required CHECK is the backstop).

    Raises DuplicateExemption on a duplicate live key (after rolling the
    failed insert back, so the caller's session stays usable). Returns the
    row's fields as a plain dict.
    """
    if reason is None or not str(reason).strip():
        raise ValueError("reason is required")

    row = models.OutputScanExemption(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        node_kind=node_kind,
        scanner=scanner,
        finding_code=finding_code,
        reason=str(reason).strip(),
        created_by_user_id=created_by_user_id,
        approved_by_user_id=None,
        approved_at=None,
        self_approved=False,
        expires_at=expires_at,
    )
    db.add(row)
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001 — classify the failed insert only
        db.rollback()
        if _is_live_key_conflict(e):
            raise DuplicateExemption(
                "live exemption already covers this key") from e
        raise
    db.refresh(row)
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "workflow_id": row.workflow_id,
        "node_kind": row.node_kind,
        "scanner": row.scanner,
        "finding_code": row.finding_code,
        "reason": row.reason,
        "created_by_user_id": row.created_by_user_id,
        "approved_by_user_id": row.approved_by_user_id,
        "self_approved": row.self_approved,
        "expires_at": row.expires_at,
    }


def approve_exemption(db, exemption, approver_user_id: Optional[int]):
    """Second-person approval (891 reviewer fix): stamps
    ``approved_by_user_id``/``approved_at`` so ``exemption_covers`` can find
    the row. Dual control is enforced HERE, not just documented: when
    ``approver_user_id`` equals the row's own ``created_by_user_id`` this
    raises ``SelfApprovalNotAllowed`` instead of silently flagging
    ``self_approved=True`` — the router is the only caller and always passes
    the AUTHENTICATED approver's own id (never a client-supplied one), so
    this check is the real maker-checker gate, matching the
    services/privileged_elevation_approval.py convention of refusing a
    self-approval outright (this table has no solo-mode setting, so there is
    no escape hatch — every exemption needs a genuine second admin).

    ``self_approved`` on the row therefore always ends up False through this
    path; the CHECK still accepts True for an out-of-band (PSQL) grant.
    """
    if exemption is None:
        return None
    if (approver_user_id is not None
            and exemption.created_by_user_id is not None
            and approver_user_id == exemption.created_by_user_id):
        raise SelfApprovalNotAllowed(
            "the approver must be a different admin than the creator")
    exemption.approved_by_user_id = approver_user_id
    exemption.approved_at = datetime.now(timezone.utc)
    exemption.self_approved = False
    db.commit()
    db.refresh(exemption)
    return {
        "id": exemption.id,
        "approved_by_user_id": exemption.approved_by_user_id,
        "self_approved": exemption.self_approved,
    }


def _is_live_key_conflict(exc) -> bool:
    """True when a failed flush is the partial-UNIQUE live-key index (or the
    reason CHECK) firing, not an unrelated DB fault. Matched on the index NAME
    only — never on row content."""
    try:
        orig = getattr(exc, "orig", None)
        name = str(orig.args[0]) if (orig is not None and orig.args) else str(exc)
    except Exception:  # noqa: BLE001 — best-effort classification
        return False
    return "uq_output_scan_exemptions_live_key" in name


def soft_delete_exemption(db, exemption, actor_user_id: Optional[int]):
    """SOFT delete only (ISO 27001): stamp ``deleted_at`` +
    ``deleted_by_user_id`` and nothing else — the row keeps its reason and
    authorship so the exemption stays attributable after it stops applying.

    A hard delete is NEVER attempted: the table's BEFORE DELETE trigger
    (trg_no_delete_output_scan_exemptions) RAISEs feature_not_supported, so
    ``db.delete()`` would poison the session. Already-revoked rows are a no-op
    (returns revoked=False).
    """
    if exemption is None or exemption.deleted_at is not None:
        return {"revoked": False, "id": getattr(exemption, "id", None)}
    exemption.deleted_at = datetime.now(timezone.utc)
    exemption.deleted_by_user_id = actor_user_id
    db.commit()
    return {"revoked": True, "id": exemption.id}


def get_exemption_by_id(db, exemption_id: Optional[int]):
    """The row with this id, or ``None``. Lives in the service so the router
    keeps its no-raw-``db.query(`` posture; the CALLER (not this helper) decides
    whether the row is in scope — tenant match + revoked checks stay at the
    HTTP boundary because they are about the caller, not the row."""
    if exemption_id is None:
        return None
    return db.query(models.OutputScanExemption).filter(
        models.OutputScanExemption.id == exemption_id).first()
