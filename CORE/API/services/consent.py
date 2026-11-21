"""GY.V5 — verifiable consent records + the ``minor`` data label.

Enables customer COPPA compliance (we enable; we never certify):

* **Consent store** — an APPEND-ONLY ``ConsentRecord`` model
  (``consent_type`` in ('parental_coppa', 'biometric', ...) so the later
  BIPA task GY.V8 reuses it). Revocation is a NEW ``status='revoked'``
  row, never an UPDATE/DELETE (ISO 27001 + the DB append-only trigger in
  migration 20261004c_consent_records.py). Guardian contact is PII and is
  stored encrypted at rest, reusing the tenant-key Fernet helpers from
  ``services/http_triggers.py`` (the existing at-rest PII pattern).
* **``minor`` label** — ``users.is_minor`` (a stored flag, mirroring how
  GY.C7 labels are represented on ``cogs.data_classification``). Effects:
  (a) external-node/LLM processing of a minor's data is blocked by the
  same C7 gate (``utils/label_policy.external_llm_blocked_for``);
  (b) FX.15 privacy erasure is accelerated — the sweep skips the
  ``privacy.erasure_grace_days`` grace window for minor-labelled users
  (hooked in ``services/scheduler._run_privacy_erasure``);
  (c) the DSAR export (``me_identity_service.export_personal_data``)
  includes the minor flag + the subject's consent records.
* **Revocation propagation** — a revocation appends the revoked record
  and QUEUES the subject's erasure through the existing FX.15 path
  (``request_account_deletion`` → ``deletion_requested_at`` → the daily
  sweep, now with zero grace for minors).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

# Extensible consent taxonomy. GY.V8 (BIPA) will exercise the second.
CONSENT_TYPES = ("parental_coppa", "biometric")
CONSENT_METHODS = ("email_plus", "signed_form", "card_verification", "other")
STATUS_GRANTED = "granted"
STATUS_REVOKED = "revoked"


# --- PII-at-rest helpers + audit ---------------------------------------------

def _encrypt_contact(db: Session, tenant_id: int,
                     contact: Optional[str]) -> Optional[str]:
    """Encrypt the guardian contact at rest (tenant-key Fernet), reusing the
    existing at-rest PII pattern from ``services/http_triggers.py``. A
    blank/absent contact is stored as NULL (never encrypts empty)."""
    if not contact or not contact.strip():
        return None
    try:
        from services import http_triggers as _trg
        tenant = db.get(models.Tenant, tenant_id)
        if tenant is None:
            # No tenant row (defensive — subject should belong to it): store
            # NULL rather than plaintext. A read returns None.
            logger.warning("consent: no tenant %s to key guardian contact",
                           tenant_id)
            return None
        return _trg.encrypt_secret(db, tenant, contact.strip())
    except Exception:  # noqa: BLE001 — never persist plaintext on failure
        logger.warning("consent: failed to encrypt guardian contact "
                       "(tenant=%s); storing NULL", tenant_id, exc_info=True)
        return None


def _decrypt_contact(db: Session, tenant_id: int,
                     row: models.ConsentRecord) -> Optional[str]:
    """Decrypt a stored guardian contact for an admin read. Corrupt/rotated
    key material returns None (never raises out of the history listing)."""
    if not row.guardian_contact:
        return None
    try:
        from services import http_triggers as _trg
        tenant = db.get(models.Tenant, tenant_id)
        if tenant is None:
            return None
        return _trg.decrypt_secret(db, tenant, row.guardian_contact)
    except Exception:  # noqa: BLE001
        logger.warning("consent: failed to decrypt guardian contact "
                       "(consent_id=%s)", row.id, exc_info=True)
        return None


def _audit(db: Session, tenant_id: Optional[int], event_name: str,
           details: Dict[str, Any]) -> None:
    """Append a metadata-only audit event. Never raises — a consent write
    must not be lost because of an audit failure."""
    try:
        db.add(models.AuditEvent(tenant_id=tenant_id,
                                 event_name=event_name,
                                 details=details))
        db.flush()
    except Exception:  # noqa: BLE001
        logger.warning("consent: failed to record %s audit event",
                       event_name, exc_info=True)


# --- Consent records (append-only) -------------------------------------------

def record_consent(
    db: Session, *, tenant_id: int, subject_user_id: int,
    consent_type: str = "parental_coppa", guardian_contact: Optional[str] = None,
    method: str = "other", recorded_by: Optional[int] = None,
    evidence_ref: Optional[str] = None,
    purpose: Optional[str] = None,
    retention_until: Optional[datetime] = None,
    destruction_schedule: Optional[str] = None,
) -> models.ConsentRecord:
    """Append a ``granted`` consent record.

    ``guardian_contact`` is encrypted at rest (tenant-key Fernet) before
    it touches the DB — the same pattern ``Secret``/trigger secrets use.
    Returns the new row (the caller owns the transaction).

    GY.V8 (BIPA): for ``consent_type='biometric'`` the written release's
    stated PURPOSE goes in ``evidence_ref`` (reference to the signed
    release document) or ``purpose`` (stored as
    ``<purpose> | <evidence_ref>`` when both are given) and the stated
    RETENTION deadline in ``retention_until`` (NULL = no explicit
    retention; the destruction sweep then applies the 3-years-from-last-
    interaction statutory default).
    """
    if consent_type not in CONSENT_TYPES:
        raise ValueError(f"unknown consent_type {consent_type!r}")
    if method not in CONSENT_METHODS:
        raise ValueError(f"unknown consent method {method!r}")
    if retention_until is not None and consent_type != "biometric":
        raise ValueError("retention_until is only settable for a "
                         "biometric (BIPA) consent")
    stored_contact = _encrypt_contact(db, tenant_id, guardian_contact)
    stored_ref = evidence_ref
    if consent_type == "biometric":
        # The BIPA written release states purpose, retention period and
        # destruction schedule — all persisted in the free-form
        # evidence_ref column (the DB only needs the resolved deadline,
        # which lives on biometric_retention_until).
        parts = []
        if purpose:
            parts.append(f"purpose: {purpose}")
        if destruction_schedule:
            parts.append(f"destruction: {destruction_schedule}")
        if evidence_ref:
            parts.append(f"release: {evidence_ref}")
        if parts:
            stored_ref = " | ".join(parts)
    row = models.ConsentRecord(
        tenant_id=tenant_id,
        subject_user_id=subject_user_id,
        consent_type=consent_type,
        guardian_contact=stored_contact,
        method=method,
        status=STATUS_GRANTED,
        recorded_by=recorded_by,
        evidence_ref=stored_ref,
        biometric_retention_until=(retention_until
                                   if consent_type == "biometric" else None),
    )
    db.add(row)
    db.flush()
    _audit(db, tenant_id, "CONSENT_RECORDED",
           {"subject_user_id": subject_user_id, "consent_type": consent_type,
            "method": method, "consent_id": row.id,
            "biometric_retention_until": (retention_until.isoformat()
                                          if retention_until else None)})
    return row


def revoke_consent(
    db: Session, *, tenant_id: int, subject_user_id: int,
    consent_type: str = "parental_coppa", recorded_by: Optional[int] = None,
    evidence_ref: Optional[str] = None,
    queue_erasure: bool = True,
) -> models.ConsentRecord:
    """Append a ``revoked`` consent record (NEVER deletes the grant).

    Points ``revoked_record_id`` at the most recent still-effective
    grant for this subject/type. When ``queue_erasure`` is set (default)
    the subject's erasure is queued through the existing FX.15 account-
    deletion path; a minor-labelled subject then erases with NO grace
    window (see ``services/scheduler._run_privacy_erasure``).
    """
    target = (db.query(models.ConsentRecord)
              .filter(models.ConsentRecord.tenant_id == tenant_id,
                      models.ConsentRecord.subject_user_id == subject_user_id,
                      models.ConsentRecord.consent_type == consent_type,
                      models.ConsentRecord.status == STATUS_GRANTED)
              .order_by(models.ConsentRecord.id.desc())
              .first())
    row = models.ConsentRecord(
        tenant_id=tenant_id,
        subject_user_id=subject_user_id,
        consent_type=consent_type,
        method="other",
        status=STATUS_REVOKED,
        recorded_by=recorded_by,
        evidence_ref=evidence_ref,
        revoked_record_id=target.id if target is not None else None,
    )
    db.add(row)
    db.flush()
    _audit(db, tenant_id, "CONSENT_REVOKED",
           {"subject_user_id": subject_user_id, "consent_type": consent_type,
            "consent_id": row.id,
            "revoked_record_id": row.revoked_record_id,
            "erasure_queued": bool(queue_erasure)})
    if queue_erasure:
        queue_erasure_for_subject(db, subject_user_id,
                                  actor_user_id=recorded_by,
                                  reason="consent_revocation")
    return row


def consent_history(
    db: Session, *, tenant_id: int, subject_user_id: int,
    consent_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """All records for a subject, oldest first. Guardian contact is
    decrypted on read (admin-only surface)."""
    q = (db.query(models.ConsentRecord)
         .filter(models.ConsentRecord.tenant_id == tenant_id,
                 models.ConsentRecord.subject_user_id == subject_user_id)
         .order_by(models.ConsentRecord.id.asc()))
    if consent_type is not None:
        q = q.filter(models.ConsentRecord.consent_type == consent_type)
    out: List[Dict[str, Any]] = []
    for r in q.all():
        out.append({
            "id": r.id,
            "tenant_id": r.tenant_id,
            "subject_user_id": r.subject_user_id,
            "consent_type": r.consent_type,
            "guardian_contact": _decrypt_contact(db, tenant_id, r),
            "method": r.method,
            "status": r.status,
            "recorded_by": r.recorded_by,
            "evidence_ref": r.evidence_ref,
            "revoked_record_id": r.revoked_record_id,
            "biometric_retention_until": (
                r.biometric_retention_until.isoformat()
                if getattr(r, "biometric_retention_until", None) else None),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def subject_consent_revoked(db: Session, *, tenant_id: int,
                            subject_user_id: int) -> bool:
    """True when a revoked record with NO later re-grant exists for the
    subject (any consent type) — the C7 gate uses this to block ALL
    processing that requires consent."""
    latest = (db.query(models.ConsentRecord.status)
              .filter(models.ConsentRecord.tenant_id == tenant_id,
                      models.ConsentRecord.subject_user_id == subject_user_id)
              .order_by(models.ConsentRecord.id.desc())
              .first())
    return bool(latest) and latest[0] == STATUS_REVOKED


def set_minor_label(
    db: Session, *, tenant_id: int, subject_user_id: int,
    is_minor: bool, actor_user_id: Optional[int] = None,
) -> models.User:
    """Set/clear the ``minor`` data label (``users.is_minor``)."""
    u = (db.query(models.User)
         .filter(models.User.id == subject_user_id,
                 models.User.tenant_id == tenant_id)
         .first())
    if u is None:
        raise LookupError("subject user not found")
    u.is_minor = bool(is_minor)
    db.flush()
    _audit(db, tenant_id, "MINOR_LABEL_CHANGED",
           {"subject_user_id": subject_user_id, "is_minor": bool(is_minor),
            "actor_user_id": actor_user_id})
    return u


# --- FX.15 erasure hook -------------------------------------------------------

def erasure_grace_days_for(db: Session, user: models.User) -> int:
    """GY.V5 — a minor-labelled subject's erasure is ACCELERATED: the
    ``privacy.erasure_grace_days`` grace window is skipped entirely
    (returns 0). Non-minors keep the configured grace (default 30)."""
    if getattr(user, "is_minor", False):
        return 0
    try:
        from services import platform_settings as psr
        return int(psr.get_setting("privacy.erasure_grace_days", db=db) or 30)
    except Exception:  # noqa: BLE001 — fall back to the documented default
        return 30


def queue_erasure_for_subject(
    db: Session, subject_user_id: int, *,
    actor_user_id: Optional[int] = None, reason: str = "consent_revocation",
) -> Optional[models.User]:
    """Queue the subject's erasure through the existing FX.15 path.

    Stamps ``deletion_requested_at`` (the erasure sweep's grace anchor)
    and deactivates the account — the same markers
    ``me_identity_service.request_account_deletion`` sets. Already
    pending/erased subjects are left untouched (idempotent)."""
    u = db.query(models.User).filter(models.User.id == subject_user_id).first()
    if u is None or u.erased_at is not None:
        return u
    now = datetime.now(timezone.utc)
    if u.deletion_requested_at is None:
        u.deletion_requested_at = now
    if u.status != models.UserStatus.DEACTIVATED:
        u.status = models.UserStatus.DEACTIVATED
    _audit(db, u.tenant_id, "ERASURE_QUEUED_CONSENT_REVOCATION",
           {"subject_user_id": u.id, "actor_user_id": actor_user_id,
            "reason": reason, "minor": bool(getattr(u, "is_minor", False))})
    return u


# --- GY.V8 — biometric (BIPA) handling ---------------------------------------
#
# BIPA (740 ILCS 14): biometric identifiers may only be collected with a
# WRITTEN RELEASE stating the purpose, the retention period and the
# destruction schedule, and may not be retained longer than the stated
# period, the achievement of the specific purpose, or THREE YEARS from the
# subject's last interaction — whichever is EARLIEST. We ENABLE the
# operator's BIPA posture; we never certify it.
#
#   * the label is ``cogs.data_classification = 'biometric'`` (GY.C7
#     taxonomy, added in services/data_classification.py);
#   * external-node processing of biometric-labelled data reuses the C7
#     gate (utils/label_policy) and additionally requires an ACTIVE
#     biometric consent per subject (missing/revoked consent = blocked);
#   * scheduled destruction: biometric-labelled outputs past their consent's
#     retention deadline (or the 3-year default) are QUEUED through the
#     existing soft-delete erasure path (rows are NEVER hard-deleted —
#     ISO 27001; the row + 'hidden' tombstone stay, and the byte-purge
#     stage follows the same lifecycle as every other soft-deleted output);
#   * legal holds (services/legal_hold.is_held) are ALWAYS skipped.
#
# Revocation STOPS processing through the existing C7 revocation path
# (``subject_consent_revoked``) — no separate BIPA hook is needed there.

# Statutory default when the release states no explicit retention period:
# 3 years from the subject's last interaction (740 ILCS 14(b)).
BIPA_DEFAULT_RETENTION_YEARS = 3
BIPA_DEFAULT_RETENTION_DAYS = 3 * 365


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Compare-safe timestamp (naive-UTC), None passes through."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _subject_last_interaction(db: Session, subject_user_id: int
                              ) -> Optional[datetime]:
    """Best-effort 'last interaction' anchor: the latest of the subject's
    session activity and account creation. NULL when nothing is known
    (the caller then cannot schedule destruction — it keeps the data,
    fail-safe for the subject)."""
    last = None
    try:
        s = (db.query(models.UserSession.last_active_at)
             .filter(models.UserSession.user_id == subject_user_id)
             .order_by(models.UserSession.last_active_at.desc())
             .first())
        if s is not None and s[0] is not None:
            last = s[0]
    except Exception:  # noqa: BLE001
        last = None
    try:
        c = (db.query(models.User.created_at)
             .filter(models.User.id == subject_user_id).first())
        if c is not None and c[0] is not None and (
                last is None or c[0] > last):
            last = c[0]
    except Exception:  # noqa: BLE001
        pass
    return last


def biometric_destruction_due(db: Session, *, tenant_id: int,
                              subject_user_id: int,
                              now: Optional[datetime] = None
                              ) -> Optional[datetime]:
    """The destruction deadline for a subject's biometric data, or None.

    min(stated retention deadline, last interaction + 3 years) — whichever
    is FIRST, per 740 ILCS 14(b). Returns None when no anchor is known
    (no active biometric grant and no last-interaction signal): the data
    is then NOT scheduled for destruction (keeping it is the fail-safe
    direction for the subject)."""
    now = now or datetime.now(timezone.utc)
    latest = (db.query(models.ConsentRecord)
              .filter(models.ConsentRecord.tenant_id == tenant_id,
                      models.ConsentRecord.subject_user_id == subject_user_id,
                      models.ConsentRecord.consent_type == "biometric",
                      models.ConsentRecord.status == STATUS_GRANTED)
              .order_by(models.ConsentRecord.id.desc())
              .first())
    deadlines = []
    if latest is not None:
        stated = getattr(latest, "biometric_retention_until", None)
        if stated is not None:
            deadlines.append(_naive(stated))
    last = _subject_last_interaction(db, subject_user_id)
    if last is not None:
        from datetime import timedelta
        deadlines.append(_naive(last) + timedelta(
            days=BIPA_DEFAULT_RETENTION_DAYS))
    if not deadlines:
        return None
    due = min(deadlines)
    if due > _naive(now):
        return None
    return due


def has_active_biometric_consent(db: Session, *, tenant_id: int,
                                 subject_user_id: int) -> bool:
    """True when the subject's LATEST biometric consent record is a grant
    (any type's revocation separately blocks via ``subject_consent_revoked``
    in the C7 gate). Fail CLOSED: a lookup error means NOT consented."""
    try:
        latest = (db.query(models.ConsentRecord)
                  .filter(models.ConsentRecord.tenant_id == tenant_id,
                          models.ConsentRecord.subject_user_id
                          == subject_user_id,
                          models.ConsentRecord.consent_type == "biometric")
                  .order_by(models.ConsentRecord.id.desc())
                  .first())
        return bool(latest) and latest.status == STATUS_GRANTED
    except Exception:  # noqa: BLE001 — fail closed
        logger.warning("consent: biometric consent lookup failed "
                       "(subject=%s)", subject_user_id, exc_info=True)
        return False


def plan_biometric_destruction(db: Session, *, now: Optional[datetime] = None
                               ) -> List[Dict[str, Any]]:
    """Read-only plan: biometric-labelled outputs past their destruction
    deadline, with held rows excluded.

    An output is biometric when its run's input cogs (or any cog whose
    files it produced) carry the ``biometric`` classification. A row is
    due when its SUBJECT's consent retention (min of stated deadline and
    3-years-from-last-interaction) has passed. Rows under any active legal
    hold (tenant- or run-scoped, ``services/legal_hold.is_held``) are
    EXCLUDED — preservation always wins over destruction.
    """
    now = _naive(now or datetime.now(timezone.utc))
    from utils.paid_import import optional_paid
    dc = optional_paid("services.data_classification")
    _lhold = optional_paid("services.legal_hold")
    # 1788 CE carve: the taxonomy is paid, but the column itself ships, so the
    # sweep keeps its own level constant (CE cannot label a cog — the label
    # validator lives in the paid taxonomy — so this normally finds nothing).
    _biometric_level = dc.BIOMETRIC_LEVEL if dc is not None else "biometric"
    cog_ids = [r[0] for r in
               db.query(models.Cog.id)
               .filter(models.Cog.data_classification == _biometric_level)
               .all()]
    if not cog_ids:
        return []
    # run id -> subject user id, for runs whose inputs are biometric.
    # (platform-wide sweep: no tenant filter — each row is resolved back to
    # its own tenant for the hold check.)
    run_subjects: Dict[int, int] = {}
    for row in (db.query(models.WorkflowRun.id,
                         models.WorkflowRun.parameters,
                         models.WorkflowRun.triggered_by_user_id)
                .all()):
        w_run_id, params, uid = row
        if isinstance(params, dict):
            ids = ((params.get("project_info") or {}).get("cog_ids") or [])
            if any(c in cog_ids for c in ids) and uid is not None:
                run_subjects[w_run_id] = uid
    if not run_subjects:
        return []
    plan: List[Dict[str, Any]] = []
    rows = (db.query(models.WorkflowNodeOutput)
            .filter(models.WorkflowNodeOutput.workflow_run_id.in_(
                list(run_subjects)))
            .order_by(models.WorkflowNodeOutput.id.asc())
            .all())
    for out in rows:
        if out.legal_hold:
            continue
        try:
            if _lhold is not None and _lhold.is_held(
                    db, tenant_id=out.tenant_id,
                    run_id=out.workflow_run_id):  # FX.162.p5 — absent -> not held
                continue
        except Exception:  # noqa: BLE001 — fail closed: keep it
            continue
        subject = run_subjects.get(out.workflow_run_id)
        if subject is None:
            continue
        due = biometric_destruction_due(db, tenant_id=out.tenant_id,
                                        subject_user_id=subject, now=now)
        if due is None:
            continue
        plan.append({
            "output_id": out.id,
            "tenant_id": out.tenant_id,
            "workflow_run_id": out.workflow_run_id,
            "subject_user_id": subject,
            "due_at": due.isoformat(),
        })
    return plan


def queue_biometric_destruction(db: Session, *,
                                dry_run: bool = True) -> Dict[str, Any]:
    """Queue biometric-labelled outputs past their destruction deadline
    for erasure via the EXISTING soft-delete path.

    Soft-delete semantics only (ISO 27001 — no hard delete): the row is
    hidden (``is_visible=False`` + ``hidden_at``) and a ``hidden``
    tombstone with a BIPA reason is appended to the append-only
    ``workflow_output_audit_logs`` table. The row's bytes then follow the
    same byte-purge lifecycle as any other soft-deleted output
    (``services/output_retention``); the audit tombstone is the
    crypto-shred/erasure QUEUE record. Held rows and already-queued rows
    are skipped; ``dry_run=True`` (the default) plans only.
    """
    now = _naive(datetime.now(timezone.utc))
    plan = plan_biometric_destruction(db, now=now)
    queued = 0
    if not dry_run and plan:
        ids = [p["output_id"] for p in plan]
        for row in (db.query(models.WorkflowNodeOutput)
                    .filter(models.WorkflowNodeOutput.id.in_(ids)).all()):
            if not row.is_visible or row.is_terminal or row.legal_hold:
                continue  # re-check live state (race guard)
            row.is_visible = False
            row.hidden_at = now
            db.add(models.WorkflowOutputAuditLog(
                output_id=row.id,
                content_hash=row.content_hash,
                tenant_id=row.tenant_id,
                action="hidden",
                performed_by_system="biometric_retention",
                reason=("BIPA (740 ILCS 14): biometric-labelled output past "
                        "the consent's retention deadline (or 3 years from "
                        "the subject's last interaction) — queued for "
                        "crypto-shred/erasure via the soft-delete path"),
            ))
            queued += 1
        db.commit()
        _audit(db, plan[0]["tenant_id"], "BIOMETRIC_DESTRUCTION_QUEUED",
               {"queued": queued, "output_ids": ids})
    return {"planned": len(plan), "queued": queued, "dry_run": bool(dry_run)}


# --- BUG.B53a — tenant minor/consent roster + bulk minor labelling ----------

MINOR_ROSTER_LIMIT_MAX = 200
MINOR_BULK_MAX = 200
CONSENT_TYPE_PARENTAL = "parental_coppa"


def tenant_minor_roster(
    db: Session, *, tenant_id: int, skip: int = 0, limit: int = 100,
) -> List[Dict[str, Any]]:
    """BUG.B53a — the tenant's users with their minor flag + consent status.

    One row per tenant user (soft-deleted users excluded), ordered by user
    id, paged. ``consent_status`` is the LATEST ``parental_coppa`` record's
    status for that subject: ``'granted'`` / ``'revoked'`` / ``'none'``
    when the subject has no parental-consent record at all.

    Metadata-only: username/email/names are the subject's own identity
    fields (the admin surface that records consent already shows them);
    the guardian contact is NEVER included here.
    """
    limit = max(1, min(int(limit), MINOR_ROSTER_LIMIT_MAX))
    skip = max(0, int(skip))

    latest: Dict[int, models.ConsentRecord] = {}
    for r in (db.query(models.ConsentRecord)
              .filter(models.ConsentRecord.tenant_id == tenant_id,
                      models.ConsentRecord.consent_type
                      == CONSENT_TYPE_PARENTAL)
              .order_by(models.ConsentRecord.id.asc()).all()):
        latest[r.subject_user_id] = r  # append-only: highest id wins

    rows = (db.query(models.User)
            .filter(models.User.tenant_id == tenant_id,
                    models.User.deleted_at.is_(None))
            .order_by(models.User.id.asc())
            .offset(skip).limit(limit).all())

    out: List[Dict[str, Any]] = []
    for u in rows:
        rec = latest.get(u.id)
        first = (u.first_name or "").strip()
        last = (u.last_name or "").strip()
        display = " ".join(p for p in (first, last) if p) or u.username
        out.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "display_name": display,
            "is_minor": bool(getattr(u, "is_minor", False)),
            "consent_status": (rec.status if rec else "none"),
            "consent_record_id": (rec.id if rec else None),
            "consent_method": (rec.method if rec else None),
            "consent_at": (rec.created_at.isoformat()
                           if rec and rec.created_at else None),
        })
    return out


def count_tenant_minor_roster(db: Session, *, tenant_id: int) -> int:
    """Total tenant users eligible for the roster (paging metadata)."""
    return int((db.query(models.User)
                .filter(models.User.tenant_id == tenant_id,
                        models.User.deleted_at.is_(None))
                .count()))


def bulk_set_minor_label(
    db: Session, *, tenant_id: int, subject_user_ids: List[int],
    is_minor: bool, actor_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """BUG.B53a — set/clear ``users.is_minor`` for many subjects at once.

    Tenant-scoped: a subject only changes when it belongs to ``tenant_id``.
    Unknown/foreign ids are reported in ``not_found`` and never touched.
    Soft changes only — the flag is a stored label, nothing is deleted, and
    consent HISTORY is untouched (revocation stays an append-only record).

    One audit event per call, identifiers only (no PII in details).
    """
    ids = [int(i) for i in dict.fromkeys(subject_user_ids) if i is not None]
    if len(ids) > MINOR_BULK_MAX:
        raise ValueError("too many subject_user_ids")

    found = (db.query(models.User)
             .filter(models.User.tenant_id == tenant_id,
                     models.User.id.in_(ids)).all()) if ids else []
    found_ids = {u.id for u in found}
    not_found = [i for i in ids if i not in found_ids]

    changed: List[int] = []
    for u in found:
        if bool(getattr(u, "is_minor", False)) != bool(is_minor):
            u.is_minor = bool(is_minor)
            changed.append(u.id)
    if changed:
        db.flush()
        _audit(db, tenant_id, "MINOR_LABEL_BULK_CHANGED",
               {"tenant_id": tenant_id, "is_minor": bool(is_minor),
                "subject_user_ids": sorted(changed),
                "count": len(changed),
                "actor_user_id": actor_user_id})
    return {"tenant_id": tenant_id, "is_minor": bool(is_minor),
            "requested": len(ids), "changed": sorted(changed),
            "unchanged": sorted(found_ids - set(changed)),
            "not_found": not_found}
