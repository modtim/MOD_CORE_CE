"""Deterministic Dockerfile transforms applied after the LLM returns.

These run on the hero Dockerfile before variant synthesis (so variants
inherit the fixes) and cover known-broken upstream patterns the LLM
faithfully translates from research repos' setup.sh:

* ``pillow-simd`` — unmaintained since 2022, no Python 3.11 wheel,
  source build fails on missing zlib/libjpeg headers. Drop-in
  replacement is regular ``Pillow``. Triggered by every TRELLIS-class
  3D / image generation repo.
* Build-time apt commands need root. The base images set ``USER mod``
  for runtime hardening; ``synthesise_variant`` already injects
  ``USER root`` for variants but the hero Dockerfile got the LLM's
  raw output and ran apt-get as ``mod`` → exit 100.
* Pip packages that shell out to a system binary / dlopen a shared lib
  (``pdf2image`` → poppler, ``opencv-python`` → libGL) build fine but
  fail the smoke probe at runtime. The LLM repair loop handles these
  badly — it abandons the working MOD base for a vanilla image and
  trips PEP 668. ``inject_system_packages_for_pip`` adds the apt deps
  deterministically on the existing base so the loop never runs.

All transforms are idempotent — safe to run repeatedly.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple


# Deprecated / broken pip packages → drop-in replacements.
# Conservative list; only includes packages observed to fail in real
# wizard builds. Add new entries as they're observed, with a comment
# explaining the failure mode and why the replacement is correct.
DEFAULT_PIP_REPLACEMENTS: Dict[str, str] = {
    # pillow-simd 9.0.0.post1 (2022) is the last release. No Python
    # 3.11 wheel; source build needs zlib + libjpeg headers AND
    # patches for newer Pillow ABI. Pillow is the maintained library
    # pillow-simd forked from — drop-in API-compatible.
    "pillow-simd": "Pillow",
}


# pip package (lowercased bare name) → system (apt) packages it needs
# that the curated base images don't already ship. The pip wheel alone
# doesn't pull these — they're runtime shared libs / CLI binaries the
# package shells out to. Without them the build's smoke probe fails
# with a missing-binary / missing-.so error that the LLM repair loop
# handles badly: it tends to abandon the working MOD base for a vanilla
# Debian/slim image and then trip PEP 668. Injecting the apt install
# deterministically (on the MOD base, where pip already works) avoids
# the loop entirely.
#
# Conservative list — only packages observed to fail in real wizard
# builds. Add entries with a comment explaining the failure mode.
DEFAULT_PIP_SYSTEM_DEPS: Dict[str, Tuple[str, ...]] = {
    # pdf2image shells out to poppler's `pdfinfo` / `pdftoppm` binaries
    # at runtime — without poppler-utils the smoke probe dies with
    # "Unable to get page count. Is poppler installed and in PATH?".
    # NOTE: poppler is GPL (see _COPYLEFT_APT_PACKAGES). We still inject
    # it so a pasted pdf2image node builds (warn-don't-block), but the
    # wizard now steers PDF tasks to pypdfium2 (BSD) at synthesis time
    # so this rarely fires.
    "pdf2image": ("poppler-utils",),
    # OpenCV dlopen's libGL + libgthread at import; headless builds hit
    # "ImportError: libGL.so.1: cannot open shared object file".
    "opencv-python": ("libgl1", "libglib2.0-0"),
    "opencv-contrib-python": ("libgl1", "libglib2.0-0"),
}


# Strong-copyleft system packages we may end up installing. Mapped to
# their SPDX licence so the build surfaces a warning — the platform
# advertises "permissive licences only", and these ship a GPL/AGPL
# binary in a tenant-facing image. We WARN rather than block (on-prem
# stance + invoking a CLI via subprocess is aggregation, not
# derivation), but the user/operator should see it. LGPL shared libs
# (e.g. libglib2.0-0) are intentionally NOT listed — dynamic linking
# is LGPL's intended, non-propagating use.
_COPYLEFT_APT_PACKAGES: Dict[str, str] = {
    "poppler-utils": "GPL-2.0",
    "libpoppler-dev": "GPL-2.0",
    "ghostscript": "AGPL-3.0",
    "libgs-dev": "AGPL-3.0",
}


def copyleft_apt_warnings(packages: Tuple[str, ...]) -> list:
    """Return one warning string per copyleft system package in
    ``packages``. Empty when all are permissive. Used to surface a
    licence notice when the dep-injection adds a GPL/AGPL binary."""
    out = []
    for p in packages:
        spdx = _COPYLEFT_APT_PACKAGES.get(p)
        if spdx:
            out.append(
                f"system package '{p}' is {spdx}-licensed (copyleft) — "
                f"it ships in a tenant-facing image the platform "
                f"advertises as permissive-only; prefer a permissive "
                f"alternative (e.g. pypdfium2 for PDF rendering)"
            )
    return out


# Match a single pip-install argument token. Permissive enough to
# preserve version specifiers (==X), markers (; python_version ...),
# extras ([extra]), and url fragments after the name. We swap only
# the bare package name when it equals a key in the replacements map.
_PIP_TOKEN_RE = re.compile(
    r"""
    (?<![\w./@-])         # left boundary: not part of a longer token
    (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)  # package name
    (?P<rest>             # version / extras / markers (kept verbatim)
        (?:\[[^\]]*\])?
        (?:[<>=!~][^\s]*)?
    )
    (?![\w./@-])          # right boundary
    """,
    re.VERBOSE,
)

_RUN_PIP_INSTALL_RE = re.compile(
    r"^(?P<lead>\s*RUN\s+(?:[^\n]*?\&\&\s+)?pip\s+install\b[^\n]*)$",
    re.MULTILINE,
)

_FROM_RE = re.compile(r"^\s*FROM\s+\S+(?:\s+AS\s+\S+)?\s*$", re.MULTILINE | re.IGNORECASE)
_USER_RE = re.compile(r"^\s*USER\s+\S+\s*$", re.MULTILINE | re.IGNORECASE)
_USER_ROOT_RE = re.compile(r"^\s*USER\s+root\s*$", re.MULTILINE | re.IGNORECASE)
_APT_INSTALL_LINE_RE = re.compile(
    r"^\s*RUN\b[^\n]*\bapt-get\b[^\n]*\binstall\b[^\n]*$", re.MULTILINE
)


def apply_pip_replacements(
    dockerfile: str,
    replacements: Dict[str, str] | None = None,
) -> Tuple[str, Dict[str, int]]:
    """Replace deprecated pip package names in every ``RUN pip install``
    line. Returns ``(new_dockerfile, count_per_replacement)`` so callers
    can log what was rewritten.

    Only the bare package name is swapped — version specifiers, extras,
    and markers attached to it are preserved. Lines that don't start
    with ``RUN ... pip install`` are untouched.
    """
    if replacements is None:
        replacements = DEFAULT_PIP_REPLACEMENTS
    if not replacements:
        return dockerfile, {}

    counts: Dict[str, int] = {k: 0 for k in replacements}

    def _swap_in_line(line: str) -> str:
        def _swap_token(m: re.Match[str]) -> str:
            name = m.group("name")
            rest = m.group("rest") or ""
            new = replacements.get(name)
            if new is None:
                return m.group(0)
            counts[name] += 1
            return new + rest
        return _PIP_TOKEN_RE.sub(_swap_token, line)

    def _swap_in_run_line(m: re.Match[str]) -> str:
        return _swap_in_line(m.group("lead"))

    new_df = _RUN_PIP_INSTALL_RE.sub(_swap_in_run_line, dockerfile)
    return new_df, counts


def inject_system_packages_for_pip(
    dockerfile: str,
    deps_map: Dict[str, Tuple[str, ...]] | None = None,
) -> Tuple[str, Tuple[str, ...]]:
    """Ensure system (apt) packages required by known pip packages are
    installed. For every pip-installed package that maps to system deps
    in ``deps_map``, inject one ``RUN apt-get install`` line right after
    the first ``USER root`` (falling back to after ``FROM``), skipping
    any package already named in an existing apt-get install line.

    Returns ``(new_dockerfile, injected_packages)``. Idempotent: a
    second run finds the packages already apt-installed and is a no-op.

    Run this AFTER ``inject_user_root_after_from`` so the apt-get line
    lands under root, and BEFORE ``inject_user_runtime_at_end``.
    """
    if deps_map is None:
        deps_map = DEFAULT_PIP_SYSTEM_DEPS
    if not deps_map:
        return dockerfile, ()

    # Collect bare pip package names across every RUN pip install line.
    pip_pkgs: set[str] = set()
    for m in _RUN_PIP_INSTALL_RE.finditer(dockerfile):
        for tok in _PIP_TOKEN_RE.finditer(m.group("lead")):
            pip_pkgs.add(tok.group("name").lower())

    needed: list[str] = []
    for pkg, sys_pkgs in deps_map.items():
        if pkg.lower() in pip_pkgs:
            needed.extend(sys_pkgs)
    if not needed:
        return dockerfile, ()

    # Drop any package already named in an existing apt-get install line
    # (idempotency + respect a Dockerfile that already handles it).
    existing_apt = "\n".join(m.group(0) for m in _APT_INSTALL_LINE_RE.finditer(dockerfile))
    to_install = [
        p for p in dict.fromkeys(needed)
        if not re.search(rf"(?<![\w.+-]){re.escape(p)}(?![\w.+-])", existing_apt)
    ]
    if not to_install:
        return dockerfile, ()

    run_line = (
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        + " ".join(to_install)
        + " && rm -rf /var/lib/apt/lists/*"
    )

    # Anchor after the first `USER root` so apt-get runs as root; fall
    # back to after the first FROM (inject_user_root_after_from normally
    # guarantees a USER root, but be defensive).
    anchor = _USER_ROOT_RE.search(dockerfile)
    if anchor is None:
        anchor = _FROM_RE.search(dockerfile)
    if anchor is None:
        return dockerfile, ()

    insert_point = anchor.end()
    lead = dockerfile[:insert_point]
    tail = dockerfile[insert_point:]
    sep_before = "" if lead.endswith("\n") else "\n"
    new_df = lead + sep_before + run_line + "\n" + tail.lstrip("\n")
    return new_df, tuple(to_install)


# Phase B (#1) — synth-declared system binaries
#
# When describe-synthesize tells us the script depends on apt
# packages or needs a source-build snippet, the Containerizer's LLM
# doesn't know to bake them into the hero Dockerfile (its prompt
# focuses on FROM + pip + COPY + ENTRYPOINT). These two injectors
# carry the synth declaration through deterministically, AFTER the
# LLM has emitted its baseline Dockerfile. Both run during the
# post-process pass that already handles pip → apt mapping.

def inject_synth_apt_packages(
    dockerfile: str,
    packages: Tuple[str, ...],
) -> Tuple[str, Tuple[str, ...]]:
    """Inject one ``RUN apt-get install`` line for the synth-declared
    packages, anchored after the first ``USER root`` (fall back to
    after ``FROM``).

    Idempotent: any package already named in an existing apt-get
    install line is skipped. Returns ``(new_dockerfile, installed)``
    where ``installed`` is the deduplicated tuple of packages that
    actually got added (empty when everything was already present
    or when the input list was empty).

    Sibling of ``inject_system_packages_for_pip`` — same anchoring
    rules, separate function so the two sources of system-deps stay
    auditable in the post-process log.
    """
    pkgs = tuple(dict.fromkeys(p for p in packages if p and p.strip()))
    if not pkgs:
        return dockerfile, ()

    existing_apt = "\n".join(m.group(0) for m in _APT_INSTALL_LINE_RE.finditer(dockerfile))
    to_install = [
        p for p in pkgs
        if not re.search(rf"(?<![\w.+-]){re.escape(p)}(?![\w.+-])", existing_apt)
    ]
    if not to_install:
        return dockerfile, ()

    run_line = (
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        + " ".join(to_install)
        + " && rm -rf /var/lib/apt/lists/*"
    )

    anchor = _USER_ROOT_RE.search(dockerfile)
    if anchor is None:
        anchor = _FROM_RE.search(dockerfile)
    if anchor is None:
        return dockerfile, ()
    insert_point = anchor.end()
    lead = dockerfile[:insert_point]
    tail = dockerfile[insert_point:]
    sep_before = "" if lead.endswith("\n") else "\n"
    new_df = lead + sep_before + run_line + "\n" + tail.lstrip("\n")
    return new_df, tuple(to_install)


# Disallowed Dockerfile directives in user-supplied dockerfile_extras
# (already enforced at synth time, mirrored here so the Containerizer
# defends against an extras blob assembled by a non-synth path —
# legacy /build-and-register-local, manual API calls). FROM/COPY/USER/
# etc. would clobber the wizard's composition.
_EXTRAS_FORBIDDEN_DIRECTIVES = frozenset({
    "FROM", "COPY", "ADD", "ENTRYPOINT", "CMD",
    "USER", "EXPOSE", "VOLUME", "HEALTHCHECK", "STOPSIGNAL", "SHELL",
})


def _scrub_extras_block(extras: str) -> str:
    """Drop any disallowed directive line, fold blank lines. Returns
    the cleaned block ready for insertion. A pure-comment / pure-
    blank block returns ""."""
    if not extras or not extras.strip():
        return ""
    physical = extras.splitlines()
    logical: list = []
    buf = ""
    for raw in physical:
        s = raw.rstrip()
        if s.lstrip().startswith("#") or not s.lstrip():
            if buf:
                logical.append(buf)
                buf = ""
            continue
        if s.endswith("\\"):
            buf += s + "\n"  # keep continuation as-is for the output
        else:
            buf += s
            logical.append(buf)
            buf = ""
    if buf:
        logical.append(buf)

    kept: list = []
    for line in logical:
        token = line.lstrip().split(None, 1)[0] if line.strip() else ""
        directive = token.upper()
        if directive in _EXTRAS_FORBIDDEN_DIRECTIVES:
            # Silent drop — the synth-side validator already rejected
            # these. If they appear here, something upstream went
            # wrong; we don't want to fail the build at this stage.
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def inject_synth_dockerfile_extras(
    dockerfile: str,
    extras: str,
) -> Tuple[str, bool]:
    """Append synth-declared dockerfile_extras to the hero Dockerfile,
    anchored after the apt-get injection (so the extras can use the
    apt-installed packages) and before the entrypoint COPY.

    Returns ``(new_dockerfile, injected)``. ``injected`` is False
    when the extras block was empty or got entirely scrubbed for
    forbidden directives.

    Anchor priority (the first that matches):
      1. After the LAST apt-get install line (extras likely build
         from system libs we just installed).
      2. After the first ``USER root``.
      3. After the first ``FROM``.

    The 8 KB length cap from the synth-side validator already bounds
    the extras blob; no additional cap here.
    """
    block = _scrub_extras_block(extras)
    if not block:
        return dockerfile, False

    apt_matches = list(_APT_INSTALL_LINE_RE.finditer(dockerfile))
    if apt_matches:
        anchor_end = apt_matches[-1].end()
    else:
        anchor = _USER_ROOT_RE.search(dockerfile)
        if anchor is None:
            anchor = _FROM_RE.search(dockerfile)
        if anchor is None:
            return dockerfile, False
        anchor_end = anchor.end()

    lead = dockerfile[:anchor_end]
    tail = dockerfile[anchor_end:]
    sep_before = "" if lead.endswith("\n") else "\n"
    new_df = (
        lead
        + sep_before
        + "# synth-declared dockerfile_extras\n"
        + block
        + "\n"
        + tail.lstrip("\n")
    )
    return new_df, True


def inject_user_root_after_from(dockerfile: str) -> Tuple[str, bool]:
    """Insert ``USER root`` immediately after the first ``FROM`` line
    when no ``USER`` directive already appears between the FROM and
    the first ``RUN``. Returns ``(new_dockerfile, was_injected)``.

    Why: curated bases set ``USER mod`` for runtime hardening; build-
    time apt-get needs root. ``gpu_variants.synthesise_variant``
    already does this for variants (after their LABEL block); the hero
    Dockerfile got the LLM's raw output, with ``USER mod`` still in
    effect, so its first ``RUN apt-get`` failed exit 100.
    """
    from_match = _FROM_RE.search(dockerfile)
    if not from_match:
        return dockerfile, False

    # If a USER directive already appears between the FROM and the
    # first RUN, don't double-inject.
    after_from = dockerfile[from_match.end() :]
    first_run = re.search(r"^\s*RUN\s", after_from, re.MULTILINE)
    head = after_from if first_run is None else after_from[: first_run.start()]
    if _USER_RE.search(head):
        return dockerfile, False

    insert_point = from_match.end()
    sep = "\n" if not dockerfile[insert_point:insert_point + 1] == "\n" else ""
    inserted = "\nUSER root" + sep
    new_df = dockerfile[:insert_point] + inserted + dockerfile[insert_point:]
    return new_df, True


# Curated MOD bases all run as ``mod`` (UID 10001) at runtime — see
# CORE/DockerContainers/BaseImages/*/Dockerfile. The admission gate
# enforces ``Final USER must be non-root``, so when we inject
# ``USER root`` for build-time access we MUST also drop back to
# ``mod`` before the entrypoint to keep the runtime hardening intact.
DEFAULT_RUNTIME_USER = "mod"


def inject_user_runtime_at_end(
    dockerfile: str,
    runtime_user: str = DEFAULT_RUNTIME_USER,
) -> Tuple[str, bool]:
    """Append ``USER <runtime_user>`` near the end of the Dockerfile so
    the final image runs as a non-root user. Pairs with
    ``inject_user_root_after_from`` — together they give root at build
    time and a hardened runtime.

    Insert point: just before the first ``ENTRYPOINT`` / ``CMD``
    directive. If neither is present, append at the very end.

    Skip when:
      - The last ``USER`` directive in the file is already non-root
        (LLM or recipe already handled it).
      - There's no ``FROM`` line (this isn't a real Dockerfile).
    """
    if not _FROM_RE.search(dockerfile):
        return dockerfile, False

    # Find the last USER directive in the file. If it's non-root,
    # the runtime user is already set; nothing to do.
    last_user_match = None
    for m in _USER_RE.finditer(dockerfile):
        last_user_match = m
    if last_user_match is not None:
        # Extract the user token from the matched line. Format:
        # "USER <name>".
        line = last_user_match.group(0).strip()
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "root":
            return dockerfile, False

    # Find the first ENTRYPOINT or CMD directive (case-insensitive,
    # at line start) — the runtime USER goes immediately before it.
    insert_re = re.compile(
        r"^\s*(?:ENTRYPOINT|CMD)\b", re.MULTILINE | re.IGNORECASE,
    )
    insert_match = insert_re.search(dockerfile)
    runtime_directive = f"USER {runtime_user}\n"

    if insert_match is not None:
        insert_point = insert_match.start()
        new_df = (
            dockerfile[:insert_point]
            + runtime_directive
            + dockerfile[insert_point:]
        )
    else:
        # No ENTRYPOINT/CMD — append at the end. Ensure a trailing
        # newline so the new line doesn't merge with the last line.
        sep = "" if dockerfile.endswith("\n") else "\n"
        new_df = dockerfile + sep + runtime_directive

    return new_df, True


# ---------------------------------------------------------------------------
# ENTRYPOINT guarantee — the tini-usage / "entrypoint never invoked" bug
# ---------------------------------------------------------------------------
#
# ROOT CAUSE (live "Multiply Numbers" wrap-base + inline-script build that
# thrashed 7 repair attempts): the curated MOD base images declare
#   ENTRYPOINT ["/usr/bin/tini", "--"]
# with NO CMD (see CORE/DockerContainers/BaseImages/*/Dockerfile). A generated
# child Dockerfile that sets NEITHER its own ENTRYPOINT NOR a CMD inherits a
# bare `tini --` that has nothing to exec. At runtime tini prints its usage
# message and exits non-zero — and because the child Dockerfile has no
# ENTRYPOINT line at all, the repair LLM can't "see" the problem and instead
# chases phantom "no input files" theories forever.
#
# The Containerizer prompt asks the weak generator to emit
#   ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
# but the 2B/8B model routinely drops it. This deterministic guard re-adds it.

_ENTRYPOINT_DIRECTIVE_RE = re.compile(r"^\s*ENTRYPOINT\b", re.MULTILINE | re.IGNORECASE)
_CMD_DIRECTIVE_RE = re.compile(r"^\s*CMD\b", re.MULTILINE | re.IGNORECASE)
_COPY_ENTRYPOINT_RE = re.compile(
    r"^\s*COPY\b[^\n]*\bentrypoint\.sh\b[^\n]*$", re.MULTILINE | re.IGNORECASE,
)

# Canonical destination the Containerizer prompt tells the LLM to COPY the
# entrypoint to (WORKDIR /data, script at /app/entrypoint.sh).
DEFAULT_ENTRYPOINT_DEST = "/app/entrypoint.sh"


def ensure_entrypoint(
    dockerfile: str,
    entrypoint_filename: str = "entrypoint.sh",
    default_dest: str = DEFAULT_ENTRYPOINT_DEST,
) -> Tuple[str, bool]:
    """Guarantee the Dockerfile ends with a runnable ENTRYPOINT.

    When the Dockerfile already declares an ``ENTRYPOINT`` or a ``CMD``, this
    is a no-op — the base image's bare-``tini`` problem is already solved.
    Otherwise we make sure the entrypoint script is COPY'd into the image and
    append ``ENTRYPOINT ["/bin/bash", "<dest>"]`` (the canonical shape the
    Containerizer prompt asks for). ``<dest>`` is taken from an existing
    ``COPY ... entrypoint.sh <dest>`` line when present, else ``default_dest``.

    Idempotent — a second pass sees the ENTRYPOINT it added and no-ops.
    Returns ``(new_dockerfile, injected)``.

    Ordering: run this BEFORE ``inject_user_runtime_at_end`` so the runtime
    ``USER mod`` lands immediately before the ENTRYPOINT we add.
    """
    if not dockerfile or not _FROM_RE.search(dockerfile):
        return dockerfile, False
    if _ENTRYPOINT_DIRECTIVE_RE.search(dockerfile) or _CMD_DIRECTIVE_RE.search(dockerfile):
        return dockerfile, False

    dest = default_dest
    copy_present = False
    m = _COPY_ENTRYPOINT_RE.search(dockerfile)
    if m:
        copy_present = True
        # The COPY destination is the last non-flag token on the line.
        toks = m.group(0).split()
        non_flag = [t for t in toks[1:] if not t.startswith("--")]
        if len(non_flag) >= 2:
            dest = non_flag[-1]

    additions: List[str] = []
    if not copy_present:
        # --chmod sets the mode during COPY under builder privilege before any
        # USER directive takes effect; the two-step COPY + `RUN chmod` fails on
        # curated bases that already switched to non-root `mod`.
        additions.append(f"COPY --chmod=0755 {entrypoint_filename} {dest}")
    additions.append(f'ENTRYPOINT ["/bin/bash", {json.dumps(dest)}]')

    sep = "" if dockerfile.endswith("\n") else "\n"
    new_df = dockerfile + sep + "\n".join(additions) + "\n"
    return new_df, True


# ---------------------------------------------------------------------------
# Self-pip-install scrub — the hallucinated `pip install <own-script-name>`
# ---------------------------------------------------------------------------
#
# The wizard materialises the user's pasted script (e.g. describe.py) INTO the
# image as source. The weak generator sees `import describe` /
# `from describe import *` and faithfully adds `RUN pip install describe`,
# treating the script's own module name as a PyPI package. That install either
# 404s (no such distribution) or silently pulls an unrelated name-squatting
# package — and the entrypoint's `from describe import *` then imports the
# wrong thing. Strip those tokens deterministically; the source is already in
# the image, it is never a dependency.

_LINE_IS_RUN_PIP_RE = re.compile(r"^\s*RUN\b[^\n]*\bpip\d?\s+install\b", re.IGNORECASE)

# pip-install argument keywords / sub-invocation tokens that are never a
# package name (so "a line with only these left → drop it").
_PIP_NON_PACKAGE_TOKENS = frozenset({
    "run", "pip", "pip3", "install", "python", "python3", "m", "sudo", "env",
    "http", "https", "true", "false",
})


def strip_self_pip_installs(
    dockerfile: str, module_names,
) -> Tuple[str, Tuple[str, ...]]:
    """Remove ``pip install`` of the user's OWN inline script/module names.

    For every ``RUN ... pip install`` line, drop tokens whose bare name matches
    an inline module name (case-insensitive), together with any version
    specifier / extras attached to it. If a line has no package tokens left
    afterwards, drop the whole ``RUN`` line. Idempotent.

    ``module_names`` is the set of the user's inline source module names
    (basename without extension) — these are source in the image, not packages.

    Returns ``(new_dockerfile, removed_names)``.
    """
    names = {str(n).strip().lower() for n in (module_names or ()) if str(n).strip()}
    # Never treat these as "self modules" even if a source file is named so —
    # they are real, commonly-installed distributions.
    names -= {"pip", "setuptools", "wheel", "requests", "numpy"}
    if not dockerfile or not names:
        return dockerfile, ()

    removed: set[str] = set()
    patterns = [
        re.compile(
            rf"(?<![\w./@-])({re.escape(name)})"
            r"(?:\[[^\]]*\])?(?:[<>=!~][^\s]*)?(?![\w./@-])",
            re.IGNORECASE,
        )
        for name in names
    ]

    def _process(line: str) -> Optional[str]:
        new_line = line
        for pat in patterns:
            def _sub(m: "re.Match[str]") -> str:
                removed.add(m.group(1).lower())
                return ""
            new_line = pat.sub(_sub, new_line)
        # Collapse whitespace the removals left behind.
        new_line = re.sub(r"[ \t]{2,}", " ", new_line).rstrip()
        # Any real package token left? (ignore pip/install keywords + flags).
        for tok in _PIP_TOKEN_RE.finditer(new_line):
            if tok.group("name").lower() not in _PIP_NON_PACKAGE_TOKENS:
                return new_line
        return None  # only pip/install/flags remain → drop the line

    changed = False
    out_lines: List[str] = []
    for line in dockerfile.splitlines():
        if _LINE_IS_RUN_PIP_RE.match(line):
            processed = _process(line)
            if processed is None:
                changed = True
                continue
            if processed != line:
                changed = True
            out_lines.append(processed)
        else:
            out_lines.append(line)

    if not changed:
        return dockerfile, ()
    new_df = "\n".join(out_lines)
    if dockerfile.endswith("\n"):
        new_df += "\n"
    return new_df, tuple(sorted(removed))


# ---------------------------------------------------------------------------
# entrypoint.sh correctness — two deterministic shell-bug fixes
# ---------------------------------------------------------------------------
#
# The weak generator emits two recurring, mechanical entrypoint.sh bugs:
#   1. `[ -s "$IN/*.txt" ]` — a glob inside double quotes NEVER expands, so the
#      test is always false (or tests a literal filename with a `*` in it).
#   2. `[ -d "$OUT_DIR" ] || mkdir -p "$OUT_DIR" && exit 1` — shell `&&`/`||`
#      are left-associative with equal precedence, so this parses as
#      `( [ -d ] || mkdir ) && exit 1` and exits 1 whenever the dir already
#      exists (the common case). Creating an output dir must never exit.

_QUOTED_GLOB_TEST_RE = re.compile(
    r'\[\s*(!\s*)?-[a-zA-Z]+\s+"(\$\{?\w+\}?)/([^"\n]*\*[^"\n]*)"\s*\]'
)
_MKDIR_EXIT_RE = re.compile(
    r'(mkdir\s+-p\s+(?:"[^"\n]*"|[^"\s&|]+))\s*&&\s*exit\s+\d+'
)


def sanitize_entrypoint_sh(entrypoint_sh: str) -> Tuple[str, List[str]]:
    """Fix the two mechanical entrypoint.sh bugs the weak generator repeats.

    Conservative + targeted: only the exact quoted-glob-test and
    ``mkdir -p ... && exit N`` antipatterns are rewritten; everything else is
    preserved byte-for-byte. Returns ``(new_sh, notes)`` where ``notes`` lists
    what was changed (for the post-process log).
    """
    if not entrypoint_sh or not entrypoint_sh.strip():
        return entrypoint_sh, []

    notes: List[str] = []
    sh = entrypoint_sh

    # Bug 1 — quoted glob in a test never expands. Rewrite to a glob-safe
    # existence probe: `ls "$VAR"/<pattern> >/dev/null 2>&1`.
    glob_count = 0

    def _fix_glob(m: "re.Match[str]") -> str:
        nonlocal glob_count
        glob_count += 1
        neg, var, pattern = m.group(1), m.group(2), m.group(3)
        check = f'ls "{var}"/{pattern} >/dev/null 2>&1'
        return ("! " + check) if neg else check

    sh = _QUOTED_GLOB_TEST_RE.sub(_fix_glob, sh)
    if glob_count:
        notes.append(
            f"rewrote {glob_count} quoted-glob input test(s) "
            f"(a glob inside quotes never expands) to a glob-safe `ls` probe"
        )

    # Bug 2 — `mkdir -p X && exit N` (shell precedence spuriously exits).
    exit_count = 0

    def _fix_mkdir(m: "re.Match[str]") -> str:
        nonlocal exit_count
        exit_count += 1
        return m.group(1)

    sh = _MKDIR_EXIT_RE.sub(_fix_mkdir, sh)
    if exit_count:
        notes.append(
            f"removed {exit_count} spurious `&& exit N` after `mkdir -p` "
            f"(shell &&/|| precedence made the container exit on success)"
        )

    return sh, notes
