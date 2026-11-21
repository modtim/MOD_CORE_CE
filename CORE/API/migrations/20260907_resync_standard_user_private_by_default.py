"""Re-sync EXISTING tenants' system "Standard User" role to private-by-default.

Background
----------
The private-by-default fix updated ``SYSTEM_ROLE_SPECS`` (crud/tenant_cud.py) so
the seeded "Standard User" role no longer carries tenant-WIDE per-resource
capabilities. But ``ensure_tenant_system_roles`` only backfills EMPTY roles and
never rewrites a populated one (to preserve per-tenant customisation). So the
code change fixes FUTURE tenant provisioning only — EXISTING tenants' live
"Standard User" role rows still carry the leaked caps, and ``authorize()`` reads
the role's live DB capabilities, so a standard user still reaches un-shared
workflows/cogs across the tenant (t28 proven: qa-user id42 viewed qa-owner's
un-shared workflow via role id83).

This migration removes the leaked, per-resource capabilities from every EXISTING
tenant's system "Standard User" role so ``authorize()`` stops leaking
immediately (no API restart needed — it reads the DB live).

Leaked capabilities removed (per-resource; must come from an explicit grant or
ownership instead):
    COG:view, COG:share, COG:download_original
    WORKFLOW:view, WORKFLOW:edit, WORKFLOW:execute, WORKFLOW:view_runs

STRICTLY SCOPED and idempotent:
  - Only rows with name == "Standard User" AND is_system_role IS TRUE AND
    deleted_at IS NULL are touched (the exact identity the specs seed).
  - Admin / Guest / External Collaborator / owner roles are NOT touched.
  - Custom / user-created roles (is_system_role False, or any other name) are
    NOT touched.
  - Per-resource ResourcePermission grants are NOT touched.
  - Only the listed leaked caps are detached; any OTHER cap on the role
    (e.g. WORKFLOW:create, SECRET:*, COG:upload, tenant customisations) is left
    intact. Removing an already-absent cap is a no-op, so re-running is safe.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260907_resync_standard_user_private_by_default.py --commit

Dry run (default) prints what would change without writing.
"""
from __future__ import annotations

import argparse
import os
import sys
import logging


# The per-resource capabilities that leaked as tenant-wide role-own-capabilities.
# (resource_type_name, capability_name). WORKFLOW:create is intentionally absent
# (genuinely tenant-global — needs no existing resource), as are SECRET:* / USER:*
# / TEAM:* and COG:upload.
LEAKED_CAPS = frozenset({
    ("COG", "view"),
    ("COG", "share"),
    ("COG", "download_original"),
    ("WORKFLOW", "view"),
    ("WORKFLOW", "edit"),
    ("WORKFLOW", "execute"),
    ("WORKFLOW", "view_runs"),
})

STANDARD_USER_ROLE_NAME = "Standard User"


def _bootstrap():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    import models  # type: ignore
    return _db, models


def _cap_key(cap):
    # cap.resource_type is a ResourceType enum; use its .name to match LEAKED_CAPS.
    rt = getattr(cap.resource_type, "name", str(cap.resource_type))
    return (rt, cap.name)


def resync_standard_user_roles(db, models, log=None):
    """Detach the leaked per-resource caps from every system 'Standard User' role.

    Operates on the passed session; the caller owns commit/rollback. Returns a
    summary dict. Idempotent — a role with no leaked caps is left untouched.
    STRICTLY scoped to name == 'Standard User' AND is_system_role IS TRUE AND
    not soft-deleted; no other role (Admin/Guest/External/custom) is inspected,
    and no ResourcePermission grant is touched.
    """
    if log is None:
        log = logging.getLogger("resync_standard_user")

    roles = db.query(models.CustomRole).filter(
        models.CustomRole.name == STANDARD_USER_ROLE_NAME,
        models.CustomRole.is_system_role.is_(True),
        models.CustomRole.deleted_at.is_(None),
    ).all()
    log.info("found %d system 'Standard User' role(s) across tenants", len(roles))

    roles_changed = 0
    caps_removed = 0
    for role in roles:
        to_remove = [c for c in role.capabilities if _cap_key(c) in LEAKED_CAPS]
        if not to_remove:
            log.info("tenant %s role id=%s: already private-by-default (no leaked caps)",
                     role.tenant_id, role.id)
            continue
        removed_keys = sorted(_cap_key(c) for c in to_remove)
        for c in to_remove:
            role.capabilities.remove(c)
        roles_changed += 1
        caps_removed += len(to_remove)
        log.info("tenant %s role id=%s: removing %d leaked cap(s): %s",
                 role.tenant_id, role.id, len(to_remove), removed_keys)

    return {
        "roles_inspected": len(roles),
        "roles_changed": roles_changed,
        "caps_removed": caps_removed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="Apply changes. Without this flag the script does a dry run.")
    args = parser.parse_args()

    # FX.96: secure logging (SecretRedactionFilter) instead of bare basicConfig.
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from utils.log_config import configure_logging
    configure_logging()
    log = logging.getLogger("resync_standard_user")

    _db, models = _bootstrap()
    db = _db.SessionLocal()

    try:
        summary = resync_standard_user_roles(db, models, log)

        log.info("---")
        log.info("Summary:")
        log.info("  Standard User system roles inspected: %d", summary["roles_inspected"])
        log.info("  roles modified:                       %d", summary["roles_changed"])
        log.info("  leaked capability links removed:      %d", summary["caps_removed"])

        if args.commit:
            db.commit()
            log.info("Changes committed.")
        else:
            db.rollback()
            log.info("Dry run — no changes written. Re-run with --commit to apply.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
