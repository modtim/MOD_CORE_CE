"""services/oauth_broker.py — sz-4.2a OAuth broker SERVICE (no routes, no
refresh job: those are 4.2b/4.2c).

Provider registry (static reviewed HTTPS endpoints only — no user-supplied
URLs, so there is no SSRF surface here), PKCE + Fernet-sealed tenant-bound
`state`, authorization-code exchange, encrypted token storage in
models.OAuthConnection (the credential vault from sz-4.1), and the tenant
BYO client credentials read from Vault (`oauth_client_id_<provider>` /
`oauth_client_secret_<provider>` through crud.secret_cud). Crypto is REUSED,
never copied: tenant-key generation/unwrap via services/http_triggers +
services/key_provider, token ciphertext via services/http_triggers.encrypt_
secret / decrypt_secret.

Security pins:
* the PKCE verifier and the sealed state never leave the server unsealed;
  `state` is "<tenant_id>.<fernet(payload)>" so completion picks the tenant
  key without trusting a plaintext claim;
* provider client secrets are NEVER logged, and exchange failures carry a
  provider error CODE only — never the provider response body, never the
  authorization code (models.OAuthConnection.last_error_code convention);
* the token POST is trust_env=False, follow_redirects=False, 15 s timeout,
  256 KiB response cap.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets as _secrets_mod  # noqa: F401 — verifier/nonce generator
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from cryptography.fernet import Fernet, InvalidToken

import models
from crud import secret_cud
from services import http_triggers
from services import platform_settings as _platform_settings
from utils import encryption
from utils.paid_import import optional_paid
key_provider = optional_paid("services.key_provider")

logger = logging.getLogger(__name__)

# --- Exceptions -------------------------------------------------------------


class OAuthNotConfigured(ValueError):
    """Tenant has no BYO client credentials in Vault for this provider."""


class OAuthStateError(ValueError):
    """Sealed state expired / tampered / sealed by another tenant's key."""


class OAuthExchangeError(ValueError):
    """Provider token endpoint refused the exchange; message is CODE ONLY."""


# --- Provider registry (static, reviewed endpoints; treat as read-only) ----

PROVIDERS: Dict[str, Dict] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "default_scopes": ["openid", "email"],
        "pkce": True,
        "token_auth": "body",
        # offline + consent so a refresh_token is actually issued.
        "extra_authorize_params": {"access_type": "offline",
                                   "prompt": "consent"},
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "revoke_url": None,  # no standard revoke URL for OAuth apps
        "default_scopes": ["repo"],
        "pkce": True,
        "token_auth": "body",
    },
    "notion": {
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "revoke_url": None,
        "default_scopes": [],  # Notion has no OAuth scopes
        "pkce": False,  # Notion does not support PKCE
        "token_auth": "basic",  # Notion requires Basic client auth + JSON body
        "token_body": "json",
        "extra_authorize_params": {"owner": "user"},
    },
    "linear": {
        "authorize_url": "https://linear.app/oauth/authorize",
        "token_url": "https://api.linear.app/oauth/token",
        "revoke_url": "https://api.linear.app/oauth/revoke",
        "default_scopes": ["read", "write"],
        "pkce": True,
        "token_auth": "body",
        "scope_sep": ",",  # Linear scopes are COMMA-separated
    },
    "slack": {
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "revoke_url": "https://slack.com/api/auth.revoke",
        "default_scopes": ["openid", "profile"],
        "pkce": True,
        "token_auth": "body",
        # Slack answers 200 OK with {"ok": false, "error": ...} on failure.
        "ok_field": "ok",
    },
    "airtable": {
        "authorize_url": "https://airtable.com/oauth2/v1/authorize",
        "token_url": "https://airtable.com/oauth2/v1/token",
        "revoke_url": None,
        "default_scopes": ["data.records:read", "data.records:write",
                           "schema.bases:read"],
        "pkce": True,  # Airtable requires PKCE on every client
        # Basic auth only when the tenant actually has a client secret
        # (Airtable allows public, secret-less PKCE clients too).
        "token_auth": "basic_if_secret",
    },
    "microsoft365": {
        "authorize_url": ("https://login.microsoftonline.com/"
                          "common/oauth2/v2.0/authorize"),
        "token_url": ("https://login.microsoftonline.com/"
                      "common/oauth2/v2.0/token"),
        "revoke_url": None,  # MS identity platform has no revoke endpoint
        # Graph short form, space-separated.
        "default_scopes": ["offline_access", "User.Read", "Files.ReadWrite"],
        "pkce": True,
        "token_auth": "body",
    },
    "ebay": {
        # eBay user OAuth (account connect). UNVERIFIED OFFLINE: the endpoint
        # paths, scope names and PKCE support below are from eBay's public
        # developer docs as recalled in this job — no web fetch was
        # available; re-check against developer.ebay.com before shipping.
        "authorize_url": "https://auth.ebay.com/oauth2/authorize",
        "token_url": "https://api.ebay.com/identity/v1/oauth2/token",
        "revoke_url": None,  # eBay documents no user-OAuth revoke endpoint
        # eBay sell scopes are full-URI names under
        # https://api.ebay.com/oauth/api_scope/ (documented set includes
        # sell.inventory, sell.account, sell.activity, sell.analytics.data,
        # sell.funds-api, sell.payment, sell.shipping_method, ...); the
        # defaults are the two an account connect needs. Space-separated.
        "default_scopes": ["https://api.ebay.com/oauth/api_scope/sell.inventory",
                           "https://api.ebay.com/oauth/api_scope/sell.account"],
        "pkce": True,  # UNVERIFIED OFFLINE: eBay documents PKCE for the code flow
        "token_auth": "basic",  # eBay requires client auth via HTTP Basic
        # eBay's redirect_uri parameter carries a RuName (the value
        # registered in the eBay developer portal), not a callback URL:
        # when the platform setting oauth.ebay.runame is non-empty it
        # REPLACES the caller's redirect_uri in the authorize + token
        # requests; empty means the standard https callback (see
        # _ru_name below).
        "redirect_uri_is_ru_name": True,
    },
    "ebay_sandbox": {
        # eBay sandbox variant (developer-portal sandbox app keys).
        "authorize_url": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "token_url": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "revoke_url": None,
        "default_scopes": ["https://api.sandbox.ebay.com/oauth/api_scope/sell.inventory",
                           "https://api.sandbox.ebay.com/oauth/api_scope/sell.account"],
        "pkce": True,  # UNVERIFIED OFFLINE: same PKCE note as "ebay"
        "token_auth": "basic",
        "redirect_uri_is_ru_name": True,
    },
}

_MAX_RESPONSE_BYTES = 256 * 1024
_TOKEN_SECRET_NAME = "oauth_client_{}_{}"  # oauth_client_{id,secret}_{provider}

# --- Platform settings the registry reads (GS.PSR.2) ------------------------
# eBay (and its sandbox variant) send a RuName — the redirect value
# registered in the eBay developer portal — in the redirect_uri parameter
# instead of a callback URL. The RuName is per-platform (it belongs to the
# OAuth app the platform registered with eBay, which is BYO per tenant in
# Vault, but the RuName string itself is a plain label, not a secret), so it
# lives in the platform-settings registry; an empty value means "use the
# standard https callback the caller passed" (the pre-existing behaviour).
RUNAME_SETTING_KEY = "oauth.{}.runame"
_RUNAME_KEYS = frozenset(
    RUNAME_SETTING_KEY.format(p) for p in ("ebay", "ebay_sandbox"))
for _ebay_provider in ("ebay", "ebay_sandbox"):
    _platform_settings.register(
        RUNAME_SETTING_KEY.format(_ebay_provider),
        str,
        "",
        description=("RuName registered with eBay's developer portal for "
                     "this provider's OAuth app; sent in place of the "
                     "callback URL in the redirect_uri parameter. Empty = "
                     "use the caller's https callback URL."),
        group="oauth",
    )
del _ebay_provider


def _ru_name(db: Session, provider: str) -> Optional[str]:
    """Non-empty RuName for this provider, or None (use the caller's URL).

    Read through the caller's session so no hidden short SessionLocal opens
    inside a flow's transaction. A settings-layer failure is non-fatal: it
    means "no override", the standard callback still works."""
    key = RUNAME_SETTING_KEY.format(provider)
    if key not in _RUNAME_KEYS:
        return None
    try:
        value = _platform_settings.get_setting(key, db=db)
    except Exception as exc:  # noqa: BLE001 — settings layer degrades to None
        logger.warning("oauth_broker: ru-name read failed provider=%s "
                       "(non-fatal): %s", provider, type(exc).__name__)
        return None
    value = str(value).strip() if value is not None else ""
    return value or None

# --- HTTP seam --------------------------------------------------------------
# Tests inject an httpx.MockTransport here; production stays None (default
# transport). The URL is always PROVIDERS[p]["token_url"] — a static literal
# from the registry above — never user input.
_TEST_TRANSPORT: Optional[httpx.BaseTransport] = None


def _http_client_factory() -> httpx.Client:
    """Seam so tests hand in a MockTransport; production gets a plain
    trust-env-off client (provider endpoints are ours, not the customer's
    URL, so no egress-policy round trip is needed here)."""
    return httpx.Client(trust_env=False, timeout=15, follow_redirects=False,
                        transport=_TEST_TRANSPORT)


# --- Tenant BYO client credentials (Vault) ----------------------------------


def _client_credentials(tenant_id: int, provider: str) -> Tuple[str, str]:
    """(client_id, client_secret) from the tenant Vault. Missing either one
    -> OAuthNotConfigured. The values are never logged, anywhere."""
    try:
        client_id = secret_cud._vault_get_tenant_secret(
            tenant_id, _TOKEN_SECRET_NAME.format("id", provider))
        client_secret = secret_cud._vault_get_tenant_secret(
            tenant_id, _TOKEN_SECRET_NAME.format("secret", provider))
    except Exception as exc:
        # Vault outage: name the provider only — never a value, never the
        # Vault path contents.
        logger.warning("oauth_broker: Vault client-credential read failed "
                       "tenant=%s provider=%s (non-fatal): %s",
                       tenant_id, provider, type(exc).__name__)
        raise OAuthNotConfigured(provider) from exc
    if not client_id or not client_secret:
        raise OAuthNotConfigured(provider)
    return str(client_id), str(client_secret)


def _tenant_and_key(db: Session, tenant_id: int) -> Tuple[models.Tenant, str]:
    """(tenant, plaintext tenant DEK) — generating + wrapping a DEK first
    when the tenant was provisioned without one (same self-heal as
    http_triggers.encrypt_secret)."""
    tenant = db.query(models.Tenant).filter(
        models.Tenant.id == tenant_id).one_or_none()
    if tenant is None:
        raise OAuthStateError(f"tenant {tenant_id} not found")
    key_material = http_triggers._ensure_tenant_encryption_key(db, tenant)
    if key_provider is None:
        raise HTTPException(status_code=402,
            detail="OAuth connections are not available in this edition.")
    plaintext_key = key_provider.unwrap_tenant_key(
        key_material, tenant.id, db=db)
    return tenant, plaintext_key


# --- start_authorization ----------------------------------------------------


def _redirect_uri_ok(redirect_uri: str) -> bool:
    """https only; http allowed ONLY for localhost (OAuth redirect-URI
    rules). Anything else (file://, javascript:, attacker hosts) is refused
    before a URL is ever built."""
    try:
        parsed = urlparse(redirect_uri)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if parsed.scheme == "http":
        return parsed.hostname == "localhost"
    return False


def start_authorization(db: Session, *, tenant_id: int, user_id: Optional[int],
                        provider: str, redirect_uri: str,
                        scopes: Optional[List[str]] = None) -> Dict[str, str]:
    """Begin an OAuth2 authorization-code + PKCE(S256) flow.

    Returns {url, state}. `state` is "<tenant_id>.<fernet(payload)>" keyed
    with the TENANT DEK, so completion picks the tenant key without trusting
    anything in the state blob itself; the PKCE verifier only ever exists
    inside that sealed payload.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"unknown oauth provider: {provider!r}")
    if not _redirect_uri_ok(redirect_uri):
        raise ValueError(
            "redirect_uri must be https (http only for localhost)")

    client_id, _secret = _client_credentials(tenant_id, provider)
    granted = list(scopes) if scopes else list(spec["default_scopes"])

    # eBay's redirect_uri parameter carries a RuName (a developer-portal
    # label), not a callback URL: when the platform setting
    # oauth.<provider>.runame is non-empty it REPLACES the caller's URL in
    # the authorize request AND in the sealed state (so the token exchange
    # posts the identical value eBay's app registered). The caller's URL is
    # still validated first — a garbage redirect_uri is refused even for
    # eBay, so the route keeps its 422 contract.
    ru_name = _ru_name(db, provider)
    if ru_name:
        redirect_for_flow = ru_name
    else:
        redirect_for_flow = redirect_uri

    # PKCE S256: verifier 43-128 chars (32 urlsafe bytes -> 43 chars).
    verifier = _secrets_mod.token_urlsafe(32)
    challenge = base64.b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    payload = {
        "t": tenant_id, "u": user_id, "p": provider,
        "r": redirect_uri, "v": verifier,
        "n": _secrets_mod.token_urlsafe(16),
    }
    if ru_name:
        # The RuName rides the SEALED state too, so the token exchange posts
        # the identical value eBay's app registered; "r" stays the caller's
        # callback URL (the callback redirect's destination).
        payload["ru"] = ru_name
    _tenant, plaintext_key = _tenant_and_key(db, tenant_id)
    blob = Fernet(plaintext_key.encode()).encrypt(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    state = f"{tenant_id}.{blob}"

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_for_flow,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if spec["pkce"] is False:
        params.pop("code_challenge")
        params.pop("code_challenge_method")
    if granted:
        params["scope"] = spec.get("scope_sep", " ").join(granted)
    params.update(spec.get("extra_authorize_params", {}))

    query = "&".join(
        f"{k}={quote_plus(str(v))}" for k, v in params.items())
    url = f"{spec['authorize_url']}?{query}"
    return {"url": url, "state": state}


# --- complete_authorization -------------------------------------------------


def _unseal_state(db: Session, state: str, max_age_s: int) -> Tuple[dict, str]:
    """("<tenant_id>.<fernet(payload)") -> (payload, tenant plaintext DEK).

    Expired / tampered / sealed by another tenant's key all land in the
    same OAuthStateError bucket. Nothing about the payload is logged.
    """
    tid, _, blob = (state or "").partition(".")
    try:
        tenant_id = int(tid)
    except ValueError:
        raise OAuthStateError("state malformed") from None
    if not blob:
        raise OAuthStateError("state malformed")
    tenant, plaintext_key = _tenant_and_key(db, tenant_id)
    try:
        raw = Fernet(plaintext_key.encode()).decrypt(blob.encode(),
                                                     ttl=max_age_s)
    except InvalidToken:
        raise OAuthStateError("state expired or tampered") from None
    try:
        payload = json.loads(raw.decode())
    except (ValueError, UnicodeError):
        raise OAuthStateError("state payload unreadable") from None
    if not isinstance(payload, dict):
        raise OAuthStateError("state payload unreadable")
    # The key already binds the tenant; this is a belt-and-braces check that
    # the claim inside the payload agrees with the prefix we keyed on.
    if payload.get("t") != tenant_id:
        raise OAuthStateError("state tenant mismatch")
    for field in ("p", "r", "v"):
        if not payload.get(field):
            raise OAuthStateError("state payload incomplete")
    return payload, plaintext_key


def _exchange_code(spec: Dict, client_id: str, client_secret: str,
                   code: str, redirect_uri: str, verifier: Optional[str]) -> dict:
    """POST the provider token endpoint. Every failure path raises
    OAuthExchangeError with a CODE ONLY — the provider response body and the
    authorization code never reach the exception, the log, or the DB.

    The response is read in chunks via `iter_bytes()` so the 256 KiB cap is
    enforced WHILE streaming, never after the whole body has already been
    buffered into memory.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if verifier:
        data["code_verifier"] = verifier

    token_auth = spec["token_auth"]
    use_basic = token_auth == "basic" or (
        token_auth == "basic_if_secret" and bool(client_secret))
    auth = (client_id, client_secret) if use_basic else None
    if not use_basic:
        data["client_id"] = client_id
        data["client_secret"] = client_secret

    client = _http_client_factory()
    post_kwargs = {"json": data} if spec.get("token_body") == "json" \
        else {"data": data}
    try:
        # ssrf-ok: URL is PROVIDERS[...]["token_url"], a static reviewed HTTPS
        # literal from the frozen registry — never user-supplied.
        request = client.build_request(
            "POST", spec["token_url"],
            headers={"Accept": "application/json"}, **post_kwargs)
        resp = client.send(request, auth=auth, stream=True)
    except httpx.HTTPError:
        client.close()
        raise OAuthExchangeError("network_unavailable") from None

    body = bytearray()
    try:
        try:
            for chunk in resp.iter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise OAuthExchangeError("response_too_large")
        except httpx.HTTPError:
            raise OAuthExchangeError("network_unavailable") from None
        status_code = resp.status_code
    finally:
        resp.close()
        client.close()

    if status_code < 200 or status_code >= 300:
        # a 3xx is NOT followed (follow_redirects=False) and is a failure.
        raise OAuthExchangeError(_error_code_only(bytes(body)))
    try:
        parsed = json.loads(bytes(body).decode())
    except (ValueError, UnicodeError):
        raise OAuthExchangeError("response_unparseable") from None
    if not isinstance(parsed, dict):
        raise OAuthExchangeError("response_unparseable")
    ok_field = spec.get("ok_field")
    if ok_field and parsed.get(ok_field) is False:
        # e.g. Slack: HTTP 200 + {"ok": false, "error": ...} is a failure.
        raise OAuthExchangeError(_error_code_only(parsed))
    if "access_token" not in parsed:
        raise OAuthExchangeError(_error_code_only(parsed))
    return parsed


def _error_code_only(body: "bytes | dict") -> str:
    """Provider error as a bare code (<=64 chars, [a-z_]+ only). Anything
    else becomes a generic token — a provider body never propagates."""
    raw = None
    if isinstance(body, dict):
        raw = body.get("error")
    else:
        try:
            parsed = json.loads(body.decode()) if body else None
            raw = parsed.get("error") if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001 — body may be empty/non-JSON
            raw = None
    if isinstance(raw, str) and len(raw) <= 64 \
            and all(c.isalpha() or c == "_" for c in raw):
        return raw
    return "exchange_failed"


def complete_authorization(db: Session, *, state: str, code: str,
                           max_age_s: int = 600) -> models.OAuthConnection:
    """Redeem the code for the sealed `state`, store the tokens encrypted
    under the tenant DEK, upsert the vault row, emit one audit row.

    Caller commits; this only flushes.
    """
    payload, plaintext_key = _unseal_state(db, state, max_age_s)
    provider = payload["p"]
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise OAuthStateError("state provider unknown")
    client_id, client_secret = _client_credentials(payload["t"], provider)

    verifier = payload["v"] if spec["pkce"] else None
    # eBay posts the RuName (sealed as "ru") in place of the callback URL;
    # every other provider posts the bound "r" exactly as before.
    tokens = _exchange_code(spec, client_id, client_secret, code,
                            payload.get("ru") or payload["r"], verifier)

    tenant, _key = _tenant_and_key(db, payload["t"])
    access = str(tokens["access_token"])
    refresh = tokens.get("refresh_token")
    refresh = str(refresh) if refresh else None
    expires_in = tokens.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))

    granted = tokens.get("scope")
    scope_sep = spec.get("scope_sep", " ")
    scopes = ([s for s in str(granted).split(scope_sep) if s] if granted
              else list(spec["default_scopes"]))

    # Best-effort external account id from THIS response only — no extra
    # provider API calls in this task (Slack's oauth.v2.access response
    # nests the workspace id at response["team"]["id"]).
    external_account_id = None
    if provider == "slack":
        team = tokens.get("team")
        if isinstance(team, dict) and team.get("id"):
            external_account_id = str(team["id"])[:255]

    _kv = key_provider.ciphertext_key_version(tenant.encryption_key)
    key_version = str(_kv if _kv is not None
                      else encryption.get_key_version())[:32]

    # Upsert on (tenant, provider, external_account_id) among LIVE rows.
    existing = None
    if external_account_id is not None:
        existing = db.query(models.OAuthConnection).filter(
            models.OAuthConnection.tenant_id == tenant.id,
            models.OAuthConnection.provider == provider,
            models.OAuthConnection.external_account_id == external_account_id,
            models.OAuthConnection.deleted_at.is_(None),
        ).one_or_none()

    created = existing is None
    if created:
        conn = models.OAuthConnection(
            tenant_id=tenant.id, user_id=payload.get("u"),
            provider=provider, external_account_id=external_account_id,
            scopes=scopes, status="active",
        )
        db.add(conn)
    else:
        conn = existing
    conn.encrypted_access_token = http_triggers.encrypt_secret(
        db, tenant, access)
    conn.encrypted_refresh_token = (
        http_triggers.encrypt_secret(db, tenant, refresh) if refresh
        else None)
    conn.key_version = key_version
    conn.access_token_expires_at = expires_at
    conn.status = "active"
    conn.last_error_code = None
    conn.updated_at = datetime.now(timezone.utc)
    db.flush()

    _audit_completion(db, tenant, payload.get("u"), provider, conn.id,
                      created)
    return conn


def _audit_completion(db: Session, tenant: models.Tenant,
                      user_id: Optional[int], provider: str,
                      connection_id: Optional[int], created: bool) -> None:
    """One audit row per completion: provider + connection id ONLY."""
    try:
        db.add(models.AuditEvent(
            tenant_id=tenant.id, user_id=user_id,
            event_name=("oauth.connection.created" if created
                        else "oauth.connection.updated"),
            details={"provider": provider,
                     "connection_id": connection_id},
        ))
    except Exception as exc:
        logger.warning("oauth_broker: audit emit failed (non-fatal): %s",
                       type(exc).__name__)


# --- token access for nodes (4.2b+ consumers) --------------------------------


def get_access_token(db: Session, conn: models.OAuthConnection) -> str:
    """Decrypted access token for a LIVE, ACTIVE connection. A revoked /
    reconnect_required / refresh_failed row is refused — nodes must re-run
    the connect flow, not silently use a dead token."""
    if conn.status != "active":
        raise OAuthStateError(f"connection {conn.id} is not active")
    tenant = db.query(models.Tenant).filter(
        models.Tenant.id == conn.tenant_id).one_or_none()
    if tenant is None:
        raise OAuthStateError("connection tenant missing")
    plain = http_triggers.decrypt_secret(
        db, tenant, conn.encrypted_access_token)
    if not plain:
        raise OAuthStateError("access token undecryptable (key rotated?)")
    return plain

