#!/usr/bin/env python3
"""Create platform tenant - run from API container"""

import sys
import logging
import time
import database
from database import SessionLocal, create_tables
from models import Tenant
from services import tenant_key_local
from utils.paid_import import optional_paid

key_provider = optional_paid("services.key_provider") or tenant_key_local

# Setup logging
from utils.log_config import configure_logging
configure_logging()
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

def main():
    # Initialize database connection (Vault dynamic creds or static fallback)
    database.initialize()

    # Ensure database tables exist
    logger.info("Ensuring database tables exist...")
    create_tables()
    logger.info("Database tables ready")

    platform_tenant_name = "Platform"

    logger.info("=" * 60)
    logger.info("Creating Platform Tenant")
    logger.info("=" * 60)
    logger.info(f"Tenant: {platform_tenant_name}")
    logger.info("")

    db = SessionLocal()

    try:
        # Check/create platform tenant (must be ID=1)
        tenant = db.query(Tenant).filter(Tenant.id == 1).first()
        if tenant:
            logger.info(f"Platform tenant already exists (ID: {tenant.id}, Name: {tenant.name})")

            # Check if encryption key exists, add it if missing
            if not tenant.encryption_key:
                logger.warning("Platform tenant missing encryption key - generating one now...")
                _tenant_key, encrypted_tenant_key = key_provider.generate_and_wrap_tenant_key(
                    tenant.id, db=db)
                tenant.encryption_key = encrypted_tenant_key
                db.commit()
                db.refresh(tenant)
                logger.info("Added encryption key to platform tenant")

            # Ensure MinIO bucket exists for platform tenant (TPN CS-1 tenant isolation)
            # Bounded retry: storage may become reachable after a transient
            # Vault-init race at first boot.
            _bucket_ok = False
            for _i in range(3):
                try:
                    from utils.minio_client import MinioClient
                    minio_client = MinioClient(tenant_id=tenant.id)
                    minio_client.ensure_bucket_exists()
                    logger.info(f"MinIO bucket '{minio_client.bucket_name}' exists for platform tenant")
                    _bucket_ok = True
                    break
                except Exception as e:
                    if is_debug_mode():
                        logger.warning(
                            "Failed to ensure MinIO bucket for platform tenant "
                            "(attempt %d/3): %s", _i + 1, e)
                    else:
                        logger.warning(
                            "Failed to ensure MinIO bucket for platform tenant "
                            "(attempt %d/3)", _i + 1)
                    time.sleep(2)
            if not _bucket_ok:
                logger.warning("Failed to ensure MinIO bucket for platform tenant")
        else:
            # Generate encryption key for new tenant. tenant_id is hard-coded
            # to 1 below (explicit PK), so the Vault transit key name can be
            # derived before the row is inserted.
            _tenant_key, encrypted_tenant_key = key_provider.generate_and_wrap_tenant_key(
                1, db=db)

            # Create platform tenant with explicit ID=1 and encryption key
            tenant = Tenant(
                id=1,
                name=platform_tenant_name,
                credit_balance=999999999,  # Unlimited credits for platform
                storage_used_bytes=0,
                encryption_key=encrypted_tenant_key
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
            logger.info(f"Created platform tenant '{platform_tenant_name}' (ID: {tenant.id})")

            # Provision MinIO bucket for platform tenant (TPN CS-1 tenant isolation)
            # Bounded retry: storage may become reachable after a transient
            # Vault-init race at first boot.
            _bucket_ok = False
            for _i in range(3):
                try:
                    from utils.minio_client import MinioClient
                    minio_client = MinioClient(tenant_id=tenant.id)
                    minio_client.ensure_bucket_exists()
                    logger.info(f"Created MinIO bucket '{minio_client.bucket_name}' for platform tenant")
                    _bucket_ok = True
                    break
                except Exception as e:
                    if is_debug_mode():
                        logger.warning(
                            "Failed to create MinIO bucket for platform tenant "
                            "(attempt %d/3): %s", _i + 1, e)
                    else:
                        logger.warning(
                            "Failed to create MinIO bucket for platform tenant "
                            "(attempt %d/3)")
                    time.sleep(2)
            if not _bucket_ok:
                logger.warning("Failed to create MinIO bucket for platform tenant")

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"Platform Tenant ID: {tenant.id}")
        logger.info("=" * 60)
        return tenant.id

    except Exception as e:
        if is_debug_mode():
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
        else:
            logger.error("Error creating platform tenant")
        db.rollback()
        return None
    finally:
        db.close()

if __name__ == "__main__":
    tenant_id = main()
    sys.exit(0 if tenant_id else 1)
