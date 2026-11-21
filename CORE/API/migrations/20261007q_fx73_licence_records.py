"""GSEC FX.73 part 1 (owner decision Q56=A) — third-party licence records.

Adds the licence_records table the SPDX/Hugging Face auto-detect service
(services/licence_detect.py) and models.LicenceRecord write/read. One row
per licensable subject (node definition / model / container image)
carrying its detected code or model licence and a commercial-use
classification. Nothing downstream reads this table yet — the manual
review queue (FX.73 p2), the hosted visibility gate (FX.73 p3, not part of this edition), and
the palette badge (FX.73 p4) all build on top of this schema; FX.99's
model/node licence labels are these same rows (no separate label table).

licence_records (NEW):
    id                    BIGINT PK
    subject_type          VARCHAR(32)  NOT NULL   node_definition|model|container_image
    subject_id            VARCHAR(255) NOT NULL   natural id of the subject
    spdx_id               VARCHAR(128) NULL       SPDX id or vendor licence slug
    licence_name          VARCHAR(255) NULL       human name when no SPDX id
    source                VARCHAR(16)  NOT NULL   spdx|huggingface|manual|unknown
    commercial_use        VARCHAR(16)  NOT NULL   allowed|non_commercial|unknown|restricted
    attribution_required  BOOLEAN      NULL
    detected_at           TIMESTAMPTZ  NULL
    reviewed_by           INTEGER      NULL       users.id FK (FX.73 p2 queue)
    reviewed_at           TIMESTAMPTZ  NULL
    created_at / updated_at / deleted_at           (deleted_at = soft-delete
                                                    marker, ISO 27001 — NO
                                                    hard deletes)

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS. Dry
run (default) PRINTS the SQL and exits 0 without touching the database.
Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007q_fx73_licence_records.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007q_fx73_licence_records.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007m. No ALTER TYPE here (no enum touched), so no autocommit
connection requirement, but the same pattern (SET ROLE "modTech" when
available) is kept for consistency with the other 20261007* migrations.

models.py is bind-mounted live into the API container, so the table
must exist before any FX.73 code path writes to it — apply immediately
after adding models.LicenceRecord. DRY-RUN ONLY from this task; the
lead applies it.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "licence_records table",
        """
CREATE TABLE IF NOT EXISTS licence_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_type VARCHAR(32) NOT NULL,
    subject_id VARCHAR(255) NOT NULL,
    spdx_id VARCHAR(128) NULL,
    licence_name VARCHAR(255) NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'unknown',
    commercial_use VARCHAR(16) NOT NULL DEFAULT 'unknown',
    attribution_required BOOLEAN NULL,
    detected_at TIMESTAMPTZ NULL,
    reviewed_by INTEGER NULL REFERENCES users(id),
    reviewed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_licence_records_source
        CHECK (source IN ('spdx', 'huggingface', 'manual', 'unknown')),
    CONSTRAINT ck_licence_records_commercial_use
        CHECK (commercial_use IN ('allowed', 'non_commercial', 'unknown', 'restricted'))
);
""",
    ),
    (
        "licence_records indexes",
        """
CREATE INDEX IF NOT EXISTS ix_licence_records_subject_type
    ON licence_records (subject_type);
CREATE INDEX IF NOT EXISTS ix_licence_records_subject_id
    ON licence_records (subject_id);
CREATE INDEX IF NOT EXISTS ix_licence_records_subject
    ON licence_records (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_licence_records_deleted_at
    ON licence_records (deleted_at);
""",
    ),
]


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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.73 licence_records schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
