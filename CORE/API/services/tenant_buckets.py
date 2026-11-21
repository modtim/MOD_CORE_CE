"""Self-heal missing per-tenant file buckets.

Tenant creation provisions ``tenant-{id}-files`` best-effort (a storage blip
only logs a warning), and a storage-backend switch leaves older tenants with
no bucket at all. Anything that hands the browser or a worker a presigned URL
then fails with NoSuchBucket. This sweep recreates the missing buckets.
"""
import logging
from typing import Callable, List, Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


def reconcile_tenant_buckets(db: Session, client_factory: Optional[Callable] = None) -> List[int]:
    """Create the file bucket for every live tenant that lacks one; return their ids."""
    if client_factory is None:
        from utils.minio_client import MinioClient
        client_factory = lambda tid: MinioClient(tenant_id=tid)
    created = []
    tenant_ids = [t for (t,) in db.query(models.Tenant.id).filter(models.Tenant.deleted_at.is_(None)).all()]
    for tid in tenant_ids:
        try:
            client = client_factory(tid)
            if client.client.bucket_exists(bucket_name=client.bucket_name):
                continue
            client.ensure_bucket_exists()
            created.append(tid)
        except Exception as e:
            logger.warning("tenant bucket reconcile failed for tenant %s: %s", tid, e)
    if created:
        logger.info("tenant bucket reconcile: created buckets for tenants %s", created)
    return created
