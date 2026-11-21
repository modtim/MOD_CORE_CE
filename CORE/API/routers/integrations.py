"""Integrations management — social login providers and email-verification toggle.

Note: the /integrations/smtp routes were retired in launch-day-followup-5
when platform SMTP moved to /admin/email-config (Vault-backed creds + last-
tested tracking + Keycloak realm sync). The Settings → Integrations UI now
calls /admin/email-config; nothing internal still references /integrations/smtp.
"""

from typing import List, Optional
from pydantic import BaseModel, ConfigDict
from fastapi import APIRouter, Depends, HTTPException
import logging

from dependencies import require_active_and_onboarded_user
from models import User
from keycloak_auth import keycloak_admin_client
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


# =========================================================================
# Schemas
# =========================================================================

class AuthProviderConfig(BaseModel):
    provider_type: str
    client_id: str
    client_secret: str


# Response models. Every field is Optional (default None) and extra keys are
# allowed, so these document the payloads without being able to reject or
# strip one.

class ConfigureAuthProviderResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None
    registration_url: Optional[str] = None
    configured: Optional[bool] = None
    help_text: Optional[str] = None


class RemoveAuthProviderResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None
    removed: Optional[bool] = None


class EmailVerificationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    email_verification_required: Optional[bool] = None
    status: Optional[str] = None
    detail: Optional[str] = None


class EmailTemplateHealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    templates: Optional[dict] = None
    missing: Optional[list] = None
    present: Optional[list] = None
    status: Optional[str] = None


class AuthProviderStatus(BaseModel):
    provider_type: str
    display_name: str
    configured: bool
    registration_url: str
    required_fields: List[str]
    help_text: str


PROVIDER_INFO = {
    "github": {
        "display_name": "GitHub",
        "registration_url": "https://github.com/settings/developers",
        "help_text": "Settings > Developer Settings > OAuth Apps > New OAuth App. Set callback URL to: {keycloak_url}/realms/{realm}/broker/github/endpoint",
        "required_fields": ["client_id", "client_secret"],
    },
    "google": {
        "display_name": "Google",
        "registration_url": "https://console.cloud.google.com/apis/credentials",
        "help_text": "Create OAuth 2.0 Client ID. Set authorized redirect URI to: {keycloak_url}/realms/{realm}/broker/google/endpoint",
        "required_fields": ["client_id", "client_secret"],
    },
    "linkedin-openid-connect": {
        "display_name": "LinkedIn",
        "registration_url": "https://www.linkedin.com/developers/apps",
        "help_text": "Create app > Auth tab > OAuth 2.0 settings. Add redirect URL: {keycloak_url}/realms/{realm}/broker/linkedin-openid-connect/endpoint",
        "required_fields": ["client_id", "client_secret"],
    },
    "facebook": {
        "display_name": "Facebook",
        "registration_url": "https://developers.facebook.com/apps",
        "help_text": "Create app > Facebook Login > Settings. Add redirect URI: {keycloak_url}/realms/{realm}/broker/facebook/endpoint",
        "required_fields": ["client_id", "client_secret"],
    },
    "microsoft": {
        "display_name": "Microsoft",
        "registration_url": "https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps",
        "help_text": "Register app > Authentication > Add redirect URI: {keycloak_url}/realms/{realm}/broker/microsoft/endpoint",
        "required_fields": ["client_id", "client_secret"],
    },
}


def _require_platform_admin(current_user: User = Depends(require_active_and_onboarded_user)):
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    return current_user


# =========================================================================
# Auth Provider Endpoints
# =========================================================================

@router.get("/auth-providers", response_model=List[AuthProviderStatus])
def list_auth_providers(current_user: User = Depends(_require_platform_admin)):
    """List all supported auth providers and their configuration status."""
    import os
    keycloak_url = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
    realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")

    try:
        configured_providers = keycloak_admin_client.list_identity_providers()
        configured_aliases = {p["alias"] for p in configured_providers}
    except Exception as e:
        logger.error("Failed to list identity providers: %s", e)
        configured_aliases = set()

    result = []
    for provider_type, info in PROVIDER_INFO.items():
        result.append(AuthProviderStatus(
            provider_type=provider_type,
            display_name=info["display_name"],
            configured=provider_type in configured_aliases,
            registration_url=info["registration_url"],
            required_fields=info["required_fields"],
            help_text=info["help_text"].format(keycloak_url=keycloak_url, realm=realm),
        ))
    return result


@router.post("/auth-providers", response_model=ConfigureAuthProviderResponse)
def configure_auth_provider(
    config: AuthProviderConfig,
    current_user: User = Depends(_require_platform_admin),
):
    """Configure a social login provider. Takes effect immediately — no restart needed."""
    if config.provider_type not in PROVIDER_INFO:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {config.provider_type}")

    try:
        result = keycloak_admin_client.configure_identity_provider(
            config.provider_type, config.client_id, config.client_secret,
        )
        return {"message": f"{PROVIDER_INFO[config.provider_type]['display_name']} configured", **result}
    except Exception as e:
        logger.error("Failed to configure %s: %s", config.provider_type, e)
        raise internal_http_error(500, "Failed to configure identity provider", e) from e


@router.delete("/auth-providers/{provider_type}", response_model=RemoveAuthProviderResponse)
def remove_auth_provider(
    provider_type: str,
    current_user: User = Depends(_require_platform_admin),
):
    """Remove a social login provider."""
    if provider_type not in PROVIDER_INFO:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_type}")

    try:
        keycloak_admin_client.remove_identity_provider(provider_type)
        return {"message": f"{PROVIDER_INFO[provider_type]['display_name']} removed"}
    except Exception as e:
        logger.error("Failed to remove %s: %s", provider_type, e)
        raise internal_http_error(500, "Failed to remove identity provider", e) from e


# =========================================================================
# SMTP endpoints retired — moved to /admin/email-config (launch-day-followup-5).
# Settings → Integrations UI now calls /admin/email-config directly. Keycloak
# realm SMTP sync was migrated into that router so password-reset emails keep
# working through the Vault-backed credential.
# =========================================================================


@router.get("/email-verification", response_model=EmailVerificationResponse)
def get_email_verification(
    current_user: User = Depends(_require_platform_admin),
):
    """Get current email verification requirement status."""
    from keycloak_auth import keycloak_admin_client
    enabled = keycloak_admin_client.get_verify_email()
    return {"email_verification_required": enabled}


@router.post("/email-verification", response_model=EmailVerificationResponse)
def set_email_verification(
    enabled: bool,
    current_user: User = Depends(_require_platform_admin),
):
    """Toggle email verification requirement. Updates Keycloak realm setting."""
    from keycloak_auth import keycloak_admin_client
    ok, status, detail = keycloak_admin_client.set_verify_email(enabled)
    if not ok:
        raise HTTPException(status_code=status if status >= 400 else 502, detail=detail)
    return {"email_verification_required": enabled}


# =========================================================================
# Email Template Health
# =========================================================================

@router.get("/email-template-health", response_model=EmailTemplateHealthResponse)
def check_email_templates(
    current_user: User = Depends(_require_platform_admin),
):
    """Check which React Email templates are present in MinIO."""
    from utils.email_sender import check_email_template_health
    return check_email_template_health()
