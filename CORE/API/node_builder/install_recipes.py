"""Move C — install-recipe persistence helpers.

Two halves:
  capture: given a successfully-built spec (smoke passed), extract
           the install RUN/COPY block from the working Dockerfile
           and store it on the LibraryInstallRecipe rows (per-tenant)
           for every library the build imports.
  read:    given a list of library names + the target accelerator,
           return the concatenated known-working recipes block as a
           string the repair LLM prompt can paste in.

The capture writes are best-effort: any failure logs a warning and
does NOT block the smoke-stage callback (we don't want a recipe-
write bug to fail a successful build).

GB.LEAK.5 — re-scoped to use LibraryInstallRecipe (per-tenant) instead
of the legacy LibraryProfile JSON columns (install_recipe_by_accel /
successful_build_count_by_accel), which leaked cross-tenant Dockerfile
install blocks.

10.3.s1 — the accelerator counters (success counts per library x
accelerator) are keyed by tenant: ``tenant_accel_counts`` returns ONE
tenant's accel->count map, and ``platform_accel_counts`` is the explicit
opt-in aggregate (sum across tenants) for the rare caller that genuinely
wants the platform-wide number. Nothing reads the legacy global
``library_profiles.successful_build_count_by_accel`` JSONB any more, so
no tenant's runs can move another tenant's numbers.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from node_builder.gpu_library_index import _normalise, extract_package_candidates

logger = logging.getLogger(__name__)


# We keep only the RUN/COPY/ENV lines from the working Dockerfile —
# not the FROM (base image is decided independently per build) and
# not the CMD/ENTRYPOINT (which is user-script-specific). These are
# the install steps that survive across builds touching the same lib.
_KEEP_DIRECTIVES = ("RUN", "COPY", "ENV", "ARG", "WORKDIR")


def extract_install_block(dockerfile: str) -> str:
    """Pull the RUN/COPY/ENV block from a working Dockerfile and
    return it as a string the LLM can paste back in.

    Strips FROM (base-image decision is per-build) and CMD/ENTRYPOINT
    (user-script-specific). Multi-line continuations (backslash) are
    preserved.
    """
    if not dockerfile:
        return ""
    out: List[str] = []
    lines = dockerfile.split("\n")
    keep_running = False
    for raw in lines:
        line = raw.rstrip()
        # Continuation of a multi-line directive we're keeping.
        if keep_running:
            out.append(raw)
            keep_running = line.endswith("\\")
            continue
        # New directive — keep only the whitelisted ones.
        stripped = line.lstrip()
        upper = stripped.upper()
        if any(upper.startswith(d + " ") or upper == d for d in _KEEP_DIRECTIVES):
            out.append(raw)
            keep_running = line.endswith("\\")
        # else: drop (FROM, CMD, ENTRYPOINT, USER, EXPOSE, etc.)
    return "\n".join(out).strip()


def _libraries_referenced(spec_data: dict) -> List[str]:
    """Pull the library names referenced by the spec's source. Uses
    the same extractor the GPU-library override uses so the two
    stay consistent."""
    text_parts: List[str] = []
    if spec_data.get("inline_source_body"):
        text_parts.append(spec_data["inline_source_body"])
    if spec_data.get("hero_dockerfile"):
        text_parts.append(spec_data["hero_dockerfile"])
    if spec_data.get("entrypoint_sh"):
        text_parts.append(spec_data["entrypoint_sh"])
    if not text_parts:
        return []
    text = "\n".join(text_parts)
    return [_normalise(c) for c in extract_package_candidates(text)]


def capture_install_recipe(
    db: Session,
    *,
    tenant_id: Optional[int],
    spec_data: dict,
    accelerator: str,
    dockerfile: str,
) -> int:
    """Persist the install block from a working Dockerfile onto the
    LibraryInstallRecipe rows (per-tenant) for every library the build
    imports. Returns the count of recipes updated (0 if no libraries
    matched or extraction yielded an empty block).

    Best-effort: caller should wrap in try/except and not fail on
    error. We don't want a recipe-write bug to fail a successful
    build.

    GB.LEAK.5 — writes LibraryInstallRecipe for (tenant_id, lib, accel)
    only; the legacy global LibraryProfile JSON columns are no longer
    written. A LibraryProfile row must still exist for the library.
    ``tenant_id`` is required (keyword-only); ``None`` is a no-op.
    """
    if tenant_id is None or not accelerator or not dockerfile:
        return 0
    accel_key = accelerator.lower().strip()
    install_block = extract_install_block(dockerfile)
    if not install_block:
        return 0

    libs = _libraries_referenced(spec_data)
    if not libs:
        return 0

    updated = 0
    changed_pairs: List[Tuple[str, str]] = []
    for lib_name in libs:
        # Only record recipes for libraries we KNOW about (a LibraryProfile
        # row created by the profiler). Unknown libs likely aren't on PyPI.
        if db.get(models.LibraryProfile, lib_name) is None:
            continue
        row = _active_recipe(db, tenant_id, lib_name, accel_key)
        if row is None:
            row = models.LibraryInstallRecipe(
                tenant_id=tenant_id,
                library_name=lib_name,
                accelerator=accel_key,
                recipe=install_block,
                success_count=0,
                provenance="captured",
            )
            db.add(row)
        else:
            # Only overwrite with a longer block, so a trivial later build
            # can't clobber a rich established recipe with a stripped one.
            if len(install_block) < len(row.recipe or ""):
                continue
            row.recipe = install_block
        # Item #10 — every successful capture is a vote this recipe works.
        row.success_count = int(row.success_count or 0) + 1
        updated += 1
        changed_pairs.append((lib_name, accel_key))

    if updated:
        db.commit()
        # GO.8 (23.7) — a saved install recipe IS a recipe change: hand the
        # (library, accelerator) pairs that actually moved to the on-change
        # gate so ONLY the variants that reference them are re-verified.
        # Best-effort: a gate failure must never fail the user's save.
        _notify_change_gate(db, changed_pairs)
    return updated


def _notify_change_gate(db, changed_pairs: Iterable[Tuple[str, str]]) -> None:
    """Best-effort on-change gate hook for a saved install recipe (GO.8 23.7).

    ``node_builder.variant_test.change_gate.enqueue_on_change`` is dark by
    default (``node_build.variant_change_gate_enabled``) and no-ops when off,
    so this costs nothing until the gate is turned on in the platform-settings
    registry. Any failure — import, DB, settings — is logged and swallowed:
    the recipe save already succeeded and must not be failed by the gate.
    """
    pairs = [(str(lib), str(accel)) for lib, accel in changed_pairs if lib and accel]
    if not pairs:
        return
    try:
        from node_builder.variant_test import change_gate as _change_gate
        summary = _change_gate.enqueue_on_change(db, recipes=pairs)
        if summary.get("enqueued"):
            logger.info(
                "change_gate: recipe change re-queued %d affected variant(s) "
                "for pairs=%s",
                summary["enqueued"], sorted(pairs),
            )
    except Exception:  # noqa: BLE001 — the save already succeeded
        logger.warning("change_gate recipe hook failed (non-fatal): %s", pairs, exc_info=True)


def _active_recipe(db: Session, tenant_id: int, lib_name: str, accel_key: str):
    return (
        db.query(models.LibraryInstallRecipe)
        .filter(
            models.LibraryInstallRecipe.tenant_id == tenant_id,
            models.LibraryInstallRecipe.library_name == lib_name,
            models.LibraryInstallRecipe.accelerator == accel_key,
            models.LibraryInstallRecipe.deleted_at.is_(None),
        )
        .first()
    )


def render_recipes_for_prompt(
    db: Session,
    *,
    tenant_id: Optional[int],
    libraries: Iterable[str],
    accelerator: str,
    max_chars: int = 6000,
) -> str:
    """Read known-working install recipes for a set of libraries on a
    given accelerator for the specified tenant and render a single block
    the repair LLM prompt can prepend. Returns an empty string when
    nothing is known.

    Caps the returned text at ``max_chars`` so the prompt doesn't
    blow its context window when a build imports many libraries.

    GB.LEAK.5 — reads only ``tenant_id``'s LibraryInstallRecipe rows;
    never another tenant's, never the legacy LibraryProfile JSON columns.
    ``tenant_id`` is required (keyword-only); ``None`` returns "".
    """
    accel_key = (accelerator or "").lower().strip()
    if tenant_id is None or not accel_key:
        return ""
    norm_libs = [_normalise(l) for l in libraries if l]
    if not norm_libs:
        return ""

    out_parts: List[str] = []
    for lib_name in norm_libs:
        # Only this tenant's active rows — never another tenant's.
        rows = (
            db.query(models.LibraryInstallRecipe)
            .filter(
                models.LibraryInstallRecipe.tenant_id == tenant_id,
                models.LibraryInstallRecipe.library_name == lib_name,
                models.LibraryInstallRecipe.deleted_at.is_(None),
            )
            .order_by(models.LibraryInstallRecipe.id)
            .all()
        )
        if not rows:
            continue
        # Prefer exact-accel match; fall back to one of this tenant's
        # recipes for a different accelerator (better signal than nothing).
        exact = next((r for r in rows if r.accelerator == accel_key), None)
        row = exact or rows[0]
        n = int(row.success_count or 0)
        confidence = "HIGH-CONFIDENCE" if n >= 5 else ("MEDIUM" if n >= 2 else "FIRST-TIME")
        if exact is not None:
            out_parts.append(
                f"# Known-working {lib_name} install for {accel_key} "
                f"({confidence}, tested in {n} successful build(s) in your organisation):\n"
                f"{row.recipe}"
            )
        else:
            out_parts.append(
                f"# Known-working {lib_name} install ({confidence}, captured for "
                f"{row.accelerator} across {n} successful build(s) in your organisation "
                f"— adapt for {accel_key} if needed):\n{row.recipe}"
            )
    if not out_parts:
        return ""
    block = "\n\n".join(out_parts)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n# [recipe block truncated — exceeded prompt budget]"
    return block


def libraries_referenced_in_spec(spec_data: dict) -> List[str]:
    """Public wrapper around the private helper — used by the repair
    endpoint to pull libraries it should look up recipes for."""
    return _libraries_referenced(spec_data)


# ---------------------------------------------------------------------------
# 10.3.s1 — tenant-scoped accelerator counters (10.3.s1)
#
# The accel counters (success counts per library x accelerator) live on
# LibraryInstallRecipe rows, which are already keyed by
# (tenant_id, library_name, accelerator). These two accessors make that
# scoping explicit so no caller can accidentally read a platform-wide
# number again: ``tenant_accel_counts`` is the per-tenant read every
# existing consumer should use, and ``platform_accel_counts`` is the
# deliberate, opt-in aggregate (sum across tenants) for the rare caller
# that genuinely wants the platform-wide figure.
# ---------------------------------------------------------------------------


def tenant_accel_counts(
    db: Session,
    *,
    tenant_id: Optional[int],
    library_name: str,
) -> Dict[str, int]:
    """10.3.s1 — one tenant's accelerator -> success-count map for a library.

    Replaces the legacy global ``library_profiles.successful_build_count_by_accel``
    JSONB read. Sums each accelerator's active ``LibraryInstallRecipe.success_count``
    for ``tenant_id`` only; another tenant's runs never move these numbers.
    ``tenant_id=None`` returns {} (no tenant scope = no counters).
    Accelerators with no active row are absent from the map (callers use
    ``.get(accel, 0)``).
    """
    if tenant_id is None or not library_name:
        return {}
    rows = (
        db.query(
            models.LibraryInstallRecipe.accelerator,
            models.LibraryInstallRecipe.success_count,
        )
        .filter(
            models.LibraryInstallRecipe.tenant_id == tenant_id,
            models.LibraryInstallRecipe.library_name == library_name,
            models.LibraryInstallRecipe.deleted_at.is_(None),
        )
        .all()
    )
    out: Dict[str, int] = {}
    for accel, count in rows:
        key = (accel or "").lower().strip()
        if not key:
            continue
        out[key] = out.get(key, 0) + int(count or 0)
    return out


def platform_accel_counts(
    db: Session,
    *,
    library_name: str,
) -> Dict[str, int]:
    """10.3.s1 — EXPLICIT platform-wide aggregate: accelerator -> summed
    success-count across ALL tenants, for the one kind of caller that
    legitimately wants the community number (e.g. a scheduler/telemetry
    tick). Ordinary per-build reads must use ``tenant_accel_counts``.

    Sums active (not soft-deleted) rows only. This is a read-only rollup:
    it writes nothing and touches no existing row's data.
    """
    if not library_name:
        return {}
    rows = (
        db.query(
            models.LibraryInstallRecipe.accelerator,
            models.LibraryInstallRecipe.success_count,
        )
        .filter(
            models.LibraryInstallRecipe.library_name == library_name,
            models.LibraryInstallRecipe.deleted_at.is_(None),
        )
        .all()
    )
    out: Dict[str, int] = {}
    for accel, count in rows:
        key = (accel or "").lower().strip()
        if not key:
            continue
        out[key] = out.get(key, 0) + int(count or 0)
    return out
