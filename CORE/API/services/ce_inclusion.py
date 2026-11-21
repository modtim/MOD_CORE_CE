"""services/ce_inclusion.py — GSEC FX.73 part 4c (owner decision Q56=A).

ONE source of truth for "is this node included in the CE (open-source)
edition?", read by the palette API so the WorkflowBuilder can badge it.

Two layers behind one interface:

1. `node_definitions.ce_included` (migration
   20261007zu_fx73_p4c_ce_included.py) — the stored source of truth, stamped
   TRUE on CREATE by services/node_scanner.py for every node whose name is in
   the CE node-seed keep-list (open-core/node-seed-keeplist.yaml). That scan
   is the only place a CE install decides what it ships, so the stamp is
   written there and nowhere else.

2. The keep-list itself as the derivation fallback: on a DB where the column
   has not been applied yet (models.py is bind-mounted live, so the model can
   outlive the migration), `ce_included_for_nodes` resolves from the keep-list
   by node name instead of raising. Same fail-closed posture as the scanner's
   loader: no badge when the keep-list is missing — a missing/broken keep-list
   yields no badge rather than a wrong one.

`commercial_safe_only_for` / the FX.73 p3 visibility gate are unrelated here:
CE inclusion is about which nodes the open-source edition ships, not about
licence commercial-use safety. Nothing in this module hides or deletes nodes.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sa_inspect

COLUMN_NAME = "ce_included"

_PRESENT: Optional[bool] = None
_CHECKED_AT: Optional[float] = None
_RECHECK_SECONDS = 60.0


def column_available(db) -> bool:
    """True if node_definitions.ce_included exists in the DB.

    Cached per process: once True, cached forever; while False, re-checked at
    most every 60 seconds. Any probe error counts as False (the caller then
    falls back to the keep-list derivation). Same pattern as
    utils/node_cost.tier_columns_available.
    """
    global _PRESENT, _CHECKED_AT

    if _PRESENT:
        return True

    now = time.monotonic()
    if _CHECKED_AT is not None and now - _CHECKED_AT < _RECHECK_SECONDS:
        return False
    _CHECKED_AT = now

    try:
        bind = db.get_bind() if hasattr(db, "get_bind") else db
        columns = {c["name"] for c in sa_inspect(bind).get_columns("node_definitions")}
        _PRESENT = COLUMN_NAME in columns
    except Exception:  # noqa: BLE001
        _PRESENT = False
    return _PRESENT


def keep_list_names() -> Optional[set]:
    """The CE node-seed keep-list as a set of node names (the derivation
    fallback). An EMPTY set when the keep-list is missing/unreadable (the
    loader fails closed and never returns None) — callers then report no node
    as CE-included (no badge, never a guessed one)."""
    from services import node_scanner

    return node_scanner._load_ce_keeplist_names()


def ce_included_for_nodes(db, node_ids: List[Any]) -> Dict[int, bool]:
    """FX.73 p4c — CE-inclusion flags for the palette badge in ONE query.

    Returns ``{node_definition_id: True}`` for nodes the CE edition ships.
    Nodes not included (or with no stamp and no keep-list match) are ABSENT
    from the dict, so the WorkflowBuilder badge renders nothing — never a
    guessed "CE".

    Stored column first; when the column is not yet applied, derive from the
    keep-list by node name (the same list the scanner stamps from).
    """
    ids = [int(i) for i in node_ids if i is not None]
    if not ids:
        return {}

    import models

    if column_available(db):
        rows = (
            db.query(models.NodeDefinition.id, models.NodeDefinition.ce_included)
            .filter(models.NodeDefinition.id.in_(ids))
            .all()
        )
        return {int(row[0]): True for row in rows if row[1]}

    # Column not applied yet: derive from the single source of truth (the
    # keep-list) so the badge still works on an un-migrated install.
    names = keep_list_names()
    if not names:
        return {}
    rows = (
        db.query(models.NodeDefinition.id, models.NodeDefinition.name)
        .filter(models.NodeDefinition.id.in_(ids))
        .all()
    )
    return {int(row[0]): True for row in rows if row[1] in names}
