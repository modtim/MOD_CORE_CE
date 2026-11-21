#!/usr/bin/env python3
"""Create platform admin user - run from API container"""

from utils.pii_redaction import mask_email

import os
import sys
import requests
import logging
import database
from database import SessionLocal
from models import Tenant, User, UserStatus

# Setup logging
from utils.log_config import configure_logging
configure_logging()
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

def get_keycloak_user_info(email: str) -> dict:
    """Get the Keycloak user ID and details for the platform admin"""
    keycloak_url = os.getenv('KEYCLOAK_INTERNAL_URL', 'http://keycloak:8080')
    realm = os.getenv('KEYCLOAK_REALM_NAME', 'modtex')
    admin_user = os.getenv('KEYCLOAK_ADMIN_USER', 'admin')
    admin_password = os.getenv('KEYCLOAK_ADMIN_PASSWORD')

    # Get admin token
    token_response = requests.post(
        f"{keycloak_url}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "username": admin_user,
            "password": admin_password,
            "grant_type": "password"
        }
    )
    token = token_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Get user by email (since registrationEmailAsUsername=true in realm)
    users_response = requests.get(
        f"{keycloak_url}/admin/realms/{realm}/users?email={email}",
        headers=headers
    )
    users = users_response.json()

    if users and len(users) > 0:
        return {
            "id": users[0]["id"],
            "username": users[0]["username"],
            "email": users[0].get("email", email),
            "firstName": users[0].get("firstName", "Platform"),
            "lastName": users[0].get("lastName", "Admin")
        }
    return None

def main():
    database.initialize()
    # Get configuration from environment
    username = os.getenv('INITIAL_USER_USERNAME', 'admin').strip('"').strip("'")
    email = os.getenv('INITIAL_USER_EMAIL', 'admin@yourdomain.com').strip('"').strip("'")
    first_name = os.getenv('INITIAL_USER_FIRST_NAME', 'Platform').strip('"').strip("'")
    last_name = os.getenv('INITIAL_USER_LAST_NAME', 'Admin').strip('"').strip("'")

    logger.info("=" * 60)
    logger.info("Creating Platform Admin User")
    logger.info("=" * 60)
    logger.info(f"Username: {username}")
    logger.info(f"Email: {mask_email(email)}")
    logger.info("")

    db = SessionLocal()

    try:
        # Verify platform tenant exists
        platform_tenant = db.query(Tenant).filter(Tenant.id == 1).first()
        if not platform_tenant:
            logger.error("Platform tenant (ID=1) not found. Run create_tenant.py first.")
            return None

        logger.info(f"Found platform tenant (ID: {platform_tenant.id})")

        # Check if user already exists (search by email since Keycloak uses email as username)
        user = db.query(User).filter(User.email == email).first()
        if user:
            logger.info(f"User '{username}' already exists in database")

            # Update user to ensure they're a platform admin and onboarded
            if not user.is_platform_admin:
                user.is_platform_admin = True
                logger.info("Set is_platform_admin = True")

            if not user.onboarding_complete:
                user.onboarding_complete = True
                logger.info("Set onboarding_complete = True")

            if user.status != UserStatus.ACTIVE:
                user.status = UserStatus.ACTIVE
                logger.info("Set status = ACTIVE")

            if user.tenant_id != platform_tenant.id:
                user.tenant_id = platform_tenant.id
                logger.info("Assigned to platform tenant")

            db.commit()
            logger.info(f"Updated user '{username}'")
            return user.id

        # Get Keycloak user info (search by email since registrationEmailAsUsername=true)
        logger.info("Fetching Keycloak user info...")
        keycloak_user = get_keycloak_user_info(email)

        if not keycloak_user:
            logger.error(f"User with email '{mask_email(email)}' not found in Keycloak.")
            logger.error("Make sure keycloak-init.sh has created the user.")
            return None

        logger.info(f"Found Keycloak user ID: {keycloak_user['id']}")

        # Use Keycloak data if available, otherwise use env vars
        email = keycloak_user.get('email', email)
        first_name = keycloak_user.get('firstName', first_name)
        last_name = keycloak_user.get('lastName', last_name)

        # Create platform admin user in database
        # NOTE: Keycloak uses email as username (registrationEmailAsUsername=true)
        user = User(
            keycloak_user_id=keycloak_user['id'],
            username=keycloak_user['username'],  # This will be the email
            email=email,
            first_name=first_name,
            last_name=last_name,
            tenant_id=platform_tenant.id,
            status=UserStatus.ACTIVE,
            onboarding_complete=True,  # Platform admin bypasses onboarding
            is_platform_admin=True,    # GOD MODE
            parent_user_id=None        # Top-level admin
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        logger.info(f"Created platform admin '{username}' in database (ID: {user.id})")
        logger.info("")
        logger.info("=" * 60)
        logger.info("Platform Admin Created Successfully")
        logger.info("=" * 60)
        logger.info(f"User ID: {user.id}")
        logger.info(f"Tenant ID: {user.tenant_id}")
        logger.info(f"Status: {user.status}")
        logger.info(f"Platform Admin: {user.is_platform_admin}")
        logger.info(f"Onboarding Complete: {user.onboarding_complete}")
        logger.info("=" * 60)

        return user.id

    except Exception as e:
        if is_debug_mode():
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
        else:
            logger.error("Error creating platform admin")
        db.rollback()
        return None
    finally:
        db.close()

if __name__ == "__main__":
    user_id = main()
    sys.exit(0 if user_id else 1)
