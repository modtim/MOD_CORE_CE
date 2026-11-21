# Auto-generated domain sub-module (Gate 2a split of routers/workflows.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List, Optional, Dict, Any, Annotated
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Body, UploadFile, File, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import cast, String
import os
import schemas, crud, models
from dependencies import get_db, require_active_and_onboarded_user, get_keycloak_token, require_harbor_credentials, require_workflow_run_key
# GY.C16 remaining routes: the export ZIP ships the workflow graph + DAG code
# (tenant data content), so auditors need the same data-content gate the run
# output / run-file routes use.
from dependencies import require_auditor_data_content
try:
    from utils.pricing import get_postpaid_credit_price, get_overage_credit_price
except ImportError:
    def get_postpaid_credit_price(*args, **kwargs):
        return 0

    def get_overage_credit_price(*args, **kwargs):
        return 0
import logging
from product_names import themed_logger
logger = themed_logger('routers.workflows_domains.workflows', 'workflows')
from pydantic import BaseModel, Field
import json
import io
import re
import zipfile
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant
from utils.http_errors import ApiError

from routers.workflows_domains._shared import *

import routers.workflows as _wf  # aggregator ns for monkeypatch-faithful delegation

# Runs with these purposes are hidden from user-facing history queries.
# Single source of truth: services/run_purposes.py (WSIP 6.8 — this module now
# points at it instead of keeping its own mirror literal).
try:
    from services.run_purposes import USER_HIDDEN_RUN_PURPOSES
except (ImportError, ModuleNotFoundError):
    USER_HIDDEN_RUN_PURPOSES = ()

from crud import workflows_domain as workflows_domain_crud
from services import workflows_domain_service
from crud import workflows_domains_workflows as workflows_domains_workflows_crud
from services import workflows_domains_workflows_service
from services import sealed_workflow as _sealed
from services import payg_setup_gate  # payg 6.7 — module-level so psr.register()
from services import workflow_restore as _workflow_restore  # expr 9.10 — module-level so its psr.register() runs at API startup
# expr 10.4b — auto-apply latest trained parameters at run-time (psr.register at
# module level so the toggle is visible in Platform settings before any run).
from services import platform_settings as _psr
_psr.register(
    "expr.auto_apply_trained_parameters", bool, True,
    description="expr 10.4b — auto-apply the latest trained parameter version "
                "on every non-training run. When ON the trigger path "
                "fetches the newest workflow_parameter_versions row for this "
                "workflow (tenant-scoped) and writes its parameters into the "
                "differentiable graph before credit-calc and dispatch. Default True.",
    group="expr",
)
# runs at API startup (Admin -> Platform settings must show the switch before
# the first run, not only after one is attempted).

router = APIRouter()


def _err(desc):  # 30.6.6 c6-J2 — responses= entries modelled on schemas.ErrorResponse
    return {"model": schemas.ErrorResponse, "description": desc}


@router.post("/{workflow_id:int}/archive", response_model=schemas.Workflow, status_code=status.HTTP_200_OK)
def archive_workflow(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Archive (soft-hide) a workflow: sets `archived_at` so it drops out of the
    default Load picker WITHOUT deleting it. Reversible via /unarchive.
    Requires the 'delete' capability (archive is a removal-class action; the
    reversibility is a safety feature, not a lowering of the authority needed).
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "delete"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return crud.set_workflow_archived(db=db, db_workflow=db_workflow, archived=True)

@router.put("/{workflow_id:int}/placement", response_model=schemas.Workflow)
def put_workflow_placement(
        workflow_id: int,
        body: schemas.WorkflowPlacementUpdate = Body(default=schemas.WorkflowPlacementUpdate()),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    GX.D1 — set (or clear) this workflow's default worker/pool placement.

    `{"placement": {"worker_id": 42}}` pins every run's tasks to that
    worker; `{"placement": {"worker_tag": "llm"}}` pins them to any worker
    advertising the tag; `{"placement": null}` or `{"clear": true}`
    un-pins. A named worker must exist and be owned by / assigned to the
    caller's tenant (404 otherwise). Requires the 'edit' capability —
    placement steers where the tenant's compute runs, the same class of
    change as editing the graph. Precedence when set: run request > node
    config > this workflow default.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")

    from services.workflow_placement import validate_worker_placement
    if getattr(body, "clear", False) or getattr(body, "placement", None) is None:
        # null placement / clear / empty body: un-pin (NULL = today's
        # behaviour, so clearing is always safe).
        db_workflow.placement = None
        db.commit()
        db.refresh(db_workflow)
        return db_workflow

    _norm, _err = validate_worker_placement(db, body.placement, current_user.tenant_id)
    if _err:
        raise HTTPException(status_code=404, detail=_err)
    db_workflow.placement = _norm
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


@router.get("/{workflow_id:int}/placement")
def get_workflow_placement(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """GX.D1 — read this workflow's default worker/pool placement.

    Returns the raw placement object ({"worker_id":..} / {"worker_tag":..})
    or null when unpinned. Requires the 'view' capability (it only reveals
    the workflow's own settings, nothing the list endpoint doesn't show).
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "view"):
        raise HTTPException(status_code=403, detail="No access to this workflow")
    return {"workflow_id": db_workflow.id, "placement": db_workflow.placement}


@router.post("/{workflow_id:int}/unarchive", response_model=schemas.Workflow, status_code=status.HTTP_200_OK)
def unarchive_workflow(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Unarchive a workflow: clears `archived_at` so it reappears in the default
    Load picker. Requires the 'delete' capability (symmetric with archive).
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "delete"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return crud.set_workflow_archived(db=db, db_workflow=db_workflow, archived=False)


@router.post("/{workflow_id:int}/restore", response_model=schemas.Workflow, status_code=status.HTTP_200_OK)
def restore_workflow(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    expr 9.10 — restore a soft-deleted workflow, memory included.

    Undo of DELETE /road/{id}: clears ``deleted_at`` on the workflow row
    and re-live the memory rows that deletion stamped with that exact
    timestamp (``workflow_memory_kv`` / ``_vector`` / ``_timeseries``).
    Authz mirrors the cog restore path: the workflow's owner, a tenant
    manager (parent user) or a platform admin.

    - 404 if the workflow does not exist in this tenant.
    - 403 if the caller is not the owner/manager.
    - 409 if the workflow is not currently deleted.
    - 410 if it was deleted more than ``expr.workflow_recovery_window_days``
      (default 30) ago — the recovery window has expired.
    """
    try:
        db_workflow = _workflow_restore.restore_workflow(
            db, tenant_id=current_user.tenant_id, workflow_id=workflow_id,
            current_user=current_user)
    except _workflow_restore.GoneError as e:
        raise HTTPException(status_code=410, detail=e.message)
    except _workflow_restore.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except _workflow_restore.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except _workflow_restore.ConflictError as e:
        raise HTTPException(status_code=409, detail=e.message)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


@router.post("/{workflow_id:int}/upgrade-nodes", status_code=status.HTTP_200_OK)
def upgrade_workflow_nodes(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    FX.103 (owner decision Q89=A) — the explicit "upgrade nodes" re-pin.

    Node builds are pinned (node-version-pinning): a re-run of a workflow
    executes exactly the image digests it was pinned to, so node updates
    never silently change existing workflows. THIS endpoint is the only
    way to move those pins forward — every node in the workflow is re-pinned
    to its node's CURRENT build in one transaction, and the change is
    audited (AuditEvent WORKFLOW_NODE_PINS_UPGRADED).

    Requires the 'edit' capability (a pin change alters what the workflow
    executes — same class of change as editing the graph). Returns the
    per-node diff (previous vs new pin + display version, `changed` flag);
    nodes whose build did not move are reported with `changed: false`.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    changes = crud.upgrade_workflow_node_pins(db, db_workflow.id)
    db.add(models.AuditEvent(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        event_name="WORKFLOW_NODE_PINS_UPGRADED",
        details={
            "workflow_id": db_workflow.id,
            "changed": [c["graph_node_id"] for c in changes if c["changed"]],
            "changes": changes,
        },
    ))
    db.commit()
    return {
        "workflow_id": db_workflow.id,
        "nodes": changes,
        "upgraded": sum(1 for c in changes if c["changed"]),
        "total": len(changes),
    }

def _save_inheritance_catalog(db, tenant_id, graphs):
    """wep 3.1 — NodeDefinition rows the save-path `extends`/`implements`
    validation needs, keyed by definition id (the adapter's node_catalog).

    Refs are harvested from the graphs about to be saved (child) AND the
    parent's latest live graph, so an overridden parent node resolves to its
    real definition, not a wildcard. Visibility filter mirrors the run path
    (worker_tasks `_node_def_for_tenant`): live rows owned by this tenant or
    by nobody (system nodes).
    """
    ids, kinds = set(), set()
    for graph in graphs:
        if not isinstance(graph, dict):
            continue
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            for key in ("node_definition_id", "definition_id",
                        "node_definition_id_key"):
                ref = node.get(key, data.get(key))
                if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                    ids.add(int(ref))
            kind = node.get("node_kind") or data.get("node_kind") or node.get("type")
            if isinstance(kind, str) and kind:
                kinds.add(kind)
    if not ids and not kinds:
        return {}
    rows = (db.query(models.NodeDefinition)
            .filter(models.NodeDefinition.deleted_at.is_(None),
                    models.NodeDefinition.tenant_id.is_(None)
                    | (models.NodeDefinition.tenant_id == tenant_id),
                    models.NodeDefinition.id.in_(sorted(ids))
                    | models.NodeDefinition.node_kind.in_(sorted(kinds)))
            .all())
    return {row.id: row for row in rows}


@router.post("/{workflow_id}/versions", response_model=schemas.WorkflowVersion, status_code=status.HTTP_201_CREATED)
def create_workflow_version(
        workflow_id: int,
        version: schemas.WorkflowVersionCreate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Creates a new, immutable version of a workflow.
    This automatically becomes the 'latest' version for the parent workflow.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    # GG.8 — sealed installed copies are run-only; new graph versions are edits.
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, db_workflow, current_user)

    # wep 3.1 — save-path `extends`/`implements`. Both None -> the adapter is a
    # zero-query passthrough and everything below runs exactly as before.
    # Refuse the save (no version row) iff ok is False; otherwise the version
    # stores the RESOLVED graph. The declaration itself is NOT persisted: the
    # extends_workflow_id / implements columns land with migration k (lead).
    from services import workflow_save_inheritance as _wsi
    if version.extends_workflow_id is not None or version.implements is not None:
        _parent_graph_for_catalog = None
        if version.extends_workflow_id is not None:
            _parent_graph_for_catalog = _wsi._parent_graph(
                db, current_user.tenant_id, version.extends_workflow_id
            )[0]
        _inh = _wsi.prepare_workflow_save(
            db,
            tenant_id=current_user.tenant_id,
            workflow_id=db_workflow.id,
            graph_json=version.graph_json,
            extends_workflow_id=version.extends_workflow_id,
            implements=version.implements,
            node_catalog=_save_inheritance_catalog(
                db, current_user.tenant_id,
                [version.graph_json, _parent_graph_for_catalog],
            ),
        )
        if not _inh["ok"]:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_inheritance",
                "errors": _inh["errors"],
            })
        version = version.model_copy(
            update={"graph_json": _inh["resolved_graph"]}
        )

    # SZ 219 — fail-closed sub-workflow composition gate: a graph whose
    # sub_workflow refs are unknown/foreign, cyclic, or deeper than
    # workflows.sub_workflow_max_depth is rejected BEFORE any version row
    # exists. No sub_workflow nodes = no-op, no queries.
    from services import sub_workflow_validation as _swv
    try:
        _swv.validate_sub_workflow_composition(
            db, current_user.tenant_id, db_workflow.id, version.graph_json
        )
    except _swv.SubWorkflowValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # expr-10.1 — differentiable-workflow save-time check: enforced by the
    # single gate in crud/workflow_cud.py create_workflow_version (reads
    # BOTH db_workflow.differentiable and the graph's own inline
    # ``differentiable`` declaration). A duplicate check used to live here,
    # keyed only off db_workflow.differentiable with its own error code
    # (``differentiable_node_missing_backward``) — removed so there is
    # exactly one validation path and one error code
    # (``differentiable_validation_failed``) across save/listing/publish/
    # import.

    # wep1.c — save-time parameter-declaration validator (pure shape check on
    # graph_json.parameters; run-time use of the declarations is gated later by
    # workflows.parameters_enabled in wep1.f).
    from services import workflow_parameters as _wp
    try:
        _wp.validate_parameter_declarations(version.graph_json or {})
    except _wp.WorkflowParametersError as e:
        try:
            db.add(models.AuditEvent(
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                event_name="WORKFLOW_PARAMETERS_REJECTED",
                details={
                    "workflow_id": db_workflow.id,
                    "code": e.code,
                    "param_name": e.param_name,
                },
            ))
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    return crud.create_workflow_version(db=db, workflow=db_workflow, version_create=version, user_id=current_user.id)

@router.get("/{workflow_id}/versions", response_model=List[schemas.WorkflowVersion])
def list_workflow_versions(
        workflow_id: int,
        limit: int = 50,
        offset: int = 0,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Lists all historical versions for a specific workflow.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "view"):
        # 403 structured so the builder (which lists versions when you click a
        # workflow in the Load dialog) can show the "request access" popup.
        raise HTTPException(status_code=403, detail={
            "code": "workflow_access_denied",
            "message": "You don't have access to this workflow.",
            "workflow_id": db_workflow.id,
            "workflow_name": db_workflow.name,
        })
    # GG.8 — sealed installed copies hide their version (graph) listing.
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, db_workflow, current_user)

    return crud.get_workflow_versions(db, workflow_id=workflow_id, limit=limit, offset=offset)


class _SealedInputEntry(BaseModel):
    node: str
    configuration: Dict[str, Any] = {}


class _SealedInputsUpdate(BaseModel):
    sealed_inputs: List[_SealedInputEntry]


@router.get("/{workflow_id}/sealed-form")
def get_sealed_form(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    GG.8 app-form — buyer-safe description of a SEALED installed copy: just
    enough for the Workflow Builder to render "inputs -> run -> outputs"
    instead of the canvas. Returns nothing beyond what the buyer's own
    `sealed_inputs` placeholder already exposes (opaque input-node ids +
    their configuration defaults) — no pipeline graph, no node_kind, no
    processing images.

    Exempt callers (the publisher tenant, platform admins) get
    `sealed: false` — they read the real graph via the normal version
    endpoint, same as every other graph surface (require_graph_readable is a
    no-op for them there).
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "view"):
        raise HTTPException(status_code=403, detail={
            "code": "workflow_access_denied",
            "message": "You don't have access to this workflow.",
            "workflow_id": db_workflow.id,
            "workflow_name": db_workflow.name,
        })

    is_sealed_for_caller = _sealed.should_redact_graph(db, db_workflow, current_user)
    version = db_workflow.latest_version
    resp: Dict[str, Any] = {
        "workflow_id": db_workflow.id,
        "name": db_workflow.name,
        "sealed": is_sealed_for_caller,
        "version_id": version.id if version else None,
    }
    if is_sealed_for_caller:
        resp["sealed_inputs"] = _sealed.sealed_inputs_from_placeholder(
            (version.graph_json or {}) if version else {}
        )
    return resp


@router.put("/{workflow_id}/sealed-inputs")
def update_sealed_inputs(
        workflow_id: int,
        body: _SealedInputsUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    GG.8 app-form — persist a sealed buyer's edited input values onto their
    OWN placeholder version row (`WorkflowVersion.graph_json["sealed_inputs"]`).
    Never touches the publisher's listing snapshot or any non-input node.

    Only entries whose `node` id is already one of the buyer's existing
    `sealed_inputs` node ids are applied — the same input-node-id
    restriction `services.sealed_workflow._apply_sealed_inputs` enforces at
    run time. A tampered/unknown node id is silently dropped (not stored,
    not an error) rather than growing the buyer's exposed-input surface.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "execute"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _sealed.is_sealed(db, db_workflow):
        raise HTTPException(status_code=400, detail="Workflow is not a sealed installed copy.")

    version = db_workflow.latest_version
    if version is None:
        raise HTTPException(status_code=404, detail="Workflow has no versions")

    current = _sealed.sealed_inputs_from_placeholder(version.graph_json or {})
    allowed_ids = [e.get("node") for e in current if isinstance(e, dict) and e.get("node")]
    by_id = {e.get("node"): dict(e.get("configuration") or {})
             for e in current if isinstance(e, dict) and e.get("node")}

    for entry in body.sealed_inputs:
        if entry.node not in by_id:
            continue  # not one of this listing's input nodes — dropped, not stored
        by_id[entry.node] = dict(entry.configuration or {})

    new_sealed_inputs = [{"node": nid, "configuration": by_id[nid]} for nid in allowed_ids]
    graph_json = dict(version.graph_json or {})
    graph_json["sealed_inputs"] = new_sealed_inputs
    version.graph_json = graph_json
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(version, "graph_json")
    db.commit()

    return {"sealed_inputs": new_sealed_inputs}


@router.get("/workflow-versions/{version_id}", response_model=schemas.WorkflowVersion)
def get_workflow_version(
        version_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Gets the details of a specific workflow version, including its graph definition.
    """
    db_version = crud.get_workflow_version_by_id(db, version_id=version_id, tenant_id=current_user.tenant_id)

    if db_version is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")

    # This is the endpoint the Workflow Builder loads a version from — enforce
    # access on the parent workflow so a tenant user can't open a workflow they
    # have no permission on.
    parent_workflow = crud.get_workflow_by_id(
        db, workflow_id=db_version.workflow_id, tenant_id=current_user.tenant_id
    )
    if parent_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")
    if not _user_can_access_workflow(db, current_user, parent_workflow, "view"):
        # Exists in-tenant but no grant — 403 with a structured payload so the
        # builder can offer "request access".
        raise HTTPException(status_code=403, detail={
            "code": "workflow_access_denied",
            "message": "You don't have access to this workflow.",
            "workflow_id": parent_workflow.id,
            "workflow_name": parent_workflow.name,
        })
    # GG.8 — sealed installed copies hide the version (graph definition).
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, parent_workflow, current_user)

    # GG.8c — the exempt reader (support / publisher tenant) sees the expanded
    # publisher pipeline, not the buyer's empty placeholder. In-memory only:
    # swap the graph on a PLAIN response copy, never on the ORM row — a later
    # flush/commit in this request would otherwise persist the publisher's
    # graph into the buyer's sealed version (leaking the sealed IP).
    if _sealed.is_sealed(db, parent_workflow):
        resp = schemas.WorkflowVersion.model_validate(db_version)
        resp.graph_json = _sealed.expand_for_run(db, db_version)
        return resp

    return schemas.WorkflowVersion.model_validate(db_version)

@router.delete("/workflow-versions/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow_version(
        version_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Delete a SINGLE workflow version (not the whole workflow).

    Guards:
      - the parent workflow must exist in the caller's tenant and the caller
        must hold the 'delete' capability on it;
      - refuses to delete the workflow's only/last version (409) — use the
        full workflow-delete for that;
      - refuses to delete a version that has run history (409), since deleting
        it would orphan/lose that history.
    If the deleted version was the workflow's latest, the parent is repointed
    to the next-highest remaining version.
    """
    db_version = crud.get_workflow_version_by_id(db, version_id=version_id, tenant_id=current_user.tenant_id)
    if db_version is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")

    parent_workflow = crud.get_workflow_by_id(
        db, workflow_id=db_version.workflow_id, tenant_id=current_user.tenant_id
    )
    if parent_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")
    if not _user_can_access_workflow(db, current_user, parent_workflow, "delete"):
        raise HTTPException(status_code=404, detail="Workflow version not found")
    # GG.8 — sealed installed copies are run-only; deleting a version is an edit.
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, parent_workflow, current_user)

    if crud.count_workflow_versions(db, workflow_id=db_version.workflow_id) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the only version of a workflow. Delete the workflow instead.",
        )

    runs = crud.version_run_count(db, version_id=version_id)
    if runs > 0:
        raise HTTPException(
            status_code=409,
            detail=f"This version has {runs} run(s) and can't be deleted; its run history would be lost.",
        )

    crud.delete_workflow_version(db=db, db_version=db_version)
    return


class _WorkflowQuoteNodeInput(BaseModel):
    # B105: byte counts must be non-negative. A negative size previously
    # passed validation, then the size-tier resolver never clamped it, so a
    # malformed quote silently resolved to the 'small' tier and billed it.
    file_sizes: Optional[List[Annotated[int, Field(ge=0)]]] = None
    input_size_bytes: Optional[Annotated[int, Field(ge=0)]] = None


class WorkflowQuoteRequest(BaseModel):
    node_inputs: Dict[str, _WorkflowQuoteNodeInput] = {}


class _WorkflowQuoteNodeBreakdown(BaseModel):
    node_id: Optional[str] = None
    size_tier: str
    credits: int
    basis: str
    pricing_source: Optional[str] = None
    predicted_runtime_seconds: Optional[float] = None


class WorkflowQuoteResponse(BaseModel):
    workflow_id: int
    total_credits: int
    breakdown: List[_WorkflowQuoteNodeBreakdown]
    confidence: str


@router.post("/{workflow_id}/quote", response_model=WorkflowQuoteResponse)
def quote_workflow_cost(
        workflow_id: int,
        quote_request: WorkflowQuoteRequest = Body(default=WorkflowQuoteRequest()),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    GX.sz-tcp.18 — per-run credit quote for a SAVED workflow, using
    size-tiered pricing. Reuses `utils.workflow_credits.compute_workflow_quote`,
    which shares its tiered-cost machinery (`utils.node_cost.resolve_tiered_cost`)
    with the actual charge path, so this can't drift from the bill. Sibling
    to `/road/predict-cost` (13.1) — that one prices an INLINE
    (unsaved) graph for the builder's live estimate; this one prices a
    saved `workflow_id`'s latest version, tenant-scoped (404 cross-tenant).

    Request body (all optional):
    {
        "node_inputs": {
            "<node_id>": {"file_sizes": [bytes, ...]} | {"input_size_bytes": N}
        }
    }
    Overrides let the caller supply the real run-time size for an input
    node whose saved graph doesn't carry one (e.g. a file-upload node with
    no file chosen yet). A node with neither an override nor a
    graph-carried size signal defaults to the "medium" tier.

    Response:
    {
        "workflow_id": 123,
        "total_credits": 7,
        "breakdown": [{"node_id": "...", "size_tier": "small", "credits": 1,
                        "basis": "tiered" | "flat"}, ...],
        "confidence": "exact" | "estimated"
    }
    `confidence` is "exact" only when EVERY node both had a real size
    signal (override or graph-carried) and a tiered-cost map; any node
    that fell back to the "medium" default or to a flat (non-tiered) cost
    makes the whole quote "estimated".

    NOTE (sz-tcp.19, not implemented yet): a regression-based size
    predictor for nodes with no size signal at all is planned as a future
    `tier_resolver` plugged into `compute_workflow_quote` — this endpoint
    only implements the deterministic (measured-or-"medium") tier path.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "view"):
        raise HTTPException(status_code=404, detail="Workflow not found")

    version = db_workflow.latest_version
    graph_json = (version.graph_json or {}) if version else {}
    nodes_list = graph_json.get("nodes", []) if isinstance(graph_json, dict) else []

    from utils.paid_import import optional_paid
    _wc = optional_paid("utils.workflow_credits")
    if _wc is None:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "workflow credits module not installed"},
        )
    from utils.workflow_credits import compute_workflow_quote
    overrides = {
        node_id: entry.model_dump(exclude_none=True)
        for node_id, entry in (quote_request.node_inputs or {}).items()
    }
    quote = compute_workflow_quote(db, nodes_list, overrides)

    return WorkflowQuoteResponse(
        workflow_id=db_workflow.id,
        total_credits=quote["total_credits"],
        breakdown=quote["breakdown"],
        confidence=quote["confidence"],
    )


@router.post("/predict-cost")
def predict_workflow_cost(
        workflow_definition: dict = Body(...),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Real-time cost prediction for workflow builder.

    Uses the same cost calculation logic as Traffic Cop but without execution.
    Returns cost breakdown, billing mode info, and actionable warnings.

    Request body:
    {
        "nodes": [...],  # Workflow graph nodes
        "edges": [...],  # Workflow graph edges
        "file_count": 10  # Number of files to process
    }

    Returns:
    {
        "total_credits": 505,
        "total_usd": 50.50,  # If pay-as-you-go
        "breakdown": [{node_id, node_kind, credits, runs}, ...],
        "billing_mode": "postpaid" | "prepaid",
        "subscription_info": {...},  # If prepaid
        "sufficient_credits": true,
        "user_balance": 10000,
        "warnings": [...]
    }
    """
    from sqlalchemy import text

    nodes_list = workflow_definition.get("nodes", [])
    edges_list = workflow_definition.get("edges", [])
    file_count = workflow_definition.get("file_count", 1)

    # Build node lookup
    nodes = {node["id"]: node for node in nodes_list}

    # Detect execution mode (batch vs grouped). Shared with the charge path
    # (calculate_workflow_credits) via one helper so the quote and the bill
    # can't drift — mega-qa-hardening Billing #1.
    from utils.paid_import import optional_paid
    _wc = optional_paid("utils.workflow_credits")
    if _wc is None:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "workflow credits module not installed"},
        )
    from utils.workflow_credits import detect_execution_mode
    execution_mode = detect_execution_mode(nodes_list)

    # Build execution order (topological sort)
    from collections import defaultdict, deque

    dependencies = defaultdict(list)
    dependents = defaultdict(list)

    for edge in edges_list:
        source = edge.get("source")
        target = edge.get("target")
        if source and target:
            dependencies[target].append(source)
            dependents[source].append(target)

    root_nodes = [node_id for node_id in nodes.keys() if node_id not in dependencies]

    execution_order = []
    in_degree = {node_id: len(deps) for node_id, deps in dependencies.items()}
    for node_id in nodes.keys():
        if node_id not in in_degree:
            in_degree[node_id] = 0

    queue = deque(root_nodes)

    while queue:
        current_level = list(queue)
        execution_order.append(current_level)
        queue.clear()

        for node_id in current_level:
            for dependent in dependents.get(node_id, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

    # Calculate cost breakdown using NodeDefinition costs from database
    breakdown = []
    # Single source of truth for the cost math (#10,
    # launch-day-followup-2): compute_workflow_node_costs is the same
    # helper that the actual charge path (calculate_workflow_credits)
    # uses, so the displayed estimate and the billed amount cannot
    # drift apart. The previous duplicated math here had a different
    # fallback table for legacy DAGs, which produced "UI 4 / charge 3"
    # off-by-one mismatches.
    from utils.paid_import import optional_paid
    _wc = optional_paid("utils.workflow_credits")
    if _wc is None:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "workflow credits module not installed"},
        )
    from utils.workflow_credits import compute_workflow_node_costs
    runs_per_node = file_count if execution_mode == "batch" else 1

    # 15b.4.s2 — task-profile pricing (inert by default). When
    # `billing.pricing_model` is 'task_profile' the displayed estimate comes
    # from utils.credit_manager.estimate_task_profile: each node priced by
    # price_task_profile from its PREDICTED profile — hardware class +
    # predicted runtime (NodeRun.estimated_duration_seconds / ResourcePattern
    # via the dispatcher's own resolution), cpu/ram/vram from WorkerLicense
    # .hardware_specs of the machine the task is expected to run on, and the
    # graph's declared input bytes as the data leg. The breakdown rides along
    # so the reservation debit can record the charge reason. The default
    # 'legacy_gb_multiplier' never enters this branch: the flat
    # cost_in_credits x runs math below stays byte-for-byte as it was.
    from utils.paid_import import optional_paid
    _cm = optional_paid("utils.credit_manager")
    if _cm is None:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "credit manager module not installed"},
        )
    from utils.credit_manager import (
        pricing_model as _tp_pricing_model,
        estimate_task_profile,
    )
    pricing_reason = None
    pricing_breakdown = None
    if _tp_pricing_model(db) == "task_profile":
        predicted_bytes = 0
        for node in nodes_list:
            raw = (node.get("data") or {}).get("input_size_bytes")
            if raw is None:
                raw = node.get("input_size_bytes")
            try:
                predicted_bytes += max(0, int(raw or 0))
            except (TypeError, ValueError):
                continue
        total_credits, quote = estimate_task_profile(
            db,
            graph={"nodes": nodes_list},
            total_input_bytes=predicted_bytes,
            runs_per_node=runs_per_node,
        )
        pricing_reason = quote["reason"]
        pricing_breakdown = quote
        breakdown = [
            {
                'node_id': row["node_id"],
                'node_kind': row["node_kind"],
                'node_type': row["node_kind"],
                'cost_per_run': row["credits_per_run"],
                'runs': row["runs"],
                'subtotal': row["credits"],
                'source': 'task_profile',
                'reason': row["reason"],
            }
            for row in quote["nodes"]
        ]
    else:
        cost_summary = compute_workflow_node_costs(
            db, nodes_list, runs_per_node,
        )
        total_credits = cost_summary["total_credits"]
        breakdown = []
        for node_cost in cost_summary["node_costs"]:
            node_id = node_cost["node_id"]
            node_data = nodes[node_id].get("data", {})
            node_kind = node_data.get("node_kind")
            cost_per_run = node_cost["credits_per_file"]
            runs = node_cost["file_count"]
            subtotal = node_cost["total_credits"]
            breakdown.append({
                'node_id': node_id,
                'node_kind': node_kind,
                'node_type': node_cost.get("node_type"),
                'cost_per_run': cost_per_run,
                'runs': runs,
                'subtotal': subtotal,
                # `source` field is informational — DB hit if the node had
                # a NodeDefinition.cost_in_credits row, else fallback to
                # the helper's default (1).
                'source': 'database' if 'note' not in node_cost else 'fallback',
            })

    # Get tenant billing info
    tenant, subscription, plan = workflows_domain_service.get_billing_context(db, current_user.tenant_id)
    # OWNER-DECISIONS 09-29 — affordability (`sufficient_credits` below) is
    # plan + purchased pools combined; a workflow can draw on purchased
    # (one-time top-up) credits too.
    user_balance = (
        (tenant.credits_remaining or 0) + (getattr(tenant, "purchased_credits_remaining", 0) or 0)
    ) if tenant else 0
    billing_mode = tenant.billing_mode if tenant else 'prepaid'

    subscription_info = None
    if billing_mode == 'prepaid' and tenant:
        if subscription:
            if plan:
                subscription_info = {
                    'plan_name': plan.name,
                    'credits_per_month': plan.credits_per_month,
                    'price_in_cents': plan.price_in_cents,
                    'credits_used_this_period': tenant.credits_used_this_period or 0,
                    'credits_remaining_after_workflow': user_balance - total_credits
                }

    # Calculate USD cost and warnings
    warnings = []
    total_usd = 0

    if billing_mode == 'postpaid':
        # Get current postpaid pricing from database (per-tenant override aware)
        postpaid_price_cents = get_postpaid_credit_price(db, current_user.tenant_id)
        postpaid_price_usd = postpaid_price_cents / 100
        total_usd = total_credits * postpaid_price_usd

        if total_credits > 1000:
            savings_with_essential = (total_credits * postpaid_price_usd) - (total_credits * 0.017)
            warnings.append({
                'type': 'savings_opportunity',
                'message': f"You could save ${savings_with_essential:.2f} with Essential plan!",
                'severity': 'info'
            })
    else:
        # Check for overage
        if subscription_info:
            total_after = subscription_info['credits_used_this_period'] + total_credits
            if total_after > subscription_info['credits_per_month']:
                overage = total_after - subscription_info['credits_per_month']
                # Get current overage pricing from database
                overage_price_cents = get_overage_credit_price(db, current_user.tenant_id)
                overage_price_usd = overage_price_cents / 100
                overage_cost = overage * overage_price_usd
                warnings.append({
                    'type': 'overage_warning',
                    'message': f"This workflow will exceed your monthly allowance by {overage:,} credits (${overage_cost:.2f})",
                    'severity': 'warning',
                    'overage_credits': overage,
                    'overage_cost_usd': overage_cost
                })

    sufficient = user_balance >= total_credits
    deficit = max(0, total_credits - user_balance)

    if not sufficient:
        warnings.append({
            'type': 'insufficient_credits',
            'message': f"You need {deficit:,} more credits to run this workflow",
            'severity': 'error',
            'deficit_credits': deficit
        })

    payload = {
        'total_credits': total_credits,
        'total_usd': total_usd if billing_mode == 'postpaid' else None,
        'breakdown': breakdown,
        'billing_mode': billing_mode,
        'subscription_info': subscription_info,
        'sufficient_credits': sufficient,
        'user_balance': user_balance,
        'deficit': deficit,
        'warnings': warnings,
        'execution_mode': execution_mode,
        'file_count': file_count
    }
    # 15b.4.s2 — the task-profile quote only rides along when that model is
    # active: the legacy (default) response shape stays byte-for-byte the same.
    if pricing_reason:
        payload['pricing_model'] = "task_profile"
        payload['pricing_reason'] = pricing_reason
        payload['pricing_breakdown'] = pricing_breakdown
    return payload

def _fortress69_epsilon_detail(budget: dict) -> dict:
    """FORTRESS.6.9 slice 2b — the ε axis of a gate dict, ready for the 402 detail.

    `services.run_budget_gate.precheck_run_budget` merges the five ε keys from
    `dp_budget.remaining` ONLY when the platform is actually enforcing a privacy
    boundary (`dp.budget_enabled` on AND a finite effective cap — owner rec A), so
    this passes them through ONLY when present: an absent key stays absent (never
    null, never the word "unlimited" — the panel must not imply a boundary the
    platform is not enforcing, design_fortress_69 §4 Q2). Decimals render as
    `str`, the same way the `DpBudgetExhausted` 402 in this router serialises
    them (`dp_budget.as_detail`), so the WB over-budget panel sees one ε shape.
    ε is never folded into `forecast_credits`/`cap`: privacy budget and credits
    are different axes (design_fortress_69 §2).
    """
    detail = {}
    for _key in ("epsilon_requested", "epsilon_spent_period", "epsilon_cap",
                 "epsilon_remaining"):
        if _key in budget:
            detail[_key] = str(budget[_key])
    if "period" in budget:
        detail["period"] = budget["period"]
    return detail



def _idempotent_replay_response(row, req_hash, run_request):
    """2400GX-n8 — the replay answer, or 409 when the body differs."""
    if row.request_hash != req_hash:
        raise HTTPException(status_code=409, detail={
            "error": "idempotency_key_reused",
            "message": "This idempotency_key was already used with a different request body."})
    if row.workflow_run_id is None:
        raise HTTPException(status_code=409, detail={
            "error": "idempotency_key_in_progress",
            "message": "A request with this key is still being created; retry shortly."})
    return {
        "detail": "Idempotent replay: returning the original workflow run",
        "workflow_run_id": row.workflow_run_id,
        "cog_ids": run_request.cog_ids,
        "idempotent_replay": True,
    }


# 2400GX-n10 — app API keys (X-API-Key) and stack identity bearers must be able
# to fire a run (app_key_scope.py / stack_identity.py allow-list this route),
# but the Keycloak route below rejects both at its user dep. A twin route on the
# SAME path, registered first, takes only those two principal kinds; every other
# request (Keycloak bearer, bogus bearer, no credentials) falls through to the
# unchanged Keycloak route below, so its status codes/bodies are byte-identical.
from fastapi.routing import APIRoute as _APIRoute
from starlette.routing import Match as _Match


def _is_programmatic_run_scope(scope) -> bool:
    _h = {k.decode("latin-1").lower(): v.decode("latin-1")
          for k, v in scope.get("headers", [])}
    _auth = _h.get("authorization", "")
    if not _auth:
        return bool(_h.get("x-api-key"))  # Bearer present => Keycloak route wins
    if _auth[:7].lower() == "bearer ":
        from utils.build_task_token import verify_stack_identity_token
        return verify_stack_identity_token(_auth[7:].strip()) is not None
    return False


class _ProgrammaticRunRoute(_APIRoute):
    def matches(self, scope):
        _m, _child = super().matches(scope)
        if _m == _Match.FULL and not _is_programmatic_run_scope(scope):
            return _Match.NONE, {}
        return _m, _child


def _run_principal(
        request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_bearer_or_api_key),
):
    """App-key / stack-identity principal for the run route: the same Harbor
    pre-flight as the Keycloak route, plus the FX.106 per-plan / per-key rate
    limit that require_active_and_onboarded_user applies to user callers
    (require_bearer_or_api_key only counts failed auth)."""
    from services import api_rate_limit_guard as _fx106_guard
    from utils.rate_limiter import _get_plan_rate_limit
    from utils.request_principal import get_api_key
    try:
        _resp = _fx106_guard.check_api_rate_limits(
            request,
            tenant_id=current_user.tenant_id,
            api_key=get_api_key(),
            plan_limit=_get_plan_rate_limit(current_user.tenant_id),
            db=db,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — guard unavailable: fail open like the user dep
        _resp = None
    if _resp is not None:
        return _resp
    return require_harbor_credentials(db, current_user)


def trigger_workflow_run_programmatic(
        version_id: int,
        run_request: schemas.WorkflowRunRequest,
        background_tasks: BackgroundTasks = None,
        db: Session = Depends(get_db),
        current_user=Depends(_run_principal),
        idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    if isinstance(current_user, JSONResponse) or hasattr(current_user, "status_code"):
        return current_user  # FX.106 429 from _run_principal
    return trigger_workflow_run(
        version_id=version_id, run_request=run_request,
        background_tasks=background_tasks, db=db, current_user=current_user,
        idempotency_key_header=idempotency_key_header)


router.add_api_route(
    "/workflow-versions/{version_id}/run", trigger_workflow_run_programmatic,
    methods=["POST"], status_code=status.HTTP_202_ACCEPTED,
    route_class_override=_ProgrammaticRunRoute, include_in_schema=False)


@router.post(
    "/workflow-versions/{version_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: _err("Harbor credential pre-flight: `require_harbor_credentials` (dependencies.py:2097) raises 400 when a Harbor registry IS configured but the tenant is missing harbor_registry/harbor_username/harbor_password."),
        402: _err("Tenant is over its credit budget / purchase required (usage_metering_required, dp_budget_exhausted, plan upgrade gates)."),
        403: _err("Not permitted (folder can_process, Harbor credential pre-flight, inactive/onboarding-incomplete principal)."),
        429: _err("FX.106 per-plan / per-API-key rate limit: `_run_principal` (workflows.py:1078) returns the guard's canonical 429 from `check_api_rate_limits` (services/api_rate_limit_guard.py:744, body built at :480) with RateLimit-* headers and retry_after_s."),
        404: _err("Workflow version not found, or not reachable from this tenant."),
        409: _err("Idempotency collision: `idempotency_key_reused` (same key, different request body) or `idempotency_key_conflict` (concurrent first call with this key did not complete). The key stays bound to its run regardless of run outcome and is purged by age >= 24h."),
        422: _err("Request body rejected by the route itself (idempotency_key malformed or body/header disagree)."),
        502: _err("Failed to trigger workflow execution (enqueue failure); credits are refunded."),
        503: _err("Dispatch is paused for maintenance (`dispatch_paused`); try again shortly."),
    },
)
def trigger_workflow_run(
        version_id: int,
        run_request: schemas.WorkflowRunRequest,
        background_tasks: BackgroundTasks = None,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_harbor_credentials),  # Harbor required to run workflows
        idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Manually triggers a run of a specific workflow version with multiple files.

    Supports both single and multi-file workflow execution.
    If parent_folder_id is provided, checks that user has 'can_process' permission.

    Idempotency (2400GX-n8, optional `idempotency_key` body field or
    `Idempotency-Key` header; body wins when both are equal, 422 if they differ
    or the key is not 1-255 chars of [A-Za-z0-9._:-]):
      * Scope = (tenant_id, workflow_version_id, key); another tenant's same
        key is independent.
      * First call creates the run as today and records key -> run_id in the
        SAME DB transaction as the run row (no run without its key).
      * Replay with the same key (any run status) returns the original
        workflow_run_id with `idempotent_replay: true`; nothing is created,
        enqueued or charged.
      * Same key + different request body (stable hash of the normalized
        body) -> 409 `idempotency_key_reused`.
      * Concurrent first calls: a UNIQUE index decides; the loser rolls back
        its own run row and returns the winner's run.
      * Keys live >= 24h (services.run_idempotency.purge_expired may drop OLD
        key rows, never runs). If the run fails before dispatch (402 credits,
        502 enqueue) the key is released so the client can retry.
      * No key: behaviour is unchanged, including force_new. Idempotency is
        checked BEFORE the force_new / active-run collapse.
    """
    from services import run_idempotency as _idem
    try:
        _idem_key = _idem.resolve_key(run_request.idempotency_key, idempotency_key_header)
    except ValueError as _bad:
        raise HTTPException(status_code=422, detail=str(_bad))
    # GT.C2 — drain mode: refuse to start a NEW run while dispatch is paused
    # for maintenance. Clear 503 (not 429/503-gate) so the Execute button and
    # the Dashboard can surface "try again shortly" rather than failing the
    # run mid-dispatch. Checked BEFORE any WorkflowRun/credit is created.
    from services import platform_settings as _psr_exec
    try:
        if _psr_exec.get_setting("dispatch.paused", db=db):
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "dispatch_paused",
                "Dispatch is paused for maintenance; try again shortly",
                headers={"X-Error-Code": "DISPATCH_PAUSED"},
            )
    except (HTTPException, ApiError):
        raise
    except Exception as _e:  # noqa: BLE001 — a settings read failure must not
        # block Execute; the getter degrades to its default (False = dispatch
        # on) rather than raise, so this path is only for unexpected errors.
        logging.debug("drain-mode execute check failed (proceeding): %s", _e)

    # FX.41 (Q23=A) — tenant strictly OVER its (downgraded) concurrent-workflow
    # limit: refuse NEW runs (existing/in-flight runs are unaffected). Checked
    # BEFORE any run/credit is created, like the drain-mode check above.
    from utils.paid_import import optional_paid
    _downgrade_mod = optional_paid("services.downgrade_readonly_service")
    if _downgrade_mod is not None:
        try:
            _downgrade_mod.block_if_overage(db, current_user.tenant_id, "workflows")
        except HTTPException:
            raise
        except Exception as _e:  # noqa: BLE001
            logging.debug("overage workflow gate failed (proceeding): %s", _e)

    # Check folder permission if parent_folder_id is provided
    if run_request.parent_folder_id is not None:
        from permissions import check_user_capability_on_cog

        if not check_user_capability_on_cog(db, current_user.id, run_request.parent_folder_id, "can_process"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to process workflows on this folder"
            )

    db_version = crud.get_workflow_version_by_id(db, version_id=version_id, tenant_id=current_user.tenant_id)
    if db_version is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")

    # Enforce execute access on the parent workflow — running consumes the
    # tenant's credits and executes someone else's pipeline.
    parent_workflow = crud.get_workflow_by_id(db, workflow_id=db_version.workflow_id, tenant_id=current_user.tenant_id)
    if not _user_can_access_workflow(db, current_user, parent_workflow, "execute"):
        raise HTTPException(status_code=404, detail="Workflow version not found")

    _idem_hash = None
    if _idem_key:
        _idem_hash = _idem.request_hash(run_request)
        _idem_row = _idem.lookup_wait(db, current_user.tenant_id, version_id, _idem_key)
        if _idem_row is not None:
            return _idempotent_replay_response(_idem_row, _idem_hash, run_request)

    # ========================================================================
    # WSIP 8.1 — autonomous canary routing: ask the resolver which version
    # THIS run should execute (live canary window / promoted candidate).
    # Gate first (wsip.autonomous_operator_autodeploy_enabled, fail-closed):
    # gate off -> the resolver is never called, zero extra queries, and
    # nothing below changes. Guarded: any resolver error keeps the requested
    # version and logs the exception CLASS name only. The candidate must
    # belong to THIS workflow/tenant or it is ignored.
    # ========================================================================
    _canary_purpose = None
    try:
        from services.wsip_feature_flags import autonomous_operator_autodeploy_enabled
        if autonomous_operator_autodeploy_enabled(db, current_user.tenant_id):
            from services.autonomous_canary_routing import (
                content_hash_for_inputs,
                resolve_run_version,
            )
            _choice = resolve_run_version(
                db,
                tenant_id=current_user.tenant_id,
                workflow_id=db_version.workflow_id,
                requested_version_id=version_id,
                input_content_hash=content_hash_for_inputs(run_request.extra_variables),
            )
            if _choice.variant in ("canary", "promoted") and int(_choice.version_id) != int(version_id):
                _alt_version = crud.get_workflow_version_by_id(
                    db, version_id=_choice.version_id, tenant_id=current_user.tenant_id
                )
                if _alt_version is not None and int(_alt_version.workflow_id) == int(db_version.workflow_id):
                    db_version = _alt_version
                    version_id = int(_alt_version.id)
                    if _choice.variant == "canary":
                        # the marker autonomous_canary_stats.CANARY_PURPOSE tallies on
                        _canary_purpose = "autonomous_canary"
    except Exception as _e:  # noqa: BLE001 — a resolver bug must never block a run
        logging.warning("wsip 8.1 canary routing skipped (%s); running the requested version",
                        type(_e).__name__)

    # GG.8c sealed-storage: a sealed buyer copy stores no graph in its own rows.
    # Expand the publisher's listing snapshot server-side (with the buyer's
    # input-param overrides) so the run path — credit calc, secret/cog checks,
    # and the persisted run conf — all operate on the real pipeline. The
    # expansion is in-memory only; it is never written back to the buyer's rows.
    graph = _sealed.expand_for_run(db, db_version) if _sealed.is_sealed(db, parent_workflow) \
        else db_version.graph_json
    if not isinstance(graph, dict) or "nodes" not in graph or "edges" not in graph:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The stored workflow version has an invalid graph_json format."
        )

    # wep1.f — bind declared workflow parameters (422 before any run exists)
    from services import workflow_parameters as _wp
    # 1784 CE carve: wep_phase1_settings is a paid module (manifest deny). CE has no
    # workflow-parameter settings, so parameters_enabled() falls back to the module's
    # own default (OFF) and the binding block below is a no-op. SaaS unchanged.
    from utils.paid_import import optional_paid
    _wep = optional_paid("services.wep_phase1_settings")
    _wep_params_enabled = (
        _wep.parameters_enabled if _wep is not None
        else (lambda db, tenant_id: False)
    )
    _pre_param_nodes = graph.get("nodes") if isinstance(graph, dict) else None
    try:
        _decls = _wp.validate_parameter_declarations(graph or {})
        if run_request.parameters and not _wep_params_enabled(db, current_user.tenant_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "parameters_disabled",
                        "message": "workflow parameters are not enabled for this tenant"}
            )
        bindings = _wp.bind_run_parameters(_decls, run_request.parameters) \
            if _wep_params_enabled(db, current_user.tenant_id) else {}
        if _wep_params_enabled(db, current_user.tenant_id):
            graph = {**graph,
                     "nodes": _wp.substitute_parameters(
                         graph["nodes"], bindings,
                         {d["name"] for d in _decls})}
    except _wp.WorkflowParametersError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": e.code, "param_name": e.param_name, "message": str(e)}
        )

    # expr 10.4b — node ids whose config this request explicitly changed via
    # an explicit (user-supplied) ${param.<name>} substitution above. The
    # auto-apply block below must never clobber those with a trained value
    # (explicit user-supplied parameters win over auto-applied ones).
    _explicit_override_node_ids = set()
    if isinstance(_pre_param_nodes, list):
        _post_param_nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if isinstance(_post_param_nodes, list) and _post_param_nodes is not _pre_param_nodes:
            _pre_by_id = {
                str(n.get("id")): n for n in _pre_param_nodes if isinstance(n, dict)
            }
            for _n in _post_param_nodes:
                if not isinstance(_n, dict):
                    continue
                _nid = str(_n.get("id"))
                if _pre_by_id.get(_nid) != _n:
                    _explicit_override_node_ids.add(_nid)

    # expr 10.4b — auto-apply the latest trained parameter version for this
    # workflow (tenant-scoped).  Skips when the setting is off, when the graph
    # has no differentiable parameters, or when purpose == "differentiable_"
    # "training" (training forward dispatches set their own params).  A DB /
    # lookup failure never blocks the run (exception class logged only).
    _dn = optional_paid("services.differentiable_nodes")
    if (_dn is not None and
            (run_request.purpose is None or run_request.purpose != "differentiable_training")):
        # 1784 CE carve: differentiable_nodes is paid; with it absent there are no
        # trained parameters to auto-apply, so the whole block is skipped.
        from services.differentiable_nodes import (
            extract_parameters, apply_parameters,
        )
        from services.workflow_parameter_versions import list_versions
        try:
            _auto_apply = _psr.get_setting(
                "expr.auto_apply_trained_parameters", db=db,
            )
            if bool(_auto_apply):
                _params = extract_parameters(graph)
                if _params:
                    _latest = list_versions(
                        db, tenant_id=current_user.tenant_id,
                        workflow_id=db_version.workflow_id,
                    )
                    if _latest:
                        _row = _latest[0]  # newest first
                        _raw = _row.get("parameters")
                        if isinstance(_raw, dict):
                            _applied_params = _raw.get("params", _raw)
                        else:
                            _decoded = (
                                json.loads(_raw)
                                if isinstance(_raw, str) else _raw
                            )
                            _applied_params = (
                                _decoded.get("params", _decoded)
                                if isinstance(_decoded, dict) else _decoded
                            )
                        if isinstance(_applied_params, dict):
                            # explicit user-supplied values win: never
                            # auto-apply a trained value onto a node whose
                            # config this request already set explicitly.
                            if _explicit_override_node_ids:
                                _applied_params = {
                                    k: v for k, v in _applied_params.items()
                                    if k.rsplit(".", 1)[0]
                                    not in _explicit_override_node_ids
                                }
                            if _applied_params:
                                graph = apply_parameters(graph, _applied_params)
        except Exception as _ae:  # noqa: BLE001 — never block a run
            logger.warning(
                "expr 10.4b auto-apply skipped: %s",
                type(_ae).__name__,
            )

    # GG.UI.6b — structured purchase-required pre-flight: refuse with a 402
    # BEFORE a WorkflowRun is created, so the WB can render the blocked-run
    # CTA panel immediately instead of failing the run mid-dispatch.
    # Dispatch-time guards (worker_tasks enqueue) stay as defence in depth.
    # A sealed installed copy is checked against its source listing with the
    # expanded snapshot (the graph variable above already holds it); open-
    # licence installed copies and ordinary workflows skip the listing half.
    # CE-NOPAID-4.5b — this site now rides the entitlement PROVIDER seam
    # (services/entitlement_provider.py, CE-NOPAID-4.5a). The provider is the
    # Paid add-on's bodies once CE-NOPAID-4.7 registers it; until then the
    # resolved provider is still the null base (the add-on is absent on BOTH
    # trees), so the guard's own free functions - today's real bodies - keep
    # the paid tree byte-identical (same 402 items, same order).
    from services.entitlement_guard import preflight_run as _entitlement_preflight_run
    from services.entitlement_provider import (
        EntitlementProvider as _NullEntitlementProvider,
        get_entitlement_provider as _get_entitlement_provider,
    )
    _provider = _get_entitlement_provider(db)
    _check_listing = _sealed.is_sealed(db, parent_workflow)
    if type(_provider) is _NullEntitlementProvider:
        _missing_items = _entitlement_preflight_run(
            db,
            current_user.tenant_id,
            db_version,
            graph=graph,
            check_listing=_check_listing,
        )
    else:
        _missing_items = _provider.preflight_run(
            db,
            current_user.tenant_id,
            db_version,
            graph=graph,
            check_listing=_check_listing,
        )
    if _missing_items:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "purchase_required",
                "message": "This run needs items you don't own yet",
                "items": _missing_items,
            },
        )

    # FORTRESS.6.3 S3 — dp-budget run-start preflight, same position as the
    # purchase_required gate above (design /tmp/design_fortress_63.md §2):
    # refuse BEFORE any WorkflowRun exists so the WB can render a 402 CTA
    # instead of failing mid-dispatch. Dispatch-time reserve() in
    # worker_tasks.enqueue_task_internal stays as defence in depth (covers
    # re-dispatch/retry paths that skip this run-start gate).
    # dp spec shape mirrors services/differentiable_nodes.py's either/or
    # union: node["data"]["dp"] wins over legacy node["config"]["dp"];
    # neither present = not a DP node. Zero cost (no import, no settings
    # read) for a graph with no DP nodes at all.
    _fortress63_dp_nodes = []
    for _fortress63_node in (
        (graph.get("nodes") or []) if isinstance(graph, dict) else []
    ):
        if not isinstance(_fortress63_node, dict):
            continue
        _fortress63_dp_spec = None
        for _fortress63_src in ("data", "config"):
            _fortress63_container = _fortress63_node.get(_fortress63_src)
            if isinstance(_fortress63_container, dict) and isinstance(
                _fortress63_container.get("dp"), dict
            ):
                _fortress63_dp_spec = _fortress63_container["dp"]
                break
        if _fortress63_dp_spec is not None:
            _fortress63_dp_nodes.append(_fortress63_dp_spec)
    # 1788 CE carve: dp_budget is a paid module; with it absent there is no DP
    # epsilon budget to enforce, so the whole preflight block is skipped (a CE
    # graph carrying a dp spec simply runs — SaaS unchanged when present).
    from utils.paid_import import optional_paid
    _dp_mod = optional_paid("services.dp_budget") if _fortress63_dp_nodes else None
    if _fortress63_dp_nodes and _dp_mod is not None:
        from services.dp_budget import DpBudgetExhausted as _DpBudgetExhausted
        try:
            _dp_mod.preflight_run(
                db, current_user.tenant_id, parent_workflow.id,
                _fortress63_dp_nodes,
            )
        except ValueError as _dp_spec_exc:
            # FORTRESS.6.2 (922): validate_dp raises ValueError for a
            # malformed dp spec. Workflows saved before 911's save-time
            # gate can still carry one, so this path stays reachable —
            # surface it as a fixed 422 code instead of an unhandled 500.
            # Exception text never reaches the response.
            logging.warning(
                "dp preflight rejected malformed dp spec on workflow "
                "id=%s: %s", parent_workflow.id, type(_dp_spec_exc).__name__,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "dp_spec_invalid",
                    "message": "A node's dp spec is invalid and could not "
                               "be validated.",
                },
            )
        except _DpBudgetExhausted as _dp_exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    **_dp_exc.as_detail(),
                    "message": "Differential-privacy epsilon budget "
                               "exhausted for this period.",
                },
            )

    # payg 6.7 — payment-setup-required pre-flight for unverified postpaid
    # tenants: a tenant that switched to PAYG postpaid billing but whose
    # SetupIntent was never verified (Tenant.payg_setup_verified_at NULL,
    # stamped by the SetupIntent webhook) can never pay for a run, so refuse
    # with a 402 BEFORE any WorkflowRun exists, same position as the gates
    # above. Fail-OPEN exactly like the budget gate: a gate error is logged,
    # the session rolled back, and the run proceeds. Platform admins are not
    # gated (the router, not the helper, owns that exemption).
    if not current_user.is_platform_admin:
        try:
            _setup_needed = payg_setup_gate.setup_required(db, current_user.tenant_id)
        except Exception as _setup_exc:  # noqa: BLE001 — never block Execute
            logging.warning("payg setup gate failed (proceeding): %s", _setup_exc)
            db.rollback()  # a failing gate query can abort the DB transaction;
            # without this, every later query on this request raises
            # InFailedSqlTransaction instead of letting the run proceed.
            _setup_needed = False
        if _setup_needed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "payment_setup_required",
                    "message": "Payment setup is not required in MOD Community Edition. "
                               "(this check is disabled in this edition). "
                               "Run allowed.",
                },
            )

    # sz-1.6.1 — pre-dispatch budget forecast gate: when the tenant has a
    # workflow_budgets.max_cost_per_run_credits cap (sz-1.2.1 storage row,
    # first reader here), the graph's forecast cost (cost_forecaster
    # estimate_forecast per node, credits == credits, no conversion) is refused
    # with a 402 BEFORE any WorkflowRun/credit exists so the WB can render the
    # over-budget panel instead of failing mid-dispatch. Fail-OPEN: a gate
    # error is logged and the run proceeds — the credit reservation below stays
    # the authoritative spend control.
    # 1784 CE carve: run_budget_gate is paid and documented FAIL-OPEN ("never block
    # Execute"), so in CE the gate is simply skipped: _budget stays None and the
    # credit reservation below remains the authoritative spend control. SaaS unchanged.
    _budget_gate = optional_paid("services.run_budget_gate")
    try:
        _budget = _budget_gate.precheck_run_budget(
            db,
            current_user.tenant_id,
            parent_workflow.id,
            (graph or {}).get("nodes") or [],
        ) if _budget_gate is not None else None
    except Exception as _budget_exc:  # noqa: BLE001 — never block Execute
        logging.warning("run-budget gate failed (proceeding): %s", _budget_exc)
        db.rollback()  # a failing gate query can abort the DB transaction;
        # without this, every later query on this request (prohibited-use
        # gate, run creation) raises InFailedSqlTransaction instead of
        # letting the run proceed fail-open.
        _budget = None
    if _budget is not None and not _budget["allowed"]:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "budget_exceeded",
                "forecast_credits": _budget["forecast_credits"],
                "cap": _budget["cap"],
                "he_unbenchmarked_nodes": _budget.get("he_unbenchmarked_nodes") or [],
                "he_dominance_warning": _budget.get("he_dominance_warning"),
                **_fortress69_epsilon_detail(_budget),
            },
        )

    # FX.132 part 3 (Q120=A) — EU AI Act Art. 8 prohibited-use gate: a run
    # whose graph contains a node tagged 'prohibited-use:<practice>' is
    # refused (403 + audit) until the tenant acknowledged the AUP clause
    # (tenant setting ai.prohibited_use_ack). Checked BEFORE any WorkflowRun
    # is created, like the drain/overage gates above.
    # 1788 CE carve: ai_prohibited_use is paid; CE has no AUP-acknowledgement
    # settings, so the gate is skipped (same CE posture as the commercial-safe
    # gate below — SaaS unchanged when the module is present).
    _ai_prohibited = optional_paid("services.ai_prohibited_use")
    if _ai_prohibited is not None:
        _ai_prohibited.check_run_prohibited_use(
            db,
            current_user.tenant_id,
            graph,
            user_id=current_user.id,
            workflow_version_id=version_id,
        )

    # GSEC FX.72 part 2 (owner decision Q55=C) — no-training-without-opt-in:
    # a graph containing a customer-training node (the SDXL LoRA trainers) is
    # refused (403 + audit) unless THIS tenant opted in (tenant setting
    # ai.training_opt_in, default False). Checked BEFORE any WorkflowRun is
    # created, like the FX.132 gate above.
    # 1788 CE carve: ai_training_consent is paid; CE has no training opt-in
    # setting, so the opt-in gate is skipped (SaaS unchanged when present).
    _ai_consent = optional_paid("services.ai_training_consent")
    if _ai_consent is not None:
        _ai_consent.check_run_training_opt_in(
            db,
            current_user.tenant_id,
            graph,
            user_id=current_user.id,
            workflow_version_id=version_id,
        )

    # FX.149 part 2b (Q136=A) — per-tenant external-AI node gate: a run whose
    # graph contains a node tagged 'external-ai' (nodes that send tenant data
    # to EXTERNAL AI providers, e.g. NIM Bridge cloud nodes) is refused
    # (403 + audit) unless the tenant passes the external-LLM policy
    # (platform egress.external_llm_enabled / sealed_mode plus tenant opt-in
    # ai.external_llm_requires_tenant_opt_in); fails closed on read errors.
    # Checked BEFORE any WorkflowRun is created, like the FX.132 gate above.
    # 1788 CE carve: external_ai_node_gate is paid; CE has no external-LLM
    # policy settings, so the gate is skipped (SaaS unchanged when present).
    _ext_ai_gate = optional_paid("services.external_ai_node_gate")
    if _ext_ai_gate is not None:
        _ext_ai_gate.check_run_external_ai(
            db,
            current_user.tenant_id,
            graph,
            user_id=current_user.id,
            workflow_version_id=version_id,
        )

    # GSEC FX.79 part 2b — data-marking blocks. (a) A workflow whose graph
    # sends data to an external AI provider (an 'external-ai'-tagged node,
    # same tag as the gate above) is REFUSED (403 + audit, reason
    # data_marking_blocks_external_ai) when the workflow itself OR any input
    # file carries a live mark whose label sets blocks_external_ai. (b) The
    # same mark scope with blocks_shared_gpu forces the WHOLE run onto the
    # tenant's own workers (audit reason data_marking_tenant_gpu_only): the
    # conf stamp below rides every task payload (ProjectInfo) and the claim
    # SQL (worker_licenses owner_type TENANT_OWNED + owner_tenant_id) keeps
    # shared / harvested / marketplace GPUs out. Checked BEFORE any
    # WorkflowRun is created, like the gates above. Unmarked items: no-op.
    # FX.90 p2b (Q76=A) — malware quarantine input gate: a run whose input
    # cogs include a quarantined artefact is refused (423 Locked + audit)
    # BEFORE any WorkflowRun/credit is created, like the gates above.
    from services import files_service as _fx90_run
    try:
        _fx90_run.check_run_quarantined_inputs(
            db,
            tenant_id=current_user.tenant_id,
            cog_ids=run_request.cog_ids,
            user_id=current_user.id,
        )
    except _fx90_run.QuarantinedArtefactError as _fx90_exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "detail": _fx90_exc.message,
                "code": _fx90_exc.code,
                "cog_ids": (_fx90_exc.details or {}),
            },
            headers={"X-Error-Code": "MALWARE_QUARANTINED"},
        )

    from utils.paid_import import optional_paid
    _dm = optional_paid("services.data_markings")
    _dm_mark_labels: dict = {}
    if _dm is not None:  # FX.162.p5 — CE carve: no markings -> unmarked run (no GPU stamp)
        try:
            _dm.check_run_data_markings(
                db,
                current_user.tenant_id,
                graph,
                workflow_id=parent_workflow.id,
                cog_ids=run_request.cog_ids,
                user_id=current_user.id,
                workflow_version_id=version_id,
                labels_out=_dm_mark_labels,
            )
        except _dm.DataMarkingBlockedError as _dm_exc:
            # 403 envelope mirrors assert_shareable (p2a) so the WB renders the
            # same "blocked by data marking" panel; code + labels let the FE name
            # the offending marks. The audit row was already written in the gate.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "detail": "blocked by data marking",
                    "code": _dm_exc.code,
                    "labels": _dm_exc.details.get("labels") if _dm_exc.details else None,
                    "action": "workflow_run",
                },
                headers={"X-Error-Code": _dm_exc.code},
            )
    _dm_gpu_stamp = None
    if _dm_mark_labels.get("blocks_shared_gpu"):
        _dm_gpu_stamp = {
            "reason": "data_marking_tenant_gpu_only",
            "labels": _dm_mark_labels["blocks_shared_gpu"],
        }

    if _dm is not None:  # FX.162.p5 — CE carve: no markings -> no US-person gate
        # GSEC FX.79 p4b — the same run scope (the workflow itself + its input
        # assets) is restricted to confirmed US persons when any live mark
        # resolves to a label def with requires_us_person (the ITAR-class set).
        # One shared check function (assert_us_person_access) per resource, so
        # the run-start refusal is byte-identical to the read/stream/export
        # refusals; the denial row rides the p4a record path inside the gate.
        for _dm_us_scope in [("workflow", parent_workflow.id)] + [
                ("asset", _cog_id) for _cog_id in (run_request.cog_ids or [])
                if _cog_id is not None]:
            try:
                _dm.assert_us_person_access(
                    db,
                    current_user.tenant_id,
                    _dm_us_scope[0],
                    _dm_us_scope[1],
                    user_id=current_user.id,
                    action="run",
                )
            except _dm.DataMarkingBlockedError as _dm_us_exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "detail": _dm_us_exc.message,
                        "code": _dm_us_exc.code,
                        "labels": (_dm_us_exc.details or {}).get("labels"),
                        "action": "workflow_run",
                    },
                    headers={"X-Error-Code": _dm_us_exc.code},
                )

    # FX.98 part 1 (Q84=A) — likeness consent gate: a run whose graph
    # contains a node tagged 'likeness' is refused (403 + audit) unless the
    # node's params link a valid, in-scope consent (likeness_consent_id) or
    # attest the subject is synthetic (likeness_synthetic: true). Checked
    # BEFORE any WorkflowRun is created, right beside the prohibited-use
    # gate above. No-op when the graph has no likeness-tagged node.
    # 1788 CE carve: likeness_consent is paid; CE has no consent ledger, so the
    # likeness gate is skipped (SaaS unchanged when present).
    _likeness_gate = optional_paid("services.likeness_consent")
    if _likeness_gate is not None:
        _likeness_gate.check_run_likeness(
            db,
            current_user.tenant_id,
            graph,
            user_id=current_user.id,
            workflow_version_id=version_id,
        )

    # GSEC FX.99 part 2 — commercial-safe-only gate: a run whose graph
    # contains a node with a non_commercial licence is refused (422)
    # when the tenant's models.commercial_safe_only setting is on
    # (default ON for paid hosted/Enterprise, OFF for CE/free/on-prem).
    # Checked BEFORE any WorkflowRun is created, like the gates above.
    # 1784 CE carve: commercial_safe is paid; with it absent every node licence is
    # allowed (the module's own CE posture), so the run-time licence gate is skipped.
    _csafe_run = optional_paid("services.commercial_safe")
    if _csafe_run is not None:
        _csafe_run.check_run_commercial_safe(
            db,
            current_user.tenant_id,
            graph,
            user_id=current_user.id,
            workflow_version_id=version_id,
        )

    # Strict per-secret enforcement at run time: the user RUNNING the workflow
    # must have access to EVERY secret it references. The worker injects
    # secret_refs by tenant identity at execution, so without this a user granted
    # only 'execute' on a shared workflow could use secrets they were never
    # granted. Reject with the missing names so the UI can offer "request access".
    referenced_secret_names = set()
    for _node in (graph.get("nodes") or []):
        for _ref in ((_node.get("data") or {}).get("secret_refs") or []):
            _name = _ref.get("secret_name")
            if _name:
                referenced_secret_names.add(_name)
    if referenced_secret_names:
        missing_secrets = workflows_domains_workflows_service.find_missing_secret_access(
            db, referenced_secret_names, current_user,
        )
        if missing_secrets:
            raise HTTPException(status_code=403, detail={
                "code": "secret_access_denied",
                "message": "You don't have access to secrets this workflow uses.",
                "missing_secrets": sorted(missing_secrets),
            })

    # ========================================================================
    # GX.R4 — ephemeral run inputs. A run may feed an input node a short-TTL
    # file via  inputs: {"<node_id>": {"ephemeral_input_id": <id>}}.  Resolve
    # the references NOW — tenant-owned and not-expired (else 404 / 410) — and
    # append the backing hidden Cogs to run_request.cog_ids so the input node
    # receives each file EXACTLY like an uploaded cog (COG_IDS ->
    # resolve-cog-hash -> content-hash staging; no worker/container change).
    # The reference key is stripped from the stored per-node inputs so it never
    # reaches a node's env/extraVariables.
    # ========================================================================
    from services import ephemeral_run_inputs as _eri
    _eph_cog_ids, _eph_sanitized_inputs = _eri.resolve_for_run(
        db, current_user.tenant_id, run_request.inputs,
    )
    _eph_cog_set = set(_eph_cog_ids)
    if _eph_cog_ids:
        run_request.cog_ids = (run_request.cog_ids or []) + _eph_cog_ids

    # ========================================================================
    # FX.90 p3b — async-scan input gate. A file whose malware scan is still
    # PENDING (p3a queued it for a large upload) or already INFECTED cannot
    # feed a run: 409 naming each file + its state, BEFORE any WorkflowRun/
    # credit exists. Runs AFTER the ephemeral resolution above so the hidden
    # backing Cogs of ephemeral inputs are gated too, and a file with NO
    # marker (a small multipart upload, never scanned) is allowed — see the
    # lead-decision comment in services/ephemeral_run_inputs.py:scan_state.
    # Byte-identical no-op while malware_scan.enabled is False (the default).
    # ========================================================================
    try:
        _eri.check_run_input_scans(
            db, current_user.tenant_id, run_request.cog_ids,
            user_id=current_user.id,
        )
    except _eri.InputScanBlockedError as _fx90b_exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": _fx90b_exc.message,
                "code": _fx90b_exc.code,
                "blocked_inputs": (_fx90b_exc.details or {}),
            },
            headers={"X-Error-Code": "MALWARE_SCAN_INPUT_PENDING"},
        )

    # Strict per-file enforcement: the runner must have access to every INPUT
    # file (cog) it passes — otherwise they could run a workflow against files
    # they don't own (enumerate ids -> process someone else's file -> read it
    # via the output). Applies to everyone — owners and platform admins included
    # (ISO/DoD tenant isolation: no admin bypass).
    #
    # GX.R4: the backing Cogs of ephemeral inputs are EXCLUDED here — they are
    # tenant-scoped objects the run references by id (already tenant-validated +
    # not-expired in resolve_for_run above) and are an implementation detail, so
    # they must not be gated by the per-file USER view grants. The existence +
    # tenant checks below still cover them (get_cogs_by_ids is tenant-scoped).
    if run_request.cog_ids:
        _eph_checked_cog_ids = [c for c in run_request.cog_ids if c not in _eph_cog_set]
        # Gate 30: read access resolves through the authorize() chokepoint
        # (check_user_capability_on_cog "view" == the legacy VIEWER level).
        missing_cog_ids = workflows_domains_workflows_service.find_missing_cog_view_access(
            db, _eph_checked_cog_ids, current_user,
        )
        if missing_cog_ids:
            raise HTTPException(status_code=403, detail={
                "code": "file_access_denied",
                "message": "You don't have access to input files this run uses.",
                "missing_cog_ids": sorted(missing_cog_ids),
            })

    # Reject input FILES that have no stored content (orphaned cogs: a Cog row
    # whose Version/ContentBlob was never created — e.g. a failed/partial
    # upload). Without this the run is created and then fails DEEP inside the
    # Input Validator container with a cryptic "Cog or version not found" 404,
    # burning a workflow_run and confusing the user. Fail fast at submit with a
    # clear, actionable message instead. FILE cogs only — folders
    # (MASTER_FOLDER/ASSET_FOLDER/...) legitimately have no version, and
    # PLACEHOLDER_FILE is intentionally contentless. Runs for everyone
    # (platform admins included): a contentless file is broken for all.
    if run_request.cog_ids:
        contentless_inputs = workflows_domains_workflows_service.find_contentless_inputs(
            db, run_request.cog_ids,
        )
        if contentless_inputs:
            raise HTTPException(status_code=422, detail={
                "code": "input_has_no_content",
                "message": (
                    "One or more selected inputs have no stored file content "
                    "(an incomplete or failed upload). Re-upload them and run again."
                ),
                "contentless_inputs": contentless_inputs,
            })

    # ========================================================================
    # workflow-agentic-execution-loop §workflow-run-execution — validate
    # the purpose flag. Accept 'production' (default) and 'wizard_test_run';
    # reject unknown values 422. Sandbox runs require wizard_session_id +
    # iteration to route the isolated MinIO prefix.
    # ========================================================================
    _ALLOWED_PURPOSES = {"production", "wizard_test_run"}
    purpose = (run_request.purpose or "production").lower()
    if purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "unknown_purpose",
                "allowed": sorted(_ALLOWED_PURPOSES),
                "got": purpose,
            },
        )
    if purpose == "wizard_test_run":
        if not run_request.wizard_session_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "wizard_session_id_required_for_sandbox_purpose",
                },
            )
        if run_request.wizard_test_run_iteration is None or run_request.wizard_test_run_iteration < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "wizard_test_run_iteration_required_for_sandbox_purpose",
                },
            )
        # workflow-agentic-execution-loop §wizard-test-run-dispatch —
        # per-tenant rate limit on sandbox dispatches. Default 100/hour
        # per tenant, overridable via env. On hit, check_rate_limit
        # raises HTTPException(429, Retry-After) which surfaces to the
        # wizard agent as "rate limit hit, fall back to non-loop turn".
        import os as _os_rate_limit
        from utils.rate_limiter import check_rate_limit
        _limit = int(_os_rate_limit.getenv("WIZARD_TEST_RUN_RATE_LIMIT_PER_HOUR", "100"))
        check_rate_limit(
            f"wizard_test_run:tenant:{current_user.tenant_id}",
            max_requests=_limit,
            window_seconds=3600,
        )

    # ========================================================================
    # GX.D1 — per-run placement override: validate the pinned worker BEFORE
    # a run row exists. A worker_id that does not exist or is not owned by /
    # assigned to THIS tenant is a 404 (other tenants' workers must not be
    # referenceable). worker_tag alone needs no ownership check — it selects
    # a pool, and the claim path only ever matches this tenant's tasks to
    # workers authorized for this tenant.
    # ========================================================================
    run_placement = None
    if run_request.placement is not None:
        from services.workflow_placement import validate_worker_placement
        _pl_norm, _pl_err = validate_worker_placement(
            db, run_request.placement, current_user.tenant_id)
        if _pl_err:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=_pl_err)
        run_placement = _pl_norm

    # ========================================================================
    # DEDUPLICATION CHECK - Prevent duplicate workflow submissions
    # ========================================================================
    # Check if this workflow version is already running for this user
    # This prevents re-runs when user navigates away and back
    from datetime import datetime, timedelta

    # Only block if the SAME user has an ACTIVE run (queued/running) of this version
    # This prevents double-clicks but allows:
    # - Different users to run the same version
    # - Same user to re-run after completion/failure
    # - Batch processing with same version
    # workflow-agentic-execution-loop §workflow-run-execution — dedup
    # must be scoped to the SAME purpose. Otherwise a sandbox test-run
    # could match a production run (or vice versa) and the second
    # dispatch would return the wrong run id with wrong sandbox semantics.
    # Airflow-like concurrent runs: force_new starts another run even while one
    # is active (the explicit "Execute new run" choice). Plain Execute stays
    # deduped so an accidental double-click doesn't double-fire. On-prem has no
    # concurrent-process cap (check_concurrent_process_limit → no limit), so a
    # forced run just starts immediately.
    active_run = None
    if not getattr(run_request, "force_new", False):
        active_run = workflows_domains_workflows_crud.get_active_run_for_dedup(
            db, version_id, current_user.id, purpose,
        )

    if active_run:
        status_str = active_run.status.value if active_run.status else "queued"
        logging.info(
            f"ℹ️ Workflow version {version_id} already has an active run ({active_run.id}) "
            f"for user {current_user.id}. Status: {status_str}."
        )
        return {
            "workflow_run_id": active_run.id,
            "status": status_str,
            "message": f"This workflow is already running (status: {status_str}). You can cancel and restart if needed.",
            "duplicate": True,
            "started_at": active_run.started_at.isoformat() if active_run.started_at else None,
            "orchestrator_run_id": active_run.orchestrator_run_id  # For cancellation
        }

    # ========================================================================
    # CREDIT CHECK - Calculate and reserve credits before workflow execution
    # Enabled by default for SaaS. Set CREDITS_ENABLED=false to disable
    # (onprem / BYO-infrastructure deployments). Default mirrors main.py's
    # /platform/features endpoint so both surfaces agree.
    # ========================================================================
    import os
    # GS.PSR.8 — billing.credits_enabled via the platform-settings
    # registry (DB > env > True), same key main.py's /platform/features
    # resolves so both surfaces keep agreeing.
    from utils.paid_import import optional_paid
    _cm = optional_paid("utils.credit_manager")
    if _cm is None:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "credit manager module not installed"},
        )
    from utils.credit_manager import credits_enabled as _credits_enabled
    credits_enabled = _credits_enabled(db)

    estimated_credits = 0
    credit_estimate = {"estimated_credits": 0, "node_count": 0, "file_count": 0, "input_data_size_gb": 0}

    # workflow-agentic-execution-loop §wizard-test-run-dispatch — sandbox
    # runs do NOT charge user credits. Platform-level cost tracking is
    # deferred to a future change; here we just skip the user-facing
    # charge so the wizard's iteration loop doesn't bill the tenant.
    if purpose == "wizard_test_run":
        credits_enabled = False

    # sz-tcp.22-wiring: the locked-quote payload, computed from the SAME
    # compute_workflow_quote() call the /quote display endpoint uses
    # (routers/workflows_domains/workflows.py ~line 520), so what the user
    # saw before clicking Run is exactly what gets locked to this run.
    # None when credits are disabled, this is a wizard sandbox run, or the
    # quote computation itself errors (never let quote-locking block a
    # dispatch — the reservation/charge path below is unaffected by this).
    locked_quote_dict = None

    if credits_enabled:
        # CARVE-U2: utils.workflow_credits is a Paid module — absent in the CE
        # carve, so this bare import used to raise ModuleNotFoundError (500) on
        # the CE image. Load through optional_paid and answer the documented
        # 402 edition posture instead.
        _wc_run = optional_paid("utils.workflow_credits")
        if _wc_run is None:
            return JSONResponse(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                content={"error": "workflow credits module not installed"},
            )
        from utils.workflow_credits import (
            calculate_workflow_credits,
            check_and_reserve_credits,
            compute_workflow_quote,
        )

        credit_estimate = calculate_workflow_credits(
            db=db,
            graph=graph,
            cog_ids=run_request.cog_ids,
            tenant_id=current_user.tenant_id
        )
        estimated_credits = credit_estimate["estimated_credits"]

        logging.info(f"Workflow credit estimate: {estimated_credits} credits")
        logging.info(f"   Nodes: {credit_estimate['node_count']}, Files: {credit_estimate['file_count']}")
        logging.info(f"   Input: {credit_estimate['input_data_size_gb']}GB")

        try:
            exact_quote = compute_workflow_quote(
                db, (graph or {}).get("nodes", []) if isinstance(graph, dict) else [], {}
            )
            # compute_workflow_quote is explicitly a SINGLE per-run quote
            # (its own docstring: "does NOT multiply by file_count/
            # runs_per_node"). calculate_workflow_credits above already
            # worked out how many times each node actually runs for THIS
            # dispatch (1x for a grouped run, file_count x for a batch
            # run) as `runs_per_node` — reuse that same factor here so the
            # locked amount matches what check_and_reserve_credits reserves
            # instead of under-quoting every multi-file batch run.
            runs_per_node = credit_estimate.get("runs_per_node") or 1
            locked_quote_dict = {
                "quoted_credits": int(exact_quote["total_credits"]) * runs_per_node,
                "breakdown": [
                    {**row, "credits": row.get("credits", 0) * runs_per_node, "runs_per_node": runs_per_node}
                    for row in exact_quote.get("breakdown", [])
                ],
            }
        except Exception as e:  # noqa: BLE001
            logging.warning(f"sz-tcp.22-wiring: compute_workflow_quote failed, run will dispatch without a locked quote: {e}")

    # Verify all cog_ids exist and belong to tenant (exclude soft-deleted)
    cogs = workflows_domain_crud.get_cogs_by_ids(db, run_request.cog_ids, current_user.tenant_id)

    if len(cogs) != len(run_request.cog_ids):
        raise HTTPException(status_code=404, detail="One or more files not found")

    # Verify metadata cogs if provided (exclude soft-deleted)
    metadata_cogs = []
    if run_request.metadata_cog_ids:
        metadata_cogs = workflows_domain_crud.get_cogs_by_ids(
            db, run_request.metadata_cog_ids, current_user.tenant_id
        )

        if len(metadata_cogs) != len(run_request.metadata_cog_ids):
            raise HTTPException(status_code=404, detail="One or more metadata files not found")

    # TPN CS-1 Compliance: Use tenant-specific bucket for content isolation
    # (BYO-S3 aware: pass db so an active StorageConnection resolves to the
    # tenant's own bucket instead of the house default)
    from utils.minio_client import get_tenant_bucket_name
    tenant_bucket = get_tenant_bucket_name(current_user.tenant_id, db)

    # Get workflow name for project_info
    workflow_name = db_version.workflow.name if db_version.workflow else "Workflow"

    full_conf = {
        "tenant_id": current_user.tenant_id,  # Top-level for log path isolation
        "tenant_bucket": tenant_bucket,  # TPN CS-1: Tenant-specific storage bucket
        # GC.MWL.7c: the user's keycloak_token is deliberately NOT threaded
        # into `conf` any more. Nothing in-process ever consumed it beyond
        # re-propagating it into per-node task_payload (which the Go worker
        # never reads — storage credentials are minted on-demand via
        # GET /workers/tasks/{id}/credentials, never from the task payload).
        # Carrying it here just meant it was also durably written into
        # `WorkflowRun.parameters`. See crud.workflow_cud.create_workflow_run.
        "nodes": graph["nodes"],
        "edges": graph["edges"],
        "project_info": {
            "tenant_id": current_user.tenant_id,
            "tenant_bucket": tenant_bucket,  # TPN CS-1: Tenant-specific storage bucket
            "user_id": current_user.id,  # For permission checks in DAGs
            "cog_ids": run_request.cog_ids,  # Array of file IDs to process
            "metadata_cog_ids": run_request.metadata_cog_ids or [],  # Optional metadata files
            "parent_folder_id": run_request.parent_folder_id,  # Optional folder context
            "keycloak_user_id": current_user.keycloak_user_id,
            "workflow_name": workflow_name,  # For export DAGs to know the workflow name
            "extra_variables": run_request.extra_variables or {},  # Runtime configuration from API request
            # GX.R1 — explicit per-node overrides: {node_id_or_label: {var: value}}.
            # Merged into each node's effective extraVariables at dispatch
            # (see services.workflow_orchestrator._apply_run_extra_variables);
            # per-node values win over the global extra_variables above.
            # GX.R4 — sanitized: ephemeral_input_id references are resolved to
            # cog_ids above and stripped here so they never reach node env.
            "inputs": _eph_sanitized_inputs or {},
        }
    }
    # wep1.f — bind declared workflow parameters
    if bindings:
        full_conf["workflow_parameters"] = bindings
    # workflow-agentic-execution-loop §wizard-test-run-dispatch — when
    # the run is a sandboxed test-run, plumb the wizard session id +
    # iteration through `conf` so the orchestrator can route output
    # writes to the isolated `_wizard_test_runs/...` prefix per task.
    if purpose == "wizard_test_run":
        full_conf["purpose"] = "wizard_test_run"
        full_conf["wizard_session_id"] = run_request.wizard_session_id
        full_conf["wizard_test_run_iteration"] = run_request.wizard_test_run_iteration
        # Sandbox enforcement defaults (worker reads these to enforce).
        # See spec §wizard-test-run-dispatch and design §1 for rationale.
        full_conf["sandbox_timeout_s"] = int(os.getenv("WIZARD_TEST_RUN_TIMEOUT_S", "60"))
        full_conf["sandbox_max_output_mb"] = int(os.getenv("WIZARD_TEST_RUN_MAX_OUTPUT_MB", "100"))

    # Airflow-style "restart from node": carry cache-reuse intent into the run's
    # stored conf so the per-task cache-read gate (_maybe_cache_short_circuit)
    # reuses unchanged upstream outputs for THIS run, and force-re-runs the
    # explicitly-restarted node(s) + downstream via cache_bust_nodes.
    if getattr(run_request, "reuse_cache", False):
        full_conf["reuse_cache"] = True
    if getattr(run_request, "cache_bust_nodes", None):
        full_conf["cache_bust_nodes"] = run_request.cache_bust_nodes

    # GX.D1 — the validated per-run placement override rides the persisted
    # run conf (WorkflowRun.parameters["placement"]) so dispatch resolves
    # run > node > workflow precedence for every task of this run.
    if run_placement is not None:
        full_conf["placement"] = run_placement

    # GSEC FX.79 part 2b — tenant-GPU-only stamp for a marked run. Lives in
    # project_info (NOT a sibling key) because the orchestrator copies
    # conf["project_info"] verbatim into every node's task payload
    # (node_conf["ProjectInfo"]), root AND downstream tasks alike — so the
    # claim SQL's data-marking guard (worker_tasks, which reads
    # task_payload->'ProjectInfo'->>'data_marking_tenant_gpu_only') sees it
    # on EVERY task of this run and restricts it to the tenant's OWN workers.
    # None for ordinary (unmarked) runs — conf is byte-identical.
    if _dm_gpu_stamp is not None:
        full_conf["project_info"]["data_marking_tenant_gpu_only"] = _dm_gpu_stamp

    # GX.R3 — per-run run-completion webhook (in addition to any per-workflow
    # subscription). The one-shot signing secret is generated NOW — before the
    # run response returns — so it can be shown to the caller exactly once;
    # only its tenant-key-encrypted form is persisted (in the run's stored
    # conf), so the post-commit enqueue can sign deliveries later.
    run_webhook_secret = None
    if run_request.callback_url:
        from services import run_completion_webhooks as _rcw
        _refusal = _rcw.ssrf_check(run_request.callback_url)
        if _refusal:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_callback_url",
                    "message": _refusal,
                },
            )
        _tenant = workflows_domain_crud.get_tenant(db, current_user.tenant_id)
        if _tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        run_webhook_secret = _rcw.generate_signing_secret()
        full_conf["callback_url"] = run_request.callback_url
        full_conf["run_webhook_secret_encrypted"] = _rcw.encrypt_secret(
            db, _tenant, run_webhook_secret
        )

    # Encrypt refresh_token if provided (backward compatible)
    encrypted_refresh_token = None
    if run_request.refresh_token:
        logging.info(f"🔐 Encrypting refresh_token for workflow (length: {len(run_request.refresh_token)} chars)")
        try:
            from utils.encryption import encrypt_value
            from services import key_provider
            # Use tenant's encryption key for refresh token
            tenant = workflows_domain_crud.get_tenant(db, current_user.tenant_id)
            if tenant and tenant.encryption_key:
                decrypted_tenant_key = key_provider.unwrap_tenant_key(
                    tenant.encryption_key, tenant.id, db=db)
                encrypted_str = encrypt_value(run_request.refresh_token, decrypted_tenant_key)
                # Convert to bytes for LargeBinary column
                encrypted_refresh_token = encrypted_str.encode('utf-8')
                logging.info(f"   ✓ Encrypted successfully (encrypted length: {len(encrypted_str)} chars)")
            else:
                logging.warning(f"   ⚠ No tenant encryption key found, cannot encrypt refresh_token")
        except Exception as e:
            logging.warning(f"   ❌ Could not encrypt refresh_token: {e}. Workflow will run without secret access capability.")
    else:
        logging.info("ℹ️ No refresh_token provided in workflow request")

    # Generate run_name: "Workflow Name v# - file1.jpg, file2.jpg"
    # Note: workflow_name was already extracted above for project_info
    version_num = db_version.version_number
    file_names = [cog.name for cog in cogs[:3]]  # First 3 file names
    if len(cogs) > 3:
        file_names.append(f"... +{len(cogs) - 3} more")
    files_str = ", ".join(file_names) if file_names else "no files"
    run_name = f"{workflow_name} v{version_num} - {files_str}"

    # overage-spend-cap §1 — hard-stop a tenant that has reached their monthly
    # overage cap BEFORE creating the run (no overage-incurring compute past the
    # cap). Raises 402 with a "request a higher limit" message.
    from utils.usage_enforcement import check_overage_cap
    check_overage_cap(db=db, tenant_id=current_user.tenant_id, raise_on_cap=True)
    # Q.2: node outputs are recorded even over the cap (the compute is already
    # paid), so a tenant already over its storage cap cannot start new runs.
    from utils.usage_enforcement import enforce_storage_quota
    enforce_storage_quota(db, current_user.tenant_id, 0, user=current_user)

    if _idem_key:
        # Claim BEFORE creating the run (same transaction): the UNIQUE index
        # decides concurrent first calls; a loser never creates a run row.
        if not _idem.claim(db, current_user.tenant_id, version_id, _idem_key, _idem_hash):
            db.rollback()
            _idem_row = _idem.lookup_wait(db, current_user.tenant_id, version_id, _idem_key)
            if _idem_row is None:
                raise HTTPException(status_code=409, detail={
                    "error": "idempotency_key_conflict",
                    "message": "A concurrent request with this key did not complete; retry."})
            return _idempotent_replay_response(_idem_row, _idem_hash, run_request)

    _idem_args = (current_user.tenant_id, version_id, _idem_key) if _idem_key else None
    try:
        db_run = crud.create_workflow_run(
            db=db,
            version=db_version,
            user_id=current_user.id,
            conf=full_conf,
            encrypted_refresh_token=encrypted_refresh_token,
            run_name=run_name,
            purpose=_canary_purpose or purpose,
            idempotency=_idem_args,
        )
    except Exception as e:
        # If column doesn't exist yet, try without it (backward compatibility)
        if "encrypted_refresh_token" in str(e) or "run_name" in str(e):
            logging.warning(f"Database not updated with new columns. Creating workflow with fallback.")
            db_run = crud.create_workflow_run(
                db=db,
                version=db_version,
                user_id=current_user.id,
                conf=full_conf,
                purpose=_canary_purpose or purpose,
                idempotency=_idem_args,
            )
        else:
            raise

    if _idem_key:
        _idem.attach_run(db, current_user.tenant_id, version_id, _idem_key, db_run.id)

    # sz-tcp.22-wiring: lock the quote the user saw before Run against this
    # run_id. Idempotent (services.run_price_quote.lock_quote never
    # overwrites), additive-only — never raises into the dispatch path.
    if locked_quote_dict is not None:
        try:
            from services.run_price_quote import lock_quote
            lock_quote(db, db_run, locked_quote_dict)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"sz-tcp.22-wiring: failed to lock run price quote for run {db_run.id}: {e}")

    # ========================================================================
    # DEDUCT CREDITS - Reserve credits for this workflow run
    # ========================================================================
    if credits_enabled:
        success, message = check_and_reserve_credits(
            db=db,
            tenant_id=current_user.tenant_id,
            workflow_run_id=db_run.id,
            estimated_credits=estimated_credits,
            user_id=current_user.id,
            allow_advance=True,
            purpose=purpose
        )

        if not success:
            # ISO 27001 no-hard-delete (MEMORY iso-no-hard-delete): WorkflowRun
            # carries SoftDeleteWithActorMixin and is in database.py's
            # soft-delete listener, so stamp `deleted_at` and RETAIN the row
            # on the credit-fail rollback — live reads drop the tombstone.
            from datetime import datetime as _dt, timezone as _tz
            db_run.deleted_at = _dt.now(_tz.utc)
            if _idem_key:
                _idem.release(db, current_user.tenant_id, version_id, _idem_key)
            db.commit()

            logging.warning(f"Insufficient credits for workflow run. {message}")
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "error": "Insufficient credits",
                    "message": message,
                    "required_credits": estimated_credits,
                    "estimate": credit_estimate
                }
            )

        logging.info(f"Reserved {estimated_credits} credits for workflow run {db_run.id}")

        # 15b.4.s4 — stage shadow at the reservation point: BOTH pricing
        # numbers for this run recorded (legacy flat node-cost math vs the
        # task-profile estimate), the one `billing.pricing_model` charges
        # already debited above. Observation only: its own try/except, it
        # never changes a balance and a shadow failure never fails the run.
        try:
            from utils.credit_manager import shadow_at_reservation
            shadow_at_reservation(
                db,
                run_id=db_run.id,
                tenant_id=current_user.tenant_id,
                graph=graph,
                cog_ids=run_request.cog_ids,
                runs_per_node=(credit_estimate.get("runs_per_node") or 1),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("15b.4.s4 reservation shadow failed for run %s: %s",
                           db_run.id, e)

    # ========================================================================
    # WSIP 3.4 — pre-dispatch failure prediction (hook) — shadow by default,
    # enforce blocks, off = no-op.  Runs AFTER credit reservation so the
    # blocked path can refund; BEFORE concurrent-process increment so the
    # blocked run never enters the counter (the decrement rollback below
    # only applies to the orchestrator-exit path at ~1866).
    # ========================================================================
    _warn_pred_p = None  # set only on warn+allow; None = no prediction to surface
    _wsip_run_ctx = {
        "node_count": len(graph.get("nodes", [])) if isinstance(graph, dict) else 0,
        "edge_count": len(graph.get("edges", [])) if isinstance(graph, dict) else 0,
        "input_count": len(cogs) if cogs else 0,
        "input_size_bytes": credit_estimate.get("input_data_size_bytes",
                                                credit_estimate.get("input_data_size_gb", 0)) * (1024 ** 3),
    }
    try:
        # 2400LR-ceMNF-F2 CE carve: services.failure_prediction_hook is a paid
        # module (open-core/manifest.yaml deny), so the bare import above raised
        # ModuleNotFoundError on EVERY run and its except logged a WARNING.
        # optional_paid answers None for an edition carve (no import attempted,
        # nothing logged) -> verdict stays None and the block below is a no-op.
        # SaaS path: same call, same args, same order, same try/except.
        from utils.paid_import import optional_paid
        _fph = optional_paid("services.failure_prediction_hook")
        verdict = None
        if _fph is not None:
            verdict = _fph.pre_dispatch_prediction(
                db,
                tenant_id=current_user.tenant_id,
                workflow_id=db_version.workflow_id,
                workflow_run_id=db_run.id,
                run_ctx=_wsip_run_ctx,
                confirm_predicted_failure=bool(run_request.confirm_predicted_failure),
            )
        if verdict is not None and not verdict.allow:
            # Blocked run — concurrent counter NOT yet incremented, so skip
            # decrement_concurrent_processes; only refund reserved credits.
            db_run.status = models.TaskStatus.BLOCKED
            _p = verdict.predicted_failure_probability
            db_run.error_message = (
                f"Predicted failure (p={_p:.2f}); "
                "resubmit with confirm_predicted_failure=true to run anyway"
                if _p is not None else
                "Predicted failure; resubmit with confirm_predicted_failure=true to run anyway"
            )
            db.commit()
            # wsip run-observability (spec workflow-run-observability): the
            # failure-prediction signal hangs off the blocked run, in the same
            # shape drift_sweep.py:207 uses. Publish-after-commit, best-effort:
            # a failed event row must never fail the (already blocked) run.
            try:
                from datetime import datetime as _wsip_dt, timezone as _wsip_tz
                db.add(models.WorkflowRunEvent(
                    workflow_run_id=db_run.id,
                    source="failure_prediction",
                    level="warn",
                    message="failure_predicted_above_threshold",
                    event_metadata={
                        "event_type": "failure_predicted_above_threshold",
                        "workflow_id": int(db_version.workflow_id),
                        "predicted_probability": _p,
                        "threshold": getattr(verdict, "threshold", None),
                        "similar_past_failure_count": getattr(
                            verdict, "similar_past_failure_count", None),
                        "predicted_at": (getattr(verdict, "predicted_at", None)
                                         or _wsip_dt.now(_wsip_tz.utc).isoformat()),
                    },
                ))
                db.commit()
            except Exception as exc:  # noqa: BLE001 — the event is best-effort
                try:
                    db.rollback()
                except Exception:  # the session is already usable
                    pass
                logging.warning("WSIP failure-prediction event failed for run %s: %s",
                                db_run.id, exc)
            _cm_pred = optional_paid("utils.credit_manager")
            if _cm_pred is None:
                logging.info(
                    f"CE carve: credit manager module not installed - no credit "
                    f"ledger to refund for predicted-failure-blocked run {db_run.id}"
                )
            else:
                _cm_pred.CreditManager.refund_credits_for_failed_workflow(
                    db=db,
                    tenant_id=current_user.tenant_id,
                    amount=estimated_credits,
                    workflow_run_id=db_run.id,
                    failure_reason="predicted_failure_blocked",
                )
            _p_out = _p if _p is not None else 0.0
            if _idem_key:  # 2400GX-n8F: confirm retry must not hit idempotency_key_reused
                _idem.release(db, current_user.tenant_id, version_id, _idem_key)
                db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "predicted_failure",
                    "workflow_run_id": db_run.id,
                    "predicted_failure_probability": _p_out,
                    "similar_past_failure_count": getattr(
                        verdict, "similar_past_failure_count", None),
                    "confirm_field": "confirm_predicted_failure",
                },
            )
        elif verdict is not None and verdict.warn and verdict.allow:
            # Warn path: keep a local for the final-success response body.
            _warn_pred_p = verdict.predicted_failure_probability
            # wsip run-observability (spec workflow-run-observability): the
            # spec's trigger is "probability above threshold", which covers
            # the confirmed-override warn+allow path too, not only the
            # blocked path. No explicit commit here — it rides the function's
            # next db.commit() (capacity-queue or orchestrator dispatch
            # below), so nothing is published before that commit happens.
            try:
                from datetime import datetime as _wsip_dt, timezone as _wsip_tz
                db.add(models.WorkflowRunEvent(
                    workflow_run_id=db_run.id,
                    source="failure_prediction",
                    level="warn",
                    message="failure_predicted_above_threshold",
                    event_metadata={
                        "event_type": "failure_predicted_above_threshold",
                        "workflow_id": int(db_version.workflow_id),
                        "predicted_probability": _warn_pred_p,
                        "threshold": getattr(verdict, "threshold", None),
                        "similar_past_failure_count": getattr(
                            verdict, "similar_past_failure_count", None),
                        "predicted_at": (getattr(verdict, "predicted_at", None)
                                         or _wsip_dt.now(_wsip_tz.utc).isoformat()),
                    },
                ))
            except Exception as exc:  # noqa: BLE001 — the event is best-effort
                logging.warning("WSIP failure-prediction event failed for run %s: %s",
                                db_run.id, exc)
        else:
            _warn_pred_p = None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — prediction failure must NEVER block a run
        logger.warning("WSIP 3.4 prediction hook failed for run %s: %s; proceeding",
                       db_run.id, exc)

    # ========================================================================
    # CONCURRENT PROCESS TRACKING - Increment process counter
    # ========================================================================
    from utils.usage_enforcement import check_concurrent_process_limit, increment_concurrent_processes

    # Check if at limit (this doesn't block, just informs - we queue workflows)
    # exclude_run_id=db_run.id — crud.create_workflow_run already inserted
    # this run with status=QUEUED + is_capacity_queued=False, so without
    # the exclude every Free-tier (cap=1) user would always count their
    # own freshly-created run against themselves and get capacity-queued
    # at "1/1 slots busy."
    can_start, queue_message = check_concurrent_process_limit(
        db=db,
        tenant_id=current_user.tenant_id,
        raise_on_limit=False,  # Don't raise - we'll queue instead
        exclude_run_id=db_run.id,
    )

    if not can_start:
        # LDF-4 §7.2 — actually queue the run instead of "log and proceed".
        # Mark capacity-queued + skip the orchestrator dispatch; the
        # scheduler tick (services.scheduler._run_concurrency_dispatch)
        # picks it up when an in-flight slot frees.
        db_run.is_capacity_queued = True
        db_run.capacity_queue_reason = (queue_message or "concurrent process limit reached")[:200]
        db_run.status = models.TaskStatus.QUEUED
        db.commit()
        logging.info(
            f"⏸️ Workflow run {db_run.id} queued for capacity: {db_run.capacity_queue_reason}"
        )
        # burst-to-saas slice 3 — the run is now KNOWN to be waiting, which is
        # the one moment the spill decision has a real wait to weigh. Offer it
        # to burst (background, best-effort). If policy says spill and the
        # target takes it, it runs on MOD instead of waiting here; otherwise it
        # stays capacity-queued and runs locally exactly as below. Default-off,
        # so this is a no-op for tenants that have not opted in.
        try:
            from services import burst_auto
            burst_auto.consider_capacity_spill(db, db_run, background_tasks)
        except Exception:
            logging.exception(f"burst: consider_capacity_spill failed for run {db_run.id}")
        # 202 Accepted — request was valid + accepted but processing
        # is deferred; the FE polls /road/runs to learn when it
        # transitions out of capacity-queued state.
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "workflow_run_id": db_run.id,
                "status": "queued",
                "is_capacity_queued": True,
                "queue_reason": db_run.capacity_queue_reason,
                "message": "Workflow accepted but waiting for an in-flight slot to free up.",
                **({"webhook_secret": run_webhook_secret} if run_webhook_secret else {}),
            },
        )

    # Increment concurrent process counter (legacy cached counter; the
    # in-flight gate above uses live query, but the counter is still
    # surfaced by some legacy routes).
    increment_concurrent_processes(db=db, tenant_id=current_user.tenant_id)
    logging.info(f"📊 Incremented concurrent processes for tenant {current_user.tenant_id}")

    # Create workflow run file records for all input files
    # Query for active File records that correspond to each Cog
    for cog in cogs:
        # Find the active File record associated with this Cog (exclude soft-deleted)
        file_record = workflows_domains_workflows_crud.get_file_by_cog_id(db, cog.id, current_user.tenant_id)

        if file_record:
            crud.create_workflow_run_file(db, workflow_run_id=db_run.id, file_id=file_record.id, title=cog.name)
        else:
            # If no File record exists for this Cog, skip it or raise an error
            logging.warning(f"No File record found for Cog {cog.id} ({cog.name}), skipping WorkflowRunFile creation")
            # Alternatively, you could raise an HTTPException here if File records are required

    # full_conf carries the run's input data through to the orchestrator.
    full_conf['workflow_run_id'] = db_run.id

    logging.info(f"Starting workflow run {db_run.id} for {len(run_request.cog_ids)} file(s)")


    # Audit: log workflow start (CMMC AU-2, SOC2)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="WORKFLOW_STARTED",
            details={"workflow_run_id": db_run.id, "workflow_name": workflow_name, "file_count": len(run_request.cog_ids)},
        ))
        db.commit()
    except Exception:
        pass

    try:
        from services.orchestration_consumer import (
            orchestration_via_queue_enabled,
            enqueue_orchestration_task,
        )
        if orchestration_via_queue_enabled():
            # scale-tiered-workloads D2 / Phase 4 — dogfood MOD's own task queue.
            # Instead of running start_workflow_execution as an in-process
            # BackgroundTask in this web worker (which does all the per-file DAG
            # planning/enqueue inline, so Execute latency grows with fan-out),
            # write ONE `orchestration` ApiTaskQueue row carrying the run id +
            # full_conf and return. The background role claims it and does the
            # planning off the request path, so Execute stays cheap and bounded
            # regardless of how many input files the workflow fans out over.
            # GC.STW.6: enqueue_orchestration_task also fires
            # pg_notify('mod_orchestration', id) in the SAME transaction as that
            # INSERT, so the background consumer's LISTEN (GC.STW.7) wakes near-
            # instantly instead of waiting for its 0.5s poll fallback.
            enqueue_orchestration_task(
                db,
                tenant_id=current_user.tenant_id,
                workflow_run_id=db_run.id,
                full_conf=full_conf,
            )
            logging.info(
                "Enqueued orchestration task for workflow run %s via MOD queue "
                "(MOD_ORCHESTRATION_VIA_QUEUE on)", db_run.id)
        else:
            # DEFAULT (flag off) — unchanged behaviour: run the orchestrator as
            # an in-process request-worker BackgroundTask after the 202.
            from services.workflow_orchestrator import start_workflow_execution
            from database import SessionLocal
            background_tasks.add_task(start_workflow_execution, SessionLocal, full_conf, db_run.id)
    except Exception as e:
        logging.error(f"Failed to start workflow run {db_run.id}: {e}")

        # Rollback: mark run as failed so dedup check doesn't block retries
        db_run.status = models.TaskStatus.FAILED
        if _idem_key:
            _idem.release(db, current_user.tenant_id, version_id, _idem_key)
        db.commit()

        # Rollback: decrement concurrent process counter
        from utils.usage_enforcement import decrement_concurrent_processes
        decrement_concurrent_processes(db=db, tenant_id=current_user.tenant_id)

        # Rollback: refund reserved credits
        # CARVE-U2: utils.credit_manager is a Paid module (absent in the CE
        # carve). The bare import used to sit INSIDE this try, so an absent
        # ledger was swallowed as "could not refund" (fail-open). Load through
        # optional_paid and call through the binding: absent -> explicit CE
        # skip; an ImportError from a PRESENT module is no longer masked.
        _cm_refund = optional_paid("utils.credit_manager")
        if _cm_refund is None:
            logging.info(
                f"CE carve: credit manager module not installed - no credit "
                f"ledger to refund for failed run {db_run.id}"
            )
        else:
            try:
                _cm_refund.CreditManager.refund_credits_for_failed_workflow(
                    db=db,
                    tenant_id=current_user.tenant_id,
                    amount=estimated_credits,
                    workflow_run_id=db_run.id,
                    failure_reason=f"Orchestrator dispatch failed: {str(e)}"
                )
                logging.info(f"💳 Refunded {estimated_credits} credits for failed workflow run {db_run.id}")
            except Exception as refund_err:
                logging.error(f"⚠ Could not refund credits for workflow run {db_run.id}: {refund_err}")

        logger.error("Failed to trigger workflow execution: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to trigger workflow execution. Please try again."
        )

    _resp = {
        "detail": f"Workflow execution initiated for {len(run_request.cog_ids)} file(s)",
        "workflow_run_id": db_run.id,
        "cog_ids": run_request.cog_ids,
        **({"idempotent_replay": False} if _idem_key else {}),
        **({"webhook_secret": run_webhook_secret} if run_webhook_secret else {}),
    }
    if _warn_pred_p is not None:
        _resp["failure_prediction"] = {
            "warn": True,
            "predicted_failure_probability": _warn_pred_p,
        }
    return _resp

@router.get("/{workflow_id}/runs", response_model=List[schemas.WorkflowRun])
def list_workflow_runs(
        workflow_id: int,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "started_at",
        order: str = "desc",
        include_details: bool = False,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Lists all historical runs for a specific workflow.

    Lightweight by default (summary only) so the RunRail 4s poll is cheap. Pass
    include_details=true to also load each run's node_runs/run_files (used by the
    version-load preview restore, which fetches a single latest run).
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    # Run history is visible only to users who can view the workflow (owner or
    # granted); 404 (not 403) so a non-visible workflow's existence isn't leaked.
    if not _user_can_view_workflow(db, current_user, workflow_id):
        raise HTTPException(status_code=404, detail="Workflow not found")

    runs = crud.get_workflow_runs(
        db, workflow_id=workflow_id, limit=limit, offset=offset,
        sort_by=sort_by, order=order, include_details=include_details,
        purposes_exclude=USER_HIDDEN_RUN_PURPOSES,
    )
    # GG.8b sealed: blank the per-node graph node_id (identifies WHICH pipeline
    # step each NodeRun is) for sealed non-exempt buyers; status/timing kept.
    if include_details and runs and _sealed.should_redact_graph(db, db_workflow, current_user):
        for run in runs:
            for nr in run.get("node_runs", []):
                nr["node_id"] = None
    return runs

@router.post(
    "/workflow-runs/{run_id}/cancel",
    status_code=status.HTTP_200_OK,
    responses={
        401: _err("Auth dependency `require_active_and_onboarded_user` -> `get_current_user` -> `_resolve_current_user` (dependencies.py:223) raises 401 on a revoked/invalid API key or bad bearer."),
        403: _err("Auth dependency `require_active_and_onboarded_user`: `require_active_user` (dependencies.py:488) raises 403 for an inactive principal; `require_onboarding_complete` (dependencies.py:498) raises 403 before onboarding is complete."),
        404: _err("Workflow run not found, or not viewable by this principal."),
        409: _err("`already_terminal` — the run is already terminal (succeeded/failed/cancelled); only queued or running runs can be cancelled."),
    },
)
def cancel_workflow_run(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Cancel a running or queued workflow run. Marks the run as FAILED in
    the database and cancels its pending/claimed/running ApiTaskQueue
    tasks. Platform admins can cancel any run, others only their tenant's.
    """
    from datetime import datetime

    # Platform admins can cancel all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    # Only a user who can view the run's workflow may cancel it (owner/granted).
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    if db_run.status not in [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "already_terminal",
            f"Run is already '{db_run.status.value}'; only queued or running runs can be cancelled.",
        )

    # Update our database using a conditional update to prevent race conditions
    # Only update if status is still QUEUED or RUNNING
    previous_status = db_run.status

    # Set error_message so the run-detail UI surfaces the cancellation
    # cause inline. The status stays FAILED (no separate CANCELLED enum
    # value today) but the message disambiguates user-cancel from a
    # genuine system failure.
    cancel_message = f"Cancelled by user (user_id={current_user.id})"
    result = workflows_domain_crud.conditional_cancel_run(
        db, run_id, completed_at=datetime.now(), error_message=cancel_message,
    )

    # Check if the update actually happened (row was affected)
    if result.rowcount == 0:
        db.rollback()
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "already_terminal",
            "The run's status changed before the cancel landed; it is already terminal.",
        )

    # Refresh to get updated state
    db.refresh(db_run)

    # Store cancellation info in parameters
    if db_run.parameters is None:
        db_run.parameters = {}
    db_run.parameters['cancelled_by'] = current_user.id
    db_run.parameters['cancelled_at'] = datetime.now().isoformat()
    db_run.parameters['cancellation_reason'] = 'User cancelled'

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(db_run, 'parameters')

    db.commit()

    # Decrement concurrent process counter only if we actually cancelled a running/queued workflow
    from utils.usage_enforcement import decrement_concurrent_processes
    if previous_status in [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]:
        decrement_concurrent_processes(db=db, tenant_id=current_user.tenant_id)

    # Cancel all active worker tasks for this workflow run
    worker_tasks_cancelled = 0
    try:
        worker_tasks_cancelled = workflows_domain_service.cancel_worker_tasks_for_run(db, run_id)
        if worker_tasks_cancelled > 0:
            logging.info(f"Cancelled {worker_tasks_cancelled} worker task(s) for workflow run {run_id}")
    except Exception as e:
        logging.warning(f"Failed to cancel worker tasks: {e}")

    # Also mark queued node_runs as blocked. Stamp the same cancel
    # message on each so the per-node failure card in the run-detail
    # UI explains the FAIL came from a user cancel, not a real error.
    try:
        workflows_domain_service.block_node_runs_for_cancel(
            db, run_id, completed_at=datetime.now(), error_message=cancel_message,
        )
    except Exception as e:
        logging.warning(f"Failed to update node run statuses: {e}")

    # Outbound webhooks (WS4.Q20b) — a user cancel terminalizes the run here
    # (status stays FAILED; no separate CANCELLED enum value today), so the
    # finalize block never fires. Dispatch the completion fan-out here with
    # the "cancelled" status so subscribers get `workflow.canceled`.
    # Background thread + own session (same pattern as
    # routers/worker_tasks.py finalize).
    try:
        from services.outbound_webhooks import dispatch_workflow_completion_background
        dispatch_workflow_completion_background(
            tenant_id=db_run.tenant_id,
            workflow_run_id=run_id,
            status_str="cancelled",
        )
        # GX.R3 — enqueue run-completion webhook deliveries (post-commit).
        try:
            from services.run_completion_webhooks import (
                enqueue_run_completion_background as _rcw_enqueue,
            )
            _rcw_enqueue(
                tenant_id=db_run.tenant_id,
                workflow_run_id=run_id,
                status_str="cancelled",
            )
        except Exception:
            logging.debug(
                f"run_webhooks: enqueue scheduling failed for run {run_id}",
                exc_info=True,
            )
    except Exception:
        logging.debug(
            f"outbound webhook: scheduling failed for run {run_id}",
            exc_info=True,
        )

    return {
        "status": "cancelled",
        "workflow_run_id": run_id,
        "worker_tasks_cancelled": worker_tasks_cancelled,
        "message": "Workflow run cancelled successfully"
    }

@router.post("/workflow-runs/{run_id}/pause", status_code=status.HTTP_200_OK)
def pause_workflow_run(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Airflow-style pause: HOLD the run's frontier. Nodes already running finish,
    but no new downstream nodes are enqueued until resume. The run stays RUNNING
    (paused is a separate flag, not a status)."""
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run or not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if db_run.status not in [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]:
        raise HTTPException(status_code=400,
                            detail=f"Cannot pause a run with status '{db_run.status.value}'.")
    # sz-2.9 — per-tenant paused-run quota. An already-paused run is idempotent:
    # re-pausing it must NOT consume/409 at the cap, so the quota is checked
    # only when this call would newly hold the run.
    if not db_run.paused:
        from services import paused_run_limits as paused_run_limits_mod
        verdict = paused_run_limits_mod.can_pause(db, db_run.tenant_id)
        if not verdict["allowed"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "paused_run_quota_exceeded",
                    "paused": verdict["paused"],
                    "quota": verdict["quota"],
                    "message": (
                        f"Tenant already holds {verdict['paused']} of {verdict['quota']} "
                        "allowed paused runs (workflows.paused_run_quota). "
                        "Resume or cancel one before pausing another."
                    ),
                },
            )
    db_run.paused = True
    db.commit()
    logging.info("⏸ Workflow run %s paused by user %s", run_id, current_user.id)
    return {"workflow_run_id": run_id, "paused": True,
            "message": "Run paused — nodes in progress finish; no new nodes start until resume."}

@router.post("/workflow-runs/{run_id}/resume", status_code=status.HTTP_200_OK)
def resume_workflow_run(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Resume a paused run: clear the hold and re-open the frontier. Re-kicks the
    DAG advance for every completed task (the dispatch guard dedupes, so only
    genuinely-ready, not-yet-enqueued downstream nodes start)."""
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run or not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if db_run.status not in [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]:
        raise HTTPException(status_code=400,
                            detail=f"Cannot resume a run with status '{db_run.status.value}'.")
    db_run.paused = False
    db.commit()
    # sz-2.9 — clear the paused_seen_at / timeout-cancel markers so a later
    # re-pause starts a fresh timeout window. Best-effort: a marker-clear
    # failure must never block the resume itself.
    try:
        from services import paused_run_limits as paused_run_limits_mod
        paused_run_limits_mod.clear_paused_markers(db, db_run)
    except Exception as marker_exc:  # noqa: BLE001 — resume must not be blocked
        logging.warning("resume: paused-run marker clear failed for run %s: %s",
                        run_id, marker_exc)
        db.rollback()
    # Re-open the frontier: replay downstream dispatch for each completed task.
    dispatched = 0
    try:
        from routers.worker_tasks import _check_and_dispatch_downstream
        completed = workflows_domain_crud.get_completed_tasks_for_run(db, run_id)
        for t in completed:
            try:
                _check_and_dispatch_downstream(db, t)
                dispatched += 1
            except Exception as _adv_exc:  # noqa: BLE001 — one branch shouldn't wedge resume
                logging.warning("resume: downstream re-kick failed for task %s: %s", t.id, _adv_exc)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logging.warning("resume: frontier re-open failed for run %s: %s", run_id, e)
    logging.info("▶ Workflow run %s resumed by user %s (re-kicked %d completed tasks)",
                 run_id, current_user.id, dispatched)
    return {"workflow_run_id": run_id, "paused": False,
            "message": "Run resumed — frontier re-opened."}

@router.post("/workflow-runs/{run_id}/pause-node", status_code=status.HTTP_200_OK)
def pause_node_in_run(
        run_id: int,
        body: dict = Body(...),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Airflow-style per-node pause: mark a graph node as a PAUSE POINT for this
    run. When the DAG reaches it (parents done), it HOLDS instead of dispatching
    — the run waits at that node until resume-node. Other branches keep going."""
    node_id = str((body or {}).get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run or not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    from sqlalchemy.orm.attributes import flag_modified
    nodes = list(db_run.paused_nodes or [])
    if node_id not in nodes:
        nodes.append(node_id)
    db_run.paused_nodes = nodes
    flag_modified(db_run, "paused_nodes")
    db.commit()
    logging.info("⏸ node %s marked as pause point on run %s by user %s", node_id, run_id, current_user.id)
    return {"workflow_run_id": run_id, "paused_nodes": nodes,
            "message": f"Node {node_id} will pause the run when reached."}

@router.post("/workflow-runs/{run_id}/resume-node", status_code=status.HTTP_200_OK)
def resume_node_in_run(
        run_id: int,
        body: dict = Body(...),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Clear a node's pause point and re-open the frontier so a held node (and its
    downstream) continue. Re-kicks downstream dispatch for the run's completed
    tasks (dispatch guard dedupes)."""
    node_id = str((body or {}).get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run or not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    from sqlalchemy.orm.attributes import flag_modified
    nodes = [n for n in (db_run.paused_nodes or []) if n != node_id]
    db_run.paused_nodes = nodes
    flag_modified(db_run, "paused_nodes")
    db.commit()
    # 2400LR-bp1: an ENTRY node has no upstream completion to re-kick it.
    try:
        from services.workflow_orchestrator import dispatch_released_root_node
        if dispatch_released_root_node(db, db_run, node_id):
            db.commit()
    except Exception as e:  # noqa: BLE001
        logging.warning("resume-node: entry-node dispatch failed for run %s: %s", run_id, e)
    # Re-open the frontier so the just-unpaused node dispatches if its parents are done.
    try:
        from routers.worker_tasks import _check_and_dispatch_downstream
        completed = workflows_domain_crud.get_completed_tasks_for_run(db, run_id)
        for t in completed:
            try:
                _check_and_dispatch_downstream(db, t)
            except Exception:  # noqa: BLE001
                pass
        db.commit()
    except Exception as e:  # noqa: BLE001
        logging.warning("resume-node: frontier re-open failed for run %s: %s", run_id, e)
    logging.info("▶ node %s pause point cleared on run %s by user %s", node_id, run_id, current_user.id)
    return {"workflow_run_id": run_id, "paused_nodes": nodes,
            "message": f"Node {node_id} resumed."}

@router.post("/workflow-runs/{run_id}/node-action", status_code=status.HTTP_200_OK)
def node_run_action(
        run_id: int,
        body: dict = Body(...),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Perform one in-run action on a node. body = {node_id, action}.

    Phase 1 actions: skip (mark success) · force_fail · cancel_downstream_now ·
    cancel_downstream_drain. All in-run, all recorded append-only.
    """
    from datetime import datetime, timezone
    from sqlalchemy.orm.attributes import flag_modified
    node_id = str((body or {}).get("node_id") or "").strip()
    action = str((body or {}).get("action") or "").strip()
    if not node_id or not action:
        raise HTTPException(status_code=400, detail="node_id and action are required")
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=current_user.tenant_id)
    if not db_run or not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if action not in _ALL_NODE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown or not-yet-supported action '{action}'")

    now = datetime.now(timezone.utc)
    nr = _noderun_for(db, db_run, node_id)
    prior = (nr.status.value if nr and hasattr(nr.status, "value") else str(nr.status)) if nr else None

    if action == "skip":
        # Synthetic completed task so downstream sees this parent as done.
        synth = models.ApiTaskQueue(
            workflow_run_id=run_id, node_id=node_id, tenant_id=db_run.tenant_id,
            queue_name="node_action", priority=0,
            task_payload={"node_kind": "operator_skip", "node_data": {}},
            status=models.ApiTaskStatus.COMPLETED, max_attempts=1,
            output_namespace="operator_skip",
            result_payload={"outputs": [], "operator_skip": True},
            started_at=now, completed_at=now,
        )
        db.add(synth)
        if nr:
            nr.status = models.TaskStatus.SUCCESS
        # Skipping resolves a pause point — clear the yellow paused state.
        _clear_node_pause(db_run, node_id)
        # Lift any BLOCKED downstream (e.g. from a prior failure of this node).
        for dn in _downstream_node_ids(db_run, node_id):
            dnr = _noderun_for(db, db_run, dn)
            if dnr and dnr.status == models.TaskStatus.BLOCKED:
                dnr.status = models.TaskStatus.QUEUED
        _node_action_audit(db, db_run, current_user, action, node_id, prior, {"skipped": True})
        db.commit()
        db.refresh(synth)
        try:
            from routers.worker_tasks import _check_and_dispatch_downstream
            _check_and_dispatch_downstream(db, synth)
            db.commit()
        except Exception as e:  # noqa: BLE001
            logging.warning("node-action skip: downstream dispatch failed for run %s: %s", run_id, e)
        return {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                "status": "success", "message": f"Node {node_id} skipped (marked success)."}

    if action == "force_fail":
        synth = models.ApiTaskQueue(
            workflow_run_id=run_id, node_id=node_id, tenant_id=db_run.tenant_id,
            queue_name="node_action", priority=0,
            task_payload={"node_kind": "operator_force_fail", "node_data": {}},
            status=models.ApiTaskStatus.FAILED, max_attempts=1,
            output_namespace="operator_force_fail",
            error_message=f"Force-failed by user {current_user.id}",
            started_at=now, completed_at=now,
        )
        db.add(synth)
        if nr:
            nr.status = models.TaskStatus.FAILED
        _clear_node_pause(db_run, node_id)  # force-fail resolves any pause point
        _node_action_audit(db, db_run, current_user, action, node_id, prior)
        db.commit()
        db.refresh(synth)
        try:
            from routers.worker_tasks import _check_and_dispatch_downstream
            _check_and_dispatch_downstream(db, synth)  # is_failure → blocks downstream
            db.commit()
        except Exception as e:  # noqa: BLE001
            logging.warning("node-action force_fail: block downstream failed for run %s: %s", run_id, e)
        return {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                "status": "failed", "message": f"Node {node_id} force-failed."}

    if action == "cancel_downstream_now":
        downstream = _downstream_node_ids(db_run, node_id)
        cancelled = 0
        for dn in downstream:
            tasks = workflows_domains_workflows_crud.get_tasks_by_node_and_statuses(
                db, run_id, dn, _ACTIVE_TASK_STATUSES,
            )
            for t in tasks:
                t.status = models.ApiTaskStatus.CANCELLED
                t.completed_at = now
                cancelled += 1
            dnr = _noderun_for(db, db_run, dn)
            if dnr and dnr.status in (models.TaskStatus.QUEUED, models.TaskStatus.RUNNING):
                dnr.status = models.TaskStatus.BLOCKED
        _node_action_audit(db, db_run, current_user, action, node_id, prior,
                           {"downstream_nodes": downstream, "cancelled_tasks": cancelled})
        db.commit()
        return {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                "cancelled_tasks": cancelled, "downstream_nodes": downstream,
                "message": f"Cancelled {cancelled} downstream task(s) after {node_id}."}

    if action == "cancel_downstream_drain":
        drain = list(db_run.drain_nodes or [])
        if node_id not in drain:
            drain.append(node_id)
        db_run.drain_nodes = drain
        flag_modified(db_run, "drain_nodes")
        _node_action_audit(db, db_run, current_user, action, node_id, prior, {"drain_nodes": drain})
        db.commit()
        return {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                "drain_nodes": drain,
                "message": f"Run will stop downstream of {node_id} after it finishes."}

    if action in _NODE_ACTIONS_PHASE2:  # rerun_same_data | rerun_with_downstream
        # Re-running a node REOPENS a terminal run. enqueue_task_internal refuses
        # to dispatch into a CANCELLED/FAILED run ("run_terminal_skipped"), so
        # without this a re-run on a finished run silently does nothing (the
        # observed "rerun does nothing" — the run had already failed). Airflow-
        # style: re-running resurrects the run to RUNNING so the new attempt (and
        # the finaliser) proceed.
        _run_state = str(getattr(db_run.status, "value", db_run.status) or "").upper()
        if _run_state in ("FAILED", "CANCELLED", "COMPLETED", "FAILURE", "SUCCESS"):
            db_run.status = models.TaskStatus.RUNNING
            logging.info("node-action %s: reopened terminal run %s (%s→RUNNING)",
                         action, run_id, _run_state)
        # Copy the node's latest REAL attempt (skip operator synthetics) so the
        # re-run uses identical inputs; enqueue a NEW attempt (guard bypassed via
        # manual_rerun) in the next output_namespace. Prior attempt is preserved.
        latest = workflows_domains_workflows_crud.get_latest_real_task_for_node(db, run_id, node_id)
        if latest is None:
            # No prior REAL execution (e.g. the node was operator-skipped, so it
            # has only a synthetic node_action task). Rather than 400, dispatch it
            # FRESH from its parents' outputs — reconstruct the task the normal
            # downstream dispatch builds (node_conf shape + parent-output
            # resolution). Falls back to 400 only if the plan/definition is gone.
            plan = (db_run.parameters or {}).get("execution_plan") or {}
            pnodes = plan.get("nodes") or {}
            deps = plan.get("dependencies") or {}
            ndef = pnodes.get(node_id) or pnodes.get(str(node_id))
            if not ndef:
                raise HTTPException(status_code=400,
                    detail="No prior task to re-run and node not found in the run's execution plan")
            node_data = ndef.get("data", {}) or {}
            project_info = (db_run.parameters or {}).get("project_info", {}) or {}
            # Each parent's latest real outputs → the node's inputs.
            parent_outputs = {}
            for parent_id in (deps.get(node_id) or deps.get(str(node_id)) or []):
                pt = workflows_domains_workflows_crud.get_latest_completed_task_for_node(
                    db, run_id, parent_id,
                )
                if pt and isinstance(pt.result_payload, dict):
                    outs = pt.result_payload.get("outputs") or []
                    if outs:
                        parent_outputs[str(parent_id)] = outs
            node_kind = node_data.get("node_kind")
            from utils.minio_client import get_tenant_bucket_name
            node_conf = {
                "tenant_id": db_run.tenant_id,
                # GC.MWL.7c: no keycloak_token — never persisted in
                # `parameters` any more, and the Go worker never read this
                # key from the task payload anyway.
                "workflow_run_id": run_id,
                "node_id": node_id,
                "node_data": node_data,
                "ProjectInfo": project_info,
                "parent_outputs": parent_outputs,
                "node_kind": node_kind,
                "executable_path": node_data.get("executable_path", ""),
                "process_type": node_data.get("process_type", "CPU"),
                "output_schema": node_data.get("output_schema", {}),
                "cost_in_credits": node_data.get("cost_in_credits", 0),
                # BYO-S3 aware: resolves to the tenant's own bucket when they
                # have an active StorageConnection, else house default.
                "minio_bucket": get_tenant_bucket_name(db_run.tenant_id, db),
                "cog_ids": project_info.get("cog_ids", []),
                "manual_rerun": True,  # bypass cache + dispatch guard
            }
            # GC.MWL.12 — borrower-side loaned run: stamp the work-loan id so
            # the worker tags this task's S3 outputs with Mod-Loan-Status /
            # Mod-Loan-Id. Ordinary runs resolve to None and the payload is
            # left untouched.
            from utils.paid_import import optional_paid
            _mwl_payload = optional_paid("services.mesh_loan_payload")
            _loan_id = (
                _mwl_payload.loan_id_for_run(db, run_id)
                if _mwl_payload is not None else None
            )
            if _loan_id is not None:
                node_conf["loan_id"] = str(_loan_id)
            # GC.MWL.C7 — owner-side: this node run may be the OTHER end of a
            # borrower's checkpoint hand-back (GC.MWL.C2 "handback"
            # decision). If GC.MWL.8's reaper tick already registered an
            # import for it, hand the worker a presigned GET so it hydrates
            # instead of a cold restart. Ordinary runs resolve to None.
            from utils.paid_import import optional_paid
            _mwl_handback = optional_paid("services.mesh_loan_handback")
            _hydrate_url = (
                _mwl_handback.hydrate_url_for_node_run(db, nr.id if nr else None)
                if _mwl_handback is not None else None
            )
            if _hydrate_url:
                node_conf["checkpoint_hydrate_url"] = _hydrate_url
            # 985d — mirror the hydrate stamp above for the export side: a
            # pending checkpoint EXPORT staged for this node run (985c).
            _export_url = (
                _mwl_handback.export_url_for_node_run(db, nr.id if nr else None)
                if _mwl_handback is not None else None
            )
            if _export_url:
                node_conf["checkpoint_export_url"] = _export_url
                _export_transfer_id = _mwl_handback.export_transfer_id_for_node_run(
                    db, nr.id if nr else None)
                if _export_transfer_id is not None:
                    node_conf["checkpoint_export_transfer_id"] = _export_transfer_id
            # Pick the queue the normal dispatch would (GPU/CPU/variant-aware).
            try:
                from utils.queue_selector import select_queue_for_node_data
                # D76: tenant-scoped lookup (same helper as root dispatch), not
                # the unscoped node_kind .first().
                from services.workflow_orchestrator import _resolve_node_def_for_routing
                _nd = _resolve_node_def_for_routing(
                    db, node_data, node_kind, db_run.tenant_id)
                fresh_queue = select_queue_for_node_data(_nd, node_data) or "CPU"
            except Exception:
                fresh_queue = node_data.get("process_type") or "CPU"
            from routers.worker_tasks import enqueue_task_internal, TaskEnqueueRequest
            attempts0 = workflows_domains_workflows_crud.count_tasks_for_node(db, run_id, node_id)
            fresh_req = TaskEnqueueRequest(
                workflow_run_id=run_id, node_run_id=(nr.id if nr else None),
                node_id=node_id, tenant_id=db_run.tenant_id,
                queue_name=fresh_queue, priority=0, task_payload=node_conf,
            )
            fresh_result = enqueue_task_internal(fresh_req, db)
            fresh_task_id = fresh_result.get("task_id")
            fresh_task = workflows_domains_workflows_crud.get_task_by_id(db, fresh_task_id) if fresh_task_id else None
            if fresh_task is not None:
                fresh_task.output_namespace = f"attempt_{attempts0}"
            if nr:
                nr.status = models.TaskStatus.QUEUED
            _clear_node_pause(db_run, node_id)
            if action == "rerun_with_downstream":
                rn = list(db_run.rerun_nodes or [])
                for dn in _downstream_node_ids(db_run, node_id):
                    if dn not in rn:
                        rn.append(dn)
                    dnr = _noderun_for(db, db_run, dn)
                    if dnr:
                        dnr.status = models.TaskStatus.QUEUED
                db_run.rerun_nodes = rn
                flag_modified(db_run, "rerun_nodes")
            _node_action_audit(db, db_run, current_user, action, node_id, prior,
                               {"new_task_id": fresh_task_id, "attempt": attempts0, "fresh_dispatch": True})
            db.commit()
            return {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                    "new_task_id": fresh_task_id, "attempt": attempts0,
                    "message": f"Re-running {node_id} (fresh dispatch — no prior execution to copy)."}
        def _rerun_one_task(task_to_copy):
            """Re-dispatch ONE prior real task exactly as the original
            single-task rerun logic did (loan_id stamp, hydrate_url stamp,
            output_namespace bump, enqueue). Called once for the unfanned
            path and once per distinct source cog_id for a FANNED node
            (sz-nice-to-haves.10) — identical behavior either way."""
            task_attempt = workflows_domains_workflows_crud.count_tasks_for_node(db, run_id, node_id)
            task_payload = dict(task_to_copy.task_payload or {})
            task_payload["manual_rerun"] = True
            # GC.MWL.12 — borrower-side loaned run: stamp the work-loan id even
            # when the copied attempt predates the stamping (or lost it), so the
            # re-run's outputs are tagged Mod-Loan-Status / Mod-Loan-Id.
            from utils.paid_import import optional_paid
            _mwl_payload = optional_paid("services.mesh_loan_payload")
            _loan_id = (
                _mwl_payload.loan_id_for_run(db, run_id)
                if _mwl_payload is not None else None
            )
            if _loan_id is not None:
                task_payload["loan_id"] = str(_loan_id)
            # GC.MWL.C7 — owner-side: stamp a hand-back hydrate URL even when the
            # copied attempt predates it, same rationale as the loan_id stamp
            # above. Ordinary runs (no hand-back staged) resolve to None.
            from utils.paid_import import optional_paid
            _mwl_handback = optional_paid("services.mesh_loan_handback")
            _hydrate_url = (
                _mwl_handback.hydrate_url_for_node_run(db, nr.id if nr else None)
                if _mwl_handback is not None else None
            )
            if _hydrate_url:
                task_payload["checkpoint_hydrate_url"] = _hydrate_url
            # 985d — mirror the hydrate stamp above for the export side: a
            # pending checkpoint EXPORT staged for this node run (985c).
            _export_url = (
                _mwl_handback.export_url_for_node_run(db, nr.id if nr else None)
                if _mwl_handback is not None else None
            )
            if _export_url:
                task_payload["checkpoint_export_url"] = _export_url
                _export_transfer_id = _mwl_handback.export_transfer_id_for_node_run(
                    db, nr.id if nr else None)
                if _export_transfer_id is not None:
                    task_payload["checkpoint_export_transfer_id"] = _export_transfer_id
            from routers.worker_tasks import enqueue_task_internal, TaskEnqueueRequest
            task_req = TaskEnqueueRequest(
                workflow_run_id=run_id, node_run_id=(nr.id if nr else None),
                node_id=node_id, tenant_id=db_run.tenant_id,
                queue_name=task_to_copy.queue_name, priority=task_to_copy.priority or 0,
                task_payload=task_payload,
            )
            task_result = enqueue_task_internal(task_req, db)
            dispatched_task_id = task_result.get("task_id")
            dispatched_task = (
                workflows_domains_workflows_crud.get_task_by_id(db, dispatched_task_id)
                if dispatched_task_id else None
            )
            if dispatched_task is not None:
                dispatched_task.output_namespace = f"attempt_{task_attempt}"
            return dispatched_task_id, task_attempt

        # sz-nice-to-haves.10: detect whether node_id is FANNED in this run
        # (2+ distinct source cog_ids among its real — non "node_action" —
        # task attempts) BEFORE deciding which task(s) to re-dispatch. Uses
        # the same dedup-by-_fanout_key convention as get_node_fanout, so a
        # fanned node reruns every distinct file's own latest real task
        # instead of silently re-running only one.
        from crud import node_runs as node_runs_crud
        from routers.node_runs_domains._shared import _fanout_key
        all_real_tasks = node_runs_crud.get_tasks_for_node(
            db, workflow_run_id=run_id, graph_node_id=node_id, order="asc"
        )
        latest_real_by_key = {}
        for t in all_real_tasks:  # ascending id order -> last write wins
            if t.queue_name == "node_action":
                continue
            latest_real_by_key[_fanout_key(t.task_payload or {})] = t
        is_fanned = len(latest_real_by_key) >= 2

        if is_fanned:
            attempts_list = []
            for _key, _task in latest_real_by_key.items():
                _cog_id = _key[1] if isinstance(_key, tuple) and _key[0] == "cog" else None
                _new_task_id, _attempt = _rerun_one_task(_task)
                attempts_list.append({
                    "cog_id": _cog_id, "new_task_id": _new_task_id, "attempt": _attempt,
                })
            # Single-attempt fields stay populated (from the last dispatched
            # file) so an unfanned-shaped caller still gets a usable pair.
            new_task_id, attempts = attempts_list[-1]["new_task_id"], attempts_list[-1]["attempt"]
        else:
            attempts_list = None
            new_task_id, attempts = _rerun_one_task(latest)
        if nr:
            nr.status = models.TaskStatus.QUEUED
        _clear_node_pause(db_run, node_id)  # re-running resolves any pause point
        extra = {"new_task_id": new_task_id, "attempt": attempts}
        if is_fanned:
            extra["fanned"] = True
            extra["attempts"] = attempts_list
        if action == "rerun_with_downstream":
            downstream = _downstream_node_ids(db_run, node_id)
            rn = list(db_run.rerun_nodes or [])
            for dn in downstream:
                if dn not in rn:
                    rn.append(dn)
                dnr = _noderun_for(db, db_run, dn)
                if dnr:
                    dnr.status = models.TaskStatus.QUEUED
            db_run.rerun_nodes = rn
            flag_modified(db_run, "rerun_nodes")
            extra["rerun_nodes"] = rn
        _node_action_audit(db, db_run, current_user, action, node_id, prior, extra)
        db.commit()
        msg = (f"Re-running {node_id} + downstream." if action == "rerun_with_downstream"
               else f"Re-running {node_id} (same data).")
        resp = {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                "new_task_id": new_task_id, "attempt": attempts, "message": msg}
        if is_fanned:
            resp["fanned"] = True
            resp["attempts"] = attempts_list
        return resp

    if action == "clear_cache":
        # Invalidate the node's content-addressed cache so a future reuse
        # recomputes it. Evicts only the cache index rows — NO blob is touched
        # (the blobs remain; ISO audit trail intact). See
        # workflows_domains_workflows_service.clear_node_cache for the two-way
        # union eviction (exact key + output-hash) this delegates to.
        cache_result = workflows_domains_workflows_service.clear_node_cache(db, db_run, node_id)
        evicted = cache_result["evicted"]
        _node_action_audit(db, db_run, current_user, action, node_id, prior,
                           {"cache_entries_evicted": int(evicted), "hashes": cache_result["hashes"]})
        db.commit()
        return {"workflow_run_id": run_id, "node_id": node_id, "action": action,
                "cache_entries_evicted": int(evicted),
                "message": f"Cleared cache for {node_id}; it will recompute on next reuse."}

@router.get("/workflow-runs/{run_id}/node-controls", status_code=status.HTTP_200_OK)
def get_node_controls(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """node-run-controls — per-node OPERATOR CONTROL state for a run, separate from
    execution status. The canvas stamps node.data.control from this so paused /
    draining / skipped / re-running get their own colour (execution status still
    drives the running/success/failure dot). Derived from the run's control lists
    + task markers — no new columns beyond drain/rerun.
    """
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=current_user.tenant_id)
    if not db_run or not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    controls: dict = {}
    for n in (db_run.paused_nodes or []):
        controls[n] = "paused"
    for n in (db_run.drain_nodes or []):
        controls.setdefault(n, "draining")  # paused wins over draining

    # Latest task per node → skipped (operator_skip synthetic) or re-running
    # (an in-flight manual_rerun attempt). Paused/draining take precedence.
    tasks = workflows_domain_crud.get_all_tasks_for_run_ordered(db, run_id)
    latest_by_node = {}
    for t in tasks:
        latest_by_node[t.node_id] = t  # ascending → last write is newest
    for node_id, t in latest_by_node.items():
        if node_id in controls:
            continue
        payload = t.task_payload or {}
        rp = t.result_payload if isinstance(t.result_payload, dict) else {}
        if payload.get("node_kind") == "operator_force_fail":
            controls[node_id] = "force_failed"
        elif payload.get("node_kind") == "operator_skip" or rp.get("operator_skip"):
            controls[node_id] = "skipped"
        elif payload.get("manual_rerun") and t.status in _ACTIVE_TASK_STATUSES_CTRL:
            controls[node_id] = "re-running"

    return {"workflow_run_id": run_id, "controls": controls}

@router.post("/{workflow_id:int}/schedule-pause", status_code=status.HTTP_200_OK)
def set_workflow_schedule_paused(
        workflow_id: int,
        body: dict = Body(default={}),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """DAG-level pause: when paused, schedules + triggers start NO new runs of
    this workflow. In-progress runs are unaffected. body = {paused: bool}."""
    wf = workflows_domain_crud.get_workflow_row(db, workflow_id, current_user.tenant_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    paused = bool((body or {}).get("paused", True))
    wf.schedule_paused = paused
    try:
        db.add(models.AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="WORKFLOW_SCHEDULE_PAUSE",
            details={"workflow_id": workflow_id, "schedule_paused": paused},
        ))
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return {"workflow_id": workflow_id, "schedule_paused": paused,
            "message": ("Schedule paused — no new runs will start."
                        if paused else "Schedule resumed.")}

@router.post("/workflow-runs/{run_id}/restart", status_code=status.HTTP_200_OK)
def restart_workflow_run(
        run_id: int,
        restart_request: schemas.WorkflowRestartRequest = None,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_harbor_credentials),  # Harbor required to run workflows
        keycloak_token: str = Depends(get_keycloak_token)
):
    """
    Restart a failed, cancelled, or completed workflow run.

    This creates a NEW workflow run with the same parameters as the original.
    The original run is NOT modified (preserves history).

    Resume modes:
    - 'full': Reprocess ALL files (creates fresh run) - DEFAULT
    - 'failed_only': Only reprocess files that failed or never completed (skip successful ones)

    For running/queued workflows, use the cancel endpoint first, then restart.
    """
    from datetime import datetime
    import requests

    # Handle case where no body is provided (default to full restart)
    if restart_request is None:
        restart_request = schemas.WorkflowRestartRequest()

    resume_mode = restart_request.resume_mode
    if resume_mode not in ['full', 'failed_only']:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid resume_mode '{resume_mode}'. Must be 'full' or 'failed_only'"
        )

    # Platform admins can restart all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    # Get original workflow run
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    # Only a user who can view the run's workflow may restart it (owner/granted).
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # Cannot restart a running/queued workflow - must cancel first
    if db_run.status in [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot restart a workflow with status '{db_run.status.value}'. Cancel it first using POST /workflow-runs/{run_id}/cancel"
        )

    # Get the workflow version to re-trigger
    db_version = crud.get_workflow_version_by_id(
        db,
        version_id=db_run.workflow_version_id,
        tenant_id=current_user.tenant_id
    )
    if not db_version:
        raise HTTPException(status_code=404, detail="Original workflow version no longer exists")

    # Extract cog_ids from original run's project_info
    original_project_info = db_run.parameters.get('project_info', {}) if db_run.parameters else {}
    cog_ids = original_project_info.get('cog_ids', [])
    metadata_cog_ids = original_project_info.get('metadata_cog_ids', [])
    parent_folder_id = original_project_info.get('parent_folder_id')

    if not cog_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot restart: original workflow run has no cog_ids in configuration"
        )

    # Handle failed_only mode - filter out files that completed successfully
    skipped_cog_ids = []
    if resume_mode == 'failed_only':
        # Get all workflow run files for this run
        run_files = crud.get_workflow_run_files(db, workflow_run_id=run_id)

        # Build set of successfully completed file IDs
        completed_file_ids = set()
        for run_file in run_files:
            if run_file.status == models.TaskStatus.COMPLETED:
                try:
                    completed_file_ids.add(int(run_file.file_id))
                except (ValueError, TypeError):
                    pass  # Skip invalid file_ids

        # Filter out completed files from cog_ids
        original_count = len(cog_ids)
        cog_ids_to_process = [cog_id for cog_id in cog_ids if cog_id not in completed_file_ids]
        skipped_cog_ids = [cog_id for cog_id in cog_ids if cog_id in completed_file_ids]

        logging.info(f"🔄 Resume mode 'failed_only': {len(skipped_cog_ids)} of {original_count} files already completed, {len(cog_ids_to_process)} files to reprocess")

        if not cog_ids_to_process:
            return {
                "status": "all_completed",
                "original_run_id": run_id,
                "message": f"All {original_count} files already completed successfully. Nothing to restart.",
                "completed_files": len(completed_file_ids),
                "skipped_cog_ids": skipped_cog_ids
            }

        cog_ids = cog_ids_to_process

    # Build the run request matching the original
    run_request = schemas.WorkflowRunRequest(
        cog_ids=cog_ids,
        metadata_cog_ids=metadata_cog_ids if metadata_cog_ids else None,
        parent_folder_id=parent_folder_id,
        refresh_token=None,  # User must provide fresh token if needed
        # Concurrent by design (restart = a new run) + Airflow-style
        # restart-from-node cache reuse / forced re-run passthrough.
        force_new=True,
        reuse_cache=bool(getattr(restart_request, "reuse_cache", False)),
        cache_bust_nodes=getattr(restart_request, "cache_bust_nodes", None),
    )

    # Store restart metadata on original run
    if db_run.parameters is None:
        db_run.parameters = {}
    if 'restart_history' not in db_run.parameters:
        db_run.parameters['restart_history'] = []
    db_run.parameters['restart_history'].append({
        'restarted_at': datetime.now().isoformat(),
        'restarted_by': current_user.id
    })

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(db_run, 'parameters')
    db.commit()

    # Trigger the new run using the existing endpoint logic
    # We call it directly to avoid HTTP overhead
    logging.info(f"🔄 Restarting workflow run {run_id} as new run")

    new_run_result = _wf.trigger_workflow_run(
        version_id=db_run.workflow_version_id,
        run_request=run_request,
        db=db,
        current_user=current_user,
        keycloak_token=keycloak_token
    )

    # Check if it was a duplicate (shouldn't happen but handle it)
    if isinstance(new_run_result, dict) and new_run_result.get('duplicate'):
        return {
            "status": "already_running",
            "original_run_id": run_id,
            "new_run_id": new_run_result.get('workflow_run_id'),
            "message": "A run of this workflow is already in progress"
        }

    # Build detailed response
    response = {
        "status": "restarted",
        "original_run_id": run_id,
        "new_run_id": new_run_result.get('workflow_run_id') if isinstance(new_run_result, dict) else None,
        "resume_mode": resume_mode,
        "new_run": new_run_result
    }

    # Add details for failed_only mode
    if resume_mode == 'failed_only':
        response["message"] = f"Workflow run {run_id} resumed (skipped {len(skipped_cog_ids)} completed files, reprocessing {len(cog_ids)} files)"
        response["skipped_files"] = len(skipped_cog_ids)
        response["reprocessing_files"] = len(cog_ids)
        response["skipped_cog_ids"] = skipped_cog_ids
    else:
        response["message"] = f"Workflow run {run_id} restarted as new run (full reprocess of {len(cog_ids)} files)"
        response["reprocessing_files"] = len(cog_ids)

    return response

@router.post("/batch-update-processing-history", status_code=status.HTTP_200_OK)
def batch_update_processing_history(
        update_request: schemas.BatchProcessingHistoryUpdate,
        db: Session = Depends(get_db),
        principal=Depends(node_or_onboarded_user_principal)
):
    """
    Batch update processing metadata for multiple cog_ids.
    Called by worker containers during workflow execution to track node-level processing history.

    Accepts either the existing active+onboarded user auth or a run_task node token
    (X-API-Key). For the run_task path the tenant is taken from the token (never the body)
    and the workflow_run_id is pinned to the token.
    """
    from datetime import datetime

    # Resolve the tenant from the credential — the token for a node caller, the
    # authenticated user otherwise. Never trust a tenant from the request body.
    if isinstance(principal, dict):
        pin_run_task_tenant(
            principal,
            tenant_id=update_request.tenant_id if update_request.tenant_id is not None else int(principal["tenant_id"]),
            run_id=update_request.workflow_run_id,
        )
        tenant_id = int(principal["tenant_id"])
    else:
        tenant_id = principal.tenant_id

    # Verify all cogs exist and belong to the resolved tenant (exclude soft-deleted)
    cogs = workflows_domain_crud.get_cogs_by_ids(db, update_request.cog_ids, tenant_id)

    if len(cogs) != len(update_request.cog_ids):
        raise HTTPException(status_code=404, detail="One or more cogs not found")

    # Update each cog's processing metadata
    for cog in cogs:
        # Get existing metadata
        metadata = cog.processing_metadata or {}

        # Initialize workflow_runs array if not exists
        if 'workflow_runs' not in metadata:
            metadata['workflow_runs'] = []

        # Find or create the workflow run entry
        workflow_run_id = str(update_request.workflow_run_id)
        run_entry = None
        for run in metadata['workflow_runs']:
            if str(run.get('workflow_run_id')) == workflow_run_id:
                run_entry = run
                break

        if not run_entry:
            run_entry = {
                'workflow_run_id': update_request.workflow_run_id,
                'nodes_executed': []
            }
            metadata['workflow_runs'].append(run_entry)

        # Add the node execution entry
        node_entry = update_request.metadata_entry.dict()
        run_entry['nodes_executed'].append(node_entry)

        # Update summary fields
        cog.processing_metadata = metadata
        cog.total_processing_cost += update_request.metadata_entry.cost
        cog.last_processed_at = datetime.utcnow()

        # Increment processing count only when workflow completes (check if this is final node)
        if update_request.metadata_entry.status in ['success', 'failure']:
            cog.processing_count += 1

    db.commit()

    return {"detail": f"Successfully updated processing history for {len(cogs)} assets"}

@router.get("/cogs/{cog_id}/processing-history", response_model=schemas.ProcessingHistoryResponse)
def get_processing_history(
        cog_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get complete processing history for a specific asset.
    Returns all workflow runs, node executions, quality metrics, and derived assets.
    """
    cog = workflows_domain_crud.get_cog(db, cog_id, current_user.tenant_id)

    if not cog:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Get derived assets (files created from this one, exclude soft-deleted)
    derived_assets = workflows_domain_crud.get_cog_children_files(db, cog_id)

    # GG.8b sealed: a cog can carry metadata from runs of DIFFERENT workflows
    # (re-processed over time), so resolve the seal decision PER run entry —
    # entries belonging to a sealed, non-exempt buyer's workflow get their
    # node_kind + parameters (settings) nulled; timestamps/status kept.
    metadata = cog.processing_metadata or {}
    _redact_wf_ids: set = set()
    for _entry in metadata.get('workflow_runs', []) if metadata else []:
        try:
            _rid = int(_entry.get('workflow_run_id'))
        except (TypeError, ValueError):
            continue
        _r = crud.get_workflow_run_by_id(db, _rid, tenant_id=current_user.tenant_id)
        if _r is None or _r.workflow is None:
            continue
        if _sealed.should_redact_graph(db, _r.workflow, current_user):
            _redact_wf_ids.add(_rid)

    # Parse processing history from JSONB (redacted copies are shallow clones —
    # never mutate the ORM object's JSONB in memory).
    workflow_runs = []
    for run in metadata.get('workflow_runs', []):
        if run.get('workflow_run_id') in _redact_wf_ids:
            nodes = [
                dict(n, node_name=None, node_kind=None, parameters=None)
                if isinstance(n, dict) else n
                for n in (run.get('nodes_executed') or [])
            ]
            run = {**run, 'nodes_executed': nodes}
        workflow_runs.append(schemas.WorkflowRunMetadata(**run))

    # Calculate total processing time
    total_time = 0.0
    for run in workflow_runs:
        if run.duration_seconds:
            total_time += run.duration_seconds

    # Aggregate quality metrics
    quality_metrics = {}
    for run in workflow_runs:
        for node in run.nodes_executed:
            if node.quality_metrics:
                for key, value in node.quality_metrics.items():
                    if key not in quality_metrics:
                        quality_metrics[key] = []
                    quality_metrics[key].append(value)

    return schemas.ProcessingHistoryResponse(
        asset_id=cog.id,
        original_filename=cog.name,
        created_at=cog.created_at,
        processing_history=workflow_runs,
        derived_assets=[{"id": d.id, "name": d.name, "created_at": d.created_at} for d in derived_assets],
        total_processing_cost=cog.total_processing_cost or 0,
        total_processing_time_seconds=total_time,
        processing_count=cog.processing_count or 0,
        last_processed_at=cog.last_processed_at,
        quality_metrics=quality_metrics
    )

@router.post("/cogs/search-by-processing")
def search_by_processing(
        workflow_name: Optional[str] = None,
        min_cost: Optional[int] = None,
        max_cost: Optional[int] = None,
        processed_after: Optional[str] = None,
        processed_before: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Search for assets based on their processing history.
    Supports filtering by workflow name, cost range, and date range.
    """
    from datetime import datetime

    after_date = datetime.fromisoformat(processed_after) if processed_after else None
    before_date = datetime.fromisoformat(processed_before) if processed_before else None

    cogs, total = workflows_domain_crud.search_cogs_by_processing(
        db, current_user.tenant_id,
        min_cost=min_cost, max_cost=max_cost,
        processed_after=after_date, processed_before=before_date,
        workflow_name=workflow_name, limit=limit, offset=offset,
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "id": cog.id,
                "name": cog.name,
                "total_processing_cost": cog.total_processing_cost or 0,
                "processing_count": cog.processing_count or 0,
                "last_processed_at": cog.last_processed_at,
                "created_at": cog.created_at
            }
            for cog in cogs
        ]
    }

@router.get("/workflow-runs/{run_id}/node-status")
def get_node_execution_status(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get real-time execution status for all nodes in a workflow run.
    Returns status of each node (pending, running, success, failure) with timestamps.
    Platform admins can see any run, others only see their tenant's runs.
    """
    # Platform admins can see all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    # Get the workflow run
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # GG.8b sealed: redact node identity from per-node status (keep status/timing).
    _redact_nodes = (
        _sealed.should_redact_graph(db, db_run.workflow, current_user)
        if db_run.workflow else False
    )

    # Cogs record their per-run node status inside processing_metadata
    # (shape: {"workflow_runs": [{"workflow_run_id": N, "nodes_executed": [...]}]}).
    # WorkflowRunFile.file_id is an opaque storage UUID, not Cog.id, so it
    # can't be joined here — scan the tenant's cogs that carry processing
    # metadata and let the inner filter pick out this run.
    cogs = workflows_domain_crud.get_cogs_with_processing_metadata(db, current_user.tenant_id)

    # Aggregate node status from all cogs
    node_status_map = {}
    for cog in cogs:
        metadata = cog.processing_metadata or {}
        for workflow_run in metadata.get('workflow_runs', []):
            if workflow_run.get('workflow_run_id') == run_id:
                for node in workflow_run.get('nodes_executed', []):
                    node_id = node.get('node_id')
                    if node_id not in node_status_map:
                        node_status_map[node_id] = {
                            'node_id': node_id,
                            'node_name': None if _redact_nodes else node.get('node_name'),
                            'node_kind': None if _redact_nodes else node.get('node_kind'),
                            'status': node.get('status'),
                            'started_at': node.get('started_at'),
                            'completed_at': node.get('completed_at'),
                            'total_cost': 0,
                            'files_processed': 0,
                            'errors': []
                        }

                    # Aggregate data
                    node_status_map[node_id]['total_cost'] += node.get('cost', 0)
                    node_status_map[node_id]['files_processed'] += 1

                    if node.get('error_details'):
                        node_status_map[node_id]['errors'].append(node.get('error_details'))

    # AI scheduler — fold per-node ETAs from NodeRun into the response so the
    # WorkflowProgress.tsx canvas badge + Dashboard logs detail can render them.
    # NodeRun.estimated_duration_seconds is stamped at run creation; on
    # completion the same row holds completed_at - started_at as the actual,
    # which the UI can compare to the estimate for the predicted-vs-actual delta.
    from datetime import datetime, timezone
    node_runs = workflows_domain_crud.get_node_run_join_rows(db, run_id)

    # A fan-out node (per-file processing) has MANY NodeRun rows for ONE graph
    # node, often with MIXED statuses (some COMPLETED, one FAILED, one RUNNING).
    # Overwriting per graph_id with whichever row an UNORDERED query returned
    # last made the reported status flip between polls, so the canvas node
    # visually "restarted" over and over. AGGREGATE every run of a node into ONE
    # stable, monotonic status (pending → running → success/failure) so the UI
    # (which renders node.status) shows a single steady state, plus per-file
    # counts for an X/N progress display.
    # NodeRun.status is TaskStatus; its .value strings are LOWERCASE
    # (queued/running/pending_action/success/failure/completed/failed/blocked).
    _ACTIVE = {"running", "pending_action"}
    _WAITING = {"queued", "blocked", "pending", "claimed", "assigned"}
    _DONE = {"completed", "success"}
    _FAIL = {"failed", "failure"}

    def _rollup(statuses: set) -> str:
        s = {str(x).lower() for x in statuses if x}
        # still in progress: anything active, OR a partially-finished fan-out
        # (some files terminal while others are still queued) — never flip such a
        # node back to pending/success/failure mid-run.
        if (s & _ACTIVE) or (s & _WAITING and (s & _DONE or s & _FAIL)):
            return "running"
        if s & _WAITING:
            return "pending"
        # all terminal: success if ANY file completed (partial failures surfaced
        # via files_failed, not by flipping the whole node red); failure only
        # when every run failed. This is also retry-safe (a failed-then-retried
        # file leaves both a FAILED and a COMPLETED run).
        if s & _DONE:
            return "success"
        if s & _FAIL:
            return "failure"
        return "pending"

    from collections import defaultdict
    _agg = defaultdict(lambda: {"statuses": set(), "est": 0, "act": 0.0,
                                "n": 0, "done": 0, "failed": 0})
    for nr, graph_id in node_runs:
        a = _agg[graph_id]
        st = nr.status.value if nr.status else None
        a["statuses"].add(st)
        a["est"] += nr.estimated_duration_seconds or 0
        if nr.started_at and nr.completed_at:
            a["act"] += (nr.completed_at - nr.started_at).total_seconds()
        a["n"] += 1
        if st in _DONE:
            a["done"] += 1
        elif st in _FAIL:
            a["failed"] += 1

    node_eta_by_id = {}
    for graph_id, a in _agg.items():
        rolled = _rollup(a["statuses"])
        node_eta_by_id[graph_id] = {
            "estimated_seconds": a["est"] or None,
            "actual_seconds": a["act"] or None,
            "node_run_status": rolled,
            # Authoritative STABLE per-node status for the canvas — overrides the
            # per-file cog-metadata status on the merge below (that one is derived
            # from an unordered scan and is what flickered).
            "status": rolled,
            # node-run rollup counts (do NOT clobber the cog-metadata
            # files_processed the merge already carries — use distinct keys).
            "node_runs_done": a["done"],
            "node_runs_failed": a["failed"],
            "node_runs_total": a["n"],
        }

    # Merge per-node ETA into the existing node entries; create stub entries
    # for nodes that haven't started yet (so the UI shows their ETA before
    # any cog has touched them).
    for graph_id, eta_data in node_eta_by_id.items():
        if graph_id in node_status_map:
            node_status_map[graph_id].update(eta_data)
        else:
            node_status_map[graph_id] = {
                "node_id": graph_id,
                "status": eta_data["node_run_status"] or "queued",
                "started_at": None,
                "completed_at": None,
                "total_cost": 0,
                "files_processed": 0,
                "errors": [],
                **eta_data,
            }

    return {
        "workflow_run_id": run_id,
        "overall_status": db_run.status,
        "nodes": list(node_status_map.values()),
        "total_files": len(cogs)
    }

@router.get("/{workflow_id:int}/runs-grid")
def get_workflow_runs_grid(
        workflow_id: int,
        limit: int = 30,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Compact Airflow-style grid: recent runs of a workflow, each with one
    STABLE rolled-up status per graph node. Powers the WorkflowBuilder grid view
    (rows = nodes, columns = runs). One query for all runs' node_runs — no N+1,
    no heavy per-run payloads (no inputs/outputs). Tenant-scoped.
    """
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped
    wf = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=tenant_filter)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # GG.8b sealed: redact node names/ids from the grid for sealed buyers.
    _grid_redact = _sealed.should_redact_graph(db, wf, current_user)

    # crud.get_workflow_runs returns DICTS (id, run_name, status[str], started_at,
    # version_number, ...), not ORM rows.
    runs = crud.get_workflow_runs(
        db, workflow_id=workflow_id, limit=max(1, min(limit, 100)),
        offset=0, sort_by="started_at", order="desc",
        purposes_exclude=USER_HIDDEN_RUN_PURPOSES,
    )
    run_ids = [r["id"] for r in runs]

    # Same stable rollup as /node-status (TaskStatus values are lowercase).
    _ACTIVE = {"running", "pending_action"}
    _WAITING = {"queued", "blocked", "pending", "claimed", "assigned"}
    _DONE = {"completed", "success"}
    _FAIL = {"failed", "failure"}

    def _rollup(statuses):
        s = {str(x).lower() for x in statuses if x}
        if (s & _ACTIVE) or (s & _WAITING and (s & _DONE or s & _FAIL)):
            return "running"
        if s & _WAITING:
            return "pending"
        if s & _DONE:
            return "success"
        if s & _FAIL:
            return "failure"
        return "pending"

    # cells[run_id][graph_node_id] = set(statuses); node_names[graph_node_id]=name;
    # node_order preserves first-seen order (newest run first → sensible top-down).
    cells: dict = {rid: {} for rid in run_ids}
    node_names: dict = {}
    node_order: list = []
    if run_ids:
        rows = workflows_domain_crud.get_run_grid_rows(db, run_ids)
        for run_id, gid, name, st in rows:
            if gid is None:
                continue
            if gid not in node_names:
                node_names[gid] = None if _grid_redact else (name or gid)
                node_order.append(gid)
            stv = st.value if hasattr(st, "value") else st
            cells.setdefault(run_id, {}).setdefault(gid, set()).add(stv)

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    runs_out = []
    for r in runs:
        rid = r["id"]
        rolled = {gid: _rollup(sts) for gid, sts in cells.get(rid, {}).items()}
        runs_out.append({
            "id": rid,
            "run_name": r.get("run_name"),
            "status": r.get("status"),
            "paused": bool(r.get("paused")),
            "started_at": _iso(r.get("started_at")),
            "completed_at": _iso(r.get("completed_at")),
            "version_number": r.get("version_number"),
            "cells": rolled,
        })
        if _grid_redact:
            # opaque keys — hide the graph node identity, keep one per node
            runs_out[-1]["cells"] = {f"node-{i}": st for i, st in enumerate(rolled.values())}

    return {
        "workflow_id": workflow_id,
        "nodes": [
            ({"graph_node_id": f"node-{i}", "name": None} if _grid_redact
             else {"graph_node_id": gid, "name": node_names.get(gid, gid)})
            for i, gid in enumerate(node_order)
        ],
        "runs": runs_out,
    }

@router.get("/workflow-runs/{run_id}/output-folders")
def get_workflow_output_folders(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get output folders created by a workflow run.
    Returns folders that contain the results of the workflow execution.
    Platform admins can see any run, others only see their tenant's runs.
    """
    # Platform admins can see all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    # Verify workflow run exists and belongs to user's tenant
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # Query cogs where processing_metadata contains this workflow_run_id
    output_folders = workflows_domain_crud.get_output_folder_cogs(db, current_user.tenant_id, run_id)

    return output_folders

@router.get("/workflow-runs/{run_id}/progress")
def get_workflow_progress(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get real-time progress for a workflow run.
    Returns percentage complete, current node, and estimated completion time.
    Platform admins can see any run, others only see their tenant's runs.
    """
    from datetime import datetime

    # Platform admins can see all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    # Get the workflow run
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # Get workflow version to know total nodes - use the run's tenant for version lookup
    run_tenant_id = db_run.tenant_id
    db_version = (
        crud.get_workflow_version_by_id(db, version_id=db_run.workflow_version_id, tenant_id=run_tenant_id)
        if db_run.workflow_version_id else None
    )
    graph = (db_version.graph_json or {}) if db_version else {}
    total_nodes = len(graph.get('nodes', []))

    # Get progress from the actual task queue — this is the authoritative
    # source of what's running, completed, and failed for this workflow run.
    from models import ApiTaskQueue, ApiTaskStatus, NodeRun
    tasks = workflows_domain_crud.get_all_tasks_for_run(db, run_id)

    total_expected_executions = max(total_nodes, len(tasks))
    completed_executions = sum(1 for t in tasks if t.status in (ApiTaskStatus.COMPLETED, ApiTaskStatus.FAILED))
    current_node = None
    total_cost = 0

    for t in tasks:
        if t.status == ApiTaskStatus.RUNNING:
            current_node = t.node_id
        cost = (t.task_payload or {}).get("cost_in_credits", 0)
        if t.status == ApiTaskStatus.COMPLETED:
            total_cost += cost

    progress_percentage = (completed_executions / total_expected_executions * 100) if total_expected_executions > 0 else 0

    # AI scheduler — prefer the predictor-driven critical-path ETA stamped
    # at run creation when available. Falls back to the historical
    # elapsed-based estimate when no estimate was stamped (legacy runs)
    # or when we have actual progress data that contradicts the estimate
    # (e.g. estimate was 60s but we're already at 80s elapsed at 50%).
    estimated_completion = None
    eta_source = None
    predicted_total = db_run.estimated_duration_seconds
    if db_run.started_at:
        from datetime import timezone as _tz
        _started = db_run.started_at
        if _started.tzinfo is None:
            _started = _started.replace(tzinfo=_tz.utc)
        elapsed = (datetime.now(_tz.utc) - _started).total_seconds()
    else:
        elapsed = 0.0

    if predicted_total and progress_percentage < 100:
        if db_run.started_at:
            estimated_completion = max(predicted_total - elapsed, 0)
        else:
            estimated_completion = float(predicted_total)
        eta_source = "predicted"

        # Soft override: if we're materially behind the prediction (>1.5×),
        # the prediction is wrong; fall back to elapsed-based estimate so
        # the UI doesn't show a stale "in 5s" countdown that won't fire.
        if elapsed > 0 and progress_percentage > 0 and progress_percentage < 100:
            elapsed_total = (elapsed / progress_percentage) * 100
            if elapsed_total > predicted_total * 1.5:
                estimated_completion = elapsed_total - elapsed
                eta_source = "elapsed_extrapolation"
    elif db_run.started_at and progress_percentage > 0 and progress_percentage < 100:
        elapsed_total = (elapsed / progress_percentage) * 100
        estimated_completion = elapsed_total - elapsed
        eta_source = "elapsed_extrapolation"

    return {
        "workflow_run_id": run_id,
        "status": db_run.status,
        "progress_percentage": round(progress_percentage, 2),
        "completed_nodes": completed_executions,
        "total_nodes": total_expected_executions,
        "current_node": current_node,
        "total_cost": total_cost,
        "started_at": db_run.started_at,
        "estimated_seconds_remaining": estimated_completion,
        # AI scheduler surfaces — explicit so the UI can label "(estimated by ML)"
        # vs "(estimated from progress so far)".
        "estimated_total_seconds": predicted_total,
        "eta_source": eta_source,
        # Surface orchestrator-level failure text so the dashboard can
        # display the real cause (pending_scan node, missing secret,
        # circular dependency, etc.) instead of just "failed".
        "error_message": getattr(db_run, "error_message", None),
    }

@router.get("/workflow-runs/{run_id}/detailed-log")
def get_workflow_detailed_log(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Nested workflow log view with per-node timing, worker details, and phase breakdowns.
    Returns everything the dashboard needs to render the accordion view in one call.
    """
    from datetime import datetime, timezone

    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    # Get workflow run
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # GG.8b sealed: redact node names/kinds in the detailed log for sealed buyers.
    _log_redact = (
        _sealed.should_redact_graph(db, db_run.workflow, current_user)
        if db_run.workflow else False
    )

    # Get workflow version for graph structure (node names, order)
    run_tenant_id = db_run.tenant_id
    db_version = crud.get_workflow_version_by_id(db, version_id=db_run.workflow_version_id, tenant_id=run_tenant_id)
    graph = db_version.graph_json if db_version else {}
    graph_nodes = graph.get('nodes', [])

    # Build node name lookup from graph
    node_names = {}
    node_dag_ids = {}
    for gn in graph_nodes:
        nid = gn.get('id', '')
        node_names[nid] = gn.get('data', {}).get('label', nid)
        node_dag_ids[nid] = gn.get('data', {}).get('node_kind', '')
    if _log_redact:
        # sealed: do not expose labels or node kinds — keep opaque ids only
        node_names = dict.fromkeys(node_names)
        node_dag_ids = dict.fromkeys(node_dag_ids)

    # Determine execution order: use stored execution_plan if available,
    # otherwise fall back to topological sort from graph edges
    node_order = []
    exec_plan = (db_run.parameters or {}).get("execution_plan", {})
    if exec_plan.get("execution_order"):
        # Flatten the level-based order into a single list
        for level in exec_plan["execution_order"]:
            node_order.extend(level)
    else:
        # Fallback: insertion order from graph
        node_order = [gn.get('id', '') for gn in graph_nodes]

    # Get workflow name
    workflow_name = ""
    if db_run.workflow:
        workflow_name = db_run.workflow.name or ""

    # Get all node runs for this workflow run
    node_runs = workflows_domain_crud.get_node_runs_for_run(db, run_id)

    # Build node_run lookup by graph_node_id
    node_run_map = {}
    for nr in node_runs:
        if nr.workflow_node:
            graph_node_id = nr.workflow_node.graph_node_id
            node_run_map[graph_node_id] = nr

    # Get linked worker tasks for this workflow
    task_map = {}  # node_run_id -> ApiTaskQueue
    node_run_ids = [nr.id for nr in node_runs if nr.id]
    if node_run_ids:
        tasks = workflows_domain_crud.get_tasks_by_node_run_ids(db, node_run_ids, run_id)
        for t in tasks:
            task_map[t.node_run_id] = t

    # Calculate total duration
    total_duration_s = None
    if db_run.started_at and db_run.completed_at:
        total_duration_s = (db_run.completed_at - db_run.started_at).total_seconds()

    # Build per-node detail
    nodes_result = []
    prev_completed_at = db_run.started_at

    for nid in node_order:
        nr = node_run_map.get(nid)
        node_info = {
            "node_id": nid,
            "name": node_names.get(nid, nid),
            "node_kind": node_dag_ids.get(nid, ""),
            "status": nr.status.value if nr and nr.status else "unknown",
            "type": "passthrough",
            "duration_s": None,
            "started_at": nr.started_at.isoformat() if nr and nr.started_at else None,
            "completed_at": nr.completed_at.isoformat() if nr and nr.completed_at else None,
        }

        # Calculate duration
        if nr and nr.started_at and nr.completed_at:
            node_info["duration_s"] = round((nr.completed_at - nr.started_at).total_seconds(), 2)

        # Check if this node has a worker task
        worker_task = task_map.get(nr.id) if nr else None
        if worker_task:
            node_info["type"] = "worker_task"
            payload = worker_task.task_payload or {}
            result = worker_task.result_payload or {}

            # Phase timing from task timestamps
            phases = {}
            if worker_task.claimed_at and worker_task.started_at:
                phases["queue_s"] = round((worker_task.started_at - worker_task.claimed_at).total_seconds(), 2)
            if worker_task.started_at and worker_task.completed_at:
                phases["process_s"] = round((worker_task.completed_at - worker_task.started_at).total_seconds(), 2)

            outputs_list = result.get("outputs", []) or []
            # Best-effort hash extraction so the dashboard's "I/O"
            # section can render clickable chips. `outputs` is
            # historically a free-form list — we accept either dicts
            # ({content_hash} | {sha256} | {hash} | {id}) or strings.
            def _extract_hashes(items):
                hashes = []
                for it in items or []:
                    if isinstance(it, dict):
                        h = (it.get("content_hash") or it.get("sha256")
                             or it.get("hash") or it.get("id"))
                        if h:
                            hashes.append(str(h))
                    elif isinstance(it, str) and len(it) >= 16:
                        hashes.append(it)
                return hashes

            full_input_hash = payload.get("source_file_location") or ""

            node_info["worker_task"] = {
                "task_id": worker_task.id,
                "worker_name": worker_task.claimed_by_worker or "",
                "processing_image": payload.get("processing_image", ""),
                # Truncated form, kept for backwards compat with the
                # existing dashboard render. Prefer `input_hashes` /
                # `output_hashes` (full values) for new UIs.
                "input_hash": (full_input_hash[:16] + "...") if full_input_hash else "",
                "input_hashes": [full_input_hash] if full_input_hash else [],
                "output_hashes": _extract_hashes(outputs_list),
                "phases": phases,
                **({"processing_image": None} if _log_redact else {}),
                "phase_durations": phases,  # alias the spec uses
                "outputs": outputs_list,
                "log_tail": result.get("log_tail", ""),
                "stderr_tail": result.get("log_tail", ""),
                "error_message": worker_task.error_message or result.get("error_message", "") or "",
                "egress": result.get("egress"),
            }
        elif nid.startswith("input-") or nid.startswith("output-"):
            node_info["type"] = "passthrough"

        nodes_result.append(node_info)

    # Calculate wait times between consecutive nodes
    wait_times = []
    for i in range(1, len(nodes_result)):
        prev = nodes_result[i - 1]
        curr = nodes_result[i]
        if prev.get("completed_at") and curr.get("started_at"):
            try:
                prev_end = datetime.fromisoformat(prev["completed_at"])
                curr_start = datetime.fromisoformat(curr["started_at"])
                wait_s = round((curr_start - prev_end).total_seconds(), 2)
                if wait_s >= 0:
                    wait_times.append({
                        "between": [prev["node_id"], curr["node_id"]],
                        "wait_s": wait_s
                    })
            except (ValueError, TypeError):
                pass

    # Run-level pass/fail/skipped tally — surfaces in the dashboard
    # header summary so operators see at a glance how the run went.
    pass_count = sum(
        1 for n in nodes_result
        if n.get("status") == "completed" and not (
            (n.get("worker_task") or {}).get("error_message")
        )
    )
    fail_count = sum(
        1 for n in nodes_result
        if n.get("status") in ("failed", "expired", "cancelled")
        or (n.get("status") == "completed" and (n.get("worker_task") or {}).get("error_message"))
    )
    skipped_count = sum(
        1 for n in nodes_result if n.get("status") == "skipped"
    )

    # #12 (launch-day-followup-2) — orchestrator/traffic-cop event log
    # surfaced inline with container output. Each row is what the
    # orchestrator was doing in the gap between two container completions
    # (dispatched, no-eligible-worker, downstream-decision, …). Best-
    # effort: any DB error returns an empty list so the rest of the
    # response keeps working.
    run_events: List[Dict[str, Any]] = []
    try:
        ev_rows = workflows_domain_crud.get_run_events(db, run_id)
        for row in ev_rows:
            run_events.append({
                "ts": row[0].isoformat() if row[0] else None,
                "source": row[1],
                "level": row[2],
                "message": row[3],
                "metadata": row[4],
            })
    except Exception:
        pass

    return {
        "workflow_run_id": run_id,
        "workflow_name": workflow_name,
        "version": db_run.workflow_version_id,
        "status": db_run.status.value if db_run.status else "unknown",
        "started_at": db_run.started_at.isoformat() if db_run.started_at else None,
        "completed_at": db_run.completed_at.isoformat() if db_run.completed_at else None,
        "total_duration_s": round(total_duration_s, 2) if total_duration_s else None,
        "nodes": nodes_result,
        "wait_times": wait_times,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skipped_count": skipped_count,
        "total_nodes": len(nodes_result),
        "run_events": run_events,
    }

@router.get("/workflow-runs/{run_id}/status", response_model=Dict[str, Any])
def get_workflow_run_status(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """DB-backed workflow run status (read from WorkflowRun + NodeRun)."""
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    return {
        "workflow_run_id": run_id,
        "status": db_run.status.value if db_run.status else "unknown",
        "run_id": db_run.orchestrator_run_id,
        "message": f"Workflow run {run_id} is {db_run.status.value}" if db_run.status else "Unknown",
    }

@router.get("/analytics/quality-metrics")
def get_quality_metrics_aggregation(
        workflow_name: Optional[str] = None,
        node_kind: Optional[str] = None,
        metric_name: Optional[str] = None,
        limit: int = 100,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Aggregate quality metrics across all assets.
    Returns statistics like average blur score, success rates, etc.
    """
    # Get all cogs with processing metadata (exclude soft-deleted)
    cogs = workflows_domain_crud.get_file_cogs_with_processing_metadata(db, current_user.tenant_id, limit)

    # Aggregate metrics
    metrics_by_node_kind = {}
    total_workflows = 0
    total_cost = 0

    for cog in cogs:
        metadata = cog.processing_metadata or {}
        for workflow_run in metadata.get('workflow_runs', []):
            total_workflows += 1

            if workflow_name and workflow_run.get('workflow_name') != workflow_name:
                continue

            for node in workflow_run.get('nodes_executed', []):
                node_kind = node.get('node_kind')

                if node_kind and node_kind != node_kind:
                    continue

                if node_kind not in metrics_by_node_kind:
                    metrics_by_node_kind[node_kind] = {
                        'node_kind': node_kind,
                        'node_name': node.get('node_name'),
                        'total_executions': 0,
                        'successful_executions': 0,
                        'failed_executions': 0,
                        'total_cost': 0,
                        'average_duration': 0,
                        'quality_metrics': {}
                    }

                node_metrics = metrics_by_node_kind[node_kind]
                node_metrics['total_executions'] += 1
                node_metrics['total_cost'] += node.get('cost', 0)
                total_cost += node.get('cost', 0)

                if node.get('status') == 'success':
                    node_metrics['successful_executions'] += 1
                elif node.get('status') == 'failure':
                    node_metrics['failed_executions'] += 1

                if node.get('duration_seconds'):
                    current_avg = node_metrics['average_duration']
                    count = node_metrics['total_executions']
                    node_metrics['average_duration'] = (current_avg * (count - 1) + node.get('duration_seconds')) / count

                # Aggregate quality metrics
                for metric_key, metric_value in node.get('quality_metrics', {}).items():
                    if metric_name and metric_key != metric_name:
                        continue

                    if metric_key not in node_metrics['quality_metrics']:
                        node_metrics['quality_metrics'][metric_key] = {
                            'values': [],
                            'average': 0,
                            'min': None,
                            'max': None
                        }

                    if isinstance(metric_value, (int, float)):
                        node_metrics['quality_metrics'][metric_key]['values'].append(metric_value)

    # Calculate final statistics
    for node_kind, metrics in metrics_by_node_kind.items():
        for metric_key, metric_data in metrics['quality_metrics'].items():
            values = metric_data['values']
            if values:
                metric_data['average'] = sum(values) / len(values)
                metric_data['min'] = min(values)
                metric_data['max'] = max(values)
                del metric_data['values']  # Remove raw values from response

    return {
        "total_workflows_analyzed": total_workflows,
        "total_cost": total_cost,
        "metrics_by_node_kind": list(metrics_by_node_kind.values())
    }

@router.get("/analytics/resource-usage")
def get_resource_usage_analytics(
        time_period_days: int = 30,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get resource usage analytics (GPU, CPU, memory) aggregated by node category.
    """
    # open-core-tier-structure: usage metering/logging is a gated feature
    # (Cap.USAGE_METERING). Honors off/warn/enforce + SaaS (plan) / self-host
    # (license) via the shared resolver.
    from utils.entitlements import get_entitlements, evaluate_feature, Cap
    _tenant = workflows_domain_crud.get_tenant(db, current_user.tenant_id)
    if not evaluate_feature(Cap.USAGE_METERING, get_entitlements(db, _tenant)).allowed:
        raise HTTPException(status_code=402, detail={"code": "usage_metering_required"})

    from datetime import datetime, timedelta

    cutoff_date = datetime.utcnow() - timedelta(days=time_period_days)

    # Get recent cogs (exclude soft-deleted)
    cogs = workflows_domain_crud.get_recently_processed_cogs(db, current_user.tenant_id, cutoff_date)

    resource_by_node_kind = {}

    for cog in cogs:
        metadata = cog.processing_metadata or {}
        for workflow_run in metadata.get('workflow_runs', []):
            for node in workflow_run.get('nodes_executed', []):
                node_kind = node.get('node_kind')

                if node_kind not in resource_by_node_kind:
                    resource_by_node_kind[node_kind] = {
                        'node_kind': node_kind,
                        'node_name': node.get('node_name'),
                        'total_executions': 0,
                        'total_duration_seconds': 0,
                        'gpu_usage': [],
                        'cpu_usage': [],
                        'memory_usage': []
                    }

                node_resource = resource_by_node_kind[node_kind]
                node_resource['total_executions'] += 1

                if node.get('duration_seconds'):
                    node_resource['total_duration_seconds'] += node.get('duration_seconds')

                resource_usage = node.get('resource_usage', {})
                if 'gpu_memory_mb' in resource_usage:
                    node_resource['gpu_usage'].append(resource_usage['gpu_memory_mb'])
                if 'cpu_percent' in resource_usage:
                    node_resource['cpu_usage'].append(resource_usage['cpu_percent'])
                if 'memory_mb' in resource_usage:
                    node_resource['memory_usage'].append(resource_usage['memory_mb'])

    # Calculate averages
    for node_kind, resources in resource_by_node_kind.items():
        if resources['gpu_usage']:
            resources['avg_gpu_memory_mb'] = sum(resources['gpu_usage']) / len(resources['gpu_usage'])
        else:
            resources['avg_gpu_memory_mb'] = 0

        if resources['cpu_usage']:
            resources['avg_cpu_percent'] = sum(resources['cpu_usage']) / len(resources['cpu_usage'])
        else:
            resources['avg_cpu_percent'] = 0

        if resources['memory_usage']:
            resources['avg_memory_mb'] = sum(resources['memory_usage']) / len(resources['memory_usage'])
        else:
            resources['avg_memory_mb'] = 0

        # Remove raw arrays
        del resources['gpu_usage']
        del resources['cpu_usage']
        del resources['memory_usage']

    return {
        "time_period_days": time_period_days,
        "resource_usage_by_dag": list(resource_by_node_kind.values())
    }

@router.get("/cogs/{cog_id}/lineage")
def get_asset_lineage(
        cog_id: int,
        depth: int = 3,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get complete lineage tree for an asset.
    Shows parent assets (what this was derived from) and child assets (what was derived from this).
    """
    cog = workflows_domain_crud.get_cog(db, cog_id, current_user.tenant_id)

    if not cog:
        raise HTTPException(status_code=404, detail="Asset not found")

    def get_ancestors(cog_obj, current_depth=0):
        """Recursively get parent assets (exclude soft-deleted)"""
        if current_depth >= depth or not cog_obj.parent_id:
            return None

        parent = workflows_domain_crud.get_cog_parent(db, cog_obj.parent_id)
        if not parent:
            return None

        return {
            "id": parent.id,
            "name": parent.name,
            "type": "folder" if parent.cog_type != models.CogType.FILE else "file",
            "created_at": parent.created_at,
            "parent": get_ancestors(parent, current_depth + 1)
        }

    def get_descendants(cog_obj, current_depth=0):
        """Recursively get child assets (exclude soft-deleted)"""
        if current_depth >= depth:
            return []

        children = workflows_domain_crud.get_cog_children_all(db, cog_obj.id)

        return [
            {
                "id": child.id,
                "name": child.name,
                "type": "folder" if child.cog_type != models.CogType.FILE else "file",
                "created_at": child.created_at,
                "processing_count": child.processing_count or 0,
                "total_cost": child.total_processing_cost or 0,
                "children": get_descendants(child, current_depth + 1)
            }
            for child in children
        ]

    return {
        "asset": {
            "id": cog.id,
            "name": cog.name,
            "created_at": cog.created_at,
            "processing_count": cog.processing_count or 0,
            "total_cost": cog.total_processing_cost or 0
        },
        "ancestors": get_ancestors(cog),
        "descendants": get_descendants(cog)
    }

@router.get("/workflow-runs/{run_id}/cost-breakdown")
def get_cost_breakdown_by_node(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get detailed cost breakdown by node for a specific workflow run.
    Shows which nodes cost the most and opportunities for optimization.
    Platform admins can see any run, others only see their tenant's runs.
    """
    # Platform admins can see all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    # Get the workflow run
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # GG.8b sealed: redact node identity from the cost breakdown for sealed buyers.
    _cost_redact = (
        _sealed.should_redact_graph(db, db_run.workflow, current_user)
        if db_run.workflow else False
    )

    # Cogs record their per-run node costs inside processing_metadata
    # (shape: {"workflow_runs": [{"workflow_run_id": N, "nodes_executed": [...]}]}).
    # WorkflowRunFile.file_id is an opaque storage UUID (FK to files.id), not
    # Cog.id, so it can't be joined here — scan the tenant's cogs that carry
    # processing metadata and let the inner filter pick out this run.
    cogs = workflows_domain_crud.get_cogs_with_processing_metadata(db, current_user.tenant_id)

    # Aggregate costs by node
    cost_by_node = {}
    total_cost = 0
    files_processed = 0

    for cog in cogs:
        metadata = cog.processing_metadata or {}
        cog_in_run = False
        for workflow_run in metadata.get('workflow_runs', []):
            if workflow_run.get('workflow_run_id') == run_id:
                cog_in_run = True
                for node in workflow_run.get('nodes_executed', []):
                    node_id = node.get('node_id')
                    cost = node.get('cost', 0)

                    if node_id not in cost_by_node:
                        cost_by_node[node_id] = {
                            'node_id': node_id,
                            'node_name': None if _cost_redact else node.get('node_name'),
                            'node_kind': None if _cost_redact else node.get('node_kind'),
                            'total_cost': 0,
                            'executions': 0,
                            'cost_per_execution': 0
                        }

                    cost_by_node[node_id]['total_cost'] += cost
                    cost_by_node[node_id]['executions'] += 1
                    total_cost += cost
        if cog_in_run:
            files_processed += 1

    # Calculate cost per execution and percentage
    for node_id, node_data in cost_by_node.items():
        node_data['cost_per_execution'] = node_data['total_cost'] / node_data['executions']
        node_data['percentage_of_total'] = (node_data['total_cost'] / total_cost * 100) if total_cost > 0 else 0

    # Sort by total cost descending
    sorted_nodes = sorted(cost_by_node.values(), key=lambda x: x['total_cost'], reverse=True)

    return {
        "workflow_run_id": run_id,
        "total_cost": total_cost,
        "total_files_processed": files_processed,
        "cost_per_file": total_cost / files_processed if files_processed > 0 else 0,
        "nodes": sorted_nodes
    }

@router.get("/{workflow_id}/export",
            dependencies=[Depends(require_auditor_data_content)])
def export_workflow(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Export a workflow as a ZIP bundle.

    The ZIP contains:
    - workflow.json: Workflow definition (graph, name, required nodes)
    - dags/: All DAG Python files referenced by the workflow

    This makes the export fully portable — the recipient gets both the
    workflow graph AND the processing code needed to run it.
    """
    # Fetch workflow with version
    workflow = workflows_domain_crud.get_workflow_for_export(db, workflow_id, current_user.tenant_id)

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if not _user_can_access_workflow(db, current_user, workflow, "view"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    # GG.8 — sealed installed copies can't be exported (the ZIP ships the graph).
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, workflow, current_user)

    if not workflow.latest_version:
        raise HTTPException(status_code=400, detail="Workflow has no versions")

    graph_json = workflow.latest_version.graph_json
    # GG.8c — the exempt reader (only one who can export a sealed copy) gets the
    # expanded publisher pipeline, not the buyer's empty placeholder.
    if _sealed.is_sealed(db, workflow):
        graph_json = _sealed.expand_for_run(db, workflow.latest_version)

    # Extract required node_kinds from the graph
    required_nodes = set()
    for node in graph_json.get("nodes", []):
        node_kind = node.get("data", {}).get("node_kind")
        if node_kind:
            required_nodes.add(node_kind)

    # Build the workflow.json manifest
    workflow_manifest = {
        "export_version": "2.0",
        "name": workflow.name,
        "description": None,
        "graph_json": graph_json,
        "required_nodes": list(required_nodes),
    }

    # GSEC FX.79 p3b — a data-marked workflow (CUI / ITAR / export-controlled
    # admin mark, services/data_markings) carries its marking WITH the export:
    # the manifest gets the stamp fields, the ZIP gets a MARKING.txt sidecar
    # (the graph + DAG files are never rewritten), and the response gets
    # X-MOD-Marking. An unmarked workflow is byte-for-byte the old export.
    # 1788 CE carve: marking_stamps is paid; with it absent the export simply
    # carries no marking — no stamp fields, no MARKING.txt sidecar, no
    # X-MOD-Marking headers (byte-for-byte the pre-FX.79 export). SaaS unchanged.
    from utils.paid_import import optional_paid
    _mk = optional_paid("services.marking_stamps")
    _stamp = None
    if _mk is not None:
        _stamp = _mk.export_stamp(db, workflow.tenant_id, "workflow", workflow_id)
        workflow_manifest.update(_mk.manifest_fields(_stamp))

    # Build the ZIP in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add workflow.json
        zf.writestr("workflow.json", json.dumps(workflow_manifest, indent=2))

        # Add each DAG file
        included_dags = []
        missing_dags = []
        for node_kind in required_nodes:
            metadata_path = _find_metadata_file(node_kind)
            if metadata_path:
                filename = os.path.basename(metadata_path)
                zf.write(metadata_path, f"dags/{filename}")
                included_dags.append(node_kind)
            else:
                missing_dags.append(node_kind)

        if missing_dags:
            logging.warning(f"Workflow export {workflow_id}: could not find metadata files for: {missing_dags}")

        # GSEC FX.79 p3b — the marking sidecar: the ZIP's binary/DAG content
        # is untouched, the marking rides along as a plain-text entry.
        if _stamp:
            zf.writestr(_mk.SIDECAR_NAME, _mk.sidecar_text(_stamp))

    zip_buffer.seek(0)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", workflow.name)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_workflow.zip"',
            **(_mk.stamp_headers(_stamp) if _mk is not None else {}),
        }
    )

@router.post("/import", response_model=schemas.WorkflowImportResult)
def import_workflow(
        import_data: schemas.WorkflowImport,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Import a workflow from an exported JSON payload (legacy v1.0 format).

    The import will:
    1. Check if user has access to all required nodes
    2. Create a new workflow with the imported graph
    3. Report any missing nodes that prevent execution
    """
    workflow_data = import_data.workflow_data
    new_name = import_data.new_name or workflow_data.name

    return _create_imported_workflow(db, current_user, workflow_data.graph_json,
                                     workflow_data.required_nodes, new_name)

def _validate_import_zip_entries(zf: zipfile.ZipFile, db: Session) -> None:
    """GSEC R-007 (SEC-6-001): reject a workflow-import ZIP before anything is
    written to disk.

    - Absolute paths, drive letters and `..` traversal in any entry name.
    - Symlink / hardlink entries (external_attr high bits).
    - More than `workflow_import.zip_max_entries` entries.
    - Total uncompressed size over `workflow_import.zip_max_uncompressed_bytes`
      (checked entry-by-entry while reading, so decompression never runs past
      the cap — the zip-bomb guard).

    Raises 422 with a clear detail on any violation.
    """
    from services import platform_settings as _psr_zip
    try:
        max_entries = int(_psr_zip.get_setting("workflow_import.zip_max_entries", db=db))
        max_bytes = int(_psr_zip.get_setting("workflow_import.zip_max_uncompressed_bytes", db=db))
    except Exception as e:  # noqa: BLE001 — settings must not break import
        logging.warning("zip-import cap settings unreadable, using defaults: %s", e)
        max_entries, max_bytes = 500, 64 * 1024 * 1024

    names = zf.namelist()
    if len(names) > max_entries:
        raise HTTPException(
            status_code=422,
            detail=f"ZIP contains {len(names)} entries; maximum allowed is {max_entries}",
        )

    total_uncompressed = 0
    for info in zf.infolist():
        name = info.filename
        # Reject absolute paths and drive-lettered paths
        if name.startswith("/") or name.startswith("\\") or (len(name) >= 2 and name[1] == ":"):
            raise HTTPException(status_code=422, detail=f"ZIP entry {name!r} has an absolute path")
        # Reject `..` traversal in any path segment
        parts = name.replace("\\", "/").split("/")
        if any(part == ".." for part in parts):
            raise HTTPException(status_code=422, detail=f"ZIP entry {name!r} attempts path traversal")
        # Reject symlink and other non-regular special entries (symlink,
        # device, fifo, socket) — a zip may carry these in external_attr.
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type not in (0, 0o100000):  # 0 = mode unset, 0o100000 = regular
            raise HTTPException(
                status_code=422,
                detail=f"ZIP entry {name!r} is not a regular file (mode {oct(file_type)})",
            )
        # Accumulate uncompressed size, reading incrementally so we stop
        # at the cap (a bomb's later entries are never inflated).
        if not name.endswith("/"):
            total_uncompressed += info.file_size
            if total_uncompressed > max_bytes:
                raise HTTPException(
                    status_code=422,
                    detail=f"ZIP uncompressed size exceeds the {max_bytes}-byte limit",
                )


@router.post("/import-zip", response_model=schemas.WorkflowImportResult)
async def import_workflow_zip(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        background_tasks: BackgroundTasks = None
):
    """
    Import a workflow from an exported ZIP bundle.

    The ZIP should contain:
    - workflow.json: Workflow definition
    - dags/: DAG Python files

    The import will:
    1. Extract metadata files into the tenant_generated/ folder
    2. Trigger a node definition refresh so the new DAGs are registered
    3. Create the workflow with the imported graph
    """
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    # Read the ZIP into memory
    contents = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(contents), "r")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file")

    # GSEC R-007: reject hostile archives (traversal, symlinks, zip bombs)
    # before any file touches disk.
    _validate_import_zip_entries(zf, db)

    # Extract workflow.json
    if "workflow.json" not in zf.namelist():
        raise HTTPException(status_code=400, detail="ZIP must contain workflow.json")

    try:
        manifest = json.loads(zf.read("workflow.json"))
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("Invalid workflow.json: %s", e)
        raise HTTPException(status_code=400, detail="Invalid workflow.json")

    graph_json = manifest.get("graph_json")
    required_nodes = manifest.get("required_nodes", [])
    workflow_name = manifest.get("name", "Imported Workflow")

    if not graph_json:
        raise HTTPException(status_code=400, detail="workflow.json missing graph_json")

    # Extract metadata files into tenant_generated/ folder
    tenant_id = current_user.tenant_id
    tenant_dag_dir = os.path.join(METADATA_ROOT, "tenant_generated")
    installed_dags = []

    # Ensure tenant_generated directory exists
    os.makedirs(tenant_dag_dir, exist_ok=True)

    for name in zf.namelist():
        if not name.startswith("dags/") or not name.endswith(".py"):
            continue

        metadata_filename = os.path.basename(name)
        if not metadata_filename:
            continue

        # SECURITY: Validate filename — no path traversal
        if ".." in metadata_filename or "/" in metadata_filename:
            continue

        # Prefix with tenant ID so DAGs are scoped to the importing tenant
        target_filename = f"tenant_{tenant_id}_{metadata_filename}"
        target_path = os.path.join(tenant_dag_dir, target_filename)

        # GSEC R-007: the scanner no longer executes DAG .py files, but we gate at
        # import too so a non-manifest (arbitrary-code) file is rejected with a
        # clear 422 instead of being stored and silently never registering.
        from services.node_scanner import is_literal_metadata_manifest
        try:
            metadata_content = zf.read(name).decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=422,
                detail=f"python DAG '{metadata_filename}' is not valid UTF-8 text",
            )
        if not is_literal_metadata_manifest(metadata_content):
            raise HTTPException(
                status_code=422,
                detail="python DAG import requires the manifest format "
                       f"(literal assignments only) — file '{metadata_filename}' "
                       "contains non-literal code and was rejected",
            )

        # Rewrite tenant_id in the metadata block to this tenant
        metadata_content = re.sub(
            r'(tenant_id\s*=\s*)(?:None|\d+)',
            rf'\g<1>{tenant_id}',
            metadata_content,
            count=1
        )

        with open(target_path, "w", encoding="utf-8") as f:
            f.write(metadata_content)

        installed_dags.append(metadata_filename)
        logging.info(f"Installed DAG file: {target_path}")

    zf.close()

    # Trigger node definition refresh if we installed any DAGs. Runs the scan
    # as a background task (own session — the request session may be rolled
    # back by the caller) so the imported nodes register right away.
    if installed_dags:
        try:
            from services.node_scanner import scan_and_register_nodes
            from database import SessionLocal
            if background_tasks is None:
                scan_and_register_nodes(SessionLocal)
            else:
                background_tasks.add_task(scan_and_register_nodes, SessionLocal)
            logging.info("Triggered node definition refresh after DAG import")
        except Exception as e:
            logging.warning(f"Could not trigger node refresh: {e}")

    # Create the workflow
    result = _create_imported_workflow(db, current_user, graph_json,
                                       required_nodes, workflow_name,
                                       installed_dags=installed_dags)
    return result

@router.post("/validate-import")
def validate_workflow_import(
        workflow_data: schemas.WorkflowExport,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Validate a legacy JSON workflow export before importing."""
    return _validate_required_nodes(db, current_user.tenant_id, workflow_data.required_nodes)
