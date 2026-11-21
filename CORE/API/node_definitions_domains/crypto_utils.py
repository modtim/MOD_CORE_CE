"""node_definitions_domains/crypto_utils — Encryption / hashing domain-primitive
NodeDefinitions (Phase 6 domain primitive, sz-6.14.1).

Registers eight system node definitions backed by the crypto-utils container
(`CORE/DockerContainers/crypto-utils`): content hash, legacy (non-security)
checksum, HMAC-SHA256 sign, HMAC-SHA256 verify, AES-256-GCM encrypt, AES-256-GCM
decrypt, encode (base64/hex) and decode (base64/hex).

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py`) so the module is
  importable + testable without a DB; `register_crypto_utils()` is the only
  DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already speaks:
  the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility checks,
  AND named ports (`data` -> `digest`/`checksum`/`signature`/`match`/
  `ciphertext`/`plaintext_bin`+`plaintext_b64`/`encoded`/`decoded_bin`+
  `decoded_b64`) so the UI can label the ports this domain actually exposes.
  this domain actually exposes. `text` and `binary` are both in
  `node_builder/analyze_prompt.py`'s VALID_INPUT_TYPES.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing system
  row rather than duplicating it (system-node names are globally unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `seed_converters.py` does — existing rows are never deleted.
- Keys/secret material: every key parameter is declared with `"secret": True`
  in its `configuration_schema.extra_variables` entry (the same shape
  `seed_switchyard_llm_node.py` uses for `API_KEY`), so the platform can route
  it through a secret reference instead of a typed literal. The entrypoint never
  logs a key and scrubs any key value out of error text before it reaches the
  error JSON.

CNSA note
---------
AES-256-GCM plus SHA-384/SHA-512 are the NSA CNSA level-2 primitives
(FIPS-201-class hash + 256-bit authenticated AES); `Crypto Hash` therefore
offers SHA-256/384/512, SHA3-256 and BLAKE2b, and md5/sha1 are quarantined in
the separate `Crypto Legacy Checksum` node, which is labelled non-security and
is for integrity-of-known-data only (never passwords, tokens, or signatures).
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_REPO_TAG = "crypto-utils:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/crypto-utils/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via heredoc so the container
# is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# crypto-utils entrypoint — one operation per run, selected by CRYPTO_OP.
#
#   export CRYPTO_OP=hash|legacy_checksum|hmac_sign|hmac_verify|aes_encrypt|aes_decrypt|encode|decode
#   python3 /app/crypto_process.py
#
# Fields are read from /data/input/fields.json (the node's `data` port); results
# are written to /data/output. On any failure the entrypoint prints exactly one
# line of error JSON to stdout (plus a /data/output/error.json sidecar) and
# exits non-zero. Keys are read ONLY from the `key`/`key_b64` fields,
# which the node spec marks secret; they are never logged and never echoed into
# error output. No network access, no eval/exec, no shell.

import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_SIZE = 10 * 1024 * 1024  # 10 MB input cap

# Security-grade digests only. md5/sha1 live in legacy_checksum, below.
HASH_ALGORITHMS = ("sha256", "sha384", "sha512", "sha3_256", "blake2b")
# NON-SECURITY: integrity of known-good data only. Never for passwords,
# tokens, MAC keys, or anything security-relevant.
LEGACY_ALGORITHMS = ("md5", "sha1")
CODECS = ("base64", "hex")

# Vet step 5 error contract: exactly one JSON line on stdout, then exit 1.
CODE_NO_INPUT = "NO_INPUT"
CODE_BAD_PARAM = "BAD_PARAM"
CODE_PROCESSING = "PROCESSING_FAILURE"
CODE_OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
CODE_INTERNAL = "INTERNAL"


class NodeError(ValueError):
    # Bad-input failure carrying its contract code. Subclasses ValueError so the
    # per-op unit tests (pytest.raises(ValueError, match=...)) keep passing while
    # main() maps each raise to its code.
    def __init__(self, msg, code=CODE_BAD_PARAM):
        super().__init__(msg)
        self.code = code


# Set when a key is read so fail() can scrub it out of any error text.
_SECRET_SEEN = None


def _note_secret(value):
    global _SECRET_SEEN
    if value:
        _SECRET_SEEN = value


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _write_bytes(name, value):
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_bytes(value)
    except OSError as exc:
        raise NodeError(f"could not write {name}: {exc}", CODE_OUTPUT_NOT_WRITTEN) from exc


def _write_text(name, value):
    _write_bytes(name, value.encode("utf-8"))


def _write_error(msg):
    # Best-effort sidecar; the stdout JSON from fail() is the contract.
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")
    except OSError:
        pass


def _scrub(msg):
    # A key must never reach the error text, in any of the three spellings a
    # caller could have echoed it in. Plaintext and raw input bytes never do
    # either: every message below this point is written by hand.
    if not _SECRET_SEEN:
        return msg
    for needle in (
        _SECRET_SEEN.decode("utf-8", "ignore"),
        _SECRET_SEEN.hex(),
        base64.b64encode(_SECRET_SEEN).decode("ascii"),
    ):
        if needle:
            msg = msg.replace(needle, "[redacted]")
    return msg


def fail(msg, code=CODE_INTERNAL):
    # Vet step 5: one JSON line on stdout, then a non-zero exit.
    msg = _scrub(msg)
    _log(f"ERROR: {msg}")
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    _write_error(msg)
    sys.exit(1)


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        raise NodeError("fields.json required on the input port", CODE_NO_INPUT)
    raw = p.read_bytes()
    if len(raw) > MAX_SIZE:
        raise NodeError(f"fields.json exceeds the {MAX_SIZE}-byte input cap", CODE_NO_INPUT)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Never echo the offending bytes back: the message stays generic.
        raise NodeError("fields.json is not valid JSON", CODE_BAD_PARAM) from None
    if not isinstance(data, dict):
        raise NodeError("fields.json must be a JSON object", CODE_BAD_PARAM)
    return _normalize_fields(data)


# The platform hands us fields.json keyed by the spec's parameter names, which
# this domain spells in caps (HASH_ALGORITHM, KEY_B64, ...). Accept the lowercase
# spelling too, and fold the two per-node algorithm names onto one `algorithm`
# key so each op stays a single pure function.
FIELD_ALIASES = {
    "hash_algorithm": "algorithm",
    "legacy_algorithm": "algorithm",
}


def _normalize_fields(data):
    out = {}
    for key, value in data.items():
        low = str(key).strip().lower()
        out[low] = value
        out[FIELD_ALIASES.get(low, low)] = value
    return out


def _payload(fields):
    # Payload bytes from `payload_b64` (raw bytes, base64) or `payload` (UTF-8 text).
    b64 = fields.get("payload_b64")
    if b64 is not None and str(b64).strip() != "":
        try:
            raw = base64.b64decode(str(b64), validate=True)
        except ValueError:
            raise NodeError("payload_b64 is not valid base64", CODE_BAD_PARAM) from None
    else:
        raw = str(fields.get("payload", "")).encode("utf-8")
    if len(raw) > MAX_SIZE:
        raise NodeError(f"payload exceeds the {MAX_SIZE}-byte input cap", CODE_NO_INPUT)
    return raw


def _key_bytes(fields):
    # Key bytes from `key_b64` (preferred, exact bytes) or `key` (UTF-8 text).
    b64 = fields.get("key_b64")
    if b64 is not None and str(b64).strip() != "":
        try:
            raw = base64.b64decode(str(b64), validate=True)
        except ValueError:
            raise NodeError("key_b64 is not valid base64", CODE_BAD_PARAM) from None
    else:
        raw = str(fields.get("key", "")).encode("utf-8")
    if not raw:
        raise NodeError(
            "Missing key: supply key or key_b64 on the secret key parameter", CODE_NO_INPUT
        )
    _note_secret(raw)
    return raw


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def op_hash(fields):
    # Security-grade digest of the payload -> digest.txt (hex).
    algo = str(fields.get("algorithm", "sha256")).strip().lower()
    if algo not in HASH_ALGORITHMS:
        raise NodeError(
            f"algorithm {algo!r} not in {HASH_ALGORITHMS}; md5/sha1 belong to "
            "Crypto Legacy Checksum, which is non-security",
            CODE_BAD_PARAM,
        )
    data = _payload(fields)
    digest = hashlib.new(algo, data).hexdigest()
    _write_text("digest.txt", digest)
    _log(f"hashed {len(data)} bytes with {algo} -> digest.txt")


def op_legacy_checksum(fields):
    # NON-SECURITY checksum (md5/sha1) -> checksum.txt (hex).
    algo = str(fields.get("algorithm", "md5")).strip().lower()
    if algo not in LEGACY_ALGORITHMS:
        raise NodeError(f"legacy_checksum only accepts {LEGACY_ALGORITHMS}", CODE_BAD_PARAM)
    data = _payload(fields)
    digest = hashlib.new(algo, data).hexdigest()
    _write_text("checksum.txt", digest)
    _log(
        f"WARN: {algo} is a NON-SECURITY checksum (integrity of known data only) — "
        "use Crypto Hash for anything security-relevant"
    )


def op_hmac_sign(fields):
    # HMAC-SHA256 of the payload under the secret key -> signature.txt (hex).
    key = _key_bytes(fields)
    data = _payload(fields)
    signature = hmac.new(key, data, hashlib.sha256).hexdigest()
    _write_text("signature.txt", signature)
    _log(f"signed {len(data)} bytes with HMAC-SHA256 -> signature.txt")


def op_hmac_verify(fields):
    # Constant-time HMAC-SHA256 verify -> match.txt ('true'/'false').
    expected = str(fields.get("signature", "")).strip()
    if not expected:
        raise NodeError("Missing signature: supply the hex digest to verify", CODE_NO_INPUT)
    key = _key_bytes(fields)
    data = _payload(fields)
    actual = hmac.new(key, data, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(actual, expected)
    _write_text("match.txt", "true" if ok else "false")
    if not ok:
        _log("WARN: signature mismatch")


def op_aes_encrypt(fields):
    # AES-256-GCM encrypt -> ciphertext.txt (base64 of nonce||ciphertext||tag).
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _key_bytes(fields)
    if len(key) != 32:
        raise NodeError(f"AES-256-GCM needs a 32-byte key, got {len(key)} bytes", CODE_BAD_PARAM)
    data = _payload(fields)
    nonce = os.urandom(12)  # 96-bit random nonce, GCM standard construction
    blob = nonce + AESGCM(key).encrypt(nonce, data, None)
    _write_text("ciphertext.txt", base64.b64encode(blob).decode("ascii"))
    _log(f"encrypted {len(data)} bytes with AES-256-GCM -> ciphertext.txt")


def op_aes_decrypt(fields):
    # AES-256-GCM decrypt of ciphertext.txt -> plaintext.bin + plaintext_b64.txt.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw_ct = str(fields.get("ciphertext", "")).strip()
    if not raw_ct:
        raise NodeError("Missing ciphertext: supply the base64 blob to decrypt", CODE_NO_INPUT)
    try:
        blob = base64.b64decode(raw_ct, validate=True)
    except ValueError:
        raise NodeError("ciphertext is not valid base64", CODE_BAD_PARAM) from None
    if len(blob) < 12 + 16:
        raise NodeError("ciphertext too short: expected nonce||ciphertext||tag", CODE_BAD_PARAM)
    key = _key_bytes(fields)
    if len(key) != 32:
        raise NodeError(f"AES-256-GCM needs a 32-byte key, got {len(key)} bytes", CODE_BAD_PARAM)
    try:
        plaintext = AESGCM(key).decrypt(blob[:12], blob[12:], None)
    except Exception:
        # The library's own message is generic, but never pass it through: a
        # tag mismatch is the only reliable signal and must not leak anything.
        raise NodeError("decryption failed: wrong key or corrupt ciphertext", CODE_PROCESSING)
    _write_bytes("plaintext.bin", plaintext)
    _write_text("plaintext_b64.txt", base64.b64encode(plaintext).decode("ascii"))
    _log(f"decrypted {len(plaintext)} bytes -> plaintext.bin")


def op_encode(fields):
    # Encode the payload with `codec` (base64|hex) -> encoded.txt.
    codec = str(fields.get("codec", "base64")).strip().lower()
    if codec not in CODECS:
        raise NodeError(f"codec {codec!r} not in {CODECS}", CODE_BAD_PARAM)
    data = _payload(fields)
    if codec == "base64":
        out = base64.b64encode(data).decode("ascii")
    else:
        out = data.hex()
    _write_text("encoded.txt", out)
    _log(f"encoded {len(data)} bytes as {codec} -> encoded.txt")


def op_decode(fields):
    # Decode `payload` with `codec` (base64|hex) -> decoded.bin + decoded_b64.txt.
    codec = str(fields.get("codec", "base64")).strip().lower()
    if codec not in CODECS:
        raise NodeError(f"codec {codec!r} not in {CODECS}", CODE_BAD_PARAM)
    raw = str(fields.get("payload", "")).strip()
    if not raw:
        raise NodeError("Missing payload: supply the encoded text to decode", CODE_NO_INPUT)
    try:
        data = base64.b64decode(raw, validate=True) if codec == "base64" else bytes.fromhex(raw)
    except ValueError:
        # Never echo the offending payload back.
        raise NodeError(f"payload is not valid {codec}", CODE_BAD_PARAM) from None
    _write_bytes("decoded.bin", data)
    _write_text("decoded_b64.txt", base64.b64encode(data).decode("ascii"))
    _log(f"decoded {len(data)} bytes from {codec} -> decoded.bin")


OPS = {
    "hash": op_hash,
    "legacy_checksum": op_legacy_checksum,
    "hmac_sign": op_hmac_sign,
    "hmac_verify": op_hmac_verify,
    "aes_encrypt": op_aes_encrypt,
    "aes_decrypt": op_aes_decrypt,
    "encode": op_encode,
    "decode": op_decode,
}


def main():
    try:
        op = os.getenv("CRYPTO_OP", "hash").strip().lower()
        # The spec default is the node's own op key ("crypto_hash"); the container
        # speaks the short name. Accept either spelling.
        if op not in OPS and op.startswith("crypto_"):
            op = op[len("crypto_"):]
        if op not in OPS:
            fail(f"unknown CRYPTO_OP {op!r}", CODE_BAD_PARAM)

        fields = _read_fields()
        OPS[op](fields)
    except SystemExit:
        raise
    except NodeError as exc:
        fail(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        fail(f"{type(exc).__name__}: {exc}", CODE_INTERNAL)


if __name__ == "__main__":
    main()
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

CRYPTO_HASH_SPEC = {
    "name": "Crypto Hash",
    "description": (
        "Security-grade digest of a file or text payload. Reads the payload on "
        "the node's input port (`data`) and emits `digest` — the hex digest "
        "under the algorithm picked by the HASH_ALGORITHM parameter: sha256 "
        "(default), sha384, sha512, sha3_256 or blake2b. SHA-384/SHA-512 with "
        "AES-256-GCM are the CNSA level-2 pair; md5/sha1 are deliberately NOT "
        "offered here — they live in Crypto Legacy Checksum, which is labelled "
        "non-security. Backed by the crypto-utils container (stdlib hashlib)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_hash",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Payload to hash (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "digest": {"type": "text", "description": "Hex digest of the payload."},
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_hash",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "HASH_ALGORITHM": {
                "type": "string",
                "default": "sha256",
                "enum": ["sha256", "sha384", "sha512", "sha3_256", "blake2b"],
                "description": (
                    "Digest algorithm. sha256 is the everyday default; pick "
                    "sha384/sha512 when pairing with AES-256-GCM for CNSA-2.0."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

CRYPTO_LEGACY_CHECKSUM_SPEC = {
    "name": "Crypto Legacy Checksum",
    "description": (
        "NON-SECURITY checksum (md5 or sha1) of a payload, for verifying "
        "integrity of data you already trust — mirror checksums, legacy "
        "manifests, old tooling. Emits `checksum` (hex). These algorithms are "
        "collision-prone and cryptographically broken: NEVER use them for "
        "passwords, tokens, MAC keys, signatures, or anything security-relevant; "
        "use Crypto Hash instead. Backed by the crypto-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_legacy_checksum",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Payload to checksum (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "checksum": {"type": "text", "description": "Hex md5/sha1 checksum."},
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_legacy_checksum",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "LEGACY_ALGORITHM": {
                "type": "string",
                "default": "md5",
                "enum": ["md5", "sha1"],
                "description": "md5 or sha1. Non-security: integrity only.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

CRYPTO_HMAC_SIGN_SPEC = {
    "name": "Crypto HMAC Sign",
    "description": (
        "HMAC-SHA256 signature of a payload under a secret key. Reads the "
        "payload on `data` and emits `signature` (hex). The key comes ONLY from "
        "the secret KEY parameter (or KEY_B64 for exact bytes) — it is never "
        "logged and never echoed into output or error JSON. Pair with Crypto "
        "HMAC Verify to check a signature without shipping the key. "
        "Backed by the crypto-utils container (stdlib hmac/hashlib)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_hmac_sign",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Payload to sign (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "signature": {"type": "text", "description": "Hex HMAC-SHA256 signature."},
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_hmac_sign",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "KEY": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Secret HMAC key (UTF-8 text). Prefer a secret reference "
                    "mapped to KEY over typing it here. Never logged."
                ),
            },
            "KEY_B64": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Base64-encoded key, when you need exact bytes rather than "
                    "text. Takes precedence over KEY. Never logged."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

CRYPTO_HMAC_VERIFY_SPEC = {
    "name": "Crypto HMAC Verify",
    "description": (
        "Verify an HMAC-SHA256 signature against a payload using a constant-time "
        "comparison. Reads the payload on `data` and emits `match` — 'true' or "
        "'false'. The key comes ONLY from the secret KEY/KEY_B64 parameter; the "
        "expected signature is supplied as the SIGNATURE parameter. Backed by "
        "the crypto-utils container (stdlib hmac.compare_digest)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_hmac_verify",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Payload whose signature is checked."},
    },
    "output_schema": {
        "output_type": "text",
        "match": {"type": "text", "description": "'true' or 'false'."},
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_hmac_verify",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "KEY": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": "Secret HMAC key (UTF-8 text). Never logged.",
            },
            "KEY_B64": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": "Base64 key (exact bytes). Takes precedence over KEY.",
            },
            "SIGNATURE": {
                "type": "string",
                "default": "",
                "description": "Hex HMAC-SHA256 signature you expect.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

CRYPTO_AES_ENCRYPT_SPEC = {
    "name": "Crypto AES Encrypt",
    "description": (
        "Authenticated symmetric encryption: AES-256-GCM with a fresh random "
        "96-bit nonce per run, emitting `ciphertext` — base64 of nonce||"
        "ciphertext||tag, the standard GCM construction. AES-256 plus a "
        "SHA-384/512 digest is the CNSA level-2 pair. The key comes ONLY from "
        "the secret KEY_B64 parameter (base64 of exactly 32 bytes) or the "
        "secret KEY parameter (UTF-8 text); neither is ever logged nor echoed "
        "into output or error JSON. Backed by the crypto-utils container "
        "(the 'cryptography' package, AESGCM)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_aes_encrypt",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Plaintext payload (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "ciphertext": {
            "type": "text",
            "description": "Base64 of nonce||ciphertext||tag.",
        },
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_aes_encrypt",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "KEY_B64": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Base64 of the 32-byte AES-256 key. Generate one with "
                    "os.urandom(32); supply it as a secret reference, not a "
                    "typed literal. Never logged."
                ),
            },
            "KEY": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Text key (UTF-8), must be exactly 32 characters. Prefer "
                    "KEY_B64. Never logged."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 2,
}

CRYPTO_AES_DECRYPT_SPEC = {
    "name": "Crypto AES Decrypt",
    "description": (
        "Decrypt the base64 nonce||ciphertext||tag blob produced by Crypto AES "
        "Encrypt (AES-256-GCM). Emits `plaintext_bin` (the recovered bytes) and "
        "`plaintext_b64` (base64, for binary payloads). A wrong key or a "
        "corrupt blob fails with a generic error that leaks nothing, and the "
        "key itself is never echoed. Key comes ONLY from the secret KEY_B64 / "
        "KEY parameters. Backed by the crypto-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_aes_decrypt",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Blob to decrypt (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "plaintext_bin": {
            "type": "binary",
            "description": "Recovered plaintext, raw bytes.",
        },
        "plaintext_b64": {
            "type": "text",
            "description": "Recovered plaintext, base64 (binary-safe).",
        },
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_aes_decrypt",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "KEY_B64": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": "Base64 of the 32-byte AES-256 key. Never logged.",
            },
            "KEY": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": "Text key (exactly 32 chars). Never logged.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 2,
}

CRYPTO_ENCODE_SPEC = {
    "name": "Crypto Encode",
    "description": (
        "Encode a payload as base64 or hex. Reads the payload on `data` and "
        "emits `encoded`. Encoding/decoding is transport framing, not "
        "encryption: it provides no secrecy. Backed by the crypto-utils "
        "container (stdlib base64)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_encode",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Payload to encode (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "encoded": {"type": "text", "description": "base64 or hex text."},
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_encode",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "CODEC": {
                "type": "string",
                "default": "base64",
                "enum": ["base64", "hex"],
                "description": "base64 or hex.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

CRYPTO_DECODE_SPEC = {
    "name": "Crypto Decode",
    "description": (
        "Decode a base64 or hex payload. Reads the encoded text on `data` and "
        "emits `decoded_bin` (the recovered bytes) plus `decoded_b64` (base64, "
        "payloads). Strict decoding: stray characters are an error, not a "
        "silent drop. Backed by the crypto-utils container (stdlib base64)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crypto_decode",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Encoded text (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "decoded_bin": {
            "type": "binary",
            "description": "Decoded payload, raw bytes.",
        },
        "decoded_b64": {
            "type": "text",
            "description": "Decoded payload, base64 (binary-safe).",
        },
    },
    "configuration_schema": {
        "CRYPTO_OP": {
            "type": "string",
            "default": "crypto_decode",
            "description": (
                "Fixed for this node: selects the crypto-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "CODEC": {
                "type": "string",
                "default": "base64",
                "enum": ["base64", "hex"],
                "description": "base64 or hex.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

NODE_DEFINITIONS = [
    CRYPTO_HASH_SPEC,
    CRYPTO_LEGACY_CHECKSUM_SPEC,
    CRYPTO_HMAC_SIGN_SPEC,
    CRYPTO_HMAC_VERIFY_SPEC,
    CRYPTO_AES_ENCRYPT_SPEC,
    CRYPTO_AES_DECRYPT_SPEC,
    CRYPTO_ENCODE_SPEC,
    CRYPTO_DECODE_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "crypto_hash": CRYPTO_HASH_SPEC,
    "crypto_legacy_checksum": CRYPTO_LEGACY_CHECKSUM_SPEC,
    "crypto_hmac_sign": CRYPTO_HMAC_SIGN_SPEC,
    "crypto_hmac_verify": CRYPTO_HMAC_VERIFY_SPEC,
    "crypto_aes_encrypt": CRYPTO_AES_ENCRYPT_SPEC,
    "crypto_aes_decrypt": CRYPTO_AES_DECRYPT_SPEC,
    "crypto_encode": CRYPTO_ENCODE_SPEC,
    "crypto_decode": CRYPTO_DECODE_SPEC,
}

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_crypto_utils(db: Session) -> int:
    """Register the 8 crypto operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping crypto domain-primitive seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,  # system node — available to all tenants
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return created
