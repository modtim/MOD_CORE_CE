"""stack-wizard-compose-import — bring a user's own docker-compose into the
stack wizard as a managed MOD stack.

A MOD stack IS a docker-compose project: ``PersistentStack.compose_spec`` is a
JSONB compose project the worker writes to ``docker-compose.yml`` and runs with
``docker compose -p <name>``. The stack wizard already GENERATES that shape via
its compose->test->critique loop, so importing one is the reverse direction:

  1. parse a user-supplied compose with a SAFE loader (never ``yaml.load``);
  2. normalize it into the SAME ``compose_spec`` shape the wizard emits
     (services / images / ports / volumes / env), rejecting constructs a managed
     stack cannot honor with a clear message rather than silently dropping them;
  3. infer the MOD ``x-mod`` metadata a stock compose lacks (accelerator from a
     GPU device reservation or image heuristic, readiness port from published
     ports, an obvious egress default) and flag the rest for the user to confirm;
  4. write the confirmed values as ``x-mod`` fields and hand the annotated spec
     to the EXISTING validate + create path
     (``routers/persistent_stacks._validate_compose`` + the ``create_stack``
     internals) so it deploys / wakes / probes like any other stack.

This module is deliberately dependency-light (``yaml`` + stdlib only): it never
imports ``models`` / ``schemas`` / ``fastapi``. The validate and create steps are
INJECTED into :func:`finalize_import`, so the whole flow is unit-testable without
a database or the running app, and the router stays the single owner of HTTP
status codes and persistence.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional, Tuple

import difflib

import yaml

# Mirror the enums the persistent_stacks router already enforces so the import
# path prompts for exactly the same values the validator/model accept. These are
# the source of truth in routers/persistent_stacks.py (_EGRESS_MODES) and
# schemas.PersistentStackCreate (ha_class / storage_class Literals); duplicated
# here only to keep this module import-light (no fastapi/schemas dependency).
EGRESS_MODES: Tuple[str, ...] = ("none", "api", "hosts")
HA_CLASSES: Tuple[str, ...] = ("stateless", "cache_backed", "snapshot_restore", "external")
STORAGE_CLASSES: Tuple[str, ...] = ("s3_native", "local_fs")

# Compose constructs a MANAGED stack cannot honor. Rejected with a clear reason
# (D1: "reject unsupported constructs ... rather than silently dropping them").
_REJECTED_TOP_LEVEL = {
    "include": "compose `include` pulls in external files the wizard cannot resolve; inline the services instead",
    "configs": "compose top-level `configs` is not supported; put config into env or an x-mod.secrets reference",
    "secrets": "compose top-level `secrets` is not supported; MOD manages secrets via x-mod.secrets (Vault)",
}
_REJECTED_SERVICE_KEYS = {
    "build": "service {name!r} uses `build:` — a managed stack needs a pre-built `image:` (no build context is uploaded)",
    "extends": "service {name!r} uses `extends:` which references another file the wizard cannot resolve; inline it",
}

# Compose-spec service keys we recognize. Anything else on a service (e.g. a
# hand-written `env:` for `environment:`) is silently dropped by the normalizer
# but rejected outright by `docker compose up`, so the stack would fail at
# deploy — surface it at import instead. Keys starting with `x-` are Compose
# extensions and always allowed.
_COMPOSE_SERVICE_KEYS: frozenset = frozenset({
    "annotations", "attach", "blkio_config", "build", "cap_add",
    "cap_drop", "cgroup", "cgroup_parent", "command", "configs",
    "container_name", "cpu_count", "cpu_percent", "cpu_period",
    "cpu_quota", "cpu_rt_period", "cpu_rt_runtime", "cpu_shares", "cpus",
    "cpuset", "credential_spec", "depends_on", "deploy", "develop",
    "device_cgroup_rules", "devices", "dns", "dns_opt", "dns_search",
    "domainname", "entrypoint", "env_file", "environment", "expose",
    "external_links", "extra_hosts", "gpus", "group_add", "healthcheck",
    "hostname", "image", "init", "ipc", "isolation", "label_file",
    "labels", "links", "logging", "mac_address", "mem_limit",
    "mem_reservation", "mem_swappiness", "memswap_limit", "models",
    "network_mode", "networks", "oom_kill_disable", "oom_score_adj", "pid",
    "pids_limit", "platform", "ports", "post_start", "pre_stop",
    "privileged", "profiles", "provider", "pull_policy", "read_only",
    "restart", "runtime", "scale", "secrets", "security_opt", "shm_size",
    "stdin_open", "stop_grace_period", "stop_signal", "storage_opt",
    "sysctls", "tmpfs", "tty", "ulimits", "use_api_socket", "user",
    "userns_mode", "uts", "volumes", "volumes_from", "working_dir"
})
# Hand-tuned hints for the near misses people actually type; difflib alone
# picks the wrong neighbour for some of them (env -> env_file, envs -> dns).
_DID_YOU_MEAN = {
    "env": "environment",
    "envs": "environment",
    "port": "ports",
    "volume": "volumes",
    "volumen": "volumes",
    "enviroment": "environment",
    "enviornment": "environment",
    "dependes_on": "depends_on",
    "health_check": "healthcheck",
    "workdir": "working_dir",
    "wd": "working_dir",
}

# Image-name tokens that strongly imply a GPU workload. Used only to SUGGEST an
# accelerator (min_vram_gb is still prompted) — never to silently place a stack.
_GPU_IMAGE_TOKENS = (
    "cuda", "nvidia", "nvcr.io", "vllm", "tensorrt", "triton", "/nim",
    "pytorch", "tensorflow", "rapids", "deepspeed", "ollama",
)
# Images that overwhelmingly run with NO outbound need — used to SUGGEST egress
# `none` (the user still confirms).
_NO_EGRESS_IMAGE_TOKENS = (
    "postgres", "redis", "valkey", "mysql", "mariadb", "mongo", "memcached",
    "qdrant", "minio", "elasticsearch", "clickhouse", "rabbitmq",
)

# gw-stack-networking GW.1 — host ports and stack subnets are MOD-owned. A
# `ports:` entry always implies a host publish in compose (even the bare
# `"3000"` short form asks Docker for an ephemeral host port), and two stacks
# — or two copies of one — publishing the same host port collide at `compose
# up`. Import converts every `ports:` entry into a container-side-only
# `x-mod.ports` record and strips the key; a host publish is kept ONLY via an
# explicit, validated `x-mod.host_ports` opt-in (see `_validate_host_ports`).
_VALID_HOST_PORT_PROTOCOLS: Tuple[str, ...] = ("tcp", "udp")
# HTTP-looking container ports, used only to SUGGEST which service is the
# front door (`x-mod.ingress`) — never to silently expose anything.
_HTTP_LIKE_PORTS: Tuple[int, ...] = (80, 443, 3000, 5000, 8000, 8080, 8443)


class ComposeImportError(Exception):
    """A user-fixable problem with an imported compose (bad YAML, wrong shape, or
    an unsupported construct). The router maps this to a 422 with the reason — it
    is never a 500. ``details`` carries the per-problem lines."""

    def __init__(self, message: str, details: Optional[List[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = list(details or [])

    def as_text(self) -> str:
        if self.details:
            return f"{self.message}: " + "; ".join(self.details)
        return self.message


# ---------------------------------------------------------------------------
# D1 — parse + normalize
# ---------------------------------------------------------------------------
def _normalize_env(name: str, env: Any) -> Optional[Dict[str, str]]:
    """Compose allows environment as a mapping (``{KEY: val}``) or a list
    (``["KEY=val", "KEY"]``). Normalize to a mapping. A bare ``KEY`` (inherit
    from host) becomes ``""`` — a managed host has no such value, so this makes
    the (empty) default explicit rather than silently carrying a host coupling."""
    if env is None:
        return None
    if isinstance(env, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in env.items()}
    if isinstance(env, list):
        out: Dict[str, str] = {}
        for item in env:
            if not isinstance(item, (str, int, float)):
                raise ComposeImportError(
                    f"service {name!r}: environment list entries must be strings")
            s = str(item)
            if "=" in s:
                k, v = s.split("=", 1)
                out[k] = v
            else:
                out[s] = ""
        return out
    raise ComposeImportError(
        f"service {name!r}: environment must be a mapping or a list")


def _normalize_ports(name: str, ports: Any) -> List[Any]:
    """Keep ports as a list, validating each entry is a shape compose accepts
    (short string ``"8080:80"``, a bare int, or a long-form mapping). We do not
    rewrite them — the worker passes them straight to ``docker compose`` — but we
    reject a malformed ``ports:`` here rather than at deploy."""
    if ports is None:
        return []
    if not isinstance(ports, list):
        raise ComposeImportError(f"service {name!r}: ports must be a list")
    out: List[Any] = []
    for p in ports:
        if isinstance(p, (str, int, dict)):
            out.append(p)
        else:
            raise ComposeImportError(
                f"service {name!r}: unsupported port entry {p!r}")
    return out


def _port_target_and_protocol(name: str, p: Any) -> Tuple[int, str]:
    """Extract the CONTAINER-side ``(target, protocol)`` from one already
    shape-validated ``ports:`` entry (short string, bare int, or long-form
    mapping). The host side (left of the last ``:`` in a short string, or
    ``published``/``host_ip`` in long form) is deliberately discarded here —
    that is the host publish GW.1 strips; see ``x-mod.host_ports`` for the
    explicit opt-in to keep one."""
    if isinstance(p, bool):
        raise ComposeImportError(f"service {name!r}: unsupported port entry {p!r}")
    if isinstance(p, int):
        return p, "tcp"
    if isinstance(p, str):
        left, _, proto = p.partition("/")
        protocol = proto.lower().strip() or "tcp"
        parts = left.split(":")
        try:
            target = int(parts[-1])
        except ValueError:
            raise ComposeImportError(f"service {name!r}: unsupported port entry {p!r}")
        return target, protocol
    if isinstance(p, dict):
        try:
            target = int(p.get("target"))
        except (TypeError, ValueError):
            raise ComposeImportError(
                f"service {name!r}: port entry {p!r} is missing a numeric 'target'")
        protocol = str(p.get("protocol") or "tcp").lower().strip() or "tcp"
        return target, protocol
    raise ComposeImportError(f"service {name!r}: unsupported port entry {p!r}")


def _validate_host_ports(raw: Any, service_names) -> List[Dict[str, Any]]:
    """Validate a user-supplied top-level ``x-mod.host_ports`` declaration —
    the ONLY way a host publish survives import. Shape:

        x-mod:
          host_ports:
            - {service: api, target: 8000, host: 8110, protocol: tcp, bind: "127.0.0.1"}
            - {service: api, target: 9000, host: "any"}

    ``host`` is either the literal ``"any"`` (worker picks a free port — see
    GW.2) or an integer 1-65535. Any other shape raises :class:`ComposeImportError`
    (D1 — reject rather than silently drop)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ComposeImportError("x-mod.host_ports must be a list")
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ComposeImportError(f"x-mod.host_ports[{i}] must be an object")
        service = entry.get("service")
        if not isinstance(service, str) or service not in service_names:
            raise ComposeImportError(
                f"x-mod.host_ports[{i}]: 'service' must name a service in this compose")
        try:
            target = int(entry.get("target"))
        except (TypeError, ValueError):
            raise ComposeImportError(
                f"x-mod.host_ports[{i}]: 'target' must be an integer port")
        if not (1 <= target <= 65535):
            raise ComposeImportError(f"x-mod.host_ports[{i}]: 'target' must be 1-65535")
        host = entry.get("host")
        if isinstance(host, str) and host.strip().lower() == "any":
            host_val: Any = "any"
        elif isinstance(host, bool):
            raise ComposeImportError(
                f"x-mod.host_ports[{i}]: 'host' must be \"any\" or an integer port")
        else:
            try:
                host_val = int(host)
            except (TypeError, ValueError):
                raise ComposeImportError(
                    f"x-mod.host_ports[{i}]: 'host' must be \"any\" or an integer port")
            if not (1 <= host_val <= 65535):
                raise ComposeImportError(f"x-mod.host_ports[{i}]: 'host' must be 1-65535")
        protocol = entry.get("protocol", "tcp")
        if protocol not in _VALID_HOST_PORT_PROTOCOLS:
            raise ComposeImportError(
                f"x-mod.host_ports[{i}]: 'protocol' must be one of "
                f"{list(_VALID_HOST_PORT_PROTOCOLS)}")
        bind = entry.get("bind")
        if bind is not None and (not isinstance(bind, str) or not bind.strip()):
            raise ComposeImportError(
                f"x-mod.host_ports[{i}]: 'bind' must be a non-empty string")
        item: Dict[str, Any] = {"service": service, "target": target,
                                "host": host_val, "protocol": protocol}
        if bind:
            item["bind"] = bind.strip()
        out.append(item)
    return out


def normalize_compose(yaml_text: str,
                      *,
                      admission: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Parse ``yaml_text`` with a SAFE loader and normalize it into the compose_spec
    JSONB shape the wizard emits. Raises :class:`ComposeImportError` (never a bare
    exception) on anything a user can fix.

    Normalizes: environment (list<->mapping), ports (validated list); auto-declares
    named volumes referenced by a service but missing from the top-level
    ``volumes:`` (exactly as the /from-wizard and /assemble paths do, so the
    downstream validator's undeclared-named-volume check passes). Preserves
    existing ``x-mod`` blocks, ``deploy``, ``depends_on``, ``networks``,
    ``healthcheck``, ``command`` and other pass-through service keys untouched.

    ``admission`` (27.4 r274) is the caller's bench-derived serving ceiling
    (``{"max_concurrency", "max_ctx"}``, see
    :func:`services.stack_bench_profile.admission_limits_for_stack`); it is
    forwarded to :func:`services.llm_serving_render.render_serving` so an
    ``x-mod.serving`` profile gets clamped to it. ``None`` (the default, and
    every preview/import path) changes nothing.
    """
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise ComposeImportError("no docker-compose content was provided")
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:  # noqa: BLE001 — surfaced as a 422, not a 500
        raise ComposeImportError(f"could not parse YAML: {e}")
    if not isinstance(data, dict):
        raise ComposeImportError("top-level docker-compose must be a mapping")

    details: List[str] = []
    for key, reason in _REJECTED_TOP_LEVEL.items():
        if key in data:
            details.append(reason)

    services = data.get("services")
    if not isinstance(services, dict) or not services:
        # Report rejected-construct problems alongside the shape problem so the
        # user sees everything at once.
        raise ComposeImportError(
            "docker-compose must define a non-empty `services` mapping", details)

    # 27.4 — expand a top-level `x-mod.serving` profile (engine, replicas,
    # slots, ctx, gateway policy) into one engine service per GPU plus the
    # SGLang Model Gateway `lb` BEFORE the per-service normalization loop, so
    # the generated services get the same env/ports treatment as user services
    # and the whole thing funnels through this single normalization point.
    # Pure dict in / dict out, idempotent, no-op without x-mod.serving.
    from utils.paid_import import optional_paid
    # GT.D6 DS-4 — `llm_serving_render` is Paid. Without it, a compose with no
    # `x-mod.serving` block is untouched (no-op either way); one WITH a serving
    # profile cannot be expanded, so surface it as a ComposeImportError (→422)
    # rather than an ImportError 500.
    _lsr = optional_paid("services.llm_serving_render")
    if _lsr is not None:
        try:
            # 27.4 r274 — the "Save & redeploy" edit path passes the stack's
            # bench-derived admission ceiling so the saved spec is clamped to
            # what this stack's own benching proved it can serve. `/import-compose`
            # (a NEW stack, nothing benched) passes nothing, so the profile stays
            # exactly as declared — byte-identical to the pre-27.4 behaviour.
            if admission is not None:
                data = _lsr.render_serving(data, admission)
            else:
                data = _lsr.render_serving(data)
        except _lsr.ServingRenderError as e:
            raise ComposeImportError(str(e))
    else:
        _top = data.get("x-mod")
        _profile = _top.get("serving") if isinstance(_top, dict) else None
        if _profile is not None:
            raise ComposeImportError(
                "This feature is not available in this edition — declare the "
                "engine services explicitly.")
    services = data.get("services")

    # gw-stack-networking GW.1 — host ports are MOD-owned: every `ports:` entry
    # converts to a container-side-only `x-mod.ports` record (see
    # `_port_target_and_protocol`) and the `ports:` key is stripped from the
    # service. Collected here, across all services, then written onto the
    # spec's top-level `x-mod` below (alongside any explicit host_ports
    # opt-in) — mirrors how `x-mod.serving` is a top-level (not per-service)
    # block.
    port_records: List[Dict[str, Any]] = []

    norm_services: Dict[str, Any] = {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            details.append(f"service {name!r} must be a mapping")
            continue
        for bad_key, tmpl in _REJECTED_SERVICE_KEYS.items():
            if bad_key in svc:
                details.append(tmpl.format(name=name))
        for key in svc:
            if not isinstance(key, str) or key in _REJECTED_SERVICE_KEYS:
                continue
            if key in _COMPOSE_SERVICE_KEYS or key.startswith("x-"):
                continue
            hint = _DID_YOU_MEAN.get(key)
            if hint is None:
                near = difflib.get_close_matches(
                    key, sorted(_COMPOSE_SERVICE_KEYS), n=1, cutoff=0.6)
                hint = near[0] if near else None
            if hint:
                details.append(
                    f"service {name!r} uses unknown key `{key}:` — docker compose "
                    f"rejects it; did you mean `{hint}:`?")
            else:
                details.append(
                    f"service {name!r} uses unknown key `{key}:` — docker compose "
                    f"rejects it, so the stack would fail at deploy")
        if not svc.get("image"):
            # `build` was rejected above; a service therefore needs an image.
            if "build" not in svc:
                details.append(f"service {name!r} needs an `image`")
        try:
            copied = copy.deepcopy(svc)
            if "environment" in copied:
                env = _normalize_env(name, copied.get("environment"))
                if env is None:
                    copied.pop("environment", None)
                else:
                    copied["environment"] = env
            if "ports" in copied:
                validated_ports = _normalize_ports(name, copied.get("ports"))
                for p in validated_ports:
                    target, protocol = _port_target_and_protocol(name, p)
                    rec = {"service": name, "target": target, "protocol": protocol}
                    if rec not in port_records:
                        port_records.append(rec)
                copied.pop("ports", None)
            norm_services[name] = copied
        except ComposeImportError as e:
            details.extend([e.message] + e.details)

    if details:
        raise ComposeImportError(
            "docker-compose has constructs a managed stack cannot import", details)

    spec: Dict[str, Any] = {"services": norm_services}

    # Top-level volumes / networks pass through (validated downstream). Named
    # volumes referenced by a service but not declared at the top level are
    # auto-declared, matching from-wizard / assemble.
    top_volumes = data.get("volumes")
    if top_volumes is not None and not isinstance(top_volumes, dict):
        raise ComposeImportError("top-level `volumes` must be a mapping")
    declared = dict(top_volumes or {})
    for svc in norm_services.values():
        for mount in (svc.get("volumes") or []):
            if isinstance(mount, str) and ":" in mount:
                src = mount.split(":")[0]
                if src and not src.startswith((".", "/", "~")):
                    declared.setdefault(src, {})
    if declared:
        spec["volumes"] = declared

    # gw-stack-networking GW.1 — stack subnets are MOD-owned too: strip any
    # `networks.*.ipam` a user pinned (worker v1.0.69 allocates the stack's
    # subnet from MOD_STACK_NET_POOL; a pinned subnet is exactly how two
    # copies of the same stack collided — consign's `192.0.2.0/24`).
    # Stripped subnets are recorded (not silently dropped) so infer_xmod can
    # surface "MOD allocates the stack subnet" to the importing user.
    networks = data.get("networks")
    stripped_subnets: Dict[str, Any] = {}
    if networks is not None:
        if not isinstance(networks, dict):
            raise ComposeImportError("top-level `networks` must be a mapping")
        cleaned_networks: Dict[str, Any] = {}
        for net_name, net_cfg in networks.items():
            cfg = copy.deepcopy(net_cfg) if isinstance(net_cfg, dict) else net_cfg
            if isinstance(cfg, dict) and "ipam" in cfg:
                stripped_subnets[net_name] = cfg.pop("ipam")
            cleaned_networks[net_name] = cfg
        spec["networks"] = cleaned_networks

    # Assemble the top-level x-mod block: converted `ports`, an explicit
    # `host_ports` opt-in (validated), and a record of any stripped subnet.
    # A user's own top-level `x-mod.serving` was already consumed above
    # (render_serving) and never reaches here — this is the ONE place a
    # top-level x-mod survives into the returned spec.
    top_xmod: Dict[str, Any] = {}
    if port_records:
        top_xmod["ports"] = port_records
    raw_top_xmod = data.get("x-mod")
    host_ports_raw = raw_top_xmod.get("host_ports") if isinstance(raw_top_xmod, dict) else None
    if host_ports_raw is not None:
        top_xmod["host_ports"] = _validate_host_ports(host_ports_raw, set(norm_services))
    if stripped_subnets:
        top_xmod["stripped_subnets"] = stripped_subnets
    # Suggest a public front-door service/port from the HTTP-looking target,
    # else the first one found — a FLAGGED suggestion (confirmed: False),
    # never a confirmed exposure. Computed here (not only at infer_xmod/
    # preview time) so it survives straight through finalize_import too.
    if port_records:
        chosen = next((r for r in port_records if r["target"] in _HTTP_LIKE_PORTS),
                      port_records[0])
        top_xmod["ingress"] = {"service": chosen["service"], "port": chosen["target"],
                               "confirmed": False}
    if top_xmod:
        spec["x-mod"] = top_xmod

    return spec


# ---------------------------------------------------------------------------
# stack-definition-edit — compose_spec -> YAML (the reverse of normalize_compose)
# ---------------------------------------------------------------------------
# GET .../definition renders a stored/redacted compose_spec back to YAML for
# the editor textarea. A plain ``yaml.safe_dump`` folds multi-line values
# (e.g. an nginx heredoc in a service's ``command``) into a ``"line1\nline2"``
# quoted scalar, which is technically valid but unreadable; this dumper forces
# literal block style (``|``) for any string containing a newline so those
# stay human-editable. Uses ``yaml.SafeDumper`` as the base (same safety class
# ``yaml.safe_dump`` uses) with only the string representer overridden, kept
# on a private subclass so this never mutates the global ``SafeDumper`` used
# elsewhere in the process.
class _LiteralBlockDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralBlockDumper.add_representer(str, _represent_str)


def compose_to_yaml(spec: Dict[str, Any]) -> str:
    """Render a compose_spec dict back to readable YAML. Must round-trip:
    ``yaml.safe_load(compose_to_yaml(spec)) == spec`` for any JSON-safe
    ``spec`` (the only shape a compose_spec ever is — JSONB in, JSONB out)."""
    return yaml.dump(spec, Dumper=_LiteralBlockDumper, sort_keys=False,
                     default_flow_style=False, allow_unicode=True, width=10000)


# ---------------------------------------------------------------------------
# D2 — x-mod inference + prompt scaffolding
# ---------------------------------------------------------------------------
def _container_ports(svc: Dict[str, Any], extra_targets: Optional[List[Any]] = None) -> List[int]:
    """Container-side ports a service listens on, derived from ``ports:`` (the
    right/target side of a mapping), ``expose:``, and — GW.1 — any target
    port already converted into the spec's top-level ``x-mod.ports`` for this
    service (``extra_targets``, since ``normalize_compose`` strips ``ports:``
    off the service once it has been converted). This is the readiness-probe
    target — the port the platform would poll — not the host-published port."""
    found: List[int] = []

    def _add(v: Any) -> None:
        try:
            iv = int(str(v).split("/")[0])
        except (TypeError, ValueError):
            return
        if iv not in found:
            found.append(iv)

    for v in (extra_targets or []):
        _add(v)
    for p in (svc.get("ports") or []):
        if isinstance(p, int):
            _add(p)
        elif isinstance(p, str):
            # forms: "80", "8080:80", "127.0.0.1:8080:80", "8080:80/tcp"
            parts = p.split(":")
            _add(parts[-1])
        elif isinstance(p, dict):
            _add(p.get("target", p.get("published")))
    for e in (svc.get("expose") or []):
        _add(e)
    return found


def _image_matches(image: Any, tokens: Tuple[str, ...]) -> bool:
    if not isinstance(image, str):
        return False
    low = image.lower()
    return any(tok in low for tok in tokens)


def _gpu_signal(svc: Dict[str, Any]) -> Optional[str]:
    """Return the SOURCE of a GPU signal ("deploy" | "image") or None.

    A compose device reservation (``deploy.resources.reservations.devices`` with
    ``capabilities: [gpu]`` or ``driver: nvidia``) is authoritative for "wants a
    GPU"; the image name is a softer heuristic. Neither can express VRAM SIZE —
    that is always prompted (see _aggregate_accelerator_demand's rationale)."""
    devices = (((svc.get("deploy") or {}).get("resources") or {})
               .get("reservations") or {}).get("devices")
    if isinstance(devices, list):
        for d in devices:
            if not isinstance(d, dict):
                continue
            caps = d.get("capabilities") or []
            if isinstance(caps, list) and any(str(c).lower() == "gpu" for c in caps):
                return "deploy"
            if str(d.get("driver", "")).lower() == "nvidia":
                return "deploy"
    if _image_matches(svc.get("image"), _GPU_IMAGE_TOKENS):
        return "image"
    return None


def _gpu_count(svc: Dict[str, Any]) -> int:
    devices = (((svc.get("deploy") or {}).get("resources") or {})
               .get("reservations") or {}).get("devices")
    if isinstance(devices, list):
        for d in devices:
            if isinstance(d, dict) and isinstance(d.get("count"), int):
                return max(1, d["count"])
    return 1


def _need(scope: str, field: str, suggested: Any, reason: str,
          service: Optional[str] = None, options: Optional[List[Any]] = None,
          required: bool = False) -> Dict[str, Any]:
    item: Dict[str, Any] = {"scope": scope, "field": field,
                            "suggested": suggested, "reason": reason,
                            "required": required}
    if service is not None:
        item["service"] = service
    if options is not None:
        item["options"] = options
    return item


def infer_xmod(compose_spec: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Infer the ``x-mod`` metadata we can and return
    ``(annotated_spec, needs_confirmation)``.

    Inferred values are WRITTEN onto a copy of the spec (accelerator when a GPU is
    detected — required+kind+count, VRAM left to prompt; readiness port from the
    container ports). Everything the compose can't express becomes a structured
    needs-confirmation item the wizard shows the user: accelerator (or its VRAM),
    egress policy, readiness path, plus the stack-level ha_class and
    storage_class. An ``x-mod`` block already present on a service is respected
    and never overwritten.
    """
    spec = copy.deepcopy(compose_spec)
    services = spec.get("services") or {}
    needs: List[Dict[str, Any]] = []

    # gw-stack-networking GW.1 — target ports already converted onto the
    # spec's top-level x-mod.ports (normalize_compose strips `ports:` off the
    # service once converted), grouped back by service so readiness inference
    # below still sees them.
    top_xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else {}
    top_ports_by_service: Dict[str, List[int]] = {}
    for entry in (top_xmod.get("ports") or []):
        if isinstance(entry, dict) and isinstance(entry.get("service"), str):
            top_ports_by_service.setdefault(entry["service"], []).append(entry.get("target"))

    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        xmod = dict(svc.get("x-mod") or {})

        # --- accelerator -----------------------------------------------------
        if "accelerator" not in xmod:
            source = _gpu_signal(svc)
            if source:
                xmod["accelerator"] = {"required": True, "kind": "cuda",
                                       "count": _gpu_count(svc)}
                needs.append(_need(
                    "service", "accelerator.min_vram_gb", None,
                    f"GPU detected from {source}; confirm the required VRAM per device (GB)",
                    service=name, required=True))
            else:
                needs.append(_need(
                    "service", "accelerator", {"required": False},
                    "no GPU signal found; confirm whether this service needs an accelerator",
                    service=name, options=["none", "cuda", "rocm", "cpu"]))

        # --- egress ----------------------------------------------------------
        if "egress" not in xmod:
            suggested = "none" if _image_matches(svc.get("image"), _NO_EGRESS_IMAGE_TOKENS) else None
            needs.append(_need(
                "service", "egress", ({"mode": suggested} if suggested else None),
                "confirm the outbound network policy for this service",
                service=name, options=list(EGRESS_MODES)))

        # --- readiness -------------------------------------------------------
        if "readiness" not in xmod:
            cports = _container_ports(svc, top_ports_by_service.get(name))
            if cports:
                xmod["readiness"] = {"port": cports[0]}
                needs.append(_need(
                    "service", "readiness.path", "/",
                    f"readiness port {cports[0]} inferred from published ports; confirm the health path",
                    service=name))
            else:
                needs.append(_need(
                    "service", "readiness", None,
                    "no published port found; confirm a readiness port/path or leave unset",
                    service=name))

        # --- smoke (correctness) --------------------------------------------
        # A managed stack cannot be characterized for correctness without knowing
        # how to make ONE real request and what a valid response looks like. Known
        # families ship a bundled fixture + request client; a custom compose has
        # neither, so we PROMPT for an optional `x-mod.smoke` block. Optional: a
        # stack with no smoke declared is still characterized (warm-time, VRAM,
        # readiness) — only the correctness assertion is skipped.
        if "smoke" not in xmod:
            needs.append(_need(
                "service", "smoke", None,
                "optional: declare a correctness smoke request + expected response "
                "so characterization can prove this service actually serves "
                "(request: {method?, path, port?, json?, body?, headers?}; "
                "expect: {status?, contains?, json_contains?})",
                service=name))

        if xmod:
            svc["x-mod"] = xmod

    # --- network (GW.1) ------------------------------------------------------
    # A stripped `ports:` publish is explained per-service, not silently
    # dropped: "reach it at <stack>-<service>:<target> on the mesh, or Expose
    # it" (the mesh naming convention `<stack>-<service>` is the LIVE internal
    # reach path — see mod-connect-service-mesh; "Expose" is the front-door
    # toggle — see caddy-stack-ingress). One note per service, listing every
    # target that lost its host publish.
    ports_by_service: Dict[str, List[int]] = {}
    for entry in (top_xmod.get("ports") or []):
        if isinstance(entry, dict) and isinstance(entry.get("service"), str):
            ports_by_service.setdefault(entry["service"], []).append(entry.get("target"))
    for svc_name in sorted(ports_by_service):
        targets = ports_by_service[svc_name]
        first = targets[0]
        needs.append(_need(
            "service", "network.ports", None,
            f"host port publishing was removed on import "
            f"({', '.join(str(t) for t in targets)}); reach it at "
            f"<stack>-{svc_name}:{first} on the mesh, or Expose it",
            service=svc_name))

    # A stripped `networks.*.ipam` subnet, one note per network.
    for net_name in sorted(top_xmod.get("stripped_subnets") or {}):
        needs.append(_need(
            "stack", "network.subnet", None,
            f"network {net_name!r} pinned a fixed subnet; MOD allocates the "
            f"stack subnet automatically so two stacks (or two copies of one) "
            f"never collide"))

    # x-mod.ingress (the HTTP-looking-target suggestion) was already computed
    # by normalize_compose so it survives straight through finalize_import
    # too — surface it here as a needs-confirmation prompt for the preview UI,
    # never auto-confirmed (GW.4 wires the actual Expose toggle).
    ingress = top_xmod.get("ingress")
    if isinstance(ingress, dict):
        needs.append(_need(
            "stack", "ingress", {"service": ingress.get("service"), "port": ingress.get("port")},
            "suggested public front-door service/port; confirm or choose "
            "another before exposing this stack"))

    # --- stack-level -------------------------------------------------------
    default_storage = "local_fs" if spec.get("volumes") else "s3_native"
    needs.append(_need("stack", "storage_class", default_storage,
                       "confirm the storage class for this stack",
                       options=list(STORAGE_CLASSES)))
    needs.append(_need("stack", "ha_class", "external",
                       "confirm the HA class (external = never auto-relocated)",
                       options=list(HA_CLASSES)))

    return spec, needs


# ---------------------------------------------------------------------------
# apply confirmed values as x-mod fields
# ---------------------------------------------------------------------------
def _apply_accelerator(name: str, xmod: Dict[str, Any], value: Any) -> None:
    if not value or (isinstance(value, dict) and value.get("required") is False
                     and "min_vram_gb" not in value):
        xmod.pop("accelerator", None)
        return
    if not isinstance(value, dict):
        raise ComposeImportError(f"service {name!r}: accelerator confirmation must be an object")
    block: Dict[str, Any] = {"required": bool(value.get("required", True))}
    if value.get("min_vram_gb") is not None:
        try:
            block["min_vram_gb"] = float(value["min_vram_gb"])
        except (TypeError, ValueError):
            raise ComposeImportError(f"service {name!r}: accelerator.min_vram_gb must be a number")
    if value.get("count") is not None:
        try:
            block["count"] = int(value["count"])
        except (TypeError, ValueError):
            raise ComposeImportError(f"service {name!r}: accelerator.count must be an integer")
    if value.get("kind"):
        block["kind"] = str(value["kind"]).strip().lower()
    xmod["accelerator"] = block


def _apply_egress(name: str, xmod: Dict[str, Any], value: Any) -> None:
    if not value:
        xmod.pop("egress", None)
        return
    if not isinstance(value, dict):
        raise ComposeImportError(f"service {name!r}: egress confirmation must be an object")
    mode = value.get("mode")
    if mode not in EGRESS_MODES:
        raise ComposeImportError(
            f"service {name!r}: egress.mode must be one of {list(EGRESS_MODES)}")
    block: Dict[str, Any] = {"mode": mode}
    if mode == "hosts":
        hosts = value.get("hosts")
        if not isinstance(hosts, list) or not hosts or not all(
                isinstance(h, str) and h.strip() for h in hosts):
            raise ComposeImportError(
                f"service {name!r}: egress mode 'hosts' needs a non-empty list of host strings")
        block["hosts"] = [h.strip() for h in hosts]
    xmod["egress"] = block


def _apply_readiness(name: str, xmod: Dict[str, Any], value: Any) -> None:
    if not value:
        xmod.pop("readiness", None)
        return
    if not isinstance(value, dict):
        raise ComposeImportError(f"service {name!r}: readiness confirmation must be an object")
    block: Dict[str, Any] = {}
    if value.get("port") is not None:
        try:
            block["port"] = int(value["port"])
        except (TypeError, ValueError):
            raise ComposeImportError(f"service {name!r}: readiness.port must be an integer")
    if value.get("path") is not None:
        block["path"] = str(value["path"])
    if block:
        xmod["readiness"] = block
    else:
        xmod.pop("readiness", None)


def _apply_smoke(name: str, xmod: Dict[str, Any], value: Any) -> None:
    """Write a confirmed `x-mod.smoke` block (a correctness request + expected
    response) onto a service. A falsy value CLEARS it. Shape:

        smoke:
          request: {method?, path, port?, protocol?, json?, body?, headers?, timeout_s?}
          expect:  {status?, contains?, json_contains?}

    Only `request.path` is required — it is what makes the block executable; a
    smoke block without it is rejected (rather than silently stored as a no-op).
    The values are consumed by services/stack_calibration.parse_xmod_smoke."""
    if not value:
        xmod.pop("smoke", None)
        return
    if not isinstance(value, dict):
        raise ComposeImportError(f"service {name!r}: smoke confirmation must be an object")
    req = value.get("request")
    if not isinstance(req, dict) or not str(req.get("path") or "").strip():
        raise ComposeImportError(
            f"service {name!r}: smoke.request needs a non-empty `path`")
    exp = value.get("expect")
    if exp is not None and not isinstance(exp, dict):
        raise ComposeImportError(f"service {name!r}: smoke.expect must be an object")
    # Stored verbatim (the calibration parser validates/coerces types defensively);
    # we only enforce the one structural invariant that makes it executable.
    xmod["smoke"] = {"request": copy.deepcopy(req),
                     **({"expect": copy.deepcopy(exp)} if isinstance(exp, dict) else {})}


def apply_confirmations(compose_spec: Dict[str, Any],
                        confirmations: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Write the user's confirmed per-service ``x-mod`` values onto a copy of the
    spec. ``confirmations["services"]`` maps a service name to any of
    ``accelerator`` / ``egress`` / ``readiness`` / ``smoke`` (a falsy value CLEARS
    that field). Stack-level ha_class / storage_class are handled by
    :func:`derive_stack_fields`, not here. Raises :class:`ComposeImportError` on a
    malformed confirmation or an unknown service name."""
    spec = copy.deepcopy(compose_spec)
    services = spec.get("services") or {}
    per_service = (confirmations or {}).get("services") or {}
    if not isinstance(per_service, dict):
        raise ComposeImportError("confirmations.services must be an object")

    for name, conf in per_service.items():
        svc = services.get(name)
        if not isinstance(svc, dict):
            raise ComposeImportError(f"confirmation references unknown service {name!r}")
        if not isinstance(conf, dict):
            raise ComposeImportError(f"confirmation for service {name!r} must be an object")
        xmod = dict(svc.get("x-mod") or {})
        if "accelerator" in conf:
            _apply_accelerator(name, xmod, conf["accelerator"])
        if "egress" in conf:
            _apply_egress(name, xmod, conf["egress"])
        if "readiness" in conf:
            _apply_readiness(name, xmod, conf["readiness"])
        if "smoke" in conf:
            _apply_smoke(name, xmod, conf["smoke"])
        if xmod:
            svc["x-mod"] = xmod
        else:
            svc.pop("x-mod", None)
    return spec


def derive_stack_fields(compose_spec: Dict[str, Any],
                        confirmations: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Resolve the stack-level fields the model needs (storage_class, ha_class)
    from the confirmations, falling back to safe defaults (local_fs when the stack
    has named volumes else s3_native; ha_class external = never auto-relocated)."""
    conf = confirmations or {}
    storage = conf.get("storage_class")
    if storage not in STORAGE_CLASSES:
        storage = "local_fs" if compose_spec.get("volumes") else "s3_native"
    ha = conf.get("ha_class")
    if ha not in HA_CLASSES:
        ha = "external"
    return {"storage_class": storage, "ha_class": ha}


# ---------------------------------------------------------------------------
# D3/D4 — validate through the existing loop, then create
# ---------------------------------------------------------------------------
def finalize_import(*, name: str,
                    confirmations: Optional[Dict[str, Any]],
                    validate_fn: Callable[[Dict[str, Any]], None],
                    create_fn: Callable[[Dict[str, Any], Dict[str, str]], Any],
                    yaml_text: Optional[str] = None,
                    compose_spec: Optional[Dict[str, Any]] = None) -> Any:
    """Normalize (from ``yaml_text`` or a pre-normalized ``compose_spec``), apply
    confirmations, run the injected ``validate_fn`` (the wizard's existing
    ``_validate_compose``), then ``create_fn`` (the existing create path) — in that
    order. Only a spec that passes validation reaches ``create_fn`` (D3). Both
    steps are injected so this stays free of DB/HTTP imports and unit-testable.

    ``validate_fn`` is expected to raise on an invalid spec (``_validate_compose``
    raises ``HTTPException(422)``); that propagates unchanged so the caller gets
    the validator's own reason. A problem in THIS module's parsing/normalization
    raises :class:`ComposeImportError`.
    """
    if yaml_text is not None:
        spec = normalize_compose(yaml_text)
    elif compose_spec is not None:
        if not isinstance(compose_spec, dict) or not compose_spec.get("services"):
            raise ComposeImportError("compose_spec must be an object with services")
        spec = copy.deepcopy(compose_spec)
    else:
        raise ComposeImportError("provide either compose (YAML) or compose_spec")

    annotated = apply_confirmations(spec, confirmations)
    stack_fields = derive_stack_fields(annotated, confirmations)
    validate_fn(annotated)          # raises on invalid — do not create
    return create_fn(annotated, stack_fields)
