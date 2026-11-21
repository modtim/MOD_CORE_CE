"""project-agent §1.3 — retain every sample-test attempt, publish the best.

``record_variant_test`` upserts ONE row per (build_spec_id, variant), so a
rebuild of the same spec overwrites the previous result. A repair that
scored worse silently replaced a better build and nothing remembered the
better one had existed.

Rolling back is possible because the earlier image is still in the
registry — pushing a new ``:latest`` moves the tag, it does not delete the
manifest. ImageBuilder already captures the digest after push
(``docker inspect --format "{{index .RepoDigests 0}}"``, build.py:687) and
already pulls-by-digest-and-retags on its source-hash cache fast path
(build.py:311/339). This migration adds the columns needed to record which
digest produced which score.

Schema (all on ``node_variant_verification``):

  - ``attempt_count`` INTEGER NOT NULL DEFAULT 1 — how many sample-test
    results have been recorded for this (spec, variant).
  - ``best_score`` REAL — the highest score seen across attempts. The
    row's existing ``score`` / ``verdict`` / ``raw_output_ref`` /
    ``image_digest`` continue to describe the PUBLISHED attempt, which is
    the best one rather than the latest one.
  - ``image_digest`` VARCHAR(255) — the pushed manifest digest for the
    published attempt, so a regression can be reverted by retagging
    rather than rebuilding.
  - ``attempt_history`` JSONB NOT NULL DEFAULT '[]' — append-only list of
    ``{attempt, score, verdict, scored_against, raw_output_ref,
    image_digest, scored_at, regressed}``. Bounded by the repair budget.

Backfill: existing rows get attempt_count=1, best_score=score, and a
single-entry history synthesised from their current values, so "best so
far" is meaningful immediately rather than only for rows written after
this runs. ``image_digest`` stays NULL for pre-existing rows — their
digest was never captured and cannot be recovered.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260805_variant_attempt_history.py --commit

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
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


_COLUMNS = [
    ("attempt_count", "INTEGER NOT NULL DEFAULT 1"),
    ("best_score", "REAL"),
    ("image_digest", "VARCHAR(255)"),
    ("attempt_history", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "node_variant_verification"):
        print("Table node_variant_verification absent — run "
              "20260523_variant_verification.py first.")
        return 1

    added = []
    for name, ddl in _COLUMNS:
        if _column_exists(cur, "node_variant_verification", name):
            print(f"Column node_variant_verification.{name} already present.")
            continue
        stmt = (
            f"ALTER TABLE node_variant_verification ADD COLUMN {name} {ddl}"
        )
        if not args.commit:
            print(f"DRY  would: {stmt}")
        else:
            cur.execute(stmt)
            print(f"WRITE {stmt}")
        added.append(name)

    # Backfill. Only meaningful once the columns exist, and only for rows
    # that have not already been backfilled — best_score IS NULL is the
    # marker, since every row written after this has it set.
    backfill = """
        UPDATE node_variant_verification
           SET best_score = score,
               attempt_count = 1,
               attempt_history = jsonb_build_array(
                   jsonb_build_object(
                       'attempt', 1,
                       'score', score,
                       'verdict', verdict,
                       'scored_against', scored_against_variant,
                       'raw_output_ref', raw_output_ref,
                       'image_digest', NULL,
                       'scored_at', to_char(scored_at,
                           'YYYY-MM-DD"T"HH24:MI:SSOF'),
                       'regressed', false
                   )
               )
         WHERE best_score IS NULL
    """
    if not args.commit:
        print("DRY  would: backfill best_score + single-entry "
              "attempt_history for rows where best_score IS NULL")
    else:
        if added or _column_exists(cur, "node_variant_verification",
                                   "best_score"):
            cur.execute(backfill)
            print(f"WRITE backfilled {cur.rowcount} existing "
                  f"verification row(s)")

    print("Done." if args.commit else "Dry run complete. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
