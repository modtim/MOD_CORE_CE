"""§5 — per-variant sample-test verification table + reference_variant column.

Adds the schema the wizard-rebuild §5 chunk needs to record per-variant
sample-test scores against a user-selectable reference variant.

Schema:

  1. ``node_definitions.reference_variant`` — VARCHAR(64), default
     ``'hero'``. The variant whose sample-test output is the reference
     for drift comparison. User-pickable from the Node settings UI.

  2. ``node_variant_verification`` — one row per (Node, variant) pair.
     Identifies a built variant by either ``build_spec_id`` (during the
     workflow run, before register completes) or ``node_definition_id``
     (after register), whichever is known. The register step
     backfills ``node_definition_id`` on existing build-time rows.

     Columns:
       - ``id`` primary key
       - ``build_spec_id`` FK → build_specs(id), nullable
       - ``node_definition_id`` FK → node_definitions(id), nullable
       - ``variant`` VARCHAR(64) — hero / cuda / rocm / intel / npu / cpu
       - ``score`` REAL — similarity score in [0, 1] (1.0 = identical
         for hero; comparison-based for non-hero)
       - ``verdict`` VARCHAR(32) — one of: verified / drift / marginal /
         incompatible / failed / untested
       - ``scored_against_variant`` VARCHAR(64) — which variant was
         the reference at scoring time (preserved so a later reference
         swap can detect stale scores)
       - ``scored_at`` TIMESTAMPTZ
       - ``output_type_at_test`` VARCHAR(32) — image / text / video /
         embedding / 3d_mesh / audio / json / binary
       - ``raw_output_ref`` VARCHAR(512) — MinIO object key for the
         produced output, for rescoring on reference swap (§5c)

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260523_variant_verification.py --commit

Idempotent — repeated runs are safe.
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
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


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
         WHERE table_name = %s
        """,
        (table,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    # 1. node_definitions.reference_variant column
    if _column_exists(cur, "node_definitions", "reference_variant"):
        print("Column node_definitions.reference_variant already present.")
    else:
        if not args.commit:
            print("DRY  would: ALTER TABLE node_definitions ADD COLUMN "
                  "reference_variant VARCHAR(64) NOT NULL DEFAULT 'hero'")
        else:
            cur.execute(
                "ALTER TABLE node_definitions "
                "ADD COLUMN reference_variant VARCHAR(64) NOT NULL "
                "DEFAULT 'hero'"
            )
            print("WRITE ALTER TABLE node_definitions ADD COLUMN "
                  "reference_variant VARCHAR(64) NOT NULL DEFAULT 'hero'")

    # 2. node_variant_verification table
    if _table_exists(cur, "node_variant_verification"):
        print("Table node_variant_verification already present.")
    else:
        if not args.commit:
            print("DRY  would: CREATE TABLE node_variant_verification (...)")
        else:
            cur.execute(
                """
                CREATE TABLE node_variant_verification (
                    id SERIAL PRIMARY KEY,
                    build_spec_id INTEGER REFERENCES build_specs(id)
                        ON DELETE CASCADE,
                    node_definition_id INTEGER REFERENCES node_definitions(id)
                        ON DELETE CASCADE,
                    variant VARCHAR(64) NOT NULL,
                    score REAL,
                    verdict VARCHAR(32) NOT NULL,
                    scored_against_variant VARCHAR(64),
                    scored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    output_type_at_test VARCHAR(32),
                    raw_output_ref VARCHAR(512),
                    CONSTRAINT nvv_either_fk
                        CHECK (build_spec_id IS NOT NULL
                               OR node_definition_id IS NOT NULL)
                )
                """
            )
            cur.execute(
                "CREATE INDEX idx_nvv_build_spec ON "
                "node_variant_verification(build_spec_id) "
                "WHERE build_spec_id IS NOT NULL"
            )
            cur.execute(
                "CREATE INDEX idx_nvv_node_def ON "
                "node_variant_verification(node_definition_id) "
                "WHERE node_definition_id IS NOT NULL"
            )
            cur.execute(
                "CREATE UNIQUE INDEX uq_nvv_build_spec_variant ON "
                "node_variant_verification(build_spec_id, variant) "
                "WHERE build_spec_id IS NOT NULL"
            )
            cur.execute(
                "CREATE UNIQUE INDEX uq_nvv_node_def_variant ON "
                "node_variant_verification(node_definition_id, variant) "
                "WHERE node_definition_id IS NOT NULL"
            )
            print("WRITE CREATE TABLE node_variant_verification + indexes")

    if not args.commit:
        print("\n(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
