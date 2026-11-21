"""Claim-time resolution of build-DAG credentials.

The build-DAG used to bake Harbor push/pull creds, the platform service API
key, and the wizard LLM key in PLAINTEXT into every build-task node's
``container_env`` — which then persisted in ``WorkflowVersion.graph_json``,
``workflow_runs.parameters``, and every ``api_task_queue.task_payload`` row.

This module replaces that with source-typed ``secret_refs``: the persisted
payload carries only a *reference* (``{"source": ..., "env_var": ...}``), never
a value. The ``worker_tasks`` ``/secrets`` endpoint resolves the plaintext
in-memory at claim time and returns it X25519/AES-256-GCM envelope-encrypted to
the worker's ephemeral key — the same mechanism user-workflow secrets already
use. Nothing secret is stored at rest.

Resolution is gated by the caller (worker_tasks) to system build-task node
kinds, so a tenant-authored node can never request these sources.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PLATFORM_TENANT_ID = 1

# Build-task nodes (test-and-repair / push / sample-test / register / ingest):
# tenant Harbor push creds + platform Harbor pull creds.
#
# NOTE: API_KEY is NO LONGER injected here. Build-DAG stages used to receive
# the long-lived platform MASTER key as API_KEY via this ref. That's been
# replaced by a per-task, scoped, short-lived token minted in the worker
# credentials endpoint (utils.build_task_token) and injected by the worker as
# API_KEY — same shape as the MinIO STS triplet. The build-task callback
# endpoints verify it and enforce its (tenant_id, build_spec_id) scope.
BUILD_TASK_SECRET_REFS: List[Dict[str, str]] = [
    {"source": "tenant_harbor", "field": "username", "env_var": "HARBOR_USERNAME"},
    {"source": "tenant_harbor", "field": "password", "env_var": "HARBOR_PASSWORD"},
    {"source": "platform_harbor", "field": "username", "env_var": "PLATFORM_HARBOR_USERNAME"},
    {"source": "platform_harbor", "field": "password", "env_var": "PLATFORM_HARBOR_PASSWORD"},
]

# Generate node (Containerizer): wizard LLM key only. It does NOT call back to
# the API (it writes artifacts to /data/output for the ingest stage to POST),
# so it needs no platform API_KEY — the old master-key ref was unused here.
GENERATE_SECRET_REFS: List[Dict[str, str]] = [
    # source name is historical — the value is per-role, not one fixed key (see below).
    {"source": "wizard_llm_api_key", "env_var": "LLM_API_KEY"},
]

# env vars stripped from the generate node's container_env (moved to refs above).
GENERATE_SECRET_ENV_KEYS = ("API_KEY", "LLM_API_KEY")

# GSEC FX.16 R-023 — stage-scoped ref sets (isolation mode). A docker-build
# stage only ever needs to PULL private base images (FROM lines on the
# platform registry); it must never hold the tenant push robot or anything
# else while the untrusted, LLM-generated Dockerfile is being built. The
# push step gets a short-lived, repo-scoped robot minted at claim time
# (``scoped_push_harbor`` source below) — never the tenant's durable
# project-level robot.
PLATFORM_HARBOR_PULL_REFS: List[Dict[str, str]] = [
    {"source": "platform_harbor", "field": "username", "env_var": "PLATFORM_HARBOR_USERNAME"},
    {"source": "platform_harbor", "field": "password", "env_var": "PLATFORM_HARBOR_PASSWORD"},
]

_KNOWN_SOURCES = {
    "tenant_harbor", "platform_harbor", "platform_api_key", "wizard_llm_api_key",
    "scoped_push_harbor",
}

# GSEC FX.16 R-023 — short-lived repo-scoped push robot TTL. Harbor robots
# expire in whole days (minimum 1), so 1 day is the shortest-lived scoped
# credential Harbor can mint; the in-process cache is capped at the same
# window so a claim always reuses the robot that was minted for it.
SCOPED_PUSH_TTL_SECONDS = 24 * 60 * 60


def scoped_push_refs(repo: str) -> List[Dict[str, str]]:
    """secret_refs for a push step: a short-lived, repo-scoped robot.

    ``repo`` is ``<tenant-project>/<image-name>`` (no registry host). The
    ref carries ONLY the repo identity — the credential itself is minted at
    claim time and never persisted (graph_json / task_payload stay clean).
    """
    return [
        {"source": "scoped_push_harbor", "field": "username",
         "env_var": "HARBOR_USERNAME", "repo": repo},
        {"source": "scoped_push_harbor", "field": "password",
         "env_var": "HARBOR_PASSWORD", "repo": repo},
    ]


# In-process mint cache: tenant_id -> (repo, username, password, expires_at).
# One entry per tenant: the username + password ref pair in a single
# /secrets call must resolve to the SAME robot, and re-claims within the
# robot's TTL must not mint a new robot per claim. The credential lives
# only in this dict — never in a DB row, payload, or log line.
_SCOPED_PUSH_CACHE: Dict[int, tuple] = {}


def prime_scoped_push_creds(tenant_id: int, repo: str) -> Optional[tuple]:
    """Mint the repo-scoped push robot for ``repo`` NOW (submit time) and
    cache it so claim-time resolution is a cache hit. Returns
    ``(username, password)`` or None when minting is unavailable (tenant
    Harbor disabled / Harbor unreachable) — the build then degrades to the
    loud "missing registry creds" failure the stage already handles."""
    return _mint_scoped_push_creds(tenant_id, repo)


def _derive_repo_for_tenant(db, tenant_id: int) -> Optional[str]:
    """Best-effort target repo for a tenant's in-flight build: the newest
    non-deleted BuildSpecRecord's image_name. Used when the in-process
    cache is cold (API restarted between submit and claim)."""
    try:
        import models
        row = (
            db.query(models.BuildSpecRecord.spec_json)
            .filter(
                models.BuildSpecRecord.tenant_id == tenant_id,
                models.BuildSpecRecord.deleted_at.is_(None),
            )
            .order_by(models.BuildSpecRecord.id.desc())
            .first()
        )
        if not row:
            return None
        spec_json = row[0]
        image_name = str((spec_json or {}).get("image_name") or "").strip().lower()
        if not image_name:
            return None
        from utils.tenant_harbor_client import project_name_for_tenant
        return f"{project_name_for_tenant(tenant_id)}/{image_name}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("_derive_repo_for_tenant(tenant=%s) failed: %s", tenant_id, exc)
        return None


def _mint_scoped_push_creds(tenant_id: int, repo: str) -> Optional[tuple]:
    """Mint (or reuse from the in-process cache) the short-lived, repo-
    scoped push robot for ``tenant_id``. Returns ``(username, password)``
    or None on any failure — the caller treats None as 'creds unavailable'
    and the stage fails with a loud, actionable error instead of a silent
    gap."""
    now = time.time()
    cached = _SCOPED_PUSH_CACHE.get(tenant_id)
    if cached and cached[3] > now:
        return (cached[1], cached[2])
    try:
        from utils.tenant_harbor_client import (
            TenantHarborClient, tenant_harbor_enabled,
        )
        if not tenant_harbor_enabled():
            logger.info(
                "mint_scoped_push: tenant Harbor disabled — no robot minted "
                "for tenant %s repo %s", tenant_id, repo,
            )
            return None
        harbor = TenantHarborClient()
        robot = harbor.create_push_robot(
            tenant_id, name=f"mod-build-{tenant_id}-{int(now)}",
            repo=repo, duration_days=1,
        )
        entry = (repo, robot.username, robot.password, now + SCOPED_PUSH_TTL_SECONDS)
        _SCOPED_PUSH_CACHE[tenant_id] = entry
        logger.info(
            "mint_scoped_push: tenant %s repo %s robot minted (expires in %ds)",
            tenant_id, repo, SCOPED_PUSH_TTL_SECONDS,
        )
        return (robot.username, robot.password)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mint_scoped_push(tenant=%s repo=%s) failed: %s", tenant_id, repo, exc,
        )
        return None


def is_build_secret_ref(ref: Any) -> bool:
    """True iff ``ref`` is a recognised source-typed build-credential ref."""
    return isinstance(ref, dict) and ref.get("source") in _KNOWN_SOURCES


def _ce_registry_cred(field: Optional[str]) -> Optional[str]:
    """Community install only: the optional registry's credential from the API
    env (``LOCAL_REGISTRY_USERNAME`` / ``_PASSWORD``), used when no tenant
    Harbor robot exists. ``None`` on the paid edition (the Harbor path is
    unchanged) and whenever the operator configured no registry credential."""
    try:
        from utils.paid_import import is_ce_edition
        if not is_ce_edition() or not (os.getenv("LOCAL_REGISTRY") or "").strip():
            return None
    except Exception:  # noqa: BLE001
        return None
    name = {"username": "LOCAL_REGISTRY_USERNAME",
            "password": "LOCAL_REGISTRY_PASSWORD"}.get(field or "")
    return (os.getenv(name) or None) if name else None


def resolve_build_secret_ref(
    db, tenant_id: int, source: str, field: Optional[str],
) -> Optional[str]:
    """Resolve one source-typed build secret to plaintext (in-memory only).

    The caller envelope-encrypts the result before it leaves the process; this
    function never persists or logs the value.
    """
    value = _resolve_build_secret_ref(db, tenant_id, source, field)
    if value is None and source in ("tenant_harbor", "scoped_push_harbor"):
        return _ce_registry_cred(field)
    return value


def _resolve_build_secret_ref(
    db, tenant_id: int, source: str, field: Optional[str],
) -> Optional[str]:
    try:
        if source == "tenant_harbor":
            from routers.node_definitions import _get_or_provision_tenant_harbor_creds
            _registry, user, password = _get_or_provision_tenant_harbor_creds(tenant_id)
            if field == "username":
                return user or None
            if field == "password":
                return password or None
            return None
        if source == "platform_harbor":
            import crud
            name = (
                "harbor_username" if field == "username"
                else "harbor_password" if field == "password"
                else None
            )
            if not name:
                return None
            return crud.get_decrypted_secret_by_name(db, name, _PLATFORM_TENANT_ID) or None
        if source == "platform_api_key":
            return os.getenv("PLATFORM_SERVICE_API_KEY") or None
        # "wizard_llm_api_key" is a SOURCE NAME, not a fixed key: the value is
        # whatever the wizard's own slot resolves to. resolved_effective_env ->
        # routers.ai_config._resolved_effective_env -> services.ai_slot_resolver
        # .resolve_slot (or _legacy_effective_env) -> routers.ai_config._managed_
        # key(role=resolve_role(...)) -> services.managed_ai_stacks.managed_llm_
        # api_key, which reads that role's Vault slot (_MANAGED_LLM_ROLE_PATHS)
        # and falls through to the shared MANAGED_LLM_API_KEY floor. The legacy
        # `sk-vllm-no-auth` sentinel is gone: empty means "no key configured"
        # (no Authorization header), never a value to authenticate with.
        if source == "wizard_llm_api_key":
            from utils.tool_config import resolved_effective_env
            return (resolved_effective_env("wizard") or {}).get("LLM_API_KEY") or None
        if source == "scoped_push_harbor":
            # GSEC FX.16 R-023 — short-lived, repo-scoped push robot. The
            # ref's own ``repo`` key is NOT available to this caller (the
            # /secrets endpoint passes only source + field), so the repo is
            # taken from the in-process mint cache (primed at submit time by
            # ``prime_scoped_push_creds``) or derived from the tenant's
            # newest BuildSpecRecord when the cache is cold. ``field`` is
            # username|password.
            if field not in ("username", "password"):
                return None
            cached = _SCOPED_PUSH_CACHE.get(tenant_id)
            if cached and cached[3] > time.time():
                return cached[1] if field == "username" else cached[2]
            repo = _derive_repo_for_tenant(db, tenant_id)
            if not repo:
                logger.warning(
                    "scoped_push_harbor: no target repo for tenant %s — "
                    "no robot minted", tenant_id,
                )
                return None
            creds = _mint_scoped_push_creds(tenant_id, repo)
            if not creds:
                return None
            return creds[0] if field == "username" else creds[1]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "resolve_build_secret_ref(source=%s field=%s tenant=%s) failed: %s",
            source, field, tenant_id, exc,
        )
    return None
