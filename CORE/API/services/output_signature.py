# API/services/output_signature.py
"""FX.97 p3a (owner decision Q83=A) — signed output hash with a per-install key.

FX.97 p1/p2 (services/provenance_report.py) report WHAT a run did; p3a makes
one output's content hash CREDIBLE: the platform signs a canonical
``sha256:<64-hex>`` message with an Ed25519 key that is minted once per
install and lives in Vault, so a verifier holding only the public half can
confirm "this install produced these exact bytes".

Key custody mirrors services/key_provider.py's Vault pattern:
* the 32-byte private key is generated once (``secrets.token_bytes``) and
  written to KV v2 at ``output-signing/key`` — never to env, never to a DB
  column, never printed or logged;
* ``key_id`` (``mod-out-<16 hex>``) is the PUBLIC handle: it is stored with
  every output and is what p3b's verify endpoint resolves;
* the private key is re-read from Vault per signing session and cached in
  process only while the DB-backed setting still names the same key_id, so
  an operator rotation in Vault takes effect on the next signing without a
  restart and a stale key is never silently reused.

Ed25519 comes from ``cryptography`` — already installed through
``python-jose[cryptography]`` (requirements.txt), so no new dependency. If it
is ever absent, signing degrades to UNSIGNED (the provenance report says so
under ``gaps``) rather than failing output registration.

p3b (the verify endpoint) is NOT built here; ``verify_signature`` below is
the service function it will call.

Settings: ONE platform setting, ``output.signature_key_id`` (group
compliance) — the public handle only, so the platform-settings audit trail
records which install signed what without any key material.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy.orm import Session  # noqa: F401 — session type for callers

logger = logging.getLogger(__name__)

ALGORITHM = "ed25519"
KEY_BYTES = 32
KEY_ID_PREFIX = "mod-out-"
VAULT_PATH = "output-signing/key"
SETTING_KEY = "output.signature_key_id"

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
    from cryptography.exceptions import InvalidSignature
    _ED25519_AVAILABLE = True
except ImportError:  # pragma: cover — CE builds without cryptography
    _ed25519 = None
    InvalidSignature = None
    _ED25519_AVAILABLE = False


def _canonical_message(content_hash: str) -> bytes:
    """The exact bytes that are signed/verified: b"sha256:<64-hex>".

    Canonical on the hash only (the content-addressed store already IS the
    SHA-256 of the bytes), so p3b can verify an output from its hash alone."""
    return f"sha256:{str(content_hash).strip()}".encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_key_id() -> str:
    return KEY_ID_PREFIX + secrets.token_hex(8)


# --- key provider (Vault by default; tests install a fake) ------------------

def _vault_token() -> str:
    """Same two auth paths the rest of the API already uses for Vault — the
    Vault Agent sidecar token file first (cheap, no round trip), then the
    service AppRole login crud.secret_cud performs."""
    path = os.getenv("VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    from crud.secret_cud import _vault_service_token
    return _vault_service_token()


def _vault_read() -> Dict[str, Any]:
    from utils.vault_client import read_kv_v2
    return read_kv_v2(VAULT_PATH, token=_vault_token()) or {}


def _vault_write(data: Dict[str, Any]) -> None:
    from utils.vault_client import write_kv_v2
    write_kv_v2(VAULT_PATH, token=_vault_token(), data=data)


_PROVIDER: Dict[str, Callable[[], Any]] = {"read": _vault_read, "write": _vault_write}


def set_key_provider(*, read: Callable[[], Any],
                     write: Callable[[Dict[str, Any]], None]) -> None:
    """Swap the key provider (tests use an in-memory fake; production never
    calls this). The key itself is still never logged."""
    _PROVIDER["read"], _PROVIDER["write"] = read, write


# The in-process key, valid only while the setting still names this key_id.
_CACHE: Dict[str, Optional[str]] = {"key_id": None, "private_b64": None}


def ensure_signing_key(db=None) -> Tuple[str, str]:
    """``(key_id, private_key_b64)`` for this install — minted once, then read
    back from the provider. Pass the caller's ``db`` session to participate
    in its transaction (the setting write is flushed, not committed)."""
    live = None
    if db is not None:
        from services import platform_settings as psr
        live = psr.get_setting(SETTING_KEY, db=db) or None
    if live and _CACHE["key_id"] == live and _CACHE["private_b64"]:
        return live, _CACHE["private_b64"]

    data = _PROVIDER["read"]() or {}
    key_id = (data.get("key_id") or "").strip() or None
    private_b64 = (data.get("private_key_b64") or "").strip() or None
    if not key_id or not private_b64:
        key_id = _new_key_id()
        private_b64 = base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")
        _PROVIDER["write"]({"key_id": key_id, "private_key_b64": private_b64})

    if db is not None:
        from services import platform_settings as psr
        psr.set_setting(SETTING_KEY, key_id, db=db, actor_user_id=None)
    _CACHE["key_id"], _CACHE["private_b64"] = key_id, private_b64
    return key_id, private_b64


def signing_key_id(db=None) -> Optional[str]:
    """The install's PUBLIC key handle, or None when none was minted yet."""
    from services import platform_settings as psr
    return (psr.get_setting(SETTING_KEY, db=db) or None)


# --- sign / verify ----------------------------------------------------------

def sign_output(content_hash: str, db=None) -> Optional[Dict[str, str]]:
    """Sign one output's content hash at registration time.

    Returns ``{algorithm, key_id, signature_b64, signed_at}`` — the three
    fields stored with the output — or None when this install cannot sign
    (no Ed25519, or the Vault key is unreachable). Signing is best-effort:
    an output that cannot be signed is still recorded, unsigned, and the
    provenance report lists the gap."""
    if not _ED25519_AVAILABLE:
        logger.warning("output signing: Ed25519 unavailable in this build; "
                       "output left unsigned")
        return None
    from utils.vault_client import VaultError  # lazy, same style as _vault_read/_vault_write
    try:
        key_id, private_b64 = ensure_signing_key(db=db)
        signature = _ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_b64)).sign(_canonical_message(content_hash))
    except VaultError:  # noqa: BLE001 — unprovisioned/unreachable Vault key:
        # boot-race / no-key install: one line, no traceback — the output is
        # still recorded unsigned and the provenance report lists the gap
        logger.warning("output signing: unavailable for this install; "
                       "output left unsigned")
        return None
    except Exception:  # noqa: BLE001 — never break output registration
        logger.exception("output signing: unavailable for this install; "
                         "output left unsigned")
        return None
    return {
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "signature_b64": base64.b64encode(signature).decode("ascii"),
        "signed_at": _now_iso(),
    }


def verify_signature(content_hash: str, key_id: Optional[str],
                     signature_b64: Optional[str], db=None) -> bool:
    """True only for a valid Ed25519 signature of the canonical hash message
    under the install key NAMED by ``key_id``. p3b's endpoint wraps this."""
    if not _ED25519_AVAILABLE or not key_id or not signature_b64:
        return False
    try:
        live, private_b64 = ensure_signing_key(db=db)
    except Exception:  # noqa: BLE001 — a verify call must not 500 the caller
        logger.warning("output signature verify: key unavailable")
        return False
    if live != key_id:
        # Signed by a different install (or a rotated key): not verifiable
        # here — p3b reports it as foreign/rotated, never as "tampered".
        return False
    try:
        _ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_b64)).public_key().verify(
            base64.b64decode(signature_b64), _canonical_message(content_hash))
        return True
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001 — malformed b64/key is a failed verify
        logger.warning("output signature verify: key_id %s unreadable", key_id)
        return False


# --- registration hook ------------------------------------------------------

def record_output_signature(db, content_hash: Optional[str],
                            tenant_id: Optional[int]) -> Optional[Dict[str, str]]:
    """Sign + store one freshly registered workflow output.

    Called by the two output-registration seams (routers/worker_tasks.py —
    the live worker-result path — and utils/minio_helpers.py — the
    container-side hash-upload path) right after the WorkflowNodeOutput row
    is added. Best-effort by contract: any failure here is logged and the
    output simply stays unsigned (the provenance report lists the gap);
    output registration NEVER fails because signing did not.

    Idempotent: an existing live (content_hash, algorithm, key_id) row is
    reused, so a re-produced hash under the same install key costs no new
    row and no re-sign. Returns the stored fields, or None unsigned."""
    import models
    from models import OutputSignature

    if not content_hash or tenant_id is None:
        return None
    existing = (db.query(OutputSignature)
                .filter(OutputSignature.content_hash == content_hash,
                        OutputSignature.algorithm == ALGORITHM,
                        OutputSignature.key_id == signing_key_id(db=db),
                        OutputSignature.deleted_at.is_(None))
                .first())
    if existing is not None:
        return {"algorithm": existing.algorithm,
                "key_id": existing.key_id,
                "signature_b64": existing.signature_b64,
                "signed_at": existing.signed_at.isoformat()
                if existing.signed_at else None}

    signed = sign_output(content_hash, db=db)
    if signed is None:
        return None
    row = OutputSignature(
        content_hash=content_hash,
        algorithm=signed["algorithm"],
        key_id=signed["key_id"],
        signature_b64=signed["signature_b64"],
        tenant_id=tenant_id,
        signed_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return signed


def signature_for(db, content_hash: Optional[str]) -> Optional[Dict[str, Any]]:
    """The live signature row for one content hash (any key), or None.

    Read side for the provenance report (FX.97 p1/p2) and for p3b. Newest
    live row wins — a tombstoned (rotated) signature is never served."""
    import models
    from models import OutputSignature

    if not content_hash:
        return None
    row = (db.query(OutputSignature)
           .filter(OutputSignature.content_hash == content_hash,
                   OutputSignature.deleted_at.is_(None))
           .order_by(OutputSignature.signed_at.desc().nullslast())
           .first())
    if row is None:
        return None
    return {
        "algorithm": row.algorithm,
        "key_id": row.key_id,
        "signature_b64": row.signature_b64,
        "signed_at": row.signed_at.isoformat() if row.signed_at else None,
    }
