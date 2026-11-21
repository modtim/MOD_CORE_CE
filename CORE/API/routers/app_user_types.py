"""GX.S1b (launch-completion-megaplan Group X) — app user types + app users.

Tenant-admin management surface for the user types an app declares (e.g.
`consignor`) and the people admitted under them:

    GET    /v1/apps/{app_id}/user-types          list the app's types
    POST   /v1/apps/{app_id}/user-types          define (or update) a type
    PUT    /v1/apps/{app_id}/user-types/{id}     label / self-signup toggle
    DELETE /v1/apps/{app_id}/user-types/{id}     soft-delete (ISO 27001)
    GET    /v1/apps/{app_id}/users               list admitted app users
    POST   /v1/apps/{app_id}/users               invite (KC user + group)
    POST   /v1/apps/{app_id}/users/{id}/disable  soft-disable (+ de-group)

All routes are tenant-admin only (the SAME `_require_tenant_admin`
capability gate as routers/apps.py — imported, not redefined) and
tenant-scoped: a foreign app id 404s (no existence leak). The app user
THEMSELVES never reach these routes — they have no MOD identity at all
(dependencies.get_current_user refuses them with
APP_USER_NO_PLATFORM_ACCESS before any of this).

Business logic lives in services/app_user_types.py (KC admin calls mocked
at the seam in tests).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db
from routers.apps import _require_tenant_admin
from services import app_user_types

router = APIRouter(
    prefix="/apps/{app_id}",
    tags=["AppUserTypes"],
    dependencies=[Depends(_require_tenant_admin)],
)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

class UserTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    label: Optional[str] = None
    self_signup: bool = False


class UserTypeOut(BaseModel):
    id: int
    name: str
    label: Optional[str] = None
    self_signup: bool
    created_at: Optional[datetime] = None


class UserTypeUpdateIn(BaseModel):
    label: Optional[str] = None
    self_signup: Optional[bool] = None


class AppUserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    user_type: str = Field(min_length=1, max_length=63)


class AppUserOut(BaseModel):
    id: int
    email: str
    user_type: str
    status: str
    kc_sub: Optional[str] = None
    invited_by: Optional[int] = None
    created_at: Optional[datetime] = None
    disabled_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# tenant-scoped app fetch (404 on foreign/soft-deleted — no existence leak)
# ---------------------------------------------------------------------------

def _get_app(db_session: Session, app_id: int, tenant_id: int) -> models.App:
    app = (
        db_session.query(models.App)
        .filter(models.App.id == app_id, models.App.tenant_id == tenant_id)
        .first()
    )
    if app is None or app.deleted_at is not None:
        raise HTTPException(status_code=404, detail="App not found")
    return app


def _type_out(t: models.AppUserType) -> UserTypeOut:
    return UserTypeOut(id=t.id, name=t.name, label=t.label,
                       self_signup=bool(t.self_signup), created_at=t.created_at)


def _user_out(u: models.AppUser) -> AppUserOut:
    return AppUserOut(id=u.id, email=u.email, user_type=u.user_type,
                      status=u.status, kc_sub=u.kc_sub, invited_by=u.invited_by,
                      created_at=u.created_at, disabled_at=u.disabled_at)


# ---------------------------------------------------------------------------
# user types
# ---------------------------------------------------------------------------

@router.get("/user-types", response_model=List[UserTypeOut])
def list_types(app_id: int,
               db: Session = Depends(get_db),
               current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    return [_type_out(t) for t in app_user_types.list_user_types(db, app)]


@router.post("/user-types", response_model=UserTypeOut, status_code=201)
def define_type(app_id: int, body: UserTypeIn,
                db: Session = Depends(get_db),
                current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    t = app_user_types.define_user_type(
        db, app, current_user, name=body.name, label=body.label,
        self_signup=body.self_signup)
    return _type_out(t)


@router.put("/user-types/{type_id}", response_model=UserTypeOut)
def update_type(app_id: int, type_id: int, body: UserTypeUpdateIn,
                db: Session = Depends(get_db),
                current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    t = app_user_types.update_user_type(
        db, app, current_user, type_id, label=body.label,
        self_signup=body.self_signup)
    return _type_out(t)


@router.delete("/user-types/{type_id}", response_model=UserTypeOut)
def delete_type(app_id: int, type_id: int,
                db: Session = Depends(get_db),
                current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    t = app_user_types.soft_delete_user_type(db, app, current_user, type_id)
    return _type_out(t)


# ---------------------------------------------------------------------------
# app users
# ---------------------------------------------------------------------------

@router.get("/users", response_model=List[AppUserOut])
def list_users(app_id: int,
               db: Session = Depends(get_db),
               current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    return [_user_out(u) for u in app_user_types.list_app_users(db, app)]


@router.post("/users", response_model=AppUserOut, status_code=201)
def invite_user(app_id: int, body: AppUserIn,
                db: Session = Depends(get_db),
                current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    u = app_user_types.invite_user(db, app, current_user,
                                   email=body.email,
                                   type_name=body.user_type)
    return _user_out(u)


@router.post("/users/{app_user_id}/disable", response_model=AppUserOut)
def disable_user(app_id: int, app_user_id: int,
                 db: Session = Depends(get_db),
                 current_user: models.User = Depends(_require_tenant_admin)):
    app = _get_app(db, app_id, current_user.tenant_id)
    u = app_user_types.disable_user(db, app, current_user, app_user_id)
    return _user_out(u)
