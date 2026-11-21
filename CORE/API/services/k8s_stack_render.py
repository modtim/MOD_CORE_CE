"""k8s-reduction-engine — design.md Seam 2 / tasks.md T3.

Pure translation layer: a persistent stack's `compose_spec` (the exact shape
`routers/persistent_stacks/_shared.py::_compose_service_from_spec` /
`services/persistent_stack_definition_service.py::render_compose_from_services`
produce and `models.PersistentStack.compose_spec` stores) -> plain-dict
Kubernetes manifests (Deployment + Service + PersistentVolumeClaim per
service).

Hard constraints (by design):
  * NO cluster calls, no I/O, no DB session, no imports from `routers/` or
    `services/persistent_stack_definition_service.py` — every function here
    is a total function of its arguments so `K8sStackBackend` (T4) and unit
    tests can call it standalone, offline, without a cluster or a DB. This
    module only READS the shape those files establish; it does not import
    them (they are owned by another workstream and may be mid-edit).
  * Deterministic: the same input always produces the same output (stable
    key ordering, sorted iteration) so a re-render used for reconciliation
    diffs cleanly instead of spuriously.

compose_spec shape this module understands (see the files above for the
authoritative producer):

    {
      "services": {
        "<name>": {
          "image": "repo/img:tag",        # OR "build": {...} (unsupported here — raises)
          "command": "a b c" | ["a", "b"],
          "environment": {"K": "V"} | ["K=V", ...],
          "expose": ["8080", ...],        # container-only ports, no host publish
          "ports": ["9000:9000", ...],    # legacy/import-compose host:container form
          "volumes": ["data:/var/lib/x", "./bind:/y", "/abs:/z"],
          "restart": "unless-stopped",
          "deploy": {
            "replicas": 2,
            "resources": {
              "reservations": {"cpus": "1.5", "memory": "2G",
                                "devices": [{"driver": "nvidia", "count": 1,
                                             "capabilities": ["gpu"]}]},
              "limits": {"cpus": "1.5", "memory": "2G"},
            },
          },
          "x-mod": {
            "accelerator": {"required": True, "min_vram_gb": 48.0,
                             "count": 1, "kind": "cuda"},
            "reachable": True,
            "egress": {"mode": "none"},
          },
        },
      },
      "volumes": {"data": {}},
    }

Only NAMED volumes (source not starting with '.', '/', '~') become PVCs —
host/relative bind mounts have no portable Kubernetes equivalent and are
skipped (documented limitation, not a silent bug: `render_stack_manifests`
returns which ones it skipped so a caller can warn).
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

# --- constants ---------------------------------------------------------------

API_VERSION_APPS = "apps/v1"
API_VERSION_CORE = "v1"

LABEL_STACK = "mod.techlabs/stack"
LABEL_SERVICE = "mod.techlabs/service"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "mod-k8s-stack-render"

# k8s-reduction-engine (W2): the Go K8s observer reads the stack's HA class
# from this label (Deployment + pod template). Docker stacks carry the same
# value as the compose service label `COMPOSE_HA_CLASS_LABEL`, stamped by
# `routers/persistent_stacks/_shared.py` from `PersistentStack.ha_class`
# (column default "external" — the one class the manager never relocates).
LABEL_HA_CLASS = "mod.techlabs/ha-class"
COMPOSE_HA_CLASS_LABEL = "com.example.ha-class"
DEFAULT_HA_CLASS = "external"
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$")

ANNOTATION_RESTART_POLICY = "mod.techlabs/compose-restart-policy"
ANNOTATION_GPU_KIND = "mod.techlabs/gpu-kind"

DEFAULT_PVC_STORAGE = "10Gi"
DEFAULT_ACCESS_MODES = ("ReadWriteOnce",)

_GPU_DEVICE_CAPABILITY = "gpu"
_NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"

_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")
_NAME_EDGE_RE = re.compile(r"^-+|-+$")


class ComposeRenderError(ValueError):
    """Raised when `compose_spec` cannot be rendered to a Kubernetes manifest
    (e.g. a `build`-only service with no runnable image reference)."""


# --- naming / labels ----------------------------------------------------------


def k8s_name(*parts: str) -> str:
    """A valid (RFC 1123 DNS subdomain / label) Kubernetes object name built
    from `parts` joined by `-`. Lower-cased, invalid characters replaced with
    `-`, leading/trailing `-` stripped, truncated to 63 chars (the label
    limit, which every object name here must satisfy since these names are
    also used as Service DNS labels)."""
    joined = "-".join(str(p) for p in parts if p not in (None, ""))
    lowered = joined.lower().replace("_", "-").replace(".", "-").replace(" ", "-")
    sanitized = _NAME_SANITIZE_RE.sub("-", lowered)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    sanitized = _NAME_EDGE_RE.sub("", sanitized)
    if not sanitized:
        sanitized = "svc"
    return sanitized[:63].rstrip("-") or "svc"


def k8s_project_key(project_name: Optional[str]) -> str:
    """The key the Go K8s observer reports a stack under: `k8s_name(stack)`
    (lower-cased, `_`/`.`/space -> `-`, RFC 1123-sanitised, <=63 chars).

    Manager-side matching of k8s observations to `PersistentStack` rows must
    compare `k8s_project_key(row_project)` against the observed key — the raw
    docker compose project name (e.g. `mod_brand_vlm`) never equals the
    sanitised k8s form (`mod-brand-vlm`). Idempotent:
    `k8s_project_key(k8s_project_key(x)) == k8s_project_key(x)`."""
    return k8s_name(project_name or "")


def resolve_ha_class(svc: Optional[Dict[str, Any]] = None,
                     ha_class: Optional[str] = None) -> str:
    """HA class for a rendered service: explicit `ha_class` (the stack row's
    column) wins, else the compose label the docker path stamps
    (`COMPOSE_HA_CLASS_LABEL`, dict or `k=v` list form), else a bare
    `ha_class` key on the service, else `DEFAULT_HA_CLASS` ("external" —
    same as the docker/DB default)."""
    if ha_class:
        return str(ha_class)
    svc = svc or {}
    raw = svc.get("labels")
    if isinstance(raw, dict) and raw.get(COMPOSE_HA_CLASS_LABEL):
        return str(raw[COMPOSE_HA_CLASS_LABEL])
    if isinstance(raw, list):
        prefix = COMPOSE_HA_CLASS_LABEL + "="
        for item in raw:
            if isinstance(item, str) and item.startswith(prefix) and item[len(prefix):]:
                return item[len(prefix):]
    if svc.get("ha_class"):
        return str(svc["ha_class"])
    return DEFAULT_HA_CLASS


def _std_labels(stack_name: str, service_name: Optional[str] = None,
                 extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    labels = {
        LABEL_MANAGED_BY: MANAGED_BY_VALUE,
        LABEL_STACK: k8s_name(stack_name),
    }
    if service_name is not None:
        labels[LABEL_SERVICE] = k8s_name(service_name)
    if extra:
        labels.update(extra)
    return labels


# --- env -----------------------------------------------------------------------


def _parse_env(svc: Dict[str, Any]) -> List[Dict[str, str]]:
    """compose `environment` (dict OR `["K=V", ...]` list) -> a sorted list of
    Kubernetes `EnvVar` dicts (`{"name": ..., "value": ...}`). Sorted by name
    for determinism — dict iteration order is insertion order in Python but
    we do not want manifest identity to depend on how the compose_spec dict
    happened to be built."""
    env = svc.get("environment")
    pairs: Dict[str, str] = {}
    if isinstance(env, dict):
        pairs = {str(k): "" if v is None else str(v) for k, v in env.items()}
    elif isinstance(env, list):
        for entry in env:
            if isinstance(entry, str) and "=" in entry:
                k, v = entry.split("=", 1)
                pairs[k] = v
    return [{"name": k, "value": pairs[k]} for k in sorted(pairs)]


# --- ports -----------------------------------------------------------------------


def _parse_port_token(raw: Any) -> Optional[Tuple[int, str]]:
    """One compose port/expose entry -> (container_port, protocol). Accepts a
    bare int, `"8080"`, `"8080/udp"`, or a published `"host:container"` /
    `"host:container/proto"` form (container side = the LAST colon segment,
    matching `persistent_stack_definition_service._service_view`'s existing
    convention). Returns None for anything unparseable rather than raising —
    a malformed port entry should not block rendering the rest of the stack."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    protocol = "TCP"
    if "/" in s:
        s, proto = s.rsplit("/", 1)
        if proto.strip().upper() in ("TCP", "UDP", "SCTP"):
            protocol = proto.strip().upper()
    port_str = s.split(":")[-1]
    try:
        port = int(port_str)
    except (TypeError, ValueError):
        return None
    if not (0 < port < 65536):
        return None
    return port, protocol


def _container_ports(svc: Dict[str, Any]) -> List[Tuple[int, str]]:
    """Merge `expose` + `ports` into a de-duplicated, order-preserving list of
    (port, protocol)."""
    seen: Dict[Tuple[int, str], None] = {}
    for raw in list(svc.get("expose") or []) + list(svc.get("ports") or []):
        parsed = _parse_port_token(raw)
        if parsed is not None:
            seen.setdefault(parsed, None)
    return list(seen.keys())


# --- resources (cpu / memory / gpu) --------------------------------------------


def _memory_to_k8s_quantity(value: Any) -> Optional[str]:
    """compose memory string/number -> a Kubernetes memory quantity.

    Mirrors `routers/persistent_stacks/_shared.py::_parse_compose_memory`'s
    own convention for THIS codebase (1024-based b/k/m/g suffixes; a bare
    number is bytes) so a reservation captured there and one rendered here
    describe the same physical amount — re-implemented locally rather than
    imported so this module has zero dependency on the routers layer.
    Returns None (no request emitted) rather than "0" for unparseable input,
    since "reserve nothing" and "unspecified" are different things a caller
    may want to distinguish."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num_bytes = float(value)
        else:
            s = str(value).strip().lower()
            if not s:
                return None
            mult = 1
            for suffix, m in (("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024),
                              ("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024),
                              ("b", 1)):
                if s.endswith(suffix):
                    mult = m
                    s = s[: -len(suffix)].strip()
                    break
            num_bytes = float(s) * mult
    except (TypeError, ValueError):
        return None
    if num_bytes <= 0:
        return None
    for divisor, suffix in ((1024 ** 3, "Gi"), (1024 ** 2, "Mi"), (1024, "Ki")):
        if num_bytes % divisor == 0:
            return f"{int(num_bytes // divisor)}{suffix}"
    return str(int(num_bytes))


def _cpu_quantity(value: Any) -> Optional[str]:
    """compose `cpus` ("1.5", 1.5, ...) -> a Kubernetes cpu quantity string.
    Kubernetes accepts a plain decimal ("1.5") directly as a cpu quantity, so
    this is pass-through validation rather than unit conversion."""
    if value is None:
        return None
    try:
        cpus = float(value)
    except (TypeError, ValueError):
        return None
    if cpus <= 0:
        return None
    return f"{cpus:g}"


def _gpu_count(svc: Dict[str, Any]) -> Tuple[int, Optional[str]]:
    """Number of GPUs a service needs, and the declared accelerator `kind`
    (e.g. "cuda"), if any.

    Prefers `x-mod.accelerator` (this codebase's VRAM-aware GPU declaration —
    see `persistent_stack_definition_service._parse_accelerator_requirements`)
    since it is the one place `count` and `kind` are both authoritative.
    Falls back to the plain-compose `deploy.resources.reservations.devices`
    form (`{"driver": "nvidia", "count": N, "capabilities": ["gpu"]}`) for a
    stack whose compose_spec was hand-authored / imported rather than built
    through the service builder."""
    xmod = svc.get("x-mod")
    accel = xmod.get("accelerator") if isinstance(xmod, dict) else None
    if isinstance(accel, dict) and (accel.get("required", True) or accel.get("count")):
        count = accel.get("count", 1)
        try:
            count = max(0, int(count))
        except (TypeError, ValueError):
            count = 1 if accel.get("required", True) else 0
        kind = accel.get("kind")
        kind = str(kind).strip() if kind else None
        if count > 0:
            return count, kind

    deploy = svc.get("deploy") or {}
    reservations = ((deploy.get("resources") or {}).get("reservations") or {})
    devices = reservations.get("devices")
    total = 0
    if isinstance(devices, list):
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            caps = dev.get("capabilities") or []
            if isinstance(caps, list) and any(
                str(c).strip().lower() == _GPU_DEVICE_CAPABILITY for c in caps
            ):
                try:
                    total += max(1, int(dev.get("count", 1)))
                except (TypeError, ValueError):
                    total += 1
    return total, None


def _resource_requirements(svc: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """compose `deploy.resources.{reservations,limits}` + GPU declaration ->
    a Kubernetes `resources` block (`{"requests": {...}, "limits": {...}}`,
    with empty sub-dicts omitted entirely).

    GPU is an "extended resource": Kubernetes requires request == limit for
    it (no overcommit of whole devices), so the same count is written to
    both regardless of what compose declared as a limit."""
    deploy = svc.get("deploy") or {}
    resources = deploy.get("resources") or {}
    reservations = resources.get("reservations") or {}
    limits_in = resources.get("limits") or {}

    requests: Dict[str, str] = {}
    limits: Dict[str, str] = {}

    cpu_req = _cpu_quantity(reservations.get("cpus"))
    if cpu_req:
        requests["cpu"] = cpu_req
    mem_req = _memory_to_k8s_quantity(reservations.get("memory"))
    if mem_req:
        requests["memory"] = mem_req

    cpu_lim = _cpu_quantity(limits_in.get("cpus"))
    if cpu_lim:
        limits["cpu"] = cpu_lim
    mem_lim = _memory_to_k8s_quantity(limits_in.get("memory"))
    if mem_lim:
        limits["memory"] = mem_lim

    gpu_count, _kind = _gpu_count(svc)
    if gpu_count > 0:
        requests[_NVIDIA_GPU_RESOURCE] = str(gpu_count)
        limits[_NVIDIA_GPU_RESOURCE] = str(gpu_count)

    out: Dict[str, Dict[str, str]] = {}
    if requests:
        out["requests"] = requests
    if limits:
        out["limits"] = limits
    return out


# --- volumes -----------------------------------------------------------------


def _is_named_volume_source(src: str) -> bool:
    return bool(src) and not src.startswith((".", "/", "~"))


def _parse_volume_mount(raw: Any) -> Optional[Tuple[str, str, bool]]:
    """One compose `volumes` entry -> (source, target, read_only), or None if
    it is not a NAMED volume (bind mounts have no portable K8s PVC
    equivalent — see module docstring)."""
    if not isinstance(raw, str) or ":" not in raw:
        return None
    parts = raw.split(":")
    src, target = parts[0], parts[1]
    if not _is_named_volume_source(src):
        return None
    read_only = len(parts) > 2 and "ro" in parts[2].split(",")
    return src, target, read_only


def _named_volume_mounts(svc: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
    out = []
    for raw in svc.get("volumes") or []:
        parsed = _parse_volume_mount(raw)
        if parsed is not None:
            out.append(parsed)
    return out


# --- command -------------------------------------------------------------------


def _parse_command(svc: Dict[str, Any]) -> Optional[List[str]]:
    command = svc.get("command")
    if command is None:
        return None
    if isinstance(command, list):
        return [str(c) for c in command]
    if isinstance(command, str):
        stripped = command.strip()
        return shlex.split(stripped) if stripped else None
    return None


# --- per-service manifests -----------------------------------------------------


def render_deployment(stack_name: str, service_name: str, svc: Dict[str, Any],
                       namespace: str, *,
                       labels: Optional[Dict[str, str]] = None,
                       ha_class: Optional[str] = None) -> Dict[str, Any]:
    """One compose service -> a Kubernetes Deployment manifest (dict).

    Deployment + pod-template labels carry `LABEL_HA_CLASS` (see
    `resolve_ha_class`) for the Go K8s observer.

    Raises `ComposeRenderError` if the service has no runnable `image` (a
    `build:`-only service needs an out-of-band image build this pure
    renderer does not perform).

    `restartPolicy` is always `"Always"` in the pod template — Kubernetes'
    API rejects any other value for a Deployment-owned pod — so compose's
    `restart` value (which CAN be `"no"`/`"on-failure"`) is instead preserved
    losslessly as the `ANNOTATION_RESTART_POLICY` annotation for a consumer
    (or a future backend choosing Deployment vs. Job) to read.
    """
    if not svc.get("image"):
        if svc.get("build"):
            raise ComposeRenderError(
                f"service {service_name!r}: 'build' is not renderable to a Kubernetes "
                "manifest without an out-of-band image build; resolve to an 'image' "
                "reference before calling render_deployment()."
            )
        raise ComposeRenderError(f"service {service_name!r}: compose service has no 'image'")

    name = k8s_name(stack_name, service_name)
    pod_labels = _std_labels(stack_name, service_name, labels)
    # Label VALUES may contain `_` — keep the class verbatim ("cache_backed"),
    # exactly as the docker path/DB store it; fall back to the default only if
    # it isn't a legal label value.
    ha_value = resolve_ha_class(svc, ha_class)
    if not _LABEL_VALUE_RE.match(ha_value):
        ha_value = DEFAULT_HA_CLASS
    pod_labels[LABEL_HA_CLASS] = ha_value
    selector_labels = {LABEL_STACK: k8s_name(stack_name), LABEL_SERVICE: k8s_name(service_name)}

    deploy = svc.get("deploy") or {}
    try:
        replicas = int(deploy.get("replicas", 1))
    except (TypeError, ValueError):
        replicas = 1
    replicas = max(0, replicas)

    container: Dict[str, Any] = {
        "name": k8s_name(service_name),
        "image": svc["image"],
    }
    command = _parse_command(svc)
    if command:
        container["command"] = command
    env = _parse_env(svc)
    if env:
        container["env"] = env
    ports = _container_ports(svc)
    if ports:
        container["ports"] = [
            {"containerPort": port, "protocol": proto} for port, proto in ports
        ]
    resources = _resource_requirements(svc)
    if resources:
        container["resources"] = resources

    volume_mounts = []
    pod_volumes = []
    for src, target, read_only in _named_volume_mounts(svc):
        vol_name = k8s_name(src)
        volume_mounts.append({
            "name": vol_name,
            "mountPath": target,
            **({"readOnly": True} if read_only else {}),
        })
        pod_volumes.append({
            "name": vol_name,
            "persistentVolumeClaim": {"claimName": k8s_name(stack_name, src)},
        })
    if volume_mounts:
        container["volumeMounts"] = volume_mounts

    annotations = {ANNOTATION_RESTART_POLICY: str(svc.get("restart") or "unless-stopped")}
    gpu_count, gpu_kind = _gpu_count(svc)
    if gpu_count > 0 and gpu_kind:
        annotations[ANNOTATION_GPU_KIND] = gpu_kind

    pod_spec: Dict[str, Any] = {
        "restartPolicy": "Always",
        "containers": [container],
    }
    if pod_volumes:
        pod_spec["volumes"] = pod_volumes

    return {
        "apiVersion": API_VERSION_APPS,
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": pod_labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector_labels},
            "template": {
                "metadata": {"labels": pod_labels, "annotations": annotations},
                "spec": pod_spec,
            },
        },
    }


def render_service(stack_name: str, service_name: str, svc: Dict[str, Any],
                    namespace: str, *,
                    labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One compose service -> a Kubernetes (ClusterIP) Service manifest.

    Emitted for EVERY service regardless of declared ports — compose gives
    every service DNS reachability on the project network by default, and
    `K8sStackBackend`'s reachability model (design.md Seam 2) is exactly this
    Service's DNS name: `http://<name>.<namespace>.svc.cluster.local:<port>`.
    A service that declares no ports gets a Service with an empty port list.
    """
    name = k8s_name(stack_name, service_name)
    svc_labels = _std_labels(stack_name, service_name, labels)
    selector_labels = {LABEL_STACK: k8s_name(stack_name), LABEL_SERVICE: k8s_name(service_name)}
    ports = _container_ports(svc)
    return {
        "apiVersion": API_VERSION_CORE,
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": svc_labels},
        "spec": {
            "type": "ClusterIP",
            "selector": selector_labels,
            "ports": [
                {
                    "name": f"port-{port}-{proto.lower()}",
                    "port": port,
                    "targetPort": port,
                    "protocol": proto,
                }
                for port, proto in ports
            ],
        },
    }


def render_pvc(stack_name: str, volume_name: str, namespace: str, *,
               labels: Optional[Dict[str, str]] = None,
               storage: str = DEFAULT_PVC_STORAGE,
               access_modes: Tuple[str, ...] = DEFAULT_ACCESS_MODES,
               storage_class_name: Optional[str] = None) -> Dict[str, Any]:
    """One compose NAMED volume -> a Kubernetes PersistentVolumeClaim.

    `storage`/`access_modes`/`storage_class_name` are the only tunables — a
    compose named-volume declaration (`volumes: {data: {}}`) carries no size
    or storage-class hint, so a caller (or a later size-policy layer) supplies
    them; a sane default (10Gi, ReadWriteOnce) is used otherwise."""
    pvc_labels = _std_labels(stack_name, extra=labels)
    spec: Dict[str, Any] = {
        "accessModes": list(access_modes),
        "resources": {"requests": {"storage": storage}},
    }
    if storage_class_name:
        spec["storageClassName"] = storage_class_name
    return {
        "apiVersion": API_VERSION_CORE,
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": k8s_name(stack_name, volume_name),
            "namespace": namespace,
            "labels": pvc_labels,
        },
        "spec": spec,
    }


# --- ServiceSpec (image-only) -> compose_spec convenience ----------------------


def service_spec_to_compose_service(spec: Dict[str, Any]) -> Dict[str, Any]:
    """A `service_builder_schemas.ServiceSpec`-shaped dict (image-sourced
    only) -> one compose service dict, mirroring
    `routers/persistent_stacks/_shared.py::_compose_service_from_spec`'s
    output shape for the parts that don't need DB-backed source resolution
    (image/env/ports/volumes/gpu/replicas/cpu/mem/command).

    Only `source.kind == "image"` is supported here — `git`/`node`/`script`/
    `site` sources need the existing DB-backed resolvers
    (`_resolve_service_fragment` in the routers layer) to turn into an image
    reference, which is deliberately outside this pure module. Render those
    through `services.persistent_stack_definition_service.render_compose_from_services`
    (or `/assemble`) first, then pass the resulting `compose_spec` to
    `render_stack_manifests` instead.
    """
    source = spec.get("source") or {}
    if source.get("kind") != "image":
        raise ComposeRenderError(
            f"service {spec.get('name')!r}: source.kind {source.get('kind')!r} needs "
            "DB-backed resolution (see render_compose_from_services) before it can be "
            "rendered by this pure module."
        )
    if not source.get("image"):
        raise ComposeRenderError(f"service {spec.get('name')!r}: source.image is required")

    svc: Dict[str, Any] = {"image": source["image"], "restart": "unless-stopped"}
    if spec.get("command"):
        svc["command"] = spec["command"]
    if spec.get("env"):
        svc["environment"] = dict(spec["env"])
    if spec.get("ports"):
        svc["expose"] = [str(p) for p in spec["ports"]]
    if spec.get("volumes"):
        svc["volumes"] = list(spec["volumes"])

    deploy: Dict[str, Any] = {}
    reservations: Dict[str, Any] = {}
    limits: Dict[str, Any] = {}
    cpu = spec.get("cpu") or 0
    if cpu > 0:
        reservations["cpus"] = str(cpu)
        limits["cpus"] = str(cpu)
    mem_gb = spec.get("mem_gb") or 0
    if mem_gb > 0:
        reservations["memory"] = f"{mem_gb}G"
        limits["memory"] = f"{mem_gb}G"
    replicas = spec.get("replicas", 1)
    if replicas and replicas != 1:
        deploy["replicas"] = replicas
    if reservations:
        deploy.setdefault("resources", {})["reservations"] = reservations
    if limits:
        deploy.setdefault("resources", {})["limits"] = limits
    if deploy:
        svc["deploy"] = deploy

    gpu = spec.get("gpu") or {}
    xmod: Dict[str, Any] = {}
    vram_gb = gpu.get("vram_gb", 0) if isinstance(gpu, dict) else 0
    if vram_gb and vram_gb > 0:
        xmod["accelerator"] = {"required": True, "min_vram_gb": float(vram_gb),
                               "count": 1, "kind": "cuda"}
    if spec.get("expose"):
        xmod["reachable"] = True
    if xmod:
        svc["x-mod"] = xmod
    return svc


def compose_spec_from_service_specs(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A list of image-sourced `ServiceSpec`-shaped dicts -> a full
    `compose_spec` (services + declared named volumes), via
    `service_spec_to_compose_service`. See that function's docstring for the
    image-only-source limitation."""
    rendered: Dict[str, Any] = {}
    declared_volumes: set = set()
    for spec in services:
        name = spec["name"]
        if name in rendered:
            raise ComposeRenderError(f"duplicate service name {name!r}")
        rendered[name] = service_spec_to_compose_service(spec)
        for raw in rendered[name].get("volumes") or []:
            parsed = _parse_volume_mount(raw)
            if parsed is not None:
                declared_volumes.add(parsed[0])
    compose_spec: Dict[str, Any] = {"services": rendered}
    if declared_volumes:
        compose_spec["volumes"] = {v: {} for v in sorted(declared_volumes)}
    return compose_spec


# --- top-level stack render -----------------------------------------------------


def render_stack_manifests(compose_spec: Dict[str, Any], *, namespace: str,
                            stack_name: str,
                            labels: Optional[Dict[str, str]] = None,
                            default_pvc_storage: str = DEFAULT_PVC_STORAGE,
                            storage_class_name: Optional[str] = None,
                            pvc_storage_overrides: Optional[Dict[str, str]] = None,
                            ha_class: Optional[str] = None,
                            ) -> Dict[str, Any]:
    """The full translation: `compose_spec` -> every manifest needed to run
    the stack in Kubernetes namespace `namespace` (namespace-per-tenant,
    design.md Seam 2).

    Returns:
        {
          "namespace": namespace,
          "deployments": [<Deployment>, ...],
          "services": [<Service>, ...],
          "persistent_volume_claims": [<PVC>, ...],
          "skipped_bind_mounts": [(service_name, source, target), ...],
        }

    One Deployment + one Service per compose service (sorted by name for
    deterministic output); one PVC per distinct NAMED volume actually
    mounted by some service (services sharing a volume name share the PVC,
    same as compose's own named-volume semantics). Host/relative bind mounts
    are not renderable to a portable PVC and are reported in
    `skipped_bind_mounts` rather than silently dropped.

    Pure: no cluster calls, no I/O. Raises `ComposeRenderError` if any
    service is unrenderable (see `render_deployment`).
    """
    if not namespace or not isinstance(namespace, str):
        raise ValueError("namespace is required")
    services = (compose_spec or {}).get("services") or {}
    if not isinstance(services, dict):
        raise ComposeRenderError("compose_spec.services must be a mapping")

    deployments: List[Dict[str, Any]] = []
    k8s_services: List[Dict[str, Any]] = []
    pvcs: List[Dict[str, Any]] = []
    seen_volumes: set = set()
    skipped_bind_mounts: List[Tuple[str, str, str]] = []
    overrides = pvc_storage_overrides or {}

    for service_name in sorted(services):
        svc = services[service_name]
        if not isinstance(svc, dict):
            continue
        deployments.append(
            render_deployment(stack_name, service_name, svc, namespace, labels=labels,
                              ha_class=ha_class)
        )
        k8s_services.append(
            render_service(stack_name, service_name, svc, namespace, labels=labels)
        )
        for raw in svc.get("volumes") or []:
            if not isinstance(raw, str) or ":" not in raw:
                continue
            src, target = raw.split(":")[0], raw.split(":")[1]
            if not _is_named_volume_source(src):
                skipped_bind_mounts.append((service_name, src, target))
                continue
            if src in seen_volumes:
                continue
            seen_volumes.add(src)
            pvcs.append(render_pvc(
                stack_name, src, namespace, labels=labels,
                storage=overrides.get(src, default_pvc_storage),
                storage_class_name=storage_class_name,
            ))

    return {
        "namespace": namespace,
        "deployments": deployments,
        "services": k8s_services,
        "persistent_volume_claims": sorted(pvcs, key=lambda p: p["metadata"]["name"]),
        "skipped_bind_mounts": skipped_bind_mounts,
    }
