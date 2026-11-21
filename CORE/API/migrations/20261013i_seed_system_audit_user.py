#!/usr/bin/env python3
"""Seed a dedicated "mod-system" user row for system-originated audit rows.

Bug (960d / Q12=B, 10-12 FINAL): ``PermissionAuditLog.actor_user_id`` is
``NOT NULL`` with an FK to ``users.id``. Several system-emitted audit call
sites in ``audit.py`` (``log_security_event`` with no ``user_id``,
``log_content_share_link_accessed`` with no ``accessor_user_id``,
``log_resource_leak_detected``, ``log_harbor_scan_retriggered``) used the
literal ``0``, which has no matching ``users`` row -> FK violation.

Tim's Q12=B overrides the earlier "make the column nullable" proposal
(``/tmp/task_queue/taken/BUG-960d-audit-actor-user-id-0.md``): seed one
dedicated system user instead. ``models.py`` is NOT touched by this change.

Append-only / ISO 27001: this script only INSERTs, never UPDATEs/DELETEs.
Idempotent: if a user with username='mod-system' already exists, no-op.

Dry run (default, no --commit) prints the guard SELECT + INSERT SQL and
exits 0 without touching the database. The lead applies with --commit.

CE-MIGROLE-5b (10-29): the fresh-DB CE chain (`tools/ce_migrate.py --no-seeds`
on an empty postgres:13) died at step 360/376 here —
`ForeignKeyViolation users_tenant_id_fkey (tenant_id)=(1) not in tenants`.
On a CE DB the platform tenant 1 is created LATER (create_tenant.py:91 `id=1`,
run by the first-boot/install path), so this seed must not assume it exists.
Fix = Option A (tenant guard): if no tenant with id SYSTEM_TENANT_ID exists,
log and no-op (rc 0). The audit-side self-heal `audit._seed_system_user`
(1900Y2b, audit.py:121) then creates the mod-system actor on the first
system-emitted audit row once tenant 1 exists, so the row still lands.
NOT added to tools/ce_migrate_skip.txt: the step is not SaaS-history-only —
it seeds a row CE needs (the audit actor), which the skip-list rule
(ce_migrate_skip.txt header) forbids listing. DBs that DO have tenant 1 are
unchanged and still idempotent.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261013i_seed_system_audit_user.py            # dry run
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261013i_seed_system_audit_user.py --commit   # apply
"""
from __future__ import annotations

import argparse
import os
import sys

SYSTEM_USERNAME = "mod-system"
SYSTEM_EMAIL = "system@mod.internal"
SYSTEM_TENANT_ID = 1  # audit.PLATFORM_AUDIT_TENANT_ID

GUARD_SQL = """
SELECT id FROM users WHERE username = 'mod-system';
""".strip()

TENANT_GUARD_SQL = """
SELECT id FROM tenants WHERE id = 1;
""".strip()

INSERT_SQL = """
INSERT INTO users (
    username, email, first_name, last_name, tenant_id,
    keycloak_user_id, status, onboarding_complete
) VALUES (
    'mod-system', 'system@mod.internal', 'MOD', 'System', 1,
    NULL, 'active', true
);
""".strip()


def seed_system_user(db) -> dict:
    """Idempotent seed. Returns {"action": "skip"|"insert"|"no_tenant", "id": <int|None>}.

    CE-MIGROLE-5b: the tenant FK guard runs FIRST. On a fresh CE DB the platform
    tenant 1 does not exist yet (create_tenant.py:91 `id=1` creates it later in
    the first-boot path), so the INSERT FK-violates. No-op + rc 0 instead;
    audit._seed_system_user (audit.py:121, 1900Y2b) creates the actor once the
    tenant exists.
    """
    import models

    tenant = db.query(models.Tenant.id).filter(
        models.Tenant.id == SYSTEM_TENANT_ID
    ).first()
    if not tenant:
        return {"action": "no_tenant", "id": None}

    existing = db.query(models.User.id).filter(
        models.User.username == SYSTEM_USERNAME
    ).first()
    if existing:
        return {"action": "skip", "id": existing[0]}

    user = models.User(
        username=SYSTEM_USERNAME,
        email=SYSTEM_EMAIL,
        first_name="MOD",
        last_name="System",
        tenant_id=SYSTEM_TENANT_ID,
        keycloak_user_id=None,
        status=models.UserStatus.ACTIVE,
        onboarding_complete=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"action": "insert", "id": user.id}


def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    return _db.SessionLocal()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the seed insert. Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        print("DRY  would check for platform tenant 1 (CE-MIGROLE-5b guard):")
        print(TENANT_GUARD_SQL)
        print("DRY  if absent -> no-op (rc 0); audit._seed_system_user self-heals later")
        print("DRY  would check for existing 'mod-system' user:")
        print(GUARD_SQL)
        print("DRY  if not present, would insert:")
        print(INSERT_SQL)
        print("DRY  append-only: existing row -> no-op (skip); "
              "no row -> single INSERT (no UPDATE/DELETE)")
        print("(dry-run; pass --commit to apply)")
        return 0

    db = _get_session()
    try:
        result = seed_system_user(db)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"FAIL seed: {exc}")
        return 1
    finally:
        db.close()

    if result["action"] == "skip":
        print(f"already seeded: mod-system user id={result['id']}")
    elif result["action"] == "no_tenant":
        print("no-op  platform tenant 1 absent (fresh CE DB) — "
              "audit._seed_system_user creates the actor once the tenant exists")
    else:
        print(f"insert  mod-system user id={result['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
