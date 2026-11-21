"""node_definitions_domains/browser_automation — Browser automation (Playwright) NodeDefinitions.

launch-completion-megaplan sz-6.1.1 (browser automation). Registers three system
node definitions backed by the browser-automation container
(`CORE/DockerContainers/browser-automation`, Playwright for Python):

    BROWSER_OP=screenshot  url -> screenshot.png
    BROWSER_OP=pdf         url -> page.pdf
    BROWSER_OP=extract     url + css selector -> extract.json (list of {selector, items})

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py`) so the module is
  importable + testable without a DB; `register_browser_automation()` is the only
  DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog speaks: the
  conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility, AND
  named ports (`data` -> `file` / `data`) so the UI can label the ports this
  domain exposes. `json`, `image` and `pdf` are all in
  `node_builder/analyze_prompt.py`'s VALID port types.
- The URL is validated BEFORE any navigation: http/https only (file:// and every
  other scheme refused), and the host is RESOLVED first — every address it
  resolves to is refused if it is private / loopback / unspecified / reserved /
  multicast, or inside the link-local + cloud-metadata range (169.254.0.0/16),
  or one of the metadata hostnames. An unresolvable host is refused too. This is
  the same SSRF posture `services/http_triggers.py` enforces, done in the
  container because the browser, not the API, is what would otherwise fetch.
- JS is ENABLED (that is the point of this domain — the web-scraper domain
  deliberately has none), but downloads are refused (`accept_downloads=False`),
  navigation is capped at 30 s, the whole run at 120 s, selectors are count- and
  length-capped, and every output file is size-capped.
- The container runs as a non-root user and launches chromium with
  `chromium_sandbox=False` (chromium's own sandbox cannot initialise inside the
  container; the container's own isolation is what bounds it).
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it (system-node names are globally
  unique). `MOD_SEED_BUNDLED_NODES` gates the INSERT path only
  (open-core-tier-structure §4.1) — existing rows are never deleted.
- `allowed_egress` is left unset (None = no egress) on all three specs: no other
  domain-primitive spec in this directory declares it, so there is no
  `["*"]` convention to follow here. See the LEAD note below.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# Bare repo:tag of the container behind all three operations. The fully-qualified
# ref (registry + project) is resolved at seed time by platform_image() so a
# private-Harbor / on-prem deployment points at its own registry.
CONTAINER_REPO_TAG = "browser-automation:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/browser-automation/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# Hard caps mirrored in the entrypoint.
MAX_URL_LEN = 2000
MAX_INPUT_BYTES = 10 * 1024 * 1024  # 10 MB per staged file
MAX_OUTPUT_BYTES = 25 * 1024 * 1024  # 25 MB per output file (png/pdf)
MAX_TEXT_BYTES = 10 * 1024 * 1024  # 10 MB of extracted text
NAV_TIMEOUT = 30  # seconds for the navigation itself
OVERALL_TIMEOUT = 120  # seconds for the whole run
MAX_SELECTORS = 50
MAX_SELECTOR_LEN = 200
MAX_MATCHES = 500  # extracted items kept per selector
VIEWPORT_DEFAULT = "1280x720"
VIEWPORT_MAX = 4096
WAIT_UNTIL_DEFAULT = "load"
WAIT_UNTIL_ALLOWED = ("load", "domcontentloaded", "networkidle", "commit")

# The entrypoint lives here as a string so the guard/param/op helpers can be
# unit-tested without a container (the test exec()s this source and repoints
# IN/OUT at a tmp dir), and so the Dockerfile heredoc is a verbatim mirror of it.
ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# browser_automation entrypoint — one operation per run, selected by BROWSER_OP.
#
#   BROWSER_OP=screenshot  url            -> screenshot.png
#   BROWSER_OP=pdf         url            -> page.pdf
#   BROWSER_OP=extract     url + SELECTOR -> extract.json
#
# Standard MOD node I/O contract: the worker stages the node's inputs into
# /data/input and collects /data/output. Parameters arrive as env vars from the
# node's configuration_schema (URL, SELECTOR, VIEWPORT, WAIT_UNTIL, FULL_PAGE);
# a fields.json staged on the `data` port overrides them ({"url": ..., "selector": ...}).
#
# Security posture: http/https only (file:// and every other scheme refused); the
# host is RESOLVED before navigation and private/loopback/link-local/metadata
# IPs refused; navigation timeout 30 s, overall 120 s; JS enabled but downloads
# refused (accept_downloads=False); selectors count/length-capped; every output
# file size-capped; the container runs as a non-root user. Bad input exits
# non-zero with a JSON error in /data/output/error.json.

import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from ipaddress import ip_address, ip_network

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_URL_LEN = 2000
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 10 * 1024 * 1024
NAV_TIMEOUT_MS = 30_000
OVERALL_TIMEOUT = 120
MAX_SELECTORS = 50
MAX_SELECTOR_LEN = 200
MAX_MATCHES = 500
VIEWPORT_DEFAULT = "1280x720"
VIEWPORT_MAX = 4096
WAIT_UNTIL_DEFAULT = "load"
WAIT_UNTIL_ALLOWED = ("load", "domcontentloaded", "networkidle", "commit")

# Cloud-metadata hostnames, refused by name as well as by resolved address.
METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "metadata"})
# The link-local / cloud-metadata range, checked explicitly even though
# ipaddress already classifies it private — the intent must survive a future
# stdlib reclassification.
METADATA_NETWORK = ip_network("169.254.0.0/16")
# Carrier-grade NAT (100.64.0.0/10) — Python's ipaddress.is_private does NOT
# cover this range, yet it is exactly the range MOD's own tailnet mesh uses
# internally, so a tenant-supplied URL resolving here must be refused
# explicitly rather than relying on is_private.
CGNAT_NETWORK = ip_network("100.64.0.0/10")
# RFC 3879 deprecated this IPv6 "site-local" block, but it is still routed on
# plenty of internal networks and Python's ipaddress module does NOT classify
# it as private/reserved — block it explicitly.
SITE_LOCAL_V6_NETWORK = ip_network("fec0::/10")


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def _write_bytes(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_bytes(data)


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(json.dumps({"error": str(msg)}, sort_keys=True), encoding="utf-8")


def _all_inputs():
    out = []
    for f in sorted(IN.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            size = f.stat().st_size
            if size > MAX_INPUT_BYTES:
                raise ValueError(
                    f"{f.name} is {size} bytes — over the {MAX_INPUT_BYTES}-byte input cap"
                )
            out.append(f)
    return out


def _params():
    # Node parameters: env vars, overridden by fields.json on the `data` port.
    out = {}
    p = IN / "fields.json"
    if p.is_file():
        raw = p.read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("fields.json exceeds the 10 MB input cap")
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            out = {str(k).strip().lower(): v for k, v in data.items()}
    params = {}
    for key in ("url", "selector", "viewport", "wait_until", "full_page"):
        params[key] = out.get(key, os.getenv(key.upper(), ""))
    return params


def _unwrap_ip(ip):
    # Expand IPv6 shorthand forms back to the address(es) they stand for, so
    # ::ffff:127.0.0.1 (ipv4_mapped), a 6to4 address and a Teredo address are
    # judged by the address they actually name, not by their wrapper form.
    # ``teredo`` is the odd one out: it yields a (server, client) pair, not a
    # single address -- both embedded IPv4 addresses must be checked, since
    # either one can route traffic to a refused target.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return (mapped,)
    mapped = getattr(ip, "sixtofour", None)
    if mapped is not None:
        return (mapped,)
    mapped = getattr(ip, "teredo", None)
    if mapped is not None:
        return mapped  # (server, client)
    return (ip,)


def _refused_addr(addr):
    # True when a resolved address must never be navigated to.
    for ip in _unwrap_ip(ip_address(addr)):
        if ip in METADATA_NETWORK or ip in CGNAT_NETWORK or ip in SITE_LOCAL_V6_NETWORK:
            return True
        if ip.is_private or ip.is_unspecified or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _guard_url(url_str):
    # Validate scheme + refuse private/loopback/link-local/metadata targets.
    # Returns the URL on success; raises ValueError with the refusal reason
    # otherwise. The host is resolved BEFORE any navigation happens, so a name
    # pointing at loopback or the cloud-metadata endpoint never reaches the
    # browser — and never reaches the network from the browser either.
    url = str(url_str or "").strip()
    if not url:
        raise ValueError("URL is required")
    if len(url) > MAX_URL_LEN:
        raise ValueError(f"URL is over {MAX_URL_LEN} characters — refused")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme {parsed.scheme or '(none)'} is refused — http/https only")
    try:
        has_userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        has_userinfo = True  # unparseable userinfo — refuse rather than guess
    if has_userinfo:
        raise ValueError("URL carries embedded userinfo (user:pass@host) — refused")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL has no host — refused")
    if host.rstrip(".") in METADATA_HOSTS or host.rstrip(".") == "localhost":
        raise ValueError(f"{host} is a metadata/loopback host — refused")
    try:
        addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"{host} does not resolve — refused") from exc
    if not addrs:
        raise ValueError(f"{host}: DNS returned no addresses — refused")
    # AF_UNSPEC, not AF_INET: a host can publish a public A record and a private
    # AAAA record, and Chromium will happily connect over IPv6. Every address
    # the resolver returned, v4 and v6 alike, has to be public.
    # Residual: DNS can rebind between this lookup and Chromium's own resolve.
    for cand in {addr[4][0] for addr in addrs}:
        if _refused_addr(cand):
            raise ValueError(f"{host} resolves to {cand}: private/loopback/link-local — refused")
    return url


def _route_guard(route):
    # Playwright request interceptor: re-runs the SSRF guard against every
    # request the page makes (navigation, redirects, and every sub-resource —
    # script/img/xhr/fetch), aborting anything that resolves to a refused
    # address. Registered on the context BEFORE goto() so it also covers the
    # entry navigation and any redirect chain off it.
    try:
        _guard_url(route.request.url)
    except ValueError as exc:
        _log(f"blocked sub-request: {exc}")
        route.abort()
        return
    route.continue_()


def _check_selectors(raw):
    # Split + cap the SELECTOR parameter; returns a list of selector strings.
    text = str(raw or "").strip()
    if not text:
        raise ValueError("SELECTOR is required for extract")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("SELECTOR had no usable selector")
    if len(parts) > MAX_SELECTORS:
        raise ValueError(f"{len(parts)} selectors — over the {MAX_SELECTORS}-selector cap")
    for p in parts:
        if len(p) > MAX_SELECTOR_LEN:
            raise ValueError(f"selector {p!r} is over {MAX_SELECTOR_LEN} characters — refused")
    return parts


def _viewport(raw):
    # Parse VIEWPORT ('WxH') into playwright's viewport dict, capped.
    text = str(raw or "").strip() or VIEWPORT_DEFAULT
    if "x" not in text:
        raise ValueError("VIEWPORT must look like 1280x720")
    w, h = text.split("x", 1)
    try:
        width, height = int(w.strip()), int(h.strip())
    except ValueError:
        raise ValueError("VIEWPORT must be two integers, e.g. 1280x720")
    if not (1 <= width <= VIEWPORT_MAX and 1 <= height <= VIEWPORT_MAX):
        raise ValueError(f"VIEWPORT {width}x{height} is out of the 1..{VIEWPORT_MAX} range")
    return {"width": width, "height": height}


def _wait_until(raw):
    text = str(raw or "").strip().lower() or WAIT_UNTIL_DEFAULT
    if text not in WAIT_UNTIL_ALLOWED:
        raise ValueError(f"WAIT_UNTIL {text!r} is not one of {WAIT_UNTIL_ALLOWED}")
    return text


def _check_output(name, data):
    # Size-cap every output file before it is written.
    if not data:
        raise ValueError(f"{name} came back empty — refused")
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError(f"{name} is {len(data)} bytes — over the {MAX_OUTPUT_BYTES}-byte output cap")
    _write_bytes(name, data)
    return len(data)


def op_screenshot(paths, params, page):
    # url -> screenshot.png.
    full_page = str(params.get("full_page") or "").strip().lower() in ("true", "1", "yes")
    data = page.screenshot(full_page=full_page)
    size = _check_output("screenshot.png", data)
    _log(f"screenshot: {size} bytes -> screenshot.png")


def op_pdf(paths, params, page):
    # url -> page.pdf.
    data = page.pdf()
    size = _check_output("page.pdf", data)
    _log(f"pdf: {size} bytes -> page.pdf")


def op_extract(paths, params, page):
    # url + css selector(s) -> extract.json (list of matches per selector).
    selectors = _check_selectors(params.get("selector"))
    results = []
    total_text = 0
    for sel in selectors:
        locators = page.query_css(sel)
        items = []
        for loc in locators[:MAX_MATCHES]:
            text = (loc.text or "").strip()
            total_text += len(text)
            if total_text > MAX_TEXT_BYTES:
                raise ValueError(f"extracted text is over {MAX_TEXT_BYTES} bytes — refused")
            items.append({"text": text, "attributes": dict(loc.attributes or {})})
        results.append({"selector": sel, "count": len(locators), "items": items})
        _log(f"extract: {sel} -> {len(locators)} match(es)")
    _write_text("extract.json", json.dumps(results, sort_keys=True, default=str))


OPS = {"screenshot": op_screenshot, "pdf": op_pdf, "extract": op_extract}


def main():
    started = time.monotonic()
    op = os.getenv("BROWSER_OP", "screenshot").strip().lower()
    # The spec spells the full node key ("browser_screenshot"); the container
    # speaks the short name. Accept either spelling.
    if op not in OPS and op.startswith("browser_"):
        op = op[len("browser_"):]
    if op not in OPS:
        _log(f"ERROR: unknown BROWSER_OP {op!r}")
        _write_error(f"unknown BROWSER_OP {op!r}")
        sys.exit(1)
    try:
        params = _params()
        paths = _all_inputs()
        url = _guard_url(params.get("url"))
        viewport = _viewport(params.get("viewport"))
        wait = _wait_until(params.get("wait_until"))
        # Imported here, not at module top: the entrypoint module must stay
        # importable/execable in environments without playwright installed
        # (the unit tests exercise the guards and ops against a stub page).
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(chromium_sandbox=False)
            context = browser.new_context(viewport=viewport, accept_downloads=False)
            # Re-validate EVERY request the browser makes, not just the initial
            # URL: a public page can redirect to, or fetch a sub-resource
            # (script/img/xhr/fetch) from, a private/loopback/metadata address.
            # Each redirect hop is its own request through this route, so this
            # closes that gap — not just the entry navigation.
            context.route("**/*", _route_guard)
            page = context.new_page()
            page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until=wait)
            if time.monotonic() - started > OVERALL_TIMEOUT:
                raise ValueError(f"run exceeded the {OVERALL_TIMEOUT}s overall timeout")
            _log(f"=== browser_automation: {op} ({len(paths)} staged file) ===")
            OPS[op](paths, params, page)
            context.close()
            browser.close()
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        _log(f"ERROR: {exc}")
        _write_error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
#
# # LEAD: egress policy — these three nodes are the only domain primitives that
# must reach the public web, and `allowed_egress` is left None (no egress) on
# all of them: no other spec in node_definitions_domains/ declares the field, so
# there is no `["*"]` convention here to follow, and the dispatch-time shape
# ({host,ports,protocol} + IP/CIDR, schemas.py `_strict_node_allowed_egress`) is
# not something a seed spec can guess per-tenant. If the browser nodes are to
# work in production the lead decides where the grant comes from (a
# platform-settings allowlist applied at dispatch, or an explicit per-node
# allowed_egress set at seed time) — the entrypoint's own SSRF guard stays
# authoritative either way.
# ---------------------------------------------------------------------------

BROWSER_SCREENSHOT_SPEC = {
    "name": "Browser Screenshot",
    "description": (
        "Render an HTTP/HTTPS page in a real browser (Playwright/chromium, JS "
        "enabled) and capture it as a PNG. The URL is validated before any "
        "navigation: http/https only, and the host is resolved first so "
        "private/loopback/link-local/cloud-metadata targets are refused "
        "outright. file:// is never accepted and downloads are never taken "
        "(accept_downloads=False). Set VIEWPORT (default 1280x720) and FULL_PAGE=true "
        "to capture past the fold; WAIT_UNTIL=networkidle waits for late JS. "
        "Navigation is capped at 30 s, the whole run at 120 s, and the PNG at "
        "25 MB. Backed by the browser-automation container."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "browser_screenshot",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {
            "type": "json",
            "description": 'Optional {"url": "..."}; the URL parameter is used when absent.',
        },
    },
    "output_schema": {
        "output_type": "image",
        "file": {
            "type": "image",
            "description": "screenshot.png of the rendered page.",
        },
    },
    "configuration_schema": {
        "BROWSER_OP": {
            "type": "string",
            "default": "browser_screenshot",
            "description": (
                "Fixed for this node: selects the browser operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "description": (
                    "The HTTP or HTTPS URL to render. Must resolve to a public IP."
                ),
            },
            "VIEWPORT": {
                "type": "string",
                "default": VIEWPORT_DEFAULT,
                "description": "Browser viewport as WxH (default 1280x720).",
            },
            "FULL_PAGE": {
                "type": "string",
                "default": "false",
                "description": "true = capture the whole page height, not just the first screen.",
            },
            "WAIT_UNTIL": {
                "type": "string",
                "default": WAIT_UNTIL_DEFAULT,
                "enum": list(WAIT_UNTIL_ALLOWED),
                "description": (
                    "When to stop waiting for the page: load (default), "
                    "domcontentloaded, networkidle or commit."
                ),
            },
        },
    },
    "accepted_formats": ["png"],
    "cost_in_credits": 2,
}

BROWSER_PDF_SPEC = {
    "name": "Browser PDF",
    "description": (
        "Render an HTTP/HTTPS page in a real browser and print it to a PDF, so "
        "JS-built pages, print stylesheets and paginated documents come out as "
        "one file the downstream PDF nodes can read. Same posture as the "
        "screenshot node: http/https only, host resolved and private/loopback/"
        "link-local/cloud-metadata targets refused, no file://, no downloads, "
        "30 s navigation / 120 s overall timeout, 25 MB output cap. Backed by "
        "the browser-automation container."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "browser_pdf",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {
            "type": "json",
            "description": 'Optional {"url": "..."}; the URL parameter is used when absent.',
        },
    },
    "output_schema": {
        "output_type": "pdf",
        "file": {
            "type": "pdf",
            "description": "page.pdf rendered from the page.",
        },
    },
    "configuration_schema": {
        "BROWSER_OP": {
            "type": "string",
            "default": "browser_pdf",
            "description": (
                "Fixed for this node: selects the browser operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "description": (
                    "The HTTP or HTTPS URL to print. Must resolve to a public IP."
                ),
            },
            "VIEWPORT": {
                "type": "string",
                "default": VIEWPORT_DEFAULT,
                "description": "Browser viewport as WxH (default 1280x720).",
            },
            "WAIT_UNTIL": {
                "type": "string",
                "default": WAIT_UNTIL_DEFAULT,
                "enum": list(WAIT_UNTIL_ALLOWED),
                "description": (
                    "When to stop waiting for the page: load (default), "
                    "domcontentloaded, networkidle or commit."
                ),
            },
        },
    },
    "accepted_formats": ["pdf"],
    "cost_in_credits": 2,
}

BROWSER_EXTRACT_SPEC = {
    "name": "Browser Extract",
    "description": (
        "Render an HTTP/HTTPS page in a real browser and pull the elements that "
        "match a CSS selector out of the FINAL (post-JS) DOM, as JSON: one entry "
        "per selector, each carrying its match count and a list of {text, "
        "attributes}. This is the JS-enabled sibling of Web Scraper Extract — "
        "use it when the data only exists after the page's own scripts run. "
        "SELECTOR is comma-separated (e.g. 'h1, .article p, a[href]'), capped at "
        "50 selectors of 200 characters and 500 items each. Same URL posture as "
        "the other two: http/https only, host resolved, private/loopback/"
        "link-local/cloud-metadata refused, no downloads, 30 s navigation / 120 s "
        "overall timeout. Backed by the browser-automation container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "browser_extract",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {
            "type": "json",
            "description": 'Optional {"url": "...", "selector": "..."}; parameters used when absent.',
        },
    },
    "output_schema": {
        "output_type": "json",
        "data": {
            "type": "json",
            "description": "extract.json: [{selector, count, items:[{text, attributes}]}].",
        },
    },
    "configuration_schema": {
        "BROWSER_OP": {
            "type": "string",
            "default": "browser_extract",
            "description": (
                "Fixed for this node: selects the browser operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "description": (
                    "The HTTP or HTTPS URL to render. Must resolve to a public IP."
                ),
            },
            "SELECTOR": {
                "type": "string",
                "default": "",
                "description": (
                    "Comma-separated CSS selectors to extract, e.g. "
                    "'h1, .article p, a[href]'. Max 50 selectors."
                ),
            },
            "VIEWPORT": {
                "type": "string",
                "default": VIEWPORT_DEFAULT,
                "description": "Browser viewport as WxH (default 1280x720).",
            },
            "WAIT_UNTIL": {
                "type": "string",
                "default": WAIT_UNTIL_DEFAULT,
                "enum": list(WAIT_UNTIL_ALLOWED),
                "description": (
                    "When to stop waiting for the page: load (default), "
                    "domcontentloaded, networkidle or commit."
                ),
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}

# Registry entries, keyed by the operation id the rest of the catalog uses.
NODE_DEFINITIONS = [
    BROWSER_SCREENSHOT_SPEC,
    BROWSER_PDF_SPEC,
    BROWSER_EXTRACT_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "browser_screenshot": BROWSER_SCREENSHOT_SPEC,
    "browser_pdf": BROWSER_PDF_SPEC,
    "browser_extract": BROWSER_EXTRACT_SPEC,
}

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_browser_automation(db: Session) -> int:
    """Register the 3 browser operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping browser_automation domain-primitive seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,  # system node — available to all tenants
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return created

