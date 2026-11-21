"""Business logic for the per-tenant Named Artifact registry (named-artifact-registry).

Owns type validation and the upsert-by-(tenant, type, name) policy; all data
access goes through `crud/named_artifacts_cud.py`. See
`routers/named_artifacts.py` for the domain description (rag / lora artifact
types, auth model) — tenant resolution from the verified credential stays a
router-level `Depends()` (it must resolve before the handler body; see
services/_base.py module docstring / migrate rules on auth guards).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
from crud import named_artifacts_cud
from services._base import ServiceError
try:
    from services import model_provenance
except ImportError:
    model_provenance = None

logger = logging.getLogger(__name__)

VALID_TYPES = ("rag", "lora")
# FX.148 p4a part 2 — of the artifact types registered here, the ones whose
# payload IS model weights (a lora .safetensors). A rag artifact is a text
# collection, not a model, so it gets no model-provenance row.
MODEL_WEIGHT_TYPES = ("lora",)


def normalize_type(t: str) -> str:
    """Validate + lowercase an artifact type. Raises ServiceError(code=
    'bad_request') if not one of VALID_TYPES (router maps -> 400)."""
    normalized = (t or "").strip().lower()
    if normalized not in VALID_TYPES:
        raise ServiceError(
            f"type must be one of {VALID_TYPES!r}, got {t!r}",
            code="bad_request",
        )
    return normalized


def upsert_named_artifact(
    db: Session,
    *,
    tenant_id: int,
    art_type: str,
    name: str,
    ref: str,
    description: Optional[str],
    metadata: Optional[Dict[str, Any]],
    user=None,
) -> models.NamedArtifact:
    """Register or update a named artifact for `tenant_id`. Upsert key is
    (tenant_id, type, name) among live rows — a second call with the same name
    updates ref/description/metadata in place rather than creating a
    duplicate."""
    normalized_type = normalize_type(art_type)
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ServiceError("name is required", code="bad_request")

    existing = named_artifacts_cud.get_live_by_name(
        db, tenant_id=tenant_id, art_type=normalized_type, name=normalized_name,
    )
    if existing is not None:
        existing.ref = ref
        existing.description = description
        existing.artifact_metadata = metadata
        existing.updated_at = datetime.now(timezone.utc)
        row = existing
    else:
        row = models.NamedArtifact(
            tenant_id=tenant_id,
            type=normalized_type,
            name=normalized_name,
            ref=ref,
            description=description,
            artifact_metadata=metadata,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    _record_provenance(db, row, user)
    return row


def _record_provenance(
    db: Session, row: models.NamedArtifact, user=None,
) -> None:
    """FX.148 p4a part 2 — one append-only model-provenance row per model-weights
    registration (import_kind="register").

    Only MODEL_WEIGHT_TYPES (lora) get a row: a rag artifact is a text
    collection, not a model, so a model-provenance row for it would be a lie.
    Written AFTER the artifact commit, never before — same rule as the
    embedding-model upload path: a row only exists for an artifact that is
    actually in the registry.

    The source claim is DECLARED, not verified: if the registration carried an
    explicit source_url/source_revision in its metadata, that is what the row
    records; otherwise the row records the resolvable `ref` as the declared
    source (the only thing the registrant asserted about where the weights
    came from). No weights are fetched here — `ref` usually names a file under
    the SDXL server's /loras or an S3 key, so file_sha256 stays None and the
    row simply says "registered, digest not computed at registration".

    NEVER fails the registration: model_provenance.record_import already
    swallows its own failures, but the settings read and the FK/DB layer can
    still raise, so the call is wrapped — a provenance recorder is not a gate.
    """
    if row.type not in MODEL_WEIGHT_TYPES:
        return
    if model_provenance is None:
        # CE: model-provenance recording is a paid/compliance feature — silently
        # skip, matching the "never fails the registration" intent above.
        return
    meta = row.artifact_metadata if isinstance(row.artifact_metadata, dict) else {}
    try:
        model_provenance.record_import(
            db,
            model_name=row.name,
            model_type=row.type,
            source_url=(meta.get("source_url") or row.ref),
            source_revision=meta.get("source_revision"),
            user=user,
            tenant_id=row.tenant_id,
            import_kind="register",
        )
    except Exception as e:  # noqa: BLE001 — recording must never fail a registration
        logger.warning("FX.148 p4a: provenance record failed for named artifact "
                       "%r (tenant=%s): %s", row.name, row.tenant_id, e)


def list_named_artifacts(
    db: Session, *, tenant_id: int, art_type: Optional[str] = None,
) -> List[models.NamedArtifact]:
    """List `tenant_id`'s live named artifacts, optionally filtered by type."""
    normalized_type = normalize_type(art_type) if art_type is not None else None
    return named_artifacts_cud.list_live(
        db, tenant_id=tenant_id, art_type=normalized_type,
    )


def get_named_artifact(
    db: Session, *, artifact_id: int, tenant_id: int,
) -> models.NamedArtifact:
    """Fetch a single live named artifact BY ID for `tenant_id`.

    The tenant filter is mandatory: a caller in tenant B asking for tenant
    A's artifact id gets ServiceError(code='not_found') — 404, NOT 403 —
    matching the router's existing cross-tenant convention (a foreign id
    looks the same as a missing one)."""
    row = named_artifacts_cud.get_live_by_id(
        db, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    if row is None:
        raise ServiceError("Named artifact not found.", code="not_found")
    return row


def publish_named_artifact(
    db: Session,
    *,
    artifact_id: int,
    tenant_id: int,
) -> Dict[str, Any]:
    """FX.131.s1 (Q118=A) — the explicit marketplace-publish action.

    A trained model is customer data and is NOT publishable by default: the
    publish gate refuses unless the artifact's training record carries BOTH
    the explicit marketplace-publish flag (``marketplace_publish`` is True)
    AND a likeness-consent link (``consent_record_id`` — the Q84 consent
    register row the model's likeness was captured under).

    No new column: the flag lives in the existing ``training_record`` JSONB
    (the FX.131 provenance record, which already homes the consent link id),
    so a pre-existing / un-flagged row is publishable=False by construction.

    Refusal is ``forbidden`` (403) — the artifact exists and is readable to
    the caller, it just is not cleared for the marketplace. A foreign/missing
    id is still a 404 (``get_named_artifact``), and a rag artifact is a 400:
    only trained models go to the marketplace."""
    row = get_named_artifact(db, artifact_id=artifact_id, tenant_id=tenant_id)
    if row.type != "lora":
        raise ServiceError(
            "Only trained-model (lora) artifacts can be published.",
            code="bad_request",
        )

    record = row.training_record if isinstance(row.training_record, dict) else {}
    consent_record_id = record.get("consent_record_id")
    if record.get("marketplace_publish") is not True:
        raise ServiceError(
            "Trained artifact is not marked publishable: set "
            "training_record.marketplace_publish=True to publish it.",
            code="forbidden",
        )
    if consent_record_id is None:
        raise ServiceError(
            "Trained artifact has no likeness-consent link "
            "(training_record.consent_record_id): a real person's likeness "
            "cannot be published to the marketplace without the consent "
            "record it was captured under.",
            code="forbidden",
        )

    return {
        "name": row.name,
        "type": row.type,
        "ref": row.ref,
        "publishable": True,
        "consent_record_id": consent_record_id,
        "consent_type": record.get("consent_type"),
        "dataset_ids": record.get("dataset_ids") or [],
    }


def download_trained_model(
    db: Session,
    *,
    artifact_id: int,
    tenant_id: int,
    minio_client,
    actor_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Tenant-scoped download of a trained-model artifact (type == "lora").

    FX.131 (Q118=A): a trained model is customer data. The ref for a lora is
    the content hash of the .safetensors the LoRA Train node wrote to the
    tenant's own bucket (node outputs land in S3 by content-hash), so the
    download presigns that object from the CALLER's tenant bucket only —
    never a foreign bucket or a shared path. Returns a dict with the
    presigned URL; a missing/foreign artifact is a not-found (404).

    Storage encryption at rest follows the tenant's existing storage
    configuration (the tenant bucket is served by the same versitygw/MinIO
    backend + BYO-S3 storage connection every other tenant file uses) — this
    endpoint does not bypass or alter that; it only resolves a tenant-owned
    object and presigns a short-lived GET against the caller's bucket.

    Every successful hand-off writes a TRAINED_MODEL_DOWNLOADED audit row
    (best-effort: an audit failure degrades to a log line and still
    delivers, matching the cogs download path)."""
    row = get_named_artifact(db, artifact_id=artifact_id, tenant_id=tenant_id)
    if row.type != "lora":
        raise ServiceError(
            "Only trained-model (lora) artifacts can be downloaded here.",
            code="bad_request",
        )

    # The ref is the resolvable location the SDXL server loads. For trained
    # models it is the bare content-hash filename the train node emitted
    # (node outputs are stored by content-hash in the tenant bucket); a ref
    # that is clearly not a bare object key (contains a path separator or
    # looks like a repo id) is refused — this endpoint serves tenant S3
    # objects only, never an external/remote ref.
    ref = (row.ref or "").strip()
    if not ref or "/" in ref or "\\" in ref or ".." in ref:
        raise ServiceError(
            "Artifact ref is not a downloadable object key.", code="not_found",
        )

    object_key = ref
    # Prefer the explicit node-output content hash when the ref itself is a
    # filename like <name>.safetensors (strip extension to the hash if the
    # ref stored the hash; otherwise the ref IS the object key).
    url: Optional[str] = None
    try:
        url = minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=object_key,
            expires=900,
            response_headers={
                "response-content-disposition":
                    f'attachment; filename="{row.name}.safetensors"',
            },
        )
    except Exception:
        url = None

    # FX.131 — audit every trained-model download (best-effort). The audit
    # row's actor_user_id is a NOT NULL FK to users.id, so resolve a real
    # user: the caller when given, else any user of the tenant (a node /
    # run-token download attributes to the tenant's first user; a tenant with
    # no users skips the audit row rather than 500 the download).
    try:
        actor = actor_user_id
        if actor is None:
            first_user = (
                db.query(models.User)
                .filter(models.User.tenant_id == tenant_id)
                .first()
            )
            actor = first_user.id if first_user is not None else None
        if actor is not None:
            from audit import log_permission_audit
            log_permission_audit(
                db=db,
                action=models.AuditAction.TRAINED_MODEL_DOWNLOADED,
                actor_user_id=actor,
                tenant_id=tenant_id,
                resource_type="named_artifact",
                resource_id=row.id,
                metadata={
                    "artifact_name": row.name,
                    "artifact_type": row.type,
                    "object_key": object_key,
                },
            )
    except Exception:  # noqa: BLE001 — never block the download on audit
        import logging
        logging.getLogger(__name__).warning(
            "FX.131 trained-model download audit emit failed (non-fatal)",
            exc_info=True,
        )

    if not url:
        raise ServiceError(
            "Trained model object is not available in tenant storage.",
            code="not_found",
        )
    return {
        "name": row.name,
        "type": row.type,
        "ref": ref,
        "object_key": object_key,
        "download_url": url,
        "training_record": row.training_record,
    }
