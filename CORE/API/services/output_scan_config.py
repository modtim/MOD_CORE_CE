"""Fortress 3.7a — per-tenant / per-workflow scanner config store (raw SQL).

No ORM model yet — all queries via ``sqlalchemy.text``. The migration
(20261012r_output_scan_configs.py) creates the backing table.
"""
from __future__ import annotations

from typing import Any, Optional, Set

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services import output_safety_scanners as _scanners


# ---------------------------------------------------------------------------
# known_scanners — live, import-order independent accessor
# ---------------------------------------------------------------------------
# The registry (output_safety_scanners._REGISTRY) is populated at import time
# by every scanner module (built-ins + commercial adapters).  This frozenset
# is computed *at call time* so it is never stale regardless of which module
# imports first.


def known_scanners() -> frozenset[str]:
    """Return the full set of registered scanner names.

    Read directly from the live registry so commercial scanners
    (e.g. ``hive_ai``) are included even when ``output_scan_config`` is
    imported *before* ``commercial_scanners``.
    """
    return frozenset(_scanners._REGISTRY.keys())  # noqa: SLF001


# Back-compat: ``osc.KNOWN_SCANNERS`` still resolves to the live set.
# Module-level __getattr__ is called on every attribute access that is not
# found in the normal module namespace, so existing code that references
# KNOWN_SCANNERS gets the live value transparently.
def __getattr__(name: str) -> frozenset[str]:
    if name == "KNOWN_SCANNERS":
        return known_scanners()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def set_scanner(
    db: Any,
    *,
    tenant_id: int,
    workflow_id: Optional[int],
    scanner: str,
    enabled: bool,
    user_id: Optional[int] = None,
) -> None:
    """Enable / disable a scanner for a tenant or tenant+workflow.

    Validates *scanner* against the registry.  Soft-deletes the existing
    live row so history is kept (ISO 27001), then inserts the new value.
    Does NOT commit — the caller must ``db.commit()``.

    CALLER RESPONSIBILITY: *workflow_id*, when given, is not validated here
    to belong to *tenant_id* (no FK through a join without a second query on
    this hot path) — the router/crud layer calling this MUST load the
    workflow through its normal tenant-scoped lookup first so a cross-tenant
    workflow_id can never be written here.
    """
    if scanner not in known_scanners():
        raise ValueError(
            f"unknown scanner {scanner!r}; known: {sorted(known_scanners())}"
        )

    wf_id = workflow_id if workflow_id is not None else 0

    def _soft_delete_live() -> None:
        db.execute(
            text("""
                UPDATE output_scan_configs
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE tenant_id = :tid
                  AND COALESCE(workflow_id, 0) = :wid
                  AND scanner = :scn
                  AND deleted_at IS NULL
            """),
            {"tid": tenant_id, "wid": wf_id, "scn": scanner},
        )

    def _insert() -> None:
        db.execute(
            text("""
                INSERT INTO output_scan_configs
                    (tenant_id, workflow_id, scanner, enabled, updated_by,
                     created_at, updated_at)
                VALUES (:tid, :wid, :scn, :en, :uid, NOW(), NOW())
            """),
            {
                "tid": tenant_id,
                "wid": workflow_id,
                "scn": scanner,
                "en": enabled,
                "uid": user_id,
            },
        )

    # Race-safe: two concurrent set_scanner calls for the same
    # (tenant_id, workflow_id, scanner) can both pass the soft-delete step
    # (no live row left to match) and then both try to INSERT a live row.
    # The partial unique index (tenant_id, COALESCE(workflow_id,0), scanner)
    # WHERE deleted_at IS NULL lets exactly one INSERT win; the loser's
    # IntegrityError is caught in a SAVEPOINT (never crashes the caller's
    # transaction) and retried once: it re-soft-deletes the winner's row
    # (now live) and inserts its own value, so the LAST writer's value is
    # always the one left live -- no duplicate live rows, no lost update.
    _soft_delete_live()
    try:
        with db.begin_nested():
            _insert()
    except IntegrityError:
        _soft_delete_live()
        with db.begin_nested():
            _insert()


def clear_scanner(
    db: Any,
    *,
    tenant_id: int,
    workflow_id: Optional[int],
    scanner: str,
) -> None:
    """Soft-delete one config row so the effective value reverts to the
    tenant-level (or the platform default if nothing remains).

    Does NOT commit.
    """
    wf_id = workflow_id if workflow_id is not None else 0
    db.execute(
        text("""
            UPDATE output_scan_configs
            SET deleted_at = NOW(), updated_at = NOW()
            WHERE tenant_id = :tid
              AND COALESCE(workflow_id, 0) = :wid
              AND scanner = :scn
              AND deleted_at IS NULL
        """),
        {"tid": tenant_id, "wid": wf_id, "scn": scanner},
    )


def effective_enabled(
    db: Any,
    *,
    tenant_id: int,
    workflow_id: Optional[int] = None,
) -> Optional[Set[str]]:
    """Return the set of scanner names that are enabled at the *effective*
    level (tenant rows overlaid by an optional workflow row).

    Returns ``None`` when the tenant has **zero** live rows at all — the
    caller should then fall back to the platform posture (
    ``compliance.output_safety_scan_enabled``).

    Workflow-level rows take precedence over tenant-level for the same
    scanner.
    """
    wf_id = workflow_id if workflow_id is not None else 0

    # Check whether the tenant has any live rows (tenant-level or workflow).
    count = db.execute(
        text("""
            SELECT COUNT(*) FROM output_scan_configs
            WHERE tenant_id = :tid AND deleted_at IS NULL
        """),
        {"tid": tenant_id},
    ).scalar_one()

    if count == 0:
        return None

    # Collect tenant-level rows (workflow_id IS NULL).
    tenant_rows = db.execute(
        text("""
            SELECT scanner, enabled FROM output_scan_configs
            WHERE tenant_id = :tid
              AND (workflow_id IS NULL OR COALESCE(workflow_id, 0) = 0)
              AND deleted_at IS NULL
        """),
        {"tid": tenant_id},
    ).fetchall()

    # Collect workflow-level rows (if a workflow was specified).
    wf_rows: list[tuple[str, bool]] = []
    if workflow_id is not None:
        wf_rows = db.execute(
            text("""
                SELECT scanner, enabled FROM output_scan_configs
                WHERE tenant_id = :tid
                  AND COALESCE(workflow_id, 0) = :wid
                  AND deleted_at IS NULL
            """),
            {"tid": tenant_id, "wid": wf_id},
        ).fetchall()

    # Overlay: workflow overrides tenant.
    enabled: set[str] = set()
    for scanner, en in tenant_rows:
        if en:
            enabled.add(scanner)
    for scanner, en in wf_rows:
        if en:
            enabled.add(scanner)
        else:
            # workflow explicitly disables → remove from enabled
            enabled.discard(scanner)

    return enabled
