"""GSEC FX.144 p2a (owner decision Q131=A) — production release approval.

FX.144 is change control: a production release needs the owner's one-click
in-app approval (diff summary + test results stored as EVIDENCE) before it
runs; the EMERGENCY path ships first and the owner reviews it within 24h
(tracked). This migration is the DATA half: the table the release-approval
service (services/release_approvals.py) writes and the approval page (p4)
lists. p2b adds the endpoint; p3a adds the emergency-review tracking reads.

    release_approvals:
        id BIGSERIAL PRIMARY KEY
        release_ref         VARCHAR(128) NOT NULL (the signed tag ref, e.g.
                            "v1.0.77" — indexed)
        rollback_ref        VARCHAR(128) NULL (the PREVIOUS tag to redeploy on
                            rollback — 09a writes it BEFORE an emergency deploy)
        image_digests       JSONB NOT NULL DEFAULT '{}' ({"service":
                            "sha256:<hex>"} — identifiers only, never secrets)
        evidence            JSONB NOT NULL DEFAULT '{}' ({"ci": {...},
                            "tests": {...}} — what the approver clicked through)
        requested_by_user_id INTEGER NOT NULL REFERENCES users(id)
        requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        approved_by_user_id INTEGER NULL REFERENCES users(id)
        approved_at         TIMESTAMPTZ NULL
        status              VARCHAR(32) NOT NULL DEFAULT 'pending'
                            (pending|approved|rejected|emergency|superseded)
        reason / decision_note TEXT NULL
        deployed_at         TIMESTAMPTZ NULL (emergency rows only)
        review_due_at       TIMESTAMPTZ NULL (deployed_at +
                            change_mgmt.emergency_review_hours, default 24h)
        review_status       VARCHAR(16) NOT NULL DEFAULT 'pending'
                            (pending|reviewed|overdue — emergency review only)
        deleted_at          TIMESTAMPTZ NULL (ISO 27001: soft delete ONLY)

Constraints:
    ck_release_approvals_status                the five states
    ck_release_approvals_review_status         the three review states
    ck_release_approvals_second_approver       approved_by_user_id IS NULL OR
                                               != requested_by_user_id — the
                                               maker-checker rule (the same
                                               DB-level rule FX.163 p1 ships for
                                               pending approvals, and the same
                                               "approver != requester" rule
                                               services/approval_gate.py enforces
                                               in code) enforced BY THE DB, so a
                                               raw INSERT cannot self-approve a
                                               production change.
    ck_release_approvals_emergency_clocks      an 'emergency' row always carries
                                               deployed_at AND review_due_at, so
                                               an emergency change can never be
                                               recorded without its review clock.

Indexes: release_ref, requested_at, status, approved_by_user_id, deployed_at,
review_due_at, review_status, deleted_at (the single-column ones are named
ix_release_approvals_<column>, matching models.py's index=True), plus the
(ref,status) composite the approval page orders by.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS the
SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzy_fx144_p2a_release_approvals.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzy_fx144_p2a_release_approvals.py --commit  # apply

NOT applied by this task (model/migration/service/tests only) — the lead
applies it. The migration runner is tools/mod_migrate.py (steps discovered
as *.py in this directory, run in filename order); this file sorts after
20261007zz (FX.97 p3a output signatures).
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "release_approvals"

_STATEMENTS = [
    (
        "release_approvals table",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    release_ref VARCHAR(128) NOT NULL,
    rollback_ref VARCHAR(128) NULL,
    image_digests JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_by_user_id INTEGER NULL REFERENCES users(id),
    approved_at TIMESTAMPTZ NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    reason TEXT NULL,
    decision_note TEXT NULL,
    deployed_at TIMESTAMPTZ NULL,
    review_due_at TIMESTAMPTZ NULL,
    review_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_status CHECK (
        status IN ('pending', 'approved', 'rejected', 'emergency',
                   'superseded')
    ),
    CONSTRAINT ck_{_TABLE}_review_status CHECK (
        review_status IN ('pending', 'reviewed', 'overdue')
    ),
    CONSTRAINT ck_{_TABLE}_second_approver CHECK (
        approved_by_user_id IS NULL
        OR approved_by_user_id != requested_by_user_id
    ),
    CONSTRAINT ck_{_TABLE}_emergency_clocks CHECK (
        status != 'emergency'
        OR (deployed_at IS NOT NULL AND review_due_at IS NOT NULL)
    )
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_release_ref ON {_TABLE} (release_ref);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requested_at ON {_TABLE} (requested_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requested_by_user_id
    ON {_TABLE} (requested_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_approved_by_user_id
    ON {_TABLE} (approved_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_approved_at ON {_TABLE} (approved_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deployed_at ON {_TABLE} (deployed_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_review_due_at ON {_TABLE}
    (review_due_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_review_status ON {_TABLE}
    (review_status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_ref_status
    ON {_TABLE} (release_ref, status);
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
        try:
            cur.execute(sql)
            print(f"OK   {label}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
