"""Dockerfile sanitization + lint for wizard-generated BuildSpecs.

The wizard's `/generate-artifacts` path hands a Dockerfile through an
LLM. LLMs reliably produce template-shaped output that's syntactically
valid Docker but semantically broken — most painfully, a bare
`RUN pip install --no-cache-dir` with no packages after, which fails
the docker build with exit code 1 every single attempt (deterministic,
so the retry budget burns through and the run dies after ~3 attempts).

Run 8 (build_spec 3, "PDF→PNG using a Rust service") tripped this:
the LLM picked the Python base image but found no Python deps to
install, and shipped the empty pip line anyway.

Two surfaces live here:

  * `sanitize_dockerfile(text)` — auto-fixes. Strips empty package-
    install lines that would otherwise fail at build time. Returns
    `(cleaned_text, removed_lines: list[str])` so the caller can log
    what got auto-fixed. Safe to apply unconditionally — never
    removes a valid install line.

  * `lint_dockerfile(text)` — reports structural problems we can't
    auto-fix. Returns a list of `LintIssue` records the caller can
    surface to the user (or refuse the build for). Empty list means
    "ship it".

Both functions are pure string-level — no Dockerfile parser
dependency, no shell — so they're cheap to call on every wizard
submit and easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


# ---------------------------------------------------------------------
# sanitize: auto-fix empty package-install lines
# ---------------------------------------------------------------------

# Each entry matches an installer invocation whose argument list (the
# part after the installer keyword) is effectively empty — pip flags
# like `--no-cache-dir` / `--upgrade` / `-q` count as flags, not
# packages. If only flags are present, the install is a no-op that
# pip/apt/dnf reject with non-zero exit. Mirroring `build_farm.py`'s
# pattern (which already conditionally emits the line) but applied to
# whatever the LLM produced.

# Flags that don't carry an argument. If after stripping these the
# install has no remaining tokens, the line is empty-effective.
_PIP_FLAG_ONLY = re.compile(
    r"^\s*--?[\w-]+\s*$"
)
_PIP_FLAG_WITH_VALUE = re.compile(
    # `--index-url URL` / `-i URL` / `--extra-index-url URL`
    r"^\s*--?(?:index-url|extra-index-url|i|trusted-host|proxy|cert|"
    r"client-cert|cache-dir|find-links|f|no-cache-dir)(?:=\S+)?\s*$"
)


def _install_line_is_empty(tokens: List[str]) -> bool:
    """True iff none of the tokens look like a package name.

    Heuristic: any token that's a bare word (no leading `-`) and isn't
    the value of a key=value flag we already trimmed counts as a
    package. Empty if no such tokens remain.
    """
    saw_package = False
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            # Flag — does it eat the next token? Common offenders:
            # `-i URL`, `--index-url URL`, `--extra-index-url URL`,
            # `--find-links URL`, `--proxy URL`. The `=URL` form is
            # self-contained.
            if "=" not in tok and tok in (
                "-i", "--index-url", "--extra-index-url",
                "--find-links", "-f", "--proxy", "--cert", "--client-cert",
                "--trusted-host", "--cache-dir",
            ):
                skip_next = True
            continue
        # Token doesn't start with `-`. That's a package name (or a
        # `-r requirements.txt` value we already swallowed, or a URL
        # that's a flag value). Either way, treat as a real install.
        saw_package = True
        break
    return not saw_package


# Match `RUN [--mount=...] <installer> [args...]` lines. Captures:
#   group "lead"     — the RUN keyword + any flags (e.g. --mount=...)
#   group "installer"— the installer name token
#   group "args"     — everything after the installer (may be empty)
#
# We intentionally don't try to handle multi-line backslash
# continuations here. Empty-install lines in the wild are always
# single-line (the LLM doesn't continue an empty install with a `\`),
# and supporting multi-line would mean parsing shell which is out of
# scope for a lint check.
_INSTALL_LINE = re.compile(
    r"""^(?P<lead>\s*RUN(?:\s+--mount=\S+)*\s+)
        (?P<installer>pip3?|python\s+-m\s+pip|apt-get|apt|dnf|yum|microdnf|apk)
        (?:\s+(?P<args>.*))?\s*$""",
    re.VERBOSE,
)


def _is_install_arg_empty(installer: str, args: str) -> bool:
    """True iff the install arg list has no real packages.

    `installer` is the matched installer keyword. `args` is the
    raw post-installer text (may be None / empty). Per-installer:
      * pip / pip3 / python -m pip: tokenise, drop pip-style flags,
        check for remaining bare tokens. `install` itself is a
        subcommand and we expect it to be present.
      * apt-get / apt / dnf / yum / microdnf / apk: same idea, but
        the subcommand (`install`, `update`) leads. We only flag
        the case where the subcommand is `install` and no package
        tokens follow.
    """
    args = (args or "").strip()
    if not args:
        return True
    tokens = args.split()

    inst = installer.split()[0].lower()  # "pip" / "pip3" / "python"
    if inst in ("pip", "pip3") or installer.lower().startswith("python"):
        if "install" not in tokens:
            # Not an install invocation (e.g. `pip cache purge`).
            return False
        # Drop everything up to and including "install".
        i = tokens.index("install")
        rest = tokens[i + 1:]
        return _install_line_is_empty(rest)

    # apt / dnf / yum / microdnf / apk
    # apt-get install <packages> [-y …]
    # apk add <packages>
    if inst in ("apt", "apt-get", "dnf", "yum", "microdnf"):
        if "install" not in tokens:
            return False
        i = tokens.index("install")
        rest = tokens[i + 1:]
        # All remaining tokens that look like real packages?
        return all(
            tok.startswith("-") or tok in ("install",)
            for tok in rest
        )
    if inst == "apk":
        if "add" not in tokens:
            return False
        i = tokens.index("add")
        rest = tokens[i + 1:]
        return all(tok.startswith("-") for tok in rest)
    return False


def sanitize_dockerfile(text: str) -> Tuple[str, List[str]]:
    """Strip empty-package install lines from a Dockerfile.

    Returns (cleaned_text, removed_lines). `removed_lines` is the
    original strings so callers can log them — useful for debugging
    the LLM's output.

    Idempotent. Safe on an already-clean Dockerfile (returns text
    unchanged + empty list).
    """
    if not text:
        return text, []
    out_lines: List[str] = []
    removed: List[str] = []
    for line in text.splitlines():
        m = _INSTALL_LINE.match(line)
        if m and _is_install_arg_empty(m.group("installer"), m.group("args") or ""):
            removed.append(line)
            continue
        out_lines.append(line)
    cleaned = "\n".join(out_lines)
    # Preserve trailing newline if the original had one.
    if text.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned, removed


# ---------------------------------------------------------------------
# lint: surface structural problems we can't auto-fix
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class LintIssue:
    code: str           # short stable identifier, e.g. "no-from"
    message: str        # user-facing one-line description
    severity: str       # "error" (blocks build) or "warning"
    line: int = 0       # 1-based line number, 0 if not line-bound


def lint_dockerfile(text: str) -> List[LintIssue]:
    """Return a list of structural issues. Empty = OK to build.

    Catches:
      * missing FROM (Docker rejects, but the LLM occasionally emits
        a partial Dockerfile that starts with WORKDIR/ENV)
      * empty install lines that sanitize didn't strip (defensive —
        sanitize should have caught them, but the caller may want a
        belt-and-suspenders check before persisting)
      * RUN with empty body — `RUN \n` or `RUN  ` — same exit-1 trap
        as empty pip install but for any command
    """
    issues: List[LintIssue] = []
    if not text or not text.strip():
        issues.append(LintIssue(
            code="empty",
            message="Dockerfile is empty.",
            severity="error",
        ))
        return issues

    lines = text.splitlines()
    saw_from = False

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        upper = stripped.split(None, 1)[0].upper() if stripped else ""

        if upper == "FROM":
            saw_from = True

        # Empty install (sanitize should have removed these — flag if
        # anything slipped through).
        m = _INSTALL_LINE.match(raw)
        if m and _is_install_arg_empty(m.group("installer"), m.group("args") or ""):
            issues.append(LintIssue(
                code="empty-install",
                message=(
                    f"`{m.group('installer')}` install with no packages — "
                    "the build will fail with exit code 1 on this line."
                ),
                severity="error",
                line=lineno,
            ))

        # `RUN` with no body / only whitespace.
        if upper == "RUN":
            rest = stripped[3:].strip()
            if not rest:
                issues.append(LintIssue(
                    code="empty-run",
                    message="`RUN` with no command — invalid Dockerfile.",
                    severity="error",
                    line=lineno,
                ))

    if not saw_from:
        issues.append(LintIssue(
            code="no-from",
            message="Dockerfile has no FROM directive.",
            severity="error",
        ))

    return issues
