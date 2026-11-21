"""Seed one small MOD Assistant Knowledge-Base document so the owner can test
RAG retrieval in the UI.

The tenant KB retrieval mechanism (embed via all-MiniLM-L6-v2 → query the
tenant's `t{tenant_id}_vectors` collection filtered to `source=="kb"`) works,
but nothing is indexed out of the box, so a fresh workspace has no KB hit to
retrieve. This is the KB analogue of the QA users / QA node seeds: a
test-enabler, not product data.

What it does (all idempotent):
  1. Ensures an `AssistantTenantConfig` row for the target tenant with
     `enabled=True` and `knowledge_base_enabled=True` — so the KB tool is
     actually offered to the model and the KB surface shows in the UI.
  2. Ensures ONE `TenantKnowledgeDocument` (source_type "paste") titled
     `MOD Assistant Sample Knowledge (seed)`, persists its text as a
     tenant-scoped blob (so the UI reindex/delete lifecycle works), and runs
     the REAL ingest path (`utils.tenant_kb_ingester.ingest_document`) — the
     same chunk → embed → upsert the /assistant/knowledge/text endpoint uses.
  3. Verifies retrieval: embeds a probe query and confirms the seeded chunk
     comes back from the tenant collection with `source=="kb"`.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260907_seed_assistant_kb_test_doc.py --commit
    # optional: --tenant-id N   (default 1, the Platform tenant)

Dry run (default) reports what would change without writing anything.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from io import BytesIO


SEED_TITLE = "MOD Assistant Sample Knowledge (seed)"
SEED_FILENAME = "mod-sample-knowledge.txt"

# A small, self-contained MOD knowledge doc. Deliberately specific so a probe
# query has an unambiguous chunk to retrieve.
SEED_TEXT = """MOD Assistant — Sample Knowledge Base Document

This is a seeded sample document used to verify that MOD Assistant can
retrieve from a tenant's private Knowledge Base.

About MOD workflows:
A MOD workflow is a directed graph of nodes. Each node references a
containerized capability (for example an FFmpeg transcode, an image
upscale, or an LLM prompt) and has typed input and output ports. Workflows
are version-controlled, shareable, and can be scheduled to run
automatically.

About MOD nodes:
Nodes are the individual steps in a workflow. The supported way to author a
new node is the Node Wizard, reached from the Workflow Builder. A node runs
as a Docker container on an enrolled worker.

Retrieval test phrase:
The magic phrase for the knowledge-base retrieval test is
"blue narwhal seventeen". If MOD Assistant can quote this phrase back, RAG
retrieval from the company Knowledge Base is working end to end.
"""

PROBE_QUERY = "what is the magic phrase for the knowledge base retrieval test?"
PROBE_EXPECT = "blue narwhal seventeen"


def _bootstrap():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    import crud  # type: ignore
    import models  # type: ignore
    import schemas  # type: ignore
    return _db, crud, models, schemas


def _ensure_config(db, models, tenant_id: int, log, commit: bool):
    cfg = (
        db.query(models.AssistantTenantConfig)
        .filter(models.AssistantTenantConfig.tenant_id == tenant_id)
        .one_or_none()
    )
    if cfg is None:
        log.info("would create AssistantTenantConfig(tenant=%s, enabled=True, "
                 "knowledge_base_enabled=True)", tenant_id)
        if commit:
            cfg = models.AssistantTenantConfig(
                tenant_id=tenant_id, enabled=True, knowledge_base_enabled=True,
            )
            db.add(cfg)
            db.flush()
        return
    changed = False
    if not cfg.enabled:
        cfg.enabled = True
        changed = True
    if not getattr(cfg, "knowledge_base_enabled", False):
        cfg.knowledge_base_enabled = True
        changed = True
    if changed:
        log.info("would enable assistant + knowledge_base on existing config "
                 "(tenant=%s)", tenant_id)
        if commit:
            db.add(cfg)
            db.flush()
    else:
        log.info("AssistantTenantConfig already enabled for tenant %s", tenant_id)


def _persist_blob(db, crud, models, schemas, tenant_id: int, user_id: int,
                  content: bytes, log) -> str:
    """Content-addressed blob persist (mirrors routers.assistant_knowledge
    ._persist_blob_as_file, synchronous). Returns the new File id."""
    from utils.minio_client import MinioClient

    content_hash = hashlib.sha256(content).hexdigest()
    object_name = content_hash

    client = MinioClient(tenant_id)
    client.ensure_bucket_exists()
    client.client.put_object(
        bucket_name=client.bucket_name,
        object_name=object_name,
        data=BytesIO(content),
        length=len(content),
        content_type="text/plain",
    )
    storage_path = f"minio://{client.bucket_name}/{object_name}"

    db_file = crud.create_file_record(
        db,
        file_data=schemas.FileCreate(
            name=SEED_FILENAME, size_bytes=len(content),
            mime_type="text/plain", storage_path="", cog_id=None,
        ),
        tenant_id=tenant_id, user_id=user_id,
    )
    db_file.storage_path = storage_path

    existing_blob = (
        db.query(models.ContentBlob)
        .filter(models.ContentBlob.id == content_hash,
                models.ContentBlob.tenant_id == tenant_id)
        .first()
    )
    if existing_blob:
        existing_blob.storage_path = storage_path
        existing_blob.mime_type = "text/plain"
        existing_blob.file_extension = "txt"
    else:
        db.add(models.ContentBlob(
            id=content_hash, tenant_id=tenant_id, size_bytes=len(content),
            mime_type="text/plain", file_extension="txt",
            storage_path=storage_path, cog_id=None,
        ))
    db.flush()
    log.info("persisted seed blob -> file_id=%s (%s)", db_file.id, storage_path)
    return db_file.id


def _embed_dir_ready(log) -> bool:
    """FIX-F28 guard (cause A): the ingest path embeds with the platform model,
    which utils.embedding_model_store.ensure_model materialises under
    MOD_EMBED_MODEL_DIR_BASE (default /app/models/embeddings — the API
    container's /app). On a host where that base cannot be created (a hosttest
    running the tree OUTSIDE the container: /app exists but is not writable)
    makedirs raises PermissionError and the whole migration chain dies at this
    step. A seed step must never fail a fresh install: skip quietly (one log
    line, exit 0), like the CE "nothing to seed" path above. Never raises.
    When the base already exists (the model is baked into the image) nothing
    changes — the ingest runs exactly as before."""
    base = os.getenv("MOD_EMBED_MODEL_DIR_BASE", "/app/models/embeddings")
    if os.path.isdir(base):
        return True
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as e:
        log.info("embedding model dir %s is not writable here (%s: %s) — skipping "
                 "the KB seed ingest; nothing to seed, migration reports success.",
                 base, type(e).__name__, e)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="Apply changes. Without this flag it is a dry run.")
    parser.add_argument("--tenant-id", type=int, default=1,
                        help="Target tenant id (default 1, the Platform tenant).")
    args = parser.parse_args()

    # FX.96: secure logging (SecretRedactionFilter) instead of bare basicConfig.
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from utils.log_config import configure_logging
    configure_logging()
    log = logging.getLogger("seed_assistant_kb")

    _db, crud, models, schemas = _bootstrap()
    # 1788o CE carve: the KB ingester is absent from this edition. Here the
    # seed step is skipped and the migration still succeeds (exit 0, like the
    # "nothing to seed" path); the build below is unchanged.
    from utils import embeddings, user_vector_store
    try:
        from utils import tenant_kb_ingester
    except ImportError:
        log.info("tenant_kb_ingester is not available in CE — skipping the KB "
                 "seed step; nothing to seed, migration reports success.")
        return
    db = _db.SessionLocal()
    tenant_id = args.tenant_id
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
        if tenant is None:
            log.error("tenant %s not found — pass --tenant-id", tenant_id)
            sys.exit(2)

        user = (
            db.query(models.User)
            .filter(models.User.tenant_id == tenant_id)
            .order_by(models.User.id.asc())
            .first()
        )
        if user is None:
            # FIX-F28 guard (cause A): a fresh DB can reach this step before any
            # user exists for the tenant (the paid hosttest inserts the platform
            # tenant only, no users). A seed step must never fail a fresh
            # install: skip quietly (one log line, exit 0), like the CE
            # "nothing to seed" path above. Behaviour is unchanged when the
            # tenant has users.
            log.info("tenant %s has no users to attribute the seed doc to — "
                     "skipping the KB seed; nothing to seed, migration reports "
                     "success.", tenant_id)
            return

        if not _embed_dir_ready(log):
            return

        content = SEED_TEXT.encode("utf-8")
        doc = (
            db.query(models.TenantKnowledgeDocument)
            .filter(models.TenantKnowledgeDocument.tenant_id == tenant_id,
                    models.TenantKnowledgeDocument.title == SEED_TITLE)
            .one_or_none()
        )
        if doc is not None:
            log.info("seed doc already exists (id=%s, status=%s) — will re-ingest",
                     doc.id, doc.status)
        else:
            log.info("would create TenantKnowledgeDocument(title=%r, source=paste, "
                     "tenant=%s)", SEED_TITLE, tenant_id)

        if not args.commit:
            log.info("dry run — no changes written. Re-run with --commit.")
            return

        file_id = _persist_blob(db, crud, models, schemas, tenant_id, user.id,
                                content, log)

        if doc is None:
            doc = models.TenantKnowledgeDocument(
                tenant_id=tenant_id, source_type="paste", title=SEED_TITLE,
                file_id=file_id, status="pending", chunk_count=0,
                bytes=len(content), created_by_user_id=user.id,
            )
            db.add(doc)
        else:
            doc.source_type = "paste"
            doc.file_id = file_id
            doc.status = "pending"
            doc.bytes = len(content)
        db.commit()
        db.refresh(doc)

        # Real ingest path — chunk → embed (all-MiniLM-L6-v2) → upsert into
        # t{tenant}_vectors with payload source=="kb". Never raises.
        tenant_kb_ingester.ingest_document(db, doc.id)
        db.refresh(doc)
        log.info("ingest finished: doc=%s status=%s chunks=%s error=%s",
                 doc.id, doc.status, doc.chunk_count, doc.error_message)
        if doc.status != "ready":
            log.error("ingest did not reach 'ready' — aborting")
            sys.exit(1)

        # Verify retrieval.
        vec = embeddings.embed_query(PROBE_QUERY)
        hits = user_vector_store.query(tenant_id, vec, top_k=3, where={"source": "kb"})
        top = hits[0] if hits else None
        got = ((top or {}).get("payload") or {}).get("text", "") if top else ""
        ok = any(PROBE_EXPECT in ((h.get("payload") or {}).get("text") or "") for h in hits)
        log.info("retrieval probe: %d hit(s); top score=%.3f; expected-phrase-found=%s",
                 len(hits), (top.get("score") if top else 0.0) or 0.0, ok)
        if not ok:
            log.error("probe did NOT retrieve the seeded phrase %r; top text=%r",
                      PROBE_EXPECT, got[:200])
            sys.exit(1)
        log.info("OK — seeded KB doc is retrievable for tenant %s.", tenant_id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
