# CORE/API/utils/storage.py
"""
Backend-agnostic storage configuration + client factory.
=======================================================

Single source of truth for the API's object-storage wiring:
- which backend is active (``STORAGE_BACKEND``),
- the internal + presigned (external) endpoints,
- the service-account credentials (loaded from Vault; root credentials are
  refused for CMMC/ISO 27001 compliance).

Two consumers:
- ``get_service_client()`` — a configured S3 client for **system** buckets
  (``platform-models``, ``worker-releases``, ``scim-archives``, wizard-ml
  scopes, ...). This replaces the ~17 ad-hoc ``Minio(root_creds)`` clients.
- ``S3Client`` (utils/s3_client.py; old name ``MinioClient`` kept as an alias) — tenant file ops; now a thin wrapper
  that sources its endpoint/creds/backend from here via ``get_service_client``
  and ``get_presigned_client``.

Backends are S3-compatible and interchangeable by configuration alone:
- ``minio``     — the AGPL MinIO server (legacy default, removed in Gate 6)
- ``versitygw`` — Apache-2.0 gateway (the target house default)

Both speak only the portable S3 data-plane, so this module NEVER uses a
MinIO-admin / STS / bucket-notification feature. Those couplings live in their
own modules and are retired gate by gate (STS → Gate 2, notifications → Gate 3,
metrics → Gate 4).
"""

import os
import logging
import threading
from dataclasses import dataclass
from typing import List, Optional

from minio import Minio

logger = logging.getLogger(__name__)

from services.debug_mode import is_debug_mode

# Backends whose client construction is identical (plain S3, static v4 creds).
# The selector exists so Gate 1 can flip the default and Gate 5 can resolve a
# per-tenant backend without touching call sites.
_SUPPORTED_BACKENDS = ("versitygw",)


def _register_settings() -> None:
    """Declare this module's tunables (register() no-ops on re-
    registration, so this is idempotent)."""
    from services import platform_settings as psr

    psr.register(
        "storage.byo_presign_proxy_host", str, "",
        group="storage",
        description="sz-mqa.3 — optional MOD-owned reverse-proxy host "
                     "(e.g. 'mod-s3-proxy:9000') that presigned (browser-"
                     "facing) URLs for BYO-S3 connections should point at, "
                     "instead of the tenant's raw endpoint. The internal "
                     "ops client always talks to the tenant's real "
                     "endpoint directly. Empty (default) = no proxy, "
                     "unchanged behaviour: the browser talks straight to "
                     "the tenant endpoint.",
    )


_register_settings()


def get_backend() -> str:
    """Active storage backend id. House default is ``versitygw`` (Gate 6.1 flip);
    ``minio`` server removed; versitygw is the sole backend."""
    backend = os.getenv("STORAGE_BACKEND", "versitygw").strip().lower()
    if backend not in _SUPPORTED_BACKENDS:
        logger.warning(
            "Unknown STORAGE_BACKEND=%r; falling back to 'versitygw'. Supported: %s",
            backend, _SUPPORTED_BACKENDS)
        backend = "versitygw"
    return backend


@dataclass(frozen=True)
class StorageConfig:
    """Resolved connection parameters for one storage backend."""
    backend: str
    internal_endpoint: str      # host:port for in-cluster ops (no scheme)
    external_endpoint: str      # host[:port] used to sign browser-facing URLs
    access_key: str
    secret_key: str
    internal_secure: bool       # TLS for internal_endpoint
    external_secure: bool       # TLS for external_endpoint (presigned)
    # Gate 5 (BYO-S3): when a tenant has an active StorageConnection, resolve_backend
    # returns a config with is_byo=True, the tenant's own endpoint on BOTH the
    # internal and external fields (the browser talks straight to the tenant's
    # endpoint), and the tenant's own bucket. house-default configs leave these
    # at the defaults (bucket=None → callers compute tenant-{id}-files).
    is_byo: bool = False
    bucket: Optional[str] = None
    region: Optional[str] = None


# ---------------------------------------------------------------------------
# Credential + endpoint resolution (factored out of MinioClient so there is
# exactly one place that reads Vault and the endpoint env vars).
# ---------------------------------------------------------------------------

# BUG-965: the loaded service account lives HERE (lock-guarded, in-process)
# instead of being written back into os.environ. A process-global env mutation
# from a request/runtime path leaks creds across tenants/threads, races between
# concurrent callers, and makes later env readers see a value they never
# configured. Env stays a READ-ONLY source: a pre-set MINIO_SERVICE_* (compose
# at startup / tests) still short-circuits the Vault load. Readers use
# get_service_credentials(); nothing writes these vars at runtime any more.
_SERVICE_CREDS_LOCK = threading.Lock()
_service_creds: Optional[tuple[str, str]] = None


def get_service_credentials() -> tuple[Optional[str], Optional[str]]:
    """The resolved service account, in-process (never logged, never in env).

    ``(None, None)`` until credentials have been resolved. This is the reader
    side of the BUG-965 fix: utils/storage_iam.py and any future consumer
    source the service creds from here rather than from os.environ.
    """
    with _SERVICE_CREDS_LOCK:
        return _service_creds if _service_creds else (None, None)


def _load_service_credentials() -> tuple[str, str]:
    """Load the storage service-account credentials from Vault. NO FALLBACK.

    vault-init stores these at secret/infrastructure/s3-service. Root
    credentials are explicitly refused. A pre-set MINIO_SERVICE_* env pair
    (startup export / tests / CE-without-Vault-Agent) short-circuits the
    Vault read — but only outside SaaS (``DEPLOYMENT_MODE=saas``), so a
    static credential can never silently stand in for Vault there (1677).
    Once loaded from Vault the pair is cached in-process (BUG-965: no
    os.environ mutation); ``reset()`` clears that cache so the next call
    re-reads Vault/env.
    """
    # Allow a pre-set service account (tests / already-exported env / CE
    # without a Vault Agent) to short-circuit — gated off in SaaS so this
    # can never substitute for Vault there even if the vars were ever set.
    access_key = os.getenv("MINIO_SERVICE_ACCESS_KEY")
    secret_key = os.getenv("MINIO_SERVICE_SECRET_KEY")
    _env_fallback_allowed = bool(
        access_key and secret_key
        and os.getenv("DEPLOYMENT_MODE", "onprem").lower() != "saas"
    )
    if _env_fallback_allowed:
        return access_key, secret_key

    try:
        from utils.vault_client import read_kv_v2
        vault_token_path = "/run/vault/token"
        if not os.path.exists(vault_token_path):
            # CE (no Vault Agent): if env creds exist, use them silently.
            if _env_fallback_allowed:
                logger.info(
                    "Vault Agent token not found (CE mode); using "
                    "MINIO_SERVICE env credentials")
                return access_key, secret_key
            raise RuntimeError(
                "Vault Agent token not found and no MINIO_SERVICE env "
                "credentials set — storage credentials unavailable")
        with open(vault_token_path) as f:
            vault_token = f.read().strip()
        if not vault_token:
            raise RuntimeError("Vault Agent token is empty")

        vault_path = "infrastructure/s3-service"
        vault_data = read_kv_v2(vault_path, vault_token)
        if not vault_data.get("access_key") or not vault_data.get("secret_key"):
            raise RuntimeError(
                "Storage service credentials missing in Vault "
                "(tried infrastructure/s3-service)")

        access_key = vault_data["access_key"]
        secret_key = vault_data["secret_key"]
        # BUG-965: cache in-process, do NOT mutate os.environ (see header note).
        global _service_creds
        with _SERVICE_CREDS_LOCK:
            _service_creds = (access_key, secret_key)
        logger.info("✓ Loaded storage service credentials from Vault")
    except Exception as e:
        logger.error("SECURITY: Cannot load storage service credentials from Vault: %s", e)
        logger.error("Ensure vault-init has run and stored the storage service account.")
        raise RuntimeError(f"Storage service credentials not available from Vault: {e}")

    # SECURITY: never fall back to root credentials.
    if os.getenv("S3_ROOT_USER") or os.getenv("MINIO_ROOT_USER") or \
       os.getenv("S3_ROOT_PASSWORD") or os.getenv("MINIO_ROOT_PASSWORD"):
        # Only a hard error if we somehow have no service creds; otherwise the
        # presence of root env vars is tolerated (compose still sets them for the
        # server itself) but never used to build a client here.
        if not access_key or not secret_key:
            raise RuntimeError(
                "SECURITY ERROR: root storage credentials detected but service "
                "account credentials are required (CMMC/ISO 27001).")
    return access_key, secret_key


def _is_loopback_host(value) -> bool:
    """True when a presign host value is only resolvable inside the manager box.

    BUG-WB-PREVIEW: dev-host values (``localhost``, ``s3.localhost``,
    ``127.x``, ``::1``) are not browser-facing, so they must never become the
    host a presigned URL is signed against. Semantics mirror
    ``routers/worker_tasks._is_loopback_endpoint`` (routers are never imported
    into utils/); handles optional ``scheme://``, ``host:port`` and bracketed
    IPv6. Never raises: unparseable input is not loopback.
    """
    try:
        host = (value or "").strip().lower()
        if not host:
            return False
        if "://" in host:
            host = host.split("://", 1)[-1]
        host = host.split("/", 1)[0]
        if host.startswith("["):
            end = host.find("]")
            host = host[1:end] if end != -1 else host[1:]
        elif host.count(":") > 1:
            pass
        else:
            host = host.split(":", 1)[0]
        if host in ("::1", "[::1]"):
            return True
        if host == "localhost" or host.endswith(".localhost"):
            return True
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except Exception:  # noqa: BLE001 — a weird host is never "loopback"
        return False


def _derive_presigned_host():
    """House-default browser-facing presign host, derived from the room domain.

    BUG-WB-PREVIEW: the presign host used to default to the literal dev value
    ``s3.localhost``, which no browser can reach — that is why Workflow Builder
    previews said "preview unavailable" and Dashboard Data tiles lost their
    thumbnails. The room already exports ``S3_SUBDOMAIN`` + ``BASE_DOMAIN``
    (domain.sh writes ``MINIO_PRESIGNED_ENDPOINT`` from exactly this pair), so
    the missing-var case is self-healing. Returns None when the pair is not
    configured or derives a loopback host — the caller then fails closed.
    Reads only names already in the tree; no new env var.
    """
    subdomain = (os.getenv("S3_SUBDOMAIN") or "s3").strip().lower()
    base_domain = (os.getenv("BASE_DOMAIN") or "").strip().lower()
    if not base_domain:
        return None
    host = f"{subdomain}.{base_domain}"
    if _is_loopback_host(host):
        return None
    return host


def _resolve_endpoints() -> tuple[str, str, bool, bool]:
    """Resolve (internal_endpoint, external_endpoint, internal_secure,
    external_secure) for the ACTIVE backend.

    The gateway path below is byte-for-byte the historical MinioClient
    logic. When ``STORAGE_BACKEND=versitygw`` (Gate 1), the internal endpoint
    resolves to the versitygw gateway instead — its own ``VGW_*`` / ``VERSITYGW_*``
    env with a ``versitygw:7070`` default — while the browser-facing presigned
    endpoint and TLS logic are shared (both backends speak the same portable S3
    data-plane, and the presigner still fronts the same nginx host until Gate 6).
    Credentials are unchanged: for Gate 1 the versitygw root account reuses the
    Vault ``infrastructure/s3-service`` account, so the loader needs no
    backend branch.
    """
    backend = get_backend()

    # Presigned/browser-facing endpoint: subdomain host, not the nginx /s3/ path
    # (the path rewrite breaks S3 signature verification). Shared across backends.
    external_secure = os.getenv("PROTOCOL", "https").lower() == "https"

    if backend == "versitygw":
        internal_url = os.getenv("VERSITYGW_INTERNAL_URL", "http://versitygw:7070")
        internal_endpoint = internal_url.split("://")[-1]
        internal_secure = internal_url.lower().startswith("https://")
        external_endpoint = os.getenv(
            "VERSITYGW_PRESIGNED_ENDPOINT",
            os.getenv(
                "MINIO_PRESIGNED_ENDPOINT",
                os.getenv("MINIO_ENDPOINT", "")))
        # BUG-WB-PREVIEW: the historical default was the literal dev host
        # "s3.localhost" — unreachable from any browser, so every <img> the
        # presigned host produced errored (WB "preview unavailable", blank
        # Dashboard thumbnails). A loopback value on the two browser-facing
        # presigned names is a dev host, not a configured one: drop it and
        # derive. An explicitly-set legacy MINIO_ENDPOINT keeps its historical
        # behaviour (test_storage_ce_creds.py pins that an explicit value still
        # resolves), so the loopback rejection is scoped to the presigned names
        # and the derived default.
        _legacy_minio_endpoint = (os.getenv("MINIO_ENDPOINT") or "").strip().lower()
        if _is_loopback_host(external_endpoint) and \
           external_endpoint.strip().lower() != _legacy_minio_endpoint:
            external_endpoint = ""
        if not external_endpoint.strip():
            external_endpoint = _derive_presigned_host()
        if not external_endpoint:
            raise RuntimeError(
                "SECURITY ERROR: no browser-facing presigned storage endpoint is "
                "configured (set VERSITYGW_PRESIGNED_ENDPOINT, or export S3_SUBDOMAIN "
                "and BASE_DOMAIN so the room domain can be derived); refusing to sign "
                "against a host the browser cannot reach (CMMC/ISO 27001).")
        logger.info("✓ STORAGE_BACKEND=versitygw; internal=%s presigned=%s",
                    internal_endpoint, external_endpoint)
        return internal_endpoint, external_endpoint, internal_secure, external_secure

# ---------------------------------------------------------------------------
# Cached config + clients
# ---------------------------------------------------------------------------

_config: Optional[StorageConfig] = None
_service_client: Optional[Minio] = None
_presigned_client: Optional[Minio] = None


def get_storage_config() -> StorageConfig:
    """The resolved house-default storage config (cached)."""
    global _config
    if _config is None:
        access_key, secret_key = _load_service_credentials()
        internal_ep, external_ep, internal_secure, external_secure = _resolve_endpoints()
        _config = StorageConfig(
            backend=get_backend(),
            internal_endpoint=internal_ep,
            external_endpoint=external_ep,
            access_key=access_key,
            secret_key=secret_key,
            internal_secure=internal_secure,
            external_secure=external_secure,
            # Pin the region so the SDK never issues a live GetBucketLocation when
            # signing a presigned URL. Without it, the presigned client does that
            # lookup against the EXTERNAL endpoint (e.g. dev-s3.<domain>), which
            # inside the cluster can resolve to a plain-HTTP listener while the
            # client uses TLS → SSL WRONG_VERSION_NUMBER and "S3 upload failed".
            # We know the region; there is no reason to discover it over the wire.
            region=os.getenv("VGW_REGION") or os.getenv("MINIO_REGION") or "us-east-1",
        )
        logger.info("Storage config resolved: backend=%s internal=%s external=%s",
                    _config.backend, _config.internal_endpoint, _config.external_endpoint)
    return _config


def get_service_client() -> Minio:
    """S3 client for SYSTEM-bucket operations, using the Vault service account.

    Use this instead of constructing ``Minio(...)`` with root credentials. The
    service account holds the MinIO ``readwrite`` policy (s3:* on all buckets),
    so it can create/use system buckets as well as tenant buckets.
    """
    global _service_client
    if _service_client is None:
        try:
            cfg = get_storage_config()
            endpoint, secure, region = cfg.internal_endpoint, cfg.internal_secure, cfg.region
            access_key, secret_key, backend = cfg.access_key, cfg.secret_key, cfg.backend
        except RuntimeError as e:
            # The service client only talks to the INTERNAL endpoint and never
            # signs a browser URL, so a missing browser-facing presign host (a
            # BASE_DOMAIN=localhost install) must not take system buckets down
            # with it (it 404'd the worker binary download route). Presigning
            # stays fail-closed: get_presigned_client still raises.
            if "presigned storage endpoint" not in str(e):
                raise
            access_key, secret_key = _load_service_credentials()
            internal_url = os.getenv("VERSITYGW_INTERNAL_URL", "http://versitygw:7070")
            endpoint = internal_url.split("://")[-1]
            secure = internal_url.lower().startswith("https://")
            region = os.getenv("VGW_REGION") or os.getenv("MINIO_REGION") or "us-east-1"
            backend = get_backend()
            logger.warning("Storage service client: no browser-facing presign host "
                           "configured; internal-only client for %s", endpoint)
        _service_client = Minio(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region,
        )
        logger.info("Storage service client initialised for %s (backend=%s)",
                    endpoint, backend)
    return _service_client


def get_presigned_client() -> Minio:
    """S3 client bound to the external endpoint, for signing browser-facing URLs."""
    global _presigned_client
    if _presigned_client is None:
        cfg = get_storage_config()
        _presigned_client = Minio(
            endpoint=cfg.external_endpoint,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secure=cfg.external_secure,
            region=cfg.region,
        )
    return _presigned_client


def build_byo_config(
    *, endpoint: str, region: str, bucket: str,
    access_key: str, secret_key: str, secure: bool, db=None,
) -> StorageConfig:
    """Build a StorageConfig for a tenant's BYO-S3 connection. By default the
    browser talks directly to the tenant endpoint, so the same host/creds
    serve both the internal ops client and the presigned (external) client.

    sz-mqa.3: when the ``storage.byo_presign_proxy_host`` platform setting is
    non-empty, presigned (browser-facing) URLs are built against that
    MOD-owned proxy host instead, while the internal ops client keeps talking
    to the tenant's real endpoint directly. Empty (default) = byte-identical
    to today's behaviour — no proxy service is required to exist yet."""
    from utils.storage_connection_test import normalize_endpoint
    from services import platform_settings as psr
    host, is_secure = normalize_endpoint(endpoint, secure)
    proxy_host = psr.get_setting("storage.byo_presign_proxy_host", db=db) or ""
    if proxy_host:
        external_endpoint = proxy_host
        external_secure = True
    else:
        external_endpoint = host
        external_secure = is_secure
    return StorageConfig(
        backend="s3",
        internal_endpoint=host,
        external_endpoint=external_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        internal_secure=is_secure,
        external_secure=external_secure,
        is_byo=True,
        bucket=bucket,
        region=region,
    )


def resolve_tenant_bucket(tenant_id: int, override: Optional[str] = None) -> str:
    """Single place the ``tenant-{id}-files`` fallback pattern lives.

    Returns the tenant's effective bucket name: the BYO override (a tenant-
    configured external S3 bucket, e.g. ``StorageConfig.bucket``) when one is
    given, otherwise the house default ``tenant-{tenant_id}-files``.
    """
    return override or f"tenant-{tenant_id}-files"


def resolve_backend(tenant_id: Optional[int] = None, db=None) -> StorageConfig:
    """Resolve the storage config for a tenant.

    If the tenant has an ACTIVE ``StorageConnection`` (BYO-S3), return a config
    bound to that endpoint/bucket with the secret decrypted fresh from Vault.
    Otherwise return the house default (versitygw/minio + ``tenant-{id}-files``).

    Never raises: any failure to resolve a BYO connection (no DB, Vault down,
    missing secret) falls back to the house default and is logged, so storage
    stays available rather than failing closed on a config lookup. Cross-tenant
    isolation is preserved — only ``tenant_id``'s own active connection is read.

    GY.V3a exception to "never raises": when the platform data-residency pin
    is set and the tenant's ACTIVE BYO connection maps outside the pin (or to
    an unknown zone), the resolver FAILS CLOSED — it raises
    ``utils.residency.ResidencyPinError`` instead of serving that store or
    silently falling back to another one. Pin off = byte-identical behaviour.
    """
    if tenant_id is None:
        return get_storage_config()

    from utils import residency

    owns_db = False
    try:
        if db is None:
            try:
                from database import _SessionLocal  # noqa: F401
                import database
                if database._SessionLocal is None:
                    return get_storage_config()
                db = database.SessionLocal()
                owns_db = True
            except Exception:
                return get_storage_config()

        from crud import storage_connection_cud as sc_crud
        conn = sc_crud.get_active_connection(db, tenant_id=tenant_id)
        if conn is None:
            return get_storage_config()

        # GY.V3a — use-time residency gate: an active BYO connection outside
        # the platform pin (or mapping to an unknown zone) is refused FAIL
        # CLOSED — the error propagates to the caller; this resolver never
        # silently serves another store for a pinned tenant. Pin off =>
        # residency_violation returns None and the rest is unchanged.
        viol = residency.residency_violation(db, conn.region, conn.endpoint)
        if viol is not None:
            raise residency.ResidencyPinError(
                f"Data-residency pin '{viol['pin']}' violated at use time: "
                f"tenant {tenant_id} active storage connection {conn.id} "
                f"maps to zone '{viol['zone']}' "
                f"(region '{viol['region'] or '<empty>'}'); refusing to "
                f"serve a store outside the pinned region (fail closed, "
                f"no fallback).",
                pin=viol["pin"], zone=viol["zone"], region=viol["region"],
            )

        secret = sc_crud.get_connection_secret(db, conn)
        if not secret:
            logger.warning(
                "Tenant %s active StorageConnection %d has no resolvable secret; "
                "falling back to house default.", tenant_id, conn.id)
            return get_storage_config()

        cfg = build_byo_config(
            endpoint=conn.endpoint, region=conn.region, bucket=conn.bucket,
            access_key=conn.access_key, secret_key=secret, secure=conn.secure,
            db=db,
        )
        logger.info(
            "resolve_backend(tenant=%s): BYO-S3 endpoint=%s bucket=%s",
            tenant_id, cfg.internal_endpoint, cfg.bucket)
        return cfg
    except residency.ResidencyPinError:
        # GY.V3a — residency refusals propagate (fail closed); they are the
        # one case where "use the house default" would silently move data.
        raise
    except Exception as exc:  # noqa: BLE001 — never fail closed on resolution
        logger.warning(
            "resolve_backend(tenant=%s): BYO resolution failed (%s); using house default.",
            tenant_id, exc)
        return get_storage_config()
    finally:
        if owns_db and db is not None:
            try:
                db.close()
            except Exception:
                pass


def build_client_for_config(cfg: StorageConfig, presigned: bool = False) -> Minio:
    """Construct a Minio client bound to a specific (possibly BYO) config. Used by
    MinioClient to route a tenant's file ops to their own endpoint."""
    endpoint = cfg.external_endpoint if presigned else cfg.internal_endpoint
    secure = cfg.external_secure if presigned else cfg.internal_secure
    return Minio(
        endpoint=endpoint,
        access_key=cfg.access_key,
        secret_key=cfg.secret_key,
        secure=secure,
        region=cfg.region or None,
    )


def byo_holds_objects(cfg: StorageConfig, *, bucket: Optional[str] = None,
                      prefix: Optional[str] = None) -> bool:
    """True when a BYO (``cfg.is_byo``) bucket still holds at least one object.

    BYO-DEACTIVATE-BLOCK — the data-safety check behind
    ``POST /storage-connections/{id}/deactivate``: a tenant's own store must not
    be deactivated while it still holds objects, because falling back to the
    house default would orphan (hide) them. This is a data-safety check, NOT a
    quantity cap or a licence gate.

    ONE-LIST-PAGE bound (BYO-REVIEW-CHANGES, docstring matches the wire): minio's
    ``list_objects`` is a lazy generator over S3 LIST PAGES, and one page carries
    up to 1000 keys. So ``islice(..., 1)`` stops after the FIRST PAGE — exactly
    one request, never a full walk of a large bucket — but that one request can
    materialise up to 1000 keys, not one key. ``prefix`` is an optional key
    prefix inside the bucket (default: the whole bucket, which is where BYO
    objects live — the migrate-on-activate copy writes content-hash keys at the
    bucket root). ``include_version=False`` is passed explicitly: only CURRENT
    object versions count, so a versioned bucket whose only entries are superseded
    old versions reads as empty (documented, matches minio's own default).

    Raises on any backend error (unreachable endpoint, missing bucket, auth):
    callers decide whether to fail open or closed. Never logs creds/keys.
    """
    from itertools import islice

    if not cfg.is_byo:
        raise ValueError(
            "byo_holds_objects is BYO-only; platform/house storage is never "
            "listed here (it is MOD-owned and deactivation does not orphan it).")
    target_bucket = bucket or cfg.bucket
    if not target_bucket:
        raise ValueError("BYO config carries no bucket")

    client = build_client_for_config(cfg)
    for _ in islice(client.list_objects(target_bucket, prefix=prefix,
                                        recursive=True,
                                        include_version=False), 1):
        return True
    return False


def byo_list_keys(cfg: StorageConfig, *, bucket: Optional[str] = None,
                  prefix: Optional[str] = None) -> List[str]:
    """BYO move-out (IMPL-BYO-MOVE-OUT-A-API, design s5 CONCERN A) — the
    FULL physical key list of a BYO bucket, the LIST-driven move-out
    manifest.

    ``byo_holds_objects`` deliberately stops at one LIST page (it only needs
    a bool); a move-out needs the actual set, so this walks every page
    (minio's ``list_objects`` is a lazy page generator) and returns the
    keys. The move-out manifest and the deactivate guard must measure the
    SAME set or the 409 could never clear on a shared bucket — that is why
    the move lists the bucket instead of reusing the content_blobs
    manifest. Foreign keys (files the customer owns that MOD never knew)
    are included: they are the tenant's own data and they move with it.

    BYO-only like its sibling: platform/house storage is never listed here.
    Raises on any backend error (unreachable endpoint, missing bucket,
    auth) — callers decide whether to fail open or closed. Never logs
    creds/keys.
    """
    if not cfg.is_byo:
        raise ValueError(
            "byo_list_keys is BYO-only; platform/house storage is never "
            "listed here (it is MOD-owned).")
    target_bucket = bucket or cfg.bucket
    if not target_bucket:
        raise ValueError("BYO config carries no bucket")

    client = build_client_for_config(cfg)
    keys: List[str] = []
    for obj in client.list_objects(target_bucket, prefix=prefix,
                                   recursive=True, include_version=False):
        name = getattr(obj, "object_name", None) or getattr(obj, "key", None)
        if name:
            keys.append(str(name))
    return keys


class ByoKeyCheckUnavailable(Exception):
    """FIX-BYO-MOD-KEYS-PRESENT-FAIL-CLOSED: the MOD-key walk REACHED the
    BYO source but could not CONFIRM a key left it (transport error, 5xx,
    AccessDenied, timeout, ...). Raised ONLY by ``byo_mod_keys_present``
    (never by the whole-bucket LIST check), so a caller can tell "the key
    check ran and errored" (fail CLOSED: nothing was changed, retry) from
    "the store could not be resolved/built at all" (the documented fail-open
    trade). The original backend error is the ``__cause__``; this wrapper
    carries no endpoint, no key and no credential.
    """


def byo_mod_keys_present(cfg: StorageConfig, keys: List[str],
                         *, bucket: Optional[str] = None) -> bool:
    """BYO move-out owner default (design s5 CONCERN A / OWNER CALL): the
    MOD-key-scoped deactivate guard.

    After a VERIFIED move-out the guard asks only whether any MOD content-
    key is still physically at the source — the same per-key walk
    ``verify_migration`` already does (Go ``Stat``, ``storage_migration.go:
    127``), here one ``stat_object`` per manifest key. The customer's own
    files (foreign keys MOD never knew) become informational: they cannot
    be moved by a manifest MOD controls, and they stay in the customer's
    bucket, unreachable from MOD, until the customer deletes them (soft
    delete only — this helper never deletes anything).

    A key that is absent at the source counts as moved (the move copied it
    into MOD storage); a key still present re-blocks the deactivate with
    the same 409 (a concurrent upload during the move — design s5: "add a
    file during the move and MOD will ask again before switching").

    FAIL CLOSED on a backend error (FIX-BYO-MOD-KEYS-PRESENT-FAIL-CLOSED,
    owner rule "MOD never deletes customer data"): ONLY the not-found class
    counts as "moved" — a ``minio.error.S3Error`` whose ``.code`` is
    ``NoSuchKey`` or ``NoSuchObject`` (what the real client raises for a 404
    on a stat: ``minio/api.py:383`` maps 404+object_name to NoSuchKey).
    EVERY other error (transport, timeout, 5xx, AccessDenied, a bucket that
    vanished mid-walk, ...) raises
    ``ByoKeyCheckUnavailable`` (the original error as ``__cause__``): an
    unconfirmed key is NOT a moved key, so a caller must never clear the
    deactivate guard on it. Callers that resolve the store BEFORE the walk
    (``resolve_backend`` falling back to the house default) keep their own
    documented fail-open trade — that class is not raised here, only
    ``ByoKeyCheckUnavailable`` is, and it means "the walk ran".
    """
    from minio.error import S3Error

    if not cfg.is_byo:
        raise ValueError(
            "byo_mod_keys_present is BYO-only; platform/house storage is "
            "never inspected here.")
    target_bucket = bucket or cfg.bucket
    if not target_bucket:
        raise ValueError("BYO config carries no bucket")
    if not keys:
        return False

    client = build_client_for_config(cfg)
    for key in keys:
        try:
            client.stat_object(target_bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                continue  # absent at the source: this key is "moved"
            # FIX-BYO-MOD-KEYS-PRESENT-FAIL-CLOSED: a non-not-found S3Error
            # (AccessDenied, NoSuchBucket mid-walk, 5xx, ...) says nothing
            # about this key: fail CLOSED, never "moved".
            raise ByoKeyCheckUnavailable(
                f"BYO MOD-key check reached the source but could not confirm "
                f"its keys (S3 code {exc.code!r})") from exc
        except Exception as exc:  # noqa: BLE001 — transport/timeout/5xx
            raise ByoKeyCheckUnavailable(
                "BYO MOD-key check reached the source but could not confirm "
                "its keys (backend error)") from exc
        return True
    return False


def build_external_s3_client(endpoint: str, access_key: str, secret_key: str,
                             secure: bool, region: Optional[str] = None) -> Minio:
    """BUG-950: the ONE factory for CUSTOMER-SUPPLIED external S3 endpoints.

    Off-box backup destinations (services/backup_transports.py) and log-export
    sinks (services/log_stream_senders/s3_export.py) point at an endpoint with
    the customer's own creds — never the house service creds — but they must
    still obey the single-construction rule (tests/test_no_root_cred_storage_clients.py
    allows ``Minio(`` only in this module) and the BUG-860d rule that every
    client gets the bounded http pool from utils.s3_client._build_http_pool
    (imported, never copied). Behaviour (endpoint/secure/region/creds) is
    exactly what the constructor produced; creds/endpoint are never logged.
    The minio import is lazy so a test that swaps sys.modules["minio"] for a
    recorder still sees exactly one construction, and _bound_client (imported,
    never copied) is what BUG-860d uses to give any client the bounded pool.
    """
    from utils.s3_client import _bound_client
    from minio import Minio
    return _bound_client(Minio(endpoint=endpoint, access_key=access_key,
                               secret_key=secret_key, secure=secure,
                               region=region))


def reset() -> None:
    """Clear cached config + clients so the next call re-reads Vault/env.

    Called on SignatureDoesNotMatch recovery (see MinioClient.reinitialize).
    """
    global _config, _service_client, _presigned_client, _service_creds
    _config = None
    _service_client = None
    _presigned_client = None
    # BUG-965: the in-process service-creds cache is part of the same "re-read
    # Vault/env" contract — clearing it lets the next load consult env again.
    _service_creds = None
