"""GX.A3 — app-key scope enforcement (default deny) + stack-pinned key (formerly deploy token).

An API key with ``app_id`` set is an **app key**. Every authenticated
request carrying one is checked HERE, in one central place (the auth
dependencies call :func:`check_app_scope`), against:

  1. the app row — must exist, not be soft-deleted, status ``active``;
  2. the route — ``(method, path)`` must match one of the allowlist
     templates below; unmatched routes are denied (a new route can never
     silently accept an app key);
  3. the resource — the path's workflow/run/version/cog id must be inside
     the app's ``allowed_workflow_ids`` / ``allowed_folder_ids`` (cogs by
     descendant walk, depth-bounded).

Denials raise ``HTTPException(403, detail="app_key_scope_denied")`` and
log an ``AUTH_APP_SCOPE_DENIED`` audit event, rate-limited to one per
key+route per minute (in-process) so a stuck polling client cannot flood
the audit table.

Non-app keys (``app_id IS NULL``) and Bearer-only requests bypass this
entirely (``check_app_scope`` no-ops).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable, NamedTuple, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# resource_kind values:
#   none       — no resource check (tenant/app-owned surface)
#   workflow   — {workflow_id} must be in allowed_workflow_ids
#   version    — {version_id} -> WorkflowVersion.workflow_id in scope
#   run        — {run_id} -> WorkflowRun.workflow_id in scope (one query)
#   cog        — {cog_id} in allowed_folder_ids or a descendant thereof
#   own-app    — {app_id} path param must equal the key's app_id
#   app-stack  — {stack_id} must be the App's own target stack (same tenant,
#                not torn down) and {service} its target stack_service
#
# Template extensions (compiled by _compile_rule): ``{name:seg}`` = exactly
# one non-empty path segment (no ``/``); a TRAILING ``/{name:opt}`` = the
# rest of the path, optional (matches with or without ``/<tail>``).
# Generic stack call route (routers/stack_call.py, router prefix /stacks,
# mounted under /v1 in main.py): all five methods the router serves.
STACK_CALL_RULES: tuple = tuple(
    (m, "/v1/stacks/{stack_id}/services/{service:seg}/call/{path:opt}", "app-stack")
    for m in ("GET", "POST", "PUT", "PATCH", "DELETE")
)

APP_SCOPE_ALLOWLIST: tuple = (
    ("GET", "/v1/road/{workflow_id}", "workflow"),
    ("GET", "/v1/road/{workflow_id}/runs", "workflow"),
    ("POST", "/v1/road/workflow-versions/{version_id}/run", "version"),
    ("GET", "/v1/road/workflow-runs/{run_id}", "run"),
    ("GET", "/v1/road/workflow-runs/{run_id}/progress", "run"),
    ("GET", "/v1/road/workflow-runs/{run_id}/node-status", "run"),
    ("POST", "/v1/road/workflow-runs/{run_id}/cancel", "run"),
    ("GET", "/v1/runs/{run_id}", "run"),
    ("GET", "/v1/runs/{run_id}/result", "run"),
    ("GET", "/v1/runs/{run_id}/previews", "run"),
    ("POST", "/v1/runs/ephemeral-inputs", "none"),
    ("GET", "/v1/assets/{cog_id}", "cog"),
    ("GET", "/v1/assets/{cog_id}/download", "cog"),
    ("GET", "/v1/assets/{cog_id}/preview-url", "cog"),
    ("GET", "/v1/assets/{cog_id}/stream", "cog"),
    ("GET", "/v1/assets/{cog_id}/thumbnail", "cog"),
    ("POST", "/v1/email/send", "none"),
    ("GET", "/v1/email/messages", "none"),
    ("GET", "/v1/email/messages/{message_id}", "none"),
    ("POST", "/v1/llm/chat/completions", "none"),
    ("GET", "/v1/llm/models", "none"),
    ("GET", "/v1/billing/credit-rate", "none"),  # 30.6.10 — tenant-wide public pricing read
    # 30.6.9 c9-J3 — PUT|GET /v1/road/by-slug/{slug}. Kind is `none`, NOT
    # the design's `own-app` (:112): own-app compares an {app_id} PATH param
    # (this file :40, _scope_violation :337-340) and this route has no {app_id}
    # segment — the slug is the only path param. The App check lives in the
    # router instead (routers/workflows_domains/workflow_by_slug.py): add_to_app_id
    # must equal the key's app_id, plus the hijack guard (an existing slug must
    # already be in the App's allowed_workflow_ids), which is strictly stronger
    # than own-app. Unmatched deploy tokens still default-deny (check_deploy_token_scope).
    ("PUT", "/v1/road/by-slug/{slug:seg}", "none"),
    ("GET", "/v1/road/by-slug/{slug:seg}", "none"),
    ("GET", "/v1/apps/{app_id}/ephemeral", "own-app"),
    ("POST", "/v1/apps/{app_id}/ephemeral", "own-app"),
    # GX.S6 — per-app storage scopes. ``app_id`` must equal the key's app_id
    # (own-app). The object-key tail after ``objects/`` may contain slashes,
    # so it is matched as a path param (``{object_key:path}``) rather than an
    # int. ``/v1/storage/scopes/stack/...`` is deliberately NOT listed — a
    # stack scope is denied to app keys (unmatched -> deny).
    ("POST", "/v1/storage/scopes/app/{app_id}/links", "own-app"),
    ("GET", "/v1/storage/scopes/app/{app_id}/objects", "own-app"),
    ("DELETE", "/v1/storage/scopes/app/{app_id}/objects/{object_key:path}", "own-app"),
    # GX.G2.s2b — app surface per docs/build-on-mod/APP-API-BRIEF.md.
    # POST fan-out only (the inbox read/mark/delete routes are
    # user-session routes). Handlers are tenant-scoped via the key
    # owner's tenant (require_bearer_or_api_key resolves app keys to
    # the owning user).
    ("POST", "/v1/notifications", "none"),
    # Scheduled HTTP triggers (routers/http_triggers.py; OZ-F3/OZ-F4 renamed the
    # public mount to /v1/monkeys, so the family is /v1/monkeys/triggers under /v1) — the full documented set in the brief's "Scheduled
    # HTTP Triggers" section.
    ("GET", "/v1/monkeys/triggers", "none"),
    ("POST", "/v1/monkeys/triggers", "none"),
    ("GET", "/v1/monkeys/triggers/{trigger_id}", "none"),
    ("PATCH", "/v1/monkeys/triggers/{trigger_id}/pause", "none"),
    ("PATCH", "/v1/monkeys/triggers/{trigger_id}/resume", "none"),
    ("POST", "/v1/monkeys/triggers/{trigger_id}/run", "none"),
    ("GET", "/v1/monkeys/triggers/{trigger_id}/runs", "none"),
    ("DELETE", "/v1/monkeys/triggers/{trigger_id}", "none"),
) + STACK_CALL_RULES

_INT = r"(?P<{name}>\d+)"

_MAX_FOLDER_WALK_DEPTH = 8
_AUDIT_COOLDOWN_SECONDS = 60.0


class _Rule(NamedTuple):
    method: str
    regex: re.Pattern
    resource_kind: str
    param_names: tuple
    template: str


_PARAM_RE = r"\{(\w+)(?::(path|seg|opt))?\}"


def _compile_rule(method: str, template: str, resource_kind: str) -> _Rule:
    # ``{name}`` -> a positive int (ids); ``{name:path}`` -> any non-empty
    # tail including slashes (storage object keys); ``{name:seg}`` -> exactly
    # one non-empty segment (no slash); trailing ``/{name:opt}`` -> optional
    # slash-prefixed tail.
    names = tuple(re.findall(_PARAM_RE, template))
    names = tuple(n for n, _k in names)
    opt = re.search(r"/\{(\w+):opt\}$", template)
    if opt:
        template = template[:opt.start()]

    def _sub(m):
        if m.group(2) == "path":
            return f"(?P<{m.group(1)}>.+)"
        if m.group(2) == "seg":
            return f"(?P<{m.group(1)}>[^/]+)"
        return _INT.format(name=m.group(1))

    pattern = re.sub(_PARAM_RE, _sub, template)
    if opt:
        pattern += f"(?:/(?P<{opt.group(1)}>.+))?"
    return _Rule(method, re.compile(f"^{pattern}$"), resource_kind, names,
                 template if not opt else template + "/{" + opt.group(1) + ":opt}")


_RULES: tuple = tuple(
    _compile_rule(m, t, k) for (m, t, k) in APP_SCOPE_ALLOWLIST
)

# =============================================================================
# 30.6.3 (2400GX-c3-J2) — per-stack DEPLOY-token route scope (default deny).
#
# A deploy token is an ApiKey row whose ``deploy_token_stack_id`` is set; its
# scopes live in ``capability_scope`` as ``"stack:<scope>"`` entries drawn
# from models.API_KEY_DEPLOY_TOKEN_SCOPES. Unlike an app key it owns NO app —
# it owns ONE STACK, so every route it may hit is pinned to that stack
# (resource_kind ``own-stack``: the ``{stack_id}`` path param must equal the
# key row's ``deploy_token_stack_id``; the stack id is NEVER read from the
# body or the query).
#
# Lead-vet scope table (S4 own-stack pin, S15 human-only deploy-token
# management routes stay off-list, S3 default-deny):
#   deploy        POST /v1/stacks/{stack_id}/deploy
#                 POST /v1/stacks/{stack_id}/revert
#   logs          POST /v1/stacks/{stack_id}/logs
#   backups:read  GET  /v1/stacks/{stack_id}/backups
#   backups:run   POST /v1/stacks/{stack_id}/backup
#   seed          POST /v1/stacks/{stack_id}/seed
#                 POST /v1/stacks/{stack_id}/seed/{seed_id}/parts
#                 POST /v1/stacks/{stack_id}/seed/{seed_id}/commit
#   implicit      GET  /v1/stacks/{stack_id}   (every token, always)
#
# Anything else — including /v1/stacks/{stack_id}/deploy-tokens (J3, human-
# only), every non-stack route, a request with no request context, a key that
# is BOTH app-bound and stack-pinned, and a token carrying no recognised
# scope — is DENIED (fail closed).
# =============================================================================

STACK_SCOPE_ALLOWLIST: dict = {
    "deploy": (
        ("POST", "/v1/stacks/{stack_id}/deploy", "own-stack"),
        ("POST", "/v1/stacks/{stack_id}/revert", "own-stack"),
    ),
    "logs": (
        ("POST", "/v1/stacks/{stack_id}/logs", "own-stack"),
    ),
    "backups:read": (
        ("GET", "/v1/stacks/{stack_id}/backups", "own-stack"),
    ),
    "backups:run": (
        ("POST", "/v1/stacks/{stack_id}/backup", "own-stack"),
    ),
    "seed": (
        ("POST", "/v1/stacks/{stack_id}/seed", "own-stack"),
        ("POST", "/v1/stacks/{stack_id}/seed/{seed_id:seg}/parts", "own-stack"),
        ("POST", "/v1/stacks/{stack_id}/seed/{seed_id:seg}/commit", "own-stack"),
    ),
}

# back-compat alias (old tests/callers stay green) — the SAME object.
DEPLOY_TOKEN_SCOPE_RULES: dict = STACK_SCOPE_ALLOWLIST

STACK_KEY_SCOPE_PREFIX = "stack:"
STACK_KEY_SCOPES: frozenset = frozenset(
    {"deploy", "logs", "backups:read", "backups:run", "seed"})

# The full request-side entries: a stack scope is ONLY an exact member of this
# set. Other "stack:<x>" entries (the platform capability "stack:author") are
# ORDINARY scopes and pass through the general machinery untouched.
_STACK_SCOPE_ENTRY_NAMES: frozenset = frozenset(
    STACK_KEY_SCOPE_PREFIX + name for name in STACK_KEY_SCOPES)

_DEPLOY_TOKEN_IMPLICIT: tuple = (
    _compile_rule("GET", "/v1/stacks/{stack_id}", "own-stack"),
)

_DEPLOY_TOKEN_RULES_BY_SCOPE: dict = {
    scope: tuple(_compile_rule(m, t, k) for (m, t, k) in rules)
    for scope, rules in STACK_SCOPE_ALLOWLIST.items()
}


def stack_key_route_rules() -> tuple:
    """Compiled gate-route set for a stack-pinned key (single source for the
    router-wide gate in dependencies.py).

    Exactly six templates: the implicit ``GET /v1/stacks/{stack_id}`` plus the
    five operational gate routes (deploy, revert, logs, backup, backups). The
    seed routes are NOT part of the gate set (they are scope-allowed, not
    gate-protected), and the human-only ``/deploy-tokens*`` routes are on no
    list at all.
    """
    return (
        _DEPLOY_TOKEN_IMPLICIT[0],
        *(_compile_rule(m, t, k)
          for scope in ("deploy", "logs", "backups:read", "backups:run")
          for (m, t, k) in STACK_SCOPE_ALLOWLIST[scope]),
    )

# Audit-flood guard: at most one AUTH_APP_SCOPE_DENIED per (key, route) per
# minute. In-process dict — a restart simply resets the clock.
_audit_last_emitted: dict = {}


def _audit_throttled(key: str) -> bool:
    """Return True when the audit event for this key+route may fire now."""
    now = time.monotonic()
    last = _audit_last_emitted.get(key)
    if last is not None and now - last < _AUDIT_COOLDOWN_SECONDS:
        return False
    _audit_last_emitted[key] = now
    return True


def _request_path(request) -> str:
    """Best-effort path extraction that works for real Starlette Requests,
    scope dicts, and simple fakes used by the unit tests."""
    url = getattr(request, "url", None)
    if url is not None and getattr(url, "path", None):
        return url.path
    scope = getattr(request, "scope", None) or {}
    if isinstance(scope, dict) and scope.get("path"):
        return scope["path"]
    if hasattr(request, "path"):
        return request.path
    return ""


def _request_method(request) -> str:
    m = getattr(request, "method", None)
    if m:
        return str(m).upper()
    scope = getattr(request, "scope", None) or {}
    if isinstance(scope, dict) and scope.get("method"):
        return str(scope["method"]).upper()
    return ""


def _deny(db, request, api_key, template: Optional[str], reason: str,
          tenant_id: Optional[int] = None, *,
          detail: str = "app_key_scope_denied",
          extra: Optional[dict] = None) -> None:
    route = template or _request_path(request)
    # FIX-DENY-AUDIT-ROLLBACK-500: read every key attribute BEFORE the audit
    # try. A failed audit insert expires the ORM key object; re-reading it
    # from the rollback-only session raised PendingRollbackError (a 500 where
    # this 403 is required). Ids only — never key material.
    key_id = api_key.id
    key_app_id = getattr(api_key, "app_id", None)
    key_user_id = getattr(api_key, "user_id", None)
    throttle_key = f"{key_id}|{route}"
    if _audit_throttled(throttle_key):
        try:
            import models  # local import — models import is heavy at module load
            # audit_events.tenant_id FKs tenants: the 0 sentinel has no row, so
            # resolve the KEY OWNER's tenant (never another tenant's data) and
            # skip the write when it cannot be resolved.
            audit_tenant = tenant_id or getattr(getattr(api_key, "user", None),
                                                "tenant_id", None)
            if audit_tenant is None and key_user_id is not None:
                row = (db.query(models.User.tenant_id)
                       .filter(models.User.id == key_user_id).first())
                audit_tenant = row[0] if row else None
            if audit_tenant is None:
                logger.warning(
                    "app-key scope denial (audit skipped: tenant unresolved): "
                    "key=%s route=%s reason=%s",
                    key_id, route, reason,
                )
            else:
                details = {
                    "api_key_id": key_id,
                    "app_id": key_app_id,
                    "route": route,
                    "reason": reason,
                }
                if extra:
                    details.update(extra)
                db.add(models.AuditEvent(
                    tenant_id=audit_tenant,
                    user_id=key_user_id,
                    event_name=models.AuditAction.AUTH_APP_SCOPE_DENIED.value,
                    details=details,
                ))
                db.commit()
        except Exception:  # audit failures must not break the 403
            try:
                db.rollback()  # first: the session must stay usable for the caller
            except Exception:
                logger.warning(
                    "app-key scope denial (audit rollback failed): key=%s route=%s",
                    key_id, route,
                )
            logger.warning(
                "app-key scope denial (audit write failed): key=%s route=%s reason=%s",
                key_id, route, reason,
            )
    logger.info(
        "app-key scope denied: key=%s app=%s route=%s reason=%s",
        key_id, key_app_id, route, reason,
    )
    raise HTTPException(status_code=403, detail=detail)


def _as_id_list(value) -> list:
    if not value:
        return []
    out = []
    for v in value if isinstance(value, (list, tuple)) else [value]:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _cog_in_allowed_folders(db, cog_id: int, allowed_folder_ids: list) -> bool:
    """True when cog_id is an allowed folder or a descendant of one.

    Walks Cog.parent_id upward, depth-bounded (8) so a pathological or
    cyclic tree cannot loop.
    """
    if cog_id in allowed_folder_ids:
        return True
    import models
    current = cog_id
    for _ in range(_MAX_FOLDER_WALK_DEPTH):
        row = (
            db.query(models.Cog.parent_id)
            .filter(models.Cog.id == current)
            .first()
        )
        parent = row[0] if row else None
        if parent is None:
            return False
        if parent in allowed_folder_ids:
            return True
        current = parent
    return False


def _workflow_in_tenant(db, workflow_id, tenant_id) -> bool:
    import models
    return (db.query(models.Workflow.id)
            .filter(models.Workflow.id == workflow_id,
                    models.Workflow.tenant_id == tenant_id)
            .first()) is not None


def _scope_violation(db, app, kind: str, params: dict, own_app_id,
                     tenant_id=None, *, pinned_stack_id=None) -> Optional[str]:
    """Per-kind scope check body. Returns the denial reason, or None if allowed.

    ``pinned_stack_id`` is the stack-key own-stack pin (the key row's
    ``deploy_token_stack_id``); it is only consulted by the ``own-stack`` kind.
    """
    import models
    if kind == "none":
        return None
    if kind == "own-stack":
        # The pin is the ROW COLUMN, never a scope string; the stack id is
        # read ONLY from the matched path param (never body/query). A None
        # pin fails closed; a non-int path param is denied.
        if pinned_stack_id is None:
            return "not_your_stack"
        try:
            sid = int(params["stack_id"])
        except (KeyError, TypeError, ValueError):
            return "not_your_stack"
        if sid != pinned_stack_id:
            return "not_your_stack"
        return None
    if kind == "own-app":
        if int(params["app_id"]) != own_app_id:
            return "foreign_app_id"
        return None
    if kind == "workflow":
        allowed = set(_as_id_list(app.allowed_workflow_ids))
        if int(params["workflow_id"]) not in allowed:
            return "workflow_not_allowed"
        return None
    if kind == "version":
        allowed = set(_as_id_list(app.allowed_workflow_ids))
        row = (
            db.query(models.WorkflowVersion.workflow_id)
            .filter(models.WorkflowVersion.id == int(params["version_id"]))
            .first()
        )
        if row is None or row[0] not in allowed:
            return "version_workflow_not_allowed"
        if tenant_id is not None and not _workflow_in_tenant(db, row[0], tenant_id):
            return "version_workflow_not_allowed"
        return None
    if kind == "run":
        allowed = set(_as_id_list(app.allowed_workflow_ids))
        row = (
            db.query(models.WorkflowRun.workflow_id)
            .filter(models.WorkflowRun.id == int(params["run_id"]))
            .first()
        )
        if row is None or row[0] not in allowed:
            return "run_workflow_not_allowed"
        if tenant_id is not None and not _workflow_in_tenant(db, row[0], tenant_id):
            return "run_workflow_not_allowed"
        return None
    if kind == "cog":
        folders = _as_id_list(app.allowed_folder_ids)
        if not _cog_in_allowed_folders(db, int(params["cog_id"]), folders):
            return "cog_not_in_allowed_folder"
        return None
    if kind == "app-stack":
        try:
            sid = int(params["stack_id"])
        except (KeyError, TypeError, ValueError):
            return "stack_not_allowed"
        stack = (
            db.query(models.PersistentStack)
            .filter(models.PersistentStack.id == sid)
            .first()
        )
        if (stack is None or getattr(stack, "status", None) == "torn_down"
                or stack.tenant_id != app.tenant_id):
            return "stack_not_allowed"
        if getattr(app, "target_kind", None) != "stack-service" \
                or app.stack_id != sid:
            return "stack_not_allowed"
        if params.get("service") != app.stack_service:
            return "stack_service_not_allowed"
        return None
    return None


def check_scope_for_app(db, app, rule_kind: str, path_params: dict,
                        request=None) -> None:
    """Enforce one rule kind against an App (no api-key row needed).

    Used by the stack-identity path. Also pins the target workflow to the
    App's tenant. Raises HTTPException(403, "app_key_scope_denied"); denials
    are audited via the same _deny as the app-key path (AUTH_APP_SCOPE_DENIED).
    """
    from types import SimpleNamespace
    shim = SimpleNamespace(id=getattr(app, "api_key_id", None),
                           app_id=getattr(app, "id", None), user_id=None)
    tenant_id = getattr(app, "tenant_id", None)
    if app is None or getattr(app, "deleted_at", None) is not None \
            or getattr(app, "status", None) != "active":
        _deny(db, request, shim, None, "app_missing_or_inactive", tenant_id)
    reason = _scope_violation(db, app, rule_kind, path_params or {},
                              getattr(app, "id", None), tenant_id)
    if reason is not None:
        _deny(db, request, shim, None, reason, tenant_id)


def check_app_scope(db, request, api_key) -> None:
    """Enforce the app-key scope for one authenticated request.

    No-op when ``api_key`` is None or has no ``app_id`` (non-app key /
    Bearer session). Raises HTTPException(403, "app_key_scope_denied") on
    any violation.
    """
    if api_key is None or getattr(api_key, "app_id", None) is None:
        return

    import models

    app = db.query(models.App).filter(models.App.id == api_key.app_id).first()
    if app is None or app.deleted_at is not None or app.status != "active":
        _deny(db, request, api_key, None, "app_missing_or_inactive")
        return  # unreachable — _deny raises

    tenant_id = getattr(app, "tenant_id", None)

    path = _request_path(request)
    if path.endswith("/"):
        path = path.rstrip("/") or "/"
    method = _request_method(request)

    match = None
    for rule in _RULES:
        if rule.method != method:
            continue
        m = rule.regex.match(path)
        if m:
            match = (rule, m.groupdict())
            break
    if match is None:
        _deny(db, request, api_key, path, "route_not_allowed", tenant_id)
        return  # unreachable — _deny raises

    rule, params = match
    reason = _scope_violation(db, app, rule.resource_kind, params, api_key.app_id)
    if reason is not None:
        _deny(db, request, api_key, rule.template, reason, tenant_id)


def is_stack_pinned_key(api_key) -> bool:
    """True when the key row is a stack-pinned key (formerly: a deploy token).

    The pin is the ROW COLUMN (``deploy_token_stack_id``) — never a scope
    string: a scope names WHAT the key may do, it cannot name WHICH stack.
    """
    return api_key is not None and getattr(
        api_key, "deploy_token_stack_id", None) is not None


def stack_scopes_of(api_key) -> list:
    """Bare recognised stack-scope names of a key's ``capability_scope``.

    An entry counts ONLY when it is an exact member of
    ``{"stack:" + n for n in STACK_KEY_SCOPES}`` — other ``stack:<x>``
    entries (the platform capability ``stack:author``) are ORDINARY scopes
    and are not stack-key scopes. Fail closed: a non-list (JSONB null, a
    dict, a bare string) yields ``[]``; non-str entries are skipped.
    """
    raw = getattr(api_key, "capability_scope", None)
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, str) and entry in _STACK_SCOPE_ENTRY_NAMES:
            out.append(entry[len(STACK_KEY_SCOPE_PREFIX):])
    return out


def _deploy_token_scopes(api_key) -> list:
    """Back-compat alias of :func:`stack_scopes_of` (old tests/callers)."""
    return stack_scopes_of(api_key)


class StackKeyRequestError(ValueError):
    """Request-shape error for a stack-pinned-key capability_scope (pure).

    ``.code`` is a stable machine-readable reason; ``.message`` is prose.
    """
    code: str = ""
    message: str = ""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(message or code)


def validate_stack_key_request(scopes, stack_id):
    """Pure (no DB) validation of a request-side ``capability_scope`` list.

    ``scopes`` — the requested capability_scope list (or None = not given);
    ``stack_id`` — the stack the request names (int) or None.

    Returns the normalised list (unchanged order) when the shape is sound, or
    None when no scopes were given. Raises :exc:`StackKeyRequestError` with
    ``.code``:

      invalid_stack_id            stack_id not a positive int (bool rejected)
      unknown_stack_scope         an entry that only case-insensitively
                                  matches a known stack scope ("Stack:Deploy"),
                                  or a non-str entry in a stack-scope shape
      duplicate_scope             the same entry twice
      stack_scope_mixed           a stack scope together with any non-stack
                                  scope (a pinned key carries ONLY stack:*)
      stack_scope_requires_stack_id  a stack scope with no stack_id
      stack_id_requires_stack_scope  a stack_id with no stack scope
    """
    if stack_id is not None:
        if isinstance(stack_id, bool) or not isinstance(stack_id, int) \
                or stack_id <= 0:
            raise StackKeyRequestError(
                "invalid_stack_id",
                "stack_id must be a positive integer")
    if scopes is None:
        return None
    if not isinstance(scopes, list):
        raise StackKeyRequestError(
            "invalid_stack_scope_list",
            "capability_scope must be a list of scope strings")

    seen = set()
    stack_entries = []
    other_entries = 0
    for entry in scopes:
        if not isinstance(entry, str):
            raise StackKeyRequestError(
                "unknown_stack_scope",
                "capability_scope entries must be strings")
        if entry in seen:
            raise StackKeyRequestError(
                "duplicate_scope",
                f"duplicate scope entry: {entry}")
        seen.add(entry)
        if entry in _STACK_SCOPE_ENTRY_NAMES:
            stack_entries.append(entry)
        elif entry.lower() in _STACK_SCOPE_ENTRY_NAMES:
            raise StackKeyRequestError(
                "unknown_stack_scope",
                f"stack scope must be exact lowercase: {entry}")
        else:
            other_entries += 1

    if stack_entries and other_entries:
        raise StackKeyRequestError(
            "stack_scope_mixed",
            "a stack-pinned key carries only stack:* scopes")
    if stack_entries and stack_id is None:
        raise StackKeyRequestError(
            "stack_scope_requires_stack_id",
            "a stack:* scope requires the stack it pins")
    if stack_id is not None and not stack_entries:
        raise StackKeyRequestError(
            "stack_id_requires_stack_scope",
            "a stack_id requires a stack:* scope")
    return list(scopes)


def check_stack_key_scope(db, request, api_key) -> None:
    """Enforce the stack-pinned-key (formerly deploy token) route scope.

    No-op for an ordinary key (``deploy_token_stack_id`` NULL and no stack
    scope). A NON-pinned key that carries an exact known stack scope is
    DENIED (``stack_scope_without_pin`` — the stack:* => pin cross-check).
    Raises HTTPException(403, "deploy_token_scope_denied") on every violation:
    no request context, a key that is both app-bound and stack-pinned, no
    recognised scope, an unmatched route, a route whose ``{stack_id}`` is not
    the key's own stack.
    """
    if api_key is None:
        return
    pinned = getattr(api_key, "deploy_token_stack_id", None) is not None
    if not pinned:
        # cross-check: a stack scope without a pin is a forged shape — deny.
        if stack_scopes_of(api_key):
            _deny(db, request, api_key, None, "stack_scope_without_pin",
                  detail="deploy_token_scope_denied",
                  extra=_deploy_token_extra(api_key))
        return

    if request is None:
        _deny(db, request, api_key, None, "no_request_context",
              detail="deploy_token_scope_denied",
              extra=_deploy_token_extra(api_key))
        return  # unreachable — _deny raises

    if getattr(api_key, "app_id", None) is not None:
        _deny(db, request, api_key, None, "deploy_token_with_app",
              detail="deploy_token_scope_denied",
              extra=_deploy_token_extra(api_key))
        return  # unreachable — _deny raises

    scopes = stack_scopes_of(api_key)
    if not scopes:
        _deny(db, request, api_key, None, "no_scopes",
              detail="deploy_token_scope_denied",
              extra=_deploy_token_extra(api_key))
        return  # unreachable — _deny raises

    path = _request_path(request)
    if path.endswith("/"):
        path = path.rstrip("/") or "/"
    method = _request_method(request)

    rules = list(_DEPLOY_TOKEN_IMPLICIT)
    for scope in scopes:
        rules.extend(_DEPLOY_TOKEN_RULES_BY_SCOPE[scope])

    match = None
    for rule in rules:
        if rule.method != method:
            continue
        m = rule.regex.match(path)
        if m:
            match = (rule, m.groupdict())
            break
    if match is None:
        _deny(db, request, api_key, path, "route_not_allowed",
              detail="deploy_token_scope_denied",
              extra=_deploy_token_extra(api_key))
        return  # unreachable — _deny raises

    rule, params = match
    reason = _scope_violation(db, None, rule.resource_kind, params, None,
                              None, pinned_stack_id=
                              api_key.deploy_token_stack_id)
    if reason is not None:
        _deny(db, request, api_key, rule.template, reason,
              detail="deploy_token_scope_denied",
              extra=_deploy_token_extra(api_key))
    return  # unreachable when denied — _deny raises


def _deploy_token_extra(api_key) -> dict:
    """Audit extras for a stack-key denial: ids only, never key material (S6)."""
    return {"deploy_token_stack_id": getattr(
        api_key, "deploy_token_stack_id", None)}


# back-compat alias (old tests/callers stay green) — the SAME function object.
check_deploy_token_scope = check_stack_key_scope
