"""LDF-4 #6 — Platform-reserved secret names.

Tenant admins SHALL NOT be able to create or update a secret whose
name collides with a platform-managed credential. Without this gate
a tenant can shadow the platform's SMTP / Stripe / Keycloak / Vault
secrets and intercept email, payment, or auth flows.

This registry is the single source of truth. Adding a new platform-
managed credential? Append it here.

The check is case-insensitive.
"""
from __future__ import annotations

from typing import FrozenSet


PLATFORM_RESERVED_SECRET_NAMES: FrozenSet[str] = frozenset({
    # Email senders
    "SMTP_PASSWORD", "SMTP_USERNAME", "SMTP_HOST", "SMTP_PORT",
    "SMTP_FROM", "SMTP_FROM_NAME",
    "BREVO_API_KEY", "MAILGUN_API_KEY", "SENDGRID_API_KEY",
    "POSTMARK_API_KEY",
    # Payments
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PUBLISHABLE_KEY", "STRIPE_API_KEY",
    # Auth — Keycloak
    "KEYCLOAK_ADMIN_PASSWORD", "KEYCLOAK_ADMIN_USER",
    "KEYCLOAK_ADMIN_CLI_SECRET",
    "KEYCLOAK_FASTAPI_CLIENT_SECRET",
    "KEYCLOAK_FASTAPI_ADMIN_SECRET",
    "KEYCLOAK_DASHBOARD_CLIENT_SECRET",
    "KEYCLOAK_SERVICE_CLIENT_SECRET",
    "KEYCLOAK_VAULT_CLIENT_SECRET",
    "KEYCLOAK_MINIO_CLIENT_SECRET",
    # Platform-level
    "VAULT_ROOT_TOKEN", "VAULT_UNSEAL_KEY",
    "VAULT_APPROLE_ROLE_ID", "VAULT_APPROLE_SECRET_ID",
    "AUDIT_HMAC_KEY", "SECRET_ENCRYPTION_KEY",
    "LICENSE_SIGNING_KEY",
    "PLATFORM_SERVICE_API_KEY",
    "PLATFORM_SERVICE_PASSWORD",
    # Storage
    "S3_ROOT_USER", "S3_ROOT_PASSWORD",
    "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",  # legacy names honored for one release
    "MINIO_NOTIFY_TOKEN",  # file_event trigger webhook secret (was INTERNAL_TRIGGER_KEY)
    # Inbound-trigger channel shared tokens (gate POST /triggers/*)
    "TRIGGER_FS_TOKEN", "TRIGGER_EMAIL_TOKEN",
    "POSTGRES_PASSWORD", "POSTGRES_USER",
    "REDIS_PASSWORD",
    # External platform-managed APIs
    "GITHUB_TOKEN",
})


class ReservedSecretNameError(Exception):
    """Raised when a tenant admin tries to create / update a secret
    whose name collides with a platform-reserved name."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"Secret name '{name}' is reserved by the platform"
        )


def is_reserved_secret_name(name: str) -> bool:
    """Case-insensitive check against the registry."""
    if not isinstance(name, str):
        return False
    return name.upper() in PLATFORM_RESERVED_SECRET_NAMES


def assert_secret_name_allowed(name: str, *, is_platform_admin: bool) -> None:
    """Raise ReservedSecretNameError if a non-platform-admin caller
    is attempting a reserved name. Platform admins are unaffected.
    """
    if is_platform_admin:
        return
    if is_reserved_secret_name(name):
        raise ReservedSecretNameError(name)
