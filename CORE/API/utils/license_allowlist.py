"""δ Phase 1 — license allowlist for code we pull into tenant builds.

The wizard's Describe-and-build flow lets users wrap an existing
GitHub repo into a tenant-runnable Docker image. The wizard's
existing repo-source flow lets users paste any git URL. Both flows
funnel into the same Containerizer + ImageBuilder pipeline, so
licence-incompatible code can end up in a tenant-facing image —
which propagates copyleft obligations onto MOD Core's distribution
channel.

Hard-block list and allowlist are intentionally separate (instead
of a single allowlist):

- ALLOWED is the safelist we let through without question
  (permissive licences whose obligations are notice-only).
- BLOCKED enumerates copyleft + non-commercial licences we refuse
  outright, with a comment explaining WHY each one is blocked.
- Anything else (unknown SPDX, null, blank) is treated as blocked
  by ``is_allowed()`` — the conservative default. The platform
  admin can extend ALLOWED if they vet new licences with legal.

Naming: SPDX identifiers (https://spdx.org/licenses/). Compared
case-insensitively because GitHub returns mixed-case (`mit` /
`MIT` / `mit-0` etc).
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, Optional


# Permissive licences whose only obligation is preserving the
# copyright + licence notice. Safe to include in a SaaS deployment
# without legal review.
ALLOWED_LICENSES: FrozenSet[str] = frozenset({
    "MIT",
    "MIT-0",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD-3-Clause-Clear",
    "ISC",
    "0BSD",
    "Unlicense",
    "CC0-1.0",
    "MPL-2.0",          # weak copyleft, file-level only — allowed
    "Zlib",
    "BlueOak-1.0.0",
    "BSL-1.0",          # Boost Software License
    "Python-2.0",
    "PostgreSQL",
})


# Copyleft + non-commercial licences. Hard-blocked because their
# obligations propagate to MOD Core's distribution channel:
# - GPL/AGPL/LGPL: copyleft contagion to anything we link/bundle.
# - SSPL/CC-BY-NC*: non-commercial restriction is incompatible
#   with paid SaaS deployment.
# - EUPL: weak copyleft but EU-jurisdiction-coupled; needs legal.
BLOCKED_LICENSES: FrozenSet[str] = frozenset({
    "GPL-1.0", "GPL-1.0-only", "GPL-1.0-or-later",
    "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.0", "LGPL-2.0-only", "LGPL-2.0-or-later",
    "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "SSPL-1.0",
    "CC-BY-NC-1.0", "CC-BY-NC-2.0", "CC-BY-NC-2.5",
    "CC-BY-NC-3.0", "CC-BY-NC-4.0",
    "CC-BY-NC-SA-1.0", "CC-BY-NC-SA-2.0", "CC-BY-NC-SA-2.5",
    "CC-BY-NC-SA-3.0", "CC-BY-NC-SA-4.0",
    "CC-BY-NC-ND-1.0", "CC-BY-NC-ND-2.0", "CC-BY-NC-ND-2.5",
    "CC-BY-NC-ND-3.0", "CC-BY-NC-ND-4.0",
    "EUPL-1.0", "EUPL-1.1", "EUPL-1.2",
    "OSL-1.0", "OSL-2.0", "OSL-2.1", "OSL-3.0",
    "EPL-1.0", "EPL-2.0",  # weak copyleft, conservatively blocked
    "CDDL-1.0", "CDDL-1.1",
    "MS-RL",  # Microsoft Reciprocal — copyleft
    "Parity-7.0.0",
})


# Strong-copyleft SYSTEM (apt) packages. The SPDX allowlist above only
# vets the wrapped repo's licence — it's blind to the apt/system deps a
# generated or pasted Dockerfile pulls in, which is exactly how a GPL
# engine (poppler for PDF) ends up in an image the platform advertises
# as permissive-only. This map closes that gap. Strong copyleft only
# (GPL/AGPL); LGPL shared libs are dynamic-link-fine and omitted.
SYSTEM_PACKAGE_LICENSES: Dict[str, str] = {
    "poppler-utils": "GPL-2.0",
    "libpoppler-dev": "GPL-2.0",
    "poppler-data": "GPL-2.0",
    "ghostscript": "AGPL-3.0",
    "libgs-dev": "AGPL-3.0",
    "libgs9": "AGPL-3.0",
}

# Strong-copyleft PyPI packages. The apt map above catches copyleft system
# libs; this is its peer for `pip install` requirements. The synthesize
# step can't resolve a wheel's real SPDX without network metadata, so we
# curate the GPL/AGPL packages that recur in PDF/document/media node builds
# (the exact ones the synthesize prompt steers away from). Bundling one of
# these into a node image IS derivation — keep it out of permissive-only
# builds. Strong copyleft only (GPL/AGPL/SSPL); names are PEP 503-normalised
# (lowercase, runs of -_. collapsed to a single -).
PYPI_PACKAGE_LICENSES: Dict[str, str] = {
    "pymupdf": "AGPL-3.0",       # fitz — AGPL unless you buy a commercial licence
    "fitz": "AGPL-3.0",          # PyMuPDF's import name, sometimes pinned directly
    "pymupdfb": "AGPL-3.0",      # PyMuPDF base wheel
    "pdf2image": "GPL-2.0",      # pure-python, but shells out to poppler (GPL)
    "python-poppler": "GPL-2.0",
    "pdftotext": "GPL-2.0",      # wraps poppler
    "wand": "AGPL-3.0",          # ImageMagick binding → pulls Ghostscript (AGPL)
    "ghostscript": "AGPL-3.0",   # python-ghostscript binding
}

# Permissive replacements to suggest when we flag a copyleft dep.
PERMISSIVE_ALTERNATIVES: Dict[str, str] = {
    "poppler-utils": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "ghostscript": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "pymupdf": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "fitz": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "pymupdfb": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "pdf2image": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "python-poppler": "pypdfium2 (BSD/Apache) for PDF rasterization",
    "pdftotext": "pypdfium2 (BSD/Apache, exposes get_textpage) for PDF text",
    "wand": "Pillow (permissive) for raster images",
}

# Match `apt-get install` (or `apt install`) and capture the rest of the
# logical line so we can pull package tokens out of it.
_APT_INSTALL_RE = re.compile(
    r"apt(?:-get)?\s+install\b(?P<rest>[^\n]*)", re.IGNORECASE
)


def _apt_packages_in(dockerfile: str) -> List[str]:
    """Best-effort extraction of package names from a Dockerfile's
    `apt-get install` lines. Drops flags (-y, --no-install-recommends),
    apt option assignments, and stops a token run at shell separators
    (&&, ;, |) so trailing commands (rm -rf …) aren't mistaken for
    packages."""
    pkgs: List[str] = []
    for m in _APT_INSTALL_RE.finditer(dockerfile or ""):
        rest = m.group("rest")
        # Cut at the first shell separator — packages only precede it.
        rest = re.split(r"&&|;|\|", rest, maxsplit=1)[0]
        for tok in rest.replace("\\", " ").split():
            if tok.startswith("-"):
                continue          # flag (-y, --no-install-recommends)
            if "=" in tok:
                tok = tok.split("=", 1)[0]   # pkg=version pin
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", tok):
                pkgs.append(tok)
    return pkgs


def apt_license_warnings(dockerfile: str) -> List[str]:
    """Scan a Dockerfile's apt installs and return one warning per
    strong-copyleft system package. Empty when clean. Warn-don't-block:
    the chosen SOURCE repo stays hard-blocked by is_blocked(), but a
    transitive system lib only warrants a surfaced notice (subprocess
    use is aggregation, not derivation) — and on-prem the operator owns
    the call."""
    out: List[str] = []
    seen = set()
    for pkg in _apt_packages_in(dockerfile):
        spdx = SYSTEM_PACKAGE_LICENSES.get(pkg)
        if spdx and pkg not in seen:
            seen.add(pkg)
            alt = PERMISSIVE_ALTERNATIVES.get(pkg)
            msg = (
                f"System package '{pkg}' is {spdx}-licensed (copyleft) — "
                f"it ships a GPL/AGPL binary in a tenant-facing image the "
                f"platform advertises as permissive-only."
            )
            if alt:
                msg += f" Prefer {alt}."
            out.append(msg)
    return out


def _pip_requirement_name(req: str) -> str:
    """Extract the PEP 503-normalised distribution name from a PEP 508
    requirement string. Strips extras, version specifiers, environment
    markers, and returns "" for URL/VCS/editable installs (which name no
    PyPI package we can map a licence to)."""
    s = (req or "").strip().lower()
    if not s or s.startswith(("-", "git+", "http://", "https://", ".", "/")):
        return ""
    s = s.split(";", 1)[0].strip()                 # drop environment marker
    s = re.split(r"[\[<>=!~ @]", s, 1)[0].strip()  # drop extras / specifier / url
    return re.sub(r"[-_.]+", "-", s)               # PEP 503 normalisation


def pip_license_warnings(requirements) -> List[str]:
    """Return one message per requirement that names a known strong-copyleft
    (GPL/AGPL) PyPI package. Empty when clean.

    Unlike the apt scan (aggregation → warn), a pip dependency is bundled
    into the node image — that's derivation — so callers REJECT on a hit to
    keep copyleft out of permissive-only builds. The synthesize prompt
    steers the LLM toward permissive engines; this is the hard backstop for
    when the model ignores the steer."""
    out: List[str] = []
    if not isinstance(requirements, (list, tuple)):
        return out
    seen = set()
    for req in requirements:
        name = _pip_requirement_name(str(req or ""))
        spdx = PYPI_PACKAGE_LICENSES.get(name)
        if spdx and name not in seen:
            seen.add(name)
            alt = PERMISSIVE_ALTERNATIVES.get(name)
            msg = (
                f"requirement '{name}' is {spdx}-licensed (copyleft) — it would "
                f"bundle GPL/AGPL code into a node image the platform advertises "
                f"as permissive-only."
            )
            if alt:
                msg += f" Use {alt} instead."
            out.append(msg)
    return out


def _normalise(spdx: Optional[str]) -> str:
    if not isinstance(spdx, str):
        return ""
    return spdx.strip()


def _matches_set(spdx: str, names: FrozenSet[str]) -> bool:
    """Case-insensitive membership."""
    if not spdx:
        return False
    target = spdx.lower()
    for entry in names:
        if entry.lower() == target:
            return True
    return False


def is_allowed(spdx: Optional[str]) -> bool:
    """True iff the SPDX licence is on the explicit allowlist.

    Conservative default: unknown / null / blank → False.
    Use this at the gate; never assume "we don't know what it is,
    let it through" — that's how copyleft contagion happens.
    """
    spdx = _normalise(spdx)
    return _matches_set(spdx, ALLOWED_LICENSES)


def is_blocked(spdx: Optional[str]) -> bool:
    """True iff the SPDX licence is on the explicit hard-block
    list. Returned alongside ``is_allowed`` so callers can give a
    user-facing reason ("GPL-3.0 is hard-blocked because…") instead
    of a generic "not on the allowlist".
    """
    spdx = _normalise(spdx)
    return _matches_set(spdx, BLOCKED_LICENSES)


def block_reason(spdx: Optional[str]) -> str:
    """Human-readable explanation of why a licence is blocked.
    Returned in API errors so the user sees the legal rationale,
    not just a "rejected" message.
    """
    spdx = _normalise(spdx)
    if not spdx:
        return (
            "This repository has no detectable open-source licence. "
            "We can only build tenant-facing images from code that "
            "is explicitly licensed under one of the permissive "
            "open-source licences we trust (MIT, Apache-2.0, BSD, "
            "ISC, MPL-2.0, etc.)."
        )
    if is_blocked(spdx):
        if spdx.upper().startswith("GPL") or spdx.upper().startswith("LGPL") or spdx.upper().startswith("AGPL"):
            return (
                f"This repository is licensed under {spdx}, a copyleft "
                "licence. Bundling copyleft code into a tenant-facing "
                "image would propagate the licence's source-disclosure "
                "obligations to MOD Core's distribution channel, "
                "including SaaS users. Pick a repository under MIT, "
                "Apache-2.0, BSD, ISC, or MPL-2.0 instead."
            )
        if "NC" in spdx.upper() or "SSPL" in spdx.upper():
            return (
                f"This repository is licensed under {spdx}, which "
                "restricts commercial use. We can't include it in a "
                "paid SaaS deployment."
            )
        return (
            f"{spdx} is on our blocked-licence list. Pick a repository "
            "with a permissive open-source licence (MIT, Apache-2.0, "
            "BSD, ISC, MPL-2.0)."
        )
    if not is_allowed(spdx):
        return (
            f"{spdx} is not on our allowlist. We only accept code "
            "under MIT, Apache-2.0, BSD-2/3-Clause, ISC, MPL-2.0, "
            "or other vetted permissive licences. If you believe "
            "this licence should be allowed, ask your platform admin "
            "to extend the allowlist after legal review."
        )
    return ""
