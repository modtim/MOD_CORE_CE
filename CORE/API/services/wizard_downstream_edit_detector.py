"""workflow-agentic-execution-loop §quality-signal — downstream-edit
detector.

For every wizard apply event in a recent time window that linked to
a concrete workflow_version_id, compare the applied graph_json
against the workflow's current latest_version.graph_json. When the
graphs differ, insert a `downstream_edited` signal into
wizard_feedback_event so the ML training loop can weight this row
between 'accepted as-is' (high signal) and 'rejected' (negative).

The job is idempotent in a soft way: a per-feedback-event lookup
suppresses duplicate `downstream_edited` rows for the same
(proposal_id, applied_workflow_version_id) tuple. It DOES re-fire if
the user edits, then edits again — each detector pass captures the
latest state and inserts a fresh signal only when one wasn't
already recorded for that exact applied-version. This keeps the
signal count proportional to the number of *distinct* applies whose
output drifted, not proportional to the number of detector ticks.

The detector window default is 7 days; we look at recent events
rather than the entire history because:
  1. Old workflows are likely to have drifted for legitimate reasons
     unrelated to the wizard's quality (post-launch maintenance).
  2. Edit signal is most informative close to apply time (the user
     was reacting to the wizard's output, not iterating on a
     long-lived workflow).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text as _sa_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _canonical_graph(g: Optional[Dict[str, Any]]) -> str:
    """Canonicalise a graph_json blob for byte-level comparison. Strips
    layout-only fields that change without semantic meaning (positions
    drift when the user pans the canvas; ids are stable). Without
    canonicalisation we'd false-positive on every viewport adjust."""
    if not isinstance(g, dict):
        return ""
    nodes = g.get("nodes") or []
    edges = g.get("edges") or []

    def _strip_node(n: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(n, dict):
            return n
        # Keep the structural fields. Drop position + UI hints.
        keep_keys = {"id", "type", "data"}
        clean = {k: v for k, v in n.items() if k in keep_keys}
        # Inside data, drop UI-only fields too (label colours,
        # collapsed flags, etc. that don't change graph behavior).
        if isinstance(clean.get("data"), dict):
            data = clean["data"]
            ui_only = {"label_color", "collapsed", "selected",
                       "position", "x", "y"}
            clean["data"] = {k: v for k, v in data.items() if k not in ui_only}
        return clean

    def _strip_edge(e: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(e, dict):
            return e
        keep_keys = {"id", "source", "target",
                     "sourceHandle", "targetHandle"}
        return {k: v for k, v in e.items() if k in keep_keys}

    clean_nodes = sorted(
        [_strip_node(n) for n in nodes],
        key=lambda x: x.get("id") or "",
    )
    clean_edges = sorted(
        [_strip_edge(e) for e in edges],
        key=lambda x: x.get("id") or "",
    )
    return json.dumps(
        {"nodes": clean_nodes, "edges": clean_edges},
        sort_keys=True, separators=(",", ":"),
    )


def detect_downstream_edits(
    db: Session,
    *,
    window_days: int = 7,
) -> Tuple[int, int]:
    """Scan recent successful applies and insert `downstream_edited`
    signals for any whose graph has drifted from the applied version.

    Returns (rows_scanned, rows_inserted).

    Scope:
      - action = 'accepted' (only successful applies leave a workflow
        behind to drift)
      - applied_workflow_version_id IS NOT NULL
      - created_at within the window
      - no prior `downstream_edited` signal for this proposal_id +
        applied_workflow_version_id

    Per row:
      - Load the applied WorkflowVersion's graph_json (the snapshot
        at apply time)
      - Load the parent Workflow's current latest_version.graph_json
      - Compare canonically; if different and the workflow isn't
        soft-deleted, insert a downstream_edited signal.

    Best-effort: per-row failures log + continue. Returns total
    inserted at the end. Caller is the APScheduler tick.
    """
    rows_scanned = 0
    rows_inserted = 0

    try:
        applies = db.execute(_sa_text(f"""
            SELECT wfe.id, wfe.tenant_id, wfe.proposal_id, wfe.apply_mode,
                   wfe.intent, wfe.applied_workflow_version_id
            FROM wizard_feedback_event wfe
            WHERE wfe.action = 'accepted'
              AND wfe.applied_workflow_version_id IS NOT NULL
              AND wfe.created_at >= now() - INTERVAL '{int(window_days)} days'
              AND NOT EXISTS (
                  SELECT 1 FROM wizard_feedback_event existing
                  WHERE existing.action = 'downstream_edited'
                    AND existing.proposal_id = wfe.proposal_id
                    AND existing.applied_workflow_version_id =
                        wfe.applied_workflow_version_id
              )
        """)).fetchall()
    except Exception as e:
        logger.warning("downstream_edit_detector: scan query failed: %s", e)
        return (0, 0)

    for row in applies:
        rows_scanned += 1
        try:
            wfe_id, tenant_id, proposal_id, apply_mode, intent, version_id = row

            applied = db.execute(_sa_text("""
                SELECT v.graph_json, v.workflow_id
                FROM workflow_versions v
                WHERE v.id = :vid
            """), {"vid": version_id}).fetchone()
            if applied is None:
                # Version got deleted — treat as deleted signal? No,
                # the explicit 'deleted' signal comes from the FE on
                # soft-delete; skipping here is the right move.
                continue

            applied_graph, workflow_id = applied
            current = db.execute(_sa_text("""
                SELECT v.graph_json, w.deleted_at
                FROM workflows w
                LEFT JOIN workflow_versions v ON v.id = w.latest_version_id
                WHERE w.id = :wid
            """), {"wid": workflow_id}).fetchone()
            if current is None:
                continue
            current_graph, deleted_at = current
            if deleted_at is not None:
                # Workflow soft-deleted — that's a separate signal.
                continue

            if _canonical_graph(applied_graph) == _canonical_graph(current_graph):
                continue  # no drift

            # Drift detected — insert downstream_edited signal.
            from services.wizard_feedback import record_quality_signal
            inserted_id = record_quality_signal(
                db,
                tenant_id=tenant_id,
                proposal_id=proposal_id,
                apply_mode=apply_mode or "replace",
                action="downstream_edited",
                applied_workflow_version_id=version_id,
                intent=intent,
                feedback_event_metadata={
                    "source": "downstream_edit_detector",
                    "parent_feedback_event_id": wfe_id,
                },
            )
            if inserted_id is not None:
                rows_inserted += 1
        except Exception as e:
            logger.warning(
                "downstream_edit_detector: per-row failure (continuing): %s",
                e,
            )
            try:
                db.rollback()
            except Exception:
                pass

    if rows_inserted:
        logger.info(
            "downstream_edit_detector: scanned %d, inserted %d signals",
            rows_scanned, rows_inserted,
        )
    return (rows_scanned, rows_inserted)
