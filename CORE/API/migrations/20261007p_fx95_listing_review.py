"""GSEC FX.95 p1 (owner decision Q81=A) — marketplace listing/version
review state machine.

Adds the review columns the staff approval workflow reads and writes
(models.WorkflowListing / models.StackListing,
services/listing_review.py, routers/admin_listing_review.py), plus the
audit-action enum values that back one audit row per review-state
transition.

Q81=A: every new listing and new version passes an automated gate
(malware scan, image vulnerability scan blocking Critical, licence
check, sandbox test run confirming declared egress domains match
behaviour — FX.95.p2, a separate not-yet-built task) and then MOD
staff approval. "Code today has no review step: listings go live as
submitted" — this migration backfills `review_status='live'` for
every row that is ALREADY `status='published'` so existing storefront
behaviour is unchanged; nothing already live gets re-reviewed.

workflow_listings / stack_listings (ADDITIVE, nullable columns):
    review_status      VARCHAR(20) NULL   submitted|auto_checks|
                                           staff_review|live|rejected;
                                           NULL = predates the review
                                           gate (back-filled to 'live'
                                           for already-published rows
                                           by this migration)
    reviewed_by         INTEGER NULL       users.id FK
    reviewed_at         TIMESTAMPTZ NULL
    rejection_reason    TEXT NULL
    submitted_at        TIMESTAMPTZ NULL

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    LISTING_SUBMITTED / LISTING_APPROVED / LISTING_REJECTED

Metadata on those rows carries listing kind/id/reviewer/reason only —
never listing content. Append-only audit rows (ISO 27001).

Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS +
ALTER TYPE ADD VALUE IF NOT EXISTS + a backfill UPDATE that only ever
touches rows still at review_status IS NULL (safe to re-run). Dry run
(default) PRINTS the SQL and exits 0 without touching the database.
Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007p_fx95_listing_review.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007p_fx95_listing_review.py --commit   # apply

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007k_fx50_security_incidents.py). Migration runner is
tools/mod_migrate.py (steps discovered as *.py in this directory, run
in filename order) — this file sorts after 20261007q (q covers a
different letter already taken; p slots before it alphabetically but
that is fine — every statement here is independently additive/
idempotent and order-independent against 20261007q_fx73_licence_records.py).

models.py is bind-mounted live into the API container, so the columns
+ enum values must exist before any FX.95 p1 code path writes to them
— apply immediately after adding the models to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "workflow_listings review columns",
        """
ALTER TABLE workflow_listings
    ADD COLUMN IF NOT EXISTS review_status VARCHAR(20) NULL,
    ADD COLUMN IF NOT EXISTS reviewed_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS rejection_reason TEXT NULL,
    ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ NULL;
""",
    ),
    (
        "workflow_listings review index",
        """
CREATE INDEX IF NOT EXISTS ix_workflow_listings_review_status
    ON workflow_listings (review_status);
""",
    ),
    (
        "stack_listings review columns",
        """
ALTER TABLE stack_listings
    ADD COLUMN IF NOT EXISTS review_status VARCHAR(20) NULL,
    ADD COLUMN IF NOT EXISTS reviewed_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS rejection_reason TEXT NULL,
    ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ NULL;
""",
    ),
    (
        "stack_listings review index",
        """
CREATE INDEX IF NOT EXISTS ix_stack_listings_review_status
    ON stack_listings (review_status);
""",
    ),
    (
        "backfill: already-published workflow listings -> review_status live",
        """
UPDATE workflow_listings
    SET review_status = 'live'
    WHERE status = 'published' AND review_status IS NULL;
""",
    ),
    (
        "backfill: already-published stack listings -> review_status live",
        """
UPDATE stack_listings
    SET review_status = 'live'
    WHERE status = 'published' AND review_status IS NULL;
""",
    ),
    (
        "auditaction listing review values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'LISTING_SUBMITTED';
""",
    ),
    (
        "auditaction listing review values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'LISTING_APPROVED';
""",
    ),
    (
        "auditaction listing review values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'LISTING_REJECTED';
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
    print("FX.95 p1 listing review schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
