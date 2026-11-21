"""FX.131 PART 1 (Q118=A) — customer-trained models: training record + audit.

Two additive, idempotent steps:

1. ``named_artifacts.training_record`` — nullable JSONB. On a trained model
   (type == "lora") records the dataset ids it was trained on and, when the
   character's likeness consent (Q84) was captured, a link to that consent
   record ({"dataset_ids": [...], "consent_record_id": N}). Pre-existing rows
   and rag-type artifacts stay NULL.

2. ``auditaction`` enum — adds ``TRAINED_MODEL_DOWNLOADED`` so a
   trained-model download gets its own audit action (metadata carries
   tenant/artifact id + name + object key, never weights content).

The enum ADD VALUE must run outside a transaction (caller connection is
autocommit), matching the FX.117 audit-action migration.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261005o_fx131_trained_models.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

FX131_AUDIT_VALUES = [
    "TRAINED_MODEL_DOWNLOADED",
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        print("ALTER TABLE named_artifacts "
              "ADD COLUMN IF NOT EXISTS training_record JSONB;")
        for v in FX131_AUDIT_VALUES:
            print(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}';")
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()

    if not _column_exists(cur, "named_artifacts", "training_record"):
        cur.execute(
            "ALTER TABLE named_artifacts "
            "ADD COLUMN IF NOT EXISTS training_record JSONB"
        )
        print("Added named_artifacts.training_record (JSONB, nullable).")
    else:
        print("named_artifacts.training_record already present.")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for v in FX131_AUDIT_VALUES:
        cur.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}'")
    if set_role_ok:
        cur.execute("RESET ROLE")
    print(f"Added {len(FX131_AUDIT_VALUES)} FX.131 audit values to "
          "auditaction enum.")

    cur.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
