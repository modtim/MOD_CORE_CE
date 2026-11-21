"""Deterministic mode-aware BuildPlan renderer (wizard-rebuild §3e).

`render_artifacts(plan)` is the single function that turns a
BuildPlan into the three Albedo-pattern artifacts:

    {
      "dockerfile":       "<Dockerfile body>",
      "entrypoint_sh":    "<entrypoint script>",
      "build_and_push_sh": "<build script>",
    }

Properties guaranteed:

  - **Deterministic**: same plan → byte-identical output. No LLM
    in the rendering path. Fixture tests in
    test_build_plan_renderer.py pin the bytes.
  - **Mode-aware**: dispatches on `plan.mode` internally. One
    public function, one canonical artifact, mode-specific
    rendering encapsulated.
  - **No LLM**: the entire renderer is pure templating + light
    string assembly. Extraction (the LLM-driven step) happens in
    extractors/<mode>.py.
  - **Lint-clean**: output passes existing utils.dockerfile_lint
    without modification. Linter rules (USER-root-final, etc.)
    are observed by the templates.

When adding a new mode:
  1. Add the mode to `ALL_MODES` in types.py.
  2. Add a `_render_<mode>(plan)` private function below.
  3. Add a case to the `render_artifacts` dispatch table.
  4. Add a per-mode fixture test in test_build_plan_renderer.py.
"""
from __future__ import annotations

import base64
import json
import shlex
from typing import Any, Optional

from .types import BuildPlan
from utils.registries import platform_image


# ---------------------------------------------------------------------------
# Curated base-image matrix
# ---------------------------------------------------------------------------

# (language, cuda_version) → curated base image ref. cuda_version=None
# means CPU variant. The renderer picks the exact match if present,
# falls back to nearest version (recorded in weak_fields) otherwise.
#
# Adding a new base: add the row here. Tests in
# test_build_plan_renderer.py pin specific renderings to specific
# bases, so a careless rename here surfaces in test_render_python_cuda_*
# rather than at run time.
TEMPLATE_BASE_MATRIX: dict[tuple[str, Optional[str]], str] = {
    ("python", None):   platform_image("base-python:3.11"),
    # Only the 12.4 CUDA tag is currently published to platform harbor.
    # 11.8 + 12.1 entries were removed 2026-05-23 after `docker manifest
    # inspect` returned "not found" for both. pick_base's nearest-CUDA
    # fallback will route any cuda_version request to 12.4.
    ("python", "12.4"): platform_image("base-cuda:12.4-python3.11"),
    ("node",   None):   platform_image("base-node:20"),
    ("go",     None):   platform_image("base-go:1.22"),
    ("rust",   None):   platform_image("base-rust:1.78"),
    ("java",   None):   platform_image("base-java:21"),
    ("shell",  None):   platform_image("base-alpine:3.19"),
    # "container" is the special pseudo-language Image-mode uses
    # when we can't introspect the base image; no curated default
    # because Image mode uses `plan.from_image_ref` directly.
}


def pick_base(language: str, cuda_version: Optional[str]) -> str:
    """Look up the base image for a (language, cuda_version) pair.

    Exact match preferred. If `cuda_version` is set but absent from
    the matrix, we fall back to the closest available CUDA major
    (e.g., 12.2 → 12.1 by major.minor); caller can detect this by
    comparing the returned base ref to the requested version.

    Raises:
      KeyError if the language is unknown — extractor should have
      caught this at extract time.
    """
    if (language, cuda_version) in TEMPLATE_BASE_MATRIX:
        return TEMPLATE_BASE_MATRIX[(language, cuda_version)]
    if cuda_version is not None:
        # Try other CUDA versions for the same language, prefer the
        # numerically-closest. Falls back to CPU base last.
        cuda_options = [
            (lang, cv) for (lang, cv) in TEMPLATE_BASE_MATRIX
            if lang == language and cv is not None
        ]
        if cuda_options:
            def _dist(opt):
                _, cv = opt
                try:
                    return abs(float(cv) - float(cuda_version))
                except ValueError:
                    return float("inf")
            best = min(cuda_options, key=_dist)
            return TEMPLATE_BASE_MATRIX[best]
        # No CUDA bases for this language → CPU.
    if (language, None) in TEMPLATE_BASE_MATRIX:
        return TEMPLATE_BASE_MATRIX[(language, None)]
    raise KeyError(f"No curated base for language={language!r}, cuda={cuda_version!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_artifacts(plan: BuildPlan) -> dict[str, Any]:
    """Render a BuildPlan into the Albedo-pattern artifacts plus
    node_metadata with the plan's port schema.

    Single public entry point. Dispatches on `plan.mode` internally.
    """
    return {
        "dockerfile": render_dockerfile(plan),
        "entrypoint_sh": render_entrypoint(plan),
        "build_and_push_sh": render_build_and_push(plan),
        "node_metadata": render_node_metadata(plan),
    }


def render_node_metadata(plan: BuildPlan) -> dict[str, Any]:
    """Return the port schema from *plan* as a plain dict.

    Every plan, regardless of whether it carries ports, gets the same
    flat structure so callers can always expect the keys to exist.

    Only non-None fields are included so the dict stays minimal and
    matches the downstream UI / register_node expectations.
    """

    def _port_dict(p: "IOPort") -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": p.name,
            "type": p.type,
            "required": p.required,
        }
        if p.default is not None:
            d["default"] = p.default
        if p.description is not None:
            d["description"] = p.description
        return d

    return {
        "input_ports": [_port_dict(p) for p in plan.input_ports],
        "output_ports": [_port_dict(p) for p in plan.output_ports],
        "total_input_ports": len(plan.input_ports),
        "total_output_ports": len(plan.output_ports),
    }


def render_dockerfile(plan: BuildPlan) -> str:
    """Render just the Dockerfile body. Mode-dispatched."""
    if plan.mode == "image":
        return _render_image_dockerfile(plan)
    if plan.mode == "dockerfile":
        return _render_dockerfile_mode_dockerfile(plan)
    if plan.mode in ("git", "script", "describe-codegen"):
        return _render_standard_dockerfile(plan)
    raise ValueError(f"Unknown BuildPlan mode: {plan.mode!r}")


def render_entrypoint(plan: BuildPlan) -> str:
    """Render the entrypoint script. Mode-dispatched.

    For now, defers to the existing io_adapter templates that
    already handle (input_type, output_type) pair selection. Future
    commits can move template selection here once all modes have
    matured.
    """
    # Read the first input_port + output_port as the type pair —
    # multi-port plans use the first declared port for template
    # selection; templates that handle multi-port show up in v2.
    in_type = plan.input_ports[0].type if plan.input_ports else "image"
    out_type = plan.output_ports[0].type if plan.output_ports else "image"
    from node_builder.io_adapter.image_mode import build_image_mode_entrypoint
    ref = plan.from_image_ref or "<unknown>"
    return build_image_mode_entrypoint(in_type, out_type, ref)


def render_build_and_push(plan: BuildPlan) -> str:
    """Render the build_and_push.sh script.

    Currently delegates to the existing io_adapter helper which
    just exports IMAGE_NAME + ADAPTER_MODE — the actual build
    happens in the build-task container.
    """
    from node_builder.io_adapter.image_mode import (
        build_image_mode_build_and_push_sh,
    )
    image_name = _derive_image_name(plan)
    return build_image_mode_build_and_push_sh(image_name)


def _derive_image_name(plan: BuildPlan) -> str:
    """Pull the image_name out of the input_ports or fall back to a
    slug. Used for build_and_push.sh's IMAGE_NAME export. The
    `/build-and-register-node` endpoint also derives the same name
    server-side — they need to agree."""
    # If the plan was produced by the Image extractor it carries
    # `from_image_ref`; we slug from that. Otherwise the extractor
    # left a name on the first input port's description (cheap
    # carrier).
    if plan.from_image_ref:
        basename = plan.from_image_ref.split(":")[0].split("/").pop() if False else (
            plan.from_image_ref.split(":")[0].split("/")[-1]
        )
        return basename or "node"
    return "node"


# ---------------------------------------------------------------------------
# Image mode — thin adapter wrapping the user's docker ref
# ---------------------------------------------------------------------------

def _render_image_dockerfile(plan: BuildPlan) -> str:
    """Emit the thin adapter Dockerfile for Image-mode plans.

    Shape:
        FROM <user-ref-or-digest>
        USER root
        RUN mkdir -p /data/input /data/output && chmod 1777 ...
        COPY entrypoint.sh /entrypoint.sh
        RUN chmod +x /entrypoint.sh
        USER <runtime_user>     -- only when base declares a non-root user
        ENTRYPOINT ["/entrypoint.sh"]

    The `USER root` upfront is the fix for non-root base images
    (vault, nginx, etc.) — without it, the mkdir/chmod on /data
    fails because the base inherited a user that lacks write perms.
    For root-default images (python:3.11-slim, ubuntu, etc.) the
    upfront `USER root` is a no-op.

    `runtime_user` comes from the §3 chunk-2 extractor via
    `utils.image_ref_preflight.resolve_runtime_user()` — peeks at
    the base image's `Config.User` field. Empty string / None
    means root, no trailing USER directive needed.

    `from_digest` takes precedence when set — using a digest pin
    is the wizard-rebuild-§3d "version pinning for reproducible
    rebuilds" requirement.
    """
    if not plan.from_image_ref:
        raise ValueError("Image-mode plan requires from_image_ref")

    # Pinned-digest reference takes precedence (reproducibility).
    if plan.from_digest:
        # Digest is "sha256:abc..." — we glue with `@` per OCI spec.
        from_line = (
            f"FROM {plan.from_image_ref.split(':')[0]}@{plan.from_digest}"
        )
    else:
        from_line = f"FROM {plan.from_image_ref}"

    lines: list[str] = [
        from_line,
        "USER root",
        "RUN mkdir -p /data/input /data/output && "
        "chmod 1777 /data/input /data/output",
        "COPY entrypoint.sh /entrypoint.sh",
        "RUN chmod +x /entrypoint.sh",
    ]

    # If the base image declares a non-root runtime user, restore it
    # at the end so the container still runs as the base intended
    # (vault user for vault, nginx for nginx, etc.). Empty / None /
    # root-equivalents fall through with no trailing USER.
    if plan.runtime_user:
        lines.append(f"USER {plan.runtime_user}")

    lines.append('ENTRYPOINT ["/entrypoint.sh"]')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Dockerfile mode — wrap user's Dockerfile with I/O entrypoint patch
# ---------------------------------------------------------------------------

def _render_dockerfile_mode_dockerfile(plan: BuildPlan) -> str:
    """Dockerfile-mode renderer (wizard-rebuild §3 chunk 2).

    The user pasted an authoritative Dockerfile body. We pass it
    through verbatim, then append the I/O setup + entrypoint patch +
    non-root USER mod at the end so the resulting image:
      - has /data/input + /data/output world-writable
      - runs as the `mod` user at runtime (satisfies the
        USER-root-final lint rule)
      - uses our entrypoint.sh as ENTRYPOINT regardless of what the
        user's Dockerfile declared

    The user's body comes first so all their RUN / COPY / ENV / ARG
    / FROM directives apply normally. The wrapper appends; never
    mutates inside the user's body.
    """
    if not plan.passthrough_dockerfile:
        raise ValueError(
            "Dockerfile-mode plan requires passthrough_dockerfile"
        )

    user_body = plan.passthrough_dockerfile.rstrip() + "\n"

    # Append wrapper. We deliberately keep it minimal so a user whose
    # Dockerfile already does some of these things doesn't get a
    # double-up — chmod and mkdir are idempotent; the trailing
    # ENTRYPOINT just replaces whatever ENTRYPOINT the user set
    # (Docker uses the last ENTRYPOINT directive).
    wrapper = (
        "\n"
        "# wizard-rebuild §3 — MOD I/O adapter wrapper. Appended\n"
        "# AFTER the user's Dockerfile body so their RUN/COPY/ENV\n"
        "# directives flow through normally. The /data/input and\n"
        "# /data/output convention + non-root final USER + our\n"
        "# entrypoint.sh are wizard-platform contracts.\n"
        "USER root\n"
        "RUN mkdir -p /data/input /data/output && "
        "chmod 1777 /data/input /data/output\n"
        "COPY entrypoint.sh /entrypoint.sh\n"
        "RUN chmod +x /entrypoint.sh\n"
        # Idempotent uid-10001 mod user. Curated mod_core bases
        # already provision it (useradd no-ops); arbitrary public
        # bases (python:3.11-slim, ubuntu, debian, alpine) don't,
        # and the trailing `USER mod` then trips docker exec with
        # `no matching entries in passwd file` (manual-test repro
        # 2026-05-23 spec=50 Dockerfile mode `FROM python:3.11-slim`).
        # Handles both glibc (useradd) and BusyBox (adduser) bases.
        "RUN id -u mod >/dev/null 2>&1 || "
        "useradd -m -u 10001 mod 2>/dev/null || "
        "adduser -D -u 10001 mod\n"
        "USER mod\n"
        f"ENTRYPOINT {json.dumps(plan.entrypoint)}\n"
    )
    return user_body + wrapper


# ---------------------------------------------------------------------------
# Standard mode — git / script / describe-codegen
# ---------------------------------------------------------------------------

def _render_standard_dockerfile(plan: BuildPlan) -> str:
    """The mainline renderer for git/script/describe-codegen modes.

    Shape (assembled in stable order):
        FROM <base from matrix or from_image_ref>
        USER root
        WORKDIR <plan.workdir>
        RUN apt-get update && apt-get install -y --no-install-recommends \
            <plan.system_deps> && rm -rf /var/lib/apt/lists/*
        COPY . <plan.workdir>
        RUN <each install_step>
        ENV <each env_var with default>
        EXPOSE <each port>
        USER mod
        ENTRYPOINT <exec-form plan.entrypoint>

    NOTE: stub for this chunk. Git/Script/Codegen extractors land
    in later §3 chunks. We define the renderer now so test fixtures
    can pin its shape against frozen plans.
    """
    # Pick base via matrix; digest pin overrides if present.
    if plan.from_image_ref:
        base = plan.from_image_ref
    else:
        base = pick_base(plan.language, plan.cuda_version)
    if plan.from_digest:
        base = f"{base.split(':')[0]}@{plan.from_digest}"

    lines: list[str] = [f"FROM {base}", "USER root"]

    if plan.workdir:
        lines.append(f"WORKDIR {plan.workdir}")

    if plan.system_deps:
        # Collapse to one RUN with apt-cache cleanup so the lint
        # rule doesn't flag separate apt-get calls.
        pkgs = " ".join(sorted(plan.system_deps))  # sort for determinism
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + pkgs
            + " && rm -rf /var/lib/apt/lists/*"
        )

    # Script-mode plans embed the body directly via base64 — no
    # build-context shenanigans, no HEREDOC sentinel collisions. Any
    # bytes the user pasted survive the round-trip exactly.
    # When script_body is unset, we COPY the build context as usual
    # (git mode + describe-codegen will eventually wire that up).
    if plan.script_body and plan.script_filename:
        encoded = base64.b64encode(plan.script_body.encode("utf-8")).decode("ascii")
        script_path = f"{plan.workdir.rstrip('/')}/{plan.script_filename}"
        lines.append(
            f"RUN mkdir -p {plan.workdir} && echo {shlex.quote(encoded)} "
            f"| base64 -d > {script_path}"
        )
    else:
        lines.append(f"COPY . {plan.workdir}")

    for step in plan.install_steps:
        lines.append(f"RUN {step}")

    for env in plan.env_vars:
        if env.default is not None:
            # shlex.quote handles paths/special chars deterministically.
            lines.append(f"ENV {env.name}={shlex.quote(env.default)}")

    # EXPOSE ports for documentation (Docker doesn't enforce).
    for port in sorted(plan.ports):
        lines.append(f"EXPOSE {port}")

    # ENTRYPOINT defaults to ["/entrypoint.sh"]; without an explicit
    # COPY to that path the entrypoint lives only at {workdir}/
    # entrypoint.sh (via the `COPY . {workdir}` above) and docker run
    # exits 127 (manual-test repro 2026-05-23 spec=59 git mode
    # psf/requests: 5 LLM repairs all chasing a missing /entrypoint.sh).
    # Image-mode and Dockerfile-mode renderers already do this.
    lines.append("COPY entrypoint.sh /entrypoint.sh")
    lines.append("RUN chmod +x /entrypoint.sh")

    # Switch to non-root user before entrypoint. Curated mod_core
    # bases already provision uid 10001 as `mod`; when a user
    # overrides with an arbitrary base (custom wrap_base_image on
    # script/git mode), the user may be missing — the idempotent
    # useradd below mirrors the Dockerfile-mode renderer.
    lines.append(
        "RUN id -u mod >/dev/null 2>&1 || "
        "useradd -m -u 10001 mod 2>/dev/null || "
        "adduser -D -u 10001 mod"
    )
    lines.append("USER mod")

    # Entrypoint exec form — JSON array.
    lines.append(f"ENTRYPOINT {json.dumps(plan.entrypoint)}")

    return "\n".join(lines) + "\n"
