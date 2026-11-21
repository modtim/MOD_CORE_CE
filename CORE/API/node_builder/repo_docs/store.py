"""Repo-docs cache facade (wizard-rebuild §3c).

`get_or_extract(db, tenant_id, repo_url, ref)` is the single public
entry. Behavior:

  Cache hit — row exists for the (tenant, repo_url, ref,
    current extractor_version) tuple AND `expires_at > NOW()`:
    return cached chunks immediately. No network, no LLM.

  Cache miss — fetch sentinels via `repo_fetcher.fetch_repo_files`,
    chunk each file via `chunk_extractor.extract_chunks`, tag
    intents via `intent_tagger.tag_chunks`, persist row with a
    7-day TTL. Return the new chunks.

Cache key includes `extractor_version` so a schema bump invalidates
old cache rows transparently (the old row sits expired-but-not-
swept until the hourly sweep deletes it).

`sweep_expired(db)` is called from the APScheduler hourly job
(`_run_repo_docs_extract_sweep` in services/scheduler.py).
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import models
from utils.repo_fetcher import fetch_repo_files

from .chunk_extractor import extract_chunks
from .intent_tagger import tag_chunks
from .types import DocChunk, RepoDocsExtract

logger = logging.getLogger(__name__)


# Cache lifetime. 7 days matches the design.md spec. Tunable per-
# deployment via a future settings field if needed.
_CACHE_TTL = timedelta(days=7)


# Resolve the current schema version once at import. We pull it from
# build_plan.types so the cache key tracks the single source of truth.
def _current_extractor_version() -> int:
    from node_builder.build_plan.types import EXTRACTOR_VERSION
    return EXTRACTOR_VERSION


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_extract(
    db: Session,
    tenant_id: int,
    repo_url: str,
    ref: str = "",
    *,
    auth_token: Optional[str] = None,
    prefer_llm: bool = True,
) -> tuple[models.RepoDocsExtract, bool]:
    """Return the cached extract for (tenant, repo_url, ref), or
    compute and persist a fresh one.

    Args:
      db: SQLAlchemy session.
      tenant_id: caller's tenant id.
      repo_url: the git URL (github / gitlab / raw.githubusercontent).
      ref: branch / tag / sha to fetch. Empty string lets the
        fetcher resolve the default branch; the resolved ref is
        stored on the row.
      auth_token: optional Bearer token forwarded to the fetcher.
      prefer_llm: forwarded to the intent tagger. False uses only
        the regex heuristic — useful for tests and air-gapped
        environments.

    Returns:
      (RepoDocsExtract row, cache_hit_bool). cache_hit_bool=True
      when the row was reused; False when we had to fetch.
    """
    version = _current_extractor_version()
    hit = _lookup_fresh(db, tenant_id, repo_url, ref, version)
    if hit is not None:
        return (hit, True)

    fresh = _extract_fresh(
        db, tenant_id, repo_url, ref, version,
        auth_token=auth_token, prefer_llm=prefer_llm,
    )
    return (fresh, False)


def sweep_expired(db: Session) -> int:
    """Delete all rows whose `expires_at < NOW()`. Returns the
    deleted row count. Called from the APScheduler hourly job."""
    result = db.execute(
        delete(models.RepoDocsExtract).where(
            models.RepoDocsExtract.expires_at < datetime.now(timezone.utc),
        )
    )
    db.commit()
    count = result.rowcount or 0
    if count:
        logger.info("repo_docs_extract sweep: deleted %d expired rows", count)
    return count


def chunks_from_row(row: models.RepoDocsExtract) -> list[DocChunk]:
    """Decode the stored `chunks_json` back into DocChunk objects.

    The store persists chunks as dicts (Pydantic-style) so the JSONB
    column round-trips cleanly. Readers want typed objects.
    """
    out: list[DocChunk] = []
    for d in (row.chunks_json or []):
        if not isinstance(d, dict):
            continue
        out.append(DocChunk(
            source_file=d.get("source_file", ""),
            start_line=int(d.get("start_line") or 0),
            end_line=int(d.get("end_line") or 0),
            heading=d.get("heading"),
            content=d.get("content", ""),
            intents=list(d.get("intents") or []),
        ))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _lookup_fresh(
    db: Session,
    tenant_id: int,
    repo_url: str,
    ref: str,
    version: int,
) -> Optional[models.RepoDocsExtract]:
    """Return a non-expired cache row, or None."""
    now = datetime.now(timezone.utc)
    stmt = select(models.RepoDocsExtract).where(
        and_(
            models.RepoDocsExtract.tenant_id == tenant_id,
            models.RepoDocsExtract.repo_url == repo_url,
            models.RepoDocsExtract.ref == ref,
            models.RepoDocsExtract.extractor_version == version,
            models.RepoDocsExtract.expires_at > now,
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def _extract_fresh(
    db: Session,
    tenant_id: int,
    repo_url: str,
    ref: str,
    version: int,
    *,
    auth_token: Optional[str],
    prefer_llm: bool,
) -> models.RepoDocsExtract:
    """Fetch + chunk + tag + persist. Returns the new row.

    On any fetch failure (unsupported URL, all files missing) we
    still persist a row — with an empty chunks list and the
    files_missing populated — so the cache absorbs the "this repo
    has no docs we can read" outcome and the wizard doesn't retry
    the same repo on every submission.
    """
    files_by_path, _included, files_missing, resolved_ref = fetch_repo_files(
        repo_url, auth_token=auth_token, ref_override=ref or None,
    )

    # Chunk + tag.
    all_chunks: list[DocChunk] = []
    for path, content in files_by_path.items():
        all_chunks.extend(extract_chunks(path, content))
    tagged = tag_chunks(all_chunks, prefer_llm=prefer_llm) if all_chunks else []

    chunks_jsonable = [_chunk_to_dict(c) for c in tagged]
    now = datetime.now(timezone.utc)
    expires_at = now + _CACHE_TTL

    # Use the row's resolved_ref (or the requested ref) as the cache
    # key's `ref` so subsequent lookups with the same input shape hit.
    stored_ref = ref or (resolved_ref or "")

    # Upsert: ON CONFLICT (tenant_id, repo_url, ref, extractor_version)
    # WHERE deleted_at IS NULL — DO UPDATE. Two concurrent wizard
    # submissions for the same repo would otherwise race; the partial
    # unique index `uq_repo_docs_extract_live_key` catches it and we
    # refresh the existing live row. Soft-deleted tombstones don't
    # block re-extraction (wizard-rebuild §11b).
    stmt = pg_insert(models.RepoDocsExtract).values(
        tenant_id=tenant_id,
        repo_url=repo_url,
        ref=stored_ref,
        extractor_version=version,
        chunks_json=chunks_jsonable,
        files_missing=list(files_missing),
        fetched_at=now,
        expires_at=expires_at,
    ).on_conflict_do_update(
        index_elements=["tenant_id", "repo_url", "ref", "extractor_version"],
        index_where=models.RepoDocsExtract.deleted_at.is_(None),
        set_={
            "chunks_json": chunks_jsonable,
            "files_missing": list(files_missing),
            "fetched_at": now,
            "expires_at": expires_at,
        },
    ).returning(models.RepoDocsExtract.id)

    new_id = db.execute(stmt).scalar_one()
    db.commit()

    row = db.get(models.RepoDocsExtract, new_id)
    if row is None:
        # Shouldn't happen — the upsert just succeeded. Tag it loudly.
        raise RuntimeError(
            f"repo_docs_extract: upsert succeeded but row {new_id} "
            f"missing on read-back"
        )
    return row


def _chunk_to_dict(c: DocChunk) -> dict:
    """Convert a DocChunk to a JSONB-storable dict. `dataclasses.asdict`
    works since DocChunk is a slots dataclass."""
    return asdict(c)
