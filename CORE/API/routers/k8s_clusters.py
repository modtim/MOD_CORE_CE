"""GT.D8.2 (launch-completion-megaplan) — tenant K8s cluster registry CRUD.

Routes (auth: ``require_bearer_or_api_key`` — Bearer token OR scoped
app key, sibling of the other tenant-scoped routers):
    GET    /v1/k8s/clusters
           the tenant's live (non-tombstoned) active clusters + the
           effective cluster count (env-only k8s deployments count as 1
           implicit cluster).
    POST   /v1/k8s/clusters
           create a cluster config. Self-host / CE: no cluster-count
           limit. On hosted SaaS: 402 "Pro feature" for tenants past their
           single-cluster floor without Cap.K8S_MULTI_CLUSTER.
           409 on a duplicate live name.
    DELETE /v1/k8s/clusters/{cluster_id}
           SOFT delete (ISO 27001 — no hard deletes): tombstones the
           row, frees its slot + name immediately.

Only the Vault REFERENCE of a kubeconfig is ever accepted/stored
(``kubeconfig_vault_ref``) — never the kubeconfig content itself.
Business logic + the paid-cap slot gate live in
services/k8s_cluster_registry.py.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/k8s/clusters", tags=["K8s Clusters"])


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------

class ClusterOut(BaseModel):
    id: int
    name: str
    kubeconfig_vault_ref: Optional[str]
    in_cluster: bool
    active: bool


class ClusterListOut(BaseModel):
    items: list[ClusterOut]
    active_count: int


class ClusterCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255,
                      description="Unique-per-tenant cluster name.")
    kubeconfig_vault_ref: Optional[str] = Field(
        None, max_length=512,
        description="Vault path of the kubeconfig secret (NEVER the "
                    "kubeconfig content). Omit when in_cluster=true.")
    in_cluster: bool = Field(
        False,
        description="Use the API process's in-cluster ServiceAccount "
                    "instead of a kubeconfig ref.")
    active: bool = True


class ClusterDeleteOut(BaseModel):
    id: int
    name: str
    status: str
    deleted_at: str


class ClusterProbeOut(BaseModel):
    id: int
    name: str
    # The stored cluster_caps dict (18.2 shape; k8s_gpu_request reads
    # kai/kai_queue/mig_profiles/time_slicing_resource/vendor/
    # gpu_memory_gb straight off it). NULL/None = probe never succeeded.
    cluster_caps: Optional[dict] = None
    caps_probed_at: Optional[str] = None
    # Set when this probe failed (conservative caps stored instead).
    probe_error: Optional[str] = None


class ClusterKaiQueueMappingRequest(BaseModel):
    project: Optional[str] = Field(
        None, max_length=255,
        description="Project scope for the mapping. Omit for a "
                    "tenant-level mapping.")
    queue_name: Optional[str] = Field(
        None, max_length=63,
        description="Existing KAI queue name to map to (k8s name: "
                    "lowercase alphanumeric and '-'). Omit/None to "
                    "REMOVE the mapping (back to MOD's deterministic "
                    "queue). The queue is only validated read-only — "
                    "it is never created or mutated.")


class ClusterKaiQueueMappingOut(BaseModel):
    id: int
    name: str
    kai_queue_map: Optional[dict] = None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@router.get("", response_model=ClusterListOut)
@router.get("/", include_in_schema=False, response_model=ClusterListOut)
def list_clusters(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """The tenant's live k8s clusters + the effective cluster count."""
    from services import k8s_cluster_registry
    rows = k8s_cluster_registry.active_clusters(db, current_user.tenant_id)
    return ClusterListOut(
        items=[ClusterOut(
            id=r.id, name=r.name,
            kubeconfig_vault_ref=r.kubeconfig_vault_ref,
            in_cluster=r.in_cluster, active=r.active,
        ) for r in rows],
        active_count=k8s_cluster_registry.active_cluster_count(
            db, current_user.tenant_id),
    )


@router.post("", response_model=ClusterOut,
             status_code=status.HTTP_201_CREATED)
@router.post("/", include_in_schema=False, response_model=ClusterOut,
             status_code=status.HTTP_201_CREATED)
def create_cluster(
    payload: ClusterCreateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Register a k8s cluster. No count limit on self-host / CE; on SaaS 402
    for tenants past their single-cluster floor without
    Cap.K8S_MULTI_CLUSTER. 409 on a duplicate live name."""
    from services import k8s_cluster_registry
    row = k8s_cluster_registry.create_cluster(
        db,
        tenant_id=current_user.tenant_id,
        name=payload.name,
        kubeconfig_vault_ref=payload.kubeconfig_vault_ref,
        in_cluster=payload.in_cluster,
        active=payload.active,
        user=current_user,
    )
    db.commit()
    db.refresh(row)
    # 18.2: best-effort capability probe on register — a probe failure must
    # NEVER fail the create (refresh_cluster_caps never raises; it stores
    # conservative caps + probe_error instead).
    try:
        k8s_cluster_registry.refresh_cluster_caps(db, row.id)
        db.commit()
    except Exception:  # noqa: BLE001 — create must not fail on probe
        logger.exception("k8s clusters: post-create caps probe failed for %s",
                         row.id)
        db.rollback()
    db.refresh(row)
    return ClusterOut(
        id=row.id, name=row.name,
        kubeconfig_vault_ref=row.kubeconfig_vault_ref,
        in_cluster=row.in_cluster, active=row.active,
    )


@router.post("/{cluster_id}/probe", response_model=ClusterProbeOut)
def probe_cluster(
    cluster_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Re-run the 18.2 capability probe against a registered cluster and
    store the result (GFD node labels, KAI queues, MIG strategy). A probe
    failure returns 200 with the stored conservative caps + probe_error —
    the probe never breaks dispatch."""
    from services import k8s_cluster_registry
    # Tenant check FIRST — never probe another tenant's cluster.
    mine = k8s_cluster_registry.get_cluster(
        db, current_user.tenant_id, cluster_id)
    if mine is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes cluster not found for this tenant.",
        )
    k8s_cluster_registry.refresh_cluster_caps(db, cluster_id)
    db.commit()
    return ClusterProbeOut(
        id=mine.id,
        name=mine.name,
        cluster_caps=mine.cluster_caps,
        caps_probed_at=mine.caps_probed_at.isoformat()
        if mine.caps_probed_at else None,
        probe_error=(mine.cluster_caps or {}).get("probe_error"),
    )


@router.put("/{cluster_id}/kai-queue-map",
            response_model=ClusterKaiQueueMappingOut)
def set_kai_queue_map(
    cluster_id: int,
    payload: ClusterKaiQueueMappingRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """18.3 (GI.3): map this tenant (optionally project) to an EXISTING
    KAI queue, or remove the mapping (``queue_name`` omitted) to return
    to MOD's deterministic queue. The mapped queue is only VALIDATED
    read-only on connect/probe — it is never created, mutated or
    deleted by MOD. 404 for a cluster not owned by the tenant."""
    from services import k8s_cluster_registry
    mine = k8s_cluster_registry.get_cluster(
        db, current_user.tenant_id, cluster_id)
    if mine is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes cluster not found for this tenant.",
        )
    k8s_cluster_registry.set_kai_queue_mapping(
        db, tenant_id=current_user.tenant_id, cluster_id=cluster_id,
        project=payload.project, queue_name=payload.queue_name,
        user=current_user,
    )
    db.commit()
    db.refresh(mine)
    return ClusterKaiQueueMappingOut(
        id=mine.id, name=mine.name, kai_queue_map=mine.kai_queue_map,
    )


@router.delete("/{cluster_id}", response_model=ClusterDeleteOut)
def delete_cluster(
    cluster_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """SOFT delete a k8s cluster (ISO 27001): tombstone only, the row
    remains. Frees the cluster slot + name immediately."""
    from services import k8s_cluster_registry
    row = k8s_cluster_registry.soft_delete_cluster(
        db, tenant_id=current_user.tenant_id, cluster_id=cluster_id,
        user=current_user,
    )
    db.commit()
    return ClusterDeleteOut(
        id=row.id, name=row.name, status="soft_deleted",
        deleted_at=row.deleted_at.isoformat(),
    )
