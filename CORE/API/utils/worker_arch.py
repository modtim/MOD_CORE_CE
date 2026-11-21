"""Canonical worker CPU architecture normalisation.

One alias map, one function, used at every point a worker's reported arch is
read or stored (heartbeat ingest, claim guard, worker detail). Go reports GOARCH
("amd64", "arm64") but `uname -m` spellings ("x86_64", "aarch64") reach the same
field from other paths, and an unnormalised "x86_64" failing to match "amd64"
would silently strand a perfectly ordinary worker — or display it wrong.

Normalising ONCE at ingest (before storing on hardware_specs) means every
downstream read sees the canonical form. The claim guard keeps applying the map
anyway so rows written before this existed are still gated correctly.

Unknown values fall through unchanged (lower-cased); an empty value normalises
to "" so the caller can apply its own fail-closed default (amd64).
"""
from __future__ import annotations

from typing import Optional

# `uname -m` / Go GOARCH spellings -> canonical GOARCH label.
_ARCH_ALIASES = {
    "x86_64": "amd64",
    "x64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "armv8": "arm64",
    "arm64": "arm64",
}


def normalize_arch(raw: Optional[str]) -> str:
    """Return the canonical GOARCH label for a reported architecture.

    Empty/None -> "". Unknown -> the lower-cased input unchanged (callers apply
    their own fail-closed default, which is amd64 everywhere here).
    """
    canonical = str(raw or "").strip().lower()
    if not canonical:
        return ""
    return _ARCH_ALIASES.get(canonical, canonical)
