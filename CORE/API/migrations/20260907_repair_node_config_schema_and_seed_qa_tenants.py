"""Repair corrupt node ``configuration_schema`` + guarantee QA tenants have a
populated node library.

ROOT CAUSE (node-library / palette blocker)
-------------------------------------------
Four MOD system converter nodes shipped with ``configuration_schema`` stored as
the jsonb *string* ``"NONE"`` instead of an object/null:

    GLB to OBJ (Blender), Image To Alpha (OpenCV),
    MediaPipe Holistic Tracking, SLDPRT Converter

A per-tenant node-library copy propagated those rows into tenants 7 / 28 / 29.
The palette endpoint ``GET /node_definitions/`` returns
``List[schemas.NodeDefinition]``; FastAPI validates the WHOLE list, and a single
row whose ``configuration_schema`` is a string (neither dict nor null) raises
``ResponseValidationError`` -> HTTP 500 -> the Workflow Builder renders an EMPTY
palette for every user in that tenant. Tenant 1 (the platform owner) held clean
copies and its system twins are name-deduped out of view, so only that account's
palette worked -- exactly the reported "works for the owner only" symptom.

The response schema is also hardened (a ``field_validator`` coerces a non-dict
``configuration_schema`` to None) so one bad row can never blank the palette
again; this migration repairs the stored data so the values are correct too.

Part 1 -- REPAIR (global, idempotent)
    Normalise any jsonb-string ``configuration_schema`` (e.g. ``"NONE"``) to
    NULL, across system + every tenant. This is the fix that unblocks the
    palette 500.

Part 2 -- QA TEST-ENABLER (idempotent)
    Ensure tenants 28 and 29 each own a tenant-scoped copy of every user-facing
    MOD system node (``node_role`` != 'system' AND NOT ``system_only``),
    create-if-missing keyed on ``name``, with a clean ``configuration_schema``.
    A no-op where the copy already exists (both tenants already hold the full
    set on stage); populates a fresh QA database so qa-owner / qa-user have
    nodes to build with. Copies are tenant-owned, not marketplace-flagged, and
    record their origin in ``forked_from_node_definition_id``.

Idempotent + dry-run by default. Pass ``--commit`` to apply.

    python migrations/20260907_repair_node_config_schema_and_seed_qa_tenants.py --commit
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from sqlalchemy import text

# FX.96: secure logging (SecretRedactionFilter) instead of bare basicConfig.
# Module-level: put API/ on the path first so utils.log_config imports cleanly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.log_config import configure_logging
configure_logging()
log = logging.getLogger("repair_node_config_schema")

# Tenants that must have a populated node library for manual QA.
QA_TENANT_IDS = [28, 29]

# Columns never carried verbatim onto a fresh tenant copy -- either identity
# (id/tenant_id/user_id) or platform/marketplace state that must reset for a
# tenant-owned palette node. Everything else is copied 1:1 from the system row.
_COPY_OVERRIDES = {
    "tenant_id": None,          # set per-target below
    "user_id": "NULL",          # tenant-shared (not user-private)
    "is_in_marketplace": "FALSE",
    "price_cents": "NULL",
    "price_tier": "NULL",
    "review_requested": "FALSE",
    "deleted_at": "NULL",
    "deleted_by_user_id": "NULL",
    "forked_from_node_definition_id": "sys.id",
    # A jsonb-string configuration_schema is coerced to NULL on copy too, so a
    # fresh QA seed never re-introduces the corruption Part 1 just repaired.
    "configuration_schema": (
        "CASE WHEN jsonb_typeof(sys.configuration_schema) = 'string' "
        "THEN NULL ELSE sys.configuration_schema END"
    ),
    # input_schema / output_schema are REQUIRED dict fields on the response
    # model. Some system nodes (e.g. the "Trigger E2E Probe") carry a jsonb
    # NULL there; copying that verbatim would 500 the palette. Coerce to '{}'.
    "input_schema": (
        "CASE WHEN sys.input_schema IS NULL "
        "OR jsonb_typeof(sys.input_schema) = 'null' "
        "THEN '{}'::jsonb ELSE sys.input_schema END"
    ),
    "output_schema": (
        "CASE WHEN sys.output_schema IS NULL "
        "OR jsonb_typeof(sys.output_schema) = 'null' "
        "THEN '{}'::jsonb ELSE sys.output_schema END"
    ),
}


def _bootstrap():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    return _db.SessionLocal()


def _repair_config_schema(db, commit: bool) -> int:
    bad = db.execute(text(
        "SELECT count(*) FROM node_definitions "
        "WHERE jsonb_typeof(configuration_schema) = 'string'"
    )).scalar() or 0
    log.info("Part 1 (repair): %s row(s) with a jsonb-string configuration_schema", bad)
    if bad and commit:
        res = db.execute(text(
            "UPDATE node_definitions SET configuration_schema = NULL "
            "WHERE jsonb_typeof(configuration_schema) = 'string'"
        ))
        log.info("  repaired %s row(s) -> configuration_schema = NULL", res.rowcount)
    return bad


def _repair_required_schemas(db, commit: bool) -> int:
    # input_schema / output_schema are REQUIRED dict fields on the response
    # model; a SQL NULL or jsonb-null value 500s the whole palette the same way
    # a jsonb-string does. Normalise them to an empty object.
    cond = (
        "(input_schema IS NULL OR jsonb_typeof(input_schema) = 'null' "
        "OR output_schema IS NULL OR jsonb_typeof(output_schema) = 'null')"
    )
    bad = db.execute(text(
        f"SELECT count(*) FROM node_definitions WHERE {cond}"
    )).scalar() or 0
    log.info("Part 1b (repair): %s row(s) with a NULL/jsonb-null input/output_schema", bad)
    if bad and commit:
        res = db.execute(text(
            "UPDATE node_definitions SET "
            "input_schema = CASE WHEN input_schema IS NULL "
            "  OR jsonb_typeof(input_schema) = 'null' THEN '{}'::jsonb "
            "  ELSE input_schema END, "
            "output_schema = CASE WHEN output_schema IS NULL "
            "  OR jsonb_typeof(output_schema) = 'null' THEN '{}'::jsonb "
            "  ELSE output_schema END "
            f"WHERE {cond}"
        ))
        log.info("  repaired %s row(s) -> input/output_schema = '{}'", res.rowcount)
    return bad


def _copyable_columns(db) -> list[str]:
    rows = db.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'node_definitions' AND column_name <> 'id' "
        "ORDER BY ordinal_position"
    )).fetchall()
    return [r[0] for r in rows]


def _seed_qa_tenant(db, cols: list[str], tenant_id: int, commit: bool) -> int:
    # Build a column-complete INSERT ... SELECT from the user-facing system
    # nodes, applying per-column overrides. Robust to schema drift because the
    # column list is discovered at runtime.
    select_exprs = []
    for c in cols:
        if c == "tenant_id":
            select_exprs.append(str(tenant_id))
        elif c in _COPY_OVERRIDES:
            select_exprs.append(_COPY_OVERRIDES[c])
        else:
            select_exprs.append(f"sys.{c}")

    where = (
        "sys.tenant_id IS NULL AND sys.deleted_at IS NULL "
        "AND (sys.system_only IS NOT TRUE) "
        "AND COALESCE(sys.node_role, 'user') <> 'system' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM node_definitions t "
        "  WHERE t.tenant_id = :tid AND t.user_id IS NULL "
        "        AND t.name = sys.name AND t.deleted_at IS NULL"
        ")"
    )

    missing = db.execute(
        text(f"SELECT count(*) FROM node_definitions sys WHERE {where}"),
        {"tid": tenant_id},
    ).scalar() or 0
    log.info("Part 2 (enabler): tenant %s missing %s user-facing system node(s)",
             tenant_id, missing)
    if missing and commit:
        insert_sql = (
            f"INSERT INTO node_definitions ({', '.join(cols)}) "
            f"SELECT {', '.join(select_exprs)} "
            f"FROM node_definitions sys WHERE {where}"
        )
        res = db.execute(text(insert_sql), {"tid": tenant_id})
        log.info("  inserted %s tenant-owned node(s) into tenant %s",
                 res.rowcount, tenant_id)
    return missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply changes")
    args = ap.parse_args()
    commit = bool(args.commit)

    db = _bootstrap()
    try:
        _repair_config_schema(db, commit)
        _repair_required_schemas(db, commit)
        cols = _copyable_columns(db)
        for tid in QA_TENANT_IDS:
            _seed_qa_tenant(db, cols, tid, commit)

        if commit:
            db.commit()
            log.info("COMMITTED.")
        else:
            db.rollback()
            log.info("(dry run) no changes written -- re-run with --commit to apply.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
