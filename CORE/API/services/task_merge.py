# CORE/API/services/task_merge.py
"""
Group L / 20.5 — `Task merge` node: match extracted tasks against existing
issues -> new issue / sub-issue / comment on an existing one.
=============================================================

The `Task extract` node (20.4) hands us structured tasks
``{"title", "description", "source"}``; the Plane connector nodes (20.3) do the
writing. What sits between them is THIS decision: for every extracted task,
look at the issues that already exist and answer one question — is this task
already on the board?

A meeting that reopens an old topic must NOT file a second issue for it. MOD
scores each (task, issue) pair as::

    score = max(title similarity, embedding cosine when an embed callable exists)

and picks the best-scoring existing issue:

  score >= task_merge.dup_threshold      -> "comment" on that issue
                                            (a repeat: add the new detail to
                                            the issue that already exists)
  score >= task_merge.related_threshold  -> "sub_issue" under that issue
                                            (same topic, new work item)
  otherwise                              -> "new_issue"

The repeat therefore always lands as a sub-task or a comment — never a
duplicate issue — which is exactly the 20.5 check.

Everything here is PURE: no HTTP, no Plane, no DB tables. The embed callable is
injected by the caller (a node container passes its model's embedder; tests pass
a deterministic fake), so the suite runs offline. Tunables are DB platform
settings registered HERE — services/platform_settings.py is off-limits to this
lane; the pattern follows utils/label_policy.py. No env vars govern behaviour.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Sequence
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

SETTINGS_GROUP = "task_merge"

DUP_SETTING_KEY = "task_merge.dup_threshold"
RELATED_SETTING_KEY = "task_merge.related_threshold"
MAX_CANDIDATES_SETTING_KEY = "task_merge.max_candidates"

import services.platform_settings as _psr_reg  # noqa: E402

_psr_reg.register(
    DUP_SETTING_KEY, float, 0.82, group=SETTINGS_GROUP,
    description="20.5 — similarity at or above which an extracted task is the "
                "SAME task as an existing issue, so it becomes a COMMENT on it "
                "instead of a new issue. Read via "
                "services.task_merge.dup_threshold.",
)
_psr_reg.register(
    RELATED_SETTING_KEY, float, 0.55, group=SETTINGS_GROUP,
    description="20.5 — similarity at or above which an extracted task is "
                "related work on an existing issue, so it becomes a SUB-ISSUE "
                "under it. Below this it is a NEW issue. Must stay <= "
                "task_merge.dup_threshold (services.task_merge thresholds "
                "clamps it).",
)
_psr_reg.register(
    MAX_CANDIDATES_SETTING_KEY, int, 200, group=SETTINGS_GROUP,
    description="20.5 — how many existing issues one extracted task is scored "
                "against (the widest fan the merge node will consider). "
                "Read via services.task_merge.max_candidates.",
)


def dup_threshold(db=None, tenant_id: Optional[int] = None) -> float:
    return float(_psr_reg.get_setting(DUP_SETTING_KEY, db=db, tenant_id=tenant_id))


def related_threshold(db=None, tenant_id: Optional[int] = None) -> float:
    return float(_psr_reg.get_setting(RELATED_SETTING_KEY, db=db, tenant_id=tenant_id))


def max_candidates(db=None, tenant_id: Optional[int] = None) -> int:
    return int(_psr_reg.get_setting(MAX_CANDIDATES_SETTING_KEY, db=db, tenant_id=tenant_id))


def thresholds(db=None, tenant_id: Optional[int] = None) -> tuple:
    """(dup, related) with related clamped to <= dup.

    A mis-tuned registry (related above dup) would make every comment also a
    sub-issue; clamping keeps the ladder ordered instead of crashing the node.
    """
    dup = dup_threshold(db, tenant_id)
    related = related_threshold(db, tenant_id)
    return dup, min(related, dup)


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _norm(text: Any) -> str:
    """Lower-case, collapse whitespace, drop punctuation-ish noise."""
    s = str(text or "").lower()
    out = []
    for ch in s:
        out.append(" " if (not ch.isalnum() and ch not in " _-") else ch)
    return " ".join("".join(out).split())


def _tokens(text: Any) -> set:
    return {t for t in _norm(text).replace("_", " ").replace("-", " ").split() if t}


def title_similarity(a: str, b: str) -> float:
    """Title-only similarity in [0,1]: token-Jaccard blended with difflib ratio.

    Token-Jaccard alone gives 0 for "Fix pump 7" vs "Fix pump 7 valve" style
    near-misses with different word counts, and difflib alone is case/word-order
    sensitive; the max of the two is forgiving in both directions.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    jaccard = 0.0
    if ta or tb:
        jaccard = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    return max(jaccard, ratio)


def cosine(u: Sequence[float], v: Sequence[float]) -> float:
    """Cosine similarity of two vectors; 0.0 when either is a zero vector."""
    if not u or not v or len(u) != len(v):
        return 0.0
    dot = nb = na = 0.0
    for a, b in zip(u, v):
        dot += a * b
        na += a * a
        nb += b * b
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def pair_score(task: Dict[str, Any], issue: Dict[str, Any],
               embed: Optional[Callable[[str], Sequence[float]]] = None,
               task_vec: Optional[Sequence[float]] = None) -> float:
    """Best of title similarity and embedding cosine for one (task, issue) pair.

    ``task_vec`` lets a caller that already embedded the task text reuse it
    across the whole candidate fan (one embed per task, not one per pair).
    """
    score = title_similarity(task.get("title", ""), issue.get("title", ""))
    if embed is None and task_vec is None:
        return score
    left = task_vec
    if left is None:
        left = embed(_task_text(task))
    right = embed(issue_text(issue)) if embed is not None else None
    if left and right:
        score = max(score, cosine(left, right))
    return score


def _task_text(task: Dict[str, Any]) -> str:
    return " ".join(str(task.get(k) or "") for k in ("title", "description", "source"))


def issue_text(issue: Dict[str, Any]) -> str:
    return " ".join(str(issue.get(k) or "") for k in ("title", "body"))


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

ACTION_NEW_ISSUE = "new_issue"
ACTION_SUB_ISSUE = "sub_issue"
ACTION_COMMENT = "comment"


def decide(task: Dict[str, Any], issues: List[Dict[str, Any]],
           embed: Optional[Callable[[str], Sequence[float]]] = None,
           db=None, tenant_id: Optional[int] = None,
           task_vec: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """One extracted task + the existing issues -> the merge verdict.

    Returns ``{"action", "issue_id", "parent_id", "score", "method"}``:
    ``issue_id`` is the existing issue the task matched (None for a new issue),
    ``parent_id`` is that issue for a sub-issue (None otherwise).
    """
    dup, related = thresholds(db, tenant_id)
    issues = list(issues)[:max_candidates(db, tenant_id)]
    best_id: Optional[int] = None
    best_score = 0.0
    best_method = "title"
    for issue in issues:
        title = title_similarity(task.get("title", ""), issue.get("title", ""))
        score = pair_score(task, issue, embed=embed, task_vec=task_vec)
        if score < related:
            continue
        issue_id = _issue_id(issue)
        if issue_id is None:
            continue
        if score > best_score:
            best_id, best_score = issue_id, score
            best_method = "embedding" if score > title else "title"
    if best_id is None:
        return {"action": ACTION_NEW_ISSUE, "issue_id": None, "parent_id": None,
                "score": round(best_score, 6), "method": "title"}
    if best_score >= dup:
        return {"action": ACTION_COMMENT, "issue_id": best_id, "parent_id": None,
                "score": round(best_score, 6), "method": best_method}
    return {"action": ACTION_SUB_ISSUE, "issue_id": best_id, "parent_id": best_id,
            "score": round(best_score, 6), "method": best_method}


def _issue_id(issue: Dict[str, Any]) -> Optional[int]:
    raw = issue.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def plan(tasks: List[Dict[str, Any]], issues: List[Dict[str, Any]],
         embed: Optional[Callable[[str], Sequence[float]]] = None,
         db=None, tenant_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Merge a whole batch. Deterministic; the batch grows as it merges.

    Verdicts are returned in input order. A task merged as a sub-issue becomes
    a candidate for the tasks that follow it, so three near-identical extracts
    collapse onto one issue instead of filing three.
    """
    cap = max_candidates(db, tenant_id)
    pool: List[Dict[str, Any]] = [dict(i) for i in issues][:cap]
    created: Dict[int, int] = {}   # placeholder issue_id -> task index that made it
    out: List[Dict[str, Any]] = []
    for idx, task in enumerate(tasks):
        task_vec = embed(_task_text(task)) if embed is not None else None
        verdict = decide(task, pool, embed=embed, db=db, tenant_id=tenant_id,
                         task_vec=task_vec)
        if verdict["action"] != ACTION_COMMENT:
            # The issue this task just put on the board becomes a candidate for
            # the tasks that follow, under a negative placeholder id (the real
            # Plane id is unknown until 20.3 writes it). created_from names the
            # batch task that owns it, so a later verdict that lands on a
            # placeholder can be re-pointed at the real issue after the write.
            placeholder = -(len(created) + 1)
            created[placeholder] = idx
            pool.append({"id": placeholder, "title": task.get("title", ""),
                         "body": task.get("description", ""),
                         "parent_id": verdict["issue_id"]})
            verdict["created_issue_id"] = placeholder
        if verdict["issue_id"] in created:
            verdict["created_from"] = created[verdict["issue_id"]]
        out.append({**verdict, "task": task})
    return out
