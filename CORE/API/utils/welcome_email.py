"""Welcome-email send + tracking helper.

One canonical send path used by both the signup flow
(routers/onboarding.py) and the /platform/signup-orphans resend
endpoint. Stamps `users.welcome_email_sent_at` on success and
`users.welcome_email_error` on failure so ops can find half-formed
accounts whose welcome never landed.

The signup flow commits the user row BEFORE this helper runs, so a
silent failure here leaves an account in a state where the user can
authenticate (Keycloak account exists) but never received the
credentials / next-step email. Pre-fix the only signal was a single
WARNING log line — easy to lose. Now it's queryable via the orphans
endpoint and surfaced in the platform-admin dashboard.
"""
from __future__ import annotations
from utils.pii_redaction import mask_email
from utils.paid_import import optional_paid


import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


def send_welcome_email(db: Session, user: "models.User") -> tuple[bool, Optional[str]]:
    """Send the welcome email for *user* and record the outcome on the
    users row.

    Returns ``(ok, error_message)``. ``ok`` is True iff the email left
    the API process without raising. ``error_message`` is the str() of
    the exception when ok is False, otherwise None.

    Idempotent in the success sense — calling repeatedly after a
    successful send overwrites sent_at with the latest timestamp and
    re-clears the error. The resend endpoint uses that to bump the
    timestamp when an operator manually retries an orphan.

    Commits the timestamp/error update in its own transaction so the
    tracking write doesn't get rolled back by an unrelated failure
    further up the call chain.
    """
    # GSEC FX.140 part 5 — on-prem installs never send the welcome email
    # through ANY path. The sign-up caller already has its own saas fence
    # (routers/onboarding.py), but the platform-admin resend endpoint
    # (routers/platform_signup_orphans.py) does not, so the guard lives in
    # the sender itself: one canonical send path, one guard, every caller
    # covered. Imported lazily to avoid an import cycle
    # (services/email_unsubscribe pulls in models/consent machinery).
    _eu = optional_paid("services.email_unsubscribe")
    if _eu is None:
        # CE carve: strip the inline check that _is_onprem() uses.
        _onprem = os.getenv("DEPLOYMENT_MODE", "onprem").lower() != "saas"
    else:
        _onprem = _eu._is_onprem()

    if _onprem:
        return False, "on-prem: welcome send suppressed"

    if not user or not user.email:
        return False, "no recipient email"

    error_message: Optional[str] = None
    try:
        # Imported lazily so this module stays import-cheap when the
        # API is starting up and email_sender hasn't loaded its SMTP
        # template environment yet.
        from utils.email_sender import send_templated_email

        send_templated_email(
            db=db,
            tenant_id=user.tenant_id,
            template_name="welcome",
            props={
                "first_name": user.first_name or user.username,
                "plan_name": "Sandbox",
            },
            recipient_email=user.email,
            recipient_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
            subject="Welcome to MOD Platform",
        )
    except Exception as e:
        error_message = str(e) or e.__class__.__name__
        logger.warning(
            "welcome email send failed for user %s (%s): %s",
            user.id, mask_email(user.email), error_message,
        )

    # Stamp the outcome on the users row. Don't let a tracking-write
    # failure mask the email outcome we're trying to record, so log
    # and swallow — the user-row state will still reflect the last
    # successful stamp, and ops can re-resend from the dashboard.
    try:
        user.welcome_email_sent_at = datetime.now(timezone.utc) if error_message is None else user.welcome_email_sent_at
        user.welcome_email_error = error_message
        db.add(user)
        db.commit()
    except Exception as commit_err:
        logger.error(
            "failed to record welcome-email outcome for user %s: %s",
            user.id, commit_err,
        )
        try:
            db.rollback()
        except Exception:
            pass

    return error_message is None, error_message
