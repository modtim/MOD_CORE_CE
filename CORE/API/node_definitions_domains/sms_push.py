"""node_definitions_domains/sms_push — Twilio SMS + Pushover push domain-primitive NodeDefinitions.

launch-completion-megaplan sz-4.6b (part b). Registers two system node definitions
backed by the sms-push container (`CORE/DockerContainers/sms-push`):
- send a Twilio SMS (POST to api.twilio.com with basic auth)
- send a Pushover push notification (POST to api.pushover.net)

Posture notes
-------------
- Specs are plain dicts so the module is importable + testable without a DB;
  `register_sms_push()` is the only DB-touching entry point.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it.
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only — existing rows are
  never deleted.
- The container entrypoint lives in the Dockerfile as a quoted heredoc.
  `ENTRYPOINT_SOURCE` is the byte-identical source of that heredoc, kept so
  the unit tests can exec the real op functions without building an image.
- Hosts are HARD-CODED constants (no user-supplied URL). allow_redirects=False.
  Message caps: SMS 1600 chars, Pushover 1024 chars → truncate with flag.
  Output receipt masks phone numbers (last 4 digits).
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "sms-push:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/sms-push/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

TWILIO_SMS_SPEC = {
    "name": "Twilio SMS",
    "description": (
        "Send an SMS message via Twilio. Configures account SID, auth token, "
        "from/to phone numbers (E.164). Reads the message from the node's input "
        "port (`message`), truncates to 1600 chars if larger. Returns an output "
        "receipt with the provider message id. Backed by the sms-push container."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "twilio_sms",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "message": {
            "type": "text",
            "description": "Message text to send. Max 1600 chars.",
        },
    },
    "output_schema": {
        "output_type": "json",
        "receipt": {
            "type": "json",
            "description": "Receipt JSON: {ok, status, provider, id?: msg_id, to_masked, truncated?}.",
        },
    },
    "configuration_schema": {
        "NOTIFY_OP": {
            "type": "string",
            "default": "twilio_sms",
            "description": "Fixed for this node: sms-push operation to run.",
        },
        "extra_variables": {
            "TWILIO_ACCOUNT_SID": {
                "type": "secret",
                "description": "Twilio Account SID (AC + 32 hex chars).",
            },
            "TWILIO_AUTH_TOKEN": {
                "type": "password",
                "description": "Twilio Auth Token.",
            },
            "TWILIO_FROM_NUMBER": {
                "type": "string",
                "description": "Sender phone number in E.164 format (e.g. +12025551234).",
            },
            "TWILIO_TO_NUMBER": {
                "type": "string",
                "description": "Recipient phone number in E.164 format (e.g. +12025551234).",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 1,
}

PUSHOVER_PUSH_SPEC = {
    "name": "Pushover Push",
    "description": (
        "Send a push notification via Pushover. Configures app token and user "
        "key. Reads the message from the node's input port (`message`), truncates "
        "to 1024 chars if larger. Title and priority are configurable "
        "(priority -2..1 only). Returns an output receipt. Backed by the "
        "sms-push container."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pushover_push",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "message": {
            "type": "text",
            "description": "Message text to send. Max 1024 chars.",
        },
    },
    "output_schema": {
        "output_type": "json",
        "receipt": {
            "type": "json",
            "description": "Receipt JSON: {ok, status, provider, id?: msg_id, truncated?}.",
        },
    },
    "configuration_schema": {
        "NOTIFY_OP": {
            "type": "string",
            "default": "pushover_push",
            "description": "Fixed for this node: sms-push operation to run.",
        },
        "extra_variables": {
            "PUSHOVER_APP_TOKEN": {
                "type": "secret",
                "description": "Pushover application token.",
            },
            "PUSHOVER_USER_KEY": {
                "type": "secret",
                "description": "Pushover user/key recipient.",
            },
            "PUSHOVER_TITLE": {
                "type": "string",
                "default": "MOD",
                "description": "Notification title.",
            },
            "PUSHOVER_PRIORITY": {
                "type": "string",
                "default": "0",
                "description": "Priority: -2 (lowest), -1 (low), 0 (normal), 1 (high). 2 (emergency) NOT allowed.",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 1,
}

NODE_DEFINITIONS = [
    TWILIO_SMS_SPEC,
    PUSHOVER_PUSH_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "twilio_sms": TWILIO_SMS_SPEC,
    "pushover_push": PUSHOVER_PUSH_SPEC,
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
"""sms-push entrypoint. See the header comment in the Dockerfile."""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

IN = Path("/data/input")
OUT = Path("/data/output")

# Hard-coded hosts — no user-supplied URL.
TWILIO_HOST = "https://api.twilio.com"
TWILIO_PATH = "/2010-04-01/Accounts/{sid}/Messages.json"
PUSHOVER_HOST = "https://api.pushover.net"
PUSHOVER_PATH = "/1/messages.json"

MAX_SMS_CHARS = 1600
MAX_PUSHOVER_CHARS = 1024
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
SID_RE = re.compile(r"^AC[0-9a-f]{32}$")

# Request limits
MAX_RESPONSE_BYTES = 64 * 1024  # 64 KiB read cap
REQUEST_TIMEOUT = (5, 15)       # connect, read


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


def read_payload():
    """Read the JSON payload from /data/input/payload.json (or any .json file)."""
    candidates = [f for f in inputs() if f.suffix.lower() == ".json"]
    if not candidates:
        raise SystemExit("ERROR: sms-push needs a .json payload on its input port")
    path = candidates[0]
    raw = path.read_bytes()
    if len(raw) > 10 * 1024 * 1024:
        raise SystemExit("ERROR: payload file exceeds the 10 MB input limit")
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict):
        value = data.get("message")
    else:
        value = data
    if not isinstance(value, str):
        raise SystemExit("ERROR: payload must be a JSON string or {\"message\": \"...\"}")
    return value


def mask_phone(number):
    """Mask a phone number to show only the last 4 digits."""
    if number and len(number) > 4 and number.startswith("+"):
        return "*" * (len(number) - 4) + number[-4:]
    if number and len(number) > 4:
        return "*" * (len(number) - 4) + number[-4:]
    return "****"


def post_to_host(url, auth, data, retries=1):
    """POST to a hard-coded host with retry on 429/5xx.

    Returns (ok, status, body_text, provider_id|None).
    Never retries on 3xx — redirect is always an error (allow_redirects=False).
    """
    for attempt in range(1 + retries):
        resp = requests.post(
            url,
            auth=auth,
            data=data,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        # 3xx → hard error (no retry)
        if resp.status_code >= 300 and resp.status_code < 400:
            raise SystemExit(
                f"ERROR: got redirect {resp.status_code}, "
                f"location: {resp.headers.get('Location', 'unknown')}"
            )
        if resp.status_code in (429,) or resp.status_code >= 500:
            if attempt < retries:
                backoff = min(5 * (attempt + 1), 5)
                log(f"WARN: {resp.status_code}, retrying in {backoff}s")
                import time
                time.sleep(backoff)
                continue
            raise SystemExit(
                f"ERROR: provider returned {resp.status_code}"
            )
        if resp.status_code >= 400:
            raise SystemExit(
                f"ERROR: provider returned {resp.status_code}"
            )

        # Success — read body with cap
        body_bytes = resp.raw.read(MAX_RESPONSE_BYTES, decode_content=True)
        try:
            body_text = body_bytes.decode("utf-8", errors="replace")
        except Exception:
            body_text = ""

        # Try to extract provider message id
        provider_id = None
        try:
            resp_json = json.loads(body_text)
            provider_id = resp_json.get("sid") or resp_json.get("id")
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        return True, resp.status_code, body_text, provider_id


def op_twilio_sms():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "")
    to_number = os.getenv("TWILIO_TO_NUMBER", "")

    # Validate SID
    if not SID_RE.match(sid):
        raise SystemExit(f"ERROR: TWILIO_ACCOUNT_SID must match ^AC[0-9a-f]{{32}}$, got {sid!r}")

    # Validate phone numbers (error text is masked — never echo a full number)
    if not E164_RE.match(from_number):
        raise SystemExit(
            f"ERROR: TWILIO_FROM_NUMBER must be E.164, got {mask_phone(from_number)!r}"
        )
    if not E164_RE.match(to_number):
        raise SystemExit(
            f"ERROR: TWILIO_TO_NUMBER must be E.164, got {mask_phone(to_number)!r}"
        )

    message = read_payload()

    # Truncate with flag
    truncated = len(message) > MAX_SMS_CHARS
    if truncated:
        message = message[:MAX_SMS_CHARS]

    to_masked = mask_phone(to_number)

    url = f"{TWILIO_HOST}/2010-04-01/Accounts/{sid}/Messages.json"
    ok, status, body, provider_id = post_to_host(
        url, (sid, token), {"From": from_number, "To": to_number, "Body": message}
    )

    receipt = {"ok": ok, "status": status, "provider": "twilio", "to_masked": to_masked}
    if provider_id:
        receipt["id"] = provider_id
    if truncated:
        receipt["truncated"] = True

    (OUT / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    log(f"sms sent to {to_masked}: status {status}, truncated={truncated}")


def op_pushover_push():
    app_token = os.getenv("PUSHOVER_APP_TOKEN", "")
    user_key = os.getenv("PUSHOVER_USER_KEY", "")
    title = os.getenv("PUSHOVER_TITLE", "MOD")
    priority_raw = os.getenv("PUSHOVER_PRIORITY", "0")

    # Validate priority — only -2..1 allowed
    try:
        priority = int(priority_raw)
    except ValueError:
        raise SystemExit(f"ERROR: PUSHOVER_PRIORITY must be -2..1, got {priority_raw!r}")
    if priority not in (-2, -1, 0, 1):
        raise SystemExit(f"ERROR: PUSHOVER_PRIORITY must be -2..1 (2=emergency NOT allowed), got {priority}")

    message = read_payload()

    # Truncate with flag
    truncated = len(message) > MAX_PUSHOVER_CHARS
    if truncated:
        message = message[:MAX_PUSHOVER_CHARS]

    url = f"{PUSHOVER_HOST}/1/messages.json"
    ok, status, body, provider_id = post_to_host(
        url, None, {
            "token": app_token,
            "user": user_key,
            "message": message,
            "title": title,
            "priority": str(priority),
        }
    )

    receipt = {"ok": ok, "status": status, "provider": "pushover"}
    if provider_id:
        receipt["id"] = provider_id
    if truncated:
        receipt["truncated"] = True

    (OUT / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    log(f"pushover sent: status {status}, truncated={truncated}")


OPS = {
    "twilio_sms": op_twilio_sms,
    "pushover_push": op_pushover_push,
}


def main():
    op = os.getenv("NOTIFY_OP", "twilio_sms").strip().lower()
    if op not in OPS:
        log(f"ERROR: unknown NOTIFY_OP {op!r} (twilio_sms, pushover_push)")
        print(json.dumps({"error": f"unknown NOTIFY_OP {op!r}"}, ensure_ascii=False))
        sys.exit(1)
    log(f"=== sms-push: {op} ===")
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


def register_sms_push(db: Session) -> int:
    """Register the 2 SMS/push operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info("MOD_SEED_BUNDLED_NODES is false — skipping SMS/push domain-primitive seed")
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
