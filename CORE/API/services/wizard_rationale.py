"""workflow-expressiveness-primitives §5.5/§5.6 (sz-expr2.4) — wizard
self-explanation rationale collector.

workflow-wizard-self-explanation says every wizard-composed workflow ships
a structured `rationale` field: [{decision_point, chosen_option,
alternatives_considered, reason}]. The collector captures those decision
points THROUGHOUT the agentic loop (topology decisions first, then node
selection, then config) and the final wizard response emits them; the
panel renders them under "Why this workflow?".

This module is the PERSISTENCE half only — the durable per-decision row
(`wizard_rationale`, tasks.md §5.6). Nothing in the wizard pipeline calls
it yet; wiring the collector into the agentic loop is a later lead step.

Public surface:
- record_rationale(db, tenant_id, *, step, rationale, ...) -> Optional[int]
    # returns the new row's id; None on insert failure (logged, not raised)
- list_rationale(db, tenant_id, *, wizard_session_id=, build_spec_id=)
    # tenant filter MANDATORY; soft-deleted rows excluded

Tenant scoping is not optional: design reasoning is tenant-private (a
tenant's benchmarks, its test-run quality scores, its own workflows
appear in the reason text), so every read path filters on tenant_id and
there is no platform-wide list here.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def record_rationale(
    db: Session,
    tenant_id: int,
    *,
    step: str,
    rationale: str,
    user_id: Optional[int] = None,
    wizard_session_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
) -> Optional[int]:
    """Write one wizard decision-point row and return its id.

    `step` is the decision-point label ('image-upscale-node-choice',
    'edge-A-to-B-port-type-bridge', ...); `rationale` is the human-
    readable reason the agent gave for the choice. Both are required by
    the table (NOT NULL) — an empty one is a collector bug, so we bail
    rather than persist a row the panel can't render.

    Returns None on insert failure: the wizard response must not depend
    on persistence succeeding (same best-effort contract as the
    feedback-event writes).
    """
    if tenant_id is None or not step or not rationale:
        logger.warning(
            "wizard_rationale: skipping row with missing tenant/step/"
            "rationale (tenant_id=%s step=%r)",
            tenant_id, step,
        )
        return None

    import models
    row = models.WizardRationale(
        tenant_id=tenant_id,
        user_id=user_id,
        wizard_session_id=wizard_session_id,
        build_spec_id=build_spec_id,
        step=str(step)[:64],
        rationale=rationale,
    )
    db.add(row)
    try:
        db.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning("wizard_rationale insert failed: %s", e)
        return None
    return row.id


def list_rationale(
    db: Session,
    tenant_id: int,
    *,
    wizard_session_id: Optional[int] = None,
    build_spec_id: Optional[int] = None,
):
    """Return this tenant's live rationale rows, oldest first.

    The tenant filter is MANDATORY — there is no cross-tenant read path
    here. Soft-deleted rows are always excluded (ISO 27001: they exist,
    they are just no longer evidence the panel may show).

    Pass `wizard_session_id` or `build_spec_id` to scope to one wizard
    conversation / one node build; pass neither to get the tenant's
    whole history (the response-payload assembly path reads one turn's
    rows, the audit/debug path reads all of them).
    """
    if tenant_id is None:
        # No unscoped read path exists by design: rationale text quotes a
        # tenant's own benchmarks and workflows.
        return []

    import models
    q = (
        db.query(models.WizardRationale)
        .filter(models.WizardRationale.tenant_id == tenant_id)
        .filter(models.WizardRationale.deleted_at.is_(None))
    )
    if wizard_session_id is not None:
        q = q.filter(
            models.WizardRationale.wizard_session_id == wizard_session_id
        )
    if build_spec_id is not None:
        q = q.filter(
            models.WizardRationale.build_spec_id == build_spec_id
        )
    return q.order_by(models.WizardRationale.created_at,
                      models.WizardRationale.id).all()
