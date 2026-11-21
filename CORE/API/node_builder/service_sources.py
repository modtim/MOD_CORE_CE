"""service-stack-builder Group C — git / script / static-site / image source
resolvers.

Each function turns one non-node service source into a Group 0 ``ServiceFragment``
(see ``service_builder_schemas.py``) — a compose-service fragment that is
**long-lived by construction**: ``restart: unless-stopped``, the image's / build's
own long-lived command, and NONE of the node one-shot executor specialization
(no ``MOD_VARIANT_SMOKE`` smoke-guard entrypoint, no ``/data/input`` → ``/data/output``
→ shutdown contract, no auto-shutdown). A stack service stays running.

Build-farm boundary
-------------------
The node wizard's build path (``node_builder/build_dag/*`` +
``routers/node_definitions.py`` build-and-register) compiles a source into an image
by dispatching a full workflow DAG to the Go worker build farm — and it produces a
*one-shot node* image (smoke-guard entrypoint, data-io contract). Retargeting that
farm to emit a long-lived SERVICE image is out of scope for this resolver and needs
the build farm actually running. So the git/script/site resolvers here do NOT invoke
the farm and do NOT fabricate a "build succeeded" result. Instead they return a
compose ``build:`` fragment — a context/dockerfile the **deploy path** builds (docker
compose / the build farm at deploy time). The boundary is marked explicitly on every
build fragment under ``build[BUILD_BOUNDARY_KEY]`` so callers (and the assemble
endpoint) can see the image is not yet built. ``service_from_image`` needs no build
and is an exact passthrough.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from service_builder_schemas import ServiceFragment


# Marker key set on any `build:` fragment whose image the DEPLOY path must still
# build (the build farm is not invoked from this resolver). Presence of this key
# == "this fragment references a build context, not a ready image".
BUILD_BOUNDARY_KEY = "x-mod-build-source"

# Default image for a static-site service. Alpine nginx stays live serving
# `/usr/share/nginx/html` under its own long-lived command.
NGINX_IMAGE = "nginx:alpine"
NGINX_DOC_ROOT = "/usr/share/nginx/html"


# --- shared config plumbing --------------------------------------------------
def _fragment(**kwargs: Any) -> ServiceFragment:
    """Build a ServiceFragment, always long-lived (`restart: unless-stopped`)."""
    kwargs.setdefault("restart", "unless-stopped")
    return ServiceFragment(**kwargs)


def _common_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the operator-supplied, source-agnostic compose fields out of **cfg.

    Only the fields a long-lived service legitimately carries — no smoke-guard,
    no data-io wiring is ever synthesized here. Unknown keys are ignored so the
    caller can pass a wider config dict without leaking junk into the fragment.
    """
    out: Dict[str, Any] = {}
    env = cfg.get("environment")
    if isinstance(env, dict) and env:
        out["environment"] = {str(k): str(v) for k, v in env.items()}
    ports = cfg.get("ports")
    if ports:
        out["ports"] = [int(p) for p in ports]
    deploy = cfg.get("deploy")
    if isinstance(deploy, dict) and deploy:
        out["deploy"] = deploy
    command = cfg.get("command")
    if command:
        out["command"] = command
    return out


# --- C.4 image (trivial passthrough) -----------------------------------------
def service_from_image(image_ref: str, **cfg: Any) -> ServiceFragment:
    """Off-the-shelf image source — the trivial passthrough.

    The compose service is exactly ``image: <image_ref>`` plus whatever env /
    ports / command the operator configured. No build, no boundary marker; the
    image is assumed to already exist in a registry.
    """
    if not image_ref or not str(image_ref).strip():
        raise ValueError("service_from_image requires a non-empty image_ref")
    return _fragment(image=str(image_ref).strip(), **_common_cfg(cfg))


# --- C.1 git repo (build-from-source, stays live) ----------------------------
def _git_context(repo_url: str, ref: Optional[str], build_context: Optional[str]) -> str:
    """Compose a docker `build.context` git URL.

    Docker's builder natively clones a git context: ``<repo>#<ref>:<subdir>``.
    We encode ref (branch/tag/sha) and build_context (subdirectory) into that
    fragment so the deploy-time build clones exactly the right tree — reusing
    the REAL docker build mechanism rather than faking a clone here.
    """
    context = str(repo_url).strip()
    ref_part = (ref or "").strip()
    sub_part = (build_context or "").strip().strip("/")
    if ref_part or sub_part:
        context = f"{context}#{ref_part}"
        if sub_part:
            context = f"{context}:{sub_part}"
    return context


def service_from_git(
    repo_url: str,
    ref: Optional[str] = None,
    build_context: Optional[str] = None,
    **cfg: Any,
) -> ServiceFragment:
    """Git-repo source — a long-lived service built from source at deploy time.

    Emits a compose ``build:`` fragment whose context is the git repo (optionally
    pinned to ``ref`` and a ``build_context`` subdir). The image is built by the
    DEPLOY path, NOT here — see the module "Build-farm boundary" note; the
    fragment is marked with ``BUILD_BOUNDARY_KEY``. No smoke-guard, no data-io:
    the built image runs its own command under ``restart: unless-stopped``.
    """
    if not repo_url or not str(repo_url).strip():
        raise ValueError("service_from_git requires a non-empty repo_url")
    build: Dict[str, Any] = {
        "context": _git_context(repo_url, ref, build_context),
        BUILD_BOUNDARY_KEY: "git",
    }
    dockerfile = cfg.get("dockerfile")
    if dockerfile:
        build["dockerfile"] = str(dockerfile)
    return _fragment(build=build, **_common_cfg(cfg))


# --- C.2 script (packaged into a long-lived service image) --------------------
def _script_run_cmd(filename: str) -> str:
    """The long-lived launch command for a packaged script, by extension."""
    lower = filename.lower()
    if lower.endswith(".py"):
        return f"python /app/{filename}"
    if lower.endswith((".sh", ".bash")):
        return f"sh /app/{filename}"
    if lower.endswith(".js") or lower.endswith(".mjs"):
        return f"node /app/{filename}"
    # No known extension → execute directly (must be executable / have a shebang).
    return f"/app/{filename}"


def _default_script_filename(script: str) -> str:
    """Pick a filename from the script's shebang when none was supplied."""
    first = (script or "").lstrip().splitlines()[0] if (script or "").strip() else ""
    if first.startswith("#!"):
        if "python" in first:
            return "script.py"
        if "node" in first:
            return "script.js"
        if "bash" in first or "/sh" in first:
            return "script.sh"
    return "script.sh"


def service_from_script(
    script: str,
    base_image: str,
    filename: Optional[str] = None,
    **cfg: Any,
) -> ServiceFragment:
    """Script source — package a raw script into a long-lived service image.

    Emits a compose ``build:`` fragment carrying a synthesized inline Dockerfile
    (``FROM base_image`` → COPY the script → run it) plus the script body and
    filename under namespaced keys, so the DEPLOY path can materialize the build
    context and build the image (build-farm boundary; see module note). The
    resulting service runs the script under its own command with
    ``restart: unless-stopped`` — no smoke-guard, no data-io contract.
    """
    if not script or not str(script).strip():
        raise ValueError("service_from_script requires a non-empty script")
    if not base_image or not str(base_image).strip():
        raise ValueError("service_from_script requires a non-empty base_image")

    fname = (filename or "").strip() or _default_script_filename(script)
    base = str(base_image).strip()
    run_cmd = _script_run_cmd(fname)
    dockerfile_inline = (
        f"FROM {base}\n"
        f"WORKDIR /app\n"
        f"COPY {fname} /app/{fname}\n"
        f"CMD {run_cmd}\n"
    )
    build: Dict[str, Any] = {
        # Deploy-path builder writes `script` to `<context>/<filename>` and builds
        # `dockerfile_inline` against it. `context: "."` is a placeholder the
        # materializer replaces with the real temp context dir.
        "context": ".",
        "dockerfile_inline": dockerfile_inline,
        "x-mod-script-filename": fname,
        "x-mod-script-body": script,
        BUILD_BOUNDARY_KEY: "script",
    }
    frag_cfg = _common_cfg(cfg)
    # The image's CMD already launches the script; only honor an explicit command
    # override if the operator supplied one.
    return _fragment(build=build, **frag_cfg)


# --- C.3 static / live website (nginx, stays live) ---------------------------
def service_from_static_site(
    site_source: str,
    site_ref: str,
    **cfg: Any,
) -> ServiceFragment:
    """Static / live website source — a long-lived nginx service.

    Two source modes:

    * ``"upload"`` — ``site_ref`` is a MinIO object key for an uploaded bundle.
      Emits an ``image: nginx:alpine`` service; the deploy path fetches the
      bundle and drops it into the nginx doc-root. No build farm needed, so this
      mode carries NO build boundary marker.
    * ``"repo"`` — ``site_ref`` is a git repo URL for a (pre-built) static site.
      Emits a compose ``build:`` fragment (git context + a synthesized nginx
      Dockerfile) that the DEPLOY path builds — build-farm boundary, marked with
      ``BUILD_BOUNDARY_KEY``.

    Either way the service is nginx running its own long-lived command under
    ``restart: unless-stopped``, listening on container port 80 (overridable via
    ``cfg['ports']``) — no smoke-guard, no data-io contract.
    """
    if site_source not in ("upload", "repo"):
        raise ValueError(
            f"service_from_static_site: site_source must be 'upload' or 'repo', "
            f"got {site_source!r}"
        )
    if not site_ref or not str(site_ref).strip():
        raise ValueError("service_from_static_site requires a non-empty site_ref")

    frag_cfg = _common_cfg(cfg)
    # Default the nginx container port unless the operator set ports explicitly.
    frag_cfg.setdefault("ports", [80])
    ref = str(site_ref).strip()

    if site_source == "upload":
        # nginx serves the uploaded bundle the deploy path stages into the
        # doc-root. The object key rides along so the deploy path knows what
        # to fetch. No build → no boundary marker.
        frag_cfg["environment"] = {
            **frag_cfg.get("environment", {}),
            "MOD_SITE_SOURCE": "upload",
            "MOD_SITE_OBJECT_KEY": ref,
        }
        return _fragment(image=NGINX_IMAGE, **frag_cfg)

    # site_source == "repo": build an nginx image that serves the repo's static
    # tree. The synthesized Dockerfile copies the cloned repo into the doc-root;
    # the deploy path builds it (build-farm boundary).
    dockerfile_inline = (
        f"FROM {NGINX_IMAGE}\n"
        f"COPY . {NGINX_DOC_ROOT}\n"
    )
    build: Dict[str, Any] = {
        "context": _git_context(ref, cfg.get("ref"), cfg.get("build_context")),
        "dockerfile_inline": dockerfile_inline,
        BUILD_BOUNDARY_KEY: "site-repo",
    }
    dockerfile = cfg.get("dockerfile")
    if dockerfile:
        # Operator brought their own build (e.g. a node build step producing
        # /dist) — use it instead of the copy-only default.
        build["dockerfile"] = str(dockerfile)
        build.pop("dockerfile_inline", None)
    return _fragment(build=build, **frag_cfg)
