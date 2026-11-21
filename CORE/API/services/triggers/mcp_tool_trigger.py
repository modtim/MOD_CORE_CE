"""mcpt.a3 — MCP ``tools/call`` receiver: exposed workflow -> trigger -> run.

``call_exposed_tool(db, tenant_id=..., user_id=..., name=..., arguments=...,
idempotency_key=...)`` resolves the caller tenant's enabled
``WorkflowMcpExposure`` row for ``name`` (its workflow must be live and in the
same tenant), finds that workflow's tenant-scoped active
``MCP_TOOL_CALL`` trigger, validates ``arguments`` against the exposure's
EXPORTED input schema (``build_workflow_mcp_schema`` of the workflow's latest
version — the very schema ``GET /tools/list`` advertises), then fires through
``services.triggers.pipeline.evaluate_and_dispatch`` with
``{"inputs": arguments, "event_key": "mcp:<exposure_id>:<idempotency_key>"}``.

Nothing here creates a run: rate-limit, dedup, conditions, dead-lettering and
audit all live in the pipeline, and this module never bypasses it.

Errors (contract fixed by the lead; the router maps them):
  * ``LookupError``  -> 404, generic, for unknown tool / foreign tenant /
    disabled exposure / soft-deleted workflow / no (or inactive) trigger.  The
    same shape for "not yours" and "not there" — no enumeration.
  * ``ValueError``   -> 400; ``args[0]`` is the offending JSON-Schema property
    path (``$`` when the whole document is wrong).

``jsonschema`` is NOT a dependency of this repo (absent from
``requirements.txt`` and from the API image), so the checker below is the
repo's own minimal one — same fail-closed style as
``services/ai_primitives/llm_call.py``: a constraint keyword it cannot enforce
is reported as a mismatch naming that property, never silently skipped.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict

from sqlalchemy.orm import Session

import models
from services.workflow_schema_export import build_workflow_mcp_schema
from services.triggers import pipeline, validator

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')


# ---------------------------------------------------------------------------
# mcpt.a5 — platform-settings registration for MCP tool-call rate limit
# ---------------------------------------------------------------------------

def _ce_aware_mcp_rate_limit_default() -> int:
    """CE has no quantity caps, so the MCP tool-call cap
    defaults to 0 (unbounded) on CE; paid keeps 1000. ANY failure falls
    back to the PAID number (same shape as
    services/triggers/pipeline.py:_ce_aware_default_rate_limit() and
    platform_settings.rate_limit_defaults())."""
    try:
        from utils.paid_import import is_ce_edition
        return 0 if is_ce_edition() else 1000
    except Exception:  # noqa: BLE001 — fail to the PAID number
        return 1000


def _register_mcp_settings() -> None:
    from services import platform_settings as psr
    psr.register(
        "mcp_tools.rate_limit_per_hour", int, _ce_aware_mcp_rate_limit_default(),
        group="mcp_tools",
        description="mcpt.a5 — per-hour cap on tool-call invocations for MCP "
                    "workflows. CE default: 0 (unbounded; "
                    "CE has no quantity caps); 0 disables "
                    "the cap; a trigger can still set its own rate_limit.",
    )
    psr.register(
        "mcp_tools.rate_limit_overflow_policy", str, "drop",
        validator=pipeline._validate_overflow_policy, group="mcp_tools",
        description="mcpt.a5 — action when the tool-call rate limit is exceeded: "
                    "'drop' (silently block) or 'defer' (park in the dead-letter "
                    "sink for later replay).",
    )


# mcpt.a5 — the rate-limit window; one hour in seconds.
_RATE_LIMIT_WINDOW_S = 3600

MCP_TOOL_CALL = models.WorkflowTriggerType.MCP_TOOL_CALL

# mcpt.a4 — the provisioning default the pipeline's dedup window reads
# (tests/test_mcp_tools_call.py:69 uses the same value).
DEFAULT_DEDUP_SECONDS = 3600

# Keywords the checker below enforces; anything else (pattern, minimum, oneOf,
# additionalProperties, ...) fails CLOSED — reported, never skipped.
_CHECKED_KEYWORDS = frozenset({"type", "required", "properties", "items", "enum"})
_METADATA_KEYWORDS = frozenset({"title", "description", "$schema", "$id",
                                "$comment", "default", "examples"})

ROOT = "$"


def _schema_errors(value: Any, schema: Any, path: str) -> list:
    """Mismatch paths for ``value`` against ``schema`` (never raises)."""
    if not isinstance(schema, dict) or not schema:
        return []
    errs: list = []
    unsupported = set(schema) - _CHECKED_KEYWORDS - _METADATA_KEYWORDS
    if unsupported:
        return [f"{path} unsupported keyword(s) {sorted(unsupported)!r}"]

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path} is not an object"]
        for req in schema.get("required") or []:
            if req not in value:
                return [f"{path} missing required property {req!r}"]
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errs.extend(_schema_errors(value[key], sub, f"{path}.{key}"))
        return errs
    if expected == "array":
        if not isinstance(value, list):
            return [f"{path} is not an array"]
        sub = schema.get("items")
        if isinstance(sub, dict):
            for i, item in enumerate(value):
                errs.extend(_schema_errors(item, sub, f"{path}[{i}]"))
        return errs
    if expected == "string":
        if not isinstance(value, str):
            return [f"{path} is not a string"]
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{path} is not an integer"]
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{path} is not a number"]
    elif expected == "boolean":
        if not isinstance(value, bool):
            return [f"{path} is not a boolean"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path} value not in enum")
    return errs


def _latest_version(db: Session, workflow_id: int):
    """Highest-id WorkflowVersion for ``workflow_id`` (same rule as tools/list)."""
    latest = None
    for ver in db.query(models.WorkflowVersion).filter(
        models.WorkflowVersion.workflow_id == workflow_id
    ).all():
        if latest is None or ver.id > latest.id:
            latest = ver
    return latest


def _node_catalog(db: Session, workflow_id: int) -> Dict[int, Any]:
    catalog: Dict[int, Any] = {}
    ids = set()
    for wn in db.query(models.WorkflowNode).filter(
        models.WorkflowNode.workflow_id == workflow_id
    ).all():
        if wn.node_definition_id is not None:
            ids.add(wn.node_definition_id)
    if not ids:
        return catalog
    for nd in db.query(models.NodeDefinition).filter(
        models.NodeDefinition.id.in_(ids)
    ).all():
        catalog[nd.id] = nd
    return catalog


def _exported_input_schema(db: Session, workflow) -> Dict[str, Any]:
    """The inputSchema the exposure ADVERTISES on tools/list (never raises)."""
    version = _latest_version(db, workflow.id)
    if version is None or not version.graph_json:
        return {}
    try:
        schema = build_workflow_mcp_schema(version.graph_json,
                                           _node_catalog(db, workflow.id))
    except Exception:  # defensive: the builder degrades, it never raises
        logger.warning("mcp_tool_trigger: schema derivation failed for workflow %s",
                       workflow.id)
        return {}
    return copy.deepcopy(schema.get("input_schema") or {})


def _rate_limit_block(max_n: int, policy: str) -> dict | None:
    """Build the rate_limit dict or None (max_n==0 → omit, platform default)."""
    if max_n <= 0:
        return None
    return {"max": max_n, "per_seconds": _RATE_LIMIT_WINDOW_S,
            "overflow_policy": policy}


def _compute_expected_rate_limit(db, tenant_id: int) -> dict | None:
    """Read the current settings and return the rate_limit dict the trigger
    *should* carry (or None if unbounded)."""
    from services import platform_settings as psr
    _register_mcp_settings()
    max_n = psr.get_setting("mcp_tools.rate_limit_per_hour", db=db,
                            tenant_id=tenant_id) or 0
    if max_n <= 0:
        return None
    policy = psr.get_setting("mcp_tools.rate_limit_overflow_policy", db=db,
                             tenant_id=tenant_id) or "drop"
    return {"max": int(max_n), "per_seconds": _RATE_LIMIT_WINDOW_S,
            "overflow_policy": policy}


def ensure_mcp_trigger(db: Session, *, tenant_id, exposure) -> Any:
    """mcpt.a4 — idempotently provision the MCP_TOOL_CALL trigger for an exposure.

    Idempotency key = (tenant_id, workflow_id, tool_name) — the same key the
    exposure table enforces via ``uq_workflow_mcp_exposure_tool``.  Returns the
    tenant's existing ACTIVE, non-deleted MCP_TOOL_CALL trigger for that key,
    otherwise creates exactly one.

    mcpt.a5: each trigger carries a ``rate_limit`` config block derived from
    platform settings (``mcp_tools.rate_limit_per_hour`` /
    ``mcp_tools.rate_limit_overflow_policy``).  When settings change, an
    existing trigger's stored rate_limit is updated in-place and flushed.

    Every query is tenant-scoped; nothing is hard-deleted (a soft-deleted
    trigger is not "the existing one", so a fresh row is created).  Flushes only
    — the caller commits.  Raises LookupError when the exposure has no tenant /
    workflow row to attach to, ValueError when the config fails validation.
    """
    tenant_id = int(tenant_id)
    _register_mcp_settings()
    if exposure is None or exposure.id is None:
        raise LookupError("mcp exposure not found for this tenant")

    # Idempotency key (tenant, workflow, tool_name).  tool_name lives in the
    # JSONB configuration, so it is matched in Python (never `.astext`).
    existing = None
    for row in (db.query(models.WorkflowTrigger)
                .filter(models.WorkflowTrigger.tenant_id == tenant_id,
                        models.WorkflowTrigger.workflow_id == exposure.workflow_id,
                        models.WorkflowTrigger.trigger_type == MCP_TOOL_CALL,
                        models.WorkflowTrigger.is_active == True,  # noqa: E712
                        models.WorkflowTrigger.deleted_at.is_(None))
                .all()):
        if (row.configuration or {}).get("tool_name") == str(exposure.tool_name):
            existing = row
            break
    if existing is not None:
        # mcpt.a5 — sync rate_limit if settings drifted
        expected = _compute_expected_rate_limit(db, tenant_id)
        actual = (existing.configuration or {}).get("rate_limit")
        logger.debug("ensure_mcp_trigger existing=%s actual=%s expected=%s",
                      existing.id, actual, expected)
        if actual != expected:
            if expected is None:
                # Replace config to ensure JSONB column detects mutation
                new_cfg = dict(existing.configuration)
                new_cfg.pop("rate_limit", None)
                existing.configuration = new_cfg
            else:
                new_cfg = dict(existing.configuration)
                new_cfg["rate_limit"] = expected
                existing.configuration = new_cfg
            db.flush()
            db.refresh(existing)
            logger.info("ensure_mcp_trigger updated rate_limit on trigger %s: %s",
                        existing.id, existing.configuration.get("rate_limit"))
        return existing

    version = _latest_version(db, exposure.workflow_id)
    if version is None:
        raise ValueError("mcp exposure has no workflow version to pin")
    from services import platform_settings as _psr
    max_n = int(_psr.get_setting("mcp_tools.rate_limit_per_hour", db=db,
                                 tenant_id=tenant_id) or 0)
    policy = _psr.get_setting("mcp_tools.rate_limit_overflow_policy", db=db,
                              tenant_id=tenant_id) or "drop"
    rl_block = _rate_limit_block(max_n, policy)
    cfg = validator.validate_trigger_config("mcp_tool_call", {
        "workflow_version_id": int(version.id),
        "tool_name": str(exposure.tool_name),
        "exposure_id": int(exposure.id),
        "schema_mode": "exported",
        "dedup_seconds": DEFAULT_DEDUP_SECONDS,
    })
    if rl_block is not None:
        cfg["rate_limit"] = rl_block

    trigger = models.WorkflowTrigger(
        workflow_id=exposure.workflow_id,
        tenant_id=tenant_id,
        trigger_type=MCP_TOOL_CALL,
        configuration=cfg,
        is_active=True,
    )
    db.add(trigger)
    db.flush()
    logger.info("mcp_tool_trigger: provisioned trigger %s for exposure %s "
                "(tenant %s, tool %s)", trigger.id, exposure.id, tenant_id,
                exposure.tool_name)
    return trigger


def call_exposed_tool(db: Session, *, tenant_id, user_id, name, arguments,
                      idempotency_key) -> Dict[str, Any]:
    """Fire one exposed-workflow MCP tool call. Returns the pipeline result.

    ``{"dispatched": bool, "reason": str|None, "run_id": int|None}`` — verbatim
    from ``pipeline.evaluate_and_dispatch``; this module adds no policy of its
    own.  Raises ``LookupError`` (unknown/foreign/disabled tool, no live
    trigger) and ``ValueError`` (arguments violate the exported input schema —
    ``args[0]`` is the offending property path).
    """
    tenant_id = int(tenant_id)

    exposure = (db.query(models.WorkflowMcpExposure)
                .filter(models.WorkflowMcpExposure.tenant_id == tenant_id,
                        models.WorkflowMcpExposure.tool_name == str(name),
                        models.WorkflowMcpExposure.enabled == True,  # noqa: E712
                        models.WorkflowMcpExposure.workflow_id.isnot(None))
                .first())
    if exposure is None:
        raise LookupError("mcp tool not exposed for this tenant")

    workflow = (db.query(models.Workflow)
                .filter(models.Workflow.id == exposure.workflow_id,
                        models.Workflow.tenant_id == tenant_id,
                        models.Workflow.deleted_at.is_(None))
                .first())
    if workflow is None:
        raise LookupError("mcp tool not exposed for this tenant")

    trigger = (db.query(models.WorkflowTrigger)
               .filter(models.WorkflowTrigger.workflow_id == workflow.id,
                       models.WorkflowTrigger.tenant_id == tenant_id,
                       models.WorkflowTrigger.trigger_type == MCP_TOOL_CALL,
                       models.WorkflowTrigger.is_active == True,  # noqa: E712
                       models.WorkflowTrigger.deleted_at.is_(None))
               .first())
    if trigger is None:
        raise LookupError("no MCP tool-call trigger for this tool")

    schema = _exported_input_schema(db, workflow)
    errs = _schema_errors(arguments, schema, ROOT)
    if errs:
        raise ValueError(errs[0].split(" ", 1)[0])

    event_ctx = {"inputs": arguments,
                 "event_key": f"mcp:{exposure.id}:{idempotency_key}"}
    result = pipeline.evaluate_and_dispatch(db, trigger, event_ctx,
                                            triggered_by_user_id=user_id)
    logger.info("mcp_tool_trigger: tool %s fired via trigger %s (tenant %s)",
                exposure.tool_name, trigger.id, tenant_id)
    return result
