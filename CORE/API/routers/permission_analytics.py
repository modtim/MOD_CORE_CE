"""
Permission Analytics Endpoints

Provides metrics and insights into permission usage across the tenant:
- Permission distribution by level and folder
- Expiring permissions trends
- External user statistics
- Template usage analytics
- Permission grant/revoke activity
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, status, Query, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from pydantic import BaseModel

import models
from dependencies import get_db, get_current_user
from services import permission_analytics_service
from services._base import NotFoundError


# --- Response Schemas ---

class PermissionDistributionByLevel(BaseModel):
    """Permission counts by level."""
    viewer: int
    editor: int
    owner: int
    total: int


class FolderPermissionStats(BaseModel):
    """Permission statistics for a single folder."""
    cog_id: int
    cog_name: str
    owner_name: str
    total_permissions: int
    viewer_count: int
    editor_count: int
    owner_count: int
    external_user_count: int
    expiring_soon_count: int  # Within 7 days


class ExternalUserStats(BaseModel):
    """Statistics about external users."""
    total_external_users: int
    total_external_permissions: int
    expiring_within_7d: int
    expiring_within_30d: int
    average_duration_days: Optional[float]


class TemplateUsageStats(BaseModel):
    """Usage statistics for permission templates."""
    template_id: int
    template_name: str
    times_used: int
    last_used_at: Optional[datetime]
    active_permissions_count: int  # Permissions granted from this template that are still active


class PermissionActivityStats(BaseModel):
    """Recent permission activity."""
    granted_last_7d: int
    granted_last_30d: int
    revoked_last_7d: int
    revoked_last_30d: int
    extended_last_7d: int
    extended_last_30d: int


class ExpirationTrendPoint(BaseModel):
    """Data point for expiration trend chart."""
    date: str  # YYYY-MM-DD
    expiring_count: int


class CapabilityDistribution(BaseModel):
    """Distribution of capability overrides."""
    capability: str
    enabled_count: int
    disabled_count: int
    default_count: int  # Using level default


class PermissionAnalyticsOverview(BaseModel):
    """High-level overview of permission analytics."""
    total_permissions: int
    total_folders_with_permissions: int
    distribution_by_level: PermissionDistributionByLevel
    external_user_stats: ExternalUserStats
    activity_stats: PermissionActivityStats
    top_shared_folders: List[FolderPermissionStats]


class UserPermissionSummary(BaseModel):
    """Summary of permissions for a specific user."""
    user_id: int
    user_email: str
    user_name: str
    total_permissions: int
    viewer_permissions: int
    editor_permissions: int
    owner_permissions: int
    expiring_soon_count: int
    folders_accessed: List[Dict[str, Any]]


router = APIRouter(
    prefix="/analytics/permissions",
    tags=["Analytics - Permissions"]
)


@router.get("/overview", response_model=PermissionAnalyticsOverview)
def get_permission_analytics_overview(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get high-level overview of permission analytics for the tenant.

    Includes:
    - Total permissions and distribution by level
    - External user statistics
    - Recent activity metrics
    - Top shared folders
    """
    return permission_analytics_service.get_permission_analytics_overview(
        db, tenant_id=current_user.tenant_id
    )


@router.get("/folders", response_model=List[FolderPermissionStats])
def get_folder_permission_stats(
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get detailed permission statistics for all folders in the tenant.

    Returns up to `limit` folders, sorted by total permission count.
    """
    return permission_analytics_service.get_folder_permission_stats(
        db, tenant_id=current_user.tenant_id, limit=limit
    )


@router.get("/expiration-trend", response_model=List[ExpirationTrendPoint])
def get_expiration_trend(
        days_ahead: int = Query(30, ge=1, le=90),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get expiration trend showing how many permissions expire each day.

    Returns data points for the next `days_ahead` days.
    Useful for charting expiration patterns.
    """
    return permission_analytics_service.get_expiration_trend(
        db, tenant_id=current_user.tenant_id, days_ahead=days_ahead
    )


@router.get("/templates", response_model=List[TemplateUsageStats])
def get_template_usage_stats(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get usage statistics for all permission templates.

    Shows which templates are most popular and how many active
    permissions were created from each template.
    """
    return permission_analytics_service.get_template_usage_stats(
        db, tenant_id=current_user.tenant_id
    )


@router.get("/capabilities", response_model=List[CapabilityDistribution])
def get_capability_distribution(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get distribution of capability overrides.

    Shows how often each capability is explicitly enabled, disabled,
    or left at default for the permission level.
    """
    return permission_analytics_service.get_capability_distribution(
        db, tenant_id=current_user.tenant_id
    )


@router.get("/users/{user_id}", response_model=UserPermissionSummary)
def get_user_permission_summary(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get detailed permission summary for a specific user.

    Shows all folders they have access to and their permission levels.
    """
    try:
        return permission_analytics_service.get_user_permission_summary(
            db, tenant_id=current_user.tenant_id, user_id=user_id
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message
        )


@router.get("/external-users", response_model=List[UserPermissionSummary])
def get_external_users_summary(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get summary of all external users and their permissions.

    Useful for auditing external access.
    """
    return permission_analytics_service.get_external_users_summary(
        db, tenant_id=current_user.tenant_id
    )
