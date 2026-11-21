"""k8s-reduction-engine (W19): in-cluster addresses for ANY managed stack family.

With ``MOD_STACK_BACKEND=k8s`` a persistent stack is a Deployment + ClusterIP
Service per compose service (`k8s_stack_render.render_service`), named
``k8s_name(stack_name, compose_service)`` in the stack namespace, exposing the
service's CONTAINER ports (`_container_ports`: ``expose`` + the container side
of ``ports``). The docker-era readiness/probe code derives worker-host / mesh
alias + published-port URLs that do not exist in a cluster, so on k8s every
non-brand stack (NIM, served LLM, generic) was never observed ready.

This module is the single helper those call sites consult FIRST:

* it returns an in-cluster address when the stack is k8s-backed, and
* ``None`` otherwise — the caller keeps its docker path unchanged (docker mode
  never gets past the env check: no DB query, no import of the renderer).

Backend selection and namespace are NOT re-derived here: they reuse
`brand_image_stacks._stack_backend` (env + W2 Cap.K8S_BACKEND gate) and
`brand_image_stacks._k8s_namespace_for` (``tenant-<id>`` or the single-ns
``MOD_K8S_STACK_NAMESPACE`` override) — the exact pair `K8sStackBackend`'s
factory (`k8s_stack_backend_for`) and the brand VLM/SDXL reachability path use,
so the DNS handed out always matches where the Service was deployed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

CLUSTER_DNS_SUFFIX = ".svc.cluster.local"


def _is_k8s(db=None, tenant_id: Optional[int] = None) -> bool:
    import os
    # Fast path: docker mode never touches the gate (byte-identical behaviour).
    if os.getenv("MOD_STACK_BACKEND", "docker").strip().lower() != "k8s":
        return False
    try:
        from services.brand_image_stacks import _stack_backend
        return _stack_backend(db, tenant_id) == "k8s"
    except Exception:  # noqa: BLE001 — gate fault => docker (fail safe)
        logger.debug("k8s_stack_endpoints: backend gate failed", exc_info=True)
        return False


def _services(row: Any) -> Dict[str, Dict[str, Any]]:
    spec = getattr(row, "compose_spec", None) or {}
    svcs = spec.get("services") if isinstance(spec, dict) else None
    if not isinstance(svcs, dict):
        return {}
    return {str(k): v for k, v in svcs.items() if isinstance(v, dict)}


def _aliases(svc: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    nets = svc.get("networks") or {}
    if isinstance(nets, dict):
        for n in nets.values():
            if isinstance(n, dict):
                out.extend(str(a) for a in (n.get("aliases") or []))
    if svc.get("container_name"):
        out.append(str(svc["container_name"]))
    return out


def _ports(svc: Dict[str, Any]) -> List[int]:
    from services.k8s_stack_render import _container_ports
    return [p for p, _proto in _container_ports(svc)]


def _cmd_port(svc: Dict[str, Any]) -> Optional[int]:
    cmd = svc.get("command")
    if isinstance(cmd, (list, tuple)):
        for i, tok in enumerate(cmd):
            if str(tok) == "--port" and i + 1 < len(cmd):
                try:
                    return int(str(cmd[i + 1]))
                except ValueError:
                    return None
    return None


def _pick_service(row: Any, *, service_name: Optional[str] = None,
                  alias: Optional[str] = None,
                  port: Optional[int] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """Which compose service the docker path meant: exact service name, then a
    network alias / container_name match, then the service exposing `port`,
    then a single-service stack, then the first service exposing any port."""
    svcs = _services(row)
    if not svcs:
        return None, {}
    for want in (service_name, alias):
        if want and want in svcs:
            return want, svcs[want]
    if alias:
        for name, svc in svcs.items():
            if alias in _aliases(svc):
                return name, svc
    if port:
        for name, svc in svcs.items():
            if int(port) in _ports(svc):
                return name, svc
    if len(svcs) == 1:
        name = next(iter(svcs))
        return name, svcs[name]
    for name, svc in svcs.items():
        if _ports(svc):
            return name, svc
    return None, {}


def service_host(stack_name: str, service_name: Optional[str],
                 tenant_id: Optional[int]) -> str:
    """``<svc>.<namespace>.svc.cluster.local`` — built exactly as
    `render_service` names the Service (no backend check; pure)."""
    import os

    from utils.paid_import import optional_paid

    # GT.D8.4: `brand_image_stacks` is PAID on CE; the CE single-cluster path
    # derives the same namespace shape (MOD_K8S_STACK_NAMESPACE override, else
    # `tenant-<id>`) without it.
    _brand = optional_paid("services.brand_image_stacks")
    if _brand is not None:
        _k8s_namespace_for = _brand._k8s_namespace_for
    else:
        def _k8s_namespace_for(tid: Optional[int]) -> str:
            override = os.getenv("MOD_K8S_STACK_NAMESPACE", "").strip()
            return override if override else f"tenant-{tid if tid is not None else 1}"
    from services.k8s_stack_render import k8s_name
    svc = k8s_name(stack_name, service_name or stack_name)
    return f"{svc}.{_k8s_namespace_for(tenant_id)}{CLUSTER_DNS_SUFFIX}"


def _stack_ident(row: Any, ref: Any = None) -> Tuple[Optional[str], Optional[int]]:
    name = getattr(ref, "stack_name", None) or getattr(row, "name", None)
    tenant = getattr(ref, "tenant_id", None)
    if tenant is None:
        tenant = getattr(row, "tenant_id", None)
    return name, tenant


def url_for(row: Any, *, service_name: Optional[str] = None,
            alias: Optional[str] = None, port: Optional[int] = None,
            path: str = "", db=None, ref: Any = None) -> Optional[str]:
    """In-cluster base URL for a stack row when k8s-backed, else None.

    Port choice when not given: the service's ``--port`` command arg if the
    Service actually exposes it, else its first container port. Returns None
    (caller keeps its own path) if no port is derivable."""
    name, tenant = _stack_ident(row, ref)
    if not name or not _is_k8s(db, tenant):
        return None
    try:
        svc_name, svc = _pick_service(row, service_name=service_name,
                                      alias=alias, port=port)
        if port is None and svc:
            exposed = _ports(svc)
            cmd = _cmd_port(svc)
            port = cmd if (cmd and cmd in exposed) else (exposed[0] if exposed else cmd)
        if not port:
            return None
        host = service_host(name, svc_name or service_name, tenant)
        return f"http://{host}:{int(port)}{path or ''}"
    except Exception:  # noqa: BLE001 — never wedge a caller; fall back to docker path
        logger.debug("k8s_stack_endpoints: url_for(%s) failed", name, exc_info=True)
        return None


def probe_host(db, ref: Any, row: Any, spec: Any) -> Optional[str]:
    """Host a readiness probe should dial on k8s, else None.

    Maps the docker-declared probe host (a network alias like a NIM's
    ``spec.alias`` / a served LLM's alias, or nothing => worker host) to the
    compose service's cluster DNS. A host that is already cluster DNS (e.g. the
    brand VLM/SDXL resolver's k8s URL) and ``status`` probes are left alone."""
    if spec is None or str(getattr(spec, "type", "") or "status").lower() == "status":
        return None
    host = getattr(spec, "host", None)
    if host and str(host).endswith(CLUSTER_DNS_SUFFIX):
        return None
    name, tenant = _stack_ident(row, ref)
    if not name or not _is_k8s(db, tenant):
        return None
    try:
        svc_name, _svc = _pick_service(row, alias=host, port=getattr(spec, "port", None))
        return service_host(name, svc_name, tenant)
    except Exception:  # noqa: BLE001
        logger.debug("k8s_stack_endpoints: probe_host(%s) failed", name, exc_info=True)
        return None


__all__ = ["CLUSTER_DNS_SUFFIX", "service_host", "url_for", "probe_host"]
