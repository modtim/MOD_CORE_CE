"""OpenBao KV v2 / AppRole / transit client (formerly utils.vault_client). Wire protocol and names unchanged; the VAULT_ADDR env name is kept."""

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


class VaultError(RuntimeError):
    pass


def _vault_addr() -> str:
    return os.getenv("VAULT_ADDR", "").rstrip("/")


_OPENBAO_ROLE_ID_FILE = "/run/openbao/approle_role_id"
_OPENBAO_SECRET_ID_FILE = "/run/openbao/approle_secret_id"


def _request(method: str, path: str, token: Optional[str] = None, json: Optional[dict] = None, timeout: int = 10):
    addr = _vault_addr()
    if not addr:
        raise VaultError("VAULT_ADDR is not set")
    url = f"{addr}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    try:
        resp = requests.request(method, url, headers=headers, json=json, timeout=timeout)
    except requests.ConnectionError as exc:
        raise VaultError(f"Vault unreachable {method} {path}: {exc}") from exc
    if resp.status_code < 200 or resp.status_code >= 300:
        raise VaultError(f"Vault request failed {method} {path}: HTTP {resp.status_code}: {resp.text[:500]}")
    if resp.text:
        return resp.json()
    return {}


def get_seal_status(timeout: int = 5) -> str:
    """Report Vault's seal state as one of ``"sealed"`` / ``"unsealed"`` /
    ``"unknown"``.

    Uses the unauthenticated ``GET /v1/sys/seal-status`` endpoint, which
    answers 200 whether or not the box is sealed — so a reachable Vault
    always yields a definitive answer. ``"unknown"`` means the Vault
    server could not be reached at all (no ``VAULT_ADDR`` set, connection
    refused, non-200, or malformed body): callers must treat that as
    "don't know", NOT as sealed.
    """
    try:
        payload = _request("GET", "/v1/sys/seal-status", token=None, timeout=timeout)
    except VaultError:
        return "unknown"
    sealed = (payload or {}).get("sealed")
    if sealed is True:
        return "sealed"
    if sealed is False:
        return "unsealed"
    return "unknown"


# ---------------------------------------------------------------------------
# Auth resolution — the one shape every KV writer/reader must use.
#
# Priority (same as CORE/API/database.py:197-224 and the file-then-env creds
# read of CORE/API/utils/build_task_token.py:87-94):
#   1) the Vault-Agent token file (paid/dev: compose mounts it and sets
#      VAULT_AGENT_TOKEN_PATH — CORE/startup/docker-compose.yml:1809), so
#      paid behaviour is byte-identical and the AppRole branch never runs;
#   2) the OpenBao AppRole login — CE ships no Vault Agent
#      (open-core/overlay/docker-compose.yml:353; open-core/tests/test_ce_clean_checkout.py:264),
#      and its role/secret ids live in the openbao-init volume at
#      /run/openbao/approle_* (overlay :559), exported by entrypoint.sh:25-29.
#   3) nothing available -> None; callers raise their own error.
# Paths only are ever logged/returned; token and secret VALUES are never
# logged, and no new env var changes behaviour (the names below are the
# ones database.py/entrypoint.sh already set).
# FIX-AICONFIG-REVIEW-CHANGES: when an agent-token path IS pinned in the env
# (paid/dev compose sets VAULT_AGENT_TOKEN_PATH; CE sets neither name) the
# AppRole branch never runs even for a missing/empty file — paid stays
# byte-identical. The AppRole login is cached in-process until ~80% of the
# lease OpenBao returned (reset_approle_token_cache() clears it).
# ---------------------------------------------------------------------------

# In-process AppRole token cache (FIX-AICONFIG-REVIEW-CHANGES item 3): one
# login per lease instead of one fresh 1h token per KV call. Values are never
# logged; the dict holds only the token and a monotonic deadline.
_APPROLE_CACHE_FRACTION = 0.8
_APPROLE_CACHE_LOCK = threading.Lock()
_APPROLE_CACHE: Dict[str, Any] = {"token": None, "expires_at": 0.0}

def _read_openbao_state_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _approle_credentials() -> tuple:
    """(role_id, secret_id): env first, then the /run/openbao files."""
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not (role_id and secret_id):
        role_id = role_id or _read_openbao_state_file(_OPENBAO_ROLE_ID_FILE)
        secret_id = secret_id or _read_openbao_state_file(_OPENBAO_SECRET_ID_FILE)
    return role_id, secret_id


def _agent_token_env_set() -> bool:
    """True when an operator pinned the Vault-Agent token path in the env.

    Paid/dev compose sets VAULT_AGENT_TOKEN_PATH (CORE/startup/docker-compose.yml);
    CE sets neither name. A pinned path means the agent mechanism is the ONLY
    mechanism: a missing/empty file there must never fall through to the AppRole
    login (paid behaviour stays byte-identical — see REV-AICONFIG-APPROLE item 1).
    FIX-APPROLE-CONCERNS C1: PRESENCE pins, not truthiness — a name set to the
    EMPTY string still means the agent mechanism was chosen, so paid must never
    fall back to the AppRole login because an env value got clobbered to ''."""
    return (
        os.getenv("OPENBAO_AGENT_TOKEN_PATH") is not None
        or os.getenv("VAULT_AGENT_TOKEN_PATH") is not None
    )


def get_vault_token() -> Optional[str]:
    """Usable OpenBao token: Vault-Agent file (paid) else AppRole login (CE).

    Returns None only when NEITHER auth path is available — callers decide
    whether that is a hard error (writers) or an empty read (readers).
    Never logs token/secret values. The AppRole login is CACHED in-process
    (_approle_token_cache) until ~80% of the lease_duration OpenBao returned,
    so one request does not mint one 1h token per KV call (REV-AICONFIG-APPROLE
    item 3); a token is never reused past its expiry, and tests reset the cache
    via reset_approle_token_cache()."""
    token_path = os.getenv("OPENBAO_AGENT_TOKEN_PATH") or os.getenv(
        "VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
    if os.path.exists(token_path):
        token = _read_openbao_state_file(token_path)
        if token:
            return token
    if _agent_token_env_set():
        # Paid shape: the path was pinned by the operator, so the agent file IS
        # the mechanism. Missing/empty file -> no token (fail closed, exactly as
        # before the AppRole fallback existed); the AppRole login is never tried.
        return None
    role_id, secret_id = _approle_credentials()
    if not (role_id and secret_id):
        return None
    return _approle_token_cached(role_id, secret_id)


def reset_approle_token_cache() -> None:
    """Drop the cached AppRole token (tests, and any caller that must force a
    fresh login). Thread-safe; never logs the token."""
    with _APPROLE_CACHE_LOCK:
        _APPROLE_CACHE["token"] = None
        _APPROLE_CACHE["expires_at"] = 0.0


def _cache_clock() -> float:
    """The cache's clock. One indirection so tests can drive expiry without
    sleeping (monkeypatch this name); never affects product behaviour."""
    return time.monotonic()


def _approle_token_cached(role_id: str, secret_id: str) -> Optional[str]:
    """AppRole login token, reused in-process until ~80% of its lease.

    The lease comes from the login response's ``lease_duration`` (the helper
    never assumes a TTL). A token is handed out only while
    ``_cache_clock() < expires_at`` where ``expires_at`` is 80% of the lease
    measured at the login, so a token is NEVER reused at or past expiry — the
    worst case is a token used at 79% of a lease that has since been revoked by
    OpenBao, which surfaces as the caller's ordinary VaultError. A login whose
    response carries no usable lease_duration is NOT cached at all (fresh login
    per call — the safe direction). Thread-safe (the API runs many threads);
    no token/secret value is ever logged."""
    now = _cache_clock()
    with _APPROLE_CACHE_LOCK:
        if _APPROLE_CACHE["token"] and now < _APPROLE_CACHE["expires_at"]:
            return _APPROLE_CACHE["token"]
    token, lease = _approle_login_with_lease(role_id, secret_id)
    if lease <= 0:
        return token
    use_until = now + lease * _APPROLE_CACHE_FRACTION
    with _APPROLE_CACHE_LOCK:
        _APPROLE_CACHE["token"] = token
        _APPROLE_CACHE["expires_at"] = use_until
    return token


def approle_login(role_id: str, secret_id: str) -> str:
    """AppRole login -> client token (unchanged contract: ~100 call sites use
    this shape). The lease-aware login lives in _approle_login_with_lease."""
    token, _lease = _approle_login_with_lease(role_id, secret_id)
    return token


def _approle_login_with_lease(role_id: str, secret_id: str) -> Tuple[str, float]:
    """AppRole login -> (client_token, lease_duration_seconds).

    ``lease_duration`` is read from the login response's ``auth`` block; a
    missing/zero value yields 0.0, which makes the cache refuse to reuse the
    token (a fresh login every call — the safe direction). Never logs values."""
    data = _request(
        "POST",
        "/v1/auth/approle/login",
        token=None,
        json={"role_id": role_id, "secret_id": secret_id},
        timeout=10,
    )
    auth = (data or {}).get("auth", {}) or {}
    token = auth.get("client_token")
    if not token:
        raise VaultError("Vault AppRole login succeeded but no client_token returned")
    lease = auth.get("lease_duration")
    try:
        lease = float(lease)
    except (TypeError, ValueError):
        lease = 0.0
    if lease < 0:
        lease = 0.0
    return token, lease


def _permission_denied(exc: BaseException) -> bool:
    """FIX-APPROLE-CONCERNS C3: is this VaultError a 403/permission denial?

    ``VaultError`` text is built by ``_request`` as
    ``... HTTP <code>: <response body>``; OpenBao answers a revoked/rotated
    token with 403 + "permission denied". Matching the CLASS alone would
    invalidate on a 500/connection error too (pointless churn), so match the
    denial markers only. The exception text is never logged here."""
    text = str(exc).lower()
    return "http 403" in text or "permission denied" in text


def _not_found(exc: BaseException) -> bool:
    """FIX-AI-CONFIG-VAULT-404-QUIET: is this failure a plain HTTP 404?

    A KV v2 read of a path that was never written answers 404, which
    ``_request`` turns into a ``VaultError`` carrying ``... HTTP <code>: ...``.
    On a fresh CE that is the NORMAL case (no seed/init script writes
    ``platform/ai-config/*``), so callers must treat it as "missing = empty"
    (DEBUG at most) instead of a WARNING — 700+ WARNING lines/day/box buried
    the real denials. Match the status marker only (same shape as
    ``_permission_denied``); the exception text is never logged here."""
    return "http 404" in str(exc).lower()


def _invalidate_approle_cache_on_denial(exc: VaultError) -> None:
    """FIX-APPROLE-CONCERNS C3: a 403 from a KV call means the token we just
    used is dead (server-side revoke) or the AppRole's secret_id rotated — the
    in-process cache ignores role_id/secret_id, so it would keep handing the
    dead token out for the rest of the lease. Drop it here (the cheapest place
    that covers every caller) so the NEXT get_vault_token() logs in fresh.
    Deliberately NOT a retry loop: the failed call still raises, exactly as
    today; one fresh login on the next call is enough. Paid is unaffected —
    its agent token never populates this cache, so the reset is a no-op."""
    if _permission_denied(exc):
        reset_approle_token_cache()


def read_kv_v2(secret_path: str, token: str) -> Dict[str, Any]:
    """
    Read KV v2 secret at `secret/data/<secret_path>`.

    Returns the `data` dict. A 403 invalidates the cached AppRole token
    (FIX-APPROLE-CONCERNS C3) and still raises, unchanged.
    """
    try:
        payload = _request("GET", f"/v1/secret/data/{secret_path.lstrip('/')}", token=token, timeout=10)
    except VaultError as exc:
        _invalidate_approle_cache_on_denial(exc)
        raise
    return (payload or {}).get("data", {}).get("data", {}) or {}


def write_kv_v2(
    secret_path: str,
    token: str,
    data: Dict[str, Any],
    cas: Optional[int] = None,
) -> None:
    """
    Write KV v2 secret at `secret/data/<secret_path>`.

    `cas` (default None = plain overwrite, exactly as before) is the KV v2
    check-and-set version: `cas=0` means "create ONLY if the key does not
    exist yet" and the server answers HTTP 400 (-> VaultError) when a row is
    already there. Callers that must be first-writer-wins across processes
    (the build-task signing keypair) use it; a plain write silently
    overwrites the other process's row.

    A 403 invalidates the cached AppRole token (FIX-APPROLE-CONCERNS C3); the
    error itself propagates exactly as before.
    """
    payload: Dict[str, Any] = {"data": data}
    if cas is not None:
        payload["options"] = {"cas": int(cas)}
    try:
        _request(
            "POST",
            f"/v1/secret/data/{secret_path.lstrip('/')}",
            token=token,
            json=payload,
            timeout=10,
        )
    except VaultError as exc:
        _invalidate_approle_cache_on_denial(exc)
        raise


def delete_kv_v2(secret_path: str, token: str) -> None:
    """
    Delete latest version of a KV v2 secret at `secret/data/<secret_path>`.

    A 403 invalidates the cached AppRole token (FIX-APPROLE-CONCERNS C3).
    """
    try:
        _request(
            "DELETE",
            f"/v1/secret/data/{secret_path.lstrip('/')}",
            token=token,
            json=None,
            timeout=10,
        )
    except VaultError as exc:
        _invalidate_approle_cache_on_denial(exc)
        raise


def create_token(token: str, policies: List[str], ttl: str = "1h", renewable: bool = True) -> str:
    payload = _request(
        "POST",
        "/v1/auth/token/create",
        token=token,
        json={"policies": policies, "ttl": ttl, "renewable": renewable},
        timeout=10,
    )
    out = (payload or {}).get("auth", {}).get("client_token")
    if not out:
        raise VaultError("Vault token creation returned no client_token")
    return out


def read_database_creds(role: str, token: str) -> Dict[str, Any]:
    """
    Read dynamic database credentials from `database/creds/<role>`.
    """
    payload = _request("GET", f"/v1/database/creds/{role}", token=token, timeout=10)
    data = (payload or {}).get("data", {}) or {}
    # Typically includes username/password + lease info in `auth` section, but keep generic.
    return data


# ---------------------------------------------------------------------------
# Transit engine — GY.C6c customer-managed key (KEK) support.
#
# The KEK itself never leaves Vault: these calls only ever send/receive
# plaintext-DEK bytes (datakey/decrypt) or ciphertext (rewrap), never the
# named transit key's own key material.
# ---------------------------------------------------------------------------

def transit_mount_exists(mount: str, token: str) -> bool:
    """Whether a transit secrets engine is mounted at ``<mount>/``.

    Uses the operator-facing ``GET /v1/sys/mounts`` listing (requires a
    token with ``sys/mounts`` read, same token used for the transit calls
    below). Never raises VaultError for "not mounted" — only for a genuine
    request failure (sealed/unreachable/permission denied)."""
    payload = _request("GET", "/v1/sys/mounts", token=token, timeout=5)
    data = (payload or {}).get("data") or payload or {}
    entry = data.get(f"{mount.strip('/')}/")
    if not isinstance(entry, dict):
        return False
    return entry.get("type") == "transit"


def transit_ensure_key(mount: str, key_name: str, token: str) -> None:
    """Idempotently create a transit key at ``<mount>/keys/<key_name>``.

    POSTs ``{"type": "aes256-gcm96"}``. A 2xx response or a Vault "already
    exists" error are both treated as success, so this is safe to call
    even when the key is already there (e.g. on a retry). Callers decide
    *when* to call this (narrow gating lives at the call site, not here);
    this function never logs key material or the token, and never reads
    back the key."""
    try:
        _request(
            "POST", f"/v1/{mount.strip('/')}/keys/{key_name}",
            token=token, json={"type": "aes256-gcm96"}, timeout=10)
    except VaultError as exc:
        if "already exists" in str(exc).lower():
            return
        raise


def transit_datakey_plaintext(mount: str, key_name: str, token: str) -> Dict[str, Any]:
    """Ask Vault to generate a brand-new random data key under
    ``<mount>/datakey/plaintext/<key_name>``.

    Returns the ``data`` dict: ``plaintext`` (standard base64, the raw DEK
    bytes — caller re-encodes for its own use and discards this value
    after unwrapping/wrapping) and ``ciphertext`` (Vault's self-describing
    ``vault:v<N>:...`` wrapped form — this is what gets persisted)."""
    payload = _request(
        "POST", f"/v1/{mount.strip('/')}/datakey/plaintext/{key_name}",
        token=token, json={}, timeout=10)
    data = (payload or {}).get("data") or {}
    if "plaintext" not in data or "ciphertext" not in data:
        raise VaultError(
            f"transit datakey response missing plaintext/ciphertext for {key_name!r}")
    return data


def transit_decrypt(mount: str, key_name: str, ciphertext: str, token: str) -> str:
    """Unwrap a transit ciphertext back to plaintext (standard base64)."""
    payload = _request(
        "POST", f"/v1/{mount.strip('/')}/decrypt/{key_name}",
        token=token, json={"ciphertext": ciphertext}, timeout=10)
    data = (payload or {}).get("data") or {}
    plaintext = data.get("plaintext")
    if plaintext is None:
        raise VaultError(f"transit decrypt response missing plaintext for {key_name!r}")
    return plaintext


def transit_rewrap(mount: str, key_name: str, ciphertext: str, token: str) -> Dict[str, Any]:
    """Re-wrap a ciphertext under the key's current (latest) version
    without ever exposing the plaintext DEK. Used for rotation: the KEK
    advances a version in Vault, then every wrapped DEK is rewrapped to
    the new version on this call."""
    payload = _request(
        "POST", f"/v1/{mount.strip('/')}/rewrap/{key_name}",
        token=token, json={"ciphertext": ciphertext}, timeout=10)
    data = (payload or {}).get("data") or {}
    if "ciphertext" not in data:
        raise VaultError(f"transit rewrap response missing ciphertext for {key_name!r}")
    return data

