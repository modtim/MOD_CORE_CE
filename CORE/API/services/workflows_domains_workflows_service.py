"""Service layer (policy) for routers/workflows_domains/workflows.py — Gate 2b.

Covers self-contained validation/policy blocks that were inline in the two
heavy/stateful handlers of that router (`trigger_workflow_run`,
`node_run_action`). Per the migration rules for this wave: those two
handlers' actual dispatch mechanics (credit reservation, capacity-queue
decision, orchestrator/queue enqueue, and the enqueue_task_internal /
_check_and_dispatch_downstream re-dispatch calls in node_run_action) are
intentionally NOT relocated here — they stay inline in the router, unchanged,
per the task instruction not to alter workflow-run dispatch / orchestrator
invocation logic. Only the surrounding read-only validation policy (which
raises/returns before any of that dispatch machinery runs) is extracted.

Each function is a verbatim extraction of logic that used to live inline —
control flow and return shapes are preserved exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

import models
from crud import workflows_domains_workflows as workflows_domains_workflows_crud


def find_missing_secret_access(
    db: Session, referenced_secret_names: Sequence[str], current_user: models.User
) -> List[str]:
    """trigger_workflow_run — strict per-secret enforcement: every secret
    name referenced by the graph must be one the RUNNING user can view.
    Returns the sorted-by-caller list of names the user cannot access (empty
    if all are accessible)."""
    from routers.secrets import _can_access_secret

    missing_secrets = []
    for name in referenced_secret_names:
        sec = workflows_domains_workflows_crud.get_secret_by_name(db, name, current_user.tenant_id)
        if not sec or not _can_access_secret(db, current_user, sec.id, "view"):
            missing_secrets.append(name)
    return missing_secrets


def find_missing_cog_view_access(
    db: Session, cog_ids: Sequence[int], current_user: models.User
) -> List[int]:
    """trigger_workflow_run — strict per-file enforcement: every input cog id
    must exist in-tenant and be view-accessible to the running user (owner
    access resolves through check_user_capability_on_cog's derived-owner
    rule). Returns the ids that fail either check."""
    from permissions import check_user_capability_on_cog

    missing_cog_ids = []
    for cid in cog_ids:
        cog = workflows_domains_workflows_crud.get_cog_any_tenant(db, cid)
        if cog is None or cog.tenant_id != current_user.tenant_id:
            missing_cog_ids.append(cid)
            continue
        if not check_user_capability_on_cog(db, current_user.id, cid, "view"):
            missing_cog_ids.append(cid)
    return missing_cog_ids


def find_contentless_inputs(db: Session, cog_ids: Sequence[int]) -> List[Dict[str, Any]]:
    """trigger_workflow_run — reject FILE cogs with no stored content (an
    orphaned/failed upload) before a run is created, rather than failing deep
    inside the Input Validator container. FOLDER-type cogs and
    PLACEHOLDER_FILE are intentionally excluded (they legitimately have no
    version)."""
    contentless_inputs = []
    for cid in cog_ids:
        cog = workflows_domains_workflows_crud.get_cog_any_tenant(db, cid)
        if (cog is not None
                and cog.cog_type == models.CogType.FILE
                and not cog.latest_version_id):
            contentless_inputs.append({"cog_id": cid, "name": cog.name})
    return contentless_inputs


def clear_node_cache(
    db: Session, db_run: models.WorkflowRun, node_id: str,
) -> Dict[str, Any]:
    """node_run_action `clear_cache` — invalidate the node's content-addressed
    cache so a future reuse recomputes it. Evicts only the cache index rows
    (no blob is touched; ISO audit trail on the underlying content is
    untouched). Two eviction paths are unioned because either alone can miss:
      1. exact lookup key rebuilt from the node's latest real task payload —
         precisely the row a future `_maybe_cache_short_circuit` would hit.
      2. output_content_hash of this node's produced outputs — catches
         sibling cache rows that resolved to the same output.
    Returns {"evicted": int, "hashes": int} — caller (router) owns the
    audit-log call, commit, and response shaping (matches the original
    inline block's commit point)."""
    from routers.workflows_domains._shared import _node_output_rows
    from services import node_output_cache as _noc

    deleted_keys: set = set()

    def _del(rows):
        for row in rows:
            if row[0] is not None:
                deleted_keys.add(row[0])

    # (1) exact-key eviction from the latest real task's payload
    real_task = workflows_domains_workflows_crud.get_latest_real_task_for_node(db, db_run.id, node_id)
    if real_task is not None:
        payload = real_task.task_payload or {}
        input_hash = payload.get("source_file_location") or ""
        if input_hash:
            image_ref = payload.get("image_digest") or payload.get("processing_image") or ""
            try:
                from routers.worker_tasks import _cache_config_for_payload
                cfg = _cache_config_for_payload(payload)
            except Exception:
                cfg = {}
            img, inp, cfg_hash, t_id = _noc._key_components(
                image_ref, input_hash, cfg, db_run.tenant_id,
            )
            _del(workflows_domains_workflows_crud.get_cache_rows_by_key(db, img, inp, cfg_hash, t_id))

    # (2) output-hash eviction for this node's produced outputs
    rows = _node_output_rows(db, db_run, node_id)
    hashes = {o.content_hash for o in rows if o.content_hash}
    if hashes:
        _del(workflows_domains_workflows_crud.get_cache_rows_by_output_hashes(db, db_run.tenant_id, hashes))

    evicted = 0
    if deleted_keys:
        evicted = workflows_domains_workflows_crud.delete_cache_rows(db, list(deleted_keys))

    return {"evicted": int(evicted), "hashes": len(hashes)}
