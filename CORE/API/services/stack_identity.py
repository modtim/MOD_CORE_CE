"""GX.S3 (launch-completion-megaplan Group X) — stack identity.

Every persistent stack gets an auto-injected, short-lived, auto-rotated
token that lets the stack's own containers call the MOD API as a scoped,
non-human principal:

  * The token is minted FRESH at every deploy (redeploy = rotation) with
    the RS256 machinery in utils/build_task_token (typ
    ``mod-stack-identity-v1``), scoped to ``(stack_id, tenant_id)`` and —
    when an App targets the stack — that ``app_id``.
  * It is delivered ONLY as a file secret (``mod_identity_token`` at
    ``/run/secrets/mod_identity_token``) via the GX.S10 file-secret
    pipeline. The token value never appears in the compose text or any
    env var; env carries only ``MOD_API_URL`` + ``MOD_IDENTITY_TOKEN_FILE``.
  * Auth (dependencies.require_bearer_or_api_key) accepts
    ``Authorization: Bearer <token>`` and resolves it to the stack's — or
    its targeting App's — service account, then enforces a default-deny
    route scope:
      - App targets the stack -> the app's service user + the app's
        existing allowlist (services/app_key_scope.check_app_scope).
      - No app -> the stack service user + the conservative stack
        allowlist (email send, llm chat, notifications POST, its OWN
        /v1/storage/scopes/stack/{id}/... surface, GET its own
        /v1/stacks/{id}).
  * Revoked / soft-deleted stack or service account -> 401.

Platform settings (services/platform_settings.py, group ``stacks``):
  stacks.identity_token_enabled        (bool, default True)
  stacks.identity_token_ttl_seconds    (int,  default 172800 = 48 h; rotated at 50%)

In-place rotation (30.6.8 c8-A2): ``push_rotated_identity`` re-mints and ships
the token to a RUNNING stack via the worker's ``refresh_secret_files`` op; the
hourly scheduler tick that drives it is ``run_stack_identity_rotation``.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from utils.build_task_token import mint_stack_identity_token

from product_names import themed_logger
logger = themed_logger(__name__, 'stacks')

IDENTITY_SECRET_NAME = "mod_identity_token"
IDENTITY_TARGET = "/run/secrets/mod_identity_token"

# 30.6.8 c8-A1 — the DIRECTORY-bind form (see the comment block above
# ``apply_stack_identity`` for why the gate exists).
IDENTITY_DIR_NAME = "identity"
IDENTITY_DIR_TARGET = "/run/mod-identity"
IDENTITY_DIR_FILE = f"{IDENTITY_DIR_TARGET}/{IDENTITY_SECRET_NAME}"
IDENTITY_DIR_VOLUME = "${MOD_SECRETS_DIR}/identity:/run/mod-identity:ro"
IDENTITY_ENV_FILE = "MOD_IDENTITY_TOKEN_FILE"
IDENTITY_ENV_API_URL = "MOD_API_URL"

SETTING_ENABLED = "stacks.identity_token_enabled"
SETTING_TTL = "stacks.identity_token_ttl_seconds"


# ---------------------------------------------------------------------------
# Stack service user (mirrors services/app_service_user — no new columns)
# ---------------------------------------------------------------------------

def stack_service_account_email(stack_id: int) -> str:
    """Deterministic, never-routable email for stack ``stack_id``."""
    return f"stack-{stack_id}@stacks.invalid"


def stack_service_account_username(stack_id: int) -> str:
    return f"stack-{stack_id}"


def _find_stack_service_user(db: Session, stack) -> Optional[models.User]:
    return (
        db.query(models.User)
        .filter(models.User.email == stack_service_account_email(stack.id))
        .first()
    )


def ensure_stack_service_user(db: Session, stack) -> models.User:
    """Idempotently create (or re-activate) the service user for ``stack``.

    Mirrors ``services.app_service_user.ensure_app_service_user`` (same
    shape: deterministic identity, no Keycloak identity, lowest-privilege
    tenant member, ``is_service_account=True``, soft-delete-only revocation).
    Returns the ``User`` row; a pre-existing row — live or previously
    soft-deleted — is reused, never duplicated (email is unique).
    """
    email = stack_service_account_email(stack.id)
    username = stack_service_account_username(stack.id)

    user = _find_stack_service_user(db, stack)
    if user is None:
        user = models.User(
            username=username,
            email=email,
            first_name="Stack",
            last_name=f"Service {stack.id}",
            tenant_id=stack.tenant_id,
            keycloak_user_id=None,  # non-human; cannot log in interactively
            parent_user_id=getattr(stack, "created_by_user_id", None),
            custom_role_id=None,
            status=models.UserStatus.ACTIVE,
            onboarding_complete=True,
            is_service_account=True,
            provisioning_source="stack",
            is_email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(
            "created stack service user: stack=%s user=%s tenant=%s",
            stack.id, user.id, stack.tenant_id,
        )
        return user

    changed = False
    if user.tenant_id != stack.tenant_id:
        user.tenant_id = stack.tenant_id
        changed = True
    if user.is_service_account is not True:
        user.is_service_account = True
        changed = True
    if user.status != models.UserStatus.ACTIVE:
        user.status = models.UserStatus.ACTIVE
        changed = True
    if user.deleted_at is not None:
        user.deleted_at = None
        changed = True
    if changed:
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _active_targeting_app(db: Session, stack) -> Optional[models.App]:
    """The first live, active App that targets this stack (None when no app
    points at it)."""
    return (
        db.query(models.App)
        .filter(models.App.stack_id == stack.id,
                models.App.tenant_id == stack.tenant_id,
                models.App.deleted_at.is_(None),
                models.App.status == "active")
        .order_by(models.App.id.asc())
        .first()
    )


def resolve_stack_principal(db: Session, stack) -> models.User:
    """The service user a stack identity token resolves to: the targeting
    App's service user when an active app exists, else the stack's own."""
    app = _active_targeting_app(db, stack)
    if app is not None:
        from services.app_service_user import ensure_app_service_user
        return ensure_app_service_user(db, app)
    return ensure_stack_service_user(db, stack)


# ---------------------------------------------------------------------------
# Deploy-time injection (the GX.S10 file-secret pipeline)
# ---------------------------------------------------------------------------

def internal_api_url() -> str:
    """The internal API URL a stack container should call (same convention
    as the node builder: API_INTERNAL_URL, default ``http://api:80``)."""
    return os.getenv("API_INTERNAL_URL", "http://api:80")


def _iso8601_z(dt: datetime) -> str:
    """UTC timestamp as strict ISO-8601 (``2026-10-27T12:00:00Z``).

    BUG-1027h: ``datetime.now(timezone.utc).isoformat() + "Z"`` yields
    ``...+00:00Z``, which is not a valid ISO-8601 timestamp. Strip the offset
    and append the Zulu designator instead."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def mint_identity_secret(db: Session, stack, *, name: str = IDENTITY_SECRET_NAME) -> dict:
    """Mint the stack's identity token and shape it as a payload-ready file
    secret (base64 content, 0400). The service user is ensured FIRST so the
    token always resolves at auth time. The token value is returned only in
    ``content_b64`` — callers must not log it.

    BUG-1027f: records the minted claim in ``stack.storage_refs["identity"]``
    so that post-mint re-targets of the same app can be detected (S5: no auth
    widening — only an extra rejection).

    BUG-1027h / c13-J1R: ``minted_at`` must be true ISO-8601. ``isoformat()``
    of a tz-aware datetime already carries ``+00:00``, so the old
    ``isoformat() + "Z"`` rendered ``...+00:00Z`` — not a valid timestamp for
    any strict parser. Same fix here, plus ``expires_at`` (now + ttl) so the
    expiry is auditable without re-decoding the token. One ``now`` feeds both
    so the pair is always exactly ``ttl`` apart. The token itself is NEVER
    stored."""
    from services.platform_settings import get_setting
    ttl = int(get_setting(SETTING_TTL, db=db))
    app = _active_targeting_app(db, stack)
    if app is not None:
        resolve_stack_principal(db, stack)  # ensures the app's service user
        app_id = app.id
    else:
        ensure_stack_service_user(db, stack)
        app_id = None
    now = datetime.now(timezone.utc)
    token = mint_stack_identity_token(
        stack_id=stack.id, tenant_id=stack.tenant_id, app_id=app_id, ttl_s=ttl,
    )
    if hasattr(stack, "storage_refs"):
        refs = dict(stack.storage_refs or {})
        refs["identity"] = {"app_id": app.id if app is not None else None,
                            "minted_at": _iso8601_z(now),
                            "expires_at": _iso8601_z(now + timedelta(seconds=ttl))}
        stack.storage_refs = refs  # reassign (S3: no token bytes persisted)
    return {
        "name": name,
        "content_b64": base64.b64encode(token.encode("utf-8")).decode("ascii"),
        "mode": 0o400,
    }


def _add_env_to_all_services(spec: dict, envmap: dict) -> None:
    """Add env entries to EVERY service (list- and dict-shaped env)."""
    services = (spec or {}).get("services") or {}
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        if isinstance(env, list):
            env.extend(f"{k}={v}" for k, v in envmap.items())
        else:
            if not isinstance(env, dict):
                env = {}
                svc["environment"] = env
            env.update(envmap)


def _add_volume_to_all_services(spec: dict, vol: str) -> None:
    """Append a volume string to EVERY service (30.6.8 c8-A1 dir-bind form).

    Same shape-tolerance as ``_add_env_to_all_services``: missing list ->
    created; non-dict service skipped; a scalar ``volumes`` becomes a one-item
    list so nothing is silently dropped; already-present volume not duplicated
    (so a double apply stays idempotent)."""
    services = (spec or {}).get("services") or {}
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes")
        if isinstance(vols, list):
            if vol not in vols:
                vols.append(vol)
        else:
            if not isinstance(vols, list):
                vols = [vols] if vols is not None else []
                svc["volumes"] = vols
            if vol not in vols:
                vols.append(vol)


def _worker_supports_secret_refresh(worker) -> bool:
    """True only when the worker explicitly advertises the ``secret_refresh``
    capability (30.6.8 c8-A1 gate).

    Strict ``is True``: a string ("true") or a truthy int (1) does NOT count —
    the dir-bind payload changes the compose shape, so only a worker that
    provably understands it (c8-W2 heartbeat stamp) may receive it. Never
    raises: a worker row without ``hardware_specs`` (or with a non-dict one)
    simply does not qualify."""
    specs = getattr(worker, "hardware_specs", None)
    return isinstance(specs, dict) and specs.get("secret_refresh") is True


def apply_stack_identity(db: Optional[Session], stack, bundle: dict, spec: dict,
                         worker=None) -> bool:
    """Append the stack identity file secret to a resolved variables bundle
    and the companion env vars to every service in the worker-copy spec.

    Called from ``_compose_yaml_with_vars`` right after ``resolve_variables``
    and BEFORE ``render_worker_spec`` (so the mount materialises with the
    rest of the bundle's mounts). Returns True when injected.

    Skips (quietly, logging the stack id only) when:
      * the platform setting ``stacks.identity_token_enabled`` is False;
      * the named target worker cannot materialise file secrets (the GX.S10
        gate — a ``None``/unknown target is allowed through, matching
        ``_worker_supports_file_secrets``'s claim-time semantics).

    The token itself is NEVER put in env or the compose text — env carries
    only MOD_API_URL and MOD_IDENTITY_TOKEN_FILE (the path).
    """
    if db is None or stack is None:
        return False
    from services.platform_settings import get_setting
    try:
        enabled = bool(get_setting(SETTING_ENABLED, db=db))
    except Exception:  # settings registry read must not break a deploy
        logger.warning("stack identity: setting %s unreadable; skipping (stack=%s)",
                       SETTING_ENABLED, getattr(stack, "id", "?"))
        return False
    if not enabled:
        return False

    # Auto-injected, not user-requested: it must never break a deploy. An
    # unknown target (claim-time placement, reconciler redeploys) can't be
    # proven to materialise file secrets, so skip rather than ship a mount
    # an older worker turns into a bind of "/mod_identity_token".
    if worker is None:
        logger.info("stack identity: target worker unknown; skipping (stack=%s)",
                    stack.id)
        return False
    from routers.persistent_stacks._shared import _worker_supports_file_secrets
    if not _worker_supports_file_secrets(worker):
        logger.info("stack identity: worker lacks file secrets; skipping (stack=%s)",
                    stack.id)
        return False

    # ---------------------------------------------------------------------
    # 30.6.8 c8-A1 — two delivery forms, gated on the worker's capability.
    #
    # Why the gate exists (c8-design "Lead decision after c8-W0", Addendum 2):
    # the identity token used to ride the GX.S10 file-secret pipeline as ONE
    # flat name (``mod_identity_token`` -> ``${MOD_SECRETS_DIR}/mod_identity_token``).
    # c8-W1R taught the worker to keep a secret's ``dir/`` name prefix and to
    # hold the ``dir/identity`` inode across redeploys, which lets the API ship
    # the token as a DIRECTORY bind (``${MOD_SECRETS_DIR}/identity`` ->
    # ``/run/mod-identity``, read-only) instead of a compose ``secrets:`` stanza.
    # An OLD worker (``file_secrets`` yes, ``secret_refresh`` absent) still
    # applies the pre-W1 rule that REJECTS a ``/`` in a secret name, so a dir-
    # bind payload would fail its deploy. Old workers therefore keep today's
    # single-file form byte-for-byte (S1); only a worker that stamps
    # ``hardware_specs["secret_refresh"] is True`` (c8-W2/W2a heartbeat) gets
    # the new form, and the gate is strict ``is True`` (S2).
    #
    # S4: the ONLY thing ever named ``identity/...`` here is the token itself,
    # so the identity dir holds exactly one file. S5: the only host path this
    # code introduces is ``${MOD_SECRETS_DIR}/identity``, mounted read-only.
    # ---------------------------------------------------------------------
    if _worker_supports_secret_refresh(worker):
        bundle.setdefault("secret_files", []).append(
            mint_identity_secret(db, stack,
                                 name=f"{IDENTITY_DIR_NAME}/{IDENTITY_SECRET_NAME}"))
        # NO bundle["mounts"] entry on purpose: render_worker_spec would emit a
        # top-level ``secrets:`` stanza, which the dir-bind worker does not want.
        _add_volume_to_all_services(spec, IDENTITY_DIR_VOLUME)
        _add_env_to_all_services(spec, {
            IDENTITY_ENV_API_URL: internal_api_url(),
            IDENTITY_ENV_FILE: IDENTITY_DIR_FILE,
        })
        logger.info("stack identity: dir-bind form (stack=%s)", stack.id)
        return True

    bundle.setdefault("secret_files", []).append(mint_identity_secret(db, stack))
    bundle.setdefault("mounts", {})[IDENTITY_SECRET_NAME] = {
        "target": IDENTITY_TARGET,
        "mode": 0o400,
        "dir_env": "MOD_SECRETS_DIR",
        "services": None,  # every service
    }
    _add_env_to_all_services(spec, {
        IDENTITY_ENV_API_URL: internal_api_url(),
        IDENTITY_ENV_FILE: IDENTITY_TARGET,
    })
    logger.info("stack identity: legacy single-file form (stack=%s)", stack.id)
    return True


# ---------------------------------------------------------------------------
# Auth-side scope enforcement (called from dependencies.py)
# ---------------------------------------------------------------------------

from services.app_key_scope import (  # noqa: E402 — reuse, not reimplement
    STACK_CALL_RULES, _compile_rule, _request_method, _request_path,
)

# Conservative stack allowlist (default deny). ``{stack_id}`` in every entry
# must equal the token's own stack id (own-stack pin).
_STACK_ALLOWLIST: tuple = (
    ("POST", "/v1/email/send", "none"),
    ("POST", "/v1/llm/chat/completions", "none"),
    ("POST", "/v1/notifications", "none"),
    ("POST", "/v1/storage/scopes/stack/{stack_id}/links", "own-stack"),
    ("GET", "/v1/storage/scopes/stack/{stack_id}/objects", "own-stack"),
    ("DELETE", "/v1/storage/scopes/stack/{stack_id}/objects/{object_key:path}", "own-stack"),
    ("GET", "/v1/stacks/{stack_id}", "own-stack"),
    # GX n2 — workflow start/read, scoped by the stack's bound App
    # (allowed_workflow_ids). Paths mirror app_key_scope.APP_SCOPE_ALLOWLIST.
    ("POST", "/v1/road/workflow-versions/{version_id}/run", "version"),
    ("GET", "/v1/road/workflow-runs/{run_id}", "run"),
    ("GET", "/v1/road/workflow-runs/{run_id}/progress", "run"),
    ("GET", "/v1/road/workflow-runs/{run_id}/node-status", "run"),
    ("GET", "/v1/runs/{run_id}", "run"),
    ("GET", "/v1/runs/{run_id}/result", "run"),
) + STACK_CALL_RULES

_APP_SCOPED_KINDS = frozenset({"version", "run", "app-stack"})

_STACK_RULES: tuple = tuple(
    _compile_rule(m, t, k) for (m, t, k) in _STACK_ALLOWLIST
)


# Allow-list: statuses in which a stack's own containers may legitimately call
# workflow routes (startup hooks run while "deploying"). created/stopped/failed/
# torn_down (and any unknown future status) are denied.
_WORKFLOW_SCOPE_STACK_STATUSES = frozenset({"running", "deploying", "migrating"})


def _stack_live(db: Session, stack_id: int, tenant_id: int) -> Optional[models.PersistentStack]:
    """The stack row iff it exists in this tenant and is not revoked.

    PersistentStack has no ``deleted_at``; the revoked/soft-deleted state is
    the ``torn_down`` status (containers + volumes gone, identity void)."""
    stack = (
        db.query(models.PersistentStack)
        .filter(models.PersistentStack.id == stack_id,
                models.PersistentStack.tenant_id == tenant_id)
        .first()
    )
    if stack is None:
        return None
    if getattr(stack, "status", None) == "torn_down":
        return None
    return stack


def check_stack_scope(db: Session, request, *, stack_id: int, app_id: Optional[int],
                      api_key=None) -> None:
    """Default-deny route scope for a stack identity token.

    * app_id set (an App targets the stack) -> enforce the APP's allowlist
      via services.app_key_scope.check_app_scope against the app's real
      api_keys row (the app is verified live/active inside it).
    * otherwise -> the conservative stack allowlist, with ``{stack_id}``
      pinned to the token's own stack.

    Raises HTTPException(403, "stack_scope_denied" / "app_key_scope_denied").
    """
    if app_id is not None:
        from services.app_key_scope import check_app_scope
        if api_key is None:
            app = db.query(models.App).filter(models.App.id == app_id).first()
            if app is not None and app.api_key_id is not None:
                api_key = (db.query(models.ApiKey)
                           .filter(models.ApiKey.id == app.api_key_id).first())
        if api_key is not None:
            check_app_scope(db, request, api_key)
            return
        # App row / api key gone -> the identity no longer resolves; deny.
        raise HTTPException(status_code=403, detail="stack_scope_denied")

    path = _request_path(request)
    if path.endswith("/"):
        path = path.rstrip("/") or "/"
    method = _request_method(request)
    for rule in _STACK_RULES:
        if rule.method != method:
            continue
        m = rule.regex.match(path)
        if not m:
            continue
        if rule.resource_kind == "own-stack":
            try:
                if int(m.groupdict()["stack_id"]) != int(stack_id):
                    continue  # foreign stack id -> keep scanning, will deny
            except (KeyError, TypeError, ValueError):
                continue
        if rule.resource_kind in _APP_SCOPED_KINDS:
            # Workflow routes: need a live App bound to this stack, and the
            # app's allowed_workflow_ids must cover the target.
            stack = (db.query(models.PersistentStack)
                     .filter(models.PersistentStack.id == int(stack_id)).first())
            app = _active_targeting_app(db, stack) if stack is not None else None
            if app is not None and (
                    stack.status not in _WORKFLOW_SCOPE_STACK_STATUSES
                    or db.query(models.App.id).filter(
                        models.App.stack_id == stack.id,
                        models.App.tenant_id == stack.tenant_id,
                        models.App.deleted_at.is_(None),
                        models.App.status == "active",
                        models.App.id != app.id).first() is not None):
                app = None  # stopped stack, or ambiguous (>1 active app): deny
            if app is None:
                logger.info("stack scope denied (no bound app): stack=%s route=%s %s",
                            stack_id, method, path)
                raise HTTPException(status_code=403, detail="stack_scope_denied")
            from services.app_key_scope import check_scope_for_app
            check_scope_for_app(db, app, rule.resource_kind, m.groupdict(), request)
        # allowed
        return
    logger.info("stack scope denied: stack=%s route=%s %s", stack_id, method, path)
    raise HTTPException(status_code=403, detail="stack_scope_denied")


def resolve_identity_claims(db: Session, request, claims: dict) -> models.User:
    """dependencies.py hook: turn verified stack-identity claims into the
    resolved service user (or 401/403).

    401 when: the stack is missing/soft-deleted in its tenant, or the
    resolved service account is soft-deleted/inactive.
    """
    tenant_id = int(claims["tenant_id"])
    stack_id = int(claims["stack_id"])
    app_id = claims.get("app_id")

    stack = _stack_live(db, stack_id, tenant_id)
    if stack is None:
        logger.info("stack identity rejected: stack not live (stack=%s tenant=%s)",
                    stack_id, tenant_id)
        raise HTTPException(status_code=401, detail="Stack identity not valid.")

    if app_id is not None:
        app = (db.query(models.App)
               .filter(models.App.id == int(app_id),
                       models.App.tenant_id == tenant_id,
                       models.App.deleted_at.is_(None))
               .first())
        if app is None or app.status != "active":
            logger.info("stack identity rejected: targeting app not live (app=%s)",
                        app_id)
            raise HTTPException(status_code=401, detail="Stack identity not valid.")
        # BUG-1027f: stack binding check — the app must still target this stack
        if app.target_kind != "stack-service" or app.stack_id != stack_id:
            logger.info("stack identity rejected: app no longer targets stack (app=%s stack=%s)",
                        app.id, stack_id)
            raise HTTPException(status_code=401, detail="Stack identity not valid.")
        from services.app_service_user import service_account_email
        user = (db.query(models.User)
                .filter(models.User.email == service_account_email(app.id))
                .first())
        if user is None or user.deleted_at is not None or \
                user.status != models.UserStatus.ACTIVE:
            logger.info("stack identity rejected: app service user not live (app=%s)",
                        app_id)
            raise HTTPException(status_code=401, detail="Stack identity not valid.")
        # scope = the app's allowlist, against the app's real key row
        from services.app_key_scope import check_app_scope
        api_key = None
        if app.api_key_id is not None:
            api_key = (db.query(models.ApiKey)
                       .filter(models.ApiKey.id == app.api_key_id).first())
        check_app_scope(db, request, api_key)
        return user

    user = (db.query(models.User)
            .filter(models.User.email == stack_service_account_email(stack_id))
            .first())
    if user is None or user.deleted_at is not None or \
            user.status != models.UserStatus.ACTIVE:
        logger.info("stack identity rejected: stack service user not live (stack=%s)",
                    stack_id)
        raise HTTPException(status_code=401, detail="Stack identity not valid.")
    check_stack_scope(db, request, stack_id=stack_id, app_id=None)
    return user


def identity_state(db: Session, stack) -> dict:
    """Read-only view of the stack's identity claim record and the live targeting
    app. Returns ``{"app_id": <recorded or None>, "minted_at": <ISO-8601 Z or None>,
    "current_app_id": <_active_targeting_app(db, stack).id or None>}``.
    Does NOT write anything (no session mutation — caller may inspect before a tx)."""
    recorded = stack.storage_refs.get("identity") if stack.storage_refs else None
    app = _active_targeting_app(db, stack)
    return {
        "app_id": recorded.get("app_id") if recorded else None,
        "minted_at": recorded.get("minted_at") if recorded else None,
        "current_app_id": app.id if app is not None else None,
    }


# ---------------------------------------------------------------------------
# 30.6.8 c8-A2 — in-place rotation of a RUNNING stack's identity token.
# ---------------------------------------------------------------------------
from sqlalchemy import text as _satext  # noqa: E402 — advisory-lock SELECT

_ROTATION_LOCK_NS = "stack_identity_rotation"
_ROTATION_LOCK_SQL = _satext(
    "SELECT pg_try_advisory_xact_lock(hashtext(:ns), :stack_id)")
SETTING_ROTATION_ENABLED = "stacks.identity_rotation_enabled"


def _stack_dir_bind_form(db, stack) -> bool:
    """True when the stack's LAST dispatched compose YAML already carries the
    dir-bind volume (``IDENTITY_DIR_VOLUME``).

    A1 records no marker in ``storage_refs``, so the honest source is the
    rendered spec the last lifecycle op shipped: ``_dispatch_stack_task`` puts
    the YAML it hands the worker on the task payload as ``compose_spec``
    (``routers/persistent_stacks/_shared.py:2016``), and the completion handler
    reads the same key back (``routers/worker_tasks.py:1207``). The stored
    ``stack.compose_spec`` never carries the volume — ``apply_stack_identity``
    mutates only the deepcopy handed to the worker — so a stack that has never
    been deployed in dir-bind form is LEGACY here, and rotation refuses (no
    mint, no dispatch): a legacy file-bind redeploy does not recreate the
    containers (BUG-1027h), so pushing would be a lie."""
    row = db.execute(
        _satext(
            "SELECT task_payload->>'compose_spec' FROM api_task_queue "
            "WHERE (task_payload->>'stack_id')::bigint = :sid "
            "  AND task_payload->>'node_kind' LIKE 'persistent-stack-%%' "
            "  AND task_payload->>'compose_spec' IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"sid": stack.id},
    ).first()
    return bool(row) and IDENTITY_DIR_VOLUME in (row[0] or "")


def push_rotated_identity(db, stack, *, now=None) -> dict:
    """Re-mint the identity token and ship it to the RUNNING stack via the
    worker's ``refresh_secret_files`` op (c8-W1). Returns
    ``{"status": "pushed"|"redeploy_required"|"skipped", "token_expires_at",
    "reason"}`` and NEVER raises.

    V5: a per-stack advisory lock (``pg_try_advisory_xact_lock`` keyed
    ``hashtext(ns), stack.id``) — a concurrent rotation answers False and
    reports ``skipped/in_progress``. V8: the OLD token is never revoked; it
    stays valid until its own exp. S2: the payload carries ONLY the identity
    file (``secret_files`` — the Go key at
    ``CORE/Worker/internal/transport/transport.go:414``), never the stack's
    other secrets. S3: the audit row (``AuditAction.KEY_ROTATED``, the shape
    ``services/enroll_token_rotation.py:write_rotation_audits`` uses) carries
    metadata only — no token bytes. S5: the mint + the dispatch share one
    SAVEPOINT, so a dispatch failure unwinds the re-mint (``storage_refs``
    keeps the OLD expiry) and writes one Notification.
    """
    from routers.persistent_stacks._shared import _dispatch_stack_task, _project_name
    from crud import persistent_stacks_domain as _psd

    def _result(status, reason, expires_at=None):
        return {"status": status, "token_expires_at": expires_at, "reason": reason}

    if db is None or stack is None:
        return _result("skipped", "no_stack")
    try:
        got = db.execute(_ROTATION_LOCK_SQL,
                         {"ns": _ROTATION_LOCK_NS, "stack_id": stack.id}).scalar()
    except Exception as exc:  # noqa: BLE001 — lock read must not break the tick
        logger.warning("stack identity: rotation lock read failed (stack=%s): %s",
                       stack.id, exc)
        return _result("skipped", "lock_unreadable")
    if not got:
        return _result("skipped", "in_progress")

    if getattr(stack, "status", None) != "running":
        return _result("redeploy_required", "not_running")
    # The bound worker is resolved from ``stack.target_worker`` — the same field
    # the deploy call site uses (persistent_stacks_domain_service.py:412
    # ``target = psd_crud.get_licensed_worker_by_name(db, to_worker)``).
    worker = _psd.get_licensed_worker_by_name(db, stack.target_worker) \
        if stack.target_worker else None
    if worker is None:
        return _result("redeploy_required", "no_bound_worker")
    if not _worker_supports_secret_refresh(worker):
        return _result("redeploy_required", "worker_lacks_secret_refresh")
    if not _stack_dir_bind_form(db, stack):
        return _result("redeploy_required", "legacy_file_form")

    # S5: the mint AND the dispatch live in one SAVEPOINT, so a dispatch failure
    # unwinds the re-mint too — ``storage_refs`` never claims an expiry the
    # container did not get — while whatever the caller staged before stays.
    try:
        with db.begin_nested():
            entry = mint_identity_secret(db, stack,
                                         name=f"{IDENTITY_DIR_NAME}/{IDENTITY_SECRET_NAME}")
            expires_at = (stack.storage_refs or {}).get("identity", {}).get("expires_at")
            _dispatch_stack_task(db, stack.target_worker, stack.tenant_id,
                                 "refresh_secret_files", _project_name(stack),
                                 stack_id=stack.id,
                                 extra_payload={"secret_files": [entry]})
    except Exception as exc:  # noqa: BLE001 — S5: no state change + Notification
        logger.warning("stack identity: refresh dispatch failed (stack=%s): %s",
                       stack.id, exc)
        _notify_rotation_failure(db, stack)
        return _result("redeploy_required", "dispatch_failed")

    import audit
    try:
        audit.log_security_event(
            db,
            action=models.AuditAction.KEY_ROTATED,
            tenant_id=stack.tenant_id,
            metadata={"kind": "stack_identity", "stack_id": stack.id,
                      "expires_at": expires_at, "source": "stack_identity"},
        )
    except Exception:  # noqa: BLE001 — audit must not break the push
        logger.warning("stack identity: audit write failed (stack=%s)", stack.id)
    return _result("pushed", None, expires_at)


def _notify_rotation_failure(db, stack, *, dedupe_key=None) -> int:
    """One tenant-scoped Notification for a rotation that could not push.

    Recipient is the stack's own service user (same tenant as the stack — S1);
    dedup is the LIVE-row-by-``dedupe_key`` query copied from
    ``services/app_notifications.py:_dedupe_exists`` (:116), so the hourly tick
    never spams. Never raises."""
    try:
        user = _find_stack_service_user(db, stack)
        if user is None and stack.created_by_user_id:
            user = (db.query(models.User)
                    .filter(models.User.id == stack.created_by_user_id,
                            models.User.tenant_id == stack.tenant_id).first())
        if user is None or user.tenant_id != stack.tenant_id:
            return 0
        key = dedupe_key or f"stack-identity-rotation:{stack.id}"
        dup = db.execute(
            _satext(
                "SELECT 1 FROM notifications n JOIN users u ON u.id = n.recipient_user_id "
                "WHERE n.dedupe_key = :k AND u.tenant_id = :t AND n.deleted_at IS NULL "
                "LIMIT 1"
            ),
            {"k": key, "t": stack.tenant_id},
        ).first()
        if dup is not None:
            return 0
        db.add(models.Notification(
            recipient_user_id=user.id,
            message=(f"Stack {stack.name}: identity token rotation could not push "
                     f"(reason: dispatch_failed); redeploy required"),
            is_read=False,
            notification_type="stack_identity_redeploy",
            severity="warning",
            dedupe_key=key,
        ))
        db.commit()
        return 1
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("stack identity: failure notification failed (stack=%s): %s",
                       stack.id, exc)
        return 0


def run_stack_identity_rotation(db=None, *, now=None, dry_run=False) -> dict:
    """The hourly tick body: rotate every RUNNING stack whose identity token
    has passed 50% of its TTL (c8-design F5), and alert (never auto-redeploy —
    S7, BUG-1027h) the ones that cannot push once under 25% remains.

    Inert when ``stacks.identity_rotation_enabled`` is false. ttl comes from
    the record (``expires_at - minted_at``) and falls back to
    ``stacks.identity_token_ttl_seconds``. Returns
    ``{checked, pushed, redeploy_required, skipped, notified}``. Never raises
    (S6); idempotent hourly (the lock + the deduped Notification)."""
    from services.platform_settings import get_setting
    counts = {"checked": 0, "pushed": 0, "redeploy_required": 0,
              "skipped": 0, "notified": 0}
    if db is None:
        return counts
    try:
        if not bool(get_setting(SETTING_ROTATION_ENABLED, db=db)):
            return counts
    except Exception:  # noqa: BLE001 — settings read must not break the tick
        logger.warning("stack identity: rotation setting unreadable; skipping")
        return counts
    now = _as_utc_local(now) or datetime.now(timezone.utc)
    ttl_setting = int(get_setting(SETTING_TTL, db=db))
    stacks = (db.query(models.PersistentStack)
              .filter(models.PersistentStack.status == "running").all())
    for stack in stacks:
        rec = (stack.storage_refs or {}).get("identity")
        if not rec or not rec.get("expires_at"):
            continue
        counts["checked"] += 1
        exp = _parse_iso(rec["expires_at"])
        if exp is None:
            continue
        mint = _parse_iso(rec.get("minted_at")) if rec.get("minted_at") else None
        ttl = int((exp - mint).total_seconds()) if mint and exp > mint else ttl_setting
        remaining = (exp - now).total_seconds()
        if remaining > ttl / 2:
            continue
        if dry_run:
            counts["pushed"] += 1
            continue
        result = push_rotated_identity(db, stack, now=now)
        if result["status"] == "pushed":
            counts["pushed"] += 1
        elif result["status"] == "skipped":
            counts["skipped"] += 1
        else:
            counts["redeploy_required"] += 1
            if remaining < ttl / 4:
                counts["notified"] += _notify_rotation_failure(
                    db, stack,
                    dedupe_key=f"stack-identity-redeploy:{stack.id}:{rec['expires_at']}")
    return counts


def _as_utc_local(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _parse_iso(value):
    """Strict-ish ISO-8601 Z parser (the record's own format, c13-J1R)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
