"""AI Config CRUD — the ONLY place `db.query(...)` lives for this domain.

Gate 2b (per-file service/CRUD migration) for `routers/ai_config.py`. That
router reasons about managed-AI (local vLLM/llama.cpp) fleet placement:
which `PersistentStack` rows are the managed-AI generator/reasoner, which
`WorkerLicense` rows are licensed, and how much VRAM the fleet's
`AcceleratorProfile` rows report per kind.

None of these rows are tenant-scoped (managed-AI stacks, worker licenses and
accelerator profiles are platform/fleet-wide infrastructure, not per-tenant
data — every caller in `routers/ai_config.py` is already gated
`current_user.is_platform_admin`), so these functions intentionally take no
`tenant_id` and apply none of the tenant-scoping `CRUDBase` provides. That
matches the pre-existing inline queries exactly (no behaviour change).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

import models


def get_running_stack_by_name(db: Session, name: str) -> Optional[models.PersistentStack]:
    """The single running `PersistentStack` row with this name, if any.

    Used to resolve which worker currently hosts a managed-AI role
    (generator/reasoner) so its advertised address can be dialled.
    """
    return (
        db.query(models.PersistentStack)
        .filter(
            models.PersistentStack.name == name,
            models.PersistentStack.status == "running",
        )
        .first()
    )


def get_worker_license_by_worker_name(db: Session, worker_name: str) -> Optional[models.WorkerLicense]:
    """The `WorkerLicense` row for one worker, by its `worker_name`."""
    return (
        db.query(models.WorkerLicense)
        .filter(
            models.WorkerLicense.deleted_at.is_(None),
            models.WorkerLicense.worker_name == worker_name,
        )
        .first()
    )


def list_running_stacks(db: Session) -> List[models.PersistentStack]:
    """Every `PersistentStack` row currently `running`, any name.

    Used by `_stack_model_view` to widen the served-model candidate set
    beyond the managed-AI pair (a tenant-deployed stack may also serve a
    catalogued or uncatalogued model).
    """
    return (
        db.query(models.PersistentStack)
        .filter(models.PersistentStack.status == "running")
        .all()
    )


def list_stacks_by_names(db: Session, names: Iterable[str]) -> List[models.PersistentStack]:
    """Every `PersistentStack` row (any status) whose name is in `names`.

    Used both to read what the managed-AI stacks are currently serving
    (`managed_ai_wanted_models`) and to diff declared-vs-deployed
    (`compute_managed_ai_plan`) — same name set, same shape, in both callers.
    """
    names = list(names)
    if not names:
        return []
    return (
        db.query(models.PersistentStack)
        .filter(models.PersistentStack.name.in_(names))
        .all()
    )


def list_stack_ids_by_names(db: Session, names: Iterable[str]) -> List[int]:
    """Just the ids of `PersistentStack` rows named in `names`.

    An id-only projection (not a full-row fetch) — kept separate from
    `list_stacks_by_names` so the "which of my own stacks already occupy a
    device" check (`_tier_with_placement`) keeps its cheaper query shape.
    """
    names = list(names)
    if not names:
        return []
    return [r[0] for r in db.query(models.PersistentStack.id).filter(
        models.PersistentStack.name.in_(names)).all()]


def list_licensed_workers(db: Session) -> List[models.WorkerLicense]:
    """Every `WorkerLicense` row with `status == LICENSED`.

    Used both for the fleet-wide "who might be downloading weights" scan
    (`managed_ai_download_progress`) and the "is there simply a bigger card
    somewhere, just busy" capacity check (`_tier_with_placement`).
    """
    return (
        db.query(models.WorkerLicense)
        .filter(
            models.WorkerLicense.status == models.WorkerStatus.LICENSED,
            models.WorkerLicense.deleted_at.is_(None),
        )
        .all()
    )


def accelerator_vram_by_kind(db: Session) -> List[Tuple[str, float]]:
    """`[(kind, total_vram_gb), ...]` across every `AcceleratorProfile` row
    that reports a VRAM figure, grouped by accelerator kind.

    Used to guess which vendor's hardware this fleet mostly runs
    (`fleet_accelerator_kind`) — the kind with the most total VRAM.
    """
    from sqlalchemy import func as _f

    return (
        db.query(models.AcceleratorProfile.kind, _f.sum(models.AcceleratorProfile.vram_gb))
        .filter(models.AcceleratorProfile.vram_gb.isnot(None))
        .group_by(models.AcceleratorProfile.kind)
        .all()
    )
