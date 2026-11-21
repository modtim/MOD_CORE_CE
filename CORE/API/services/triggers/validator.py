"""Per-kind sub-schema validator for trigger configuration.

Exposed API:

    validate_trigger_config(kind, config) -> dict

Validates a trigger's ``configuration`` dict against a per-kind schema
derived from the Pydantic model in ``schemas.WorkflowTriggerCreate``.
Raises ``ValueError`` with a human-readable message on any invalid
field. Returns the (possibly default-mutated) config dict so callers
can use the returned value directly.

This module is deliberately dependency-free: no DB, no models, no
schemas import — it is a pure validation helper that can be called
standalone or imported as a pre-hook in the trigger-create router.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Set

# ---------------------------------------------------------------------------
# Shared constants (mirrors schemas.py so this module is self-contained)
# ---------------------------------------------------------------------------

TRIGGER_TYPE_PATTERN = (
    r'^(schedule|workflow_completion|webhook|file_event|log_pattern_alert|'
    r'queue_empty|interval|one_shot|node_completion|fan_in|'
    r'system_threshold|db_row_event|'
    r'api_call|email|chatops|sms|git|'
    r'manual|approval|form|composite|'
    r'message_bus|poll|mcp_tool_call)$'
)

THRESHOLD_METRICS: Set[str] = {"queue_depth", "workers_online", "failed_runs"}
ON_STATUS_VALUES: Set[str] = {"succeeded", "failed", "any"}
FILE_EVENT_VALUES: Set[str] = {"created", "modified", "deleted", "any"}
LOG_LEVEL_VALUES: Set[str] = {"WARNING", "ERROR", "CRITICAL", "any"}
LOG_MATCH_FIELD_VALUES: Set[str] = {"message", "module", "any"}
QUEUE_SCOPE_VALUES: Set[str] = {"tenant", "workflow"}
BUCKET_PATTERN = r'^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when a trigger config fails sub-schema validation."""


def validate_trigger_config(kind: str, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Validate *config* against the sub-schema for *kind*.

    Returns the config dict (possibly with defaults filled in) on success.
    Raises ``ValidationError`` with a message naming the failing field(s).
    """
    if config is None:
        config = {}
    cfg = dict(config)  # work on a copy; defaults may mutate it

    kind_map = {
        "schedule": _validate_schedule,
        "workflow_completion": _validate_workflow_completion,
        "webhook": _validate_webhook,
        "file_event": _validate_file_event,
        "log_pattern_alert": _validate_log_pattern_alert,
        "queue_empty": _validate_queue_empty,
        "interval": _validate_interval,
        "one_shot": _validate_one_shot,
        "node_completion": _validate_node_completion,
        "fan_in": _validate_fan_in,
        "system_threshold": _validate_system_threshold,
        "db_row_event": _validate_db_row_event,
        "api_call": _validate_noop,
        "email": _validate_email,
        "chatops": _validate_chatops,
        "sms": _validate_noop,
        "git": _validate_git,
        "manual": _validate_noop,
        "approval": _validate_approval,
        "form": _validate_form,
        "composite": _validate_composite,
        "message_bus": _validate_message_bus,
        "poll": _validate_poll,
        "mcp_tool_call": _validate_mcp_tool_call,
    }

    validator = kind_map.get(kind)
    if validator is None:
        raise ValidationError(
            f"unknown trigger kind {kind!r}; "
            f"known kinds: {sorted(kind_map)}"
        )

    return validator(cfg)


# ---------------------------------------------------------------------------
# Per-kind validators — mirrors schemas.py WorkflowTriggerCreate rules
# ---------------------------------------------------------------------------


def _err(field: str, msg: str) -> ValidationError:
    return ValidationError(f"configuration.{field}: {msg}")


def _validate_schedule(cfg: dict) -> dict:
    cron = cfg.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        raise _err("cron", "is required for schedule triggers")
    parts = cron.strip().split()
    if len(parts) != 5:
        raise _err("cron",
                   "must have 5 fields: minute hour day month weekday")
    tz = cfg.get("timezone")
    if tz is not None and not isinstance(tz, str):
        raise _err("timezone", "must be a string")
    return cfg


def _validate_workflow_completion(cfg: dict) -> dict:
    src = cfg.get("source_workflow_id")
    if not isinstance(src, int):
        raise _err("source_workflow_id", "must be an int")
    on_status = cfg.get("on_status")
    if on_status not in ON_STATUS_VALUES:
        raise _err("on_status",
                   f"must be one of {sorted(ON_STATUS_VALUES)}")
    fwd = cfg.get("forward_outputs", False)
    if not isinstance(fwd, bool):
        raise _err("forward_outputs", "must be a bool")
    cfg.setdefault("forward_outputs", False)
    return cfg


def _validate_webhook(cfg: dict) -> dict:
    if "secret_hash" in cfg or "secret_token" in cfg:
        raise _err("secret_hash",
                   "is server-managed; do not supply it")
    methods = cfg.get("allowed_methods")
    if methods is None:
        cfg["allowed_methods"] = ["POST"]
    elif (not isinstance(methods, list) or not methods
          or any(not isinstance(m, str) for m in methods)):
        raise _err("allowed_methods",
                   "must be a non-empty list of strings")
    fb = cfg.get("forward_body", False)
    if not isinstance(fb, bool):
        raise _err("forward_body", "must be a bool")
    # NOTE: deliberately no setdefault here — mirrors schemas.py exactly.
    # trigger_fire_service.get_valid_webhook_trigger() reads
    # `configuration.get("forward_body", True)`, i.e. an OMITTED key means
    # "forward" at fire time. Writing a False default back into the config
    # here would silently flip that behavior for every trigger created
    # through this validator, so leave an absent key absent.
    return cfg


def _validate_file_event(cfg: dict) -> dict:
    source = cfg.get("source", "s3")
    if source not in ("s3", "minio", "fs"):
        raise _err("source", "must be 's3' (legacy alias: 'minio') or 'fs'")
    cfg.setdefault("source", source)
    if source in ("s3", "minio"):
        bucket = cfg.get("bucket")
        if not isinstance(bucket, str) or not re.match(BUCKET_PATTERN, bucket):
            raise _err("bucket", "must match 'tenant-<id>-files'")
    else:  # fs
        path = cfg.get("path")
        if not isinstance(path, str) or not path.strip():
            raise _err("path", "is required when source='fs'")
    for opt_key in ("prefix", "suffix", "glob"):
        v = cfg.get(opt_key)
        if v is not None and not isinstance(v, str):
            raise _err(opt_key, "must be a string")
    event_type = cfg.get("event_type", "created")
    if event_type not in FILE_EVENT_VALUES:
        raise _err("event_type",
                   f"must be one of {sorted(FILE_EVENT_VALUES)}")
    cfg.setdefault("event_type", event_type)
    fop = cfg.get("forward_object_path", False)
    if not isinstance(fop, bool):
        raise _err("forward_object_path", "must be a bool")
    return cfg


def _validate_log_pattern_alert(cfg: dict) -> dict:
    pattern = cfg.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise _err("pattern", "(regex string) is required")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _err("pattern", f"is not a valid regex: {exc}")
    log_level = cfg.get("log_level", "ERROR")
    if log_level not in LOG_LEVEL_VALUES:
        raise _err("log_level",
                   f"must be one of {sorted(LOG_LEVEL_VALUES)}")
    cfg.setdefault("log_level", log_level)
    match_field = cfg.get("match_field", "message")
    if match_field not in LOG_MATCH_FIELD_VALUES:
        raise _err("match_field",
                   f"must be one of {sorted(LOG_MATCH_FIELD_VALUES)}")
    cfg.setdefault("match_field", match_field)
    cooldown = cfg.get("cooldown_seconds", 60)
    if not isinstance(cooldown, int) or cooldown < 1:
        raise _err("cooldown_seconds", "must be an int >= 1")
    cfg.setdefault("cooldown_seconds", cooldown)
    return cfg


def _validate_queue_empty(cfg: dict) -> dict:
    scope = cfg.get("scope")
    if scope not in QUEUE_SCOPE_VALUES:
        raise _err("scope",
                   f"must be one of {sorted(QUEUE_SCOPE_VALUES)}")
    src = cfg.get("source_workflow_id")
    if scope == "workflow":
        if not isinstance(src, int):
            raise _err("source_workflow_id",
                       "is required when scope='workflow'")
    else:
        if src is not None:
            raise _err("source_workflow_id",
                       "must be null when scope='tenant'")
    cooldown = cfg.get("cooldown_seconds", 300)
    if not isinstance(cooldown, int) or cooldown < 30:
        raise _err("cooldown_seconds", "must be an int >= 30")
    cfg.setdefault("cooldown_seconds", cooldown)
    return cfg


def _validate_interval(cfg: dict) -> dict:
    secs = cfg.get("interval_seconds")
    if not isinstance(secs, int) or secs < 10:
        raise _err("interval_seconds", "must be an int >= 10")
    return cfg


def _validate_one_shot(cfg: dict) -> dict:
    run_at = cfg.get("run_at")
    if not isinstance(run_at, str) or not run_at.strip():
        raise _err("run_at", "(ISO 8601 datetime string) is required")
    try:
        datetime.fromisoformat(run_at.replace("Z", "+00:00"))
    except ValueError:
        raise _err("run_at", "must be a valid ISO 8601 datetime")
    return cfg


def _validate_node_completion(cfg: dict) -> dict:
    if not isinstance(cfg.get("source_workflow_id"), int):
        raise _err("source_workflow_id", "must be an int")
    if not isinstance(cfg.get("workflow_node_id"), int):
        raise _err("workflow_node_id", "must be an int")
    on_status = cfg.get("on_status", "any")
    if on_status not in ON_STATUS_VALUES:
        raise _err("on_status",
                   f"must be one of {sorted(ON_STATUS_VALUES)}")
    cfg.setdefault("on_status", on_status)
    return cfg


def _validate_fan_in(cfg: dict) -> dict:
    sources = cfg.get("sources")
    if (not isinstance(sources, list) or len(sources) < 2
            or any(not isinstance(s, int) for s in sources)):
        raise _err("sources",
                   "must be a list of >=2 source workflow ids (int)")
    win = cfg.get("window_seconds", 3600)
    if not isinstance(win, int) or win < 1:
        raise _err("window_seconds", "must be an int >= 1")
    cfg.setdefault("window_seconds", win)
    on_status = cfg.get("on_status", "succeeded")
    if on_status not in ON_STATUS_VALUES:
        raise _err("on_status",
                   f"must be one of {sorted(ON_STATUS_VALUES)}")
    cfg.setdefault("on_status", on_status)
    return cfg


def _validate_system_threshold(cfg: dict) -> dict:
    if cfg.get("metric") not in THRESHOLD_METRICS:
        raise _err("metric",
                   f"must be one of {sorted(THRESHOLD_METRICS)}")
    if cfg.get("op") not in ("gt", "lt", "gte", "lte"):
        raise _err("op", "must be one of gt/lt/gte/lte")
    if not isinstance(cfg.get("value"), (int, float)):
        raise _err("value", "(number) is required")
    rv = cfg.get("rearm_value", cfg["value"])
    if not isinstance(rv, (int, float)):
        raise _err("rearm_value", "must be a number")
    cfg.setdefault("rearm_value", rv)
    win = cfg.get("window_seconds", 300)
    if not isinstance(win, int) or win < 1:
        raise _err("window_seconds", "must be an int >= 1")
    cfg.setdefault("window_seconds", win)
    return cfg


def _validate_db_row_event(cfg: dict) -> dict:
    if not isinstance(cfg.get("table"), str) or not cfg["table"].strip():
        raise _err("table", "(str) is required")
    if cfg.get("op", "insert") not in ("insert", "update", "delete", "any"):
        raise _err("op", "must be insert/update/delete/any")
    cfg.setdefault("op", cfg.get("op", "insert"))
    flt = cfg.get("filter")
    if flt is not None and not isinstance(flt, dict):
        raise _err("filter", "must be an object")
    return cfg


def _validate_noop(_cfg: dict) -> dict:
    return _cfg  # no required fields


def _validate_email(cfg: dict) -> dict:
    if not isinstance(cfg.get("address"), str) or "@" not in cfg["address"]:
        raise _err("address", "(inbound email address) is required")
    return cfg


def _validate_chatops(cfg: dict) -> dict:
    if cfg.get("provider") not in ("slack", "teams"):
        raise _err("provider", "must be 'slack' or 'teams'")
    if not isinstance(cfg.get("signing_secret"), str) or not cfg["signing_secret"]:
        raise _err("signing_secret", "is required")
    if cfg.get("command") is not None and not isinstance(cfg["command"], str):
        raise _err("command", "must be a string")
    return cfg


def _validate_git(cfg: dict) -> dict:
    if cfg.get("provider") not in ("github", "gitlab"):
        raise _err("provider", "must be 'github' or 'gitlab'")
    if not isinstance(cfg.get("signing_secret"), str) or not cfg["signing_secret"]:
        raise _err("signing_secret", "is required")
    for opt in ("branch",):
        if cfg.get(opt) is not None and not isinstance(cfg[opt], str):
            raise _err(opt, "must be a string")
    events = cfg.get("events")
    if events is not None and (not isinstance(events, list)
                               or any(not isinstance(e, str) for e in events)):
        raise _err("events", "must be a list of strings")
    return cfg


def _validate_approval(cfg: dict) -> dict:
    appr = cfg.get("approvers")
    if appr is not None and not isinstance(appr, list):
        raise _err("approvers", "must be a list")
    return cfg


def _validate_form(cfg: dict) -> dict:
    if "secret_hash" in cfg or "secret_token" in cfg:
        raise _err("secret", "is server-managed; do not supply it")
    fields = cfg.get("fields")
    if fields is not None and not isinstance(fields, list):
        raise _err("fields", "must be a list")
    return cfg


def _validate_composite(cfg: dict) -> dict:
    if cfg.get("op") not in ("and", "or"):
        raise _err("op", "must be 'and' or 'or'")
    cons = cfg.get("constituents")
    if (not isinstance(cons, list) or len(cons) < 2
            or any(not isinstance(c, int) for c in cons)):
        raise _err("constituents",
                   "must be a list of >=2 trigger ids (int)")
    win = cfg.get("window_seconds", 3600)
    if not isinstance(win, int) or win < 1:
        raise _err("window_seconds", "must be an int >= 1")
    cfg.setdefault("window_seconds", win)
    return cfg


def _validate_message_bus(cfg: dict) -> dict:
    if cfg.get("provider") not in ("redis", "kafka", "nats", "rabbitmq", "mqtt"):
        raise _err("provider",
                   "must be redis/kafka/nats/rabbitmq/mqtt")
    if not isinstance(cfg.get("topic"), str) or not cfg["topic"].strip():
        raise _err("topic", "is required")
    return cfg


def _validate_poll(cfg: dict) -> dict:
    if cfg.get("kind") not in ("http", "rss"):
        raise _err("kind", "must be 'http' or 'rss'")
    url = cfg.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise _err("url", "must be an http(s) URL")
    # SSRF guard — mirrors schemas.py's poll validation exactly (DNS-resolves
    # the host and refuses private/loopback/link-local/reserved targets).
    # Local import: keeps this module importable with zero deps when the
    # caller only needs the non-network kinds.
    from services.triggers.streaming import url_is_blocked
    blocked = url_is_blocked(url)
    if blocked:
        raise _err("url", f"refused: {blocked}")
    secs = cfg.get("interval_seconds", 300)
    if not isinstance(secs, int) or secs < 30:
        raise _err("interval_seconds", "must be an int >= 30")
    cfg.setdefault("interval_seconds", secs)
    return cfg


_TOOL_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')

_ALLOWED_SCHEMA_MODES = {"exported", "lenient"}

_ALLOWED_OVERFLOW_POLICIES = {"drop", "defer"}


def _validate_mcp_tool_call(cfg: dict) -> dict:
    wf_vid = cfg.get("workflow_version_id")
    if isinstance(wf_vid, bool) or not isinstance(wf_vid, int) or wf_vid <= 0:
        raise _err("workflow_version_id",
                   "must be an int > 0 (not bool)")
    tn = cfg.get("tool_name")
    if not isinstance(tn, str) or not _TOOL_NAME_RE.fullmatch(tn):
        raise _err("tool_name",
                   "(str) required; must match ^[a-z][a-z0-9_-]{0,63}$")
    eid = cfg.get("exposure_id")
    if eid is not None:
        if isinstance(eid, bool) or not isinstance(eid, int) or eid <= 0:
            raise _err("exposure_id", "must be an int > 0 (not bool) or absent")
    sm = cfg.get("schema_mode", "exported")
    if sm not in _ALLOWED_SCHEMA_MODES:
        raise _err("schema_mode",
                   f"must be one of {sorted(_ALLOWED_SCHEMA_MODES)}")
    cfg.setdefault("schema_mode", sm)
    # mcpt.a4 — the auto-provisioned trigger carries the pipeline's dedup window
    # (services/triggers/pipeline.py reads cfg["dedup_seconds"]); no default is
    # filled here so the config shape stays exactly what the caller sent.
    dwin = cfg.get("dedup_seconds")
    if dwin is not None:
        if isinstance(dwin, bool) or not isinstance(dwin, int) or dwin < 0:
            raise _err("dedup_seconds", "must be an int >= 0 (not bool) or absent")
    # mcpt.a5 — optional rate_limit dict.
    if "rate_limit" in cfg:
        _validate_mcp_rate_limit(cfg["rate_limit"])
    # Reject unknown keys.
    allowed = {"workflow_version_id", "tool_name", "exposure_id", "schema_mode",
               "dedup_seconds", "rate_limit"}
    for key in cfg:
        if key not in allowed:
            raise _err(key, "is not a recognised configuration key")
    return cfg


def _validate_mcp_rate_limit(rl: Any) -> None:
    """Validate the ``rate_limit`` sub-dict for mcp_tool_call triggers."""
    if not isinstance(rl, dict):
        raise _err("rate_limit", "must be an object")
    if "max" in rl:
        v = rl["max"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise _err("rate_limit.max", "must be an int >= 1 (not bool)")
    if "per_seconds" in rl:
        v = rl["per_seconds"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise _err("rate_limit.per_seconds", "must be an int >= 1 (not bool)")
    if "overflow_policy" in rl:
        v = rl["overflow_policy"]
        if str(v).lower() not in _ALLOWED_OVERFLOW_POLICIES:
            raise _err("rate_limit.overflow_policy",
                       f"must be one of {_ALLOWED_OVERFLOW_POLICIES}")
