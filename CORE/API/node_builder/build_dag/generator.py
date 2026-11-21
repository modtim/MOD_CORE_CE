"""δ DAG-wizard B2 — convert BuildSpec to graph_json.

Produces the orchestrator-compatible graph_json shape that
workflow_orchestrator.build_execution_plan() consumes. Each DAG
node references a NodeDefinition (by name) that handles one
specific operation: build a single variant, smoke-test it, push
it, run the γ sample test, or register the final NodeDefinition.

DAG shape per BuildSpec:

    [build-hero] ----+--> [smoke-hero] --> [push-hero] --+
                     |                                   |
    [build-cuda]    -+-> [smoke-cuda] --> [push-cuda] ---+
                     |                                   |
    [build-rocm]    -+-> [smoke-rocm] --> [push-rocm] ---+--> [register]
                                                         ^
    (when sample present:)                               |
    [push-hero] -> [sample-test] -------------------------+

The actual per-variant NodeDefinitions that handle each task type
are seeded separately (B4). This module just emits the structural
graph_json — it doesn't need to know how individual nodes execute.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .types import BuildSpec

logger = logging.getLogger(__name__)


# System-internal NodeDefinition names that the build DAG references.
# Each is a tiny task container that handles one operation.
NODE_BUILD_VARIANT = "system-build-one-variant"
NODE_SMOKE_VARIANT = "system-smoke-one-variant"
NODE_PUSH_VARIANT = "system-push-one-variant"
# Legacy γ sample-test (hero only, wizard-supplied sample_input). Still
# kept for backwards-compat builds whose specs omit a build_plan; new
# §5 builds use NODE_SAMPLE_TEST_VARIANT below per-variant.
NODE_SAMPLE_TEST = "system-sample-test"
# §5 — per-variant sample-test. Runs after each variant's push,
# uploads the produced output to MinIO, and POSTs the result to the
# /wizard/variant-sample-test-result endpoint which scores it against
# the spec's reference (hero by default) and writes a
# node_variant_verification row. Hero's verdict gates register; non-
# hero verdicts populate the Node's per-variant verified/drift/
# incompatible state.
NODE_SAMPLE_TEST_VARIANT = "system-sample-test-one-variant"
NODE_REGISTER = "system-register-node"
# Auto-iterating "build → smoke → LLM-repair" loop for the hero variant.
# Internally does what the build+smoke pair does for non-hero variants,
# but loops until smoke passes (or the inner/outer repair budgets in
# the BuildSpec are exhausted). On success the image is pushed by the
# downstream push-hero node as today.
NODE_TEST_AND_REPAIR = "system-test-and-repair-one-variant"
# Generate phase — Containerizer LLM produces Dockerfile + entrypoint +
# scripts and writes them to /data/output as sentinel-wrapped JSON in
# stdout. The orchestrator's downstream-dispatch hook reads the result
# and persists artifacts onto the BuildSpec before firing downstream
# build nodes. Optional — when the BuildSpec is created with artifacts
# already populated (legacy /build-and-register-local path), the
# generate node is skipped.
NODE_WIZARD_GENERATE = "system-wizard-generate"
# Second phase of the wizard build's generate chain — runs as a regular
# DAG node between `generate` and the variant builds. Reads the
# Containerizer's artifact JSON from /data/input/albedo-artifacts.json,
# validates it, and merges onto the BuildSpec via the API. A failure
# here shows up in the run timeline like any other failed node, which
# is the whole point of doing it as a node instead of an orchestrator
# hook.
NODE_WIZARD_INGEST_ARTIFACTS = "system-wizard-ingest-artifacts"


def _node(node_id: str, node_def_name: str, label: str, x: int, y: int,
          extra_data: Dict[str, Any] = None) -> Dict[str, Any]:
    data = {"label": label, "nodeDefinitionName": node_def_name}
    if extra_data:
        data.update(extra_data)
    return {
        "id": node_id,
        "type": "process",
        "position": {"x": x, "y": y},
        "data": data,
    }


def _edge(edge_id: str, source: str, target: str) -> Dict[str, Any]:
    return {"id": edge_id, "source": source, "target": target}


def generate_single_variant_rebuild_dag(spec: BuildSpec, variant: str) -> Dict[str, Any]:
    """Emit a minimal DAG that REBUILDS one already-registered variant with the
    operator's edited source (spec.source_overrides_by_variant[variant]).

    Shape:  build-{variant} -> push-{variant} -> sample-test-{variant}

    Deliberately omits generate/ingest (no LLM regen — we build the operator's
    source verbatim) and omits register (the Node already exists; the per-
    variant sample-test posts to /wizard/variant-sample-test-result which
    updates the Node's gpu_variants/verification state in place, exactly as a
    non-hero variant does in the full build). The variant's Dockerfile is
    reused from the original build (spec.variants[variant], or hero_dockerfile
    when variant == "hero").
    """
    if variant == "hero":
        dockerfile = spec.hero_dockerfile
    else:
        match = next((v for v in (spec.variants or []) if v.variant == variant), None)
        dockerfile = match.dockerfile if match else spec.hero_dockerfile
    build_id = f"build-{variant}"
    push_id = f"push-{variant}"
    sample_id = f"sample-test-{variant}"
    nodes = [
        _node(build_id, NODE_BUILD_VARIANT, f"Rebuild {variant}", 80, 0,
              {"variant": variant, "dockerfile": dockerfile}),
        _node(push_id, NODE_PUSH_VARIANT, f"Push {variant}", 80, 130, {"variant": variant}),
        _node(sample_id, NODE_SAMPLE_TEST_VARIANT, f"Sample test {variant}", 80, 260,
              {"variant": variant}),
    ]
    edges = [
        _edge(f"e-{variant}-build-push", build_id, push_id),
        _edge(f"e-{variant}-push-sample", push_id, sample_id),
    ]
    return {"nodes": nodes, "edges": edges}


def generate_build_dag(spec: BuildSpec, *, include_generate_phase: bool = False) -> Dict[str, Any]:
    """Emit a graph_json dict representing the build DAG for ``spec``.

    Returns ``{"nodes": [...], "edges": [...]}`` — the canonical
    shape Workflow.graph_json stores.

    `include_generate_phase=True` prepends a `generate` node that runs
    the Containerizer LLM. On its completion the orchestrator's
    downstream-dispatch hook (in worker_tasks._dispatch_artifacts_into_spec)
    parses the artifacts from the task's result_payload and persists
    them onto the BuildSpec; the downstream `test-and-repair-hero`
    node then reads them from the spec as usual. This makes the
    wizard's GENERATE → BUILD chain a single workflow run — no
    client-side polling + dispatching dance.

    `include_generate_phase=False` (default) is the legacy path: caller
    has already populated the BuildSpec's Dockerfile/entrypoint and
    just wants the build half of the DAG.
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    # Raw-context builds (bring-your-own Dockerfile + context) skip every
    # wizard-specific stage — no LLM generate, no artifact ingest, no
    # smoke/sample-test/repair. Just: build the given Dockerfile against the
    # extracted context, push, register. build_one_variant handles the context
    # extraction when spec.raw_context is set.
    if getattr(spec, "raw_context", False):
        nodes.append(_node("build-hero", NODE_BUILD_VARIANT, "Build image", 80, 0, {"variant": "hero"}))
        nodes.append(_node("push-hero", NODE_PUSH_VARIANT, "Push image", 80, 130, {"variant": "hero"}))
        nodes.append(_node("register", NODE_REGISTER, "Register node", 80, 260, {}))
        edges.append(_edge("e-build-push", "build-hero", "push-hero"))
        edges.append(_edge("e-push-register", "push-hero", "register"))
        return {"nodes": nodes, "edges": edges}

    # All variant keys we'll build, including hero. Hero ALWAYS builds;
    # extra variants come from spec.variants when the spec was already
    # fanned out (legacy pre-generated path) OR from variants_for_mode
    # when this is the inputs-only path (spec.variants is empty at
    # submit time because the Containerizer hasn't run yet).
    variant_keys: List[Tuple[str, str]] = [("hero", spec.hero_dockerfile)]
    if spec.variants:
        # Legacy pre-generated-artifacts path: spec.variants was
        # populated by apply_variant_fanout(spec, plan) at submit
        # time, so each entry has both a variant key and a
        # rendered Dockerfile. Safe to schedule build-{variant}
        # tasks directly.
        for v in spec.variants:
            variant_keys.append((v.variant, v.dockerfile))
    # Inputs-only path: spec.variants is empty at DAG-creation
    # time. We used to call variants_for_mode here and pre-schedule
    # build-cuda / build-rocm / etc., but the Containerizer doesn't
    # actually emit per-variant Dockerfiles for plain CPU scripts —
    # so build_one_variant runs, looks up its variant in spec.variants,
    # finds nothing, exits with "variant 'X' not found in spec".
    # That failure marked otherwise-successful workflow_runs as
    # FAILED even though the hero passed (wfr 54 in this session).
    #
    # Pragmatic fix: hero-only in the inputs-only path. Multi-variant
    # fanout is reserved for the legacy path where the spec has
    # rendered per-variant Dockerfiles. Future: dynamic DAG expansion
    # after ingest-artifacts populates spec.variants.

    register_deps: List[str] = []

    # Lay out one column per variant; one row per stage.
    col_width = 200
    row_height = 130

    # Generate phase — two nodes in series when include_generate_phase.
    # `generate` runs the Containerizer LLM; its /data/output (which
    # includes albedo-artifacts.json) flows to `ingest-artifacts` via
    # the workflow's normal node-to-node data flow. `ingest-artifacts`
    # validates + merges onto the BuildSpec, then variant entry nodes
    # hang off ingest-artifacts.
    #
    # Both nodes are real DAG nodes. Either failing surfaces in the
    # run timeline like any other failed node — no orchestrator-side
    # hooks, no special-case code. That's the whole point of the
    # wizard-as-workflow-native refactor.
    chain_head: Optional[str] = None  # which node the variant entries hang off
    if include_generate_phase:
        nodes.append(_node(
            "generate", NODE_WIZARD_GENERATE, "Generate (LLM)",
            -2 * col_width, 0,
            {"variant": "hero", "phase": "generate"},
        ))
        nodes.append(_node(
            "ingest-artifacts", NODE_WIZARD_INGEST_ARTIFACTS, "Ingest artifacts",
            -col_width, 0,
            {"variant": "hero", "phase": "ingest"},
        ))
        edges.append(_edge("e-generate-ingest", "generate", "ingest-artifacts"))
        chain_head = "ingest-artifacts"

    # Track the hero's test-and-repair node id so non-hero variants
    # can gate themselves behind hero's PASS. Without this gate, non-
    # hero variants build in parallel from the PRE-repair spec and
    # smoke-fail for the same reason hero originally did (manual-test
    # repro 2026-05-22 spec=35 Python111 — all 4 non-hero variants
    # smoke-failed exit=2 because hero's LLM-added PIL handling
    # wasn't propagated to them). Set in the hero branch below;
    # consumed by the non-hero branch.
    hero_tr_id: Optional[str] = None

    for i, (variant, dockerfile) in enumerate(variant_keys):
        x = 80 + i * col_width
        push_id = f"push-{variant}"
        # entry_id = whichever node is the "first" for this variant.
        # We need it both for incoming-edge wiring from `generate` AND
        # for nothing else — but capturing it makes the wiring obvious.
        entry_id: str

        if variant == "hero":
            # Hero variant runs through the auto-iterating
            # test-and-repair loop. It performs the build + smoke
            # internally and loops with LLM repair until smoke passes
            # (or budgets exhausted in BuildSpec). The push node is
            # downstream as today.
            tr_id = f"test-and-repair-{variant}"
            hero_tr_id = tr_id    # consumed by non-hero gate below
            entry_id = tr_id
            nodes.append(_node(
                tr_id, NODE_TEST_AND_REPAIR, f"Auto-build + test {variant}",
                x, 120, {"variant": variant},
            ))
            nodes.append(_node(
                push_id, NODE_PUSH_VARIANT, f"Push {variant}",
                x, 120 + 2 * row_height, {"variant": variant},
            ))
            edges.append(_edge(f"e-{variant}-tr-push", tr_id, push_id))
        else:
            # Non-hero variants now also run through the auto-iterating
            # test-and-repair stage (same Python file as hero, with
            # VARIANT env set). Hero's loop fixes most issues transitively
            # via sync-variants-from-hero, but VARIANT-SPECIFIC failures
            # (a CUDA wheel that doesn't exist for cuda12.6 but does for
            # 12.4, a ROCm runtime base that's missing for the chosen
            # base image) need their OWN LLM repair iterations — hero's
            # loop never sees those failures. Budget is smaller per
            # variant (5 inner + 3 outer; see test_and_repair_one_variant
            # main()) so a variant that genuinely can't be made to work
            # marks incompatible quickly instead of spending hero's
            # 30+10 budget per variant.
            tr_id = f"test-and-repair-{variant}"
            entry_id = tr_id
            nodes.append(_node(
                tr_id, NODE_TEST_AND_REPAIR, f"Auto-build + test {variant}",
                x, 120, {"variant": variant},
            ))
            nodes.append(_node(
                push_id, NODE_PUSH_VARIANT, f"Push {variant}",
                x, 120 + 2 * row_height, {"variant": variant},
            ))
            edges.append(_edge(f"e-{variant}-tr-push", tr_id, push_id))

        # Wire each variant's entry node off the chain head (which is
        # `ingest-artifacts` when generate-phase is active, `None`
        # otherwise — legacy callers that pre-populated the BuildSpec).
        # By the time these entry nodes fire, ingest-artifacts has
        # already merged the LLM output onto the spec.
        if chain_head is not None:
            edges.append(_edge(f"e-ingest-{variant}", chain_head, entry_id))

        # Non-hero variant builds wait for hero's test-and-repair to
        # PASS. Hero's LLM repair iterations modify spec.hero_dockerfile
        # + spec.entrypoint_sh; without this gate, non-hero variants
        # would build in parallel from the pre-repair spec and fail
        # the same way hero originally did. Gating sacrifices some
        # parallelism for correctness; the variant_sync_from_hero
        # endpoint (called from test_and_repair's success callback)
        # propagates the repaired hero to non-hero variants before
        # they fire.
        if variant != "hero" and hero_tr_id:
            edges.append(_edge(
                f"e-{hero_tr_id}-{variant}-build", hero_tr_id, entry_id,
            ))

        # §5 — per-variant sample-test sits downstream of each push.
        # §6 — only hero's sample-test gates `register`; non-hero
        # sample-tests run in the background and call back to
        # /variant-verified to update the registered Node's
        # gpu_variants map retroactively. This lets the Node land in
        # the library as soon as hero passes ("Available on CPU,
        # verifying CUDA / ROCm / Intel / NPU").
        sample_id = f"sample-test-{variant}"
        nodes.append(_node(
            sample_id, NODE_SAMPLE_TEST_VARIANT,
            f"Sample test {variant}",
            x, 120 + 3 * row_height, {"variant": variant},
        ))
        edges.append(_edge(f"e-{variant}-push-sample", push_id, sample_id))
        if variant == "hero":
            # Hero's publish-gate verdict is the only one that blocks
            # register. Non-hero sample-tests are orphan branches
            # below register — they keep running, but the workflow
            # is allowed to advance to register the moment hero
            # passes its sample-test.
            register_deps.append(sample_id)
    # Non-hero sample-tests gate themselves behind hero's sample-test
    # completing so the dispatcher can compare against hero's
    # already-persisted MinIO output.
    for variant, _ in variant_keys:
        if variant == "hero":
            continue
        edges.append(_edge(
            f"e-sample-hero-{variant}",
            "sample-test-hero",
            f"sample-test-{variant}",
        ))

    register_x = 80 + len(variant_keys) * col_width + 60

    # Register stage — depends on every push-* (and sample-test when
    # present). Marks the build as complete and writes the
    # NodeDefinition row.
    register_id = "register"
    nodes.append(_node(
        register_id, NODE_REGISTER, "Register node",
        register_x, 120 + 2 * row_height,
        {
            "image_name": spec.image_name,
            "node_name": spec.node_name,
            "defer_registration": spec.defer_registration,
        },
    ))
    for dep in register_deps:
        edges.append(_edge(f"e-{dep}-register", dep, register_id))

    return {"nodes": nodes, "edges": edges}
