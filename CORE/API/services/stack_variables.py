"""GX.S10a (launch-completion-megaplan Group X) — stack variables + secrets.

Contract: the internal design notes
gx-s10-stack-variables-secrets.md §1–3.

The STORED compose keeps `<NAME>` / `${NAME}` / `${NAME:-default}` placeholders.
Real values are substituted only into the WORKER copy at deploy time — the same
posture as the existing per-service `x-mod.secrets`. Secret values never land
in the DB, API responses, stack revisions, audits or logs: Vault is the only
store, and the DB `stack_secret_index` row records path/metadata only.

Kinds (top-level `x-mod.variables` block, stored in the compose spec):
  plain         value stored literally in the spec (must not be a secret)
  secret_env    value in Vault; every placeholder occurrence is replaced by it
  secret_file   value in Vault; delivered as a file, placeholder -> the file PATH
  storage_file  tenant asset (cog) mounted read-only; placeholder -> the path;
                the CURRENT version at each deploy unless pin_version is set

`_shared.py` (never import this module at its own import time — function-local
imports only, the same convention services/persistent_stacks_workers_fleet_
service.py uses) calls `resolve_variables` + `render_worker_spec` from
`_compose_yaml`; the domain service passes the bundle's `secret_files` /
`storage_files` onto the task payload.
"""
from __future__ import annotations

import base64
import binascii
import copy
import os
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException
from utils.http_errors import internal_http_error
from product_names import themed_logger

# --- placeholder syntax (contract §1) -----------------------------------------
# Tim's syntax: <NAME>. Compose-native: ${NAME} and ${NAME:-default}.
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
ANGLE_RE = re.compile(rf"<({_NAME})>")
DOLLAR_RE = re.compile(r"\$\{(" + _NAME + r")(?::-([^}]*))?\}")
NAME_FULL_RE = re.compile(rf"^{_NAME}$")
# suggested_kind: secret-looking names get the secret_env suggestion.
_SECRET_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL|PRIVATE|CERT|LICEN[CS]E)",
    re.IGNORECASE)

VARIABLE_KINDS = ("plain", "secret_env", "secret_file", "storage_file")


class StackVariableError(HTTPException):
    """Fail-closed variable error (422) that always names the variable(s)."""


# =============================================================================
# detect — POST /v1/stacks/compose/variables/detect
# =============================================================================

def parse_compose_text(text: str) -> dict:
    """Parse caller-supplied compose YAML for the detect endpoint. 422 with the
    parser's own message on anything malformed (never a 500)."""
    import yaml
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="compose must be non-empty YAML text")
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise internal_http_error(422, "Invalid compose YAML", e) from e
    if not isinstance(spec, dict) or not isinstance(spec.get("services"), dict):
        raise HTTPException(status_code=422,
                            detail="compose must be a mapping with a 'services' object")
    return spec


def suggested_kind(name: str) -> str:
    """`secret_env` for secret-looking names (contract §3), else `plain`."""
    return "secret_env" if _SECRET_NAME_RE.search(name or "") else "plain"


def declared_variables(spec) -> dict:
    """The top-level x-mod.variables block ({} when absent)."""
    if not isinstance(spec, dict):
        return {}
    xmod = spec.get("x-mod")
    block = xmod.get("variables") if isinstance(xmod, dict) else None
    return block if isinstance(block, dict) else {}


def spec_has_variables(spec) -> bool:
    return bool(declared_variables(spec))


def _walk_strings(obj, service: Optional[str], path: str, found: dict) -> None:
    """Collect every placeholder occurrence in every string value.
    `found` is {name: {"syntaxes": set, "occurrences": [{service, path}],
    "defaults": [..]}}. Service None = a top-level (non-service) string."""
    if isinstance(obj, str):
        for m in ANGLE_RE.finditer(obj):
            _note(found, m.group(1), service, path, "angle", None)
        for m in DOLLAR_RE.finditer(obj):
            _note(found, m.group(1), service, path, "dollar", m.group(2))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{path}.{k}" if path else str(k)
            _walk_strings(v, service, key, found)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_strings(v, service, f"{path}[{i}]", found)


def _note(found: dict, name: str, service, path: str, syntax: str,
          default: Optional[str]) -> None:
    e = found.setdefault(name, {"syntaxes": set(), "occurrences": [], "defaults": []})
    e["syntaxes"].add(syntax)
    # Cap occurrence fan-out — a variable referenced in a giant env map must
    # not balloon the detect response. 50 is far past any real compose.
    if len(e["occurrences"]) < 50:
        e["occurrences"].append({"service": service, "path": path})
    if default and default not in e["defaults"]:
        e["defaults"].append(default)


def detect_variables(spec: dict) -> list:
    """One entry per placeholder name found anywhere in the spec (contract §3)."""
    found: dict = {}
    services = spec.get("services") if isinstance(spec, dict) else None
    if isinstance(services, dict):
        for name, svc in services.items():
            _walk_strings(svc, name, "", found)
        # top-level strings outside services (top-level labels, networks, ...)
        for k, v in spec.items():
            if k not in ("services", "x-mod"):
                _walk_strings(v, None, str(k), found)
    declared = declared_variables(spec)
    out = []
    for name in sorted(found):
        e = found[name]
        syntaxes = e["syntaxes"]
        # Both spellings: dollar wins (it carries the :-default semantics).
        syntax = "dollar" if "dollar" in syntaxes else "angle"
        out.append({
            "name": name,
            "syntax": syntax,
            "occurrences": e["occurrences"],
            "default": e["defaults"][0] if e["defaults"] else None,
            "suggested_kind": suggested_kind(name),
            "declared": name in declared,
        })
    return out


# =============================================================================
# save — apply_variables (create / PUT definition / import finalize)
# =============================================================================

def _vault_path(tenant_id, stack_name: str, name: str) -> str:
    return f"tenants/{tenant_id}/stacks/{stack_name}/vars/{name}"


def _require_vault_write(path: str, data: dict) -> None:
    from routers.persistent_stacks._shared import _vault_write
    _vault_write(path, data)


def _secret_file_cap(db) -> int:
    from services.platform_settings import get_setting
    return int(get_setting("stacks.secret_file_max_bytes", db=db))


def _inline_secret_payload(db, kind: str, value, b64, var_name: str) -> dict:
    """Build the Vault payload for an INLINE `value` (create + PUT paths).

    `b64: true` (kind secret_file ONLY) means the caller sent base64 — the UI
    uses this for binary files, which `readAsText` would corrupt. We decode,
    enforce the `stacks.secret_file_max_bytes` cap on the DECODED bytes and
    re-encode canonically so the stored payload is always `{value: <b64>,
    b64: true}`. A plain text secret_file gets the same cap on its UTF-8
    length. Every other kind is unchanged: `{"value": str(value)}`.

    NEVER include the value (or a slice of it) in an error detail or a log.
    """
    if b64:
        if kind != "secret_file":
            raise HTTPException(status_code=422,
                                detail=f"variable {var_name!r}: b64 is only for kind "
                                       f"secret_file")
        try:
            raw = base64.b64decode(str(value), validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422,
                                detail=f"variable {var_name!r}: value is not valid "
                                       f"base64") from None
        cap = _secret_file_cap(db)
        if len(raw) > cap:
            raise HTTPException(
                status_code=422,
                detail=f"variable {var_name!r}: value is {len(raw)} bytes; "
                       f"secret_file values are capped at {cap} bytes "
                       f"(stacks.secret_file_max_bytes)")
        return {"value": base64.b64encode(raw).decode("ascii"), "b64": True}
    if kind == "secret_file":
        cap = _secret_file_cap(db)
        size = len(str(value).encode("utf-8"))
        if size > cap:
            raise HTTPException(
                status_code=422,
                detail=f"variable {var_name!r}: value is {size} bytes; "
                       f"secret_file values are capped at {cap} bytes "
                       f"(stacks.secret_file_max_bytes)")
        return {"value": str(value)}
    return {"value": str(value)}


def upsert_secret_index(db, tenant_id, path: str, stack_name: str, name: str,
                        user_id: Optional[int]) -> None:
    """Append/soft-delete (ISO) row in stack_secret_index. Best-effort: an index
    write failure must not break the Vault write itself (the value is already
    durably in Vault) — but it is logged."""
    try:
        import models
        now = models.func.now()
        live = (db.query(models.StackSecretIndex)
                .filter(models.StackSecretIndex.tenant_id == tenant_id,
                        models.StackSecretIndex.path == path,
                        models.StackSecretIndex.deleted_at.is_(None))
                .first())
        if live is not None:
            live.deleted_at = now
        db.add(models.StackSecretIndex(
            tenant_id=tenant_id, path=path, stack_name=stack_name, name=name,
            updated_by_user_id=user_id))
        db.flush()
    except Exception as e:  # noqa: BLE001
        themed_logger(__name__, 'stacks').warning("stack_secret_index upsert failed for %s: %s",
                                            path, e)


def _get_file_cog(db, tenant_id, cog_id: int, var_name: str) -> "models.Cog":
    """Tenant-scoped FILE cog lookup. Cross-tenant / missing / deleted all read
    as 404 (never leak another tenant's existence); a folder cog is 422."""
    import models
    cog = (db.query(models.Cog)
           .filter(models.Cog.id == int(cog_id),
                   models.Cog.tenant_id == tenant_id,
                   models.Cog.deleted_at.is_(None))
           .first())
    if cog is None:
        raise HTTPException(status_code=404,
                            detail=f"variable {var_name!r}: cog {cog_id} not found")
    if cog.cog_type != models.CogType.FILE:
        raise HTTPException(status_code=422,
                            detail=f"variable {var_name!r}: cog {cog_id} is not a file")
    return cog


def _blob_for(db, tenant_id, cog, var_name: str,
              pin_version: Optional[int]) -> "models.ContentBlob":
    import models
    if pin_version:
        v = (db.query(models.Version)
             .filter(models.Version.cog_id == cog.id,
                     models.Version.version_number == int(pin_version),
                     models.Version.tenant_id == tenant_id)
             .first())
        if v is None or not v.content_id:
            raise StackVariableError(
                status_code=422,
                detail=f"variable {var_name!r}: pinned version {pin_version} "
                       f"not found for cog {cog.id}")
        blob_id = v.content_id
    else:
        blob_id = cog.latest_version_id
    if not blob_id:
        raise StackVariableError(
            status_code=422,
            detail=f"variable {var_name!r}: cog {cog.id} has no content")
    blob = (db.query(models.ContentBlob)
            .filter(models.ContentBlob.id == blob_id,
                    models.ContentBlob.tenant_id == tenant_id)
            .first())
    if blob is None:
        raise StackVariableError(
            status_code=422,
            detail=f"variable {var_name!r}: content missing for cog {cog.id}")
    return blob


def _storage_object(cfg, tenant_id: int, blob):
    """(bucket, object_name) for a blob's storage_path (`s3://bucket/sha...`,
    legacy `minio://bucket/sha...` still understood — dual-read, P10c)."""
    sp = blob.storage_path or ""
    bucket = cfg.bucket or f"tenant-{tenant_id}-files"
    for scheme in _storage_schemes():
        if sp.startswith(f"{scheme}://"):
            rest = sp[len(scheme) + 3:]
            if "/" in rest:
                return rest.split("/", 1)
            return bucket, rest
    return bucket, sp.lstrip("/")


def _storage_schemes() -> tuple:
    """Schemes accepted when parsing a stored storage_path (dual-read).

    `s3` is the primary default; `minio` stays a REAL accepted value so an
    existing deployment with STORAGE_SCHEME=minio keeps its old behaviour.
    Stored values are never rewritten.
    """
    configured = os.getenv("STORAGE_SCHEME", "s3")
    return tuple(dict.fromkeys((configured, "s3", "minio")))


def _storage_bytes(db, tenant_id: int, blob) -> bytes:
    """Read a tenant's own object (from_cog_id snapshot path)."""
    from utils.storage import build_client_for_config, resolve_backend
    cfg = resolve_backend(tenant_id, db=db)
    client = build_client_for_config(cfg, presigned=False)
    bucket, obj = _storage_object(cfg, tenant_id, blob)
    resp = client.get_object(bucket_name=bucket, object_name=obj)
    try:
        return resp.read()
    finally:
        try:
            resp.close()
            resp.release_conn()
        except Exception:  # noqa: BLE001
            pass


def _snapshot_cog_bytes(db, tenant_id: int, user, from_cog_id: int,
                        var_name: str) -> bytes:
    """secret_file + from_cog_id: copy the cog's bytes into Vault at save time
    (a snapshot — the cog id is NOT kept afterwards). Capped by the platform
    setting `stacks.secret_file_max_bytes` (default 1 MiB) → 422 over the cap."""
    from services.platform_settings import get_setting
    cap = int(get_setting("stacks.secret_file_max_bytes", db=db))
    cog = _get_file_cog(db, tenant_id, from_cog_id, var_name)
    if user is not None and getattr(user, "id", None) is not None:
        from permissions import can_user_view_cog
        if not can_user_view_cog(db, user.id, cog.id):
            raise HTTPException(status_code=403,
                                detail=f"variable {var_name!r}: no read access to cog {cog.id}")
    blob = _blob_for(db, tenant_id, cog, var_name, None)
    size = int(blob.size_bytes or 0)
    if size > cap:
        raise HTTPException(
            status_code=422,
            detail=f"variable {var_name!r}: cog {cog.id} is {size} bytes; "
                   f"secret_file snapshots are capped at {cap} bytes "
                   f"(stacks.secret_file_max_bytes)")
    return _storage_bytes(db, tenant_id, blob)


def apply_variables(db, spec: dict, variables: Optional[dict], *, tenant_id,
                    stack_name: str, user) -> dict:
    """Merge caller-supplied `variables` into the spec's x-mod.variables, write
    secret values to Vault (or snapshot a cog), and rewrite the block with REFS
    ONLY — the stored spec never carries a secret value. 422 when a detected
    placeholder is left unresolved (the error lists the missing names); 403 for
    an existing_path outside the tenant's own Vault prefix; 422 over the
    secret_file snapshot cap. `user` may be None only on system paths that
    already own the cog (the create/definition endpoints always pass one)."""
    if not variables:
        return spec
    if not isinstance(variables, dict):
        raise HTTPException(status_code=422, detail="variables must be an object")
    if not isinstance(spec, dict):
        raise HTTPException(status_code=422, detail="compose_spec must be an object")
    xmod = spec.get("x-mod")
    if not isinstance(xmod, dict):
        xmod = {}
        spec["x-mod"] = xmod
    declared = declared_variables(spec)
    merged = dict(declared)
    for k, v in variables.items():
        if not NAME_FULL_RE.match(str(k)):
            raise HTTPException(status_code=422,
                                detail=f"variable name {k!r} is not a valid NAME")
        merged[str(k)] = v if isinstance(v, dict) else {}

    tenant_prefix = f"tenants/{tenant_id}/"
    user_id = getattr(user, "id", None) if user is not None else None
    for name, entry in merged.items():
        kind = entry.get("kind")
        if kind not in VARIABLE_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"variable {name!r}: kind must be one of {list(VARIABLE_KINDS)}")

        if kind == "plain":
            if not isinstance(entry.get("value"), str):
                raise HTTPException(status_code=422,
                                    detail=f"variable {name!r}: plain needs a string value")
            merged[name] = {"kind": "plain", "value": entry["value"]}
            continue

        if kind in ("secret_env", "secret_file"):
            path = entry.get("existing_path")
            if path:
                # The tenant prefix is the security boundary (mirrors
                # _require_under_prefix on the write endpoint).
                if not isinstance(path, str) or not path.startswith(tenant_prefix) \
                        or ".." in path.split("/"):
                    raise HTTPException(status_code=403,
                                        detail=f"variable {name!r}: existing_path "
                                               f"must be under {tenant_prefix!r}")
            else:
                if entry.get("value") is None and entry.get("from_cog_id") is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"variable {name!r}: needs a value, from_cog_id, "
                               f"or existing_path")
                path = _vault_path(tenant_id, stack_name, name)
            # Write the value (or snapshot a cog) — never keep it in the spec.
            if entry.get("from_cog_id") is not None:
                if kind != "secret_file":
                    raise HTTPException(status_code=422,
                                        detail=f"variable {name!r}: from_cog_id is "
                                               f"only for kind secret_file")
                raw = _snapshot_cog_bytes(db, tenant_id, user,
                                          int(entry["from_cog_id"]), name)
                _require_vault_write(path, {"value": base64.b64encode(raw).decode("ascii"),
                                            "b64": True})
            elif entry.get("value") is not None:
                _require_vault_write(
                    path, _inline_secret_payload(db, kind, entry["value"],
                                                 entry.get("b64"), name))
            elif not path.startswith(tenant_prefix + "stacks/"):
                # an existing_path outside our own vars/ may legitimately hold a
                # value written earlier through the stack-secrets endpoint
                pass
            # (existing_path with no value: the value already lives there.)
            out = {"kind": kind, "vault": path}
            if kind == "secret_file":
                out["file"] = entry.get("file") or f"/run/secrets/{name.lower()}"
                out["mode"] = entry.get("mode") or "0400"
                if entry.get("services"):
                    out["services"] = [str(s) for s in entry["services"]]
            merged[name] = out
            upsert_secret_index(db, tenant_id, path, stack_name, name, user_id)
            continue

        # storage_file
        cog_id = entry.get("cog_id")
        if cog_id is None:
            raise HTTPException(status_code=422,
                                detail=f"variable {name!r}: storage_file needs cog_id")
        cog = _get_file_cog(db, tenant_id, cog_id, name)
        if user_id is not None:
            from permissions import can_user_view_cog
            if not can_user_view_cog(db, user_id, cog.id):
                raise HTTPException(status_code=403,
                                    detail=f"variable {name!r}: no read access "
                                           f"to cog {cog.id}")
        out = {"kind": "storage_file", "cog_id": int(cog.id)}
        default_mount = f"/data/{cog.name}" if cog.name else f"/data/{cog.id}"
        out["file"] = entry.get("file") or default_mount
        out["mode"] = entry.get("mode") or "0444"
        if entry.get("services"):
            out["services"] = [str(s) for s in entry["services"]]
        if entry.get("pin_version") is not None:
            _blob_for(db, tenant_id, cog, name, int(entry["pin_version"]))
            out["pin_version"] = int(entry["pin_version"])
        merged[name] = out

    xmod["variables"] = merged

    # Fail closed on UNRESOLVED placeholders: anything detected in the final
    # spec must be declared. (When `variables` was empty we returned earlier,
    # so plain legacy `${...}` composes keep the compose-interpolation posture.)
    missing = [e["name"] for e in detect_variables(spec) if not e["declared"]]
    if missing:
        raise HTTPException(
            status_code=422,
            detail="unresolved placeholders — declare a variable for each: "
                   + ", ".join(missing))
    return spec


# =============================================================================
# deploy — resolve + render the worker copy
# =============================================================================

def _read_vault_value(path: str, var_name: str) -> Any:
    """Read `{value: ...}` (optionally `{value: <b64>, b64: true}`) from Vault.
    Fail closed — a secret that cannot be resolved must NEVER degrade to an
    empty string or a deploy that starts without its secret."""
    if not path:
        raise StackVariableError(status_code=422,
                                 detail=f"variable {var_name!r}: no vault ref")
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not (role_id and secret_id):
        raise StackVariableError(
            status_code=422, detail=f"variable {var_name!r}: Vault approle not "
                                    f"configured; cannot resolve {path!r}")
    from utils.vault_client import approle_login, read_kv_v2, VaultError
    try:
        token = approle_login(role_id=role_id, secret_id=secret_id)
        data = read_kv_v2(path, token=token)
    except VaultError as e:
        raise internal_http_error(502, f"variable {var_name!r} could not be resolved", e) from e
    if not isinstance(data, dict) or "value" not in data:
        raise StackVariableError(
            status_code=422, detail=f"variable {var_name!r}: no value at {path!r}")
    return data


def _mode_int(mode, default: int) -> int:
    try:
        return int(str(mode), 8)
    except (TypeError, ValueError):
        return default


def _referencing_services(spec: dict, name: str) -> List[str]:
    """Services whose strings contain a placeholder for `name` (mount default)."""
    found: dict = {}
    services = spec.get("services") if isinstance(spec, dict) else {}
    if isinstance(services, dict):
        for svc_name, svc in services.items():
            _walk_strings(svc, svc_name, "", found)
    return [o["service"] for o in found.get(name, {}).get("occurrences", [])
            if o["service"]]


def resolve_variables(db, stack) -> dict:
    """Resolve a stack's x-mod.variables at deploy (contract §3). Returns a
    bundle:
      resolved     {NAME: value} — secret_file/storage_file vars map to the
                   file PATH (so `--key-file <NAME>` just works)
      secret_files [{name, content_b64, mode}]   (payload-ready)
      storage_files[{name, url, size, sha256?, mode}]  (payload-ready; the url
                   is a short-TTL presigned GET, never logged)
      mounts       {NAME: {target, mode, dir_env, services}}
    Fails closed (422 naming the variable) on any unresolvable ref, missing
    cog, soft-deleted cog, or out-of-tenant cog."""
    spec = stack.compose_spec or {}
    declared = declared_variables(spec)
    if not declared:
        return {"resolved": {}, "secret_files": [], "storage_files": [], "mounts": {}}

    resolved: Dict[str, str] = {}
    secret_files: List[dict] = []
    storage_files: List[dict] = []
    mounts: Dict[str, dict] = {}

    for name, entry in declared.items():
        if not isinstance(entry, dict):
            raise StackVariableError(status_code=422,
                                     detail=f"variable {name!r}: malformed declaration")
        kind = entry.get("kind")
        if kind in ("secret_env", "secret_file"):
            data = _read_vault_value(entry.get("vault"), name)
            if data.get("b64"):
                try:
                    raw = base64.b64decode(str(data["value"]))
                except Exception:
                    raise StackVariableError(status_code=422,
                                             detail=f"variable {name!r}: "
                                                    f"stored b64 value is corrupt")
            else:
                raw = str(data["value"]).encode("utf-8")
            if kind == "secret_env":
                resolved[name] = str(data["value"])
            else:
                mode = _mode_int(entry.get("mode"), 0o400)
                target = entry.get("file") or f"/run/secrets/{name.lower()}"
                secret_files.append({"name": name,
                                     "content_b64": base64.b64encode(raw).decode("ascii"),
                                     "mode": mode})
                mounts[name] = {"target": target, "mode": mode,
                                "dir_env": "MOD_SECRETS_DIR",
                                "services": entry.get("services")}
                resolved[name] = target
        elif kind == "storage_file":
            info = _resolve_storage_file(db, stack.tenant_id, name, entry)
            storage_files.append(info)
            mounts[name] = {"target": info["target"], "mode": info["mode"],
                            "dir_env": "MOD_FILES_DIR",
                            "services": entry.get("services")}
            resolved[name] = info["target"]
        elif kind == "plain":
            resolved[name] = str(entry.get("value", ""))
        else:
            raise StackVariableError(status_code=422,
                                     detail=f"variable {name!r}: unknown kind {kind!r}")

    return {"resolved": resolved, "secret_files": secret_files,
            "storage_files": storage_files, "mounts": mounts}


def _resolve_storage_file(db, tenant_id: int, name: str, entry: dict) -> dict:
    """storage_file at deploy: the cog must still exist, be in this tenant and
    be live (fail closed, naming the variable); mint a short-TTL presigned GET
    (BYO-S3 aware). The URL carries a presign signature and is NEVER logged."""
    from services.platform_settings import get_setting
    cap = int(get_setting("stacks.storage_file_max_bytes", db=db))
    cog = _get_file_cog(db, tenant_id, entry.get("cog_id"), name)
    blob = _blob_for(db, tenant_id, cog, name, entry.get("pin_version"))
    size = int(blob.size_bytes or 0)
    if size > cap:
        raise StackVariableError(
            status_code=422,
            detail=f"variable {name!r}: file is {size} bytes; storage_file is "
                   f"capped at {cap} bytes (stacks.storage_file_max_bytes)")
    from utils.storage import build_client_for_config, resolve_backend
    cfg = resolve_backend(tenant_id, db=db)
    client = build_client_for_config(cfg, presigned=True)
    bucket, obj = _storage_object(cfg, tenant_id, blob)
    try:
        url = client.presigned_get_object(
            bucket_name=bucket, object_name=obj, expires=timedelta(seconds=900))
    except Exception as e:  # noqa: BLE001 — never log e (it may carry the URL)
        raise StackVariableError(status_code=502,
                                 detail=f"variable {name!r}: storage presign failed")
    # FX.155 part 3 (owner Q142=A) — G5: a stack-variable delivery is a
    # signed data hand-off; every issue writes one delivery-audit row (the
    # set-time audit recorded the variable NAME only). System actor: deploy
    # resolution has no caller user, and record_delivery never raises.
    # 1788 CE carve: delivery_audit is paid; CE records no delivery row.
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    (_delivery_audit.record_delivery if _delivery_audit is not None
     else (lambda *a, **kw: None))(
        db,
        delivery_path="presign",
        actor_user_id=0,
        tenant_id=tenant_id,
        resource_id=cog.id,
        resource_type="cog",
        object_key=obj,
        expiry_seconds=900,
        metadata={"channel": "stack_variable", "variable": name},
    )
    mode = _mode_int(entry.get("mode"), 0o444)
    default_mount = f"/data/{cog.name}" if cog.name else f"/data/{cog.id}"
    return {"name": name, "url": url, "size": size,
            "sha256": blob.id or None, "mode": mode,
            "target": entry.get("file") or default_mount}


def payload_files(bundle: Optional[dict]) -> dict:
    """The payload additions for a resolved bundle ({}, when nothing to add)."""
    if not bundle:
        return {}
    out = {}
    if bundle.get("secret_files"):
        out["secret_files"] = bundle["secret_files"]
    if bundle.get("storage_files"):
        out["storage_files"] = bundle["storage_files"]
    return out


def _substitute(text: str, resolved: Dict[str, str]) -> str:
    for name in sorted(resolved, key=len, reverse=True):
        value = resolved[name]
        pat = re.compile(
            rf"(?:<{re.escape(name)}>|\$\{{{re.escape(name)}(?::-[^}}]*)?\}})")
        text = pat.sub(lambda _m, v=value: v, text)
    return text


def _walk_substitute(obj, resolved: Dict[str, str]) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                obj[k] = _substitute(v, resolved)
            else:
                _walk_substitute(v, resolved)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = _substitute(v, resolved)
            else:
                _walk_substitute(v, resolved)


def render_worker_spec(spec: dict, bundle: dict) -> None:
    """Rewrite the WORKER copy in place (called by `_compose_yaml`): substitute
    every placeholder, strip x-mod.variables (the worker only needs the result),
    and materialise the top-level `secrets:` block + per-service read-only
    mounts for secret_file / storage_file vars."""
    resolved = bundle.get("resolved") or {}
    if resolved:
        _walk_substitute(spec, resolved)
    xmod = spec.get("x-mod")
    if isinstance(xmod, dict):
        xmod.pop("variables", None)
    mounts = bundle.get("mounts") or {}
    if not mounts:
        return
    services = spec.get("services") or {}
    for name, m in mounts.items():
        declared_services = m.get("services")
        if declared_services:
            targets = [s for s in declared_services if s in services]
        else:
            # default: the services that reference the placeholder, else all.
            referencing = _referencing_services(spec, name)
            targets = referencing or list(services.keys())
        spec.setdefault("secrets", {})[name] = {
            "file": f"${{{m['dir_env']}}}/{name}"}
        for svc_name in targets:
            svc = services.get(svc_name)
            if not isinstance(svc, dict):
                continue
            entries = svc.setdefault("secrets", [])
            if not isinstance(entries, list):
                entries = []
                svc["secrets"] = entries
            entries.append({"source": name, "target": m["target"], "mode": m["mode"]})


# =============================================================================
# read side — GET /{id}/variables + GET /secrets/catalog
# =============================================================================

def _live_index_rows(db, tenant_id: int, path: str):
    import models
    return (db.query(models.StackSecretIndex)
            .filter(models.StackSecretIndex.tenant_id == tenant_id,
                    models.StackSecretIndex.path == path,
                    models.StackSecretIndex.deleted_at.is_(None))
            .order_by(models.StackSecretIndex.id.desc())
            .first())


def _username(db, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    import models
    u = db.query(models.User).filter(models.User.id == user_id).first()
    return u.username if u else None


def list_stack_variables(db, stack) -> list:
    """GET /v1/stacks/{id}/variables — NO values (plain included: the UI
    re-reads those from the compose). """
    spec = stack.compose_spec or {}
    declared = declared_variables(spec)
    services = spec.get("services") or {}
    out = []
    for name in sorted(declared):
        entry = declared[name] if isinstance(declared[name], dict) else {}
        kind = entry.get("kind")
        row = None
        has_value = kind == "plain"
        if kind in ("secret_env", "secret_file"):
            path = entry.get("vault") or ""
            row = _live_index_rows(db, stack.tenant_id, path)
            if row is not None:
                has_value = True
            elif path:
                # The index is our fast path; an index-less value (written
                # through the older stack-secrets endpoint) is confirmed with a
                # single Vault read. Best-effort: any read failure stays False
                # (the deploy would fail closed and name the variable anyway).
                try:
                    _read_vault_value(path, name)
                    has_value = True
                except HTTPException:
                    pass
        services_list = entry.get("services")
        if not services_list:
            ref = _referencing_services(spec, name)
            services_list = ref if ref else list(services.keys())
        item = {
            "name": name,
            "kind": kind,
            "services": services_list,
            "has_value": has_value,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            "updated_by": _username(db, row.updated_by_user_id) if row else None,
        }
        if kind in ("secret_file", "storage_file"):
            item["file"] = entry.get("file")
        if kind == "storage_file":
            item["cog_id"] = entry.get("cog_id")
        out.append(item)
    return out


def secret_catalog(db, tenant_id: int) -> list:
    """GET /v1/stacks/secrets/catalog — the tenant's existing secrets for the
    "use existing secret" dropdown, served from the DB index (Vault LIST is not
    granted to the approle). Soft-deleted rows are excluded; NO values."""
    import models
    rows = (db.query(models.StackSecretIndex)
            .filter(models.StackSecretIndex.tenant_id == tenant_id,
                    models.StackSecretIndex.deleted_at.is_(None))
            .order_by(models.StackSecretIndex.updated_at.desc())
            .all())
    out = []
    for r in rows:
        parts = r.path.split("/")
        stack_name = parts[3] if len(parts) > 3 and parts[0] == "tenants" else (r.stack_name or "")
        out.append({"path": r.path, "stack_name": stack_name, "name": r.name,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None})
    return out


# =============================================================================
# write side — PUT /{id}/variables/{name}
# =============================================================================

def set_stack_variable(db, stack, name: str, body: dict, user) -> list:
    """Set or rotate one variable's value. Writes Vault, sets
    needs_redeploy=true, audits STACK_VARIABLE_SET (NAME ONLY). Returns the
    refreshed list_stack_variables view."""
    declared = declared_variables(stack.compose_spec or {})
    if name not in declared or not isinstance(declared[name], dict):
        raise HTTPException(status_code=422,
                            detail=f"variable {name!r} is not declared in this "
                                   f"stack's x-mod.variables")
    entry = dict(declared[name])
    kind = body.get("kind") or entry.get("kind")
    if kind not in VARIABLE_KINDS:
        raise HTTPException(status_code=422,
                            detail=f"kind must be one of {list(VARIABLE_KINDS)}")

    if kind != entry.get("kind"):
        # a kind change rewrites the stored declaration
        if kind == "storage_file":
            if body.get("cog_id") is None:
                raise HTTPException(status_code=422, detail="cog_id is required")
            cog = _get_file_cog(db, stack.tenant_id, body["cog_id"], name)
            from permissions import can_user_view_cog
            if not can_user_view_cog(db, user.id, cog.id):
                raise HTTPException(status_code=403,
                                    detail=f"no read access to cog {cog.id}")
            entry = {"kind": "storage_file", "cog_id": int(cog.id),
                     "file": body.get("file") or (f"/data/{cog.name}" if cog.name
                                                  else f"/data/{cog.id}"),
                     "mode": body.get("mode") or "0444"}
            if body.get("pin_version") is not None:
                _blob_for(db, stack.tenant_id, cog, name, int(body["pin_version"]))
                entry["pin_version"] = int(body["pin_version"])
        elif kind == "secret_file":
            entry = {"kind": "secret_file",
                     "file": body.get("file") or f"/run/secrets/{name.lower()}",
                     "mode": body.get("mode") or "0400"}
            if body.get("services"):
                entry["services"] = [str(s) for s in body["services"]]
        else:
            entry = {"kind": kind}
    elif kind == "storage_file" and body.get("cog_id") is not None and \
            body.get("cog_id") != entry.get("cog_id"):
        # re-point an existing storage_file at another cog
        cog = _get_file_cog(db, stack.tenant_id, body["cog_id"], name)
        from permissions import can_user_view_cog
        if not can_user_view_cog(db, user.id, cog.id):
            raise HTTPException(status_code=403,
                                detail=f"no read access to cog {cog.id}")
        entry["cog_id"] = int(cog.id)
        if body.get("file"):
            entry["file"] = body["file"]

    if kind in ("secret_env", "secret_file"):
        path = entry.get("vault") or _vault_path(stack.tenant_id, stack.name, name)
        if body.get("from_cog_id") is not None:
            if kind != "secret_file":
                raise HTTPException(status_code=422,
                                    detail="from_cog_id is only for kind secret_file")
            raw = _snapshot_cog_bytes(db, stack.tenant_id, user,
                                      int(body["from_cog_id"]), name)
            _require_vault_write(path, {"value": base64.b64encode(raw).decode("ascii"),
                                        "b64": True})
        elif body.get("value") is not None:
            _require_vault_write(
                path, _inline_secret_payload(db, kind, body["value"],
                                             body.get("b64"), name))
        else:
            raise HTTPException(status_code=422,
                                detail=f"variable {name!r}: needs a value or from_cog_id")
        if "vault" not in entry:
            entry["vault"] = path
            _write_declared(stack, name, entry)
        upsert_secret_index(db, stack.tenant_id, path, stack.name, name, user.id)
    elif kind == "plain":
        if body.get("value") is None:
            raise HTTPException(status_code=422,
                                detail=f"variable {name!r}: plain needs a value")
        entry = {"kind": "plain", "value": str(body["value"])}
        _write_declared(stack, name, entry)

    stack.needs_redeploy = True
    from routers.persistent_stacks._shared import _audit_stack
    _audit_stack(db, "STACK_VARIABLE_SET", stack, user, {"variable": name})
    db.add(stack)
    db.commit()
    db.refresh(stack)
    return list_stack_variables(db, stack)


def _write_declared(stack, name: str, entry: dict) -> None:
    """Rewrite one x-mod.variables entry in the stored spec."""
    # Copy, never mutate in place: compose_spec is plain JSONB (no mutation
    # tracking), so re-assigning the SAME dict is not a change and the entry
    # (incl. its Vault path) was silently dropped on commit.
    spec = copy.deepcopy(stack.compose_spec or {})
    xmod = spec.setdefault("x-mod", {})
    xmod.setdefault("variables", {})[name] = entry
    stack.compose_spec = spec
    try:
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(stack, "compose_spec")
    except Exception:  # plain objects in unit tests carry no instance state
        pass


# NOTE: the worker capability gate (409 on an old worker for a secret_file
# stack) lives in routers/persistent_stacks/_shared.py
# (_require_worker_file_secrets / _worker_supports_file_secrets) and is called
# from services/persistent_stacks_domain_service.py at deploy + migrate. It
# prefers the worker's reported `file_secrets` heartbeat capability when present
# on hardware_specs, falling back to the agent-version gate (>= v1.0.75, the
# release that ships GX.S10b). Kept out of this module to keep a single source
# of truth next to the other deploy-time gates.
