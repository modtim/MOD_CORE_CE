# API/migrations/20261007zz_fx91_p2c_egress_approval.py
"""FX.91 p2c [B] — tenant-admin one-time egress approval: the NEW table.

models.NodeEgressApproval (services/node_egress_approval.py reads/writes it)
is a small sidecar to `node_definitions`, one live row per (tenant, node):

    node_egress_approvals
      id                       BIGSERIAL PRIMARY KEY
      tenant_id                INT -> tenants.id   NULL = platform/system node
      node_definition_id       INT -> node_definitions.id
      approved_by_user_id      INT -> users.id
      approved_at              TIMESTAMPTZ
      domains_approved         JSONB   the canonical set the admin approved
      revoked_at               TIMESTAMPTZ
      revoked_by_user_id       INT -> users.id (SET NULL on delete)
      deleted_at               TIMESTAMPTZ   (soft-delete tombstone)
      deleted_by_user_id       INT -> users.id (SET NULL on delete)

Approval validity is the set comparison between ``domains_approved`` and the
node's current ``egress_domains`` (FX.91 p2a), so a changed declaration
invalidates the approval without touching this row — that is the whole
"one-time" contract, and it is why the set is stored here rather than a
boolean.

ISO 27001: NO hard delete. Revocation is the ``deleted_at`` tombstone plus
``revoked_at``/``revoked_by_user_id``; the row and its evidence always stay,
and a re-approval REVIVES it. The uniqueness index is partial on
``deleted_at IS NULL`` (same idiom node_definitions uses for its name
uniqueness) so a revoked row never squats the (tenant, node) key.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS on every
index. Nothing existing is altered, dropped, or truncated — additive only.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zz_fx91_p2c_egress_approval.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zz_fx91_p2c_egress_approval.py --commit   # apply

NOT applied by the p2c task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_egress_approvals"

_STATEMENTS = [
    (
        f"{_TABLE} table (FX.91 p2c)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NULL REFERENCES tenants (id) NULL,
    node_definition_id INTEGER NOT NULL REFERENCES node_definitions (id),
    approved_by_user_id INTEGER NULL REFERENCES users (id) NULL,
    approved_at TIMESTAMPTZ NULL,
    domains_approved JSONB NULL,
    revoked_at TIMESTAMPTZ NULL,
    revoked_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    deleted_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL
);
""",
    ),
    (
        f"{_TABLE} soft-delete tombstone index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        f"{_TABLE} node lookup index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_node_definition_id
    ON {_TABLE} (node_definition_id);
""",
    ),
    (
        f"{_TABLE} one live approval per (tenant, node)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live_tenant_node
    ON {_TABLE} (tenant_id, node_definition_id)
    WHERE deleted_at IS NULL;
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
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"{_TABLE} ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
