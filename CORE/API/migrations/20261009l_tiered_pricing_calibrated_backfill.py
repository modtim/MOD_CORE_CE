"""sz-tcp.1 — retroactive calibrated-branch backfill for tiered credit pricing.

20260513_tiered_credit_pricing.py's retroactive backfill only knew two
buckets: ``cost_in_credits == 0`` -> ``cost_basis='free'``, everything else
-> ``cost_basis='estimated'`` with ratio-scaled tiers (0.5x/1x/2.5x). Any
NodeDefinition that was ALREADY measured (``calibration_status='calibrated'``)
at the time that migration ran got swept into the 'estimated' bucket right
alongside never-measured nodes — it never received the measured-cost
formula the live calibration hook
(``scheduler.pattern_updater._auto_calculate_credit_cost``) applies to every
node that calibrates *after* the tiered-pricing columns exist.

This migration finds that leftover population —
``calibration_status='calibrated' AND cost_basis='estimated'`` — and reuses
``_auto_calculate_credit_cost`` (same GPU/CPU/RAM-rate formula, same
``apply_node_cost()`` tier derivation via ``scaled_tiers()``) to compute real
per-tier credits from each node's recorded calibration task/resource-sample
rows. This is intentionally the SAME formula the live hook uses, not a
reinvention — see utils/node_cost.py + scheduler/pattern_updater.py.

Nodes whose calibration task rows no longer exist (pruned) or produced no
usable resource samples are left untouched by the formula itself — not
every 'estimated'+'calibrated' row necessarily has surviving evidence to
recompute from, and this migration never guesses a replacement value.

Each node that actually gets recomputed (its ``cost_basis`` flips to
'calibrated') logs an append-only ``AuditEvent(event_name='cost.recompute')``
with the before/after cost + tiers — ISO 27001: no silent cost changes.

Idempotent: a second run finds zero candidates (the filter excludes rows
already flipped to ``cost_basis='calibrated'``), so it never recomputes
twice or double-writes an audit event.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261009l_tiered_pricing_calibrated_backfill.py
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261009l_tiered_pricing_calibrated_backfill.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    return _db.SessionLocal()


def _candidates(session, models):
    """NodeDefinition rows measured as 'calibrated' but still stamped with
    the naive 'estimated' basis left by 20260513_tiered_credit_pricing.py's
    retroactive backfill."""
    NodeDefinition = models.NodeDefinition
    return session.query(NodeDefinition).filter(
        NodeDefinition.calibration_status == "calibrated",
        NodeDefinition.cost_basis == "estimated",
    ).all()


def recompute_calibrated_nodes(session, models) -> list[dict]:
    """Recompute cost_credits_by_tier for every candidate node via the SAME
    formula the live calibration hook uses
    (scheduler.pattern_updater._auto_calculate_credit_cost). Returns one
    change-record per node that actually recomputed (basis flipped to
    'calibrated'). Does NOT write AuditEvents or commit — callers decide
    whether this is a dry-run preview or a real apply.
    """
    from scheduler import pattern_updater

    changes = []
    for node in _candidates(session, models):
        old_cost = node.cost_in_credits
        old_tiers = dict(node.cost_credits_by_tier or {})
        old_basis = node.cost_basis

        pattern_updater._auto_calculate_credit_cost(node, session)

        if node.cost_basis == "calibrated" and node.cost_basis != old_basis:
            changes.append({
                "node_id": node.id,
                "tenant_id": node.tenant_id,
                "old_cost_in_credits": old_cost,
                "new_cost_in_credits": node.cost_in_credits,
                "old_cost_credits_by_tier": old_tiers,
                "new_cost_credits_by_tier": dict(node.cost_credits_by_tier or {}),
            })
    return changes


def _write_audit_events(session, models, changes: list[dict]) -> None:
    """One append-only cost.recompute AuditEvent per recomputed node.

    b1fbebaf0 (sz-tcp.14) made pattern_updater._auto_calculate_credit_cost
    audit the recompute itself, so the backfill must not write a second row
    for the same node — ISO 27001 wants exactly one event per cost change.
    We only write here when the hook did not (e.g. tier columns absent,
    where the hook returns before auditing), preserving this migration's
    more precise 'tiered_credit_pricing_calibrated_backfill' provenance.
    """
    AuditEvent = models.AuditEvent
    for change in changes:
        already = session.query(AuditEvent).filter(
            AuditEvent.event_name == "cost.recompute",
            AuditEvent.details["node_definition_id"].as_string()
            == str(change["node_id"]),
        ).count()
        if already:
            continue
        session.add(AuditEvent(
            tenant_id=change["tenant_id"],
            user_id=None,
            event_name="cost.recompute",
            details={
                "node_definition_id": change["node_id"],
                "reason": "tiered_credit_pricing_calibrated_backfill",
                "old_cost_in_credits": change["old_cost_in_credits"],
                "new_cost_in_credits": change["new_cost_in_credits"],
                "old_cost_credits_by_tier": change["old_cost_credits_by_tier"],
                "new_cost_credits_by_tier": change["new_cost_credits_by_tier"],
                "cost_basis": "calibrated",
            },
        ))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="apply the recompute (default is dry-run)")
    args = ap.parse_args()

    session = _get_session()
    import models  # noqa: F401

    try:
        preview = _candidates(session, models)
        print(f"{len(preview)} node(s) calibrated but stamped cost_basis='estimated'")
        for node in preview[:10]:
            print(f"    id={node.id:<5} {node.name[:44]:<44} cost={node.cost_in_credits}")
        if len(preview) > 10:
            print(f"    ... and {len(preview) - 10} more")

        if not preview:
            print("nothing to recompute.")
            return 0

        if not args.commit:
            print(
                f"\n(dry-run) would recompute {len(preview)} node(s) via "
                "pattern_updater._auto_calculate_credit_cost and log one "
                "cost.recompute AuditEvent per successful recompute. "
                "Re-run with --commit to apply."
            )
            return 0

        changes = recompute_calibrated_nodes(session, models)
        _write_audit_events(session, models, changes)
        session.commit()
        print(
            f"\nrecomputed {len(changes)}/{len(preview)} node(s); "
            f"{len(preview) - len(changes)} had no usable calibration "
            "samples and were left untouched."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
