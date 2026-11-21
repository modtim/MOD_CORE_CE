# CORE/API/utils/storage_iam.py
"""
versitygw IAM minting — tenant-scoped storage credentials (Gate 2).
==================================================================

versitygw has **no STS / AssumeRole**. Tenant isolation is achieved by minting a
*distinct versitygw IAM account per tenant that OWNS that tenant's bucket*, and
relying on versitygw's bucket-ownership enforcement to deny every other bucket.

Why ownership (and not policy / ACL / project-group)
----------------------------------------------------
Empirically verified on the pinned image ``ghcr.io/versity/versitygw:v1.7.0``
(posix backend, internal file-dir IAM) — see
``tests/MANUAL_TEST_ISOLATION.md`` and ``tests/test_versitygw_isolation.py``:

- A ``user``-role account can read/write ONLY the buckets it **owns**.
- A bucket **policy** granting a non-owner principal Allow does **NOT** grant
  access (denied anyway).
- A shared **project-id / group-id** does **NOT** grant access.
- **ACLs** are disabled by the default ``BucketOwnerEnforced`` object-ownership
  and error out when enabling on the posix backend.

So ownership is the single enforced lever, which makes cross-tenant access
**deny-by-default and un-overridable** — a strong isolation property. The
minting shape below is therefore ownership-based.

Lifecycle / "short-lived"
-------------------------
versitygw IAM keys have **no TTL**. Because ownership is exclusive (one owner per
bucket), a *truly per-task ephemeral* account is not viable — it would require
transferring bucket ownership to the fresh account for the task and back, which
races under concurrent tasks for the same tenant. Instead:

- The API mints/ensures a **stable per-tenant account** (idempotent) that owns
  ``tenant-{id}-files``; the per-task worker credential is this tenant account's
  key (tenant-scoped, exactly like the STS creds were, minus the expiry token).
- **Revocation** = delete/rotate the tenant account via :func:`revoke_tenant_account`
  / :func:`rotate_tenant_secret`, bounded by versitygw's IAM cache TTL
  (``VGW_IAM_CACHE_*``) — tune that for prompt revocation.

Production wiring uses the **Vault IAM backend** (``--iam-vault-*``), matching the
Fernet+Vault secret convention; this module is backend-agnostic — it talks only
to versitygw's admin HTTP API, which is identical across IAM backends. The proof
harness (``scripts/versitygw-gate2.compose.yml``) uses the internal file-dir IAM
backend because it demonstrates the same enforcement fastest.

This module is INERT unless explicitly called: it does not run at import, and the
default task-credential path remains STS (Gate 2 keeps STS the default until the
negative isolation test 2.5 gates its removal).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import requests
from minio.credentials import Credentials
from minio.signer import sign_v4_s3

logger = logging.getLogger(__name__)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# versitygw admin roles
ROLE_USER = "user"          # can read/write only OWNED buckets
ROLE_USERPLUS = "userplus"  # + self-service bucket create
ROLE_ADMIN = "admin"        # all buckets — never handed to a tenant


def tenant_bucket(tenant_id: int) -> str:
    """The canonical per-tenant bucket name (matches the MinIO layout)."""
    return f"tenant-{tenant_id}-files"


def tenant_access_key(tenant_id: int) -> str:
    """Stable, deterministic access-key id for a tenant's owner account."""
    return f"tenant-{tenant_id}"


@dataclass(frozen=True)
class MintedCredentials:
    """A tenant-scoped versitygw credential bundle handed to the worker."""
    tenant_id: int
    access_key: str
    secret_key: str
    bucket: str
    endpoint: str          # host:port for in-cluster S3 ops (no scheme)
    secure: bool


class VersitygwAdminError(RuntimeError):
    """Raised when a versitygw admin API call fails."""


class ByoTenantMintError(RuntimeError):
    """Raised when a tenant whose effective storage backend is BYO-S3 is
    passed to a mint function. The worker credential for such a tenant is
    the tenant's OWN S3 key (issued via /workers/tasks/{id}/credentials),
    never a minted versitygw IAM account — minting one here would be dead
    weight at best (versitygw is not the tenant's storage) and a
    cross-backend misroute at worst. Callers are expected to pre-gate on
    ``resolve_backend(tenant_id).is_byo``; this guard makes the contract
    fail loud instead of silently minting into the wrong backend."""


class VersitygwAdminClient:
    """Thin client for the versitygw admin HTTP API (SigV4-signed, service=s3).

    Endpoints/shapes were captured from ``versitygw admin`` v1.7.0:
      - create-user:         PATCH /create-user            (XML <Account> body)
      - delete-user:         PATCH /delete-user?access=..
      - list-users:          PATCH /list-users
      - create-bucket:       PATCH /{bucket}/create        (X-Vgw-Owner header)
      - change-bucket-owner: PATCH /change-bucket-owner/?bucket=..&owner=..
    """

    def __init__(
        self,
        admin_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: Optional[str] = None,
        timeout: int = 15,
    ):
        self.admin_url = (
            admin_url or os.getenv("VERSITYGW_ADMIN_URL", "http://versitygw:7071")
        ).rstrip("/")
        # Admin (root) credentials for versitygw. In production these are the
        # versitygw root account (Vault); reuse the storage service creds by
        # default so no new secret is required for single-tenant proofs.
        # BUG-965: source the service pair from utils.storage's in-process cache
        # (it used to be written into os.environ at runtime, which leaked creds
        # across tenants/threads). Env stays only as the startup fallback for
        # callers that configured MINIO_SERVICE_* themselves (compose/tests).
        svc_access, svc_secret = (None, None)
        try:
            from utils import storage as _storage
            svc_access, svc_secret = _storage.get_service_credentials()
        except Exception:  # noqa: BLE001 — storage may be unresolvable here
            pass
        self.access_key = (
            access_key
            or os.getenv("VERSITYGW_ROOT_ACCESS_KEY")
            or svc_access
            or os.getenv("MINIO_SERVICE_ACCESS_KEY")
        )
        self.secret_key = (
            secret_key
            or os.getenv("VERSITYGW_ROOT_SECRET_KEY")
            or svc_secret
            or os.getenv("MINIO_SERVICE_SECRET_KEY")
        )
        if not self.access_key or not self.secret_key:
            raise VersitygwAdminError(
                "versitygw admin credentials not configured "
                "(VERSITYGW_ROOT_ACCESS_KEY / VERSITYGW_ROOT_SECRET_KEY)")
        self.region = region or os.getenv("VGW_REGION", "us-east-1")
        self.timeout = timeout

    # -- signing ---------------------------------------------------------
    def _request(self, path: str, body: bytes = b"") -> requests.Response:
        """Sign + send one admin request. All admin calls are HTTP PATCH."""
        url = f"{self.admin_url}{path}"
        split = urlsplit(url)
        host = split.netloc
        body_sha = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256
        now = datetime.now(timezone.utc)
        headers = {
            "Host": host,
            "x-amz-content-sha256": body_sha,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
        }
        signed = sign_v4_s3(
            method="PATCH",
            url=split,
            region=self.region,
            headers=headers,
            credentials=Credentials(self.access_key, self.secret_key),
            content_sha256=body_sha,
            date=now,
        )
        resp = requests.request(
            "PATCH", url, headers=signed, data=body, timeout=self.timeout)
        return resp

    @staticmethod
    def _ok(resp: requests.Response, action: str, allow_status=(200, 201, 204)):
        if resp.status_code not in allow_status:
            raise VersitygwAdminError(
                f"versitygw admin {action} failed: "
                f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp

    # -- user ops --------------------------------------------------------
    def create_user(self, access: str, secret: str, role: str = ROLE_USER,
                     user_id: int = 0, group_id: int = 0, project_id: int = 0):
        body = (
            f"<Account><Access>{access}</Access><Secret>{secret}</Secret>"
            f"<Role>{role}</Role><UserID>{user_id}</UserID>"
            f"<GroupID>{group_id}</GroupID><ProjectID>{project_id}</ProjectID>"
            f"</Account>"
        ).encode()
        resp = self._request("/create-user", body)
        # Already-exists surfaces as a 4xx; the caller decides idempotency.
        return resp

    def delete_user(self, access: str):
        resp = self._request(f"/delete-user?access={access}")
        return self._ok(resp, f"delete-user {access}", allow_status=(200, 204))

    def list_users(self) -> str:
        resp = self._request("/list-users")
        return self._ok(resp, "list-users").text

    # -- bucket ops ------------------------------------------------------
    def create_bucket(self, bucket: str, owner: str) -> requests.Response:
        url = f"{self.admin_url}/{bucket}/create"
        split = urlsplit(url)
        now = datetime.now(timezone.utc)
        headers = {
            "Host": split.netloc,
            "x-amz-content-sha256": _EMPTY_SHA256,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "X-Vgw-Owner": owner,
        }
        signed = sign_v4_s3(
            method="PATCH", url=split, region=self.region, headers=headers,
            credentials=Credentials(self.access_key, self.secret_key),
            content_sha256=_EMPTY_SHA256, date=now,
        )
        return requests.request(
            "PATCH", url, headers=signed, timeout=self.timeout)

    def change_bucket_owner(self, bucket: str, owner: str):
        resp = self._request(
            f"/change-bucket-owner/?bucket={bucket}&owner={owner}")
        return self._ok(resp, f"change-bucket-owner {bucket}->{owner}")


# ---------------------------------------------------------------------------
# High-level tenant lifecycle
# ---------------------------------------------------------------------------

def _endpoint_and_secure() -> tuple[str, bool]:
    internal = os.getenv("VERSITYGW_INTERNAL_URL", "http://versitygw:7070")
    return internal.split("://")[-1], internal.lower().startswith("https://")


def _load_or_make_secret(tenant_id: int, provided: Optional[str]) -> str:
    """Resolve the tenant account's secret.

    Priority: explicit arg > env override (tests) > a freshly generated secret.
    In production this must be persisted via the Secret/Vault convention so the
    stable per-tenant account keeps a stable key across API restarts; that
    persistence is wired where the credential path calls this (Phase B),
    intentionally NOT here, so this helper stays free of DB/Vault side effects
    and callable from a test.
    """
    if provided:
        return provided
    env = os.getenv(f"VERSITYGW_TENANT_{tenant_id}_SECRET")
    if env:
        return env
    return secrets.token_urlsafe(24)


def _ensure_house_backend(tenant_id: int, db=None) -> None:
    """BYO guard (WS4.Q17.15): versitygw IAM minting is a HOUSE-DEFAULT-only
    operation. A tenant whose effective storage backend is an ACTIVE BYO-S3
    StorageConnection gets its worker credential as the tenant's OWN S3 key
    via /workers/tasks/{id}/credentials (see ``resolve_backend(...).is_byo``
    and the BYO branch in routers/worker_tasks.py) — a minted versitygw IAM
    account for that tenant is useless (versitygw is not that tenant's
    storage) and would be dead weight. Callers are expected to pre-gate on
    ``resolve_backend(tenant_id).is_byo`` (the /credentials endpoint does,
    before it ever reaches here); this guard makes the contract fail loud if
    a BYO tenant slips through, rather than silently minting into the wrong
    backend.

    ``db`` is optional: pass the caller's session when it is already open
    (tests reuse the fixture session); when omitted the guard resolves with
    its own short-lived session and closes it, so the request-path backend
    is never held (D4: no external call while a pooled backend is in a
    transaction). Resolution follows ``resolve_backend``'s never-raise
    contract — a resolution error is logged and treated as house-default.
    """
    try:
        from utils import storage as _storage
        cfg = _storage.resolve_backend(tenant_id, db)
    except Exception as exc:  # noqa: BLE001 — never let the guard itself break minting
        logger.warning(
            "storage_iam BYO guard: resolve_backend(%s) failed (%s); "
            "proceeding on house-default assumption (callers pre-gate)",
            tenant_id, exc)
        return
    if getattr(cfg, "is_byo", False):
        raise ByoTenantMintError(
            f"tenant {tenant_id} has an ACTIVE BYO-S3 connection: its worker "
            "credential must be the tenant's own S3 key (issued via "
            "/workers/tasks/{id}/credentials), never a minted versitygw IAM "
            "account")


def mint_tenant_account(
    tenant_id: int,
    secret: Optional[str] = None,
    admin: Optional[VersitygwAdminClient] = None,
    role: str = ROLE_USER,
    bucket: Optional[str] = None,
    access_key: Optional[str] = None,
    db=None,
) -> MintedCredentials:
    """Idempotently ensure a scoped versitygw owner account + bucket.

    - creates account ``access_key`` (default ``tenant-{id}``, role ``user``)
      if absent,
    - ensures ``bucket`` (default ``tenant-{id}-files``) exists and is OWNED
      by that account,
    and returns the scoped credential bundle. Ownership is what enforces
    cross-account deny (see module docstring).

    ``bucket``/``access_key`` overrides (GC.PFM.7c) let a caller reuse this
    same scoped-minting primitive for a *non-tenant* bucket (e.g. the shared
    platform ``platform-calibration-data`` fixture bucket) by minting a
    DEDICATED account for a distinct ``access_key`` that owns ONLY the
    overridden ``bucket`` — never the real ``tenant-{id}-files`` bucket, so
    the returned credential can't reach anything else. Pass a reserved
    sentinel ``tenant_id`` (not a real tenant row) together with both
    overrides so the account/secret namespace can't collide with any real
    tenant's own account.

    Idempotent: re-minting an existing account returns creds for the same account.
    The caller owns secret persistence (Vault) — see :func:`_load_or_make_secret`.

    Raises :class:`ByoTenantMintError` if the tenant's effective storage backend
    is BYO-S3 (WS4.Q17.15 — minting is house-default only).
    """
    # WS4.Q17.15 — house-default only: never mint for a BYO-S3 tenant.
    _ensure_house_backend(tenant_id, db)
    admin = admin or VersitygwAdminClient()
    access = access_key or tenant_access_key(tenant_id)
    bucket = bucket or tenant_bucket(tenant_id)
    sec = _load_or_make_secret(tenant_id, secret)

    # create-user (idempotent: tolerate already-exists)
    resp = admin.create_user(access, sec, role=role, user_id=tenant_id)
    if resp.status_code not in (200, 201, 204):
        txt = resp.text.lower()
        if "exist" not in txt and resp.status_code != 409:
            raise VersitygwAdminError(
                f"create-user tenant {tenant_id} failed: "
                f"HTTP {resp.status_code}: {resp.text[:300]}")
        logger.info("versitygw account %s already exists; reusing", access)

    # ensure the bucket exists and is owned by the tenant account
    cresp = admin.create_bucket(bucket, owner=access)
    if cresp.status_code not in (200, 201, 204):
        txt = cresp.text.lower()
        if "exist" in txt or cresp.status_code == 409:
            # bucket exists — make sure the tenant account owns it
            admin.change_bucket_owner(bucket, owner=access)
        else:
            raise VersitygwAdminError(
                f"create-bucket {bucket} failed: "
                f"HTTP {cresp.status_code}: {cresp.text[:300]}")

    endpoint, secure = _endpoint_and_secure()
    logger.info("Minted versitygw tenant creds: tenant=%s account=%s bucket=%s",
                tenant_id, access, bucket)
    return MintedCredentials(
        tenant_id=tenant_id, access_key=access, secret_key=sec,
        bucket=bucket, endpoint=endpoint, secure=secure)


def revoke_tenant_account(
    tenant_id: int, admin: Optional[VersitygwAdminClient] = None
) -> None:
    """Delete a tenant's versitygw account (offboarding / hard revocation).

    Revocation takes effect after versitygw's IAM cache TTL. NOTE this leaves
    ``tenant-{id}-files`` owner-less; a subsequent :func:`mint_tenant_account`
    re-owns it. Callers that only want to rotate a leaked key should use
    :func:`rotate_tenant_secret` instead so the bucket keeps an owner.
    """
    admin = admin or VersitygwAdminClient()
    admin.delete_user(tenant_access_key(tenant_id))
    logger.info("Revoked versitygw account for tenant=%s", tenant_id)


def rotate_tenant_secret(
    tenant_id: int, new_secret: Optional[str] = None,
    admin: Optional[VersitygwAdminClient] = None, db=None,
) -> MintedCredentials:
    """Rotate the tenant account's secret without changing ownership.

    Implemented as delete+recreate of the account, then re-own the bucket — the
    bucket briefly has no valid credential holder but is never re-owned by
    anyone else, so isolation is preserved throughout. Bounded by IAM cache TTL.

    Raises :class:`ByoTenantMintError` for BYO-S3 tenants (the recreate step is
    a mint — same house-default-only contract as :func:`mint_tenant_account`).
    """
    # WS4.Q17.15 — fail before touching versitygw: rotation re-mints.
    _ensure_house_backend(tenant_id, db)
    admin = admin or VersitygwAdminClient()
    try:
        admin.delete_user(tenant_access_key(tenant_id))
    except VersitygwAdminError:
        pass  # absent is fine; mint will create it
    return mint_tenant_account(tenant_id, secret=new_secret, admin=admin)
