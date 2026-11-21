"""CRUD (query-only) layer for the onboarding domain (Gate 2b migration).

Every function here is a plain SQLAlchemy read — no policy, no HTTP, no
external I/O. `services/onboarding_service.py` composes these into business
logic; `routers/onboarding.py` (and the private helpers that stay there
because tests monkeypatch them by name — see that file's module docstring
note) call down into this module instead of running `db.query(...)` inline.

Fetches here are intentionally tenant-UNSCOPED only where the caller already
supplies the scope as an explicit argument (e.g. `tenant_id`) — there is no
implicit "current tenant" here, matching the existing per-domain crud style
(see crud/stripe_webhooks.py, crud/workflows_domain.py for the same
`get_tenant_by_id` shape).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def has_human_platform_admin(db: Session) -> bool:
    """Does any human (non-internal-service) user already hold platform
    admin? Excludes the seed `api_service` / `*@internal.local` row so the
    seed service user never satisfies the bootstrap-signup gate."""
    return db.query(models.User).filter(
        models.User.is_platform_admin == True,  # noqa: E712
        ~models.User.email.like('%@internal.local'),
    ).first() is not None


def get_root_parent_user(db: Session, tenant_id: int) -> Optional[models.User]:
    """The tenant's root user (no parent) — used to chain a cross-tenant
    invitation under an existing hierarchy when the original inviter isn't
    in the target tenant."""
    return db.query(models.User).filter(
        models.User.tenant_id == tenant_id,
        models.User.parent_user_id == None,  # noqa: E711
    ).first()


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_admin_role(db: Session, tenant_id: int) -> Optional["models.CustomRole"]:
    """The tenant's 'Admin' CustomRole, if one exists."""
    return db.query(models.CustomRole).filter(
        models.CustomRole.tenant_id == tenant_id,
        models.CustomRole.name == "Admin",
    ).first()


def count_files(db: Session, tenant_id: int) -> int:
    return db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.cog_type == models.CogType.FILE,
        models.Cog.deleted_at.is_(None),
    ).count()


def count_workflows(db: Session, tenant_id: int) -> int:
    """Excludes `is_system` workflows (wizard node-build DAGs) — those run
    under the building tenant but aren't the user "building their first
    workflow"."""
    if not hasattr(models, 'Workflow'):
        return 0
    return db.query(models.Workflow).filter(
        models.Workflow.tenant_id == tenant_id,
        models.Workflow.is_system.isnot(True),
    ).count()


def count_workflow_runs(db: Session, tenant_id: int) -> int:
    """Total (any-status) non-system workflow runs for the tenant."""
    if not hasattr(models, 'Workflow'):
        return 0
    return db.query(models.WorkflowRun).join(
        models.Workflow, models.WorkflowRun.workflow_id == models.Workflow.id,
    ).filter(
        models.WorkflowRun.tenant_id == tenant_id,
        models.Workflow.is_system.isnot(True),
    ).count()


def count_successful_workflow_runs(db: Session, tenant_id: int) -> int:
    """Non-system workflow runs that reached `TaskStatus.COMPLETED`."""
    return db.query(models.WorkflowRun).join(
        models.Workflow, models.WorkflowRun.workflow_id == models.Workflow.id,
    ).filter(
        models.WorkflowRun.tenant_id == tenant_id,
        models.WorkflowRun.status == models.TaskStatus.COMPLETED,
        models.Workflow.is_system.isnot(True),
    ).count()


def count_invitations(db: Session, tenant_id: int) -> int:
    """Non-deleted invitations (pending + accepted both count as "the
    tenant has invited people")."""
    if not hasattr(models, 'Invitation'):
        return 0
    return db.query(models.Invitation).filter(
        models.Invitation.tenant_id == tenant_id,
        models.Invitation.deleted_at.is_(None),
    ).count()


def count_api_keys(db: Session, tenant_id: int) -> int:
    """ApiKeys owned by any user in the tenant. ApiKey has `user_id`, not
    `tenant_id` — joins via User."""
    return db.query(models.ApiKey).join(
        models.User, models.ApiKey.user_id == models.User.id,
    ).filter(
        models.User.tenant_id == tenant_id,
        models.ApiKey.deleted_at.is_(None),
    ).count()


def get_checklist_reward(db: Session, tenant_id: int) -> Optional["models.OnboardingChecklistReward"]:
    return db.query(models.OnboardingChecklistReward).filter(
        models.OnboardingChecklistReward.tenant_id == tenant_id,
    ).first()
