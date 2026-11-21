"""Seed platform system-op workflows — dogfood-billing Phase 1.

Creates the is_system Workflow + SCHEDULE WorkflowTrigger that drive
platform-internal ops as MOD workflows ("workflow drives, API executes"). The
trigger config names a whitelisted action in services.system_ops; when it fires,
scheduler._run_system_op_trigger runs the handler IN-API and records a
WorkflowRun for history/observability.

Phase 1 seeds only weekly overage billing. The weekly APScheduler cron stays in
place as a fallback — both are idempotent (delta off the interim-invoice ledger),
so they can't double-charge.

Idempotent: skips a spec whose trigger (matched by internal_action) already
exists. Safe to re-run.

    docker exec my_fastapi_app python3 scripts/seed_platform_system_workflows.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: E402

# Specs: (workflow_name, cron, internal_action, description-for-log)
SPECS = [
    ("Platform: Weekly Overage Accounting", "0 3 * * 1",
     "process_weekly_overage_billing",
     "Mondays 03:00 UTC — collect accrued usage notes for the period"),
]


def _pick_owner(db):
    """A platform admin to own the system workflow (created_by + tenant)."""
    admin = (db.query(models.User)
             .filter(models.User.is_platform_admin == True)  # noqa: E712
             .order_by(models.User.id.asc()).first())
    if not admin:
        raise SystemExit("No platform-admin user found to own system workflows.")
    return admin


def seed(db) -> int:
    from utils.paid_import import optional_paid
    system_ops = optional_paid("services.system_ops")
    if system_ops is None:
        print("SKIPPED (module not in this edition)")
        return 0
    owner = _pick_owner(db)
    created = 0
    for name, cron, action, desc in SPECS:
        if not system_ops.is_registered(action):
            print(f"  SKIP '{action}' — not registered in services.system_ops")
            continue
        existing = (db.query(models.WorkflowTrigger)
                    .filter(models.WorkflowTrigger.trigger_type == models.WorkflowTriggerType.SCHEDULE,
                            models.WorkflowTrigger.deleted_at.is_(None))
                    .all())
        if any((t.configuration or {}).get("internal_action") == action for t in existing):
            print(f"  OK   '{action}' — trigger already exists, skipping")
            continue

        wf = models.Workflow(
            name=name, is_system=True,
            tenant_id=owner.tenant_id, created_by_user_id=owner.id)
        db.add(wf); db.flush()

        trig = models.WorkflowTrigger(
            workflow_id=wf.id, tenant_id=owner.tenant_id,
            created_by_user_id=owner.id,
            trigger_type=models.WorkflowTriggerType.SCHEDULE,
            configuration={"cron": cron, "timezone": "UTC", "internal_action": action},
            is_active=True)
        db.add(trig); db.flush()
        print(f"  NEW  workflow #{wf.id} + trigger #{trig.id}: {name} [{cron}] -> {action}")
        print(f"       {desc}")
        created += 1

    db.commit()
    return created


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import database as _db
    _db.initialize()
    from database import SessionLocal
    db = SessionLocal()
    try:
        n = seed(db)
        print(f"Done. {n} new system workflow(s) seeded.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
