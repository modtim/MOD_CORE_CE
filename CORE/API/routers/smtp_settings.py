# In API/routers/smtp_settings.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud, schemas
from dependencies import get_db, get_current_user

router = APIRouter(
    tags=["smtp-settings"],
    responses={404: {"description": "Not found"}},
)


@router.get("/", response_model=schemas.SMTPSettings)
def read_smtp_settings(current_user: schemas.User = Depends(get_current_user), db: Session = Depends(get_db)):
    db_smtp_settings = crud.get_smtp_settings_by_user_id(db, user_id=current_user.id)
    if db_smtp_settings is None:
        raise HTTPException(status_code=404, detail="SMTP settings not found")
    return db_smtp_settings


@router.post("/", response_model=schemas.SMTPSettings)
def create_smtp_settings_for_user(
    smtp_settings: schemas.SMTPSettingsCreate,
    current_user: schemas.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_smtp_settings = crud.get_smtp_settings_by_user_id(db, user_id=current_user.id)
    if db_smtp_settings:
        raise HTTPException(status_code=400, detail="SMTP settings already exist for this user")
    result = crud.create_smtp_settings(db=db, smtp_settings=smtp_settings, user_id=current_user.id)

    # Audit: admin config change (CMMC AU-2)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="ADMIN_CONFIG_CHANGE",
            details={"setting": "smtp", "action": "created"},
        ))
        db.commit()
    except Exception:
        pass

    return result


@router.put("/", response_model=schemas.SMTPSettings)
def update_current_user_smtp_settings(
    smtp_settings_update: schemas.SMTPSettingsUpdate,
    current_user: schemas.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_smtp_settings = crud.get_smtp_settings_by_user_id(db, user_id=current_user.id)
    if not db_smtp_settings:
        raise HTTPException(status_code=404, detail="SMTP settings not found")
    result = crud.update_smtp_settings(db=db, smtp_settings_id=db_smtp_settings.id, smtp_settings_update=smtp_settings_update)

    # Audit: admin config change (CMMC AU-2)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="ADMIN_CONFIG_CHANGE",
            details={"setting": "smtp", "action": "updated"},
        ))
        db.commit()
    except Exception:
        pass

    return result


@router.delete("/", response_model=schemas.SMTPSettings)
def delete_current_user_smtp_settings(
    current_user: schemas.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_smtp_settings = crud.get_smtp_settings_by_user_id(db, user_id=current_user.id)
    if not db_smtp_settings:
        raise HTTPException(status_code=404, detail="SMTP settings not found")
    return crud.delete_smtp_settings(db=db, smtp_settings_id=db_smtp_settings.id)