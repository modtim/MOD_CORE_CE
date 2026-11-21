"""Build the container_env dict for a `system-wizard-generate` DAG node.

The generate node runs the Containerizer image (containerizer:latest)
and produces albedo-artifacts.json describing a hero Dockerfile +
entrypoint.sh + build-and-push.sh + per-variant Dockerfiles. This
module translates a BuildSpec's wizard inputs into the env vars the
Containerizer reads.

The legacy `/generate-artifacts` endpoint builds the same env shape
inline; it's slated for deletion once the new wizard-as-workflow
path is the only consumer. Until then this module is the canonical
builder for the workflow path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from .types import BuildSpec
from utils.registries import (
    build_push_registry,
    platform_project,
    tenant_registry,
)

if TYPE_CHECKING:
    import models

logger = logging.getLogger(__name__)


def _pick_output_mode(spec: BuildSpec) -> str:
    """Return 'codebase' or 'albedo' — same heuristic as the legacy
    endpoint's _pick_generation_mode, adapted to BuildSpec inputs.

    Codebase pipeline (multi-stage, parallel) for:
      - repo sources (git URL in source_repo_url)
      - pasted body > 4 KB OR > 80 lines
    Albedo (single-call, fast) for everything else.
    """
    if spec.source_repo_url:
        url = spec.source_repo_url.strip().lower()
        if url.startswith(("http://", "https://", "git@", "git://")):
            return "codebase"
    body = (spec.inline_source_body or "").strip()
    if len(body) > 4000 or body.count("\n") + 1 > 80:
        return "codebase"
    return "albedo"


def _resolve_cluster_filtered_bases(
    db: Session,
    tenant_id: int,
    *,
    user_process_type: Optional[str] = None,
) -> Tuple[List[str], str]:
    """Curated base-image refs filtered to the cluster's capabilities,
    with the same fallback-to-all behavior the legacy endpoint uses.

    ``user_process_type`` is the user's per-node accelerator pick (CPU /
    CUDA / ROCM / INTEL / NPU / unknown-string). When provided, the
    cluster-capabilities hint targets THIS choice instead of the
    cluster's highest-perf accelerator — the right behavior because
    the user has already decided what compute this node needs.

    If lookup fails for any reason (cluster table missing, capability
    detection error), returns an empty list — the caller skips the
    constraint block rather than locking the LLM into an empty set.
    """
    try:
        from utils.curated_base_images import CURATED_BASE_IMAGES
        from services.cluster_capabilities import (
            describe_capabilities_for_llm,
            get_cluster_capabilities,
        )
        cluster_caps = get_cluster_capabilities(db, tenant_id=tenant_id)
        _HINT_TO_CAP = {
            "CPU": "cpu", "CUDA": "cuda", "ROCM": "rocm",
            "INTEL": "intel", "NPU": "npu",
        }
        filtered = [
            img for img in CURATED_BASE_IMAGES
            if _HINT_TO_CAP.get(img.process_type_hint.upper(), "cpu") in cluster_caps
        ]
        if not filtered:
            filtered = list(CURATED_BASE_IMAGES)
        return (
            [img.image_ref for img in filtered],
            describe_capabilities_for_llm(cluster_caps, user_process_type=user_process_type),
        )
    except Exception as exc:
        logger.warning("generate_env: cluster-filtered base lookup failed: %s", exc)
        return ([], "")


def _constraint_block(canonical_refs: List[str], cluster_hint: str) -> str:
    """The prompt-tail that pins the LLM's FROM directive to a curated
    set. Empty string if there are no refs — caller appends to the
    user's additional_instructions."""
    if not canonical_refs:
        return ""
    return (
        "\n\nCLUSTER CAPABILITY CONTEXT:\n" + cluster_hint +
        "\n\nIMPORTANT — base-image admission rules:\n"
        "The Dockerfile's FROM directive MUST be EXACTLY one of "
        "these full image references — copy the entire string "
        "including the tag suffix:\n"
        + "\n".join(f"  - {ref}" for ref in canonical_refs)
        + "\n"
        "Do NOT use nvcr.io/, docker.io/, gcr.io/, ghcr.io/, or "
        "any other registry. Do NOT shorten or modify the tag. "
        "Bases requiring accelerators NOT in this cluster have "
        "been removed from the list above — you physically "
        "cannot pick one that won't run. Dockerfiles with "
        "non-allowlisted or non-canonical FROMs are rejected "
        "at build time and the user has to start over."
    )


# Canonical accelerator classes — exactly the ones with a curated base image
# (see utils/curated_base_images.py). Anything outside this set is not a real
# hardware class and must never reach variant selection (this is what kept a
# stray "bob" from ever being buildable). `cpu` is always available.
_CANONICAL_HW_CLASSES = frozenset({"cpu", "cuda", "rocm", "intel", "npu"})


def _fleet_hw_tags(db: Session) -> set:
    """The hardware classes the ENROLLED WORKER FLEET can actually run, taken
    from each worker's self-reported `hardware_specs.hardware_tags`. This is the
    ground truth — add a worker and its accelerators become available; remove it
    and they're gone. Intersected with the canonical set so a worker can't widen
    the buildable set with a bogus tag either."""
    tags = {"cpu"}  # every worker can run CPU
    try:
        import models
        for w in db.query(models.WorkerLicense).filter(
            models.WorkerLicense.deleted_at.is_(None)
        ).all():
            specs = w.hardware_specs or {}
            for t in (specs.get("hardware_tags") or []):
                if isinstance(t, str):
                    tags.add(t.strip().lower())
    except Exception as exc:
        logger.debug("generate_env: worker fleet hw-tag scan failed: %s", exc)
    return tags & _CANONICAL_HW_CLASSES


def _enabled_hw_tags(db: Session) -> List[str]:
    """Hardware classes the wizard may build variants for.

    Derived ENTIRELY from the actual enrolled fleet (`_fleet_hw_tags`) so it
    self-heals: a stray "bob" tag can never inject a class no worker has, and
    a new "npu" worker joining the fleet becomes buildable immediately.

    GE.AIC.5 (2026-09-29): the Dashboard's GPU Compatibility tab/allowlist
    (`PlatformGPUSettings.hardware_tags`) was removed — "we do the builds
    based on the accelerators available" (owner). This function no longer
    reads that setting at all; it is a pure fleet derivation. The setting
    row + its API (`routers/gpu_settings.py`) are kept for compatibility but
    are deprecated and unused by any reader.
    """
    return sorted(_fleet_hw_tags(db))


def _looks_like_dockerfile(body: str) -> bool:
    """Heuristic for the INLINE_SOURCE_FILENAME hint. The Containerizer
    uses the filename extension to decide script vs Dockerfile."""
    if not body:
        return False
    if re.search(r"^FROM\s+[\w.\-/:@]+", body, re.MULTILINE):
        return True
    return body.lstrip().lower().startswith("# syntax=docker")


def build_wizard_generate_env(
    *,
    db: Session,
    spec: BuildSpec,
    build_spec_id: int,
    api_url: str,
    api_key: str,
) -> Dict[str, str]:
    """Produce the container_env dict for a `system-wizard-generate`
    DAG node based on a BuildSpec's wizard inputs.

    Required:
      - spec: the BuildSpec (must have at least one of source_repo_url
        / inline_source_body / wrap_base_image populated)
      - build_spec_id: the persisted BuildSpecRecord.id so the
        Containerizer can write back to the right row later
      - api_url / api_key: how the Containerizer talks back to the API

    Returns a flat str->str env dict ready to be set on
    node['data']['container_env'].
    """
    # Source-input encoding. REPO_URL is mutually exclusive with the
    # other two (repos already carry their own source tree, so a wrap
    # base or pasted body would be ignored). WRAP_BASE_IMAGE and
    # INLINE_SOURCE_BODY can BOTH be set — that's how the wizard's
    # script/Dockerfile sources have always worked: curated base
    # image as the FROM hint AND the user's pasted body as source.
    # See legacy /generate-artifacts: separate `is_repo_source` branch
    # for the FROM hint, separate `if extra` block for the inline body.
    is_repo = bool(spec.source_repo_url)
    is_wrap = bool(spec.wrap_base_image) and not is_repo
    is_inline = bool(spec.inline_source_body) and not is_repo

    output_mode = _pick_output_mode(spec)

    # Prompt tail: pin the LLM's FROM directive.
    # Pass `user_process_type=spec.process_type` so the cluster-
    # capabilities hint targets the USER'S accelerator pick, not the
    # cluster's highest-perf one. Without this, the LLM wrote CUDA
    # bases for CPU face-detection nodes on any cluster that had a
    # CUDA worker — bloating the image by 5+ GB.
    canonical_refs, cluster_hint = _resolve_cluster_filtered_bases(
        db, spec.tenant_id, user_process_type=spec.process_type,
    )
    constraint = _constraint_block(canonical_refs, cluster_hint)

    # Combine the metadata block + user-pasted extras + constraint into
    # ADDITIONAL_INSTRUCTIONS. The Containerizer's prompt assembly
    # prepends standard scaffolding; this is the user-tail piece.
    meta_lines = [
        f"Node display name: {spec.node_name}",
        f"Input type: {spec.input_type}",
        f"Output type: {spec.output_type}",
        f"Process type: {spec.process_type}",
    ]
    if spec.description:
        meta_lines.append(f"Description: {spec.description}")
    if spec.container_env:
        meta_lines.append(f"Expected env vars: {json.dumps(spec.container_env)}")
    extras = spec.additional_instructions or ""
    combined_instructions = (
        "\n".join(meta_lines) + "\n" + (extras + constraint).strip()
    ).strip()

    # HARBOR-J2 — CE LAN installs ship no tenant registry (tenant_registry() is
    # "", commit f3938afb3) and no registry container at all. The Containerizer
    # composes `{LOCAL_REGISTRY}/tenant-{TENANT_ID}/{IMAGE_NAME}`, so an empty
    # LOCAL_REGISTRY yields the malformed `/tenant-N/...` ref and a push with no
    # leading slash to strip. With no tenant registry AND no explicit registry
    # env (HARBOR_REGISTRY / DOCKER_REGISTRY / CE_IMAGE_REGISTRY) the operator
    # has selected no publish target at all, so the honest posture is the local
    # daemon with BARE refs — create_container.py tags bare and skips push when
    # REGISTRY_TARGET=local and LOCAL_REGISTRY is blank. Reaching for the
    # platform default there would point a CE LAN build at MOD's production
    # Harbor. The platform defaults apply only when a publish target IS
    # selected, and the whole block is byte-identical to HEAD whenever a
    # tenant registry is configured.
    tenant_reg = tenant_registry()
    explicit_registry = (
        (os.getenv("HARBOR_REGISTRY") or "").strip()
        or (os.getenv("DOCKER_REGISTRY") or "").strip()
        or (os.getenv("CE_IMAGE_REGISTRY") or "").strip()
    )
    if tenant_reg or explicit_registry:
        registry_target = "local" if tenant_reg else "harbor"
        harbor_registry = os.getenv("HARBOR_REGISTRY", "ghcr.io")
        harbor_project = os.getenv("HARBOR_PROJECT", "mod_core")
        if not tenant_reg:
            # No tenant registry but the operator named a registry: push to the
            # platform publish target (HARBOR_REGISTRY, else DOCKER_REGISTRY,
            # else the CE mirror host).
            harbor_registry = build_push_registry()
            harbor_project = platform_project()
    else:
        registry_target = "local"
        harbor_registry = ""
        harbor_project = ""

    env: Dict[str, str] = {
        "OUTPUT_MODE": output_mode,
        "IMAGE_NAME": spec.image_name,
        "NODE_NAME": spec.node_name,
        "TENANT_ID": str(spec.tenant_id),
        "API_URL": api_url,
        "API_KEY": api_key,
        "BUILD_SPEC_ID": str(build_spec_id),
        "REGISTRY_TARGET": registry_target,
        "LOCAL_REGISTRY": tenant_reg,
        "HARBOR_REGISTRY": harbor_registry,
        "HARBOR_PROJECT": harbor_project,
        "ADDITIONAL_INSTRUCTIONS": combined_instructions,
        # Redirect HuggingFace caches off /root (sandbox tmpfs, 32 MB)
        # onto /tmp (sandbox tmpfs, 256 MB). The Containerizer's
        # codebase-pipeline indexer loads
        # Snowflake/snowflake-arctic-embed-s (~133 MB) at startup; on
        # the 32 MB /root tmpfs the download bombed with
        # "No space left on device (os error 28)" and the whole
        # generate phase emitted empty Dockerfile/entrypoint/build-and-
        # push, which then ingest-artifacts correctly rejected with
        # 400 "missing required keys" until the worker hit its
        # retry limit. Repro 2026-06-01 task=6465.
        #
        # Sets every HF library cache var because the libraries
        # don't share defaults — hub respects HF_HOME, transformers
        # respects TRANSFORMERS_CACHE, sentence_transformers respects
        # both depending on version. All three to /tmp keeps the
        # download to a single location and within the larger tmpfs.
        "HF_HOME": "/tmp/hf-cache",
        "HUGGINGFACE_HUB_CACHE": "/tmp/hf-cache/hub",
        "TRANSFORMERS_CACHE": "/tmp/hf-cache",
        "SENTENCE_TRANSFORMERS_HOME": "/tmp/hf-cache/sentence-transformers",
    }

    # The generate container's egress allow-list reaches the LLM host only, so
    # the Hugging Face Hub is unreachable at run time. Community images bake the
    # embedding model in (CODEBASE_EMBED_BAKED_DIR); declaring the container
    # offline makes the retrieval index skip fast when it is not baked in
    # instead of stalling on a download that cannot succeed. Paid unchanged.
    from utils.paid_import import is_ce_edition
    if is_ce_edition():
        env["CODEBASE_EMBED_OFFLINE"] = "1"

    # LLM_* envs from the AI config resolver.
    try:
        from utils.tool_config import resolved_effective_env
        env.update(resolved_effective_env("wizard"))
    except Exception as exc:
        # No LLM config = the generate node will fail-fast inside the
        # Containerizer with a clear error. Don't fail at env-build.
        logger.warning("generate_env: LLM env resolution failed: %s", exc)

    # The generate container runs in the egress-controlled user sandbox,
    # which CANNOT reach internal service hostnames like vllm-big. When the
    # wizard LLM is our internal no-auth vLLM, repoint LLM_BASE_URL at the
    # configured EXTERNAL endpoint (LLM_BASE_URL_EXTERNAL, derived from
    # OLLAMA_SUBDOMAIN + base domain) so it routes through nginx like any
    # other allowlisted egress. External providers already carry a public
    # base_url + a real key, so they're left untouched.
    # GT.P2.6c — CE-safe shim (delegates to the paid resolver on Pro; on CE
    # there are no managed AI stacks, so it reports False and nothing is
    # repointed).
    from utils.tool_config import is_managed_local_llm
    _ext_llm = (os.getenv("LLM_BASE_URL_EXTERNAL") or "").strip()
    if _ext_llm and is_managed_local_llm(env.get("LLM_BASE_URL", "")):
        env["LLM_BASE_URL"] = _ext_llm

    # Canonical bases as JSON so the Containerizer's defensive snap pass
    # can recover from near-miss FROM lines even if the LLM ignored the
    # constraint block.
    if canonical_refs:
        env["CURATED_BASE_IMAGES_JSON"] = json.dumps(canonical_refs)

    # REPO_URL excludes the others. WRAP_BASE_IMAGE and
    # INLINE_SOURCE_BODY are independent — script/Dockerfile sources
    # set both (curated base + pasted body).
    if is_repo:
        env["REPO_URL"] = spec.source_repo_url or ""
        # GSEC R-100 — clone bounds for the generate (repo) stage. The
        # Containerizer's clone_repo_tool reads CLONE_TIMEOUT_S / CLONE_MAX_MB
        # to cap the clone step (subprocess timeout + working-tree size
        # check); the DB platform settings are the single source of truth so
        # an operator can tune per-deployment without a release. Fail-closed
        # to the conservative defaults on any read error.
        try:
            from services.platform_settings import get_setting
            clone_timeout_s = int(get_setting(
                "wizard.clone_timeout_s", db=db) or 900)
            clone_max_mb = int(get_setting(
                "wizard.clone_max_mb", db=db) or 2048)
        except Exception:  # noqa: BLE001 — settings read must never sink env
            logger.warning(
                "generate_env: clone-bounds settings read failed — using "
                "defaults (900s / 2048MB)", exc_info=True,
            )
            clone_timeout_s, clone_max_mb = 900, 2048
        env["CLONE_TIMEOUT_S"] = str(clone_timeout_s)
        env["CLONE_MAX_MB"] = str(clone_max_mb)
    else:
        if is_wrap:
            env["WRAP_BASE_IMAGE"] = spec.wrap_base_image or ""
        if is_inline:
            body = spec.inline_source_body or ""
            env["INLINE_SOURCE_BODY"] = body
            env["INLINE_SOURCE_FILENAME"] = (
                spec.inline_source_filename
                or ("Dockerfile" if _looks_like_dockerfile(body) else "user_script.py")
            )

    # Codebase-pipeline-only knobs.
    if output_mode == "codebase":
        if is_repo:
            env["CODEBASE_SOURCES"] = spec.source_repo_url or ""
        # HARBOR-J2 — MOD_REGISTRY is the TENANT registry, nothing else: HEAD
        # emitted `tenant_registry()` verbatim, so an empty tenant registry
        # yields "" here for EVERY input, explicit HARBOR_REGISTRY included.
        # Reaching for platform_registry() (MOD's production Harbor) on an
        # explicit registry would silently point a CE LAN codebase build at
        # MOD's registry — the exact thing this module exists to stop, and a
        # diff from HEAD. The leading-'/' refs the empty value produces
        # downstream (generate_artifacts_v2.py:76 f"{registry}/{node}" ->
        # "/node", build_verify.py:75 -> "/mod-verify:<ts>") are NOT fixed
        # here: those consumers are a separate job (HARBOR-J3). Bare refs
        # do NOT follow from MOD_REGISTRY="" — the f-strings prepend the
        # slash. Byte-identical to HEAD in both cases.
        env["MOD_REGISTRY"] = tenant_reg
        env["QDRANT_URL"] = os.getenv("QDRANT_URL", "http://qdrant:6333")

    # Platform-level GPU compat (Dashboard → Settings).
    hw_tags = _enabled_hw_tags(db)
    if hw_tags:
        env["ENABLED_HW_TAGS"] = ",".join(hw_tags)

    # User-skipped variants from the Review-step picker.
    if spec.skipped_variants:
        cleaned = sorted({v.strip().lower() for v in spec.skipped_variants if v and v.strip()})
        if cleaned:
            env["SKIPPED_VARIANTS"] = ",".join(cleaned)

    # Phase B (#1) — system binaries the describe-synthesize LLM
    # declared the script depends on. Containerizer's
    # `inject_system_deps_into_dockerfile` post-pass reads these
    # and bakes them into the emitted hero Dockerfile so the
    # script's `subprocess.run(['ffmpeg', ...])` etc. resolve at
    # runtime. Comma-separated for apt names (matches the existing
    # SKIPPED_VARIANTS convention); raw newline-bearing string for
    # dockerfile_extras (no encoding needed — Docker env vars
    # support multi-line values).
    if spec.system_packages:
        env["SYSTEM_PACKAGES"] = ",".join(
            p.strip() for p in spec.system_packages if p and p.strip()
        )
    if spec.dockerfile_extras and spec.dockerfile_extras.strip():
        env["DOCKERFILE_EXTRAS"] = spec.dockerfile_extras

    return env


# ---------------------------------------------------------------------------
# stack-mesh-fabric §6 (GC.SMF.6) — MOD_STACK_<ALIAS>_URL injection
# ---------------------------------------------------------------------------
#
# A node can bind directly to a reachable persistent-stack service (rather
# than going through a NIM/managed-model resolver). When it does, the worker
# executing that node's container needs a URL it can actually dial. A mesh
# MagicDNS name (`_stack_mesh_endpoints`, _shared.py:387) only resolves on a
# host joined to the tailnet — i.e. an enrolled worker — never from wherever
# "local" dispatch happens to run (mirrors the LLM_BASE_URL sandbox-repoint
# above: an address that's only reachable from one side of a network split
# must be gated on which side is actually asking). So this only fires for
# worker-side (remote) dispatch; local dispatch gets nothing injected and
# keeps whatever address it already had.
@dataclass(frozen=True)
class StackMeshBinding:
    """One node-input binding to a reachable persistent-stack service.

    `alias` is the node-input alias the env var is named after (sanitised
    into the `MOD_STACK_<ALIAS>_URL` shape below). `service` must be a key
    returned by `_stack_mesh_endpoints(stack)` — i.e. a service on `stack`
    that actually carries `x-mod.reachable: true` and therefore has a mesh
    sidecar / MagicDNS identity. `port` is the container-side port that
    service listens on for this binding, authored wherever the node's
    stack-binding itself is declared (same as any other node input) — it is
    NOT derivable from `_stack_mesh_endpoints`, which carries identity/
    reachability, not per-service port numbers.
    """
    alias: str
    stack: "models.PersistentStack"
    service: str
    port: int


def _sanitize_stack_alias(raw: str) -> str:
    """`<ALIAS>` segment of `MOD_STACK_<ALIAS>_URL`: upper-snake, env-safe."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", raw or "").strip("_").upper()
    return cleaned or "STACK"


def build_stack_mesh_env(
    bindings: Sequence[StackMeshBinding],
    *,
    remote: bool,
) -> Dict[str, str]:
    """`MOD_STACK_<ALIAS>_URL=http://<magicdns-name>:<port>` for each reachable
    stack-service a node binds (stack-mesh-fabric §6 / GC.SMF.6).

    `remote` is True iff this env is being built for worker-side (mesh-
    joined) dispatch. When False, returns `{}` unconditionally — the
    MagicDNS names in `bindings` would not resolve from a non-mesh-joined
    dispatch path, so nothing is injected rather than injecting an address
    that will fail to connect.

    Skips (does not raise for) a binding whose service isn't actually
    present in `_stack_mesh_endpoints(stack)` — e.g. the service lost its
    `x-mod.reachable` flag after the binding was authored — since a stale
    binding shouldn't take down env-building for the node's other inputs.
    """
    if not remote:
        return {}
    from routers.persistent_stacks._shared import _stack_mesh_endpoints

    env: Dict[str, str] = {}
    for binding in bindings:
        endpoints = _stack_mesh_endpoints(binding.stack)
        entry = endpoints.get(binding.service)
        if not entry:
            continue
        magic = entry.get("magicdns_name")
        if not magic:
            continue
        env_name = f"MOD_STACK_{_sanitize_stack_alias(binding.alias)}_URL"
        env[env_name] = f"http://{magic}:{binding.port}"
    return env


# ---------------------------------------------------------------------------
# GC.SMF.6 follow-up — the production caller (node → stack binding resolution)
# ---------------------------------------------------------------------------
#
# `build_stack_mesh_env` above takes bindings, but nothing production ever
# built one: there was no node→stack binding concept feeding it (GC.SMF.6
# landed PARTIAL 2026-09-25). This module is where the node-env path lives, so
# the resolver lands here too: it reuses the ONE node→stack resolution point
# that already exists (`stack_wake.resolve_required_stack`, D2 — the same map
# the node-bindings overlay reads) plus the stack row lookup, and derives the
# container-side port with the same read order the Endpoints cell uses
# (`expose` → legacy `ports:` → the new-format `x-mod.ports` record). Nothing
# here invents a parallel mapping, and the URL itself still comes ONLY from
# `_stack_mesh_endpoints` inside `build_stack_mesh_env` — never from a stack
# field, never a public/host address fallback.
def _stack_service_container_port(stack: "models.PersistentStack",
                                  service: str) -> Optional[int]:
    """Container-side port a stack service listens on, read in the SAME order
    the Dashboard's Endpoints cell reads it
    (`routers/persistent_stacks/persistent_stacks.py` `stack_networking`
    `_container_port`): the service's `expose`, then its legacy `ports:`, then
    the stack-level `x-mod.ports` record (the new format, whose per-service
    `ports:` are stripped at deploy). None when the spec declares no port —
    the caller emits no env var rather than guessing one."""
    spec = stack.compose_spec if isinstance(stack.compose_spec, dict) else {}
    services = spec.get("services") if isinstance(spec.get("services"), dict) else {}
    svc = services.get(service)
    if isinstance(svc, dict):
        exp = svc.get("expose")
        if isinstance(exp, list) and exp:
            head = str(exp[0]).split("/")[0].strip()
            if head.isdigit():
                return int(head)
        pl = svc.get("ports")
        if isinstance(pl, list) and pl:
            p0 = pl[0]
            if isinstance(p0, dict):
                target = p0.get("target")
            else:
                target = str(p0).split("/")[0].split(":")[-1].strip()
            if str(target or "").isdigit():
                return int(target)
    top = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else {}
    for p in (top.get("ports") or []):
        if isinstance(p, dict) and p.get("service") == service and p.get("target") is not None:
            if str(p["target"]).isdigit():
                return int(p["target"])
    return None


def _node_stack_mesh_bindings(
    db: Session,
    *,
    node,
    tenant_id: int,
) -> List[StackMeshBinding]:
    """The reachable stack-services `node` binds, as `StackMeshBinding`s.

    Resolution is delegated ENTIRELY to `stack_wake.resolve_required_stack`
    (the single node→stack decision point: an explicit `requires_stack` hint,
    else the NIM/VLM/SDXL/LLM kind maps) and the row lookup to
    `crud.persistent_stacks_domain.get_stack_by_name_and_tenant`, so this can
    never disagree with the node-bindings overlay or the wake path. A node that
    resolves to no stack, whose stack row is gone, or whose services declare no
    mesh endpoint / no port contributes nothing — never a raise, never a
    guessed address.
    """
    try:
        from services.stack_wake import resolve_required_stack
        from crud.persistent_stacks_domain import get_stack_by_name_and_tenant
        from routers.persistent_stacks._shared import _stack_mesh_endpoints
    except Exception as exc:  # noqa: BLE001 — CE carve / import cycle
        logger.debug("generate_env: stack-mesh binding imports failed: %s", exc)
        return []
    try:
        ref = resolve_required_stack(node, tenant_id=tenant_id)
        if ref is None:
            return []
        stack = get_stack_by_name_and_tenant(
            db, ref.stack_name, ref.tenant_id)
    except Exception as exc:  # noqa: BLE001 — resolution must never sink env
        logger.warning("generate_env: stack-binding resolve failed: %s", exc)
        return []
    if stack is None:
        return []
    endpoints = _stack_mesh_endpoints(stack)
    if not endpoints:
        return []
    # Fail-closed on the site mesh being unset: `_stack_mesh_endpoints`
    # SYNTHESISES a deterministic MagicDNS name for every reachable service
    # whether or not an identity was ever minted (same note `_stack_reachability`
    # carries at _shared.py:1305-1308), so it cannot tell "reach declared" from
    # "reach established". Provisioned-ness is the STORED mint record
    # (`stack.mesh_endpoints`) — a mesh-less site minted nothing, so it gets no
    # env var rather than a name nothing can resolve.
    stored = stack.mesh_endpoints if isinstance(stack.mesh_endpoints, dict) else {}
    if not any(
        (e.get("status") or "provisioning") != "revoked"
        for e in (stored.get(s) for s in endpoints) if isinstance(e, dict)
    ):
        return []
    bindings: List[StackMeshBinding] = []
    for service in endpoints:
        port = _stack_service_container_port(stack, service)
        if port is None:
            continue  # fail-closed: no declared port → no env var
        alias = (ref.stack_name if len(endpoints) == 1
                 else f"{ref.stack_name}-{service}")
        bindings.append(StackMeshBinding(
            alias=alias, stack=stack, service=service, port=port))
    return bindings


def build_node_stack_mesh_env(
    db: Session,
    *,
    node,
    tenant_id: int,
    remote: bool,
) -> Dict[str, str]:
    """Production entry point for the §6 injection: the `MOD_STACK_<ALIAS>_URL`
    envs for whatever reachable stack `node` resolves to, on worker-side
    (mesh-joined) dispatch only.

    This is the non-test caller `build_stack_mesh_env` was missing — the
    dispatch path (or any future node-env builder) calls THIS, never the raw
    binding builder, so the node→stack resolution and the fail-closed port
    check live in one place. Inert (returns `{}`) when the site mesh is unset
    (no endpoint entries), when the node binds no stack, when no port is
    declared, and for every local dispatch.
    """
    if not remote:
        return {}
    return build_stack_mesh_env(
        _node_stack_mesh_bindings(db, node=node, tenant_id=tenant_id),
        remote=True,
    )
