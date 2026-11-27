"""App CRUD operations - GX.A3 app-row lookups for the apps router.

Backs CORE/API/routers/apps.py (api_lint R5: routers must not hold raw
`db.query(` sites). Pure read helpers: no provisioning, no commits - the
router owns the seams and their commit boundaries.
"""
from typing import Optional

from sqlalchemy.orm import Session

import models


def get_revived_app(
    db: Session,
    *,
    tenant_id: int,
    slug: str,
) -> Optional[models.App]:
    """F16 (EARLY.APPS lifecycle): the SOFT-deleted app row for (tenant, slug).

    uq_apps_tenant_slug (models.py) covers soft-deleted rows, so a re-install
    of a removed slug must REVIVE the revoked row in place instead of INSERT-
    ing a duplicate (which would raise IntegrityError -> unhandled 500).
    Returns the row whose deleted_at is set, or None. Read-only: the caller
    decides the revive-vs-create branch and owns the commit.
    """
    return (
        db.query(models.App)
        .filter(models.App.tenant_id == tenant_id, models.App.slug == slug,
                models.App.deleted_at.isnot(None))
        .first()
    )
