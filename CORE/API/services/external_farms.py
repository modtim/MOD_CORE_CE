"""BRIEF-EXTFARM-1 — data layer for the external-orchestrator FARM registry.

The External Orchestrators page (routers/system_external.py) is read-only today:
GET /system/external only AGGREGATES api_task_queue submit rows (services/
external_load.py). There is no way to register a farm and nowhere to keep the
credential a farm needs, so brief 2's POST /system/external/farms and brief 3's
UI both need this module first.

THE CREDENTIAL NEVER LIVES HERE. It is written to Vault KV v2 at
``tenants/{tenant_id}/external-farms/{farm_id}`` and the row keeps ONLY that
reference path (``models.ExternalFarmConnection.vault_path``), the same shape as
``TenantEmailConfig.password_vault_path``. The tenant-scoped prefix is deliberate:
the ``core-api`` AppRole policy in open-core/overlay/scripts/first-run-openbao-init.sh
already grants create/read/update/delete/list on ``secret/data/tenants/*`` plus
read/list/delete on ``secret/metadata/tenants/*`` (the metadata delete a KV v2
delete needs), so no policy change and no POLICY_VERSION bump is required — and a
policy change could not reach existing installs anyway, whose openbao-init DONE
marker makes the heredoc a no-op. The path is also isolated per tenant, matching
this module's tenant_id filtering. There are no existing farm rows to migrate:
the feature never worked on the old prefix (no token, no policy grant), so no row
was ever committed with the old path. Consequences the tests pin:

* create is fail-closed: the Vault write happens BEFORE the row is committed,
  so a Vault error (unreachable, timeout, TLS error, malformed body) leaves NO
  row — a farm whose secret was never stored would be a dead-end row the UI
  could not use;
* the endpoint is validated before it is stored: http/https only, a host is
  required, and any userinfo (``@``, user, password, token) in the URL is
  refused, so no credential is ever persisted at rest in the DB column;
* delete commits the ROW first, then removes the Vault secret best-effort: an
  orphan Vault secret is safer than an orphan row whose vault_path points at a
  path that may already be gone;
* the credential string never appears in a row, a returned dict, an exception
  message, or a log line — only kind/name/id are ever logged, and validation
  errors never quote the endpoint (it can carry a credential).

Tenant isolation: every entry point takes ``tenant_id`` and filters on it, so a
foreign tenant's farm id reads as not-found (same result as a missing id — no
error shape that leaks which ids exist in other tenants).

Import guard (CE): stdlib + sqlalchemy + ``requests`` (exception types only —
this module makes no outbound request of its own beyond the vault_client calls)
+ ``models`` + ``utils.vault_client`` only. No router is imported (a router must
not be a dependency of a service); the token helper is the small service-side
copy from services/db_password_rotation.py.
"""
from __future__ import annotations

import logging
import os
import unicodedata
from typing import Optional
from urllib.parse import urlparse

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from utils import vault_client

logger = logging.getLogger(__name__)

# Tenant-scoped so the EXISTING core-api policy already grants this path
# (secret/data/tenants/* + secret/metadata/tenants/*); see the module docstring.
VAULT_FARM_PATH_PREFIX = "tenants"
ALLOWED_KINDS = ("kubernetes", "qube", "deadline")
ALLOWED_ENDPOINT_SCHEMES = ("http", "https")
# Column widths from models.ExternalFarmConnection: over-long values must
# raise ValueError here, never surface as a raw psycopg IntegrityError.
FARM_KIND_MAX_LEN = 32
FARM_NAME_MAX_LEN = 120
FARM_ENDPOINT_MAX_LEN = 512
# Errors the Vault helper can raise: vault_client wraps ONLY
# requests.ConnectionError, so timeouts / TLS errors / chunked-encoding drops
# and a malformed JSON body escape it as requests.* or ValueError.
VAULT_FAILURES = (vault_client.VaultError, requests.RequestException, ValueError)


class ExternalFarmVaultError(RuntimeError):
    """Vault could not be reached/used. Never carries the credential."""


class DuplicateFarmNameError(RuntimeError):
    """(tenant_id, name) is already taken. Names the name, never a secret."""


def _vault_token() -> Optional[str]:
    """The Vault token for this process, or None (fail closed).

    First choice is the Vault Agent token file (same path/env convention as
    database.py and services/db_password_rotation.py). CE does not run the Vault
    Agent for this container, so when that file is missing/empty we fall back to
    a direct AppRole login the same way crud/secret_cud.py's
    _vault_service_token() and database.py's _vault_login() do — the pair comes
    from VAULT_APPROLE_ROLE_ID / VAULT_APPROLE_SECRET_ID, which the api container
    exports from /run/openbao/approle_{role,secret}_id. The token bytes are never
    logged, returned as a value the caller can store, or audited; ANY failure
    (no file, no AppRole env, login refused, unreachable Vault) yields None so
    the caller keeps raising ExternalFarmVaultError = fail closed 503."""
    path = os.getenv("VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass

    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not role_id or not secret_id:
        return None
    try:
        return vault_client.approle_login(role_id=role_id, secret_id=secret_id)
    except VAULT_FAILURES:
        # VaultError / requests.* / ValueError — never chained, never quoted:
        # the AppRole login body carries the pair and the response the token.
        return None


def _farm_vault_path(tenant_id, farm_id) -> str:
    """``tenants/{tenant_id}/external-farms/{farm_id}`` — tenant-scoped path."""
    return f"{VAULT_FARM_PATH_PREFIX}/{tenant_id}/external-farms/{farm_id}"


def _endpoint_reason(endpoint: str) -> Optional[str]:
    """Why ``endpoint`` is unacceptable (None = acceptable).

    The reason NEVER quotes the endpoint: a rejected URL can itself carry a
    credential (``https://user:tok@host``) and these strings end up in API
    responses and logs. Style follows services/ai_stack_provider.py:269
    (scheme allowlist via urlparse) and services/admin_plans_webhooks_service.py:90.
    """
    if len(endpoint) > FARM_ENDPOINT_MAX_LEN:
        return f"farm endpoint must be {FARM_ENDPOINT_MAX_LEN} characters or fewer"
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f or
           unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp")
           for ch in endpoint):
        return "farm endpoint must not contain whitespace, control or invisible characters"
    try:
        parsed = urlparse(endpoint)
    except Exception:  # noqa: BLE001 — malformed URL -> refuse
        return "farm endpoint is not a parseable URL"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_ENDPOINT_SCHEMES:
        return "farm endpoint must use the http or https scheme"
    if "@" in parsed.netloc:
        return ("farm endpoint must not embed a credential "
                "(user, password, or token in the URL)")
    if not parsed.hostname:
        return "farm endpoint must name a host"
    return None


def _compensate_vault_delete(path: str, farm_name: str) -> None:
    """Best-effort removal of a Vault secret whose row never got committed.

    Swallows every failure: the caller is already raising its own domain error,
    and a leftover secret with no row is strictly safer than a row whose secret
    was never stored. Nothing about the payload is logged — only the farm name.
    """
    try:
        vault_client.delete_kv_v2(path, token=_vault_token())
    except Exception:  # noqa: BLE001 — compensation is best-effort
        logger.warning("Farm %s left an unreferenced Vault secret: cleanup failed",
                       farm_name)


def create_farm(db: Session, tenant_id: int, kind: str, name: str,
                endpoint: str, credential: Optional[str] = None):
    """Register one farm for ``tenant_id``; return the ORM row.

    Raises ValueError on an unknown kind, an over-long kind/name/endpoint, or
    an endpoint that is not a credential-free http(s) URL; DuplicateFarmNameError
    on a taken (tenant_id, name); ExternalFarmVaultError when a credential was
    supplied but Vault rejected the write (the row is rolled back — fail closed).
    """
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unknown farm kind: {kind}")
    if not name or not endpoint:
        raise ValueError("farm name and endpoint are required")
    if len(kind) > FARM_KIND_MAX_LEN:
        raise ValueError(f"farm kind must be {FARM_KIND_MAX_LEN} characters or fewer")
    if len(name) > FARM_NAME_MAX_LEN:
        raise ValueError(f"farm name must be {FARM_NAME_MAX_LEN} characters or fewer")
    reason = _endpoint_reason(endpoint)
    if reason:
        raise ValueError(reason)

    existing = db.query(models.ExternalFarmConnection).filter(
        models.ExternalFarmConnection.tenant_id == tenant_id,
        models.ExternalFarmConnection.name == name,
    ).first()
    if existing is not None:
        raise DuplicateFarmNameError(
            f"a farm named {name!r} is already registered for this tenant")

    row = models.ExternalFarmConnection(
        tenant_id=tenant_id, kind=kind, name=name, endpoint=endpoint)
    try:
        db.add(row)
        db.flush()
    except IntegrityError:
        # The DB-side CHECK / unique constraint caught what the pre-check missed
        # (a race, or a row added in the same transaction).
        db.rollback()
        raise DuplicateFarmNameError(
            f"a farm named {name!r} is already registered for this tenant") from None

    stored_path = None
    if credential:
        path = _farm_vault_path(tenant_id, row.id)
        token = _vault_token()
        if token is None:
            # No Vault auth at all (no agent token file, no usable AppRole login):
            # fail closed WITHOUT attempting an unauthenticated write.
            db.rollback()
            logger.warning("Farm %s could not be registered: no Vault auth available",
                           name)
            raise ExternalFarmVaultError(
                f"farm credential could not be stored for {name}") from None
        try:
            vault_client.write_kv_v2(path, token=token,
                                     data={"credential": credential})
        except VAULT_FAILURES:
            # Fail closed for EVERY Vault failure mode, not just the one vault_client
            # wraps: the flushed row above is still pending in this session, so it MUST
            # be rolled back here or a later commit by the caller would persist a farm
            # with vault_path NULL and no secret behind it. The Vault error text can
            # name the secret PATH; it never carries the payload, and we do not chain
            # it so nothing can.
            db.rollback()
            logger.warning("Farm %s could not be registered: Vault write failed",
                           name)
            raise ExternalFarmVaultError(
                f"farm credential could not be stored for {name}") from None
        stored_path = path
        row.vault_path = path

    try:
        db.commit()
    except IntegrityError:
        # Duplicate-name race: the secret was written but the row never lands, so
        # compensate by deleting it (best-effort) rather than leaving an orphan.
        db.rollback()
        if stored_path:
            _compensate_vault_delete(stored_path, name)
        raise DuplicateFarmNameError(
            f"a farm named {name!r} is already registered for this tenant") from None

    logger.info("Farm %s (%s) registered for tenant %s", name, kind, tenant_id)
    return row


def list_farms(db: Session, tenant_id: int) -> list:
    """Plain dicts for the page: id/kind/name/endpoint/is_active/has_credential.

    ``vault_path`` is deliberately NOT returned — the API never needs the path,
    and a boolean is enough for the UI's "credential stored" chip.
    """
    rows = db.query(models.ExternalFarmConnection).filter(
        models.ExternalFarmConnection.tenant_id == tenant_id,
    ).order_by(models.ExternalFarmConnection.id).all()
    return [{
        "id": r.id,
        "kind": r.kind,
        "name": r.name,
        "endpoint": r.endpoint,
        "is_active": bool(r.is_active),
        "has_credential": r.vault_path is not None,
        "created_at": r.created_at,
    } for r in rows]


def delete_farm(db: Session, tenant_id: int, farm_id: int) -> bool:
    """Drop a farm and its Vault secret. True = deleted, False = not found.

    A foreign tenant's id is not-found (False), exactly like a missing id.
    The ROW is deleted and committed FIRST, then the Vault secret is removed
    best-effort: an orphan Vault secret is safer than an orphan row whose
    vault_path points at a path that may already be gone (a row the UI would
    show as having a credential it can never use). A failed Vault cleanup is
    logged (name only) and swallowed — it never leaves a row behind.
    """
    row = db.query(models.ExternalFarmConnection).filter(
        models.ExternalFarmConnection.id == farm_id,
        models.ExternalFarmConnection.tenant_id == tenant_id,
    ).first()
    if row is None:
        return False

    # Captured BEFORE the delete: the ORM object is reset once the row is gone.
    path = row.vault_path
    name = row.name

    db.delete(row)
    db.commit()
    logger.info("Farm %s deleted for tenant %s", name, tenant_id)

    if path:
        try:
            vault_client.delete_kv_v2(path, token=_vault_token())
        except VAULT_FAILURES:
            logger.warning("Farm %s deleted but its Vault secret cleanup failed", name)
    return True
