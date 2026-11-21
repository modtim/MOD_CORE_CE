"""Q15 (owner decision, 2026-10-03) — REMOVE the FaceFusion face-swap node.

Reason (owner): the face-swap models (InsightFace ``inswapper`` family) carry a
non-commercial licence, and face-swap / deepfake carries deepfake liability.
The node is removed everywhere (SaaS and on-prem).

ISO 27001 HARD RULE — no hard deletes:
  * This migration NEVER ``DELETE``s a row. It SOFT-deletes the FaceFusion
    ``NodeDefinition`` row(s) — the system definition (``tenant_id IS NULL``)
    AND every tenant palette copy — by stamping the model's existing soft-delete
    columns (``NodeDefinition.deleted_at`` + ``deleted_by_user_id`` via
    ``SoftDeleteWithActorMixin``). The rows remain in the table, invisible to
    listings / palette / lookup-by-name via the ``with_loader_criteria`` filter
    in ``database.py``.
  * It only touches rows whose name matches ``facefusion`` (case-insensitive).
    No other node is touched.
  * It writes ONE audit row per tenant affected, through the EXISTING audit
    helper (``audit.log_soft_delete`` → ``CONTENT_DELETE_INITIATED``). No new
    ``AuditAction`` enum value is added.
  * It does not delete Docker images or containers. The source dir
    ``CORE/DockerContainers/FaceFusion`` is left on disk (and added to the
    build-exclusion in ``CORE/startup/update-system.sh`` Phase 3.7).

Idempotent: a row already soft-deleted (``deleted_at`` set) is never re-stamped
and never re-audited, so a second run is a no-op.

Dry run (default, no --commit) PRINTS the rows that would be soft-deleted
(id, name, tenant) and exits 0 WITHOUT touching the database. ``--commit``
applies.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003h_remove_facefusion_node.py            # dry run: prints rows
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003h_remove_facefusion_node.py --commit   # apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# Canonical strings shared with the run-path message (workflow_orchestrator).
REMOVED_NODE_NAME = "FaceFusion"
REMOVED_NODE_DATE = "2026-10-03"


def _match_name(name) -> bool:
    """True iff a NodeDefinition name is the FaceFusion node (case-insensitive)."""
    return bool(name) and str(name).strip().lower() == REMOVED_NODE_NAME.lower()


def _resolve_audit_actor(db, models, tenant_id):
    """Pick a real user id under whose authority the audit event is recorded.

    Mirrors ``services.scheduler._resolve_audit_actor_for_tenant``: the tenant's
    first platform admin, then any non-deleted user in the tenant, then the
    platform-wide first admin. Returns ``None`` when no candidate exists (the
    caller still soft-deletes; the audit row is simply skipped — a soft-delete
    without an actor is still an ISO-safe soft-delete).
    """
    try:
        if tenant_id is not None:
            admin = (
                db.query(models.User)
                .filter(
                    models.User.tenant_id == tenant_id,
                    models.User.is_platform_admin.is_(True),
                )
                .order_by(models.User.id)
                .first()
            )
            if admin is not None:
                return int(admin.id)
            any_user = (
                db.query(models.User)
                .filter(models.User.tenant_id == tenant_id)
                .order_by(models.User.id)
                .first()
            )
            if any_user is not None:
                return int(any_user.id)
        platform_admin = (
            db.query(models.User)
            .filter(models.User.is_platform_admin.is_(True))
            .order_by(models.User.id)
            .first()
        )
        if platform_admin is not None:
            return int(platform_admin.id)
    except Exception:  # noqa: BLE001 — actor resolution must never abort the sweep
        pass
    return None


def _candidate_rows(db, models):
    """Every FaceFusion NodeDefinition row, INCLUDING already-soft-deleted ones.

    The ``with_loader_criteria`` filter in ``database.py`` hides ``deleted_at``
    rows, so we must opt back in with ``include_deleted=True`` to (a) report the
    true live count in the dry run and (b) make the apply idempotent.
    """
    nd = models.NodeDefinition
    rows = db.query(nd).execution_options(include_deleted=True).all()
    return [r for r in rows if _match_name(r.name)]


def _find_rows(db, models):
    """Return (to_delete, already) lists of FaceFusion rows.

    ``to_delete`` = live rows (``deleted_at IS NULL``) that need the stamp;
    ``already`` = rows already soft-deleted (idempotent no-op set).
    """
    to_delete, already = [], []
    for r in _candidate_rows(db, models):
        if r.deleted_at is None:
            to_delete.append(r)
        else:
            already.append(r)
    return to_delete, already


def remove_facefusion_nodes(db, models, *, commit: bool = False) -> dict:
    """Soft-delete the FaceFusion node definition + all tenant copies.

    ``commit=False`` (dry run / test) stamps the rows in-memory but does NOT
    commit — the caller decides persistence (the test's SAVEPOINT, or the
    ``--commit`` path). ``commit=True`` commits.

    Returns a summary dict: ``{"system": n, "tenant": n, "already_soft_deleted":
    n, "audits": n, "rows": [(id, name, tenant_id, system_or_tenant), ...]}``.
    """
    to_delete, already = _find_rows(db, models)

    now = datetime.now(timezone.utc)
    rows = []
    audits = 0
    for r in to_delete:
        actor = _resolve_audit_actor(db, models, r.tenant_id)
        # Soft-delete via the model's own mechanism (SoftDeleteWithActorMixin).
        r.deleted_at = now
        if actor is not None:
            r.deleted_by_user_id = actor
        kind = "system" if r.tenant_id is None else "tenant"
        rows.append((r.id, r.name, r.tenant_id, kind))

        # ONE audit row per affected tenant/row, via the EXISTING helper.
        try:
            import audit  # type: ignore
            audit.log_soft_delete(
                db=db,
                actor_user_id=actor,
                tenant_id=r.tenant_id if r.tenant_id is not None else 0,
                resource_type="node_definition",
                resource_id=r.id,
                metadata={
                    "node_name": r.name,
                    "reason": (
                        f"{REMOVED_NODE_NAME} was removed from MOD on "
                        f"{REMOVED_NODE_DATE} (Q15): non-commercial InsightFace "
                        f"inswapper licence + deepfake liability"
                    ),
                    "image_ref": r.executable_path or "",
                    "kind": kind,
                },
            )
            audits += 1
        except Exception:  # noqa: BLE001 — audit must never break the sweep
            pass

    summary = {
        "system": sum(1 for _, _, t, k in rows if k == "system"),
        "tenant": sum(1 for _, _, t, k in rows if k == "tenant"),
        "already_soft_deleted": len(already),
        "audits": audits,
        "rows": rows,
    }
    if commit:
        db.commit()
    return summary


# ---------------------------------------------------------------------------
# Standalone CLI (dry-run / --commit) for the live DB.
# ---------------------------------------------------------------------------

def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import database as _db  # type: ignore
    import models  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    from sqlalchemy.orm import sessionmaker  # type: ignore
    Session = sessionmaker(bind=eng)
    return Session(), models


def main() -> int:
    p = argparse.ArgumentParser(description="Q15 — soft-delete the FaceFusion node")
    p.add_argument("--commit", action="store_true", help="apply (default is dry run)")
    args = p.parse_args()

    db, models = _get_session()
    try:
        to_delete, already = _find_rows(db, models)

        print("=" * 74)
        print(f"Q15 FaceFusion removal — {'APPLY' if args.commit else 'DRY RUN'}")
        print(f"  removed on {REMOVED_NODE_DATE}; non-commercial inswapper licence")
        print("=" * 74)
        print(f"FaceFusion NodeDefinition rows found (incl. already soft-deleted):")
        for r in _candidate_rows(db, models):
            state = "already soft-deleted" if r.deleted_at is not None else "LIVE (will be soft-deleted)"
            kind = "system" if r.tenant_id is None else f"tenant {r.tenant_id}"
            print(f"   id={r.id:<6} name={r.name!r:<24} {kind:<16} [{state}]")

        print()
        print(f"Will soft-delete: {len(to_delete)} row(s)  "
              f"(system={sum(1 for r in to_delete if r.tenant_id is None)}, "
              f"tenant copies={sum(1 for r in to_delete if r.tenant_id is not None)})")
        print(f"Already soft-deleted (no-op): {len(already)} row(s)")

        if not args.commit:
            print("\n(dry-run; no writes. pass --commit to apply)")
            return 0

        summary = remove_facefusion_nodes(db, models, commit=True)
        print(f"\nAPPLIED — soft-deleted {len(summary['rows'])} row(s) "
              f"(system={summary['system']}, tenant={summary['tenant']}), "
              f"audit rows written: {summary['audits']}")
        for rid, name, tenant, kind in summary["rows"]:
            print(f"   ✓ id={rid} name={name!r} {kind} tenant={tenant}")
        print("\nDone. Rows remain in node_definitions with deleted_at set (no hard delete).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
