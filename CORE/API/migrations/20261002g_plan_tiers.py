"""GT.D12 (launch-completion-megaplan) — express SaaS plans as tiers.

Live state on 10-01: the two live standard plans `Free` and `Platform`
both have `plans.tier IS NULL`, so `utils.entitlements.entitlements_from_plan`
resolves each of them to the five legacy boolean columns only (sso/scim/
custom_registry/custom_nodes/show_timing). This migration stamps the canonical
commercial tier onto each so SaaS plans express the same Cap.* vocabulary an
on-prem license at that tier does:

    Free     -> tier='community'    (the CE bundle — the SaaS floor)
    Platform -> the tier above this edition's floor (that tier's caps stay out of this edition)

Additive + idempotent:
  * UPDATE ... WHERE tier IS NULL only — a plan already stamped by a prior run
    (or set deliberately by an admin) is never re-touched, so a second run is a
    no-op.
  * No deletes, no other column writes.
  * Plans matched by exact name, and the match is REFUSED when ambiguous: if a
    target name matches more than one non-deleted plan row, the migration
    refuses to run rather than guess. `Free` and `Platform` are unique
    standard-plan names in this install.

Audited: every row the migration touches is recorded in the ISO
`plan_tier_assignments` ledger (append-only, BEFORE UPDATE OR DELETE guard) —
plan_id, plan name, old->new tier, applied_by, applied_at.

Dry run (default, no --commit) PRINTS the per-plan capability delta — the caps
gained over the plan's current legacy-boolean resolution, computed through the
REAL `utils.entitlements` resolver — plus the background behaviours those caps
switch on (burst, predictive warm/sleep, mesh federation, marketplace publish,
GPU pool, managed build farm). It then exits 0 without touching the database.
The lead reviews the delta and applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261002g_plan_tiers.py            # dry run: prints delta
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261002g_plan_tiers.py --commit   # apply

LEAD APPLIES — not applied by the task agent.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

# name -> canonical tier (utils.entitlements.TIER_*). Only these two live
# standard plans are stamped; everything else is left alone.
_TIER_ASSIGNMENTS = (
    ("Free", "community"),
    ("Platform", "business"),
)

# Background behaviours these caps switch on — the exact call sites that gate
# on the capability via evaluate_feature (grep'd from the tree).
_BACKGROUND_BEHAVIOURS = (
    ("burst (burst-to-saas)", "burst", "routers/burst_connect.py:58"),
    ("predictive warm/sleep (pre-warm + characterization)", "predictive",
     "services/stack_prewarm.py:65, services/stack_wake.py:116, services/stack_calibration.py:78"),
    ("mesh federation / wizard authoring", "stack_wizard",
     "routers/managers.py:396, services/persistent_stack_definition_service.py"),
    ("marketplace publish (seller)", "marketplace_publish",
     "routers/node_definitions_domains/_shared.py:1079"),
    ("GPU-pool management", "gpu_pool", "routers/admin_gpu_pool.py:76"),
    ("managed build farm (managed_ai)", "managed_ai", "managed build-farm dispatch"),
)

# The columns the entitlements resolver reads off the plan.
_PLAN_COLUMNS = (
    "id", "name", "tier", "sso_enabled", "scim_enabled",
    "allow_custom_registry", "allow_custom_nodes", "show_timing",
    "max_seats", "max_storage_gb", "credits_per_month",
    "max_concurrent_processes", "max_cpu_hours_monthly", "included_egress_gb",
)

_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS plan_tier_assignments (
    id           SERIAL PRIMARY KEY,
    plan_id      INTEGER NOT NULL,
    plan_name    VARCHAR(100) NOT NULL,
    old_tier     VARCHAR(20),
    new_tier     VARCHAR(20) NOT NULL,
    applied_by   VARCHAR(255) NOT NULL,
    applied_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION plan_tier_assignments_no_mutate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only table % (ISO no-hard-delete)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_no_update_plan_tier_assignments ON plan_tier_assignments;
CREATE TRIGGER trg_no_update_plan_tier_assignments
    BEFORE UPDATE ON plan_tier_assignments
    FOR EACH ROW EXECUTE FUNCTION plan_tier_assignments_no_mutate();
DROP TRIGGER IF EXISTS trg_no_delete_plan_tier_assignments ON plan_tier_assignments;
CREATE TRIGGER trg_no_delete_plan_tier_assignments
    BEFORE DELETE ON plan_tier_assignments
    FOR EACH ROW EXECUTE FUNCTION plan_tier_assignments_no_mutate();
"""


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _fetch_plans(conn):
    """Return {plan_name: duck-typed plan row} for every non-deleted plan,
    built from the live columns so the REAL resolver can read it."""
    import models  # type: ignore

    cur = conn.cursor()
    cur.execute(
        f"SELECT {', '.join(_PLAN_COLUMNS)} FROM plans WHERE deleted_at IS NULL"
    )
    cols = [c[0] for c in cur.description]
    plans = {}
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        plan = models.Plan()
        for col in cols:
            setattr(plan, col, r[col])
        plans[r["name"]] = plan
    return plans


def _print_delta(plans):
    from utils import entitlements as ent  # type: ignore

    print("=" * 78)
    print("PLAN TIER ASSIGNMENT — DRY-RUN CAPABILITY DELTA")
    print("(resolved through utils.entitlements.entitlements_from_plan)")
    print("=" * 78)
    for name, tier in _TIER_ASSIGNMENTS:
        plan = plans.get(name)
        if plan is None:
            print(f"\n[SKIP] plan {name!r}: not found (already deleted?) — nothing to stamp.")
            continue
        cur_es = ent.entitlements_from_plan(plan)
        # Same row, tier stamped — the resolver is pure over duck-typed attrs.
        setattr(plan, "tier", tier)
        try:
            new_es = ent.entitlements_from_plan(plan)
        finally:
            setattr(plan, "tier", None)
        gained = sorted(new_es.features - cur_es.features)
        lost = sorted(cur_es.features - new_es.features)
        print(f"\n### plan {name!r} (id={plan.id})  tier NULL -> {tier!r}")
        print(f"  caps GAINED ({len(gained)}):")
        if gained:
            for c in gained:
                print(f"    + {c}")
        else:
            print("    (none — already grants this tier's full bundle)")
        if lost:
            print(f"  caps LOST ({len(lost)}): {lost}  <-- REVIEW")
        print("  background behaviours switched on by the gained caps:")
        switched = [(label, cap, site)
                    for (label, cap, site) in _BACKGROUND_BEHAVIOURS if cap in gained]
        if switched:
            for label, cap, site in switched:
                print(f"    * {label}  [cap={cap}]  @ {site}")
        else:
            print("    (none)")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="GT.D12 plan tier assignment")
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)

    try:
        # --- Ambiguity guard: refuse if a target name matches >1 row --------
        ok = True
        for name, _tier in _TIER_ASSIGNMENTS:
            cur.execute(
                "SELECT count(*) FROM plans WHERE name = %s AND deleted_at IS NULL",
                (name,),
            )
            n = cur.fetchone()[0]
            if n == 0:
                print(f"(plan {name!r}: not found — will skip)")
            elif n > 1:
                print(f"REFUSED: name {name!r} matches {n} non-deleted plans — "
                      "ambiguous; not running.")
                ok = False
            else:
                print(f"(plan {name!r}: unique match)")
        if not ok:
            return 2

        # --- Dry run: print the capability delta, no writes -----------------
        if not args.commit:
            _print_delta(_fetch_plans(conn))
            print("(dry-run; no writes. pass --commit to apply)")
            return 0

        # --- Commit: ensure audit ledger, then stamp each plan --------------
        cur.execute(_AUDIT_DDL)
        for name, tier in _TIER_ASSIGNMENTS:
            cur.execute(
                "UPDATE plans SET tier = %s "
                "WHERE name = %s AND tier IS NULL AND deleted_at IS NULL",
                (tier, name),
            )
            updated = cur.rowcount
            if updated:
                cur.execute(
                    "SELECT id FROM plans WHERE name = %s AND deleted_at IS NULL",
                    (name,),
                )
                plan_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO plan_tier_assignments "
                    "(plan_id, plan_name, old_tier, new_tier, applied_by) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (plan_id, name, None, tier,
                     f"migration 20261002g_plan_tiers@{socket.gethostname()}"),
                )
            print(f"WRITE  plans.tier {name!r}: NULL -> {tier!r} "
                  f"({updated} row(s) updated; 0 if already stamped)")
    finally:
        if set_role_ok:
            if args.commit:
                try:
                    cur.execute(
                        "ALTER TABLE plan_tier_assignments OWNER TO "
                        f"{owner}")
                except Exception as e:  # noqa: BLE001
                    print(f"(ALTER TABLE plan_tier_assignments OWNER TO "
                          f"{owner} failed: {e})")
            cur.execute("RESET ROLE")
        cur.close()

    print("plan tier assignments ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
