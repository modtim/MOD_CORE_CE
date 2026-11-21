"""Business logic for the `_shared.py` helper functions that embed inline
data access (Gate 2b migration of `routers/node_definitions_domains/_shared.py`
itself, openspec: stack-training-and-prewarm).

`_shared.py` hosts no endpoint handlers (Gate 2a already moved every
`@router.*` handler into a per-domain sub-module) — it is a pure
helpers/Pydantic-models/constants module the sibling domain modules
`from ._shared import *`, and that several already-migrated domain services
(`node_authoring_service.py`, `node_definitions_node_wizard_service.py`, ...)
import directly as `from routers.node_definitions_domains import _shared as sh`
and call by name (e.g. `sh._resolve_tenant_node_for_admin(...)`,
`sh._dispatch_trivy_scan(...)`).

Data access -> `crud/node_definitions_shared.py` (the ONLY place `db.query` /
`session.query` lives for this slice). Every `_shared.py` function this module
backs stays defined in `_shared.py` under its ORIGINAL name as a thin
delegation to the function here — signature, docstring and (where the
original raised) exception type are preserved exactly, because those names
are imported/monkeypatched by sibling domain modules, their services, and
tests using the pre-migration path.

Two helpers here (`resolve_tenant_node_for_admin`, `node_definition_use_capability`)
raise `HTTPException` directly rather than a `ServiceError` — matching the
pre-migration `_shared.py` behavior exactly, because callers such as
`node_authoring_service.py` invoke them directly (not through a router
try/except that translates `ServiceError` -> `HTTPException`) and rely on the
raw `HTTPException` propagating (FastAPI's own exception handler catches it
regardless of which layer raised it). This is a deliberate, narrow exception
to the normal service-layer convention (shared migration rule #5's spirit,
applied to non-endpoint shared helpers with pre-existing HTTPException
contracts).

Pure, DB-free helpers explicitly required to keep their module home in
`_shared.py` (`_image_is_in_tenant_harbor`, `_run_admission`,
`_load_node_contract`) are NOT duplicated or wrapped here — they have no
inline query to migrate and are imported directly from `_shared` below.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import crud as crud_pkg
import crud.node_definitions_shared as crud
import models
from routers.node_definitions_domains._shared import (
    _decide_harbor_scan_outcome,
    _image_is_in_tenant_harbor,
    _is_service_account,
    _looks_like_web_search_artifact,
    _parse_tenant_harbor_ref,
    _web_search_expand_budget_s,
    _web_search_expand_poll_s,
    logger,
)

__all__ = [
    "run_web_query_expansion",
    "submit_raw_context_build",
    "resolve_tenant_node_for_admin",
    "node_definition_use_capability",
    "bad_federation_peers",
    "effective_billing_user",
    "effective_creator_user_id",
    "clear_soft_deletes_for_node_refs",
    "dispatch_trivy_scan",
    "persist_scan_outcome",
    "poll_harbor_scan_async",
    "auto_calibrate_new_node_hook",
]


# ---------------------------------------------------------------------
# add-web-search-node §6 — web query-expansion for the describe-search
# discovery path. Verbatim MOVE of the pre-migration `_run_web_query_expansion`
# body — see `_shared.py` for the wrapper + full behavioral docstring.
# ---------------------------------------------------------------------

def run_web_query_expansion(
    db: Session, description: str, *, user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    description = (description or "").strip()
    if not description:
        return None
    budget = _web_search_expand_budget_s()
    if budget <= 0:
        return None
    try:
        import copy

        from models import TaskStatus
        from utils.minio_client import get_tenant_bucket_name

        # Local import to avoid a module-load-time cycle: this module is
        # imported lazily (inside `_shared.py`'s thin wrapper, at call time),
        # so `_shared` is already fully loaded by the time we get here — but
        # importing the constant this way keeps the dependency explicit and
        # avoids re-declaring it.
        from routers.node_definitions_domains._shared import _WEB_SEARCH_WORKFLOW_NAME

        wf = crud.get_system_workflow_by_name(db, _WEB_SEARCH_WORKFLOW_NAME)
        if wf is None:
            logger.info(
                "web-expand: hidden '%s' workflow not seeded — skipping "
                "expansion", _WEB_SEARCH_WORKFLOW_NAME,
            )
            return None

        version = None
        if getattr(wf, "latest_version_id", None):
            version = crud.get_workflow_version_by_id(db, wf.latest_version_id)
        if version is None:
            version = crud.get_latest_workflow_version(db, wf.id)
        if version is None or not version.graph_json:
            logger.info(
                "web-expand: workflow %s has no runnable version — skipping",
                wf.id,
            )
            return None

        # Deep-copy the seeded graph and inject QUERY into the
        # system_web_search node's extra_variables (the seeder leaves
        # QUERY=""). The orchestrator dispatches from conf["nodes"], NOT
        # from version.graph_json, so this override reaches the container
        # without mutating the shared seeded version. Both snake_case
        # (`extra_variables`, the seeder convention) and camelCase
        # (`extraVariables`, read by the worker-task extra-vars→env step)
        # are set so QUERY threads to the container env regardless.
        graph = copy.deepcopy(version.graph_json or {})
        nodes = graph.get("nodes") or []
        query_node_id: Optional[str] = None
        query_value = description[:2000]
        for n in nodes:
            data = n.get("data") or {}
            if data.get("node_kind") == "system_web_search":
                ev = dict(data.get("extra_variables") or {})
                ev["QUERY"] = query_value
                ev.setdefault("TOP_K", "5")
                data["extra_variables"] = ev
                data["extraVariables"] = dict(ev)
                n["data"] = data
                query_node_id = n.get("id")
                break
        if query_node_id is None:
            logger.info(
                "web-expand: no system_web_search node in workflow %s graph "
                "— skipping", wf.id,
            )
            return None

        tenant_id = wf.tenant_id
        tenant_bucket = get_tenant_bucket_name(tenant_id, db)
        full_conf = {
            "tenant_id": tenant_id,
            "tenant_bucket": tenant_bucket,
            "nodes": graph.get("nodes", []),
            "edges": graph.get("edges", []),
            "project_info": {
                "tenant_id": tenant_id,
                "tenant_bucket": tenant_bucket,
                "user_id": user_id,
                "cog_ids": [],
                "metadata_cog_ids": [],
                "parent_folder_id": None,
                "workflow_name": wf.name,
                "extra_variables": {"QUERY": query_value},
            },
        }

        # crud_pkg.create_workflow_run also creates the WorkflowNode + NodeRun
        # rows the /admin/web-search/result callback joins on to store the
        # artifact — without them the callback 404s and the artifact is
        # lost. It reads the (fixed) version.graph_json for those rows; the
        # QUERY override lives only in full_conf, which is what the
        # orchestrator dispatches from.
        db_run = crud_pkg.create_workflow_run(
            db=db,
            version=version,
            user_id=user_id,
            conf=full_conf,
            run_name=f"{wf.name} (node wizard expand)",
        )
        run_id = db_run.id
        full_conf["workflow_run_id"] = run_id

        from services.workflow_orchestrator import start_workflow_execution
        from database import SessionLocal
        threading.Thread(
            target=start_workflow_execution,
            args=(SessionLocal, full_conf, run_id),
            daemon=True,
        ).start()

        # Poll for the artifact (arrives on NodeRun.outputs via the
        # callback) or a terminal run status, within the budget. The
        # orchestrator + callback run in their own sessions, so expire this
        # session each tick to force a fresh DB read.
        terminal = {
            TaskStatus.COMPLETED, TaskStatus.FAILED,
            TaskStatus.SUCCESS, TaskStatus.FAILURE, TaskStatus.BLOCKED,
        }
        interval = _web_search_expand_poll_s()
        deadline = time.monotonic() + budget
        artifact: Optional[Dict[str, Any]] = None
        while time.monotonic() < deadline:
            time.sleep(interval)
            db.expire_all()
            nr = crud.get_latest_node_run_for_graph_node(db, run_id, query_node_id)
            if nr is not None and _looks_like_web_search_artifact(nr.outputs):
                artifact = nr.outputs
                break
            run_row = crud.get_workflow_run_by_id(db, run_id)
            if run_row is not None and run_row.status in terminal:
                if nr is not None and _looks_like_web_search_artifact(nr.outputs):
                    artifact = nr.outputs
                break

        if artifact is None:
            logger.info(
                "web-expand: run %s produced no artifact within %.1fs budget "
                "— falling back to keyword pool", run_id, budget,
            )
            return None
        if not artifact.get("ok", True):
            logger.info(
                "web-expand: run %s artifact ok=false (empty/failed search) "
                "— falling back to keyword pool", run_id,
            )
            return None
        logger.info(
            "web-expand: run %s artifact ready — %d jargon, %d github, "
            "%d pypi", run_id,
            len(artifact.get("jargon") or []),
            len(artifact.get("github_repos") or []),
            len(artifact.get("pypi_packages") or []),
        )
        return artifact
    except Exception as e:
        logger.warning(
            "web-expand: query expansion failed (fallback to keyword pool): "
            "%s", e,
        )
        return None


# ---------------------------------------------------------------------
# δ DAG-wizard B4 — raw-context (bring-your-own Dockerfile) build submit.
# Verbatim MOVE of the pre-migration `submit_raw_context_build` body.
# ---------------------------------------------------------------------

def submit_raw_context_build(
    db, *, image_name: str, node_name: str, dockerfile: str, context_bytes: bytes,
    tenant_id: int, user_id: int, process_type: str = "CPU",
    input_type: str = "image", output_type: str = "image",
    cost_in_credits: int = 1, description: Optional[str] = None,
    node_metadata: Optional[Dict[str, Any]] = None, filename: str = "context.tar.gz",
) -> Dict[str, Any]:
    import hashlib as _hashlib
    import io as _io
    import os as _os
    from utils.storage import get_service_client
    from node_builder.build_dag.types import BuildSpec, ContextArtifact
    from node_builder.build_dag.submit import create_build_workflow

    sha = _hashlib.sha256(context_bytes).hexdigest()
    bucket = _os.getenv("BUILD_CONTEXT_BUCKET", "platform-build-context")
    key = f"contexts/{sha}.tar.gz"
    mc = get_service_client()
    if not mc.bucket_exists(bucket):
        mc.make_bucket(bucket)
    mc.put_object(bucket, key, _io.BytesIO(context_bytes), len(context_bytes),
                  content_type="application/gzip")

    spec = BuildSpec(
        image_name=image_name, node_name=node_name, tenant_id=int(tenant_id),
        hero_dockerfile=dockerfile, raw_context=True,
        context_artifact=ContextArtifact(
            bucket=bucket, object_key=key, sha256=sha,
            size_bytes=len(context_bytes), filename=filename),
        input_type=input_type, output_type=output_type, process_type=process_type,
        cost_in_credits=cost_in_credits, description=description, node_metadata=node_metadata,
    )
    rec = models.BuildSpecRecord(tenant_id=int(tenant_id), spec_json=spec.model_dump())
    db.add(rec); db.commit(); db.refresh(rec)
    spec._build_spec_id = rec.id  # type: ignore[attr-defined]
    workflow = create_build_workflow(db, spec, user_id=user_id)
    workflow_run_id = None
    try:
        from services.workflow_orchestrator import dispatch_trigger_run
        wf = crud.get_workflow_by_id(db, workflow["workflow_id"])
        run = dispatch_trigger_run(db, workflow=wf, triggered_by_user_id=user_id,
                                   extra_inputs={"build_spec_id": rec.id})
        workflow_run_id = getattr(run, "id", None)
        rec.workflow_run_id = workflow_run_id
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("submit_raw_context_build: trigger failed: %s", e)
    logger.info("raw-context build submitted: image=%s spec=%d run=%s sha=%s (%d bytes)",
                image_name, rec.id, workflow_run_id, sha[:12], len(context_bytes))
    return {"build_spec_id": rec.id, "workflow_id": workflow["workflow_id"],
            "workflow_run_id": workflow_run_id, "context_sha256": sha}


# ---------------------------------------------------------------------
# δ §17 tenant marketplace curate — resolve + policy-check a tenant-shared
# NodeDefinition for the calling tenant admin. Raises HTTPException directly
# (see module docstring) — matches the pre-migration `_shared.py` behavior
# other domain services depend on.
# ---------------------------------------------------------------------

def resolve_tenant_node_for_admin(db, *, node_definition_id, tenant_id):
    nd = crud.get_node_definition_by_id(db, node_definition_id)
    if nd is None or nd.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if nd.tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "System-shared nodes are governed by the platform "
                "marketplace, not the tenant marketplace."
            ),
        )
    if nd.user_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "User-private nodes can't be team-granted. Promote "
                "via /node-definitions/{id}/share-with-tenant first."
            ),
        )
    return nd


def node_definition_use_capability(db):
    cap = crud.get_use_capability(db)
    if cap is None:
        # Seed regression — fail loud so it's obvious in logs/errors.
        raise HTTPException(
            status_code=500,
            detail=(
                "(node_definition, use) capability missing — "
                "seed_all_capabilities() did not run or has been "
                "modified to drop this row."
            ),
        )
    return cap


# ---------------------------------------------------------------------
# Foolproof #7 — federation peer trust. Verbatim MOVE of the
# pre-migration `_bad_federation_peers` body.
# ---------------------------------------------------------------------

def bad_federation_peers(db: Session) -> set:
    min_entries = int(os.getenv("MOD_FEDERATION_PEER_MIN_ENTRIES", "5"))
    fail_rate_cap = float(os.getenv("MOD_FEDERATION_PEER_FAIL_RATE_CAP", "0.5"))
    rows = crud.get_federation_peer_aggregates(db)
    bad: set = set()
    for r in rows:
        if int(r.entries) < min_entries:
            continue
        total = int(r.s) + int(r.f)
        if total <= 0:
            continue
        if (float(r.f) / float(total)) >= fail_rate_cap:
            bad.add(r.source_peer)
    return bad


# ---------------------------------------------------------------------
# §22.5 node-creation billing-principal resolution. Verbatim MOVE of the
# pre-migration `_effective_billing_user` / `_effective_creator_user_id` bodies.
# ---------------------------------------------------------------------

def effective_billing_user(
    db: Session, current_user: "models.User", build_spec_id: Optional[int],
) -> "models.User":
    if not build_spec_id:
        return current_user
    uname = getattr(current_user, "username", "") or ""
    # The seeded service user isn't reliably flagged is_service_account, so
    # also match it by its known username; a real user's direct create still
    # bills themselves.
    is_service = _is_service_account(current_user) or uname in (
        "api@service.local", "platform-service", "api_service",
    )
    if not is_service:
        return current_user
    try:
        rec = crud.get_build_spec_record(db, build_spec_id)
        if rec and rec.workflow_run_id:
            run = crud.get_workflow_run_by_id(db, rec.workflow_run_id)
            if run and run.triggered_by_user_id:
                u = crud.get_user_by_id(db, run.triggered_by_user_id)
                if u is not None:
                    return u
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "billing-user resolve failed for build_spec_id=%s: %s",
            build_spec_id, exc,
        )
    return current_user


def effective_creator_user_id(
    db: Session, current_user: "models.User", build_spec_id: Optional[int],
) -> Optional[int]:
    creator = effective_billing_user(db, current_user, build_spec_id)
    uname = getattr(creator, "username", "") or ""
    if _is_service_account(creator) or uname in (
        "api@service.local", "platform-service", "api_service",
    ):
        return None
    return creator.id


# ---------------------------------------------------------------------
# Tenant-image soft-delete auto-clear on re-register. Verbatim MOVE of the
# pre-migration `_clear_soft_deletes_for_node_refs` body. The service owns
# the transaction boundary (db.delete + db.commit) per services/_base.py.
# ---------------------------------------------------------------------

def clear_soft_deletes_for_node_refs(
    db: Session,
    tenant_id: Optional[int],
    node_def: "models.NodeDefinition",
) -> int:
    if tenant_id is None:
        return 0
    # Collect every (repo, tag) tail this node references — from
    # executable_path and every gpu_variants value. Same parsing
    # convention crud._hide_nodes_with_soft_deleted_images uses.
    refs: List[str] = []
    if getattr(node_def, "executable_path", ""):
        refs.append(node_def.executable_path)
    for v in (node_def.gpu_variants or {}).values():
        if isinstance(v, str) and v:
            refs.append(v)
    tails: set = set()
    for ref in refs:
        tail = ref.split("/")[-1]
        if ":" in tail:
            tails.add(tail)
    if not tails:
        return 0

    # Build the list of (repo, tag) pairs to clear.
    pairs = [tuple(t.split(":", 1)) for t in tails if t.count(":") == 1]
    if not pairs:
        return 0

    cleared = 0
    for repo, tag in pairs:
        row = crud.find_active_tenant_image_deletion(db, tenant_id, repo, tag)
        if row:
            # ISO-exception: TenantImageDeletion IS the soft-delete tombstone
            # for a Harbor image (it has no SoftDeleteMixin filter column of its
            # own — its `deleted_at` records WHEN the image was deleted). Clearing
            # the row on re-register is the image's reactivation, not a
            # business-record deletion; hard-remove is required (soft-deleting a
            # tombstone would leave the image hidden forever).
            db.delete(row)
            cleared += 1
            logger.info(
                "Auto-cleared soft-delete on re-register: tenant=%s "
                "node=%s repo=%s tag=%s",
                tenant_id, node_def.id, repo, tag,
            )
    if cleared:
        db.commit()
    return cleared


# ---------------------------------------------------------------------
# Trivy security scan dispatch (Harbor-native poll path + legacy
# TrivyScanner path). Verbatim MOVE of the pre-migration `_dispatch_trivy_scan`
# body.
# ---------------------------------------------------------------------

def dispatch_trivy_scan(db: Session, node_def_id: int, image_path: str, tenant_id: int) -> int:
    from utils.minio_client import get_tenant_bucket_name as _get_tenant_bucket_name
    if _image_is_in_tenant_harbor(image_path):
        repo, reference = _parse_tenant_harbor_ref(image_path)
        if repo and reference:
            node = crud.get_node_definition_by_id(db, node_def_id)
            if node:
                node.scan_status = "pending_scan"
                db.commit()
            poll_harbor_scan_async(
                node_def_id=node_def_id,
                tenant_id=tenant_id,
                repo=repo,
                reference=reference,
            )
            logger.info(
                "Harbor scan poll started for node %d (%s@%s)",
                node_def_id, repo, reference,
            )
            return 0
        logger.warning(
            "Tenant Harbor image %s did not parse into repo+ref; falling back to legacy trivy dispatch",
            image_path,
        )

    # Update node status
    node = crud.get_node_definition_by_id(db, node_def_id)
    if node:
        node.scan_status = "pending_scan"
        db.commit()

    from utils.registries import platform_registry
    registry = platform_registry()

    # node_definitions.executable_path stores the bare image reference
    # (e.g. "metalness:latest" or "accelerator-benchmark-cuda:latest").
    # The worker's executor prefixes the registry + project when pulling
    # the processing_image for task execution; trivy's docker pull path
    # needs the same prefix or it tries Docker Hub and 404s. Only prefix
    # when the image doesn't already have a registry host in it (contains
    # a '/' AND the first segment has a '.' or ':' indicating a registry).
    def _with_registry(img: str) -> str:
        if "/" in img:
            first = img.split("/", 1)[0]
            if "." in first or ":" in first or first == "localhost":
                return img  # already fully-qualified
        return f"{registry}/modtim/{img}"
    target_image = _with_registry(image_path)

    # The trivyscanner container needs to `docker pull` the target
    # image before scanning — its docker-socket mount uses the host
    # daemon, but the docker CLI inside the container has no auth
    # config, so it 401s against Harbor on every pull. Package the
    # Harbor creds as a base64'd ~/.docker/config.json equivalent and
    # pass as REGISTRY_AUTH env var; the entrypoint materialises it
    # into /root/.docker/config.json before the pull.
    #
    # Creds are tenant-scoped in Vault; fall back to the platform
    # tenant (id=1) because system nodes (tenant_id=NULL) don't have
    # their own secrets. Silent best-effort: if any secret is missing
    # we still dispatch — trivy will just fail with the real "no
    # basic auth credentials" message surfaced in the result.
    registry_auth_b64 = ""
    harbor_host = registry
    try:
        from crud.secret_cud import get_decrypted_secret_by_name
        cred_tenant = tenant_id or 1
        harbor_user = get_decrypted_secret_by_name(db, "harbor_username", cred_tenant)
        harbor_pass = get_decrypted_secret_by_name(db, "harbor_password", cred_tenant)
        harbor_reg = get_decrypted_secret_by_name(db, "harbor_registry", cred_tenant)
        if harbor_reg:
            harbor_host = harbor_reg.strip()
        if harbor_user and harbor_pass:
            auth_blob = base64.b64encode(f"{harbor_user}:{harbor_pass}".encode()).decode()
            docker_cfg = {"auths": {harbor_host: {"auth": auth_blob}}}
            registry_auth_b64 = base64.b64encode(json.dumps(docker_cfg).encode()).decode()
    except Exception as e:
        logger.warning("Could not build REGISTRY_AUTH for trivy dispatch: %s", e)

    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"trivy-scan-{node_def_id}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=8,
        task_payload={
            "node_kind": "trivy_scan",
            "processing_image": f"{registry}/modtim/trivyscanner:latest",
            "container_env": {
                "TARGET_IMAGE": target_image,
                "NODE_DEFINITION_ID": str(node_def_id),
                # Base64-encoded docker config JSON with Harbor auth.
                # Entrypoint.sh decodes this into /root/.docker/config.json
                # so the container's docker pull of TARGET_IMAGE auths
                # correctly against Harbor. Empty string = no auth
                # available; trivy's pull will fail with a clear 401
                # message rather than silently zero-output.
                "REGISTRY_AUTH": registry_auth_b64,
            },
            "source_file_location": "",
            # BYO-S3 aware (trivy skips download/upload, but keep bucket
            # consistent with every other task payload for this tenant).
            "minio_bucket": _get_tenant_bucket_name(tenant_id, db),
            "gpu_required": False,
            "container_timeout": 600,
            "skip_download": True,
            "skip_upload": True,
            # Trivy pulls the TARGET_IMAGE from Harbor before scanning —
            # that requires reaching a Docker daemon. Set the opt-in
            # flag so the worker bind-mounts /var/run/docker.sock into
            # the trivyscanner container. Any worker (embedded or
            # remote) that has a local Docker daemon can now claim
            # these tasks from the CPU queue; the flag just controls
            # whether the worker mounts the socket.
            "needs_docker_socket": True,
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="trivy_scan",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logger.info("Dispatched Trivy scan task %d for node %d (image: %s)", task.id, node_def_id, image_path)
    return task.id


# ---------------------------------------------------------------------
# Harbor scan-outcome persistence + background poll. Verbatim MOVE of the
# pre-migration `_persist_scan_outcome` / `_poll_harbor_scan_async` bodies.
# Both manage their OWN `SessionLocal()` session (they run outside request
# scope, from a daemon thread or the scheduler sweep) — `crud.get_node_definition_by_id`
# accepts any `Session`, so it works unchanged here.
# ---------------------------------------------------------------------

def persist_scan_outcome(node_id: int, outcome: str, detail: Optional[str]) -> None:
    from database import SessionLocal
    session = SessionLocal()
    try:
        node = crud.get_node_definition_by_id(session, node_id)
        if not node:
            return
        node.scan_status = outcome
        # `scanned_at` is the timestamp the API committed the terminal
        # outcome — distinguishes a fresh write from a stale row that
        # was stuck mid-poll for hours.
        node.scanned_at = datetime.now(timezone.utc)
        if hasattr(node, "scan_detail"):
            node.scan_detail = (detail or "")[:2000]
        session.commit()
        logger.info("Harbor scan outcome for node %d: %s (%s)", node_id, outcome, detail or "")
    except Exception as e:
        logger.error("Failed to persist scan outcome for node %d: %s", node_id, e)
        session.rollback()
    finally:
        session.close()


def poll_harbor_scan_async(
    node_def_id: int,
    tenant_id: int,
    repo: str,
    reference: str,
    timeout_s: int = 300,
    interval_s: int = 5,
) -> None:
    from database import SessionLocal

    def _worker() -> None:
        from utils.paid_import import optional_paid
        _harbor_mod = optional_paid("utils.tenant_harbor_client")
        if _harbor_mod is None:
            logger.info("SKIPPED (not in this edition): tenant harbor scan poll")
            return
        TenantHarborClient, TenantHarborError = (
            _harbor_mod.TenantHarborClient, _harbor_mod.TenantHarborError)
        started = time.time()
        try:
            harbor = TenantHarborClient()
        except Exception as e:
            logger.error("Harbor client init failed for node %d: %s", node_def_id, e)
            persist_scan_outcome(node_def_id, "scan_failed", f"harbor client init: {e}")
            return

        while time.time() - started < timeout_s:
            try:
                overview = harbor.get_scan_status(tenant_id, repo, reference)
            except TenantHarborError as e:
                logger.warning("Harbor scan poll error for node %d: %s", node_def_id, e)
                time.sleep(interval_s)
                continue
            outcome, detail = _decide_harbor_scan_outcome(overview)
            if outcome is not None:
                persist_scan_outcome(node_def_id, outcome, detail)
                return
            time.sleep(interval_s)

        persist_scan_outcome(node_def_id, "scan_failed", f"Harbor scan timeout after {timeout_s}s")

    # Mark as scanning immediately AND stamp the time so the sweep can
    # tell "started polling 30s ago" from "stuck for hours."
    session = SessionLocal()
    try:
        node = crud.get_node_definition_by_id(session, node_def_id)
        if node:
            node.scan_status = "scanning"
            # Stamp scan_result with poll_started_at so the sweep can
            # detect rows that have been mid-scan for >max_age and
            # write scan_failed:timeout instead of polling forever.
            sr = dict(node.scan_result) if isinstance(node.scan_result, dict) else {}
            sr["poll_started_at"] = datetime.now(timezone.utc).isoformat()
            sr["poll_tenant_id"] = tenant_id
            sr["poll_repo"] = repo
            sr["poll_reference"] = reference
            node.scan_result = sr
            session.commit()
    finally:
        session.close()

    thread = threading.Thread(
        target=_worker,
        name=f"harbor-scan-poll-{node_def_id}",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------
# AI scheduler auto-calibration hook. Verbatim MOVE of the pre-migration
# `_auto_calibrate_new_node_hook` body.
# ---------------------------------------------------------------------

def auto_calibrate_new_node_hook(
    db: Session, new_def: "models.NodeDefinition",
    current_user, effective_tenant_id: Optional[int],
) -> None:
    if os.getenv("AUTO_CALIBRATE_NEW_NODES", "true").lower() == "false":
        return
    try:
        from services.calibration_dispatcher import auto_queue_for_node
        dispatch_tenant_id = effective_tenant_id or getattr(current_user, "tenant_id", None)
        if dispatch_tenant_id is None:
            dispatch_tenant_id = crud.get_fallback_tenant_id(db)
        if dispatch_tenant_id is None:
            logger.info(
                "auto_queue_for_node=%s skipped (no tenant resolvable)",
                new_def.id,
            )
            return
        dispatch_result = auto_queue_for_node(
            db, node_definition_id=new_def.id,
            tenant_id_for_dispatch=dispatch_tenant_id,
        )
        try:
            db.add(models.AuditEvent(
                tenant_id=dispatch_tenant_id,
                user_id=getattr(current_user, "id", None),
                event_name="CALIBRATION_AUTO_QUEUED",
                details={
                    "trigger_source": "node_created",
                    "node_definition_id": new_def.id,
                    "node_name": new_def.name,
                    "outcome": "queued" if dispatch_result else "deferred",
                },
            ))
            db.commit()
        except Exception as audit_err:
            logger.debug(
                "AuditEvent emit failed for new-node auto-queue %d: %s",
                new_def.id, audit_err,
            )
            db.rollback()
    except Exception as e:
        logger.debug("auto_queue_for_node failed (non-critical): %s", e)
