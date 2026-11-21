"""GC.PFM.18 — backfill the calibration-fixture migration for existing tenants.

New tenants get `dispatch_migration` called automatically at provisioning
time (GC.PFM.9, `services/users_router_service.py:create_tenant`). Tenants
that existed before that hook landed never had a migration dispatched, so
their `fixture_status` is stuck at NULL (or something other than 'ready').
This script finds them and dispatches one migration each.

Eligibility (a tenant needs a migration dispatched):
    - active: `deleted_at IS NULL` (soft-deleted tenants are skipped — no
      point migrating a tenant nobody can use)
    - `fixture_status IS NULL OR fixture_status != 'ready'`

Idempotent: a tenant already `fixture_status == 'pending'` with an OPEN
(non-terminal) `fixture_migrator` task already in `ApiTaskQueue` is skipped
— re-running the script (or running it after GC.PFM.9 already dispatched
one) does not pile up duplicate tasks. A tenant that is 'pending' but has
no open task (e.g. the task went terminal without ever flipping the tenant
to ready/failed) is still eligible and gets a fresh dispatch.

`task_payload` is the custom JSONB TypeDecorator in `models.py` — filter it
with `.as_string()`, NEVER `.astext` (that path throws on this type; see
migrations/20260730_reset_falsely_calibrated_nodes.py for the established
pattern).

No deletes anywhere. Dry-run by default; pass --commit to actually dispatch.
Never run this against the live DB without operator sign-off.

    docker exec -w /app my_fastapi_app python \\
        migrations/20260925_backfill_tenant_fixtures.py
    docker exec -w /app my_fastapi_app python \\
        migrations/20260925_backfill_tenant_fixtures.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    return _db.SessionLocal()


def _open_fixture_migrator_task_tenant_ids(session, models) -> set:
    """Tenant ids with a non-terminal 'fixture_migrator' ApiTaskQueue row."""
    ApiTaskQueue = models.ApiTaskQueue
    ApiTaskStatus = models.ApiTaskStatus
    terminal = (
        ApiTaskStatus.COMPLETED,
        ApiTaskStatus.FAILED,
        ApiTaskStatus.EXPIRED,
        ApiTaskStatus.CANCELLED,
    )
    rows = session.query(ApiTaskQueue.tenant_id, ApiTaskQueue.status).filter(
        ApiTaskQueue.task_payload["node_kind"].as_string() == "fixture_migrator",
    ).all()
    return {tenant_id for (tenant_id, status) in rows if status not in terminal}


def _eligible_tenants(session, models):
    """Active tenants whose fixtures aren't 'ready', minus ones already
    pending with an open fixture_migrator task."""
    Tenant = models.Tenant

    open_task_tenant_ids = _open_fixture_migrator_task_tenant_ids(session, models)

    candidates = session.query(Tenant).filter(
        Tenant.deleted_at.is_(None),
        (Tenant.fixture_status.is_(None)) | (Tenant.fixture_status != "ready"),
    ).all()

    out = []
    for t in candidates:
        if t.fixture_status == "pending" and t.id in open_task_tenant_ids:
            continue  # already has a migration in flight — idempotent skip
        out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="dispatch the migrations (default is dry-run)")
    args = ap.parse_args()

    session = _get_session()
    import models  # noqa: F401
    from services import fixture_service

    try:
        eligible = _eligible_tenants(session, models)

        print(f"tenants needing a fixture migration: {len(eligible)}")
        for t in eligible[:20]:
            print(f"    id={t.id:<6} {t.name[:44]:<44} fixture_status={t.fixture_status!r}")
        if len(eligible) > 20:
            print(f"    ... and {len(eligible) - 20} more")

        if not eligible:
            print("nothing to do.")
            return 0

        if not args.commit:
            print(f"\n(dry-run) would dispatch {len(eligible)} migration(s). "
                  f"Re-run with --commit to apply.")
            return 0

        dispatched = 0
        for t in eligible:
            try:
                fixture_service.dispatch_migration(session, t.id)
                dispatched += 1
            except Exception as exc:  # never let one bad tenant abort the sweep
                print(f"    WARNING: dispatch failed for tenant {t.id}: {exc}",
                      file=sys.stderr)
        print(f"\ndispatched {dispatched}/{len(eligible)} migration(s).")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
