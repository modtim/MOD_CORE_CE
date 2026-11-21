# In Main/utils/encryption.py
"""
Encryption utilities with key rotation support.

CMMC/ISO 27001 Compliance: SC-13 (Cryptographic Key Management)

Key Rotation Policy:
- Keys should be rotated at least annually or after suspected compromise
- Multiple keys can be active during rotation period (MultiFernet)
- Old keys are kept for decryption only, new keys used for encryption
- Key version is tracked via KEY_VERSION environment variable

Environment Variables:
- SECRET_ENCRYPTION_KEY: Current primary encryption key (required)
- SECRET_ENCRYPTION_KEY_PREVIOUS: Previous key for decryption during rotation (optional)
- ENCRYPTION_KEY_VERSION: Current key version for audit tracking (optional, default: 1)
"""

import hashlib
import hmac
import os
import logging
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# Key version for audit tracking
ENCRYPTION_KEY_VERSION = os.getenv("ENCRYPTION_KEY_VERSION", "1")

# Current (primary) encryption key - REQUIRED
ENCRYPTION_KEY = os.getenv("SECRET_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "SECURITY ERROR: SECRET_ENCRYPTION_KEY environment variable not set. "
        "This is required for CMMC/ISO 27001 compliance."
    )

# Previous key for rotation support - OPTIONAL
# During key rotation, set this to the old key to decrypt existing data
ENCRYPTION_KEY_PREVIOUS = os.getenv("SECRET_ENCRYPTION_KEY_PREVIOUS")

# Create Fernet instances
_primary_fernet = Fernet(ENCRYPTION_KEY.encode())

# MultiFernet for key rotation support
# First key is used for encryption, all keys tried for decryption
if ENCRYPTION_KEY_PREVIOUS:
    _previous_fernet = Fernet(ENCRYPTION_KEY_PREVIOUS.encode())
    master_fernet = MultiFernet([_primary_fernet, _previous_fernet])
    logger.info(
        f"Encryption initialized with key rotation support (version: {ENCRYPTION_KEY_VERSION}). "
        "Previous key available for decryption."
    )
else:
    master_fernet = _primary_fernet
    logger.info(f"Encryption initialized (version: {ENCRYPTION_KEY_VERSION})")

def generate_key() -> str:
    """Generates a new Fernet encryption key."""
    return Fernet.generate_key().decode()


def get_key_version() -> str:
    """Get current encryption key version for audit tracking."""
    return ENCRYPTION_KEY_VERSION


def encrypt_value(value: str, key: str) -> str:
    """
    Encrypts a string value using the provided Fernet key.

    CMMC SC-13: Uses AES-128-CBC via Fernet specification.
    """
    if not value:
        return ""
    fernet = Fernet(key.encode())
    return fernet.encrypt(value.encode()).decode()


def decrypt_value(encrypted_value: str, key: str) -> str:
    """
    Decrypts a string value using the provided Fernet key.

    CMMC SC-13: Supports key rotation by trying current key first.
    """
    if not encrypted_value:
        return ""
    fernet = Fernet(key.encode())
    try:
        return fernet.decrypt(encrypted_value.encode()).decode()
    except InvalidToken:
        # Handle cases where the token is invalid or corrupted
        logger.warning("Decryption failed - invalid token or incorrect key")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt secret due to invalid token or incorrect key."
        )


def rotate_encrypted_value(encrypted_value: str, old_key: str, new_key: str) -> str:
    """
    Re-encrypt a value with a new key during key rotation.

    CMMC/ISO 27001 Compliance: SC-13 (Cryptographic Key Management)

    Use this during key rotation to re-encrypt existing data with the new key.

    Args:
        encrypted_value: Value encrypted with old_key
        old_key: The key used to encrypt the value
        new_key: The new key to use for encryption

    Returns:
        Value encrypted with new_key
    """
    if not encrypted_value:
        return ""

    # Decrypt with old key
    decrypted = decrypt_value(encrypted_value, old_key)

    # Encrypt with new key
    return encrypt_value(decrypted, new_key)


def check_key_rotation_needed() -> dict:
    """
    Check if key rotation is recommended.

    Returns status information about encryption key configuration.

    CMMC SC-13: Keys should be rotated at least annually.
    """
    return {
        "key_version": ENCRYPTION_KEY_VERSION,
        "rotation_in_progress": ENCRYPTION_KEY_PREVIOUS is not None,
        "recommendation": (
            "Key rotation is in progress. Re-encrypt data and remove SECRET_ENCRYPTION_KEY_PREVIOUS."
            if ENCRYPTION_KEY_PREVIOUS
            else "Consider rotating keys annually or after any suspected compromise."
        )
    }


# ---------------------------------------------------------------------------
# CE-safe HMAC signing (license-free — HKDF fallback on SECRET_ENCRYPTION_KEY)
# ---------------------------------------------------------------------------
_HMAC_SIGN_HKDF_INFO = b"mod-ce-hmac-sign-v1"


def _signing_key_bytes() -> bytes:
    """Return HMAC signing key bytes.

    Paid tree: uses `LICENSE_SIGNING_KEY` directly (byte-identical to the
    original ``trial_license`` helpers).  CE tree: falls back to
    HKDF-SHA256 deriving a 32-byte key from ``SECRET_ENCRYPTION_KEY`` with a
    fixed domain-separated ``info`` label — the same shape already shipping
    in ``services/app_forward_auth.py``'s ``_derive_key()``.
    """
    key = os.getenv("LICENSE_SIGNING_KEY", "")
    if key:
        return key.encode("utf-8")
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=b"mod-ce-hmac-sign-salt-v1",
        info=_HMAC_SIGN_HKDF_INFO,
    ).derive(ENCRYPTION_KEY.encode("utf-8"))


def sign_payload(payload: str) -> str:
    """HMAC-SHA256 sign a payload string.

    CE-safe drop-in for ``utils.trial_license._sign_payload`` — no new env
    var; when ``LICENSE_SIGNING_KEY`` is absent (CE) it derives the key from
    ``SECRET_ENCRYPTION_KEY`` via HKDF.
    """
    return hmac.new(
        _signing_key_bytes(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(payload: str, signature: str) -> bool:
    """Constant-time verify of an HMAC-SHA256 signature.

    CE-safe drop-in for ``utils.trial_license._verify_signature``.
    """
    return hmac.compare_digest(sign_payload(payload), signature or "")