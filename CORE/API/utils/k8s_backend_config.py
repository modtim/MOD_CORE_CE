"""k8s-reduction-engine — backend selection config (task T7, design.md
"Config / tiering").

Reads `MOD_EXECUTOR_BACKEND` / `MOD_STACK_BACKEND` (docker|k8s, default
docker) at call time — same pattern as `services/stack_prewarm._enabled()` —
so a deploy/test can flip it via env with no code change. Default-OFF /
fail-safe: any unrecognized value falls back to "docker" rather than
silently routing task/stack submission at a K8s cluster that may not exist.

This module is CONFIG + CONNECTION ONLY. It does not implement K8sBackend
(T2) or K8sStackBackend (T4) — those consult `get_executor_backend()` /
`get_stack_backend()` (or the `is_*_k8s()` convenience wrappers) to decide
whether to route through their K8s path, and call `load_k8s_client()` /
`load_k8s_api_client()` to get a connected client without knowing how
in-cluster vs. kubeconfig resolution works.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

BACKEND_DOCKER = "docker"
BACKEND_K8S = "k8s"

_VALID_BACKENDS = (BACKEND_DOCKER, BACKEND_K8S)


def _normalize_backend(raw: Optional[str]) -> str:
    """Validate a raw backend string, defaulting to docker for empty/unknown
    input. Fail-safe: an on-prem box with no K8s cluster configured should
    never silently try to submit K8s Jobs/Deployments."""
    value = (raw or "").strip().lower()
    if value in _VALID_BACKENDS:
        return value
    if value:
        logger.warning(
            "k8s_backend_config: unrecognized backend %r, falling back to %r",
            raw, BACKEND_DOCKER,
        )
    return BACKEND_DOCKER


def get_executor_backend() -> str:
    """Which backend ephemeral node tasks submit to: "docker" (today's
    Docker-container executor) or "k8s" (T2's K8s Job executor)."""
    return _normalize_backend(os.getenv("MOD_EXECUTOR_BACKEND"))


def get_stack_backend() -> str:
    """Which backend persistent stacks (NIM/VLM/SDXL/etc.) deploy to:
    "docker" (today's docker-compose stacks) or "k8s" (T4's K8sStackBackend:
    Deployment+Service+PVC)."""
    return _normalize_backend(os.getenv("MOD_STACK_BACKEND"))


def is_executor_k8s() -> bool:
    return get_executor_backend() == BACKEND_K8S


def is_stack_k8s() -> bool:
    return get_stack_backend() == BACKEND_K8S


def get_kubeconfig_path() -> Optional[str]:
    """Explicit kubeconfig file for the k8s client loader. Empty/unset ⇒
    load_k8s_client() tries in-cluster ServiceAccount config first, falling
    back to $KUBECONFIG / ~/.kube/config (kubernetes-py's own default
    resolution)."""
    return os.getenv("KUBECONFIG") or None


def load_k8s_client(kubeconfig_path: Optional[str] = None):
    """Load a Kubernetes API client — CONNECTION ONLY, no operations.

    Resolution order (matches the Go k8sclient loader in
    CORE/Worker/internal/k8sclient/client.go):
      1. In-cluster ServiceAccount config (kubernetes.config.load_incluster_config),
         used when the API process itself runs as a pod (granted the RBAC in
         CORE/Worker/k8s/rbac.yaml).
      2. An explicit/kubeconfig-env/default-path kubeconfig file
         (kubernetes.config.load_kube_config), for a process that runs
         outside the cluster but submits Jobs/Deployments into it.

    Returns a `kubernetes.client.ApiClient`. Raises
    `kubernetes.config.ConfigException` if neither resolves. T2/T4 build
    their typed clients (BatchV1Api/AppsV1Api/CoreV1Api/...) from this.
    """
    from kubernetes import client, config  # local import: optional dep, only
    # needed on a box that actually enables the k8s backend.

    try:
        config.load_incluster_config()
        logger.info("load_k8s_client: using in-cluster ServiceAccount config")
    except config.ConfigException:
        path = kubeconfig_path or get_kubeconfig_path()
        config.load_kube_config(config_file=path)
        logger.info(
            "load_k8s_client: using kubeconfig %s", path or "(default ~/.kube/config)",
        )

    return client.ApiClient()


def load_k8s_api_client(kubeconfig_path: Optional[str] = None):
    """Alias of load_k8s_client — kept for callers that prefer the more
    explicit name (mirrors Go's LoadClientset naming)."""
    return load_k8s_client(kubeconfig_path=kubeconfig_path)
