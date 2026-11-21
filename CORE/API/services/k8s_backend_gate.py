"""k8s-reduction-engine T11 — capability gate on the K8s backend.

`utils.k8s_backend_config` (T7) answers "what does the ENV select?"
(MOD_EXECUTOR_BACKEND / MOD_STACK_BACKEND, fail-safe docker). This module
answers "what backend is this TENANT actually allowed to use?": "k8s" only if
the env selects k8s AND the tenant's entitlements grant `Cap.K8S_BACKEND`
(granted by the tier bundles in utils/entitlements.py); otherwise "docker", with the reason logged.

Strict by design: it checks the *granted* bit (`EntitlementSet.has`), NOT the
warn/enforce rollout decision — the rollout switch softens denials of UI
features, but it must never route a tenant without the granted bit at a
Kubernetes cluster. Any failure resolving entitlements fails safe to docker.

Callers (T2 K8sBackend dispatch, T4 K8sStackBackend deploy) should use
`effective_executor_backend` / `effective_stack_backend` in place of the raw
`get_executor_backend()` / `get_stack_backend()` accessors.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from utils import entitlements
from utils.entitlements import Cap
from utils.k8s_backend_config import (
    BACKEND_DOCKER,
    BACKEND_K8S,
    get_executor_backend,
    get_stack_backend,
)

logger = logging.getLogger(__name__)


def _load_tenant(db, tenant_id):
    if db is None or tenant_id is None:
        return None
    try:
        import models
        return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    except Exception:
        logger.exception("k8s_backend_gate: tenant %s lookup failed", tenant_id)
        return None


def tenant_has_k8s_backend(db, tenant_id=None, tenant: Any = None) -> bool:
    """True iff the tenant's resolved entitlements grant Cap.K8S_BACKEND.

    SaaS resolves from the tenant's subscription plan; self-host resolves the
    deployment-wide license (tenant ignored) — both via
    `utils.entitlements.get_entitlements`. Errors => False (fail safe)."""
    if tenant is None:
        tenant = _load_tenant(db, tenant_id)
    try:
        es = entitlements.get_entitlements(db=db, tenant=tenant)
    except Exception:
        logger.exception("k8s_backend_gate: entitlement resolution failed")
        return False
    return es.has(Cap.K8S_BACKEND)


def _effective(kind: str, env_getter: Callable[[], str], db, tenant_id,
               tenant: Any) -> str:
    selected = env_getter()
    if selected != BACKEND_K8S:
        return BACKEND_DOCKER
    if tenant_has_k8s_backend(db, tenant_id=tenant_id, tenant=tenant):
        return BACKEND_K8S
    logger.warning(
        "k8s_backend_gate: %s backend env selects k8s but tenant %s lacks "
        "capability %r (not granted); using docker",
        kind, tenant_id if tenant_id is not None else getattr(tenant, "id", None),
        Cap.K8S_BACKEND,
    )
    return BACKEND_DOCKER


def effective_executor_backend(db, tenant_id=None, tenant: Any = None) -> str:
    """Backend for ephemeral node tasks: "k8s" only if MOD_EXECUTOR_BACKEND=k8s
    AND the tenant holds Cap.K8S_BACKEND; else "docker"."""
    return _effective("executor", get_executor_backend, db, tenant_id, tenant)


def effective_stack_backend(db, tenant_id=None, tenant: Any = None) -> str:
    """Backend for persistent stacks: "k8s" only if MOD_STACK_BACKEND=k8s AND
    the tenant holds Cap.K8S_BACKEND; else "docker"."""
    return _effective("stack", get_stack_backend, db, tenant_id, tenant)


def executor_backend_for_task(db, task: Any) -> str:
    """W7: per-task executor backend for a claimed ApiTaskQueue row, using the
    task's OWN tenant (not the worker's). Never raises: any error => "docker"."""
    try:
        return effective_executor_backend(db, tenant_id=getattr(task, "tenant_id", None))
    except Exception:
        logger.exception("k8s_backend_gate: executor backend for task %s failed; using docker",
                         getattr(task, "id", None))
        return BACKEND_DOCKER


def _cluster_dispatch_allowed(db, tenant_id, tenant: Any,
                              requested_name: str) -> Optional[str]:
    """GT.D8.2 — named-cluster overflow check.

    Returns None when the tenant MAY dispatch to `requested_name`, or a
    short reason string when it may not (caller logs + fails safe to
    docker). Rules (fail-safe: ANY error resolving the registry or the
    tenant's entitlements => not allowed):

      * the cluster must exist in the tenant's live active registry rows;
      * self-host / CE: any live active cluster may be dispatched to (no
        cluster-count limit, owner 11-09);
      * SaaS: a tenant WITHOUT Cap.K8S_MULTI_CLUSTER may dispatch only to
        its FIRST active cluster — overflow clusters fall back to docker
        with a warning, never a hard crash mid-run.
    """
    try:
        from services import k8s_cluster_registry as registry
        clusters = registry.active_clusters(db, tenant_id)
    except Exception:
        logger.exception(
            "k8s_backend_gate: active_clusters lookup failed; using docker")
        return "registry lookup failed"
    if not clusters:
        return "cluster %r not found in registry" % (requested_name,)
    wanted = next((c for c in clusters if c.name == requested_name), None)
    if wanted is None:
        return "cluster %r not found in registry" % (requested_name,)
    if wanted.id == clusters[0].id:
        return None  # the first active cluster — always allowed
    if not entitlements.is_saas():
        return None  # self-host / CE: no cluster-count limit
    # Overflow (not the first): only the multi-cluster cap may route it.
    try:
        if tenant is None:
            tenant = _load_tenant(db, tenant_id)
        es = entitlements.get_entitlements(db=db, tenant=tenant)
    except Exception:
        logger.exception("k8s_backend_gate: entitlement resolution failed")
        return "entitlement resolution failed"
    if es.has(Cap.K8S_MULTI_CLUSTER):
        return None
    return ("tenant lacks %r — dispatching only to the first active "
            "cluster" % (Cap.K8S_MULTI_CLUSTER,))


def effective_executor_cluster_backend(db, tenant_id=None, tenant: Any = None,
                                       cluster_name: Optional[str] = None) -> str:
    """GT.D8.2 — like `effective_executor_backend`, but for a NAMED
    cluster. `cluster_name=None` is the default dispatch (env KUBECONFIG
    / first active cluster) and behaves exactly as before. A non-first
    cluster for a tenant without Cap.K8S_MULTI_CLUSTER falls back to
    docker with a warning (never a hard crash mid-run)."""
    if cluster_name is None:
        return effective_executor_backend(db, tenant_id=tenant_id, tenant=tenant)
    reason = _cluster_dispatch_allowed(db, tenant_id, tenant, cluster_name)
    if reason is not None:
        logger.warning(
            "k8s_backend_gate: executor dispatch to cluster %r for tenant %s "
            "refused (%s); using docker", cluster_name,
            tenant_id if tenant_id is not None else getattr(tenant, "id", None),
            reason)
        return BACKEND_DOCKER
    return effective_executor_backend(db, tenant_id=tenant_id, tenant=tenant)


def effective_stack_cluster_backend(db, tenant_id=None, tenant: Any = None,
                                    cluster_name: Optional[str] = None) -> str:
    """GT.D8.2 — like `effective_stack_backend`, but for a NAMED cluster
    (see `effective_executor_cluster_backend` for the overflow rules)."""
    if cluster_name is None:
        return effective_stack_backend(db, tenant_id=tenant_id, tenant=tenant)
    reason = _cluster_dispatch_allowed(db, tenant_id, tenant, cluster_name)
    if reason is not None:
        logger.warning(
            "k8s_backend_gate: stack deploy to cluster %r for tenant %s "
            "refused (%s); using docker", cluster_name,
            tenant_id if tenant_id is not None else getattr(tenant, "id", None),
            reason)
        return BACKEND_DOCKER
    return effective_stack_backend(db, tenant_id=tenant_id, tenant=tenant)


def task_payload_with_executor_backend(db, task: Any):
    """W7: copy of the task's payload with `executor_backend` ("docker"|"k8s")
    set, for the worker poll response. The worker routes per task on it; a
    missing field (old API) means docker. Never mutates the ORM JSONB and never
    raises (errors => executor_backend="docker")."""
    payload = getattr(task, "task_payload", None)
    if payload is None:
        return None
    try:
        out = dict(payload)
    except Exception:
        return payload
    backend = executor_backend_for_task(db, task)
    out["executor_backend"] = backend
    # 14.13 (D-GPU Layer 2): for k8s tasks, ride the API-computed pod-spec GPU
    # fragment (14.12 translator run against the target cluster's stored GI.2
    # probe) onto the poll payload as `k8s_gpu`. The Go Job builder applies it
    # verbatim; absent (docker / non-GPU / env-only / CE / any error) the
    # worker falls back to today's whole-GPU placement. Never raises, never
    # mutates the stored JSONB.
    if backend == BACKEND_K8S:
        try:
            from services import k8s_cluster_registry as _registry
            k8s_gpu = _registry.build_k8s_gpu(db, task)
            if k8s_gpu:
                out["k8s_gpu"] = k8s_gpu
        except Exception:  # noqa: BLE001 — never block a claim over GPU sizing
            logger.exception("k8s_backend_gate: k8s_gpu build failed for task "
                             "%s; worker falls back to its placement",
                             getattr(task, "id", None))
    return out


__all__ = [
    "tenant_has_k8s_backend",
    "effective_executor_backend",
    "effective_stack_backend",
    "executor_backend_for_task",
    "task_payload_with_executor_backend",
    "effective_executor_cluster_backend",
    "effective_stack_cluster_backend",
]
