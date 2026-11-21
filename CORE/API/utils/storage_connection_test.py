"""Connection test for BYO-S3 storage connections (Gate 5, task 5.4).

Validates a tenant's S3-compatible connection end to end:
  1. reachability  — can we open a connection to the endpoint?
  2. auth          — do the access/secret keys sign correctly?
  3. bucket        — does the named bucket exist and is it accessible?
  4. round-trip    — put / get (byte-compare) / delete a small probe object.

Returns ``(ok, reason)``. A connection SHALL NOT activate until this passes
(enforced by the router). Uses only the portable S3 data-plane (the ``minio``
SDK is Apache-2.0 and backend-agnostic), so it works against versitygw, AWS S3,
or any S3-compatible endpoint.

GSEC R-066 (SSRF): the endpoint is tenant-supplied, so before any Minio
put/get/delete we resolve it and refuse private/internal targets (cloud
metadata, loopback, RFC1918, link-local, CGNAT/tailnet, multicast, reserved).
Self-host / CE (DEPLOYMENT_MODE != saas) is single-tenant with admin-created
connections, so a LAN MinIO/NAS is allowed by default there (owner 11-09); an
operator can still set ``storage.byo_allow_private_endpoints`` to False in the
DB to re-arm the guard. SaaS keeps the guard on unless the setting is flipped.
The metadata range and metadata hostnames stay blocked in every mode. The blocked-network list is the
shared one in ``services/app_routing.py`` (SSRF fence §7.5).
"""
import io
import ipaddress
import logging
import secrets as _secrets
import socket

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)

# Auth-class S3 error codes (wrong key / denied) vs. everything else.
_AUTH_CODES = {
    "SignatureDoesNotMatch", "InvalidAccessKeyId", "AccessDenied",
    "InvalidSecurity", "AuthorizationHeaderMalformed",
}
_NO_BUCKET_CODES = {"NoSuchBucket", "NoSuchBucketPolicy"}

CORS_NOTE = (
    "For direct browser upload/download, the endpoint must allow CORS from the "
    "Dashboard origin (PUT/GET/HEAD, Authorization + content headers). If the "
    "browser is rejected, uploads fail visibly — configure CORS on the bucket."
)

# GSEC R-066 — always-blocked regardless of the allow-private setting.
ALLOW_PRIVATE_SETTING_KEY = "storage.byo_allow_private_endpoints"
_METADATA_NET = ipaddress.ip_network("169.254.0.0/16")  # cloud metadata / link-local
_METADATA_HOSTNAMES = {"localhost", "metadata.google.internal"}
_DENY_REASON = "endpoint resolves to a private/internal address — not allowed"


def normalize_endpoint(endpoint: str, secure: bool) -> tuple[str, bool]:
    """Return (host[:port], secure) from a stored endpoint that may or may not
    carry a scheme. An explicit scheme wins over the ``secure`` flag."""
    ep = (endpoint or "").strip()
    if ep.startswith("https://"):
        return ep[len("https://"):].rstrip("/"), True
    if ep.startswith("http://"):
        return ep[len("http://"):].rstrip("/"), False
    return ep.rstrip("/"), bool(secure)


def allow_private_endpoints(db=None) -> bool:
    """Effective value of ``storage.byo_allow_private_endpoints``.

    SaaS: the setting as stored (registered default False). Self-host/CE: an
    EXPLICIT stored value still wins (so an operator can turn the guard back
    on), but with nothing stored the default is True."""
    from services import platform_settings as psr
    from utils.entitlements import is_saas
    if is_saas():
        return bool(psr.get_setting(ALLOW_PRIVATE_SETTING_KEY, db=db))
    owns = db is None
    if owns:
        from database import SessionLocal
        db = SessionLocal()
    try:
        value, source = psr.resolve_source(ALLOW_PRIVATE_SETTING_KEY, db)
    finally:
        if owns:
            db.close()
    return True if source == "default" else bool(value)


def check_endpoint_allowed(endpoint: str, secure: bool, db=None) -> tuple[bool, str]:
    """GSEC R-066 SSRF gate for a tenant-supplied BYO-S3 endpoint.

    Returns (ok, reason). Resolves ALL addresses of the host (so DNS names
    pointing at private IPs are caught) and blocks any address in the shared
    private/loopback/link-local/CGNAT/reserved/multicast/unspecified ranges
    (services/app_routing.py). Hostnames ``localhost`` and
    ``metadata.google.internal`` are always blocked. When private endpoints are
    allowed (``allow_private_endpoints``: default on self-host/CE, off on SaaS
    unless ``storage.byo_allow_private_endpoints`` is True), the general
    private check is skipped but 169.254.0.0/16 + metadata hostnames remain
    blocked.
    """
    from services import app_routing

    host, is_secure = normalize_endpoint(endpoint, secure)
    if not host:
        return False, "endpoint is required"

    # Strip the port; a bracketed IPv6 literal keeps its address part.
    if host.startswith("["):
        hostpart, _, portpart = host.partition("]")
        host = hostpart[1:]
        port = portpart.lstrip(":")
    else:
        host, _, port = host.partition(":")
    host = host.rstrip(".")
    if not host:
        return False, "endpoint must have a host"
    if host.lower() in _METADATA_HOSTNAMES:
        return False, _DENY_REASON

    allow_private = allow_private_endpoints(db)
    try:
        probe_port = int(port) if port.isdigit() else (443 if is_secure else 80)
        infos = socket.getaddrinfo(host, probe_port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"endpoint host {host!r} does not resolve: {exc}"

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip in _METADATA_NET:
            return False, _DENY_REASON
        if not allow_private and app_routing.is_blocked_ip(ip):
            return False, _DENY_REASON
    return True, ""


def _client(endpoint: str, region: str, access_key: str, secret_key: str, secure: bool) -> Minio:
    host, is_secure = normalize_endpoint(endpoint, secure)
    return Minio(
        endpoint=host,
        access_key=access_key,
        secret_key=secret_key,
        secure=is_secure,
        region=region or None,
    )


def test_connection(
    *,
    endpoint: str,
    region: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    secure: bool = True,
    db=None,
) -> tuple[bool, str]:
    """Run the full validation. Returns (ok, human-readable reason)."""
    # GSEC R-066 — SSRF gate before any Minio put/get/delete can fire.
    ok, reason = check_endpoint_allowed(endpoint, secure, db=db)
    if not ok:
        return False, reason

    try:
        client = _client(endpoint, region, access_key, secret_key, secure)
    except Exception as exc:  # noqa: BLE001 — malformed endpoint / bad args
        return False, f"Invalid connection parameters: {exc}"

    # 1+2+3 — reachability, auth, bucket existence.
    try:
        exists = client.bucket_exists(bucket)
    except S3Error as exc:
        if exc.code in _AUTH_CODES:
            return False, f"Authentication failed ({exc.code}): check the access/secret key."
        if exc.code in _NO_BUCKET_CODES:
            return False, f"Bucket '{bucket}' does not exist or is not accessible."
        return False, f"S3 error while checking bucket ({exc.code}): {exc.message}"
    except Exception as exc:  # noqa: BLE001 — connection refused / DNS / TLS
        return False, f"Cannot reach endpoint '{endpoint}': {exc}"

    if not exists:
        return False, f"Bucket '{bucket}' does not exist on the endpoint."

    # 4 — probe object put / get / delete round-trip.
    probe_key = f"__mod_conn_test__/{_secrets.token_hex(8)}.probe"
    payload = _secrets.token_bytes(32)
    try:
        client.put_object(bucket, probe_key, io.BytesIO(payload), length=len(payload),
                          content_type="application/octet-stream")
    except S3Error as exc:
        code = exc.code
        if code in _AUTH_CODES:
            return False, f"Write denied ({code}): the credentials cannot write to '{bucket}'."
        return False, f"Probe upload failed ({code}): {exc.message}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Probe upload failed: {exc}"

    try:
        resp = client.get_object(bucket, probe_key)
        try:
            got = resp.read()
        finally:
            resp.close()
            resp.release_conn()
        if got != payload:
            return False, "Probe round-trip mismatch: downloaded bytes differ from uploaded."
    except Exception as exc:  # noqa: BLE001
        # Best-effort cleanup before returning.
        try:
            client.remove_object(bucket, probe_key)
        except Exception:  # noqa: BLE001
            pass
        return False, f"Probe download failed: {exc}"

    try:
        client.remove_object(bucket, probe_key)
    except Exception as exc:  # noqa: BLE001
        return False, f"Probe delete failed (upload/download OK): {exc}"

    return True, "Connection OK: reachable, authenticated, bucket present, put/get/delete round-trip passed."
