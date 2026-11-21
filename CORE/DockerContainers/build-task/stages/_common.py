"""Shared helpers for build-task stages."""
import json
import os
import socket
import subprocess
import sys
from typing import Any, Dict, Optional

import requests


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    val = os.environ.get(name, default)
    if required and not val:
        log(f"ERROR: env var {name} is required")
        sys.exit(1)
    return val


def probe_network(spec: Dict[str, Any]) -> str:
    """Docker `--network` value for a smoke/repair probe container.

    Default is ``none`` — no egress, no injection path — which is correct for
    the ~all nodes that read /data/input and write /data/output with no
    external calls. But a node whose Containerizer analysis DECLARED runtime
    egress (``spec.allowed_egress``, e.g. it git-clones a repo or hits an API)
    genuinely needs the network at smoke time, or it fails DNS on --network=none
    and the LLM thrashes trying to "fix" a non-code problem.

    When egress is declared, route the probe to the SAME egress network the
    worker uses at runtime (netpol.go: AllowedEgress non-empty -> egress net),
    so the smoke env matches production and adds no new exposure. Fail closed
    (``none``) if that egress network isn't present on the daemon — a declared
    egress node that can't reach its egress net is not "back on the open
    internet"; returning ``bridge`` here would silently defeat the egress
    declaration, so we deliberately return ``none`` and log so the operator
    knows the net is missing (it should exist as a shared service network).
    """
    egress = spec.get("allowed_egress") or []
    if not egress:
        return "none"
    net = os.environ.get("MOD_WIZARD_EGRESS_NETWORK", "mod-user-egress")
    try:
        r = subprocess.run(
            ["docker", "network", "inspect", net],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return net
        log(f"WARNING [probe_network] egress declared but network '{net}' absent — failing closed (none, not bridge)")
    except Exception as e:  # noqa: BLE001
        log(f"WARNING [probe_network] network inspect failed ({e}) — failing closed (none, not bridge)")
    return "none"


def fetch_build_spec(api_url: str, api_key: str, spec_id: int) -> Dict[str, Any]:
    """Fetch a serialized BuildSpec from the API.

    Currently goes through a dedicated /build-specs/{id} endpoint
    (B4 wires this up). Returns the dict form of BuildSpec; callers
    pull whatever fields they need.
    """
    r = requests.get(
        f"{api_url}/v1/woodsman/build-specs/{spec_id}",
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _self_container_id() -> Optional[str]:
    """Find this container's docker ID from inside it.

    `socket.gethostname()` works most of the time but the worker can
    override hostname. The cgroup path is the authoritative source.
    Tries (in order): hostname, /proc/1/cpuset, /proc/self/cgroup,
    /proc/self/mountinfo. Returns the first match — even a partial
    container ID is enough for `docker inspect`.
    """
    candidates = []
    try:
        candidates.append(socket.gethostname())
    except Exception:
        pass
    # /proc/1/cpuset on cgroup v1 docker hosts ends with the container ID:
    #   /docker/<64-char-id>
    try:
        with open("/proc/1/cpuset") as f:
            line = f.read().strip()
            if "/docker/" in line:
                candidates.append(line.rsplit("/", 1)[-1])
    except Exception:
        pass
    # /proc/self/cgroup line like "0::/docker/<id>" or older formats.
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                if "docker" in line:
                    last = line.strip().rsplit("/", 1)[-1]
                    if len(last) >= 12 and all(c in "0123456789abcdef" for c in last):
                        candidates.append(last)
                        break
    except Exception:
        pass
    # Deduplicate, keep order.
    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[0] if out else None


def _parse_mountinfo_for_data() -> Optional[str]:
    """Fallback: read /proc/self/mountinfo and extract the host source
    path for whatever's mounted at /data. Used when `docker inspect`
    is unreachable (no docker socket, or this isn't a docker host).

    The mountinfo format:
      36 35 98:0 /<source-path> /<mount-point> rw,noatime master:1 - ext4 /dev/sda1 rw,errors=remount-ro
                  ^-- field 4    ^-- field 5

    For a docker volume bind-mounted at /data, field 4 will be the
    on-host path inside the docker root (e.g. /var/lib/docker/volumes/
    <volname>/_data) and field 5 will be /data.
    """
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 5:
                    continue
                # field 5 (index 4) is the mount point in this namespace.
                if parts[4] == "/data":
                    src = parts[3]
                    # mountinfo escapes special chars as octal (e.g.
                    # \040 for space). For docker volumes the path is
                    # plain ASCII so we don't bother unescaping here.
                    if src and src != "/" and src != "":
                        return src
    except Exception:
        pass
    return None


def resolve_data_host_path() -> Optional[str]:
    """Return the HOST filesystem path that backs /data inside this
    build-task container, or None if /data isn't a docker-managed mount.

    Why this is needed: stages do nested `docker run -v {path}:/data/*`.
    The `{path}` is resolved by the HOST's docker daemon — not by the
    build-task's filesystem. Paths created via tempfile.mkdtemp() inside
    the build-task don't exist on the host. The daemon silently
    auto-creates them as empty root-owned dirs, then the wizard image's
    non-root USER can't write to /data/output.

    Tries two paths in order:
      1. `docker inspect <self>` — authoritative when the docker socket
         is reachable and the container ID resolves.
      2. /proc/self/mountinfo parsing — works without docker access by
         reading the kernel's mount table directly. Catches the case
         where docker inspect returns empty (custom hostname, weird
         container runtime) but the bind mount IS visible.

    Returns None only when BOTH paths fail — usually means /data isn't
    a docker volume at all (dev mode running outside docker).
    """
    cid = _self_container_id()
    if cid:
        try:
            res = subprocess.run(
                ["docker", "inspect", "--format",
                 '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}',
                 cid],
                capture_output=True, text=True, timeout=10,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    # Fallback: parse the kernel's mount table. Works without docker.
    return _parse_mountinfo_for_data()


def resolve_data_volume_name() -> Optional[str]:
    """Return the docker NAMED VOLUME backing /data inside this build-task
    container, or None when /data is a plain bind mount / not docker-managed.

    Companion to `resolve_data_host_path`. Why a volume NAME: on a
    Docker-Desktop/WSL2 host the docker daemon lives in a VM, so the
    on-host `Mountpoint` of a named volume (`/var/lib/docker/volumes/
    <vol>/_data`) does NOT exist in the daemon's filesystem. A nested
    `docker run -v {mountpoint}/subdir:/data/input` then gets an
    auto-created EMPTY dir even though the build-task wrote the synth
    file into the volume at /data (WS6.8d repro: hflip/blur smoke probes
    saw /data/input with 0 files; the same script passed when the
    daemon-root layout was different). Passing the volume NAME to a
    nested `docker run -v {vol}:<target>` makes the daemon mount the
    named volume's root at <target> — the same volume this container
    already has at /data — with no host-path indirection at all.

    Returns None for a bind-mounted /data (a path, not a volume) —
    callers must then fall back to `resolve_data_host_path`, which is
    correct on bare-metal Linux hosts where the daemon shares the
    filesystem.
    """
    cid = _self_container_id()
    if not cid:
        return None
    try:
        res = subprocess.run(
            ["docker", "inspect", "--format",
             '{{range .Mounts}}{{if eq .Destination "/data"}}{{if .Name}}{{.Name}}{{else}}BIND{{end}}{{end}}{{end}}',
             cid],
            capture_output=True, text=True, timeout=10,
        )
        out = res.stdout.strip()
        if res.returncode == 0 and out and out != "BIND":
            return out
    except Exception:
        pass
    return None


def min_free_gb_default() -> int:
    """Free-disk floor (GB) for the build pre-flight; ``BUILD_MIN_FREE_GB``
    overrides the 30 GB default so a small homelab disk can opt in to a
    lower floor knowingly."""
    try:
        return max(1, int(float(os.environ.get("BUILD_MIN_FREE_GB", "30"))))
    except ValueError:
        return 30


def check_docker_disk_space(min_gb: int = 30) -> Optional[str]:
    """Inspect the docker daemon's storage usage and refuse to proceed
    if there's not enough headroom for the build.

    Stage repro: TRELLIS.2 builds (~10 GB each) ran 5+ times via
    auto-repair, plus 84 GB of stale build cache, plus 161 GB of
    dangling images. The host hit 92% disk; BuildKit cancelled mid-
    build with an error containing "docker" (the system reporting the
    cancel). The LLM pattern-matched that as "Docker daemon issue"
    and kept rewriting the Dockerfile to add/remove docker-cli — a
    fix that can never address the real problem.

    Returns None if there's enough space, or a human-readable error
    string the stage can _progress() to the user and bail with.
    `min_gb` is a conservative floor — TRELLIS-class builds need
    ~15-20 GB working space; 30 GB leaves room for the build cache
    + the existing live containers.

    Two checks:
      1. `docker system df` summary — total reclaimable + size of
         existing images / build cache. If the docker root is on the
         same filesystem as / and / is >90% full, we refuse.
      2. statvfs(/var/lib/docker) for the actual free bytes.
    """
    try:
        # Find the docker root dir to statvfs.
        info = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True, text=True, timeout=10,
        )
        docker_root = (info.stdout or "/var/lib/docker").strip()
    except Exception:
        docker_root = "/var/lib/docker"

    free_gb: Optional[float] = None
    try:
        import os as _os
        stat = _os.statvfs(docker_root)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    except Exception:
        # If we can't read statvfs, don't block — better to attempt the
        # build and surface a real error than block on a faulty check.
        return None

    if free_gb is None:
        return None

    if free_gb < min_gb:
        # Build a helpful message that names the recovery action.
        return (
            f"Refusing to start the build: only {free_gb:.1f} GB free on "
            f"the docker root ({docker_root}); need at least {min_gb} GB. "
            f"Run `docker system prune -a --volumes -f` on the host to "
            f"reclaim space, then retry. (Auto-repair WILL run on this "
            f"node, but a build that fails on disk pressure produces "
            f"misleading error messages the LLM keeps mis-interpreting "
            f"as 'docker daemon' issues — the loop won't converge.)"
        )
    return None


def registry_is_unset(value: Optional[str]) -> bool:
    """True when ``value`` is no registry at all.

    Empty/blank, and the auto-seeded Vault placeholder ``PLEASE_CONFIGURE``
    that every Settings→Secrets row carries until an operator edits it. The
    placeholder is not a host: handing it to docker produces
    ``Get "https://PLEASE_CONFIGURE/v2/"`` + ``server misbehaving``, which
    buries the real pull failure under a garbage login. The rest of the
    codebase already agrees on this ("unset") — same test as
    worker_tasks.py:5258, worker_management.py:1682,
    platform_feature_config.py:68, harbor_proxy_cache.py:227,
    dependencies.py:2455, utils/github_token.py:24. Kept case-insensitive so
    a re-typed variant cannot slip through.
    """
    cleaned = (value or "").strip()
    return cleaned == "" or cleaned.upper() == "PLEASE_CONFIGURE"


def docker_login(registry: str, username: str, password: str) -> None:
    """Log in to the given Docker registry."""
    proc = subprocess.run(
        ["docker", "login", registry, "--username", username, "--password-stdin"],
        input=password, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        log(f"docker login failed: {proc.stderr}")
        raise RuntimeError("docker login failed")


def filtered_build_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """GSEC FX.16 R-023 — env for docker/buildx/skopeo subprocesses.

    The stage container legitimately holds API_KEY and the registry
    creds (resolved from claim-time secret_refs), but the docker CLI
    child must not inherit them: the build runs an UNTRUSTED,
    LLM-generated Dockerfile, and a leaked API token or push robot in
    the build-step env is an exfiltration surface. Registry auth for
    the build is already established by a separate
    ``docker login --password-stdin`` (see docker_login) — nothing
    registry-related is needed in the child env. Drops API_KEY,
    *_PASSWORD, *_TOKEN, any name containing SECRET, and HARBOR_*
    names; passes everything else (PATH, HOME, DOCKER_HOST, …)
    through.
    """
    env = dict(os.environ)
    for k in list(env):
        u = k.upper()
        if (
            u == "API_KEY"
            or u.endswith(("_PASSWORD", "_TOKEN"))
            or "SECRET" in u
            or u.startswith("HARBOR_")
            or u.startswith("PLATFORM_HARBOR_")
        ):
            del env[k]
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# The ONE place the image-build command is assembled.
#
# Both build call sites (build_one_variant.py and the hero build inside
# test_and_repair_one_variant.py) go through ``build_command`` /
# ``build_subprocess_env`` so there is a single seam to change when the build
# backend moves (e.g. a rootless BuildKit sidecar): point the builder with the
# ``BUILD_BUILDER`` env (passed as ``--builder``) and/or ``DOCKER_HOST`` /
# ``BUILDX_BUILDER`` (both pass through ``filtered_build_env`` untouched), or
# swap the argv below. Nothing else in the stages assembles a buildx command.
#
# Resource caps (CE/homelab defaults, ONLY when CE_BUILD_CAPS=1; env-tunable; "0"/"off" disables a cap):
#   BUILD_MEMORY_LIMIT  memory cap for each RUN step (default "6g"). Applied
#                       as memory + memory-swap so the cap is a real ceiling:
#                       with memory alone the daemon lets the step swap and
#                       the limit is not enforced.
#   BUILD_CPUS          CPU cap for each RUN step, in CPUs (default "4").
#   BUILD_TIMEOUT_S     wall-clock cap for one build (default 3600).
#   BUILD_NETWORK       optional --network for RUN steps (default: the
#                       builder's own, i.e. the daemon's default bridge).
#   BUILD_MIN_FREE_GB   free-disk floor of the pre-flight (default 30).
# ---------------------------------------------------------------------------
BUILD_MEMORY_LIMIT_DEFAULT = "6g"
BUILD_CPUS_DEFAULT = "4"
BUILD_TIMEOUT_S_DEFAULT = 3600
_CPU_PERIOD_US = 100000

_BUILDX_RESOURCE_SUPPORT: Optional[bool] = None


def ce_build_caps_enabled() -> bool:
    """CE_BUILD_CAPS=1 (set by the CE overlay compose only) turns on the default
    memory/CPU/time safety caps. Absent = paid: no default caps (old limits);
    an operator can still set BUILD_MEMORY_LIMIT / BUILD_CPUS / BUILD_TIMEOUT_S."""
    return (os.environ.get("CE_BUILD_CAPS") or "").strip().lower() in ("1", "true", "yes", "on")


def _env_cap(name: str, default: str) -> Optional[str]:
    """Env-tunable cap value; None when explicitly disabled (0/off/none/'')."""
    raw = os.environ.get(name)
    if raw is None and not ce_build_caps_enabled():
        return None  # paid / flag absent: the pre-seam behaviour (no default caps)
    val = (default if raw is None else raw).strip().lower()
    if val in ("", "0", "off", "none", "false", "unlimited"):
        return None
    return val


def build_timeout_s() -> Optional[int]:
    """Wall-clock cap (seconds) for one image build; None = no cap."""
    raw = _env_cap("BUILD_TIMEOUT_S", str(BUILD_TIMEOUT_S_DEFAULT))
    if raw is None:
        return None
    try:
        secs = int(float(raw))
    except ValueError:
        log(f"WARNING [build] BUILD_TIMEOUT_S={raw!r} is not a number; "
            f"using {BUILD_TIMEOUT_S_DEFAULT}s")
        return BUILD_TIMEOUT_S_DEFAULT
    return secs if secs > 0 else None


def buildx_supports_resource() -> bool:
    """Does this buildx accept ``--resource``? Probed once per process: an
    older buildx would reject the unknown flag and fail the whole build, so
    the caps are skipped (with a log line) rather than breaking builds."""
    global _BUILDX_RESOURCE_SUPPORT
    if _BUILDX_RESOURCE_SUPPORT is None:
        try:
            out = subprocess.run(
                ["docker", "buildx", "build", "--help"],
                capture_output=True, text=True, timeout=30,
            )
            _BUILDX_RESOURCE_SUPPORT = "--resource" in ((out.stdout or "") + (out.stderr or ""))
        except Exception:  # noqa: BLE001 — probe failure == no caps, never a failed build
            _BUILDX_RESOURCE_SUPPORT = False
        if not _BUILDX_RESOURCE_SUPPORT:
            log("WARNING [build] this buildx has no --resource flag; "
                "BUILD_MEMORY_LIMIT / BUILD_CPUS are not applied")
    return _BUILDX_RESOURCE_SUPPORT


def build_resource_args() -> list:
    """``--resource`` arguments for the configured memory / CPU caps."""
    mem = _env_cap("BUILD_MEMORY_LIMIT", BUILD_MEMORY_LIMIT_DEFAULT)
    cpus_raw = _env_cap("BUILD_CPUS", BUILD_CPUS_DEFAULT)
    cpu_quota: Optional[int] = None
    if cpus_raw is not None:
        try:
            cpus = float(cpus_raw)
            cpu_quota = int(cpus * _CPU_PERIOD_US) if cpus > 0 else None
        except ValueError:
            log(f"WARNING [build] BUILD_CPUS={cpus_raw!r} is not a number; CPU cap skipped")
    if mem is None and cpu_quota is None:
        return []
    if not buildx_supports_resource():
        return []
    args: list = []
    if mem is not None:
        args += ["--resource", f"memory={mem}", "--resource", f"memory-swap={mem}"]
    if cpu_quota is not None:
        args += ["--resource", f"cpu-period={_CPU_PERIOD_US}",
                 "--resource", f"cpu-quota={cpu_quota}"]
    return args


def build_command(tag: str, ctx_dir: str, *, progress: Optional[str] = None) -> list:
    """argv for ``docker buildx build`` of ``ctx_dir`` into the local daemon
    as ``tag``. ``progress`` e.g. "plain" for line-oriented output.

    --provenance=false / --sbom=false drop the attestation manifests (a
    registry rejects the index's empty-layer blob); --output=type=docker
    loads the result into the daemon's image store so the push/smoke stages
    can see it (the default, cacheonly, would leave nothing behind).
    """
    cmd = ["docker", "buildx", "build"]
    builder = (os.environ.get("BUILD_BUILDER") or "").strip()
    if builder:
        cmd += ["--builder", builder]
    network = (os.environ.get("BUILD_NETWORK") or "").strip()
    if network:
        cmd += ["--network", network]
    if progress:
        cmd.append(f"--progress={progress}")
    cmd += ["--provenance=false", "--sbom=false", "--output=type=docker"]
    cmd += build_resource_args()
    cmd += ["-t", tag, ctx_dir]
    return cmd


def build_subprocess_env() -> Dict[str, str]:
    """Env for the build subprocess: credential-filtered (see
    ``filtered_build_env``) with BuildKit forced on."""
    return filtered_build_env({"DOCKER_BUILDKIT": "1"})


def anonymous_registry_push(harbor_url: str, user: str, password: str) -> bool:
    """True for a deliberate credential-less push (Community's optional
    auth-less registry:2): ``MOD_PUBLISH_MODE=registry_anonymous`` stamped by
    the API, a registry host present, and no credentials. Without that
    explicit stamp, missing credentials remain a ``creds_missing`` failure."""
    mode = (os.environ.get("MOD_PUBLISH_MODE") or "").strip().lower()
    return (mode == "registry_anonymous" and bool((harbor_url or "").strip())
            and not user and not password)


def local_publish_mode(harbor_url: str) -> bool:
    """True when this build publishes to the LOCAL docker daemon only.

    Requires BOTH an explicit ``MOD_PUBLISH_MODE=local`` (stamped on the stage
    env by the API only for a Community install with no registry configured)
    AND an empty registry host. An empty ``HARBOR_URL`` on its own is NOT
    enough: on a registry-backed install it means the registry lookup failed,
    and that must stay a loud ``creds_missing`` failure, never a silent
    local-only "publish".
    """
    mode = (os.environ.get("MOD_PUBLISH_MODE") or "").strip().lower()
    return mode == "local" and not (harbor_url or "").strip()


def report_back(
    api_url: str,
    api_key: str,
    *,
    spec_id: int,
    stage: str,
    variant: Optional[str],
    success: bool,
    payload: Dict[str, Any],
) -> None:
    """Best-effort post of stage result back to the API. Used for
    surfacing the final image digest, smoke-test result, etc."""
    body = {
        "spec_id": spec_id,
        "stage": stage,
        "variant": variant,
        "success": success,
        "payload": payload,
    }
    try:
        requests.post(
            f"{api_url}/v1/woodsman/build-specs/{spec_id}/stage-result",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json=body, timeout=15,
        )
    except Exception as e:
        log(f"report_back failed (non-fatal): {e}")
