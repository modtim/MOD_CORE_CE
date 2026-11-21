"""Shared escalation helpers for the support chat and the MOD
assistant.

The /support/email-conversation endpoint used to own the
"render-transcript + fan-out-to-requester + fan-out-to-tenant-admins +
audit" sequence inline. The assistant agent (services/
assistant_orchestrator.py) now exposes a `contact_support` tool that
needs the same fan-out for chat sessions it owns. Rather than
duplicate ~80 lines of resolve / send / log code, the pieces both
sides share live here.

Public surface (callers outside this module):
- SUPPORT_EMAIL                 — env-derived platform-wide CC inbox
- PLATFORM_EMAIL_TENANT_ID      — tenant whose email config drives
                                  outbound (typically id=1)
- resolve_email_tenant(...)     — pick tenant config for FROM:
- get_tenant_admin_emails(...)  — list of inboxes that own customer
                                  service for a tenant
- render_transcript(...)        — turn a list of {role, content,
                                  sources} dicts into (html, text)
- send_support_escalation(...)  — the email fan-out itself
"""

from __future__ import annotations
from utils.pii_redaction import mask_email


import html
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")

# Tenant whose email config is used for ALL support-chat outbound
# email — escalation transcripts and emergency alerts. By convention
# tenant_id=1 is the platform-admin's tenant and is the only tenant
# expected to have SMTP configured. Override via env if a different
# convention is in use.
PLATFORM_EMAIL_TENANT_ID = int(os.getenv("PLATFORM_EMAIL_TENANT_ID", "1"))


def resolve_email_tenant(db: Session, fallback_tenant_id: int) -> int:
    """Resolve which tenant's email config to use for a support
    outbound. Try the platform tenant first (so the From: address
    reads as the platform); fall back to the requester's tenant if
    the platform doesn't have a usable provider config (smtp_host
    for custom, or brevo/mailgun API key). Without this fallback,
    a half-configured platform tenant silently drops every send.
    """
    try:
        import crud
        cfg = crud.get_decrypted_email_config(
            db, tenant_id=PLATFORM_EMAIL_TENANT_ID
        )
        if cfg and (
            cfg.get("smtp_host")
            or cfg.get("brevo_api_key")
            or cfg.get("mailgun_api_key")
        ):
            return PLATFORM_EMAIL_TENANT_ID
    except Exception as e:
        logger.warning("platform email config lookup failed: %s", e)
    return fallback_tenant_id


def get_tenant_admin_emails(db: Session, tenant_id: int) -> List[str]:
    """Return the emails of all active tenant-admin users for *tenant_id*.

    Tenant admins are the parent users of a tenant (parent_user_id IS
    NULL), active, not soft-deleted. These are the people who own
    customer service for their own tenant — escalations belong in
    their inbox, not just the platform admin's.
    """
    if not tenant_id:
        return []
    try:
        import models
        rows = db.query(models.User).filter(
            models.User.tenant_id == tenant_id,
            models.User.parent_user_id.is_(None),
            models.User.status == models.UserStatus.ACTIVE,
            models.User.deleted_at.is_(None),
        ).all()
        return [u.email for u in rows if u.email]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "support_escalation: tenant_admin lookup failed for tenant %s: %s",
            tenant_id, e,
        )
        return []


def render_transcript(
    messages: Iterable[Dict[str, Any]],
    *,
    include_sources: bool = False,
) -> Tuple[str, str]:
    """Render a list of message dicts as (html, plaintext) transcripts.

    Each message dict must have:
      - `role`: "user" / "assistant" (anything else is skipped)
      - `content`: str

    Optional:
      - `sources`: list of {title, url} dicts (only rendered when
        include_sources=True; the support-chat sessions carry these,
        the assistant sessions currently don't)

    HTML output is escaped. Plaintext output is one line per
    role+content with sources joined on the same line.
    """
    html_lines: List[str] = []
    text_lines: List[str] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        who = "You" if role == "user" else "Assistant"
        content = m.get("content") or ""
        html_lines.append(
            f"<p><strong>{html.escape(who)}:</strong><br>{html.escape(content)}</p>"
        )
        text_lines.append(f"{who}: {content}")
        if include_sources:
            sources = m.get("sources") or []
            if sources:
                html_lines.append(
                    "<ul>"
                    + "".join(
                        f'<li><a href="{html.escape(s.get("url", ""))}">'
                        f"{html.escape(s.get('title', ''))}</a></li>"
                        for s in sources
                    )
                    + "</ul>"
                )
                text_lines.append(
                    "Sources: "
                    + ", ".join(
                        f"{s.get('title', '')} ({s.get('url', '')})"
                        for s in sources
                    )
                )

    transcript_html = "\n".join(html_lines) or "<p>(empty conversation)</p>"
    transcript_text = "\n\n".join(text_lines) or "(empty conversation)"
    return transcript_html, transcript_text


def send_support_escalation(
    db: Session,
    *,
    user,  # models.User — typed loosely to avoid an import cycle
    session_label: str,
    transcript_html: str,
    transcript_text: str,
    note: Optional[str] = None,
    recipient: Optional[str] = None,
    cc_support: bool = True,
    origin: str = "support-chat",
) -> Dict[str, Any]:
    """Email a chat transcript to (a) the requester, (b) every active
    tenant admin for their tenant. Reuses utils.email_sender's
    send_chat_transcript_email under the hood so the email shape
    matches the existing /support/email-conversation contract.

    `origin` distinguishes the call site in admin-side previews and
    audit metadata — "support-chat" for the legacy widget, "mod-
    assistant" for tool-invocations from the assistant orchestrator.

    Returns a result envelope:
      {
        "ok": bool,
        "recipient": str,
        "tenant_admin_count": int,
        "tenant_admins_failed": List[str],
        "message": str,
      }

    On a hard failure (requester send errors out), returns ok=False
    with a populated `message`. Tenant-admin send failures are
    individually logged but do NOT flip ok to False — the requester-
    side delivery is the source of truth for "the user got their
    transcript."
    """
    resolved_recipient = recipient or user.email
    if not resolved_recipient:
        return {
            "ok": False,
            "recipient": "",
            "tenant_admin_count": 0,
            "tenant_admins_failed": [],
            "message": "No recipient email available",
        }

    tenant_admin_emails = get_tenant_admin_emails(db, user.tenant_id)
    # Don't double-deliver to a tenant admin who's ALSO the user
    # initiating the send (admin testing their own chat).
    user_email_l = (user.email or "").lower()
    tenant_admin_emails = [
        e for e in tenant_admin_emails if e.lower() != user_email_l
    ]

    try:
        from utils.email_sender import send_chat_transcript_email
        email_tenant = resolve_email_tenant(db, user.tenant_id)

        # Send #1: the requester gets their transcript.
        ok = send_chat_transcript_email(
            db=db,
            tenant_id=email_tenant,
            recipient_email=resolved_recipient,
            recipient_name=user.username or resolved_recipient,
            session_id=session_label,
            transcript_html=transcript_html,
            transcript_text=transcript_text,
            note=note,
            cc_support=cc_support,
            support_email=SUPPORT_EMAIL or None,
        )

        # Send #2+: tenant admins each get a copy flagged with the
        # origin. They CAN reply to the user from there. Failures
        # here log but don't fail the user-facing request — the
        # requester-side send is the contract.
        origin_tag = (
            "[USER-INITIATED ESCALATION — mod-assistant]"
            if origin == "mod-assistant"
            else "[USER-INITIATED ESCALATION]"
        )
        admin_note = (
            f"{origin_tag} User {user.email} requested support."
            + (f" Their note: {note}" if note else "")
        )
        failed_admins: List[str] = []
        for admin_email in tenant_admin_emails:
            try:
                send_chat_transcript_email(
                    db=db,
                    tenant_id=email_tenant,
                    recipient_email=admin_email,
                    recipient_name="Tenant admin",
                    session_id=session_label,
                    transcript_html=transcript_html,
                    transcript_text=transcript_text,
                    note=admin_note,
                    cc_support=False,
                    support_email=None,
                )
            except Exception as admin_err:
                failed_admins.append(admin_email)
                logger.warning(
                    "support_escalation: tenant-admin notify failed for %s: %s",
                    mask_email(admin_email), admin_err,
                )
    except Exception as e:
        logger.error("support_escalation dispatch failed: %s", e)
        return {
            "ok": False,
            "recipient": resolved_recipient,
            "tenant_admin_count": len(tenant_admin_emails),
            "tenant_admins_failed": [],
            "message": f"Failed to send: {e}",
        }

    if not ok:
        return {
            "ok": False,
            "recipient": resolved_recipient,
            "tenant_admin_count": len(tenant_admin_emails),
            "tenant_admins_failed": failed_admins,
            "message": "Email worker did not enqueue the message",
        }

    return {
        "ok": True,
        "recipient": resolved_recipient,
        "tenant_admin_count": len(tenant_admin_emails),
        "tenant_admins_failed": failed_admins,
        "message": f"Transcript emailed to {resolved_recipient}.",
    }
