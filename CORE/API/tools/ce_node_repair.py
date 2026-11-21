#!/usr/bin/env python3
"""ce-node-repair — the ONE-SHOT CE existing-DB node-row repair.

`872a2028d` (2026-10-25) fixed the CE boot seed for FRESH DBs only: the scan
scopes keep-listed nodes to the platform tenant (`node_scanner.py:1098`
`node_tenant_id = default_tenant_id()`), and the CE stamp is written ONLY on
create (`node_scanner.py:1165 "ce_included": True if is_ce_keeplisted else
None`). Create-vs-update is a name lookup scoped to that tenant
(`node_authoring_service.py:1642-1647`), and `:1658`
`update_payload.pop('ce_included', None)` means a
re-scan never stamps an existing row.

An install that booted BEFORE 872a2028d (Tim's box: cr13 rc18 -> rc19) still
holds the old `tenant_id IS NULL` rows for the 7 `*-input` seeders. Nothing
removes them (ISO), so the rc19 boot scan creates a tenant-1 copy BESIDE the
stale NULL-tenant row, and the stale row's `ce_included` stays NULL.

Symptom fixed (the brief's UNKNOWN, reproduced on a synthetic DB state in the
test): BOTH twins are reachable. The palette dedup
(`crud/node_definition_cud.py:174`) hides the less-specific twin, but the
stale NULL row still answers system-scope lookups
(`node_authoring_service.py:1643`), admin/system listing
(`routers/node_definitions_domains/node_authoring.py:846
admin_list_system_nodes`), and the digest/pinning sweep, which deliberately
includes `tenant_id IS NULL` rows (`node_scanner.py:564-566`) — so a pinned
workflow resolves the stale, unstamped twin (stale-digest pinning) while the
palette shows a duplicate system row. This repair stamps the tenant copy and
MARKS the stale row so neither lookup can pick it up again.

ISO: NO hard removal of product data or audit rows. Repair = UPDATE / MARK
only; the module issues no row-removal statement of any kind (see the W4 test,
which greps this file for the three removal spellings).
Idempotent: a second run finds nothing to write and prints 0. Fresh rc19
install: no stale NULL rows exist -> no-op, exit 0.
CE-gated: the keep-list (`services/ce_inclusion.py:66 keep_list_names()` ->
`node_scanner._load_ce_keeplist_names()`); an internal DB is never touched.
"""
from __future__ import annotations

import os
import sys

# Same sys.path shape as tools/ce_migrate.py:24 (API_DIR) + :54
# (sys.path.insert(API_DIR) then `import database`), so this file works both as
# `python3 tools/ce_node_repair.py` inside the api image (WORKDIR /app) and as
# a file-loaded module in the API test suite.
API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MARK_KEY = "ce_superseded_by"


def _plan(rows, keep_names, platform_tenant_id):
    """Pure planner over an in-memory row set — the exact logic the live pass
    issues, so the test can drive it without a DB (no SessionLocal, no live DB).

    `rows` is an iterable of dicts with `name`, `tenant_id`, `ce_included`,
    `custom_metadata` and `id`. Returns a list of UPDATE plans:
      (a) stamp the platform-tenant copy  -> {"stamp": <id>}
      (b) mark the stale NULL-tenant twin -> {"mark": <id>, "by": <copy_id>}
    A name with no NULL-tenant twin, or no tenant copy, yields nothing.
    """
    by_name = {}
    for r in rows:
        by_name.setdefault(str(r["name"]), {"system": [], "copy": []})
        if r.get("tenant_id") is None:
            by_name[str(r["name"])]["system"].append(r)
        elif r.get("tenant_id") == platform_tenant_id:
            by_name[str(r["name"])]["copy"].append(r)

    plans = []
    for name in sorted(by_name):
        if name not in keep_names:
            continue
        system_rows = by_name[name]["system"]
        copy_rows = by_name[name]["copy"]
        if not system_rows or not copy_rows:
            continue
        for copy in copy_rows:
            if copy.get("ce_included") is not True:
                plans.append({"stamp": copy["id"], "name": name})
        for stale in system_rows:
            marked = (stale.get("custom_metadata") or {}).get(MARK_KEY)
            if stale.get("ce_included") is not False or marked is None:
                plans.append(
                    {"mark": stale["id"], "by": copy_rows[0]["id"], "name": name}
                )
    return plans


def main(argv=None) -> int:
    """Scan node_definitions, stamp + mark, print the count only. 0 = nothing to do."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    import database  # type: ignore
    from models import NodeDefinition
    from services.node_scanner import (
        _is_ce_edition,
        default_tenant_id,
        _load_ce_keeplist_names,
    )

    if not _is_ce_edition():
        print("ce-node-repair: not available in this edition", flush=True)
        return 0
    keep_names = _load_ce_keeplist_names()
    if keep_names is None:
        # Fails-open on a missing keep-list: the scan seeded everything, so a
        # repair with no CE boundary would be edition-unsafe. No-op loudly.
        print("ce-node-repair: keep-list unavailable, no-op", flush=True)
        return 0

    platform_tenant_id = default_tenant_id()
    # NON-FATAL: api boots behind `service_completed_successfully`, so a broken
    # repair must never block it. Any failure -> one masked line, rollback, 0.
    session = None
    try:
        session = database.SessionLocal()
        fetched = session.execute(
            _select_candidates(NodeDefinition, keep_names, platform_tenant_id)
        ).mappings().all()
        rows = [dict(r) for r in fetched]
        plans = _plan(rows, keep_names, platform_tenant_id)
        by_id = {r["id"]: r for r in rows}
        for p in plans:
            if "stamp" in p:
                session.query(NodeDefinition).filter(
                    NodeDefinition.id == p["stamp"]
                ).update({NodeDefinition.ce_included: True}, synchronize_session=False)
            elif "mark" in p:
                # New dict (never mutate the loaded JSON in place: untracked).
                merged = dict(by_id[p["mark"]].get("custom_metadata") or {})
                merged[MARK_KEY] = p["by"]
                session.query(NodeDefinition).filter(
                    NodeDefinition.id == p["mark"]
                ).update(
                    {
                        NodeDefinition.ce_included: False,
                        NodeDefinition.custom_metadata: merged,
                    },
                    synchronize_session=False,
                )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - deliberately non-fatal
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(f"ce-node-repair: skipped ({type(exc).__name__})", flush=True)
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"ce-node-repair: {len(plans)}", flush=True)
    return 0


def _select_candidates(model, keep_names, platform_tenant_id):
    """One SELECT over the keep-listed names in BOTH scopes (NULL tenant and
    the platform tenant). Read-only; the writes come from main()'s UPDATEs.
    SQLAlchemy 2.0 `select()` fed to `session.execute(...)`, the repo pattern
    in resource_permissions.py:256 / authz_review.py:142; the UPDATEs mirror
    crud/worker_management.py:420 (`.update({Model.col: v}, synchronize_session=False)`).
    NodeDefinition has no `query` property, so `model.query` is unavailable."""
    from sqlalchemy import select

    return (
        select(
            model.id,
            model.name,
            model.tenant_id,
            model.ce_included,
            model.custom_metadata,
        )
        .where(model.name.in_(sorted(keep_names)))
        .where((model.tenant_id.is_(None)) | (model.tenant_id == platform_tenant_id))
    )


# FIX-CE-NODE-REPAIR-MAIN-GUARD (S-compose-ce-node-repair, row 6 WRONG): without
# this guard the compose one-shot (`exec python3 tools/ce_node_repair.py`,
# open-core/overlay/docker-compose.yml:985-992) only DEFINES main() and exits 0
# - the api waits on it with service_completed_successfully, so upgrades
# (rc18 -> rc19) silently skipped the repair. House style (tools/ce_migrate.py:148,
# tools/seed_platform_settings.py:89): main() takes the CLI argv verbatim and
# returns the exit code.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
