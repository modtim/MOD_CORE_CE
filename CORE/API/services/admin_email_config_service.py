# API/services/admin_email_config_service.py
"""Business logic for the platform email-config domain
(routers/admin_email_config.py).

Companion to `crud/admin_email_config.py` (the ONLY place this domain's
`db.query(models.TenantEmailConfig)` runs).

Scope note (Gate 2b, api-refactor opsx): the Vault read/write helpers
(`_vault_token` / `_read_email_creds_from_vault` / `_write_email_creds_to_vault`)
and the Vault->Keycloak-realm-SMTP sync + audit-emit orchestration inside
`update_email_config` / `test_email_config` stay in the ROUTER, not here --
same precedent as `services/postmark_webhooks_service.py` keeping signature
verification in its router. Two reasons, both load-bearing:

  1. `tests/test_admin_email_config.py` patches them by module path
     (`patch("routers.admin_email_config._read_email_creds_from_vault", ...)`).
     That patch only takes effect if the code calling the function resolves
     it as a bare name through `routers.admin_email_config`'s own module
     globals at call time -- moving the call site into this module (a static
     `from routers.admin_email_config import ...` binds the reference at
     IMPORT time, before any per-test patch is applied) would silently stop
     honoring the patch and start hitting real Vault/SMTP in CI.
  2. `tests/test_phase3_comprehensive.py` and `tests/test_recent_fixes.py`
     assert `"update_realm_smtp" in inspect.getsource(update_email_config)`
     -- the literal Keycloak-sync call must remain textually inside the
     router's `update_email_config` function body, not delegated out.

What DOES move here: the DB fetch-or-create (via `crud.admin_email_config`),
the pure response-shaping (`serialize_config`, no DB/Vault I/O), and the
test-send audit emit (`emit_test_audit`, a plain `AuditEvent` insert with no
external call). None of these three symbols are patched or imported by any
test (grepped), so they're safe to relocate without touching a patch site.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy.orm import Session

import models
import schemas
from crud import admin_email_config as admin_email_config_crud
from utils.smtp_security import config_is_complete

logger = logging.getLogger(__name__)


def get_or_create_platform_email_row(db: Session, tenant_id: int) -> models.TenantEmailConfig:
    """Return the platform tenant's `TenantEmailConfig` row, creating it if missing."""
    row = admin_email_config_crud.get_platform_email_config_row(db, tenant_id)
    if not row:
        row = admin_email_config_crud.create_platform_email_config_row(
            db, tenant_id, provider="custom"
        )
    return row


def serialize_config(
    row: models.TenantEmailConfig, vault_data: dict
) -> "schemas.PlatformEmailConfigResponse":
    """Build the response. Never includes the password itself."""
    password_in_vault = bool(vault_data.get("smtp_password"))
    # Legacy fallback: if no Vault path is set, the password may still be
    # in the encrypted column (pre-migration state).
    password_in_legacy = bool(row.encrypted_smtp_password) and not row.password_vault_path
    password_set = password_in_vault or password_in_legacy

    # 'auto' (NULL) keeps the historical rule: host + user + password + sender.
    # An explicit Encryption choice also allows an unauthenticated server
    # (local relay / smtp-relay.gmail.com): host + sender only.
    security = getattr(row, "smtp_security", None)
    is_configured = config_is_complete(
        row.smtp_host, row.smtp_user, password_set, row.sender_email, security
    )

    return schemas.PlatformEmailConfigResponse(
        provider=row.provider,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port,
        smtp_user=row.smtp_user,
        sender_name=row.sender_name,
        sender_email=row.sender_email,
        smtp_security=security or "auto",
        smtp_allow_selfsigned=bool(getattr(row, "smtp_allow_selfsigned", False)),
        is_configured=is_configured,
        password_set=password_set,
        last_tested_at=row.last_tested_at,
        last_test_result=row.last_test_result,
        last_test_error=row.last_test_error,
    )


def emit_test_audit(
    db: Session,
    actor: models.User,
    recipient: str,
    result: "schemas.EmailTestResponse",
) -> None:
    """Audit emit for the test-send button. Never raises."""
    try:
        db.add(models.AuditEvent(
            tenant_id=actor.tenant_id,
            user_id=actor.id,
            event_name="ADMIN_EMAIL_TEST_SENT",
            details={
                "recipient": recipient,
                "success": result.success,
                "error": result.error,
                "provider_message_id": result.provider_message_id,
            },
        ))
        db.commit()
    except Exception:
        logger.warning("Audit emit for ADMIN_EMAIL_TEST_SENT failed", exc_info=True)
