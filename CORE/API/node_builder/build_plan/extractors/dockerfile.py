"""Dockerfile-mode BuildPlan extractor (wizard-rebuild §3 chunk 2).

When a user pastes a Dockerfile body and submits, we produce a
BuildPlan with `mode="dockerfile"` and `passthrough_dockerfile=
<user's body>`. The renderer wraps the body with:

  - I/O directory setup (chmod 1777 on /data/input + /data/output)
  - Non-root USER mod injection at the end (satisfies the
    USER-root-final lint rule)
  - COPY entrypoint.sh + ENTRYPOINT directives

The user's COPY / RUN / ENV / WORKDIR / app-level Dockerfile body
flows through verbatim. No LLM call — the user gave us the
authoritative recipe.

Parsing pulls a few inferred fields out of the user's body (FROM
line for `from_image_ref`, EXPOSE for `ports`, ENV directives for
`env_vars`) so the View Build Plan UI has something to render
beyond just "(passthrough)". These inferred fields are ADVISORY —
they don't drive the rendered Dockerfile; the user's body is
authoritative.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ..types import (
    EXTRACTOR_VERSION,
    BuildPlan,
    EnvVar,
    IOPort,
    default_ports_for_types,
)

logger = logging.getLogger(__name__)


# Match the FIRST FROM directive in the Dockerfile body. Multi-stage
# builds have multiple FROMs; the first is the build-stage base.
# We allow `--platform=...` flags between FROM and the image ref.
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<ref>\S+)(?:\s+AS\s+\S+)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Match EXPOSE directives. Docker spec allows multiple ports per
# line (`EXPOSE 80 443/tcp`). We accept the simple form here.
_EXPOSE_RE = re.compile(
    r"^\s*EXPOSE\s+(?P<ports>[\d\s/a-z]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Match `ENV KEY=VALUE` or `ENV KEY VALUE`. We don't decode escape
# sequences — just grab the name + raw default.
_ENV_RE = re.compile(
    r"^\s*ENV\s+(?P<name>[A-Z_][A-Z0-9_]*)\s*[=\s]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Match WORKDIR. The wizard's adapter normally sets WORKDIR /data/input
# but the user's Dockerfile may have its own.
_WORKDIR_RE = re.compile(
    r"^\s*WORKDIR\s+(?P<dir>\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# COPY directives that reference paths not bundled with the wizard
# submission. Dockerfile mode currently embeds only the Dockerfile body;
# `COPY . .` / `COPY package.json` / `COPY src/ /app` all fail at build
# time because the build context has no source files alongside the
# generated Dockerfile. Reject up front with a clear error rather than
# burning credits + 5 build-task containers per variant on a doomed
# build (manual-test repro 2026-05-22 spec=38 Node.js App: 7 outer-
# repair attempts then a thrash exit, plus 4 variant builds all with
# the same `npm error path /app/package.json ENOENT` failure).
_COPY_DIRECTIVE_RE = re.compile(
    r"^\s*COPY\s+(?P<args>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def detect_unbundled_copy_sources(dockerfile_body: str) -> list[str]:
    """Return the list of COPY source-paths in `dockerfile_body` that
    reference files not bundled with the wizard. Empty list when the
    Dockerfile is self-contained.

    Allows:
      - `COPY --from=<stage>` (multi-stage builds; no local files needed)
      - `COPY entrypoint.sh /entrypoint.sh` (the wizard's own wrapper
        appends this; user-supplied is still flagged)

    Flags everything else: `COPY . .`, `COPY package.json /app/`,
    `COPY src/ /app/src/`, `COPY *.py /app/`, etc.
    """
    if not dockerfile_body:
        return []
    flagged: list[str] = []
    for m in _COPY_DIRECTIVE_RE.finditer(dockerfile_body):
        args = m.group("args").strip()
        # --from=<stage> means copy from another build stage — no local
        # files involved.
        if args.lower().startswith("--from="):
            continue
        # Strip leading flags (--chown=, --chmod=, --link, etc.) so
        # we can read the actual source path.
        tokens = args.split()
        idx = 0
        while idx < len(tokens) and tokens[idx].startswith("--"):
            # `--flag=value` is one token; bare `--flag value` is two.
            if "=" in tokens[idx]:
                idx += 1
            else:
                idx += 2
        srcs = tokens[idx:-1] if len(tokens) - idx >= 2 else []
        for src in srcs:
            # Strip quotes if present.
            s = src.strip("\"'")
            if not s:
                continue
            flagged.append(s)
    return flagged


def extract_dockerfile_plan(
    *,
    dockerfile_body: str,
    node_name: str,
    input_type: str = "image",
    output_type: str = "image",
    container_env: Optional[dict[str, str]] = None,
) -> BuildPlan:
    """Build a BuildPlan for a Dockerfile-mode submission.

    Args:
      dockerfile_body: the raw Dockerfile text the user pasted.
      node_name: Node display name.
      input_type / output_type: legacy singleton port types.
      container_env: optional env-var overrides from the wizard's
        Configure step. Merged with anything inferred from the
        body's ENV directives (wizard overrides win on conflict).

    Returns:
      A BuildPlan with `mode="dockerfile"`, `passthrough_dockerfile`
      holding the user's body, and a sprinkling of inferred fields
      for UI display.

    No LLM call. No network call.
    """
    if not dockerfile_body or not dockerfile_body.strip():
        raise ValueError("dockerfile_body must be non-empty")

    in_ports, out_ports = default_ports_for_types(input_type, output_type)

    # Inferred fields for UI display + minor renderer hints.
    inferred_from = _extract_first_from(dockerfile_body)
    inferred_ports = _extract_exposes(dockerfile_body)
    inferred_envs = _extract_envs(dockerfile_body)
    inferred_workdir = _extract_workdir(dockerfile_body)

    # Merge inferred ENVs with wizard-supplied container_env. Wizard
    # values win — they're explicitly set by the user, not parsed
    # heuristically.
    env_var_map: dict[str, EnvVar] = {}
    for name, default in inferred_envs.items():
        env_var_map[name] = EnvVar(name=name, required=True, default=default)
    if container_env:
        for name, default in container_env.items():
            if not name:
                continue
            env_var_map[name] = EnvVar(
                name=name,
                required=True,
                default=str(default) if default is not None else None,
            )

    plan = BuildPlan(
        mode="dockerfile",
        # Language can't be inferred from a Dockerfile reliably —
        # the FROM line might be `alpine:3.20` or `nvidia/cuda:...`.
        # Mark as "container" (the same special value Image mode uses).
        language="container",
        package_manager="none",
        workdir=inferred_workdir or "/data/input",
        entrypoint=["/entrypoint.sh"],

        # User's Dockerfile is authoritative. Renderer wraps with I/O.
        passthrough_dockerfile=dockerfile_body,
        from_image_ref=inferred_from,

        input_ports=in_ports,
        output_ports=out_ports,
        env_vars=list(env_var_map.values()),
        ports=list(inferred_ports),

        # Dockerfile mode is high confidence — the user supplied the
        # authoritative recipe. The only uncertainty is whether the
        # entrypoint template fits their image, which test-and-repair
        # can iterate on if needed.
        confidence=0.90,
        extractor_version=EXTRACTOR_VERSION,
    )
    return plan


# ---------------------------------------------------------------------------
# Body-parsing helpers
# ---------------------------------------------------------------------------

def _extract_first_from(body: str) -> Optional[str]:
    m = _FROM_RE.search(body)
    return m.group("ref") if m else None


def _extract_exposes(body: str) -> list[int]:
    """Pull EXPOSE directives. Each directive can list multiple ports
    (`EXPOSE 80 443/tcp 8080`); we flatten to a sorted unique list."""
    ports: set[int] = set()
    for m in _EXPOSE_RE.finditer(body):
        for tok in m.group("ports").split():
            # Strip protocol suffix if present (e.g. `443/tcp`).
            port_str = tok.split("/", 1)[0]
            try:
                port = int(port_str)
            except ValueError:
                continue
            if 1 <= port <= 65535:
                ports.add(port)
    return sorted(ports)


def _extract_envs(body: str) -> dict[str, str]:
    """Pull ENV directives. We don't try to decode escape sequences
    or handle the multi-pair-on-one-line form (Docker allows
    `ENV K1=V1 K2=V2`); v1 only catches the single-pair shape, which
    is what most Dockerfiles use."""
    envs: dict[str, str] = {}
    for m in _ENV_RE.finditer(body):
        name = m.group("name")
        value = m.group("value").strip()
        # Strip surrounding quotes if present.
        if (value.startswith('"') and value.endswith('"')) or \
                (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        envs[name] = value
    return envs


def _extract_workdir(body: str) -> Optional[str]:
    m = _WORKDIR_RE.search(body)
    return m.group("dir") if m else None
