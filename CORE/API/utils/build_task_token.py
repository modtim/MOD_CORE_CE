"""Per-task, short-lived, scoped credential for wizard build-task stages.

The build-task DAG stages (ingest / build / smoke / push / sample / register /
test-and-repair) call back into the API to read/update their BuildSpec and to
register the resulting NodeDefinition. Historically they did this with the
long-lived *platform service master key* injected as ``API_KEY`` — a broad,
eternal credential sitting inside a container that chews on user-supplied build
content. If that container or the worker leaked, the attacker held the master
key.

This module replaces that with a credential shaped like the MinIO STS triplet
the same container already receives: minted on demand per task, scoped to a
single ``(tenant_id, build_spec_id)``, and expiring within the stage's runtime.
If it leaks it's useless within the hour and can only finish *its own* build.

Signing is ASYMMETRIC (RS256): the PRIVATE key mints, the PUBLIC key verifies.
This matters for the distributed/multi-region model — only a component holding
the private key can MINT tokens. Workers never hold either key (they just relay
the finished token the API hands them), and future peer managers can be given
ONLY the public key so they can verify without being able to forge. (A symmetric
HS256 secret would let any verifier also mint — exactly the property we must
avoid once verification is distributed.)

Keys live base64-encoded in env (so multi-line PEM survives an env file):
  - ``BUILD_TASK_TOKEN_PRIVATE_KEY_B64`` — API-only (NOT shipped to workers).
  - ``BUILD_TASK_TOKEN_PUBLIC_KEY_B64``  — safe to distribute to any verifier.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from jose import jwt, JWTError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from datetime import datetime, timezone

from services import platform_settings as _psr  # DB-backed settings registry

# Token type marker — guards against a token minted for some other purpose
# (or a generic JWT) being accepted as a build-task credential.
TOKEN_TYPE = "build_task"

# Default lifetime. A single stage (build/smoke/test-and-repair/...) runs inside
# the build-task container_timeout (30 min); the token must outlive the slowest
# stage but stay short enough that a leak is quickly worthless. 45 min covers a
# full-timeout stage with margin. Each stage fetches a FRESH token at claim, so
# this is per-stage, not per-build.
DEFAULT_TTL_SECONDS = 45 * 60

_ALGORITHM = "RS256"

# Runtime keypair cache, populated by ensure_manager_keypair() for a manager
# that has no env-provided key (i.e. a freshly-enrolled peer manager loads or
# generates its OWN keypair from its Vault). The bootstrap manager uses the env
# key and never touches this. PEM strings.
_RUNTIME_PRIVATE_PEM: Optional[str] = None
_RUNTIME_PUBLIC_PEM: Optional[str] = None

# 2400LR-ceGP-F1 — CE's AppRole creds come from FILES, not compose env:
# open-core/overlay/docker-compose.yml:445 mounts openbao-init-state:/run/openbao:ro
# and CORE/API/entrypoint.sh:25-29 cats those raw files into the env. Any start path
# that skips entrypoint.sh leaves VAULT_APPROLE_* empty, so the self-heal below used
# to bail "skipped — no VAULT_APPROLE_*". Same file-then-env shape as the MinIO /
# encryption-key secrets in entrypoint.sh: env wins (paid/dev byte-identical), the
# files are the fallback. Paths only — values are never logged or returned anywhere
# except the Vault login itself.
_OPENBAO_ROLE_ID_FILE = "/run/openbao/approle_role_id"
_OPENBAO_SECRET_ID_FILE = "/run/openbao/approle_secret_id"


def _read_openbao_state_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _approle_credentials() -> tuple:
    """(role_id, secret_id): env first, then the /run/openbao files. Missing ->
    empty strings."""
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not (role_id and secret_id):
        role_id = role_id or _read_openbao_state_file(_OPENBAO_ROLE_ID_FILE)
        secret_id = secret_id or _read_openbao_state_file(_OPENBAO_SECRET_ID_FILE)
    return role_id, secret_id


def _decode_b64_pem(env_var: str) -> Optional[str]:
    raw = os.getenv(env_var, "")
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        # Allow a raw (already-PEM) value too, in case an operator pasted PEM
        # directly rather than base64. PEM begins with "-----BEGIN".
        if raw.lstrip().startswith("-----BEGIN"):
            return raw
        return None


def _private_key() -> str:
    """The PEM private key used to MINT tokens. Env (bootstrap manager) first,
    then the per-manager Vault-backed runtime key. API-only; raises if neither
    so we fail closed (never sign with an empty/garbage key)."""
    pem = _decode_b64_pem("BUILD_TASK_TOKEN_PRIVATE_KEY_B64") or _RUNTIME_PRIVATE_PEM
    if not pem:
        raise RuntimeError(
            "No build-task signing key available (env BUILD_TASK_TOKEN_PRIVATE_KEY_B64 "
            "unset and no per-manager key provisioned). This key is API-only "
            "(never on workers)."
        )
    return pem


def _public_key() -> str:
    """The PEM public key used to VERIFY tokens. Env first, then the per-manager
    runtime key. Safe to distribute."""
    pem = _decode_b64_pem("BUILD_TASK_TOKEN_PUBLIC_KEY_B64") or _RUNTIME_PUBLIC_PEM
    if not pem:
        raise RuntimeError(
            "No build-task public key available to verify tokens."
        )
    return pem


def is_configured() -> bool:
    """True iff a signing (private) key is available (env or per-manager Vault).
    Callers use this to decide whether to mint a token at all (so an un-
    provisioned deploy degrades to the legacy path instead of crashing)."""
    return bool(_decode_b64_pem("BUILD_TASK_TOKEN_PRIVATE_KEY_B64") or _RUNTIME_PRIVATE_PEM)


def can_verify() -> bool:
    """True iff a public key is available to verify tokens."""
    return bool(_decode_b64_pem("BUILD_TASK_TOKEN_PUBLIC_KEY_B64") or _RUNTIME_PUBLIC_PEM)


# ---------------------------------------------------------------------------
# kid + JWKS (launch-completion-megaplan 30.6.5 / GX.S1c c5a).
#
# Every MOD-minted RS256 token carries a `kid` in its JOSE header, and the API
# publishes the matching public keys at GET /v1/.well-known/jwks.json (plus the
# unversioned alias), so any verifier — MOD's own verifiers, a peer manager
# given only the public half, or a third-party receiver — can resolve WHICH key
# a token was minted with instead of guessing. Rotation is additive: the
# previous public key stays published AND accepted for a window after a
# rotation (never hard-removed mid-window, so tokens minted before the flip
# still verify until they expire), then it is retired by `retire_after`.
#
# Tunables are DB platform settings (never env):
#   security.jwks_previous_public_key_b64  str  — base64 of the PREVIOUS
#       manager PUBLIC key PEM ("" = no previous key).
#   security.jwks_previous_retire_after    str  — ISO-8601 timestamp; the
#       previous key is published/accepted only while now < this value ("" =
#       no previous key window at all, fail-closed).
# ---------------------------------------------------------------------------

_JWKS_KEY_MEMBERS = ("kty", "kid", "alg", "use", "n", "e")
_JWKS_PREVIOUS_KEY_SETTING = "security.jwks_previous_public_key_b64"
_JWKS_PREVIOUS_RETIRE_AFTER_SETTING = "security.jwks_previous_retire_after"


def _register_jwks_settings() -> None:
    """Idempotently register the JWKS tunables (tunables = DB settings)."""
    if _JWKS_PREVIOUS_KEY_SETTING in _psr._REGISTRY:
        return
    _psr.register(
        _JWKS_PREVIOUS_KEY_SETTING,
        str,
        "",
        group="security",
        description="Base64 of the PREVIOUS manager PUBLIC key PEM (the key "
                    "MOD rotated away from). Published in "
                    "GET /v1/.well-known/jwks.json and accepted by the "
                    "verifiers only while now < "
                    "security.jwks_previous_retire_after. Empty = no "
                    "previous key. PUBLIC material only — never the private "
                    "half.",
    )
    _psr.register(
        _JWKS_PREVIOUS_RETIRE_AFTER_SETTING,
        str,
        "",
        group="security",
        description="ISO-8601 timestamp at which the previous manager public "
                    "key stops being published/accepted (>=24h after the "
                    "rotation per the GX.S1c MUST). Empty = the previous key "
                    "is not published/accepted at all (fail-closed).",
    )


_register_jwks_settings()


def _der_subject_public_key_info(pem: str) -> bytes:
    """DER SubjectPublicKeyInfo bytes for a PEM public key (the canonical,
    serialization-independent form we hash the `kid` from)."""
    key = serialization.load_pem_public_key(pem.encode("utf-8"))
    return key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def _kid(pem: str) -> str:
    """Stable key id: "mod-mgr-" + first 8 hex of sha256(DER SPKI).

    Deterministic — the same PEM always yields the same kid, so a published
    kid and a minted kid agree without any stored counter."""
    return "mod-mgr-" + hashlib.sha256(_der_subject_public_key_info(pem)).hexdigest()[:8]


def _b64url_int(value: int) -> str:
    """base64url, no padding, big-endian — the JWK encoding for n/e."""
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode().rstrip("=")


def _jwk(pem: str) -> Optional[Dict[str, Any]]:
    """The JWK for a PEM public key: EXACTLY {kty,kid,alg,use,n,e}.

    Public members only — never d/p/q/dp/dq/qi. Returns None if the PEM is
    not a usable RSA public key (fail-closed: nothing is published)."""
    try:
        numbers = serialization.load_pem_public_key(pem.encode("utf-8")).public_numbers()
    except Exception:
        return None
    if numbers is None:
        return None
    return {
        "kty": "RSA",
        "kid": _kid(pem),
        "alg": _ALGORITHM,
        "use": "sig",
        "n": _b64url_int(numbers.n),
        "e": _b64url_int(numbers.e),
    }


def _previous_public_key(db=None) -> Optional[str]:
    """The previous manager PUBLIC key PEM, but only while it is in-window.

    Returns None (fail-closed) when the setting is unset, the window is
    unset/unparsable, or now >= retire_after. Reads DB platform settings, so
    a rotation is a settings write with no deploy."""
    raw = ""
    after = ""
    try:
        raw = str(_psr.get_setting(_JWKS_PREVIOUS_KEY_SETTING, db=db) or "")
        after = str(_psr.get_setting(_JWKS_PREVIOUS_RETIRE_AFTER_SETTING, db=db) or "")
    except Exception:  # settings store blip -> no previous key, never a 500
        return None
    if not raw or not after:
        return None
    try:
        retire = datetime.fromisoformat(after.replace("Z", "+00:00"))
    except ValueError:
        return None
    if retire.tzinfo is None:
        retire = retire.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= retire:
        return None
    pem = _decode_b64_pem_raw(raw)
    if not pem:
        return None
    try:
        numbers = serialization.load_pem_public_key(pem.encode("utf-8")).public_numbers()
    except Exception:
        return None
    if numbers is None:
        return None
    return pem


def _decode_b64_pem_raw(raw: str) -> Optional[str]:
    """base64 -> PEM text (also accepts an already-PEM value). No env lookup."""
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw if raw.lstrip().startswith("-----BEGIN") else None


def jwks_document(db=None) -> Dict[str, Any]:
    """The JWKS body: {"keys":[...]} — current key, plus the previous key
    while it is in-window. Served from memory only (no outbound fetch).

    Every member set is exactly {kty,kid,alg,use,n,e}: no private material,
    ever. Keys whose PEM is unusable are dropped rather than guessed."""
    keys = []
    try:
        current = _jwk(_public_key())
    except RuntimeError:
        current = None
    if current:
        keys.append(current)
    previous_pem = _previous_public_key(db=db)
    if previous_pem:
        previous = _jwk(previous_pem)
        current_kid = current.get("kid") if current else None
        if previous and previous["kid"] != current_kid:
            keys.append(previous)
    return {"keys": keys[:4]}  # S7: JWKS <= 4 keys


def _kid_and_key_for(token: str, db=None) -> tuple:
    """(kid, pem) — the ONE kid/key resolution shared by every verifier AND the
    auth funnels (RTA401).

    kid is None when the JOSE header carries none; pem is None when NO key this
    manager can verify with (unknown kid, or no keypair at all). The funnels
    need the kid back so they can tell "unknown signing key id" apart from
    "this is not a JWT at all" — that difference decides whether the value is
    ever retried as an API key (it must not be)."""
    try:
        header = jwt.get_unverified_header(token)
    except (JWTError, TypeError, ValueError):
        return (None, None)
    kid = header.get("kid") if isinstance(header, dict) else None
    try:
        current_pem = _public_key()
    except RuntimeError:
        return (kid, None)
    if kid is None:
        return (None, current_pem)
    try:
        if _kid(current_pem) == kid:
            return (kid, current_pem)
    except (ValueError, TypeError):
        return (kid, None)
    previous_pem = _previous_public_key(db=db)
    if previous_pem:
        try:
            if _kid(previous_pem) == kid:
                return (kid, previous_pem)
        except (ValueError, TypeError):
            return (kid, None)
    # signkey-race-fix belt: this worker's globals may predate a key another
    # worker persisted. Re-read KV ONCE (rate-limited) and accept ONLY a pair
    # whose kid matches EXACTLY — still fail-closed: no match -> None -> 401.
    refreshed = _refresh_runtime_key_for_kid(kid)
    if refreshed:
        return (kid, refreshed)
    return (kid, None)


def _verify_key_for(token: str, db=None) -> Optional[str]:
    """The PEM the verifiers must use for THIS token, or None = reject.

    - no `kid` -> the current key (tokens minted before this change still
      verify until they expire);
    - `kid` == current kid -> the current key;
    - `kid` == previous kid AND the previous key is in-window -> the previous
      key;
    - any other kid -> None (unknown key: reject, never fall back)."""
    return _kid_and_key_for(token, db=db)[1]


# ---------------------------------------------------------------------------
# RTA401 — a JWT-shaped credential gets the JWT verdict ONLY.
#
# A value that parses as a JWT (three base64url segments whose JOSE header
# carries `alg` AND `kid`) is a MOD-minted credential, so it must NEVER be
# retried as an API key: no key-prefix lookup, no bcrypt compare. When its kid
# matches no key this manager knows — current, previous-in-window, or the ONE
# signkey-race KV re-read above — the caller 401s with UNKNOWN_KID_DETAIL so
# the operator sees the real cause (a rotated/foreign signing key) instead of a
# generic "invalid API key". Fail-closed either way; the signature checks in
# the verifiers are untouched. The kid is a public identifier (SHA-256 DER-SPKI
# digest, 16 hex chars) — logging it is safe; the token value and any key
# material never are.
# ---------------------------------------------------------------------------

UNKNOWN_KID_DETAIL = "unknown signing key id"


def is_jwt_shaped(token: str) -> bool:
    """True when `token` is three base64url segments whose JOSE header carries
    both `alg` and `kid` — i.e. a MOD-minted JWT, not an API key."""
    if not token or not isinstance(token, str):
        return False
    if token.count(".") != 2:
        return False
    try:
        header = jwt.get_unverified_header(token)
    except (JWTError, TypeError, ValueError):
        return False
    if not isinstance(header, dict):
        return False
    return bool(header.get("alg")) and bool(header.get("kid"))


def unknown_signing_key_id(token: str, db=None) -> bool:
    """True ONLY for a JWT-shaped value whose `kid` matches no key this manager
    knows, AFTER the ONE rate-limited KV re-read. A manager with no keypair at
    all is NOT a kid verdict — the caller keeps its generic 401 there."""
    if not is_jwt_shaped(token):
        return False
    try:
        _public_key()
    except RuntimeError:
        return False
    kid, pem = _kid_and_key_for(token, db=db)
    if kid is not None and pem is None:
        logging.getLogger(__name__).debug("unknown signing key id: %s", kid)
        return True
    return False


# ---------------------------------------------------------------------------
# signkey-race-fix (CE launch) — first-writer-wins convergence.
#
# ensure_manager_keypair() runs in EVERY uvicorn worker (main.py `_startup_tasks`
# "build_task_keypair"; the worker floor is 2, CORE/API/utils/worker_sizing.py:317-342)
# and utils.startup_lock.SEED_GROUP_SINGLE_FLIGHT_TASKS deliberately does NOT cover
# it — a worker that SKIPPED the self-heal would have no keypair in ITS OWN process
# globals at all (is_configured() False -> the run-token mint 503s on that worker),
# so single-flight is the wrong tool here. The fix is convergence instead: after the
# generate+write, RE-READ the KV row and adopt whatever is persisted. Two workers
# that both saw empty KV and both generated a pair now both end on the SAME (last-
# written) pair, so the kid every worker mints is the kid every worker verifies.
# A write that raises because a row already exists takes the same re-read path.
# Logging names the kid only — never key material.
# ---------------------------------------------------------------------------

_KV_ADOPT_WINDOW_S = 60.0
_KV_ADOPT_LAST_ATTEMPT = 0.0


def _manager_vault_path() -> str:
    """This manager's build-task signing KV path (unchanged: never the policy,
    never the AppRole, never a new env var)."""
    return f"managers/{os.getenv('MANAGER_ID', '01m')}/build_task_signing"


def _kv_pair_from(existing) -> tuple:
    """(private_pem, public_pem) from a KV row, or (None, None) if unusable."""
    if not isinstance(existing, dict):
        return None, None
    priv_b64 = existing.get("private_key_b64")
    pub_b64 = existing.get("public_key_b64")
    if not (priv_b64 and pub_b64):
        return None, None
    try:
        return (
            base64.b64decode(priv_b64).decode("utf-8"),
            base64.b64decode(pub_b64).decode("utf-8"),
        )
    except Exception:
        return None, None


def _adopt_persisted_keypair(vault_path: str, vault_token: str, *, reason: str) -> str:
    """Re-read the KV row after our write; if it holds a DIFFERENT pair, adopt it.

    First-writer-wins convergence: the loser of the boot race discards its locally
    generated pair and uses the persisted one, so every worker in the pool ends on
    the same kid. Returns a log fragment naming the kid only — never key material.
    If the re-read finds nothing usable we keep the local pair (the write itself
    may already have landed; the next boot reads it from KV).
    """
    global _RUNTIME_PRIVATE_PEM, _RUNTIME_PUBLIC_PEM
    try:
        from utils.vault_client import read_kv_v2
        stored = read_kv_v2(vault_path, token=vault_token)
    except Exception:
        return "kept local key (re-read failed)"
    priv, pub = _kv_pair_from(stored)
    if not (priv and pub):
        return "kept local key (no persisted row)"
    if priv == _RUNTIME_PRIVATE_PEM and pub == _RUNTIME_PUBLIC_PEM:
        return "kept local key (persisted matches)"
    _RUNTIME_PRIVATE_PEM = priv
    _RUNTIME_PUBLIC_PEM = pub
    try:
        kid = _kid(pub)
    except Exception:
        kid = "unknown"
    return f"adopted persisted key (kid={kid}; {reason})"


def _refresh_runtime_key_for_kid(kid: str) -> Optional[str]:
    """Belt: one rate-limited KV re-read for a kid this worker's globals miss.

    Only for the per-manager (Vault) key path — a bootstrap manager whose key
    comes from env never consults KV here. Returns the PEM ONLY when the
    persisted pair's kid matches EXACTLY; anything else -> None (fail-closed:
    unknown kid stays unknown, the caller 401s)."""
    global _KV_ADOPT_LAST_ATTEMPT, _RUNTIME_PRIVATE_PEM, _RUNTIME_PUBLIC_PEM
    if _decode_b64_pem("BUILD_TASK_TOKEN_PRIVATE_KEY_B64") or _decode_b64_pem(
        "BUILD_TASK_TOKEN_PUBLIC_KEY_B64"
    ):
        return None
    now = time.time()
    if now - _KV_ADOPT_LAST_ATTEMPT < _KV_ADOPT_WINDOW_S:
        return None
    _KV_ADOPT_LAST_ATTEMPT = now
    vault_path = _manager_vault_path()
    try:
        from utils.vault_client import approle_login, read_kv_v2
        role_id, secret_id = _approle_credentials()
        if not (role_id and secret_id):
            return None
        token = approle_login(role_id=role_id, secret_id=secret_id)
        stored = read_kv_v2(vault_path, token=token)
    except Exception:
        return None
    priv, pub = _kv_pair_from(stored)
    if not (priv and pub):
        return None
    try:
        if _kid(pub) != kid:
            return None
    except (ValueError, TypeError):
        return None
    global _RUNTIME_PRIVATE_PEM, _RUNTIME_PUBLIC_PEM
    _RUNTIME_PRIVATE_PEM = priv
    _RUNTIME_PUBLIC_PEM = pub
    return pub


def ensure_manager_keypair() -> str:
    """Startup self-heal: guarantee THIS manager can mint build-task tokens with
    its own keypair.

      - Bootstrap manager (env key set) -> no-op.
      - Otherwise read this manager's keypair from its own Vault at
        `managers/{MANAGER_ID}/build_task_signing`; if absent, generate a fresh
        per-manager RSA keypair and persist it there.

    Per-manager by construction — a cloned/enrolled manager never shares another
    manager's private key. Fail-safe: any Vault/crypto error is swallowed and
    reported in the return string; startup never crashes (the manager just can't
    mint until Vault is reachable, no worse than having no key). Returns a short
    status string for the startup log.
    """
    global _RUNTIME_PRIVATE_PEM, _RUNTIME_PUBLIC_PEM

    if _decode_b64_pem("BUILD_TASK_TOKEN_PRIVATE_KEY_B64"):
        return "env (bootstrap manager) — no-op"

    manager_id = os.getenv("MANAGER_ID", "01m")
    vault_path = f"managers/{manager_id}/build_task_signing"
    try:
        from utils.vault_client import approle_login, read_kv_v2, write_kv_v2
        role_id, secret_id = _approle_credentials()
        if not (role_id and secret_id):
            return "skipped — no VAULT_APPROLE_* env or /run/openbao files (cannot self-provision)"
        token = approle_login(role_id=role_id, secret_id=secret_id)

        existing = None
        try:
            existing = read_kv_v2(vault_path, token=token)
        except Exception:
            existing = None
        if existing and existing.get("private_key_b64") and existing.get("public_key_b64"):
            _RUNTIME_PRIVATE_PEM = base64.b64decode(existing["private_key_b64"]).decode("utf-8")
            _RUNTIME_PUBLIC_PEM = base64.b64decode(existing["public_key_b64"]).decode("utf-8")
            return f"loaded from Vault ({vault_path})"

        # Generate a fresh per-manager keypair and persist it.
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pub = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        write_note = "generated + persisted to Vault"
        try:
            # cas=0 = create-only: KV v2 refuses the write (HTTP 400) when a
            # row already exists, so exactly ONE worker's pair is ever
            # persisted. A plain write silently OVERWRITES the other worker's
            # row, and the earlier writer's post-write re-read then saw its own
            # pair (the later write had not landed yet) -> two workers on two
            # kids for the process lifetime -> node 401 "unknown signing key
            # id" whenever a token crossed workers (gold room cr-s1292, D76).
            write_kv_v2(vault_path, token=token, data={
                "private_key_b64": base64.b64encode(priv).decode(),
                "public_key_b64": base64.b64encode(pub).decode(),
            }, cas=0)
        except Exception:
            # A row already exists (the other worker won the boot race): our
            # write is moot, but the re-read below still converges us.
            write_note = "generated (write lost — row already exists)"
        _RUNTIME_PRIVATE_PEM = priv.decode("utf-8")
        _RUNTIME_PUBLIC_PEM = pub.decode("utf-8")
        adopted = _adopt_persisted_keypair(vault_path, token, reason="boot race")
        if adopted.startswith("adopted persisted key"):
            return f"{write_note} ({vault_path}) -> {adopted}"
        return f"{write_note} ({vault_path})"
    except Exception as exc:  # fail-safe — never crash startup
        return f"unavailable (fail-safe): {exc}"


def mint_build_task_token(
    *,
    tenant_id: int,
    build_spec_id: int,
    task_id: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a signed, scoped, short-lived build-task token (RS256).

    Never persisted or logged by callers — treated like the STS session token
    it sits next to.
    """
    now = int(time.time())
    claims: Dict[str, Any] = {
        "typ": TOKEN_TYPE,
        "tenant_id": int(tenant_id),
        "build_spec_id": int(build_spec_id),
        "task_id": int(task_id),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm=_ALGORITHM,
        headers={"kid": _kid(_public_key())},
    )


def verify_build_task_token(token: str, db=None) -> Optional[Dict[str, Any]]:
    """Verify signature (public key) + expiry + type. Returns the claims dict on
    success, or None if the token is missing, malformed, expired, wrong-typed,
    or not signed by our key. Never raises — callers treat None as "not a valid
    build-task token" and fall through to other auth."""
    if not token:
        return None
    key = _verify_key_for(token, db=db)
    if key is None:
        return None  # unknown kid -> reject, never fall back to the current key
    try:
        claims = jwt.decode(token, key, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    except RuntimeError:
        # Public key unset — can't verify; treat as not-a-build-token.
        return None
    if claims.get("typ") != TOKEN_TYPE:
        return None
    # build_spec_id / tenant_id are required for scope enforcement; a token
    # without them is unusable for the scoped endpoints.
    if claims.get("build_spec_id") is None or claims.get("tenant_id") is None:
        return None
    return claims


# ---------------------------------------------------------------------
# Run-scoped vector token (add-rag-vector-store).
#
# RAG nodes (Vector Upsert / Vector Query) run as WORKFLOW nodes, not build
# tasks, so they can't use a build-spec-scoped token. This is the same RS256
# machinery, scoped to a (tenant_id[, workflow_run_id]) and short-lived. The
# orchestrator mints one per RAG node run and injects it as API_KEY; the vector
# endpoints verify it (public key) and pin the tenant. NOT accepted by the
# build-task endpoints (distinct `typ`), and vice-versa.
# ---------------------------------------------------------------------

VECTOR_RUN_TOKEN_TYPE = "mod-vector-run-v1"
_VECTOR_RUN_TTL_SECONDS = int(os.getenv("VECTOR_RUN_TOKEN_TTL", "3600"))


def mint_vector_run_token(
    *,
    tenant_id: int,
    workflow_run_id: Optional[int] = None,
    ttl_seconds: int = _VECTOR_RUN_TTL_SECONDS,
) -> str:
    """Mint a signed, tenant-scoped, short-lived token for the vector API.
    Treated like an STS session token — never persisted or logged by callers."""
    now = int(time.time())
    claims: Dict[str, Any] = {
        "typ": VECTOR_RUN_TOKEN_TYPE,
        "tenant_id": int(tenant_id),
        "run_id": (int(workflow_run_id) if workflow_run_id is not None else None),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm=_ALGORITHM,
        headers={"kid": _kid(_public_key())},
    )


def verify_vector_run_token(token: str, db=None) -> Optional[Dict[str, Any]]:
    """Verify signature + expiry + type. Returns claims (with tenant_id) or None.
    Never raises — callers treat None as 'not a vector token' and fall through."""
    if not token:
        return None
    key = _verify_key_for(token, db=db)
    if key is None:
        return None
    try:
        claims = jwt.decode(token, key, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    except RuntimeError:
        return None
    if claims.get("typ") != VECTOR_RUN_TOKEN_TYPE:
        return None
    if claims.get("tenant_id") is None:
        return None
    return claims


# ---------------------------------------------------------------------------
# Run-scoped node-callback token (worker-mediated-node-credentials).
# ---------------------------------------------------------------------------
# Vended to a RUNNING node by the worker on demand, so the node never holds a
# standing credential. Same RS256 machinery as the vector token, scoped to a
# (tenant_id, run_id) and short-lived: a node that runs for a week simply
# re-fetches when its cached token nears expiry, so TTL can stay small. Accepted
# ONLY by the node-callback endpoints, which additionally PIN the tenant.
RUN_TASK_TOKEN_TYPE = "mod-run-task-v1"
_RUN_TASK_TTL_SECONDS = int(os.getenv("RUN_TASK_TOKEN_TTL", "900"))  # 15 min


def mint_run_task_token(
    *,
    tenant_id: int,
    workflow_run_id: Optional[int] = None,
    ttl_seconds: int = _RUN_TASK_TTL_SECONDS,
) -> str:
    """Mint a signed, tenant+run-scoped, short-lived node-callback token.
    Treated like an STS session token — never persisted or logged by callers."""
    now = int(time.time())
    claims: Dict[str, Any] = {
        "typ": RUN_TASK_TOKEN_TYPE,
        "tenant_id": int(tenant_id),
        "run_id": (int(workflow_run_id) if workflow_run_id is not None else None),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm=_ALGORITHM,
        headers={"kid": _kid(_public_key())},
    )


def verify_run_task_token(token: str, db=None) -> Optional[Dict[str, Any]]:
    """Verify signature + expiry + type. Returns claims (with tenant_id, run_id)
    or None. Never raises — callers treat None as 'not a run_task token' and fall
    through to the normal auth path."""
    if not token:
        return None
    key = _verify_key_for(token, db=db)
    if key is None:
        return None
    try:
        claims = jwt.decode(token, key, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    except RuntimeError:
        return None
    if claims.get("typ") != RUN_TASK_TOKEN_TYPE:
        return None
    if claims.get("tenant_id") is None:
        return None
    return claims


# ---------------------------------------------------------------------------
# Tenant-scoped model-cache token (model-cache-per-tenant-gated-repos, GF.C0b.5).
# ---------------------------------------------------------------------------
# Minted (services/model_cache_service.mint_model_cache_token) alongside
# HF_TOKEN injection wherever a stack/node-run's env is built for a container
# that talks to mod-model-cache — mirrors /workers/tasks/{id}/credentials
# (short-lived, minted fresh, never a raw tenant_id the client controls).
#
# The client is pointed at mod-model-cache with HF_TOKEN=<this token>, so
# huggingface_hub presents it as `Authorization: Bearer <token>` on every
# request. mod-model-cache verifies it with ONLY the public key (same
# distribution as the build-task public key: safe for any verifier to hold),
# resolves tenant_id from the claims, then calls
# GET /v1/internal/model-cache/hf-token (routers/model_cache_internal.py),
# forwarding this SAME token as X-API-Key, to fetch the tenant's real HF
# token. Distinct `typ` — not accepted by any other *_token verifier here,
# and this file's other verifiers reject a model-cache token in turn.
MODEL_CACHE_TOKEN_TYPE = "mod-model-cache-v1"
MODEL_CACHE_TOKEN_AUD = "model-cache"
# Stacks/node-runs live up to 24h before a routine refresh; mint fresh at
# creation and let the caller re-mint on refresh rather than push TTL higher.
DEFAULT_MODEL_CACHE_TTL_SECONDS = 24 * 60 * 60


def mint_model_cache_token(
    *, tenant_id: int, ttl_seconds: int = DEFAULT_MODEL_CACHE_TTL_SECONDS
) -> str:
    """Mint a signed, tenant-scoped, short-lived token for mod-model-cache.
    Treated like an STS session token — never persisted or logged by callers."""
    now = int(time.time())
    claims: Dict[str, Any] = {
        "typ": MODEL_CACHE_TOKEN_TYPE,
        "aud": MODEL_CACHE_TOKEN_AUD,
        "tenant_id": int(tenant_id),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm=_ALGORITHM,
        headers={"kid": _kid(_public_key())},
    )


def verify_model_cache_token(token: str, db=None) -> Optional[Dict[str, Any]]:
    """Verify signature + expiry + audience + type. Returns claims (with
    tenant_id) or None. Never raises — callers (both the internal endpoint
    here AND mod-model-cache itself, which holds only the public key) treat
    None as 'reject, unauthenticated'."""
    if not token:
        return None
    key = _verify_key_for(token, db=db)
    if key is None:
        return None
    try:
        claims = jwt.decode(
            token, key, algorithms=[_ALGORITHM], audience=MODEL_CACHE_TOKEN_AUD
        )
    except JWTError:
        return None
    except RuntimeError:
        return None
    if claims.get("typ") != MODEL_CACHE_TOKEN_TYPE:
        return None
    if claims.get("tenant_id") is None:
        return None
    return claims


# ---------------------------------------------------------------------------
# Stack identity token (launch-completion-megaplan GX.S3).
# ---------------------------------------------------------------------------
# Every persistent stack gets an auto-injected, short-lived, auto-rotated
# token (same RS256 machinery: only the API holds the private key) scoped to
# its (stack_id, tenant_id) and, when an App targets the stack, that
# app_id. It is minted fresh at each deploy (redeploy = rotation) and
# delivered to the stack's containers as a FILE secret (never env), resolved
# at auth time to the stack's (or the app's) non-human service user
# (services/stack_identity.py). Distinct `typ` — not accepted by any other
# verifier here, and vice-versa.
#
# TODO(GX.S3b): in-place rotation via a worker refresh op (push a re-minted
# token to the running stack without a redeploy) would let this TTL drop far
# below the 7-day default; for now rotation is redeploy-driven, so the TTL
# must outlive the expected redeploy cadence.
STACK_IDENTITY_TOKEN_TYPE = "mod-stack-identity-v1"
DEFAULT_STACK_IDENTITY_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days (redeploy re-mints)


def mint_stack_identity_token(
    *,
    stack_id: int,
    tenant_id: int,
    app_id: Optional[int] = None,
    ttl_s: int = DEFAULT_STACK_IDENTITY_TTL_SECONDS,
) -> str:
    """Mint a signed, stack-scoped identity token. Treated like an STS
    session token — never persisted or logged by callers."""
    now = int(time.time())
    claims: Dict[str, Any] = {
        "typ": STACK_IDENTITY_TOKEN_TYPE,
        "stack_id": int(stack_id),
        "tenant_id": int(tenant_id),
        "app_id": (int(app_id) if app_id is not None else None),
        "iat": now,
        "exp": now + int(ttl_s),
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm=_ALGORITHM,
        headers={"kid": _kid(_public_key())},
    )


def verify_stack_identity_token(token: str, db=None) -> Optional[Dict[str, Any]]:
    """Verify signature + expiry + type. Returns claims (with stack_id,
    tenant_id, app_id) or None. Never raises — callers treat None as
    'not a stack identity token' and fall through to other auth."""
    if not token:
        return None
    key = _verify_key_for(token, db=db)
    if key is None:
        return None
    try:
        claims = jwt.decode(token, key, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    except RuntimeError:
        return None
    if claims.get("typ") != STACK_IDENTITY_TOKEN_TYPE:
        return None
    if claims.get("stack_id") is None or claims.get("tenant_id") is None:
        return None
    return claims


# ---------------------------------------------------------------------------
# MOD -> stack/app trigger-call token (launch-completion-megaplan GX.S7).
# ---------------------------------------------------------------------------
# When MOD fires a scheduled HTTP trigger (services/http_triggers) AT a
# Persistent Stack service or App target, it mints a short-TTL, single-use
# caller-identity token (typ ``mod-trigger-call-v1``, same RS256 manager key)
# and sends it as ``Authorization: Bearer <token>`` IN ADDITION TO the
# per-trigger HMAC headers (X-MOD-Signature / X-MOD-Timestamp — kept, so
# existing receivers keep working). The token binds the call to its
# (tenant, target, trigger, fired_at, body) — a receiver verifies signature +
# expiry + audience + the body hash to prove MOD itself made this call, not
# an imposter. The private key stays API-only (BUILD_TASK_TOKEN_*_KEY_B64 /
# per-manager Vault), so only MOD can mint. Distinct `typ` — not accepted by
# any other verifier here, and vice-versa.
TRIGGER_CALL_TOKEN_TYPE = "mod-trigger-call-v1"
TRIGGER_CALL_TOKEN_MAX_TTL_SECONDS = 300  # hard cap: short-lived by design

# `aud` prefixes: "stack:<id>" for stack-service targets, "app:<id>" for app
# targets. The verifier must pin the audience to the resource it IS.
TRIGGER_CALL_AUD_STACK = "stack"
TRIGGER_CALL_AUD_APP = "app"


def mint_trigger_call_token(
    *,
    tenant_id: int,
    target_kind: str,
    target_id: int,
    trigger_id: int,
    body: bytes,
    fired_at: int,
    ttl_seconds: int = TRIGGER_CALL_TOKEN_MAX_TTL_SECONDS,
) -> str:
    """Mint a signed, call-scoped MOD trigger-call token (RS256).

    ``target_kind`` is ``"stack"`` or ``"app"`` (``target_id`` the resource id
    — it becomes the ``aud``); ``body`` is the exact request body bytes the
    call is being made with (its SHA-256 is pinned in ``body_sha256``);
    ``fired_at`` is the unix fire timestamp (same one the X-MOD-Timestamp
    header carries). The TTL is capped at 300 s: the token is valid only for
    the one in-flight call. Never persisted or logged by callers."""
    if target_kind not in (TRIGGER_CALL_AUD_STACK, TRIGGER_CALL_AUD_APP):
        raise ValueError(f"target_kind must be 'stack' or 'app' (got {target_kind!r})")
    now = int(time.time())
    ttl = max(1, min(int(ttl_seconds), TRIGGER_CALL_TOKEN_MAX_TTL_SECONDS))
    claims: Dict[str, Any] = {
        "typ": TRIGGER_CALL_TOKEN_TYPE,
        "iss": "mod-trigger",
        "aud": f"{target_kind}:{int(target_id)}",
        "tenant_id": int(tenant_id),
        "trigger_id": int(trigger_id),
        "run_id": int(fired_at),  # the fire's unix timestamp (per-call nonce)
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "body_sha256": hashlib.sha256(body or b"").hexdigest(),
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm=_ALGORITHM,
        headers={"kid": _kid(_public_key())},
    )


def verify_trigger_call_token(
    token: str,
    *,
    tenant_id: int,
    target_kind: str,
    target_id: int,
    body: bytes,
    db=None,
) -> Optional[Dict[str, Any]]:
    """Verify a trigger-call token presented by MOD to a stack/app receiver.

    Checks signature (public key) + expiry + ``typ`` + ``aud`` pinned to
    ``<target_kind>:<target_id>`` + ``tenant_id`` + the body SHA-256. Returns
    the claims (incl. ``trigger_id`` and ``run_id`` = fired_at) or None. Never
    raises — the caller treats None as "reject, unauthenticated". Callers
    SHOULD additionally reject a replayed ``jti`` (e.g. an LRU of recent jti
    values) since the 300 s TTL window allows a captured token to be replayed
    until it expires."""
    if not token:
        return None
    key = _verify_key_for(token, db=db)
    if key is None:
        return None
    try:
        claims = jwt.decode(
            token, key,
            algorithms=[_ALGORITHM],
            audience=f"{target_kind}:{int(target_id)}",
        )
    except JWTError:
        return None
    except (RuntimeError, ValueError, TypeError):
        return None
    if claims.get("typ") != TRIGGER_CALL_TOKEN_TYPE:
        return None
    if claims.get("tenant_id") != int(tenant_id):
        return None
    if claims.get("body_sha256") != hashlib.sha256(body or b"").hexdigest():
        return None
    return claims
