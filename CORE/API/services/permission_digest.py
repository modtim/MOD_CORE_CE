"""Daily permission-update digest sender.

Runs every 15 minutes as an APScheduler job. For each user with
unsent rows in `pending_permission_emails`, checks whether the user's
local time falls inside the 09:00-09:14 window. If yes, drains every
unsent row for that user, renders one HTML digest covering all the
queued events, sends a single email, stamps `sent_at` on every
drained row.

The 15-min cadence pairs with the 15-min send window: every
local-09:00 across every timezone lands inside at least one tick, so
a user in Pacific gets their digest within 15 min of 09:00 PST, a
user in CET gets theirs within 15 min of 09:00 CET, etc. Quiet days
(no queued events) → no email sent.

**Time-sensitive notifications bypass this path** (billing, permission
requests, invitations, welcome). Only the two ambient categories
(workflow_complete, permission_expiration) route through here.

Timezone source: `users.timezone` if/when the column lands.
Pre-column: every user defaults to UTC. Operators in non-UTC zones
will get their digest at 09:00 UTC until that's fixed — better than
the per-event spam they had before, and an easy upgrade path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


# Send window — the digest fires when user-local time is in
# [SEND_HOUR:SEND_MINUTE, SEND_HOUR:SEND_MINUTE + WINDOW_MINUTES).
# With WINDOW_MINUTES == APScheduler tick interval (15 min), every
# local 09:00 across every timezone lands inside exactly one tick.
SEND_HOUR = 9
SEND_MINUTE = 0
WINDOW_MINUTES = 15


def _get_user_local_now(user: "models.User", utc_now: datetime) -> datetime:
    """Resolve the user's current local time.

    Pre-`users.timezone`-column: every user is UTC. Once the column
    lands (e.g. as a string IANA zone like "America/Los_Angeles") this
    helper resolves it via zoneinfo and returns the right local time.
    """
    tz_name = getattr(user, "timezone", None) or "UTC"
    if tz_name == "UTC":
        return utc_now.astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc_now.astimezone(ZoneInfo(tz_name))
    except Exception:
        # Bad value in the column shouldn't break the digest for
        # everyone. Fall back to UTC for this user.
        return utc_now.astimezone(timezone.utc)


def _user_is_in_send_window(local_now: datetime) -> bool:
    """True when local_now falls inside [SEND_HOUR:SEND_MINUTE,
    SEND_HOUR:SEND_MINUTE + WINDOW_MINUTES)."""
    if local_now.hour != SEND_HOUR:
        return False
    return SEND_MINUTE <= local_now.minute < SEND_MINUTE + WINDOW_MINUTES


def _render_digest_subject(event_count: int) -> str:
    plural = "" if event_count == 1 else "s"
    return f"Daily MOD digest — {event_count} update{plural}"


def _render_digest_body(user: "models.User", events: list[models.PendingPermissionEmail]) -> tuple[str, str]:
    """Return (plain_text, html) bodies for the digest.

    Kept simple — operators want signal, not chrome. Groups events by
    type, lists each event's most useful summary line.
    """
    first_name = (user.first_name or user.username or "there").strip()

    # Group by event_type so the digest reads as sections, not a wall.
    by_type: dict[str, list[models.PendingPermissionEmail]] = {}
    for e in events:
        by_type.setdefault(e.event_type, []).append(e)

    text_lines = [
        f"Hi {first_name},",
        "",
        f"You have {len(events)} MOD update(s) from the past day:",
        "",
    ]
    html_parts = [
        f"<p>Hi {first_name},</p>",
        f"<p>You have <strong>{len(events)}</strong> MOD update(s) from the past day:</p>",
    ]

    for event_type, rows in by_type.items():
        if event_type == "workflow_complete":
            heading = f"Workflow completions ({len(rows)})"
            text_lines.append(heading)
            text_lines.append("-" * len(heading))
            html_parts.append(f"<h3>{heading}</h3><ul>")
            for r in rows:
                p = r.event_payload or {}
                name = p.get("workflow_name") or f"run #{p.get('workflow_run_id')}"
                stat = p.get("status") or "completed"
                line = f"  • {name} — {stat}"
                text_lines.append(line)
                html_parts.append(f"<li><strong>{name}</strong> — {stat}</li>")
            html_parts.append("</ul>")
            text_lines.append("")
        elif event_type == "permission_expiration":
            heading = f"Permission expiration warnings ({len(rows)})"
            text_lines.append(heading)
            text_lines.append("-" * len(heading))
            html_parts.append(f"<h3>{heading}</h3><ul>")
            for r in rows:
                p = r.event_payload or {}
                folder = p.get("folder_name") or "(unknown folder)"
                days = p.get("days_until_expiration")
                days_txt = f"in {days} day(s)" if days is not None else "soon"
                line = f"  • {folder} expires {days_txt}"
                text_lines.append(line)
                html_parts.append(f"<li><strong>{folder}</strong> expires {days_txt}</li>")
            html_parts.append("</ul>")
            text_lines.append("")
        else:
            # Forward-compat: any new digest event_type prints generically.
            heading = f"{event_type} ({len(rows)})"
            text_lines.append(heading)
            html_parts.append(f"<h3>{heading}</h3><ul>")
            for r in rows:
                text_lines.append(f"  • {r.event_payload}")
                html_parts.append(f"<li>{r.event_payload}</li>")
            html_parts.append("</ul>")

    text_lines.append("— MOD")
    html_parts.append("<p>— MOD</p>")
    return "\n".join(text_lines), "\n".join(html_parts)


def _send_digest_for_user(db: Session, user: "models.User", rows: list[models.PendingPermissionEmail]) -> bool:
    """Render + send one digest. Returns True on success, False on failure
    (the rows stay unsent so the next pass retries)."""
    if not rows:
        return True
    if not user.email:
        logger.warning("permission_digest: user %s has no email; marking rows sent to drain queue", user.id)
        return True  # drain the rows; nothing to send to

    subject = _render_digest_subject(len(rows))
    text_body, html_body = _render_digest_body(user, rows)

    try:
        from utils.email_sender import send_templated_email, EmailTemplateMissing
        # We can't rely on a "digest" template existing in every
        # tenant's email_templates table. Send a plain text + HTML
        # body directly via the same mailer.
        send_templated_email(
            db=db,
            tenant_id=user.tenant_id,
            template_name=None,  # bypass template lookup; use body_override
            props={},
            recipient_email=user.email,
            recipient_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
            subject=subject,
            body_override=html_body,
            text_body_override=text_body,
        )
        return True
    except TypeError:
        # Older send_templated_email signatures didn't accept overrides;
        # fall back to firing the raw SMTP send path.
        try:
            from utils.email_sender import _get_email_env, _send_email_smtp
            env = _get_email_env(db, user.tenant_id)
            env["RECIPIENT_EMAIL"] = user.email
            env["RECIPIENT_NAME"] = f"{user.first_name or ''} {user.last_name or ''}".strip()
            env["SUBJECT"] = subject
            env["BODY_HTML"] = html_body
            env["BODY_TEXT"] = text_body
            ok = _send_email_smtp(env)
            return bool(ok)
        except Exception as e:
            logger.warning("permission_digest: fallback SMTP send failed for user %s: %s", user.id, e)
            return False
    except Exception as e:
        logger.warning("permission_digest: send failed for user %s: %s", user.id, e)
        return False


def run_digest_tick(db_factory) -> dict:
    """APScheduler entry point. Run every 15 minutes.

    Scans users with unsent rows in pending_permission_emails. For
    each one whose local time is currently in the 09:00 send window,
    drains rows + sends the digest + stamps sent_at.

    Returns a small counters dict for /metrics surfacing later.
    """
    db = db_factory()
    counters = {"users_checked": 0, "digests_sent": 0, "events_drained": 0, "errors": 0}
    try:
        utc_now = datetime.now(timezone.utc)
        # Pull every distinct user_id with unsent rows. Cheap thanks to
        # the partial index on (user_id) WHERE sent_at IS NULL.
        user_ids = [
            row[0]
            for row in db.query(models.PendingPermissionEmail.user_id)
            .filter(models.PendingPermissionEmail.sent_at.is_(None))
            .distinct()
            .all()
        ]
        for uid in user_ids:
            counters["users_checked"] += 1
            user = db.query(models.User).filter(models.User.id == uid, models.User.deleted_at.is_(None)).first()
            if user is None:
                # User was soft-deleted after queueing — drain their rows.
                _drain(db, uid)
                continue

            local_now = _get_user_local_now(user, utc_now)
            if not _user_is_in_send_window(local_now):
                continue

            rows = (
                db.query(models.PendingPermissionEmail)
                .filter(
                    models.PendingPermissionEmail.user_id == uid,
                    models.PendingPermissionEmail.sent_at.is_(None),
                )
                .order_by(models.PendingPermissionEmail.created_at.asc())
                .all()
            )
            if not rows:
                continue

            ok = _send_digest_for_user(db, user, rows)
            if ok:
                stamp = datetime.now(timezone.utc)
                for r in rows:
                    r.sent_at = stamp
                db.commit()
                counters["digests_sent"] += 1
                counters["events_drained"] += len(rows)
            else:
                counters["errors"] += 1
                db.rollback()
                logger.info(
                    "permission_digest: send failed for user %s with %d queued events — will retry next tick",
                    uid, len(rows),
                )
        return counters
    except Exception as e:
        logger.exception("permission_digest tick raised: %s", e)
        counters["errors"] += 1
        return counters
    finally:
        try:
            db.close()
        except Exception:
            pass


def _drain(db: Session, user_id: int) -> None:
    """Mark every unsent row for a (now-deleted) user as sent so they
    don't linger in the queue forever."""
    try:
        stamp = datetime.now(timezone.utc)
        db.query(models.PendingPermissionEmail).filter(
            models.PendingPermissionEmail.user_id == user_id,
            models.PendingPermissionEmail.sent_at.is_(None),
        ).update({"sent_at": stamp}, synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
