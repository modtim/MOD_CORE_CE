"""MOD Core (open-source) — no-op billing stub.

Core runs workflows free and unlimited; there is no credit system. This module
replaces the Cloud `utils/workflow_credits.py` in the public build (the real
module is denylisted in open-core/manifest.yaml). Signatures match what
`routers/workflows.py` imports so the run path is unchanged.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple


def calculate_workflow_credits(
    db=None, graph: Optional[Dict] = None, cog_ids: Optional[List[int]] = None,
    *args, **kwargs,
) -> Dict:
    """Core is free: every workflow estimates at zero credits."""
    return {"total_credits": 0, "node_costs": {}}


def check_and_reserve_credits(
    db=None, tenant_id: int = 1, workflow_run_id: Optional[int] = None,
    *args, **kwargs,
) -> Tuple[bool, str]:
    """Core is free: always permit, reserve nothing."""
    return True, "MOD Core: unlimited (no credit system)"


def detect_execution_mode(nodes=None, *args, **kwargs) -> str:
    """Batch vs grouped from node `data.batch_mode` — same first-hit rule as
    the Cloud module (pure detection, no pricing). predict-cost
    (workflows.py:759) reads this; the old no-op returned None.
    """
    for node in (nodes or []):
        node_data = node.get("data", {}) or {}
        if node_data.get("batch_mode", "batch") == "grouped":
            return "grouped"
    return "batch"


def compute_workflow_quote(
    db=None, nodes=None, node_input_overrides=None, *args, **kwargs,
) -> Dict:
    """Core is free: a per-run quote is always zero credits.

    Shape matches the Cloud module's return (routers/workflows_domains/
    workflows.py:712-717 reads total_credits/breakdown/confidence; the
    run-path lock at :1974 reads total_credits + breakdown). Never reads
    tenant data, never writes.
    """
    return {"total_credits": 0, "breakdown": [], "confidence": "exact"}


def compute_workflow_node_costs(
    db=None, nodes=None, file_count=1, *args, **kwargs,
) -> Dict:
    """Core is free: the batch-cost summary is zero with an empty node list.

    Shape matches the Cloud module's return (:268 — total_credits + node_costs
    list; predict-cost at :861-872 subscripts both).
    """
    return {"total_credits": 0, "node_costs": []}


def __getattr__(name: str):
    """Any other helper imported from this module is a free/no-op."""
    def _noop(*args, **kwargs):
        return None
    return _noop
