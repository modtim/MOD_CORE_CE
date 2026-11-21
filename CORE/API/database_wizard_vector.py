"""wizard-learning-from-feedback §1.4 — connection helper for the
dedicated `my_wizard_vector_db` Postgres+pgvector instance.

Why a separate module: this is a NEW database, isolated from the
main app DB. It uses static credentials (env-var driven) instead of
Vault dynamic rotation because (a) Vault isn't yet wired to mint
roles for this instance and (b) DDL operations like CREATE TABLE
on per-tenant embedding tables need a stable owner across calls
(see §1.3 for the same lesson on the main DB).

Config (env vars on the API container):
- WIZARD_VECTOR_DB_HOST    (default: postgres-wizard-vector)
- WIZARD_VECTOR_DB_PORT    (default: 5432)
- WIZARD_VECTOR_DB_USER    (default: modtech_wizard)
- WIZARD_VECTOR_DB_PASSWORD (default: falls back to POSTGRES_PASSWORD)
- WIZARD_VECTOR_DB_NAME    (default: modtech_wizard_vector)

Public surface:
- get_engine() -> SQLAlchemy engine, lazily initialised
- get_session() -> Session bound to that engine
- ensure_tenant_embedding_table(tenant_id) -> idempotent CREATE
  TABLE IF NOT EXISTS for `tenant_<id>_proposal_embeddings`
- table_name_for_tenant(tenant_id) -> str (also used by retrieval
  + ingestion code so we have one source of truth for naming)

The embedding row schema is documented in design.md D1 and the
wizard-rag-retrieval spec; this module is the canonical
DDL implementation.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


_engine_lock = threading.Lock()
_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def _build_url() -> str:
    host = os.environ.get("WIZARD_VECTOR_DB_HOST", "postgres-wizard-vector")
    port = os.environ.get("WIZARD_VECTOR_DB_PORT", "5432")
    user = os.environ.get("WIZARD_VECTOR_DB_USER", "modtech_wizard")
    password = (
        os.environ.get("WIZARD_VECTOR_DB_PASSWORD")
        or os.environ.get("POSTGRES_PASSWORD")
        or ""
    )
    db = os.environ.get("WIZARD_VECTOR_DB_NAME", "modtech_wizard_vector")
    if not password:
        raise RuntimeError(
            "WIZARD_VECTOR_DB_PASSWORD (or POSTGRES_PASSWORD fallback) "
            "is required to connect to the wizard vector DB."
        )
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        url = _build_url()
        # GSEC R-057 (inert): same bootstrap TLS knobs as the main engine.
        from database import _pg_ssl_connect_args
        _engine = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=300,
            future=True,
            connect_args=_pg_ssl_connect_args(),
        )
        _session_factory = sessionmaker(bind=_engine, autoflush=False, future=True)
    return _engine


def get_session() -> Session:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory()


_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def table_name_for_tenant(tenant_id: object) -> str:
    """Resolve the per-tenant embedding table name. Validates the
    tenant id shape so we never interpolate user-controlled garbage
    into a CREATE TABLE statement."""
    s = str(tenant_id)
    if not _TENANT_ID_RE.match(s):
        raise ValueError(
            f"tenant_id {s!r} contains characters outside [A-Za-z0-9_]; "
            "refusing to use it as a Postgres identifier."
        )
    return f"tenant_{s}_proposal_embeddings"


_EMBEDDING_DIM = 1024  # default (Arctic); actual width resolved via _current_dim()
_SHARED_TABLE_NAME = "platform_proposal_embeddings_shared"


def _current_dim() -> int:
    """The width of the wizard embedding model in effect (honors the Node Wizard
    pin). Lazy import avoids a circular dependency; falls back to the default
    when the Pro sidecar is absent."""
    try:
        from services.wizard_pro_client import embedding_dim
        return int(embedding_dim())
    except Exception:  # noqa: BLE001
        return _EMBEDDING_DIM


def _table_dim(conn, name: str) -> int | None:
    """Return the vector(N) dim of the embedding column on `name`,
    or None if the table doesn't exist. Lets the helper detect dim
    drift (e.g., old 384-dim tables left over from arctic-s
    prototyping) and rebuild instead of silently inserting at the
    wrong dim."""
    row = conn.execute(text("""
        SELECT atttypmod
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = :name AND a.attname = 'embedding'
    """), {"name": name}).fetchone()
    if row is None:
        return None
    return int(row[0])


def ensure_tenant_embedding_table(tenant_id: object) -> str:
    """Idempotently create the per-tenant embedding table + IVFFlat
    index. Returns the table name.

    Schema:
    - id, feedback_event_id, embedding (vector), embedding_model_version,
      created_at — the core retrieval fields.
    - few_shot_text — the rendered (canvas, prompt, proposal) text the
      Builder system prompt embeds as a few-shot example. Denormalised
      at write time so retrieval is a single vector-DB query (no
      cross-DB join to support_chat_messages at request time).

    If the table exists with a different `embedding` dim than the
    current `_EMBEDDING_DIM` (e.g., a 384-dim table left from arctic-s
    prototyping), it is dropped and recreated at the new dim.
    """
    name = table_name_for_tenant(tenant_id)
    dim = _current_dim()
    eng = get_engine()
    with eng.begin() as conn:
        # Self-heal: the pgvector extension must exist before any vector() DDL.
        # (A fresh wizard DB that never ran the extension init would 500 here.)
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        existing_dim = _table_dim(conn, name)
        if existing_dim is not None and existing_dim != dim:
            logger.warning(
                "wizard vector: dropping %s (dim=%s) to recreate at dim=%s",
                name, existing_dim, dim,
            )
            conn.execute(text(f"DROP TABLE {name} CASCADE"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {name} (
                id BIGSERIAL PRIMARY KEY,
                feedback_event_id INTEGER NOT NULL,
                embedding vector({dim}) NOT NULL,
                embedding_model_version TEXT NOT NULL,
                few_shot_text TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        # Defensive ALTER for tables that pre-date the few_shot_text
        # column (e.g., tenant_1 from §1.4b dev testing).
        conn.execute(text(f"""
            ALTER TABLE {name}
                ADD COLUMN IF NOT EXISTS few_shot_text TEXT NOT NULL DEFAULT ''
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_event ON {name} (feedback_event_id)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_created_at ON {name} (created_at)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_model_version ON {name} (embedding_model_version)"))
        conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS ix_{name}_embedding_ivfflat
                ON {name}
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
        """))
    return name


def ensure_shared_embedding_table() -> str:
    """Idempotently provision the cross-tenant shared embedding table.

    No `tenant_id` column by design — every record is the output of
    `strip_for_cross_tenant()` and carries no tenant-identifying
    content. The strip pipeline (services/wizard_ml/cross_tenant_strip.py)
    is the privacy contract.

    Differs from per-tenant tables: also stores the resolved `intent`
    classification so retrieval can pre-filter by intent before the ANN
    search (e.g., audit_graph requests only retrieve audit-graph
    patterns).
    """
    name = _SHARED_TABLE_NAME
    dim = _current_dim()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        existing_dim = _table_dim(conn, name)
        if existing_dim is not None and existing_dim != dim:
            logger.warning(
                "wizard vector: dropping %s (dim=%s) to recreate at dim=%s",
                name, existing_dim, dim,
            )
            conn.execute(text(f"DROP TABLE {name} CASCADE"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {name} (
                id BIGSERIAL PRIMARY KEY,
                feedback_event_id INTEGER NOT NULL,
                embedding vector({dim}) NOT NULL,
                embedding_model_version TEXT NOT NULL,
                intent TEXT NOT NULL,
                few_shot_text TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(f"""
            ALTER TABLE {name}
                ADD COLUMN IF NOT EXISTS few_shot_text TEXT NOT NULL DEFAULT ''
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_event ON {name} (feedback_event_id)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_created_at ON {name} (created_at)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_model_version ON {name} (embedding_model_version)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_intent ON {name} (intent)"))
        conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS ix_{name}_embedding_ivfflat
                ON {name}
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
        """))
    return name


def shared_table_name() -> str:
    return _SHARED_TABLE_NAME


def list_tenant_embedding_tables() -> list[str]:
    """Every per-tenant proposal-embedding table currently in the DB."""
    eng = get_engine()
    with eng.begin() as conn:
        rows = conn.execute(text(
            "SELECT tablename FROM pg_tables "
            "WHERE tablename ~ '^tenant_[A-Za-z0-9_]+_proposal_embeddings$'"
        )).fetchall()
    return sorted(r[0] for r in rows)


def _vec_literal(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def migrate_wizard_embeddings() -> dict:
    """Re-embed the wizard's pgvector stores to the currently-pinned Node Wizard
    model. Source is the `few_shot_text` column (kept at write time), so this is
    a true re-embed, not a data-losing drop. Per table: read text FIRST, embed
    with the new model (the Pro sidecar does the embed+version via
    `wizard_pro_client.reembed_all`), THEN drop+recreate at the new dim and
    re-insert — so a failure mid-embed leaves the old table intact.
    """
    from services import wizard_pro_client as _pro
    if not _pro.pro_available():
        # CE / sidecar absent: the wizard vectors are the sidecar's to produce,
        # so there is nothing to re-embed to — skip (the docs/RAG path is
        # unaffected; caller logs the "skipped" status).
        return {"dim": _current_dim(), "version": _pro.model_version(), "tables": [],
                "migrated": 0, "skipped_no_text": 0, "status": "skipped"}
    new_dim = _current_dim()
    version = _pro.model_version()
    result = {"dim": new_dim, "version": version, "tables": [],
              "migrated": 0, "skipped_no_text": 0, "status": "ok"}

    eng = get_engine()
    targets = [(_SHARED_TABLE_NAME, True)] + [(t, False) for t in list_tenant_embedding_tables()]

    for name, is_shared in targets:
        # 1) Read rows (with source text) — table untouched.
        with eng.begin() as conn:
            if _table_dim(conn, name) is None:
                continue
            cols = "feedback_event_id, few_shot_text, created_at" + (", intent" if is_shared else "")
            rows = conn.execute(text(f"SELECT {cols} FROM {name}")).fetchall()
        keep = [r for r in rows if (r[1] or "").strip()]
        result["skipped_no_text"] += len(rows) - len(keep)
        if not keep:
            # Nothing re-embeddable — still bring the empty table to the new dim.
            _recreate_wizard_table(name, is_shared, new_dim)
            result["tables"].append(name)
            continue

        # 2) Re-embed the text with the NEW model BEFORE we drop anything.
        # reembed_all batches internally; None means the sidecar dropped
        # mid-run — the old table stays intact.
        embedded = _pro.reembed_all([r[1] for r in keep])
        if embedded is None:
            raise RuntimeError("wizard_pro reembed_all returned None mid-migration")
        vecs, _ver = embedded

        # 3) Drop+recreate at the new dim, then re-insert.
        _recreate_wizard_table(name, is_shared, new_dim)
        with eng.begin() as conn:
            for r, v in zip(keep, vecs):
                params = {"e": r[0], "emb": _vec_literal(v), "ver": version,
                          "txt": r[1], "ts": r[2]}
                if is_shared:
                    params["intent"] = r[3]
                    conn.execute(text(
                        f"INSERT INTO {name} (feedback_event_id, embedding, embedding_model_version, intent, few_shot_text, created_at) "
                        f"VALUES (:e, (:emb)::vector, :ver, :intent, :txt, :ts)"), params)
                else:
                    conn.execute(text(
                        f"INSERT INTO {name} (feedback_event_id, embedding, embedding_model_version, few_shot_text, created_at) "
                        f"VALUES (:e, (:emb)::vector, :ver, :txt, :ts)"), params)
        result["migrated"] += len(keep)
        result["tables"].append(name)

    logger.info("wizard embedding migration: dim=%s version=%s (%d migrated, %d skipped, %d tables)",
                new_dim, version, result["migrated"], result["skipped_no_text"], len(result["tables"]))
    return result


def _recreate_wizard_table(name: str, is_shared: bool, dim: int) -> None:
    """Drop + recreate one wizard embedding table at `dim` (reusing the ensure_*
    DDL, which drops on dim-drift and rebuilds the indexes)."""
    if is_shared:
        # ensure_* reads _current_dim(); the pin already changed, so it recreates
        # at the new dim. Force the drop first so an unchanged dim still rebuilds.
        eng = get_engine()
        with eng.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        ensure_shared_embedding_table()
    else:
        m = re.match(r"^tenant_(.+)_proposal_embeddings$", name)
        tid = m.group(1) if m else None
        eng = get_engine()
        with eng.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        if tid is not None:
            ensure_tenant_embedding_table(tid)


def healthcheck() -> bool:
    try:
        eng = get_engine()
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
            row = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).fetchone()
        return row is not None
    except Exception as e:  # pragma: no cover
        logger.warning("wizard vector DB healthcheck failed: %s", e)
        return False
