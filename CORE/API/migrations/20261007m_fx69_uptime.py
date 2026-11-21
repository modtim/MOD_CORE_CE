"""GSEC FX.69 part 2 (owner decision Q52=A; Q71=C) — hosted-uptime SLA
measurement (services/uptime.py, models.UptimeSample /
models.MaintenanceWindow).

Q52=A: hosted uptime SLA target is monthly 99.5%; the
scheduled maintenance excluded only with >=72h notice and
<=4h/month (240 min); service credits 10% below target, 25% below
99.0%, 50% below 95% (cap 50%). Q71=C: the public status page (FX.69
p3) runs on the same site — no availability promise during outages.

uptime_samples (NEW, append-only telemetry — one probe sample per
component per minute, recorded by the scheduler job
fx69_uptime_sampling in services/scheduler.py from the /health probe
fan-out):
    id            BIGINT PK (identity)
    ts            TIMESTAMPTZ NOT NULL  sample timestamp (UTC)
    component     VARCHAR(64) NOT NULL  /health probe key (api/database/
                                        keycloak/minio/redis/harbor/workers/
                                        model_cache/crypto_self_test)
    ok            BOOLEAN NOT NULL      probe status was "up"
    latency_ms    INTEGER NULL          probe round-trip (NULL on error)
    created_at    TIMESTAMPTZ NOT NULL

maintenance_windows (NEW, soft-delete only — ISO 27001, no hard
deletes):
    id            VARCHAR(36) PK        uuid4 string
    start_at      TIMESTAMPTZ NOT NULL  window start
    end_at        TIMESTAMPTZ NOT NULL  window end (CK end_at > start_at)
    announced_at  TIMESTAMPTZ NULL      when the window was announced;
                                        exclusion requires >=72h before
                                        start_at (NULL = never announced
                                        -> minutes count as downtime)
    reason        VARCHAR(500) NULL
    created_by    INTEGER NULL
    created_at / updated_at / deleted_at (deleted_at = soft-delete marker)

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
Dry run (default) PRINTS the SQL and exits 0 without touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007m_fx69_uptime.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007m_fx69_uptime.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007l. models.py is bind-mounted live into the API container, so
the tables must exist before any FX.69 part-2 code path writes to them.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "uptime_samples table",
        """
CREATE TABLE IF NOT EXISTS uptime_samples (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    component VARCHAR(64) NOT NULL,
    ok BOOLEAN NOT NULL,
    latency_ms INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        "uptime_samples indexes",
        """
CREATE INDEX IF NOT EXISTS ix_uptime_samples_ts
    ON uptime_samples (ts);
CREATE INDEX IF NOT EXISTS ix_uptime_samples_component
    ON uptime_samples (component);
CREATE INDEX IF NOT EXISTS ix_uptime_samples_ok
    ON uptime_samples (ok);
CREATE INDEX IF NOT EXISTS ix_uptime_samples_component_ts
    ON uptime_samples (component, ts);
""",
    ),
    (
        "maintenance_windows table",
        """
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id VARCHAR(36) PRIMARY KEY,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    announced_at TIMESTAMPTZ NULL,
    reason VARCHAR(500) NULL,
    created_by INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_maintenance_windows_end_after_start
        CHECK (end_at > start_at)
);
""",
    ),
    (
        "maintenance_windows indexes",
        """
CREATE INDEX IF NOT EXISTS ix_maintenance_windows_start_at
    ON maintenance_windows (start_at);
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
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.69 uptime SLA measurement schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
