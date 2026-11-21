"""Web query-expansion for the node wizard's discovery.

The candidate POOL is the ceiling on discovery quality: the GitHub/PyPI search is
built from a small local model's keywords, and the embedding reranker can only
reorder what was fetched. So a genuinely-relevant repo that uses domain JARGON
the user didn't say ("photogrammetry" for "make a 3d model from photos",
"speech recognition" for "transcribe audio") is never fetched, and no reranker
can save it.

This module bridges that gap the way a human would: it runs a WEB SEARCH on the
plain-English description and mines the results for (a) domain jargon terms, (b)
direct github.com repos and pypi.org packages the web recommends. Those feed back
into the GitHub/PyPI search (widening the pool) and can seed candidates directly.

Keyless by design — DuckDuckGo's HTML endpoint needs no API key (a Google Custom
Search / Brave provider can be slotted in later behind the same interface). It is
best-effort: any failure returns an empty expansion and the caller proceeds with
its existing keywords, so the wizard never breaks because the web was
unreachable.
"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

_DDG_HTML = "https://html.duckduckgo.com/html/"
_WIKI_API = "https://en.wikipedia.org/w/api.php"
# A descriptive UA — DuckDuckGo (like Wikipedia) 403s the default requests agent.
_UA = "MODCore-node-wizard/1.0 (+https://example.com)"
_TIMEOUT_S = 10
_MAX_RESULTS = 12
# Default number of {title,url,snippet} results returned to the web-search node
# (the container's TOP_K env, string-coerced, overrides this).
_DEFAULT_TOP_K = 5

_GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I)
# A GitHub gist path is github.com/<user>/<long-hex-id> — not a usable repo.
_GIST_ID_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
_PYPI_RE = re.compile(r"pypi\.org/project/([A-Za-z0-9_.-]+)", re.I)
# DuckDuckGo-HTML result anchor + snippet classes. Kept liberal — DDG tweaks its
# markup, so we also fall back to a bare tag-strip of the whole doc for terms.
_RESULT_A_RE = re.compile(r'result__a[^>]*>(.*?)</a>', re.I | re.S)
_SNIPPET_RE = re.compile(r'result__snippet[^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# Full result anchor (attrs + inner text) so we can also recover its href for the
# results[] list. Kept separate from _RESULT_A_RE (which only mines inner text).
_RESULT_ANCHOR_RE = re.compile(r'<a\b([^>]*result__a[^>]*)>(.*?)</a>', re.I | re.S)
_HREF_RE = re.compile(r'href="([^"]*)"', re.I)

# github paths that are NOT repos (topic/marketplace/sponsors/etc.) — never
# treat these as candidate repos.
_GITHUB_NON_REPO = {
    "topics", "features", "about", "pricing", "marketplace", "sponsors",
    "collections", "trending", "login", "join", "settings", "search",
    "explore", "orgs", "apps", "notifications",
}

_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "and", "or", "with", "from", "into",
    "in", "on", "that", "this", "it", "is", "are", "be", "as", "by", "at",
    "how", "you", "your", "can", "will", "use", "using", "best", "free", "open",
    "source", "opensource", "open-source", "library", "libraries", "tool",
    "tools", "software", "python", "github", "com", "www", "https", "http",
    "convert", "create", "generate", "make", "get", "top", "list", "code",
    "project", "projects", "guide", "tutorial", "example", "examples", "api",
    "apis", "app", "online", "vs", "what", "which", "why", "when", "new",
    "2024", "2025", "2026", "review", "compare", "comparison", "accuracy",
    "speed", "fast", "easy", "simple", "download", "install", "based", "into",
    "reddit", "stack", "overflow", "medium", "blog", "docs", "documentation",
    # web-chrome / marketing filler seen in result snippets (demoted anyway by
    # the distinctiveness score, but drop the obvious ones outright).
    "files", "file", "windows", "mac", "macos", "linux", "upload", "uploads",
    "click", "account", "pricing", "credits", "powered", "seconds", "minutes",
    "turn", "any", "your", "our", "their", "free", "premium", "trial",
}

# Repo names that are aggregators/placeholders, not tools.
_JUNK_REPO_NAMES = {
    "open_source", "opensource", "open-source", "projects", "project",
    "list", "lists", "tutorials", "tutorial", "examples", "example",
    "demo", "demos", "test", "tests", "sample", "samples", "code",
    "resources", "collection", "awesome", "docs", "documentation",
}


@dataclass
class WebExpansion:
    """What the web told us about the user's request."""
    keywords: List[str] = field(default_factory=list)      # domain jargon to widen the search
    github_repos: List[str] = field(default_factory=list)  # "owner/repo" the web named
    pypi_packages: List[str] = field(default_factory=list)  # pypi projects the web named
    results: List[Dict[str, str]] = field(default_factory=list)  # top {title,url,snippet}
    ok: bool = False                                        # did the web search actually run

    def as_dict(self) -> dict:
        return {
            "keywords": list(self.keywords),
            "github_repos": list(self.github_repos),
            "pypi_packages": list(self.pypi_packages),
            "results": [dict(r) for r in self.results],
            "ok": self.ok,
        }

    def as_node_artifact(self) -> dict:
        """The web-search NODE's output artifact JSON (add-web-search-node).

        Same data as :meth:`as_dict` but keyed to the node's OUTPUT contract:
        ``jargon`` (not ``keywords``) plus ``results`` first."""
        return {
            "results": [dict(r) for r in self.results],
            "github_repos": list(self.github_repos),
            "pypi_packages": list(self.pypi_packages),
            "jargon": list(self.keywords),
            "ok": self.ok,
        }


def _search_html_raw(query: str) -> str:
    """One DuckDuckGo-HTML fetch for an EXACT query. Returns the unescaped HTML
    only on a substantive 200, else '' (a 202 bot-challenge or a stripped page
    is treated as no result). Never raises."""
    try:
        r = requests.get(
            _DDG_HTML, params={"q": query},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; caller proceeds
        logger.info("web_expand: DDG request failed for %r: %s", query[:50], exc)
        return ""
    # 202 = DDG bot-challenge/rate-limit; a real result page is 200 and big.
    if r.status_code == 200 and len(r.text or "") > 8000:
        return html.unescape(r.text)
    logger.info("web_expand: DDG %s (%db) for %r",
                r.status_code, len(r.text or ""), query[:50])
    return ""


def _search_html(description: str) -> str:
    """Best DuckDuckGo-HTML page for `description`, or '' on failure.

    DDG returns HTTP 202 with a stripped page for queries it deems bot-like — a
    long natural-language prefix ("open source tool or library to …") trips it,
    while a short keyword-shaped query ("<desc> open source") returns 200. So we
    try a few keyword-shaped variants and take the first substantive 200. This is
    the difference between the photogrammetry bridge working and returning
    nothing (measured 2026-08-29)."""
    for q in (f"{description} open source",
              f"{description} open source library",
              description):
        doc = _search_html_raw(q)
        if doc:
            return doc
    return ""


def _clean_repo(full_name: str) -> str:
    fn = full_name.strip().strip("/")
    # Drop a trailing ".git" and any path past owner/repo.
    parts = fn.split("/")
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.lower().endswith(".git") else repo
    if owner.lower() in _GITHUB_NON_REPO or not repo:
        return ""
    # Generic/aggregator repo names carry no tool signal (e.g. someone's repo
    # literally named "open_source", or an awesome-list). Drop them — they're the
    # same junk the reranker fights downstream.
    if repo.lower() in _JUNK_REPO_NAMES or repo.lower().startswith("awesome"):
        return ""
    # A long hex string is a GitHub GIST id (github.com/<user>/<32-hex>), not a
    # usable repo — drop it.
    if _GIST_ID_RE.match(repo):
        return ""
    return f"{owner}/{repo}"


def _clean_result_url(href: str) -> str:
    """The real destination URL behind a DDG-HTML result anchor.

    DDG wraps hits in a redirect (``//duckduckgo.com/l/?uddg=<url-encoded>&rut=…``);
    unwrap the ``uddg`` param when present. Protocol-relative ``//host/…`` links
    get an ``https:`` prefix so the returned URL is always absolute."""
    href = html.unescape((href or "").strip())
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def _extract_results(doc: str, top_k: int) -> List[Dict[str, str]]:
    """Top ``top_k`` search hits as ``{title, url, snippet}`` dicts.

    Titles come from the result anchors, snippets from the parallel
    ``result__snippet`` blocks (zipped by position — DDG emits one of each per
    hit). Best-effort: a hit with neither title nor url is skipped, and a bad
    ``top_k`` yields an empty list rather than raising."""
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        return []
    if k <= 0:
        return []
    anchors = _RESULT_ANCHOR_RE.findall(doc)
    snippets = _SNIPPET_RE.findall(doc)
    results: List[Dict[str, str]] = []
    for i, (attrs, inner) in enumerate(anchors):
        if len(results) >= k:
            break
        href_m = _HREF_RE.search(attrs)
        url = _clean_result_url(href_m.group(1)) if href_m else ""
        title = _TAG_RE.sub("", inner).strip()
        snippet = ""
        if i < len(snippets):
            snippet = _TAG_RE.sub("", snippets[i]).strip()
        if not title and not url:
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


# Marketing compound adjectives that leak from result snippets/titles — never a
# useful search term. Exact set + a suffix rule for the long tail.
_MARKETING = {
    "production-ready", "production-grade", "high-performance", "industry-standard",
    "state-of-the-art", "user-friendly", "developer-friendly", "commercially-friendly",
    "privacy-focused", "community-driven", "watermark-free", "enterprise-grade",
    "cutting-edge", "easy-to-use", "cross-platform", "lightning-fast", "battle-tested",
    "fully-featured", "feature-rich", "well-documented", "self-hosted", "open-source",
}
_MARKETING_SUFFIX = ("-ready", "-grade", "-friendly", "-focused", "-driven",
                     "-performance", "-featured", "-documented")
# A jargon term longer than this is almost always a junk repo slug leaking from a
# result title (e.g. "noob2-project-character-reference-bypass-injector-research").
_MAX_TERM_LEN = 24


def _is_marketing(w: str) -> bool:
    return w in _MARKETING or ("-" in w and w.endswith(_MARKETING_SUFFIX))


def _is_query_variant(w: str, query_words: set) -> bool:
    """True if `w` is the same word the user already typed, or a trivial
    plural/singular of it (photo/photos, image/images) — so we don't surface the
    query back as "jargon". Crucially this is EXACT/plural only, NOT a shared
    prefix: "photogrammetry" must survive next to the query word "photos"."""
    for q in query_words:
        if w == q:
            return True
        if w == q + "s" or w + "s" == q:
            return True
        if q.endswith("s") and w == q[:-1]:
            return True
        if w.endswith("s") and w[:-1] == q:
            return True
    return False


def _extract_terms(doc: str, query: str, top_n: int = 6) -> List[str]:
    """Domain terms the results emphasise that the user did NOT already say.

    The point is the JARGON BRIDGE: for "make a 3d model from photos" the results
    say "photogrammetry" — a single, highly distinctive mention that must outrank
    the many generic "image"/"models" hits. So we score by frequency PLUS a
    distinctiveness bonus for longer, rarer domain terms, and keep a term when it
    either recurs (>=2) OR is a distinctive single mention (len>=9). Terms that
    are just the query's own words (or their plural) are dropped."""
    chunks = _RESULT_A_RE.findall(doc) + _SNIPPET_RE.findall(doc)
    text = " ".join(_TAG_RE.sub(" ", c) for c in chunks)
    if len(text) < 40:
        # Markup changed / no structured results — fall back to the whole page.
        text = _TAG_RE.sub(" ", doc)
    query_words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    counts: Counter = Counter()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+\-]{3,}", text):
        lw = w.lower()
        if (lw in _STOPWORDS or lw.isdigit() or len(lw) > _MAX_TERM_LEN
                or _is_marketing(lw) or _is_query_variant(lw, query_words)):
            continue
        counts[lw] += 1

    def _score(term: str, n: int) -> float:
        # Frequency + a length bonus, so one mention of "photogrammetry" (14) can
        # beat several mentions of "image" (5). Bonus starts past 6 chars.
        return n + max(0, len(term) - 6) * 0.6

    ranked = sorted(counts.items(), key=lambda kv: (-_score(*kv), -counts[kv[0]]))
    out = [w for w, n in ranked if n >= 2 or len(w) >= 9]
    return out[:top_n]


_WIKI_TITLE_NOISE = re.compile(
    r"\b(software|app|application|windows|online|converter|tool|free|list of)\b", re.I
)
_WIKI_PARENS = re.compile(r"\s*\([^)]*\)")  # drop "(speech recognition system)" etc.


def _wikipedia_terms(description: str, limit: int = 4) -> List[str]:
    """Concept/jargon terms from Wikipedia's full-text search on the description.

    Wikipedia is a strong, more-lenient (less bot-blocked) concept bridge: the
    top article titles for a colloquial description name the domain concept the
    user did not (e.g. "transcribe audio to text" -> "Whisper (speech recognition
    system)", "convert heic to jpg" -> "High Efficiency Image File Format"). We
    return the cleaned titles as extra jargon candidates, UNIONED with the DDG
    jargon so a good bridge term from EITHER source reaches the per-keyword GitHub
    search. Best-effort: [] on any failure. Titles that are just the query, a
    generic "…software/app", or a "List of …" are dropped."""
    desc = (description or "").strip()
    if not desc:
        return []
    try:
        r = requests.get(
            _WIKI_API,
            params={"action": "query", "list": "search", "srsearch": desc,
                    "srlimit": limit, "format": "json"},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT_S,
        )
        if r.status_code != 200:
            return []
        hits = (r.json().get("query", {}) or {}).get("search", []) or []
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.info("web_expand: wikipedia lookup failed: %s", exc)
        return []
    q_words = {w for w in re.findall(r"[a-z0-9]+", desc.lower()) if len(w) > 2}
    out: List[str] = []
    for h in hits:
        title = (h.get("title") or "").strip()
        if not title or ":" in title:  # skip Category:/Help:/etc.
            continue
        base = _WIKI_PARENS.sub("", title).strip()   # keep the concept, drop the qualifier
        cand = base or title
        low = cand.lower()
        if low in q_words or _WIKI_TITLE_NOISE.search(cand) or cand.lower().startswith("list of"):
            continue
        if cand not in out:
            out.append(cand)
    return out


def _collect_repos(doc: str, into: List[str], seen: set, cap: int) -> None:
    """Append cleaned, deduped github "owner/repo"s found in `doc` to `into`."""
    for m in _GITHUB_RE.findall(doc):
        if len(into) >= cap:
            return
        cleaned = _clean_repo(m)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            into.append(cleaned)


def _collect_pypi(doc: str, into: List[str], seen: set, cap: int) -> None:
    """Append deduped pypi project names found in `doc` to `into`."""
    for m in _PYPI_RE.findall(doc):
        if len(into) >= cap:
            return
        name = m.strip().lower()
        if name and name not in seen:
            seen.add(name)
            into.append(name)


def expand_from_web(
    description: str,
    *,
    max_results: int = _MAX_RESULTS,
    top_k: int = _DEFAULT_TOP_K,
) -> WebExpansion:
    """Mine the web for jargon + named tools that widen the wizard's search pool.

    Also collects the top ``top_k`` search hits as ``{title, url, snippet}`` dicts
    (``WebExpansion.results``) for the web-search node's OUTPUT artifact, in
    addition to the jargon/github/pypi the wizard already uses.

    Best-effort: returns ``WebExpansion(ok=False)`` on any failure so the caller
    proceeds with its existing keywords."""
    desc = (description or "").strip()
    if not desc:
        return WebExpansion()
    # GSEC FX.19 R-042 — wizard internet lookups disabled (sealed mode or
    # egress.wizard_internet_enabled False): skip the DDG + Wikipedia fetches
    # and return an empty expansion; the wizard proceeds with its keywords.
    from utils import egress_policy
    _r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_WIZARD_INTERNET)
    if _r042:
        logger.info("web_expand: lookup disabled by platform setting — %s",
                    _r042)
        return WebExpansion()
    # Two INDEPENDENT keyless sources, unioned (wizard-discovery-jargon-bridge):
    #   1. ONE DuckDuckGo query — jargon from result snippets + any direct
    #      github/pypi links. Single query only: multiple DDG queries trip its
    #      per-IP rate limit (measured — 202s). Repo breadth comes from the
    #      per-keyword AUTHENTICATED GitHub search on the caller side, not more
    #      DDG queries.
    #   2. Wikipedia full-text concept titles — a more-lenient bridge that names
    #      the domain concept the user didn't (whisper, HEIF, …).
    # Either source alone is enough to proceed (ok=True); both dry → not ok.
    doc = _search_html(desc)
    wiki_terms = _wikipedia_terms(desc)
    if not doc and not wiki_terms:
        return WebExpansion()

    repos: List[str] = []
    pypi: List[str] = []
    seen_r: set = set()
    seen_p: set = set()
    if doc:
        _collect_repos(doc, repos, seen_r, max_results)
        _collect_pypi(doc, pypi, seen_p, max_results)

    ddg_jargon = _extract_terms(doc, desc) if doc else []
    results = _extract_results(doc, top_k) if doc else []

    # Union DDG jargon + Wikipedia concept terms (DDG first, dedup case-insensitive)
    # so a good bridge term from EITHER source reaches the per-keyword GitHub search.
    keywords: List[str] = list(ddg_jargon)
    _seen_kw = {k.lower() for k in keywords}
    for w in wiki_terms:
        if w.lower() not in _seen_kw:
            keywords.append(w)
            _seen_kw.add(w.lower())

    exp = WebExpansion(
        keywords=keywords, github_repos=repos, pypi_packages=pypi,
        results=results, ok=True,
    )
    logger.info(
        "web_expand(%r): %d jargon (%d ddg + %d wiki), %d repo(s), %d pypi, %d result(s)",
        desc[:60], len(keywords), len(ddg_jargon), len(wiki_terms),
        len(repos), len(pypi), len(results),
    )
    return exp


def web_search(query: str, *, top_k: int = _DEFAULT_TOP_K) -> dict:
    """Run a web search and return the web-search NODE's OUTPUT artifact JSON.

    Sibling of :func:`expand_from_web` that the ``system-web-search`` container
    (add-web-search-node) reuses: same DDG search + extraction, shaped to the
    node's OUTPUT contract::

        {"results": [{"title","url","snippet"}], "github_repos": [...],
         "pypi_packages": [...], "jargon": [...], "ok": bool}

    Best-effort — never raises; an empty query or any failure yields
    ``ok=False`` with empty lists. ``top_k`` caps ``results`` and is coerced
    from a string/int (the container passes the TOP_K env verbatim)."""
    return expand_from_web(query, top_k=top_k).as_node_artifact()
