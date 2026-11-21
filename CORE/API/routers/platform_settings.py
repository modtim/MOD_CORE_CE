"""GS.PSR.3 — HTTP surface for the DB-backed platform-settings registry.

Admin surface for the tunables/feature flags registered in
`services/platform_settings.py` (GS.PSR.1+2). Everything here is
platform-admin only:

- GET    /platform/settings          list every registered key (platform
                                      scope) with value, source, type,
                                      default, group, description.
- GET    /platform/settings/{key}    one key's resolved value + source.
- PATCH  /platform/settings/{key}    set a value (optionally tenant-scoped).
                                      Validation errors -> 422 with the
                                      validator message, unknown key -> 404.
- DELETE /platform/settings/{key}    revert to env/default by soft-deleting
                                      the live DB row (never a hard delete).

Writes commit on success and record an event through the platform's
standard admin-audit helper (ADMIN_FEATURE_FLAG_CHANGE) in addition to
the registry's own append-only `platform_settings_audit` row.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, HTTPException, Request, status)
from pydantic import BaseModel
from sqlalchemy.orm import Session

import audit
import models
from dependencies import (get_db, require_active_and_onboarded_user,
                          require_auditor_or_admin, require_fresh_auth)
try:
    from services import hf_cache_relayout as hcr
except ImportError:  # CE carve — module absent
    hcr = None
    class _HFRelayoutGateError(Exception):  # CE stand-in
        blocking_workers: list[int] = []

_HFCacheRelayoutGateError = (hcr.HFCacheRelayoutGateError
                             if hcr is not None else _HFRelayoutGateError)

from services import platform_settings as psr
from services._base import ServiceError

# FX.162.p5 — compliance is paid-only (owner Q157): the CE carve strips
# services/compliance_profiles.py, so this module-scope import is guarded.
# `cprof is None` on CE; the two `except _ProfileLockConflict` clauses below
# keep working via the never-raised stand-in, and the profile-lock 409
# builder is unreachable on CE (nothing raises it).
try:
    from services import compliance_profiles as cprof
except ImportError:  # CE carve — module absent, not a bug inside it
    cprof = None

    class _ProfileLockConflict(Exception):
        """CE stand-in: never raised (the real service is absent)."""
else:
    _ProfileLockConflict = cprof.ProfileLockConflict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform/settings", tags=["Platform Settings"])

# GSEC FX.65 p3b — this is the only generic by-key, tenant-scoped settings
# write path in the API (grep set_setting( across routers/: the only other
# hits are fixed-key service calls, never a caller-supplied key string), so
# it is the one place a `kms.*` key could be flipped outside its dedicated,
# validated surface: `kms.byok_provider`/`kms.byok_key_ref` need a live
# health-check gate (routers/tenant/byok.py PUT), and `kms.revoked_dark` is
# system-managed — written ONLY by services/kms_tenant_state.py
# (record_unwrap/clear_dark), never a direct write target (see that
# setting's own registration comment in services/platform_settings.py).
# Platform-only kms.* keys (e.g. kms.health_interval_minutes) are unaffected
# — this denylist only applies when a tenant_id is given.
_TENANT_SCOPED_KMS_DENYLIST_PREFIX = "kms."


def _require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Platform admin required")
    return current_user


class SettingRead(BaseModel):
    key: str
    type: str
    value: Any
    source: str
    default: Any
    group: str
    description: str
    restart_required: bool
    env_fallback: Optional[str] = None
    tenant_overrides: List[int] = []


class SettingsListResponse(BaseModel):
    settings: List[SettingRead]


class SettingUpdateRequest(BaseModel):
    value: Any
    tenant_id: Optional[int] = None


class SettingUpdateResponse(BaseModel):
    key: str
    value: Any
    source: str
    tenant_id: Optional[int] = None
    # GY.C0 — re-declare: PATCHing compliance.declared_environment
    # returns the SAME preview payload the compliance preview->apply
    # flow produces (no auto-apply; the admin confirms explicitly).
    preview: Optional[Dict[str, Any]] = None


# GY.C0 — declared compliance environment (read-only surface; writing
# goes through the ordinary PATCH /platform/settings/{key} path above,
# which is validated + audited).


class ComplianceNeedRead(BaseModel):
    id: str
    title: str
    description: str
    feature: str
    available_on: str
    available_on_this_edition: bool


class ComplianceEnvironmentRead(BaseModel):
    title: str
    declared: bool


class ComplianceDeclaredEnvironmentResponse(BaseModel):
    declared_environment: List[str]
    source: str
    edition: str
    map_version: int
    environments: Dict[str, ComplianceEnvironmentRead]
    needs: List[ComplianceNeedRead]
    ce_note: Optional[str] = None
    # GY.C0.s1a — ask-once banner: True only while the declared environment
    # was never written AND the banner was never dismissed. Silences itself
    # once the admin declares (even an explicit empty list = "none of these")
    # or dismisses via POST .../prompt/dismiss.
    prompt_pending: bool = False
    audit_retention: Optional[dict] = None
    # GY.C4.s2b — password floor the declaration implies (metadata only).
    password_policy: Optional[dict] = None


def _spec_for(key: str):
    """The registry spec or 404 — mirrors get_setting's own error case."""
    try:
        return psr._spec(key)
    except psr.SettingNotRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown platform setting: {key}",
        ) from None


def _emit_audit(db: Session, admin: models.User, action: str,
                key: str, old: Any, new: Any,
                tenant_id: Optional[int], request: Request) -> None:
    """Standard admin-audit event. Never raises — the registry's own
    audit row already covers the append-only trail, and an audit failure
    must not mask the mutation result."""
    try:
        audit.log_admin_action(
            db,
            models.AuditAction.ADMIN_FEATURE_FLAG_CHANGE,
            admin.id,
            request=request,
            target_tenant_id=tenant_id,
            resource_type="platform_setting",
            metadata={"action": action, "key": key,
                      "old_value": old, "new_value": new},
        )
    except Exception:  # noqa: BLE001
        logger.warning("platform settings admin audit emit failed "
                       "(non-fatal)", exc_info=True)


@router.get("", response_model=SettingsListResponse)
def list_platform_settings(
    db: Session = Depends(get_db),
    # GY.C16: read-only config — platform admin OR time-boxed auditor.
    _admin: models.User = Depends(require_auditor_or_admin),
) -> SettingsListResponse:
    """Every registered platform setting with its resolved value and source."""
    return SettingsListResponse(
        settings=[SettingRead(**row) for row in psr.list_settings(db)])


@router.get("/{key}", response_model=SettingRead)
def get_platform_setting(key: str,
                         db: Session = Depends(get_db),
                         # GY.C16: read-only — platform admin OR auditor.
                         _admin: models.User = Depends(require_auditor_or_admin),
                         tenant_id: Optional[int] = None) -> SettingRead:
    """One setting's resolved value + source (optionally tenant-scoped)."""
    spec = _spec_for(key)
    # owner 75 (11-13): hidden-only registry entries are not part of CE's
    # admin surface — same 404 as an unknown key. Paid is unaffected.
    if psr.is_ce_hidden(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown platform setting: {key}",
        )
    value, source = psr.resolve_source(key, db, tenant_id=tenant_id)
    return SettingRead(
        key=spec.key,
        type=spec.type_.__name__,
        value=value,
        source=source,
        default=spec.default,
        group=spec.group,
        description=spec.description,
        restart_required=spec.restart_required,
        env_fallback=spec.env_fallback,
    )


def _profile_lock_409(e, db: Session) -> HTTPException:
    """GY.C1 — a write weakened a setting an active compliance profile
    requires: persist the audit row on the same session, commit, then
    409 with {setting, profile, required}."""
    e.emit_audit()
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "setting": e.key,
                                "profile": e.profile,
                                "required":
                                    cprof.requirement_to_text(e.requirement),
                                "reason": "locked_by_compliance_profile",
                            }) from None
    return HTTPException(status_code=status.HTTP_409_CONFLICT,
                         detail={
                             "setting": e.key,
                             "profile": e.profile,
                             "required":
                                 cprof.requirement_to_text(e.requirement),
                             "reason": "locked_by_compliance_profile",
                         })


@router.patch("/{key}", response_model=SettingUpdateResponse)
def update_platform_setting(key: str,
                            payload: SettingUpdateRequest,
                            request: Request,
                            db: Session = Depends(get_db),
                            admin: models.User = Depends(_require_platform_admin),
                            fresh: models.User = Depends(require_fresh_auth)) -> SettingUpdateResponse:  # GY.C4b
    """Set a platform (or tenant-scoped) setting value.

    Type/validator failures -> 422 with the validator message; an unknown
    key -> 404. GY.C1: weakening a setting an active compliance profile
    requires -> 409 with {setting, profile, required} + audit. Commits on
    success."""
    _spec_for(key)
    if payload.tenant_id is not None and key.startswith(_TENANT_SCOPED_KMS_DENYLIST_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{key} cannot be set for a tenant via the generic settings "
                   f"API; use the dedicated BYOK endpoints (/tenant/byok) or "
                   f"the platform clear-dark endpoint.",
        )
    # GSEC FX.80 p4a (owner Q63=A, hosted cloud only) — enterprise
    # dedicated-GPU promise: tenant-scoped `gpu.dedicated=true` requires
    # Cap.DEDICATED_GPU (403 otherwise). Checked BEFORE set_setting so a
    # refused write never soft-supersedes or re-inserts the live row; on
    # refusal the gate has already flushed a platform_settings_audit row,
    # which is committed here (same commit-then-409 shape as
    # _profile_lock_409) so a refused entitlement write leaves a trail.
    if (key == psr.DEDICATED_GPU_SETTING_KEY
            and payload.tenant_id is not None
            and payload.value is True):
        try:
            psr.check_dedicated_gpu_gate(
                True, db=db, tenant_id=payload.tenant_id)
        except HTTPException:
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            raise
    try:
        new_value = psr.set_setting(
            key, payload.value, db=db,
            actor_user_id=admin.id, tenant_id=payload.tenant_id)
    except psr.SettingNotRegisteredError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Unknown platform setting: {key}") from None
    except _HFCacheRelayoutGateError as e:
        # GSEC R-159: isolation ON is refused until every online worker has
        # reported its one-time HF cache re-layout complete. R6: a fixed
        # message + structured fields — never raw str(e).
        db.add(models.AuditEvent(
            tenant_id=None, user_id=admin.id,
            event_name="platform_setting_set_refused",
            details={"action": "set", "key": key, "value": payload.value,
                     "reason": "hf_cache_relayout_incomplete",
                     "blocking_workers": e.blocking_workers}))
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "setting": key,
            "reason": "hf_cache_relayout_incomplete",
            "blocking_workers": e.blocking_workers,
            "message": "Cannot enable models.hf_cache_tenant_isolation: "
                       "the one-time HF cache re-layout is not complete on "
                       "every online worker. Trigger it via "
                       "POST /platform/hf-cache/relayout and wait for each "
                       "worker to report complete (GET "
                       "/platform/hf-cache/relayout/status).",
        }) from None
    except _ProfileLockConflict as e:
        raise _profile_lock_409(e, db)
    except ServiceError as exc:
        # GY.C3b phishing-resistant-only safety rail (and any future
        # service-level refusal): 409 with the service's clear message
        # (curated service copy — never raw provider error text).
        status_code = (status.HTTP_409_CONFLICT if exc.code == "conflict"
                       else status.HTTP_400_BAD_REQUEST)
        raise HTTPException(status_code=status_code,
                            detail=exc.message) from None
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(e)) from None

    value, source = psr.resolve_source(key, db, tenant_id=payload.tenant_id)
    _emit_audit(db, admin, "set", key, None, new_value,
                payload.tenant_id, request)
    db.commit()

    # GY.C0 — re-declare: changing the declared environment returns the
    # SAME preview payload the compliance preview->apply flow would
    # produce for the profiles those environments map to. NEVER
    # auto-applies; the admin saves the declaration, reviews the
    # preview, and applies it explicitly. Best-effort: a preview
    # failure must not fail the settings write.
    preview = None
    if (key == "compliance.declared_environment"
            and payload.tenant_id is None
            and isinstance(value, (list, tuple))):
        try:
            from services import compliance_environments as cenv
            declared = [e for e in value if e in cenv.ALLOWED_ENVIRONMENTS]
            preview = cprof.preview_declared_environment_change(
                declared, db=db)
        except Exception:  # noqa: BLE001
            logger.warning("re-declare preview "
                           "failed (non-fatal)", exc_info=True)

    return SettingUpdateResponse(key=key, value=value, source=source,
                                 tenant_id=payload.tenant_id,
                                 preview=preview)


@router.delete("/{key}", response_model=SettingUpdateResponse)
def delete_platform_setting(key: str,
                            request: Request,
                            db: Session = Depends(get_db),
                            admin: models.User = Depends(_require_platform_admin),
                            tenant_id: Optional[int] = None) -> SettingUpdateResponse:
    """Revert a setting to env/default by soft-deleting the live DB row.

    404 on an unknown key; 404 when there is no live row to revert (the
    key already resolves from env/default). GY.C1: reverting a locked
    setting to a weaker env/default -> 409 + audit (same shape as the
    PATCH path)."""
    _spec_for(key)
    try:
        removed = psr.clear_setting(
            key, db=db, actor_user_id=admin.id, tenant_id=tenant_id)
    except _ProfileLockConflict as e:
        raise _profile_lock_409(e, db)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No stored value for setting {key!r} to revert "
                   "(already on env/default)")
    value, source = psr.resolve_source(key, db, tenant_id=tenant_id)
    _emit_audit(db, admin, "revert", key, None, value, tenant_id, request)
    db.commit()
    return SettingUpdateResponse(key=key, value=value, source=source,
                                 tenant_id=tenant_id)


# --- FX.57 p2 — run the system-log archive tick on demand ---------------

class LogArchiveNowResponse(BaseModel):
    """Summary of one on-demand system-log archive tick (same shape the
    nightly 03:55 UTC job logs)."""
    status: str
    rows: int = 0
    manifest: Optional[str] = None
    errors: int = 0
    reason: Optional[str] = None


@router.post("/logs/archive-now", response_model=LogArchiveNowResponse)
def archive_logs_now(request: Request,
                     db: Session = Depends(get_db),
                     admin: models.User = Depends(_require_platform_admin)
                     ) -> LogArchiveNowResponse:
    """Run the FX.57 system-log archive tick immediately (it otherwise
    runs nightly at 03:55 UTC).

    Exports wizard_run_logs rows older than `logs.searchable_days` (and
    not yet archived) to the platform system-logs bucket, verifies the
    upload, and only then soft-marks the rows `archived_at`. Rows are
    NEVER hard-deleted (ISO 27001). Returns `{"status": "disabled"}`
    when `logs.archive_job_enabled` is off. Platform admin only, audited
    (ADMIN_SYSTEM_MAINTENANCE)."""
    # 1788i CE carve: system_log_archive is paid; archiving is OFF in CE, so the
    # on-demand tick answers the same shape the endpoint already returns when
    # logs.archive_job_enabled is off — never a 500.
    try:
        from services import system_log_archive as sla
    except ImportError:
        result = {"status": "disabled",
                  "reason": "system_log_archive is not available in CE"}
    else:
        result = sla.run_log_archive_tick(db=db)
    try:
        audit.log_admin_action(
            db,
            models.AuditAction.ADMIN_SYSTEM_MAINTENANCE,
            admin.id,
            request=request,
            resource_type="system-logs",
            metadata={"action": "archive_now", "result": result},
        )
    except Exception:  # noqa: BLE001 — audit must never mask the result
        logger.warning("system-log archive-now audit emit failed "
                       "(non-fatal)", exc_info=True)
    db.commit()
    return LogArchiveNowResponse(
        status=str(result.get("status", "error")),
        rows=int(result.get("rows") or 0),
        manifest=result.get("manifest"),
        errors=int(result.get("errors") or 0),
        reason=result.get("reason"),
    )
