#!/usr/bin/env python3
"""
Get the invitation link for testing.
"""
from utils.pii_redaction import mask_email

import os
import sys
import logging
sys.path.insert(0, '/app')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import models

# Setup logging
from utils.log_config import configure_logging
configure_logging()
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data). GS.PSR.9:
# resolved through the DB-backed platform-settings registry (DB > DEBUG_MODE
# env > default) rather than the env var directly.
from services.debug_mode import is_debug_mode

DEBUG_MODE = is_debug_mode()


def build_accept_link(dashboard_url: str, token: str) -> str:
    """Accept-page URL for an invitation token.

    Mirrors the shape used by the real invitation email
    (utils/email_sender.py:543): the accept page is served at the dashboard
    web root, NOT under /pages/. Pure function — no DB, no logging.
    """
    return f"{dashboard_url.rstrip('/')}/accept-invitation.html?token={token}"


DATABASE_URL = os.getenv("DATABASE_URL")
from database import _pg_ssl_connect_args
engine = create_engine(DATABASE_URL, connect_args=_pg_ssl_connect_args())
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = SessionLocal()

try:
    invitation = db.query(models.Invitation).filter(
        models.Invitation.status == models.InvitationStatus.PENDING
    ).first()

    if invitation:
        dashboard_url = os.getenv("DASHBOARD_URL", "http://dashboard.localhost")
        # FX.96 (G-1/G-2): the raw invitation token/link must never reach a log,
        # even under DEBUG_MODE. The digest identifies the row for correlation;
        # the credential itself is fetched from the DB/admin UI when needed.
        from utils.secret_redaction import hash_for_log
        token_digest = hash_for_log(invitation.token) if invitation.token else "h:none"
        # FX.96: the digest is all we ever log; the working link (real token)
        # goes to stdout only, and only when the operator passes --show-link.
        show_link = "--show-link" in sys.argv

        logger.info("=" * 80)
        logger.info("INVITATION LINK FOR TESTING")
        logger.info("=" * 80)
        logger.info("")
        logger.info(f"Email: {mask_email(invitation.email)}")
        logger.info(f"Created: {invitation.created_at}")
        logger.info(f"Expires: {invitation.expires_at}")
        logger.info("")
        # FX.96: never log the raw token — only its digest (DEBUG_MODE no longer
        # opens a raw-secret path here; see G-2 in log-redaction-audit.md).
        logger.info(f"Token: <digest {token_digest}> (raw token never logged; "
                    f"read models.Invitation id={invitation.id} from the DB)")
        logger.info("")
        # FX.96: the logged link carries the token DIGEST only — the raw
        # invitation link is never written to a log, in any mode.
        link = f"{dashboard_url.rstrip('/')}/accept-invitation.html?token={token_digest}"
        logger.info(f"Invitation Link (logged form — digest, NOT a working link): {link}")
        logger.info("Re-run with --show-link to print the accept URL to this "
                    "terminal (not logged)")
        logger.info("")
        logger.info("=" * 80)
        if show_link and invitation.token:
            print(build_accept_link(dashboard_url, invitation.token))
    else:
        logger.info("No pending invitations found")

finally:
    db.close()
