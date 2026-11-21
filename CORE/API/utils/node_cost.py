"""Pure functions for tiered credit pricing.

This module provides helper functions for computing and validating
per-tier cost values derived from a single cost_in_credits integer.
Ratios: small=0.5x, medium=1x, large=2.5x — reflects typical input-size scaling.
"""

from __future__ import annotations

import math
import time


def scaled_tiers(c: int) -> dict[str, int]:
    """Derive per-tier credit values from a single existing cost_in_credits integer.

    Ratios: small=0.5x, medium=1x, large=2.5x — reflects typical input-size scaling.
    Free nodes (c=0 or c<0) get all zeros.

    Args:
        c: The cost_in_credits integer value.

    Returns:
        A dict with keys 'small', 'medium', 'large' containing the derived values.
        For c <= 0, all values are 0.
        For c > 0:
            small  = max(1, ceil(c * 0.5))
            medium = c
            large  = max(1, ceil(c * 2.5))
    """
    if c <= 0:
        return {"small": 0, "medium": 0, "large": 0}
    return {
        "small": max(1, math.ceil(c * 0.5)),
        "medium": c,
        "large": max(1, math.ceil(c * 2.5)),
    }


def tiers_consistent(cost: int | None, tiers: dict[str, int] | None) -> bool:
    """Check if cost_in_credits and cost_credits_by_tier are consistent.

    This mirrors the CHECK constraint:
    - If tiers is empty ({}) or None, always consistent.
    - Otherwise, cost must equal tiers['medium'].

    Args:
        cost: The cost_in_credits integer value.
        tiers: The cost_credits_by_tier dict.

    Returns:
        True if consistent, False otherwise.
    """
    if not tiers:
        return True
    if cost is None:
        return False
    return cost == tiers.get("medium")


def apply_node_cost(
    nd: object,
    credits: int,
    *,
    basis: str | None = None,
) -> None:
    """Set cost_in_credits and derive/update tiers.

    This is the canonical way to set cost on a node definition.

    Behavior:
    - Sets nd.cost_in_credits = credits
    - If tiers are non-empty OR basis is given, assigns a NEW dict from scaled_tiers(credits)
    - Never mutates JSONB in place (SQLAlchemy won't see it)
    - Sets nd.cost_basis when basis is given
    - Sets nd.cost_basis = 'free' when credits == 0

    Args:
        nd: The node definition object (SimpleNamespace in tests, ORM model in production).
        credits: The cost_in_credits value to set.
        basis: Optional basis value ('estimated', 'calibrated', 'manual', 'free').
    """
    nd.cost_in_credits = credits

    # If tiers are non-empty or basis is specified, re-derive tiers
    current_tiers = getattr(nd, "cost_credits_by_tier", None)
    if (current_tiers and current_tiers.get("medium") is not None) or basis is not None:
        nd.cost_credits_by_tier = scaled_tiers(credits)

    # Set basis when explicitly provided or when credits == 0 (free)
    if basis is not None:
        nd.cost_basis = basis
    elif credits == 0:
        nd.cost_basis = "free"


def resolve_tiered_cost(
    nd: object,
    size_tier: str | None,
    db: object = None,
) -> int:
    """GX.sz-tcp.11 — resolve the credits to charge for ONE run of `nd`.

    Uses `nd.cost_credits_by_tier[size_tier]` when the node has a non-empty
    tier map AND a `size_tier` was given. A tier key missing from the map
    falls back to 'medium'; an empty/absent map, a missing 'medium' entry,
    or no `size_tier` at all falls back to the flat `nd.cost_in_credits`
    (unchanged pre-tiered-pricing behavior).

    `db` is optional: when given, gates the tier-map read behind
    `tier_columns_available(db)` so a live DB that hasn't run the tier
    migration yet (deferred() column absent) is never touched — same
    guard the rest of this module uses. Callers that already know the
    tier columns exist (e.g. tests using a plain object/SimpleNamespace,
    or in-memory NodeDefinition instances) may omit `db`.
    """
    flat = int(getattr(nd, "cost_in_credits", 0) or 0)
    if nd is None or not size_tier:
        return flat
    if db is not None and not tier_columns_available(db):
        return flat
    tiers = getattr(nd, "cost_credits_by_tier", None)
    if not tiers:
        return flat
    value = tiers.get(size_tier)
    if value is None:
        value = tiers.get("medium")
    if value is None:
        return flat
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return flat
    if resolved < 0:
        return flat
    return resolved


def sync_tiers_to_cost(nd: object) -> bool:
    """Re-derive tiers from cost_in_credits if inconsistent.

    Used by the before_insert/before_update mapper listener.

    Args:
        nd: The node definition object.

    Returns:
        True if tiers were updated, False if already consistent or no-op.
    """
    cost = getattr(nd, "cost_in_credits", None)
    tiers = getattr(nd, "cost_credits_by_tier", None)

    # If tiers are empty or None, nothing to sync
    if not tiers:
        return False

    # Check consistency
    if tiers_consistent(cost, tiers):
        return False

    # Re-derive tiers from cost (None or <=0 gives zero tiers)
    nd.cost_credits_by_tier = scaled_tiers(cost or 0)
    return True


# ---------------------------------------------------------------------------
# Live-DB schema probe
# ---------------------------------------------------------------------------
# The tier columns (cost_credits_by_tier, cost_basis, regression_coefficients)
# are mapped as deferred() on NodeDefinition but may not exist in a live DB
# until the tier migration is applied. Touching (reading OR assigning) one of
# them on a live instance emits SQL naming a missing column. Callers that write
# tier data must gate on tier_columns_available(db) first.

_TIER_RECHECK_SECONDS = 60.0
_tier_columns_present = False  # sticky once True (columns are never dropped)
_tier_columns_checked_at: float | None = None


def tier_columns_available(db) -> bool:
    """Return True if node_definitions.cost_credits_by_tier exists in the DB.

    ``db`` may be a Session (probed via its bound engine/connection) or a
    Connection/Engine directly (e.g. from a mapper event listener).
    Caching (per process): once True, cached forever; while False, the DB
    is re-checked at most every 60 seconds. Any probe error counts as False.
    """
    global _tier_columns_present, _tier_columns_checked_at

    if _tier_columns_present:
        return True

    now = time.monotonic()
    if (
        _tier_columns_checked_at is not None
        and now - _tier_columns_checked_at < _TIER_RECHECK_SECONDS
    ):
        return False
    _tier_columns_checked_at = now

    try:
        from sqlalchemy import inspect as sa_inspect

        bind = db.get_bind() if hasattr(db, "get_bind") else db
        columns = {c["name"] for c in sa_inspect(bind).get_columns("node_definitions")}
    except Exception:
        return False

    if "cost_credits_by_tier" in columns:
        _tier_columns_present = True
        return True
    return False


def _reset_tier_columns_cache() -> None:
    """Test helper: clear the per-process tier-column probe cache."""
    global _tier_columns_present, _tier_columns_checked_at
    _tier_columns_present = False
    _tier_columns_checked_at = None
