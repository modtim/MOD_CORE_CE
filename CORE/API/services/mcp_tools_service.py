"""sz-mcp-per-workflow.3 (mcp-fleet §per-workflow) — MCP tools/list business logic.

``list_exposed_tools(db, tenant_id)`` returns the MCP ``tools/list`` rows for ONE
tenant: every ``WorkflowMcpExposure`` with ``enabled IS TRUE`` whose referenced
Workflow is not soft-deleted and lives in that tenant, folded into the MCP tool
shape (name, description, inputSchema, outputSchema, workflow_id) with the
schemas derived from the workflow's latest version graph.

Tenant isolation is enforced HERE, not in the router: every query is scoped to
``tenant_id`` (exposure rows directly; workflow/version/node rows only ever via
workflow ids that already passed the tenant + soft-delete filter).  The caller's
tenant comes solely from the authenticated user — there is no query-param or
header override of any kind.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import models
from services import platform_settings as psr  # leaf module: no audit cycle
from services.workflow_schema_export import build_workflow_mcp_schema

logger = logging.getLogger(__name__)

MAX_TOOLS = 500
DESCRIPTION_MAX = 1000

# mcpt.a6 — tenant feature flag gating the whole MCP-tools surface (list + call).
PUBLISH_ENABLED_KEY = "mcp_tools.workflow_publish_mcp_enabled"
PUBLISH_GROUP = "mcp_tools"


def publish_enabled(db, tenant_id) -> bool:
    """True when MCP publishing is turned on for ``tenant_id``.

    Registry precedence (tenant row > platform row > registered default), read
    exactly like ``services/auto_build_settings.py:auto_node_build_enabled``
    reads its per-tenant bool. ``tenant_id=None`` asks the platform-wide
    answer only. A settings-read failure returns False — publishing is fail-closed:
    a broken settings layer must never expose (or let a caller invoke) another
    tenant's workflows as MCP tools.
    """
    try:
        return bool(psr.get_setting(PUBLISH_ENABLED_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — fail closed (off), never open
        logger.warning("mcp_tools: publish-flag read failed; keeping MCP publishing OFF")
        return False


def _register_settings() -> None:
    psr.register(
        PUBLISH_ENABLED_KEY,
        bool,
        False,
        group=PUBLISH_GROUP,
        description="mcpt.a6 — publishing workflows as MCP tools is opt-in per "
                    "tenant: when False, GET /mcp-tools/tools/list returns no "
                    "tools and POST /mcp-tools/tools/call answers the generic "
                    "not-found trap for every name (anti-enumeration). Default "
                    "False at first deploy, so an existing exposure stays dark "
                    "until an operator turns it on. Per-tenant overrides win "
                    "over the platform row.",
    )


_register_settings()


def list_exposed_tools(db, tenant_id) -> List[Dict[str, Any]]:
    """List enabled MCP exposures as MCP tools, scoped to ``tenant_id``.

    Returns a list of dicts ``{name, description, inputSchema, outputSchema,
    workflow_id}`` sorted by ``name``, capped at ``MAX_TOOLS`` (500).
    Never raises: per-workflow schema failures are logged and skipped.
    """
    tenant_id = int(tenant_id)

    exposures = (
        db.query(models.WorkflowMcpExposure)
        .filter(
            models.WorkflowMcpExposure.tenant_id == tenant_id,
            models.WorkflowMcpExposure.enabled == True,  # noqa: E712
            models.WorkflowMcpExposure.workflow_id.isnot(None),
        )
        .all()
    )

    # Bulk-load the live workflows for this tenant (tenant filter + soft-delete
    # gate). Exposures pointing at another tenant's or deleted workflow die here.
    wf_by_id: Dict[int, Any] = {}
    for wf in db.query(models.Workflow).filter(
        models.Workflow.tenant_id == tenant_id,
        models.Workflow.deleted_at.is_(None),
    ).all():
        wf_by_id[wf.id] = wf

    live_ids = [exp.workflow_id for exp in exposures if exp.workflow_id in wf_by_id]
    if not live_ids:
        return []

    # Latest version per workflow (highest id) in one bulk query.
    latest_version: Dict[int, Any] = {}
    for ver in db.query(models.WorkflowVersion).filter(
        models.WorkflowVersion.workflow_id.in_(live_ids)
    ).all():
        cur = latest_version.get(ver.workflow_id)
        if cur is None or ver.id > cur.id:
            latest_version[ver.workflow_id] = ver

    # Node ids per workflow in one bulk query, then one bulk catalog load.
    node_ids_by_wf: Dict[int, set] = {}
    for wn in db.query(models.WorkflowNode).filter(
        models.WorkflowNode.workflow_id.in_(live_ids)
    ).all():
        if wn.node_definition_id is not None:
            node_ids_by_wf.setdefault(wn.workflow_id, set()).add(wn.node_definition_id)

    all_nd_ids = set()
    for ids in node_ids_by_wf.values():
        all_nd_ids |= ids

    catalog_all: Dict[int, Any] = {}
    if all_nd_ids:
        for nd in db.query(models.NodeDefinition).filter(
            models.NodeDefinition.id.in_(all_nd_ids)
        ).all():
            catalog_all[nd.id] = nd

    tools: List[Dict[str, Any]] = []
    for exp in exposures:
        wf_id = exp.workflow_id
        wf = wf_by_id.get(wf_id)
        if wf is None:
            continue

        version = latest_version.get(wf_id)
        if version is None or version.graph_json is None:
            logger.warning("mcp_tools: no version graph for workflow %s", wf_id)
            continue

        node_catalog = {
            nid: catalog_all[nid]
            for nid in node_ids_by_wf.get(wf_id, set())
            if nid in catalog_all
        }

        try:
            schema = build_workflow_mcp_schema(version.graph_json, node_catalog)
        except Exception:  # defensive: builder degrades to {} but never raises
            logger.warning("mcp_tools: schema derivation failed for workflow %s", wf_id)
            continue

        # Workflow has no description column — name is the human label.
        desc = wf.name or f"Workflow {wf_id}"
        if len(desc) > DESCRIPTION_MAX:
            desc = desc[:DESCRIPTION_MAX]

        tools.append({
            "name": exp.tool_name,
            "description": desc,
            "inputSchema": schema.get("input_schema", {}),
            "outputSchema": schema.get("output_schema", {}),
            "workflow_id": wf_id,
        })

    tools.sort(key=lambda t: t["name"])
    return tools[:MAX_TOOLS]
