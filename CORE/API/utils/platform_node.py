"""Platform-node classification — single source of truth.

A node is a "platform node" (i.e. shipped with the system, vetted at
build time, exempt from the runtime trivy scan gate) when:

  - its tenant_id IS NULL (existing convention used by seed_converters.py
    and the system_only flag), OR
  - as a defensive fallback, its name starts with "system." (catches any
    legacy or mis-seeded row whose tenant_id was set in error)

The convention here is consumed by:
  - services/workflow_orchestrator.py — bypass the scan-status gate
  - the API startup routine — idempotent backfill of stuck pending_scan
    rows back to "approved"
  - the WorkflowBuilder node-library client — route under "Platform
    nodes" vs "My nodes" (the JS mirror of this predicate lives in
    CORE/WorkflowBuilder)
"""

from __future__ import annotations


def is_platform_node(node_def) -> bool:
    """True if `node_def` is a platform-shipped node.

    Accepts a NodeDefinition ORM row. Defensive against missing
    attributes (returns False rather than raising) so callers can lean
    on it inside hot paths without try/except.
    """
    if node_def is None:
        return False
    tenant_id = getattr(node_def, "tenant_id", "missing")
    if tenant_id is None:
        return True
    name = getattr(node_def, "name", None) or ""
    return name.startswith("system.")
