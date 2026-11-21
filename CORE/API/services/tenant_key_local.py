"""CE local tenant-key path. Byte-compatible with services.key_provider's
local provider. Non-CE backends live in key_provider (not shipped in CE).

Every ``Tenant.encryption_key`` is a per-tenant data-encryption key (DEK)
wrapped by the global master key ``SECRET_ENCRYPTION_KEY`` (a Fernet key
read from the environment — see ``utils/encryption.py``). That local path
is exactly what is copied here, verbatim, from ``services/key_provider.py``
so a DEK wrapped by either module unwraps identically under the other.

The Vault-transit / PKCS#11 / KMIP KEK backends are non-CE and stay in
``key_provider``; an envelope that one of them produced is recognised by
its own self-describing prefix and rejected with ``PaidKeyBackendRequired``
instead of being silently mis-decrypted.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from utils import encryption
from services._base import ConflictError

VAULT_CIPHERTEXT_PREFIX = "vault:v"  # Vault's own transit format marker
PKCS11_CIPHERTEXT_PREFIX = "pkcs11:v1:"
KMIP_CIPHERTEXT_PREFIX = "kmip:v1:"


class PaidKeyBackendRequired(RuntimeError):
    """Raised when an envelope was wrapped by a Paid KEK backend."""


def current_provider(db=None) -> str:
    return "local"


def _local_master_key() -> str:
    return os.getenv("SECRET_ENCRYPTION_KEY")


def is_vault_wrapped(envelope: Optional[str]) -> bool:
    return isinstance(envelope, str) and envelope.startswith(VAULT_CIPHERTEXT_PREFIX)


def is_pkcs11_wrapped(envelope: Optional[str]) -> bool:
    return isinstance(envelope, str) and envelope.startswith(
        PKCS11_CIPHERTEXT_PREFIX)


def is_kmip_wrapped(envelope: Optional[str]) -> bool:
    return isinstance(envelope, str) and envelope.startswith(
        KMIP_CIPHERTEXT_PREFIX)


def ciphertext_key_version(envelope: str) -> Optional[int]:
    """Vault ciphertext is ``vault:v<N>:...`` — pull N back out for
    evidence reporting. None for anything else (incl. malformed)."""
    if not is_vault_wrapped(envelope):
        return None
    try:
        return int(envelope.split(":")[1].lstrip("v"))
    except (IndexError, ValueError):
        return None


def generate_and_wrap_tenant_key(tenant_id: int, db=None) -> Tuple[str, str]:
    """Generate a brand-new tenant DEK and wrap it for storage.

    Returns ``(plaintext_key, wrapped_envelope)``. The plaintext is a
    Fernet key (urlsafe-base64, 32 raw bytes)."""
    plaintext = encryption.generate_key()
    wrapped = encryption.encrypt_value(plaintext, _local_master_key())
    return plaintext, wrapped


def unwrap_tenant_key(wrapped_envelope: str, tenant_id: int, db=None) -> str:
    """Unwrap a stored tenant DEK. Dispatch is on the envelope's own
    format, so an envelope produced by a Paid backend is recognised and
    refused rather than mis-decrypted."""
    if is_vault_wrapped(wrapped_envelope) or is_pkcs11_wrapped(wrapped_envelope) \
            or is_kmip_wrapped(wrapped_envelope):
        raise PaidKeyBackendRequired(
            f"Tenant {tenant_id}'s key was wrapped by a key backend "
            "(Vault transit / PKCS#11 / KMIP), which is not available in "
            "MOD Community Edition; this build cannot read it."
        )
    return encryption.decrypt_value(wrapped_envelope, _local_master_key())


def check_vault_transit_ready(db=None) -> None:
    """CE never has a Vault-transit KEK — refuse the setting change (409)."""
    raise ConflictError(
        "Cannot enable the Vault-transit KEK backend=vault_transit: the Vault-transit "
        "KEK backend is not available in MOD Community "
        "Edition."
    )


def destroy_tenant_transit_key(tenant_id: int, db=None) -> dict:
    """No per-tenant transit key exists in CE — nothing to destroy."""
    return {"destroyed": False, "key_name": f"tenant-{tenant_id}",
            "error": "Vault transit is not available in MOD Community Edition"}
