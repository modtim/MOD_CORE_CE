"""CIS-style hardening checker for the mod-wizard-pro runtime image.

Usage:
    python3 tools/check_hardened_image.py <image-ref> [--compose compose.hardened.yml]

Prints a per-check PASS/FAIL table and exits 0 when every check passes,
1 otherwise.

Checks (each maps to a CIS Docker Benchmark control):
    1. non-root user            CIS 4.1   User is set, non-empty, and not 0/root
    2. no shell in image        CIS 4.4/5  no /bin/sh or /bin/bash (or dash/ash) on disk
    3. no secret-looking ENV    CIS 5.2   no KEY/SECRET/TOKEN/PASSWORD in image env
    4. healthcheck present      (ops)     image HEALTHCHECK or compose healthcheck
    5. read-only rootfs         CIS 5.10  compose service sets read_only: true
    6. all capabilities dropped CIS 5.12  compose service cap_drop includes ALL
    7. no-new-privileges        CIS 5.13  compose service security_opt has it

The image checks 1/2/3/4 read `docker image inspect` (+ a throwaway
`docker create` + `docker export` to list the filesystem). The compose
checks 5/6/7 read a compose YAML file. When no --compose file is supplied,
the compose-backed checks are reported as SKIP (not FAIL) and only the
image checks decide the exit code.

The checker functions are pure (they take plain data) so the unit test
(`tests/test_check_hardened_image.py`) exercises them without any docker.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# Env-var names that would indicate a baked-in secret. Matched as a
# substring of the env var NAME (never the value).
SECRET_ENV_PATTERNS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")

# Shell binary basenames that must NOT exist in the runtime image. Matched
# under a bin/sbin directory so e.g. `bash-completion` or `libshim` do not
# false-positive, while /bin/sh, /usr/bin/bash, /usr/bin/ash all do.
SHELL_BASENAMES = ("sh", "bash", "dash", "ash")

EXIT_PASS = 0
EXIT_FAIL = 1


# ---------------------------------------------------------------------------
# Pure checkers — no docker, no I/O. Feed them data from inspect/yaml/tar.
# ---------------------------------------------------------------------------

def check_non_root_user(user: Optional[str]) -> bool:
    """CIS 4.1 — the image runs as a non-root user. Empty/absent and
    '0'/'root' both fail; anything else (named user or non-zero uid) passes."""
    if user is None:
        return False
    u = str(user).strip()
    if not u:
        return False
    if u == "root":
        return False
    if u == "0":
        return False
    # A "uid" or "uid:gid" form: the uid part must not be 0.
    uid_part = u.split(":")[0].strip()
    if uid_part == "0":
        return False
    return True


def check_no_shell(image_entries: List[str]) -> bool:
    """CIS 4.4/5 — no shell binary on disk. `image_entries` is the flat list
    of paths from the exported filesystem (the tar member names). True when
    no known shell basename appears under a bin/ or sbin/ directory."""
    import posixpath
    found = False
    for e in image_entries:
        norm = e.lstrip("./")
        base = posixpath.basename(norm)
        if base in SHELL_BASENAMES:
            parent = posixpath.dirname(norm)
            # Only count a real shell: it lives in .../bin or .../sbin.
            leaf = posixpath.basename(parent)
            if leaf in ("bin", "sbin"):
                found = True
                break
    return not found


def check_no_secret_env(env: List[str]) -> bool:
    """CIS 5.2 — no secret-looking environment variable is baked into the
    image. Only the NAME of each `NAME=value` pair is inspected; values are
    never read or compared (nothing here may print a secret)."""
    for item in env or []:
        name = str(item).split("=", 1)[0].strip()
        if not name:
            continue
        up = name.upper()
        for pat in SECRET_ENV_PATTERNS:
            if pat in up:
                return False
    return True


def check_healthcheck_present(image_healthcheck: Optional[Any],
                              compose_healthcheck: Optional[Any]) -> bool:
    """Ops — a healthcheck exists either as the image's HEALTHCHECK
    instruction or the compose service's `healthcheck` block. Either one
    present is a pass."""
    if image_healthcheck:
        return True
    if compose_healthcheck:
        return True
    return False


def check_compose_read_only(service: Dict[str, Any]) -> bool:
    """CIS 5.10 — the compose service declares a read-only rootfs."""
    return bool(service.get("read_only"))


def check_compose_cap_drop_all(service: Dict[str, Any]) -> bool:
    """CIS 5.12 — the compose service drops ALL capabilities. Accepted
    spellings: `cap_drop: [ALL]`, `cap_drop: all`, or `CAP_ALL`."""
    caps = service.get("cap_drop")
    if not caps:
        return False
    if isinstance(caps, str):
        caps = [caps]
    for c in caps:
        if str(c).strip().upper() in ("ALL", "CAP_ALL"):
            return True
    return False


def check_compose_no_new_privileges(service: Dict[str, Any]) -> bool:
    """CIS 5.13 — the compose service sets no-new-privileges:true in
    security_opt (as a string or a list)."""
    opts = service.get("security_opt")
    if not opts:
        return False
    if isinstance(opts, str):
        opts = [opts]
    for o in opts:
        if str(o).strip() == "no-new-privileges:true":
            return True
    return False


def check_compose_no_docker_sock(service: Dict[str, Any]) -> bool:
    """The service must not mount the Docker daemon socket."""
    vols = service.get("volumes") or []
    for v in vols:
        if isinstance(v, str):
            if "docker.sock" in v:
                return False
        elif isinstance(v, dict):
            if "docker.sock" in str(v.get("source", "")):
                return False
    return True


# ---------------------------------------------------------------------------
# Docker / YAML adapters — the thin I/O layer the pure checkers feed from.
# ---------------------------------------------------------------------------

def _run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def docker_image_inspect(image: str) -> Dict[str, Any]:
    rc, out, err = _run(["docker", "image", "inspect", image])
    if rc != 0:
        raise RuntimeError(f"docker image inspect failed: {err.strip()}")
    import json
    return json.loads(out)[0]


def docker_tar_entries(image: str) -> List[str]:
    """List the filesystem of the image via a throwaway container that this
    process itself created (`docker create`) and removes again. The base
    image has no shell, so the create command overrides the entrypoint with
    a no-op and the export stream is parsed for member names."""
    import tarfile
    import tempfile
    tag = "mod-check-tmp-" + os.urandom(4).hex()
    try:
        _run(["docker", "create", "--name", tag, "--entrypoint", "/bin/true", image])
        with tempfile.TemporaryDirectory() as d:
            tar_path = os.path.join(d, "rootfs.tar")
            rc, out, err = _run(["docker", "export", "-o", tar_path, tag])
            if rc != 0:
                raise RuntimeError(f"docker export failed: {err.strip()}")
            entries: List[str] = []
            with tarfile.open(tar_path, "r") as tf:
                entries = tf.getnames()
        return entries
    finally:
        # Remove the throwaway container we created (create, not run — no
        # process ever started). Best-effort; ignore if already gone.
        _run(["docker", "rm", "-f", tag])


def load_compose(path: str, service: Optional[str] = None) -> Dict[str, Any]:
    import yaml
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    services = data.get("services") or {}
    if service is not None:
        if service not in services:
            available = ", ".join(sorted(services.keys())) or "(none)"
            raise RuntimeError(
                f"service {service!r} not found in {path}; "
                f"available services: {available}"
            )
        svc = services[service]
        return svc if isinstance(svc, dict) else {}
    # Return the first service's dict (default, unchanged behavior when no
    # --service is given); callers may pass a specific service by name.
    for svc in services.values():
        if isinstance(svc, dict):
            return svc
    return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image", help="docker image ref to check")
    ap.add_argument("--compose", default=None, help="compose YAML with the service")
    ap.add_argument("--service", default=None,
                    help="name of the service within --compose to check "
                         "(defaults to the first service found)")
    args = ap.parse_args(argv)

    rows: List[Tuple[str, str, str]] = []  # (check, status, detail)

    def add(check: str, ok: bool, detail: str = "") -> None:
        rows.append((check, "PASS" if ok else "FAIL", detail))

    # ---- image checks (need docker) ----
    try:
        cfg = docker_image_inspect(args.image)
        user = (cfg.get("Config") or {}).get("User")
        add("non-root user (CIS 4.1)", check_non_root_user(user),
            f"user={user!r}")

        env = (cfg.get("Config") or {}).get("Env") or []
        add("no secret env (CIS 5.2)", check_no_secret_env(env),
            f"{len(env)} env var(s)")

        hc = cfg.get("Config", {}).get("Healthcheck")
        entries = docker_tar_entries(args.image)
        add("no shell (CIS 4.4/5)", check_no_shell(entries),
            f"{len(entries)} fs entries")
    except Exception as e:  # noqa: BLE001 — report as failure, keep table
        for c in ("non-root user (CIS 4.1)", "no shell (CIS 4.4/5)",
                  "no secret env (CIS 5.2)"):
            rows.append((c, "FAIL", f"image check errored: {e}"))
        hc = None
        compose_hc = None

    # ---- compose checks ----
    compose_service: Dict[str, Any] = {}
    compose_hc = None
    if args.compose:
        try:
            compose_service = load_compose(args.compose, service=args.service)
            compose_hc = compose_service.get("healthcheck")
            add("read-only rootfs (CIS 5.10)",
                check_compose_read_only(compose_service))
            add("drop all caps (CIS 5.12)",
                check_compose_cap_drop_all(compose_service))
            add("no-new-privileges (CIS 5.13)",
                check_compose_no_new_privileges(compose_service))
            add("no docker.sock mount",
                check_compose_no_docker_sock(compose_service))
        except Exception as e:  # noqa: BLE001
            rows.append(("compose parse", "FAIL", str(e)))
    else:
        for c in ("read-only rootfs (CIS 5.10)", "drop all caps (CIS 5.12)",
                  "no-new-privileges (CIS 5.13)", "no docker.sock mount"):
            rows.append((c, "SKIP", "no --compose file supplied"))

    add("healthcheck present", check_healthcheck_present(hc, compose_hc),
        "image" if hc else ("compose" if compose_hc else "none"))

    # ---- print table ----
    width = max(len(r[0]) for r in rows)
    print(f"{'CHECK':<{width}}  {'STATUS':<6}  DETAIL")
    print("-" * (width + 12))
    for check, status, detail in rows:
        print(f"{check:<{width}}  {status:<6}  {detail}")

    fails = [r for r in rows if r[1] == "FAIL"]
    print("-" * (width + 12))
    if fails:
        print(f"RESULT: FAIL ({len(fails)} failed)")
        return EXIT_FAIL
    print("RESULT: PASS")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
