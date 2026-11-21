"""Snap a Dockerfile's hero FROM line to a known-canonical curated base.

The wizard's LLM is told the curated allowlist but occasionally emits
a plausible-looking tag that does not exist in Harbor (e.g.
``base-cuda:12.4`` instead of ``base-cuda:12.4-python3.11``). When the
FROM repo matches a curated base-* repo unambiguously, rewrite to the
canonical full ref so the hero build does not 404 at FROM.

Variants are protected separately by gpu_variants.synthesise_variant —
this module is the equivalent safety net for the hero Dockerfile.

The canonical list is supplied by the API at task-enqueue time as the
``CURATED_BASE_IMAGES_JSON`` environment variable (a JSON list of full
image refs). When unset or empty, snapping is a no-op so the
Containerizer keeps working in dev environments where the env hasn't
been wired up.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, List, Optional, Tuple


_FROM_RE = re.compile(r"^(?P<prefix>FROM\s+)(?P<ref>\S+)", re.MULTILINE)


def _load_refs_from_env(env_value: Optional[str]) -> List[str]:
    if not env_value:
        return []
    try:
        data = json.loads(env_value)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in data if isinstance(x, str) and x.strip()]


def _index_by_repo(refs: Iterable[str]) -> dict:
    by_repo: dict = {}
    for ref in refs:
        repo = ref.split(":", 1)[0]
        by_repo.setdefault(repo, []).append(ref)
    return by_repo


def snap_to_canonical(
    dockerfile: str,
    canonical_refs: Optional[List[str]] = None,
) -> Tuple[str, bool]:
    """Rewrite the first FROM in *dockerfile* to a canonical ref when the
    current ref's repo matches exactly one curated entry.

    Returns ``(new_dockerfile, was_snapped)``.

    Behaviour:
      - FROM matches a canonical ref exactly         → unchanged, False
      - FROM repo has 1 canonical tag, current ≠ it  → rewritten, True
      - FROM repo has >1 canonical tag               → unchanged, False
        (ambiguous; the LLM has to pick correctly via the prompt)
      - FROM repo not in curated list                → unchanged, False
      - No FROM directive                            → unchanged, False
      - canonical_refs unset/empty                   → unchanged, False
    """
    if canonical_refs is None:
        canonical_refs = _load_refs_from_env(
            os.getenv("CURATED_BASE_IMAGES_JSON")
        )
    if not canonical_refs:
        return dockerfile, False

    m = _FROM_RE.search(dockerfile)
    if not m:
        return dockerfile, False

    current = m.group("ref")
    if current in canonical_refs:
        return dockerfile, False

    current_repo = current.split(":", 1)[0]
    matches = _index_by_repo(canonical_refs).get(current_repo, [])
    if len(matches) != 1:
        return dockerfile, False

    canonical = matches[0]
    new_df = (
        dockerfile[: m.start("ref")]
        + canonical
        + dockerfile[m.end("ref") :]
    )
    return new_df, True
