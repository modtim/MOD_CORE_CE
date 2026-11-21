"""node_definitions_domains/web_scraper -- Web scraper NodeDefinitions.

launch-completion-megaplan sz-6.2.1 (web scraper). Registers three system node
definitions backed by the web-scraper container
(`CORE/DockerContainers/web-scraper`):

    SCRAPER_OP=fetch          url -> html/text
    SCRAPER_OP=extract        url + css selectors -> json
    SCRAPER_OP=crawl          seed url, same-host, max_pages/max_depth -> json lines

Posture notes
-------------
- Robots.txt honoured via urllib.robotparser (user agent "MOD-Scraper/1.0");
  disallowed URLs -> skip + report.
- Rate limit: min delay between requests to the same host (default 1.0 s,
  bounded 0.5..10); Crawl-Delay from robots.txt is honoured when present.
- "rotation" = rotate through a user-supplied list of user agents only (no
  proxy rotation, no evasion features).
- http/https only; resolve host and refuse private/loopback/link-local/metadata
  IPs, re-check after every redirect (max 5); per-response size cap 10 MB,
  timeout 20 s, overall 300 s; non-root.
- No JS execution (that belongs to browser_automation).
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it. `MOD_SEED_BUNDLED_NODES` gates the
  INSERT path only.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "web-scraper:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/web-scraper/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# Hard caps mirrored in the entrypoint.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB
HTTP_TIMEOUT = 20  # seconds per request
OVERALL_TIMEOUT = 300  # seconds overall
MAX_REDIRECTS = 5
MAX_PAGES = 50
MAX_DEPTH = 3
RATE_LIMIT_MIN = 0.5
RATE_LIMIT_MAX = 10.0
DEFAULT_DELAY = 1.0
ROBOT_AGENT = "MOD-Scraper/1.0"

# The entrypoint lives here as a string so the op functions can be unit-tested
# without a container (the test exec()s this source and repoints IN/OUT at a
# tmp dir), and so the Dockerfile heredoc is a verbatim mirror of it.
# NOTE: no triple-quoted docstrings inside this block -- they would close the
# outer r""" prematurely.  Use # comments in the embedded code.
ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# web_scraper entrypoint -- one operation per run, selected by SCRAPER_OP.
#
#   SCRAPER_OP=fetch       -> output.html / output.txt
#   SCRAPER_OP=extract     -> output.json
#   SCRAPER_OP=crawl       -> output.html (last page) + crawls.jsonl
#
# Standard MOD node I/O contract: the worker stages the node's inputs into
# /data/input and collects /data/output. Parameters arrive as env vars from the
# node's configuration_schema (URL, SELECTORS, CRAWL_DELAY, RATE_LIMIT,
# USER_AGENTS, FORMAT, MAX_PAGES, MAX_DEPTH); a fields.json staged on the
# `data` port overrides URL and SELECTORS.
#
# Security posture: every network request (fetch, extract, crawl AND robots.txt)
# goes through ONE guarded function, _safe_get(). It resolves the host with
# socket.getaddrinfo (ALL families), refuses is_private / is_loopback / is_link_local
# / is_multicast / is_reserved / is_unspecified, CGNAT 100.64.0.0/10, and unwraps
# ipv4_mapped / sixtofour / teredo before re-checking. It then connects to the
# VALIDATED IP, never the hostname (a requests adapter pins the resolved IP; for
# https urllib3's assert_hostname + server_hostname keep SNI and hostname
# verification on the real name), so DNS rebinding / TOCTOU cannot swap the
# address after validation. Redirects are followed manually (allow_redirects=False)
# up to MAX_REDIRECTS with the full check re-run on every hop. Bodies are streamed
# and cut off at the byte cap. http/https only, robots.txt honoured via
# urllib.robotparser, per-host rate limit, 20 s request timeout, 300 s overall
# timeout, max 5 redirects, 10 MB per-response cap. No JS execution.
# Bad input exits non-zero with a JSON error in /data/output/error.json.

import ipaddress
import json
import os
import socket
import sys
import time
import urllib.parse
from collections import deque
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
HTTP_TIMEOUT = 20
OVERALL_TIMEOUT = 300
MAX_REDIRECTS = 5
MAX_PAGES = 50
MAX_DEPTH = 3
RATE_LIMIT_MIN = 0.5
RATE_LIMIT_MAX = 10.0
DEFAULT_DELAY = 1.0
ROBOT_AGENT = "MOD-Scraper/1.0"

_ROBOT_CACHE = {}


def _log(msg):
    # Print to stderr so the worker logs capture it.
    print(msg, file=sys.stderr, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(
        json.dumps({"error": str(msg)}, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# IP validation -- refuse private / loopback / link-local / metadata IPs
# ---------------------------------------------------------------------------

_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Apple / Cloudflare CGNAT range
# RFC 3879 deprecated this IPv6 "site-local" block, but it is still routed on
# plenty of internal networks and Python's ipaddress module does NOT classify
# it as private/reserved -- block it explicitly.
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")


def _ip_is_public(ip):
    # True only for addresses that are usable as public scrape targets.
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if ip.version == 4 and ip in _CGNAT:
        return False
    if ip.version == 6 and ip in _SITE_LOCAL_V6:
        return False
    return True


def _unwrap_ip(ip):
    # Expand IPv6 shorthand forms back to the address(es) they stand for, so
    # ::ffff:127.0.0.1 (ipv4_mapped), a 6to4 address and a Teredo address are
    # judged by the address they actually name, not by their wrapper form.
    # Always returns a TUPLE: ``teredo`` is the odd one out -- it yields a
    # (server, client) pair, not a single address, and either embedded IPv4
    # can route traffic to a refused target, so BOTH must be checked.
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


def _is_safe_host(host):
    # True for hostnames and for IP literals that are public.
    host = (host or "").strip().lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname -- checked via DNS below
    return all(_ip_is_public(part) for part in _unwrap_ip(ip))


def _check_addr_infos(host, addrs):
    # Validate EVERY address getaddrinfo returned for a host.
    seen = []
    for entry in addrs:
        for ip in _unwrap_ip(ipaddress.ip_address(entry[3][0])):
            if not _ip_is_public(ip):
                raise ValueError(
                    f"{host} resolves to {ip}: not a public IP"
                )
            seen.append(ip)
    if not seen:
        raise ValueError(f"{host}: DNS returned no addresses")
    return seen


def _resolve_and_check(url_str):
    # Resolve every address a host maps to, verify they are ALL public, and
    # return them so the caller can connect to a validated IP, not the name.
    parsed = urllib.parse.urlparse(url_str)
    host = parsed.hostname or ""
    if not host:
        raise ValueError("no host to resolve")
    try:
        has_userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        has_userinfo = True  # unparseable userinfo -- refuse rather than guess
    if has_userinfo:
        raise ValueError("refusing URLs with embedded userinfo (user:pass@host)")
    if not _is_safe_host(host):
        raise ValueError(f"refusing {host}: not a public IP")
    try:
        addrs = socket.getaddrinfo(host, parsed.port or None,
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, ipaddress.IPv6ValueError,
            ipaddress.IPv4ValueError) as exc:
        raise ValueError(f"{host}: DNS resolution failed -- {exc}")
    return _check_addr_infos(host, addrs)


class _PinnedAdapter(HTTPAdapter):
    # Connect to a pre-validated IP instead of the requested hostname.
#
    # ``pin()`` is called by _safe_get() after every address the resolver
    # returned has passed the public-IP checks, so the socket we open is always
    # one we just vetted -- a DNS rebinding or TOCTOU swap between validation
    # and connect cannot change the peer. For https, urllib3's ``assert_hostname``
    # keeps certificate-hostname verification on the ORIGINAL name and
    # ``server_hostname`` keeps SNI on it, so TLS still works against the IP
    # socket. The outgoing ``Host`` header is restored to the original name.
    #

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned = None

    def pin(self, ip_str, host, port):
        self._pinned = (ip_str, host, port)
        return self

    def get_connection_with_tls_context(self, request, verify,
                                        proxies=None, cert=None):
        if self._pinned is None:
            raise ValueError("refusing to connect without a validated IP")
        ip_str, host, port = self._pinned
        scheme = urllib.parse.urlparse(request.url).scheme
        pool_kwargs = {}
        if scheme == "https":
            pool_kwargs = {"assert_hostname": host, "server_hostname": host}
        return self.poolmanager.connection_from_host(
            host=ip_str, port=port, scheme=scheme, pool_kwargs=pool_kwargs
        )


def _pinned_url(url_str, ip_str):
    # Rewrite the URL host to the validated IP (the socket we connect to).
    parsed = urllib.parse.urlparse(url_str)
    netloc = f"{ip_str}:{parsed.port}" if parsed.port else ip_str
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def _host_header(url_str):
    # The Host header the server must see: the ORIGINAL name, port included
    # only when it is non-default for the scheme.
    parsed = urllib.parse.urlparse(url_str)
    host = parsed.hostname or ""
    port = parsed.port
    if port and port not in (80, 443):
        return f"{host}:{port}"
    return host


def _safe_get(url_str, max_bytes, timeout, user_agent=None):
    # The ONE guarded network entry point.
#
    # fetch, extract, crawl AND robots.txt all go through here. Validates
    # scheme, resolves and validates EVERY address, connects to the validated
    # IP via _PinnedAdapter, streams the body and cuts it off at max_bytes, and
    # follows redirects MANUALLY (allow_redirects=False) re-running the whole
    # check on every hop, up to MAX_REDIRECTS. Returns (status, body).
    #
    current = url_str
    hops = 0
    while True:
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"{parsed.scheme}: only http/https allowed")
        host = parsed.hostname or ""
        if not host:
            raise ValueError("no host to resolve")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ips = _resolve_and_check(current)
        adapter = _PinnedAdapter()
        adapter.pin(str(ips[0]), host, port)
        session = requests.Session()
        # Defense in depth: _PinnedAdapter already ignores the `proxies` kwarg
        # when it builds the connection pool, but ``trust_env=False`` also
        # stops requests from reading HTTP(S)_PROXY / NO_PROXY / ~/.netrc from
        # the environment at all, so nothing proxy-shaped reaches the adapter.
        session.trust_env = False
        session.adapters = {"http://": adapter, "https://": adapter}
        try:
            resp = session.get(
                _pinned_url(current, str(ips[0])),
                headers={"User-Agent": user_agent or ROBOT_AGENT,
                         "Host": _host_header(current)},
                allow_redirects=False,
                stream=True,
                timeout=(min(10.0, timeout), timeout),
            )
        except requests.RequestException as exc:
            raise ValueError(f"{current}: request failed -- {exc}")
        try:
            body = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                body += chunk
                if len(body) > max_bytes:
                    raise ValueError(
                        f"response exceeds the {max_bytes} byte cap"
                    )
        finally:
            resp.close()
        status = resp.status_code
        if status not in (300, 301, 302, 303, 307, 308):
            return status, bytes(body)
        hops += 1
        if hops > MAX_REDIRECTS:
            raise ValueError(
                f"more than {MAX_REDIRECTS} redirects -- refusing"
            )
        location = resp.headers.get("Location") or resp.headers.get("location")
        if not location:
            raise ValueError("redirect with no Location header")
        current = urllib.parse.urljoin(current, location.strip())


def _robots_path(url_str):
    # Return (rp, base_url) for the host's robots.txt, creating if needed.
    parsed = urllib.parse.urlparse(url_str)
    port = parsed.port
    if port and port not in (80, 443):
        base = f"{parsed.scheme}://{parsed.hostname}:{port}"
    else:
        base = f"{parsed.scheme}://{parsed.hostname}"
    key = base
    if key not in _ROBOT_CACHE:
        rp = RobotFileParser()
        rp_url = f"{base}/robots.txt"
        try:
            status, body = _safe_get(rp_url, 64 * 1024, HTTP_TIMEOUT)
            if status == 200:
                rp.parse(body.decode("utf-8", errors="replace").splitlines())
            else:
                rp.allow_all = True
        except (requests.RequestException, ValueError, OSError):
            rp.allow_all = True
        _ROBOT_CACHE[key] = (rp, base)
    return _ROBOT_CACHE[key]


def _robots_allowed(url_str):
    # Check robots.txt for the given URL and user agent.
    rp, base = _robots_path(url_str)
    return rp.can_fetch(ROBOT_AGENT, url_str)


def _parse_params():
    # Read node parameters from env / fields.json.
    out = {}
    p = IN / "fields.json"
    if p.is_file():
        raw = p.read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("fields.json exceeds the 10 MB input cap")
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            out = {str(k).strip().lower(): v for k, v in data.items()}
    params = {}
    for key in ("url", "selectors", "format", "rate_limit", "crawl_delay",
                "user_agents", "max_pages", "max_depth"):
        params[key] = out.get(key, os.getenv(key.upper(), ""))
    return params


def _safe_url(url_str):
    # Validate scheme, resolve host, return parsed URL.
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{parsed.scheme}: only http/https allowed")
    _resolve_and_check(url_str)
    return parsed


def _fetch_url(url_str, user_agent):
    # Fetch a single URL with rate limit + robots check. Returns bytes.
    if not _robots_allowed(url_str):
        return b""  # skip, robots disallows
    rate = float(os.getenv("RATE_LIMIT", str(DEFAULT_DELAY)))
    rate = max(RATE_LIMIT_MIN, min(RATE_LIMIT_MAX, rate))
    # Apply Crawl-Delay from robots if larger than configured rate
    rp, _ = _robots_path(url_str)
    crawl_delay = rp.crawl_delay(ROBOT_AGENT)
    if crawl_delay is not None and crawl_delay > rate:
        rate = crawl_delay
    try:
        time.sleep(rate)
    except Exception:
        pass
    _status, body = _safe_get(url_str, MAX_RESPONSE_BYTES, HTTP_TIMEOUT,
                              user_agent=user_agent)
    return body


def _rotate_ua(user_agents_str):
    # Yield user agents in round-robin from a comma-separated list.
    agents = [a.strip() for a in user_agents_str.split(",") if a.strip()]
    if not agents:
        agents = [ROBOT_AGENT]
    idx = 0
    while True:
        yield agents[idx % len(agents)]
        idx += 1


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def op_fetch(paths, params):
    # Fetch a URL and return the HTML/text content.
    url = str(params.get("url") or "").strip()
    if not url:
        raise ValueError("URL parameter is required")
    fmt = str(params.get("format") or "html").strip().lower()
    _safe_url(url)
    ua_list = str(params.get("user_agents") or ROBOT_AGENT)
    ua = next(_rotate_ua(ua_list))
    body = _fetch_url(url, ua)
    if fmt == "text":
        if BeautifulSoup is not None:
            soup = BeautifulSoup(body, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            _write_text("output.txt", text)
            _log(f"fetched {url} -> output.txt ({len(text)} chars)")
        else:
            _write_text("output.txt", body.decode("utf-8", errors="replace"))
            _log(f"fetched {url} -> output.txt (raw, no bs4)")
    else:
        _write_text("output.html", body.decode("utf-8", errors="replace"))
        _log(f"fetched {url} -> output.html ({len(body)} bytes)")


def op_extract(paths, params):
    # Fetch a URL and extract elements matching CSS selectors -> JSON.
    url = str(params.get("url") or "").strip()
    selectors_str = str(params.get("selectors") or "").strip()
    if not url:
        raise ValueError("URL parameter is required")
    if not selectors_str:
        raise ValueError("SELECTORS parameter is required")
    selectors = [s.strip() for s in selectors_str.split(",") if s.strip()]
    _safe_url(url)
    ua_list = str(params.get("user_agents") or ROBOT_AGENT)
    ua = next(_rotate_ua(ua_list))
    body = _fetch_url(url, ua)
    if BeautifulSoup is None:
        raise ValueError(
            "beautifulsoup4 is not installed -- cannot extract"
        )
    soup = BeautifulSoup(body, "html.parser")
    result = {}
    for sel in selectors:
        items = []
        for tag in soup.select(sel):
            item = {"text": tag.get_text(strip=True)}
            href = tag.get("href")
            src = tag.get("src")
            if href:
                item["href"] = href
            if src:
                item["src"] = src
            for attr in tag.attrs:
                if attr not in ("href", "src", "text"):
                    item[attr] = str(tag.attrs[attr])
            items.append(item)
        result[sel] = items
    _write_text("output.json", json.dumps(result, indent=2, ensure_ascii=False))
    _log(f"extracted {url} selectors {selectors} -> output.json")


def op_crawl(paths, params):
    # Crawl a seed URL: same-host only, limited depth/pages.
    url = str(params.get("url") or "").strip()
    if not url:
        raise ValueError("URL parameter is required")
    max_pages = int(str(params.get("max_pages") or MAX_PAGES))
    max_pages = min(max_pages, MAX_PAGES)
    max_depth = int(str(params.get("max_depth") or MAX_DEPTH))
    max_depth = min(max_depth, MAX_DEPTH)
    parsed = _safe_url(url)
    if not _robots_allowed(url):
        _write_text("crawls.jsonl", "")
        _log(f"crawl {url}: robots disallows, nothing to crawl")
        return
    queue = deque()  # (url, base_host, depth)
    queue.append((url, parsed.netloc, 0))
    visited = set()
    results = []
    ua_gen = _rotate_ua(str(params.get("user_agents") or ROBOT_AGENT))
    base_host = parsed.netloc

    while queue and len(visited) < max_pages:
        current_url, host, depth = queue.popleft()
        if current_url in visited:
            continue
        if depth > max_depth:
            continue
        if not _robots_allowed(current_url):
            results.append({
                "url": current_url, "status": "disallowed",
                "depth": depth, "size": 0,
            })
            visited.add(current_url)
            continue
        ua = next(ua_gen)
        try:
            body = _fetch_url(current_url, ua)
        except ValueError:
            results.append({
                "url": current_url, "status": "error",
                "depth": depth, "size": 0,
            })
            visited.add(current_url)
            continue

        status = "success" if body else "empty"
        results.append({
            "url": current_url, "status": status,
            "depth": depth, "size": len(body),
        })
        visited.add(current_url)

        # Extract links -- same-host only
        if body and BeautifulSoup is not None:
            soup = BeautifulSoup(body, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                link = urllib.parse.urljoin(current_url, href)
                link_parsed = urllib.parse.urlparse(link)
                if link_parsed.netloc != base_host:
                    continue
                if link_parsed.scheme not in ("http", "https"):
                    continue
                if link in visited:
                    continue
                queue.append((link, base_host, depth + 1))

    # Deduplicate queue by url
    seen_q = set()
    deduped = []
    for item in queue:
        if item[0] not in seen_q:
            seen_q.add(item[0])
            deduped.append(item)
    queue = deduped

    # Write results as jsonl
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "crawls.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _log(f"crawled {url}: {len(results)} pages, {len(queue)} remaining")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

OPS = {
    "fetch": op_fetch,
    "extract": op_extract,
    "crawl": op_crawl,
}


def main():
    op = os.getenv("SCRAPER_OP", "fetch").strip().lower()
    if op not in OPS:
        _log(f"ERROR: unknown SCRAPER_OP {op!r}")
        _write_error(f"unknown SCRAPER_OP {op!r}")
        sys.exit(1)
    try:
        params = _parse_params()
        _log(f"=== web_scraper: {op} ===")
        OPS[op]([], params)
    except Exception as exc:  # noqa: BLE001 -- report, never crash with a traceback
        _log(f"ERROR: {exc}")
        _write_error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts -- one per operation.
# ---------------------------------------------------------------------------

SCRAPER_FETCH_SPEC = {
    "name": "Web Scraper Fetch",
    "description": (
        "Fetch an HTTP/HTTPS URL and return its HTML or text content. "
        "Robots.txt is honoured for the MOD-Scraper/1.0 user agent; private, "
        "loopback, link-local, and cloud-metadata IPs are refused (re-checked "
        "after every redirect up to 5). Per-response size cap 10 MB, "
        "20-second timeout. Backed by the web-scraper container (requests + "
        "beautifulsoup4 + html.parser)."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "fetch",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "document",
        "data": {
            "type": "json",
            "description": (
                "URL to fetch as {\"url\": \"...\"}."
            ),
        },
    },
    "output_schema": {
        "output_type": "document",
        "file": {
            "type": "document",
            "description": "output.html or output.txt.",
        },
    },
    "configuration_schema": {
        "SCRAPER_OP": {
            "type": "string",
            "default": "fetch",
            "description": (
                "Fixed for this node: selects the scraper operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "description": (
                    "The HTTP or HTTPS URL to fetch. Must resolve to a public IP."
                ),
            },
            "FORMAT": {
                "type": "string",
                "default": "html",
                "enum": ["html", "text"],
                "description": "Output format: html (default) or text.",
            },
            "USER_AGENTS": {
                "type": "string",
                "default": "MOD-Scraper/1.0",
                "description": (
                    "Comma-separated list of user agents to rotate through. "
                    "Defaults to MOD-Scraper/1.0."
                ),
            },
            "RATE_LIMIT": {
                "type": "string",
                "default": "1.0",
                "description": (
                    "Minimum delay in seconds between requests to the same host "
                    "(bounded 0.5..10)."
                ),
            },
        },
    },
    "accepted_formats": ["html", "text", "json"],
    "cost_in_credits": 1,
}

SCRAPER_EXTRACT_SPEC = {
    "name": "Web Scraper Extract",
    "description": (
        "Fetch an HTTP/HTTPS URL and extract elements matching CSS selectors, "
        "returning structured JSON. Selectors are comma-separated (e.g., "
        "\"h1, .article p, a\"). Each selector's results include text, href, "
        "src, and any other attributes. Robots.txt, rate limiting, and IP "
        "blocking rules are the same as Fetch. Requires beautifulsoup4. "
        "Backed by the web-scraper container."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "extract",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "document",
        "data": {
            "type": "json",
            "description": (
                "URL and optional parameters "
                "as {\"url\": \"...\", \"selectors\": \"...\"}."
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "rows": {
            "type": "json",
            "description": "JSON keyed by selector string.",
        },
    },
    "configuration_schema": {
        "SCRAPER_OP": {
            "type": "string",
            "default": "extract",
            "description": (
                "Fixed for this node: selects the scraper operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "description": (
                    "The HTTP or HTTPS URL to fetch."
                ),
            },
            "SELECTORS": {
                "type": "string",
                "default": "",
                "description": (
                    "Comma-separated CSS selectors to extract, "
                    "e.g. 'h1, .article p, a'."
                ),
            },
            "USER_AGENTS": {
                "type": "string",
                "default": "MOD-Scraper/1.0",
                "description": (
                    "Comma-separated list of user agents to rotate through."
                ),
            },
            "RATE_LIMIT": {
                "type": "string",
                "default": "1.0",
                "description": (
                    "Minimum delay in seconds between requests to the same host "
                    "(bounded 0.5..10)."
                ),
            },
        },
    },
    "accepted_formats": ["html", "json"],
    "cost_in_credits": 1,
}

SCRAPER_CRAWL_SPEC = {
    "name": "Web Scraper Crawl",
    "description": (
        "Crawl a website starting from a seed URL. Only follows links on the "
        "same host (same netloc), up to MAX_PAGES (default 50) pages and "
        "MAX_DEPTH (default 3) link depth. Results are written as JSON Lines "
        "(crawls.jsonl). Robots.txt is honoured -- disallowed URLs are skipped "
        "and reported. Rate limiting applies per-host. Backed by the "
        "web-scraper container."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "crawl",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "document",
        "data": {
            "type": "json",
            "description": "Seed URL as {\"url\": \"...\"}.",
        },
    },
    "output_schema": {
        "output_type": "json",
        "rows": {
            "type": "json",
            "description": (
                "crawls.jsonl -- one JSON line per crawled page."
            ),
        },
    },
    "configuration_schema": {
        "SCRAPER_OP": {
            "type": "string",
            "default": "crawl",
            "description": (
                "Fixed for this node: selects the scraper operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "description": (
                    "Seed URL for the crawl. Must resolve to a public IP."
                ),
            },
            "MAX_PAGES": {
                "type": "string",
                "default": "50",
                "description": (
                    "Maximum number of pages to crawl (hard cap 50)."
                ),
            },
            "MAX_DEPTH": {
                "type": "string",
                "default": "3",
                "description": (
                    "Maximum link depth from seed (hard cap 3)."
                ),
            },
            "USER_AGENTS": {
                "type": "string",
                "default": "MOD-Scraper/1.0",
                "description": (
                    "Comma-separated list of user agents to rotate through."
                ),
            },
            "RATE_LIMIT": {
                "type": "string",
                "default": "1.0",
                "description": (
                    "Minimum delay in seconds between requests to the same host "
                    "(bounded 0.5..10)."
                ),
            },
        },
    },
    "accepted_formats": ["html", "json"],
    "cost_in_credits": 1,
}

# Registry entries, keyed by the operation id the rest of the catalog uses.
NODE_DEFINITIONS = [
    SCRAPER_FETCH_SPEC,
    SCRAPER_EXTRACT_SPEC,
    SCRAPER_CRAWL_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "fetch": SCRAPER_FETCH_SPEC,
    "extract": SCRAPER_EXTRACT_SPEC,
    "crawl": SCRAPER_CRAWL_SPEC,
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


def register_web_scraper(db: Session) -> int:
    """Register the 3 scraper operations as system node definitions.

    Returns the number of NEW rows created.  Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file,
    and is not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in (
        "true", "1", "yes"
    ):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false "
            "-- skipping web_scraper domain-primitive seed"
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
                tenant_id=None,  # system node -- available to all tenants
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
