"""Platform-admin embedding-model store (upload → MinIO).

Lets a platform admin seed the `platform-models/embeddings/<name>/` mirror by
uploading a sentence-transformers model directory (as a .tar.gz) — so the
platform never needs HuggingFace at build/deploy time. Same shape as artifact
staging: safe extraction, validation, upload to MinIO via embedding_model_store.
"""
import os
import re
import shutil
import tarfile
import tempfile
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import models
from dependencies import get_db, require_active_and_onboarded_user
from sqlalchemy.orm import Session
from services import files_service
from services import pickle_import_scan
try:
    from services import model_trust_approval
    from services import model_provenance
    from services import model_signing
except ImportError:
    model_trust_approval = None
    model_provenance = None
    model_signing = None
import audit
from utils import embedding_model_store as ems
from utils import http_errors

logger = logging.getLogger(__name__)

# clean-v2-api (admin dissolve wave): moved from `/admin/embedding-models` to
# the top-level `/embedding-models` resource (design.md: "embedding-models
# (moved out of /admin)"). `router` itself carries no prefix — routes live on
# `_impl` (mounted at the canonical prefix). The old `/admin/embedding-models`
# back-compat alias for settings.js's per-tool model picker has been removed
# (final clean-cut) now that settings.js is repointed to `/embedding-models`.
router = APIRouter(tags=["embedding-models"])
_impl = APIRouter()

_MAX_MEMBERS = int(os.getenv("EMBEDDING_UPLOAD_MAX_MEMBERS", "5000"))
_MAX_BYTES = int(os.getenv("EMBEDDING_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))  # 10 GB
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def require_platform_admin(current_user: models.User = Depends(require_active_and_onboarded_user)):
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="This endpoint requires platform admin privileges")
    return current_user


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    base = os.path.realpath(dest)
    total = count = 0
    for m in tar.getmembers():
        if m.islnk() or m.issym():
            raise HTTPException(400, f"archive contains a link member: {m.name}")
        if not (m.isreg() or m.isdir()):
            raise HTTPException(400, f"archive contains a non-regular member: {m.name}")
        count += 1
        if count > _MAX_MEMBERS:
            raise HTTPException(400, f"archive exceeds max member count ({_MAX_MEMBERS})")
        total += max(0, m.size)
        if total > _MAX_BYTES:
            raise HTTPException(413, f"archive exceeds max size ({_MAX_BYTES} bytes)")
        target = os.path.realpath(os.path.join(dest, m.name))
        if target != base and not target.startswith(base + os.sep):
            raise HTTPException(400, f"archive member escapes destination: {m.name}")
    tar.extractall(dest)


# Known dimensions of the platform's active embedding consumers. Uploading a
# model whose dimension differs from the store a tool will write into silently
# breaks retrieval (vectors of mismatched width can't share a collection), so
# the upload gate surfaces this. See dimension-migration notes.
#   RAG (utils/embeddings.py)            = all-MiniLM-L6-v2 (384)
#   Wizard (services/wizard_ml)          = snowflake-arctic-embed-l (1024)
_ACTIVE_CONSUMER_DIMS = {
    "rag": int(os.getenv("MOD_EMBED_DIM", "384")),
    "wizard": int(os.getenv("WIZARD_EMBED_DIM", "1024")),
}


class ModelOut(BaseModel):
    name: str
    files: int
    bytes: int
    dimension: int | None = None
    is_sentence_transformer: bool | None = None
    # §4.5 — the type is reported wherever a model is listed. Without it a
    # consumer picking "a model" can be handed the wrong kind, and only find out
    # on a worker at serve time.
    type: str = "embedding"
    # §4.7 — whether the fleet can host it, for language models. Reported here
    # rather than discovered at deploy: a stored model nothing can run is a
    # fact worth knowing while looking at the list of stored models.
    servable: bool | None = None
    unservable_reason: str | None = None


def _llm_servable(m: dict):
    """§4.7 — can any worker host this stored language model?

    Estimated from the stored size, because an uploaded model declares no
    hardware requirement: weights plus room for KV cache and activations. Crude
    on purpose and stated as an estimate — the alternative is asking an uploader
    for a number they usually do not have, and being wrong there produces a
    stack that will not place rather than a warning that might be pessimistic.
    """
    _mai = None
    try:
        from utils.paid_import import optional_paid
        _mai = optional_paid("services.managed_ai_stacks")
        if _mai is None:
            return None, None
        from database import SessionLocal
        from routers.persistent_stacks import _managed_ai_placement
        gb = float(m.get("bytes") or 0) / 1e9
        if gb <= 0:
            return None, None
        need = round(gb * 1.6, 1)          # weights + KV/activation headroom
        kind = _mai.normalize_kind(os.getenv("MANAGED_AI_ACCELERATOR_KIND")
                              or _mai.DEFAULT_ACCELERATOR_KIND)
        db = SessionLocal()
        try:
            v = _managed_ai_placement(db, {kind: [need]}, None)
        finally:
            db.close()
        if v.get("placeable"):
            return True, None
        return False, (f"estimated ~{need} GB needed (from {gb:.1f} GB of "
                       f"weights). {v.get('reason') or 'No worker can host it.'}")
    except Exception:  # noqa: BLE001
        # _mai is None here only when the module itself is absent (CE) or the
        # optional_paid() lookup failed before it ran — never a real failure
        # worth a traceback. Once _mai is bound the module IS present, so any
        # exception past that point is a genuine bug in the paid path and
        # must keep its traceback.
        logger.debug("servability check failed for %s", m.get("name"),
                     exc_info=_mai is not None)
        return None, None


class MigrateRequest(BaseModel):
    # Which store to re-embed: user_rag (AI Assistant), wizard (Node Wizard
    # pgvector), or docs (MOD docs). Default the user RAG store.
    target: str = "user_rag"
    # Optional explicit tenant list (user_rag only); None = every tenant.
    tenant_ids: list[int] | None = None


@_impl.post("/migrate", summary="Re-embed a store to its pinned model")
def start_migration(body: MigrateRequest | None = None, _admin=Depends(require_platform_admin)):
    """Kick off a re-embed of the chosen store (user_rag / wizard / docs) to its
    currently-pinned model. Runs as one background job; poll
    /embedding-models/migration-status."""
    from utils import embedding_migration as em
    target = (body.target if body else None) or "user_rag"
    if target == "docs":
        from utils.paid_import import optional_paid
        if optional_paid("utils.docs_ingester") is None:
            raise HTTPException(status_code=402, detail="Docs embeddings are not available in this edition.")
    return em.start(target=target, tenant_ids=(body.tenant_ids if body else None) or None)


@_impl.get("/migration-status", summary="Embedding migration progress")
def migration_status(_admin=Depends(require_platform_admin)):
    from utils import embedding_migration as em
    return em.get_status()


@_impl.get("", response_model=list[ModelOut])
def list_embedding_models(_admin=Depends(require_platform_admin)):
    """The embedding models seeded in MinIO (so deploys never need HuggingFace)."""
    out = []
    for m in ems.list_models():
        mtype = m.get("type", "embedding")
        manifest = ems.read_manifest(m["name"], mtype) or {}
        servable = unservable = None
        if mtype == "llm":
            servable, unservable = _llm_servable(m)
        out.append(ModelOut(
            name=m["name"],
            files=m["files"],
            bytes=m["bytes"],
            type=mtype,
            # Dimension is an embedding property; a language model has none.
            dimension=(ems.dimension_of(m["name"]) if mtype == "embedding" else None),
            is_sentence_transformer=manifest.get("is_sentence_transformer"),
            servable=servable,
            unservable_reason=unservable,
        ))
    return out


class UploadResult(BaseModel):
    name: str
    files: int
    bytes: int
    dimension: int | None = None
    is_sentence_transformer: bool = False
    # Non-fatal advisories the UI surfaces (e.g. dimension mismatch vs the
    # active RAG/wizard stores). Present == "it uploaded, but read this".
    warnings: list[str] = []


def _probe_dimension(model_dir: str):
    """Load the model and encode a probe string to confirm it actually embeds
    and to get the TRUE output width. Best-effort — heavy models / missing
    torch just fall back to the config-derived dimension."""
    try:
        from sentence_transformers import SentenceTransformer
        vec = SentenceTransformer(model_dir).encode("probe")
        return int(len(vec))
    except Exception as e:  # noqa: BLE001
        logger.info("embedding probe skipped (%s) — using config dimension", e)
        return None


@_impl.post("", response_model=UploadResult)
def upload_embedding_model(
    name: str = Form(...),
    archive: UploadFile = File(...),
    # §4.1 — the type is DECLARED, and required. It is not detected: a wrong
    # guess means an embedding model offered where a language model is wanted,
    # which fails on a worker at serve time, far from the upload that caused it.
    # Required rather than defaulted, because a silent default is how everything
    # ends up filed as whatever the store used to hold.
    model_type: str = Form(...),
    overwrite: bool = Form(False),
    probe: bool = Form(True),
    # FX.148 p4a — the provenance CLAIM. Declared, not verified: the record
    # states where the uploader said the model came from (repo URL + commit/
    # tag), so the inventory can answer "which build of which repo is this"
    # and so FX.148 p4b (cosign) has something to attest against. Optional:
    # an upload with no claim still gets a provenance row (hashes + actor +
    # verdict), it just records the claim as absent.
    source_url: str | None = Form(None),
    source_revision: str | None = Form(None),
    current_user: models.User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Upload a sentence-transformers model directory (.tar.gz) → MinIO under
    `name`. Gated: safe extraction, must carry config + weights, dimension is
    detected (and probe-verified), name-collision is blocked unless overwrite,
    and a dimension mismatch vs the active RAG/wizard stores is surfaced.
    Loaders then resolve it MinIO-first; HuggingFace is never touched."""
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name):
        raise HTTPException(400, "name must be non-empty [A-Za-z0-9._-]")

    mtype = (model_type or "").strip().lower()
    if mtype not in ems.MODEL_TYPES:
        raise HTTPException(
            400, f"model_type must be one of {list(ems.MODEL_TYPES)} — an upload "
                 f"has to say what kind of model it is, because validation and "
                 f"storage differ by kind")

    # Collision guard — never silently clobber an in-use model. Scoped to the
    # TYPE: an embedding and a language model may share a name without either
    # shadowing the other.
    if not overwrite and ems.model_exists(name, mtype):
        raise HTTPException(
            409, f"a model named '{name}' already exists; re-upload with overwrite=true to replace it")

    tmp = tempfile.mkdtemp(prefix="embmodel_")
    try:
        model_dir = os.path.join(tmp, "m")
        os.makedirs(model_dir, exist_ok=True)
        try:
            with tarfile.open(fileobj=archive.file, mode="r:*") as tar:
                _safe_extract(tar, model_dir)
        except tarfile.TarError as e:
            raise HTTPException(400, f"not a valid tar archive: {e}")

        # A sentence-transformers model may be at the root or one level down.
        root = model_dir
        if not ems.has_config(root):
            subs = [d for d in os.listdir(model_dir) if os.path.isdir(os.path.join(model_dir, d))]
            if len(subs) == 1 and ems.has_config(os.path.join(model_dir, subs[0])):
                root = os.path.join(model_dir, subs[0])

        # --- Structural gates, per declared type ------------------------------
        ok, detail = ems.validate_for_type(root, mtype)
        if not ok:
            raise HTTPException(
                400, f"this does not look like a {mtype} model: {detail}")
        report = ems.inspect_model_dir(root)

        # --- Dimension: config-derived, then probe-verified -------------------
        # Embedding-only. A language model has no embedding width to check, and
        # probing one would load a chat model to ask it a question it has no
        # answer to.
        dim = report["dimension"] if mtype == "embedding" else None
        warnings: list[str] = []
        if mtype == "llm":
            warnings.append(
                "Language model stored. It is served from object storage the "
                "same way embeddings are; nothing here checks that a worker can "
                "host it — the model list reports that separately.")
        if probe and mtype == "embedding":
            probed = _probe_dimension(root)
            if probed:
                if dim and probed != dim:
                    warnings.append(
                        f"declared dimension {dim} but the model actually emits {probed}-d "
                        f"vectors; using the probed value")
                dim = probed
        if dim is None and mtype == "embedding":
            warnings.append(
                "could not determine the embedding dimension — vector-store "
                "compatibility can't be checked automatically")
        if mtype == "embedding" and not report["is_sentence_transformer"]:
            warnings.append(
                "no modules.json / ST config — this looks like a raw encoder, not a "
                "packaged sentence-transformers model; it may still embed but pooling "
                "behaviour is not guaranteed")

        # --- Compatibility gate vs the active stores --------------------------
        if dim is not None and mtype == "embedding":
            for consumer, cdim in _ACTIVE_CONSUMER_DIMS.items():
                if dim != cdim:
                    warnings.append(
                        f"dimension {dim} ≠ the active {consumer} store ({cdim}-d): pinning "
                        f"this model to {consumer} requires re-embedding that corpus "
                        f"(dimension migration) or retrieval will break")

        # --- FX.90 p2a (Q76=A): scan the staged artefact BEFORE it is
        # registered (mirror_dir writes MinIO; the audit write commits, so
        # the scan must precede every store). Infected -> 422 and the temp
        # extraction dir is deleted by the finally below; nothing is
        # mirrored. Scanner down follows the upload path's fail mode
        # (fail_closed default -> 503). Disabled setting -> no scan call.
        try:
            files_service.gate_artefact_malware_scan(
                db, dir_root=root, source="model", artefact=name,
                user=current_user,
            )
        except files_service.MalwareInfectedError as e:
            raise HTTPException(
                status_code=422,
                detail=f"Artefact blocked: model '{name}' contains malware "
                       f"({getattr(e, 'message', 'signature unknown')})",
            )
        except files_service.MalwareScanUnavailableError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Artefact blocked: malware scanner unavailable "
                       f"({getattr(e, 'message', '')})",
            )

        # --- FX.148 p3a (Q135=C): pickle-import scan on the SAME staged
        # artefact, right after the ClamAV gate. ClamAV matches byte
        # signatures; this matches the semantic case it cannot — a
        # well-formed pickle whose callable is malicious (os.system,
        # builtins.eval, subprocess...). Nothing is unpickled here.
        # No-op while models.pickle_scan_enabled is False; default-on is
        # safe because a clean model (safetensors / weights-only) is
        # unaffected. Unsafe -> 422 and the temp dir is removed by the
        # finally below, so nothing is mirrored/registered.
        try:
            pickle_import_scan.gate_artefact_pickle_scan(
                db, dir_root=root, source="model", artefact=name,
                user=current_user,
            )
        except pickle_import_scan.PickleUnsafeError as e:
            raise HTTPException(
                status_code=422,
                detail=f"Model blocked: '{name}' carries an unsafe pickle "
                       f"({getattr(e, 'message', 'dangerous opcode')})",
            )
        except pickle_import_scan.PickleScanUnavailableError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Model blocked: pickle scan unavailable "
                       f"({getattr(e, 'message', '')})",
            )

        # --- FX.148 p3b (Q135=C): custom model code (trust_remote_code) ----
        # The third door on the same staged artefact. A repo can ask a loader
        # to import code that lives inside it — `config.json` `auto_map`
        # naming in-repo classes, an explicit `trust_remote_code: true`, or a
        # shipped script carrying `--trust-remote-code`. Neither ClamAV
        # (byte signatures) nor p3a's disassembler (pickle opcodes) sees it:
        # there is no pickle and no opcode, just a JSON key. Off by default;
        # lifted only by a per-model owner approval WITH a reason
        # (models.trust_remote_code_approvals). Nothing is imported here.
        # CE: model governance (remote-code trust gating, provenance hashing,
        # signing) is a paid/compliance feature, not shipped in CE — this is
        # a genuine CE capability gap, not a bug. Skip the whole gate.
        if model_trust_approval is not None and model_provenance is not None and model_signing is not None:
            try:
                model_trust_approval.gate_artefact_trust_remote_code(
                    db, dir_root=root, source="model", artefact=name,
                    user=current_user,
                )
            except model_trust_approval.TrustRemoteCodeBlockedError as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Model blocked: '{name}' requests custom model code "
                           f"({getattr(e, 'message', 'auto_map / trust_remote_code')})",
                )
            except model_trust_approval.TrustCodeCheckUnavailableError as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"Model blocked: custom-code check unavailable "
                           f"({getattr(e, 'message', '')})",
                )

        # --- Persist (manifest travels with the model) ------------------------
        ems.write_manifest(root, {
            "name": name,
            # The type travels WITH the model, so anything reading a manifest
            # later knows what it is holding without inferring from the prefix.
            "type": mtype,
            "dimension": dim,
            "is_sentence_transformer": report["is_sentence_transformer"],
            "has_weights": report["has_weights"],
        })
        ems.mirror_dir(name, root, mtype)

        # --- FX.148 p4b: sign the manifest digest, THEN record provenance ---
        # Order is the point of this block. The artefact is hashed ONCE
        # through the recorder's own walk (services/model_provenance.hash_staged_dir
        # — the same function record_import uses, so the digests the signer
        # attests are byte-for-byte the digests the row records, and the
        # staged files are read from disk once, not twice). The manifest
        # digest of those digests is cosigned, and only then is the
        # provenance row written: with models.sign_on_import_fail_closed=True
        # a signing failure refuses the upload BEFORE any row exists, so the
        # inventory never carries provenance for an artefact the platform
        # refused. With fail_open the import proceeds unsigned (logged).
        #
        # The outcome is recorded as an append-only audit event
        # (audit.log_security_event), never as an update to a provenance row
        # — provenance rows are append-only and are never rewritten.
        prov_hashes = None
        prov_truncated = False
        # CE: model governance (signing + provenance recording) is a paid/
        # compliance feature, not shipped in CE — skip this entire block.
        # The model import still completes, just unsigned/unverified.
        if model_trust_approval is not None and model_provenance is not None and model_signing is not None:
            try:
                if model_signing.signing_enabled(db=db):
                    cfg = model_provenance._get_config(db=db)
                    prov_hashes, prov_truncated = model_provenance.hash_staged_dir(
                        root,
                        max_files=int(cfg[model_provenance.SETTING_MAX_HASH_FILES]),
                        max_bytes=int(cfg[model_provenance.SETTING_MAX_FILE_BYTES]))
                    manifest = model_signing.manifest_digest(prov_hashes)
                    sign_result = model_signing.sign_model_digest(
                        db, sha256_hex=manifest, model_name=name)
                    audit.log_security_event(
                        db,
                        action=models.AuditAction.CONTENT_FINGERPRINTED,
                        user_id=getattr(current_user, "id", None),
                        tenant_id=getattr(current_user, "tenant_id", None),
                        metadata={
                            "detector": "model_signing",
                            "model_name": name,
                            "model_type": mtype,
                            "manifest_digest": manifest,
                            "signed": bool(sign_result.get("signed")),
                            "reason": sign_result.get("reason"),
                            "file_count": len(prov_hashes),
                        },
                    )
            except RuntimeError as e:  # sign_model_digest raised = fail_closed
                raise http_errors.internal_http_error(
                    500, "Model import refused: model signing failed", e,
                    log=logger,
                ) from e
            except ValueError as e:  # manifest digest failure, same gate
                if model_signing.fail_closed(db=db):
                    raise http_errors.internal_http_error(
                        500, "Model import refused: manifest digest failed", e,
                        log=logger,
                    )
                logger.warning("FX.148 p4b: manifest digest failed for '%s' (%s); "
                               "import continues unsigned", name, e)
            except Exception as e:  # noqa: BLE001 — settings/hash walk failure
                logger.warning("FX.148 p4b: signing step failed for '%s' (%s); "
                               "import continues unsigned", name, e)

            # --- FX.148 p4a: provenance row (append-only, one per import) ----
            # Written AFTER the signing block above, never before: the gates (FX.90
            # malware, FX.148 p3a pickle, p3b custom code) all ran above, so a
            # row only ever exists for an artefact that passed them, and it
            # carries the verdict that gated it. A re-import (overwrite) writes a
            # NEW row — the prior provenance stays readable (ISO 27001: records
            # are never rewritten or destroyed). Recording failure never fails
            # the upload (record_import logs and returns None).
            try:
                model_provenance.record_import(
                    db,
                    model_name=name,
                    model_type=mtype,
                    dir_root=root,
                    source_url=(source_url or None),
                    source_revision=(source_revision or None),
                    user=current_user,
                    import_kind="upload",
                    file_hashes=prov_hashes,
                    hash_truncated=prov_truncated,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("FX.148 p4a: provenance record failed for '%s' (%s)",
                               name, e)

        files = [f for _r, _d, fs in os.walk(root) for f in fs]
        size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(root) for f in fs)
        logger.info("%s model '%s' uploaded to MinIO (%d files, dim=%s, warnings=%d)",
                    mtype, name, len(files), dim, len(warnings))
        return UploadResult(
            name=name, files=len(files), bytes=size, dimension=dim,
            is_sentence_transformer=bool(report["is_sentence_transformer"]),
            warnings=warnings,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ProvenanceOut(BaseModel):
    """One provenance row (newest first). READ-ONLY surface — FX.148 p4a."""
    id: int
    model_name: str
    model_type: str
    source_url: str | None = None
    source_revision: str | None = None
    file_sha256: dict | None = None
    file_count: int = 0
    total_bytes: int = 0
    scan_status: str | None = None
    scan_result: dict | None = None
    imported_by_user_id: int | None = None
    imported_by_tenant_id: int | None = None
    import_kind: str = "upload"
    created_at: str | None = None


@_impl.get("/provenance/{name}", response_model=list[ProvenanceOut])
def get_model_provenance(
    name: str,
    model_type: str | None = None,
    limit: int | None = None,
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """FX.148 p4a — provenance history of one stored model, newest first.

    The inventory's read-only provenance surface: source claim, revision,
    per-file sha256s, the importing actor, the timestamp and the pickle-scan
    verdict that gated the import. Append-only — there is no write/update
    route here, and a re-import shows as an additional row, never a
    replacement.
    """
    if model_provenance is None:
        # CE: provenance rows are never written (paid/compliance feature) —
        # nothing to list. Fail safe: no data, not an error.
        return []
    rows = model_provenance.list_for(db, name, model_type=model_type,
                                     limit=limit)
    return [ProvenanceOut(**{**r,
                             "created_at": (r["created_at"].isoformat()
                                            if r["created_at"] else None)})
            for r in rows]


router.include_router(_impl, prefix="/embedding-models")
