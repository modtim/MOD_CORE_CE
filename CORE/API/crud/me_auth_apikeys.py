# API/crud/me_auth_apikeys.py
"""Data access for the `/me` auth/session domain (routers/me/auth_apikeys.py).

The ONLY place `db.query(models.UserSession)` runs for this domain (Gate 2b
migration). `UserSession` carries no `tenant_id` of its own -- it is scoped by
`user_id`, and the caller is always the CURRENT user (from
`require_active_and_onboarded_user`), so there is no cross-tenant surface here
to additionally filter -- ported 1:1 from the pre-migration inline query.
"""
from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

import models


def list_sessions_for_user_newest_first(db: Session, user_id: int) -> List[models.UserSession]:
    """All of `user_id`'s sessions, most-recently-active first."""
    return (
        db.query(models.UserSession)
        .filter(models.UserSession.user_id == user_id)
        .order_by(models.UserSession.last_active_at.desc())
        .all()
    )


def delete_session(db: Session, session: models.UserSession, *, commit: bool = True) -> None:
    """Hard-delete one session row.

    `UserSession` has no `deleted_at` column and isn't in database.py's
    soft-delete listener -- a session is a live-login record, not durable
    business data, so a hard delete here is the pre-existing, unchanged
    behavior (not something this migration "fixes" -- see the shared Gate-2b
    migration rules on models without soft-delete).
    """
    # ISO-exception: transient coordination/session row; hard-delete required
    # (UserSession is an ephemeral live-login record, not durable business data;
    # revocation removes the session row — it has no deleted_at column and is not
    # in the soft-delete listener).
    db.delete(session)
    if commit:
        db.commit()
