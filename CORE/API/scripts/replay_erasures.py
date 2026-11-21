"""Q3(c) — restore-safety: RE-APPLY privacy erasures after a restore.

A privacy erasure (user/tenant PII scrub + byte purge + crypto-shred) leaves
its trace only in the main database. A backup taken BEFORE that erasure, when
restored, resurrects the erased person's PII. This script is the other half of
the fix: it reads the append-only erasure LEDGER (the JSONL file that survives
a DB restore — see ``services/erasure_ledger.py``) and re-applies every
erasure recorded in it that the restored database no longer reflects.

It calls the SAME functions the daily erasure job uses
(``services/scheduler.py::_erase_user_bytes`` / ``_erase_tenant_bytes``), so
replay does exactly what the original erasure did. Replay:

  * is DRY-RUN by default (loads + reports, writes nothing); pass ``--commit``
    to actually re-apply.
  * is IDEMPOTENT — a subject already erased (``users.erased_at`` /
    ``tenants.bytes_purged_at`` set) is a no-op, so running replay twice does
    not double-scrub or double-purge.
  * RESPECTS LEGAL HOLD the same way the job does — a subject currently under
    an active hold is skipped (its bytes are preserved), matching
    ``services/legal_hold.is_held`` (which fails closed).
  * writes ONE ``ERASURE_REPLAYED`` audit row per replayed subject.

It is hooked into the restore path (``CORE/startup/update-system.sh`` rollback):
after the databases are restored, the restore runs this script in dry-run
mode, prints the summary, then re-runs it with ``--commit``.

The ledger stores ID-only data (subject type + id + timestamp + scope) — no
PII — so replay never needs, and never logs, personal data.

Usage (inside the API container):
    docker exec -w /app my_fastapi_app python3 scripts/replay_erasures.py              # dry-run
    docker exec -w /app my_fastapi_app python3 scripts/replay_erasures.py --commit     # apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _select_database() -> str:
    """Point ``database.initialize`` at a test DB when asked (conftest
    convention). Must run BEFORE ``import database``. Returns a label."""
    url = os.environ.get("TEST_DATABASE_URL")
    name = os.environ.get("TEST_DATABASE_NAME")
    if not url and name:
        password = os.environ.get("POSTGRES_PASSWORD")
        if not password:
            raise SystemExit("TEST_DATABASE_NAME set but POSTGRES_PASSWORD is empty; "
                             "run inside my_fastapi_app or set TEST_DATABASE_URL.")
        user = os.environ.get("POSTGRES_USER", "modTech")
        host = os.environ.get("POSTGRES_HOST", "postgres-app")
        port = os.environ.get("POSTGRES_PORT", "5432")
        url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
    if url:
        os.environ["DATABASE_URL"] = url
        os.environ.pop("VAULT_ADDR", None)
        return f"test DB {url.rsplit('/', 1)[-1]}"
    return f"live DB {os.environ.get('POSTGRES_DB', 'modTech')} (Vault)"


def _row_key(r) -> str:
    return f"{getattr(r, 'subject_type', '?')}:{getattr(r, 'subject_id', '?')}"


def _is_already_erased(row, subject_type: str) -> bool:
    if subject_type == "user":
        return row is not None and row.erased_at is not None
    return row is not None and row.bytes_purged_at is not None


def _is_held(db, row, subject_type: str) -> bool:
    """Same legal-hold check the erasure job runs (fail closed)."""
    from utils.paid_import import optional_paid
    _lhold = optional_paid("services.legal_hold")
    if _lhold is None:  # CE carve: no legal holds exist -> not held
        return False
    if subject_type == "user":
        return _lhold.is_held(db, tenant_id=row.tenant_id, user_id=row.id)
    return _lhold.is_held(db, tenant_id=row.id)


def _apply_one(db, row, subject_type: str) -> None:
    """Re-apply ONE erasure using the job's own functions. The caller has
    already confirmed the subject is not erased and not under hold."""
    import models
    import services.scheduler as scheduler

    if subject_type == "tenant":
        # Restore the tombstone if the pre-erasure restore left the tenant
        # active, so the row is soft-deleted again (the erasure that produced
        # this ledger entry only ever runs on a soft-deleted tenant).
        if row.deleted_at is None:
            row.deleted_at = datetime.now(timezone.utc)
            db.add(row)
        # record_ledger=False: the entry already exists; do not duplicate it.
        scheduler._erase_tenant_bytes(db, row, record_ledger=False)
    else:
        # _erase_user_bytes re-scrubs PII + soft-deletes (via
        # crud.delete_user_permanently when deleted_at is None) and stamps
        # erased_at. No-op if already erased (guarded by the caller).
        scheduler._erase_user_bytes(db, row, record_ledger=False)

    db.add(models.AuditEvent(
        tenant_id=(row.tenant_id if subject_type == "user" else row.id),
        user_id=(row.id if subject_type == "user" else None),
        event_name="ERASURE_REPLAYED",
        details={
            "subject_type": subject_type,
            "subject_id": row.id,
            "reason": "post-restore replay of a recorded privacy erasure",
        },
    ))
    db.commit()


def replay(db, *, ledger_path: str | None = None, dry_run: bool = False) -> dict:
    """Re-apply every ledger-recorded erasure the DB no longer reflects.

    ``db`` is the caller's session (the daily job's own session in prod; the
    ``db_session`` fixture in tests). Returns a summary dict with per-status
    counts and the per-subject breakdown. Never raises on a per-subject
    failure — it rolls back that subject and records it, then continues."""
    # 1788o CE carve: erasure_ledger is internal-only (manifest deny). The script itself
    # ships, so in CE it says so and succeeds — an all-zero summary means the
    # caller prints the normal shape and main() exits 0. Entitlement build unchanged.
    try:
        from services import erasure_ledger as _erledger
    except ImportError:
        print("erasure ledger not available in CE")
        return {
            "path": ledger_path or "n/a",
            "dry_run": dry_run,
            "total": 0,
            "replayed": 0,
            "already_erased": 0,
            "held_skipped": 0,
            "would_replay": 0,
            "missing_subject": 0,
            "failed": 0,
            "details": [],
        }
    import models

    path = ledger_path or _erledger.resolve_path()
    records = _erledger.read_lines(path)

    summary = {
        "path": path,
        "dry_run": dry_run,
        "total": len(records),
        "replayed": 0,
        "already_erased": 0,
        "held_skipped": 0,
        "would_replay": 0,
        "missing_subject": 0,
        "failed": 0,
        "details": [],
    }

    for rec in records:
        subject_type = rec["subject_type"]
        subject_id = int(rec["subject_id"])
        key = f"{subject_type}:{subject_id}"
        entry = {
            "key": key,
            "request_id": rec.get("request_id"),
            "status": None,
        }
        try:
            row = (
                db.query(models.User).filter(models.User.id == subject_id).first()
                if subject_type == "user"
                else db.query(models.Tenant).filter(models.Tenant.id == subject_id).first()
            )
            if row is None:
                entry["status"] = "missing_subject"
                summary["missing_subject"] += 1
                continue

            if _is_already_erased(row, subject_type):
                entry["status"] = "already_erased"
                summary["already_erased"] += 1
                continue

            if _is_held(db, row, subject_type):
                entry["status"] = "held_skipped"
                summary["held_skipped"] += 1
                continue

            if dry_run:
                entry["status"] = "would_replay"
                summary["would_replay"] += 1
                continue

            _apply_one(db, row, subject_type)
            entry["status"] = "replayed"
            summary["replayed"] += 1
        except Exception as e:  # noqa: BLE001 — one bad subject must not abort replay
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            entry["status"] = "failed"
            entry["error"] = str(e)
            summary["failed"] += 1

        summary["details"].append(entry)
    return summary


def _print_summary(s: dict) -> None:
    verb = "WOULD re-apply" if s["dry_run"] else "Re-applied"
    print(f"=== Q3(c) erasure replay ({'dry-run' if s['dry_run'] else 'commit'}) ===")
    print(f"ledger file: {s['path']}")
    print(f"recorded erasures: {s['total']}")
    print(f"{verb}: {s['replayed'] + s['would_replay']}")
    print(f"already erased (no-op): {s['already_erased']}")
    print(f"skipped (legal hold):   {s['held_skipped']}")
    print(f"subject not found:      {s['missing_subject']}")
    print(f"failed:                 {s['failed']}")
    for d in s["details"]:
        if d["status"] in ("replayed", "would_replay", "failed", "held_skipped"):
            extra = f" ({d['error']})" if d.get("error") else ""
            print(f"  - {d['key']}: {d['status']}{extra}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Re-apply privacy erasures recorded in the erasure ledger "
                    "after a restore (dry-run by default).")
    p.add_argument("--commit", action="store_true",
                   help="actually re-apply (default: dry-run only)")
    p.add_argument("--ledger-path", default=None,
                   help="override the ledger file path "
                        f"(default: {os.environ.get('MOD_ERASURE_LEDGER_PATH', 'auto')})")
    args = p.parse_args(argv)

    db_label = _select_database()
    import database

    database.initialize()
    db = database.SessionLocal()
    try:
        summary = replay(db, ledger_path=args.ledger_path, dry_run=not args.commit)
        _print_summary(summary)
        print(f"(database: {db_label})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
