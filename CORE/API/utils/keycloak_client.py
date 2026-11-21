"""
Keycloak Client Utility

Provides functions to interact with Keycloak API for user and group management.
"""

import os
import re
import requests
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Group-name alias mapping (old minio names → new S3 names).
# Used for backward-compat dual-read in get_group_id: the lookup tries the
# canonical new name first, then falls back through aliases so that
# existing Keycloak realms with the old group names still resolve.
S3_GROUP_ALIASES = {
    "minio_tenant_admin": "s3_tenant_admin",
    "minio_super_admin": "platform_s3_admin",
}

# Reverse alias map (new → old) for bidirectional resolution.
S3_GROUP_ALIASES_REV = {v: k for k, v in S3_GROUP_ALIASES.items()}

# Compile the old-name prefix regex used to resolve tenant-scoped aliases
# (e.g. a pre-existing "minio_tenant_1234" → "s3_tenant_1234").
_TENANT_PREFIX_RE = re.compile(r"^minio_tenant_(\d+)$")

# Compile the new-name prefix regex used to resolve tenant-scoped aliases
# (e.g. a pre-existing "s3_tenant_1234" → "minio_tenant_1234").
_NEW_TENANT_PREFIX_RE = re.compile(r"^s3_tenant_(\d+)$")

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

KEYCLOAK_URL = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
REALM_NAME = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
ADMIN_CLI_SECRET = os.getenv("KEYCLOAK_ADMIN_CLI_SECRET")
KEYCLOAK_ADMIN_USER = os.getenv("KEYCLOAK_ADMIN_USER", "admin")
KEYCLOAK_ADMIN_PASSWORD = os.getenv("KEYCLOAK_ADMIN_PASSWORD")


def get_admin_token() -> Optional[str]:
    """Get an access token for Keycloak admin API."""
    try:
        response = requests.post(
            f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
            data={
                "username": KEYCLOAK_ADMIN_USER,
                "password": KEYCLOAK_ADMIN_PASSWORD,
                "grant_type": "password",
                "client_id": "admin-cli",
                "client_secret": ADMIN_CLI_SECRET
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        response.raise_for_status()
        return response.json().get("access_token")
    except Exception as e:
        logger.error("Failed to get Keycloak admin token (%s: %s)",
                     type(e).__name__,
                     getattr(getattr(e, "response", None), "status_code", None))
        return None


def get_group_id(group_name: str, token: str) -> Optional[str]:
    """Get the ID of a Keycloak group by name.

    Supports dual-read: the canonical new name is tried first; if the
    realm still holds the legacy ``minio_tenant_*`` names the lookup
    resolves them via the alias map so that existing deployments keep
    working without a live migration.
    """
    try:
        response = requests.get(
            f"{KEYCLOAK_URL}/admin/realms/{REALM_NAME}/groups",
            params={"search": group_name},
            headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        groups = response.json()

        # Prefer-new-first: try the exact requested name first.
        new_canonical_id = None
        for group in groups:
            if group.get("name") == group_name:
                # Exact match found; if this is the old name and the new
                # canonical name also exists, prefer the new one.
                if group_name in S3_GROUP_ALIASES:
                    new_name = S3_GROUP_ALIASES[group_name]
                    for g in groups:
                        if g.get("name") == new_name:
                            new_canonical_id = g.get("id")
                            break
                    if new_canonical_id:
                        return new_canonical_id
                return group.get("id")

        # If the caller asked for the old name, also try the new (canonical) one.
        translated_new = _resolve_group_alias(group_name)
        if translated_new and translated_new != group_name:
            for group in groups:
                if group.get("name") == translated_new:
                    return group.get("id")

        # If the caller asked for the new name, also try the old (legacy) one.
        translated_old = _resolve_group_alias_reverse(group_name)
        if translated_old and translated_old != group_name:
            for group in groups:
                if group.get("name") == translated_old:
                    return group.get("id")

        return None
    except Exception as e:
        logger.error(f"Failed to get group ID for '{group_name}': {e}")
        return None


def _resolve_group_alias(name: str) -> Optional[str]:
    """Resolve a legacy group name to its new canonical name.

    Handles the exact aliases in S3_GROUP_ALIASES as well as the
    tenant-scoped prefix pattern ``minio_tenant_{id}`` →
    ``s3_tenant_{id}``.  Returns None if the name is already canonical.
    """
    # 1. exact alias map
    if name in S3_GROUP_ALIASES:
        return S3_GROUP_ALIASES[name]

    # 2. tenant-prefix: minio_tenant_{int} → s3_tenant_{int}
    m = _TENANT_PREFIX_RE.match(name)
    if m:
        return f"s3_tenant_{m.group(1)}"

    return None


def _resolve_group_alias_reverse(name: str) -> Optional[str]:
    """Resolve a new canonical name back to its legacy alias.

    Handles the exact aliases in S3_GROUP_ALIASES_REV as well as the
    tenant-scoped prefix pattern ``s3_tenant_{id}`` →
    ``minio_tenant_{id}``.  Returns None if the name is not a new name.
    """
    # 1. exact reverse alias map
    if name in S3_GROUP_ALIASES_REV:
        return S3_GROUP_ALIASES_REV[name]

    # 2. tenant-prefix: s3_tenant_{int} → minio_tenant_{int}
    m = _NEW_TENANT_PREFIX_RE.match(name)
    if m:
        return f"minio_tenant_{m.group(1)}"

    return None


def _expand_group_name(name: str) -> list:
    """Return a set of all equivalent group names for one given name.

    The returned list contains the original name plus its old and new aliases.
    Used on READ paths (forward-auth) to expand token groups before
    intersecting with allowed_groups config.
    """
    result = [name]
    alias = _resolve_group_alias(name)
    if alias and alias != name:
        result.append(alias)
    alias_rev = _resolve_group_alias_reverse(name)
    if alias_rev and alias_rev != name:
        result.append(alias_rev)
    return result


def send_verify_email(keycloak_user_id: str) -> bool:
    """Trigger Keycloak's send-verify-email admin endpoint for a
    user. Keycloak uses the realm-level SMTP config (which the
    admin_email_config endpoint syncs from the platform tenant's
    Postmark setup) so the message ships through the same
    provider as every other platform email.

    Returns True on a 2xx, False on any failure. Errors are
    logged but do NOT raise — the verify-email send is best-
    effort; the API gate that rejected the user is the source
    of truth.
    """
    token = get_admin_token()
    if not token:
        logger.error("send_verify_email: could not obtain admin token")
        return False
    try:
        dashboard_url = os.getenv("DASHBOARD_URL", "http://dashboard.localhost")
        params = {
            "client_id": "dashboard-client",
            "redirect_uri": dashboard_url,
        }
        response = requests.put(
            f"{KEYCLOAK_URL}/admin/realms/{REALM_NAME}/users/{keycloak_user_id}/send-verify-email",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=10,
        )
        if response.status_code in (200, 204):
            logger.info("send_verify_email: sent for user_id=%s", keycloak_user_id)
            return True
        logger.warning(
            "send_verify_email: HTTP %d for user_id=%s, body=%s",
            response.status_code, keycloak_user_id, response.text[:200],
        )
        return False
    except Exception as e:
        logger.error("send_verify_email failed for user_id=%s: %s", keycloak_user_id, e)
        return False


def get_user_id_by_username(username: str, token: str) -> Optional[str]:
    """Get the Keycloak user ID by username."""
    try:
        response = requests.get(
            f"{KEYCLOAK_URL}/admin/realms/{REALM_NAME}/users",
            params={"username": username, "exact": "true"},
            headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        users = response.json()

        if users and len(users) > 0:
            return users[0].get("id")

        return None
    except Exception as e:
        logger.error(f"Failed to get user ID for '{username}': {e}")
        return None


def assign_user_to_group(keycloak_user_id: str, group_name: str) -> bool:
    """
    Assign a Keycloak user to a group.

    Args:
        keycloak_user_id: The Keycloak UUID of the user (not username)
        group_name: The name of the group (e.g., "s3_tenant_admin" or legacy "minio_tenant_admin")

    Returns:
        True if successful, False otherwise
    """
    token = get_admin_token()
    if not token:
        logger.error("Cannot assign user to group: failed to get admin token")
        return False

    group_id = get_group_id(group_name, token)
    if not group_id:
        logger.error(f"Cannot assign user to group: group '{group_name}' not found")
        return False

    try:
        response = requests.put(
            f"{KEYCLOAK_URL}/admin/realms/{REALM_NAME}/users/{keycloak_user_id}/groups/{group_id}",
            headers={"Authorization": f"Bearer {token}"}
        )

        if response.status_code == 204:
            if is_debug_mode():
                logger.info(f"Assigned user {keycloak_user_id} to group '{group_name}'")
            else:
                logger.info(f"Assigned user to group '{group_name}'")
            return True
        else:
            logger.error(f"Failed to assign user to group: HTTP {response.status_code}")
            return False

    except Exception as e:
        if is_debug_mode():
            logger.error(f"Failed to assign user to group '{group_name}': {e}")
        else:
            logger.error(f"Failed to assign user to group '{group_name}'")
        return False


def get_group_by_id(group_id: str) -> Optional[dict]:
    """
    Get a Keycloak group by its ID, including attributes.

    Args:
        group_id: The UUID of the Keycloak group

    Returns:
        Group dict with attributes, or None if not found
    """
    token = get_admin_token()
    if not token:
        return None

    try:
        response = requests.get(
            f"{KEYCLOAK_URL}/admin/realms/{REALM_NAME}/groups/{group_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to get group by ID '{group_id}': {e}")
        return None


def validate_group_belongs_to_tenant(group_id: str, tenant_id: int) -> bool:
    """
    Validate that a Keycloak group belongs to the specified tenant.

    This checks the group's 'tenant_id' attribute in Keycloak.
    Groups should have a 'tenant_id' attribute set when created.

    Args:
        group_id: The UUID of the Keycloak group
        tenant_id: The tenant ID to validate against

    Returns:
        True if the group belongs to the tenant, False otherwise
    """
    group = get_group_by_id(group_id)
    if not group:
        logger.warning(f"Group '{group_id}' not found in Keycloak")
        return False

    # Check the tenant_id attribute
    attributes = group.get("attributes", {})
    group_tenant_ids = attributes.get("tenant_id", [])

    # Keycloak attributes are lists of strings
    if str(tenant_id) in group_tenant_ids:
        return True

    if is_debug_mode():
        logger.warning(
            f"Group '{group_id}' does not belong to tenant {tenant_id}. "
            f"Group tenant_ids: {group_tenant_ids}"
        )
    else:
        logger.warning(f"Group '{group_id}' does not belong to tenant {tenant_id}")
    return False


def create_tenant_group(tenant_id: int, tenant_name: str) -> Optional[str]:
    """
    Create a Keycloak group for a tenant with the tenant_id attribute.

    This enables tenant isolation when using a single Keycloak realm.
    The group is created with a 'tenant_id' attribute that can be validated
    when granting permissions to keycloak_group_id.

    Args:
        tenant_id: The database ID of the tenant
        tenant_name: The name of the tenant (used for group naming)

    Returns:
        The Keycloak group ID if successful, None otherwise
    """
    token = get_admin_token()
    if not token:
        logger.error("Cannot create tenant group: failed to get admin token")
        return None

    # Create a sanitized group name
    safe_name = tenant_name.replace(" ", "_").replace("/", "_")[:50]
    group_name = f"tenant_{tenant_id}_{safe_name}"

    try:
        # Create the group with tenant_id attribute
        response = requests.post(
            f"{KEYCLOAK_URL}/admin/realms/{REALM_NAME}/groups",
            json={
                "name": group_name,
                "attributes": {
                    "tenant_id": [str(tenant_id)]
                }
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
        )

        if response.status_code == 201:
            # Get the group ID from the Location header
            location = response.headers.get("Location", "")
            group_id = location.split("/")[-1] if location else None
            if is_debug_mode():
                logger.info(f"Created Keycloak group '{group_name}' for tenant {tenant_id}")
            else:
                logger.info(f"Created Keycloak group for tenant {tenant_id}")
            return group_id
        elif response.status_code == 409:
            # Group already exists, try to find it
            if is_debug_mode():
                logger.info(f"Keycloak group '{group_name}' already exists, retrieving ID")
            else:
                logger.info(f"Keycloak group already exists, retrieving ID")
            existing_id = get_group_id(group_name, token)
            return existing_id
        else:
            logger.error(f"Failed to create Keycloak group: HTTP {response.status_code}")
            return None

    except Exception as e:
        if is_debug_mode():
            logger.error(f"Failed to create Keycloak group for tenant {tenant_id}: {e}")
        else:
            logger.error(f"Failed to create Keycloak group for tenant {tenant_id}")
        return None


def assign_user_to_group_by_username(username: str, group_name: str) -> bool:
    """
    Assign a user to a group by looking up their username first.

    Args:
        username: The username of the user
        group_name: The name of the group

    Returns:
        True if successful, False otherwise
    """
    token = get_admin_token()
    if not token:
        return False

    user_id = get_user_id_by_username(username, token)
    if not user_id:
        logger.error(f"Cannot assign user to group: user '{username}' not found in Keycloak")
        return False

    return assign_user_to_group(user_id, group_name)
