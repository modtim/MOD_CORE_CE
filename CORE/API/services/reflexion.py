"""Reflexion retriever §5.4 — shape-similarity + 90-day quality-signal join.

Retrieves the calling tenant's past wizard sessions whose proposals are
similar (cosine >= threshold) to a new query, enriched with the
`wizard_feedback_event` quality signal so the Builder LLM can prefer
patterns associated with accepted/kept workflows over rejected ones.

Public surface:
    retrieve_reflexion(tenant_id, query_vec, k=10)
    -> list[ReflexionSummary]
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text as _sa_text
from sqlalchemy.orm import Session

from services import (
    expressiveness_metrics,
    platform_settings as psr,  # leaf module: no audit cycle
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Module constants (no env vars — set here)
# ---------------------------------------------------------------------

SIMILARITY_THRESHOLD: float = 0.7
"""Minimum cosine similarity (0.0-1.0) for a candidate to be included."""

LOOKBACK_DAYS: int = 90
"""Number of days back to query for reflexion candidates."""

MAX_RESULTS: int = 10
"""Maximum number of past sessions to return."""


# ---------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------

@dataclass
class ReflexionSummary:
    """One past session that matched the reflexion query."""
    proposal_id: int
    action: str  # accepted | rejected_* | dismissed_without_action |
                 # downstream_edited | deleted
    was_edited: bool
    similarity: float
    session_id: Optional[str] = None
    user_prompt: Optional[str] = None
    workflow_run_id: Optional[int] = None

    @property
    def quality_label(self) -> str:
        """Map action/was_edited to the spec outcome labels."""
        if self.action == "accepted" and not self.was_edited:
            return "applied_and_kept"
        if self.action == "accepted" and self.was_edited:
            return "applied_and_edited"
        if self.action.startswith("rejected_"):
            return "rejected"
        if self.action == "deleted":
            return "deleted"
        return self.action  # fallback: dismissed_without_action etc.


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Cosine similarity between two vectors. Returns 0.0 for zero-len."""
    if not v1 or not v2:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    mag1 = math.sqrt(sum(a * a for a in v1))
    mag2 = math.sqrt(sum(b * b for b in v2))
    if mag1 == 0.0 or mag2 == 0.0:
        return 0.0
    return dot / (mag1 * mag2)


def _fetch_proposal_vectors(
    db: Session,
    feedback_event_ids: List[int],
) -> Dict[int, List[float]]:
    """Fetch stored proposal vectors for feedback events from the
    wizard vector DB system (Qdrant/pgvector).

    Returns a dict mapping feedback_event_id -> embedding vector.
    Falls back to empty dict if the vector DB is unavailable — the
    main DB query still works; the caller must handle zero-vectors
    (which yield 0.0 similarity, filtering out under the threshold).
    """
    try:
        from database_wizard_vector import get_engine
        engine = get_engine()
        ids_str = ",".join(str(i) for i in feedback_event_ids)
        if not ids_str:
            return {}
        sql = _sa_text(
            "SELECT feedback_event_id, embedding "
            "FROM tenant_proposal_embeddings "
            "WHERE feedback_event_id IN (" + ids_str + ")"
        )
        rows = engine.connect().execute(sql).fetchall()
        return {
            int(row[0]): list(row[1])
            for row in rows
        }
    except Exception:  # pragma: no cover
        return {}


# ---------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------

def retrieve_reflexion(
    db: Session,
    tenant_id: int,
    query_vec: List[float],
    *,
    k: int = MAX_RESULTS,
) -> List[ReflexionSummary]:
    """Retrieve past wizard sessions for Reflexion injection.

    Args:
        db: SQLAlchemy session bound to the main app database.
        tenant_id: Caller's tenant — every query is scoped to this tenant.
        query_vec: Embedding of the new prompt/proposal to match against.
        k: Max results (default MAX_RESULTS = 10).

    Returns a list of ReflexionSummary sorted by similarity descending,
    filtered to only include entries within the 90-day window and with
    cosine similarity >= SIMILARITY_THRESHOLD.
    """
    _start = time.perf_counter()
    try:
        if tenant_id is None:
            return []
        if not query_vec:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

        # ── Step 1: fetch feedback events within window + quality signals ──
        rows = db.execute(
            _sa_text("""
                SELECT
                    fe.id AS feedback_event_id,
                    fe.action,
                    fe.was_edited,
                    fe.proposal_id,
                    fe.created_at,
                    wtr.id AS test_run_id,
                    wtr.workflow_run_id,
                    wtr.wizard_session_id
                FROM wizard_feedback_event fe
                LEFT JOIN wizard_test_runs wtr
                    ON wtr.id = fe.wizard_test_run_id
                WHERE fe.tenant_id = :tenant_id
                  AND fe.created_at >= :cutoff
                ORDER BY fe.created_at DESC
            """),
            {"tenant_id": tenant_id, "cutoff": cutoff},
        ).fetchall()

        if not rows:
            return []

        feedback_ids = [int(r[0]) for r in rows]

        # ── Step 2: fetch proposal vectors from vector DB ──
        vectors = _fetch_proposal_vectors(db, feedback_ids)

        # ── Step 3: compute similarity + filter ──
        scored: List[Any] = []
        for r in rows:
            fe_id = int(r[0])
            vec = vectors.get(fe_id, [])
            sim = _cosine_similarity(query_vec, vec)
            if sim < SIMILARITY_THRESHOLD:
                continue
            scored.append({
                "feedback_event_id": fe_id,
                "action": r[1],
                "was_edited": bool(r[2]),
                "proposal_id": int(r[3]),
                "similarity": sim,
                "test_run_id": int(r[5]) if r[5] else None,
                "workflow_run_id": int(r[6]) if r[6] else None,
                "session_id": r[7],
            })

        # ── Step 4: sort + trim ──
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        top = scored[:k]

        # ── Step 5: enrich with session data ──
        session_ids = [s["session_id"] for s in top if s["session_id"]]
        sessions: Dict[Optional[str], Dict[str, str]] = {}
        if session_ids:
            # tenant_id filter is defense-in-depth here: session_ids are already
            # scoped to this tenant via the fe.tenant_id join above, but the
            # session table itself is independently tenant-owned, so this query
            # must never trust the id list alone to avoid cross-tenant leakage.
            sess_rows = db.execute(
                _sa_text("""
                    SELECT id,
                           (SELECT content FROM support_chat_messages
                            WHERE session_id = s.id AND role = 'user'
                            ORDER BY created_at ASC LIMIT 1) AS first_user_prompt
                    FROM support_chat_sessions s
                    WHERE s.id = ANY(:sids) AND s.tenant_id = :tenant_id
                """),
                {"sids": session_ids, "tenant_id": tenant_id},
            ).fetchall()
            # session.id is String(36) UUID — keep as string key
            sessions = {r[0]: {"prompt": r[1]} for r in sess_rows}

        results: List[ReflexionSummary] = []
        for s in top:
            sid = s["session_id"]
            sess = sessions.get(sid, {})
            results.append(ReflexionSummary(
                proposal_id=s["proposal_id"],
                action=s["action"],
                was_edited=s["was_edited"],
                similarity=s["similarity"],
                session_id=sid,
                user_prompt=sess.get("prompt"),
                workflow_run_id=s["workflow_run_id"],
            ))

        return results
    finally:
        _elapsed = time.perf_counter() - _start
        try:
            expressiveness_metrics.observe_reflexion_retrieval_duration(_elapsed)
        except Exception:  # noqa: BLE001 — metric failure must never change behaviour
            pass


# ---------------------------------------------------------------------
# Platform-settings gate: reflexion retrieval
# is opt-in and OFF by default. A settings-read failure must keep it OFF
# (fail closed), same shape as services.auto_build_settings.
# ---------------------------------------------------------------------

ENABLED_KEY = "wizard.reflexion_enabled"
GROUP = "wizard"


def reflexion_enabled(db: Session, tenant_id: Optional[int] = None) -> bool:
    """True when the Builder prompt may be augmented with past-session
    reflexion summaries for `tenant_id`.

    Tenant override > platform row > registered default (False). A
    settings-read failure keeps the flag OFF — reflexion retrieval must
    never become a hard dependency of the wizard turn.
    """
    try:
        return bool(psr.get_setting(ENABLED_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — fail closed (off), never open
        logger.warning("reflexion: enabled-read failed; keeping retrieval OFF")
        return False


def _register_settings() -> None:
    psr.register(
        ENABLED_KEY,
        bool,
        False,
        group=GROUP,
        description="Gates whether the Builder prompt "
                    "is augmented with prior-session reflexion summaries "
                    "(services.reflexion.retrieve_reflexion) before calling "
                    "the wizard LLM. Opt-in: default False at first deploy, "
                    "so the Builder prompt is unchanged until an operator "
                    "turns it on. Per-tenant overrides win over the "
                    "platform row.",
    )


_register_settings()
