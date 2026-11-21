"""13.5c — secret-looking inline compose values -> MOD tenant secrets.

The compose import front door (preview/finalize) and the Copy… flow (the
dashboard pulls ``GET /stacks/{id}/definition`` — whose secret-looking env
values come back as ``***redacted***`` — then re-imports the spec) let a user
land a secret-looking value inline in ``environment``. This module offers to
move those values into tenant secrets:

* :func:`detect_inline_secrets` lists ``{"service", "env", "secret"}`` for
  every inline value whose KEY matches the product's secret-look regex
  (``_SECRET_ENV_RE``). It NEVER includes the value itself — the caller reads
  the value straight from the spec.
* :func:`move_inline_secrets` creates/updates the tenant secrets through the
  SAME storage path as 13.5a (``crud.create_or_update_secret``: encrypted DB
  row + Vault write) and rewrites those env values to ``${secret:NAME}`` so
  plaintext never lands in ``compose_spec`` (13.5a's
  ``normalize_tenant_secret_refs`` then converts the whole value into an
  ``x-mod.secrets`` named ref, exactly like a hand-written
  ``${secret:NAME}`).
* :func:`redact_preview_spec` masks secret-looking inline VALUES in a preview
  copy (``_redact_compose_secrets``-style, but scoped to the inline values the
  detector flags) so a pasted secret does not echo back in the preview.
* :func:`rehydrate_clone` (the Copy… side) restores ``***redacted***``
  placeholders back to ``${secret:NAME}`` refs whenever the SOURCE stack
  already holds a named tenant-secret ref for that env; redacted values with
  no named ref (raw ``vault:`` refs, plaintext) are reported as unresolved so
  the caller keeps today's "replace before creating" behaviour.

Values are never logged and never appear in API responses.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

# Whole-value `${secret:NAME}` / any `${VAR}` interpolation forms.
_SECRET_REF_RE = re.compile(r"^\$\{secret:([A-Za-z0-9][A-Za-z0-9_.\-]*)\}$")
_ANY_INTERP_RE = re.compile(r"^\$\{[^{}]+\}$")


def _shared() -> Any:
    # Lazy: the router package imports this service at request time, so an
    # import-time cycle is impossible; the lazy form also keeps this module
    # importable without the router tree loaded (unit tests).
    from routers.persistent_stacks import _shared
    return _shared


# ---------------------------------------------------------------------------
# env iteration / access (dict and KEY=value list forms)
# ---------------------------------------------------------------------------

def _iter_env(env):
    if isinstance(env, dict):
        for k, v in env.items():
            yield str(k), v
    elif isinstance(env, list):
        for e in env:
            if isinstance(e, str) and "=" in e:
                k, v = e.split("=", 1)
                yield k, v


def _get_env_value(spec: Dict[str, Any], svc: str, env: str) -> Optional[str]:
    services = spec.get("services")
    if not isinstance(services, dict):
        return None
    s = services.get(svc)
    if not isinstance(s, dict):
        return None
    e = s.get("environment")
    if isinstance(e, dict):
        v = e.get(env)
        return v if isinstance(v, str) else None
    if isinstance(e, list):
        for item in e:
            if isinstance(item, str) and "=" in item and item.split("=", 1)[0] == env:
                return item.split("=", 1)[1]
    return None


def _set_env_value(spec: Dict[str, Any], svc: str, env: str, value: str) -> None:
    s = spec["services"][svc]
    e = s.get("environment")
    if isinstance(e, dict):
        e[env] = value
    elif isinstance(e, list):
        for i, item in enumerate(e):
            if isinstance(item, str) and "=" in item and item.split("=", 1)[0] == env:
                e[i] = f"{env}={value}"
                return
        e.append(f"{env}={value}")
    else:
        s["environment"] = {env: value}


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def detect_inline_secrets(spec: Any) -> List[Dict[str, str]]:
    """Secret-looking inline env values per service.

    Both env forms (mapping and ``KEY=value`` list) are scanned, using the
    product's secret-look KEY regex (``_SECRET_ENV_RE``). Values that are
    already a ``${secret:...}`` ref, any other whole ``${VAR}``
    interpolation, or the redaction sentinel are skipped (nothing to move).
    Each item is ``{"service", "env", "secret"}`` — a SUGGESTED secret name
    (``<ENV>_<SERVICE>`` when two services share a key, else ``<ENV>``) — and
    never carries the value.
    """
    re_mod = _shared()
    secret_re = re_mod._SECRET_ENV_RE
    sentinel = re_mod.REDACTED_SENTINEL
    services = (spec or {}).get("services") if isinstance(spec, dict) else None
    if not isinstance(services, dict):
        return []

    found: List[Dict[str, str]] = []
    env_key_services: Dict[str, List[str]] = {}
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for key, val in _iter_env(svc.get("environment")):
            if not secret_re.search(key):
                continue
            if not isinstance(val, str):
                continue
            if val == sentinel or _SECRET_REF_RE.match(val.strip()) \
                    or _ANY_INTERP_RE.match(val.strip()):
                continue
            found.append({"service": sname, "env": key})
            env_key_services.setdefault(key, []).append(sname)

    for item in found:
        svcs = env_key_services[item["env"]]
        if len(svcs) > 1:
            item["secret"] = f"{item['env']}_{item['service'].upper()}"
        else:
            item["secret"] = item["env"]
    return found


# ---------------------------------------------------------------------------
# move-to-secrets (import finalize)
# ---------------------------------------------------------------------------

def move_inline_secrets(db: Session, tenant_id: int, spec: Dict[str, Any],
                        selections: Optional[List[Dict[str, str]]],
                        user: Optional[models.User] = None) -> Dict[str, Any]:
    """Create/update the selected tenant secrets and rewrite those env values
    to ``${secret:NAME}`` in `spec` (mutated and returned).

    `selections` is the caller's chosen subset/renaming of
    :func:`detect_inline_secrets` items: ``[{"service", "env", "secret"}]``
    (``secret`` optional — the suggested name wins). A selection that names no
    such inline value is a 422 (preview and finalize re-normalize
    independently, so a stale selection must not silently drop a secret).
    Secrets go through ``crud.create_or_update_secret`` — the exact 13.5a
    storage path (encrypted DB row + Vault). The value is read from `spec`
    only to hand it to the secret store; the env slot is then replaced with
    the ref, so the plaintext never persists in `spec`/``compose_spec``.
    """
    from services import stack_tenant_secrets as _sts
    from utils.vault_client import VaultError

    if not selections:
        return spec

    sel_items: Dict[Tuple[str, str], str] = {}
    for sel in selections:
        if not isinstance(sel, dict):
            raise HTTPException(status_code=422,
                                detail="move_secrets entries must be objects")
        svc = str(sel.get("service") or "")
        env = str(sel.get("env") or "")
        name = str(sel.get("secret") or "").strip()
        if not svc or not env or not name:
            raise HTTPException(
                status_code=422,
                detail="move_secrets entries need service, env and secret")
        if not _SECRET_REF_RE.match(f"${{secret:{name}}}"):
            raise HTTPException(status_code=422,
                                detail=f"invalid secret name {name!r}")
        sel_items[(svc, env)] = name

    for (svc, env), name in sel_items.items():
        value = _get_env_value(spec, svc, env)
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"no inline value to move for service {svc!r} env {env!r} "
                       "(already a secret ref or no longer present)")
        _upsert_tenant_secret(db, tenant_id, name, value, user)
        _set_env_value(spec, svc, env, f"${{secret:{name}}}")

    # Whole-`${secret:NAME}` values -> x-mod.secrets named refs (13.5a shape),
    # removing the literal from environment — idempotent on a re-run.
    _sts.normalize_tenant_secret_refs(spec)
    return spec


def _upsert_tenant_secret(db: Session, tenant_id: int, name: str, value: str,
                          user: Optional[models.User] = None) -> None:
    """Create-or-update through the 13.5a storage path
    (``crud.create_or_update_secret``). When no `user` is supplied a tenant
    user row is resolved here so the DB access stays in the service (lint R5).
    A Vault write failure is a 502 naming the secret, never its value."""
    import crud
    from utils.vault_client import VaultError

    if user is None:
        user = (db.query(models.User)
                .filter(models.User.tenant_id == tenant_id)
                .first())
    if user is None:
        raise HTTPException(status_code=404,
                            detail=f"tenant {tenant_id} has no user to attribute the secret to")
    try:
        crud.create_or_update_secret(
            db, secret_name=name, value=value, user=user,
            description="Created by 'Move to Secrets' on stack compose import")
    except VaultError as e:
        logger.warning("secret %r could not be stored (tenant %s): %s",
                       name, tenant_id, e)
        raise HTTPException(
            status_code=502,
            detail=f"secret {name!r} could not be stored (Vault unavailable)")


# ---------------------------------------------------------------------------
# preview redaction (never echo a pasted secret back)
# ---------------------------------------------------------------------------

def redact_preview_spec(spec: Any) -> Any:
    """Mask secret-looking inline env VALUES in `spec` (mutated and returned).

    Keys matching ``_SECRET_ENV_RE`` whose value is a concrete string (not a
    ``${...}`` form and not already the sentinel) become the sentinel. This is
    the preview-side counterpart of ``_redact_compose_secrets`` so a pasted
    secret does not echo back in the preview response; `${secret:...}` /
    ``${VAR}`` refs and already-redacted values pass through untouched."""
    re_mod = _shared()
    secret_re = re_mod._SECRET_ENV_RE
    sentinel = re_mod.REDACTED_SENTINEL
    services = (spec or {}).get("services") if isinstance(spec, dict) else None
    if not isinstance(services, dict):
        return spec
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for key, val in _iter_env(svc.get("environment")):
            if not secret_re.search(key) or not isinstance(val, str):
                continue
            if val == sentinel or _ANY_INTERP_RE.match(val.strip()):
                continue
            _set_env_value(spec, sname, key, sentinel)
    return spec


# ---------------------------------------------------------------------------
# Copy… (clone) — restore named refs behind redacted placeholders
# ---------------------------------------------------------------------------

def rehydrate_clone(spec: Any, source_stack: Any
                    ) -> Tuple[Any, Dict[str, Any]]:
    """Copy…: restore ``***redacted***`` env placeholders in a copied spec to
    ``${secret:NAME}`` refs when the SOURCE stack holds a named
    tenant-secret ref for that env.

    Returns ``(spec, info)`` where ``info = {"restored":
    [{"service", "env", "secret"}], "unresolved": [{"service", "env"}]}``.
    Restored values are written back as a literal ``${secret:NAME}`` in the
    environment (NOT normalized here — the Copy… flow re-imports the spec
    through ``/import-compose``, whose validator normalizes the ref into
    ``x-mod.secrets``). Redacted values the source did NOT back with a named
    ref (raw ``vault:`` refs, plaintext secrets) stay as the sentinel and are
    reported as unresolved — the caller keeps today's "replace before creating"
    behaviour for those. A no-op when the spec carries no sentinels.
    """
    re_mod = _shared()
    sentinel = re_mod.REDACTED_SENTINEL
    services = (spec or {}).get("services") if isinstance(spec, dict) else None
    if not isinstance(services, dict):
        return spec, {"restored": [], "unresolved": []}

    source_refs = source_stack.secret_refs if isinstance(source_stack, dict) else (
        getattr(source_stack, "secret_refs", None) or {})
    if not isinstance(source_refs, dict):
        source_refs = {}

    restored: List[Dict[str, str]] = []
    unresolved: List[Dict[str, str]] = []
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        pairs = list(_iter_env(env))
        if not any(v == sentinel for _, v in pairs):
            continue
        svc_refs = source_refs.get(sname) or {}
        if not isinstance(svc_refs, dict):
            svc_refs = {}
        out: Dict[str, str] = {}
        for key, val in pairs:
            if val == sentinel:
                ref = svc_refs.get(key)
                if isinstance(ref, dict) and isinstance(ref.get("secret"), str) \
                        and ref["secret"].strip():
                    out[key] = f"${{secret:{ref['secret'].strip()}}}"
                    restored.append({"service": sname, "env": key,
                                     "secret": ref["secret"].strip()})
                else:
                    unresolved.append({"service": sname, "env": key})
                    out[key] = val
            else:
                out[key] = val
        if isinstance(env, dict):
            svc["environment"] = out
        elif isinstance(env, list):
            svc["environment"] = [f"{k}={v}" for k, v in out.items()]

    # Deliberately no normalize_tenant_secret_refs here: Copy… re-imports the
    # spec, and the import path (validate_fn) normalizes ${secret:NAME} into
    # x-mod.secrets. Leaving the literal keeps the re-imported YAML faithful.
    return spec, {"restored": restored, "unresolved": unresolved}
