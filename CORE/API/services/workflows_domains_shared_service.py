"""Business logic for the `_shared.py` helper functions that embed inline
data access (Gate 2b migration of `routers/workflows_domains/_shared.py`
itself, openspec: stack-training-and-prewarm). Mirrors the already-completed
sibling migration in `services/node_definitions_shared_service.py` — same
shape, same rationale.

`_shared.py` hosts no endpoint handlers of its own (Gate 2a already moved
every `@router.*` handler into a per-domain sub-module under
`routers/workflows_domains/`) — it is a pure helpers/constants module the
sibling domain modules (`workflows.py`, `assets_cogs_files.py`, ...)
`from routers.workflows_domains._shared import *`, and that at least one
already-migrated domain service (`workflows_domains_workflows_service.py`)
imports directly by name (`from routers.workflows_domains._shared import
_node_output_rows`) and calls.

Data access -> `crud/workflows_domains_shared.py` (the ONLY place
`db.query` lives for this slice). Every `_shared.py` function this module
backs stays defined in `_shared.py` under its ORIGINAL name as a thin
delegation to the function here — signature and docstring are preserved
exactly, because those names are imported and called by sibling domain
modules, their services, and (for `_user_can_see_timing`,
`_user_can_access_workflow`) tests using the pre-migration import path
(`from routers.workflows import _user_can_see_timing`, which re-exports this
module's names via `from routers.workflows_domains._shared import *`).

Pure, DB-free helpers (`_user_can_access_workflow`, `_run_view_scope`,
`_user_can_view_workflow`, `_user_can_view_run`, `_format_duration`,
`_clear_node_pause`, `_downstream_node_ids`, `_find_metadata_file`) have no
inline query to migrate and are NOT duplicated or wrapped here — they stay
defined directly in `_shared.py`, untouched. `_node_action_audit` also stays
untouched: it has no `db.query` (only an ISO append-only `db.add` + a call
into `services.workflow_orchestrator._emit_run_event`), so it is not in
scope for the "db.query -> crud" move; relocating an audit-trail helper's
delicate try/except-swallow behavior for no query-migration benefit would
be a needless behavior-preservation risk.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import crud.workflows_domains_shared as crud
import models
import schemas

__all__ = [
    "user_can_see_timing",
    "node_output_rows",
    "noderun_for",
    "create_imported_workflow",
    "validate_required_nodes",
]


# ---------------------------------------------------------------------
# _user_can_see_timing — verbatim MOVE, see `_shared.py` for the wrapper +
# full behavioral docstring.
# ---------------------------------------------------------------------

def user_can_see_timing(db: Session, user: models.User) -> bool:
    if user.is_platform_admin:
        return True
    from utils import entitlements
    tenant = crud.get_tenant(db, user.tenant_id)
    return entitlements.evaluate_feature(
        entitlements.Cap.SHOW_TIMING,
        entitlements.get_entitlements(db, tenant),
    ).allowed


# ---------------------------------------------------------------------
# _node_output_rows — verbatim MOVE.
# ---------------------------------------------------------------------

def node_output_rows(db: Session, run, graph_node_id: str) -> List[models.WorkflowNodeOutput]:
    wn = crud.get_workflow_node_by_graph_id(db, run.workflow_id, graph_node_id)
    if not wn:
        return []
    nr_ids = crud.get_node_run_ids_for_workflow_node(db, run.id, wn.id)
    if not nr_ids:
        return []
    return crud.get_workflow_node_outputs(db, run.id, nr_ids)


# ---------------------------------------------------------------------
# _noderun_for — verbatim MOVE.
# ---------------------------------------------------------------------

def noderun_for(db: Session, run, graph_node_id: str) -> Optional[models.NodeRun]:
    wn = crud.get_workflow_node_by_graph_id(db, run.workflow_id, graph_node_id)
    if not wn:
        return None
    return crud.get_node_run_for_workflow_node(db, run.id, wn.id)


# ---------------------------------------------------------------------
# _create_imported_workflow — verbatim MOVE. Owns its own transaction
# boundary (workflow + version create, then commit) exactly as the
# pre-migration inline body did; only the two read queries (dedup-name
# existence check, node-definition availability check) go through crud.
# ---------------------------------------------------------------------

def create_imported_workflow(
    db: Session,
    current_user,
    graph_json: dict,
    required_nodes: list,
    new_name: str,
    installed_dags: list = None,
) -> schemas.WorkflowImportResult:
    """Shared logic for creating an imported workflow."""
    import os
    from fastapi import HTTPException
    # 1788 CE carve: differentiable_nodes is paid; with it absent there is no
    # gradient-semantics validator, so the gate is inert (same CE posture as
    # crud/workflow_cud.create_workflow_version, which 1784 already wrapped).
    from utils.paid_import import optional_paid
    _diff = optional_paid("services.differentiable_nodes")
    from crud.workflow_cud import _enforce_dp_gate  # FORTRESS 6.2 (928b)

    # EXPR 10.1: same save-time differentiable-declaration gate the crud create
    # path enforces — an import is a save of a USER-SUPPLIED graph, so it must
    # not bypass it. Runs BEFORE any db.add / row creation; inert ([]) for
    # graphs that do not declare differentiable=true. Never log graph content.
    _diff_violations = (
        _diff.validate_differentiable(graph_json) if _diff is not None else []
    )
    if _diff_violations:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "differentiable_validation_failed",
                "violations": _diff_violations[:50],
            },
        )

    # FORTRESS 6.2 (928b) — same DP spec gate the save path (911,
    # crud/workflow_cud.create_workflow_version) enforces: an import saves a
    # USER-SUPPLIED graph, so it must not bypass the gate. Runs BEFORE any
    # db.add / row creation (no Workflow/WorkflowVersion rows on refusal),
    # same 422 shape as 911, inert for graphs declaring no dp.
    _enforce_dp_gate(graph_json)

    # Check for existing workflow with same name
    existing = crud.get_workflow_by_name(db, current_user.tenant_id, new_name)

    if existing:
        base_name = new_name
        counter = 1
        while existing:
            new_name = f"{base_name} ({counter})"
            existing = crud.get_workflow_by_name(db, current_user.tenant_id, new_name)
            counter += 1

    # Check which nodes the user has access to
    user_nodes = crud.get_node_definitions_for_tenant(db, current_user.tenant_id)
    user_dag_ids = {node.node_kind for node in user_nodes}

    required_set = set(required_nodes)
    missing_nodes = required_set - user_dag_ids

    # Create the workflow — stamp the home region (this manager's region) so the
    # mesh can relay a remote-fired trigger to the owning manager. Empty/unset
    # region leaves it NULL (single-region: always runs locally).
    new_workflow = models.Workflow(
        name=new_name,
        tenant_id=current_user.tenant_id,
        created_by_user_id=current_user.id,
        home_zone=(os.getenv("MANAGER_REGION", "") or None),
    )
    db.add(new_workflow)
    db.flush()

    new_version = models.WorkflowVersion(
        workflow_id=new_workflow.id,
        version_number=1,
        graph_json=graph_json
    )
    db.add(new_version)
    db.flush()

    new_workflow.latest_version_id = new_version.id
    db.commit()

    # Build result message
    if installed_dags and missing_nodes:
        message = (
            f"Workflow imported. {len(installed_dags)} DAG file(s) installed. "
            f"Note: {len(missing_nodes)} node(s) may not be available until the node refresh completes: "
            f"{', '.join(missing_nodes)}"
        )
    elif missing_nodes:
        message = (
            f"Workflow imported with warnings: Missing nodes: {', '.join(missing_nodes)}. "
            f"You won't be able to run this workflow until you have access to these nodes."
        )
    elif installed_dags:
        message = f"Workflow imported successfully with {len(installed_dags)} DAG file(s) installed."
    else:
        message = "Workflow imported successfully. All required nodes are available."

    return schemas.WorkflowImportResult(
        success=True,
        workflow_id=new_workflow.id,
        workflow_name=new_name,
        missing_nodes=list(missing_nodes),
        message=message
    )


# ---------------------------------------------------------------------
# _validate_required_nodes — verbatim MOVE.
# ---------------------------------------------------------------------

def validate_required_nodes(db: Session, tenant_id: int, required_nodes_list: list) -> Dict[str, Any]:
    """Shared validation logic for checking node availability."""
    user_nodes = crud.get_node_definitions_for_tenant(db, tenant_id)
    user_dag_ids = {node.node_kind for node in user_nodes}

    required_nodes = set(required_nodes_list)
    missing_nodes = required_nodes - user_dag_ids
    available_nodes = required_nodes & user_dag_ids

    return {
        "valid": len(missing_nodes) == 0,
        "available_nodes": list(available_nodes),
        "missing_nodes": list(missing_nodes),
        "message": "All nodes available" if not missing_nodes else f"Missing {len(missing_nodes)} node(s): {', '.join(missing_nodes)}"
    }
