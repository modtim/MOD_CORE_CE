"""Exemplar retrieval — piece 2 of the wizard grounding/learning program.

The wizard already LEARNS install recipes (install_recipes.py) and
error→fix pairs (remediation), but only feeds them into the *repair*
prompt. Nothing seeds the *generation* prompt with a prior WORKING whole
node. So for a task like "photogrammetry from images" the coder invents a
tool's CLI from scratch every time, even if a working OpenMVS/COLMAP node
already exists in the catalog.

This module closes that gap by reusing infrastructure that already
exists — no new qdrant collection, no new embeddings:

  1. `utils.nodes_ingester.search_user_nodes` (qdrant `node_definitions_index`,
     all-MiniLM-L6-v2) finds catalog nodes semantically similar to the
     request. Its filter already scopes to system/platform nodes + the
     caller's own tenant/user — so an exemplar never leaks one tenant's
     private script to another. That scoping is the privacy guarantee.
  2. Any REGISTERED node is by definition a build that passed test-and-
     repair + smoke, i.e. a success. Its full recipe (script, base image,
     system packages, dockerfile_extras, I/O types) lives in
     `BuildSpecRecord.spec_json`, reachable via `NodeDefinition.build_spec_id`.

`fetch_exemplar` returns a dict the generation prompt renders, or None
when nothing is similar/usable. Fail-open: any error → None (the wizard
just generates without an exemplar).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Minimum cosine similarity for a node to be worth showing as an
# exemplar. all-MiniLM cosine for genuinely-related tasks sits ~0.55+;
# below that the "example" is more distracting than helpful. Tunable.
_MIN_SCORE = float(os.getenv("WIZARD_EXEMPLAR_MIN_SCORE", "0.55"))

# Caps so a large prior node can't blow the generation prompt's budget.
_SCRIPT_CAP = 2500
_EXTRAS_CAP = 800


def _recipe_from_spec(spec: Any) -> Dict[str, Any]:
    """Pull the reusable recipe fields out of a BuildSpecRecord.spec_json
    blob. Best-effort — spec_json shape drifts (synth output, then mutated
    by the repair loop), so every field is optional."""
    if not isinstance(spec, dict):
        return {}
    script = spec.get("script_content") or ""
    if not script:
        # Fall back to the main python source file in the bundle.
        for f in (spec.get("source_files") or []):
            if isinstance(f, dict) and str(f.get("filename", "")).endswith(".py"):
                script = f.get("content") or ""
                if script:
                    break
    return {
        "script": script,
        "base_image": spec.get("base_image_hint") or spec.get("base_image") or "",
        "system_packages": list(spec.get("system_packages") or []),
        "dockerfile_extras": spec.get("dockerfile_extras") or "",
        "input_type": spec.get("suggested_input_type") or spec.get("input_type") or "",
        "output_type": spec.get("suggested_output_type") or spec.get("output_type") or "",
    }


def fetch_exemplar(
    db, description: str, *,
    tenant_id: Optional[int],
    user_id: Optional[int],
    exclude_name: Optional[str] = None,
    min_score: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Find the most similar prior WORKING node and return its recipe as
    a generation exemplar, or None. Fail-open on any error."""
    description = (description or "").strip()
    if not description:
        return None
    threshold = _MIN_SCORE if min_score is None else min_score
    try:
        import models
        from utils import nodes_ingester
    except Exception as e:  # pragma: no cover - import guard
        logger.info("exemplar retrieval unavailable: %s", e)
        return None

    try:
        hits: List[dict] = nodes_ingester.search_user_nodes(
            db, description, tenant_id=tenant_id, user_id=user_id, top_k=5,
        )
    except Exception as e:
        logger.info("exemplar search failed: %s", e)
        return None

    excl = (exclude_name or "").strip().lower()
    for h in hits or []:
        try:
            if (h.get("score") or 0.0) < threshold:
                continue
            name = (h.get("name") or "").strip()
            if excl and name.lower() == excl:
                continue
            node_id = h.get("node_id")
            if node_id is None:
                continue
            nd = (
                db.query(models.NodeDefinition)
                .filter(models.NodeDefinition.id == node_id)
                .first()
            )
            if nd is None or not getattr(nd, "build_spec_id", None):
                continue
            spec_row = (
                db.query(models.BuildSpecRecord)
                .filter(models.BuildSpecRecord.id == nd.build_spec_id)
                .first()
            )
            if spec_row is None or not spec_row.spec_json:
                continue
            recipe = _recipe_from_spec(spec_row.spec_json)
            if not recipe.get("script"):
                continue  # nothing to learn from without the script
            recipe["script"] = recipe["script"][:_SCRIPT_CAP]
            recipe["dockerfile_extras"] = (recipe["dockerfile_extras"] or "")[:_EXTRAS_CAP]
            recipe["name"] = nd.name
            recipe["description"] = (nd.description or "").strip()
            recipe["score"] = float(h.get("score") or 0.0)
            recipe["source"] = "learned"
            logger.info(
                "exemplar: seeding generation with %r (score=%.3f)",
                nd.name, recipe["score"],
            )
            return recipe
        except Exception as e:
            logger.info("exemplar candidate skipped: %s", e)
            continue
    return None
