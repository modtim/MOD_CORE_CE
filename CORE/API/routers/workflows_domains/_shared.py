# Auto-generated domain sub-module (Gate 2a split of routers/workflows.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Body, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import cast, String
import os
import schemas, crud, models
from dependencies import get_db, require_active_and_onboarded_user, get_keycloak_token, require_harbor_credentials, require_workflow_run_key
try:
    from utils.pricing import get_postpaid_credit_price, get_overage_credit_price
except ImportError:
    def get_postpaid_credit_price(*args, **kwargs):
        return 0

    def get_overage_credit_price(*args, **kwargs):
        return 0
from product_names import themed_logger
logger = themed_logger('routers.workflows_domains._shared', 'workflows')
from pydantic import BaseModel
import json
import io
import re
import zipfile
import uuid
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant

def _user_can_see_timing(db: Session, user: models.User) -> bool:
    """Check if the user's deployment/tenant is entitled to workflow timing.
    Resolves through the central entitlement gate (SaaS Plan, self-host
    license, or Core baseline — which grants timing)."""
    from services import workflows_domains_shared_service as _svc
    return _svc.user_can_see_timing(db, user)

def _user_can_access_workflow(db, current_user, db_workflow, capability: str = "view") -> bool:
    """Per-workflow authorization. Tenant-scoping alone is NOT enough — without
    this, any tenant user could load/edit/delete any workflow in the tenant.

    Access is granted to the workflow's owner or a user/team/role that holds the
    requested capability via the resource-permission system. No platform-admin
    bypass (ISO/DoD tenant isolation): a platform admin needs a real grant in the
    tenant, same as any user.
    """
    if db_workflow is None:
        return False
    # Ownership is resolved as an OWNER grant via authorize() (Gate 09/10), NOT a
    # created_by shortcut — so a scoped credential owned by the creator can still
    # be capped. grant_owner_permissions writes the grant at creation; existing
    # workflows are covered by backfill_workflow_owner_grants.
    from resource_permissions import authorize
    return authorize(
        db, current_user, models.ResourceType.WORKFLOW, capability, db_workflow.id
    )

def _run_view_scope(db, current_user):
    """(see_all, ids) — which workflows' RUN history `current_user` may see.

    Mirrors GET /workflows/{id}'s "view" decision via
    resource_permissions.workflow_view_scope, so a user sees run history only for
    workflows they own or hold an active "view" grant/share on — the same gate
    the workflow-list endpoint uses. Tenant-scoping alone is NOT enough: without
    this a standard user could read run metadata (and costs/outputs) for the
    owner's private workflows. Platform admins retain full visibility (the
    system/admin path is preserved).

    see_all=True -> ids is None (every workflow in scope). Otherwise ids is the
    exact set of visible workflow ids (possibly empty).
    """
    if getattr(current_user, "is_platform_admin", False):
        return True, None
    from resource_permissions import workflow_view_scope
    return workflow_view_scope(db, current_user)

def _user_can_view_workflow(db, current_user, workflow_id) -> bool:
    """True if `current_user` may see run history for `workflow_id`."""
    see_all, ids = _run_view_scope(db, current_user)
    return see_all or (workflow_id is not None and workflow_id in (ids or set()))

def _user_can_view_run(db, current_user, db_run) -> bool:
    """True if `current_user` may see `db_run` — its workflow is in their view
    scope (owned/granted), or they are a platform admin."""
    if db_run is None:
        return False
    return _user_can_view_workflow(db, current_user, db_run.workflow_id)

def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m {int(seconds % 60)}s"
    else:
        return f"{int(seconds / 3600)}h {int((seconds % 3600) / 60)}m"

_NODE_ACTIONS_PHASE1 = {"skip", "force_fail", "cancel_downstream_now", "cancel_downstream_drain"}

_NODE_ACTIONS_PHASE2 = {"rerun_same_data", "rerun_with_downstream"}

_NODE_ACTIONS_PHASE3 = {"clear_cache"}

_ALL_NODE_ACTIONS = _NODE_ACTIONS_PHASE1 | _NODE_ACTIONS_PHASE2 | _NODE_ACTIONS_PHASE3

def _node_output_rows(db, run, graph_node_id):
    """WorkflowNodeOutput rows produced by graph_node_id in this run."""
    from services import workflows_domains_shared_service as _svc
    return _svc.node_output_rows(db, run, graph_node_id)

_ACTIVE_TASK_STATUSES = (
    models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
    models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING,
)

def _clear_node_pause(run, node_id: str) -> bool:
    """Drop a node from the run's pause list. A skip / force-fail / re-run
    RESOLVES a pause point, so the node must leave the yellow paused state
    rather than showing paused + the new state at once. Returns True if a pause
    was actually cleared."""
    from sqlalchemy.orm.attributes import flag_modified
    current = list(run.paused_nodes or [])
    if node_id not in current:
        return False
    run.paused_nodes = [n for n in current if n != node_id]
    flag_modified(run, "paused_nodes")
    return True

def _node_action_audit(db, run, current_user, action: str, node_id: str,
                       prior: str = None, extra: dict = None):
    """Append-only record of one control action (ISO): run timeline + audit log."""
    from services.workflow_orchestrator import _emit_run_event
    meta = {"node_id": node_id, "action": action, "prior_state": prior}
    if extra:
        meta.update(extra)
    try:
        _emit_run_event(db, run.id, "operator", "info",
                        f"node-action '{action}' on {node_id} by user {current_user.id}", meta)
    except Exception:  # noqa: BLE001
        pass
    try:
        db.add(models.AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="NODE_RUN_ACTION",
            details={"workflow_run_id": run.id, **meta},
        ))
    except Exception:  # noqa: BLE001
        pass

def _noderun_for(db, run, graph_node_id: str):
    from services import workflows_domains_shared_service as _svc
    return _svc.noderun_for(db, run, graph_node_id)

def _downstream_node_ids(run, node_id: str) -> list:
    """All graph_node_ids strictly downstream of node_id, from the run's plan."""
    plan = (run.parameters or {}).get("execution_plan") or {}
    dependents = plan.get("dependents", {}) or {}
    out, seen, queue = [], set(), list(dependents.get(node_id, []) or [])
    while queue:
        n = queue.pop(0)
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        queue.extend(dependents.get(n, []) or [])
    return out

_ACTIVE_TASK_STATUSES_CTRL = (
    models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
    models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING,
)

METADATA_ROOT = os.getenv("METADATA_ROOT", "/metadata")

def _find_metadata_file(node_kind: str) -> Optional[str]:
    """
    Find the metadata `.py` file for a given node_kind by scanning the metadata directory.

    Searches system/ and tenant_generated/ folders for .py files containing
    a matching node_kind assignment in their METADATA BLOCK.
    """
    search_dirs = [
        os.path.join(METADATA_ROOT, "system"),
        os.path.join(METADATA_ROOT, "tenant_generated"),
    ]

    # Pattern: node_kind = "some_id" or node_kind = 'some_id'
    pattern = re.compile(rf'''node_kind\s*=\s*["']{re.escape(node_kind)}["']''')

    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for filename in os.listdir(search_dir):
            if not filename.endswith(".py"):
                continue
            filepath = os.path.join(search_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    # Only read the first 50 lines (metadata block is at the top)
                    head = "".join(f.readline() for _ in range(50))
                if pattern.search(head):
                    return filepath
            except Exception:
                continue
    return None

# F12-WORKFLOW-IMPORT-SHOWS-REAL-ERROR: a failed workflow import must reach the
# user with a SAFE reason instead of a bare framework 500. All four import routes
# (/import, /import-zip, /validate-import, /validate-import-zip) funnel through
# the two helpers below, so the mapping lives here once. Style follows 42d77c5594
# (model_cache_admin): short human sentence, no stack traces, no paths, no secrets.
def _sanitize_import_reason(text, limit: int = 160) -> str:
    """One-line, path-free reason safe to show a user."""
    cleaned = " ".join(str(text).split())
    for marker in ("Traceback", "/app/", 'File "'):
        cleaned = cleaned.replace(marker, "[redacted]")
    return cleaned[:limit]


def _reraise_safe_import_error(exc: Exception, action: str) -> None:
    """Map an exception escaping the shared import path to a safe HTTPException.

    - ValueError / KeyError (the user's file being wrong) -> 422 with the
      sanitized reason built from the exception's own message.
    - anything else -> fixed 500 sentence + a correlation id that is also
      logged (never the raw exception text).
    """
    if isinstance(exc, (ValueError, KeyError)):
        raise HTTPException(
            status_code=422,
            detail=f"invalid workflow: {_sanitize_import_reason(exc)}",
        )
    corr = uuid.uuid4().hex[:12]
    logger.error("%s failed unexpectedly (ref %s): %s: %s",
                 action, corr, type(exc).__name__, exc, exc_info=True)
    raise HTTPException(
        status_code=500,
        detail=f"could not {action}: internal error (ref {corr})",
    )


def _create_imported_workflow(
    db: Session,
    current_user,
    graph_json: dict,
    required_nodes: list,
    new_name: str,
    installed_dags: list = None,
) -> schemas.WorkflowImportResult:
    """Shared logic for creating an imported workflow."""
    from services import workflows_domains_shared_service as _svc
    try:
        return _svc.create_imported_workflow(
            db, current_user, graph_json, required_nodes, new_name, installed_dags,
        )
    except HTTPException:
        raise
    except Exception as e:
        _reraise_safe_import_error(e, "import workflow")

def _validate_required_nodes(db: Session, tenant_id: int, required_nodes_list: list) -> dict:
    """Shared validation logic for checking node availability."""
    from services import workflows_domains_shared_service as _svc
    try:
        return _svc.validate_required_nodes(db, tenant_id, required_nodes_list)
    except HTTPException:
        raise
    except Exception as e:
        _reraise_safe_import_error(e, "validate imported nodes")

__all__ = [
    '_user_can_see_timing',
    '_user_can_access_workflow',
    '_run_view_scope',
    '_user_can_view_workflow',
    '_user_can_view_run',
    '_format_duration',
    '_node_output_rows',
    '_clear_node_pause',
    '_node_action_audit',
    '_noderun_for',
    '_downstream_node_ids',
    '_find_metadata_file',
    '_create_imported_workflow',
    '_validate_required_nodes',
    '_NODE_ACTIONS_PHASE1',
    '_NODE_ACTIONS_PHASE2',
    '_NODE_ACTIONS_PHASE3',
    '_ALL_NODE_ACTIONS',
    '_ACTIVE_TASK_STATUSES',
    '_ACTIVE_TASK_STATUSES_CTRL',
    'METADATA_ROOT',
]
