"""MOD Assistant site map — load, validate, cache.

The YAML at CORE/API/data/assistant_site_map.yaml is the only place
the chat agent's `navigate` / `open-dialog` / `open-new-tab` actions
can reference. The model can hallucinate a URL all it wants; the
orchestrator only forwards an action whose target is in the parsed
map. That's the grounding guarantee.

Validation runs at startup (raise → API crash, exit non-zero, surface
the diagnostic in the log) so a malformed PR breaks loud rather than
shipping a silently-broken agent. The hot-reload endpoint
(POST /assistant/site-map/reload, platform-admin only) catches the
same exception and returns 400 with the message, leaving the
in-process cache on the prior valid version.

Public API:
  load_and_validate(path) -> SiteMap   raises if invalid
  get_map() -> SiteMap                 raises RuntimeError if not loaded
  reload_from_disk(path) -> SiteMap    catches + re-raises so the
                                        caller can decide how to surface

`get_map()` returns the same SiteMap instance until reload swaps the
module-level reference, so callers that hold a reference for one
request see a consistent snapshot.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Path defaults to the in-repo location; tests / dev can override by
# pointing at a fixture file.
DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "assistant_site_map.yaml",
)

_VALID_APPS = {"dashboard", "workflow_builder"}


class SiteMapError(Exception):
    """Raised when the YAML is malformed or fails cross-checks."""


@dataclass(frozen=True)
class Dialog:
    id: str
    display_name: str
    opens_when_url_matches: Optional[str] = None


@dataclass(frozen=True)
class Tab:
    label: str
    display_name: str


@dataclass(frozen=True)
class Page:
    display_name: str
    url: str
    app: str
    summary: str
    dialogs: List[Dialog] = field(default_factory=list)
    tabs: List[Tab] = field(default_factory=list)
    anchors: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SiteMap:
    pages: List[Page]
    # Pre-built indexes so the orchestrator doesn't re-scan on every
    # validation. Built once at load time.
    url_to_page: Dict[str, Page]
    dialog_id_to_pages: Dict[str, List[Page]]

    def has_url(self, url: str, app: Optional[str] = None) -> bool:
        page = self.url_to_page.get(url)
        if page is None:
            return False
        return app is None or page.app == app

    def page_for_url(self, url: str, app: Optional[str] = None) -> Optional[Page]:
        """Resolve a URL to a Page. When `app` is given, the lookup is
        scoped to that app — important when two apps share a URL
        (Dashboard `/` vs WorkflowBuilder `/`) and the caller knows
        which one the user is on.
        """
        if app is not None:
            for p in self.pages:
                if p.url == url and p.app == app:
                    return p
            return None
        return self.url_to_page.get(url)

    def pages_with_dialog(self, dialog_id: str) -> List[Page]:
        return self.dialog_id_to_pages.get(dialog_id, [])


# Module-level cache + lock. The lock is RLock because reload may be
# triggered from a request thread while another request is reading;
# we want fresh readers to block on the swap (a few microseconds) and
# then read the new map.
_lock = RLock()
_current: Optional[SiteMap] = None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SiteMapError(message)


def _parse_dialog(raw: dict, page_label: str, idx: int) -> Dialog:
    where = f"page '{page_label}' dialogs[{idx}]"
    _require(isinstance(raw, dict), f"{where}: not a mapping")
    _require("id" in raw and isinstance(raw["id"], str) and raw["id"], f"{where}: missing or empty 'id'")
    _require("display_name" in raw and isinstance(raw["display_name"], str) and raw["display_name"],
             f"{where}: missing or empty 'display_name'")
    opens = raw.get("opens_when_url_matches")
    if opens is not None:
        _require(isinstance(opens, str) and opens, f"{where}: 'opens_when_url_matches' must be a non-empty string")
    return Dialog(id=raw["id"], display_name=raw["display_name"], opens_when_url_matches=opens)


def _parse_tab(raw: dict, page_label: str, idx: int) -> Tab:
    where = f"page '{page_label}' tabs[{idx}]"
    _require(isinstance(raw, dict), f"{where}: not a mapping")
    _require("label" in raw and isinstance(raw["label"], str) and raw["label"],
             f"{where}: missing or empty 'label'")
    _require("display_name" in raw and isinstance(raw["display_name"], str) and raw["display_name"],
             f"{where}: missing or empty 'display_name'")
    return Tab(label=raw["label"], display_name=raw["display_name"])


def _parse_page(raw: dict, idx: int) -> Page:
    where = f"pages[{idx}]"
    _require(isinstance(raw, dict), f"{where}: not a mapping")
    for key in ("display_name", "url", "app", "summary"):
        _require(key in raw and isinstance(raw[key], str) and raw[key],
                 f"{where}: missing or empty '{key}'")
    app = raw["app"]
    _require(app in _VALID_APPS,
             f"{where}: 'app' must be one of {sorted(_VALID_APPS)}; got {app!r}")
    url = raw["url"]
    _require(url.startswith("/"),
             f"{where}: 'url' must be a relative path starting with '/'; got {url!r}")

    label = raw["display_name"]
    dialogs_raw = raw.get("dialogs") or []
    _require(isinstance(dialogs_raw, list),
             f"{where}: 'dialogs' must be a list")
    dialogs = [_parse_dialog(d, label, i) for i, d in enumerate(dialogs_raw)]

    tabs_raw = raw.get("tabs") or []
    _require(isinstance(tabs_raw, list),
             f"{where}: 'tabs' must be a list")
    tabs = [_parse_tab(t, label, i) for i, t in enumerate(tabs_raw)]

    anchors_raw = raw.get("anchors") or []
    _require(isinstance(anchors_raw, list),
             f"{where}: 'anchors' must be a list")
    for i, a in enumerate(anchors_raw):
        _require(isinstance(a, str) and a,
                 f"{where} anchors[{i}]: must be a non-empty string")

    return Page(
        display_name=label,
        url=url,
        app=app,
        summary=raw["summary"],
        dialogs=dialogs,
        tabs=tabs,
        anchors=list(anchors_raw),
    )


def _build_indexes(pages: List[Page]) -> SiteMap:
    url_to_page: Dict[str, Page] = {}
    dialog_id_to_pages: Dict[str, List[Page]] = {}
    for page in pages:
        # (url, app) collisions are rejected so the orchestrator's
        # url->page lookup is unambiguous. Dashboard and WorkflowBuilder
        # both have a "/" route — qualify by app to keep them distinct.
        key = f"{page.app}:{page.url}"
        if key in url_to_page:
            raise SiteMapError(
                f"duplicate (app, url): {page.app} {page.url} appears more than once"
            )
        # Keep the same (url, app) keying for the public lookup so the
        # orchestrator's `has_url(url, app=...)` filter stays a O(1)
        # dictionary read.
        url_to_page[key] = page
        for d in page.dialogs:
            dialog_id_to_pages.setdefault(d.id, []).append(page)
    # Public-facing url_to_page is keyed by url ALONE for the common
    # case (orchestrator already knows app from page.app). When the
    # caller needs to disambiguate, they pass app=... to has_url.
    flat: Dict[str, Page] = {}
    for page in pages:
        # If two apps share a url, the per-app index keeps both; the
        # flat one stores the dashboard version (most common consumer
        # context). has_url(url, app=...) is the canonical lookup.
        existing = flat.get(page.url)
        if existing is None or page.app == "dashboard":
            flat[page.url] = page
    return SiteMap(
        pages=list(pages),
        url_to_page=flat,
        dialog_id_to_pages=dialog_id_to_pages,
    )


def load_and_validate(path: str = DEFAULT_PATH) -> SiteMap:
    """Read, parse, validate, index, swap into the cache. Returns the
    new SiteMap. Raises SiteMapError on any failure; the caller (startup
    or hot-reload endpoint) decides how to surface.
    """
    global _current
    if not os.path.isfile(path):
        raise SiteMapError(f"site map not found at {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SiteMapError(f"YAML parse error at {path}: {e}") from e

    _require(isinstance(doc, dict),
             "top-level document must be a mapping (got a list or scalar)")
    pages_raw = doc.get("pages")
    _require(isinstance(pages_raw, list) and pages_raw,
             "'pages' must be a non-empty list")

    pages = [_parse_page(p, i) for i, p in enumerate(pages_raw)]
    site_map = _build_indexes(pages)

    with _lock:
        _current = site_map
    logger.info("Assistant site map loaded: %d pages, %d distinct dialogs",
                len(pages), len(site_map.dialog_id_to_pages))
    return site_map


def get_map() -> SiteMap:
    with _lock:
        if _current is None:
            raise RuntimeError("Assistant site map not loaded — load_and_validate() must run at startup")
        return _current


def reload_from_disk(path: str = DEFAULT_PATH) -> SiteMap:
    """Hot-reload entry point. Caller should catch SiteMapError and
    surface as HTTP 400 with the diagnostic; the in-process cache is
    NOT swapped if validation fails, so the prior valid map keeps
    serving requests.
    """
    return load_and_validate(path)
