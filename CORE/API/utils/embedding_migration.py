"""Embedding re-embed job runner (one job at a time, in-memory status).

MOD has THREE embedding stores, each pinned from a Settings → AI Configuration
tab and each with its own re-embed path. Changing a pin queues a job here:

  target = "user_rag"  → the AI Assistant's user RAG store (qdrant, per-tenant
                          `t{id}_vectors`) — rebuild-in-place per tenant.
  target = "wizard"    → the Node Wizard's pgvector proposal store — re-embed
                          from the `few_shot_text` column.
  target = "docs"      → MOD's docs store (qdrant `docs_embeddings`) — re-ingest
                          from the `.md` files (source always present).

Only one job runs at a time. Status is in-memory (the Dashboard polls it) — a
process restart loses progress but never data (each store's migration is
fail-safe / idempotent and can be re-run).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_TARGETS = ("user_rag", "wizard", "docs")

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_status: Dict[str, Any] = {
    "state": "idle",          # idle | running | done | error
    "target": None,
    "model": None,
    "started_at": None,
    "finished_at": None,
    "total_tenants": 0,       # only meaningful for user_rag
    "done_tenants": 0,
    "current_tenant": None,
    "results": [],            # per-tenant (user_rag) or single-op (wizard/docs)
    "error": None,
}


def get_status() -> Dict[str, Any]:
    with _lock:
        return dict(_status)


def is_running() -> bool:
    with _lock:
        return _status["state"] == "running"


# --- per-target workers ----------------------------------------------------

def _run_user_rag(tenant_ids: Optional[List[int]]) -> None:
    from utils import user_vector_store as uvs
    from utils.embeddings import invalidate_model_cache, current_model_name
    invalidate_model_cache()
    try:
        targets = tenant_ids if tenant_ids else uvs.list_tenant_collections()
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status.update(state="error", error=f"could not list collections: {e}")
        return
    with _lock:
        _status.update(model=current_model_name(), total_tenants=len(targets), done_tenants=0, results=[])
    for tid in targets:
        with _lock:
            _status["current_tenant"] = tid
        try:
            res = uvs.migrate_tenant_collection(tid)
        except Exception as e:  # noqa: BLE001
            res = {"tenant_id": tid, "status": "error", "error": str(e)}
            logger.warning("user_rag migration failed for tenant %s: %s", tid, e)
        with _lock:
            _status["results"].append(res)
            _status["done_tenants"] += 1
            _status["current_tenant"] = None


def _run_wizard() -> None:
    from database_wizard_vector import migrate_wizard_embeddings
    from services import wizard_pro_client as _pro
    with _lock:
        _status["model"] = _pro.model_version()  # "ce" when the sidecar is absent
    res = migrate_wizard_embeddings()
    with _lock:
        _status["results"] = [res]


def _run_docs() -> None:
    from utils.paid_import import optional_paid
    _di = optional_paid("utils.docs_ingester")
    if _di is None:
        return  # CE: docs_ingester denied — route returns 402 before starting
    with _lock:
        _status["model"] = _di.resolve_docs_model()
    res = _di.migrate_docs_embeddings()
    with _lock:
        _status["results"] = [res]


def _run(target: str, tenant_ids: Optional[List[int]]) -> None:
    try:
        if target == "user_rag":
            _run_user_rag(tenant_ids)
        elif target == "wizard":
            _run_wizard()
        elif target == "docs":
            _run_docs()
        with _lock:
            if _status["state"] != "error":
                _status["state"] = "done"
    except Exception as e:  # noqa: BLE001
        logger.exception("embedding migration (%s) crashed", target)
        with _lock:
            _status.update(state="error", error=str(e))
    finally:
        with _lock:
            _status["finished_at"] = time.time()
    logger.info("embedding migration finished: target=%s state=%s", target, get_status()["state"])


def start(target: str = "user_rag", tenant_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Kick off a re-embed for `target` in a background thread. Refuses (returns
    started=False) if a job is already running."""
    global _thread
    if target not in VALID_TARGETS:
        return {"started": False, "error": f"unknown target {target!r}"}
    with _lock:
        if _status["state"] == "running":
            return {"started": False, **_status}
        _status.update(state="running", target=target, model=None, started_at=time.time(),
                       finished_at=None, error=None, results=[], done_tenants=0,
                       total_tenants=0, current_tenant=None)
    _thread = threading.Thread(target=_run, args=(target, tenant_ids), daemon=True,
                               name=f"embed-migration-{target}")
    _thread.start()
    return {"started": True, **get_status()}
