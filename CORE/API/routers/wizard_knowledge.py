"""routers/wizard_knowledge.py -- wizard-knowledge-pack Phase 2
(`the internal design notes` 2.5-2.8): the
platform-admin-gated `POST /wizard/update-knowledge` endpoint that
stitches together `services/knowledge_pack_consumer.py`'s pipeline
(`fetch_manifest -> verify_signature -> download_pack ->
verify_content_hash -> apply_pack`) and emits the audit trail.

Two invocation paths, selected by the request body:
  - Default (network) path: `pack_url` (optional override of the
    distribution manifest URL; defaults to `DEFAULT_MANIFEST_URL`) is
    passed to `fetch_manifest()`. The returned manifest's own
    `source_url` field points at the pack body, fetched via
    `download_pack()`.
  - Air-gap manual-upload path: `pack_bytes_b64` is the base64 of a
    single JSON document combining the signed manifest fields
    (`version`/`checksum`/`signature`) and the pack body under a
    `pack` key -- i.e. exactly what an operator would save from the
    distribution endpoint for a disconnected install. No network call
    is made; `source_url` is recorded as `None` in the ledger. The
    same `verify_signature()`/`verify_content_hash()`/`apply_pack()`
    calls run either way -- air-gap is not a weaker verification path.

Every invocation (success, rejection, or integrity rollback) emits
exactly one `models.AuditEvent` row -- see `_emit_audit()`.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services import knowledge_pack_consumer as kpc
from services import wizard_knowledge_settings as wks

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Wizard Knowledge Pack"])

# Distribution endpoint (design.md:19-28) -- fixed infra literal, not a
# behavior tunable, so this is a module constant rather than a platform
# setting or env var.
DEFAULT_MANIFEST_URL = "https://example.com/wizard-knowledge/latest.json"

_EVENT_APPLIED = "wizard_knowledge_pack_applied"
_EVENT_REJECTED = "wizard_knowledge_pack_rejected"
_EVENT_ROLLED_BACK = "wizard_knowledge_pack_rolled_back"


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Platform-admin gate -- mirrors `routers/admin_email_config.py`'s
    local dependency (each admin router defines its own; there is no
    shared central one)."""
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires platform admin privileges",
        )
    return current_user


class UpdateKnowledgeRequest(BaseModel):
    pack_url: Optional[str] = None
    pack_bytes_b64: Optional[str] = None


class UpdateKnowledgeResponse(BaseModel):
    version_applied: Optional[str] = None
    rows_upserted: int = 0
    integrity_status: str
    rejection_reasons: Optional[list] = None


def _emit_audit(db: Session, user: models.User, event_name: str,
                details: Dict[str, Any]) -> None:
    """Emit exactly one append-only `AuditEvent` row. Never raises --
    an audit failure must not mask the real response, but every
    invocation of the endpoint still attempts exactly one emit."""
    try:
        db.add(models.AuditEvent(
            tenant_id=user.tenant_id,
            user_id=user.id,
            event_name=event_name,
            details=details,
        ))
        db.commit()
    except Exception:
        logger.warning("Audit emit for %s failed", event_name, exc_info=True)


def _resolve_manifest_and_pack(
    db: Session, body: UpdateKnowledgeRequest,
) -> "tuple[Optional[dict], bytes, Optional[str]]":
    """Returns `(manifest, pack_bytes, source_url)`. `manifest` is
    `None` on an unreachable/malformed fetch (network path only --
    fail-closed, matches `fetch_manifest`'s own contract)."""
    if body.pack_bytes_b64:
        # Air-gap manual-upload path -- no network call. The uploaded
        # blob combines the signed manifest fields with the pack body
        # under a `pack` key (see module docstring).
        #
        # Size cap BEFORE decode: a knowledge pack is a small JSON doc
        # (denylist/prompt-override/hint lists), not bulk media, so an
        # unbounded base64 body here is a memory-DoS vector (decode of
        # an attacker-controlled blob with no limit). Fail closed with
        # the same rejection shape as a malformed blob.
        cap = wks.max_pack_b64_bytes(db)
        if len(body.pack_bytes_b64) > cap:
            return None, b"", None
        try:
            raw = base64.b64decode(body.pack_bytes_b64)
            blob = json.loads(raw)
        except Exception:
            return None, b"", None
        if not isinstance(blob, dict):
            return None, b"", None
        manifest = {k: blob.get(k) for k in ("version", "checksum", "signature")}
        pack = blob.get("pack")
        if not isinstance(pack, dict):
            return manifest, b"", None
        pack_bytes = json.dumps(pack, sort_keys=True).encode("utf-8")
        return manifest, pack_bytes, None

    manifest_url = body.pack_url or DEFAULT_MANIFEST_URL
    manifest = kpc.fetch_manifest(manifest_url, db)
    if manifest is None:
        return None, b"", None
    source_url = manifest.get("source_url")
    pack_bytes = kpc.download_pack(source_url, db) if source_url else b""
    return manifest, pack_bytes, source_url


@router.post("/wizard/update-knowledge", response_model=UpdateKnowledgeResponse)
def update_knowledge(
    body: UpdateKnowledgeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    manifest, pack_bytes, source_url = _resolve_manifest_and_pack(db, body)

    if manifest is None:
        reasons = ["manifest_unreachable"]
        _emit_audit(db, current_user, _EVENT_REJECTED,
                   {"rejection_reasons": reasons})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"detail": "manifest_unreachable",
                                   "rejection_reasons": reasons})

    version = manifest.get("version")
    pubkey = wks.wizard_knowledge_pack_pubkey()
    if not kpc.verify_signature(manifest, pubkey):
        reasons = ["signature_invalid"]
        _emit_audit(db, current_user, _EVENT_REJECTED,
                   {"version": version, "rejection_reasons": reasons})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"detail": "signature_invalid",
                                   "rejection_reasons": reasons})

    if not kpc.verify_content_hash(pack_bytes, manifest.get("checksum")):
        reasons = ["checksum_mismatch"]
        _emit_audit(db, current_user, _EVENT_REJECTED,
                   {"version": version, "rejection_reasons": reasons})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"detail": "checksum_mismatch",
                                   "rejection_reasons": reasons})

    try:
        pack = json.loads(pack_bytes)
    except Exception:
        reasons = ["pack_malformed"]
        _emit_audit(db, current_user, _EVENT_REJECTED,
                   {"version": version, "rejection_reasons": reasons})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"detail": "pack_malformed",
                                   "rejection_reasons": reasons})
    if source_url is not None:
        pack.setdefault("source_url", source_url)

    try:
        result = kpc.apply_pack(db, pack, version, actor_user_id=current_user.id)
    except kpc.KnowledgePackDowngradeError:
        reasons = ["downgrade_rejected"]
        _emit_audit(db, current_user, _EVENT_REJECTED,
                   {"version": version, "rejection_reasons": reasons})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"detail": "downgrade_rejected",
                                   "rejection_reasons": reasons})
    except kpc.KnowledgePackReplayError:
        reasons = ["replay_rejected"]
        _emit_audit(db, current_user, _EVENT_REJECTED,
                   {"version": version, "rejection_reasons": reasons})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"detail": "replay_rejected",
                                   "rejection_reasons": reasons})

    if result.get("integrity_status") == "rolled_back":
        reasons = result.get("reasons") or []
        _emit_audit(db, current_user, _EVENT_ROLLED_BACK,
                   {"version": version, "rejection_reasons": reasons,
                    "rows_upserted": result.get("rows_upserted", 0)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"detail": "integrity_check_failed",
                   "rejection_reasons": reasons})

    rows_upserted = result.get("rows_upserted", 0)
    _emit_audit(db, current_user, _EVENT_APPLIED,
               {"version": version, "rows_upserted": rows_upserted})
    return UpdateKnowledgeResponse(
        version_applied=version,
        rows_upserted=rows_upserted,
        integrity_status="applied",
    )
