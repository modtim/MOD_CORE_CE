"""node_definitions_domains/chat_webhooks — Slack + Discord webhook output nodes.

Registers two system node definitions (Slack Webhook, Discord Webhook) backed
by the chat-webhooks container.  Plain-dict specs; only
`register_chat_webhooks` touches the DB.
"""

import logging
import os
import re
import urllib.parse

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "chat-webhooks:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/chat-webhooks/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------

ALLOWED_HOSTS: dict[str, set[str]] = {
    "slack": {"hooks.slack.com"},
    "discord": {"discord.com", "discordapp.com"},
}

ALLOWED_PATH_PREFIXES: dict[str, str] = {
    "slack": "/services/",
    "discord": "/api/webhooks/",
}

MSG_CAPS: dict[str, int] = {
    "slack": 40000,
    "discord": 2000,
}

MAX_RESP_BYTES = 64 * 1024
DISCORD_429_RETRY_CAP = 5

_IP_LITERAL_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$"
    r"|^\[(?:[0-9a-fA-F:]{2,39})\]$"
    r"|^\[[0-9a-fA-F:]*%[^\]]+\]$",
)

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts
# ---------------------------------------------------------------------------

SLACK_WEBHOOK_SPEC = {
    "name": "Slack Webhook",
    "description": (
        "Send a message to a Slack channel via an incoming webhook URL. "
        "Reads the message body on the node's input port (`data`) and posts "
        "it to Slack. Supports Slack Block Kit (``BLOCKS_JSON``) for rich "
        "layouts. The webhook URL must point to "
        "``hooks.slack.com/services/...``."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "slack_webhook",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": "Message body text (or MESSAGE config overrides this).",
        },
    },
    "output_schema": {
        "output_type": "json",
        "data": {
            "type": "json",
            "description": "JSON receipt: {ok, status, provider, truncated}.",
        },
    },
    "configuration_schema": {
        "CHAT_OP": {
            "type": "string",
            "default": "slack_webhook",
            "description": "Fixed for this node: chat-webhooks operation.",
        },
        "WEBHOOK_URL": {
            "type": "string",
            "secret": True,
            "description": "Slack incoming webhook URL (https://hooks.slack.com/services/…).",
        },
        "MESSAGE": {
            "type": "string",
            "description": "Override message text; input data wins if present.",
        },
        "USERNAME": {
            "type": "string",
            "description": "Optional override for the bot username.",
        },
        "BLOCKS_JSON": {
            "type": "string",
            "description": "Optional Slack Block Kit JSON array (Slack-specific config).",
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 1,
}

DISCORD_WEBHOOK_SPEC = {
    "name": "Discord Webhook",
    "description": (
        "Send a message to a Discord channel via a webhook URL.  Reads the "
        "message body on the node's input port (``data``) and posts it to "
        "Discord. Supports Discord embeds via ``EMBEDS_JSON``.  The webhook "
        "URL must point to ``discord.com/api/webhooks/…`` or "
        "``discordapp.com/api/webhooks/…``."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "discord_webhook",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": "Message body text (or MESSAGE config overrides this).",
        },
    },
    "output_schema": {
        "output_type": "json",
        "data": {
            "type": "json",
            "description": "JSON receipt: {ok, status, provider, truncated}.",
        },
    },
    "configuration_schema": {
        "CHAT_OP": {
            "type": "string",
            "default": "discord_webhook",
            "description": "Fixed for this node: chat-webhooks operation.",
        },
        "WEBHOOK_URL": {
            "type": "string",
            "secret": True,
            "description": "Discord webhook URL (https://discord.com/api/webhooks/… or discordapp.com…).",
        },
        "MESSAGE": {
            "type": "string",
            "description": "Override message text; input data wins if present.",
        },
        "USERNAME": {
            "type": "string",
            "description": "Optional override for the bot username.",
        },
        "EMBEDS_JSON": {
            "type": "string",
            "description": "Optional Discord embeds JSON array (Discord-specific config).",
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 1,
}

NODE_DEFINITIONS = [SLACK_WEBHOOK_SPEC, DISCORD_WEBHOOK_SPEC]

NODE_DEFINITIONS_BY_OP = {
    "slack_webhook": SLACK_WEBHOOK_SPEC,
    "discord_webhook": DISCORD_WEBHOOK_SPEC,
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
# Security helpers (no network — importable on slim host).
# ---------------------------------------------------------------------------


def _detect_provider(url: str) -> str | None:
    """Return 'slack' or 'discord' if the URL matches an allowlisted host."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    host = parsed.hostname or ""
    for provider, allowed in ALLOWED_HOSTS.items():
        if host in allowed:
            return provider
    return None


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """Return (ok, error_msg).  Scheme, host, path, userinfo, port, IP checks."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Invalid URL"

    if parsed.scheme != "https":
        return False, "WEBHOOK_URL must use https:// scheme"

    host = parsed.hostname
    if not host:
        return False, "WEBHOOK_URL has no hostname"

    if _IP_LITERAL_RE.match(host):
        return False, "WEBHOOK_URL must not be an IP literal"

    if parsed.username:
        return False, "WEBHOOK_URL must not contain userinfo"

    port = parsed.port
    if port is not None and port != 443:
        return False, "WEBHOOK_URL must not specify an explicit non-443 port"

    provider = _detect_provider(url)
    if provider is None:
        return False, (
            "WEBHOOK_URL host is not allowlisted "
            f"(allowed: {', '.join(sorted(ALLOWED_HOSTS['slack'] | ALLOWED_HOSTS['discord']))})"
        )

    expected_prefix = ALLOWED_PATH_PREFIXES[provider]
    if not parsed.path.startswith(expected_prefix):
        return False, f"WEBHOOK_URL path must start with {expected_prefix}"

    return True, ""


def mask_url(url: str) -> str:
    """Return 'provider/***' so the actual URL never leaks."""
    provider = _detect_provider(url) or "webhook"
    return f"{provider}/***"


def truncate_message(text: str, provider: str) -> tuple[str, bool]:
    """Truncate to provider cap; returns (text, truncated)."""
    cap = MSG_CAPS.get(provider, 2000)
    if len(text) <= cap:
        return text, False
    return text[:cap] + "…", True

# ---------------------------------------------------------------------------
# Entrypoint operations (exec'd in-process for tests).
# ---------------------------------------------------------------------------


def _send_slack(webhook_url, message, username, blocks_json):
    """Post to Slack webhook. Returns (receipt dict)."""
    import json as _json
    import requests as _req

    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload: dict = {}
    if message:
        payload["text"] = message
    if username:
        payload["username"] = username
    if blocks_json:
        try:
            payload["blocks"] = _json.loads(blocks_json)
        except (ValueError, TypeError):
            payload["blocks"] = blocks_json

    resp = _req.post(
        webhook_url,
        json=payload,
        headers=headers,
        allow_redirects=False,
        timeout=(5, 15),
    )
    if 300 <= resp.status_code < 400:
        raise RuntimeError(f"slack webhook responded with a redirect (status {resp.status_code})")
    receipt: dict = {
        "ok": resp.ok,
        "status": resp.status_code,
        "provider": "slack",
    }
    if resp.text:
        body = resp.text[:MAX_RESP_BYTES]
        if body:
            receipt["detail"] = body
    return receipt


def _send_discord(webhook_url, message, username, embeds_json):
    """Post to Discord webhook with 429 retry. Returns (receipt dict)."""
    import json as _json
    import requests as _req
    import time as _time

    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload: dict = {}
    if message:
        payload["content"] = message
    if username:
        payload["username"] = username
    if embeds_json:
        try:
            payload["embeds"] = _json.loads(embeds_json)
        except (ValueError, TypeError):
            payload["embeds"] = embeds_json

    resp = _req.post(
        webhook_url,
        json=payload,
        headers=headers,
        allow_redirects=False,
        timeout=(5, 15),
    )

    if resp.status_code == 429:
        retry_after = 1
        try:
            ra = resp.headers.get("Retry-After")
            if ra:
                retry_after = min(int(ra), DISCORD_429_RETRY_CAP)
            elif "retry_after" in resp.json():
                retry_after = min(int(resp.json()["retry_after"]), DISCORD_429_RETRY_CAP)
        except (ValueError, KeyError):
            pass
        _time.sleep(retry_after)
        resp = _req.post(
            webhook_url,
            json=payload,
            headers=headers,
            allow_redirects=False,
            timeout=(5, 15),
        )

    if 300 <= resp.status_code < 400:
        raise RuntimeError(f"discord webhook responded with a redirect (status {resp.status_code})")
    receipt: dict = {
        "ok": resp.ok,
        "status": resp.status_code,
        "provider": "discord",
    }
    if resp.text:
        body = resp.text[:MAX_RESP_BYTES]
        if body:
            receipt["detail"] = body
    return receipt


OPS = {
    "slack_webhook": _send_slack,
    "discord_webhook": _send_discord,
}


def _entrypoint_main():
    """Entrypoint — dispatches by CHAT_OP."""
    import json as _json
    import os as _os
    import sys as _sys
    from pathlib import Path as _Path

    IN = _Path("/data/input")
    OUT = _Path("/data/output")
    OUT.mkdir(parents=True, exist_ok=True)

    op = _os.getenv("CHAT_OP", "").strip().lower()
    if op not in OPS:
        print(_json.dumps({"error": f"unknown CHAT_OP {_os.getenv('CHAT_OP')!r}"}))
        _sys.exit(1)

    try:
        input_data: str | None = None
        json_files = [f for f in IN.iterdir() if f.is_file() and f.suffix.lower() == ".json"]
        if json_files:
            input_data = json_files[0].read_text("utf-8")

        webhook_url = _os.getenv("WEBHOOK_URL", "")
        message_cfg = _os.getenv("MESSAGE", "")
        username = _os.getenv("USERNAME", "")
        extra_json = _os.getenv(
            "BLOCKS_JSON" if op == "slack_webhook" else "EMBEDS_JSON", ""
        )

        message = (
            input_data if (input_data and not message_cfg)
            else message_cfg if message_cfg
            else None
        )

        ok, err = validate_webhook_url(webhook_url)
        if not ok:
            print(_json.dumps({"error": err}))
            _sys.exit(1)

        provider = _detect_provider(webhook_url)
        truncated = False
        if message:
            message, truncated = truncate_message(message, provider)

        receipt = OPS[op](webhook_url, message, username, extra_json)
        if truncated:
            receipt["truncated"] = True

        (OUT / "receipt.json").write_text(_json.dumps(receipt, indent=2))
        print(_json.dumps(receipt))

    except SystemExit:
        raise
    except Exception as exc:
        reason = str(exc) or "operation failed"
        provider = _detect_provider(webhook_url) if "webhook_url" in dir() else None
        print(
            _json.dumps(
                {"error": reason, "provider": provider or "unknown"}
            )
        )
        _sys.exit(1)


# ---------------------------------------------------------------------------
# Container entrypoint — byte-identical to the heredoc in the Dockerfile.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''
#!/usr/bin/env python3
"""chat-webhooks entrypoint. See the header comment in the Dockerfile."""

import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")

MSG_CAPS = {
    "slack": 40000,
    "discord": 2000,
}
MAX_RESP_BYTES = 64 * 1024
DISCORD_429_RETRY_CAP = 5

ALLOWED_HOSTS = {
    "slack": {"hooks.slack.com"},
    "discord": {"discord.com", "discordapp.com"},
}

ALLOWED_PATH_PREFIXES = {
    "slack": "/services/",
    "discord": "/api/webhooks/",
}

_IP_LITERAL_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$"
    r"|^\[(?:[0-9a-fA-F:]{2,39})\]$"
    r"|^\[[0-9a-fA-F:]*%[^\]]+\]$",
)


def _detect_provider(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    host = parsed.hostname or ""
    for provider, allowed in ALLOWED_HOSTS.items():
        if host in allowed:
            return provider
    return None


def validate_webhook_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Invalid URL"

    if parsed.scheme != "https":
        return False, "WEBHOOK_URL must use https:// scheme"

    host = parsed.hostname
    if not host:
        return False, "WEBHOOK_URL has no hostname"

    if _IP_LITERAL_RE.match(host):
        return False, "WEBHOOK_URL must not be an IP literal"

    if parsed.username:
        return False, "WEBHOOK_URL must not contain userinfo"

    port = parsed.port
    if port is not None and port != 443:
        return False, "WEBHOOK_URL must not specify an explicit non-443 port"

    provider = _detect_provider(url)
    if provider is None:
        return False, "WEBHOOK_URL host is not allowlisted"

    expected_prefix = ALLOWED_PATH_PREFIXES[provider]
    if not parsed.path.startswith(expected_prefix):
        return False, f"WEBHOOK_URL path must start with {expected_prefix}"

    return True, ""


def truncate_message(text: str, provider: str) -> tuple[str, bool]:
    cap = MSG_CAPS.get(provider, 2000)
    if len(text) <= cap:
        return text, False
    return text[:cap] + "…", True


def _send_slack(webhook_url, message, username, blocks_json):
    import requests as _req
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload: dict = {}
    if message:
        payload["text"] = message
    if username:
        payload["username"] = username
    if blocks_json:
        try:
            payload["blocks"] = json.loads(blocks_json)
        except (ValueError, TypeError):
            payload["blocks"] = blocks_json
    resp = _req.post(
        webhook_url, json=payload, headers=headers,
        allow_redirects=False, timeout=(5, 15),
    )
    if 300 <= resp.status_code < 400:
        raise RuntimeError(f"slack webhook responded with a redirect (status {resp.status_code})")
    receipt = {
        "ok": resp.ok,
        "status": resp.status_code,
        "provider": "slack",
    }
    if resp.text:
        body = resp.text[:MAX_RESP_BYTES]
        if body:
            receipt["detail"] = body
    return receipt


def _send_discord(webhook_url, message, username, embeds_json):
    import requests as _req
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload: dict = {}
    if message:
        payload["content"] = message
    if username:
        payload["username"] = username
    if embeds_json:
        try:
            payload["embeds"] = json.loads(embeds_json)
        except (ValueError, TypeError):
            payload["embeds"] = embeds_json
    resp = _req.post(
        webhook_url, json=payload, headers=headers,
        allow_redirects=False, timeout=(5, 15),
    )
    if resp.status_code == 429:
        retry_after = 1
        try:
            ra = resp.headers.get("Retry-After")
            if ra:
                retry_after = min(int(ra), DISCORD_429_RETRY_CAP)
            elif "retry_after" in resp.json():
                retry_after = min(int(resp.json()["retry_after"]), DISCORD_429_RETRY_CAP)
        except (ValueError, KeyError):
            pass
        time.sleep(retry_after)
        resp = _req.post(
            webhook_url, json=payload, headers=headers,
            allow_redirects=False, timeout=(5, 15),
        )
    if 300 <= resp.status_code < 400:
        raise RuntimeError(f"discord webhook responded with a redirect (status {resp.status_code})")
    receipt = {
        "ok": resp.ok,
        "status": resp.status_code,
        "provider": "discord",
    }
    if resp.text:
        body = resp.text[:MAX_RESP_BYTES]
        if body:
            receipt["detail"] = body
    return receipt


OPS = {
    "slack_webhook": _send_slack,
    "discord_webhook": _send_discord,
}


def main():
    IN.mkdir(parents=True, exist_ok=True)

    op = os.getenv("CHAT_OP", "").strip().lower()
    if op not in OPS:
        print(json.dumps({"error": f"unknown CHAT_OP {os.getenv('CHAT_OP')!r}"}))
        sys.exit(1)

    try:
        input_data = None
        json_files = [f for f in IN.iterdir() if f.is_file() and f.suffix.lower() == ".json"]
        if json_files:
            input_data = json_files[0].read_text("utf-8")

        webhook_url = os.getenv("WEBHOOK_URL", "")
        message_cfg = os.getenv("MESSAGE", "")
        username = os.getenv("USERNAME", "")
        extra_json = os.getenv(
            "BLOCKS_JSON" if op == "slack_webhook" else "EMBEDS_JSON", ""
        )

        message = (
            input_data if (input_data and not message_cfg)
            else message_cfg if message_cfg
            else None
        )

        ok, err = validate_webhook_url(webhook_url)
        if not ok:
            print(json.dumps({"error": err}))
            sys.exit(1)

        provider = _detect_provider(webhook_url)
        truncated = False
        if message:
            message, truncated = truncate_message(message, provider)

        receipt = OPS[op](webhook_url, message, username, extra_json)
        if truncated:
            receipt["truncated"] = True

        (Path("/data/output") / "receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))

    except SystemExit:
        raise
    except Exception as exc:
        reason = str(exc) or "operation failed"
        try:
            provider = _detect_provider(webhook_url)
        except NameError:
            provider = "unknown"
        print(json.dumps({"error": reason, "provider": provider or "unknown"}))
        sys.exit(1)


main()
'''

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_chat_webhooks(db: Session) -> int:
    """Register the 2 chat-webhook operations as system node definitions.

    Returns the number of NEW rows created. Idempotent.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info("MOD_SEED_BUNDLED_NODES is false — skipping chat-webhooks seed")
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
                tenant_id=None,
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
