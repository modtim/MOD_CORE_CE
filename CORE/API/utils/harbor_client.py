"""Platform-Harbor HTTP client.

Thin companion to utils.tenant_harbor_client. Where tenant_harbor_client
targets the second, tenant-facing Harbor (tenant-harbor.${BASE_DOMAIN}),
this module talks to the PRIMARY platform Harbor at
harbor.${BASE_DOMAIN} which holds MOD's curated base images.

``create_robot``/``delete_robot`` are used by the platform robot bootstrap
script (``scripts/bootstrap-platform-robots.sh``) to mint scoped robot
accounts for each internal consumer (ImageBuilder pushes, TrivyScanner
pulls, etc). Consumers then read their robot creds from Vault at
``secret/data/infrastructure/platform_harbor/robots/<service>``
instead of sharing one ``HARBOR_ADMIN_PASSWORD`` across the fleet.

``ensure_registry_endpoint``/``ensure_proxy_project`` (GF.C3) are used by
``services/harbor_proxy_cache.py`` to idempotently stand up Harbor
proxy-cache projects for docker.io/nvcr.io/ghcr.io so node/NIM image
pulls come through Harbor's cache instead of hitting upstream directly.

``ensure_project_pull_grant`` (GF.C3a) grants the platform pull
principal (the Harbor account every worker authenticates as when
pulling platform/system node images — see
``services/harbor_proxy_cache.py``'s module docstring for the full
trace) read access on one of those proxy-cache projects. It tries a
system-level robot permission update first (mirrors
``utils.tenant_harbor_client``'s ``grant_repo_pull`` merge-and-PUT
idiom), falling back to a project-member add for a regular Harbor
user/LDAP account.

Distinct from the existing ``routers/harbor.py``, which manages the
user-facing Harbor credential settings in the Dashboard.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import requests
from utils.registries import build_push_registry

logger = logging.getLogger(__name__)


class PlatformHarborError(RuntimeError):
    """Any non-2xx response from platform Harbor (or config errors)."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        #: HTTP status code that triggered this error, when known — lets
        #: callers distinguish "bad/insufficient credential" (401/403)
        #: from any other failure without parsing the message string.
        self.status_code = status_code


@dataclass(frozen=True)
class PlatformRobotCredential:
    username: str
    password: str


def _load_platform_admin_creds_from_vault(
    fallback_username: str = "admin",
) -> tuple[str, Optional[str]]:
    """Resolve ``(admin_username, admin_password)`` for the PRIMARY
    platform Harbor, mirroring ``utils.tenant_harbor_client.
    _load_admin_creds_from_vault`` for the tenant-facing Harbor.

    Vault-first: reads ``admin_password`` from ``secret/data/
    infrastructure/platform_harbor`` via the core-api AppRole. Falls back
    to the existing ``HARBOR_ADMIN_USERNAME``/``HARBOR_ADMIN_PASSWORD``
    env vars. Unlike the tenant-Harbor helper this NEVER raises — this
    credential only gates a best-effort startup bootstrap
    (``services/harbor_proxy_cache.py``), so an unresolved password comes
    back as ``None`` for the caller to soft-fail on, not a hard error.
    """
    env_user = os.getenv("HARBOR_ADMIN_USERNAME", fallback_username)
    env_pw = os.getenv("HARBOR_ADMIN_PASSWORD")
    if env_pw:
        return env_user, env_pw

    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not role_id or not secret_id:
        return env_user, None
    try:
        from utils.vault_client import VaultError, approle_login, read_kv_v2
        token = approle_login(role_id=role_id, secret_id=secret_id)
        data = read_kv_v2("infrastructure/platform_harbor", token=token)
    except Exception as exc:  # noqa: BLE001 — soft-fail, never blocks startup
        logger.debug(
            "harbor_client: platform Harbor admin Vault lookup failed: %s", exc
        )
        return env_user, None
    password = (data or {}).get("admin_password")
    return env_user, (password or None)


class PlatformHarborClient:
    """Minimal Harbor v2.0 client for platform-admin operations.

    Constructor resolves admin creds — explicit args first, then Vault
    (``secret/data/infrastructure/platform_harbor``), then env
    (``HARBOR_ADMIN_USERNAME``/``HARBOR_ADMIN_PASSWORD``) — via
    :func:`_load_platform_admin_creds_from_vault`.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        admin_username: Optional[str] = None,
        admin_password: Optional[str] = None,
        request_timeout: float = 10.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("HARBOR_REGISTRY")
            or build_push_registry()
        ).rstrip("/")
        # Accept bare host or full URL.
        if "://" not in self.base_url:
            self.base_url = "https://" + self.base_url
        if admin_username is None or admin_password is None:
            vault_user, vault_pw = _load_platform_admin_creds_from_vault(
                fallback_username=admin_username or "admin",
            )
            admin_username = admin_username or vault_user
            admin_password = admin_password if admin_password is not None else vault_pw
        self._auth = (admin_username or "admin", admin_password or "")
        #: True only when an admin_password was actually resolved (Vault
        #: or env) — an empty-string password (the historical default)
        #: is a guaranteed 401, not a usable credential.
        self.has_admin_credentials = bool(admin_password)
        self.request_timeout = request_timeout

    def create_robot(
        self,
        name: str,
        scope_project: str = "mod_core",
        actions: Optional[list[str]] = None,
        duration_days: int = -1,
    ) -> PlatformRobotCredential:
        """Mint a project-scoped robot on platform Harbor.

        ``scope_project`` defaults to ``mod_core`` which holds MOD's
        curated base images. ``actions`` defaults to ``["pull"]`` for
        read-only consumers; pass ``["pull", "push"]`` for ImageBuilder
        which needs to push rebuilt base images too.

        Returns the newly-minted (username, password). Password is
        only revealed ONCE; caller must persist to Vault immediately.
        """
        actions = actions or ["pull"]
        body = {
            "name": name,
            "duration": duration_days,
            "level": "project",
            "permissions": [
                {
                    "kind": "project",
                    "namespace": scope_project,
                    "access": [
                        {"resource": "repository", "action": a}
                        for a in actions
                    ],
                }
            ],
        }
        url = f"{self.base_url}/api/v2.0/robots"
        try:
            resp = requests.post(url, auth=self._auth, json=body, timeout=self.request_timeout)
        except requests.RequestException as exc:
            raise PlatformHarborError(f"POST {url} failed: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise PlatformHarborError(
                f"POST {url} returned HTTP {resp.status_code}: {resp.text[:400]}"
            )
        payload = resp.json() or {}
        user = payload.get("name")
        pw = payload.get("secret")
        if not user or not pw:
            raise PlatformHarborError(
                "Harbor robot creation returned no name/secret — refusing to continue"
            )
        return PlatformRobotCredential(username=user, password=pw)

    def delete_robot(self, robot_id: int) -> None:
        """Delete a robot by its Harbor ID (used by rotate-platform-robots)."""
        url = f"{self.base_url}/api/v2.0/robots/{robot_id}"
        try:
            resp = requests.delete(url, auth=self._auth, timeout=self.request_timeout)
        except requests.RequestException as exc:
            raise PlatformHarborError(f"DELETE {url} failed: {exc}") from exc
        if resp.status_code not in (200, 204, 404):
            raise PlatformHarborError(
                f"DELETE {url} returned HTTP {resp.status_code}: {resp.text[:400]}"
            )

    # ------------------------------------------------------------------
    # GF.C3 — proxy-cache bootstrap (registry endpoints + projects)
    # ------------------------------------------------------------------
    #
    # Idempotent GET-then-POST, matching the convention already used by
    # utils/tenant_harbor_client.py and utils/manager_harbor_client.py: a
    # name match on GET (or a 409 on POST, re-fetched) means "already
    # exists" and is returned completely untouched. Nothing here ever
    # deletes or updates an existing registry/project — a name collision
    # with different settings is left as-is for an operator to reconcile
    # by hand, never silently overwritten.

    def _request(
        self,
        method: str,
        path: str,
        json: Any = None,
        allow_status: tuple = (200, 201, 204),
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, json=json, auth=self._auth, timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise PlatformHarborError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code in allow_status:
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise PlatformHarborError(
            f"{method} {url} returned HTTP {resp.status_code}: {resp.text[:400]}",
            status_code=resp.status_code,
        )

    def _request_status(self, method: str, path: str, json: Any = None) -> int:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, json=json, auth=self._auth, timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise PlatformHarborError(f"{method} {url} failed: {exc}") from exc
        return resp.status_code

    def find_registry_by_name(self, name: str) -> Optional[dict]:
        payload = self._request("GET", "/api/v2.0/registries", allow_status=(200,)) or []
        for item in payload:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return None

    def ensure_registry_endpoint(
        self,
        name: str,
        url: str,
        registry_type: str,
        access_key: Optional[str] = None,
        access_secret: Optional[str] = None,
        insecure: bool = False,
    ) -> dict:
        """Idempotently ensure a Harbor registry endpoint (an "upstream" a
        proxy-cache project pulls through) exists.

        ``registry_type`` is Harbor's own vocabulary — ``docker-hub`` for
        docker.io, ``docker-registry`` for a generic v2 registry (nvcr.io,
        ghcr.io). ``access_key``/``access_secret`` are omitted entirely
        (never sent as empty strings) when the upstream is anonymous, so
        Harbor treats the endpoint as unauthenticated rather than as
        "authenticated with blank credentials".
        """
        existing = self.find_registry_by_name(name)
        if existing is not None:
            return existing

        body: dict = {"name": name, "type": registry_type, "url": url, "insecure": insecure}
        if access_key:
            body["access_key"] = access_key
        if access_secret:
            body["access_secret"] = access_secret

        status = self._request_status("POST", "/api/v2.0/registries", json=body)
        if status in (200, 201):
            found = self.find_registry_by_name(name)
            return found if found is not None else body
        if status == 409:
            found = self.find_registry_by_name(name)
            if found is not None:
                return found
            raise PlatformHarborError(
                f"Registry endpoint {name!r} 409'd on create but was not "
                "found on re-fetch"
            )
        raise PlatformHarborError(
            f"POST /api/v2.0/registries for {name!r} returned HTTP {status}",
            status_code=status,
        )

    def find_project_by_name(self, project_name: str) -> Optional[dict]:
        payload = self._request(
            "GET", f"/api/v2.0/projects?name={project_name}", allow_status=(200,)
        ) or []
        for item in payload:
            if isinstance(item, dict) and item.get("project_name") == project_name:
                return item
        return None

    def ensure_proxy_project(
        self, project_name: str, registry_id: Any, public: bool = False,
    ) -> dict:
        """Idempotently ensure a Harbor proxy-cache project exists.

        An existing project with this name is returned untouched — this
        never PUTs an update to ``registry_id``/``public`` on a project
        that's already there, so a manually-tuned project survives every
        re-run of the bootstrap step.
        """
        existing = self.find_project_by_name(project_name)
        if existing is not None:
            return existing

        body = {"project_name": project_name, "public": public, "registry_id": registry_id}
        status = self._request_status("POST", "/api/v2.0/projects", json=body)
        if status in (200, 201):
            found = self.find_project_by_name(project_name)
            return found if found is not None else body
        if status == 409:
            found = self.find_project_by_name(project_name)
            if found is not None:
                return found
            raise PlatformHarborError(
                f"Project {project_name!r} 409'd on create but was not "
                "found on re-fetch"
            )
        raise PlatformHarborError(
            f"POST /api/v2.0/projects for {project_name!r} returned HTTP {status}",
            status_code=status,
        )

    # ------------------------------------------------------------------
    # GF.C3a — proxy-cache pull grants (the principal every worker
    # authenticates as when pulling platform/system node images; see
    # services/harbor_proxy_cache.py's module docstring for the full
    # trace of how that principal is resolved and consumed).
    # ------------------------------------------------------------------

    #: Harbor project role IDs (v2 API). 3 = Guest — read-only, i.e. pull.
    #: Never grant a stronger role than this from here.
    _GUEST_ROLE_ID = 3
    _PULL_ACCESS = [{"resource": "repository", "action": "pull"}]

    def find_robot_by_name(self, name: str) -> Optional[dict]:
        """List-then-filter robot lookup — same idiom as
        ``find_registry_by_name``/``find_project_by_name`` above (and the
        ``q=Field%3Dvalue`` robot-query convention
        ``utils.tenant_harbor_client._revoke_all_robots`` already uses).

        Returns ``None`` if ``name`` isn't a robot account at all — the
        caller falls back to a project-member add for a regular Harbor
        user/LDAP account rather than treating that as an error.
        """
        payload = self._request(
            "GET", f"/api/v2.0/robots?q=Name%3D{name}", allow_status=(200,),
        ) or []
        if not isinstance(payload, list):
            return None
        for item in payload:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return None

    def _robot_put_body(self, robot: dict) -> dict:
        """Project a full GET robot object down to the fields the robot
        PUT endpoint accepts. Never includes the secret."""
        return {
            "name": robot.get("name"),
            "level": robot.get("level") or "system",
            "duration": robot.get("duration", -1),
            "disable": bool(robot.get("disable", False)),
            "permissions": robot.get("permissions") or [],
        }

    @staticmethod
    def _project_pull_perm_id(perm: dict) -> dict:
        """Identity of a project-level permission entry for grant
        matching (kind + namespace only — this grant is project-wide,
        not per-repository, so no extra tagging is needed)."""
        if not isinstance(perm, dict) or perm.get("kind") != "project":
            return {"kind": str((perm or {}).get("kind"))}
        return {"kind": "project", "namespace": str(perm.get("namespace"))}

    def _grant_robot_project_pull(self, robot: dict, project_name: str) -> bool:
        """Merge a project-level pull permission into a robot's existing
        permissions and PUT the full object back. Idempotent — an
        already-present grant makes no PUT. Returns whether a change was
        made.

        Only valid for a ``level: "system"`` robot: those can carry
        permissions across multiple project namespaces. A
        ``level: "project"`` robot is bound to the single project it was
        created against — Harbor does not support widening its scope via
        this PUT — so that case raises rather than silently no-op'ing or
        sending a PUT Harbor will reject/ignore.
        """
        if (robot.get("level") or "project") != "system":
            raise PlatformHarborError(
                f"robot {robot.get('name')!r} is a project-level robot "
                f"scoped to its original project — cannot widen it to "
                f"{project_name!r} via a permissions PUT; recreate it as "
                "a system-level robot (or use a different principal) to "
                "grant pull across multiple proxy-cache projects"
            )
        permissions = list(robot.get("permissions") or [])
        want_id = {"kind": "project", "namespace": project_name}
        for perm in permissions:
            if self._project_pull_perm_id(perm) == want_id:
                return False
        permissions.append({
            "kind": "project",
            "namespace": project_name,
            "access": list(self._PULL_ACCESS),
        })
        body = self._robot_put_body(robot)
        body["permissions"] = permissions
        self._request("PUT", f"/api/v2.0/robots/{robot['id']}", json=body)
        return True

    def find_project_member(self, project_name: str, principal_name: str) -> Optional[dict]:
        payload = self._request(
            "GET",
            f"/api/v2.0/projects/{project_name}/members?entityname={principal_name}",
            allow_status=(200,),
        ) or []
        if not isinstance(payload, list):
            return None
        for item in payload:
            if isinstance(item, dict) and item.get("entity_name") == principal_name:
                return item
        return None

    def _grant_user_project_pull(self, principal_name: str, project_name: str) -> bool:
        """Add ``principal_name`` as a Guest (pull-only) project member.
        Idempotent — an existing membership of ANY role is left
        completely untouched (never upgraded, never downgraded); this
        only adds a member when there is none.
        """
        existing = self.find_project_member(project_name, principal_name)
        if existing is not None:
            return False
        body = {"role_id": self._GUEST_ROLE_ID, "member_user": {"username": principal_name}}
        status = self._request_status(
            "POST", f"/api/v2.0/projects/{project_name}/members", json=body,
        )
        if status in (200, 201, 204):
            return True
        if status == 409:
            return False  # race: member added between our GET and POST
        raise PlatformHarborError(
            f"POST /api/v2.0/projects/{project_name}/members for "
            f"{principal_name!r} returned HTTP {status}"
        )

    def ensure_project_pull_grant(self, principal_name: str, project_name: str) -> bool:
        """Idempotently grant ``principal_name`` pull access on
        ``project_name``.

        Existing grants (of ANY strength — Guest or higher, or a robot's
        pre-existing broader permission) are left untouched; this never
        downgrades or removes a permission, only adds one when missing.

        Tries a system-level robot account first (Harbor robot accounts
        are addressed by name via ``find_robot_by_name``; a project-level
        robot raises rather than being silently mis-granted — see
        ``_grant_robot_project_pull``), falling back to a project-member
        add for a regular Harbor user/LDAP account. Raises
        ``PlatformHarborError`` if ``principal_name`` is neither a known
        robot nor addable as a project member (e.g. no such Harbor user)
        — the caller (``services.harbor_proxy_cache``) logs that as a
        named warning and never raises further.
        """
        robot = self.find_robot_by_name(principal_name)
        if robot is not None:
            return self._grant_robot_project_pull(robot, project_name)
        return self._grant_user_project_pull(principal_name, project_name)
