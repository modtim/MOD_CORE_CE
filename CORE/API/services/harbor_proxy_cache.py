"""GF.C3 (API/bootstrap half) — Harbor proxy-cache bootstrap.

Ensures platform Harbor fronts docker.io, nvcr.io and ghcr.io as
proxy-cache projects, so node/NIM image pulls (worker-side rewrite is
GF.C3b, out of scope here — Go Worker is another agent's tree right now)
can be redirected through Harbor's cache instead of hitting each upstream
directly on every pull.

Two independent halves live in this module:

  * ``ensure_harbor_proxy_cache_projects`` — the idempotent bootstrap
    step. For each upstream: ensure a Harbor registry endpoint (GET-then-
    POST, never updated/deleted once it exists) via
    ``utils.harbor_client.PlatformHarborClient``, then ensure a
    non-public project proxying through it. Wired into API startup
    (``main.py``) behind the ``harbor_proxy_cache_enabled`` platform
    setting.

  * ``rewrite_to_proxy`` — the pure image-ref rewrite contract. No I/O,
    no Harbor calls; just the string transform an image ref undergoes to
    route through the matching proxy-cache project. The API doesn't call
    this today (nothing routes NodeDefinition/NIM image resolution
    through it yet — that's a follow-up), but it is the single place the
    mapping is defined so both this bootstrap step and any future caller
    (API-side or, once ported, Worker-side GF.C3b) agree on the shape.

NGC credential: nvcr.io serves gated NVIDIA NIM images that need the NGC
API key to pull through Harbor's proxy. That key is stored the same way
``services/nim_router_service.py`` already reads it for the live NGC
catalog fetch — a platform-level (cross-tenant) ``Secret`` row named
``ngc_api_key`` (or legacy ``NIM_API_KEY``), decrypted via
``crud.secret_cud.get_decrypted_secret_by_name``. If neither secret is
configured, the nvcr.io registry endpoint is created WITHOUT credentials
(anonymous) and a named warning is logged — gated NIM pulls will fail
until an NGC key is added, but the bootstrap step itself does not fail
or block on it.

GF.C3a — pull-access grant: the three proxy-cache projects above are
created non-public (``public=False``), so by default nobody but a
platform Harbor admin can pull through them. The principal a worker
actually authenticates as when pulling platform/system node images
today is the platform-tenant (``tenant_id=1``) ``harbor_username``/
``harbor_password`` Secret pair:

  * ``routers/worker_tasks.py:get_task_credentials`` (``POST
    /workers/tasks/{task_id}/credentials``, the endpoint the Go Worker
    calls once a task is claimed) resolves ``harbor_registry`` /
    ``harbor_username`` / ``harbor_password`` via its local
    ``_resolve_harbor()`` helper, which reads the tenant's own Secret
    row first and falls back to ``PLATFORM_TENANT_ID = 1`` whenever it's
    unset or still the seeded ``PLEASE_CONFIGURE`` placeholder — "SaaS
    pattern: harbor_* are platform-shared secrets (every tenant pulls
    from the same Harbor)" (see the comment immediately above that
    block). The Go Worker's ``docker login``/pull then uses exactly that
    ``registry``/``username``/``password`` triple — there is no
    per-worker or per-tenant Harbor identity on the pull path today.
  * The same platform-tenant fallback is used identically by
    ``dependencies.py:require_harbor_credentials`` (the pre-flight check
    gating workflow runs) and ``routers/worker_management.py``'s
    ``_harbor_creds_missing``/profiling pre-check — all three read the
    exact same three Secret names off ``tenant_id=1`` when a tenant has
    none of its own.
  * ``CORE/startup/scripts/bootstrap-platform-robots.sh`` is a DIFFERENT
    mechanism entirely: it mints per-service robots (``mod-imagebuilder``,
    ``mod-containerizer``, ``mod-trivyscanner``, ``mod-node_scanner``,
    ``mod-api_healthcheck``) scoped to the single ``mod_core`` project,
    written to Vault at ``infrastructure/platform_harbor/robots/<service>``
    — none of those are the ``harbor_username``/``harbor_password``
    Secret pair, none of them are what a worker authenticates as, and
    none of them get touched here.
  * ``utils/registry_credentials_client.py`` (License-Server-minted
    on-prem registry creds) has zero callers anywhere in the API today
    — dead code, not the live path.

This IS a long-lived, admin-provisioned credential (not per-task/
short-lived — a static grant is the right design), so
``ensure_harbor_proxy_cache_projects`` idempotently grants it pull on
each of the three proxy projects via
``PlatformHarborClient.ensure_project_pull_grant``. If the
``harbor_username`` Secret isn't configured yet, or the account it names
doesn't exist in Harbor as either a system robot or a Harbor user, that
is logged as a named warning (never raised) — the projects are still
created/ensured; only the grant is skipped until an operator configures
a real principal.

GF.C3a-b — the grant is applied to a SET of principals, not just that
one. ``_resolve_pull_principals`` returns the platform-tenant principal
above FIRST, plus any additional principals named by the DB platform
setting ``harbor_proxy_cache.extra_pull_principals`` (registered in this
module via ``platform_settings.register`` — no new env var), so an
operator who adds a second pull identity (a system robot a service
container authenticates as, a LDAP user, ...) gets it granted on every
proxy-cache project on the very same reconcile path, idempotently, with
zero code change. The per-service robots minted by
``CORE/startup/scripts/bootstrap-platform-robots.sh`` are NOT in that
default set: line 86-87 creates them with ``level: "project"`` scoped to
``mod_core``, and Harbor has no API to widen a project-level robot
(``_grant_robot_project_pull`` raises rather than sending a PUT Harbor
would ignore) — an operator who wants one of those pulling through the
proxy cache recreates it as a system robot and names it in the setting.
This module only ever ADDS ``pull`` (read) permissions: it never grants
push/admin, never removes or downgrades an existing grant, and a
principal Harbor can't resolve is a named warning, never a raise, never
a block on the bootstrap step.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services import platform_settings
from utils.paid_import import optional_paid
from utils.harbor_client import PlatformHarborClient, PlatformHarborError

# Paid module (CE-absent): nim_router_service supplies the platform NGC secret.
_nim_router_service = optional_paid("services.nim_router_service")

logger = logging.getLogger(__name__)

_PLATFORM_TENANT_ID = 1

#: (secret name) candidates for the NGC API key, in lookup order — matches
#: services/nim_router_service.py's own fallback chain exactly.
_NGC_SECRET_NAMES = ("ngc_api_key", "NIM_API_KEY")

# ---------------------------------------------------------------------------
# Upstream table — the single source of truth for registry + project names.
# ---------------------------------------------------------------------------
UPSTREAMS: Dict[str, Dict[str, str]] = {
    "docker.io": {
        "registry_name": "upstream-dockerhub",
        "registry_type": "docker-hub",
        "registry_url": "https://hub.docker.com",
        "project": "proxy-dockerhub",
    },
    "nvcr.io": {
        "registry_name": "upstream-nvcr",
        "registry_type": "docker-registry",
        "registry_url": "https://nvcr.io",
        "project": "proxy-nvcr",
    },
    "ghcr.io": {
        "registry_name": "upstream-ghcr",
        "registry_type": "docker-registry",
        "registry_url": "https://ghcr.io",
        "project": "proxy-ghcr",
    },
}

#: host -> project, derived once from UPSTREAMS so rewrite_to_proxy has
#: O(1) lookups without repeating the table.
_HOST_TO_PROJECT: Dict[str, str] = {
    host: spec["project"] for host, spec in UPSTREAMS.items()
}

#: Every proxy-cache project this module ensures — the ``proxy-*`` set the
#: pull grants below are applied to, derived from the same table so a new
#: upstream can never be granted on one path and missed on the other.
_PROXY_PROJECTS: tuple = tuple(spec["project"] for spec in UPSTREAMS.values())

# ---------------------------------------------------------------------------
# GF.C3a-b — extra pull principals (DB platform setting, no env var)
# ---------------------------------------------------------------------------
#: Registry key for the operator-extensible set of ADDITIONAL Harbor
#: principals that need pull on the proxy-cache projects. Empty default:
#: the platform-tenant ``harbor_username`` Secret alone is the only
#: identity the live pull path uses today (see the docstring trace).
_EXTRA_PULL_PRINCIPALS_KEY = "harbor_proxy_cache.extra_pull_principals"

platform_settings.register(
    _EXTRA_PULL_PRINCIPALS_KEY,
    list,
    [],
    group="harbor",
    description="GF.C3a-b — additional Harbor principal names (system "
                "robot account names like 'robot$foo', or Harbor/LDAP "
                "usernames) that are granted PULL (read) access on every "
                "proxy-cache project (proxy-dockerhub/proxy-nvcr/"
                "proxy-ghcr) alongside the platform-tenant "
                "'harbor_username' Secret. Grants are idempotent, pull-"
                "only (never push/admin), and an existing grant of any "
                "strength is never modified. Use this for a second pull "
                "identity a service container authenticates as; a "
                "project-level robot (level:'project', e.g. the ones "
                "bootstrap-platform-robots.sh mints) cannot be widened "
                "and must be recreated as a system robot first.",
)


def _resolve_ngc_credential(db: Any) -> Optional[str]:
    if db is None:
        return None
    if _nim_router_service is None:
        return None
    for name in _NGC_SECRET_NAMES:
        try:
            value = _nim_router_service.get_secret(name, db)
        except Exception as exc:  # noqa: BLE001
            logger.debug("harbor_proxy_cache: secret lookup for %s failed: %s", name, exc)
            continue
        if value:
            return value
    return None


def _resolve_platform_pull_principal(db: Any) -> Optional[str]:
    """The Harbor account name every worker authenticates as when pulling
    platform/system node images today — see this module's docstring
    (GF.C3a) for the full trace. Resolved the same way
    ``routers/worker_tasks.py``'s ``_resolve_harbor`` falls back to the
    platform tenant: the ``harbor_username`` Secret on ``tenant_id=1``.

    Returns ``None`` if unset, empty, or still the auto-seeded
    ``PLEASE_CONFIGURE`` placeholder — matching the same "unset" check
    every other Harbor-credential reader in this codebase uses.
    """
    if db is None:
        return None
    try:
        from crud.secret_cud import get_decrypted_secret_by_name
        value = get_decrypted_secret_by_name(db, "harbor_username", _PLATFORM_TENANT_ID)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "harbor_proxy_cache: harbor_username secret lookup failed: %s", exc
        )
        return None
    if not value or value.strip() in ("", "PLEASE_CONFIGURE"):
        return None
    return value.strip()


def _resolve_extra_pull_principals(db: Any) -> list[str]:
    """Operator-extensible set of ADDITIONAL principals (GF.C3a-b).

    Read from the DB platform setting registered above — never an env var.
    Anything malformed (non-list, non-string entries, blank/placeholder
    names) is dropped with a debug log rather than turned into a Harbor
    call, so a typo'd settings row can't produce a bogus grant attempt.
    """
    if db is None:
        return []
    try:
        raw = platform_settings.get_setting(_EXTRA_PULL_PRINCIPALS_KEY, db=db)
    except Exception as exc:  # noqa: BLE001 — settings row unreadable
        logger.debug(
            "harbor_proxy_cache: extra-principal setting lookup failed: %s", exc
        )
        return []
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            logger.debug(
                "harbor_proxy_cache: ignoring non-string extra-principal entry %r",
                type(item).__name__,
            )
            continue
        cleaned = item.strip()
        if cleaned == "" or cleaned == "PLEASE_CONFIGURE":
            continue
        names.append(cleaned)
    return names


def _resolve_pull_principals(db: Any) -> list[str]:
    """Every principal that needs pull on the proxy-cache projects, in
    grant order: the platform-tenant ``harbor_username`` Secret first
    (the identity every worker actually authenticates as today), then
    the operator-configured extras from the DB setting above. De-duplicated
    so an operator who repeats the tenant principal in the setting gets
    one grant attempt, not two."""
    ordered = [_resolve_platform_pull_principal(db)]
    ordered += _resolve_extra_pull_principals(db)
    seen: set[str] = set()
    principals: list[str] = []
    for name in ordered:
        if name is None or name in seen:
            continue
        seen.add(name)
        principals.append(name)
    return principals


def ensure_harbor_proxy_cache_projects(
    db: Any = None,
    client: Optional[PlatformHarborClient] = None,
) -> Dict[str, Any]:
    """Idempotently ensure the three upstream proxy-cache projects exist.

    Safe to call on every startup / re-run: an upstream whose registry
    endpoint and project both already exist makes zero Harbor writes
    (only the two idempotency GETs). Never deletes or recreates an
    existing endpoint/project, even if this table's URL/type disagree
    with what's already there — an operator's manual edit always wins.

    Returns a counters dict:
      ``{"ensured": [...], "warnings": [...], "errors": [...]}``

    Credential posture: registry-endpoint + project creation are Harbor-
    admin-only operations, so this needs a Harbor admin account (or a
    system robot with registry+project create perms) — see
    ``utils.harbor_client.PlatformHarborClient``/
    ``_load_platform_admin_creds_from_vault``. If no admin-capable
    credential is configured at all, or Harbor rejects the one that is
    (401/403) on the very first call, this logs exactly ONE warning and
    stops the whole pass — it never retries the same missing/bad
    credential once per upstream.
    """
    if db is not None and not platform_settings.get_setting(
        "harbor_proxy_cache_enabled", db=db
    ):
        return {"skipped": "harbor_proxy_cache_enabled is False"}

    if client is None:
        client = PlatformHarborClient()

    counters: Dict[str, Any] = {"ensured": [], "warnings": [], "errors": []}

    if not getattr(client, "has_admin_credentials", True):
        logger.warning(
            "harbor_proxy_cache: no Harbor admin credential configured "
            "(checked Vault secret/data/infrastructure/platform_harbor "
            "and the HARBOR_ADMIN_PASSWORD env var) — registry/project "
            "creation needs a Harbor admin account or a system robot "
            "with registry+project create permissions. Configure one of "
            "those two sources before the proxy-cache bootstrap can run. "
            "Skipping this pass entirely."
        )
        counters["warnings"].append("no_admin_credential_configured")
        return counters

    ngc_key = _resolve_ngc_credential(db)
    if ngc_key is None:
        logger.warning(
            "harbor_proxy_cache: no NGC credential found (Secret "
            "'ngc_api_key' or 'NIM_API_KEY') — creating the nvcr.io proxy "
            "registry endpoint ANONYMOUS. Gated NVIDIA NIM images will "
            "fail to pull through Harbor's cache until an NGC key is "
            "configured."
        )
        counters["warnings"].append("nvcr_anonymous_no_ngc_key")

    pull_principal = _resolve_platform_pull_principal(db)
    if pull_principal is None:
        logger.warning(
            "harbor_proxy_cache: no platform 'harbor_username' Secret "
            "configured (tenant_id=%d) — skipping pull-access grants on "
            "the proxy-cache projects. Workers pull platform/system node "
            "images using exactly that credential (see this module's "
            "docstring); configure it via Settings → Secrets before "
            "relying on the proxy cache.",
            _PLATFORM_TENANT_ID,
        )
        counters["warnings"].append("no_pull_principal_configured")

    #: GF.C3a-b — the tenant principal above plus any operator-configured
    #: extras (DB setting), de-duplicated. Empty when nothing is configured.
    pull_principals = _resolve_pull_principals(db)

    for upstream_host, spec in UPSTREAMS.items():
        access_key = None
        access_secret = None
        if upstream_host == "nvcr.io" and ngc_key:
            # NGC's own docker-login convention: literal username
            # "$oauthtoken", password = the NGC API key.
            access_key = "$oauthtoken"
            access_secret = ngc_key

        try:
            registry = client.ensure_registry_endpoint(
                name=spec["registry_name"],
                url=spec["registry_url"],
                registry_type=spec["registry_type"],
                access_key=access_key,
                access_secret=access_secret,
            )
            registry_id = (registry or {}).get("id")
            if registry_id is None:
                registry_id = (registry or {}).get("registry_id")
            client.ensure_proxy_project(
                project_name=spec["project"],
                registry_id=registry_id,
                public=False,
            )
            counters["ensured"].append(spec["project"])

            for principal in pull_principals:
                try:
                    client.ensure_project_pull_grant(principal, spec["project"])
                except PlatformHarborError as grant_exc:
                    logger.warning(
                        "harbor_proxy_cache: pull-grant principal %r not "
                        "usable for project %s: %s",
                        principal, spec["project"], grant_exc,
                    )
                    counters["warnings"].append(
                        f"pull_grant_principal_not_found:{principal}:"
                        f"{spec['project']}"
                    )
        except PlatformHarborError as exc:
            if exc.status_code in (401, 403):
                logger.warning(
                    "harbor_proxy_cache: Harbor rejected the configured "
                    "admin credential (HTTP %s) ensuring upstream %s — "
                    "the account lacks registry/project admin permission "
                    "(needs a Harbor admin, or a system robot with "
                    "registry+project create perms). Configure Vault "
                    "secret/data/infrastructure/platform_harbor "
                    "(admin_password) or HARBOR_ADMIN_PASSWORD with an "
                    "admin-capable account. Stopping this pass — not "
                    "retrying the same credential on the remaining "
                    "upstream(s).",
                    exc.status_code, upstream_host,
                )
                counters["warnings"].append("admin_credential_rejected")
                break
            logger.error(
                "harbor_proxy_cache: ensure failed for upstream %s: %s",
                upstream_host, exc,
            )
            counters["errors"].append(upstream_host)

    return counters


# ---------------------------------------------------------------------------
# Pure image-ref rewrite contract (GF.C3 §3). Worker-side port is GF.C3b.
# ---------------------------------------------------------------------------

def rewrite_to_proxy(image_ref: str, harbor_host: str) -> str:
    """Rewrite a bare/upstream image ref to pull through Harbor's proxy-cache.

    ``nginx:1``            -> ``<harbor_host>/proxy-dockerhub/library/nginx:1``
    ``bitnami/nginx:1``    -> ``<harbor_host>/proxy-dockerhub/bitnami/nginx:1``
    ``nvcr.io/nim/x:1``    -> ``<harbor_host>/proxy-nvcr/nim/x:1``
    ``ghcr.io/o/r:t``      -> ``<harbor_host>/proxy-ghcr/o/r:t``

    Left completely untouched (returned verbatim):
      * a ref already under ``harbor_host`` (idempotent — rewriting a
        rewritten ref would double-prefix it).
      * a digest-pinned ref (``...@sha256:...``) — a digest pin is
        already exact and registry-agnostic; forcing it through a
        different host than the one it was resolved against is not this
        function's call to make.
      * any host not in :data:`UPSTREAMS` (nothing to rewrite it to).

    Pure function — no network, no Harbor client, no DB. Deliberately
    the ONLY place this mapping is written down so a future caller
    (nothing wires this into a live path yet on the API side; the Go
    Worker port is GF.C3b) can't drift from the bootstrap step above.
    """
    ref = (image_ref or "").strip()
    if not ref:
        return ref

    host_prefix = (harbor_host or "").strip().rstrip("/")
    if host_prefix and (ref == host_prefix or ref.startswith(host_prefix + "/")):
        return ref

    if "@sha256:" in ref:
        return ref

    first_seg, sep, remainder = ref.partition("/")
    if sep and ("." in first_seg or ":" in first_seg or first_seg == "localhost"):
        host = first_seg
        rest = remainder
    else:
        host = "docker.io"
        rest = ref

    project = _HOST_TO_PROJECT.get(host)
    if project is None:
        return ref

    if host == "docker.io" and "/" not in rest:
        # Docker Hub's own convention for unqualified "official" images.
        rest = f"library/{rest}"

    if not host_prefix:
        return ref

    return f"{host_prefix}/{project}/{rest}"
