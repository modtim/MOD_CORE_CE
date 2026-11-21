"""Phase 8 — cross-tenant index of discovered HF→OV architecture mappings.

When `arch_resolver` (Containerizer-side) proposes an OpenVINO mapping
for an HF class that wasn't in the static allowlist and the
downstream port succeeds, we record the mapping in
`PortOutcome.diagnostics['arch_mapping']`. Those rows accumulate across
tenants; this module aggregates them into a single "best-known"
mapping per HF class so future operators hit the cache-path directly.

Query shape:

    index = build_index(db, since_days=90)
    # {
    #   "SomeNewArchForCausalLM": {
    #     "ov_class": "OVModelForCausalLM",
    #     "task": "text-generation",
    #     "observation_count": 14,
    #     "tenant_count": 6,
    #     "first_seen": "2026-03-12T04:12:00+00:00",
    #     "last_seen":  "2026-04-22T10:51:03+00:00",
    #   },
    #   ...
    # }

The nightly recipe-evolution job (Phase 10) reads this index and
promotes high-confidence entries (observation_count >= 5 across >= 2
tenants) into the Containerizer's static `_HF_TO_OV` + allowlist.

Read-only; no writes through this module. Writers are on the
Containerizer side via `PortOutcome.diagnostics` during smoke result
processing.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Match the Containerizer's _KNOWN_OV_CLASSES (a superset is fine —
# extra classes land as None-entries and get filtered downstream).
_VALID_OV_PREFIXES = ("OVModel", "OVStableDiffusion")


def _is_valid_ov_class(ov: Any) -> bool:
    if not isinstance(ov, str) or not ov:
        return False
    return any(ov.startswith(p) for p in _VALID_OV_PREFIXES)


def _is_valid_task(task: Any) -> bool:
    if not isinstance(task, str) or not task:
        return False
    # Optimum tasks are always kebab-cased ASCII; cheap filter.
    return task.replace("-", "").isalnum()


def build_index(
    db: Session,
    *,
    since_days: int = 90,
    min_confidence: float = 0.6,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate all PortOutcome rows whose diagnostics carry an
    `arch_mapping` entry. Returns a dict keyed by HF class name with
    the most-observed (ov_class, task) winner + counts.

    `since_days` limits the scan to recent observations so stale
    mappings from earlier porter versions don't skew the aggregate.
    `min_confidence` drops low-quality LLM hits before counting.
    """
    from models import PortOutcome  # local import — avoids circular
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)

    rows = db.query(PortOutcome).filter(
        PortOutcome.created_at >= cutoff,
        PortOutcome.diagnostics.isnot(None),
    ).all()

    # { hf_class: { (ov_class, task): { count, tenants, first, last } } }
    bucket: Dict[str, Dict[tuple, Dict[str, Any]]] = {}

    for row in rows:
        diag = row.diagnostics
        if not isinstance(diag, dict):
            continue
        mapping = diag.get("arch_mapping")
        if not isinstance(mapping, dict):
            continue
        hf = str(mapping.get("hf_class") or "").strip()
        ov = str(mapping.get("ov_class") or "").strip()
        task = str(mapping.get("task") or "").strip()
        try:
            conf = float(mapping.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if not hf:
            continue
        if not _is_valid_ov_class(ov) or not _is_valid_task(task):
            continue
        if conf < min_confidence:
            continue

        by_hf = bucket.setdefault(hf, {})
        key = (ov, task)
        slot = by_hf.setdefault(key, {
            "count": 0,
            "tenants": set(),
            "first_seen": None,
            "last_seen": None,
        })
        slot["count"] += 1
        if row.tenant_id is not None:
            slot["tenants"].add(int(row.tenant_id))
        ca = row.created_at
        if ca is not None:
            if slot["first_seen"] is None or ca < slot["first_seen"]:
                slot["first_seen"] = ca
            if slot["last_seen"] is None or ca > slot["last_seen"]:
                slot["last_seen"] = ca

    # Collapse each HF class to its most-observed winner. Ties
    # broken by tenant diversity, then most-recent last_seen.
    # bucket only contains hfs that passed validation, so by_key is
    # always non-empty and best_key is always set after the inner loop.
    out: Dict[str, Dict[str, Any]] = {}
    for hf, by_key in bucket.items():
        best_key = None
        best_score: tuple = (-1, -1, None)
        for key, slot in by_key.items():
            score = (
                slot["count"],
                len(slot["tenants"]),
                slot["last_seen"] or _dt.datetime.min,
            )
            if best_key is None or score > best_score:
                best_key = key
                best_score = score
        slot = by_key[best_key]
        out[hf] = {
            "ov_class": best_key[0],
            "task": best_key[1],
            "observation_count": slot["count"],
            "tenant_count": len(slot["tenants"]),
            "first_seen": slot["first_seen"].isoformat() if slot["first_seen"] else None,
            "last_seen": slot["last_seen"].isoformat() if slot["last_seen"] else None,
        }
    return out


def promotion_candidates(
    index: Dict[str, Dict[str, Any]],
    *,
    min_count: int = 5,
    min_tenants: int = 2,
) -> Dict[str, Dict[str, Any]]:
    """Filter the index to entries that pass the "safe to bake into the
    static allowlist" threshold. Defaults match the Phase 10 nightly
    job's criteria."""
    out: Dict[str, Dict[str, Any]] = {}
    for hf, entry in index.items():
        if int(entry.get("observation_count") or 0) < min_count:
            continue
        if int(entry.get("tenant_count") or 0) < min_tenants:
            continue
        out[hf] = entry
    return out


__all__ = ["build_index", "promotion_candidates"]
