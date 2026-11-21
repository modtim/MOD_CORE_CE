"""sz-3.3 + 3.8 — CRDT snapshot tick + disaster-recovery restore service.

Periodic snapshot writer (tick), manual snapshot capture, and restore from
snapshot when the live CRDT state row is missing/corrupt.

Settings via psr.register:
  crdt.snapshot_interval_s   (default 60)
  crdt.snapshot_keep         (default 50 live snapshots per workflow)
  crdt.snapshot_max_bytes    (default 16 MiB)

ISO 27001: NEVER hard-delete snapshot rows; retention pruning stamps
deleted_at + deleted_by instead.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import models
from services import platform_settings as psr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform-settings registrations (sz-3.3 tunables)
# ---------------------------------------------------------------------------

def _validate_keep(value: int) -> None:
    if int(value) < 1:
        raise ValueError(
            f"crdt.snapshot_keep must be >= 1, got {value!r}")


def _validate_max_bytes(value: int) -> None:
    if int(value) < 0:
        raise ValueError(
            f"crdt.snapshot_max_bytes must be >= 0, got {value!r}")


psr.register(
    "crdt.snapshot_interval_s",
    int,
    60,
    group="crdt",
    description="sz-3.3 — how often (seconds) the periodic snapshot job "
                "fires. Default 60.",
)
psr.register(
    "crdt.snapshot_keep",
    int,
    50,
    group="crdt",
    validator=_validate_keep,
    description="sz-3.3 — number of live snapshots retained per "
                "(tenant, workflow). Oldest beyond this are soft-deleted. "
                "Default 50.",
)
psr.register(
    "crdt.snapshot_max_bytes",
    int,
    16 * 1024 * 1024,
    group="crdt",
    validator=_validate_max_bytes,
    description="sz-3.3 — maximum ydoc_state byte size before a tick "
                "skips taking a snapshot for that row. "
                "Default 16 MiB (16777216).",
)

# Valid reason values from the DB CHECK constraint.
_VALID_SNAPSHOT_REASONS: frozenset = frozenset(
    {"periodic", "manual", "pre_restore", "shutdown"}
)


# ---------------------------------------------------------------------------
# Helper: sha256 of bytes -> hex string (64 chars).
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# snapshot_tick — periodic snapshot writer (sz-3.3)
# ---------------------------------------------------------------------------

def snapshot_tick(
    db,
    *,
    now: Optional[datetime] = None,
    batch: int = 200,
) -> Dict[str, int]:
    """Take a snapshot of every live WorkflowCrdtState whose bytes changed
    since the workflow's latest *live* snapshot.  Returns counts dict.

    Each workflow's row is processed inside its own SAVEPOINT so one bad
    row never aborts the tick for others.  Bounded by ``batch`` rows.
    Ordering: oldest updated_at first (most likely changed).

    Returns {
        "ticked": int,           # snapshots written
        "skipped_unchanged": int,
        "skipped_over_max_bytes": int,
        "skipped_retention_soft_deleted": int,
    }
    """
    if now is None:
        now = datetime.now(timezone.utc)

    keep = int(psr.get_setting("crdt.snapshot_keep"))
    max_bytes = int(psr.get_setting("crdt.snapshot_max_bytes"))

    counts: Dict[str, int] = {
        "ticked": 0,
        "skipped_unchanged": 0,
        "skipped_over_max_bytes": 0,
        "skipped_retention_soft_deleted": 0,
    }

    # Fetch live state rows, oldest updated_at first, bounded by batch.
    rows = (
        db.query(models.WorkflowCrdtState)
        .filter(models.WorkflowCrdtState.deleted_at.is_(None))
        .order_by(models.WorkflowCrdtState.updated_at.asc())
        .limit(batch)
        .all()
    )

    for row in rows:
        sp = db.begin_nested()  # per-workflow SAVEPOINT
        try:
            _tick_one_row(db, row, now, keep, max_bytes, counts)
            sp.commit()
        except Exception:
            sp.rollback()
            logger.exception(
                "snapshot_tick: SAVEPOINT rollback for state id=%s", row.id
            )
            counts.setdefault("errors", 0)
            counts["errors"] += 1

    return counts


def _tick_one_row(
    db,
    state_row: models.WorkflowCrdtState,
    now: datetime,
    keep: int,
    max_bytes: int,
    counts: dict,
) -> None:
    """Try to snapshot a single WorkflowCrdtState row."""

    ydoc_bytes = state_row.ydoc_state
    if ydoc_bytes is None or len(ydoc_bytes) == 0:
        counts["skipped_unchanged"] += 1
        return

    # If the row is over the byte cap, skip entirely.
    if len(ydoc_bytes) > max_bytes:
        counts["skipped_over_max_bytes"] += 1
        return

    sha = _sha256_hex(ydoc_bytes)

    # Find the latest *live* snapshot for this (tenant, workflow).
    latest_snap = (
        db.query(models.WorkflowCrdtSnapshots)
        .filter(
            models.WorkflowCrdtSnapshots.tenant_id == state_row.tenant_id,
            models.WorkflowCrdtSnapshots.workflow_id == state_row.workflow_id,
            models.WorkflowCrdtSnapshots.deleted_at.is_(None),
        )
        .order_by(models.WorkflowCrdtSnapshots.created_at.desc())
        .first()
    )

    # If latest snapshot has matching sha → unchanged, skip.
    if latest_snap and latest_snap.sha256 == sha:
        counts["skipped_unchanged"] += 1
        return

    # Take the snapshot.
    snap = models.WorkflowCrdtSnapshots(
        tenant_id=state_row.tenant_id,
        workflow_id=state_row.workflow_id,
        ydoc_state=ydoc_bytes,
        sha256=sha,
        byte_size=len(ydoc_bytes),
        reason="periodic",
        created_at=now,
        created_by=None,
    )
    db.add(snap)
    db.flush()
    counts["ticked"] += 1

    # Run retention: soft-delete oldest beyond keep.
    _enforce_retention(db, state_row.tenant_id, state_row.workflow_id, keep, now)


def _enforce_retention(
    db,
    tenant_id: int,
    workflow_id: int,
    keep: int,
    now: datetime,
) -> None:
    """Soft-delete oldest live snapshots beyond *keep* for the
    (tenant, workflow) pair."""
    all_live = (
        db.query(models.WorkflowCrdtSnapshots)
        .filter(
            models.WorkflowCrdtSnapshots.tenant_id == tenant_id,
            models.WorkflowCrdtSnapshots.workflow_id == workflow_id,
            models.WorkflowCrdtSnapshots.deleted_at.is_(None),
        )
        .order_by(models.WorkflowCrdtSnapshots.created_at.asc())
        .all()
    )
    if len(all_live) <= keep:
        return
    # Delete the excess oldest.
    excess = all_live[:len(all_live) - keep]
    for s in excess:
        s.deleted_at = now
        s.deleted_by = None  # system marker
    db.flush()


# ---------------------------------------------------------------------------
# take_snapshot — manual / pre-restore / shutdown capture (sz-3.3)
# ---------------------------------------------------------------------------

def take_snapshot(
    db,
    tenant_id: int,
    workflow_id: int,
    *,
    reason: str,
    actor_user_id: Optional[int] = None,
) -> Optional[models.WorkflowCrdtSnapshots]:
    """Capture a snapshot of the current live CRDT state for the given
    workflow.  *reason* is validated against the CHECK set.

    Returns the created snapshot row or None if no live state exists.
    Raises ValueError for an invalid reason.
    """
    if reason not in _VALID_SNAPSHOT_REASONS:
        raise ValueError(
            f"Invalid snapshot reason {reason!r}; "
            f"must be one of {sorted(_VALID_SNAPSHOT_REASONS)}"
        )

    state_row = (
        db.query(models.WorkflowCrdtState)
        .filter(
            models.WorkflowCrdtState.tenant_id == tenant_id,
            models.WorkflowCrdtState.workflow_id == workflow_id,
            models.WorkflowCrdtState.deleted_at.is_(None),
        )
        .first()
    )
    if state_row is None:
        return None

    ydoc_bytes = state_row.ydoc_state or b""
    sha = _sha256_hex(ydoc_bytes)

    snap = models.WorkflowCrdtSnapshots(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        ydoc_state=ydoc_bytes,
        sha256=sha,
        byte_size=len(ydoc_bytes),
        reason=reason,
        created_by=actor_user_id,
    )
    db.add(snap)
    db.flush()
    return snap


# ---------------------------------------------------------------------------
# restore_latest — disaster-recovery restore (sz-3.8)
# ---------------------------------------------------------------------------

def restore_latest(
    db,
    tenant_id: int,
    workflow_id: int,
    *,
    actor_user_id: int,
) -> Dict[str, Any]:
    """Restore the live WorkflowCrdtState from the newest live snapshot
    whose sha256 matches its ydoc_state bytes.

    When the state row is missing / empty / corrupt:
      - Take a "pre_restore"-style snapshot first if the existing state
        row exists and has data (reason validated against CHECK set).
      - Rebuild the state row from the newest valid snapshot.
      - Skip snapshots whose sha256 does not match their ydoc_state
        (corrupted on disk) and report them.

    Tenant-scoped: LookupError if tenant_id or workflow_id do not exist.

    Returns {
        "restored_from_snapshot_id": int | None,
        "skipped_corrupt": List[int],   # snapshot ids that failed integrity
        "snapshot_id": int,             # snapshot id used (if restored)
        "pre_restore_snapshot_id": int | None,  # pre-restore snap id
    }
    """
    # --- tenant-scope guard ---
    tenant = db.query(models.Tenant).filter(
        models.Tenant.id == tenant_id
    ).first()
    if tenant is None:
        raise LookupError(f"tenant_id={tenant_id} not found")

    workflow = db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
        models.Workflow.tenant_id == tenant_id,
    ).first()
    if workflow is None:
        raise LookupError(
            f"workflow_id={workflow_id} not found for tenant_id={tenant_id}"
        )

    skipped_corrupt: List[int] = []

    # --- find newest live snapshot ---
    latest_snap = (
        db.query(models.WorkflowCrdtSnapshots)
        .filter(
            models.WorkflowCrdtSnapshots.tenant_id == tenant_id,
            models.WorkflowCrdtSnapshots.workflow_id == workflow_id,
            models.WorkflowCrdtSnapshots.deleted_at.is_(None),
        )
        .order_by(models.WorkflowCrdtSnapshots.created_at.desc())
        .first()
    )

    if latest_snap is None:
        return {
            "restored_from_snapshot_id": None,
            "skipped_corrupt": [],
            "snapshot_id": None,
            "pre_restore_snapshot_id": None,
        }

    # Validate integrity of the latest snapshot (sha matches bytes).
    if not _snap_intact(latest_snap):
        skipped_corrupt.append(latest_snap.id)
        # Walk older snapshots.
        remaining = (
            db.query(models.WorkflowCrdtSnapshots)
            .filter(
                models.WorkflowCrdtSnapshots.tenant_id == tenant_id,
                models.WorkflowCrdtSnapshots.workflow_id == workflow_id,
                models.WorkflowCrdtSnapshots.deleted_at.is_(None),
                models.WorkflowCrdtSnapshots.id != latest_snap.id,
            )
            .order_by(models.WorkflowCrdtSnapshots.created_at.desc())
            .all()
        )
        for s in remaining:
            if _snap_intact(s):
                latest_snap = s
                break
            skipped_corrupt.append(s.id)

    if latest_snap is None or not _snap_intact(latest_snap):
        return {
            "restored_from_snapshot_id": None,
            "skipped_corrupt": skipped_corrupt,
            "snapshot_id": None,
            "pre_restore_snapshot_id": None,
        }

    # --- check the live state row ---
    state_row = (
        db.query(models.WorkflowCrdtState)
        .filter(
            models.WorkflowCrdtState.tenant_id == tenant_id,
            models.WorkflowCrdtState.workflow_id == workflow_id,
            models.WorkflowCrdtState.deleted_at.is_(None),
        )
        .first()
    )

    pre_restore_id: Optional[int] = None
    need_restore = False

    if state_row is None:
        need_restore = True
    elif state_row.ydoc_state is None or len(state_row.ydoc_state) == 0:
        need_restore = True
    elif _sha256_hex(state_row.ydoc_state) != latest_snap.sha256:
        # Corrupt state — sha mismatch vs stored sha.
        need_restore = True

    if need_restore and state_row is not None and state_row.ydoc_state:
        # Take a pre-restore snapshot first.
        ps = take_snapshot(
            db, tenant_id, workflow_id,
            reason="pre_restore", actor_user_id=actor_user_id,
        )
        if ps:
            pre_restore_id = ps.id

    # Rebuild or overwrite the state row.
    ydoc_bytes = latest_snap.ydoc_state
    state_row = (
        db.query(models.WorkflowCrdtState)
        .filter(
            models.WorkflowCrdtState.tenant_id == tenant_id,
            models.WorkflowCrdtState.workflow_id == workflow_id,
            models.WorkflowCrdtState.deleted_at.is_(None),
        )
        .first()
    )

    if state_row is None:
        state_row = models.WorkflowCrdtState(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            ydoc_state=ydoc_bytes,
            state_vector=None,
            update_count=0,
            byte_size=len(ydoc_bytes),
        )
        db.add(state_row)
    else:
        state_row.ydoc_state = ydoc_bytes
        state_row.state_vector = None
        state_row.update_count = 0
        state_row.byte_size = len(ydoc_bytes)

    db.flush()

    # Write audit event "crdt.state.restored".
    _write_audit_event(
        db, tenant_id, actor_user_id,
        "crdt.state.restored",
        {
            "workflow_id": workflow_id,
            "restored_from_snapshot_id": latest_snap.id,
            "pre_restore_snapshot_id": pre_restore_id,
        },
    )

    return {
        "restored_from_snapshot_id": latest_snap.id,
        "skipped_corrupt": skipped_corrupt,
        "snapshot_id": latest_snap.id,
        "pre_restore_snapshot_id": pre_restore_id,
    }


def _snap_intact(snap: models.WorkflowCrdtSnapshots) -> bool:
    """True if the stored sha256 matches the bytes of ydoc_state."""
    if snap.ydoc_state is None:
        return False
    return _sha256_hex(snap.ydoc_state) == snap.sha256


def _write_audit_event(
    db,
    tenant_id: int,
    user_id: int,
    event_name: str,
    details: dict,
) -> None:
    """Write an audit-event row for the CRDT restore operation."""
    row = models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=event_name,
        details=details,
    )
    db.add(row)
