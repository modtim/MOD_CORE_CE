"""GSEC FX.79 part 1a — data-marking labels (CUI / ITAR / export-controlled).

Owner decision Q62=A (gov/DoD profile): admins mark workflows, files and
outputs with CUI, CUI//SP-category, or a custom label; marked items cannot
leave the tenant (external share / marketplace / external AI), ITAR-labelled
items are restricted to US-person users (customer attests, MOD enforces),
and every mark/clear is audit-logged (DATA_MARKING_SET /
DATA_MARKING_CLEARED).

data_markings (NEW): one active label applied to one tenant resource.
    id              BIGINT PK
    tenant_id       INTEGER NOT NULL      tenants.id FK
    resource_type   VARCHAR(40) NOT NULL  workflow|asset|node_definition|
                                          named_artifact|dataset
    resource_id     VARCHAR(64) NOT NULL  target row id as a string
    label           VARCHAR(40) NOT NULL  system or tenant-defined code
    dissemination   VARCHAR(120) NULL     e.g. NOFORN / FED ONLY
    set_by_user_id  INTEGER NULL          users.id FK
    set_at          TIMESTAMPTZ NOT NULL
    reason          TEXT NULL
    deleted_at      TIMESTAMPTZ NULL      soft-delete marker (ISO 27001)
    PARTIAL UNIQUE (tenant_id, resource_type, resource_id, label)
        WHERE deleted_at IS NULL  ->  uq_data_markings_live

marking_label_defs (NEW): the label catalogue. tenant_id NULL = system
label; set = tenant-defined label.
    id / tenant_id (NULL system) / code / description /
    requires_us_person (default FALSE) / blocks_external_ai (default TRUE) /
    blocks_external_share (default TRUE) / created_at / deleted_at
    UNIQUE (tenant_id, code)  ->  uq_marking_label_defs_tenant_code

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    DATA_MARKING_SET, DATA_MARKING_CLEARED — via
    ALTER TYPE ADD VALUE IF NOT EXISTS.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; seeds are ON CONFLICT DO
NOTHING (tenant rows) / WHERE NOT EXISTS (system rows, because a bare
UNIQUE with NULL tenant_id never conflicts in PG). Dry run (default)
PRINTS the SQL and exits 0 without touching the database. Apply with
--commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zb_fx79_data_markings.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zb_fx79_data_markings.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007za. models.py is bind-mounted live into the API container, so the
tables must exist before any FX.79 part-1a code path writes to them —
apply immediately after adding the models to models.py. NOT applied by
this task (model/migration/tests only, p1a) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

# (code, description, requires_us_person, blocks_external_ai,
#  blocks_external_share) — the Q62 system label set.
_SYSTEM_LABELS = (
    ("CUI",
     "Controlled Unclassified Information (US DoD SP-protected-category "
     "family). Marked items stay in-tenant.", True, True, True),
    ("CUI//SP-EXPT",
     "CUI, Export Control protected. US-person access only.", True,
     True, True),
    ("ITAR",
     "ITAR (22 CFR 120-130) defense articles/technical data. Restricted to "
     "users with the admin-set US-person attribute (customer attests, MOD "
     "enforces, MOD does not verify).", True, True, True),
    ("EAR99",
     "EAR99 (EAR catch-all export control). No license required but tracked.",
     False, True, True),
    ("EXPORT_CONTROLLED",
     "Generic export-controlled (customer-defined jurisdiction).", False,
     True, True),
    ("PROPRIETARY",
     "Customer proprietary. Not shared outside the tenant.", False, True,
     True),
)

_STATEMENTS = [
    (
        "data_markings table",
        """
CREATE TABLE IF NOT EXISTS data_markings (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    resource_type VARCHAR(40) NOT NULL,
    resource_id VARCHAR(64) NOT NULL,
    label VARCHAR(40) NOT NULL,
    dissemination VARCHAR(120) NULL,
    set_by_user_id INTEGER NULL REFERENCES users(id),
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason TEXT NULL,
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_data_markings_resource_type CHECK (
        resource_type IN ('workflow', 'asset', 'node_definition',
                          'named_artifact', 'dataset')
    )
);
""",
    ),
    (
        "data_markings indexes",
        """
CREATE INDEX IF NOT EXISTS ix_data_markings_tenant_id
    ON data_markings (tenant_id);
CREATE INDEX IF NOT EXISTS ix_data_markings_resource_type
    ON data_markings (resource_type);
CREATE INDEX IF NOT EXISTS ix_data_markings_label
    ON data_markings (label);
CREATE INDEX IF NOT EXISTS ix_data_markings_deleted_at
    ON data_markings (deleted_at);
""",
    ),
    (
        "data_markings partial unique (active marks only)",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_data_markings_live
    ON data_markings (tenant_id, resource_type, resource_id, label)
    WHERE deleted_at IS NULL;
""",
    ),
    (
        "marking_label_defs table",
        """
CREATE TABLE IF NOT EXISTS marking_label_defs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NULL REFERENCES tenants(id),
    code VARCHAR(40) NOT NULL,
    description TEXT NULL,
    requires_us_person BOOLEAN NOT NULL DEFAULT FALSE,
    blocks_external_ai BOOLEAN NOT NULL DEFAULT TRUE,
    blocks_external_share BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_marking_label_defs_tenant_code UNIQUE (tenant_id, code)
);
""",
    ),
    (
        "marking_label_defs indexes",
        """
CREATE INDEX IF NOT EXISTS ix_marking_label_defs_code
    ON marking_label_defs (code);
CREATE INDEX IF NOT EXISTS ix_marking_label_defs_tenant_id
    ON marking_label_defs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_marking_label_defs_deleted_at
    ON marking_label_defs (deleted_at);
""",
    ),
]

# One seed statement per system label — idempotent. System rows have
# tenant_id NULL, and a bare UNIQUE(tenant_id, code) never treats two NULLs
# as a conflict in PG, so dedupe with WHERE NOT EXISTS instead of
# ON CONFLICT.
_SEED_STATEMENTS = [
    (
        f"marking_label_defs system seed {code}",
        f"""
INSERT INTO marking_label_defs
    (tenant_id, code, description, requires_us_person,
     blocks_external_ai, blocks_external_share)
SELECT NULL, '{code}', {desc}, {us}, {ai}, {sh}
WHERE NOT EXISTS (
    SELECT 1 FROM marking_label_defs
    WHERE tenant_id IS NULL AND code = '{code}' AND deleted_at IS NULL
);
""",
    )
    for code, desc, us, ai, sh in (
        (c,
         "'" + d.replace("'", "''") + "'",
         "TRUE" if r else "FALSE",
         "TRUE" if b else "FALSE",
         "TRUE" if s else "FALSE")
        for c, d, r, b, s in _SYSTEM_LABELS
    )
]


def seed_system_label_defs(run_sql) -> None:
    """Idempotently insert the system MarkingLabelDef rows (one statement
    per label). ``run_sql`` executes one complete statement (a DBAPI
    cursor works; the test path passes a SQLAlchemy text-executor so the
    statements join the session's transaction). Imported by
    tests/test_gsec_fx79_p1a_markings.py for the create_all-based sibling
    test DB.

    Each seed is a SINGLE statement — executed whole; never split on
    ';' (descriptions may legitimately contain semicolons).
    """
    for label, sql in _SEED_STATEMENTS:
        run_sql(sql.strip().rstrip(";"))
        print(f"WRITE  {label}")


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
        for label, sql in _STATEMENTS + _SEED_STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — SET ROLE only if it exists
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    seed_system_label_defs(cur.execute)
    # ALTER TYPE ADD VALUE cannot run inside a transaction, hence the
    # autocommit connection; ADD VALUE IF NOT EXISTS keeps re-runs safe.
    for value in ("DATA_MARKING_SET", "DATA_MARKING_CLEARED"):
        cur.execute(
            f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}';"
        )
        print(f"WRITE  auditaction {value}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.79 p1a data-marking schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
