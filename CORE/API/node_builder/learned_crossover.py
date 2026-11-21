"""human-assisted-discovery — the learned-crossover read/record helper.

The curated crossover map (node_builder/jargon_map.py:crossover_for) is
hand-authored. This module is its LEARNED sibling: when the describe-search
verdict is WEAK the wizard asks the user for a library / GitHub repo / technical
term; the parsed answer is recorded here keyed to the description's arctic-embed
embedding, and a later near-identical description retrieves it by cosine
similarity — so the platform stops asking the same question twice.

Both functions are best-effort and NEVER raise: a learning store that can break
the search path is worse than no learning store. On any failure `record_*` is a
no-op and `learned_crossover_for` returns `{}`.

Storage is JSONB embeddings + cosine-in-Python (NO pgvector) — mirroring
node_builder/rerank.py's `_cosine`. arctic-embed vectors are unit-normalised by
the embedding client, so the dot product IS the cosine.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity of two arctic-embed vectors. Both are unit-normalised
    by the embedding client, so the dot product IS the cosine. Guards against a
    length / None mismatch (returns 0.0). Mirrors node_builder/rerank._cosine."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def _embed(text: str) -> List[float]:
    """Embed `text` via the wizard Pro sidecar client. Raises when the
    sidecar is absent/unreachable (CE fallback) so callers' existing
    best-effort try/except skips the vector path."""
    from services.wizard_pro_client import embed
    vec = embed(text or "")
    if vec is None:
        raise RuntimeError("wizard_pro embed unavailable")
    return vec


def _as_list(v: Any) -> List[str]:
    """Coerce a JSONB payload column to a list[str], tolerating None / non-list
    stored shapes so a malformed row never breaks a merge."""
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    return [str(v)]


def record_learned_crossover(
    db,
    *,
    tenant_id: int,
    description: str,
    jargon: List[str],
    github: List[str],
    pypi: List[str],
    created_by,
) -> None:
    """Embed `description` and store a WizardLearnedCrossover row for `tenant_id`.

    Best-effort: any failure (embedding unavailable, DB error) is swallowed —
    the caller's search must complete regardless. Rolls back the session on
    error so a poisoned transaction doesn't leak into the caller.
    """
    if not description or not str(description).strip():
        return
    try:
        import models

        vec = _embed(description)
        row = models.WizardLearnedCrossover(
            tenant_id=tenant_id,
            description=description,
            embedding=vec,
            jargon=list(jargon or []),
            github=list(github or []),
            pypi=list(pypi or []),
            approved_cross_tenant=False,
            created_by=created_by,
            hit_count=0,
        )
        db.add(row)
        db.commit()
    except Exception as e:  # noqa: BLE001 — learning must never break search
        logger.info("record_learned_crossover skipped (%s)", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def learned_crossover_for(
    db,
    description: str,
    *,
    tenant_id: int,
    threshold: float = 0.80,
) -> Dict[str, List[str]]:
    """Merged {jargon, github, pypi} learned from rows whose stored description
    embedding is within `threshold` cosine of `description`.

    Candidate rows: only this tenant's own rows. Cross-tenant promotion is
    disabled (GB.LEAK.1). `hit_count` is bumped on every row that clears the
    threshold. Returns `{}` (falsy, same shape as an empty merge) on ANY
    failure — best-effort, never raises.
    """
    if not description or not str(description).strip():
        return {}
    try:
        import models

        qvec = _embed(description)
        if not qvec:
            return {}

        rows = (
            db.query(models.WizardLearnedCrossover)
            .filter(
                models.WizardLearnedCrossover.tenant_id == tenant_id,
            )
            .all()
        )

        out: Dict[str, List[str]] = {"jargon": [], "github": [], "pypi": []}
        matched = False
        for row in rows:
            sim = _cosine(qvec, row.embedding or [])
            if sim < threshold:
                continue
            matched = True
            for term in _as_list(row.jargon):
                if term not in out["jargon"]:
                    out["jargon"].append(term)
            for repo in _as_list(row.github):
                if repo not in out["github"]:
                    out["github"].append(repo)
            for pkg in _as_list(row.pypi):
                if pkg not in out["pypi"]:
                    out["pypi"].append(pkg)
            try:
                row.hit_count = (row.hit_count or 0) + 1
            except Exception:  # noqa: BLE001
                pass

        if not matched:
            return {}
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return out
    except Exception as e:  # noqa: BLE001 — retrieval must never break search
        logger.info("learned_crossover_for skipped (%s)", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {}
