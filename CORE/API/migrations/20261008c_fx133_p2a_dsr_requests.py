#!/usr/bin/env python3
"""GSEC FX.133 p2a (owner decision Q121=A) — data-subject (DSR) requests:
the NEW table + its constraints/indexes.

Q121=A: "The rights-request flow supports the state request types, appeals
and per-state deadlines (usually 45 days)." A data subject (a customer of
a controller) files a DSR — a TYPE, the record it is about, their own
words — and the controller walks it through a state machine with a
statutory deadline clock. This migration is the DATA half (p2b adds the
per-state deadline timers, p3 the Global Privacy Control signal).

    dsr_requests
      id                     BIGSERIAL PRIMARY KEY
      tenant_id              INTEGER NOT NULL  FK tenants(id)   (controller)
      subject_user_id        INTEGER NULL      FK users(id)     (the subject)
      request_type           VARCHAR(24) NOT NULL
                             access_export|rectification|erasure|restriction|
                             objection|portability
      subject_ref            VARCHAR(255) NOT NULL   (data category/record)
      description            TEXT NOT NULL           (REQUIRED reason)
      status                 VARCHAR(16) NOT NULL DEFAULT 'received'
                             received|verified|in_progress|completed|rejected
      due_date               TIMESTAMPTZ NOT NULL    (received_at + statutory)
      verified_at / decided_at / decided_by_user_id   clocks + decider
      anonymised             BOOLEAN NOT NULL DEFAULT false
      hard_delete            BOOLEAN NOT NULL DEFAULT false  (CHECK: always
                             false — see ck_..._no_hard_delete)
      appeal_text / appeal_at                        appeal against a decision
      parent_id              INTEGER NULL FK dsr_requests(id) (re-filed/ext)
      statutory_days         INTEGER NOT NULL DEFAULT 45  (1..3650)
      state_law_label        VARCHAR(24) NULL  (know/delete/correct/...)
      received_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
      created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at             TIMESTAMPTZ NULL      soft delete ONLY

FKs carry NO `ON DELETE` clause: deleting a tenant/user must never erase
the record of what that tenant was asked (ISO 27001 A.18.1.3).

Constraints (the DB enforces the compliance rules, not just the service):
    ck_..._type                the six request types only
    ck_..._status              the five states only
    ck_..._no_hard_delete      hard_delete = false, ALWAYS. An erasure DSR
                               is soft-delete/anonymise ONLY (ISO 27001:
                               records shall not be destroyed), so the DB
                               refuses any row that claims a hard delete —
                               no code path, present or future, can turn a
                               subject's "forget me" into destroyed data.
    ck_..._statutory_days      1..3650 days (the statutory window)
    ck_..._decision_clocks     a decided row carries decided_at
    ck_..._verified_clock      a 'verified' row carries verified_at

Indexes: request_type, status, tenant_id, subject_user_id, due_date,
received_at, deleted_at, parent_id, state_law_label, subject_ref + the
three composites the queue reads order by.

Additive only: nothing existing is altered, dropped, or truncated.
Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS.

No auditaction enum labels are needed: the DSR audit rows are AuditEvent
rows (event_name strings dsr.received / dsr.verified / dsr.in_progress /
dsr.completed / dsr.rejected / dsr.appealed), and audit_events.event_name
is a free String(100), not a PG enum.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261008c_fx133_p2a_dsr_requests.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261008c_fx133_p2a_dsr_requests.py --commit   # apply

NOT applied by the p2a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "dsr_requests"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.133 p2a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    subject_user_id INTEGER NULL REFERENCES users(id),
    request_type VARCHAR(24) NOT NULL,
    subject_ref VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'received',
    due_date TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ NULL,
    decided_at TIMESTAMPTZ NULL,
    decided_by_user_id INTEGER NULL REFERENCES users(id),
    anonymised BOOLEAN NOT NULL DEFAULT false,
    hard_delete BOOLEAN NOT NULL DEFAULT false,
    appeal_text TEXT NULL,
    appeal_at TIMESTAMPTZ NULL,
    parent_id INTEGER NULL REFERENCES {_TABLE}(id),
    statutory_days INTEGER NOT NULL DEFAULT 45,
    state_law_label VARCHAR(24) NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_type CHECK (
        request_type IN ('access_export', 'rectification', 'erasure',
                        'restriction', 'objection', 'portability')
    ),
    CONSTRAINT ck_{_TABLE}_status CHECK (
        status IN ('received', 'verified', 'in_progress', 'completed',
                   'rejected')
    ),
    CONSTRAINT ck_{_TABLE}_no_hard_delete CHECK (hard_delete = false),
    CONSTRAINT ck_{_TABLE}_statutory_days CHECK (
        statutory_days > 0 AND statutory_days <= 3650
    ),
    CONSTRAINT ck_{_TABLE}_decision_clocks CHECK (
        status = 'received' OR status = 'verified' OR status = 'in_progress'
        OR decided_at IS NOT NULL
    ),
    CONSTRAINT ck_{_TABLE}_verified_clock CHECK (
        status != 'verified' OR verified_at IS NOT NULL
    )
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_request_type
    ON {_TABLE} (request_type);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_subject_user
    ON {_TABLE} (subject_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_due_date ON {_TABLE} (due_date);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_received_at ON {_TABLE} (received_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_parent ON {_TABLE} (parent_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_state_law_label
    ON {_TABLE} (state_law_label);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_subject_ref ON {_TABLE} (subject_ref);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_status
    ON {_TABLE} (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status_due
    ON {_TABLE} (status, due_date);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_subject
    ON {_TABLE} (tenant_id, subject_ref);
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
        "FX.133 p2a ready: dsr_requests exists with the six request types, "
        "the five states, the always-false hard_delete CHECK and the "
        "statutory-days CHECK."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
