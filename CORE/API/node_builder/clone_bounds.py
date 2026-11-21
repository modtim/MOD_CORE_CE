"""GSEC R-100 — deterministic bounds for every ``git clone`` the wizard emits.

The Node Wizard asks an LLM to write Dockerfiles (and describe-synthesize
``dockerfile_extras``). Those LLMs historically emit bare ``git clone <url>``
lines, which in the build container are UNBOUNDED: full history, full blob
set, LFS smudge, no timeout — a malicious or merely large upstream repo can
hang a build or exhaust disk. Until R-100 the only guard was prompt advice
(``use git clone --depth 1``), which the model ignores at will.

This module is a PURE, deterministic post-generation normalizer / admission
gate. It runs over already-generated Dockerfile text and:

  * rewrites a bare ``git clone`` to a bounded one —
    ``git clone --depth 1 --filter=blob:none`` (+ ``--shallow-submodules``
    when the line is recursive), and
  * ensures ``GIT_LFS_SKIP_SMUDGE=1`` is set for the build, and
  * REJECTS anything that cannot be safely bounded (``git fetch --unshallow``
    / any ``--unshallow`` / ``--no-depth``) — these exist precisely to defeat
    the shallow clone, so we refuse rather than guess.

Already-bounded clones (one that already carries ``--depth`` /
``--single-branch`` / ``--filter``) are left byte-for-byte untouched; we only
guarantee the LFS skip is present.

No DB, no network, no side effects — it is pure string -> string + a change
report, so it is trivially unit-testable and safe to call from both the
artifact-admission path and the describe-synthesize validator.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# The bound flags we insert. ``--depth 1`` keeps only the tip commit;
# ``--filter=blob:none`` defers blob download (partial clone) so a huge
# repo's objects don't all land in the build container.
_BOUND_FLAGS = "--depth 1 --filter=blob:none"
# Submodule flag, only meaningful alongside a recursive clone.
_SUBMODULE_FLAG = "--shallow-submodules"
# Environment that stops LFS from smudging (downloading) pointer files.
_LFS_ENV = "GIT_LFS_SKIP_SMUDGE=1"
_LFS_ENV_LINE = f"ENV {_LFS_ENV}"

# A ``git clone`` keyword occurrence (case-insensitive, tolerant of extra
# whitespace). We rewrite around THIS match, never the surrounding shell.
_CLONE_RE = re.compile(r"git\s+clone\b", re.IGNORECASE)
# Any flag that means "this clone already has a size bound on it" — its
# presence makes us leave the flag list alone (we never double-add).
_BOUNDED_FLAG_RE = re.compile(r"--(?:depth|single-branch|filter)\b")
# Submodule recursion — needs the shallow-submodules companion flag.
_RECURSIVE_RE = re.compile(r"--(?:recursive|-r)\b", re.IGNORECASE)
# History-unshallow / de-bound operations we refuse to emit.
_UNBOUND_RE = re.compile(r"--unshallow\b|--no-depth\b", re.IGNORECASE)
# A Dockerfile comment / blank physical line (skip — not an executable clone).
_COMMENT_LINE_RE = re.compile(r"^\s*(#|$)")


class CloneBoundsRejection(Exception):
    """Raised when generated Dockerfile text cannot be made safe.

    ``reason`` is operator-facing: it names the offending construct and what
    to change. The admission gate surfaces it as a 400 with this message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _line_is_comment(line: str) -> bool:
    return bool(_COMMENT_LINE_RE.match(line))


def has_unbounded_construct(text: str) -> bool:
    """True if ``text`` contains a history-unshallowing construct
    (``--unshallow`` / ``--no-depth``) that the admission gate must reject
    rather than rewrite. Public for the describe-synthesize validator, which
    rejects at the LLM-output boundary before a Dockerfile ever exists."""
    if not isinstance(text, str):
        return False
    return bool(_UNBOUND_RE.search(text))


def bound_clone_line(line: str) -> Tuple[str, Optional[str], List[str]]:
    """Bound every ``git clone`` on ONE physical Dockerfile line.

    Returns ``(new_line, rejection_reason_or_None, changes)`` where ``changes``
    is a human-readable list of what was rewritten (empty when the line needed
    no change). A rejection reason is non-None only when the line contains an
    un-shallowable construct (``--unshallow`` / ``--no-depth``).
    """
    if _line_is_comment(line):
        return line, None, []

    if _UNBOUND_RE.search(line):
        snippet = line.strip()[:120]
        return line, (
            "generated Dockerfile un-bounds the clone ("
            f"uses --unshallow / --no-depth): {snippet!r}. The wizard build "
            "container only allows shallow, partial clones — remove the "
            "history-unshallowing flag."
        ), []

    if not _CLONE_RE.search(line):
        return line, None, []

    # NOTE: no line-level "already bounded" shortcut — a chained line may
    # carry one bounded and one bare clone; the per-clone scan below
    # handles each clone on its own merits.

    # Bound EVERY bare clone on the line (the model occasionally chains
    # `git clone a /a && git clone b /b`). A clone already carrying a bound
    # flag in its own flag window (the contiguous --token run immediately
    # after the "clone" keyword) is left alone. --shallow-submodules is a
    # no-op for a non-recursive clone, so when the line carries a recursive
    # flag we attach it to every clone on the line — safe either way.
    recursive = bool(_RECURSIVE_RE.search(line))
    flags = _BOUND_FLAGS + (" " + _SUBMODULE_FLAG if recursive else "")

    new_line = line
    pos = 0
    changes: List[str] = []
    while True:
        m = _CLONE_RE.search(new_line, pos)
        if m is None:
            break
        # Does THIS clone already carry a bound flag in its own flag window?
        j = m.end()
        while j < len(new_line) and new_line[j].isspace():
            j += 1
        bounded_here = False
        while j < len(new_line) and new_line[j] == "-":
            k = j
            while k < len(new_line) and new_line[k] not in " \t":
                k += 1
            if re.match(r"^--(?:depth|single-branch|filter)\b",
                        new_line[j:k]):
                bounded_here = True
                break
            j = k
            while j < len(new_line) and new_line[j].isspace():
                j += 1
        if bounded_here:
            pos = m.end()  # skip it; keep scanning for later bare clones
            continue
        new_line = new_line[:m.end()] + " " + flags + new_line[m.end():]
        pos = m.end() + len(flags) + 1
        changes.append(
            f"bounded unbounded clone on line: {line.strip()[:100]!r}"
        )

    if not changes:
        return line, None, []
    return new_line, None, changes


def normalize_dockerfile(text: str) -> Tuple[str, List[str], List[str]]:
    """Rewrite every unbounded ``git clone`` in a Dockerfile and ensure the
    LFS-skip env is set. Returns ``(new_text, changes, rejections)``.

    ``rejections`` is non-empty (and no rewrite is returned in that case —
    ``new_text`` is the ORIGINAL) when an un-shallowable construct is found;
    callers should surface it as an admission rejection.
    """
    if not isinstance(text, str) or not text:
        return text, [], []
    if "git" not in text:
        return text, [], []

    lines = text.splitlines()
    out: List[str] = []
    changes: List[str] = []
    rejections: List[str] = []
    clone_line_idx: Optional[int] = None

    for i, line in enumerate(lines):
        new_line, reason, line_changes = bound_clone_line(line)
        if reason is not None:
            rejections.append(reason)
        else:
            out.append(new_line)
            changes.extend(line_changes)
        if clone_line_idx is None and _CLONE_RE.search(line) \
                and not _line_is_comment(line):
            clone_line_idx = len(out) - 1

    if rejections:
        # Fail-closed: return the ORIGINAL text untouched so the caller
        # rejects on the reason rather than shipping a half-rewritten file.
        return text, [], rejections

    # Ensure GIT_LFS_STOP is set once, before the first clone (which is always
    # after a FROM, so the ENV is valid). Skip if already present anywhere.
    if clone_line_idx is not None and _LFS_ENV not in text:
        out.insert(clone_line_idx, _LFS_ENV_LINE)
        changes.append("added " + _LFS_ENV_LINE + " before first clone")

    # Rejoin preserving the original trailing-newline state.
    new_text = "\n".join(out)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, changes, []


def admit_dockerfile(text: str) -> Tuple[str, List[str]]:
    """Admission-gate form of :func:`normalize_dockerfile`.

    Returns ``(normalized_text, changes)`` on success, or raises
    :class:`CloneBoundsRejection` with a clear operator-facing reason when the
    text cannot be safely bounded. This is the shape the ingest path wants
    (raise -> 400) as opposed to the validator shape (list of reasons).
    """
    new_text, changes, rejections = normalize_dockerfile(text)
    if rejections:
        raise CloneBoundsRejection(rejections[0])
    return new_text, changes
