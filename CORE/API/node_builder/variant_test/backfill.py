"""Variant-test backfill sweep.

When a node was registered without testing every accelerator variant its cluster
can run — hardware offline at build time, or a variant never built — nothing
revisited it. Its ``gpu_variants`` stayed incomplete and the WorkflowBuilder
"needs testing / verifying X" chip never cleared (the silent-strand: the test
was never (re)queued once the build moment passed).

This sweep closes the gap. For each registered node missing an accelerator
variant whose hardware is ONLINE now (via the corrected
``online_accelerator_kinds`` gate), it either:

  - **enqueue_test** — the variant's image was already built but never tested
    (a verification row sits at ``untested`` / ``untested_awaiting_hardware``):
    re-queue the sample-test (idempotent); the heartbeat drain dispatches it to
    the matching worker.
  - **build** — the variant was never built: recover the originating BuildSpec,
    scope it to that one variant, and dispatch a fresh build-DAG. The register
    step merges the new variant into the existing node (same image_name).

Guards: only nodes that look like variant builds (have gpu_variants) are
considered; ``not_recommended_variants`` are skipped; builds are capped per tick,
cooled-down per (node, variant), and skipped when the recipe is unrecoverable.
Nothing is silently truncated — the sweep logs deferrals and skips.

Non-wizard nodes (hand-written in ``CORE/DockerContainers/<node>/``) never have a
BuildSpec to recover. When the flag ``VARIANT_BACKFILL_NON_WIZARD_ENABLED`` is on
and a node's own build source (context dir + Dockerfile) is resolvable, the sweep
synthesizes a raw-context BuildSpec from that source and dispatches the same
single-variant rebuild DAG the BuildSpec path uses — building ONLY the variant
tag, never the hero image.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .queue import _utcnow, online_accelerator_kinds, enqueue_pending

logger = logging.getLogger(__name__)

# Verdicts that mean "the image got built but the sample-test never validated it"
# — these are safe to re-enqueue (the image exists in Harbor).
_RETESTABLE_VERDICTS = {"untested", "untested_awaiting_hardware"}
# Verdicts that mean "we tried and it genuinely didn't work" — don't auto-retry.
_TERMINAL_FAIL_VERDICTS = {"failed", "incompatible"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _skip_variants(nd) -> set:
    """Variants the build heuristic already marked not-recommended /
    architecturally-incompatible for this node — never backfill those."""
    out: set = set()
    nrv = getattr(nd, "not_recommended_variants", None)
    if isinstance(nrv, dict):
        out = {str(k).strip().lower() for k in nrv.keys()}
    elif isinstance(nrv, list):
        out = {str(v).strip().lower() for v in nrv}
    return out


def _derive_image_base(gpu_variants: Dict[str, Any]) -> Optional[str]:
    """The image base shared by a node's variant tags (``{base}:latest-{variant}``),
    so a never-tested-but-built variant's image_ref can be reconstructed."""
    for v in (gpu_variants or {}).values():
        if isinstance(v, str) and ":latest-" in v:
            return v.rsplit(":latest-", 1)[0]
    return None


def _classify(db, node_id: int, variant: str, base: Optional[str]) -> Tuple[str, Optional[str]]:
    """Decide what to do for a missing (node, variant): enqueue_test / build / skip."""
    import models
    vrow = (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.node_definition_id == node_id,
            models.NodeVariantVerification.variant == variant,
            models.NodeVariantVerification.deleted_at.is_(None),
        )
        .order_by(models.NodeVariantVerification.id.desc())
        .first()
    )
    verdict = (getattr(vrow, "verdict", None) or "").lower() if vrow else None
    if verdict in _TERMINAL_FAIL_VERDICTS:
        return "skip", None
    if vrow is not None and verdict in _RETESTABLE_VERDICTS and base:
        return "enqueue_test", f"{base}:latest-{variant}"
    # No row (never attempted) — or a row we can't re-test from → rebuild.
    return "build", None


def find_backfill_candidates(db, online_kinds: Optional[set] = None) -> List[Dict[str, Any]]:
    """Registered tenant nodes missing an accelerator variant whose hardware is
    online NOW. Returns dicts:
    ``{node_id, tenant_id, name, variant, action, image_ref}`` where action is
    ``enqueue_test`` / ``build`` / ``skip``."""
    import models
    online = (online_kinds if online_kinds is not None else online_accelerator_kinds(db))
    want = {k for k in online if k and k != "cpu"}
    if not want:
        return []

    # Only consider tenant-scoped nodes that look like variant builds (have at
    # least one gpu_variant). System catalog nodes (tenant_id NULL, no
    # gpu_variants) are CPU tools, not multi-variant builds — never backfill.
    nodes = (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.deleted_at.is_(None),
            models.NodeDefinition.tenant_id.isnot(None),
        )
        .all()
    )
    candidates: List[Dict[str, Any]] = []
    for nd in nodes:
        gv = nd.gpu_variants if isinstance(nd.gpu_variants, dict) else {}
        if not gv:
            continue  # not a variant build
        have = {str(k).strip().lower() for k in gv.keys()}
        missing = want - have - _skip_variants(nd)
        if not missing:
            continue
        base = _derive_image_base(gv)
        for variant in sorted(missing):
            action, image_ref = _classify(db, nd.id, variant, base)
            candidates.append({
                "node_id": nd.id,
                "tenant_id": nd.tenant_id,
                "name": nd.name,
                "variant": variant,
                "action": action,
                "image_ref": image_ref,
            })
    return candidates


def rank_candidates_by_run_count(candidates: List[Dict[str, Any]],
                                 db) -> List[Dict[str, Any]]:
    """GO.10.s1 — usage-priority ranking: order backfill candidates by the
    number of ``NodeRun`` rows recorded for each candidate's node definition,
    descending (the most-used nodes get their missing variants first).

    Nodes with no run history sort last; ties keep the incoming order (stable
    sort), so the ranking never reshuffles work it cannot distinguish."""
    import models
    from sqlalchemy import func

    if not candidates:
        return []

    node_ids = {c["node_id"] for c in candidates}
    counts = {
        int(nid): int(n)
        for nid, n in (
            db.query(
                models.WorkflowNode.node_definition_id,
                func.count(models.NodeRun.id),
            )
            .join(models.NodeRun,
                  models.NodeRun.workflow_node_id == models.WorkflowNode.id)
            .filter(models.WorkflowNode.node_definition_id.in_(node_ids))
            .group_by(models.WorkflowNode.node_definition_id)
            .all()
        )
        if nid is not None
    }
    return sorted(candidates, key=lambda c: -counts.get(c["node_id"], 0))


def _service_user(db):
    import models
    username = os.getenv("PLATFORM_SERVICE_USERNAME", "platform-service")
    return (
        db.query(models.User).filter(models.User.username == username).first()
        or db.query(models.User).filter(models.User.is_platform_admin.is_(True)).first()
    )


def _recent_backfill_build(db, node_id: int, variant: str, cooldown_hours: int) -> bool:
    """True if we already kicked a backfill build for this (node, variant) within
    the cooldown window — avoids retrigger storms and backs off after a failure."""
    import models
    cutoff = _utcnow() - timedelta(hours=cooldown_hours)
    recent = (
        db.query(models.BuildSpecRecord)
        .filter(
            models.BuildSpecRecord.created_at >= cutoff,
            models.BuildSpecRecord.deleted_at.is_(None),
        )
        .all()
    )
    for r in recent:
        sj = r.spec_json if isinstance(r.spec_json, dict) else {}
        md = sj.get("node_metadata") if isinstance(sj.get("node_metadata"), dict) else {}
        # Both the wizard-recovery path (`variant_backfill`) and the
        # non-wizard source-synthesis path (`variant_backfill_non_wizard`)
        # stamp a {node_definition_id, variant} marker so the cooldown window
        # dedupes either origin.
        for marker_key in ("variant_backfill", "variant_backfill_non_wizard"):
            bf = md.get(marker_key)
            if isinstance(bf, dict) and bf.get("node_definition_id") == node_id \
                    and str(bf.get("variant") or "").lower() == variant:
                return True
    return False


def _trigger_variant_build(db, node_id: int, variant: str) -> bool:
    """Recover the node's originating BuildSpec, scope it to the one missing
    variant, and dispatch a fresh build-DAG. Returns True on dispatch."""
    import models
    # Recover the BuildSpec via the node_variant_verification bridge.
    bsid_row = (
        db.query(models.NodeVariantVerification.build_spec_id)
        .filter(
            models.NodeVariantVerification.node_definition_id == node_id,
            models.NodeVariantVerification.build_spec_id.isnot(None),
            models.NodeVariantVerification.deleted_at.is_(None),
        )
        .order_by(models.NodeVariantVerification.id.desc())
        .first()
    )
    if not bsid_row:
        # No wizard BuildSpec to recover. Non-wizard nodes (hand-written
        # DockerContainers/<node>) have no linkage by construction — when the
        # opt-in flag is on, synthesize a raw-context BuildSpec from the node's
        # own build source instead. All sweep-level guards (autobuild, shared
        # background budget, max_builds, per-(node,variant) cooldown) were
        # already checked before this call.
        if _env_bool("VARIANT_BACKFILL_NON_WIZARD_ENABLED", False):
            return _trigger_variant_build_non_wizard(db, node_id, variant)
        logger.info("backfill: node %s variant %s — no BuildSpec linkage, can't auto-build", node_id, variant)
        return False
    rec = (
        db.query(models.BuildSpecRecord)
        .filter(models.BuildSpecRecord.id == bsid_row[0], models.BuildSpecRecord.deleted_at.is_(None))
        .first()
    )
    if rec is None or not isinstance(rec.spec_json, dict):
        logger.info("backfill: node %s variant %s — BuildSpec recipe pruned/missing", node_id, variant)
        return False

    try:
        from node_builder.build_dag.types import BuildSpec
        spec = BuildSpec(**rec.spec_json)
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: node %s — BuildSpec reconstruct failed: %s", node_id, exc)
        return False

    target = next((v for v in (spec.variants or []) if str(getattr(v, "variant", "")).lower() == variant), None)
    if target is None:
        logger.info("backfill: node %s variant %s not in the original plan — manual rebuild needed", node_id, variant)
        return False

    spec.variants = [target]
    spec.defer_registration = False
    md = dict(spec.node_metadata or {})
    md["variant_backfill"] = {"node_definition_id": node_id, "variant": variant}
    spec.node_metadata = md

    new_rec = models.BuildSpecRecord(tenant_id=rec.tenant_id, spec_json=spec.model_dump())
    db.add(new_rec)
    db.commit()
    db.refresh(new_rec)
    spec._build_spec_id = new_rec.id  # type: ignore[attr-defined]

    from node_builder.build_dag.submit import create_build_workflow
    from services.workflow_orchestrator import dispatch_trigger_run
    actor = _service_user(db)
    actor_id = getattr(actor, "id", None)
    wf_info = create_build_workflow(db, spec, user_id=actor_id)
    wf = db.query(models.Workflow).filter(models.Workflow.id == wf_info["workflow_id"]).first()
    run = dispatch_trigger_run(
        db, workflow=wf, triggered_by_user_id=actor_id,
        extra_inputs={"build_spec_id": new_rec.id, "variant_backfill": True},
    )
    new_rec.workflow_run_id = getattr(run, "id", None)
    db.commit()
    logger.info(
        "backfill: dispatched rebuild node=%s variant=%s build_spec=%s workflow_run=%s",
        node_id, variant, new_rec.id, new_rec.workflow_run_id,
    )
    return True


# ---------------------------------------------------------------------
# Non-wizard (hand-written DockerContainers/<node>) backfill source.
#
# A node built from a raw Dockerfile has no NodeVariantVerification row and no
# BuildSpec to recover, so `_trigger_variant_build` always bails with "no
# BuildSpec linkage". The node's own build source is recoverable from its
# `executable_path` (the image name) — which is the container dir name under
# CORE/DockerContainers/ (each dir's build-and-push.sh derives its image tag
# from the dir name, lowercased). When that dir + Dockerfile exist on this host
# we can synthesize a raw-context BuildSpec and build the missing variant.
#
# Gated by VARIANT_BACKFILL_NON_WIZARD_ENABLED (default off). When off, this
# whole section is unreachable and the sweep behaves byte-identically to before.
# ---------------------------------------------------------------------

# CORE/DockerContainers root as seen from the API process. In the live container
# the api service mounts `../DockerContainers:/DockerContainers`; on a source
# checkout (dev/tests) it is the sibling of CORE/API. Overridable for tests.
def _docker_containers_root() -> Optional[str]:
    override = os.getenv("DOCKER_CONTAINERS_ROOT")
    if override:
        return override
    live = "/DockerContainers"
    if os.path.isdir(live):
        return live
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "..", "DockerContainers")
    )


def _image_name_base(executable_path: Optional[str]) -> Optional[str]:
    """Image NAME (no tag, no registry prefix) from a node's executable_path.
    ``"ghcr.io/modtim/stablediffusion:latest"`` ->
    ``"stablediffusion"``. Returns None when empty/unparseable."""
    if not executable_path:
        return None
    s = str(executable_path).strip()
    if not s:
        return None
    s = s.rsplit(":", 1)[0]          # strip tag (or :@digest -> handled below)
    if "@" in s:                      # digest-pinned: name comes before @
        s = s.split("@", 1)[0]
    s = s.rsplit("/", 1)[-1]          # strip registry + project path
    return s or None


def resolve_node_build_source(node_def) -> Optional[Tuple[str, str]]:
    """Resolve a node's own build source: ``(context_dir, dockerfile_path)``.

    ``context_dir`` is the DockerContainers/<node>/ directory and
    ``dockerfile_path`` the Dockerfile inside it. Returns None (never guesses)
    when the node's executable_path doesn't map to a real directory on this host
    or that directory has no Dockerfile.
    """
    root = _docker_containers_root()
    if not root:
        return None
    base = _image_name_base(getattr(node_def, "executable_path", None))
    if not base:
        return None
    # Case-insensitive match (dir "Albedo" <-> image "albedo").
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    match = None
    for e in entries:
        if e.lower() == base.lower():
            match = e
            break
    if match is None:
        return None
    context_dir = os.path.join(root, match)
    if not os.path.isdir(context_dir):
        return None
    dockerfile_path = os.path.join(context_dir, "Dockerfile")
    if not os.path.isfile(dockerfile_path):
        return None
    return context_dir, dockerfile_path


def _tar_dir_bytes(dir_path: str,
                   extra_files: Optional[Dict[str, str]] = None) -> Optional[bytes]:
    """Tar + gzip a directory into bytes for the raw-context tarball.
    `extra_files` (filename → text) are appended as additional context-root
    members after the directory listing — callers only pass files that are
    absent from the dir, so nothing real is ever shadowed. Returns None on any
    error (caller logs + skips)."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(os.listdir(dir_path)):
            tar.add(os.path.join(dir_path, name), arcname=name, recursive=True)
        for name in sorted(extra_files or {}):
            data = (extra_files or {})[name].encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _render_variant_dockerfile(base_dockerfile: str, variant: str, node_name: str) -> Optional[Tuple[str, Any]]:
    """Render a variant Dockerfile from the node's own (hero) Dockerfile body,
    the same way the wizard fan-out does: build a dockerfile-mode BuildPlan
    carrying the hero body, adapt it for the target variant
    (`adapt_plan_for_variant` — torch --index-url rewrites etc.), and render
    (`render_dockerfile`). Returns `(dockerfile, adapted_plan)` or None on any
    error so the caller skips. The adapted plan comes back so the caller can
    render the matching entrypoint (`render_entrypoint`) for the Dockerfile's
    `COPY entrypoint.sh /entrypoint.sh` contract when the node's own context
    ships no entrypoint.sh.
    """
    from node_builder.build_dag.variant_strategy import (
        adapt_plan_for_variant, variants_for_mode,
    )
    from node_builder.build_plan.types import BuildPlan
    from node_builder.build_plan.renderer import render_dockerfile

    plan = BuildPlan(
        mode="dockerfile",
        language="python",
        package_manager="none",
        passthrough_dockerfile=base_dockerfile,
        input_ports=[],
        output_ports=[],
    )
    # Pick the VariantSpec whose key == the requested variant. The strategy's
    # universal set covers cpu/cuda/rocm/intel/npu; anything outside it (a
    # non-canonical worker tag) has no render recipe → skip, never guess.
    target = next(
        (v for v in variants_for_mode(plan)
         if v.accelerator == variant or v.key == variant),
        None,
    )
    if target is None:
        return None
    # The strategy marks variants it can't build (e.g. a body with no
    # recognizable GPU-capable lib → non-cpu variants are CPU-only; or a
    # single-accelerator library restriction). The wizard fan-out skips
    # those; we do the same — never render a Dockerfile the strategy
    # already refused.
    if getattr(target, "compatibility", "compatible") != "compatible":
        return None
    adapted = adapt_plan_for_variant(plan, target)
    return render_dockerfile(adapted), adapted


def build_non_wizard_spec(db, node, variant: str) -> Optional[Any]:
    """Resolve ``node``'s own build source (``resolve_node_build_source``) and
    construct — but do NOT persist or dispatch — a raw-context ``BuildSpec``
    for ONE variant. Builds ONLY the variant tag (``image:latest-<variant>``);
    never the hero.

    This is the single place that turns a hand-written
    ``CORE/DockerContainers/<node>/`` source into a ``BuildSpec``; every
    caller (the backfill sweep's ``_trigger_variant_build_non_wizard``, which
    persists it as a ``BuildSpecRecord`` and dispatches the rebuild DAG
    itself, and the corpus harness's ``--allow-non-wizard-fallback`` path in
    ``corpus_harness._spec_for_cell``, which stamps corpus metadata before
    ``dispatch_cell`` persists + dispatches it) reuses this same construction
    — no parallel schema.

    Returns None (never guesses) when the source, the variant's render
    recipe, or the context upload is unavailable; each reason is logged.
    """
    node_id = node.id

    source = resolve_node_build_source(node)
    if source is None:
        logger.info(
            "backfill: non-wizard node %s variant %s — build source unresolvable "
            "(no DockerContainers/<node>/Dockerfile for executable_path %r), skipped",
            node_id, variant, getattr(node, "executable_path", None),
        )
        return None
    context_dir, dockerfile_path = source

    try:
        with open(dockerfile_path, "r", encoding="utf-8", errors="replace") as f:
            base_df = f.read()
        rendered = _render_variant_dockerfile(base_df, variant, node.name or "")
        if rendered is None:
            logger.info(
                "backfill: non-wizard node %s variant %s — no render recipe for "
                "variant, skipped", node_id, variant,
            )
            return None
        variant_df, adapted_plan = rendered
        # The rendered Dockerfile COPYs entrypoint.sh (renderer contract), but
        # nothing renders it for a hand-written node — supply it from the
        # platform template ONLY when the node's own context ships none. A
        # node's own entrypoint.sh always wins; it is never overwritten.
        try:
            ctx_has_entrypoint = "entrypoint.sh" in os.listdir(context_dir)
        except OSError:
            ctx_has_entrypoint = False
        extra_files: Optional[Dict[str, str]] = None
        if not ctx_has_entrypoint:
            from node_builder.build_plan.renderer import render_entrypoint
            extra_files = {"entrypoint.sh": render_entrypoint(adapted_plan)}
        context_bytes = _tar_dir_bytes(context_dir, extra_files=extra_files)
        if context_bytes is None:
            logger.info("backfill: non-wizard node %s — context tar failed, skipped", node_id)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: non-wizard node %s — source prep failed: %s", node_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None

    # Image NAME (harbor repo) = the image the node already runs. The variant
    # tag is image_name:latest-<variant>; the hero image_name:latest is never
    # rebuilt here.
    image_name = _image_name_base(getattr(node, "executable_path", None))
    if not image_name:
        logger.info("backfill: non-wizard node %s — no image_name, skipped", node_id)
        return None
    tenant_id = node.tenant_id
    if tenant_id is None:
        logger.info(
            "backfill: non-wizard node %s — system node (tenant_id NULL), "
            "not a tenant backfill target, skipped", node_id,
        )
        return None

    from node_builder.build_dag.types import BuildSpec, BuildVariantSpec, ContextArtifact
    import hashlib
    import os as _os

    sha = hashlib.sha256(context_bytes).hexdigest()
    bucket = _os.getenv("BUILD_CONTEXT_BUCKET", "platform-build-context")
    key = f"contexts/{sha}.tar.gz"

    spec = BuildSpec(
        image_name=image_name,
        node_name=node.name or f"node-{node_id}",
        tenant_id=int(tenant_id),
        # hero_dockerfile is present so the rebuild DAG's build step is well
        # defined, but this variant rebuild never builds the hero (build-{variant}
        # uses the variant Dockerfile below).
        hero_dockerfile=base_df,
        raw_context=True,
        context_artifact=ContextArtifact(
            bucket=bucket, object_key=key, sha256=sha,
            size_bytes=len(context_bytes), filename="context.tar.gz",
        ),
        variants=[BuildVariantSpec(variant=variant, dockerfile=variant_df)],
        defer_registration=True,  # the Node already exists; variant-verified merges it
        input_type=str((node.input_schema or {}).get("input_type", "image")) if isinstance(node.input_schema, dict) else "image",
        output_type=str((node.output_schema or {}).get("output_type", "image")) if isinstance(node.output_schema, dict) else "image",
        process_type=str(getattr(node, "process_type", None) or "GPU"),
        cost_in_credits=int(getattr(node, "cost_in_credits", 1) or 1),
        description=getattr(node, "description", None),
        node_type=getattr(node, "type", None),
        container_timeout=int(getattr(node, "container_timeout", 7200) or 7200),
        node_metadata={
            "variant_backfill_non_wizard": {
                "node_definition_id": node_id, "variant": variant,
                "context_dir": context_dir,
            }
        },
    )

    # Upload the context tarball (content-addressed) exactly like
    # submit_raw_context_build does.
    try:
        import io as _io
        from utils.storage import get_service_client
        mc = get_service_client()
        if not mc.bucket_exists(bucket):
            mc.make_bucket(bucket)
        mc.put_object(bucket, key, _io.BytesIO(context_bytes), len(context_bytes),
                      content_type="application/gzip")
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: non-wizard node %s — context upload failed: %s", node_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None

    return spec


def _trigger_variant_build_non_wizard(db, node_id: int, variant: str) -> bool:
    """Non-wizard backfill: synthesize a raw-context BuildSpec from the node's
    own build source (``build_non_wizard_spec``) and dispatch a single-variant
    rebuild DAG for `variant`. Returns True on dispatch, False (skip) when the
    source is unresolvable or anything else fails.
    """
    import models

    node = (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.id == node_id,
            models.NodeDefinition.deleted_at.is_(None),
        )
        .first()
    )
    if node is None:
        logger.info("backfill: non-wizard node %s not found", node_id)
        return False

    spec = build_non_wizard_spec(db, node, variant)
    if spec is None:
        return False

    new_rec = models.BuildSpecRecord(tenant_id=spec.tenant_id, spec_json=spec.model_dump())
    db.add(new_rec)
    db.commit()
    db.refresh(new_rec)
    spec._build_spec_id = new_rec.id  # type: ignore[attr-defined]

    from node_builder.build_dag.submit import create_build_workflow
    from services.workflow_orchestrator import dispatch_trigger_run
    actor = _service_user(db)
    actor_id = getattr(actor, "id", None)
    wf_info = create_build_workflow(db, spec, user_id=actor_id, rebuild_variant=variant)
    wf = db.query(models.Workflow).filter(models.Workflow.id == wf_info["workflow_id"]).first()
    run = dispatch_trigger_run(
        db, workflow=wf, triggered_by_user_id=actor_id,
        extra_inputs={
            "build_spec_id": new_rec.id,
            "rebuild_variant": variant,
            "variant_backfill": True,
            "variant_backfill_non_wizard": True,
        },
    )
    new_rec.workflow_run_id = getattr(run, "id", None)
    db.commit()
    logger.info(
        "backfill: dispatched non-wizard rebuild node=%s variant=%s image=%s build_spec=%s workflow_run=%s",
        node_id, variant, spec.image_name, new_rec.id, new_rec.workflow_run_id,
    )
    return True


def run_backfill_sweep(db, *, max_builds: Optional[int] = None,
                       cooldown_hours: Optional[int] = None,
                       autobuild: Optional[bool] = None) -> Dict[str, Any]:
    """Sweep entry point. Enqueues re-tests + triggers scoped rebuilds for
    registered nodes missing an online accelerator variant. Idempotent."""
    if max_builds is None:
        max_builds = _env_int("VARIANT_BACKFILL_MAX_BUILDS_PER_TICK", 2)
    if cooldown_hours is None:
        cooldown_hours = _env_int("VARIANT_BACKFILL_COOLDOWN_HOURS", 6)
    if autobuild is None:
        autobuild = _env_bool("VARIANT_BACKFILL_AUTOBUILD", True)

    # GO.10.s3 — dispatch order is usage-priority: the most-used nodes (by
    # recorded NodeRun count) get their missing variants first, so a capped
    # tick spends its builds on the nodes users actually run.
    candidates = rank_candidates_by_run_count(find_backfill_candidates(db), db)
    enqueued: List[Dict] = []
    built: List[Dict] = []
    deferred: List[Dict] = []
    skipped: List[Dict] = []
    builds_done = 0

    # Shared background budget: variant-builds compete with calibration for
    # one fleet-% cap (the 'Background work uses at most X% of the fleet'
    # slider). If that budget is already full, defer builds this tick so
    # background never grows past the cap — real user work always has room.
    bg_full = False
    try:
        from services.calibration_coordinator import _count_in_flight, background_cap
        bg_full = _count_in_flight(db) >= background_cap(db)
    except Exception:  # noqa: BLE001 — never let the gate crash the sweep
        bg_full = False

    for c in candidates:
        action = c["action"]
        if action == "skip":
            skipped.append(c)
            continue
        if action == "enqueue_test":
            try:
                enqueue_pending(
                    db, build_spec_id=None, node_definition_id=c["node_id"],
                    variant=c["variant"], image_ref=c["image_ref"],
                )
                enqueued.append(c)
            except Exception as exc:  # noqa: BLE001
                logger.warning("backfill: enqueue_test failed node=%s variant=%s: %s",
                               c["node_id"], c["variant"], exc)
                skipped.append({**c, "reason": "enqueue_failed"})
            continue
        # action == "build"
        if not autobuild:
            skipped.append({**c, "reason": "autobuild_disabled"})
            continue
        if bg_full:
            deferred.append({**c, "reason": "background_budget_full"})
            continue
        if builds_done >= max_builds:
            deferred.append(c)
            continue
        if _recent_backfill_build(db, c["node_id"], c["variant"], cooldown_hours):
            skipped.append({**c, "reason": "cooldown_or_inflight"})
            continue
        try:
            ok = _trigger_variant_build(db, c["node_id"], c["variant"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill: build trigger failed node=%s variant=%s: %s",
                           c["node_id"], c["variant"], exc)
            try:
                db.rollback()
            except Exception:
                pass
            ok = False
        if ok:
            built.append(c)
            builds_done += 1
        else:
            skipped.append({**c, "reason": "build_unavailable"})

    if enqueued or built or deferred:
        logger.info(
            "variant backfill: %d re-test(s) queued, %d rebuild(s) dispatched, "
            "%d deferred over cap (%d), %d skipped",
            len(enqueued), len(built), len(deferred), max_builds, len(skipped),
        )
    return {
        "enqueued": enqueued, "built": built,
        "deferred": deferred, "skipped": skipped,
    }
