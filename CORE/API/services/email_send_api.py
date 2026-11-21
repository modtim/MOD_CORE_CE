"""GX.S4 (launch-completion-megaplan Group X) — general email send API.

Business logic for `routers/email_send_api.py`. Apps built on MOD send email
through the TENANT'S EXISTING SMTP config — resolution reuses
`utils.email_sender._resolve_email_config` (TenantEmailConfig row + Vault
password override), so there is exactly ONE SMTP config path in the platform.

Edition split (GX.S0, owner decision 2026-10-01):
  * CE:  POST /v1/email/send + the `email_messages` record (status
         queued/sent/failed from our own SMTP attempt) for everyone.
  * Metered (`usage_metering` capability — the same entitlement axis
         `dependencies.require_entitlement` resolves): delivery/bounce
         status updates from the existing Postmark webhook (this module's
         `apply_provider_bounce`, wired into
         services/postmark_webhooks_service.handle_bounce_payload), and
         template rendering (seam below).

The record row stores METADATA ONLY — recipient lists, total size,
attachment filename/mime/size. NEVER attachment bytes or message bodies.
Rows are soft-delete only (ISO 27001).

Limits are DB platform settings (services.platform_settings) — never env.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import models
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform settings (services.platform_settings) — DB-backed, never env.
# ---------------------------------------------------------------------------
MAX_RECIPIENTS_KEY = "email.max_recipients"
MAX_TOTAL_SIZE_KEY = "email.max_total_size_bytes"
MAX_ATTACHMENTS_KEY = "email.max_attachments"
RATE_PER_MINUTE_KEY = "email.rate_limit_per_minute"
RATE_PER_HOUR_KEY = "email.rate_limit_per_hour"

DEFAULT_MAX_RECIPIENTS = 25
DEFAULT_MAX_TOTAL_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
DEFAULT_MAX_ATTACHMENTS = 5
DEFAULT_RATE_PER_MINUTE = 30
DEFAULT_RATE_PER_HOUR = 300


def _setting(key: str, default, db, tenant_id: Optional[int]):
    """Resolve a registered setting (tenant row > platform row > default)."""
    from services import platform_settings
    try:
        return platform_settings.get_setting(key, db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — degrade to the default, never raise
        return default


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
# RFC-5322-ish local part; keeps the validation lenient for +tags and quoted
# dots while still rejecting spaces/control chars/second @.
_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
)
_MAX_SUBJECT_LEN = 512  # matches email_messages.subject VARCHAR(512)


def _assert_no_header_injection(*values: Optional[str]) -> None:
    """422 on any CR/LF (header injection) in caller-supplied header fields."""
    for v in values:
        if v and ("\r" in v or "\n" in v):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Newline characters (CR/LF) are not allowed in "
                       "subject or addresses.",
            )


def validate_addresses(field: str, addresses: List[str]) -> List[str]:
    """Validate + normalize (strip + lowercase) a recipient list.

    Returns the validated list. Raises 422 on CR/LF (header injection),
    empty strings, or addresses that don't look like `local@domain.tld`.
    """
    out: List[str] = []
    for raw in addresses or []:
        if raw is None:
            continue
        addr = str(raw).strip()
        _assert_no_header_injection(addr)
        if not addr:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field}: empty address.",
            )
        if "@" not in addr:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field}: '{addr}' is not a valid email address.",
            )
        addr = addr.lower()
        if not _ADDRESS_RE.match(addr):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field}: '{addr}' is not a valid email address.",
            )
        out.append(addr)
    return out


def validate_subject(subject: Optional[str]) -> str:
    s = (subject or "").strip()
    _assert_no_header_injection(s)
    if not s:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject is required.",
        )
    if len(s) > _MAX_SUBJECT_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subject must be at most {_MAX_SUBJECT_LEN} characters.",
        )
    return s


# ---------------------------------------------------------------------------
# Limits + rate limit
# ---------------------------------------------------------------------------
def limit_bundle(db, tenant_id: Optional[int]) -> Dict[str, Any]:
    """All GX.S4 tunables as DB platform settings (tenant row wins)."""
    return {
        "max_recipients": int(_setting(MAX_RECIPIENTS_KEY,
                                       DEFAULT_MAX_RECIPIENTS, db, tenant_id)),
        "max_total_size_bytes": int(_setting(MAX_TOTAL_SIZE_KEY,
                                             DEFAULT_MAX_TOTAL_SIZE_BYTES, db, tenant_id)),
        "max_attachments": int(_setting(MAX_ATTACHMENTS_KEY,
                                        DEFAULT_MAX_ATTACHMENTS, db, tenant_id)),
        "rate_per_minute": int(_setting(RATE_PER_MINUTE_KEY,
                                        DEFAULT_RATE_PER_MINUTE, db, tenant_id)),
        "rate_per_hour": int(_setting(RATE_PER_HOUR_KEY,
                                      DEFAULT_RATE_PER_HOUR, db, tenant_id)),
    }


def _tenant_content_attachment(
    db, tenant_id: int, attachments: List[Dict[str, Any]]
) -> Optional[Tuple[str, str]]:
    """FX.155 part 3 (owner Q142=A) — G6. Return ``(filename, reason)`` for
    the FIRST attachment that carries tenant content, else ``None``.

    Two signals, both fail CLOSED (a lookup error is treated as a match):
      * the attachment bytes' sha256 equals a ``ContentBlob.id`` for this
        tenant (blobs are content-addressed by the sha256 of the bytes), or
      * the filename names a cog (``<cog name>`` or a ``<cog name>.<ext>``)
        in this tenant's file cogs.
    """
    import hashlib

    try:
        for a in attachments:
            raw = bytes(a.get("_bytes") or b"")
            fname = str(a.get("filename") or "").strip()
            if raw:
                digest = hashlib.sha256(raw).hexdigest()
                blob = (db.query(models.ContentBlob.id)
                        .filter(models.ContentBlob.tenant_id == tenant_id,
                                models.ContentBlob.id == digest)
                        .first())
                if blob is not None:
                    return (fname or "(unnamed)",
                            "bytes match a tenant content blob")
            if fname:
                stem = fname.rsplit(".", 1)[0]
                base = fname.lower()
                hit = (db.query(models.Cog.id)
                       .filter(models.Cog.tenant_id == tenant_id,
                               models.Cog.deleted_at.is_(None))
                       .filter((models.Cog.name == fname)
                               | (models.Cog.name == stem)
                               | (models.Cog.name == base)
                               | (models.Cog.name == stem.lower()))
                       .first())
                if hit is not None:
                    return (fname, "filename matches a tenant cog")
    except Exception:  # noqa: BLE001 — fail closed on any lookup error
        logger.warning("FX.155: tenant-content attachment check errored; "
                       "refusing the send (fail closed)")
        return (str(attachments[0].get("filename") or "(unnamed)"),
                "content check could not complete")
    return None


def _rate_limited(tenant_id: int, limits: Dict[str, Any]) -> bool:
    """True when the tenant is over the per-minute OR per-hour cap.

    Uses the shared Redis sliding-window limiter (utils.rate_limiter); a
    Redis outage degrades to open (same posture as every other limiter).
    """
    from utils import rate_limiter
    # FIX-CE-UNLIMITED-B1: a limit <= 0 is the CE "unlimited" sentinel
    # (check_rate_limit would refuse everything at 0). No-op for finite limits.
    if int(limits["rate_per_minute"]) > 0:
        ok1, _r1, _s1 = rate_limiter.check_rate_limit(
            f"email_send:{tenant_id}",
            max_requests=limits["rate_per_minute"],
            window_seconds=60,
            raise_on_limit=False,
        )
        if not ok1:
            return True
    if int(limits["rate_per_hour"]) <= 0:
        return False
    ok2, _r2, _s2 = rate_limiter.check_rate_limit(
        f"email_send_hour:{tenant_id}",
        max_requests=limits["rate_per_hour"],
        window_seconds=3600,
        raise_on_limit=False,
    )
    return not ok2


# ---------------------------------------------------------------------------
# Edition seam (GX.S0): delivery/bounce tracking + templates are metered,
# resolved through the same entitlement axis as dependencies.require_entitlement.
# ---------------------------------------------------------------------------
DELIVERY_TRACKING_CAPABILITY = "usage_metering"


def has_delivery_tracking(db, tenant: Optional[models.Tenant]) -> bool:
    """Metered seam — True when the tenant's license entitles delivery/bounce
    tracking (and template rendering). CE tenants send fine but their
    records only ever show our own queued/sent/failed outcome."""
    from utils import entitlements
    try:
        es = entitlements.get_entitlements(db, tenant)
        return entitlements.evaluate_feature(
            DELIVERY_TRACKING_CAPABILITY, es).allowed
    except Exception as e:  # noqa: BLE001 — fail-open on resolver errors
        logger.warning("email_send_api: entitlement check failed: %s", e)
        return False


# TODO(GX.S4 templates, metered): template rendering (a `template` field on
# POST /v1/email/send expanding a stored template) lands behind the same
# has_delivery_tracking() seam. No Cap in the entitlements catalogue is
# specific to email templates, and the owner decision covers "Delivery/bounce
# tracking + templates" as one metered axis — reuse usage_metering rather than
# inventing a second capability.


# ---------------------------------------------------------------------------
# Bounce / delivery status (metered seam — called from the Postmark webhook)
# ---------------------------------------------------------------------------
# Postmark bounce Types that count as delivery failure.
_FAILURE_TYPES = {"HardBounce", "SoftBounce", "Undeliverable", "Bounced"}
# Postmark message IDs embed an underscore + timestamp (e.g.
# "abc@1234.0"). Our header value is RFC 5322-safe ("=emsgid-<id>"), so the
# record match uses the suffix after the last underscore.
_POSTMARK_TS_SUFFIX_RE = re.compile(r"_\d+\.\d+(?:\.|$)")


def _provider_matches(record_pid: Optional[str], postmark_id: Optional[str]) -> bool:
    if not postmark_id:
        return False
    if not record_pid:
        return False
    if postmark_id in record_pid or record_pid in postmark_id:
        return True
    m = _POSTMARK_TS_SUFFIX_RE.search(postmark_id)
    if m:
        ts = m.group(0).rstrip(".")
        return ts and ts in record_pid
    return False


def apply_provider_bounce(db, *, recipient: Optional[str],
                          message_id: Optional[str],
                          bounce_type: Optional[str],
                          tenant_id: Optional[int] = None) -> int:
    """Metered seam (has_delivery_tracking) — flip a matching EmailMessage row
    to failed when Postmark reports a bounce for its provider_message_id.

    Called from services.postmark_webhooks_service.handle_bounce_payload
    after signature verification. Best-effort: any error is logged, never
    raised — the webhook's own audit + flagging must not break on us.
    Returns the number of rows updated (0 for CE tenants / no match).
    """
    try:
        tenant = None
        if tenant_id is not None:
            tenant = db.query(models.Tenant).filter(
                models.Tenant.id == tenant_id).first()
        if tenant is None:
            # Resolve from the recipient's user (mirrors the webhook's own
            # user-flagging path); a non-user recipient has no tenant to
            # gate on — skip tracking.
            u = db.query(models.User).filter(
                models.User.email == (recipient or "").strip()).first()
            if u is not None:
                tenant = db.query(models.Tenant).filter(
                    models.Tenant.id == u.tenant_id).first()
        if tenant is None or not has_delivery_tracking(db, tenant):
            return 0
        q = db.query(models.EmailMessage).filter(
            models.EmailMessage.deleted_at.is_(None),
            models.EmailMessage.status.in_(["queued", "sent"]),
        )
        if tenant_id is not None:
            q = q.filter(models.EmailMessage.tenant_id == tenant_id)
        now = datetime.now(timezone.utc)
        updated = 0
        for row in q.all():
            if not _provider_matches(row.provider_message_id, message_id):
                continue
            failed = (bounce_type or "") in _FAILURE_TYPES
            row.status = "failed" if failed else "sent"
            row.sent_at = row.sent_at or now
            row.error = row.error or (
                f"postmark_{(bounce_type or 'delivery').lower()}"
                if failed else "postmark_delivered")
            updated += 1
        if updated:
            db.commit()
        return updated
    except Exception as e:  # noqa: BLE001
        logger.warning("email_send_api: bounce tracking failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


# ---------------------------------------------------------------------------
# Send path
# ---------------------------------------------------------------------------
def _smtp_env(db, tenant_id: int) -> Optional[Dict[str, str]]:
    """Resolve the tenant's SMTP env via the EXISTING plumbing
    (utils.email_sender._resolve_email_config → TenantEmailConfig + Vault).
    Returns None when no usable SMTP config exists."""
    from utils import email_sender
    cfg = email_sender._resolve_email_config(db, tenant_id)
    if not cfg or not cfg.get("smtp_host"):
        return None
    return {
        "SMTP_HOST": cfg.get("smtp_host"),
        "SMTP_PORT": str(cfg.get("smtp_port", 587) or 587),
        "SMTP_USER": cfg.get("smtp_user") or "",
        "SMTP_PASSWORD": cfg.get("smtp_password") or "",
        "SENDER_EMAIL": cfg.get("sender_email") or "",
        "SENDER_NAME": cfg.get("sender_name") or "MOD Platform",
    }


def _build_message(env: Dict[str, str], *, subject: str,
                   to_addrs: List[str], cc_addrs: List[str],
                   bcc_addrs: List[str], text: str, html: Optional[str],
                   attachments: List[Dict[str, Any]]) -> Tuple[Any, List[str]]:
    """Build a MIME message + full recipient list. Attachments are
    metadata-only here: the caller has already validated sizes, and the
    bytes travel in this function's closure for the lifetime of ONE send
    only — they are never persisted."""
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr, make_msgid

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr((env["SENDER_NAME"], env["SENDER_EMAIL"]))
    all_addrs = to_addrs + cc_addrs + bcc_addrs
    msg["To"] = ", ".join(to_addrs) if to_addrs else (
        cc_addrs[0] if cc_addrs else (bcc_addrs[0] if bcc_addrs else env["SENDER_EMAIL"]))
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if bcc_addrs:
        msg["Bcc"] = ", ".join(bcc_addrs)
    msg["Message-ID"] = make_msgid(domain=env.get("SMTP_HOST") or None)

    body = MIMEMultipart("alternative")
    if text:
        body.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        body.attach(MIMEText(html, "html", "utf-8"))
    if not text and not html:
        body.attach(MIMEText("", "plain", "utf-8"))
    msg.attach(body)

    for att in attachments or []:
        mime = att.get("mime_type") or "application/octet-stream"
        main, _, sub = mime.partition("/")
        part = MIMEBase(main or "application", sub or "octet-stream")
        part.set_payload(att.get("_b64") or "")
        part.add_header(
            "Content-Disposition", "attachment",
            filename=att.get("filename", "attachment"),
        )
        msg.attach(part)
    return msg, all_addrs


def _send_smtp(env: Dict[str, str], msg, all_addrs: List[str]) -> None:
    """The smtplib boundary — mocked at this level by tests. Raises on
    failure (smtplib errors). Port 465 → SMTP_SSL, else SMTP + STARTTLS
    (port 25 plain), mirroring utils.email_sender._send_email_smtp."""
    import smtplib
    import ssl

    host = env["SMTP_HOST"]
    port = int(env.get("SMTP_PORT") or 587)
    user = env.get("SMTP_USER") or ""
    password = env.get("SMTP_PASSWORD") or ""
    sender = env["SENDER_EMAIL"]
    if port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg, from_addr=sender, to_addrs=all_addrs)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            if port != 25:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg, from_addr=sender, to_addrs=all_addrs)


def _worker(message_id: int, payload: Dict[str, Any],
            session: Optional[Any] = None) -> None:
    """Background send. Production path: its OWN short-lived session (the
    request session is already committed/closed by the time this runs) —
    consistent with the platform's threading.Thread pattern; no new env
    vars, no queue.

    `payload` carries the in-memory-only parts (text/html bodies + decoded
    attachment bytes) so the send actually sends them. They live in the
    thread's closure for ONE send and are never persisted to the record.

    `session` is a test seam: pass an open session to run the worker
    synchronously against it (tests do this; the worker then does NOT own
    and does NOT close the session)."""
    from database import SessionLocal
    own = session is None
    if own:
        session = SessionLocal()
    db = session
    try:
        row = db.query(models.EmailMessage).filter(
            models.EmailMessage.id == message_id).first()
        if row is None:
            logger.warning("email_send_api: message %d vanished before send",
                           message_id)
            return
        env = _smtp_env(db, row.tenant_id)
        if not env:
            # Config removed between request and worker — record it.
            row.status = "failed"
            row.error = "SMTP config unavailable at send time"
            db.commit()
            return
        try:
            msg, all_addrs = _build_message(
                env,
                subject=row.subject,
                to_addrs=list(row.to or []),
                cc_addrs=list(row.cc or []),
                bcc_addrs=list(row.bcc or []),
                text=payload.get("text") or "",
                html=payload.get("html"),
                attachments=payload.get("attachments") or [],
            )
            if not row.provider_message_id:
                # Postmark SMTP preserves the Message-ID we set, so this is
                # the id its bounce webhook reports back (metered tracking).
                row.provider_message_id = str(msg.get("Message-ID") or "")[:512] \
                    or None
            _send_smtp(env, msg, all_addrs)
            row.status = "sent"
            row.sent_at = datetime.now(timezone.utc)
        except Exception as e:  # noqa: BLE001
            row.status = "failed"
            row.error = str(e)[:2000]
            db.add(models.AuditEvent(
                tenant_id=row.tenant_id,
                user_id=row.sender_user_id,
                event_name=models.AuditAction.EMAIL_SEND_FAILED.value,
                details={
                    "email_message_id": row.id,
                    "recipients": len(list(row.to or []) + list(row.cc or [])
                                    + list(row.bcc or [])),
                    "size_bytes": row.size_bytes,
                    "error": str(e)[:500],
                },
            ))
            logger.warning("email_send_api: send failed id=%d: %s",
                           message_id, e)
        db.commit()
    finally:
        if own:
            db.close()


def dispatch_send(message_id: int, payload: Dict[str, Any]) -> threading.Thread:
    """Spawn the daemon thread that performs the SMTP send off the request
    thread and updates the record status. `payload` = in-memory bodies +
    attachment bytes (never persisted)."""
    t = threading.Thread(
        target=_worker, args=(message_id, payload),
        name=f"email-send-{message_id}", daemon=True,
    )
    t.start()
    return t


def prepare_send(db, *, tenant_id: int, user_id: Optional[int],
                 app_id: Optional[int], to: List[str], cc: List[str],
                 bcc: List[str], subject: str, text: Optional[str],
                 html: Optional[str],
                 attachments: Optional[List[Dict[str, Any]]]) -> models.EmailMessage:
    """Validate a send request, enforce the platform limits (422/413/429),
    require a usable SMTP config (409 — record NOT created), then create the
    `queued` record + audit row and dispatch the background send.

    Returns the record (id + queued status). The SMTP send itself runs on a
    daemon thread (dispatch_send) and flips status to sent/failed.
    """
    subject = validate_subject(subject)
    to_addrs = validate_addresses("to", to)
    cc_addrs = validate_addresses("cc", cc)
    bcc_addrs = validate_addresses("bcc", bcc)
    if not to_addrs and not cc_addrs and not bcc_addrs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one recipient is required (to, cc or bcc).",
        )

    attachments = attachments or []
    for a in attachments:
        name = str(a.get("filename") or "attachment").strip()
        _assert_no_header_injection(name)
        a["filename"] = name

    if not text and not html and not attachments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of text, html or attachments.",
        )

    limits = limit_bundle(db, tenant_id)

    total_recipients = len(to_addrs) + len(cc_addrs) + len(bcc_addrs)
    if total_recipients > limits["max_recipients"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many recipients ({total_recipients}); the platform "
                   f"limit is {limits['max_recipients']} per message.",
        )

    if len(attachments) > limits["max_attachments"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many attachments ({len(attachments)}); the platform "
                   f"limit is {limits['max_attachments']} per message.",
        )

    # FX.155 part 3 (owner Q142=A) — G6: customer content never leaves the
    # platform as an email attachment. An attachment whose bytes hash-match
    # a tenant cog/content blob (or whose filename names a cog) is refused
    # 403 — the expiring, revocable, audited share link is the outbound
    # surface. System mail without tenant content is unaffected (it is not
    # tenant-content-attached). Best-effort DB check: a lookup failure
    # fails closed (refuse) — content must not ride out on a broken check.
    if attachments:
        _refuse = _tenant_content_attachment(db, tenant_id, attachments)
        if _refuse is not None:
            _fname, _why = _refuse
            try:
                db.add(models.AuditEvent(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_name="EMAIL_TENANT_CONTENT_ATTACHMENT_REFUSED",
                    details={"filename": _fname, "reason": _why,
                             "attachments": len(attachments)},
                ))
                db.commit()
            except Exception:  # noqa: BLE001 — refusal still stands
                logger.warning("FX.155: attachment-refusal audit write failed")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Email attachments carrying tenant content are not "
                       f"allowed ({_fname}). Share the content with an "
                       f"expiring share link instead.",
            )

    size = (len(text.encode("utf-8")) if text else 0) \
         + (len(html.encode("utf-8")) if html else 0) \
         + len(subject.encode("utf-8"))
    att_meta: List[Dict[str, Any]] = []
    att_payload: List[Dict[str, Any]] = []
    for a in attachments:
        raw = a.get("_bytes") or b""
        raw = bytes(raw)
        size += len(raw)
        att_meta.append({
            "filename": a["filename"],
            "mime_type": a.get("mime_type") or "application/octet-stream",
            "size_bytes": len(raw),
        })
        att_payload.append({
            "filename": a["filename"],
            "mime_type": a.get("mime_type") or "application/octet-stream",
            "_b64": base64.b64encode(raw).decode("ascii"),
        })

    if size > limits["max_total_size_bytes"]:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Message too large ({size} bytes); the platform limit is "
                   f"{limits['max_total_size_bytes']} bytes.",
        )

    if _rate_limited(tenant_id, limits):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Email rate limit exceeded. Try again shortly.",
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": str(limits["rate_per_minute"]),
            },
        )

    # SMTP config — reuse the tenant's existing resolution (409, no record).
    env = _smtp_env(db, tenant_id)
    if not env:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SMTP is not configured for this tenant. Set the tenant "
                   "email config (Admin → Email) before sending.",
        )

    row = models.EmailMessage(
        tenant_id=tenant_id,
        app_id=app_id,
        sender_user_id=user_id,
        to=to_addrs,
        cc=cc_addrs,
        bcc=bcc_addrs,
        subject=subject,
        size_bytes=size,
        attachments=att_meta,  # metadata only — never bytes/bodies
        status="queued",
    )
    db.add(row)
    db.flush()
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=models.AuditAction.EMAIL_SENT.value,
        details={
            "email_message_id": row.id,
            "recipients": total_recipients,
            "size_bytes": size,
            "attachments": len(att_meta),
            # deliberately NO body / attachment content (audit hygiene)
        },
    ))
    db.commit()
    db.refresh(row)

    dispatch_send(row.id, {
        "text": text or "",
        "html": html,
        "attachments": att_payload,  # in-memory only, one send's lifetime
    })
    return row


def get_message(db, message_id: int, tenant_id: int) -> Optional[models.EmailMessage]:
    """Tenant-scoped fetch (cross-tenant ids are indistinguishable from
    missing — no existence oracle)."""
    return db.query(models.EmailMessage).filter(
        models.EmailMessage.id == message_id,
        models.EmailMessage.tenant_id == tenant_id,
        models.EmailMessage.deleted_at.is_(None),
    ).first()


def list_messages(db, tenant_id: int, *, skip: int, limit: int,
                  status: Optional[str] = None):
    """Tenant-scoped paginated list. Returns (items, total)."""
    q = db.query(models.EmailMessage).filter(
        models.EmailMessage.tenant_id == tenant_id,
        models.EmailMessage.deleted_at.is_(None),
    )
    if status:
        q = q.filter(models.EmailMessage.status == status)
    total = q.count()
    items = q.order_by(models.EmailMessage.created_at.desc(),
                       models.EmailMessage.id.desc()).offset(skip).limit(limit).all()
    return items, total


def message_public(row: models.EmailMessage) -> Dict[str, Any]:
    """API-safe representation (no secrets; metadata only by construction)."""
    return {
        "id": row.id,
        "app_id": row.app_id,
        "sender_user_id": row.sender_user_id,
        "to": list(row.to or []),
        "cc": list(row.cc or []),
        "bcc": list(row.bcc or []),
        "subject": row.subject,
        "size_bytes": row.size_bytes,
        "attachments": list(row.attachments or []),
        "status": row.status,
        "error": row.error,
        "provider_message_id": row.provider_message_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }
