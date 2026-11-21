# crypto-utils — MOD node container for the encryption / hashing domain primitives
# (launch-completion-megaplan sz-6.14.1). One container, eight operations:
#
#   CRYPTO_OP=hash             payload -> digest.txt        (sha256/sha384/sha512/sha3_256/blake2b)
#   CRYPTO_OP=legacy_checksum  payload -> checksum.txt      (md5/sha1 — NON-SECURITY)
#   CRYPTO_OP=hmac_sign        payload + key -> signature.txt
#   CRYPTO_OP=hmac_verify      payload + key + sig -> match.txt ('true'/'false', constant-time)
#   CRYPTO_OP=aes_encrypt      payload + 32B key -> ciphertext.txt (b64 nonce||ct||tag)
#   CRYPTO_OP=aes_decrypt      ciphertext + 32B key -> plaintext.bin / plaintext_b64.txt
#   CRYPTO_OP=encode           payload -> encoded.txt       (base64|hex)
#   CRYPTO_OP=decode           payload -> decoded.bin / decoded_b64.txt
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output. Fields arrive
# as /data/input/fields.json (the node's `data` port).
#
# Security posture: no network access at runtime, no eval/exec, no shell=True,
# inputs capped at 10 MB, bad input exits non-zero with JSON in
# /data/output/error.json. Keys come ONLY from the spec's secret KEY / KEY_B64
# parameters and are never logged nor echoed into output or error JSON.
#
# CNSA: AES-256-GCM + SHA-384/512 are the CNSA level-2 pair. md5/sha1 are
# quarantined in legacy_checksum and are integrity-of-known-data only.
#
# Authored only in sz-6.14.1 — build/push is a lead deploy step (no disk space
# for a build in the agent lane).

FROM python:3.11-slim

# The only non-stdlib dependency: AES-256-GCM (hashing, HMAC and base64/hex are
# all stdlib). Pinned to the version the API image already resolves.
RUN pip install --no-cache-dir cryptography==50.0.1

RUN mkdir -p /data/input /data/output /app

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync). This block is a
# verbatim mirror of ENTRYPOINT_SOURCE in
# CORE/API/node_definitions_domains/crypto_utils.py.
RUN cat > /app/crypto_process.py <<'PYEOF'
#!/usr/bin/env python3
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
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/crypto_process.py"]
