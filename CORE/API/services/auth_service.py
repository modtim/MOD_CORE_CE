"""Service layer for `routers/auth.py` (Gate 2b migration).

AUTH IS THE MOST SECURITY-SENSITIVE FILE in this migration wave. Per the
task's explicit override of the usual router/service/crud split: every
token/credential/session-authentication check, the JIT-provisioning
403 flows (`_run_jit_if_needed`), and the SSO-password-bypass audit
(`_maybe_audit_password_bypass`) are LEFT IN PLACE in `routers/auth.py`,
byte-for-byte, rather than relocated here. None of that is a `db.query` —
it is JWT-claims inspection, calls into `keycloak_auth`/`sso.*`, and
`HTTPException` raises — so there is nothing in it for this migration to
move.

The ONE thing this module owns is the single inline `db.query` the router
had: a `UserSession` lookup used for pure multi-session BOOKKEEPING (last-
active timestamp / IP refresh) after a successful token exchange. This is
not a security check — it never gates access, never raises, and runs only
after the JIT/audit block above it has already decided the login is
allowed. Moving it here carries none of the "collapses a 403 into a 404" or
"changes who gets in" risk the auth-logic proper does, so it is the one
piece the task's fallback ("relocate ONLY the db.query read and leave the
surrounding auth logic in the handler") did not require staying in the
router — but it is kept intentionally tiny and literal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models
from crud import auth as crud


def record_user_session_activity(
    db: Session,
    *,
    user_id: int,
    session_state: str,
    ip_address: Optional[str],
    device_info: str,
) -> None:
    """Update-or-create the caller's `UserSession` bookkeeping row for this
    login. Mirrors the original inline logic in
    `exchange_authorization_code` exactly, including that an empty/falsy
    `session_state` always creates a fresh row rather than looking one up
    (Keycloak sessions without a `session_state` claim). Does not commit —
    the caller (router) owns the transaction boundary, same as before.
    """
    existing = (
        crud.get_user_session_by_keycloak_session_id(db, session_state)
        if session_state
        else None
    )
    if existing:
        existing.last_active_at = datetime.now(timezone.utc)
        existing.ip_address = ip_address
    else:
        db.add(
            models.UserSession(
                user_id=user_id,
                keycloak_session_id=session_state,
                device_info=device_info,
                ip_address=ip_address,
            )
        )
