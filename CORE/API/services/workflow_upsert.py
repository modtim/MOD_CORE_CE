"""30.6.9 c9-J2 — workflow UPSERT-by-slug service (PUT /v1/road/by-slug/{slug}).

Per-tenant slug only (uq_workflows_tenant_slug, models.py:2270 partial unique
index): another tenant holding the same slug is INVISIBLE here, so there is no
cross-tenant oracle. The tenant ALWAYS comes from the `tenant_id` argument,
never from `req` (schemas.py WorkflowUpsertRequest is extra="forbid").

Create path (mirrored, see the note below):
  services/workflows_domains_shared_service.py:104 create_imported_workflow()
  builds models.Workflow(name, tenant_id, created_by_user_id, home_zone) at
  +63, flushes, then models.WorkflowVersion(workflow_id, version_number=1,
  graph_json) at +72 and commits INTERNALLY at :184. Because that commit is
  inside the helper, a slug set after it returns is NOT atomic with the row,
  and an IntegrityError on uq_workflows_tenant_slug could not roll the
  committed row back (ISO: no deletes). So the CREATE case builds the same
  rows here, inside one db.begin_nested() savepoint, with the slug set at
  construction — the insert is then catchable and the S7 race re-selects in
  the SAME tenant and continues as the update case.

Version-append path (copied): crud/workflow_cud.py:1225-1241 — max(version_number)
+1, new models.WorkflowVersion, then workflow.latest_version_id = new id.

Hash: services/subgraph_canonical.py — canonical_hash(:181) over
canonicalize_subgraph(:63) with ALL node ids (canonicalize_subgraph takes the
node-id set it keeps, :71-77, so "all nodes" = every n["id"] in graph_json).

Cap: the JSON import route has no byte cap; the zip guard reads
`workflow_import.zip_max_uncompressed_bytes` via
services/platform_settings.get_setting (routers/workflows_domains/workflows.py:4865)
— same reading, applied to len(json.dumps(graph_json)) -> 413 graph_too_large.

Commit model: ONE db.commit() at the end of upsert(); the caller does not commit.
Every 4xx is raised BEFORE any write, so a refusal (e.g. a foreign add_to_app_id)
leaves no workflow row. Audit details carry slug/workflow_id/version_id/app_id
only — NEVER graph_json.
"""
import json
import logging
import re

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session
from sqlalchemy import func

import models
import schemas
from services import subgraph_canonical, platform_settings
from services import workflows_domains_shared_service as _svc

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,149}$")
MAX_REQUIRED_NODES = 50


def _err(status_code: int, msg: str, code: str):
    """The project's code-carrying error shape (services/stack_seed.py:55)."""
    from fastapi import HTTPException as _H
    return _H(status_code=status_code, detail={"detail": msg, "code": code})


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise _err(422, "slug must match ^[a-z][a-z0-9_-]{0,149}$", "slug_invalid")
    return slug


def graph_hash(graph_json: dict) -> str:
    """Whole-graph canonical hash (see the module docstring cite)."""
    node_ids = {n.get("id") for n in (graph_json.get("nodes") or [])
                if n.get("id") is not None}
    return subgraph_canonical.canonical_hash(
        subgraph_canonical.canonicalize_subgraph(graph_json, node_ids))


def get_by_slug(db: Session, tenant_id: int, slug: str):
    """Per-tenant slug lookup. Archived rows ARE returned; the caller decides."""
    return (db.query(models.Workflow)
            .filter(models.Workflow.tenant_id == tenant_id,
                    models.Workflow.slug == slug)
            .first())


def _latest_version(db: Session, workflow):
    if workflow.latest_version_id is not None:
        v = (db.query(models.WorkflowVersion)
             .filter(models.WorkflowVersion.id == workflow.latest_version_id)
             .first())
        if v is not None:
            return v
    return (db.query(models.WorkflowVersion)
            .filter(models.WorkflowVersion.workflow_id == workflow.id)
            .order_by(models.WorkflowVersion.version_number.desc())
            .first())


def _latest_graph_hash(db: Session, workflow):
    """Stored hash, or computed on the fly for pre-migration NULL rows.

    NEVER writes the hash onto an old version (c9 vet: no backfill writes)."""
    v = _latest_version(db, workflow)
    if v is None:
        return None
    if v.graph_hash is not None:
        return v.graph_hash
    return graph_hash(v.graph_json or {})


def _graph_size_guard(db: Session, graph_json: dict) -> None:
    """Cap = the zip-import setting, read the way workflows.py:4865 reads it."""
    try:
        max_bytes = int(platform_settings.get_setting(
            "workflow_import.zip_max_uncompressed_bytes", db=db))
    except Exception as e:  # noqa: BLE001 — same fallback as the zip guard
        logging.warning("upsert cap setting unreadable, using default: %s", e)
        max_bytes = 64 * 1024 * 1024
    if len(json.dumps(graph_json)) > max_bytes:
        raise _err(413, "graph_json exceeds the import size cap", "graph_too_large")


def _validate_required_nodes(db: Session, tenant_id: int, required_nodes: list) -> None:
    """Reuse the shared validator (routers/workflows_domains/_shared.py:218)."""
    res = _svc.validate_required_nodes(db, tenant_id, list(required_nodes))
    if not res.get("valid"):
        raise _err(422, res.get("message") or "required nodes unavailable",
                   "required_nodes_unavailable")


def _audit(db: Session, tenant_id: int, user_id, event_name: str, details: dict) -> None:
    """Audit rows carry slug/workflow_id/version_id/app_id ONLY — never graph_json
    (S4). Shape copied from routers/workflows_domains/workflows.py:235."""
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=event_name,
        details=details,
    ))


def _get_app(db: Session, tenant_id: int, app_id: int):
    """Tenant-pinned App fetch (routers/apps.py:151 _get_app pattern).

    Missing or foreign-tenant -> 404 app_not_found (no existence leak)."""
    app = (db.query(models.App)
           .filter(models.App.id == app_id, models.App.tenant_id == tenant_id)
           .first())
    if app is None or app.deleted_at is not None:
        raise _err(404, "App not found", "app_not_found")
    return app


def _add_to_app_scope(db: Session, app, tenant_id: int, user_id,
                      workflow_id: int) -> bool:
    """Append the workflow id to the App's JSONB scope list (S10: same
    transaction as the upsert). Already present -> no-op, no audit (Q2 A)."""
    current = list(app.allowed_workflow_ids or [])
    if workflow_id in current:
        return False
    app.allowed_workflow_ids = current + [workflow_id]
    flag_modified(app, "allowed_workflow_ids")
    _audit(db, tenant_id, user_id, "WORKFLOW_APP_SCOPE_ADDED",
           {"app_id": app.id, "workflow_id": workflow_id})
    return True


def upsert(db: Session, *, user, tenant_id: int, slug: str,
           req: "schemas.WorkflowUpsertRequest", installed_dags=None
           ) -> "schemas.WorkflowUpsertResult":
    """Idempotent per-tenant UPSERT. Commits ONCE at the end; the caller does not.

    Authorization is NOT here (c9-J3 does it in the router). The tenant comes
    from the argument only (S1). Raises the vet's error set before any write,
    so a refusal leaves no rows (S10/atomicity)."""
    slug = validate_slug(slug)
    if len(req.required_nodes) > MAX_REQUIRED_NODES:
        raise _err(422, "required_nodes exceeds 50", "too_many_required_nodes")
    _graph_size_guard(db, req.graph_json)
    _validate_required_nodes(db, tenant_id, req.required_nodes)

    # Resolve the App BEFORE any write: a foreign/missing add_to_app_id 404s
    # with no workflow row at all (reviewer check 2 / T10).
    app = None
    if req.add_to_app_id is not None:
        app = _get_app(db, tenant_id, req.add_to_app_id)

    created = False
    version_created = False
    wf = get_by_slug(db, tenant_id, slug)

    if wf is not None and wf.archived_at is not None:
        # ISO: never resurrect, never twin.
        raise _err(409, "workflow is archived", "workflow_archived")

    new_hash = graph_hash(req.graph_json)
    version_id = None

    if wf is None:
        # CREATE — savepoint so an IntegrityError on uq_workflows_tenant_slug
        # rolls back ONLY this insert and the race re-selects (S7, no 500).
        try:
            with db.begin_nested():
                new_wf = models.Workflow(
                    name=req.name,
                    tenant_id=tenant_id,
                    created_by_user_id=(user.id if user is not None else None),
                    slug=slug,
                )
                db.add(new_wf)
                db.flush()
                new_version = models.WorkflowVersion(
                    workflow_id=new_wf.id,
                    created_by_user_id=(user.id if user is not None else None),
                    version_number=1,
                    graph_json=req.graph_json,
                    graph_hash=new_hash,
                )
                db.add(new_version)
                db.flush()
                new_wf.latest_version_id = new_version.id
                # psycopg2 defers INSERT errors to the next round-trip; this
                # read forces a uq_workflows_tenant_slug violation to surface
                # INSIDE the savepoint so the except below can re-select.
                db.query(models.Workflow).filter(
                    models.Workflow.id == new_wf.id).first()
                version_id = new_version.id
                created = True
                version_created = True
                wf = new_wf
                _audit(db, tenant_id, user.id if user is not None else None,
                       "WORKFLOW_UPSERT_CREATED",
                       {"slug": slug, "workflow_id": new_wf.id})
        except IntegrityError:
            # the begin_nested() context already rolled the SAVEPOINT back;
            # a session-level rollback would wipe the outer transaction.
            wf = get_by_slug(db, tenant_id, slug)
            if wf is None:
                raise _err(500, "slug race lost with no row", "slug_race_lost")
            if wf.archived_at is not None:
                raise _err(409, "workflow is archived", "workflow_archived")

    if not created:
        # UPDATE case (also the race-lost path above).
        old_hash = _latest_graph_hash(db, wf)
        if old_hash != new_hash:
            latest_number = (db.query(func.max(models.WorkflowVersion.version_number))
                             .filter(models.WorkflowVersion.workflow_id == wf.id)
                             .scalar() or 0)
            new_version = models.WorkflowVersion(
                workflow_id=wf.id,
                created_by_user_id=(user.id if user is not None else None),
                version_number=latest_number + 1,
                graph_json=req.graph_json,
                graph_hash=new_hash,
            )
            db.add(new_version)
            db.flush()
            wf.latest_version_id = new_version.id
            version_id = new_version.id
            version_created = True
            db.add(wf)
            _audit(db, tenant_id, user.id if user is not None else None,
                   "WORKFLOW_UPSERT_VERSION_CREATED",
                   {"slug": slug, "workflow_id": wf.id, "version_id": version_id})
        elif req.name and wf.name != req.name:
            wf.name = req.name  # no audit of its own (brief)
            db.add(wf)

    app_scope_added = False
    if app is not None:
        app_scope_added = _add_to_app_scope(
            db, app, tenant_id, user_id_of(user), wf.id)

    db.commit()
    return schemas.WorkflowUpsertResult(
        workflow_id=wf.id,
        version_id=version_id,
        created=created,
        version_created=version_created,
        app_scope_added=app_scope_added,
    )


def wf_id(wf) -> int:
    return wf.id


def user_id_of(user):
    return user.id if user is not None else None
