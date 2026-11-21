"""
Docs Ingestion Pipeline for RAG Support Chatbot.

Reads Docusaurus markdown files, splits into chunks, embeds with
sentence-transformers, and upserts into Qdrant vector database.
"""

import os
import re
import logging
import hashlib
from typing import List, Optional

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
# qdrant-hardening: pass the key VERBATIM (incl "" when unset). qdrant enables
# auth on an empty key too, so a dropped header → 401; "" matches an empty-key
# instance and the real value matches a provisioned one.
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")  # verbatim: qdrant empty-key auth rejects a dropped header
QDRANT_COLLECTION = "docs_embeddings"
EMBEDDING_DIM = 384
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

DOCS_PATHS = [
    os.getenv("DOCS_PATH", "/app/../docs-site/docs"),
    "/path/to/MOD_CORE/CORE/docs-site/docs",
]

_DEFAULT_DOCS_MODEL = "all-MiniLM-L6-v2"
_model = None
_model_name_loaded = None


def _stack_pin_docs():
    """(stack_id, served_model) if the MOD-docs embedding store is pinned to
    a MOD Stack, else None. GE.AIC.1-3."""
    try:
        from routers.ai_config import _read_tool_config
        raw = _read_tool_config("assistant") or {}
        if raw.get("embedding_provider_docs") == "stack":
            stack_id = raw.get("embedding_stack_id_docs")
            if stack_id:
                return stack_id, raw.get("embedding_model_docs") or None
    except Exception as e:  # noqa: BLE001
        logger.debug("docs embedding stack pin lookup failed: %s", e)
    return None


def _resolve_docs_stack_endpoint():
    """(base_url, model_name) if the docs store is pinned to a stack, else
    None. Raises RuntimeError — naming the stack — when one IS pinned but
    can't serve right now; no silent fallback to the local model."""
    pin = _stack_pin_docs()
    if pin is None:
        return None
    stack_id, served_model = pin
    from database import SessionLocal
    from services.ai_stack_provider import resolve_stack_endpoint, StackProviderError
    db = SessionLocal()
    try:
        return resolve_stack_endpoint(db, stack_id, served_model)
    except StackProviderError as e:
        raise RuntimeError(str(e))
    finally:
        db.close()


def resolve_docs_model() -> str:
    """The embedding model for the MOD docs store: the AI Assistant pin when its
    store sub-choice targets 'docs', else the default. Fail-safe."""
    try:
        from routers.ai_config import _read_tool_config
        pin = (_read_tool_config("assistant") or {}).get("embedding_model_docs")
        if pin and str(pin).strip():
            return str(pin).strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("docs embedding pin lookup failed (using default): %s", e)
    return _DEFAULT_DOCS_MODEL


def invalidate_docs_model() -> None:
    """Drop the cached model so the next ingest/search reloads the pinned one."""
    global _model, _model_name_loaded
    _model = None
    _model_name_loaded = None


def _get_embedding_model():
    global _model, _model_name_loaded
    want = resolve_docs_model()
    if _model is None or _model_name_loaded != want:
        try:
            from sentence_transformers import SentenceTransformer
            from utils.embedding_model_store import ensure_model
            # MinIO-first (HF only as a one-time seed) — keeps HF out of deploy.
            hf = want if "/" in want else f"sentence-transformers/{want}"
            # Offline/local-only at runtime: allow_hf=False + local_files_only
            # so a load can never block on a HuggingFace fetch or freshness
            # HEAD-check while a DB transaction is held (pool-exhaustion cause).
            path = ensure_model(hf_id=hf, local_dir=f"/app/models/embeddings/{want}", name=want, allow_hf=False)
            _model = SentenceTransformer(path, local_files_only=True)
            _model_name_loaded = want
            logger.info("docs: loaded embedding model %s", want)
        except Exception as e:  # noqa: BLE001
            logger.warning("docs embedding model unavailable (%s): %s", want, e)
            return None
    return _model


def docs_dim() -> int:
    """Width of the docs embedding model in effect (default fast-path, else load)."""
    try:
        stack_ep = _resolve_docs_stack_endpoint()
    except RuntimeError:
        stack_ep = None
    if stack_ep is not None:
        from services.ai_stack_provider import embed_via_stack
        try:
            vec = embed_via_stack(stack_ep[0], stack_ep[1], ["_"])
            return len(vec[0])
        except Exception as e:  # noqa: BLE001
            logger.warning("docs: stack embedding dim probe failed: %s", e)
    if resolve_docs_model() == _DEFAULT_DOCS_MODEL:
        return EMBEDDING_DIM
    m = _get_embedding_model()
    return int(m.get_sentence_embedding_dimension()) if m is not None else EMBEDDING_DIM


def _get_qdrant_client():
    from utils import internal_tls

    try:
        from qdrant_client import QdrantClient
        # GY.C6.s3 — build the hop through the internal-TLS seam so a flip of
        # gsec.transport.internal_tls_enforce actually enforces this hop:
        # `require` re-verifies against the platform CA bundle and refuses a
        # plain http:// URL. `off` (the default) returns no kwargs at all, so
        # the call below keeps today's shape byte-identically.
        hop_kwargs = internal_tls.internal_client_kwargs(url=QDRANT_URL)
        return QdrantClient(
            url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=10, **hop_kwargs
        )
    except internal_tls.InternalTLSConfigError:
        # Fail-closed: a `require` refusal is NOT a qdrant outage. The bare
        # handler below swallows everything and returns None, which would
        # silently disable docs RAG (fail-open) — so the refusal re-raises.
        raise
    except ImportError:
        logger.warning("qdrant-client not installed — docs ingestion disabled")
        return None
    except Exception as e:
        logger.warning("Could not connect to Qdrant: %s", e)
        return None


def _find_docs_dir() -> Optional[str]:
    for path in DOCS_PATHS:
        if os.path.isdir(path):
            return path
    return None


def _read_markdown_files(docs_dir: str) -> List[dict]:
    files = []
    for root, _dirs, filenames in os.walk(docs_dir):
        for fname in filenames:
            if fname.endswith(".md"):
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, docs_dir)
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                content = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
                files.append({
                    "path": rel_path,
                    "content": content.strip(),
                    "url": "/" + rel_path.replace(".md", "").replace("\\", "/"),
                })
    return files


_H2_SPLIT_RE = re.compile(r"^(##\s+)", re.MULTILINE)


def _split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Chunk a markdown doc by H2 headings first, then fall back to
    word-based sliding window for any section that exceeds chunk_size.

    Pre-fix the chunker walked the doc with a 500-word window and 50-
    word overlap. For long structured docs like the Glossary (one H2
    per term — "## Tenant", "## Node", "## Worker", etc.), the first
    chunk swallowed the doc preamble PLUS the first half-dozen
    definitions; the snippet returned to the LLM was the preamble
    only, so the model couldn't answer "what is a tenant" from
    context and punted to a (broken) docs URL instead.

    H2-aware chunking keeps each term's definition as its own embedded
    record. Glossary "## Tenant" → its own ~200-word chunk that
    actually matches a "what is a tenant" query semantically AND fits
    inside a snippet cap small enough for the LLM to read.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    # Try H2 split first.
    sections: List[str] = []
    matches = list(_H2_SPLIT_RE.finditer(text))
    if matches:
        # First "section" is everything before the first H2 (frontmatter,
        # H1 title, intro paragraph).
        first_h2 = matches[0].start()
        if first_h2 > 0:
            preamble = text[:first_h2].strip()
            if preamble:
                sections.append(preamble)
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[start:end].strip()
            if section:
                sections.append(section)
    else:
        sections = [text]

    # Any section that's still bigger than chunk_size words gets the
    # sliding-window fallback so an over-long section doesn't blow
    # past the embedding model's input cap.
    chunks: List[str] = []
    for section in sections:
        section_words = section.split()
        if len(section_words) <= chunk_size:
            chunks.append(section)
            continue
        start = 0
        while start < len(section_words):
            end = start + chunk_size
            chunks.append(" ".join(section_words[start:end]))
            if end >= len(section_words):
                break
            start = end - overlap
    return chunks


def _extract_title(content: str, path: str) -> str:
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return path.replace(".md", "").replace("-", " ").title()


def ingest_docs() -> dict:
    """
    Main ingestion function. Reads docs, chunks, embeds, upserts to Qdrant.

    Returns:
        dict with ingestion stats
    """
    docs_dir = _find_docs_dir()
    if not docs_dir:
        return {"error": "Docs directory not found", "paths_checked": DOCS_PATHS}

    try:
        stack_ep = _resolve_docs_stack_endpoint()
    except RuntimeError as e:
        return {"error": f"Stack embedding provider unavailable: {e}"}
    model = None
    if stack_ep is None:
        model = _get_embedding_model()
        if model is None:
            return {"error": "Embedding model not available"}

    client = _get_qdrant_client()
    if client is None:
        return {"error": "Qdrant not available"}

    from qdrant_client.models import Distance, VectorParams, PointStruct

    # Ensure the collection exists AT THE CURRENT MODEL'S DIM. If a pinned model
    # changed the width, drop + recreate so we never upsert mismatched vectors.
    dim = docs_dim()
    try:
        info = client.get_collection(QDRANT_COLLECTION)
        if int(info.config.params.vectors.size) != int(dim):
            logger.info("docs: collection dim %s → %s, recreating %s",
                        info.config.params.vectors.size, dim, QDRANT_COLLECTION)
            client.delete_collection(QDRANT_COLLECTION)
            raise Exception("recreate")
    except Exception:
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=int(dim), distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection: %s (dim=%s)", QDRANT_COLLECTION, dim)

    files = _read_markdown_files(docs_dir)
    total_chunks = 0
    points = []

    for doc in files:
        title = _extract_title(doc["content"], doc["path"])
        chunks = _split_into_chunks(doc["content"])

        for i, chunk in enumerate(chunks):
            chunk_id = hashlib.md5(f"{doc['path']}:{i}".encode(), usedforsecurity=False).hexdigest()
            if stack_ep is not None:
                from services.ai_stack_provider import embed_via_stack
                embedding = embed_via_stack(stack_ep[0], stack_ep[1], [chunk])[0]
            else:
                embedding = model.encode(chunk).tolist()

            points.append(PointStruct(
                id=chunk_id,
                vector=embedding,
                payload={
                    "text": chunk,
                    "source_file": doc["path"],
                    "title": title,
                    "page_url": doc["url"],
                    "chunk_index": i,
                },
            ))
            total_chunks += 1

    if points:
        batch_size = 100
        for i in range(0, len(points), batch_size):
            client.upsert(
                collection_name=QDRANT_COLLECTION,
                points=points[i:i + batch_size],
            )

    logger.info("Ingested %d chunks from %d files into Qdrant", total_chunks, len(files))

    return {
        "success": True,
        "files_processed": len(files),
        "chunks_created": total_chunks,
        "collection": QDRANT_COLLECTION,
    }


def search_docs(query: str, top_k: int = 5) -> List[dict]:
    """
    Search the docs collection for relevant chunks.

    Args:
        query: User's question
        top_k: Number of results to return

    Returns:
        List of matching chunks with metadata
    """
    try:
        stack_ep = _resolve_docs_stack_endpoint()
    except RuntimeError as e:
        logger.warning("docs search: stack embedding provider unavailable: %s", e)
        return []
    model = None
    if stack_ep is None:
        model = _get_embedding_model()
        if model is None:
            return []

    client = _get_qdrant_client()
    if client is None:
        return []

    if stack_ep is not None:
        from services.ai_stack_provider import embed_via_stack
        query_vector = embed_via_stack(stack_ep[0], stack_ep[1], [query])[0]
    else:
        query_vector = model.encode(query).tolist()

    try:
        results = client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
        )
    except Exception as e:
        logger.warning("Qdrant search failed: %s", e)
        return []

    return [
        {
            "text": hit.payload.get("text", ""),
            "title": hit.payload.get("title", ""),
            "page_url": hit.payload.get("page_url", ""),
            "source_file": hit.payload.get("source_file", ""),
            "score": hit.score,
        }
        for hit in results
    ]


def migrate_docs_embeddings() -> dict:
    """Re-embed the MOD docs store to the currently-pinned Assistant→docs model.
    Source is the .md files on disk, so this is a clean re-ingest: reload the
    model, then ingest_docs() recreates the collection at the new dim and
    re-embeds every chunk. Zero data-loss risk."""
    invalidate_docs_model()
    result = ingest_docs()
    result["model"] = resolve_docs_model()
    result["dim"] = docs_dim()
    result["status"] = "ok" if result.get("success") else "error"
    return result
