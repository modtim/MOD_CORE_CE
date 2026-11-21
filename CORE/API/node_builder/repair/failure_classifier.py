"""Infra-vs-plan failure classifier (wizard-rebuild §0b).

When a build fails, the repair loop today consumes one LLM repair
attempt per failure — regardless of whether the failure was caused
by something the LLM can fix (build-substance: missing system dep,
wrong Python version, etc.) or by something fundamentally outside
the LLM's reach (infra-transient: Harbor 502, worker disconnect,
out-of-disk-space on the builder).

Stage observation 2026-05-21: a tenant's wizard build burned 8 outer
repair attempts in a row because Harbor was returning 502s during a
maintenance window. The LLM kept proposing different Dockerfile
"fixes" for what was actually a registry-layer flap. Wasted tokens,
wasted user time, no actual repair possible.

This module classifies the failure BEFORE the LLM call so the
repair endpoint can short-circuit infra-transient failures into a
free retry (rebuild without modification, no LLM tokens consumed,
no budget counter incremented).

Free-retry policy (enforced by the endpoint, not this module):
  - Up to 3 free infra-transient retries per spec.
  - Persistent infra issues (4+ in a row) eventually count against
    the budget so a stuck spec doesn't loop forever.
  - The repair-budget tier semantics from §7 stay intact for
    build-substance failures.

This module is intentionally narrow: it ONLY classifies. The
"what to do with that classification" logic lives in the endpoint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Union


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Each entry: (rule_name, compiled_regex, human-readable description).
# Patterns are matched case-insensitively against the combined
# stdout + stderr tail. Order matters only for the "matched_pattern"
# reporting — the first match wins.
#
# When adding patterns:
#   - Prefer broad-but-specific phrases that ONLY show up for infra
#     issues, not for build-substance issues. E.g., "502 Bad Gateway"
#     is always infra; "module not found" is always substance.
#   - Keep regexes simple — false-positives are cheap (we'll retry an
#     extra time on something that wasn't really infra), false-negatives
#     are expensive (we burn LLM budget on real infra issues).
_INFRA_TRANSIENT_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # HTTP-level registry / API errors
    ("http-502", re.compile(r"\b502 bad gateway\b", re.I),
     "registry/proxy returned 502 Bad Gateway"),
    ("http-503", re.compile(r"\b503 service unavailable\b", re.I),
     "service returned 503 (likely maintenance / overload)"),
    ("http-504", re.compile(r"\b504 gateway timeout\b", re.I),
     "gateway timeout reaching upstream"),
    ("http-429", re.compile(r"\b429 too many requests\b|\btoo many requests\b", re.I),
     "rate-limited by upstream registry"),

    # TCP / network-layer issues
    ("conn-refused", re.compile(r"connection refused", re.I),
     "TCP connection refused by remote"),
    ("conn-reset", re.compile(r"connection reset by peer|connection reset", re.I),
     "TCP connection reset mid-transfer"),
    ("conn-timeout", re.compile(r"\b(?:i/o timeout|read timed out|connect.* timed out|connection timed out)\b", re.I),
     "network I/O timeout"),
    ("conn-deadline", re.compile(r"context deadline exceeded", re.I),
     "Go-client context deadline (transient timeout)"),
    ("dns-fail", re.compile(r"temporary failure in name resolution|no such host|nodename nor servname provided", re.I),
     "DNS lookup failed transiently"),
    ("net-unreachable", re.compile(r"network is unreachable|no route to host", re.I),
     "transient routing failure"),
    ("tls-handshake", re.compile(r"tls handshake timeout|tls: bad record mac|broken pipe", re.I),
     "TLS handshake / transport disrupted"),

    # Local disk / resource pressure on the build worker
    ("disk-full", re.compile(r"no space left on device|disk quota exceeded", re.I),
     "build worker out of disk space (operator action needed)"),
    ("oom", re.compile(r"out of memory|cannot allocate memory|oom-killed", re.I),
     "build worker out of memory"),
    ("buildkit-oom", re.compile(r"runc.*killed by signal: killed|signal 9", re.I),
     "build process killed (likely OOM)"),

    # Harbor / Docker daemon transient errors
    ("harbor-timeout", re.compile(r"harbor.*time(?:d|s)? out|harbor.*deadline", re.I),
     "Harbor request timeout"),
    ("dockerd-fail", re.compile(r"docker daemon (?:not running|stopped|restart)", re.I),
     "Docker daemon transient unavailability"),
    ("buildx-restart", re.compile(r"buildx.*driver.*restart|builder.*recreate", re.I),
     "BuildKit builder restarted mid-build"),

    # Worker / orchestration layer transients
    ("worker-disconnect", re.compile(r"worker (?:disconnected|lost|gone|unhealthy)", re.I),
     "build worker disconnected during build"),
    ("task-cancelled", re.compile(r"task.*cancelled by orchestrator|workflow_run.*cancelled", re.I),
     "orchestrator cancelled the task (not a build failure)"),

    # apt / pip layer transient mirror failures (separate from "package
    # doesn't exist" which is build-substance).
    ("apt-mirror", re.compile(
        r"apt-get update.*failed|hash sum mismatch|the repository.*does not have a release file|failed to fetch.*deb",
        re.I,
    ), "apt mirror flake (transient; retry usually works)"),
    ("pip-mirror", re.compile(
        r"could not fetch URL.*proxy error|read timed out\..*pip|connection broken.*pypi",
        re.I,
    ), "PyPI mirror transient error"),
]

# Patterns that LOOK like infra but are actually build-substance.
# These get pulled out FIRST so they don't accidentally match the
# patterns above. (Order: more-specific first, then generic infra.)
_BUILD_SUBSTANCE_OVERRIDE_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # `pip install foo==9.9.9` failing with "no matching distribution"
    # is build-substance: the user pinned a version that doesn't exist.
    ("pip-no-matching-version", re.compile(
        r"could not find a version that satisfies the requirement|"
        r"no matching distribution found for",
        re.I,
    ), "pinned package version doesn't exist on PyPI"),
    # `apt-get install nonexistent-pkg` is build-substance, not mirror.
    ("apt-no-package", re.compile(
        r"unable to locate package|e: package .* has no installation candidate",
        re.I,
    ), "apt can't find the named package (typo or wrong distro)"),
    # `pip install` failing due to compiler errors is build-substance.
    ("compile-error", re.compile(
        r"error: command '\S+(?:gcc|g\+\+|clang|nvcc)' failed|"
        r"error: \[errno \d+\]|"
        r"fatal error: \S+\.h: no such file",
        re.I,
    ), "compile error during native extension build"),
    # ModuleNotFoundError at smoke-test time → install step missed a dep
    ("module-not-found", re.compile(
        r"modulenotfounderror: no module named|importerror: no module named",
        re.I,
    ), "Python import error at runtime (install step missed a dep)"),
    # Generic Dockerfile FROM-resolution failure with a CLEAR name issue
    # (the image doesn't exist) — substance, not infra.
    ("from-image-not-found", re.compile(
        r"failed to resolve source metadata.*manifest unknown|"
        r"pull access denied|not found: manifest unknown|"
        r"repository does not exist or may require",
        re.I,
    ), "FROM image doesn't exist or requires auth"),
]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of classify(). `category` is the only field callers
    should branch on; `matched_pattern` + `reason` are for audit
    logs + the user-facing repair history."""
    category: Literal["infra-transient", "build-substance"]
    matched_pattern: Optional[str] = None
    reason: str = ""


@dataclass
class _MinimalFailureContext:
    """Just enough of FailureContext for classification.

    The full FailureContext from repair_loop carries exit_code,
    output_files, input_seed, etc. We only need the text tails."""
    stdout_tail: str = ""
    stderr_tail: str = ""
    exit_code: int = -1


# Accept either the real FailureContext (duck-typed by having
# stdout_tail / stderr_tail / exit_code attrs) or a plain dict from
# request.failure. Both are common at the call sites.
FailureLike = Union[_MinimalFailureContext, dict, "object"]


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------

def _coerce(failure: FailureLike) -> _MinimalFailureContext:
    """Normalize anything FailureContext-shaped or dict-shaped to a
    _MinimalFailureContext. Keeps the public classify() signature
    permissive for callers in different parts of the codebase."""
    if isinstance(failure, _MinimalFailureContext):
        return failure
    if isinstance(failure, dict):
        return _MinimalFailureContext(
            stdout_tail=str(failure.get("stdout_tail", "")),
            stderr_tail=str(failure.get("stderr_tail", "")),
            exit_code=int(failure.get("exit_code", -1)),
        )
    # Duck-typed (real FailureContext from repair_loop, or anything
    # with the same attribute names).
    return _MinimalFailureContext(
        stdout_tail=str(getattr(failure, "stdout_tail", "") or ""),
        stderr_tail=str(getattr(failure, "stderr_tail", "") or ""),
        exit_code=int(getattr(failure, "exit_code", -1) or -1),
    )


def classify(failure: FailureLike) -> ClassificationResult:
    """Classify a build failure as infra-transient or build-substance.

    Algorithm:
      1. Combine stdout + stderr tails into one searchable text.
      2. Check substance-override patterns FIRST. These catch
         build-substance failures whose error messages superficially
         look like infra (e.g., "pull access denied" sounds network-y
         but is really a substance issue — the named image doesn't
         exist or needs auth).
      3. If no substance override matched, check infra-transient
         patterns. First match wins.
      4. If neither matched, default to build-substance. We err on
         the side of "this is a real repair problem, consume budget"
         because the LLM's repair attempts are bounded; infra
         retries are bounded too but the consequences of mis-classifying
         a real build issue AS infra are worse (loop forever on a
         spec that needs human attention).
    """
    ctx = _coerce(failure)
    text = (ctx.stdout_tail or "") + "\n" + (ctx.stderr_tail or "")

    # Substance overrides first.
    for name, pattern, reason in _BUILD_SUBSTANCE_OVERRIDE_PATTERNS:
        if pattern.search(text):
            return ClassificationResult(
                category="build-substance",
                matched_pattern=name,
                reason=reason,
            )

    # Then infra-transient patterns.
    for name, pattern, reason in _INFRA_TRANSIENT_PATTERNS:
        if pattern.search(text):
            return ClassificationResult(
                category="infra-transient",
                matched_pattern=name,
                reason=reason,
            )

    # No match → default substance. Consumes budget, lets the
    # LLM repair attempt fire normally.
    return ClassificationResult(
        category="build-substance",
        matched_pattern=None,
        reason="no infra-transient signal detected; treating as repairable",
    )


# ---------------------------------------------------------------------------
# Helpers exposed for tests / observability
# ---------------------------------------------------------------------------

def infra_transient_pattern_names() -> Iterable[str]:
    """Returns the rule names for the infra patterns we know about.
    Useful for the wizard-info dashboard's "what kinds of infra
    failures are we seeing" breakdown."""
    return [name for name, _, _ in _INFRA_TRANSIENT_PATTERNS]


def build_substance_override_pattern_names() -> Iterable[str]:
    """Same, for the build-substance overrides."""
    return [name for name, _, _ in _BUILD_SUBSTANCE_OVERRIDE_PATTERNS]
