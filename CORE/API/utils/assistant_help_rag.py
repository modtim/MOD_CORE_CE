"""MOD Assistant — help RAG retrieval.

Thin wrapper over the existing support-chat docs RAG
(utils.docs_ingester). The assistant orchestrator needs a retrieval
surface shaped as [{title, url, snippet, score}]; the support-chat
pipeline already has the docs ingested into Qdrant with the same
docs-site corpus the assistant cares about, so this module's job
is purely adaptation.

Why not the spec'd parallel redis-stack pipeline:
  Doc set, embedding model, and refresh cadence already match what
  the spec asks for. Building a second pipeline would duplicate ops
  surface (a second vector store, a second build hook, a second GC
  sweep) without functional gain. If the assistant ever wants a
  different doc scope or model than support-chat, this module is
  the seam where we swap — the orchestrator never sees Qdrant
  directly.

Public API:
  retrieve(query: str, k: int = 5, min_score: float = 0.3)
      -> List[{title, url, snippet, score}]
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from utils import docs_ingester

logger = logging.getLogger(__name__)

# Snippet length cap. Bumped 2026-06-04 (300 → 800) so glossary
# entries — typically 200–400 chars of definition + "Related" links —
# arrive at the model in full instead of truncated to the H2 header.
# Pre-fix the LLM only saw the doc preamble for hits whose chunk was
# the doc head, and punted users to a "go look at the docs" URL.
_DEFAULT_SNIPPET_CHARS = 800


def _is_ce_edition() -> bool:
    """True on the CE (open-source) carve. Lazy import, same pattern as
    permissions._ce_edition: this module stays import-light and the edition
    predicate is resolved at call time. May raise - the ONLY caller guards it
    (fail-to-paid)."""
    from utils.paid_import import is_ce_edition
    return bool(is_ce_edition())


def _docs_host_from_base(base: str) -> str:
    """BASE_DOMAIN with any `:port` stripped, mirroring the split
    main._cors_origins() does (`host, _, port = BASE_DOMAIN.partition(":")`,
    main.py:1657) so a port-carrying CE LAN BASE_DOMAIN (`203.0.113.150:3000`)
    yields the bare host. A bracketed IPv6 literal (`[fe80::1]`, `[fe80::1]:3000`)
    keeps its brackets so the composed URL stays resolvable."""
    if base.startswith("["):
        end = base.find("]")
        return base if end == -1 else base[:end + 1]
    return base.partition(":")[0]


def _docs_base_url() -> str:
    """Absolute URL prefix for docs-site citations. Citation hrefs were
    previously relative ("/glossary") which made the panel render
    `<a target="_blank" href="/glossary">` — that resolves to the
    CURRENT host (Dashboard or WorkflowBuilder subdomain) and 404s
    against the wrong app. Build the full docs URL up front so
    citations land on the public docs site regardless of which
    subdomain the user is sitting on when they click."""
    proto = os.getenv("PROTOCOL", "https").strip() or "https"
    sub = os.getenv("DOCS_SUBDOMAIN", "docs").strip() or "docs"
    base = os.getenv("BASE_DOMAIN", "").strip()
    if not base:
        # On-prem deploys with no BASE_DOMAIN set fall back to relative
        # URLs — the panel will resolve them against the docs sibling
        # container. Acceptable degradation.
        return ""
    try:
        ce = _is_ce_edition()
    except Exception:  # fail-to-paid: a broken edition probe keeps paid URLs
        ce = False
    if ce:
        # FIX-CE-ASSISTANT-HELP-RAG-DOCS-URL (11-11): CE is a single-host
        # install - the docs site is NOT a vhost there, mod-docs is published
        # on the plain host port 3002 (open-core/overlay/docker-compose.yml,
        # `"${CE_BIND_ADDR:-127.0.0.1}:3002:80"`), and the CE api block passes
        # no DOCS_SUBDOMAIN default, so the `docs.<base>` form shipped a dead
        # host (`http://docs.localhost`) to every assistant citation. Same
        # rule CORE/Dashboard/inject-config.sh now uses for Help/docs links.
        # BASE_DOMAIN may carry a port on a CE LAN install (`203.0.113.150:3000`)
        # -> use its host part, the way main._cors_origins() splits it.
        # NOTE: user-set DOCS_SUBDOMAIN on CE selects a vhost CE has no nginx
        # for - kept as-is on the branch below, documented, not changed.
        if not os.getenv("DOCS_SUBDOMAIN", "").strip():
            return f"{proto}://{_docs_host_from_base(base)}:3002"
    return f"{proto}://{sub}.{base}"


def _absolutize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url  # already absolute
    base = _docs_base_url()
    if not base:
        return url
    if not url.startswith("/"):
        url = "/" + url
    return base + url


def _truncate(text: str, max_chars: int) -> str:
    """Hard-cap at max_chars without crossing a word boundary mid-word."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Walk back to the last whitespace so we don't end mid-word.
    last_space = cut.rfind(" ")
    if last_space > max_chars - 60:  # only walk back if the gain is small
        cut = cut[:last_space]
    return cut.rstrip() + "..."


def retrieve(
    query: str,
    k: int = 5,
    min_score: float = 0.2,
    truncate_to: int = _DEFAULT_SNIPPET_CHARS,
) -> List[Dict[str, Any]]:
    """Retrieve up to k docs passages relevant to query.

    Args:
        query: natural-language question from the assistant turn.
        k: max results to return (default 5).
        min_score: drop hits below this similarity score so the
            orchestrator doesn't pass low-confidence noise into the
            model context. Default 0.2 (lowered 2026-06-04 from 0.3)
            balances typo tolerance — MiniLM is character-sensitive,
            so "tennent" scores ~0.08 vs "tenant" at ~0.55, and the
            old 0.3 floor turned every misspelling into "no docs
            context, punt the user". Tune up (0.4+) only if
            hallucinated citations become an issue.
        truncate_to: per-snippet char cap (default 300). The
            orchestrator can ask for shorter or longer per call;
            longer helps for "explain X in detail" turns.

    Returns:
        List of {title, url, snippet, score} dicts ordered by score
        descending. Empty list when retrieval is unavailable (docs
        not ingested, Qdrant unreachable, embedding model missing)
        OR when no hit clears min_score.
    """
    if not query or not query.strip():
        return []

    try:
        raw_hits = docs_ingester.search_docs(query.strip(), top_k=k)
    except Exception as e:
        # search_docs already swallows its own qdrant errors and
        # returns []; this catch is the belt for unexpected
        # bugs (model load failure, etc.). Never let a RAG
        # retrieval crash propagate out of an assistant turn.
        logger.warning("assistant_help_rag.retrieve: search_docs raised: %s", e)
        return []

    results: List[Dict[str, Any]] = []
    for hit in raw_hits:
        score = float(hit.get("score") or 0.0)
        if score < min_score:
            continue
        results.append({
            "title": hit.get("title") or "Untitled",
            "url": _absolutize_url(hit.get("page_url") or ""),
            "snippet": _truncate(hit.get("text") or "", truncate_to),
            "score": score,
        })

    return results
