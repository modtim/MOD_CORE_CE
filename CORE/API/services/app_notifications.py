"""GX.S8 (launch-completion-megaplan Group X) — app notification fan-out.

`POST /v1/notifications` (routers/notifications.py) fans one app-authored
notification out to a target set, all resolved within the caller's tenant:

  * in-app `Notification` rows (CE) — honours NotificationPreference
    opt-outs (category "product", via the "product_" type prefix)
  * email via the GX.S4 service (CE) — chunked at S4's recipient cap,
    never duplicating SMTP code
  * Push (usage-metering seam) reuses the `usage_metering` capability, the
    same delivery axis GX.S4 uses for delivery/bounce tracking;
    dispatch itself is a TODO stub.

Cross-tenant target ids are silently dropped and counted. `app_user_types`
is accepted but inert until GX.S1b lands.

Limits are DB platform settings (services.platform_settings, group
"notifications") — never env.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

import models
from services import email_send_api as esa

logger = logging.getLogger(__name__)

# notification_type carries the "product_" prefix so the existing category
# mapping (crud/notification_preferences.py) files these rows under the
# "product" preference category — opt-outs work with zero crud changes.
APP_NOTIFICATION_TYPE = "product_app"
# Metered push seam — reuses `usage_metering` (GX.S4's metered delivery axis; no
# push-specific Cap exists in the entitlements catalogue).
PUSH_CAPABILITY = "usage_metering"

MAX_RECIPIENTS_KEY = "notifications.max_recipients_per_call"
RATE_PER_MINUTE_KEY = "notifications.rate_limit_per_minute"

DEFAULT_MAX_RECIPIENTS = 500
DEFAULT_RATE_PER_MINUTE = 60

_VALID_CHANNELS = ("in_app", "email", "push")


def _setting(key: str, default, db, tenant_id: Optional[int]):
    from services import platform_settings
    try:
        return platform_settings.get_setting(key, db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — degrade to the default, never raise
        return default


def limit_bundle(db, tenant_id: Optional[int]) -> Dict[str, int]:
    return {
        "max_recipients": _setting(MAX_RECIPIENTS_KEY, DEFAULT_MAX_RECIPIENTS,
                                   db, tenant_id),
        "rate_per_minute": _setting(RATE_PER_MINUTE_KEY, DEFAULT_RATE_PER_MINUTE,
                                    db, tenant_id),
    }


def has_push(db, tenant, tenant_id: Optional[int] = None) -> bool:
    """Metered seam — True when the tenant is entitled to push delivery.
    Reuses `usage_metering` (see PUSH_CAPABILITY). Fail-closed. The
    tenant row is loaded here (service layer) when not passed in."""
    from utils import entitlements
    try:
        if tenant is None and tenant_id is not None:
            tenant = db.query(models.Tenant).filter(
                models.Tenant.id == tenant_id).first()
        es = entitlements.get_entitlements(db, tenant)
        return entitlements.evaluate_feature(PUSH_CAPABILITY, es).allowed
    except Exception as e:  # noqa: BLE001
        logger.warning("app_notifications: entitlement check failed: %s", e)
        return False


def _rate_limited(tenant_id: int, limits: Dict[str, int]) -> bool:
    from utils import rate_limiter
    # FIX-CE-UNLIMITED-B1: <= 0 is the CE "unlimited" sentinel.
    if int(limits["rate_per_minute"]) <= 0:
        return False
    ok, _r, _s = rate_limiter.check_rate_limit(
        f"app_notifications:{tenant_id}",
        max_requests=limits["rate_per_minute"],
        window_seconds=60,
        raise_on_limit=False,
    )
    return not ok


def validate_link(link: Optional[str]) -> Optional[str]:
    """http(s) URL or a relative path only; reject script/data schemes."""
    if link is None:
        return None
    link = link.strip()
    if not link or len(link) > 500:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="link must be a non-empty string of at most 500 chars.")
    low = link.lower()
    if "://" in low and not (low.startswith("http://") or low.startswith("https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="link must be an http(s) URL or a relative path.")
    if low.startswith(("javascript:", "data:", "vbscript:", "file:")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="link must be an http(s) URL or a relative path.")
    return link


def _dedupe_exists(db, tenant_id: int, dedupe_key: str) -> bool:
    """True when any LIVE in-app row for a user of THIS tenant already
    carries the key (dedupe is tenant-scoped, not global)."""
    row = db.execute(
        text(
            "SELECT 1 FROM notifications n "
            "JOIN users u ON u.id = n.recipient_user_id "
            "WHERE n.dedupe_key = :k AND u.tenant_id = :t "
            "  AND n.deleted_at IS NULL LIMIT 1"
        ),
        {"k": dedupe_key, "t": tenant_id},
    ).first()
    return row is not None


def _scim_group_tables_present(db) -> bool:
    """True when BOTH SCIM group tables exist in the current database.

    The group lookup below runs raw SQL on `scim_group_memberships`/
    `scim_groups`; a CE database never creates them (open-core scrubs the
    creation), so client-supplied `group_ids` would otherwise hit a
    request-path ProgrammingError. This is a table-existence capability
    check, not a licence gate: paid always has the tables, so its code
    path and SQL stay byte-identical.
    """
    return bool(
        db.execute(
            text(
                "SELECT to_regclass('public.scim_groups') IS NOT NULL "
                "AND to_regclass('public.scim_group_memberships') IS NOT NULL"
            )
        ).scalar()
    )


def _resolve_targets(db, tenant_id: int,
                     user_ids: List[int],
                     group_ids: List[int]) -> Dict[str, Any]:
    """Resolve user_ids + group_ids to live in-tenant users.

    Cross-tenant or missing ids are silently dropped and counted
    separately. Group ids resolve through `scim_group_memberships`
    (groups are SCIM/IdP groups; there is no other groups table)."""
    user_ids = list(dict.fromkeys(user_ids or []))
    group_ids = list(dict.fromkeys(group_ids or []))

    users: Dict[int, models.User] = {}
    dropped_users = 0
    if user_ids:
        found = (
            db.query(models.User)
            .filter(
                models.User.id.in_(user_ids),
                models.User.tenant_id == tenant_id,
                models.User.deleted_at.is_(None),
            )
            .all()
        )
        users.update({u.id: u for u in found})
        dropped_users = len(user_ids) - len(found)

    group_dropped = 0
    if group_ids and _scim_group_tables_present(db):
        rows = db.execute(
            text(
                "SELECT gm.group_id, u.id FROM scim_group_memberships gm "
                "JOIN scim_groups g ON g.id = gm.group_id "
                "JOIN users u ON u.id = gm.user_id "
                "WHERE g.tenant_id = :t AND gm.group_id = ANY(:gs) "
                "  AND g.deleted_at IS NULL AND u.tenant_id = :t "
                "  AND u.deleted_at IS NULL"
            ),
            {"t": tenant_id, "gs": list(group_ids)},
        ).all()
        member_ids = list({r[1] for r in rows})
        if member_ids:
            found = (
                db.query(models.User)
                .filter(
                    models.User.id.in_(member_ids),
                    models.User.tenant_id == tenant_id,
                    models.User.deleted_at.is_(None),
                )
                .all()
            )
            users.update({u.id: u for u in found})
        # a group id that resolved to zero in-tenant members counts as dropped
        matched = {r[0] for r in rows}
        group_dropped = len([g for g in group_ids if g not in matched])
    elif group_ids:
        # The SCIM group tables are absent (a CE database never creates them):
        # skip the lookup and count every requested group id as dropped so the
        # caller's response reports them as dropped. No recipients from groups.
        logger.info(
            "app_notifications: SCIM group tables absent; skipping group "
            "lookup for %d requested group id(s)", len(group_ids))
        group_dropped = len(group_ids)

    return {
        "users": users,
        "dropped_users": dropped_users,
        "dropped_groups": group_dropped,
    }


def _emit_email(db, tenant_id: int, user_id: Optional[int],
                users: Dict[int, models.User], *, title: str, body: str,
                severity: str, link: Optional[str],
                limit: int) -> Dict[str, Any]:
    """Email fan-out via the S4 service (mocked in tests). Chunked at S4's
    recipient cap. SMTP-unconfigured (409) degrades to a skipped note;
    other errors mark the channel failed."""
    channels = {
        "sent": 0, "failed": 0,
        "skipped": None,  # None = no skip; else a reason string
    }
    addrs = sorted({u.email for u in users.values() if u.email})
    if not addrs:
        channels["skipped"] = "no_emails"
        return channels
    try:
        for i in range(0, len(addrs), max(1, limit)):
            chunk = addrs[i:i + max(1, limit)]
            esa.prepare_send(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                app_id=None,  # GX.A2 adds the apps axis
                to=chunk,
                cc=[],
                bcc=[],
                subject=title,
                text=f"{body}\n\nSeverity: {severity}"
                     + (f"\n\nLink: {link}" if link else ""),
                html=None,
                attachments=[],
            )
        channels["sent"] = len(addrs)
    except HTTPException as e:
        if e.status_code == status.HTTP_409_CONFLICT:
            channels["skipped"] = "smtp_not_configured"
        else:
            channels["failed"] = len(addrs)
            logger.warning("app_notifications: email fan-out failed: %s",
                           e.detail)
    except Exception as e:  # noqa: BLE001
        channels["failed"] = len(addrs)
        logger.warning("app_notifications: email fan-out error: %s", e)
    return channels


def fan_out(db: Session, *, tenant_id: int, user_id: Optional[int],
            targets: Dict[str, Any], title: str, body: str,
            severity: str, link: Optional[str],
            channels: List[str], dedupe_key: Optional[str]) -> Dict[str, Any]:
    """Fan one notification out. See module docstring. Returns the
    per-channel counts response body."""
    link = validate_link(link)
    ch = list(dict.fromkeys(channels or ["in_app"]))
    unknown = [c for c in ch if c not in _VALID_CHANNELS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown channel(s): {', '.join(unknown)}")

    from crud import notification_preferences as nprefs

    limits = limit_bundle(db, tenant_id)
    if _rate_limited(tenant_id, limits):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="App notification rate limit exceeded. Try again shortly.",
            headers={"Retry-After": "60"},
        )

    app_user_types = list((targets or {}).get("app_user_types") or [])
    resolved = _resolve_targets(
        db, tenant_id,
        list((targets or {}).get("user_ids") or []),
        list((targets or {}).get("group_ids") or []),
    )
    users = resolved["users"]
    if not users:
        # still report drop counts + not_yet_supported; nothing delivered
        response = {
            "deduped": False,
            "delivered": {"in_app": 0, "email": {"sent": 0, "failed": 0,
                                                 "skipped": "no_recipients"},
                          "push": {"skipped": "no_recipients"}},
            "dropped": {
                "user_ids": resolved["dropped_users"],
                "group_ids": resolved["dropped_groups"],
            },
            "app_user_types": {"accepted": len(app_user_types),
                               "status": "not_yet_supported"},
        }
        if app_user_types:
            response["app_user_types"]["note"] = (
                "app user-type targeting lands with GX.S1b (TODO)")
        return response

    if len(users) > limits["max_recipients"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many recipients ({len(users)}); the platform limit "
                   f"is {limits['max_recipients']} per call.")

    if dedupe_key and _dedupe_exists(db, tenant_id, dedupe_key):
        return {
            "deduped": True,
            "delivered": {"in_app": 0,
                          "email": {"sent": 0, "failed": 0,
                                    "skipped": "deduped"},
                          "push": {"skipped": "deduped"}},
            "dropped": {
                "user_ids": resolved["dropped_users"],
                "group_ids": resolved["dropped_groups"],
            },
            "app_user_types": {"accepted": len(app_user_types),
                               "status": "not_yet_supported"},
        }

    delivered: Dict[str, Any] = {}
    if "in_app" in ch:
        created = 0
        for uid, u in users.items():
            if not nprefs.is_enabled(db, user_id=uid,
                                     notification_type=APP_NOTIFICATION_TYPE,
                                     channel="in_app"):
                continue
            db.add(models.Notification(
                recipient_user_id=uid,
                message=title if not body else f"{title}: {body}",
                is_read=False,
                notification_type=APP_NOTIFICATION_TYPE,
                link_url=link,
                severity=severity,
                dedupe_key=dedupe_key,
            ))
            created += 1
        db.flush()
        db.commit()
        delivered["in_app"] = created

    if "email" in ch:
        # chunk at the SMALLER of our per-call cap and S4's per-message
        # recipient cap, so S4's own 422 never trips mid-fan-out
        s4_cap = esa._setting(esa.MAX_RECIPIENTS_KEY,
                              esa.DEFAULT_MAX_RECIPIENTS, db, tenant_id)
        delivered["email"] = _emit_email(
            db, tenant_id, user_id, users,
            title=title, body=body, severity=severity, link=link,
            limit=min(limits["max_recipients"], s4_cap),
        )
    else:
        delivered["email"] = {"sent": 0, "failed": 0, "skipped": "not_requested"}

    if "push" in ch:
        if not has_push(db, None, tenant_id=tenant_id):
            delivered["push"] = {"skipped": "paid_feature"}
        else:
            # TODO(GX.S8 push, metered): actual push dispatch (FCM/APNs or the
            # tenant's push provider). The entitlement seam is live
            # (has_push / usage_metering); the send is a stub until a
            # provider is wired.
            delivered["push"] = {"sent": 0, "failed": 0,
                                 "skipped": "provider_not_configured"}
    else:
        delivered["push"] = {"skipped": "not_requested"}

    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=models.AuditAction.APP_NOTIFICATION_SENT.value,
        details={
            "severity": severity,
            "recipients": len(users),
            "channels": ch,
            "dedupe_key": dedupe_key,
        },
    ))
    db.commit()

    response = {
        "deduped": False,
        "delivered": delivered,
        "dropped": {
            "user_ids": resolved["dropped_users"],
            "group_ids": resolved["dropped_groups"],
        },
        "app_user_types": {"accepted": len(app_user_types),
                           "status": "not_yet_supported"},
    }
    if app_user_types:
        response["app_user_types"]["note"] = (
            "app user-type targeting lands with GX.S1b (TODO)")
    return response
