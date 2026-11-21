"""Tenant assistant knowledge-base ingestion (tenant-assistant-knowledge-base).

A tenant admin adds company knowledge from an upload, pasted text, or an
existing storage folder. Each source becomes a `TenantKnowledgeDocument`; this
module turns its bytes into text, chunks the text, embeds every chunk with the
platform-standard local model (`utils.embeddings`), and upserts the chunks into
the tenant's own `t{tenant_id}_vectors` collection via `utils.user_vector_store`.

Isolation is by construction: `tenant_id` is read off the persisted document row
(which the endpoint set from the authenticated principal, never the request
body), and the collection name is derived from it inside `user_vector_store`.
KB chunks share the collection with workflow-node vectors, so every point carries
payload `{source:"kb", kb_document_id, title, chunk_index, text}` (design D2) —
KB queries filter `source=="kb"` and delete/reindex filter `kb_document_id`.

Ingestion runs off the request path via FastAPI `BackgroundTasks` (design D4).
`ingest_document` is written as a plain callable and NEVER raises out of the
background entrypoint: on any failure it removes partial chunks
(`delete_by_document`) and records `failed` + `error_message`. Because
`BackgroundTasks` are in-process and lost on an API restart, `startup_sweep`
flips stale `processing` rows back to `failed` so they can be reindexed.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Chunking target window (characters) + overlap, paragraph-aware (design D6).
CHUNK_TARGET_CHARS = int(os.getenv("TENANT_KB_CHUNK_CHARS", "1000"))
CHUNK_OVERLAP_CHARS = int(os.getenv("TENANT_KB_CHUNK_OVERLAP", "150"))

# A `processing` row older than this on API startup was orphaned by a restart
# (BackgroundTasks are in-process) — sweep it to `failed` so it can be reindexed.
STALE_PROCESSING_MINUTES = int(os.getenv("TENANT_KB_STALE_PROCESSING_MIN", "30"))

# Extensions we can extract text from. Kept lowercase, no leading dot.
_TEXT_EXTS = {"txt", "md", "markdown", "text"}
_CSV_EXTS = {"csv", "tsv"}
_PDF_EXTS = {"pdf"}
_DOCX_EXTS = {"docx"}
SUPPORTED_EXTENSIONS = _TEXT_EXTS | _CSV_EXTS | _PDF_EXTS | _DOCX_EXTS


class KBIngestError(ValueError):
    """Unsupported or undecodable input — surfaced to the admin as the
    document's `error_message`."""


# =============================================================================
# Type detection + text extraction (design D5)
# =============================================================================

def _ext_of(filename: str) -> str:
    name = (filename or "").strip().lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1]


def is_supported(filename: str) -> bool:
    """True if we have an extractor for this filename's extension."""
    return _ext_of(filename) in SUPPORTED_EXTENSIONS


def _decode_text(data: bytes) -> str:
    """Best-effort decode of a text/markdown blob."""
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise KBIngestError("could not decode file as text (unknown encoding)")


def _extract_csv(data: bytes) -> str:
    """Flatten CSV/TSV rows into lines of ` | `-joined cells."""
    text = _decode_text(data)
    delimiter = "\t" if "\t" in text.split("\n", 1)[0] else ","
    lines: List[str] = []
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for row in reader:
            cells = [c.strip() for c in row if c and c.strip()]
            if cells:
                lines.append(" | ".join(cells))
    except csv.Error as e:
        raise KBIngestError(f"could not parse CSV: {e}")
    return "\n".join(lines)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as e:  # pragma: no cover - import guard
        raise KBIngestError(f"PDF support unavailable (pypdf not installed): {e}")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise KBIngestError(f"could not read PDF (corrupt or encrypted): {e}")
    if getattr(reader, "is_encrypted", False):
        # An empty-password decrypt sometimes works; otherwise fail clearly.
        try:
            reader.decrypt("")
        except Exception:
            raise KBIngestError("PDF is encrypted and cannot be read")
    parts: List[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt.strip():
            parts.append(txt)
    text = "\n\n".join(parts).strip()
    if not text:
        raise KBIngestError(
            "no extractable text in PDF (scanned/image-only PDFs are not supported)")
    return text


def _extract_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except Exception as e:  # pragma: no cover - import guard
        raise KBIngestError(f"DOCX support unavailable (python-docx not installed): {e}")
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise KBIngestError(f"could not read DOCX (corrupt or wrong format): {e}")
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise KBIngestError("no extractable text in DOCX")
    return text


def extract_text(filename: str, data_bytes: bytes) -> str:
    """Extract plain text from a source blob, dispatching on the filename's
    extension. Raises `KBIngestError` on an unsupported type or undecodable
    content. `data_bytes` is the raw file content."""
    ext = _ext_of(filename)
    if ext in _TEXT_EXTS:
        text = _decode_text(data_bytes)
    elif ext in _CSV_EXTS:
        text = _extract_csv(data_bytes)
    elif ext in _PDF_EXTS:
        text = _extract_pdf(data_bytes)
    elif ext in _DOCX_EXTS:
        text = _extract_docx(data_bytes)
    else:
        raise KBIngestError(f"unsupported file type: .{ext or '(none)'}")
    if not text or not text.strip():
        raise KBIngestError("document contained no extractable text")
    return text


# =============================================================================
# Chunking (design D6) — paragraph-aware, target window with overlap.
# =============================================================================

def _overlap_tail(chunk: str, overlap: int) -> str:
    """Last `overlap` chars of `chunk`, trimmed to a word boundary so an
    overlapping chunk doesn't start mid-word."""
    if overlap <= 0 or len(chunk) <= overlap:
        return chunk
    tail = chunk[-overlap:]
    sp = tail.find(" ")
    if 0 < sp < len(tail) - 1:
        tail = tail[sp + 1:]
    return tail


def _window_split(text: str, target: int, overlap: int) -> List[str]:
    """Sliding word-window split for a block larger than `target` chars."""
    words = text.split()
    if not words:
        return []
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for w in words:
        add = len(w) + (1 if cur else 0)
        if cur and cur_len + add > target:
            chunk = " ".join(cur)
            chunks.append(chunk)
            tail = _overlap_tail(chunk, overlap)
            cur = tail.split() if tail else []
            cur_len = len(" ".join(cur))
        cur.append(w)
        cur_len += len(w) + (1 if cur[:-1] else 0)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def chunk_text(
    text: str, *, target: int = CHUNK_TARGET_CHARS, overlap: int = CHUNK_OVERLAP_CHARS,
) -> List[str]:
    """Split extracted text into overlapping chunks around `target` chars,
    breaking on paragraph boundaries where possible. Each returned chunk keeps
    its raw text (stored verbatim in the point payload)."""
    text = (text or "").strip()
    if not text:
        return []
    import re
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: List[str] = []
    cur = ""
    for para in paragraphs:
        if len(para) > target:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_window_split(para, target, overlap))
            continue
        candidate = f"{cur}\n\n{para}" if cur else para
        if cur and len(candidate) > target:
            chunks.append(cur)
            tail = _overlap_tail(cur, overlap)
            cur = f"{tail}\n\n{para}" if tail else para
        else:
            cur = candidate
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c and c.strip()]


# =============================================================================
# Source-byte resolution
# =============================================================================

def _read_file_bytes(db, tenant_id: int, file_id: str) -> bytes:
    """Read a stored File's raw bytes from the tenant's object storage.
    `tenant_id` is authoritative (from the doc row); the File must belong to it."""
    import models
    from utils.minio_client import MinioClient

    file_row = (
        db.query(models.File)
        .filter(
            models.File.id == file_id,
            models.File.tenant_id == tenant_id,
            models.File.deleted_at.is_(None),
        )
        .first()
    )
    if file_row is None:
        raise KBIngestError("source file not found for this tenant")
    storage_path = file_row.storage_path or ""
    if not storage_path:
        raise KBIngestError("source file has no storage path")

    if "://" in storage_path:
        after_scheme = storage_path.split("://", 1)[1]
        object_path = after_scheme.split("/", 1)[1] if "/" in after_scheme else after_scheme
    else:
        object_path = storage_path

    client = MinioClient(tenant_id)
    response = None
    try:
        response = client.client.get_object(client.bucket_name, object_path)
        return response.read()
    except Exception as e:
        raise KBIngestError(f"could not read source file from storage: {e}")
    finally:
        try:
            if response is not None:
                response.close()
                response.release_conn()
        except Exception:
            pass


def _resolve_source(db, doc) -> tuple[str, str]:
    """Resolve a document's (filename, text). `filename` drives type detection
    for upload/folder; paste is decoded as plain text regardless of title."""
    import models

    source_type = doc.source_type
    if source_type == "paste":
        if not doc.file_id:
            raise KBIngestError("pasted-text document has no stored content")
        data = _read_file_bytes(db, doc.tenant_id, doc.file_id)
        return (doc.title or "pasted-text", _decode_text(data))

    # upload / folder — both point at a stored File blob.
    if not doc.file_id:
        raise KBIngestError(f"{source_type} document has no source file")
    file_row = (
        db.query(models.File)
        .filter(models.File.id == doc.file_id, models.File.tenant_id == doc.tenant_id)
        .first()
    )
    filename = (file_row.name if file_row and file_row.name else doc.title) or "document"
    if not is_supported(filename):
        raise KBIngestError(f"unsupported file type: {filename}")
    data = _read_file_bytes(db, doc.tenant_id, doc.file_id)
    return (filename, extract_text(filename, data))


# =============================================================================
# Ingest entrypoint (design D4) — never raises out of the background task.
# =============================================================================

def _set_status(db, doc, status: str, *, chunk_count: Optional[int] = None,
                error_message: Optional[str] = None) -> None:
    doc.status = status
    if chunk_count is not None:
        doc.chunk_count = chunk_count
    doc.error_message = error_message
    db.add(doc)
    db.commit()


def ingest_document(db, doc_id: int) -> None:
    """Background entrypoint: load the document, extract → chunk → embed →
    upsert into `t{tenant_id}_vectors`, and mark `ready` (+ chunk_count). On ANY
    failure, remove partial chunks and mark `failed` (+ error_message). Never
    raises — it is dispatched via `BackgroundTasks`.

    Call as `background_tasks.add_task(ingest_document, db, doc_id)`."""
    import models
    from utils import embeddings, user_vector_store

    doc = db.query(models.TenantKnowledgeDocument).filter(
        models.TenantKnowledgeDocument.id == doc_id).first()
    if doc is None:
        logger.warning("tenant KB ingest: document %s not found", doc_id)
        return

    tenant_id = doc.tenant_id
    try:
        _set_status(db, doc, "processing", error_message=None)

        filename, text = _resolve_source(db, doc)
        chunks = chunk_text(text)
        if not chunks:
            raise KBIngestError("document produced no chunks after extraction")

        vectors = embeddings.embed_texts(chunks)
        if len(vectors) != len(chunks):
            raise KBIngestError("embedding count did not match chunk count")

        title = doc.title or filename
        points: List[Dict[str, Any]] = [
            {
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "kb",
                    "kb_document_id": str(doc_id),
                    "title": title,
                    "chunk_index": idx,
                    "text": chunk,
                },
            }
            for idx, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]

        # Reindex-safe: clear any prior chunks for this document before writing.
        user_vector_store.delete_by_document(tenant_id, str(doc_id))
        user_vector_store.upsert(tenant_id, points, dim=embeddings.current_dim())

        _set_status(db, doc, "ready", chunk_count=len(points), error_message=None)
        logger.info("tenant KB ingest: document %s ready (%d chunks)", doc_id, len(points))
    except Exception as e:  # noqa: BLE001 — background task must never raise
        logger.warning("tenant KB ingest: document %s failed: %s", doc_id, e)
        try:
            user_vector_store.delete_by_document(tenant_id, str(doc_id))
        except Exception as cleanup_err:  # noqa: BLE001
            logger.error("tenant KB ingest: partial-chunk cleanup failed for %s: %s",
                         doc_id, cleanup_err)
        try:
            db.rollback()
        except Exception:
            pass
        try:
            doc = db.query(models.TenantKnowledgeDocument).filter(
                models.TenantKnowledgeDocument.id == doc_id).first()
            if doc is not None:
                _set_status(db, doc, "failed", error_message=str(e)[:2000])
        except Exception as status_err:  # noqa: BLE001
            logger.error("tenant KB ingest: could not record failure for %s: %s",
                         doc_id, status_err)


# =============================================================================
# Folder enumeration (design/task 2.5) — used by the folder endpoint.
# =============================================================================

def enumerate_supported_files(db, tenant_id: int, cog_id: int) -> List[Dict[str, Any]]:
    """List the supported files directly under a folder Cog, scoped to the
    tenant. Returns `[{"file_id", "name", "size_bytes"}]` for files whose type
    we can extract; unsupported files are skipped. The caller creates one
    `TenantKnowledgeDocument` per returned file and schedules `ingest_document`.

    Fail-closed on tenant scope: the folder Cog must belong to `tenant_id`, and
    every returned file is re-checked against `tenant_id`."""
    import models

    folder = (
        db.query(models.Cog)
        .filter(
            models.Cog.id == cog_id,
            models.Cog.tenant_id == tenant_id,
            models.Cog.deleted_at.is_(None),
        )
        .first()
    )
    if folder is None:
        raise KBIngestError("folder not found for this tenant")

    rows = (
        db.query(models.File)
        .join(models.Cog, models.File.cog_id == models.Cog.id)
        .filter(
            models.Cog.parent_id == cog_id,
            models.File.tenant_id == tenant_id,
            models.File.deleted_at.is_(None),
            models.Cog.deleted_at.is_(None),
        )
        .all()
    )
    out: List[Dict[str, Any]] = []
    for f in rows:
        if is_supported(f.name or ""):
            out.append({
                "file_id": f.id,
                "name": f.name,
                "size_bytes": f.size_bytes,
            })
    return out


# =============================================================================
# Startup sweep (design D4 / Risks) — reap orphaned `processing` rows.
# =============================================================================

def startup_sweep(db, *, stale_minutes: int = STALE_PROCESSING_MINUTES) -> int:
    """Flip any `processing` document older than `stale_minutes` back to
    `failed` — BackgroundTasks are in-process and lost on an API restart, so a
    row still `processing` after a restart is orphaned. Returns the number
    swept. Safe to call on every startup (idempotent). Never raises."""
    import models

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    try:
        stale = (
            db.query(models.TenantKnowledgeDocument)
            .filter(models.TenantKnowledgeDocument.status == "processing")
            .all()
        )
        swept = 0
        for doc in stale:
            ts = doc.updated_at or doc.created_at
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts is None or ts <= cutoff:
                doc.status = "failed"
                doc.error_message = (
                    "ingestion interrupted by a service restart — reindex to retry")
                db.add(doc)
                swept += 1
        if swept:
            db.commit()
            logger.info("tenant KB startup sweep: reset %d stale processing rows", swept)
        return swept
    except Exception as e:  # noqa: BLE001
        logger.error("tenant KB startup sweep failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
