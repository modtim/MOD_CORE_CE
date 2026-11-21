"""CRUD (query-only) layer for the `ml` domain (Gate 2b migration).

Two read-only queries backing the ML queue-routing training endpoints in
`routers/ml.py`. Both are service-account-only (Airflow/DAG-trainer callers,
not end users) and intentionally cross-tenant — the routing model needs
training signal from the whole platform, not one tenant — so neither filters
on `tenant_id`. That matches the pre-migration inline queries exactly; this
migration does not add tenant scoping that wasn't there before.

Soft-delete note (ISO 27001 / iso-no-hard-delete memory): `Cog` has
`deleted_at` (`SoftDeleteMixin`). `get_training_data_stats` does NOT filter
it — the pre-migration `check_training_data_availability` query never did
either. `list_cogs_with_processing_history` DOES filter
`deleted_at.is_(None)` — matching the pre-migration `get_training_data`
query. These are genuinely different pre-existing behaviors (not a typo),
ported 1:1 per the migration rules rather than "fixed".

JSONB note: `Cog.processing_metadata` is the custom `models.JSONB`
TypeDecorator (impl=generic `JSON`), which has no `.astext` comparator (that
accessor is PostgreSQL-dialect-JSONB-specific, not generic-JSON). The
pre-migration router used `.astext` throughout
`check_training_data_availability`'s SQL query, which raises
`AttributeError: ... object has no attribute 'astext'` on every single call
of that endpoint -- a live 500 bug for its entire lifetime. Fixed here to
`.as_string()` per the migration's JSONB rule (the same fix already applied
in `crud/external_invitations.py` for the identical bug class on the same
custom type). `get_training_data`'s query never used `.astext` (it loads
whole rows and walks the JSON in Python), so it is unaffected and ported
verbatim.
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


def get_training_data_stats(db: Session) -> Any:
    """Aggregate counts + date range for successful, fully-populated
    queue-routing training runs recorded in `Cog.processing_metadata`.

    Cross-tenant by design (service-account only). Matches the pre-migration
    inline query verbatim except `.astext` -> `.as_string()` (see module
    docstring) -- that change fixes a standing 500, it does not alter the
    query's filtering semantics.

    Returns the raw SQLAlchemy result row (`.total_runs`, `.unique_workers`,
    `.unique_dags`, `.oldest_run`, `.newest_run`); shaping into the response
    dict is the service's job.
    """
    node = models.Cog.processing_metadata['processing_history'][0]['nodes_executed'][0]

    query = db.query(
        func.count().label('total_runs'),
        func.count(func.distinct(node['worker_id'].as_string())).label('unique_workers'),
        func.count(func.distinct(node['node_kind'].as_string())).label('unique_dags'),
        func.min(
            func.to_timestamp(node['started_at'].as_string(), 'YYYY-MM-DD"T"HH24:MI:SS')
        ).label('oldest_run'),
        func.max(
            func.to_timestamp(node['started_at'].as_string(), 'YYYY-MM-DD"T"HH24:MI:SS')
        ).label('newest_run'),
    ).filter(
        models.Cog.processing_metadata.isnot(None),
        models.Cog.processing_metadata['processing_history'].isnot(None),
        node['status'].as_string() == 'success',
        node['duration_seconds'].isnot(None),
        node['hardware_specs'].isnot(None),
        node['parameters']['queue_name'].isnot(None),
        node['parameters']['file_format'].isnot(None),
    )

    return query.first()


def list_cogs_with_processing_history(db: Session, *, limit: Optional[int]) -> List["models.Cog"]:
    """Bounded list of non-deleted Cogs that have processing history, for the
    training-data export. Cross-tenant by design (service-account only);
    matches the pre-migration inline query verbatim.

    NOTE: `limit` is the caller-supplied training-data row cap forwarded from
    the request (default 10000 in the router), not an
    `api_conventions.Pagination` page size -- the original endpoint has no
    pagination envelope and this migration doesn't add one (would change the
    response shape / contract snapshot).
    """
    query = db.query(models.Cog).filter(
        models.Cog.processing_metadata.isnot(None),
        models.Cog.processing_metadata['processing_history'].isnot(None),
        models.Cog.deleted_at.is_(None),
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()
