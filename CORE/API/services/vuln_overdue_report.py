"""GSEC FX.52 — weekly remediation-SLA overdue report to platform admins.

The GSEC register (docs/security-audit/REGISTER.md, path from the
``vuln.register_path`` platform setting) is parsed by
``tools.register_due_dates`` (the single due-date authority shared with the
``vuln.remediation_overdue`` conformance check). This module composes the
overdue list — open items past their SLA due date without an unexpired
``ACCEPTED YYYY-MM-DD`` risk-acceptance sign-off — and emails it to every
active platform admin through the existing email service (the
``_get_email_env`` + ``_send_email_smtp`` inline-SMTP path used by
services/vault_seal_monitor.py and services/permission_digest.py).

Gated by the ``vuln.overdue_email_enabled`` platform setting (default True;
re-read every tick, so it can be turned off without a restart). The
scheduler runs ``run_overdue_report`` weekly (Mondays 06:00 UTC). Best-effort
like every scheduler tick: sends are logged, never raised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from pathlib import Path

from services import platform_settings as psr
from tools import register_due_dates as due

logger = logging.getLogger(__name__)

REGISTER_PATH_DEFAULT = "docs/security-audit/REGISTER.md"
SETTING_ENABLED = "vuln.overdue_email_enabled"
SETTING_REGISTER_PATH = "vuln.register_path"
#: Cap on rows listed in one email (the counts are always exact).
MAX_ROWS_PER_EMAIL = 50
#: SLA-window settings (days): the policy defaults live in
#: tools.register_due_dates.SLA_DAYS/KEV_CRITICAL_DAYS; these settings let an
#: owner retune a window without a code change (0 = no due date for it).
SLA_SETTING_KEYS = {
    "critical": "vuln.sla_days_critical",
    "high": "vuln.sla_days_high",
    "medium": "vuln.sla_days_medium",
    "low": "vuln.sla_days_low",
}
SLA_KEV_KEY = "vuln.sla_days_kev_critical"


def sla_days(db=None) -> Dict[str, int]:
    """The ``vuln.sla_days_*`` settings as {severity: days}."""
    return {sev: int(psr.get_setting(key, db=db))
            for sev, key in SLA_SETTING_KEYS.items()}


def kev_critical_days(db=None) -> int:
    """The ``vuln.sla_days_kev_critical`` setting (CISA KEV window)."""
    return int(psr.get_setting(SLA_KEV_KEY, db=db))


def register_path(db=None) -> str:
    """Absolute path of the register: the ``vuln.register_path`` setting
    (repo-relative or absolute) resolved against the tool's repo root."""
    rel = str(psr.get_setting(SETTING_REGISTER_PATH, db=db)
              or REGISTER_PATH_DEFAULT)
    p = Path(rel)
    if p.is_absolute():
        return str(p)
    return str(due.REPO_ROOT / p)


def collect_overdue(db=None) -> Dict[str, Any]:
    """One pass over the register: {path, rows, open, overdue_count,
    overdue: [{id, severity, found, due, days_overdue}]} sorted by days
    overdue. A missing/unreadable register raises (the tick logs it)."""
    path = register_path(db)
    text = open(path, "r", encoding="utf-8").read()
    rows = due.parse_register(text,
                              sla_days=sla_days(db),
                              kev_critical_days=kev_critical_days(db))
    overdue = sorted((r for r in rows if r["overdue"]),
                     key=lambda r: -r["days_overdue"])
    fmt = "%Y-%m-%d"
    return {
        "path": path,
        "rows": len(rows),
        "open": sum(1 for r in rows if r["open"]),
        "overdue_count": len(overdue),
        "overdue": [
            {"id": r["id"],
             "severity": r["severity"],
             "found": r["found"].strftime(fmt) if r["found"] else None,
             "due": r["due"].strftime(fmt) if r["due"] else None,
             "days_overdue": r["days_overdue"]}
            for r in overdue[:MAX_ROWS_PER_EMAIL]
        ],
    }


def _compose_bodies(summary: Dict[str, Any]):
    """(text, html) bodies for the weekly report."""
    subject = (f"MOD GSEC: {summary['overdue_count']} vulnerability(s) "
               "past remediation SLA")
    lines = [
        f"Register: {summary['path']}",
        f"Open items: {summary['open']} · past due without an unexpired "
        f"risk-acceptance sign-off: {summary['overdue_count']}",
        "",
    ]
    for r in summary["overdue"]:
        lines.append(
            f"  {r['id']} [{r['severity']}] found {r['found']} "
            f"due {r['due']} — {r['days_overdue']}d overdue")
    lines.append("")
    lines.append(
        "Remediate, or record a written owner sign-off in the register "
        "Notes column as `ACCEPTED YYYY-MM-DD` (expiry date). "
        "See docs/security-audit/REMEDIATION_SLA_POLICY.md.")
    text = "\n".join(lines)
    html = (f"<h2>{subject}</h2><pre>"
            + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</pre>")
    return subject, text, html


def send_overdue_report(db, summary: Dict[str, Any]) -> int:
    """Email the overdue list to every active platform admin (existing
    email service, inline-SMTP path). Returns admins emailed; never raises
    per admin."""
    import models

    try:
        from utils.email_sender import _get_email_env, _send_email_smtp

        subject, text, html = _compose_bodies(summary)
        admins = db.query(models.User).filter(
            models.User.is_platform_admin.is_(True),
            models.User.status == models.UserStatus.ACTIVE,
        ).all()
        if not admins:
            logger.warning("vuln_overdue_report: no active platform admins")
            return 0
        sent = 0
        for admin in admins:
            try:
                env = _get_email_env(
                    db, admin.tenant_id, admin.email,
                    f"{admin.first_name or ''} {admin.last_name or ''}".strip()
                    or admin.email,
                )
                if not env:
                    logger.warning(
                        "vuln_overdue_report: no email env for admin (tenant %s)",
                        admin.tenant_id)
                    continue
                env["EMAIL_TYPE"] = "notification"
                env["TRANSCRIPT_TEXT"] = text
                env["TRANSCRIPT_HTML"] = html
                env["__SUBJECT_OVERRIDE__"] = subject
                if _send_email_smtp(env):
                    sent += 1
            except Exception as e:  # noqa: BLE001 — per-admin best effort
                logger.warning("vuln_overdue_report: send failed for admin %s: %s",
                               admin.tenant_id, e)
        return sent
    except Exception as e:  # noqa: BLE001
        logger.warning("vuln_overdue_report: email stage failed: %s", e)
        return 0


def run_overdue_report(db) -> Dict[str, Any]:
    """One weekly tick: gate → collect → email. Returns a small summary
    for the scheduler log. The gate is re-read every tick (no restart
    needed to disable)."""
    if not bool(psr.get_setting(SETTING_ENABLED, db=db)):
        logger.info("vuln_overdue_report: skipped (overdue_email_enabled=false)")
        return {"sent": 0, "skipped": True}
    try:
        summary = collect_overdue(db)
    except OSError as e:
        logger.warning("vuln_overdue_report: register unreadable: %s", e)
        return {"sent": 0, "skipped": True, "unreadable": True}
    if summary["overdue_count"] == 0:
        logger.info("vuln_overdue_report: nothing past due — no email sent")
        return {"sent": 0, "overdue_count": 0}
    sent = send_overdue_report(db, summary)
    logger.info("vuln_overdue_report: %d admin(s) emailed, %d overdue item(s)",
                sent, summary["overdue_count"])
    return {"sent": sent, "overdue_count": summary["overdue_count"]}
