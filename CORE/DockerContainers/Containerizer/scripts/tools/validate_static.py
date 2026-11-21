"""Stage 8 — static validation of the four generated artifacts.

No LLM involvement. Catches the common failure modes that silently
break user-facing previews: broken Python metadata, broken shell syntax,
malformed Dockerfile directives, and cross-artifact inconsistencies
(env var declared in node metadata but never used, egress hosts that don't
match pip indices, I/O types that contradict entrypoint behaviour).

The orchestrator calls this after `generate_artifacts_v2.generate()`
returns. A non-empty `issues` list feeds Stage 11's refine pass.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model — one Issue per problem. Severity distinguishes between
# things the refine loop should try to fix (ERROR) vs things worth
# surfacing in the UI but not blocking (WARN).
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    severity: str                  # "error" | "warn"
    artifact: str                  # "dockerfile" | "entrypoint_sh" | "build_and_push_sh" | "node_metadata_py" | "cross"
    kind: str                      # short tag e.g. "python_syntax", "missing_from_tag"
    message: str                   # human-readable, sent back to the refine LLM
    line: Optional[int] = None


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def errors_by_artifact(self) -> dict[str, list[Issue]]:
        out: dict[str, list[Issue]] = {}
        for i in self.issues:
            if i.severity != "error":
                continue
            out.setdefault(i.artifact, []).append(i)
        return out

    def to_dict(self) -> dict:
        return {
            "issues": [
                {
                    "severity": i.severity, "artifact": i.artifact,
                    "kind": i.kind, "message": i.message, "line": i.line,
                }
                for i in self.issues
            ],
            "has_errors": self.has_errors,
        }


def validate(artifacts: dict, build_profile: Optional[dict] = None) -> ValidationReport:
    """Run every static check on an ArtifactResult.to_dict() output."""
    report = ValidationReport()

    _check_dockerfile(artifacts.get("dockerfile", ""), report, build_profile)
    _check_shell("entrypoint_sh", artifacts.get("entrypoint_sh", ""), report)
    _check_shell("build_and_push_sh", artifacts.get("build_and_push_sh", ""), report)
    _check_node_metadata(artifacts.get("node_metadata_py", ""), report)
    _check_cross_artifact(artifacts, report, build_profile)

    return report


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

_VALID_DIRECTIVES = {
    "FROM", "RUN", "CMD", "LABEL", "MAINTAINER", "EXPOSE", "ENV", "ADD",
    "COPY", "ENTRYPOINT", "VOLUME", "USER", "WORKDIR", "ARG",
    "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL",
}


def _check_dockerfile(text: str, report: ValidationReport, profile: Optional[dict]) -> None:
    if not text.strip():
        report.issues.append(Issue(severity="error", artifact="dockerfile",
                                   kind="empty", message="Dockerfile is empty."))
        return

    lines = text.splitlines()
    logical: list[tuple[int, str]] = []   # (lineno, normalised line)
    buf = ""
    start_line = 0
    for idx, raw in enumerate(lines, 1):
        s = raw.rstrip()
        # Skip comments and blank lines
        if not s.strip() or s.strip().startswith("#"):
            if buf:
                logical.append((start_line, buf))
                buf = ""
            continue
        # Handle line continuation
        if s.endswith("\\"):
            if not buf:
                start_line = idx
            buf += s[:-1].strip() + " "
            continue
        if buf:
            logical.append((start_line, buf + s.strip()))
            buf = ""
        else:
            logical.append((idx, s.strip()))

    if buf:
        logical.append((start_line, buf.rstrip()))

    saw_from = False
    has_entrypoint = False
    has_cmd = False
    has_expose = False
    workdir_seen: Optional[str] = None
    copy_sources: list[tuple[int, str]] = []

    for lineno, line in logical:
        m = re.match(r"^(\w+)\s+(.*)$", line)
        if not m:
            report.issues.append(Issue(severity="error", artifact="dockerfile",
                                       kind="bad_directive_line",
                                       message=f"Unparseable directive on line {lineno}: {line!r}",
                                       line=lineno))
            continue
        directive = m.group(1).upper()
        args = m.group(2).strip()

        if directive not in _VALID_DIRECTIVES:
            report.issues.append(Issue(severity="error", artifact="dockerfile",
                                       kind="unknown_directive",
                                       message=f"Unknown Dockerfile directive {directive!r} on line {lineno}.",
                                       line=lineno))
            continue

        if directive == "FROM":
            saw_from = True
            # Strip --platform=… prefix
            base = re.sub(r"--platform=\S+\s+", "", args).split(" AS ")[0].strip()
            if ":" not in base and "@" not in base and base != "scratch":
                report.issues.append(Issue(
                    severity="error", artifact="dockerfile",
                    kind="missing_from_tag",
                    message=f"FROM must pin a tag or digest — got {base!r} on line {lineno}. "
                            f"Use e.g. `FROM python:3.11-slim` not `FROM python`.",
                    line=lineno,
                ))

        if directive == "WORKDIR":
            workdir_seen = args
        if directive == "ENTRYPOINT":
            has_entrypoint = True
            # Must be exec-form referencing entrypoint.sh
            if "entrypoint.sh" not in args:
                report.issues.append(Issue(
                    severity="error", artifact="dockerfile",
                    kind="entrypoint_wrong",
                    message=f"ENTRYPOINT on line {lineno} must run entrypoint.sh "
                            f"(got {args!r}). Use: "
                            f'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]',
                    line=lineno,
                ))
        if directive == "CMD":
            has_cmd = True
        if directive == "EXPOSE":
            has_expose = True
        if directive == "COPY":
            parts = args.split()
            if len(parts) >= 2:
                copy_sources.append((lineno, parts[0]))

    if not saw_from:
        report.issues.append(Issue(severity="error", artifact="dockerfile",
                                   kind="no_from",
                                   message="Dockerfile has no FROM directive."))
    if workdir_seen not in ("/data", "/data/"):
        report.issues.append(Issue(
            severity="warn", artifact="dockerfile", kind="workdir_not_data",
            message=f"WORKDIR should be /data (got {workdir_seen!r}). "
                    f"MOD nodes mount /data/input and /data/output.",
        ))
    if not has_entrypoint:
        report.issues.append(Issue(
            severity="error", artifact="dockerfile", kind="no_entrypoint",
            message="Dockerfile has no ENTRYPOINT. MOD nodes must run "
                    "entrypoint.sh (the generator-supplied script).",
        ))
    if has_cmd:
        report.issues.append(Issue(
            severity="error", artifact="dockerfile", kind="has_cmd",
            message="Dockerfile declares CMD. MOD nodes use ENTRYPOINT only "
                    "— remove the CMD line.",
        ))
    if has_expose:
        report.issues.append(Issue(
            severity="warn", artifact="dockerfile", kind="has_expose",
            message="Dockerfile has EXPOSE. MOD nodes are batch-style "
                    "(no network server); EXPOSE is unused.",
        ))

    # COPY sources — every referenced file except entrypoint.sh /
    # build-and-push.sh is expected to exist in the repo. We can't
    # check repo paths from here (the artifact is context-free at
    # validation time), but we can flag obviously-bogus sources like
    # paths with spaces or Windows separators.
    for lineno, src in copy_sources:
        if "\\" in src:
            report.issues.append(Issue(
                severity="error", artifact="dockerfile", kind="bad_copy_src",
                message=f"COPY source uses backslash {src!r} on line {lineno}. "
                        f"Dockerfiles use forward slashes.",
                line=lineno,
            ))


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

def _check_shell(label: str, text: str, report: ValidationReport) -> None:
    if not text.strip():
        report.issues.append(Issue(severity="error", artifact=label,
                                   kind="empty",
                                   message=f"{label} is empty."))
        return

    # Shebang
    first = text.splitlines()[0] if text.splitlines() else ""
    if not first.startswith("#!"):
        report.issues.append(Issue(
            severity="error", artifact=label, kind="no_shebang",
            message=f"{label} is missing a shebang. First line must be "
                    f"`#!/bin/bash` (entrypoint) or `#!/usr/bin/env bash` (build).",
        ))

    # `bash -n` via a temp file — catches syntax errors fast.
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as tf:
        tf.write(text); path = tf.name
    try:
        r = subprocess.run(["bash", "-n", path], capture_output=True, timeout=10)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace").strip()
            report.issues.append(Issue(
                severity="error", artifact=label, kind="bash_syntax",
                message=f"bash -n failed: {err}",
            ))
    except subprocess.TimeoutExpired:
        report.issues.append(Issue(
            severity="warn", artifact=label, kind="bash_timeout",
            message=f"bash -n timed out (script is valid but slow to parse).",
        ))
    finally:
        Path(path).unlink(missing_ok=True)

    # entrypoint-specific checks
    if label == "entrypoint_sh":
        if "/data/input/" not in text and "/data/input" not in text:
            report.issues.append(Issue(
                severity="warn", artifact=label, kind="no_input_check",
                message="entrypoint.sh doesn't reference /data/input/. "
                        "MOD nodes are supposed to read inputs from there.",
            ))
        if "/data/output" not in text:
            report.issues.append(Issue(
                severity="warn", artifact=label, kind="no_output_write",
                message="entrypoint.sh doesn't reference /data/output/. "
                        "MOD nodes are supposed to write results there.",
            ))
        if "set -e" not in text:
            report.issues.append(Issue(
                severity="error", artifact=label, kind="no_set_e",
                message="entrypoint.sh must `set -e` so errors fail the container.",
            ))


# ---------------------------------------------------------------------------
# node metadata .py
# ---------------------------------------------------------------------------

def _check_node_metadata(text: str, report: ValidationReport) -> None:
    if not text.strip():
        report.issues.append(Issue(severity="error", artifact="node_metadata_py",
                                   kind="empty",
                                   message="node metadata is empty."))
        return

    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        report.issues.append(Issue(
            severity="error", artifact="node_metadata_py", kind="python_syntax",
            message=f"SyntaxError line {e.lineno}: {e.msg}", line=e.lineno,
        ))
        return

    # Look for NODE_METADATA assignment
    node_meta: Optional[ast.Dict] = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "NODE_METADATA":
                    if isinstance(stmt.value, ast.Dict):
                        node_meta = stmt.value
                    break

    if node_meta is None:
        report.issues.append(Issue(
            severity="error", artifact="node_metadata_py",
            kind="no_node_metadata",
            message="node metadata must assign a NODE_METADATA dict at module top-level.",
        ))
        return

    # Extract keys
    keys: set[str] = set()
    for k in node_meta.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)

    required = {"name", "description", "input_type", "output_type",
                "env_vars", "allowed_egress"}
    missing = required - keys
    if missing:
        report.issues.append(Issue(
            severity="error", artifact="node_metadata_py", kind="missing_keys",
            message=f"NODE_METADATA missing required keys: {sorted(missing)}",
        ))


# ---------------------------------------------------------------------------
# Cross-artifact consistency
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\{\s*['\"]name['\"]\s*:\s*['\"]([A-Z_][A-Z0-9_]*)['\"]")
_PIP_INDEX_RE = re.compile(r"--index-url\s+https?://([^\s/]+)")
_APT_SOURCE_RE = re.compile(r"deb\s+https?://([^\s/]+)")
_HF_HUB_RE = re.compile(r"(huggingface\.co)", re.I)
_CURL_HOST_RE = re.compile(r"curl\s+[^\|]*?https?://([^/\s]+)")


def _check_cross_artifact(
    artifacts: dict, report: ValidationReport, profile: Optional[dict],
) -> None:
    """The metadata / Dockerfile / entrypoint must agree on env vars and egress."""
    dag = artifacts.get("node_metadata_py", "")
    dockerfile = artifacts.get("dockerfile", "")
    entrypoint = artifacts.get("entrypoint_sh", "")

    # --- env-var consistency: declared in node metadata but never referenced --
    declared_envs = set(_ENV_VAR_RE.findall(dag))
    # Strip the python `required` key name and such from false positives.
    declared_envs = {e for e in declared_envs
                     if e not in {"REQUIRED", "DEFAULT", "PURPOSE", "NAME"}}
    used_in_entry = set(re.findall(r"\$(?:\{)?([A-Z_][A-Z0-9_]*)(?:\})?", entrypoint))
    used_in_dockerfile = set(re.findall(r"\$(?:\{)?([A-Z_][A-Z0-9_]*)(?:\})?", dockerfile))
    used = used_in_entry | used_in_dockerfile
    for name in declared_envs:
        # Skip env vars that vLLM / CUDA use that the entrypoint may not.
        if name in {"PATH", "HOME", "USER", "PWD"}:
            continue
        if name not in used:
            report.issues.append(Issue(
                severity="warn", artifact="cross", kind="env_declared_unused",
                message=f"Metadata declares env var {name} but neither "
                        f"entrypoint.sh nor Dockerfile references it.",
            ))

    # --- egress sanity: hosts the Dockerfile pulls from should also
    #     appear in the metadata's allowed_egress list.
    dag_egress = set(re.findall(r"['\"]([A-Za-z0-9.-]+\.[A-Za-z]{2,})['\"]",
                                 dag))
    dockerfile_hosts = set()
    dockerfile_hosts.update(_PIP_INDEX_RE.findall(dockerfile))
    dockerfile_hosts.update(_APT_SOURCE_RE.findall(dockerfile))
    dockerfile_hosts.update(_CURL_HOST_RE.findall(dockerfile))
    if re.search(r"pip install", dockerfile) and not re.search(r"--index-url", dockerfile):
        dockerfile_hosts.add("pypi.org")
        dockerfile_hosts.add("files.pythonhosted.org")
    if _HF_HUB_RE.search(dockerfile) or _HF_HUB_RE.search(entrypoint):
        dockerfile_hosts.add("huggingface.co")
    for host in dockerfile_hosts:
        # Bail on obvious non-hosts that look like versions.
        if not re.search(r"[A-Za-z]", host):
            continue
        if host not in dag_egress:
            report.issues.append(Issue(
                severity="warn", artifact="cross", kind="egress_missing",
                message=f"Dockerfile/entrypoint fetch from {host} but "
                        f"Metadata allowed_egress doesn't include it.",
            ))
