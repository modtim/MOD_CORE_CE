"""Megaplan 25.6 / Group Q.4 — byte-purge retention for ISO-off installs.

Owner rule (binding, 2026-09-27):
  * MOD's own platform is always ISO 27001: records are soft-deleted and
    NEVER purged.
  * Byte-purge retention (this module) is opt-in and only takes effect on
    customer on-prem installs that explicitly set MOD_ISO_MODE=false.
    SaaS always forces ISO on (see services.scheduler.iso_mode_on()).

Two-stage lifecycle for an eligible "intermediate" WorkflowNodeOutput row:

  Stage 1 (soft-delete): once older than MOD_RETENTION_INTERMEDIATE_DAYS
    (default 30) and not pinned/final, flip is_visible=False + hidden_at,
    and append a WorkflowOutputAuditLog("hidden") tombstone. This is the
    SAME table/pattern services/scheduler.py::_run_retention_cleanup and
    services/assets_cogs_files_service.py's legal-hold helpers already use
    — nothing new invented here.

  Stage 2 (byte purge): once a soft-deleted row has sat past
    MOD_RETENTION_PURGE_GRACE_DAYS (default 7), remove the underlying
    object bytes from tenant storage (utils/minio_client.MinioClient),
    but ONLY when no other visible WorkflowNodeOutput row for the same
    tenant still references the same content_hash (content-addressed
    storage is deduped per (content_hash, tenant_id) — see
    models.ContentBlob). The WorkflowNodeOutput row and its tombstones
    are never removed; a second tombstone action="bytes_purged" records
    the purge.

"Pinned" note: WorkflowNodeOutput (models.py ~L2893) has no dedicated
`pinned` boolean. The closest existing safe "don't touch" signal is
`legal_hold` (model docstring: "legal_hold=True prevents any deletion"),
so this module treats legal_hold as the pinned-equivalent. "Final/terminal
output" is the existing `is_terminal` column (models.py ~L2926: "Final
workflow output?"). An intermediate is only a candidate when
is_terminal=False AND legal_hold=False AND is_visible=True.

Network I/O rule: stage 2 never calls storage while a DB transaction is
open. Reads happen first, the session is committed (ending that
transaction) before any MinIO call, and tombstone writes happen only
after the I/O completes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

import models

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


@dataclass
class RetentionCandidate:
    id: int
    tenant_id: int
    content_hash: str
    size_bytes: int
    created_at: Optional[datetime]


@dataclass
class RetentionPlan:
    """Dry-run report. Read-only — never mutates the DB."""
    max_age_days: int
    cutoff: datetime
    candidates: List[RetentionCandidate] = field(default_factory=list)
    candidate_count: int = 0
    total_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "max_age_days": self.max_age_days,
            "cutoff": self.cutoff.isoformat(),
            "candidate_count": self.candidate_count,
            "total_bytes": self.total_bytes,
        }


@dataclass
class _PurgeCandidate:
    id: int
    tenant_id: int
    content_hash: str
    size_bytes: int
    still_referenced: bool


def plan_expired_intermediates(db, *, now=None, max_age_days=None) -> RetentionPlan:
    """Dry-run: find intermediate outputs eligible for stage-1 retention.

    Eligible = is_visible=True (not already soft-deleted) AND
    is_terminal=False (not a final workflow output — models.py ~L2926)
    AND legal_hold=False (the pinned-equivalent safe signal — see module
    docstring) AND created_at older than max_age_days.

    Pure read. Never mutates the DB. Safe to call regardless of ISO mode —
    callers decide what, if anything, to do with the report.
    """
    if max_age_days is None:
        max_age_days = _env_int("MOD_RETENTION_INTERMEDIATE_DAYS", 30)
    now = _now(now)
    cutoff = now - timedelta(days=max_age_days)

    rows = (
        db.query(models.WorkflowNodeOutput)
        .filter(
            models.WorkflowNodeOutput.is_visible.is_(True),
            models.WorkflowNodeOutput.is_terminal.is_(False),
            models.WorkflowNodeOutput.legal_hold.is_(False),
            models.WorkflowNodeOutput.created_at < cutoff,
        )
        .order_by(models.WorkflowNodeOutput.id.asc())
        .all()
    )

    candidates = [
        RetentionCandidate(
            id=r.id,
            tenant_id=r.tenant_id,
            content_hash=r.content_hash,
            size_bytes=int(r.size_bytes or 0),
            created_at=r.created_at,
        )
        for r in rows
    ]
    total_bytes = sum(c.size_bytes for c in candidates)
    return RetentionPlan(
        max_age_days=max_age_days,
        cutoff=cutoff,
        candidates=candidates,
        candidate_count=len(candidates),
        total_bytes=total_bytes,
    )


def _soft_delete_candidates(db, candidates: List[RetentionCandidate], *, now: datetime) -> int:
    """Stage 1: flip is_visible/hidden_at + write the 'hidden' tombstone.

    Reuses the exact pattern services/scheduler.py::_run_retention_cleanup
    already uses for expired outputs, and the WorkflowOutputAuditLog table
    services/assets_cogs_files_service.py uses for legal-hold events — no
    new tombstone mechanism invented.
    """
    if not candidates:
        return 0
    ids = [c.id for c in candidates]
    rows = (
        db.query(models.WorkflowNodeOutput)
        .filter(models.WorkflowNodeOutput.id.in_(ids))
        .all()
    )
    count = 0
    for row in rows:
        # Re-check live state — guards a race where the row was hidden,
        # legal-held, or promoted to terminal between plan() and here.
        if not row.is_visible or row.is_terminal or row.legal_hold:
            continue
        row.is_visible = False
        row.hidden_at = now
        db.add(models.WorkflowOutputAuditLog(
            output_id=row.id,
            content_hash=row.content_hash,
            tenant_id=row.tenant_id,
            action="hidden",
            performed_by_system="output_retention",
            reason="Intermediate output past MOD_RETENTION_INTERMEDIATE_DAYS",
        ))
        count += 1
    db.commit()
    return count


def _select_purge_candidates(db, *, grace_cutoff: datetime) -> List["_PurgeCandidate"]:
    """Stage-2 read phase only — no writes, no network I/O here.

    Selects rows soft-deleted longer than grace_days that have not already
    had their bytes purged (no existing 'bytes_purged' tombstone), and
    tags each with whether another VISIBLE row in the same tenant still
    shares its content_hash (content-addressed storage is deduped per
    (content_hash, tenant_id) — see models.ContentBlob).
    """
    already_purged_ids = {
        row[0]
        for row in db.query(models.WorkflowOutputAuditLog.output_id)
        .filter(models.WorkflowOutputAuditLog.action == "bytes_purged")
        .all()
        if row[0] is not None
    }

    rows = (
        db.query(models.WorkflowNodeOutput)
        .filter(
            models.WorkflowNodeOutput.is_visible.is_(False),
            models.WorkflowNodeOutput.hidden_at.isnot(None),
            models.WorkflowNodeOutput.hidden_at < grace_cutoff,
            models.WorkflowNodeOutput.legal_hold.is_(False),
            models.WorkflowNodeOutput.is_terminal.is_(False),
        )
        .order_by(models.WorkflowNodeOutput.id.asc())
        .all()
    )

    out: List[_PurgeCandidate] = []
    for r in rows:
        if r.id in already_purged_ids:
            continue
        out.append(_PurgeCandidate(
            id=r.id,
            tenant_id=r.tenant_id,
            content_hash=r.content_hash,
            size_bytes=int(r.size_bytes or 0),
            still_referenced=_blob_still_referenced(db, r),
        ))
    return out


def _blob_still_referenced(db, r) -> bool:
    """The blob is shared across outputs, uploaded files and cogs (one
    content_blobs row per (hash, tenant)), so its bytes may only go when no
    other live or held reference remains anywhere."""
    other_output = (
        db.query(models.WorkflowNodeOutput.id)
        .filter(
            models.WorkflowNodeOutput.tenant_id == r.tenant_id,
            models.WorkflowNodeOutput.content_hash == r.content_hash,
            models.WorkflowNodeOutput.id != r.id,
            or_(models.WorkflowNodeOutput.is_visible.is_(True),
                models.WorkflowNodeOutput.legal_hold.is_(True),
                models.WorkflowNodeOutput.is_terminal.is_(True)),
        )
        .first()
    )
    if other_output is not None:
        return True
    blob = (
        db.query(models.ContentBlob)
        .filter(models.ContentBlob.id == r.content_hash,
                models.ContentBlob.tenant_id == r.tenant_id)
        .first()
    )
    if blob is None:
        return False
    if blob.storage_path and db.query(models.File.id).filter(
            models.File.tenant_id == r.tenant_id,
            models.File.storage_path == blob.storage_path,
            models.File.deleted_at.is_(None)).first() is not None:
        return True
    if blob.cog_id is not None and db.query(models.Cog.id).filter(
            models.Cog.id == blob.cog_id,
            models.Cog.deleted_at.is_(None)).first() is not None:
        return True
    return False


def _purge_blob_bytes(*, tenant_id: int, content_hash: str) -> bool:
    """Remove one content-addressed object's bytes from tenant storage.

    Isolated into its own module-level function (rather than inlined) so
    tests can monkeypatch it with a stub instead of touching real
    MinIO/versitygw — see tests/test_output_retention.py. Reuses
    utils.minio_client.MinioClient, the same client
    services/scheduler.py::_retire_one_tenant uses for tenant bucket
    cleanup. Returns True on success, including "object already gone".
    """
    from utils.minio_client import MinioClient, get_tenant_bucket_name
    mc = MinioClient(tenant_id=tenant_id)
    # No `db` threaded in here on purpose: `_purge_stage` commits before this
    # loop specifically so storage network I/O never runs inside an open DB
    # transaction (see its docstring / the owner rule). `mc.bucket_name` is
    # already BYO-resolved (MinioClient.__init__ calls resolve_backend
    # itself); the `get_tenant_bucket_name(tenant_id)` fallback only fires if
    # that's somehow falsy, and it still resolves BYO on its own (opens/closes
    # its own short-lived session) — house default if that's unavailable.
    bucket = mc.bucket_name or get_tenant_bucket_name(tenant_id)
    try:
        mc.client.remove_object(bucket, content_hash)
    except Exception as e:
        code = getattr(e, "code", "")
        if code in ("NoSuchKey", "NoSuchBucket"):
            return True
        raise
    return True


def _purge_stage(db, candidates: List["_PurgeCandidate"], *, now: datetime):
    """Stage 2: purge bytes, then write tombstones.

    Reads are already done (candidates passed in). Commit here to close
    out that read transaction BEFORE any storage network call — per the
    owner rule, network I/O must never happen inside an open DB
    transaction. Tombstone writes happen only after all I/O completes.
    """
    db.commit()

    purged = 0
    purged_bytes = 0
    skipped = 0
    succeeded: List[_PurgeCandidate] = []

    for c in candidates:
        if c.still_referenced:
            skipped += 1
            continue
        try:
            ok = _purge_blob_bytes(tenant_id=c.tenant_id, content_hash=c.content_hash)
        except Exception as e:
            logger.warning(
                "output_retention: byte purge failed for output %s (hash=%s): %s",
                c.id, c.content_hash, e,
            )
            continue
        if ok:
            purged += 1
            purged_bytes += c.size_bytes
            succeeded.append(c)

    for c in succeeded:
        db.add(models.WorkflowOutputAuditLog(
            output_id=c.id,
            content_hash=c.content_hash,
            tenant_id=c.tenant_id,
            action="bytes_purged",
            performed_by_system="output_retention",
            reason="Soft-deleted past MOD_RETENTION_PURGE_GRACE_DAYS; object bytes removed",
        ))
    if succeeded:
        db.commit()

    return purged, purged_bytes, skipped


def apply_retention(db, *, now=None, grace_days=None, dry_run: bool = True) -> dict:
    """Apply (or dry-run) the two-stage retention sweep.

    ISO on (SaaS, or on-prem without MOD_ISO_MODE=false): returns
    {"mode": "iso_on", "purged": 0, ...} and touches NOTHING — not even a
    soft-delete. This is the owner rule's hard line.

    ISO off: stage 1 soft-deletes expired unpinned intermediates + writes
    a 'hidden' tombstone; stage 2 purges object bytes for rows soft-
    deleted longer than grace_days (skipping any content_hash still
    referenced by a live row), and writes a 'bytes_purged' tombstone.
    dry_run=True (the default) runs stage 1's planning query only and
    changes nothing, mirroring plan_expired_intermediates().
    """
    from services.scheduler import iso_mode_on  # local import — avoid a
    # module-load cycle (scheduler imports many services at call time).

    now = _now(now)

    if iso_mode_on():
        return {
            "mode": "iso_on",
            "dry_run": dry_run,
            "generated_at": now.isoformat(),
            "soft_deleted": 0,
            "purged": 0,
            "bytes_purged": 0,
            "skipped_still_referenced": 0,
        }

    if grace_days is None:
        grace_days = _env_int("MOD_RETENTION_PURGE_GRACE_DAYS", 7)

    plan = plan_expired_intermediates(db, now=now)

    # GY.C15 — legal hold / preserve mode: outputs of a held tenant must
    # not be soft-deleted or byte-purged (supports preservation
    # requirements, e.g. DFARS 252.204-7012 90-day). Resolved ONCE for the
    # sweep; held_tenants() is fail closed (a lookup error => every
    # tenant treated as held). The row-level legal_hold flag below is the
    # finer per-output signal; this is the tenant/platform-level one.
    from utils.paid_import import optional_paid
    _lhold = optional_paid("services.legal_hold")
    if _lhold is None:  # FX.162.p5 — CE carve: no legal holds exist
        lh_platform_wide, lh_tenant_ids = False, set()
    else:
        lh_platform_wide, lh_tenant_ids = _lhold.held_tenants(db)

    result = {
        "mode": "iso_off",
        "dry_run": dry_run,
        "generated_at": now.isoformat(),
        "stage1_candidates": plan.candidate_count,
        "stage1_bytes": plan.total_bytes,
        "soft_deleted": 0,
        "purged": 0,
        "bytes_purged": 0,
        "skipped_still_referenced": 0,
        "skipped_legal_hold": 0,
    }
    if dry_run:
        return result

    held_stage1 = [c for c in plan.candidates
                   if lh_platform_wide or c.tenant_id in lh_tenant_ids]
    if held_stage1:
        logger.info(
            "output_retention: skipping %d candidate(s) — legal hold "
            "active on their tenant(s); rows left in place",
            len(held_stage1),
        )
        result["skipped_legal_hold"] += len(held_stage1)
    unheld_stage1 = [c for c in plan.candidates
                     if not (lh_platform_wide or c.tenant_id in lh_tenant_ids)]

    result["soft_deleted"] = _soft_delete_candidates(
        db, unheld_stage1, now=now)

    grace_cutoff = now - timedelta(days=grace_days)
    purge_candidates = _select_purge_candidates(db, grace_cutoff=grace_cutoff)
    held_purge = [c for c in purge_candidates
                  if lh_platform_wide or c.tenant_id in lh_tenant_ids]
    if held_purge:
        logger.info(
            "output_retention: skipping %d byte-purge candidate(s) — "
            "legal hold active on their tenant(s); bytes preserved",
            len(held_purge),
        )
        result["skipped_legal_hold"] += len(held_purge)
    unheld_purge = [c for c in purge_candidates
                    if not (lh_platform_wide or c.tenant_id in lh_tenant_ids)]
    purged, purged_bytes, skipped = _purge_stage(db, unheld_purge, now=now)
    result["purged"] = purged
    result["bytes_purged"] = purged_bytes
    result["skipped_still_referenced"] = skipped
    return result


# ---------------------------------------------------------------------------
# GY.C5.s2 — audit-retention branch (retention PER PROFILE)
# ---------------------------------------------------------------------------
# The audit side of retention is a different duty from the two-stage output
# lifecycle above: ISO 27001 says audit rows are NEVER removed (append-only,
# "retained forever"), so this branch does not delete and does not even
# soft-delete audit rows. What it does is drive and enforce the ONLINE window
# — how long audit rows stay in the online tables before the nightly WORM
# export (services/audit_archive.py) — from the compliance profile / declared
# environment, and record each pass as an append-only audit event.
#
# The window itself is the existing platform setting audit.online_retention_months
# (registered in services/audit_archive.py; compliance profiles pin a floor on
# it, GY.C5.s1). services/compliance_environments.py owns the declared-environment
# floors and resolves the EFFECTIVE window (live setting raised to the declared
# floor); this module is the job that reads that effective window, so a profile
# value below the floor can never widen the GC window.
#
# No new settings for the window itself (no env vars, per owner rule): the only
# new knob is the job switch, registered here through the ordinary
# platform-settings registry (psr.register), same pattern as ids_fim.py /
# audit_archive.py.

from services import platform_settings as psr


def _cenv():
    """services.compliance_environments, or None on the CE carve (FX.162.p5).

    compliance_environments is a held-back paid module, so this module must
    import cleanly without it: on CE there is no declared-environment floor and
    the effective window is just the live setting."""
    from utils.paid_import import optional_paid
    return optional_paid("services.compliance_environments")


psr.register(
    "retention.audit_gc_enabled",
    bool,
    False,
    group="retention",
    description="GY.C5.s2 — enable the audit-retention GC pass: it resolves the "
                "profile-driven effective online window "
                "(audit.online_retention_months raised to the declared "
                "environment's floor), counts the audit rows past that window, "
                "and records the pass as an append-only audit event. It NEVER "
                "deletes or soft-deletes audit rows (ISO 27001 no-hard-delete); "
                "removal of audit rows is not a MOD operation. Default off.",
)


def _effective_audit_window(db) -> Dict[str, Any]:
    """Effective online audit-retention window, CE-safe (FX.162.p5).

    Paid: services/compliance_environments resolves the live setting raised to
    the declared environment's floor. On the CE carve that module is absent
    (and no environment can be declared), so the live setting alone is the
    window and the floor is 0."""
    cenv = _cenv()
    if cenv is not None:
        return cenv.effective_audit_retention_months(db=db)
    live = int(psr.get_setting("audit.online_retention_months", db=db))
    return {"declared_environment": [], "live_months": live,
            "floor_months": 0, "effective_months": live,
            "effective_from": "setting"}


def audit_gc_tick(db, *, now=None, dry_run: bool = False) -> dict:
    """One audit-retention pass: profile-driven window + append-only record.

    Reads the effective window from services/compliance_environments
    (live ``audit.online_retention_months`` raised to the declared-environment
    floor), counts rows older than that window in each audit table
    (services/audit_archive._TABLES — the same table list the WORM export uses),
    and writes one AuditEvent per table with rows past the window.

    dry_run=True counts without writing. Nothing here deletes a row, changes an
    audit row, or touches object bytes: the audit trail is preserved; the
    window only decides what the WORM export considers "old"."""
    from utils.paid_import import optional_paid

    now = _now(now)
    if not bool(psr.get_setting("retention.audit_gc_enabled", db=db)):
        return {"status": "disabled", "reason": "retention.audit_gc_enabled"}

    # 1647c1 CE: audit_archive is paid-only. The scheduler tick that enables
    # this pass is itself gated by retention.audit_gc_enabled (default off), so
    # on CE the pass is a no-op — skip with one log, never raise.
    audit_archive = optional_paid("services.audit_archive")
    if audit_archive is None:
        logger.info("audit_gc: skipped — services.audit_archive not in this edition")
        return {"status": "skipped", "reason": "module services.audit_archive not in this edition"}

    window = _effective_audit_window(db)
    months = int(window["effective_months"])
    cutoff = now - timedelta(days=months * 30)  # months as 30 days, as audit_archive does

    tables: Dict[str, int] = {}
    events = 0
    for table, model, ts_col in audit_archive._TABLES:
        rows = (
            db.query(model)
            .filter(getattr(model, ts_col) <= cutoff)
            .count()
        )
        tables[table] = int(rows)
        if dry_run or rows == 0:
            continue
        db.add(models.AuditEvent(
            tenant_id=None,
            user_id=None,
            event_name="audit.retention_gc_window",
            details={
                "table": table,
                "rows_past_window": int(rows),
                "effective_months": months,
                "live_months": int(window["live_months"]),
                "floor_months": int(window["floor_months"]),
                "effective_from": window["effective_from"],
                "window_cutoff": cutoff.isoformat(),
                "rows_deleted": 0,  # ISO 27001: audit rows are never deleted
            },
        ))
        events += 1
    if not dry_run and events:
        db.commit()
    return {
        "status": "ok",
        "dry_run": dry_run,
        "generated_at": now.isoformat(),
        "effective_months": months,
        "live_months": int(window["live_months"]),
        "floor_months": int(window["floor_months"]),
        "effective_from": window["effective_from"],
        "declared_environment": window["declared_environment"],
        "window_cutoff": cutoff.isoformat(),
        "tables": tables,
        "events_written": events,
        "rows_deleted": 0,
    }


def _check_audit_retention_window_matches_profile(db):
    """GY.C5.s2 — the audit GC window honours the declared environment.

    Metadata only (reads settings; no S3, no row scan). A declared environment
    with a retention floor and a live window below it = fail: the retention job
    would run on a window weaker than the profile requires. Nothing declared =
    not-applicable. CheckResult is fetched at call time through the CE guard so
    this module stays importable on the CE carve (FX.162.p5: services.conformance
    is held back)."""
    from utils.paid_import import optional_paid
    eng = optional_paid("services.conformance.engine")
    if eng is None:  # CE: the check is never registered, so this never runs
        return None
    CheckResult = eng.CheckResult
    w = _effective_audit_window(db)
    floor = int(w["floor_months"])
    live = int(w["live_months"])
    evidence = {
        "declared_environment": w["declared_environment"],
        "audit.online_retention_months": live,
        "declared_floor_months": floor,
        "effective_months": int(w["effective_months"]),
        "effective_from": w["effective_from"],
    }
    if floor <= 0:
        return CheckResult("not-applicable", dict(evidence, unknown=True))
    if live >= floor:
        return CheckResult("pass", evidence)
    return CheckResult(
        "fail",
        dict(evidence, reasons=[
            "a declared compliance environment requires at least "
            f"{floor} months of online audit retention but "
            "audit.online_retention_months is set to "
            f"{live} months — the audit-retention GC window is weaker "
            "than the declared environment requires"]))


# FX.162.p5 — the check only exists where the declared-environment map exists
# (services.conformance + services.compliance_environments are both held back on
# CE), so the import + registration are guarded; on CE there is nothing to check.
_CENV = _cenv()
# 1634d — services.conformance is held back on the CE carve, so the engine names
# come in through the CE-guard idiom (None on CE) rather than a bare module-level
# import that would crash boot before any request is served.
try:
    from services.conformance.engine import (
        Check, register_check, get_check,
    )
except (ImportError, ModuleNotFoundError):
    Check = register_check = get_check = None

if _CENV is not None and Check is not None:
    _CHECK_ID = "audit.retention_window_matches_profile"
    if get_check(_CHECK_ID) is None:  # import-time idempotence
        register_check(Check(
            id=_CHECK_ID,
            title="Audit online-retention window is at least the floor the "
                  "declared environment requires (GY.C5.s2; audit "
                  "rows are never deleted — the window only drives the WORM "
                  "export)",
            controls={"nist_800_53": ["AU-11"], "iso_27001": ["A.8.22"]},
            kind="auto",
            fn=_check_audit_retention_window_matches_profile,
        ))
