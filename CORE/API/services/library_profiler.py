"""Library profiler — learns GPU requirements per package, caches in
``library_profiles``.

Signature: ``profile(package: str) -> str`` returns one of
``'CPU' | 'CUDA' | 'ROCM' | 'UNKNOWN'``.

Lookup order on cache miss:
  1. Curated seed (``node_builder.gpu_library_index.CUDA_REQUIRED``)
  2. PyPI JSON metadata (classifier check)
  3. Recursive walk of declared deps (depth 3, short-circuit on CUDA)
  4. Default to CPU when nothing else fires

Cache is refreshed when older than 30 days. Network failures during a
miss return UNKNOWN and DO NOT persist — we'll retry next time rather
than poison the cache with a transient fetch error.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Set

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from database import SessionLocal
from node_builder.gpu_library_index import (
    ACCELERATOR_LIBRARIES,
    MULTI_TARGET_DEFAULTS,
    _normalise,
)

logger = logging.getLogger(__name__)

_PYPI_TIMEOUT_S = 5.0
_MAX_DEPTH = 3
_FRESH_DAYS = 30

# wizard-knowledge-pack Phase 1 (986b) — source-province ranking for
# library_profiles writes: manual > local_capture > mod_pack. A write
# whose source ranks below the existing row's source never overwrites
# it (see _persist). Unknown sources rank 0.
_SOURCE_PRIORITY = {"manual": 3, "local_capture": 2, "mod_pack": 1}
_DEP_NAME_RE = re.compile(r"^\s*([a-zA-Z][\w.\-]*)")

# PyPI classifier prefix → accelerator type. Only CUDA + ROCm have
# standardised trove classifiers; the rest of the accelerators we
# track (TPU/Habana/IPU/Inferentia/MPS) have no classifier so they
# rely on the curated seed + transitive dep walk only.
_CLASSIFIER_PATTERNS = (
    ("Environment :: GPU :: NVIDIA CUDA", "CUDA"),
    ("Environment :: GPU :: AMD ROCm", "ROCM"),
)

# Cache the curated lists in pre-normalised form: accelerator type →
# frozenset of normalised library names. Iteration order matters for
# the (rare) library that appears in multiple curated lists — first
# match wins. We rely on dict insertion order matching
# ACCELERATOR_LIBRARIES (Python 3.7+ guarantee).
_CURATED: dict[str, frozenset[str]] = {
    accel_type: frozenset(_normalise(n) for n in libs)
    for accel_type, libs in ACCELERATOR_LIBRARIES.items()
}

# Same shape for the multi-target defaults — normalised lookup key
# to the default accelerator type. Library is multi-target so any
# variant will work; this is just the wizard's pre-fill default.
_MULTI_TARGET: dict[str, str] = {
    _normalise(name): accel_type
    for name, accel_type in MULTI_TARGET_DEFAULTS.items()
}


def _curated_match(norm: str) -> Optional[str]:
    """Return the accelerator type if ``norm`` is in any curated set,
    else None. First match wins (CUDA first per ACCELERATOR_LIBRARIES
    ordering)."""
    for accel_type, libs in _CURATED.items():
        if norm in libs:
            return accel_type
    return None


def profile(
    package: str,
    *,
    db: Optional[Session] = None,
    _depth: int = 0,
    _seen: Optional[Set[str]] = None,
) -> str:
    """Return the GPU requirement for ``package``.

    Recursive calls share ``_seen`` and ``_depth`` to bound the walk
    and break cycles. Public callers always invoke with the defaults.
    """
    norm = _normalise(package)
    if not norm:
        return "UNKNOWN"

    if _seen is None:
        _seen = set()
    if norm in _seen:
        # cycle — treat as no-evidence-from-this-branch
        return "UNKNOWN"
    _seen.add(norm)
    if _depth >= _MAX_DEPTH:
        return "UNKNOWN"

    # We use our own session so the profiler can be called from any
    # request context without threading a session through the analyze
    # endpoint and the curated-seed override path. If the DB isn't
    # initialised (unit tests of upstream callers, scripted smoke
    # tools), we still want a useful answer — fall back to the
    # curated-seed-only fast path with no persistence.
    own_session = db is None
    session: Optional[Session] = db
    if session is None:
        try:
            session = SessionLocal()
        except RuntimeError:
            # No DB — curated + multi-target lookup only, no caching, no PyPI.
            # Mirrors the full path's lookup order so callers that
            # can't initialise a DB (unit tests, scripted smoke tools)
            # still get the same answer for known libs.
            curated = _curated_match(norm)
            if curated is not None:
                return curated
            if norm in _MULTI_TARGET:
                return _MULTI_TARGET[norm]
            return "UNKNOWN"
    try:
        # 1. Cache hit + fresh?
        row = session.get(models.LibraryProfile, norm)
        if row is not None:
            age = datetime.now(timezone.utc) - row.learned_at
            if age < timedelta(days=_FRESH_DAYS):
                return row.gpu_required

        # 2. Curated seed — covers all accelerator types we know of.
        curated = _curated_match(norm)
        if curated is not None:
            _persist(session, norm, curated, f"curated seed (hand-maintained {curated}-required list)")
            return curated

        # 2b. Multi-target libraries — work on multiple accelerators
        # depending on configuration. Pick a deterministic default
        # so the wizard has something to pre-fill; variants for
        # other accelerators get built downstream.
        if norm in _MULTI_TARGET:
            mt_type = _MULTI_TARGET[norm]
            _persist(session, norm, mt_type, f"multi-target library — default {mt_type} (variants will cover other accelerators)")
            return mt_type

        # 3. PyPI metadata
        meta = _fetch_pypi(norm)
        if meta is None:
            # Network failed or 404 — don't persist; let next call retry.
            return "UNKNOWN"

        info = meta.get("info") or {}

        # Classifier check — only CUDA + ROCm have standardised classifiers.
        for c in info.get("classifiers") or []:
            for prefix, accel_type in _CLASSIFIER_PATTERNS:
                if prefix in c:
                    _persist(session, norm, accel_type, f"PyPI classifier: {c}")
                    return accel_type

        # 4. Walk declared deps (recursive, short-circuit on any accelerator hit)
        for dep_spec in info.get("requires_dist") or []:
            dep_name = _parse_dep_name(dep_spec)
            if not dep_name:
                continue
            sub = profile(dep_name, db=session, _depth=_depth + 1, _seen=_seen)
            if sub not in (None, "CPU", "UNKNOWN"):
                _persist(session, norm, sub, f"transitive dep: {dep_name} requires {sub}")
                return sub

        # 5. No accelerator signal found
        _persist(session, norm, "CPU", "no accelerator classifier or transitive accelerator dep")
        return "CPU"
    finally:
        if own_session:
            session.close()


def _fetch_pypi(package: str) -> Optional[dict]:
    """Return parsed PyPI JSON or None on any failure. Never raises."""
    # GSEC FX.19 R-042 / GY.C8 — wizard internet lookups refused (sealed
    # mode or egress.wizard_internet_enabled False): no outbound call.
    from utils import egress_policy
    if egress_policy.refuse_reason(egress_policy.CATEGORY_WIZARD_INTERNET):
        logger.info("library_profiler: PyPI fetch refused (egress disabled)")
        return None
    try:
        r = httpx.get(
            f"https://pypi.org/pypi/{package}/json",
            timeout=_PYPI_TIMEOUT_S,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.info("library_profiler: PyPI fetch failed for %s: %s", package, e)
        return None


def _parse_dep_name(dep_spec: str) -> Optional[str]:
    """Extract the bare package name from a PyPI requires_dist entry.

    ``tinycudann>=1.0`` → ``tinycudann``
    ``torch[cuda]==2.1`` → ``torch``
    ``foo; python_version >= "3.8"`` → ``foo``
    """
    m = _DEP_NAME_RE.match(dep_spec or "")
    return m.group(1).lower() if m else None


def _source_priority(source: Optional[str]) -> int:
    """wizard-knowledge-pack Phase 1 (986b): source-province ranking.

    manual > local_capture > mod_pack. Unknown/NULL sources rank 0 so
    any real write can supersede them. Used by _persist to decide
    whether an incoming write may overwrite an existing row.
    """
    return _SOURCE_PRIORITY.get(source, 0)


def _persist(
    session: Session,
    name: str,
    gpu_required: str,
    evidence: str,
    source: str = "local_capture",
    pack_version: Optional[str] = None,
) -> None:
    """Upsert one row. Uses simple get-then-update/insert; the
    profile() function is the only writer and is called from request
    handlers (not concurrent batch jobs), so we don't need
    ON CONFLICT for correctness.

    wizard-knowledge-pack Phase 1 (986b): a write never downgrades an
    existing row's source — a mod_pack write can't clobber a manual or
    local_capture row. Equal-or-higher priority writes refresh the row
    (this keeps the existing 30-day refresh semantics intact for
    local_capture re-learns)."""
    existing = session.get(models.LibraryProfile, name)
    now = datetime.now(timezone.utc)
    if existing is not None:
        if _source_priority(source) >= _source_priority(existing.source):
            existing.gpu_required = gpu_required
            existing.evidence = evidence[:500]
            existing.source = source
            existing.pack_version = pack_version
            existing.learned_at = now
    else:
        session.add(models.LibraryProfile(
            name=name,
            gpu_required=gpu_required,
            evidence=evidence[:500],
            learned_at=now,
            source=source,
            pack_version=pack_version,
        ))
    session.commit()
