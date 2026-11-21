"""KP.10 — knowledge-pack reject/rollback-rate spike alert.

`routers/wizard_knowledge.py` already emits one `models.AuditEvent` row
per rejected/rolled-back knowledge-pack apply attempt (`event_name` ==
``"wizard_knowledge_pack_rejected"`` or
``"wizard_knowledge_pack_rolled_back"``). Nothing watches the rate of
those events; a spike means either a bad pack got published or a
customer install's version/clock state is wrong.

Same satellite-alert-module shape as `services/backup_alerts.py`:

    check_knowledge_pack_health(db, now=None) -> list of problem dicts
    raise_knowledge_pack_alerts(db, now=None) -> upsert ONE SecurityAlert
                                                  per over-threshold tenant
                                                  (stable subject = dedupe
                                                  key) + email through
                                                  services.security_alert_delivery.
    knowledge_pack_alert_tick()              -> scheduler-job body
                                                  (own SessionLocal, never
                                                  raises). NOT registered
                                                  with the scheduler by
                                                  this module — wiring it
                                                  into services/scheduler.py
                                                  is a deferred follow-up
                                                  (KP.10 avoids a file
                                                  clash with the in-flight
                                                  KP.7 scheduler.py edit).

Applies to SaaS and on-prem alike (unlike backups, there is no
on-prem-only gate here) — a bad pack affects both deployment shapes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from services import platform_settings as _psr
# FIX-KP-CE-1 (11-05): the FX.162.p5 alert engine + delivery layers stay Pro-only
# in the CE carve (services/security_alert_delivery.py imports the denied
# services/tenant_security_contact_service.py unguarded at :146, so it cannot
# ship as-is). CE posture: the health check still runs (audit-event counts +
# platform settings are CE), but the SecurityAlert upsert + email side effects
# are skipped and logged once — never raised, so the 15-min scheduler tick
# stays quiet. Fail-closed semantics for signature/hash/tenant checks live in
# knowledge_pack_consumer/sanitizer and are untouched here.
# FIX-KP-CE-2 (11-05): `logger` is bound BEFORE the guards (the except branches
# log — the FIX-KP-CE-1 shape raised NameError at import in CE). Each import
# swallows ONLY its own absent module, via the repo's edition predicate
# (utils.paid_import._is_absent, same idiom as main.py:1576 and :2109-2127): an
# ImportError from INSIDE the engine (a real bug / missing third-party dep) has
# a different exc.name and re-raises, so CE never hides it.
logger = logging.getLogger(__name__)

try:
    from services import security_alert_engine as _alert_engine
except ImportError as _engine_exc:
    from utils.paid_import import _is_absent
    if not _is_absent(_engine_exc, "services.security_alert_engine"):
        raise
    _alert_engine = None
    logger.info(
        "knowledge_pack_alerts: services.security_alert_engine not in this "
        "edition — reject-spike findings are computed but not raised as "
        "SecurityAlerts")

try:
    from services import security_alert_delivery as _alert_delivery
except ImportError as _delivery_exc:
    from utils.paid_import import _is_absent
    if not _is_absent(_delivery_exc, "services.security_alert_delivery"):
        raise
    _alert_delivery = None
    logger.info(
        "knowledge_pack_alerts: services.security_alert_delivery not in this "
        "edition — new SecurityAlerts are not emailed")

#: SecurityAlert.rule_key for every finding this module raises.
RULE_KNOWLEDGE_PACK_REJECT_SPIKE = "knowledge_pack_reject_spike"

SEVERITY = "medium"

_REJECT_EVENTS = (
    "wizard_knowledge_pack_rejected",
    "wizard_knowledge_pack_rolled_back",
)

THRESHOLD_SETTING_KEY = "wizard_knowledge_pack_alert_threshold"
WINDOW_MINUTES_SETTING_KEY = "wizard_knowledge_pack_alert_window_minutes"

_psr.register(
    THRESHOLD_SETTING_KEY,
    int,
    3,
    group="wizard",
    description="Reject/rollback events for one tenant within the "
                "alert window that trigger a knowledge_pack_reject_spike "
                "SecurityAlert (default 3).",
)
_psr.register(
    WINDOW_MINUTES_SETTING_KEY,
    int,
    60,
    group="wizard",
    description="Lookback window (minutes) the knowledge-pack "
                "reject/rollback-rate spike check counts events over "
                "(default 60).",
)


def _aware(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def check_knowledge_pack_health(db: Session, now: Any = None) -> List[Dict]:
    """Return one problem dict per tenant whose reject/rollback count
    within the alert window is >= the threshold. Empty list = healthy."""
    now = _aware(now) or datetime.now(timezone.utc)
    threshold = int(_psr.get_setting(THRESHOLD_SETTING_KEY, db=db)
                    or 3)
    window_minutes = int(_psr.get_setting(WINDOW_MINUTES_SETTING_KEY, db=db)
                         or 60)
    since = now - timedelta(minutes=window_minutes)

    rows = (
        db.query(models.AuditEvent.tenant_id,
                 func.count(models.AuditEvent.id))
        .filter(models.AuditEvent.event_name.in_(_REJECT_EVENTS))
        .filter(models.AuditEvent.timestamp >= since)
        .filter(models.AuditEvent.timestamp <= now)
        .group_by(models.AuditEvent.tenant_id)
        .all()
    )

    problems: List[Dict] = []
    for tenant_id, count in rows:
        if count < threshold:
            continue
        problems.append({
            "key": RULE_KNOWLEDGE_PACK_REJECT_SPIKE,
            "subject": f"knowledge_pack_reject_spike:tenant:{tenant_id}",
            "details": {
                "count": int(count),
                "window_minutes": window_minutes,
                "tenant_id": tenant_id,
            },
        })
    return problems


def raise_knowledge_pack_alerts(db: Session, now: Any = None) -> int:
    """Upsert ONE SecurityAlert per over-threshold tenant and email the
    platform admins for the NEW rows only (same engine Finding ->
    _upsert_alert -> commit -> send_new_alert_emails pattern as
    backup_alerts.raise_backup_alerts). NEVER raises: a check/upsert/email
    hiccup must not break the scheduler tick. Returns the number of NEW
    alerts raised."""
    now = _aware(now) or datetime.now(timezone.utc)
    try:
        problems = check_knowledge_pack_health(db, now)
    except Exception as e:  # noqa: BLE001 — the check itself must not raise
        logger.exception("knowledge-pack health check raised: %s", e)
        return 0

    new_alerts: List[Any] = []
    if _alert_engine is None:
        # CE carve (FIX-KP-CE-1): no engine -> findings are computed but never
        # raised; one INFO line per call, zero exceptions, so the 15-min tick
        # stays quiet.
        if problems:
            logger.info(
                "knowledge-pack reject-spike findings (%d) not raised: "
                "alert engine not in this edition", len(problems))
        return 0
    for problem in problems:
        try:
            finding = _alert_engine.Finding(
                rule_key=RULE_KNOWLEDGE_PACK_REJECT_SPIKE,
                severity=SEVERITY,
                tenant_id=problem["details"]["tenant_id"],
                subject=problem["subject"],
                details=dict(problem["details"]),
                count=1,
            )
            alert, created = _alert_engine._upsert_alert(db, finding, now, now)
            if created:
                new_alerts.append(alert)
            db.commit()
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the rest
            logger.exception("knowledge-pack alert upsert raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    if new_alerts:
        try:
            _alert_delivery.send_new_alert_emails(db, new_alerts)
        except Exception as e:  # noqa: BLE001 — delivery is a side effect
            logger.exception("knowledge-pack alert delivery raised: %s", e)

    return len(new_alerts)


def knowledge_pack_alert_tick() -> None:
    """Scheduler-job body (NOT yet registered — see module docstring:
    the deferred `_scheduler.add_job(knowledge_pack_alert_tick, ...)`
    registration belongs in services/scheduler.py once KP.7's in-flight
    scheduler.py edit has landed). Opens its own short session and never
    raises out, same shape as backup_alerts.backup_health_tick."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        raise_knowledge_pack_alerts(db)
    except Exception as e:  # noqa: BLE001 — a tick must never crash the loop
        logger.exception("knowledge-pack alert tick raised: %s", e)
    finally:
        db.close()
