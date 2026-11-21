"""
Direct email sender.

Runs inside the API process. Calls crud functions directly instead of
making HTTP round-trips through Keycloak → API.

For provider == "custom" (per-tenant SMTP config) we send inline via
Python's smtplib so we don't depend on a separate email-sender container
image being pushed to Harbor. For provider == "brevo" / "mailgun" we
still enqueue a container task (those providers' images carry their
own template assets, and inline-rendering them here would duplicate
that work).
"""

from utils.pii_redaction import mask_email
from utils.smtp_security import build_ssl_context, friendly_smtp_error, resolve_smtp_mode

import os
import logging
import smtplib
import ssl
import time
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session

import crud
import models
from utils.registries import tenant_registry, platform_registry, join_image_ref

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# launch-day-followup-5 §1A — Vault-backed email config + 60s cache.
#
# When a tenant_email_configs row sets `password_vault_path`, the SMTP
# password is read from Vault on each send. To avoid a Vault round-trip
# per email under burst load, we maintain a per-tenant in-process cache
# (TTL 60s) of the resolved env. The admin email-config save endpoint
# calls `invalidate_email_env_cache(tenant_id)` to drop the entry on
# config change so the next send picks up the new value without a
# container restart.
# ----------------------------------------------------------------------

_EMAIL_ENV_CACHE_TTL_SECONDS = 60
_email_env_cache: Dict[int, Tuple[float, Dict[str, str]]] = {}
_email_env_cache_lock = threading.Lock()


def invalidate_email_env_cache(tenant_id: int) -> None:
    """Drop the cached email env for `tenant_id`.

    Called by POST /admin/email-config on save so the next outbound
    email picks up the freshly-saved config without an API restart.
    """
    with _email_env_cache_lock:
        _email_env_cache.pop(tenant_id, None)


def _read_smtp_password_from_vault(vault_path: str) -> Optional[str]:
    """Read the SMTP password from Vault at the given KV v2 path.

    Returns None on any Vault error so the caller can fall back to the
    legacy resolution path. Errors are logged at warning level — Vault
    being unreachable is operationally important.
    """
    try:
        # Lazy import — vault_client is heavy and not all callers of
        # email_sender require it (e.g. tests).
        from utils.vault_client import VaultError, approle_login, read_kv_v2
    except Exception:  # pragma: no cover — module import edge cases
        return None

    try:
        token: Optional[str] = None
        agent_token_path = "/run/vault/token"
        if os.path.exists(agent_token_path):
            with open(agent_token_path) as f:
                tok = f.read().strip()
            if tok:
                token = tok
        if token is None:
            role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
            secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
            if not role_id or not secret_id:
                return None
            token = approle_login(role_id=role_id, secret_id=secret_id)
        data = read_kv_v2(vault_path, token=token)
        return data.get("smtp_password")
    except VaultError as e:
        logger.warning("Could not read SMTP password from Vault path %s: %s", vault_path, e)
        return None
    except Exception as e:
        logger.warning("Unexpected Vault error reading %s: %s", vault_path, e)
        return None


EXPECTED_TEMPLATES = [
    "welcome", "email-verification", "invitation", "password-reset",
    "payment-received", "payment-failed", "trial-starting",
    "trial-expiring-7d", "trial-expiring-3d", "trial-expiring-1d",
    "subscription-cancelled", "subscription-upgraded",
    "usage-alert-80", "usage-alert-95", "storage-alert",
    "credit-renewal", "monthly-digest",
    "permission-granted", "permission-request", "permission-expiring",
    "upgrade-prompt", "feature-announcement", "win-back", "roi-digest",
    "activation-welcome", "activation-nudge", "activation-bonus-expiry",
    "activation-celebration",
    "payg-soft-warn", "payg-hard-pause", "payg-auto-recharge-failed",
    "bandwidth-alert",
]

TEMPLATE_BUCKET = "email-templates"


class EmailTemplateMissing(Exception):
    """send_templated_email could not load the .tsx template from MinIO.

    Raised when the bucket or object is absent. Callers wrap in
    try/except so the email failure doesn't abort the triggering
    request, but the raise (and the EMAIL_TEMPLATE_MISSING audit
    event emitted alongside it) make the failure visible — without
    this, a missing email-templates bucket produces zero signal
    until users notice their welcome / payment-receipt / alert
    mail never arrived.
    """

    def __init__(self, template_name: str, bucket: str):
        self.template_name = template_name
        self.bucket = bucket
        super().__init__(
            f"Email template '{template_name}.tsx' not found in MinIO "
            f"bucket '{bucket}'"
        )


def _emit_template_missing_audit(
    db: Session,
    tenant_id: Optional[int],
    template_name: str,
    recipient_email: str,
) -> None:
    """Best-effort audit emit for a missing email template. Never raises;
    audit failure must not mask the original EmailTemplateMissing raise.
    """
    try:
        ev = models.AuditEvent(
            event_name="EMAIL_TEMPLATE_MISSING",
            tenant_id=tenant_id,
            user_id=None,
            details={
                "template_name": template_name,
                "bucket": TEMPLATE_BUCKET,
                "recipient_email": recipient_email,
            },
        )
        db.add(ev)
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "EMAIL_TEMPLATE_MISSING audit emit failed (template=%s, recipient=%s): %s",
            template_name, mask_email(recipient_email), e,
        )


def _download_template_from_minio(template_name: str) -> Optional[str]:
    """Download a .tsx email template from MinIO. Returns content or None."""
    try:
        from utils.storage import get_service_client
        client = get_service_client()

        object_name = f"{template_name}.tsx"
        response = client.get_object(TEMPLATE_BUCKET, object_name)
        content = response.read().decode("utf-8")
        response.close()
        response.release_conn()
        return content
    except Exception as e:
        logger.warning("Template '%s' not found in MinIO: %s", template_name, e)
        return None


def _dispatch_react_email(db: Session, tenant_id: int, template_content: str,
                          props: dict, subject: str, recipient_email: str,
                          recipient_name: str = "",
                          headers: Optional[dict] = None):
    """Dispatch the React Email render+send container as a worker task."""
    import json
    from models import ApiTaskQueue, ApiTaskStatus
    from utils.minio_client import get_tenant_bucket_name

    email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
    if not email_config:
        logger.warning("No email config for tenant %d — skipping templated email", tenant_id)
        return

    protocol = os.getenv("PROTOCOL", "https")
    base_domain = os.getenv("BASE_DOMAIN", "localhost")
    dashboard_sub = os.getenv("DASHBOARD_SUBDOMAIN", "dashboard")
    dashboard_url = f"{protocol}://{dashboard_sub}.{base_domain}"

    props["dashboard_url"] = props.get("dashboard_url", dashboard_url)

    # FX.140 part 2 (Q128=A): CAN-SPAM physical-address footer. The
    # platform setting `email.postal_address` (services/platform_settings)
    # is passed to the template props as `postal_address` for NON-service
    # sends only (the RFC 8058 headers mark those — service callers pass
    # no headers and stay byte-for-byte unchanged). Empty setting = no
    # address configured, nothing injected. Template rendering of the
    # footer is a follow-up (the prop is available to every template).
    if headers:
        try:
            from services.platform_settings import get_setting
            postal = get_setting("email.postal_address", db=db,
                                 tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 — a settings miss must not break a send
            postal = ""
        if postal:
            props["postal_address"] = str(postal)

    env = {
        "EMAIL_TEMPLATE_CONTENT": template_content,
        "EMAIL_PROPS": json.dumps(props),
        "EMAIL_SUBJECT": subject,
        "RECIPIENT_EMAIL": recipient_email,
        "RECIPIENT_NAME": recipient_name,
        "SENDER_EMAIL": email_config.get("sender_email", f"noreply@{base_domain}"),
        "SENDER_NAME": email_config.get("sender_name", "MOD Platform"),
        # FX.140 part 1 (Q128=A): RFC 8058 one-click unsubscribe headers
        # (services/email_unsubscribe) for non-service sends. Empty for
        # service/transactional email — those sends stay unchanged.
        "EMAIL_HEADERS": json.dumps(headers) if headers else "",
        "SMTP_HOST": email_config.get("smtp_host", ""),
        "SMTP_PORT": str(email_config.get("smtp_port", 587)),
        "SMTP_USER": email_config.get("smtp_user", ""),
        "SMTP_PASSWORD": email_config.get("smtp_password", ""),
    }

    registry = platform_registry()
    local_registry = tenant_registry()
    if not local_registry:
        # FIX-IMGREF-2: no tenant registry (LAN CE: BASE_DOMAIN is an IP,
        # tenant_registry() renders "") -> nothing can serve react-email.
        # Same shape as the no-email-config skip above: warn + return, no
        # ApiTaskQueue row, no commit. A bare/unqualified ref would enqueue a
        # task the worker can never pull (guaranteed FAILED row).
        logger.warning(
            "email: no tenant registry configured for tenant %d — "
            "skipping react-email dispatch", tenant_id)
        return
    full_image = join_image_ref(local_registry, "react-email:latest")

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id="email-react",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=8,
        task_payload={
            "node_kind": "email_react",
            "processing_image": full_image,
            "container_env": {k: v for k, v in env.items() if v},
            "source_file_location": "",
            "minio_bucket": get_tenant_bucket_name(tenant_id, db),
            "gpu_required": False,
            "container_timeout": 120,
            "skip_download": True,
            "skip_upload": True,
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=2,
        output_namespace="email",
    )
    db.add(task)
    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logger.info("Dispatched React Email task for '%s' to %s", subject, recipient_email)


def send_templated_email(
    db: Session,
    tenant_id: int,
    template_name: str,
    props: dict,
    recipient_email: str,
    recipient_name: str = "",
    subject: str = "MOD Platform",
    headers: Optional[dict] = None,
):
    """Send an email using a React Email template from MinIO.

    `headers` (FX.140 part 1, Q128=A) is an optional dict of extra MIME
    headers — the RFC 8058 one-click unsubscribe pair from
    services/email_unsubscribe for non-service sends. Service/
    transactional callers pass nothing and stay unchanged.

    Downloads the template .tsx from MinIO, dispatches the ReactEmail
    container. Raises ``EmailTemplateMissing`` and emits an
    ``EMAIL_TEMPLATE_MISSING`` audit event when the template (or its
    bucket) is absent, so a misconfigured stage / forgotten seed step
    surfaces in audit_events instead of silently dropping mail. All
    callers in the codebase wrap this in try/except so the raise does
    not abort the triggering request.
    """
    template_content = _download_template_from_minio(template_name)
    if template_content is None:
        logger.error(
            "EMAIL_TEMPLATE_MISSING: '%s' to %s — template '%s.tsx' not found in MinIO bucket '%s'",
            subject, mask_email(recipient_email), template_name, TEMPLATE_BUCKET,
        )
        _emit_template_missing_audit(db, tenant_id, template_name, recipient_email)
        raise EmailTemplateMissing(template_name, TEMPLATE_BUCKET)

    _dispatch_react_email(db, tenant_id, template_content, props, subject,
                          recipient_email, recipient_name, headers)


def check_email_template_health() -> dict:
    """Check which email templates are present in MinIO."""
    present = []
    missing = []
    for name in EXPECTED_TEMPLATES:
        content = _download_template_from_minio(name)
        if content:
            present.append(name)
        else:
            missing.append(name)

    return {
        "healthy": len(missing) == 0,
        "total": len(EXPECTED_TEMPLATES),
        "present": len(present),
        "missing": missing,
        "present_list": present,
    }


def _resolve_email_config(db: Session, tenant_id: int) -> Optional[dict]:
    """Resolve a tenant's email config dict, with Vault-backed password
    override when `password_vault_path` is set on the row.

    Returns the same shape as `crud.get_decrypted_email_config`, but
    with `smtp_password` overridden by the Vault value when available.
    Falls back to the legacy resolution if Vault is unreachable or the
    row has no vault path.
    """
    email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
    if not email_config:
        return None

    # If the row has a Vault path, prefer that value. Otherwise leave
    # the legacy result untouched. Look up the row directly so we don't
    # change the existing crud contract.
    try:
        row = db.query(models.TenantEmailConfig).filter(
            models.TenantEmailConfig.tenant_id == tenant_id
        ).first()
    except Exception:
        row = None

    # Strict isinstance check — Mock sessions from tests return Mock
    # objects with truthy attributes; we don't want to chase Vault
    # paths that don't exist.
    vault_path = (
        row.password_vault_path
        if isinstance(row, models.TenantEmailConfig)
        else None
    )
    if vault_path:
        vault_password = _read_smtp_password_from_vault(vault_path)
        if vault_password:
            email_config["smtp_password"] = vault_password
        # If Vault was unreachable but a vault path was set, we keep
        # whatever the legacy crud returned (likely None) — sends will
        # short-circuit on missing password rather than silently using
        # a stale legacy value.

    return email_config


def _get_email_env(db: Session, tenant_id: int, recipient_email: str,
                   recipient_name: str, extra_env: dict = None) -> Optional[dict]:
    """
    Build environment variables for the email sender container.
    Returns None if email config is not available.

    When the tenant's email config row has `password_vault_path` set,
    the resolved base env (sender info + provider creds + Vault-read
    SMTP password) is cached in-process for
    `_EMAIL_ENV_CACHE_TTL_SECONDS` so we don't hit Vault on every send
    under burst load. When there's no Vault path the cache is bypassed
    — `crud.get_decrypted_email_config` is fast enough and the cache
    would just complicate testability.

    Per-call recipient fields and `extra_env` are merged on top of the
    cached or freshly-resolved base.
    """
    base_env = _build_base_env(db, tenant_id)
    if base_env is None:
        return None

    env = dict(base_env)
    env["RECIPIENT_EMAIL"] = recipient_email
    env["RECIPIENT_NAME"] = recipient_name

    if extra_env:
        env.update(extra_env)

    # Filter out empty values
    return {k: v for k, v in env.items() if v}


def _row_has_vault_path(db: Session, tenant_id: int) -> bool:
    """True iff the tenant_email_configs row has a non-null
    password_vault_path. Used to decide whether to cache.

    Strict isinstance check on the returned row — Mock-based test
    sessions return ``Mock`` objects from `db.query(...).first()`
    which have truthy attribute access for any name; treating those
    as "has vault path" would cache test responses across tests.
    """
    try:
        row = db.query(models.TenantEmailConfig).filter(
            models.TenantEmailConfig.tenant_id == tenant_id
        ).first()
    except Exception:
        return False
    if not isinstance(row, models.TenantEmailConfig):
        return False
    return bool(row.password_vault_path)


def _build_base_env(db: Session, tenant_id: int) -> Optional[Dict[str, str]]:
    """Return the per-tenant base env (no recipient fields).

    Cache only kicks in when the row has a `password_vault_path` set —
    that's the only case where avoiding a per-send round-trip matters.
    Without a Vault path, the existing crud call is cheap and we keep
    test mocks working as before.
    """
    use_cache = _row_has_vault_path(db, tenant_id)

    if use_cache:
        now = time.monotonic()
        with _email_env_cache_lock:
            cached = _email_env_cache.get(tenant_id)
            if cached and (now - cached[0]) < _EMAIL_ENV_CACHE_TTL_SECONDS:
                return cached[1]

    email_config = _resolve_email_config(db, tenant_id)
    if not email_config:
        logger.warning("No email config for tenant %d — skipping email", tenant_id)
        return None

    protocol = os.getenv("PROTOCOL", "https")
    base_domain = os.getenv("BASE_DOMAIN", "localhost")
    dashboard_sub = os.getenv("DASHBOARD_SUBDOMAIN", "dashboard")
    dashboard_url = f"{protocol}://{dashboard_sub}.{base_domain}"

    env: Dict[str, str] = {
        "DASHBOARD_URL": dashboard_url,
        "SENDER_NAME": email_config.get("sender_name", "System"),
        "SENDER_EMAIL": email_config.get("sender_email", f"noreply@{base_domain}"),
    }

    provider = email_config.get("provider", "brevo")

    # Postmark IS just SMTP (smtp.postmarkapp.com:587 + server-token
    # creds), so treat it identically to `custom`. The provider
    # label is purely cosmetic at this point — what matters is
    # whether the row has smtp_host populated. Lumping them
    # together keeps `_dispatch_email_container` from trying to
    # pull a non-existent `harbor.../modtim/brevo:latest` image
    # for any non-`custom` provider that's actually SMTP-backed.
    if provider in ("custom", "postmark") or email_config.get("smtp_host"):
        env.update({
            "USE_SMTP": "true",
            "SMTP_HOST": email_config.get("smtp_host") or "",
            "SMTP_PORT": str(email_config.get("smtp_port", 587)),
            "SMTP_USER": email_config.get("smtp_user") or "",
            "SMTP_PASSWORD": email_config.get("smtp_password") or "",
        })
        # Explicit encryption / self-signed opt-in (utils/smtp_security). Only
        # emitted when set so an unconfigured row yields the same env as before.
        if email_config.get("smtp_security") in ("starttls", "ssl", "none"):
            env["SMTP_SECURITY"] = email_config["smtp_security"]
        if email_config.get("smtp_allow_selfsigned") is True:
            env["SMTP_ALLOW_SELFSIGNED"] = "true"
    elif provider == "mailgun":
        env.update({
            "MAILGUN_API_KEY": email_config.get("mailgun_api_key") or "",
            "MAILGUN_DOMAIN": email_config.get("mailgun_domain") or "",
        })
    else:
        env.update({
            "BREVO_API_KEY": email_config.get("brevo_api_key") or "",
            "BREVO_TEMPLATE_ID": str(email_config.get("brevo_template_id", "")),
        })

    if use_cache:
        with _email_env_cache_lock:
            _email_env_cache[tenant_id] = (time.monotonic(), env)

    return env


def _get_email_image(provider: str) -> str:
    """Get the Docker image for the email sender based on provider."""
    if provider == "mailgun":
        return "mailgun:latest"
    return "brevo:latest"


_EMAIL_TYPE_SUBJECTS = {
    "support_chat_transcript": "Your MOD support chat transcript",
    "invitation": "You've been invited to MOD",
    "permission_notification": "MOD permission update",
    "permission_expiration": "MOD permission expiring soon",
    "permission_request": "MOD permission request",
    "invoice_generated": "Your MOD invoice",
    "payment_failed": "MOD payment failed",
    "payment_success": "MOD payment received",
    "grace_period_warning": "MOD account — action required",
    "credit_low": "Your usage is running low",
}


def _synthesize_smtp_body(env: dict, email_type: str):
    """Build a (text, html) body pair for inline-SMTP email types
    that don't ride on a React Email template. The invitation
    case is the most important — without a clickable accept URL
    the recipient has no way to finalize their account. Other
    types fall back to a minimal but still useful notification.

    Inputs come from `env` which `_get_email_env` populates per
    tenant: DASHBOARD_URL, SENDER_NAME, RECIPIENT_NAME, plus
    type-specific extras the caller passed via `extra_env`.
    """
    dashboard_url = env.get("DASHBOARD_URL", "").rstrip("/")
    sender_name = env.get("SENDER_NAME", "MOD Platform")

    if email_type == "invitation":
        token = env.get("INVITATION_TOKEN", "")
        accept_url = f"{dashboard_url}/accept-invitation.html?token={token}"
        recipient_name = env.get("RECIPIENT_NAME") or "there"
        text = (
            f"Hi {recipient_name},\n\n"
            f"{sender_name} invited you to join MOD Platform.\n\n"
            f"Click the link below to accept the invitation and set up your account:\n"
            f"{accept_url}\n\n"
            f"If you weren't expecting this, you can safely ignore this email — "
            f"the link expires in 7 days.\n\n"
            f"— MOD Platform\n"
        )
        html = (
            f"<!doctype html><html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1f2937;line-height:1.55;max-width:560px;margin:0 auto;padding:24px;\">"
            f"<p>Hi {_html_escape(recipient_name)},</p>"
            f"<p><strong>{_html_escape(sender_name)}</strong> invited you to join MOD Platform.</p>"
            f"<p style=\"margin:28px 0;\"><a href=\"{accept_url}\" "
            f"style=\"background:#0333D0;color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;font-weight:600;display:inline-block;\">"
            f"Accept invitation</a></p>"
            f"<p style=\"font-size:13px;color:#6b7280;\">Or paste this URL into your browser:<br>"
            f"<a href=\"{accept_url}\" style=\"color:#0333D0;word-break:break-all;\">{accept_url}</a></p>"
            f"<p style=\"font-size:13px;color:#6b7280;margin-top:24px;\">If you weren't expecting this, you can safely ignore this email — the link expires in 7 days.</p>"
            f"<p style=\"font-size:13px;color:#9ca3af;\">— MOD Platform</p>"
            f"</body></html>"
        )
        return text, html

    if email_type == "resource_monitor_digest":
        import json as _json
        try:
            offenders = _json.loads(env.get("OFFENDERS_JSON") or "[]")
        except (ValueError, TypeError):
            offenders = []
        try:
            power_users = _json.loads(env.get("POWER_USERS_JSON") or "[]")
        except (ValueError, TypeError):
            power_users = []

        admin_url = f"{dashboard_url}/?tab=resource-monitor#analytics"

        # ---- format helpers ----
        def _fmt_num(n, suffix=""):
            try:
                v = float(n)
            except (TypeError, ValueError):
                return "—"
            if v >= 1000:
                return f"{v:,.0f}{suffix}"
            if v >= 10:
                return f"{v:.1f}{suffix}"
            return f"{v:.2f}{suffix}"

        def _fmt_credits(c):
            try:
                return f"{int(c):,}"
            except (TypeError, ValueError):
                return "—"

        def _fmt_ratio(r, threshold=None):
            if r is None:
                return "—"
            try:
                rf = float(r)
                if rf >= 1e6:
                    return "∞"
                label = f"{rf:.2f}"
                if threshold and rf > threshold:
                    label = f"{label}↑"
                return label
            except (TypeError, ValueError):
                return "—"

        # ---- text body ----
        def _text_row(o):
            above = ",".join(o.get("resources_above_threshold") or []) or "—"
            return (
                f"  • tenant={o.get('tenant_id')} user={o.get('user_id')}"
                f" gpu_sec={_fmt_num(o.get('gpu_seconds'))}"
                f" cpu_core_sec={_fmt_num(o.get('cpu_core_seconds'))}"
                f" ram_gb_sec={_fmt_num(o.get('ram_gb_seconds'))}"
                f" io_mb={_fmt_num(o.get('io_mb'))}"
                f" credits={_fmt_credits(o.get('credits_used'))}"
                f" above={above}"
            )

        text_lines = ["MOD platform — daily resource monitor",
                      "=" * 40, ""]
        if offenders:
            text_lines.append(f"Top {len(offenders)} offender(s) (last 24h):")
            for o in offenders:
                text_lines.append(_text_row(o))
            text_lines.append("")
        if power_users:
            text_lines.append(f"Top {len(power_users)} efficient user(s) (last 24h):")
            for p in power_users:
                text_lines.append(_text_row(p))
            text_lines.append("")
        text_lines.append(f"Investigate: {admin_url}")
        text = "\n".join(text_lines) + "\n"

        # ---- html body ----
        def _pill(ratio, threshold=None):
            """Color-banded per-resource ratio pill. Red when over
            the (optional) threshold; otherwise green-to-amber by
            absolute magnitude."""
            try:
                rf = float(ratio)
            except (TypeError, ValueError):
                return "<span style=\"color:#9ca3af;\">—</span>"
            if rf >= 1e6:
                label = "∞"
                bg, fg = "#fee2e2", "#991b1b"
            elif threshold is not None and rf > threshold:
                label = _fmt_ratio(rf)
                bg, fg = "#fee2e2", "#991b1b"
            else:
                label = _fmt_ratio(rf)
                bg, fg = "#dcfce7", "#166534"
            return (
                f"<span style=\"display:inline-block;padding:2px 8px;border-radius:999px;"
                f"background:{bg};color:{fg};font-size:12px;font-weight:600;\">{label}</span>"
            )

        def _row(t):
            above = set(t.get("resources_above_threshold") or [])
            thresholds = t.get("thresholds") or {}
            return (
                "<tr>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;\">{_html_escape(t.get('tenant_id'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;\">{_html_escape(t.get('user_id'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;\">{_fmt_num(t.get('gpu_seconds'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;\">{_fmt_num(t.get('cpu_core_seconds'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;\">{_fmt_num(t.get('ram_gb_seconds'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;\">{_fmt_num(t.get('io_mb'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;\">{_fmt_credits(t.get('credits_used'))}</td>"
                f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;text-align:right;\">"
                f"{_pill(t.get('gpu_seconds_per_credit'), thresholds.get('gpu') if 'gpu' in above else thresholds.get('gpu'))}"
                f" {_pill(t.get('cpu_core_seconds_per_credit'), thresholds.get('cpu') if 'cpu' in above else thresholds.get('cpu'))}"
                f" {_pill(t.get('ram_gb_seconds_per_credit'), thresholds.get('ram') if 'ram' in above else thresholds.get('ram'))}"
                f" {_pill(t.get('io_mb_per_credit'), thresholds.get('io') if 'io' in above else thresholds.get('io'))}"
                "</td>"
                "</tr>"
            )

        offender_table = "".join(_row(o) for o in offenders) or (
            "<tr><td colspan=\"8\" style=\"padding:12px;color:#6b7280;\">None.</td></tr>"
        )
        power_table = "".join(_row(p) for p in power_users) or (
            "<tr><td colspan=\"8\" style=\"padding:12px;color:#6b7280;\">None.</td></tr>"
        )

        head = (
            "<thead><tr style=\"text-align:left;background:#f9fafb;\">"
            "<th style=\"padding:8px 12px;\">Tenant</th>"
            "<th style=\"padding:8px 12px;\">User</th>"
            "<th style=\"padding:8px 12px;text-align:right;\">GPU sec</th>"
            "<th style=\"padding:8px 12px;text-align:right;\">CPU·s</th>"
            "<th style=\"padding:8px 12px;text-align:right;\">RAM GB·s</th>"
            "<th style=\"padding:8px 12px;text-align:right;\">IO MB</th>"
            "<th style=\"padding:8px 12px;text-align:right;\">Credits</th>"
            "<th style=\"padding:8px 12px;text-align:right;\">Per-credit</th>"
            "</tr></thead>"
        )

        html = (
            "<!doctype html><html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1f2937;line-height:1.55;max-width:840px;margin:0 auto;padding:24px;\">"
            "<h2 style=\"margin:0 0 4px 0;\">MOD platform — daily resource monitor</h2>"
            "<p style=\"color:#6b7280;margin-top:0;\">Native resource consumption vs. usage, last 24 hours. Each row's four pills are GPU / CPU / RAM / IO per credit. Red = over threshold; ∞ = used resource with zero credits billed.</p>"
            "<h3 style=\"margin:24px 0 8px 0;color:#b91c1c;\">Offenders</h3>"
            "<table style=\"width:100%;border-collapse:collapse;font-size:14px;\">"
            f"{head}<tbody>{offender_table}</tbody>"
            "</table>"
            "<h3 style=\"margin:24px 0 8px 0;color:#15803d;\">Efficient power users</h3>"
            "<table style=\"width:100%;border-collapse:collapse;font-size:14px;\">"
            f"{head}<tbody>{power_table}</tbody>"
            "</table>"
            f"<p style=\"margin-top:24px;\"><a href=\"{admin_url}\" "
            "style=\"background:#0333D0;color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;font-weight:600;display:inline-block;\">"
            "Open Resource Monitor</a></p>"
            "<p style=\"font-size:13px;color:#9ca3af;margin-top:24px;\">— MOD Platform</p>"
            "</body></html>"
        )
        return text, html

    # Generic fallback for non-invitation types (permission
    # changes, low credit warnings, etc.). The dashboard URL
    # gives the recipient a place to go even if we don't have a
    # type-specific deep link.
    note = env.get("USER_NOTE", "")
    text = (
        f"This is an automated MOD notification ({email_type}).\n"
        + (f"{note}\n\n" if note else "\n")
        + f"Visit your dashboard for details: {dashboard_url}\n"
    )
    return text, None


def _html_escape(s):
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;"))


_selfsigned_warned = False


def _warn_selfsigned_once(host, port) -> None:
    global _selfsigned_warned
    if not _selfsigned_warned:
        _selfsigned_warned = True
        logger.warning(
            "SMTP 'Allow self-signed certificate' is ON for %s:%s: the server certificate "
            "and host name are NOT verified (encrypted, but not authenticated).", host, port,
        )


def _send_email_smtp(env: dict) -> bool:
    """Inline SMTP send for provider='custom'. Replaces the email-sender
    container path for tenants with their own SMTP config — removes the
    Harbor `modtim/brevo:latest` / `mailgun:latest` image dependency
    that wasn't pushed at launch time.

    Reads SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD from env
    (already populated by `_get_email_env` for the custom provider).
    Builds a multipart message from TRANSCRIPT_HTML + TRANSCRIPT_TEXT
    when present (support-chat case); otherwise sends a minimal plain
    notification synthesized from EMAIL_TYPE so non-template paths
    aren't silently dropped.

    Transport security comes from `utils.smtp_security.resolve_smtp_mode`
    (SMTP_SECURITY = starttls | ssl | none, else the legacy port rule: 465 ->
    SMTP_SSL, 25 -> plain, anything else -> STARTTLS). SMTP_ALLOW_SELFSIGNED=true
    skips certificate verification (WARNING logged once).
    Returns True on successful send, False on any failure (logged).
    """
    host = env.get("SMTP_HOST")
    port_str = env.get("SMTP_PORT", "587")
    user = env.get("SMTP_USER")
    password = env.get("SMTP_PASSWORD")
    sender_email = env.get("SENDER_EMAIL")
    sender_name = env.get("SENDER_NAME", "MOD")
    recipient = env.get("RECIPIENT_EMAIL")
    cc = env.get("CC_SUPPORT")

    if not host or not sender_email or not recipient:
        logger.error("SMTP send skipped — missing host/sender/recipient")
        return False

    try:
        port = int(port_str)
    except ValueError:
        logger.error("SMTP send skipped — invalid port %r", port_str)
        return False

    email_type = env.get("EMAIL_TYPE", "notification")
    subject = _EMAIL_TYPE_SUBJECTS.get(email_type, "MOD notification")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = recipient
    if cc:
        msg["Cc"] = cc

    # FX.140 part 1 (Q128=A): extra MIME headers — the RFC 8058
    # List-Unsubscribe / List-Unsubscribe-Post pair set by
    # services.email_unsubscribe for non-service sends (empty for
    # service/transactional email, so those messages stay unchanged).
    extra_headers = env.get("EMAIL_HEADERS")
    if extra_headers:
        try:
            import json as _json
            for _hk, _hv in (_json.loads(extra_headers) or {}).items():
                if _hv:
                    msg[str(_hk)] = str(_hv)
        except Exception:  # noqa: BLE001 — a malformed header blob must not drop the send
            logger.warning("EMAIL_HEADERS unparsable — send proceeds without them")

    # Body: prefer the html+text the caller supplied (support
    # transcripts); else synthesize a body per email_type so the
    # recipient actually has something actionable in the inbox.
    text_body = env.get("TRANSCRIPT_TEXT")
    html_body = env.get("TRANSCRIPT_HTML")
    if not text_body and not html_body:
        text_body, html_body = _synthesize_smtp_body(env, email_type)
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [recipient] + ([cc] if cc else [])
    mode = resolve_smtp_mode(port, env.get("SMTP_SECURITY"))
    allow_selfsigned = str(env.get("SMTP_ALLOW_SELFSIGNED", "")).lower() == "true"
    if allow_selfsigned:
        _warn_selfsigned_once(host, port)
    try:
        if mode == "ssl":
            ctx = build_ssl_context(allow_selfsigned)
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as smtp:
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(msg, from_addr=sender_email, to_addrs=recipients)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                if mode == "starttls":
                    smtp.starttls(context=build_ssl_context(allow_selfsigned))
                    smtp.ehlo()
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(msg, from_addr=sender_email, to_addrs=recipients)
        logger.info("SMTP email sent (type=%s, to=%s, host=%s:%d)",
                    email_type, mask_email(recipient), host, port)
        return True
    except Exception as e:
        logger.error("SMTP send failed (type=%s, to=%s, host=%s:%d): %s",
                     email_type, mask_email(recipient), host, port,
                     friendly_smtp_error(e, host, port, mode, secrets=[password]))
        return False


def _dispatch_email_container(db: Session, tenant_id: int, env: dict, provider: str = "brevo"):
    """Send an email — inline via SMTP whenever the env has
    SMTP_HOST set (covers `provider='custom'` AND `provider=
    'postmark'`, both of which are SMTP-backed under the hood),
    otherwise enqueue an email-sender container task for the Go
    worker. The container path is legacy; the brevo / mailgun
    Harbor images were never pushed at launch so this branch
    only matters if the operator wires those up themselves."""
    if env.get("SMTP_HOST"):
        _send_email_smtp(env)
        return

    # CE first boot: no provider creds -> the legacy container image is
    # never published (pull 401 -> FAILED email-send row). Skip quietly.
    if not any(env.get(k) for k in ("BREVO_API_KEY", "MAILGUN_API_KEY")):
        logger.info("email: no provider configured; skipping container dispatch")
        return None

    from models import ApiTaskQueue, ApiTaskStatus
    from utils.minio_client import get_tenant_bucket_name

    registry = platform_registry()
    image_name = _get_email_image(provider)
    full_image = f"{registry}/modtim/{image_name}"

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id="email-send",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=5,  # Higher priority than processing tasks
        task_payload={
            "node_kind": "email_send",
            "processing_image": full_image,
            "container_env": env,
            "source_file_location": "",
            "minio_bucket": get_tenant_bucket_name(tenant_id, db),
            "gpu_required": False,
            "container_timeout": 120,
            "skip_download": True,
            "skip_upload": True,
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=2,
        output_namespace="email",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Notify workers
    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logger.info("Email task dispatched (task_id=%s, provider=%s, to=%s)",
                task.id, provider, mask_email(env.get("RECIPIENT_EMAIL", "?")))
    return task.id


def send_invitation_email(db: Session, invitation_id: int, tenant_id: int):
    """Send an invitation email. Replaces send_invitation_email_dag."""
    try:
        invitation = crud.get_invitation_by_id(db, invitation_id=invitation_id)
        if not invitation:
            logger.warning("Invitation %d not found — skipping email", invitation_id)
            return

        inviter = invitation.inviter
        recipient_name = f"{inviter.first_name} {inviter.last_name}" if inviter else "A colleague"

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"

        env = _get_email_env(db, tenant_id, invitation.email, recipient_name, {
            "INVITATION_TOKEN": invitation.token,
            "EMAIL_TYPE": "invitation",
        })
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send invitation email for invitation %d: %s", invitation_id, e)


def send_chat_transcript_email(
    db: Session,
    tenant_id: int,
    recipient_email: str,
    recipient_name: str,
    *,
    session_id: str,
    transcript_html: str,
    transcript_text: str,
    note: Optional[str] = None,
    cc_support: bool = False,
    support_email: Optional[str] = None,
) -> bool:
    """Phase 4 — email a support-chat transcript to the user (and
    optionally CC the platform support inbox). Reuses the same
    tenant-aware SMTP-resolution path as
    send_permission_notification_email so per-tenant SMTP overrides
    apply automatically.

    Returns True on dispatch (email worker enqueued); False on the
    config-missing or env-build short-circuits.
    """
    try:
        extra = {
            "EMAIL_TYPE": "support_chat_transcript",
            "SESSION_ID": session_id,
            "TRANSCRIPT_HTML": transcript_html,
            "TRANSCRIPT_TEXT": transcript_text,
        }
        if note:
            extra["USER_NOTE"] = note
        if cc_support and support_email:
            extra["CC_SUPPORT"] = support_email

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"

        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if not env:
            return False
        _dispatch_email_container(db, tenant_id, env, provider)
        return True
    except Exception as e:
        logger.error("Failed to send chat-transcript email to %s: %s",
                     mask_email(recipient_email), e)
        return False


def send_permission_notification_email(db: Session, tenant_id: int,
                                        recipient_email: str, recipient_name: str,
                                        permission_type: str, details: dict = None):
    """Send a permission change notification. Replaces send_permission_notification_dag."""
    try:
        extra = {
            "EMAIL_TYPE": "permission_notification",
            "PERMISSION_TYPE": permission_type,
        }
        if details:
            for k, v in details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"

        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send permission email to %s: %s", mask_email(recipient_email), e)


def send_permission_expiration_email(db: Session, tenant_id: int,
                                      recipient_email: str, recipient_name: str,
                                      expiration_details: dict = None):
    """Send permission expiration warning. Replaces send_permission_expiration_email_dag."""
    try:
        extra = {"EMAIL_TYPE": "permission_expiration"}
        if expiration_details:
            for k, v in expiration_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"

        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send expiration email to %s: %s", mask_email(recipient_email), e)


def send_permission_request_email(db: Session, tenant_id: int,
                                   recipient_email: str, recipient_name: str,
                                   request_details: dict = None):
    """Send permission request notification. Replaces send_permission_request_notification_dag."""
    try:
        extra = {"EMAIL_TYPE": "permission_request"}
        if request_details:
            for k, v in request_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"

        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send permission request email to %s: %s", mask_email(recipient_email), e)


# =============================================================================
# Billing Email Notifications
# =============================================================================

def send_invoice_generated_email(db: Session, tenant_id: int,
                                  recipient_email: str, recipient_name: str,
                                  invoice_details: dict = None):
    """Send invoice generated notification. Replaces billing email task."""
    try:
        extra = {"EMAIL_TYPE": "invoice_generated"}
        if invoice_details:
            for k, v in invoice_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"
        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send invoice email to %s: %s", mask_email(recipient_email), e)


def send_payment_failed_email(db: Session, tenant_id: int,
                               recipient_email: str, recipient_name: str,
                               failure_details: dict = None):
    """Send payment failure notification."""
    try:
        extra = {"EMAIL_TYPE": "payment_failed"}
        if failure_details:
            for k, v in failure_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"
        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send payment failed email to %s: %s", mask_email(recipient_email), e)


def send_payment_success_email(db: Session, tenant_id: int,
                                recipient_email: str, recipient_name: str,
                                payment_details: dict = None):
    """Send payment success/receipt notification."""
    try:
        extra = {"EMAIL_TYPE": "payment_success"}
        if payment_details:
            for k, v in payment_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"
        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send payment success email to %s: %s", mask_email(recipient_email), e)


def send_grace_period_warning_email(db: Session, tenant_id: int,
                                     recipient_email: str, recipient_name: str,
                                     warning_details: dict = None):
    """Send grace period warning notification."""
    try:
        extra = {"EMAIL_TYPE": "grace_period_warning"}
        if warning_details:
            for k, v in warning_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"
        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send grace period warning to %s: %s", mask_email(recipient_email), e)


def send_credit_low_email(db: Session, tenant_id: int,
                           recipient_email: str, recipient_name: str,
                           credit_details: dict = None):
    """Send credit running low notification."""
    try:
        extra = {"EMAIL_TYPE": "credit_low"}
        if credit_details:
            for k, v in credit_details.items():
                extra[k.upper()] = str(v)

        email_config = crud.get_decrypted_email_config(db, tenant_id=tenant_id)
        provider = email_config.get("provider", "brevo") if email_config else "brevo"
        env = _get_email_env(db, tenant_id, recipient_email, recipient_name, extra)
        if env:
            _dispatch_email_container(db, tenant_id, env, provider)
    except Exception as e:
        logger.error("Failed to send credit low email to %s: %s", mask_email(recipient_email), e)


# =============================================================================
# tenant-resource-monitor — daily digest
# =============================================================================

def send_resource_monitor_digest(db: Session) -> int:
    """Compose + send the daily resource-monitor digest to every active
    platform admin.

    Spec §5. Body content lives in ``_synthesize_smtp_body``'s
    ``resource_monitor_digest`` branch (filled in by §4). Returns the
    count of admins emailed; 0 means either no offenders + no
    power-users, or a send error (always logged, never raised — the
    scheduler thread shouldn't unwind on a transient SMTP hiccup).
    """
    from datetime import datetime, timezone, timedelta
    import json
    from sqlalchemy import func as sql_func

    try:
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=24)

        # Offenders — recent RESOURCE_LEAK_DETECTED audit rows.
        # Audit metadata already has per-resource native totals,
        # ratios, and which resource(s) crossed; we just bubble it up.
        leak_rows = db.query(models.PermissionAuditLog).filter(
            models.PermissionAuditLog.action == models.AuditAction.RESOURCE_LEAK_DETECTED,
            models.PermissionAuditLog.timestamp >= since,
        ).order_by(models.PermissionAuditLog.timestamp.desc()).all()
        offenders = []
        for row in leak_rows[:10]:
            md = row.audit_metadata or {}
            native = md.get("native_totals") or {}
            ratios = md.get("ratios") or {}
            offenders.append({
                "tenant_id": md.get("tenant_id"),
                "user_id": md.get("user_id"),
                "gpu_seconds": native.get("gpu_seconds"),
                "cpu_core_seconds": native.get("cpu_core_seconds"),
                "ram_gb_seconds": native.get("ram_gb_seconds"),
                "io_mb": native.get("io_mb"),
                "credits_used": md.get("credits_used"),
                "gpu_seconds_per_credit": ratios.get("gpu"),
                "cpu_core_seconds_per_credit": ratios.get("cpu"),
                "ram_gb_seconds_per_credit": ratios.get("ram"),
                "io_mb_per_credit": ratios.get("io"),
                "thresholds": md.get("thresholds"),
                "resources_above_threshold": md.get("resources_above_threshold") or [],
            })

        # Power users — lowest max-ratio in the same window, capped 5.
        # "Efficient" means well under threshold across the board, so
        # we score by max per-resource ratio and take the lowest.
        from services.resource_monitor import _get_config as _rm_get_config

        config = _rm_get_config(db)
        thresholds = {
            "gpu": config.gpu_seconds_per_credit_threshold,
            "cpu": config.cpu_core_seconds_per_credit_threshold,
            "ram": config.ram_gb_seconds_per_credit_threshold,
            "io":  config.io_mb_per_credit_threshold,
        }

        pair_resource = db.query(
            models.TaskResourceAggregate.tenant_id,
            models.TaskResourceAggregate.user_id,
            sql_func.sum(models.TaskResourceAggregate.gpu_seconds).label("gpu_seconds"),
            sql_func.sum(models.TaskResourceAggregate.cpu_core_seconds).label("cpu_core_seconds"),
            sql_func.sum(models.TaskResourceAggregate.ram_gb_seconds).label("ram_gb_seconds"),
            sql_func.sum(
                models.TaskResourceAggregate.disk_read_mb
                + models.TaskResourceAggregate.disk_write_mb
            ).label("io_mb"),
        ).filter(
            models.TaskResourceAggregate.completed_at >= since,
            models.TaskResourceAggregate.completed_at < now,
        ).group_by(
            models.TaskResourceAggregate.tenant_id,
            models.TaskResourceAggregate.user_id,
        ).all()
        tenant_credit_used: dict = {}
        for row in db.query(
            models.CreditUsageLog.tenant_id,
            sql_func.sum(models.CreditUsageLog.credits_used).label("credits_used"),
        ).filter(
            models.CreditUsageLog.created_at >= since,
            models.CreditUsageLog.created_at < now,
        ).group_by(models.CreditUsageLog.tenant_id).all():
            tenant_credit_used[row.tenant_id] = int(row.credits_used or 0)

        power_users = []
        for row in pair_resource:
            credits = tenant_credit_used.get(row.tenant_id, 0)
            if credits <= 0:
                continue
            gpu = float(row.gpu_seconds or 0.0)
            cpu = float(row.cpu_core_seconds or 0.0)
            ram = float(row.ram_gb_seconds or 0.0)
            io  = float(row.io_mb or 0.0)
            ratios = {
                "gpu": gpu / credits,
                "cpu": cpu / credits,
                "ram": ram / credits,
                "io":  io / credits,
            }
            worst = max(
                ratios["gpu"] / thresholds["gpu"],
                ratios["cpu"] / thresholds["cpu"],
                ratios["ram"] / thresholds["ram"],
                ratios["io"]  / thresholds["io"],
            )
            # "Efficient" → no per-resource ratio exceeds half its threshold.
            if worst < 0.5 and (gpu + cpu + ram + io) > 0:
                power_users.append({
                    "tenant_id": row.tenant_id,
                    "user_id": row.user_id,
                    "gpu_seconds": gpu,
                    "cpu_core_seconds": cpu,
                    "ram_gb_seconds": ram,
                    "io_mb": io,
                    "credits_used": credits,
                    "gpu_seconds_per_credit": ratios["gpu"],
                    "cpu_core_seconds_per_credit": ratios["cpu"],
                    "ram_gb_seconds_per_credit": ratios["ram"],
                    "io_mb_per_credit": ratios["io"],
                    "thresholds": thresholds,
                    "resources_above_threshold": [],
                    "_score": worst,
                })
        power_users.sort(key=lambda x: x["_score"])
        for p in power_users:
            p.pop("_score", None)
        power_users = power_users[:5]

        if not offenders and not power_users:
            logger.info(
                "resource_monitor digest: skipped — no offenders and no power users"
            )
            return 0

        admins = db.query(models.User).filter(
            models.User.is_platform_admin.is_(True),
            models.User.status == models.UserStatus.ACTIVE,
        ).all()
        if not admins:
            logger.warning("resource_monitor digest: no active platform admins")
            return 0

        sent = 0
        for admin in admins:
            try:
                extra = {
                    "EMAIL_TYPE": "resource_monitor_digest",
                    "OFFENDERS_JSON": json.dumps(offenders),
                    "POWER_USERS_JSON": json.dumps(power_users),
                }
                env = _get_email_env(
                    db, admin.tenant_id, admin.email,
                    f"{admin.first_name or ''} {admin.last_name or ''}".strip() or admin.email,
                    extra,
                )
                if not env:
                    logger.warning(
                        "resource_monitor digest: no email env for admin %s (tenant %s)",
                        mask_email(admin.email), admin.tenant_id,
                    )
                    continue
                email_config = crud.get_decrypted_email_config(db, tenant_id=admin.tenant_id)
                provider = (email_config or {}).get("provider", "brevo")
                _dispatch_email_container(db, admin.tenant_id, env, provider)
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "resource_monitor_digest_send_failed: admin=%s err=%s",
                    mask_email(admin.email), e,
                )
        return sent
    except Exception as e:  # noqa: BLE001
        logger.exception("resource_monitor digest failed: %s", e)
        return 0

