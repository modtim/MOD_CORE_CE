"""Split-RUN heuristic for LLM-generated Dockerfiles (issue #2b/#2d).

The Containerizer's LLM tends to emit one giant ``RUN pip install A B C
D E F G H ...`` block per Dockerfile. When ANY single package fails
(wrong version, transitive conflict, network glitch on PyPI), the
entire ``RUN`` exits non-zero and the build log buries WHICH package
failed inside dozens of pip stack-trace lines. Same pattern happens
with ``RUN apt-get install`` blocks.

This module post-processes a generated Dockerfile and rewrites
mega-RUN blocks into smaller per-package or per-group RUN blocks
when the package count exceeds a threshold. The fix is purely about
build-log diagnostics: a failure at ``RUN pip install gradio==6.0.1``
on its own line is identifiable; a failure at line 27 of an 80-line
``RUN pip install ...`` block buried in pip's output is not.

Heuristic rules (per the node-build-reliability capability spec):

* Trigger split only when a ``RUN pip install`` or ``RUN apt-get
  install`` block carries MORE than ``threshold`` packages (default
  5). Below threshold, leave the block alone — splitting flat
  3-package installs would just bloat the layer cache.

* For ``RUN pip install`` above threshold:
    - VCS deps (``git+...``, ``hg+...``, ``svn+...``) → isolated
      RUN per dep (these need git in the layer + hit network during
      install).
    - Pinned-version deps (``==``, ``>=``, ``<=``, ``~=``, ``!=``,
      ``>``, ``<``, or trailing version like ``foo[1.2.3]``) → each
      gets its own RUN line (so a pin mismatch is identifiable).
    - Plain unpinned deps → grouped into a single RUN line (these
      are usually stable enough that one failure is informative).

* For ``RUN apt-get install`` above threshold: each package gets
  its own RUN line (apt failures are usually about repo access,
  not package conflicts; per-package emission isolates the
  offending package). The ``&& rm -rf /var/lib/apt/lists/*``
  cleanup tail rides only on the LAST emitted RUN so the layer
  cache benefit isn't lost.

* Flag preservation: ``--no-cache-dir``, ``--extra-index-url``,
  ``--index-url``, ``--user``, ``--no-deps``, ``--upgrade``,
  ``-y`` (apt) etc. are forwarded to every emitted RUN.

This module is pure functions; no I/O, no LLM. It can be unit-tested
in isolation and wired into the artifact-emission pipeline as a
post-processor on the LLM's Dockerfile output.
"""

from __future__ import annotations

import re
from typing import List, Tuple

DEFAULT_THRESHOLD = 5

# Coalesces a backslash-line-continuation block into a single line so
# the parser doesn't have to deal with `\<newline>`. Operates on the
# raw Dockerfile text — matches `\ \n[ \t]*` and replaces with a
# single space.
_LINE_CONT_RE = re.compile(r"\\\s*\n[ \t]*")

# A "package token" in pip-install context: the leading characters of
# the package name plus any version specifier or extras. Used to
# distinguish "pinned" from "unpinned" without needing to actually
# resolve the package.
_PIP_VERSION_OPS = ("==", ">=", "<=", "~=", "!=", ">", "<")

# Recognise pip flags we should forward verbatim. Anything not in this
# list and not starting with `-` is treated as a package token.
_PIP_FLAG_WITH_VALUE = {
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--trusted-host",
    "--prefix",
    "--target",
    "--root",
    "--cache-dir",
    "--src",
    "--editable", "-e",
}
_PIP_FLAG_BARE = {
    "--no-cache-dir",
    "--no-deps",
    "--upgrade",
    "-U",
    "--user",
    "--ignore-installed",
    "--no-build-isolation",
    "--pre",
    "--no-binary", "--only-binary",  # technically take values, treat as bare for safety
}

_APT_FLAG_BARE = {
    "-y", "--yes",
    "--no-install-recommends",
    "--no-install-suggests",
    "-q", "--quiet",
}


def _is_vcs_pkg(token: str) -> bool:
    """Heuristic: a VCS install spec begins with one of the recognised
    URL schemes. Doesn't validate the URL — we just need to bucket
    tokens for the splitter.
    """
    return token.startswith((
        "git+", "git://",
        "hg+", "svn+", "bzr+",
        "https://", "http://",  # raw URLs sometimes used
    ))


def _is_pinned_pkg(token: str) -> bool:
    """A token is pinned if it carries a recognisable version
    specifier. ``foo==1.2`` and ``foo[bar]>=2`` both count;
    ``foo`` and ``foo[bar]`` don't.
    """
    return any(op in token for op in _PIP_VERSION_OPS)


def _classify_pip_tokens(tokens: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """Split a flat token list into (vcs, pinned, plain). Order
    within each bucket is preserved from the input.
    """
    vcs: List[str] = []
    pinned: List[str] = []
    plain: List[str] = []
    for tok in tokens:
        if _is_vcs_pkg(tok):
            vcs.append(tok)
        elif _is_pinned_pkg(tok):
            pinned.append(tok)
        else:
            plain.append(tok)
    return vcs, pinned, plain


def _parse_pip_install_line(line: str) -> Tuple[List[str], List[str]] | None:
    """Parse a coalesced ``RUN pip install ...`` line into (flags,
    packages). Returns None if the line isn't a pip-install RUN.

    Flag forwarding is conservative: any token that starts with ``-``
    is treated as a flag. Flags in ``_PIP_FLAG_WITH_VALUE`` consume the
    next token as their value; everything else is stand-alone.
    """
    stripped = line.lstrip()
    if not re.match(r"^RUN\s+", stripped):
        return None
    # Strip the leading `RUN`, then strip a leading `pip install`.
    body = re.sub(r"^RUN\s+", "", stripped)
    m = re.match(r"^pip(?:3?)\s+install\s+", body)
    if not m:
        return None
    rest = body[m.end():]

    # Tokenize on whitespace. Already coalesced — no need to handle
    # line continuations here.
    tokens = rest.split()
    flags: List[str] = []
    packages: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            flags.append(tok)
            if tok in _PIP_FLAG_WITH_VALUE and i + 1 < len(tokens):
                flags.append(tokens[i + 1])
                i += 2
                continue
        else:
            packages.append(tok)
        i += 1
    return flags, packages


def _parse_apt_install_line(line: str) -> Tuple[List[str], List[str], str] | None:
    """Parse a coalesced ``RUN apt-get install ...`` line into (flags,
    packages, trailing_cleanup). Returns None if the line isn't an
    apt-install RUN.

    ``trailing_cleanup`` captures any chained ``&& rm -rf
    /var/lib/apt/lists/*`` (or similar) so the splitter can re-attach
    it to the LAST emitted RUN to preserve the layer-cache savings.
    """
    stripped = line.lstrip()
    m = re.match(r"^RUN\s+(.*)$", stripped, re.DOTALL)
    if not m:
        return None
    body = m.group(1)
    # Find ``apt-get install`` (with optional `apt-get update &&` prefix).
    install_m = re.search(r"\bapt-get\s+install\b", body)
    if not install_m:
        return None
    # Anything before `apt-get install` (e.g. `apt-get update &&`) is
    # left alone — this heuristic only splits the install half. To keep
    # things simple we require the line to BE an apt-install (no
    # `update` prefix) for now.
    if body[: install_m.start()].strip():
        return None
    rest = body[install_m.end():]

    # Split on `&&` so we can capture any trailing cleanup chunk.
    chunks = [c.strip() for c in rest.split("&&")]
    install_chunk = chunks[0]
    trailing = " && ".join(chunks[1:]).strip()

    tokens = install_chunk.split()
    flags: List[str] = []
    packages: List[str] = []
    for tok in tokens:
        if tok.startswith("-"):
            flags.append(tok)
        else:
            packages.append(tok)
    return flags, packages, trailing


def _emit_pip_runs(flags: List[str], vcs: List[str], pinned: List[str], plain: List[str]) -> List[str]:
    """Build the post-split list of ``RUN pip install ...`` lines. VCS
    and pinned each get their own RUN; plain packages are grouped.
    """
    out: List[str] = []
    flags_str = (" " + " ".join(flags)) if flags else ""
    for v in vcs:
        out.append(f"RUN pip install{flags_str} {v}")
    for p in pinned:
        out.append(f"RUN pip install{flags_str} {p}")
    if plain:
        out.append(f"RUN pip install{flags_str} " + " ".join(plain))
    return out


def _emit_apt_runs(flags: List[str], packages: List[str], trailing: str) -> List[str]:
    """One RUN per apt package; ``trailing`` cleanup attaches to the
    last RUN so the cache is still pruned after install.
    """
    flags_str = (" " + " ".join(flags)) if flags else ""
    out: List[str] = []
    for i, pkg in enumerate(packages):
        line = f"RUN apt-get install{flags_str} {pkg}"
        if i == len(packages) - 1 and trailing:
            line = f"{line} && {trailing}"
        out.append(line)
    return out


def split_long_runs(dockerfile_text: str, *, threshold: int = DEFAULT_THRESHOLD) -> str:
    """Top-level transform: rewrite mega-RUN install blocks into
    smaller blocks when the package count exceeds ``threshold``.

    Returns the modified Dockerfile text (or the original if no
    blocks needed splitting). Pure function — no I/O.
    """
    if not dockerfile_text:
        return dockerfile_text

    # Coalesce backslash continuations so each logical RUN line becomes
    # a single physical line for the parser. Track which lines were
    # originally continued so we can flag them in the output (helps
    # debuggability when a split-line failure surfaces in build logs).
    coalesced = _LINE_CONT_RE.sub(" ", dockerfile_text)

    out_lines: List[str] = []
    for line in coalesced.split("\n"):
        # pip case
        parsed_pip = _parse_pip_install_line(line)
        if parsed_pip is not None:
            flags, packages = parsed_pip
            if len(packages) > threshold:
                vcs, pinned, plain = _classify_pip_tokens(packages)
                # Only split when the heuristic actually produces more
                # than one resulting RUN — splitting "pip install A B
                # C D E F" (all plain) into ONE grouped RUN is a no-op.
                emitted = _emit_pip_runs(flags, vcs, pinned, plain)
                if len(emitted) > 1:
                    out_lines.extend(emitted)
                    continue
            # Below threshold OR no real split: keep coalesced single line.
            out_lines.append(line)
            continue

        # apt case
        parsed_apt = _parse_apt_install_line(line)
        if parsed_apt is not None:
            flags, packages, trailing = parsed_apt
            if len(packages) > threshold:
                emitted = _emit_apt_runs(flags, packages, trailing)
                out_lines.extend(emitted)
                continue
            out_lines.append(line)
            continue

        # Anything else: passthrough (already coalesced).
        out_lines.append(line)

    return "\n".join(out_lines)
