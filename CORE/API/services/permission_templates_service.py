"""Permission Templates service layer.

Business logic / policy for the permission-templates domain, moved out of
`routers/permission_templates.py` (Gate 2b). Owns the transaction boundary and
all domain rules (permission-level validation, name uniqueness, privilege
escalation checks, cross-tenant grantee guards); all data access goes through
`crud/permission_templates.py` and the existing `permissions.py` /
`cog_grants.py` primitives.

Router handlers map `ServiceError` subclasses to HTTP status codes. The status
codes used here match the pre-migration router exactly (mostly plain 400s, not
409s, for "name already exists" — preserved for snapshot parity, not because
409 wouldn't be more correct).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request
from sqlalchemy.orm import Session

import audit
import models
from crud import permission_templates as templates_crud
from models import PermissionLevel
from permissions import check_user_capability_on_cog, create_permission, validate_permission_grant
from cog_grants import list_cog_grants
from services._base import NotFoundError, ServiceError

_TEMPLATE_FIELDS = (
    "can_upload",
    "can_create_subfolders",
    "can_move_files",
    "can_process",
    "can_share",
    "can_delete",
)


class BadRequestError(ServiceError):
    """Malformed/invalid request (router maps -> 400, matching the
    pre-migration router which used HTTP_400_BAD_REQUEST for these, not 409 or
    422)."""

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


def _validate_level(raw_level: str) -> PermissionLevel:
    try:
        return PermissionLevel(raw_level.lower())
    except ValueError:
        raise BadRequestError(f"Invalid permission level: {raw_level}")


def list_templates(
    db: Session, *, tenant_id: int, include_inactive: bool = False
) -> List[models.PermissionTemplate]:
    """List a tenant's templates (active-only by default)."""
    return templates_crud.list_templates(db, tenant_id=tenant_id, include_inactive=include_inactive)


def get_template(db: Session, template_id: int, *, tenant_id: int) -> models.PermissionTemplate:
    """Fetch one template, tenant-scoped; 404 if missing or another tenant's."""
    template = templates_crud.get_template(db, template_id, tenant_id=tenant_id)
    if not template:
        raise NotFoundError("Template not found")
    return template


def create_template(
    db: Session,
    *,
    tenant_id: int,
    created_by_user_id: int,
    data: Dict[str, Any],
) -> models.PermissionTemplate:
    """Create a new template. `data` is the create-request payload
    (name/description/permission_level/duration_days/can_* overrides)."""
    _validate_level(data["permission_level"])

    if templates_crud.get_by_name(db, tenant_id=tenant_id, name=data["name"]):
        raise BadRequestError(f"Template with name '{data['name']}' already exists")

    values = {
        "tenant_id": tenant_id,
        "name": data["name"],
        "description": data.get("description"),
        "permission_level": data["permission_level"].lower(),
        "duration_days": data.get("duration_days"),
        "created_by_user_id": created_by_user_id,
        "is_active": True,
        "times_used": 0,
    }
    for field in _TEMPLATE_FIELDS:
        values[field] = data.get(field)

    return templates_crud.create_template(db, values)


def update_template(
    db: Session,
    template_id: int,
    *,
    tenant_id: int,
    update_data: Dict[str, Any],
) -> models.PermissionTemplate:
    """Apply a partial update. `update_data` is already `exclude_unset`-filtered
    by the router (it needs the Pydantic request object to know what was
    actually supplied)."""
    template = templates_crud.get_template(db, template_id, tenant_id=tenant_id)
    if not template:
        raise NotFoundError("Template not found")

    if update_data.get("permission_level"):
        _validate_level(update_data["permission_level"])

    new_name = update_data.get("name")
    if new_name and new_name != template.name:
        if templates_crud.get_by_name(db, tenant_id=tenant_id, name=new_name, exclude_id=template_id):
            raise BadRequestError(f"Template with name '{new_name}' already exists")

    return templates_crud.update_template(db, template, update_data)


def delete_template(db: Session, template_id: int, *, tenant_id: int) -> None:
    """Delete a template (hard delete — see crud.delete_template docstring)."""
    template = templates_crud.get_template(db, template_id, tenant_id=tenant_id)
    if not template:
        raise NotFoundError("Template not found")
    templates_crud.delete_template(db, template)


def duplicate_template(
    db: Session,
    template_id: int,
    *,
    tenant_id: int,
    created_by_user_id: int,
    new_name: str,
) -> models.PermissionTemplate:
    """Clone an existing template under a new name."""
    original = templates_crud.get_template(db, template_id, tenant_id=tenant_id)
    if not original:
        raise NotFoundError("Template not found")

    if templates_crud.get_by_name(db, tenant_id=tenant_id, name=new_name):
        raise BadRequestError(f"Template with name '{new_name}' already exists")

    description = f"Copy of {original.name}" + (f": {original.description}" if original.description else "")
    values = {
        "tenant_id": tenant_id,
        "name": new_name,
        "description": description,
        "permission_level": original.permission_level,
        "duration_days": original.duration_days,
        "created_by_user_id": created_by_user_id,
        "is_active": True,
        "times_used": 0,
    }
    for field in _TEMPLATE_FIELDS:
        values[field] = getattr(original, field)

    return templates_crud.create_template(db, values)


def apply_template(
    db: Session,
    template_id: int,
    *,
    tenant_id: int,
    current_user_id: int,
    cog_ids: List[int],
    user_ids: Optional[List[int]],
    emails: Optional[List[str]],
    override_duration_days: Optional[int],
    invitation_message: Optional[str],
    http_request: Optional[Request] = None,
) -> Dict[str, Any]:
    """Apply a template across (cog, grantee) pairs.

    Returns a plain dict shaped like the pre-migration `ApplyTemplateResponse`
    (template_id/template_name/total_requested/successful/failed/results); the
    router validates it into the response schema. Per-recipient failures (no
    `can_share`, privilege escalation, cross-tenant grantee) are recorded as
    `status="error"` result rows — NOT raised as HTTP errors — matching the
    original endpoint's behavior of always returning 200 with a scorecard.
    """
    template = templates_crud.get_template(db, template_id, tenant_id=tenant_id)
    if not template:
        raise NotFoundError("Template not found")

    if not template.is_active:
        raise BadRequestError("Cannot apply inactive template")

    if not cog_ids:
        raise BadRequestError("At least one cog_id must be provided")

    if not (user_ids or emails):
        raise BadRequestError("At least one user_id or email must be provided")

    duration_days = override_duration_days if override_duration_days is not None else template.duration_days
    try:
        perm_level = PermissionLevel(template.permission_level)
    except ValueError:
        raise BadRequestError(f"Invalid permission level in template: {template.permission_level}")

    results: List[Dict[str, Any]] = []
    successful = 0
    failed = 0

    for cog_id in cog_ids:
        if not check_user_capability_on_cog(db, current_user_id, cog_id, "can_share"):
            num_recipients = len(user_ids or []) + len(emails or [])
            for _ in range(num_recipients):
                results.append({
                    "cog_id": cog_id,
                    "status": "error",
                    "error_message": "You don't have permission to share this folder",
                })
                failed += 1
            continue

        is_valid, error_msg = validate_permission_grant(db, current_user_id, cog_id, perm_level)
        if not is_valid:
            num_recipients = len(user_ids or []) + len(emails or [])
            for _ in range(num_recipients):
                results.append({"cog_id": cog_id, "status": "error", "error_message": error_msg})
                failed += 1
            continue

        if user_ids:
            for user_id in user_ids:
                try:
                    target_user = templates_crud.get_user_in_tenant(db, user_id, tenant_id=tenant_id)
                    if not target_user:
                        results.append({
                            "cog_id": cog_id,
                            "user_id": user_id,
                            "status": "error",
                            "error_message": "Cannot grant permission to user from a different organization",
                        })
                        failed += 1
                        continue

                    existing_grants = list_cog_grants(db, cog_ids=[cog_id], user_id=user_id)
                    existing = existing_grants[0] if existing_grants else None

                    if existing:
                        results.append({
                            "cog_id": cog_id,
                            "user_id": user_id,
                            "status": "already_exists",
                            "permission_id": existing.id,
                        })
                        successful += 1
                        continue

                    expires_at = None
                    if duration_days:
                        expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

                    new_perm = create_permission(
                        db,
                        cog_id=cog_id,
                        level=perm_level,
                        granted_by_user_id=current_user_id,
                        user_id=user_id,
                        expires_at=expires_at,
                        invitation_context={
                            "applied_from_template": True,
                            "template_id": template.id,
                            "template_name": template.name,
                            "invitation_message": invitation_message,
                        },
                        can_upload=template.can_upload,
                        can_create_subfolders=template.can_create_subfolders,
                        can_move_files=template.can_move_files,
                        can_process=template.can_process,
                        can_share=template.can_share,
                        can_delete=template.can_delete,
                    )

                    audit.log_permission_granted(
                        db=db,
                        actor_user_id=current_user_id,
                        tenant_id=tenant_id,
                        target_user_id=user_id,
                        resource_type="cog",
                        resource_id=cog_id,
                        permission_id=new_perm.id,
                        permission_level=template.permission_level,
                        metadata={
                            "applied_from_template": True,
                            "template_id": template.id,
                            "template_name": template.name,
                        },
                        request=http_request,
                    )

                    results.append({
                        "cog_id": cog_id,
                        "user_id": user_id,
                        "status": "granted",
                        "permission_id": new_perm.id,
                    })
                    successful += 1

                except Exception as e:
                    results.append({
                        "cog_id": cog_id,
                        "user_id": user_id,
                        "status": "error",
                        "error_message": str(e),
                    })
                    failed += 1

        if emails:
            for email in emails:
                try:
                    existing_user = templates_crud.get_user_by_email_in_tenant(db, email, tenant_id=tenant_id)

                    if existing_user:
                        existing_grants = list_cog_grants(db, cog_ids=[cog_id], user_id=existing_user.id)
                        existing_perm = existing_grants[0] if existing_grants else None

                        if existing_perm:
                            results.append({
                                "cog_id": cog_id,
                                "email": email,
                                "user_id": existing_user.id,
                                "status": "already_exists",
                                "permission_id": existing_perm.id,
                            })
                            successful += 1
                            continue

                    # TODO: Create invitation for external user
                    # This would involve creating an Invitation record and triggering email
                    results.append({
                        "cog_id": cog_id,
                        "email": email,
                        "status": "error",
                        "error_message": "External user invitations not yet implemented",
                    })
                    failed += 1

                except Exception as e:
                    results.append({
                        "cog_id": cog_id,
                        "email": email,
                        "status": "error",
                        "error_message": str(e),
                    })
                    failed += 1

    templates_crud.record_usage(db, template, successful_count=successful)

    return {
        "template_id": template.id,
        "template_name": template.name,
        "total_requested": len(results),
        "successful": successful,
        "failed": failed,
        "results": results,
    }
