"""δ DAG-wizard B3 — programmatic Workflow + WorkflowVersion creation
for system-internal builds.

Wraps the orchestrator's existing workflow-create + trigger
mechanism so the new /build-and-register-node endpoint (B4) can
dispatch a build as a workflow run without going through HTTP.

The shape produced by ``create_build_workflow``:
  1. A Workflow row with name="build-{node_name}-{timestamp}"
     scoped to the building tenant
  2. A WorkflowVersion row with graph_json from the metadata generator
  3. (Caller's job) Trigger the run via workflow_orchestrator.

Why a separate helper instead of inlining in the endpoint:
- Tested in isolation (no FastAPI runtime needed)
- Reusable from future fuzz daemon, retry/replay paths, etc.
- Keeps the endpoint thin and easy to review
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .build_secrets import (
    BUILD_TASK_SECRET_REFS,
    GENERATE_SECRET_REFS,
    GENERATE_SECRET_ENV_KEYS,
    PLATFORM_HARBOR_PULL_REFS,
    prime_scoped_push_creds,
    scoped_push_refs,
)
from .generator import generate_build_dag
from .types import BuildSpec
from utils.registries import build_push_registry

logger = logging.getLogger(__name__)

BUILD_FARM_UNAVAILABLE_CODE = "build_farm_unavailable"
BUILD_FARM_UNAVAILABLE_MESSAGE = (
    "Node building is not set up on this install: the node-builder image is "
    "not configured (BUILD_TASK_IMAGE is empty). Ask whoever runs this install "
    "to enable the node builder (set BUILD_TASK_IMAGE in the API environment "
    "and restart). Hand-written nodes in CORE/Metadata/custom/ work without it "
    "- see docs/authoring-nodes.md."
)
BUILD_FARM_NO_CONTAINERIZER_MESSAGE = (
    "This kind of node build (a script, repository or Dockerfile to turn into "
    "a container) needs the Containerizer image, which is not configured "
    "(CONTAINERIZER_IMAGE is empty). Ask whoever runs this install to set "
    "CONTAINERIZER_IMAGE in the API environment and restart. Building a node "
    "from an existing container image does not need it, and hand-written "
    "nodes in CORE/Metadata/custom/ work without it - see "
    "docs/authoring-nodes.md."
)

WIZARD_LLM_NOT_CONFIGURED_CODE = "wizard_llm_not_configured"
WIZARD_LLM_NOT_CONFIGURED_MESSAGE = (
    "No AI model is configured for the Node Wizard. Turning a script or "
    "repository into a node needs one to write the container. Open Settings > "
    "AI configuration, pick a model for the Node Wizard (your own API key, or "
    "a local model server) and submit again. Building from an existing "
    "container image does not need an AI model."
)


def build_farm_available(*, needs_containerizer: bool = False) -> "tuple[bool, str]":
    """Can this install run build-farm builds? Returns ``(ok, reason)``.

    Rule (availability, not a hardcoded "CE = no"): the worker needs the
    ``build-task`` image (resolved in main.py's system-node seeding from
    ``BUILD_TASK_IMAGE``, defaulting to the private ``${DOCKER_REGISTRY}/modtim/*``
    namespace, which a CE install cannot pull from), and - only for builds
    that run the generate phase (script / repository / Dockerfile-generation
    mode, ``needs_containerizer=True``) - the ``containerizer`` image
    (``CONTAINERIZER_IMAGE``). Image-source builds skip the Containerizer, so
    they need ``BUILD_TASK_IMAGE`` alone. Paid/SaaS (``is_ce_edition()`` False)
    always has them. No registry probe here: this runs in the request path.

    NOT checked here: that a worker with docker access is online. Workers do
    not report docker-socket access to the API (the embedded worker mounts the
    socket by design); a missing worker surfaces as a queued task, never as
    this refusal. 409 is reserved for images that are genuinely not configured.
    """
    from utils.paid_import import is_ce_edition

    if not is_ce_edition():
        return True, "paid edition"
    if not os.getenv("BUILD_TASK_IMAGE", "").strip():
        return False, "build-task image (BUILD_TASK_IMAGE) not configured in this edition"
    if needs_containerizer and not os.getenv("CONTAINERIZER_IMAGE", "").strip():
        return False, "containerizer image (CONTAINERIZER_IMAGE) not configured in this edition"
    return True, "image overrides configured"


def assert_build_farm_available(*, needs_containerizer: bool = False) -> None:
    """Raise HTTP 409 ``build_farm_unavailable`` when builds can't run.

    Call BEFORE creating any rows / enqueuing. ``HTTPException`` subclass so it
    propagates through the routers unchanged.
    """
    ok, reason = build_farm_available(needs_containerizer=needs_containerizer)
    if ok:
        return
    from fastapi import HTTPException

    logger.info("build submit refused: %s", reason)
    message = (
        BUILD_FARM_NO_CONTAINERIZER_MESSAGE
        if needs_containerizer and os.getenv("BUILD_TASK_IMAGE", "").strip()
        else BUILD_FARM_UNAVAILABLE_MESSAGE
    )
    raise HTTPException(
        status_code=409,
        detail={"code": BUILD_FARM_UNAVAILABLE_CODE, "message": message},
    )


def wizard_llm_configured() -> "tuple[bool, str]":
    """Is a model configured for the ``wizard`` AI tool? ``(ok, reason)``.

    The tool's slot resolves to ``{}`` when unconfigured (and the Containerizer
    would then silently default to openai/gpt-4o-mini with no key). A resolver
    failure is NOT treated as "unconfigured" (fail open: the build proceeds as
    before and the Containerizer reports the real error).

    Community only: the LLM there is bring-your-own, so an empty slot is the
    normal "not set up yet" state. Paid resolves a managed model through its
    own resolver and is never gated here.
    """
    from utils.paid_import import is_ce_edition

    if not is_ce_edition():
        return True, "paid edition (managed resolver)"
    try:
        from utils.tool_config import resolved_effective_env
        env = resolved_effective_env("wizard") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("wizard LLM preflight: resolver failed (%s) - not blocking", exc)
        return True, "resolver unavailable"
    if (env.get("LLM_MODEL") or "").strip() and (env.get("LLM_PROVIDER") or "").strip():
        return True, "configured"
    return False, "no model configured for the wizard AI tool"


def assert_wizard_llm_configured() -> None:
    """Raise HTTP 409 ``wizard_llm_not_configured`` when the generate phase
    (Containerizer) would run with no model. Call before any row is created,
    and only for builds that need the Containerizer."""
    ok, reason = wizard_llm_configured()
    if ok:
        return
    from fastapi import HTTPException

    logger.info("build submit refused: %s", reason)
    raise HTTPException(
        status_code=409,
        detail={"code": WIZARD_LLM_NOT_CONFIGURED_CODE,
                "message": WIZARD_LLM_NOT_CONFIGURED_MESSAGE},
    )


def _ce_publish_target() -> "tuple[Optional[str], str, str, str]":
    """Where a Community install publishes built images.

    Returns ``(publish_mode, registry_host, user, password)``:
      - not CE                         -> ``(None, "", "", "")`` (the Harbor
        path is untouched; an empty tenant Harbor stays a loud failure).
      - CE, ``LOCAL_REGISTRY`` unset   -> ``("local", "", "", "")``: no
        registry, the image stays in the embedded worker's docker daemon.
      - CE, ``LOCAL_REGISTRY`` set     -> the optional registry (the registry:2
        profile, or any registry the operator names). With
        ``LOCAL_REGISTRY_USERNAME``/``_PASSWORD`` it pushes authenticated as
        always (mode None); without them ``("registry_anonymous", ...)`` for an
        auth-less registry:2.
    Only the explicit ``LOCAL_REGISTRY`` operator setting counts - never a
    derived/guessed hostname.
    """
    from utils.paid_import import is_ce_edition

    if not is_ce_edition():
        return None, "", "", ""
    host = (os.getenv("LOCAL_REGISTRY") or "").strip()
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    if not host:
        return "local", "", "", ""
    user = (os.getenv("LOCAL_REGISTRY_USERNAME") or "").strip()
    pw = os.getenv("LOCAL_REGISTRY_PASSWORD") or ""
    if user and pw:
        return None, host, user, pw
    return "registry_anonymous", host, "", ""


class DifferentiableDeclarationError(Exception):
    """Raised when a wizard-built graph declares ``differentiable: true`` but
    a node has no differentiable path (EXPR 10.1 save-gate, mirrored from
    ``crud.workflow_cud.create_workflow_version``). Callers map this to HTTP
    422; the build itself is refused, never a version that cannot be trained.
    """

# GSEC FX.16 R-023 — per-stage build-credential matrix (isolation mode).
#   platform pull → stages that run `docker build` on the untrusted,
#     LLM-generated Dockerfile and need private FROM base images. This is
#     the ONLY registry credential a build stage ever carries — never the
#     tenant push robot, never anything else.
#   tenant registry (short-lived repo-scoped robot) → stages that push the
#     built image or pull it back from the tenant Harbor.
# The docker-build stages are the untrusted-content surface; the push stage
# is the only step that needs a push credential, and it gets a scoped,
# expiring robot — not the tenant's durable project robot.
_STAGES_NEEDING_PLATFORM_PULL = {"build_variant", "test_and_repair_variant"}
# sample_test (legacy hero γ test) is deliberately ABSENT: it `docker run`s
# the locally built tag and reads no HARBOR_* env (sample_one_test.py), so it
# needs no registry credential at all. sample_test_variant stays — its stage
# falls back to `docker pull` from the tenant Harbor.
_STAGES_NEEDING_TENANT_REGISTRY = {
    "push_variant", "test_and_repair_variant",
    "smoke_variant", "sample_test_variant",
}


# Operator knobs the build stages read (see build-task/stages/_common.py
# build_command): forwarded from the API environment to the build stages.
_BUILD_TUNABLE_ENVS = (
    "BUILD_MEMORY_LIMIT", "BUILD_CPUS", "BUILD_TIMEOUT_S",
    "BUILD_BUILDER", "BUILD_NETWORK", "BUILD_MIN_FREE_GB", "CE_BUILD_CAPS",
)


def _isolate_build_credentials(db) -> bool:
    """GSEC FX.16 R-023 — DB platform setting
    ``node_builder.isolate_build_credentials`` (default True). Fails
    CLOSED to the isolated (safer) posture on any read error."""
    try:
        from services.platform_settings import get_setting
        return bool(get_setting("node_builder.isolate_build_credentials", db=db))
    except Exception:  # noqa: BLE001
        return True


def _external_llm_remediation_allowed(db, tenant_id) -> bool:
    """FX.149 (Q136=A) — may this build's LLM remediation call an
    EXTERNAL provider (Anthropic)? The builder container cannot read
    platform settings, so the decision is made HERE at dispatch and
    rides ``container_env`` as ``EXTERNAL_LLM_REMEDIATION_ALLOWED``.

    Mirrors the gated seam in
    routers/ai_config.resolve_llm_data_routing: the platform external-LLM
    switch (egress.external_llm_enabled, or egress.sealed_mode) must be
    ON, AND when the tenant opt-in gate
    (``ai.external_llm_requires_tenant_opt_in``, default True) is ON the
    tenant must have opted in (tenant-scope row set to False). Fails
    CLOSED (False) on any read error — no silent external call."""
    if tenant_id is None:
        return False
    try:
        from utils import egress_policy
        if not egress_policy.egress_allowed(
                egress_policy.CATEGORY_EXTERNAL_LLM, db=db):
            return False
        from services.platform_settings import get_setting
        key = "ai.external_llm_requires_tenant_opt_in"
        platform_on = get_setting(key, db=db) is True
        tenant_value = get_setting(key, db=db, tenant_id=tenant_id)
        opted_in = (tenant_value is False)
        return (not platform_on) or opted_in
    except Exception:  # noqa: BLE001 — fail closed on gate-read errors
        return False


def _llm_egress_hosts(base_url: Optional[str]) -> list:
    """The hostname(s) the generate container must be allowed to reach for
    the LLM call, as a flat list of strings (the worker's allowed_egress
    format). Empty when no host can be parsed."""
    from urllib.parse import urlparse
    try:
        host = urlparse(base_url or "").hostname
        return [host] if host else []
    except Exception:  # noqa: BLE001
        return []


def _assert_registry_isolated(
    spec: BuildSpec, harbor_url: str, *, local_publish: bool = False,
) -> None:
    """node-code-edit-rebuild §2 (Stream B) — registry-isolation guard.

    Raises ``RegistryIsolationError`` (from ``services.node_build_dispatch``,
    imported lazily to avoid a circular import — that module imports
    ``create_build_workflow`` from this one at module scope) when a build
    cannot be proven isolated to its own tenant's registry:

      - ``spec.tenant_id`` is missing (no tenant identity to isolate against), or
      - ``harbor_url`` failed to resolve (empty), or
      - ``harbor_url`` resolves to MOD's own platform registry.

    ``local_publish`` (Community install, no registry configured: the build
    publishes to the local docker daemon and pushes nothing anywhere) waives
    ONLY the "no registry resolved" check - there is no registry to leak into -
    the tenant-identity requirement still holds.

    Never falls back to MOD's registry for a tenant build — a failure here is
    a hard stop, not a warning. Comparison is scheme-stripped/trailing-slash-
    stripped on both sides so ``https://host/`` and ``host`` compare equal.
    """
    from services.node_build_dispatch import RegistryIsolationError
    from utils.registries import platform_registry

    def _norm(url: str) -> str:
        return (url or "").replace("https://", "").replace("http://", "").rstrip("/")

    normalized_harbor = _norm(harbor_url)
    normalized_platform = _norm(platform_registry())

    if local_publish and not normalized_harbor:
        if getattr(spec, "tenant_id", None) is not None:
            return
    if (
        getattr(spec, "tenant_id", None) is None
        or not normalized_harbor
        or normalized_harbor == normalized_platform
    ):
        raise RegistryIsolationError(
            f"build for tenant_id={getattr(spec, 'tenant_id', None)!r} resolved to "
            f"registry {normalized_harbor!r}, which is not isolated from MOD's "
            f"platform registry {normalized_platform!r}"
        )


def _build_target_repo(spec: BuildSpec) -> str:
    """GSEC FX.16 R-023 — the Harbor repo this build pushes to
    (``<tenant-project>/<image-name>``), for the repo-scoped push robot.
    Lowercased to match Harbor's repo naming; empty when the spec carries
    no image name (the push stage then fails loudly on missing creds)."""
    try:
        from utils.tenant_harbor_client import project_name_for_tenant
        image_name = str(getattr(spec, "image_name", "") or "").strip().lower()
        if not image_name:
            return ""
        return f"{project_name_for_tenant(spec.tenant_id)}/{image_name}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("build-dag: target-repo derivation failed: %s", exc)
        return ""


def _build_workflow_name(spec: BuildSpec) -> str:
    """Stable but unique-per-build workflow name. Uses node_name +
    timestamp so the same node rebuilt N times produces N distinct
    workflow rows."""
    safe = "".join(
        c if c.isalnum() or c in "-_." else "-" for c in spec.node_name
    ).strip("-")
    safe = safe or "build"
    return f"build-{safe}-{int(time.time())}"


def create_build_workflow(
    db: Session,
    spec: BuildSpec,
    *,
    user_id: Optional[int] = None,
    include_generate_phase: bool = False,
    rebuild_variant: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Workflow + initial WorkflowVersion for ``spec``.

    Returns ``{"workflow_id", "workflow_version_id", "graph_json"}``.
    Does NOT trigger a run — caller does that via the orchestrator's
    dispatch path. Decoupled so the caller can persist BuildSpec
    metadata alongside (e.g. to a build_artifacts table later) before
    starting workers.

    When ``include_generate_phase=True``, the DAG starts with a
    ``system-wizard-generate`` node (LLM produces artifacts) followed
    by a ``system-wizard-ingest-artifacts`` node (merges onto
    BuildSpec). Used by the unified wizard endpoint that submits with
    just inputs, no pre-generated artifacts.
    """
    # Backstop for every enqueue path (callers also guard earlier, before they
    # persist a BuildSpec): no Workflow/Version rows when builds can't run.
    # The generate phase runs the Containerizer, which needs its image AND a
    # configured wizard LLM; image-source / rebuild DAGs need neither.
    assert_build_farm_available(needs_containerizer=include_generate_phase)
    if include_generate_phase:
        assert_wizard_llm_configured()

    # Local import — avoid pulling in heavy modules at import time
    # of this file.
    import models

    if rebuild_variant is not None:
        from .generator import generate_single_variant_rebuild_dag
        graph_json = generate_single_variant_rebuild_dag(spec, rebuild_variant)
    else:
        graph_json = generate_build_dag(spec, include_generate_phase=include_generate_phase)

    # EXPR.10.1 — the save-time differentiable-declaration gate in
    # crud/workflow_cud.create_workflow_version refuses a version whose graph
    # declares ``differentiable: true`` but leaves a node without a gradient
    # path. This path builds WorkflowVersion rows directly, bypassing that
    # gate, so apply the SAME check here — before any row is added, so a
    # refused build leaves ZERO workflow_versions rows. Follows this
    # function's existing failure convention (raised domain exception, cf.
    # RegistryIsolationError); callers map it to HTTP 422. Never logs graph
    # content — only the offending node ids the validator itself returns.
    # 1788j CE carve: services.differentiable_nodes is paid/denied in CE — the
    # feature is OFF, so the declaration gate is inert (no violations) and the
    # normal submit proceeds; SaaS keeps the identical check.
    from utils.paid_import import optional_paid
    _diff = optional_paid("services.differentiable_nodes")
    _diff_violations = _diff.validate_differentiable(graph_json) if _diff is not None else []
    if _diff_violations:
        raise DifferentiableDeclarationError(
            f"{len(_diff_violations)} node(s) violate the differentiable "
            "declaration: differentiable_declaration_invalid"
        )

    # Resolve nodeDefinitionName → nodeDefinitionId so the orchestrator's
    # enrichment pass can populate node_kind / executable_path. Looked up
    # against system-shared NodeDefinition rows seeded at startup
    # (system-build-one-variant, etc.).
    import models as _models
    name_to_id: Dict[str, int] = {}
    for n in graph_json.get("nodes", []):
        nd_name = n.get("data", {}).get("nodeDefinitionName")
        if nd_name and nd_name not in name_to_id:
            row = db.query(_models.NodeDefinition).filter(
                _models.NodeDefinition.name == nd_name,
                _models.NodeDefinition.tenant_id.is_(None),
            ).first()
            if row is not None:
                name_to_id[nd_name] = row.id
    for n in graph_json.get("nodes", []):
        nd_name = n.get("data", {}).get("nodeDefinitionName")
        if nd_name in name_to_id:
            n["data"]["nodeDefinitionId"] = name_to_id[nd_name]

    # B5b — inject per-node container_env so the worker can thread
    # STAGE / BUILD_SPEC_ID / VARIANT / API_URL / Harbor creds into
    # the build-task container at run time. The orchestrator copies
    # data.container_env into task_payload.container_env when present
    # (small change in workflow_orchestrator.py).
    api_url = os.getenv("API_INTERNAL_URL", "http://api:80")
    api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")

    # Tenant Harbor (separate from platform Harbor) is what the legacy
    # ImageBuilder pushes to: per-tenant project + auto-provisioned
    # robot creds, gated by an admission allowlist that only accepts
    # tenant-Harbor refs (`stage-harbor.../tenant-X/...`) for
    # tenant-built images. Strip the scheme — Docker image refs use
    # `host[:port]/path`, not `https://host/path`.
    try:
        from routers.node_definitions import _get_or_provision_tenant_harbor_creds
        registry, harbor_user, harbor_pass = _get_or_provision_tenant_harbor_creds(spec.tenant_id)
        harbor_url = registry.replace("https://", "").replace("http://", "").rstrip("/")
    except Exception as _e:
        logger.warning("build-dag: tenant Harbor creds lookup failed: %s", _e)
        harbor_url = ""
        harbor_user = ""
        harbor_pass = ""

    # Community install with no tenant Harbor: publish to the local docker
    # daemon (no registry), or to the operator's optional LOCAL_REGISTRY. The
    # stage env carries MOD_PUBLISH_MODE so the push stage knows this is a
    # deliberate posture and not a failed registry lookup (which stays a loud
    # ``creds_missing`` failure on the paid path).
    publish_mode: Optional[str] = None
    ce_registry_in_use = False
    if not harbor_url:
        publish_mode, _ce_host, _ce_user, _ce_pass = _ce_publish_target()
        if _ce_host:
            harbor_url, harbor_user, harbor_pass = _ce_host, _ce_user, _ce_pass
            ce_registry_in_use = True
        if publish_mode == "local":
            logger.info("build-dag: no registry configured - local publish mode "
                        "(images stay on the worker's docker daemon)")

    # node-code-edit-rebuild §2 — registry isolation. Enforced ONLY for client
    # edit/copy/rebuild dispatches, which opt in by setting
    # ``spec._enforce_tenant_registry`` (see
    # services.node_build_dispatch.seed_spec_from_revision). MOD's own
    # platform/system builds (PLATFORM_TENANT_ID=1 legitimately pushes to
    # mod_core) never set it, so this guard has ZERO blast radius on the
    # pre-existing create_build_workflow callers (build-and-register, build_farm,
    # ml_retrain, backfill, …).
    if getattr(spec, "_enforce_tenant_registry", False):
        _assert_registry_isolated(
            spec, harbor_url, local_publish=(publish_mode == "local"))

    # Platform Harbor pull creds. The synth emits Dockerfiles with FROM
    # lines pointing at private base images on registry.example.com
    # (modtim/base-python, modtim/base-intel-xpu, ...). The build-task
    # container runs `docker build` against a daemon with no platform-Harbor
    # auth, so it would 401 on the FROM. We resolve harbor_username /
    # harbor_password from the platform-tenant Secrets table (tenant_id=1)
    # — the same place Settings → Secrets writes them — and pass them
    # through as PLATFORM_HARBOR_*. The build-task does a `docker login`
    # before `docker build`.
    plat_harbor_url = ""
    plat_harbor_user = ""
    plat_harbor_pass = ""
    try:
        import crud
        PLATFORM_TENANT_ID = 1
        plat_harbor_user = crud.get_decrypted_secret_by_name(db, "harbor_username", PLATFORM_TENANT_ID) or ""
        plat_harbor_pass = crud.get_decrypted_secret_by_name(db, "harbor_password", PLATFORM_TENANT_ID) or ""
        plat_harbor_url = crud.get_decrypted_secret_by_name(db, "harbor_registry", PLATFORM_TENANT_ID) or ""
        # The auto-seeded Vault placeholder is NOT a registry. Canonical "unset"
        # test, shared by five other modules: worker_tasks.py:5258,
        # worker_management.py:1682, platform_feature_config.py:68,
        # harbor_proxy_cache.py:227, dependencies.py:2455,
        # utils/github_token.py:24 (_PLACEHOLDER). This credential path missed
        # it: the placeholder reached PLATFORM_HARBOR_URL, the build stage ran
        # `docker login PLEASE_CONFIGURE` (dial tcp: lookup on a host that
        # does not exist) and buried the real base-image 403 under it.
        # A placeholder row means "this install never configured a platform
        # registry" — no fallback either, so PLATFORM_HARBOR_URL stays empty.
        # An ABSENT row keeps the legacy fallback (paid installs unchanged).
        _plat_url_placeholder = plat_harbor_url.strip().upper() == "PLEASE_CONFIGURE"
        plat_harbor_user = "" if plat_harbor_user.strip().upper() == "PLEASE_CONFIGURE" else plat_harbor_user
        plat_harbor_pass = "" if plat_harbor_pass.strip().upper() == "PLEASE_CONFIGURE" else plat_harbor_pass
        plat_harbor_url = plat_harbor_url.replace("https://", "").replace("http://", "").rstrip("/")
        if _plat_url_placeholder:
            plat_harbor_url = ""
        elif not plat_harbor_url:
            plat_harbor_url = build_push_registry()
            # The env fallback can itself be seeded verbatim with the
            # placeholder; same guard.
            if plat_harbor_url.strip().upper() == "PLEASE_CONFIGURE":
                plat_harbor_url = ""
    except Exception as _e:
        logger.warning("build-dag: platform Harbor creds lookup failed: %s", _e)
    if not plat_harbor_url:
        logger.info("build-dag: no platform Harbor registry configured "
                    "(unset or the seeded placeholder) - build stages skip the "
                    "base-image login; tenant push path unchanged")

    # Map build-task system NodeDefinition name → STAGE env var. The
    # ingest-artifacts node runs the same build-task image, just with
    # a different STAGE, so it lives in this map. The generate node
    # is NOT here — it runs the Containerizer image and needs a
    # different env shape (LLM creds, REPO_URL/INLINE/WRAP, etc.) —
    # see _build_wizard_generate_env below.
    name_to_stage = {
        "system-build-one-variant": "build_variant",
        "system-smoke-one-variant": "smoke_variant",
        "system-push-one-variant": "push_variant",
        "system-sample-test": "sample_test",
        "system-sample-test-one-variant": "sample_test_variant",
        "system-register-node": "register_node",
        "system-test-and-repair-one-variant": "test_and_repair_variant",
        "system-wizard-ingest-artifacts": "ingest_artifacts",
    }

    # Need the build_spec_id — caller (the endpoint) sets it on the
    # spec via the build_spec_record after persistence. We embed it
    # in every node's data so the worker doesn't need to know about
    # workflow_run.parameters.
    build_spec_id = getattr(spec, "_build_spec_id", None)

    # GSEC FX.16 R-023 — prime the short-lived, repo-scoped push robot at
    # submit time (isolated builds only) so claim-time secret resolution is
    # a cache hit. The credential lives only in the build_secrets in-process
    # cache — never in graph_json / task_payload. Failure is non-fatal: the
    # claim-time resolver derives the repo from the newest BuildSpecRecord
    # and re-mints.
    if _isolate_build_credentials(db) and not publish_mode and not ce_registry_in_use:
        _repo = _build_target_repo(spec)
        if _repo:
            try:
                prime_scoped_push_creds(spec.tenant_id, _repo)
            except Exception as _e:  # noqa: BLE001
                logger.warning("build-dag: scoped push priming failed: %s", _e)

    # FX.149 (Q136=A) — resolve the external-LLM remediation gate once
    # per build (same tenant for every stage); the builder container
    # cannot read platform settings, so the outcome rides container_env
    # as EXTERNAL_LLM_REMEDIATION_ALLOWED (absent == OFF).
    _external_remediation_ok = _external_llm_remediation_allowed(
        db, getattr(spec, "tenant_id", None))

    for n in graph_json.get("nodes", []):
        nd_name = n.get("data", {}).get("nodeDefinitionName", "")

        if nd_name == "system-wizard-generate":
            # Generate node runs the Containerizer; its env is built
            # from the BuildSpec's wizard inputs (REPO_URL /
            # INLINE_SOURCE_BODY / WRAP_BASE_IMAGE / etc.) via the
            # canonical helper. build_spec_id is required so the
            # downstream ingest-artifacts node can look up the right
            # spec row.
            if build_spec_id is None:
                logger.error(
                    "build-dag: system-wizard-generate node present but "
                    "spec._build_spec_id not set — Containerizer won't "
                    "be able to write artifacts back. Caller forgot to "
                    "persist the BuildSpec before create_build_workflow()."
                )
                continue
            from .generate_env import build_wizard_generate_env
            gen_env = build_wizard_generate_env(
                db=db, spec=spec,
                build_spec_id=int(build_spec_id),
                api_url=api_url, api_key=api_key,
            )
            # Strip the secrets (API key + LLM key) out of the persisted env;
            # the worker resolves them at claim time via secret_refs.
            # EXCEPTION: a managed local LLM with no MANAGED_LLM_API_KEY needs no
            # auth — skip its secret_ref so the claim path doesn't chase a key that
            # doesn't exist. A real key always rides secret_refs, never plaintext.
            # GT.P2.6c — CE-safe shim (delegates to the paid resolver on Pro;
            # on CE there are no managed AI stacks, so it reports False).
            from utils.tool_config import is_managed_local_llm
            # generate_env may already have repointed a managed URL to the
            # external gateway, so that URL counts as managed too.
            _llm_url = gen_env.get("LLM_BASE_URL", "")
            _ext_llm = (os.getenv("LLM_BASE_URL_EXTERNAL") or "").strip()
            _noauth_llm = (
                is_managed_local_llm(_llm_url) or (_ext_llm and _llm_url == _ext_llm)
            ) and not (gen_env.get("LLM_API_KEY") or "").strip()
            for _k in GENERATE_SECRET_ENV_KEYS:
                gen_env.pop(_k, None)
            n["data"]["container_env"] = gen_env
            n["data"]["secret_refs"] = [
                dict(r) for r in GENERATE_SECRET_REFS
                if not (r.get("env_var") == "LLM_API_KEY" and _noauth_llm)
            ]
            # The generate container must reach the LLM endpoint. Allowlist the
            # LLM host so the worker places it on the egress network instead of
            # the zero-outbound user network.
            _egress = _llm_egress_hosts(gen_env.get("LLM_BASE_URL"))
            if _egress:
                n["data"]["allowed_egress"] = _egress
            continue

        stage = name_to_stage.get(nd_name)
        if not stage:
            continue
        # Only NON-secret config goes into container_env (which persists in
        # graph_json / workflow_runs.parameters / task_payload). Registry
        # creds are attached as source-typed secret_refs and resolved at
        # claim time via the envelope-encrypted /secrets endpoint — see
        # node_builder/build_dag/build_secrets.py.
        #
        # GSEC FX.16 R-023 — isolation mode (DB platform setting
        # node_builder.isolate_build_credentials, default True): the
        # docker-build stages never carry the tenant push robot; only the
        # push/pull stages get a short-lived, repo-scoped robot. Legacy
        # mode (setting False) restores the full BUILD_TASK_SECRET_REFS
        # set on every stage.
        isolate = _isolate_build_credentials(db)
        env = {"STAGE": stage, "API_URL": api_url}
        # CE node builder isolation: point the build stages at the rootless
        # BuildKit sidecar (compose `buildkitd`). Non-secret config only.
        _bk_host = os.getenv("MOD_BUILDKIT_HOST", "").strip()
        if _bk_host and stage in _STAGES_NEEDING_PLATFORM_PULL:
            env["BUILDKIT_HOST"] = _bk_host
        if os.getenv("MOD_REQUIRE_ISOLATED_BUILD", "").strip() == "1" and stage in _STAGES_NEEDING_PLATFORM_PULL:
            env["MOD_REQUIRE_ISOLATED_BUILD"] = "1"
        if not isolate:
            env["HARBOR_URL"] = harbor_url
            if plat_harbor_url:
                env["PLATFORM_HARBOR_URL"] = plat_harbor_url
        elif stage in _STAGES_NEEDING_PLATFORM_PULL:
            if plat_harbor_url:
                env["PLATFORM_HARBOR_URL"] = plat_harbor_url
        elif stage in _STAGES_NEEDING_TENANT_REGISTRY:
            env["HARBOR_URL"] = harbor_url
        if stage == "push_variant" and publish_mode:
            env["MOD_PUBLISH_MODE"] = publish_mode
        if stage == "register_node" and ce_registry_in_use:
            # The optional CE registry: the registered node ref must carry the
            # registry host so a second worker can pull it (no credentials).
            env["HARBOR_URL"] = harbor_url
        if stage in _STAGES_NEEDING_PLATFORM_PULL:
            # Operator build tunables (resource caps, timeout, builder
            # endpoint) ride to the build stages only when set on the API.
            for _tune in _BUILD_TUNABLE_ENVS:
                _tv = os.getenv(_tune)
                if _tv is not None and _tv.strip() != "":
                    env[_tune] = _tv.strip()
        variant = n.get("data", {}).get("variant")
        if variant:
            env["VARIANT"] = variant
        if build_spec_id is not None:
            env["BUILD_SPEC_ID"] = str(build_spec_id)
        # FX.149 (Q136=A) — external (Anthropic) LLM remediation gate.
        # The builder container cannot read platform settings, so the
        # dispatch-time decision rides container_env: "1" only when the
        # platform external-LLM switch + tenant opt-in allow it. Absent
        # (the default) keeps the builder on the non-LLM path.
        if _external_remediation_ok:
            env["EXTERNAL_LLM_REMEDIATION_ALLOWED"] = "1"
        # GC.SMF.6 §6 — stack-mesh env for worker-side (mesh-joined) dispatch.
        # Merged AFTER the stage env is assembled and with setdefault, so a mesh
        # key can never overwrite STAGE/API_URL/HARBOR_*/VARIANT/... ; the builder
        # returns {} for local dispatch, a node bound to no stack, a mesh-less
        # site, and any other tenant's stack (resolve is tenant-scoped).
        from .generate_env import build_node_stack_mesh_env
        for _mesh_k, _mesh_v in build_node_stack_mesh_env(
            db, node=n, tenant_id=spec.tenant_id, remote=True,
        ).items():
            env.setdefault(_mesh_k, _mesh_v)
        n["data"]["container_env"] = env
        if not isolate:
            n["data"]["secret_refs"] = [dict(r) for r in BUILD_TASK_SECRET_REFS]
        else:
            refs: List[Dict[str, str]] = []
            if stage in _STAGES_NEEDING_PLATFORM_PULL:
                refs.extend(dict(r) for r in PLATFORM_HARBOR_PULL_REFS)
            if stage in _STAGES_NEEDING_TENANT_REGISTRY and publish_mode not in (
                "local", "registry_anonymous",
            ):
                # (no credential to mint when there is no registry / no auth)
                refs.extend(scoped_push_refs(_build_target_repo(spec)))
            if refs:
                n["data"]["secret_refs"] = refs

    workflow_name = _build_workflow_name(spec)

    # System-internal build workflows live alongside tenant workflows.
    # They're owned by the building tenant so per-tenant quotas + audit
    # apply correctly. `is_system=True` keeps them out of the user-facing
    # load-workflow picker (the wizard's name-prefix filter is no longer
    # needed once this flag is set + GET /workflows defaults to hiding
    # system workflows).
    workflow = models.Workflow(
        name=workflow_name,
        tenant_id=spec.tenant_id,
        created_by_user_id=user_id,
        is_system=True,
    )
    db.add(workflow)
    db.flush()

    version = models.WorkflowVersion(
        workflow_id=workflow.id,
        version_number=1,
        graph_json=graph_json,
    )
    db.add(version)
    db.flush()

    # Wire the latest_version_id pointer so the workflow is actually
    # runnable (orchestrator reads from latest_version_id when no
    # explicit version is given).
    workflow.latest_version_id = version.id

    # BUG (live 10-09, run 2240 / workflow 383 / version 380) — a build
    # workflow created here had NO `workflow_nodes` rows, so the run the
    # caller dispatched immediately was terminalized by the orchestrator as
    # "orphaned: no node runs (workflow has no workflow_nodes rows)":
    # `_start_workflow` only creates a NodeRun for a graph node that already
    # has a backing WorkflowNode row, and this function — unlike every other
    # workflow-creation path (`crud.workflow_cud.create_workflow_run`,
    # `install_workflow`, the B85c1 seed sweep in main.py) — never
    # materialized them. Run 2241 worked only because the manual re-run went
    # through `create_workflow_run`, which find-or-created the rows then.
    # Materialize them here, BEFORE the caller commits/dispatches, so the
    # single_variant and full_fanout rebuild paths (and every other
    # create_build_workflow caller) are runnable on the first run.
    # Insert-only and idempotent; it does not commit — this function owns the
    # transaction, so the rows land in the same commit as the workflow/version.
    from crud.workflow_cud import sync_workflow_nodes

    synced_nodes = sync_workflow_nodes(db, workflow, graph_json)

    db.commit()
    db.refresh(workflow)
    db.refresh(version)

    logger.info(
        "build-dag: created workflow_id=%d version_id=%d for node='%s'",
        workflow.id, version.id, spec.node_name,
    )

    return {
        "workflow_id": workflow.id,
        "workflow_version_id": version.id,
        "workflow_name": workflow_name,
        "graph_json": graph_json,
        # BUG (10-09): rows materialized before the caller dispatches the run.
        "workflow_node_count": synced_nodes,
    }
