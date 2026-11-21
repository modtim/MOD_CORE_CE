"""Phase 4 — context builders for the unified support LLM.

Three context layers wrap every support-chat call:
  1. user/tenant — who's asking, what plan, what they've recently done
  2. features catalog — curated list of platform features (one heading
     per feature with What/How/Plan subsections)
  3. documentation RAG — the existing docs_ingester index

This module owns the first two; layer 3 lives in `utils/docs_ingester`.
"""
from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# User / tenant context.
# ---------------------------------------------------------------------

_USER_CACHE: Dict[int, Tuple[float, str]] = {}  # user_id → (epoch_expiry, body)
_USER_CACHE_TTL_SECONDS = 60.0


def build_user_context(db: Session, user) -> str:
    """Compact <=400-token block describing the asking user. Cached
    60 s per user_id to amortise the recent-activity query."""
    uid = getattr(user, "id", None)
    if uid is None:
        return ""
    now = time.time()
    cached = _USER_CACHE.get(uid)
    if cached is not None and cached[0] > now:
        return cached[1]

    parts: List[str] = []
    parts.append(f"User: {user.first_name or ''} {user.last_name or ''} <{user.email}>")
    parts.append(f"Username: {user.username or '?'}")
    role = "platform admin" if getattr(user, "is_platform_admin", False) else "tenant member"
    parts.append(f"Role: {role}")

    # Tenant info (best-effort).
    tenant_block = _tenant_block(db, getattr(user, "tenant_id", None))
    if tenant_block:
        parts.append(tenant_block)

    # Recent activity — the user's last 5 audit events provide a hint
    # at what they've been doing without spelling out internals.
    recent = _recent_activity(db, uid)
    if recent:
        parts.append("Recent activity:")
        for line in recent:
            parts.append(f"  - {line}")

    body = "\n".join(parts)
    _USER_CACHE[uid] = (now + _USER_CACHE_TTL_SECONDS, body)
    return body


def _tenant_block(db: Session, tenant_id: Optional[int]) -> str:
    if not tenant_id:
        return ""
    try:
        import models
        tenant = db.query(models.Tenant).filter(
            models.Tenant.id == tenant_id,
        ).first()
        if not tenant:
            return ""
        plan_name = "?"
        try:
            sub = (
                db.query(models.Subscription)
                  .filter(models.Subscription.tenant_id == tenant_id,
                          models.Subscription.status == "active")
                  .order_by(models.Subscription.created_at.desc())
                  .first()
            )
            if sub and sub.plan_id:
                plan = db.query(models.Plan).filter(
                    models.Plan.id == sub.plan_id,
                ).first()
                plan_name = plan.name if plan else "?"
        except Exception:
            pass
        return f"Tenant: {tenant.name} (plan: {plan_name}, id={tenant.id})"
    except Exception:
        return ""


def _recent_activity(db: Session, user_id: int) -> List[str]:
    try:
        import models
        rows = (
            db.query(models.PermissionAuditLog)
              .filter(models.PermissionAuditLog.actor_user_id == user_id)
              .order_by(models.PermissionAuditLog.id.desc())
              .limit(5)
              .all()
        )
        out: List[str] = []
        for r in rows:
            action = r.action.value if r.action else "?"
            out.append(f"{action} on {r.resource_type or '?'} #{r.resource_id or '?'}")
        return out
    except Exception:
        return []


def reset_user_cache_for_tests() -> None:
    """Clear the in-process user-context cache (test helper)."""
    _USER_CACHE.clear()


# ---------------------------------------------------------------------
# Features catalog.
# ---------------------------------------------------------------------

# The catalog is a single curated markdown file shipped in the docs
# repo. Format: one ## heading per feature, with three known
# subsections rendered as plain prose.
def _candidate_features_paths() -> tuple[str, ...]:
    """Resolve every plausible filesystem location for features.md so
    the catalog works in the API container (mount at /app), in tests
    (running from CORE/API), and in dev (CORE/ as project root)."""
    cands: list[str] = [
        "/app/CORE/docs-site/docs/features.md",
        "CORE/docs-site/docs/features.md",
    ]
    here = Path(__file__).resolve()
    for n in (2, 3, 4):
        try:
            cands.append(str(here.parents[n] / "docs-site" / "docs" / "features.md"))
            cands.append(str(here.parents[n] / "CORE" / "docs-site" / "docs" / "features.md"))
        except IndexError:
            break
    # Fallback for /app/CORE/API checkouts.
    cands.append("/path/to/MOD_CORE/CORE/docs-site/docs/features.md")
    return tuple(cands)


FEATURES_PATH_CANDIDATES = _candidate_features_paths()


@lru_cache(maxsize=1)
def load_features_catalog() -> List[Dict[str, str]]:
    """Returns a list of {id, title, plan_required, summary, how_to}
    dicts parsed from the features.md catalog. Cached at module-load
    time; the /platform/support/reindex-docs admin endpoint clears
    this cache via `load_features_catalog.cache_clear()`."""
    text = ""
    for cand in FEATURES_PATH_CANDIDATES:
        try:
            p = Path(cand)
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace")
                break
        except Exception:
            continue
    if not text:
        logger.warning("support_context: features.md not found in any candidate path")
        return []

    return _parse_features_markdown(text)


def _parse_features_markdown(text: str) -> List[Dict[str, str]]:
    """Parse the features catalog into structured entries."""
    lines = text.splitlines()
    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    current_section: Optional[str] = None
    buffer: List[str] = []

    def _flush_section():
        nonlocal current, current_section, buffer
        if current is None or current_section is None:
            return
        current[current_section] = "\n".join(buffer).strip()
        buffer = []

    def _flush_entry():
        nonlocal current, current_section
        if current is None:
            return
        _flush_section()
        # Only keep entries that have at least a title.
        if current.get("title"):
            entries.append(current)
        current = None
        current_section = None

    for raw in lines:
        line = raw.rstrip()
        # New feature header: "## Feature title".
        if line.startswith("## "):
            _flush_entry()
            title = line[3:].strip()
            current = {
                "id": _slugify(title),
                "title": title,
                "summary": "",
                "how_to": "",
                "plan_required": "",
            }
            current_section = None
            continue
        if current is None:
            continue
        # Subsection heading: "**What it does**" or "### How to use it".
        sub = _detect_subsection(line)
        if sub is not None:
            _flush_section()
            current_section = sub
            continue
        if current_section is not None:
            buffer.append(line)
    _flush_entry()
    return entries


def _detect_subsection(line: str) -> Optional[str]:
    """Detect subsection markers like **What it does** / ### How to /
    *Plan*. Only triggers on heading-shaped lines (bold-wrapped or
    starting with #) so prose containing the same words isn't
    misclassified."""
    stripped = line.strip()
    if not stripped:
        return None
    is_heading = (
        (stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4)
        or (stripped.startswith("*") and stripped.endswith("*") and len(stripped) > 2)
        or stripped.startswith("#")
    )
    if not is_heading:
        return None
    inner = stripped.strip("*# ").lower()
    norm = re.sub(r"[^a-z0-9]", "", inner)
    if "whatitdoes" in norm or "summary" in norm or "overview" in norm:
        return "summary"
    if "howtouse" in norm or "howto" in norm or "usage" in norm:
        return "how_to"
    if norm in ("plan", "tier", "planrequired", "tiers"):
        return "plan_required"
    return None


def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "feature"


def select_features_for_query(
    query: str,
    all_features: List[Dict[str, str]],
    k: int = 5,
) -> List[Dict[str, str]]:
    """Return the top-k features most relevant to `query`. Uses
    rapidfuzz when available; falls back to a tokens-overlap score
    otherwise. The LLM does the final filtering — this is a recall
    pass."""
    if not query or not all_features:
        return []
    try:
        from rapidfuzz import fuzz  # type: ignore

        def _score(entry: Dict[str, str]) -> int:
            haystack = " ".join((entry.get("title") or "",
                                  entry.get("summary") or ""))
            return int(fuzz.partial_ratio(query, haystack))

        scored = sorted(all_features, key=_score, reverse=True)
        return scored[:k]
    except ImportError:
        # Tokens-overlap fallback.
        q_tokens = set(_tokenize(query))
        scored = []
        for entry in all_features:
            haystack = (entry.get("title") or "") + " " + (entry.get("summary") or "")
            overlap = len(q_tokens & set(_tokenize(haystack)))
            scored.append((overlap, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:k]]


def _tokenize(s: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


# ---------------------------------------------------------------------
# Public entry — assemble the full prompt context block.
# ---------------------------------------------------------------------

def build_prompt_context(
    db: Session,
    user,
    query: str,
    *,
    k_features: int = 5,
) -> Dict[str, Any]:
    """Returns {user_block, features, raw_query} so callers can splice
    the pieces into their preferred prompt template."""
    user_block = build_user_context(db, user)
    features = select_features_for_query(query, load_features_catalog(), k=k_features)
    return {
        "user_block": user_block,
        "features": features,
        "raw_query": query,
    }


__all__ = [
    "build_user_context",
    "load_features_catalog",
    "select_features_for_query",
    "build_prompt_context",
    "reset_user_cache_for_tests",
]
