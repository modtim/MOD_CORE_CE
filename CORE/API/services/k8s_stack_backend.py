"""k8s-reduction-engine — design.md Seam 2 / tasks.md T4.

`K8sStackBackend`: persistent-stack lifecycle on Kubernetes.

    deploy(stack_name, compose_spec)   compose_spec -> PVC + Deployment + Service (idempotent)
    scale(stack_name, replicas)        wake=1 / sleep=0 (sleep frees the GPU/VRAM)
    endpoint(stack_name, ...)          http://<svc>.<ns>.svc.cluster.local:<port>
    teardown(stack_name, delete_volumes=False)
    status(stack_name)                 ready/desired replicas (wake-readiness)

Manifests come from T3's pure renderer (`services.k8s_stack_render`) and are
used as-is. The typed kube clients (`AppsV1Api` / `CoreV1Api`) are INJECTED so
unit tests run against `unittest.mock` with no cluster; `from_config()` builds
real ones via T7's loader (`utils.k8s_backend_config.load_k8s_client`).

Data safety (ISO soft-delete-minded): PVCs are never deleted implicitly —
only `teardown(..., delete_volumes=True)` removes them, and `deploy` never
touches an existing PVC's spec.

No DB access, no lifecycle wiring (that is Batch 2: stack_wake etc.). The
`kubernetes` package is an optional dependency and is NOT imported at module
load — API errors are recognised by their HTTP `.status` attribute.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.k8s_stack_render import (
    LABEL_STACK,
    k8s_name,
    render_stack_manifests,
)

# GT.D8.4: the W9 tenant-isolation + W24 per-service-egress NetworkPolicy code
# lives in the PAID `services.k8s_stack_netpol` (denied from the CE build).
# Load it optionally so this CE module imports cleanly; on CE `_netpol is None`
# — the isolation policy then fails closed (isolation_policy) and the egress
# policy is skipped (deploy). CE never ships a paid module.
from utils.paid_import import optional_paid

_netpol = optional_paid("services.k8s_stack_netpol")
# The isolation policy NAME is a wire contract (the worker's Go code keys off
# it), so keep a CE-safe value even when the paid module is absent.
ISOLATION_POLICY_NAME = getattr(_netpol, "POLICY_NAME", "mod-tenant-isolation")

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409

# W8: labels stamped on a namespace the API creates for a tenant's stacks.
NS_LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
NS_MANAGED_BY_VALUE = "mod"
NS_LABEL_TENANT = "mod.techlabs/tenant-id"

# W12: stamped on a Deployment by the worker's K8s self-fence (Go
# `K8sFencedReplicasAnnotation`, internal/executor/k8s_stackfence.go) with the
# pre-fence replica count. Recovery is MANAGER-driven, exactly as on Docker
# (heartbeat/fence.go `noteFenceRecovered`: the worker never restarts what it
# fenced — the manager may already have re-placed it, and a worker-side restore
# is the two-live-copies state fencing exists to prevent). So nothing reads this
# back to restore scale; any manager placement decision (scale/wake/sleep or a
# re-deploy) supersedes it and clears it, so a stale record can't mislead an
# operator into "restoring" a stack the manager has since moved or slept.
FENCED_REPLICAS_ANNOTATION = "mod.techlabs/fenced-replicas"


def _api_status(exc: BaseException) -> Optional[int]:
    """HTTP status of a kubernetes `ApiException` (duck-typed; the optional
    `kubernetes` dep need not be importable for this module to load)."""
    status = getattr(exc, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _meta_name(manifest: Dict[str, Any]) -> str:
    return manifest["metadata"]["name"]


def _get(obj: Any, *path: str) -> Any:
    """Walk attribute-or-key `path` on a kube model object or a plain dict."""
    cur = obj
    for key in path:
        if cur is None:
            return None
        cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
    return cur


def _clearing_fence_record(dep: Dict[str, Any]) -> Dict[str, Any]:
    """Patch body for re-deploying an existing Deployment: the rendered
    manifest plus a `null` for the fence annotation (merge/strategic-merge
    patch semantics delete the key). The manifest itself is not mutated — it
    is also the create body and is returned to the caller."""
    meta = dict(dep.get("metadata") or {})
    ann = dict(meta.get("annotations") or {})
    ann[FENCED_REPLICAS_ANNOTATION] = None
    meta["annotations"] = ann
    return {**dep, "metadata": meta}


def _scale_preserving(dep: Dict[str, Any]) -> Dict[str, Any]:
    """W28 patch body for re-deploying an existing Deployment WITHOUT deciding
    its scale: the rendered manifest minus `spec.replicas`. A strategic-merge
    patch leaves an absent field untouched, so a stack that is asleep
    (replicas=0) or worker-fenced stays exactly as it is — a spec update must
    never silently wake a GPU stack. The fence annotation is deliberately NOT
    cleared: no scale decision was made, so the record still describes
    reality. Does not mutate `dep` (it is also the create body)."""
    spec = {k: v for k, v in (dep.get("spec") or {}).items() if k != "replicas"}
    return {**dep, "spec": spec}


def _items(listing: Any) -> List[Any]:
    return list(_get(listing, "items") or [])


class K8sStackBackend:
    """Deployment + Service + PVC backend for persistent stacks in one namespace
    (namespace-per-tenant, design.md Seam 2)."""

    def __init__(self, apps_api: Any, core_api: Any, namespace: str, *,
                 render_kwargs: Optional[Dict[str, Any]] = None,
                 manage_namespace: bool = False,
                 namespace_labels: Optional[Dict[str, str]] = None,
                 networking_api: Any = None,
                 control_namespace: Optional[str] = None,
                 task_namespace: Optional[str] = None,
                 custom_objects_api: Any = None):
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace is required")
        self.apps = apps_api
        self.core = core_api
        self.namespace = namespace
        # Passed through to render_stack_manifests (labels, storage class, ...).
        self.render_kwargs = dict(render_kwargs or {})
        # W8: when True, `deploy` ensures the namespace exists (per-tenant
        # chart mode, API holds ClusterRole namespaces get/create). False when
        # the namespace is operator-managed (MOD_K8S_STACK_NAMESPACE override /
        # single-ns chart mode, where the API cannot read namespaces).
        self.manage_namespace = bool(manage_namespace)
        self.namespace_labels = {NS_LABEL_MANAGED_BY: NS_MANAGED_BY_VALUE,
                                 **dict(namespace_labels or {})}
        # W9: NetworkingV1Api for the per-tenant isolation NetworkPolicy
        # (required when manage_namespace; see ensure_isolation_policy).
        self.networking = networking_api
        self.control_namespace = control_namespace
        self.task_namespace = task_namespace
        # GB.EGR.6: CustomObjectsApi for the companion CiliumNetworkPolicy
        # (only used when MOD_K8S_EGRESS_FQDN=cilium; see k8s_stack_netpol).
        self.custom_objects = custom_objects_api

    @classmethod
    def from_config(cls, namespace: str, *, kubeconfig_path: Optional[str] = None,
                    render_kwargs: Optional[Dict[str, Any]] = None,
                    manage_namespace: bool = False,
                    namespace_labels: Optional[Dict[str, str]] = None,
                    ) -> "K8sStackBackend":
        """Build real typed clients via T7's loader (in-cluster SA, else kubeconfig)."""
        from kubernetes import client  # optional dep

        from utils.k8s_backend_config import load_k8s_client

        api_client = load_k8s_client(kubeconfig_path=kubeconfig_path)
        return cls(client.AppsV1Api(api_client), client.CoreV1Api(api_client),
                   namespace, render_kwargs=render_kwargs,
                   manage_namespace=manage_namespace,
                   namespace_labels=namespace_labels,
                   networking_api=client.NetworkingV1Api(api_client),
                   custom_objects_api=client.CustomObjectsApi(api_client))

    # -- helpers -------------------------------------------------------------

    def _selector(self, stack_name: str) -> str:
        return f"{LABEL_STACK}={k8s_name(stack_name)}"

    def render(self, stack_name: str, compose_spec: Dict[str, Any]) -> Dict[str, Any]:
        return render_stack_manifests(compose_spec, namespace=self.namespace,
                                      stack_name=stack_name, **self.render_kwargs)

    def _list_deployments(self, stack_name: str) -> List[Any]:
        return _items(self.apps.list_namespaced_deployment(
            self.namespace, label_selector=self._selector(stack_name)))

    # -- namespace -----------------------------------------------------------

    def ensure_namespace(self) -> str:
        """Create the stack namespace if missing (W8) and (re)apply its tenant
        isolation NetworkPolicy (W9). Returns "exists", "created", or "skipped"
        (operator-managed). A 409 on create is a lost race with another deploy
        and counts as success. Never deletes."""
        if not self.manage_namespace:
            return "skipped"
        result = self._ensure_namespace_object()
        self.ensure_isolation_policy()
        return result

    def isolation_policy(self) -> Dict[str, Any]:
        """The `mod-tenant-isolation` NetworkPolicy body for this namespace."""
        if _netpol is None:
            raise RuntimeError(
                "k8s: tenant isolation NetworkPolicy module (k8s_stack_netpol) "
                "unavailable on this edition — cannot manage an API-owned "
                "namespace without its isolation policy")
        return _netpol.build_tenant_isolation_policy(
            self.namespace, self.namespace_labels.get(NS_LABEL_TENANT),
            control_namespace=(self.control_namespace
                               or _netpol.resolve_control_namespace()),
            task_namespace=(self.task_namespace
                            or _netpol.resolve_task_namespace()),
            task_pod_selector=_netpol.resolve_task_pod_selector())

    def ensure_isolation_policy(self) -> str:
        """Create-or-patch the tenant isolation NetworkPolicy (W9). Idempotent:
        applied on every managed deploy so pre-W9 namespaces converge.

        FAIL-CLOSED: any failure (no NetworkingV1Api, 403, API error) raises
        and aborts the deploy. The namespace is API-managed, so nobody else
        will fence it — a stack started without the policy is reachable by
        every pod in the cluster, incl. other tenants' task pods. A failed
        deploy is loud and retryable; a silently exposed stack is neither."""
        if self.networking is None:
            raise RuntimeError(
                f"k8s: no NetworkingV1Api — refusing to deploy into {self.namespace!r} "
                "without its tenant isolation NetworkPolicy")
        body = self.isolation_policy()
        ns = self.namespace
        try:
            self.networking.create_namespaced_network_policy(ns, body)
            outcome = "created"
        except Exception as exc:  # noqa: BLE001 — ApiException, duck-typed
            status = _api_status(exc)
            if status == _HTTP_FORBIDDEN:
                raise PermissionError(
                    f"k8s: API service account may not manage NetworkPolicies in {ns!r} "
                    "(403). Per-tenant mode needs networkpolicies get/create/patch "
                    "(chart <release>-api-stacks).") from exc
            if status != _HTTP_CONFLICT:
                raise
            try:
                self.networking.patch_namespaced_network_policy(
                    ISOLATION_POLICY_NAME, ns, body)
            except Exception as pexc:  # noqa: BLE001
                if _api_status(pexc) == _HTTP_FORBIDDEN:
                    raise PermissionError(
                        f"k8s: API service account may not patch NetworkPolicy "
                        f"{ns}/{ISOLATION_POLICY_NAME} (403).") from pexc
                raise
            outcome = "patched"
        logger.info("k8s: isolation policy %s/%s %s", ns, ISOLATION_POLICY_NAME, outcome)
        return outcome

    def _ensure_namespace_object(self) -> str:
        ns = self.namespace
        try:
            self.core.read_namespace(ns)
            return "exists"
        except Exception as exc:  # noqa: BLE001 — ApiException, duck-typed
            status = _api_status(exc)
            if status == _HTTP_FORBIDDEN:
                raise PermissionError(
                    f"k8s: API service account may not read namespace {ns!r} (403). "
                    "Per-tenant mode needs ClusterRole namespaces get/create "
                    "(chart <release>-api-stacks); or set MOD_K8S_STACK_NAMESPACE "
                    "to an operator-managed namespace.") from exc
            if status != _HTTP_NOT_FOUND:
                raise
        body = {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": ns, "labels": dict(self.namespace_labels)}}
        try:
            self.core.create_namespace(body)
        except Exception as exc:  # noqa: BLE001
            status = _api_status(exc)
            if status == _HTTP_CONFLICT:
                return "exists"
            if status == _HTTP_FORBIDDEN:
                raise PermissionError(
                    f"k8s: API service account may not create namespace {ns!r} (403). "
                    "Grant namespaces create (chart <release>-api-stacks) or "
                    "pre-create it.") from exc
            raise
        logger.info("k8s: created namespace %s labels=%s", ns, self.namespace_labels)
        return "created"

    # -- deploy --------------------------------------------------------------

    def deploy(self, stack_name: str, compose_spec: Dict[str, Any], *,
               apply_replicas: bool = False) -> Dict[str, Any]:
        """Render and create-or-update every object. Idempotent: an existing
        Deployment/Service (409) is patched; an existing PVC is left as-is
        (PVC specs are largely immutable and hold user data). Returns the
        rendered manifests plus a per-kind `applied` summary.

        W28 replica preservation: a Deployment that does not exist yet is
        created with the renderer's replicas (a first deploy comes up). An
        EXISTING Deployment keeps its live `spec.replicas` (and any W12 fence
        record) unless `apply_replicas=True` — the caller's explicit
        to-running intent — in which case the rendered replicas are applied and
        the fence record is cleared (a manager scale decision). Scale otherwise
        belongs to scale()/wake()/sleep(). `replicas_preserved` lists the
        Deployments patched without touching their scale."""
        manifests = self.render(stack_name, compose_spec)
        # Namespace-per-tenant: the first deploy into tenant-<id> must create it.
        self.ensure_namespace()
        # W24: per-service egress NetworkPolicy (x-mod.egress) BEFORE any pod
        # exists. Paid tier (GT.D8.4); CE has no netpol module, so the per-
        # service egress posture is skipped (the W9 namespace isolation policy
        # is what fail-closes above when the module is absent).
        if _netpol is not None:
            _netpol.ensure_stack_egress_policy(self, stack_name, compose_spec)
        applied: Dict[str, Dict[str, str]] = {
            "persistent_volume_claims": {}, "deployments": {}, "services": {},
        }
        ns = self.namespace

        # PVCs first so Deployment pods can bind their claims on schedule.
        for pvc in manifests["persistent_volume_claims"]:
            name = _meta_name(pvc)
            try:
                self.core.create_namespaced_persistent_volume_claim(ns, pvc)
                applied["persistent_volume_claims"][name] = "created"
            except Exception as exc:  # noqa: BLE001 — ApiException, duck-typed
                if _api_status(exc) != _HTTP_CONFLICT:
                    raise
                applied["persistent_volume_claims"][name] = "exists"

        preserved: List[str] = []
        for dep in manifests["deployments"]:
            outcome = self._create_or_patch(
                self.apps.create_namespaced_deployment,
                self.apps.patch_namespaced_deployment, dep,
                patch_body=(_clearing_fence_record(dep) if apply_replicas
                            else _scale_preserving(dep)))
            applied["deployments"][_meta_name(dep)] = outcome
            if outcome == "patched" and not apply_replicas:
                preserved.append(_meta_name(dep))

        for svc in manifests["services"]:
            applied["services"][_meta_name(svc)] = self._create_or_patch(
                self.core.create_namespaced_service,
                self.core.patch_namespaced_service, svc)

        for service_name, src, target in manifests.get("skipped_bind_mounts") or []:
            logger.warning("k8s deploy %s/%s: bind mount %s:%s not portable to k8s; skipped",
                           ns, stack_name, src, target)
        logger.info("k8s deploy %s/%s: %s (replicas preserved: %s)",
                    ns, stack_name, applied, preserved)
        return {"manifests": manifests, "applied": applied,
                "replicas_preserved": preserved}

    def _create_or_patch(self, create_fn, patch_fn, manifest: Dict[str, Any],
                         patch_body: Optional[Dict[str, Any]] = None) -> str:
        # Strategic-merge PATCH on conflict: no resourceVersion needed and it
        # preserves server-assigned fields (e.g. a Service's clusterIP) that a
        # blind PUT/replace would reject.
        try:
            create_fn(self.namespace, manifest)
            return "created"
        except Exception as exc:  # noqa: BLE001
            if _api_status(exc) != _HTTP_CONFLICT:
                raise
        patch_fn(_meta_name(manifest), self.namespace,
                 manifest if patch_body is None else patch_body)
        return "patched"

    # -- scale ---------------------------------------------------------------

    def scale(self, stack_name: str, replicas: int) -> List[str]:
        """Scale every Deployment of the stack to `replicas` (wake=1, sleep=0 —
        0 releases the pods and therefore their GPU/VRAM). Returns the names
        scaled; empty if the stack has no Deployments."""
        if replicas < 0:
            raise ValueError("replicas must be >= 0")
        deployments = self._list_deployments(stack_name)
        names = [_get(d, "metadata", "name") for d in deployments]
        for d, name in zip(deployments, names):
            self.apps.patch_namespaced_deployment_scale(
                name, self.namespace, {"spec": {"replicas": int(replicas)}})
            # W12: the manager has now decided this stack's scale, so a worker
            # self-fence record is stale. Cleared AFTER the scale succeeds: if
            # the scale raises, the record still describes reality.
            if FENCED_REPLICAS_ANNOTATION in (_get(d, "metadata", "annotations") or {}):
                self.apps.patch_namespaced_deployment(
                    name, self.namespace,
                    {"metadata": {"annotations": {FENCED_REPLICAS_ANNOTATION: None}}})
                logger.info("k8s scale %s/%s: cleared stale %s on %s",
                            self.namespace, stack_name, FENCED_REPLICAS_ANNOTATION, name)
        if not names:
            logger.warning("k8s scale %s/%s: no deployments found", self.namespace, stack_name)
        else:
            logger.info("k8s scale %s/%s -> %d: %s", self.namespace, stack_name, replicas, names)
        return names

    def wake(self, stack_name: str) -> List[str]:
        return self.scale(stack_name, 1)

    def sleep(self, stack_name: str) -> List[str]:
        return self.scale(stack_name, 0)

    # -- endpoint ------------------------------------------------------------

    def endpoint(self, stack_name: str, service_name: Optional[str] = None,
                 port: Optional[int] = None) -> str:
        """`http://<svc>.<ns>.svc.cluster.local:<port>` — the same form as
        `brand_image_stacks.resolve_stack_endpoint` (without a path). The
        Service is `k8s_name(stack_name, service_name or stack_name)`. When
        `port` is omitted it is read from the live Service's first port."""
        svc = k8s_name(stack_name, service_name or stack_name)
        if port is None:
            live = self.core.read_namespaced_service(svc, self.namespace)
            ports = _get(live, "spec", "ports") or []
            if not ports:
                raise LookupError(f"service {self.namespace}/{svc} exposes no ports")
            port = _get(ports[0], "port")
        return f"http://{svc}.{self.namespace}.svc.cluster.local:{int(port)}"

    # -- status --------------------------------------------------------------

    def status(self, stack_name: str) -> Dict[str, Any]:
        """Replica rollup for wake-readiness. `ready` is True only when every
        Deployment has ready >= desired and desired > 0."""
        per: Dict[str, Dict[str, int]] = {}
        for d in self._list_deployments(stack_name):
            desired = int(_get(d, "spec", "replicas") or 0)
            ready = int(_get(d, "status", "ready_replicas")
                        or _get(d, "status", "readyReplicas") or 0)
            per[_get(d, "metadata", "name")] = {"desired": desired, "ready": ready}
        desired_total = sum(v["desired"] for v in per.values())
        ready_total = sum(v["ready"] for v in per.values())
        return {
            "deployments": per,
            "desired": desired_total,
            "ready_replicas": ready_total,
            "ready": bool(per) and desired_total > 0
                     and all(v["ready"] >= v["desired"] for v in per.values()),
        }

    # -- teardown ------------------------------------------------------------

    def teardown(self, stack_name: str, delete_volumes: bool = False) -> Dict[str, List[str]]:
        """Delete the stack's Deployments and Services (404 tolerated). PVCs
        are deleted ONLY when `delete_volumes=True`; the default keeps data."""
        ns, sel = self.namespace, self._selector(stack_name)
        deleted: Dict[str, List[str]] = {
            "deployments": [], "services": [], "persistent_volume_claims": [],
        }
        targets = [
            ("deployments", self.apps.list_namespaced_deployment,
             self.apps.delete_namespaced_deployment),
            ("services", self.core.list_namespaced_service,
             self.core.delete_namespaced_service),
        ]
        if delete_volumes:
            targets.append(("persistent_volume_claims",
                            self.core.list_namespaced_persistent_volume_claim,
                            self.core.delete_namespaced_persistent_volume_claim))
        for kind, list_fn, delete_fn in targets:
            for obj in _items(list_fn(ns, label_selector=sel)):
                name = _get(obj, "metadata", "name")
                try:
                    delete_fn(name, ns, propagation_policy="Background")
                except Exception as exc:  # noqa: BLE001
                    if _api_status(exc) != _HTTP_NOT_FOUND:
                        raise
                deleted[kind].append(name)
        logger.info("k8s teardown %s/%s (delete_volumes=%s): %s",
                    ns, stack_name, delete_volumes, deleted)
        return deleted


def k8s_stack_backend_for(db, tenant_id: Optional[int] = None, *,
                          kubeconfig_path: Optional[str] = None,
                          render_kwargs: Optional[Dict[str, Any]] = None,
                          ) -> Optional[K8sStackBackend]:
    """The ONLY sanctioned way to obtain a live `K8sStackBackend` (W2).

    Returns None unless `k8s_backend_gate.effective_stack_backend(db,
    tenant_id)` says "k8s" (MOD_STACK_BACKEND=k8s AND the tenant holds
    Cap.K8S_BACKEND) — callers then fall through to the docker path. The
    namespace is `brand_image_stacks._k8s_namespace_for(tenant_id)` so it
    matches the Service DNS `resolve_stack_endpoint` hands consumers."""
    from services.k8s_backend_gate import effective_stack_backend

    if effective_stack_backend(db, tenant_id) != "k8s":
        return None
    import os

    # GT.D8.4: `brand_image_stacks` (brand VLM/SDXL reachability) is PAID on CE.
    # The CE single-cluster path derives the same namespace shape without it:
    # MOD_K8S_STACK_NAMESPACE override, else `tenant-<id>`.
    _brand = optional_paid("services.brand_image_stacks")
    if _brand is not None:
        _k8s_namespace_for = _brand._k8s_namespace_for
        fallback_tenant = _brand.BRAND_STACKS_TENANT_ID
    else:
        def _k8s_namespace_for(tid: Optional[int]) -> str:
            override = os.getenv("MOD_K8S_STACK_NAMESPACE", "").strip()
            if override:
                return override
            return f"tenant-{tid if tid is not None else 1}"
        fallback_tenant = 1

    # W8: with the MOD_K8S_STACK_NAMESPACE override the namespace is
    # operator-managed (single-ns chart mode: namespaced Role only, cannot read
    # namespaces) — don't touch it. Otherwise ensure tenant-<id> on deploy.
    operator_managed = bool(os.getenv("MOD_K8S_STACK_NAMESPACE", "").strip())
    effective_tenant = tenant_id if tenant_id is not None else fallback_tenant
    return K8sStackBackend.from_config(
        _k8s_namespace_for(tenant_id),
        kubeconfig_path=kubeconfig_path,
        render_kwargs=render_kwargs,
        manage_namespace=not operator_managed,
        namespace_labels=None if operator_managed
        else {NS_LABEL_TENANT: str(effective_tenant)})


__all__ = ["K8sStackBackend", "k8s_stack_backend_for"]
