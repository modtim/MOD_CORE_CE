"""Data-access layer for `routers/node_definitions_domains/workers_fleet.py`
(Gate 2b migration; see `services/_base.py` for the layering contract).

The ONLY place `db.query(models.X)` / `db.get(models.X, ...)` lives for this
slice: the `BuildSpecRecord` lookup backing `/wizard/repair-artifacts`, and
the `LibraryProfile` lookup its hard-library budget-bump check makes.
Distinct from `crud/node_defs_task_queue.py` (a different, already-migrated
`node_definitions_domains` sub-module that happens to fetch the same
`BuildSpecRecord` model for its own endpoints) — not shared, not touched by
this migration and vice versa.

Both fetches are tenant-UNscoped, verbatim extractions of the original
inline queries — the service layer applies whatever policy the original
handler applied inline (SHARED RULES #5).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_build_spec_record(db: Session, spec_id: int) -> Optional["models.BuildSpecRecord"]:
    """`repair_artifacts`: fetch a BuildSpecRecord by id. Unscoped — matches
    the original inline `filter_by(id=...)` query exactly."""
    return db.query(models.BuildSpecRecord).filter_by(id=spec_id).first()


def get_library_profile(db: Session, normalized_name: str) -> Optional["models.LibraryProfile"]:
    """`repair_artifacts` hard-library budget-bump check: fetch a
    LibraryProfile by its normalized primary key. Matches the original
    inline `db.get(models.LibraryProfile, norm)` exactly."""
    return db.get(models.LibraryProfile, normalized_name)
