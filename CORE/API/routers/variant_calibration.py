"""Phase 9 — INT8 calibration UX backend.

    POST /node_definitions/{node_id}/calibration
    GET  /node_definitions/{node_id}/calibration

Operators uploading an Intel NPU node can now supply representative
inputs for INT8 post-training quantisation instead of relying on
Optimum's default dataset. Without per-node calibration data, INT8
NPU inference frequently regresses accuracy vs FP32 CPU by 2–5% —
the node-creation step is the right place to collect it.

The endpoint stashes the calibration config on
`NodeDefinition.custom_metadata.calibration_inputs` as:

    {
        "source":      "hf_dataset" | "minio_refs",
        "dataset_id":  str | null,    # populated when source="hf_dataset"
        "minio_refs":  [str, ...] | [],
                                      # populated when source="minio_refs"
        "split":       str | null,    # e.g. "validation", "test[:100]"
        "sample_count": int,          # soft cap; runner may take fewer
        "content_hash": str,          # SHA-256 of the config blob for
                                      # cache / idempotency
        "created_at":  iso8601,
    }

Phase-9's calibration_runner (ImageBuilder side) reads this via the
`INLINE_CALIBRATION_CONFIG` env var, passes the right CLI args to
`optimum-cli export openvino`, and emits a signal on the variant
smoke report so the wizard can surface accuracy Δ vs default.

Per-tenant calibration cache: identical `content_hash` across two
nodes under the same tenant hits a pre-quantised IR in MinIO
(P9's storage convention — see calibration_runner.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel as PydanticBaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.variant_calibration_service import get_node_definition

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["NodeDefinitions — INT8 Calibration"],
    prefix="/node-definitions",
)


MIN_SAMPLE_COUNT = 1
MAX_SAMPLE_COUNT = 1024
MAX_MINIO_REFS = 1024


class CalibrationConfig(PydanticBaseModel):
    """Output shape for the calibration config dict."""
    source: str
    dataset_id: Optional[str] = None
    minio_refs: Optional[List[str]] = None
    split: Optional[str] = None
    sample_count: int
    content_hash: str
    created_at: str


class SetCalibrationResponse(PydanticBaseModel):
    node_id: int
    calibration: CalibrationConfig


class GetCalibrationResponse(PydanticBaseModel):
    node_id: int
    calibration: Optional[CalibrationConfig] = None


class ClearCalibrationResponse(PydanticBaseModel):
    node_id: int
    cleared: bool


class CalibrationRequest(PydanticBaseModel):
    """Body for POST /.../calibration."""
    source: str = Field(
        ...,
        description='Either "hf_dataset" (public HF repo) or "minio_refs" (pre-uploaded objects).',
    )
    dataset_id: Optional[str] = Field(
        default=None,
        description='HuggingFace dataset id, required when source="hf_dataset".',
    )
    minio_refs: List[str] = Field(
        default_factory=list,
        description='MinIO object paths inside the tenant bucket. Required when source="minio_refs".',
    )
    split: Optional[str] = Field(
        default=None,
        description='Optional split spec for HF datasets (e.g. "validation", "test[:100]").',
    )
    sample_count: int = Field(
        default=100,
        description="Max number of samples to feed into POT calibration.",
    )


def _compute_content_hash(body: Dict[str, Any]) -> str:
    """Stable SHA-256 over the canonical calibration config. Same
    config across tenants yields the same hash → shared pre-quantised
    IR cache entry (keyed per-tenant for isolation)."""
    payload = {
        "source": body.get("source"),
        "dataset_id": body.get("dataset_id") or "",
        "minio_refs": sorted(body.get("minio_refs") or []),
        "split": body.get("split") or "",
        "sample_count": int(body.get("sample_count") or 0),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


@router.post("/{node_id}/calibration", status_code=status.HTTP_200_OK, response_model=SetCalibrationResponse)
def set_calibration(
    node_id: int,
    body: CalibrationRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    """Attach (or replace) the INT8 calibration config on a node."""
    if body.source not in ("hf_dataset", "minio_refs"):
        raise HTTPException(
            status_code=400,
            detail='source must be "hf_dataset" or "minio_refs"',
        )
    if body.source == "hf_dataset" and not (body.dataset_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail='dataset_id is required when source="hf_dataset"',
        )
    if body.source == "minio_refs":
        if not body.minio_refs:
            raise HTTPException(
                status_code=400,
                detail='minio_refs must be non-empty when source="minio_refs"',
            )
        if len(body.minio_refs) > MAX_MINIO_REFS:
            raise HTTPException(
                status_code=400,
                detail=f"minio_refs length exceeds {MAX_MINIO_REFS}",
            )
        for r in body.minio_refs:
            if not isinstance(r, str) or not r.strip() or ".." in r:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid minio_ref: {r!r}",
                )
    if body.sample_count < MIN_SAMPLE_COUNT or body.sample_count > MAX_SAMPLE_COUNT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"sample_count must be between {MIN_SAMPLE_COUNT} "
                f"and {MAX_SAMPLE_COUNT}"
            ),
        )

    node = get_node_definition(db, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if node.tenant_id is not None and node.tenant_id != current_user.tenant_id \
            and not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=403,
            detail="Calibration requires ownership of this node",
        )

    body_dict = body.model_dump()
    content_hash = _compute_content_hash(body_dict)
    config = {
        "source": body.source,
        "dataset_id": body.dataset_id,
        "minio_refs": body.minio_refs,
        "split": body.split,
        "sample_count": body.sample_count,
        "content_hash": content_hash,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    cm = dict(node.custom_metadata or {})
    cm["calibration_inputs"] = config
    node.custom_metadata = cm or None
    db.commit()

    logger.info(
        "P9 calibration set: node=%s tenant=%s source=%s hash=%s samples=%d",
        node.name, node.tenant_id, body.source, content_hash,
        body.sample_count,
    )
    return {"node_id": node.id, "calibration": config}


@router.get("/{node_id}/calibration", response_model=GetCalibrationResponse)
def get_calibration(
    node_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    node = get_node_definition(db, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if node.tenant_id is not None and node.tenant_id != current_user.tenant_id \
            and not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Not your node")
    cm = node.custom_metadata or {}
    return {
        "node_id": node.id,
        "calibration": cm.get("calibration_inputs") or None,
    }


@router.delete("/{node_id}/calibration", status_code=status.HTTP_200_OK, response_model=ClearCalibrationResponse)
def clear_calibration(
    node_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    """Remove the calibration config — reverts to Optimum's default
    dataset on the next build."""
    node = get_node_definition(db, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    if node.tenant_id is not None and node.tenant_id != current_user.tenant_id \
            and not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Not your node")
    cm = dict(node.custom_metadata or {})
    had = "calibration_inputs" in cm
    cm.pop("calibration_inputs", None)
    node.custom_metadata = cm or None
    db.commit()
    return {"node_id": node.id, "cleared": had}
