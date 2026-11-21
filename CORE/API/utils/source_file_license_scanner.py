"""δ Phase 12 — heuristic licence-header scanner for uploaded
source files.

Phase 11 catches GPL/AGPL when the user supplies a GitHub URL —
GitHub's licence detection is the source of truth. But the wizard's
Script source-type accepts a free-form upload (single file or
multi-file bundle) with no upstream metadata. A user can paste
GPL'd code into the editor and it'd ship into the build context
unchecked.

This scanner applies a lightweight heuristic to file BODIES:
SPDX-License-Identifier comments + a small set of high-confidence
copyleft phrases (`GNU General Public License`, `GNU Affero
General Public License`, etc.). It refuses files whose headers
explicitly declare a hard-blocked licence.

It is NOT a full licence audit. A user who strips the header
escapes the check — but most legitimate copyleft code carries
its header religiously, and the goal is to catch the
copy-paste-without-thinking case.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from utils import license_allowlist


# SPDX-License-Identifier comments are the structured signal we
# trust most. RFC: https://spdx.dev/ids/.
_SPDX_RE = re.compile(
    r"SPDX-License-Identifier\s*[:=]?\s*([A-Za-z0-9.\-+]+)",
    re.IGNORECASE,
)

# Free-text patterns. Each pattern maps to an SPDX label so
# block_reason can surface a usable message. Ordered by
# specificity (AGPL before GPL).
_FREE_TEXT_PATTERNS: List[tuple[re.Pattern[str], str]] = [
    (re.compile(r"GNU\s+Affero\s+General\s+Public\s+License", re.IGNORECASE), "AGPL-3.0"),
    (re.compile(r"GNU\s+Lesser\s+General\s+Public\s+License", re.IGNORECASE), "LGPL-3.0"),
    (re.compile(r"GNU\s+General\s+Public\s+License,\s*Version\s*3", re.IGNORECASE), "GPL-3.0"),
    (re.compile(r"GNU\s+General\s+Public\s+License,\s*Version\s*2", re.IGNORECASE), "GPL-2.0"),
    (re.compile(r"GNU\s+General\s+Public\s+License", re.IGNORECASE), "GPL-3.0"),
    (re.compile(r"Server\s+Side\s+Public\s+License", re.IGNORECASE), "SSPL-1.0"),
    (
        re.compile(r"Creative\s+Commons\s+Attribution[\-\s]NonCommercial", re.IGNORECASE),
        "CC-BY-NC-4.0",
    ),
]

# Truncate scanned body to head — licence headers ALWAYS appear at
# the top of the file. Scanning the whole body wastes time on
# large generated bundles.
_HEADER_BYTES = 8 * 1024  # first 8 KB


@dataclass
class FileScanResult:
    filename: str
    ok: bool
    detected_license: Optional[str] = None
    reasons: List[str] = field(default_factory=list)


def scan_file(filename: str, content: str) -> FileScanResult:
    """Inspect a single file's header for a hard-blocked licence
    declaration. Returns ``ok=False`` with a reason when one is
    detected; ``ok=True`` otherwise (including when no licence is
    declared — the absence of a header is not on its own a refusal
    signal at this layer)."""
    if not isinstance(content, str):
        return FileScanResult(filename=filename, ok=True)
    head = content[:_HEADER_BYTES]
    # SPDX first — structured + authoritative.
    m = _SPDX_RE.search(head)
    if m:
        spdx = m.group(1).strip()
        if license_allowlist.is_blocked(spdx):
            return FileScanResult(
                filename=filename,
                ok=False,
                detected_license=spdx,
                reasons=[
                    f"{filename!r} declares SPDX-License-Identifier: {spdx}, "
                    "which is on the hard-blocked list. "
                    + license_allowlist.block_reason(spdx)
                ],
            )
        # Allowed / unknown SPDX → return ok with the detected
        # label so callers can surface "MIT-licensed file" UX.
        return FileScanResult(
            filename=filename,
            ok=True,
            detected_license=spdx,
        )
    # Free-text fall-through.
    for pattern, label in _FREE_TEXT_PATTERNS:
        if pattern.search(head):
            return FileScanResult(
                filename=filename,
                ok=False,
                detected_license=label,
                reasons=[
                    f"{filename!r} appears to declare {label} in its header text. "
                    + license_allowlist.block_reason(label)
                ],
            )
    return FileScanResult(filename=filename, ok=True)


@dataclass
class BundleScanResult:
    ok: bool
    files: List[FileScanResult] = field(default_factory=list)
    blocking_files: List[FileScanResult] = field(default_factory=list)


def scan_bundle(files: List[dict]) -> BundleScanResult:
    """Run scan_file across every entry in a multi-file source
    bundle. Returns a structured result with the per-file detail
    and an overall ok flag (False if ANY file failed)."""
    out = BundleScanResult(ok=True)
    for f in files or []:
        if not isinstance(f, dict):
            continue
        result = scan_file(
            filename=f.get("filename") or "<unnamed>",
            content=f.get("body") or "",
        )
        out.files.append(result)
        if not result.ok:
            out.ok = False
            out.blocking_files.append(result)
    return out
