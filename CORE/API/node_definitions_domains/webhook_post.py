"""node_definitions_domains/webhook_post -- generic signed WEBHOOK POST node.

launch-completion-megaplan sz-4.6c (part c). Registers ONE system node
definition backed by the webhook-post container
(`CORE/DockerContainers/webhook-post`):

    WEBHOOK_OP=post    body (input file on `data`, or the BODY template) -> receipt.json

Posture notes
-------------
- Specs are plain dicts (same shape as `qr_barcode.py` / `web_scraper.py`) so
  the module is importable + testable without a DB; `register_webhook_post()`
  is the only DB-touching entry point.
- The SSRF guard is the reviewed one from `web_scraper.py` (commit
  d4c722c42), copied not reinvented: every address getaddrinfo returned is
  validated (private / loopback / link-local / multicast / reserved /
  unspecified, CGNAT 100.64/10, fec0::/10 site-local), ipv4_mapped /
  sixtofour / teredo forms are unwrapped before the check, URLs with embedded
  userinfo are refused, the socket connects to the validated IP through
  _PinnedAdapter and `trust_env=False` stops requests from reading
  HTTP(S)_PROXY / NO_PROXY / ~/.netrc.
- Redirects are NOT followed here at all (web_scraper re-validated every hop;
  a webhook target that answers 3xx is not what this node promised) -- any
  3xx is an error.
- The URL is treated as a secret: webhook URLs routinely embed the shared
  secret / token in their path, so URL is declared `"secret": True` and is
  NEVER echoed. Every error masks it to scheme+host; the receipt carries
  status / bytes_sent / a text-only response preview only -- never the URL,
  the request headers, or the signing secret.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it. `MOD_SEED_BUNDLED_NODES` gates the
  INSERT path only, exactly as `seed_converters.py` does.
- The container entrypoint lives in the Dockerfile as a quoted heredoc
  (self-contained, no sidecar). `ENTRYPOINT_SOURCE` here is the byte-identical
  source of that heredoc, kept so the unit tests can exec the real op
  functions without building an image.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "webhook-post:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/webhook-post/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# Hard caps mirrored in the entrypoint.
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MiB request body
MAX_RESPONSE_BYTES = 64 * 1024  # response read is byte-capped
PREVIEW_CHARS = 512
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
MAX_RETRIES = 2
MAX_RETRY_AFTER = 10
BACKOFF_BASE = 1.0
MAX_HEADERS = 20
MAX_HEADER_BYTES = 1024

# ---------------------------------------------------------------------------
# Container entrypoint -- byte-identical to the heredoc in the Dockerfile.
# Kept here so the unit tests can exec the real op functions without building
# an image. NOTE: no triple-quoted docstrings inside this block -- they would
# close the raw string.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''
#!/usr/bin/env python3
# webhook-post entrypoint -- one operation per run, selected by WEBHOOK_OP.
#   WEBHOOK_OP=post  -> receipt.json
# Bad input exits non-zero with a JSON error in /data/output/error.json.

import hmac
import hashlib
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
PREVIEW_CHARS = 512
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
MAX_RETRIES = 2
MAX_RETRY_AFTER = 10
BACKOFF_BASE = 1.0
MAX_HEADERS = 20
MAX_HEADER_BYTES = 1024
RETRY_STATUSES = (429, 502, 503, 504)
METHODS = ("POST", "PUT", "PATCH")
# Hop-by-hop / framing headers the caller may not override: the worker's own
# framing (Content-Length, Transfer-Encoding, Connection) and the authority
# (Host) must stay ours, and Cookie is refused as a credential-smuggling
# vector against shared-host targets.
FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "cookie"}
)
# The signature headers this node owns; a caller-supplied copy would forge it.
OWNED_HEADERS = frozenset({"x-mod-signature", "x-mod-timestamp"})


def _log(msg):
    # Print to stderr so the worker logs capture it.
    print(msg, file=sys.stderr, flush=True)


def _write_json(name, payload):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(
        json.dumps({"error": str(msg)}, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# URL masking -- the URL often embeds the shared secret in its path.
# ---------------------------------------------------------------------------


def _mask_url(url_str):
    # scheme + host ONLY -- path, query, port and userinfo never survive.
    try:
        parsed = urllib.parse.urlparse(url_str)
    except ValueError:
        return "unparseable url (masked)"
    host = (parsed.hostname or "").lower()
    if not host:
        return f"{parsed.scheme} (masked)"
    return f"{parsed.scheme}://{host}"


# ---------------------------------------------------------------------------
# IP validation -- refuse private / loopback / link-local / metadata IPs
# (copied from the reviewed guard in web_scraper.py, commit d4c722c42)
# ---------------------------------------------------------------------------

_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Apple / Cloudflare CGNAT range
# RFC 3879 deprecated this IPv6 "site-local" block, but it is still routed on
# plenty of internal networks and Python's ipaddress module does NOT classify
# it as private/reserved -- block it explicitly.
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")


def _ip_is_public(ip):
    # True only for addresses that are usable as public webhook targets.
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


def _is_safe_host(host):
    # True for hostnames and for IP literals that are public.
    host = (host or "").strip().lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname -- checked via DNS below
    return all(_ip_is_public(candidate) for candidate in _unwrap_ip(ip))


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
    # ``pin()`` is called by _post_once() after every address the resolver
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


# ---------------------------------------------------------------------------
# The ONE guarded network entry point -- manual POST, redirects never followed.
# ---------------------------------------------------------------------------


def _post_once(url_str, body, headers, max_bytes):
    # Validate scheme, resolve + validate EVERY address, connect to the
    # validated IP via _PinnedAdapter, stream the response and cut it off at
    # max_bytes. Redirects are NOT followed: any 3xx is an error. Returns
    # (status, response_body, response_headers).
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{parsed.scheme}: only http/https allowed")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("no host to resolve")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ips = _resolve_and_check(url_str)
    adapter = _PinnedAdapter()
    adapter.pin(str(ips[0]), host, port)
    session = requests.Session()
    # Defense in depth: _PinnedAdapter already ignores the `proxies` kwarg
    # when it builds the connection pool, but ``trust_env=False`` also stops
    # requests from reading HTTP(S)_PROXY / NO_PROXY / ~/.netrc from the
    # environment at all, so nothing proxy-shaped reaches the adapter.
    session.trust_env = False
    session.adapters = {"http://": adapter, "https://": adapter}
    try:
        resp = session.post(
            _pinned_url(url_str, str(ips[0])),
            data=body,
            headers=headers,
            allow_redirects=False,
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise ValueError(f"{_mask_url(url_str)}: request failed -- {exc}")
    try:
        out = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            out += chunk
            if len(out) > max_bytes:
                raise ValueError(
                    f"response exceeds the {max_bytes} byte cap"
                )
    finally:
        resp.close()
    return resp.status_code, bytes(out), dict(resp.headers or {})


# ---------------------------------------------------------------------------
# Input staging -- body from the `data` port file or the BODY template.
# ---------------------------------------------------------------------------


def _inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


def _read_body():
    # Body bytes: BODY template config wins over an input file on `data`.
    body_env = os.getenv("BODY")
    if body_env is not None and body_env.strip() != "":
        raw = body_env.encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError(
                f"BODY template is {len(raw)} bytes, over the {MAX_BODY_BYTES} byte cap"
            )
        return raw
    candidates = [f for f in _inputs() if f.name != "fields.json"]
    if not candidates:
        raise ValueError(
            "no body: put a file on the node's `data` port or set the BODY template"
        )
    path = candidates[0]
    raw = path.read_bytes()
    if not raw:
        raise ValueError("empty body -- nothing to post")
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError(
            f"body file is {len(raw)} bytes, over the {MAX_BODY_BYTES} byte cap"
        )
    return raw


def _parse_params():
    # Read node parameters from env / fields.json (fields.json wins).
    out = {}
    p = IN / "fields.json"
    if p.is_file():
        raw = p.read_bytes()
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("fields.json exceeds the 5 MiB input cap")
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            out = {str(k).strip().lower(): v for k, v in data.items()}
    params = {}
    for key in ("url", "method", "headers_json", "signing_secret",
                "content_type", "allow_http"):
        params[key] = out.get(key, os.getenv(key.upper(), ""))
    return params


def _build_headers(params):
    # Validate + build the request headers. Refuses hop-by-hop / authority /
    # cookie overrides and the signature headers this node owns.
    raw = params.get("headers_json") or ""
    headers = {}
    if str(raw).strip():
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"HEADERS_JSON is not valid JSON -- {exc}")
        if not isinstance(data, dict):
            raise ValueError("HEADERS_JSON must be a JSON object")
        if len(data) > MAX_HEADERS:
            raise ValueError(f"HEADERS_JSON carries {len(data)} headers, cap is {MAX_HEADERS}")
        for name, value in data.items():
            name = str(name).strip()
            if not name:
                raise ValueError("HEADERS_JSON has an empty header name")
            if name.lower() in FORBIDDEN_HEADERS:
                raise ValueError(
                    f"refusing to override the {name} header"
                )
            if name.lower() in OWNED_HEADERS:
                raise ValueError(
                    f"refusing a caller-supplied {name} header -- this node signs itself"
                )
            value = str(value)
            if len(value.encode("utf-8")) > MAX_HEADER_BYTES:
                raise ValueError(
                    f"header {name} is {len(value.encode('utf-8'))} bytes, cap is {MAX_HEADER_BYTES}"
                )
            headers[name] = value
    content_type = str(params.get("content_type") or "application/json").strip()
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _sign(headers, body, signing_secret):
    # Add X-MOD-Signature (hmac-sha256 over "<ts>.<body>") + X-MOD-Timestamp.
    secret = signing_secret if isinstance(signing_secret, bytes) else str(signing_secret).encode("utf-8")
    ts = str(int(time.time()))
    digest = hmac.new(secret, ts.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    headers = dict(headers)
    headers["X-MOD-Timestamp"] = ts
    headers["X-MOD-Signature"] = f"sha256={digest}"
    return headers, ts, digest


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def op_post(paths, params):
    # POST/PUT/PATCH a body to a validated public URL, write receipt.json.
    url = str(params.get("url") or "").strip()
    if not url:
        raise ValueError("URL parameter is required")
    method = str(params.get("method") or "POST").strip().upper()
    if method not in METHODS:
        raise ValueError(f"METHOD {method!r} not in POST/PUT/PATCH")
    allow_http = str(params.get("allow_http") or "false").strip().lower() in (
        "true", "1", "yes"
    )
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "http" and not allow_http:
        raise ValueError(
            "refusing http:// -- set ALLOW_HTTP=true only if the target really is plain http"
        )
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{parsed.scheme or '(none)'}: only http/https allowed")
    body = _read_body()
    headers = _build_headers(params)
    secret = params.get("signing_secret") or ""
    if secret:
        headers, _ts, _digest = _sign(headers, body, secret)

    attempts = 0
    status = None
    resp_body = b""
    resp_headers = {}
    while True:
        status, resp_body, resp_headers = _post_once(url, body, headers, MAX_RESPONSE_BYTES)
        if status not in RETRY_STATUSES:
            break
        if attempts >= MAX_RETRIES:
            raise ValueError(
                f"upstream answered {status} after {attempts} retry(s) -- giving up"
            )
        delay = BACKOFF_BASE * (2 ** attempts)
        retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
        try:
            hinted = float(str(retry_after).strip()) if retry_after else None
        except ValueError:
            hinted = None
        if hinted is not None and 0 < hinted <= MAX_RETRY_AFTER:
            delay = hinted
        attempts += 1
        _log(f"WARN: {status} -- retry {attempts}/{MAX_RETRIES} after {delay}s")
        time.sleep(delay)

    if 300 <= status < 400:
        raise ValueError(
            f"upstream answered {status} (a redirect) -- this node never follows redirects"
        )
    if not 200 <= status < 300:
        raise ValueError(f"upstream answered {status} -- posting failed")

    preview = ""
    truncated = False
    textish = str(resp_headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if textish in ("", "text/plain", "text/html", "text/csv", "application/json"):
        preview = resp_body.decode("utf-8", errors="replace")
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS]
            truncated = True
    receipt = {
        "ok": True,
        "status": status,
        "bytes_sent": len(body),
        "response_preview": preview,
        "truncated": truncated,
        "retries_used": attempts,
    }
    _write_json("receipt.json", receipt)
    _log(f"posted {len(body)} bytes -> {status} ({attempts} retry(s))")


OPS = {
    "post": op_post,
}


def main():
    op = os.getenv("WEBHOOK_OP", "post").strip().lower()
    if op not in OPS:
        _log(f"ERROR: unknown WEBHOOK_OP {op!r}")
        _write_error(f"unknown WEBHOOK_OP {op!r}")
        sys.exit(1)
    try:
        params = _parse_params()
        _log(f"=== webhook_post: {op} ===")
        OPS[op]([], params)
    except Exception as exc:  # noqa: BLE001 -- report, never crash with a traceback
        reason = str(exc) or "operation failed"
        _log(f"ERROR: {reason}")
        _write_error(reason)
        sys.exit(1)


main()
'''

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts -- one per operation.
# ---------------------------------------------------------------------------

WEBHOOK_POST_SPEC = {
    "name": "Webhook POST",
    "description": (
        "Send a signed POST/PUT/PATCH to an external webhook endpoint. The "
        "body is the file on the node's input port (`data`) or the BODY "
        "template config (5 MiB cap). The URL must be https (http only with "
        "ALLOW_HTTP=true) and must resolve to a public IP: private, loopback, "
        "link-local, CGNAT and metadata addresses are refused before anything "
        "is sent, as are URLs with embedded userinfo; redirects are never "
        "followed. With SIGNING_SECRET the request carries X-Mod-Signature "
        "(hmac-sha256 over \"<timestamp>.<body>\") plus X-Mod-Timestamp. "
        "429/502/503/504 get at most 2 retries with backoff (Retry-After "
        "honoured up to 10 s); other 4xx/5xx fail. Emits `receipt` - status, "
        "bytes sent and a text-only response preview - never the URL, the "
        "headers or the secret. Backed by the webhook-post container (requests)."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "webhook_post",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": (
                "File whose bytes are the request body (or set BODY instead)."
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "receipt": {
            "type": "json",
            "description": (
                "receipt.json - {ok, status, bytes_sent, response_preview, "
                "truncated, retries_used}."
            ),
        },
    },
    "configuration_schema": {
        "WEBHOOK_OP": {
            "type": "string",
            "default": "post",
            "description": (
                "Fixed for this node: selects the webhook-post operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "URL": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Webhook endpoint. https only (http needs ALLOW_HTTP=true). "
                    "Treated as a secret: webhook URLs often embed a token, so "
                    "it is never echoed back. Must resolve to a public IP."
                ),
            },
            "METHOD": {
                "type": "string",
                "default": "POST",
                "description": "HTTP method: POST, PUT or PATCH.",
            },
            "HEADERS_JSON": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional JSON object of extra request headers (max 20, "
                    "each value max 1 KiB). Host, Content-Length, Transfer-"
                    "Encoding, Connection, Cookie and the signature headers "
                    "cannot be overridden."
                ),
            },
            "SIGNING_SECRET": {
                "type": "password",
                "default": "",
                "secret": True,
                "description": (
                    "Optional HMAC key. When set the request carries X-Mod-"
                    "Signature: sha256=<hmac of \"<unix ts>.<body>\"> and X-Mod-"
                    "Timestamp. Never logged or echoed."
                ),
            },
            "CONTENT_TYPE": {
                "type": "string",
                "default": "application/json",
                "description": "Content-Type for the request body.",
            },
            "BODY": {
                "type": "string",
                "default": "",
                "description": (
                    "Body template used instead of the input file (max 5 MiB). "
                    "Leave empty to post the file on the `data` port."
                ),
            },
            "ALLOW_HTTP": {
                "type": "string",
                "default": "false",
                "description": "Set true only for a target that is really plain http.",
            },
        },
    },
    "accepted_formats": ["json", "txt", "csv", "xml"],
    "cost_in_credits": 1,
}

# Registry entries, keyed by the operation id the rest of the catalog uses.
NODE_DEFINITIONS = [
    WEBHOOK_POST_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "post": WEBHOOK_POST_SPEC,
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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_webhook_post(db: Session) -> int:
    """Register the webhook-POST node as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in (
        "true", "1", "yes"
    ):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false "
            "-- skipping webhook_post domain-primitive seed"
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
