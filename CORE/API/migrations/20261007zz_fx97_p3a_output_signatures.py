# API/migrations/20261007zz_fx97_p3a_output_signatures.py
"""FX.97 p3a (owner decision Q83=A) — signed output hash: the NEW table.

models.OutputSignature (services/output_signature.py writes it, p3b's
verify endpoint reads it) is a small sidecar to `workflow_node_outputs`:
one live row per (content_hash, algorithm, key_id) — the Ed25519
signature of the canonical message b"sha256:<content_hash>" made with
this install's per-install key:

    output_signatures
      id                       BIGSERIAL PRIMARY KEY
      content_hash             VARCHAR(64)  NOT NULL   the SHA256 hex
      algorithm                VARCHAR(32)  NOT NULL   'ed25519'
      key_id                   VARCHAR(128) NOT NULL   PUBLIC handle mod-out-<hex>
      signature_b64            TEXT         NOT NULL   64-byte signature, base64
      tenant_id                INTEGER -> tenants.id   NOT NULL
      signed_at                TIMESTAMPTZ  NULL
      deleted_at               TIMESTAMPTZ  NULL      (soft-delete tombstone)
      deleted_by_user_id       INTEGER -> users.id     ON DELETE SET NULL

NO key material is stored anywhere in the DB: the private key lives in
Vault KV v2 at output-signing/key (services/output_signature.py), and
`key_id` is only the public handle p3b resolves, mirrored in the
platform-settings registry (output.signature_key_id).

ISO 27001: NO hard delete. A superseded signature (rotated install key)
gets the `deleted_at` tombstone and its evidence stays; a re-signed hash
under a fresh key mints a new row. The uniqueness index is partial on
`deleted_at IS NULL` (same idiom node_egress_approvals uses) so a tombstone
never squats the (content_hash, algorithm, key_id) key.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS on every
index. Nothing existing is altered, dropped, or truncated — additive only.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zz_fx97_p3a_output_signatures.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zz_fx97_p3a_output_signatures.py --commit   # apply

NOT applied by the p3a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "output_signatures"

_STATEMENTS = [
    (
        f"{_TABLE} table (FX.97 p3a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    content_hash VARCHAR(64) NOT NULL,
    algorithm VARCHAR(32) NOT NULL,
    key_id VARCHAR(128) NOT NULL,
    signature_b64 TEXT NOT NULL,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    signed_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL
);
""",
    ),
    (
        f"{_TABLE} content lookup index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_content_hash
    ON {_TABLE} (content_hash);
""",
    ),
    (
        f"{_TABLE} tenant index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id
    ON {_TABLE} (tenant_id);
""",
    ),
    (
        f"{_TABLE} tombstone index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        f"{_TABLE} one live signature per (content_hash, algorithm, key_id)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live_key
    ON {_TABLE} (content_hash, algorithm, key_id)
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
