"""workflow-agentic-execution-loop §G — workflow→node-wizard handoff.

When the workflow wizard's Builder proposes a graph that references
a node the user can't see (a `missing_nodes` validator finding), the
workflow wizard should hand the user a prefilled node-wizard form
that names what to build and what shape the new node must have.

The handoff is purely informational — no behaviour change without
the user clicking the "Build this missing node" CTA on the FE
panel. The server side surfaces:

  - SSE event `needs_node_build` per missing node, carrying the
    `NodeHandoffSpec` payload
  - JSON `node_build_handoffs` array on the /apply 422 response when
    apply hits `missing_nodes`

`NodeHandoffSpec` shape (FE consumes this verbatim):

  {
    "node_name": "<missing-name>",
    "input_type": "<inferred-from-upstream-or-null>",
    "output_type": "<inferred-from-downstream-or-null>",
    "description": "<one-sentence-spec>",
    "process_type": "CPU" | "CUDA" | "MPS",
    "suggested_wrap_base_image": "<optional-string-or-null>",
    "prefill_url": "/dashboard/nodes/wizard?prefilled=<urlencoded-json>",
    "rationale": "<why-this-node-is-needed-in-this-workflow>",
  }

Inference rules (deliberately conservative — wrong inferences
mislead the user; null is better than a guess):

  - `input_type`: the output_type of the upstream node in the
    proposal if that node has a known output_type in the catalog.
    Falls back to null when upstream is anchor / unresolved.
  - `output_type`: same logic against downstream consumer.
  - `process_type`: default 'CPU' unless the user_goal mentions
    GPU-flavoured terms (cuda, gpu, blender, stable-diffusion, etc.)
  - `suggested_wrap_base_image`: a tiny built-in dictionary keyed on
    common node-name patterns (python_*, blender_*, etc.). Null for
    everything else.

`build_node_handoffs(missing_nodes, proposal, catalog_entries,
user_goal)` is the single entry point.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# NodeHandoffSpec
# ---------------------------------------------------------------------

@dataclass
class NodeHandoffSpec:
    node_name: str
    input_type: Optional[str] = None
    output_type: Optional[str] = None
    description: str = ""
    process_type: str = "CPU"
    suggested_wrap_base_image: Optional[str] = None
    prefill_url: str = ""
    rationale: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_name": self.node_name,
            "input_type": self.input_type,
            "output_type": self.output_type,
            "description": self.description,
            "process_type": self.process_type,
            "suggested_wrap_base_image": self.suggested_wrap_base_image,
            "prefill_url": self.prefill_url,
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------

# Lowercase substrings that signal GPU intent on the user_goal or
# node_name. Conservative — when in doubt we default CPU; users
# can flip to CUDA in the node-wizard form.
_GPU_HINTS = {
    "cuda", "gpu", "stable diffusion", "stable-diffusion",
    "sd1.5", "sd1_5", "sdxl", "blender", "comfyui", "comfy",
    "diffusion", "llama", "qwen", "deepseek", "mistral",
    "inference", "embedding", "torch", "tensorrt", "onnxruntime-gpu",
}

# Node-name keyword → suggested wrap_base_image. Substring match,
# first hit wins. Entries are deliberately broad starting points
# the user can swap on the form.
_BASE_IMAGE_HINTS = [
    ("blender",     "linuxserver/blender:latest"),
    ("ffmpeg",      "jrottenberg/ffmpeg:6-ubuntu"),
    ("python",      "python:3.11-slim"),
    ("node",        "node:20-slim"),
    ("rust",        "rust:1.75-slim"),
    ("go ",         "golang:1.22-alpine"),
    ("imagemagick", "dpokidov/imagemagick:latest"),
    ("pandoc",      "pandoc/core:latest"),
    ("aws",         "amazon/aws-cli:latest"),
    ("postgres",    "postgres:16-alpine"),
]


def _slug_to_human(slug: str) -> str:
    """`my_image_process` → `My Image Process`. Used in descriptions
    when the missing node name is snake_case rather than display-cased."""
    if not slug:
        return ""
    return " ".join(p.capitalize() for p in slug.replace("-", "_").split("_"))


def _detect_process_type(user_goal: str, node_name: str) -> str:
    """CUDA vs CPU based on goal + node-name keywords. Default CPU."""
    blob = f"{user_goal or ''} {node_name or ''}".lower()
    for hint in _GPU_HINTS:
        if hint in blob:
            return "CUDA"
    return "CPU"


def _suggest_base_image(node_name: str, user_goal: str) -> Optional[str]:
    """First substring match wins; None when nothing matches."""
    blob = f"{node_name or ''} {user_goal or ''}".lower()
    for keyword, image in _BASE_IMAGE_HINTS:
        if keyword in blob:
            return image
    return None


def _infer_io_from_proposal(
    missing_name: str,
    proposal: Dict[str, Any],
    catalog_by_name: Dict[str, Dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """Find a proposal node whose nodeDefinitionName == missing_name,
    then walk the proposal's edges to infer the expected input_type
    (from upstream) and output_type (from downstream)."""
    nodes = proposal.get("nodes") or []
    edges = proposal.get("edges") or []
    # Find the node id(s) in the proposal that reference this missing
    # nodeDefinitionName. There can be > 1 if the wizard proposed it
    # twice; first hit is fine since the IO contract is the same.
    target_id: Optional[str] = None
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if (n.get("data") or {}).get("nodeDefinitionName") == missing_name:
            target_id = n.get("id")
            break
    if target_id is None:
        return (None, None)

    by_id = {n.get("id"): n for n in nodes if isinstance(n, dict) and n.get("id")}

    def _resolve_type(node_id: str, side: str) -> Optional[str]:
        """side='output' → return that node's output_type from catalog.
        side='input' → return that node's input_type from catalog."""
        n = by_id.get(node_id) or {}
        ref = (n.get("data") or {}).get("nodeDefinitionName")
        entry = catalog_by_name.get(ref) if ref else None
        if not entry:
            return None
        t = entry.get(f"{side}_type")
        if isinstance(t, str) and t and t.lower() not in ("none", ""):
            return t.lower()
        return None

    inferred_input: Optional[str] = None
    inferred_output: Optional[str] = None
    for e in edges:
        if not isinstance(e, dict):
            continue
        if e.get("target") == target_id and not inferred_input:
            inferred_input = _resolve_type(e.get("source"), "output")
        if e.get("source") == target_id and not inferred_output:
            inferred_output = _resolve_type(e.get("target"), "input")
        if inferred_input and inferred_output:
            break
    return (inferred_input, inferred_output)


def _build_prefill_url(spec: NodeHandoffSpec) -> str:
    """Build a node-wizard prefill URL the FE can route to. The
    `prefilled` query param is URL-encoded JSON the node wizard's
    Settings panel deserialises on mount."""
    payload = {
        "node_name": spec.node_name,
        "input_type": spec.input_type,
        "output_type": spec.output_type,
        "description": spec.description,
        "process_type": spec.process_type,
    }
    if spec.suggested_wrap_base_image:
        payload["wrap_base_image"] = spec.suggested_wrap_base_image
    encoded = urllib.parse.quote(
        json.dumps({k: v for k, v in payload.items() if v is not None},
                   separators=(",", ":")),
        safe="",
    )
    return f"/dashboard/nodes/wizard?prefilled={encoded}"


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def build_node_handoffs(
    *,
    missing_nodes: Set[str],
    proposal: Dict[str, Any],
    catalog_entries: List[Dict[str, Any]],
    user_goal: str,
) -> List[NodeHandoffSpec]:
    """Build a NodeHandoffSpec for each missing node referenced in the
    proposal. Returns a list (one entry per missing name); empty list
    when there's nothing to hand off.

    Best-effort: any per-spec exception logs + skips that one rather
    than aborting the whole list.
    """
    if not missing_nodes:
        return []
    if not isinstance(proposal, dict):
        return []
    catalog_by_name = {
        e.get("name"): e for e in (catalog_entries or [])
        if isinstance(e, dict) and e.get("name")
    }
    handoffs: List[NodeHandoffSpec] = []
    for missing_name in sorted(missing_nodes):
        try:
            in_t, out_t = _infer_io_from_proposal(
                missing_name, proposal, catalog_by_name,
            )
            spec = NodeHandoffSpec(
                node_name=missing_name,
                input_type=in_t,
                output_type=out_t,
                description=_describe(missing_name, in_t, out_t, user_goal),
                process_type=_detect_process_type(user_goal, missing_name),
                suggested_wrap_base_image=_suggest_base_image(missing_name, user_goal),
                rationale=_rationale(missing_name, user_goal),
            )
            spec.prefill_url = _build_prefill_url(spec)
            handoffs.append(spec)

            try:
                from services import wizard_metrics
                wizard_metrics.record_node_handoff(
                    result="emitted",
                    has_input=bool(in_t), has_output=bool(out_t),
                )
            except Exception:
                pass

        except Exception as e:
            logger.warning(
                "wizard_node_handoff: per-name failure (continuing): %s "
                "(name=%s)", e, missing_name,
            )
    return handoffs


def _describe(
    missing_name: str,
    input_type: Optional[str], output_type: Optional[str],
    user_goal: str,
) -> str:
    """Short prose description the node wizard pre-renders. Inputs:
    everything we know about the missing node + the original user
    goal so the LLM-side of the node wizard has working context."""
    human = _slug_to_human(missing_name) or missing_name
    # I/O clause — the concrete transformation THIS node performs.
    if input_type and output_type:
        io = f" It takes {input_type} input and produces {output_type} output."
    elif input_type:
        io = f" It takes {input_type} input."
    elif output_type:
        io = f" It produces {output_type} output."
    else:
        io = ""

    # Frame it as a SINGLE atomic step. The node this hint drives is ONE leaf of
    # a larger workflow the Workflow Wizard already composed — the node builder
    # must build ONLY this one transformation, not the whole pipeline. Without
    # this framing a name like "Video to 3d" reads as the entire task, so the
    # node wizard (rightly) flags it as multi-step and bounces the user back —
    # an infinite loop. Being explicit ("one step", "only this transformation")
    # keeps the build scoped and reads as a node, not a workflow.
    body = (
        f"'{human}' — a SINGLE step of a larger workflow.{io} "
        f"Build ONLY this one transformation as one node; do NOT add any "
        f"upstream or downstream steps (the workflow already provides those)."
    )
    if user_goal:
        body += (
            f" For context, this node is one step of the larger goal "
            f"\"{(user_goal or '').strip()}\" — but build just this step."
        )
    return body


def _rationale(missing_name: str, user_goal: str) -> str:
    """One-liner the FE renders on the handoff card. Tells the user
    WHY they're being routed to the node wizard."""
    return (
        f"The workflow wizard proposed using {missing_name!r}, but that "
        f"node isn't visible in your library yet. Build it once with "
        f"the node wizard and re-ask the workflow wizard — your "
        f"proposal will validate cleanly the second time."
    )
