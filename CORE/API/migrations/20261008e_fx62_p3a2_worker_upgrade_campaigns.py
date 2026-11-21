#!/usr/bin/env python3
"""FX.62 p3a2 (API-side rolling worker upgrade) — the two NEW tables.

FX.62 p3a1 gave the WORKER side the version stamp (it loads the new image from
the bundle / local registry and heartbeats the version it is now running).
What the API side still lacks is the sequencing memory: `worker_licenses.update_requested`
says "drain and self-update" but nothing remembers WHICH release we are rolling
to, WHICH workers are already in it, and — when one of them fails — WHY the
rollout stopped. services/worker_update_rolling.py is the service half; this is
the data half:

    worker_upgrade_campaigns
      id                     BIGSERIAL PRIMARY KEY
      target_version         VARCHAR(50) NOT NULL   (the release being rolled)
      status                 VARCHAR(16) NOT NULL DEFAULT 'open'
                             open|held|completed|abandoned
      hold_reason            TEXT NULL              (why the rollout STOPPED)
      held_at / completed_at TIMESTAMPTZ NULL
      actor_user_id          INTEGER NULL FK users(id)
      created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at             TIMESTAMPTZ NULL       soft delete ONLY

    worker_upgrade_steps
      id                     BIGSERIAL PRIMARY KEY
      campaign_id            INTEGER NOT NULL FK worker_upgrade_campaigns(id)
      worker_license_id      INTEGER NOT NULL FK worker_licenses(id)
      worker_name            VARCHAR(255) NOT NULL  (kept even if the worker row
                             is later soft-deleted — the record stays readable)
      from_version           VARCHAR(50) NULL
      to_version             VARCHAR(50) NOT NULL
      state                  VARCHAR(16) NOT NULL DEFAULT 'queued'
                             queued|requested|healthy|failed|timeout|skipped
      requested_at / resolved_at TIMESTAMPTZ NULL
      error                  TEXT NULL
      attempt                INTEGER NOT NULL DEFAULT 1
      created_at / deleted_at

FKs carry NO `ON DELETE` clause (and certainly no ONDELETE): deleting a
tenant/user/worker must never erase the record of who was upgraded to what
(ISO 27001 A.18.1.3 — protection of records). `ON DELETE SET NULL` appears
nowhere in this file on purpose.

CHECKs (the DB enforces the rollout states, not just the service):
    ck_..._status   the four campaign states only
    ck_..._state    the six step states only
    ck_..._hold_clock  a `held` campaign has held_at, and vice versa
    ck_..._resolved    a resolved step (healthy/failed/timeout/skipped) has
                       resolved_at; an unresolved one (queued/requested) does
                       not — so a step can never claim "healthy" without a
                       time it was observed healthy.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS.
"""
from __future__ import annotations

import argparse
import os
import sys

_CAMPAIGNS = "worker_upgrade_campaigns"
_STEPS = "worker_upgrade_steps"


_STATEMENTS = [
    (
        f"{_CAMPAIGNS} table (FX.62 p3a2)",
        f"""
CREATE TABLE IF NOT EXISTS {_CAMPAIGNS} (
    id BIGSERIAL PRIMARY KEY,
    target_version VARCHAR(50) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    hold_reason TEXT NULL,
    held_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    actor_user_id INTEGER NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_CAMPAIGNS}_status CHECK (
        status IN ('open', 'held', 'completed', 'abandoned')
    ),
    CONSTRAINT ck_{_CAMPAIGNS}_hold_clock CHECK (
        (status = 'held') = (held_at IS NOT NULL)
    )
);
""",
    ),
    (
        f"{_CAMPAIGNS} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_CAMPAIGNS}_status ON {_CAMPAIGNS} (status);
CREATE INDEX IF NOT EXISTS ix_{_CAMPAIGNS}_target ON {_CAMPAIGNS} (target_version);
CREATE INDEX IF NOT EXISTS ix_{_CAMPAIGNS}_deleted_at ON {_CAMPAIGNS} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_CAMPAIGNS}_status_created
    ON {_CAMPAIGNS} (status, created_at);
""",
    ),
    (
        f"{_STEPS} table (FX.62 p3a2)",
        f"""
CREATE TABLE IF NOT EXISTS {_STEPS} (
    id BIGSERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES {_CAMPAIGNS}(id),
    worker_license_id INTEGER NOT NULL REFERENCES worker_licenses(id),
    worker_name VARCHAR(255) NOT NULL,
    from_version VARCHAR(50) NULL,
    to_version VARCHAR(50) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'queued',
    requested_at TIMESTAMPTZ NULL,
    resolved_at TIMESTAMPTZ NULL,
    error TEXT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_STEPS}_state CHECK (
        state IN ('queued', 'requested', 'healthy', 'failed', 'timeout', 'skipped')
    ),
    CONSTRAINT ck_{_STEPS}_resolved CHECK (
        (state IN ('healthy', 'failed', 'timeout', 'skipped'))
        = (resolved_at IS NOT NULL)
    ),
    CONSTRAINT ck_{_STEPS}_attempt CHECK (attempt >= 1)
);
""",
    ),
    (
        f"{_STEPS} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_STEPS}_campaign ON {_STEPS} (campaign_id);
CREATE INDEX IF NOT EXISTS ix_{_STEPS}_worker ON {_STEPS} (worker_license_id);
CREATE INDEX IF NOT EXISTS ix_{_STEPS}_state ON {_STEPS} (state);
CREATE INDEX IF NOT EXISTS ix_{_STEPS}_campaign_state
    ON {_STEPS} (campaign_id, state);
CREATE INDEX IF NOT EXISTS ix_{_STEPS}_deleted_at ON {_STEPS} (deleted_at);
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
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "FX.62 p3a2 ready: worker_upgrade_campaigns + worker_upgrade_steps "
        "exist with the four campaign states, the six step states, the hold "
        "clock and the resolved-step CHECKs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
