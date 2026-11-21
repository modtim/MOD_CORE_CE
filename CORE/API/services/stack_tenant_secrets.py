"""13.5a — persistent stacks referencing TENANT secrets by NAME.

A stack's `secret_refs` may hold `{ENV: {"secret": "<name>"}}` (a named
tenant secret) alongside the existing bare `vault:` path strings.
Deployment resolves named refs through the secrets service with a
tenant-scope check: the secret must be the stack tenant's own, or a
platform secret shared with it (specific tenant list, or all tenants).
Another tenant's secret → 403; no accessible secret → 404; an
accessible-but-unreadable secret → 502. Every failure names the SECRET,
never its value. Resolved values are returned in-memory only — callers
place them in the worker task payload (env or materialized file) and
never in logs, API responses, or the stored compose_spec.

Also owns the compose-native normalization (see
`normalize_tenant_secret_refs`): `${secret:NAME}` whole environment values
and top-level `secrets:` + service `secrets:` (file-type) lists are
rewritten into the same x-mod.secrets ref shape at create/update/import.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import crud
import models
from utils.vault_client import VaultError
from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

# Moved to stack_secret_refs.py (storePub-b1); re-exported for existing importers.
from services.stack_secret_refs import _SECRET_REF_RE, is_named_ref, named_secret_name  # moved (storePub-b1); re-exported


class TenantSecretNormalizationError(ValueError):
    """A compose-native secret form that cannot be converted (422 / 422-ish
    at every on-ramp). Carries a user-fixable message naming the construct."""


def optional_secret_names(stack) -> set:
    """13.5f — names the stack's package declared OPTIONAL.

    Read from the top-level `x-mod.secrets` metadata block of the stored
    compose_spec — the same block `services.stack_packages.
    package_required_secrets` reads for the catalog's `required_secrets`
    (an entry whose `required` is explicitly false). A ref in this set that
    is absent for the tenant is skipped at deploy: the env var is simply
    not injected and the file not mounted. Every other ref (including a
    name with no metadata at all) stays fail-closed."""
    spec = getattr(stack, "compose_spec", None)
    if not isinstance(spec, dict):
        return set()
    xmod = spec.get("x-mod")
    metadata = xmod.get("secrets") if isinstance(xmod, dict) else None
    if not isinstance(metadata, dict):
        return set()
    return {
        str(name) for name, meta in metadata.items()
        if isinstance(meta, dict) and meta.get("required") is False
    }


def file_secret_refs(stack) -> Dict[str, Dict[str, str]]:
    """`{service: {NAME: target-or-None}}` for every file-type named ref on
    the stack (13.5a). Empty when the stack has none."""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    refs = stack.secret_refs if isinstance(stack.secret_refs, dict) else {}
    for svc, envmap in refs.items():
        if not isinstance(envmap, dict):
            continue
        for env, ref in envmap.items():
            if isinstance(ref, dict) and ref.get("type") == "file":
                out.setdefault(svc, {})[str(env)] = (
                    ref.get("target") if isinstance(ref.get("target"), str) else None)
    return out


# ---------------------------------------------------------------------------
# deploy-time resolution (tenant-scoped)
# ---------------------------------------------------------------------------

def _is_platform_admin(user: Any) -> bool:
    return bool(getattr(user, "is_platform_admin", False))


def vault_path_allowed(path: str, tenant_id: Any, user: Any) -> bool:
    """13.5a deploy-time tenant-scope check for raw `vault:` refs.

    System paths (user is None) and platform admins keep the existing
    behaviour unconditionally. For an ordinary tenant user:
      - the stack's own tenant prefix (`tenants/<id>/...`) — allowed;
      - `platform/` and `infrastructure/...` — platform-owned namespaces
        (platform config, managed-AI, HF token) referenced by vetted
        catalog templates; left as-is (existing behaviour, not a new
        capability — the approle policy still bounds what the API can read);
      - anything else (e.g. another tenant's `tenants/<other>/...`) → 403.
    """
    if user is None or _is_platform_admin(user):
        return True
    if not isinstance(tenant_id, int):
        return False  # cannot establish scope — refuse
    if path.startswith(f"tenants/{tenant_id}/"):
        return True
    if path.startswith("platform/") or path.startswith("infrastructure/"):
        return True
    return False


def _secret_owned_elsewhere(db: Session, name: str) -> bool:
    """True when SOME tenant (a real tenant row, not a platform-shared
    secret) owns a secret of this name — the 403 vs 404 distinction so a
    cross-tenant reference is refused, not merely 'not found'."""
    row = (
        db.query(models.Secret.id)
        .filter(
            models.Secret.name == name,
            models.Secret.tenant_id.isnot(None),
            models.Secret.deleted_at.is_(None),
        )
        .first()
    )
    return row is not None


def _resolve_named_value(db: Session, tenant_id: int, name: str) -> str:
    """One named secret for `tenant_id`, tenant-scoped. Fails closed (403 /
    404 / 502) naming the secret, never its value."""
    # Scope check: own tenant, or a platform secret shared with this tenant
    # (specific id list / all tenants) — `resolve_secret_for_tenant` returns
    # ONLY those (never another tenant's own secret).
    secret = crud.resolve_secret_for_tenant(
        db, secret_name=name, tenant_id=tenant_id)
    if secret is None:
        if _secret_owned_elsewhere(db, name):
            raise HTTPException(
                status_code=403,
                detail=f"secret {name!r} belongs to another tenant")
        raise HTTPException(
            status_code=404,
            detail=f"secret {name!r} not found for tenant {tenant_id}")
    try:
        value = crud.resolve_decrypted_secret_for_tenant(
            db, secret_name=name, tenant_id=tenant_id)
    except VaultError:
        raise HTTPException(
            status_code=502,
            detail=f"secret {name!r} found but could not be read from Vault")
    if value is None:
        raise HTTPException(
            status_code=502,
            detail=f"secret {name!r} found but could not be read from Vault")
    return value


def resolve_stack_tenant_secrets(db: Session, stack) -> Dict[str, Dict[str, str]]:
    """Resolve the ENV-TYPE named tenant-secret refs on `stack` at deploy.

    Returns `{service: {NAME: value}}` for EVERY named ref (env- and
    file-type alike) — the same shape as `_resolve_stack_secrets` output so
    callers merge it straight into `secret_env`. The caller decides the
    DELIVERY channel per ref: env-type → container env (the existing
    `secret_env` injection), file-type → the worker file-secret channel
    (see `file_secret_refs`; its value must NOT also be injected as an env
    var). Fails closed:
      403 — the name exists under ANOTHER tenant (tenant isolation);
      404 — no accessible secret with that name (missing);
      502 — an accessible secret that cannot be read from Vault.
    Details name the secret, never its value.

    ONE exception (13.5f): a ref the stack's package declared OPTIONAL
    (`optional_secret_names`) that is absent for the tenant is SKIPPED —
    it simply does not appear in the result, so the env var is not
    injected and the file not mounted. Required refs (and optional refs
    that DO resolve) keep the fail-closed behaviour above.
    """
    refs = stack.secret_refs if isinstance(stack.secret_refs, dict) else {}
    tenant_id = stack.tenant_id
    optional = optional_secret_names(stack)
    out: Dict[str, Dict[str, str]] = {}
    for svc, envmap in refs.items():
        if not isinstance(envmap, dict):
            continue
        for env, ref in envmap.items():
            if not is_named_ref(ref):
                continue
            name = named_secret_name(ref)
            if name in optional and crud.resolve_secret_for_tenant(
                    db, secret_name=name, tenant_id=tenant_id) is None:
                # 13.5f — package-declared optional, absent for the
                # tenant: skip (no env injected / file not mounted).
                continue
            out.setdefault(svc, {})[str(env)] = _resolve_named_value(
                db, tenant_id, name)
    return out


def resolve_named_secret_value(db: Session, tenant_id: int, name: str) -> str:
    """Public wrapper for `_resolve_named_value` (deploy paths that hold a
    single named ref, e.g. a logical-backup DB password)."""
    return _resolve_named_value(db, tenant_id, name)


# ---------------------------------------------------------------------------
# 13.5e — secret rotation → "restart needed" on every stack that uses it
# ---------------------------------------------------------------------------

def stacks_using_secret(db: Session, tenant_id: int, name: str) -> List[int]:
    """IDs of this tenant's live stacks that reference `name` in any of the
    three 13.5a named-ref forms. All three compose-native forms
    (`{service:{ENV:{"secret": name}}}`, `${secret:NAME}`, and top-level
    `secrets:`) are normalised into the SAME x-mod.secrets shape at
    create/update/import, so a single scan of the stored `secret_refs`
    (already the parsed `{service: {env: {"secret": name, ...}}}`) covers
    them — no re-parsing of compose. Soft-deleted / torn-down stacks
    (`status == 'torn_down'` is the stack's soft-delete) are excluded, as
    are any stacks of another tenant (tenant-scoped). Secret values never
    appear here — only stack IDs."""
    rows = (
        db.query(models.PersistentStack)
        .filter(
            models.PersistentStack.tenant_id == tenant_id,
            models.PersistentStack.status != "torn_down",
        )
        .all()
    )
    out: List[int] = []
    for st in rows:
        refs = st.secret_refs if isinstance(st.secret_refs, dict) else {}
        hit = False
        for envmap in refs.values():
            if not isinstance(envmap, dict):
                continue
            for ref in envmap.values():
                if is_named_ref(ref) and named_secret_name(ref) == name:
                    hit = True
                    break
            if hit:
                break
        if hit:
            out.append(st.id)
    return out


def _audit_secret_rotation(db: Session, stack, actor: Any, name: str) -> None:
    """One audit row per rotated stack: who (`actor`), which stack, and the
    secret NAME only — never its value. Mirrors the `_audit_stack` pattern
    (best-effort, never blocks the caller)."""
    try:
        db.add(models.AuditEvent(
            tenant_id=actor.tenant_id,
            user_id=actor.id,
            event_name="STACK_SECRET_ROTATED",
            details={"stack_id": stack.id, "secret_name": name},
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("secret-rotation audit failed for stack %s: %s", stack.id, e)


def flag_stacks_for_secret_rotation(db: Session, tenant_id: int, name: str,
                                    actor: Any) -> int:
    """13.5e — after a tenant secret's VALUE changes, flag every live stack of
    `tenant_id` that references `name` as `needs_redeploy=True` and audit one
    `STACK_SECRET_ROTATED` row per stack (who + which stack + secret NAME,
    never the value). Returns the number of stacks flagged. The operator's
    one-click redeploy is the existing `POST /{stack_id}/deploy`; a successful
    deploy clears the flag (worker task completion handler). Tenant-scoped: a
    same-named secret owned by another tenant is never touched."""
    flagged = 0
    for stack_id in stacks_using_secret(db, tenant_id, name):
        st = db.get(models.PersistentStack, stack_id)
        if st is None:
            continue
        st.needs_redeploy = True
        db.add(st)
        _audit_secret_rotation(db, st, actor, name)
        flagged += 1
    db.commit()
    return flagged


# ---------------------------------------------------------------------------
# compose-native normalization (create / update / import)
# ---------------------------------------------------------------------------

def _normalize_env_map(name: str, env, sblock: Dict[str, Any]) -> None:
    """Rewrite whole-VALUE `${secret:NAME}` entries into named refs, removing
    the literal from the environment. Partial interpolation (`PRE${secret:X}`)
    is deliberately NOT supported — only whole values."""
    if isinstance(env, dict):
        for k in list(env.keys()):
            v = env[k]
            if isinstance(v, str):
                m = _SECRET_REF_RE.match(v.strip())
                if m:
                    env.pop(k)
                    sblock.setdefault(str(k), {"secret": m.group(1)})
    elif isinstance(env, list):
        for i, item in enumerate(list(env)):
            if not isinstance(item, str) or "=" not in item:
                continue
            k, v = item.split("=", 1)
            m = _SECRET_REF_RE.match(v.strip())
            if m:
                env.pop(i)
                sblock.setdefault(k.strip(), {"secret": m.group(1)})


def normalize_tenant_secret_refs(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite compose-native secret forms into x-mod.secrets named refs.

      1. a service environment value that is WHOLLY `${secret:NAME}` →
         `x-mod.secrets[ENV] = {"secret": NAME}` (literal removed);
      2. top-level compose `secrets:` (file-type declarations) + a service's
         `secrets:` list → `x-mod.secrets[NAME] = {"secret": NAME,
         "type": "file"}` (plus `"target"` when the service entry names one).
         The compose declarations themselves are REMOVED from the stored spec
         (MOD manages them; the worker materialisation is the deploy-time
         file channel). `external: true` and `environment:`-type top-level
         secrets are refused — only file-type is convertible.

    Mutates and returns `spec` (idempotent). Raises
    :class:`TenantSecretNormalizationError` on a non-convertible form."""
    if not isinstance(spec, dict):
        return spec

    # 1 — whole-value ${secret:NAME} in every service environment
    services = spec.get("services")
    if isinstance(services, dict):
        for name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            if "environment" in svc:
                xmod = svc.get("x-mod")
                if not isinstance(xmod, dict):
                    xmod = {}
                    svc["x-mod"] = xmod
                sblock = xmod.get("secrets")
                if not isinstance(sblock, dict):
                    sblock = {}
                    xmod["secrets"] = sblock
                _normalize_env_map(name, svc["environment"], sblock)

    # 2 — compose-native file secrets: top-level declaration + service lists
    top = spec.get("secrets")
    if top is not None:
        if not isinstance(top, dict):
            raise TenantSecretNormalizationError(
                "top-level `secrets` must be a mapping of secret name to declaration")
        for sname, decl in top.items():
            if isinstance(decl, dict):
                if decl.get("external"):
                    raise TenantSecretNormalizationError(
                        f"top-level secret {sname!r}: `external: true` docker "
                        "secrets are not supported — create a MOD tenant secret "
                        "with this name and reference it via ${secret:...} or "
                        "x-mod.secrets")
                if "environment" in decl:
                    raise TenantSecretNormalizationError(
                        f"top-level secret {sname!r}: `environment`-type docker "
                        "secrets are not supported — use a ${secret:...} "
                        "environment value instead")
                if "file" not in decl:
                    raise TenantSecretNormalizationError(
                        f"top-level secret {sname!r}: only `file`-type compose "
                        "secrets can be imported as MOD tenant secrets")
        del spec["secrets"]
    if isinstance(services, dict):
        for name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            svc_secrets = svc.pop("secrets", None)
            if svc_secrets is None:
                continue
            if not isinstance(svc_secrets, list):
                raise TenantSecretNormalizationError(
                    f"service {name!r}: `secrets` must be a list")
            xmod = svc.get("x-mod")
            if not isinstance(xmod, dict):
                xmod = {}
                svc["x-mod"] = xmod
            sblock = xmod.get("secrets")
            if not isinstance(sblock, dict):
                sblock = {}
                xmod["secrets"] = sblock
            for entry in svc_secrets:
                sname = None
                target = None
                if isinstance(entry, str):
                    sname = entry
                elif isinstance(entry, dict):
                    for key in ("source", "secret", "name"):
                        if isinstance(entry.get(key), str):
                            sname = entry[key]
                            break
                    if isinstance(entry.get("target"), str):
                        target = entry["target"]
                if not isinstance(sname, str) or not sname.strip():
                    raise TenantSecretNormalizationError(
                        f"service {name!r}: secrets list entry {entry!r} "
                        "must name a secret")
                sname = sname.strip()
                ref: Dict[str, Any] = {"secret": sname, "type": "file"}
                if isinstance(target, str) and target.strip():
                    ref["target"] = target.strip()
                sblock.setdefault(sname, ref)

    # Prune any now-EMPTY x-mod.secrets / x-mod the normalization introduced
    # (e.g. a service whose env had no whole-value secret refs).
    if isinstance(services, dict):
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            xmod = svc.get("x-mod")
            if isinstance(xmod, dict):
                sblock = xmod.get("secrets")
                if isinstance(sblock, dict) and not sblock:
                    xmod.pop("secrets")
                if not xmod:
                    svc.pop("x-mod")
    return spec
