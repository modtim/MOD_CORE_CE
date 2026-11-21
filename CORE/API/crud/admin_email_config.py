# API/crud/admin_email_config.py
"""Data access for the platform email-config domain
(routers/admin_email_config.py).

The ONLY place `db.query(models.TenantEmailConfig)` runs for this domain
(Gate 2b migration). `TenantEmailConfig` has no `deleted_at` column and isn't
in database.py's soft-delete listener -- filters ported 1:1 from the
pre-migration inline query (see the shared Gate-2b migration rules on models
without soft-delete).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_platform_email_config_row(db: Session, tenant_id: int) -> Optional[models.TenantEmailConfig]:
    """Fetch the platform tenant's `TenantEmailConfig` row, or None."""
    return db.query(models.TenantEmailConfig).filter(
        models.TenantEmailConfig.tenant_id == tenant_id
    ).first()


def create_platform_email_config_row(
    db: Session, tenant_id: int, *, provider: str = "custom"
) -> models.TenantEmailConfig:
    """Insert the platform tenant's `TenantEmailConfig` row. Postmark talks
    SMTP, so `provider` stays 'custom' kind (ported verbatim).

    1796 — `tenant_email_configs.tenant_id` is `unique=True`, so the boot
    workers that run `_startup_verify_email_sync` concurrently can both see
    "no row" and both INSERT: the loser used to raise a
    `tenant_email_configs_tenant_id_key` UniqueViolation. The insert is now
    wrapped in a Postgres advisory transaction lock (same pattern as
    `_startup_seed_ce_admin_user` in main.py) so the check and the INSERT
    serialize, and the commit is additionally guarded against `IntegrityError`
    — a loser rolls back and returns the winner's row.
    """
    # Stable identifier for the advisory lock. Any unique int works; the value
    # is just a key into a Postgres-wide namespace (main.py's boot seeds use
    # 0x7a8b5c00deadbee0..bee3; this domain keeps its own slot).
    ADVISORY_LOCK_ID = 0x7a8b5c00deadbee4  # noqa: E501

    from sqlalchemy import text as _sa_text
    from sqlalchemy.exc import IntegrityError

    db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
               {"k": ADVISORY_LOCK_ID})

    row = get_platform_email_config_row(db, tenant_id)
    if row is not None:
        return row

    row = models.TenantEmailConfig(tenant_id=tenant_id, provider=provider)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Another boot worker won the INSERT — roll back and hand back its row.
        db.rollback()
        winner = get_platform_email_config_row(db, tenant_id)
        if winner is not None:
            return winner
        raise
    db.refresh(row)
    return row
