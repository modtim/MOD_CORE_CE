"""node_definitions_domains/email_send — SMTP email-send domain-primitive NodeDefinitions.

launch-completion-megaplan sz-4.6 (part). Registers one system node definition
backed by the email-send container (`CORE/DockerContainers/email-send`):
- send an email over SMTP (starttls or TLS), plain text and/or HTML
  (HTML present -> multipart/alternative with both parts).

Posture notes
-------------
- Specs are plain dicts so the module is importable + testable without a DB;
  `register_email_send()` is the only DB-touching entry point.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it.
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only — existing rows are
  never deleted.
- The container entrypoint lives in the Dockerfile as a quoted heredoc.
  `ENTRYPOINT_SOURCE` is the byte-identical source of that heredoc, kept so
  the unit tests can exec the real op functions without building an image.
- stdlib only in the container (smtplib + email.message) — no pip deps.
  SMTP_SECURITY is starttls|ssl only: a plaintext transport option is
  REFUSED. Certificate verification is ON (ssl.create_default_context),
  never disabled. SMTP_HOST is resolved with getaddrinfo AF_UNSPEC and every
  resolved address must be public (SSRF guard copied verbatim from the
  reviewed browser_automation.py); we then connect to the VALIDATED IP (pin)
  while TLS server_hostname/SNI stays the configured SMTP_HOST.
- Caps: subject <= 998 chars, combined body <= 1 MiB, <= 50 recipients.
  CR/LF in any header value is refused (header injection). Output receipt
  masks addresses (first char + ***@domain); password/username never appear
  in any message or log.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "email-send:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/email-send/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

EMAIL_SEND_SPEC = {
    "name": "Email Send",
    "description": (
        "Send an email over SMTP (starttls or TLS, plaintext refused). Configures "
        "SMTP host/port, username/password, sender and up to 50 recipients. Reads "
        "optional subject/body_text/body_html overrides from the node's input port "
        "(`content`); body_html present yields multipart/alternative with both "
        "parts. Subject <= 998 chars, combined body <= 1 MiB. Returns an output "
        "receipt with the message id and recipient count (addresses masked). "
        "Backed by the email-send container."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "smtp_send",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "content": {
            "type": "json",
            "description": (
                "Optional JSON overrides: {subject?, body_text?, body_html?, to?}. "
                "Absent keys fall back to the node's configuration."
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "receipt": {
            "type": "json",
            "description": "Receipt JSON: {status, recipients_count, message_id}.",
        },
    },
    "configuration_schema": {
        "EMAIL_OP": {
            "type": "string",
            "default": "smtp_send",
            "description": "Fixed for this node: email-send operation to run.",
        },
        "extra_variables": {
            "SMTP_HOST": {
                "type": "string",
                "description": "SMTP server host (resolved + SSRF-guarded before connect).",
            },
            "SMTP_PORT": {
                "type": "string",
                "default": "587",
                "description": "SMTP port (1..65535). Default 587 (submission).",
            },
            "SMTP_USERNAME": {
                "type": "secret",
                "description": "SMTP auth username (optional; AUTH skipped when empty).",
            },
            "SMTP_PASSWORD": {
                "type": "password",
                "description": "SMTP auth password.",
            },
            "SMTP_SECURITY": {
                "type": "string",
                "default": "starttls",
                "description": "Transport security: starttls (default) or ssl. Plaintext is refused.",
            },
            "EMAIL_FROM": {
                "type": "string",
                "description": "Sender address (e.g. noreply@example.com).",
            },
            "EMAIL_TO": {
                "type": "string",
                "description": "Recipient addresses, comma-separated, max 50.",
            },
            "EMAIL_SUBJECT": {
                "type": "string",
                "description": "Subject line, max 998 chars.",
            },
            "EMAIL_BODY_TEXT": {
                "type": "string",
                "description": "Plain-text body (text/plain part).",
            },
            "EMAIL_BODY_HTML": {
                "type": "string",
                "description": "Optional HTML body -> multipart/alternative with both parts.",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 1,
}

NODE_DEFINITIONS = [
    EMAIL_SEND_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "smtp_send": EMAIL_SEND_SPEC,
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
# Container entrypoint — byte-identical to the heredoc in the Dockerfile.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''
#!/usr/bin/env python3
"""email-send entrypoint. See the header comment in the Dockerfile."""

import json
import os
import re
import socket
import ssl
import sys
import time
from pathlib import Path
from email.message import EmailMessage
from email.utils import parseaddr, make_msgid
from ipaddress import ip_address, ip_network
from smtplib import SMTP, SMTP_SSL, SMTPException

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_SUBJECT_CHARS = 998
MAX_BODY_BYTES = 1024 * 1024      # 1 MiB, text + html combined
MAX_RECIPIENTS = 50
MAX_INPUT_BYTES = 10 * 1024 * 1024  # 10 MB per staged file
REQUEST_TIMEOUT = 20              # seconds, whole operation budget
SMTP_SECURITY_MODES = ("starttls", "ssl")

METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "metadata"})
METADATA_NETWORK = ip_network("169.254.0.0/16")
CGNAT_NETWORK = ip_network("100.64.0.0/10")
SITE_LOCAL_V6_NETWORK = ip_network("fec0::/10")

CRLF_RE = re.compile(r"[\r\n]")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


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


def _mask_addr(addr):
    """Mask an email address: first char + ***@domain (never echo the full form)."""
    addr = str(addr or "")
    at = addr.rfind("@")
    if at <= 0:
        return "****"
    return addr[0] + "***@" + addr[at + 1:]


def _check_header_value(name, value):
    """Refuse CR/LF in any header value (header injection)."""
    if CRLF_RE.search(value):
        raise ValueError(f"{name} carries a CR/LF — header injection refused")
    return value


def _validate_addr(addr, what):
    """parseaddr + simple domain check; returns the address on success."""
    parsed = parseaddr(addr)
    if parsed != ("", addr):
        raise ValueError(f"{what} is not a bare email address — refused")
    if "@" not in addr or " " in addr:
        raise ValueError(f"{what} {addr!r} is not a valid address — refused")
    name, domain = addr.rsplit("@", 1)
    if not name or not domain or "." not in domain:
        raise ValueError(f"{what} {addr!r} has no usable domain — refused")
    return addr


def _resolve_host(host):
    """getaddrinfo AF_UNSPEC + refuse private/loopback/link-local targets.

    Returns the validated IP to connect to (pin). TLS server_hostname stays
    the configured SMTP_HOST so SNI/cert verification still checks the name.
    """
    try:
        addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        raise ValueError(f"{host} does not resolve — refused")
    if not addrs:
        raise ValueError(f"{host}: DNS returned no addresses — refused")
    ips = set()
    for cand in {addr[4][0] for addr in addrs}:
        if _refused_addr(cand):
            raise ValueError(f"{host} resolves to {cand}: private/loopback/link-local — refused")
        for ip in _unwrap_ip(ip_address(cand)):
            ips.add(str(ip))
    if len(ips) != 1:
        raise ValueError(f"{host} resolves to several addresses — refused (pin ambiguous)")
    return ips.pop()


def read_overrides():
    """Optional JSON overrides from /data/input (subject/body_text/body_html/to)."""
    candidates = [f for f in inputs() if f.suffix.lower() == ".json"]
    if not candidates:
        return {}
    path = candidates[0]
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise SystemExit("ERROR: payload file exceeds the 10 MB input limit")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit('ERROR: payload must be a JSON object of {"subject","body_text","body_html","to"}')
    return data


def op_smtp_send():
    host = os.getenv("SMTP_HOST", "").strip().lower().rstrip(".")
    port_raw = os.getenv("SMTP_PORT", "587").strip()
    security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")

    if not host:
        raise SystemExit("ERROR: SMTP_HOST is required")
    try:
        port = int(port_raw)
    except ValueError:
        raise SystemExit(f"ERROR: SMTP_PORT must be numeric, got {port_raw!r}")
    if not (1 <= port <= 65535):
        raise SystemExit(f"ERROR: SMTP_PORT must be 1..65535, got {port}")
    if security not in SMTP_SECURITY_MODES:
        raise SystemExit(
            f"ERROR: SMTP_SECURITY must be starttls or ssl (plaintext refused), got {security!r}"
        )

    from_addr = os.getenv("EMAIL_FROM", "").strip()
    to_raw = os.getenv("EMAIL_TO", "")
    subject = os.getenv("EMAIL_SUBJECT", "")
    body_text = os.getenv("EMAIL_BODY_TEXT", "")
    body_html = os.getenv("EMAIL_BODY_HTML", "")

    overrides = read_overrides()
    subject = overrides.get("subject", subject)
    body_text = overrides.get("body_text", body_text)
    body_html = overrides.get("body_html", body_html)
    if overrides.get("to"):
        to_raw = overrides["to"]

    subject = _check_header_value("EMAIL_SUBJECT", str(subject))
    if len(subject) > MAX_SUBJECT_CHARS:
        raise SystemExit(f"ERROR: subject is over {MAX_SUBJECT_CHARS} chars — refused")
    if len(body_text.encode("utf-8")) + len(body_html.encode("utf-8")) > MAX_BODY_BYTES:
        raise SystemExit(f"ERROR: combined body is over 1 MiB — refused")

    recipients = [a.strip() for a in str(to_raw).split(",") if a.strip()]
    if not recipients:
        raise SystemExit("ERROR: EMAIL_TO needs at least one recipient")
    if len(recipients) > MAX_RECIPIENTS:
        raise SystemExit(f"ERROR: {len(recipients)} recipients — over the {MAX_RECIPIENTS} cap")
    for a in recipients:
        _validate_addr(a, "EMAIL_TO entry")
    _validate_addr(from_addr, "EMAIL_FROM")

    ip = _resolve_host(host)

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if not body_text and not body_html:
        raise SystemExit("ERROR: need at least one of EMAIL_BODY_TEXT / EMAIL_BODY_HTML")
    if body_text:
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_html, subtype="html")

    context = ssl.create_default_context()
    smtp = None
    try:
        if security == "ssl":
            smtp = SMTP_SSL(timeout=REQUEST_TIMEOUT)
            smtp._host = host
            smtp._get_socket = lambda h, p, t: context.wrap_socket(
                socket.create_connection((ip, p), t), server_hostname=host
            )
            smtp.connect(host, port)
        else:
            smtp = SMTP(timeout=REQUEST_TIMEOUT)
            smtp._host = host
            smtp._get_socket = lambda h, p, t: socket.create_connection((ip, p), t)
            smtp.connect(host, port)
            smtp.starttls(context=context)
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
    except SMTPException as exc:
        raise SystemExit(f"ERROR: smtp transport failed: {exc}")
    except OSError as exc:
        raise SystemExit(f"ERROR: connect failed: {exc}")
    finally:
        if smtp is not None:
            try:
                smtp.close()
            except OSError:
                pass

    message_id = msg["Message-ID"] or ""
    receipt = {
        "status": "sent",
        "recipients_count": len(recipients),
        "message_id": message_id,
    }
    (OUT / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    masked = ", ".join(_mask_addr(a) for a in recipients)
    log(f"email sent to {masked}: {len(recipients)} recipient(s), id={message_id}")


OPS = {
    "smtp_send": op_smtp_send,
}


def main():
    op = os.getenv("EMAIL_OP", "smtp_send").strip().lower()
    if op not in OPS:
        log(f"ERROR: unknown EMAIL_OP {op!r} (smtp_send)")
        print(json.dumps({"error": f"unknown EMAIL_OP {op!r}"}, ensure_ascii=False))
        sys.exit(1)
    log(f"=== email-send: {op} ===")
    try:
        OPS[op]()
    except (SystemExit, ValueError, OSError, KeyError) as exc:
        reason = str(exc) or "operation failed"
        log(f"ERROR: {reason}")
        print(json.dumps({"error": reason}, ensure_ascii=False))
        sys.exit(1)


main()
'''

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_email_send(db: Session) -> int:
    """Register the email-send operation as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info("MOD_SEED_BUNDLED_NODES is false — skipping email-send domain-primitive seed")
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
