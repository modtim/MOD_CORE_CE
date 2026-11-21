"""node-code-edit-rebuild §2 (Stream B) — build-dispatch module.

Bridges a node's durable source (``models.NodeSourceRevision``, owned by
``services.node_source_revisions``) to the existing build-DAG machinery
(``node_builder.build_dag.submit.create_build_workflow`` +
``services.workflow_orchestrator.dispatch_trigger_run``).

Three entry points (Contract 1, ``the internal design notes
INTERFACES.md``):

  - ``seed_spec_from_revision`` — revision -> persisted BuildSpec.
  - ``run_source_critics`` — warnings-only static checks on a revision's
    source; never raises.
  - ``dispatch_rebuild`` — seed + dispatch a workflow run for either a
    single-variant rebuild or a full fan-out rebuild.

``RegistryIsolationError`` is raised by the registry-isolation guard in
``node_builder.build_dag.submit`` (see ``_assert_registry_isolated`` there)
and is defined here so it has a stable, dependency-light import location —
Stream A's router imports it from this module to map it to HTTP 409.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from node_builder.build_dag.types import BuildArtifact, BuildSpec, BuildVariantSpec

logger = logging.getLogger(__name__)


class RegistryIsolationError(Exception):
    """Raised when a client build would resolve to MOD's platform registry or
    lacks a tenant identity. Stream A maps this to HTTP 409 (registry_not_isolated).
    """


# --- image_name derivation ---------------------------------------------------

_SLUG_BAD_CHARS = re.compile(r"[^a-z0-9-]+")


def _slugify(text: str) -> str:
    slug = _SLUG_BAD_CHARS.sub("-", (text or "").strip().lower()).strip("-")
    return slug


def _image_name_for_node(node: "models.NodeDefinition") -> str:
    """Bare image-repo slug for a node. Prefers the tail of the existing
    ``executable_path`` (e.g. ``registry/tenant-1/pdf-to-png:latest`` ->
    ``pdf-to-png``) so a rebuild keeps pushing the same repo; falls back to a
    sanitized ``node.name`` for a node that has never built (no
    executable_path yet)."""
    exe = getattr(node, "executable_path", None) or ""
    if exe:
        tail = exe.split("/")[-1].split(":")[0]
        slug = _slugify(tail)
        if slug:
            return slug
    return _slugify(getattr(node, "name", None) or "") or "node"


# --- Contract 1: seed_spec_from_revision -------------------------------------

def _coerce_artifacts(files: Optional[List[Any]]) -> List[BuildArtifact]:
    out: List[BuildArtifact] = []
    for f in files or []:
        if isinstance(f, BuildArtifact):
            out.append(f)
        elif isinstance(f, dict):
            out.append(BuildArtifact.model_validate(f))
    return out


def _coerce_variants(snapshot: Optional[Any]) -> List[BuildVariantSpec]:
    out: List[BuildVariantSpec] = []
    for v in snapshot or []:
        if isinstance(v, BuildVariantSpec):
            out.append(v)
        elif isinstance(v, dict):
            out.append(BuildVariantSpec.model_validate(v))
    return out


def seed_spec_from_revision(
    db: Session,
    node: "models.NodeDefinition",
    revision: "models.NodeSourceRevision",
    *,
    scope: Optional[str] = None,
) -> BuildSpec:
    """Build (and persist) a ``BuildSpec`` from a node's saved
    ``NodeSourceRevision``.

    Maps ``revision.hero`` -> hero_dockerfile/entrypoint_sh/build_and_push_sh/
    source_files, ``revision.variants_snapshot`` -> spec.variants,
    ``revision.source_overrides_by_variant`` -> spec.source_overrides_by_variant.
    ``tenant_id``/``node_name`` come from ``node``; ``image_name`` is derived
    from ``node.executable_path`` (or a sanitized ``node.name`` fallback).

    ``scope`` (the rebuild scope, see ``dispatch_rebuild``) is stamped onto
    the spec as ``rebuild_node_id``/``rebuild_scope`` so the push-result
    handler can record the pushed digest for ``node`` — the single-variant
    rebuild DAG omits the register/manifest stages that would otherwise set
    ``NodeDefinition.image_digest`` + a ``node_versions`` row.

    Persists a ``models.BuildSpecRecord(tenant_id=node.tenant_id,
    spec_json=spec.model_dump())``, sets ``spec._build_spec_id`` to the new
    row's id, and returns the spec.
    """
    import models

    hero = revision.hero or {}
    spec = BuildSpec(
        image_name=_image_name_for_node(node),
        node_name=node.name,
        tenant_id=node.tenant_id,
        hero_dockerfile=hero.get("dockerfile") or "",
        entrypoint_sh=hero.get("entrypoint_sh") or "",
        build_and_push_sh=hero.get("build_and_push_sh") or "",
        source_files=_coerce_artifacts(hero.get("source_files")),
        variants=_coerce_variants(revision.variants_snapshot),
        source_overrides_by_variant={
            variant: _coerce_artifacts(files)
            for variant, files in (revision.source_overrides_by_variant or {}).items()
        },
        rebuild_node_id=node.id,
        rebuild_scope=scope,
    )

    rec = models.BuildSpecRecord(tenant_id=node.tenant_id, spec_json=spec.model_dump())
    db.add(rec)
    db.flush()
    spec._build_spec_id = rec.id  # type: ignore[attr-defined]
    # Opt this spec into the registry-isolation guard in create_build_workflow.
    # Only client edit/copy/rebuild dispatches (i.e. specs seeded from a saved
    # revision) set this — MOD platform/system builds never do, keeping the
    # guard's blast radius scoped to this feature.
    spec._enforce_tenant_registry = True  # type: ignore[attr-defined]
    return spec


# --- Contract 1: run_source_critics ------------------------------------------

_FROM_RE = re.compile(r"(?im)^\s*FROM\s+(\S+)")


def _first_from_image(dockerfile: str) -> Optional[str]:
    m = _FROM_RE.search(dockerfile or "")
    return m.group(1) if m else None


def run_source_critics(revision: "models.NodeSourceRevision") -> List[Dict[str, Any]]:
    """Run the executability + GPU-classification critics on the revision's
    source. Returns a list of warning dicts ``[{critic, severity, message,
    ...}]``. Never raises for "code looks broken" — critic failures/exceptions
    are swallowed and simply produce no warning for that check. Caller (Stream
    A) persists the result to ``revision.critic_warnings``.
    """
    warnings: List[Dict[str, Any]] = []
    hero = getattr(revision, "hero", None) or {}
    dockerfile = hero.get("dockerfile") or ""

    # Executability critic — deterministic structural checks shared with the
    # wizard's ingest-artifacts critic (node_builder/critic_artifacts.py).
    # Deliberately uses only the deterministic layer (no LLM call): fast,
    # dependency-light, and safe to run synchronously on every rebuild
    # request.
    try:
        from node_builder.critic_artifacts import _deterministic_issues

        bundle = {
            "image_name": _image_name_for_node_safe(revision),
            "dockerfile": dockerfile,
            "entrypoint_sh": hero.get("entrypoint_sh") or "",
            "build_and_push_sh": hero.get("build_and_push_sh") or "",
        }
        for issue in _deterministic_issues(bundle):
            warnings.append({
                "critic": "executability",
                "severity": "warning",
                "message": issue,
            })
    except Exception as e:  # noqa: BLE001 — warnings only, never raise
        logger.warning("run_source_critics: executability critic failed: %s", e)

    # GPU-classification sanity check — flags a hero Dockerfile that looks
    # CUDA-only (per the same heuristic the build-fanout planner uses), so a
    # rebuild targeting a non-CUDA variant surfaces the likely incompatibility
    # up front instead of failing deep in a build task.
    try:
        from node_builder.build_dag.variant_strategy import is_likely_c_extension_image

        base_image = _first_from_image(dockerfile)
        if base_image:
            is_cuda_only, reason = is_likely_c_extension_image(base_image)
            if is_cuda_only:
                warnings.append({
                    "critic": "gpu_classification",
                    "severity": "info",
                    "message": (
                        f"Base image '{base_image}' looks CUDA-only ({reason}); "
                        "rebuilds targeting non-CUDA variants may fail or be "
                        "marked architecturally-incompatible."
                    ),
                })
    except Exception as e:  # noqa: BLE001 — warnings only, never raise
        logger.warning("run_source_critics: GPU classification critic failed: %s", e)

    return warnings


def _image_name_for_node_safe(revision: "models.NodeSourceRevision") -> str:
    """Best-effort image_name for the executability critic's structural
    checks — the critic only cares about /-and-: shape, so the denormalized
    ``revision.node_name`` is close enough; never raises."""
    try:
        return _slugify(getattr(revision, "node_name", None) or "")
    except Exception:  # noqa: BLE001
        return ""


# --- Contract 1: dispatch_rebuild --------------------------------------------

SCOPE_SINGLE_VARIANT = "single_variant"
SCOPE_FULL_FANOUT = "full_fanout"


def dispatch_rebuild(
    db: Session,
    *,
    node: "models.NodeDefinition",
    revision: "models.NodeSourceRevision",
    scope: str,
    variant: Optional[str] = None,
    user_id: Optional[int],
) -> Dict[str, Any]:
    """Seed a BuildSpec from ``revision`` and dispatch a rebuild workflow run.

    ``scope`` is one of ``"single_variant"`` / ``"full_fanout"``:
      - single_variant: ``create_build_workflow(db, spec, user_id=user_id,
        rebuild_variant=variant)`` — rebuilds only ``variant`` from the
        operator's edited source (no generate, no register).
      - full_fanout: ``create_build_workflow(db, spec, user_id=user_id,
        include_generate_phase=False)`` — spec.variants is populated (from
        ``revision.variants_snapshot``), so this fans out over every variant
        with no generate/ingest phase and re-registers the node.

    Raises ``RegistryIsolationError`` (before or during ``create_build_workflow``)
    when the build cannot be proven isolated to the building tenant's own
    registry — a node with no tenant identity (a system node, not yet forked)
    can never be rebuilt through this path, and the guard inside
    ``build_dag.submit`` covers every other isolation failure at the point the
    tenant Harbor URL is actually resolved.

    Returns ``{"workflow_run_id": int, "workflow_id": int, "scope": scope}``.
    """
    import models
    from node_builder.build_dag.submit import (
        assert_build_farm_available,
        create_build_workflow,
    )

    assert_build_farm_available()  # 409 build_farm_unavailable (2400LR-wiz1)
    from services.workflow_orchestrator import dispatch_trigger_run

    if getattr(node, "tenant_id", None) is None:
        raise RegistryIsolationError(
            "node has no tenant identity (system node) — fork/copy it before rebuilding"
        )

    # Stamp the rebuild identity (node + scope) onto the spec so the
    # push-result handler can record the pushed digest for THIS node — a
    # single-variant rebuild's DAG omits the register/manifest stages that
    # would otherwise set image_digest + a node_versions row (node-version-
    # pinning GA.NMF.F8). See seed_spec_from_revision.
    spec = seed_spec_from_revision(db, node, revision, scope=scope)

    if scope == SCOPE_SINGLE_VARIANT:
        result = create_build_workflow(
            db, spec, user_id=user_id, rebuild_variant=variant,
        )
    elif scope == SCOPE_FULL_FANOUT:
        result = create_build_workflow(
            db, spec, user_id=user_id, include_generate_phase=False,
        )
    else:
        raise ValueError(f"unknown rebuild scope: {scope!r}")

    workflow = db.get(models.Workflow, result["workflow_id"])
    run = dispatch_trigger_run(
        db,
        workflow=workflow,
        triggered_by_user_id=user_id,
        extra_inputs={
            "build_spec_id": spec._build_spec_id,  # type: ignore[attr-defined]
            "scope": scope,
            "node_id": node.id,
        },
    )

    return {
        "workflow_run_id": run.id,
        "workflow_id": workflow.id,
        "scope": scope,
    }
