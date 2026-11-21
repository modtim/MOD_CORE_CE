"""WSIP.1k — per-tenant flags for the remaining self-improving-production phases.

Mirror of services/drift_phase1_settings.py for the five WSIP phases that
drift detection left out (spec: the internal design notes
production row 1.11 — "per-tenant runtime config flags ... defaults all
false at deploy"). Everything ships OFF everywhere and is turned on per
tenant through the DB-backed platform-settings registry — no new `tenants`
column, no env vars (settings-not-env-files).

Keys THIS module registers (group "workflows", tenant-overridable: tenant
row > platform row > default; the enable flags default False = phase off):

    wsip.failure_prediction_mode                 str   default "off"
        ("off" | "shadow" | "enforce"; design.md phase 1 runs prediction as
         a background shadow that never surfaces warnings to users)
    wsip.conversational_debug_enabled            bool  default False
    wsip.adversary_enabled                       bool  default False
    wsip.autonomous_operator_propose_enabled     bool  default False
    wsip.autonomous_operator_autodeploy_enabled  bool  default False

Failure posture: FAIL CLOSED. A settings-read error answers the SAFE value
(phase OFF / mode "off") and logs a WARNING naming only the exception CLASS
— never the message body, which can carry DB/SQL detail (GSEC). A broken
settings store must never switch a phase ON. An out-of-vocabulary mode
value reads back "off".

Rule (design.md): autonomous_operator_autodeploy_enabled is only EFFECTIVE
when autonomous_operator_propose_enabled is also true — the read helper
returns False otherwise, regardless of what the store says.

Registration happens at import (module-level `psr.register`, exactly as
services/drift_phase1_settings.py does it), so any importer of this module
gets the keys declared.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

from services import platform_settings as psr  # leaf module: no audit cycle

FAILURE_PREDICTION_MODE_KEY = "wsip.failure_prediction_mode"
CONVERSATIONAL_DEBUG_ENABLED_KEY = "wsip.conversational_debug_enabled"
ADVERSARY_ENABLED_KEY = "wsip.adversary_enabled"
AUTONOMOUS_OPERATOR_PROPOSE_ENABLED_KEY = (
    "wsip.autonomous_operator_propose_enabled")
AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED_KEY = (
    "wsip.autonomous_operator_autodeploy_enabled")

DEFAULT_FAILURE_PREDICTION_MODE = "off"
DEFAULT_CONVERSATIONAL_DEBUG_ENABLED = False
DEFAULT_ADVERSARY_ENABLED = False
DEFAULT_AUTONOMOUS_OPERATOR_PROPOSE_ENABLED = False
DEFAULT_AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED = False

FAILURE_PREDICTION_MODES = frozenset({"off", "shadow", "enforce"})

#: Keys whose degraded read has already been warned about. "Once per key"
#: keeps a broken settings store loud the first time, silent on every
#: subsequent request (same shape as drift_phase1_settings).
_DEGRADED_KEYS: set = set()


def _validate_failure_prediction_mode(value: str) -> None:
    if str(value) not in FAILURE_PREDICTION_MODES:
        raise ValueError(
            "wsip.failure_prediction_mode must be one of "
            f"{sorted(FAILURE_PREDICTION_MODES)}, got {value!r}")


psr.register(
    FAILURE_PREDICTION_MODE_KEY,
    str,
    DEFAULT_FAILURE_PREDICTION_MODE,
    validator=_validate_failure_prediction_mode,
    group="workflows",
    description="WSIP.1k — per-tenant mode for failure prediction. "
                "'off' = phase off (default at deploy); 'shadow' = predict "
                "failure probability in the background, never surfaced to "
                "users (calibration); 'enforce' = predictions may surface "
                "warnings/pre-empts. Invalid values read back 'off'.",
)
psr.register(
    CONVERSATIONAL_DEBUG_ENABLED_KEY,
    bool,
    DEFAULT_CONVERSATIONAL_DEBUG_ENABLED,
    validator=None,
    group="workflows",
    description="WSIP.1k — per-tenant switch for conversational debug (the "
                "assistant-led run post-mortem). Default False = OFF for "
                "every tenant until an operator opts in. Tenant row "
                "overrides the platform row. Read through the helpers in "
                "this module, which fail CLOSED (phase off) on a settings "
                "error.",
)
psr.register(
    ADVERSARY_ENABLED_KEY,
    bool,
    DEFAULT_ADVERSARY_ENABLED,
    validator=None,
    group="workflows",
    description="WSIP.1k — per-tenant switch for the adversary phase (self-"
                "play against the workflow). Default False = OFF for every "
                "tenant until an operator opts in. Tenant row overrides "
                "the platform row. Read through the helpers in this "
                "module, which fail CLOSED (phase off) on a settings error.",
)
psr.register(
    AUTONOMOUS_OPERATOR_PROPOSE_ENABLED_KEY,
    bool,
    DEFAULT_AUTONOMOUS_OPERATOR_PROPOSE_ENABLED,
    validator=None,
    group="workflows",
    description="WSIP.1k — per-tenant switch for autonomous-operator "
                "PROPOSALS (candidate variants offered, never deployed "
                "without review). Default False = OFF for every tenant "
                "until an operator opts in. Tenant row overrides the "
                "platform row. Read through the helpers in this module, "
                "which fail CLOSED (phase off) on a settings error.",
)
psr.register(
    AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED_KEY,
    bool,
    DEFAULT_AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED,
    validator=None,
    group="workflows",
    description="WSIP.1k — per-tenant switch for autonomous-operator "
                "AUTO-DEPLOY of candidate variants. Only EFFECTIVE while "
                "autonomous_operator_propose_enabled is also true — the "
                "read helper answers False otherwise (fail closed). "
                "Default False = OFF for every tenant until an operator "
                "opts in. Read through the helpers in this module, which "
                "fail CLOSED (phase off) on a settings error.",
)


def _read(key: str, db: Optional[Any], tenant_id: Optional[int]) -> Any:
    """One settings read (tenant row > platform row > default).

    Returns (value, error_class_name_or_None). A non-None name means the
    settings layer raised: the caller answers its SAFE value and logs the
    exception class name only — the message body (DB/SQL detail) never
    reaches the logs.
    """
    try:
        return psr.get_setting(key, db=db, tenant_id=tenant_id), None
    except Exception as exc:  # noqa: BLE001 — fail closed (phase off)
        return None, type(exc).__name__


def _log_degraded(key: str, errors: Tuple[str, ...]) -> None:
    """WARNING for a degraded read: key + exception class names only."""
    if key in _DEGRADED_KEYS:
        return
    _DEGRADED_KEYS.add(key)
    logger.warning("wsip_feature_flags: %s unreadable (%s); using the safe "
                   "value (phase off / mode off) — self-improving "
                   "production flags fail CLOSED on a settings outage",
                   key, "/".join(sorted(set(errors))))


def reset_degraded_logging() -> None:
    """Test seam: forget which keys have logged a degraded read."""
    _DEGRADED_KEYS.clear()


# --- Readers ---------------------------------------------------------------


def failure_prediction_mode(db: Optional[Any],
                            tenant_id: Optional[int] = None) -> str:
    """Prediction mode for `tenant_id` — 'off'/'shadow'/'enforce'.

    A read error or an out-of-vocabulary value answers 'off' (phase off)
    with one WARNING per key.
    """
    value, err = _read(FAILURE_PREDICTION_MODE_KEY, db, tenant_id)
    if err is not None:
        _log_degraded(FAILURE_PREDICTION_MODE_KEY, (err,))
        return DEFAULT_FAILURE_PREDICTION_MODE
    try:
        mode = str(value)
    except (TypeError, ValueError):
        _log_degraded(FAILURE_PREDICTION_MODE_KEY, ("ValueError",))
        return DEFAULT_FAILURE_PREDICTION_MODE
    if mode not in FAILURE_PREDICTION_MODES:
        _log_degraded(FAILURE_PREDICTION_MODE_KEY, ("ValueError",))
        return DEFAULT_FAILURE_PREDICTION_MODE
    return mode


def conversational_debug_enabled(db: Optional[Any],
                                 tenant_id: Optional[int] = None) -> bool:
    """True only when tenant `tenant_id` has conversational debug ON."""
    value, err = _read(CONVERSATIONAL_DEBUG_ENABLED_KEY, db, tenant_id)
    if err is not None:
        _log_degraded(CONVERSATIONAL_DEBUG_ENABLED_KEY, (err,))
        return DEFAULT_CONVERSATIONAL_DEBUG_ENABLED
    return bool(value)


def adversary_enabled(db: Optional[Any],
                      tenant_id: Optional[int] = None) -> bool:
    """True only when tenant `tenant_id` has the adversary phase ON."""
    value, err = _read(ADVERSARY_ENABLED_KEY, db, tenant_id)
    if err is not None:
        _log_degraded(ADVERSARY_ENABLED_KEY, (err,))
        return DEFAULT_ADVERSARY_ENABLED
    return bool(value)


def autonomous_operator_propose_enabled(db: Optional[Any],
                                        tenant_id: Optional[int] = None) -> bool:
    """True only when tenant `tenant_id` accepts operator proposals."""
    value, err = _read(AUTONOMOUS_OPERATOR_PROPOSE_ENABLED_KEY, db, tenant_id)
    if err is not None:
        _log_degraded(AUTONOMOUS_OPERATOR_PROPOSE_ENABLED_KEY, (err,))
        return DEFAULT_AUTONOMOUS_OPERATOR_PROPOSE_ENABLED
    return bool(value)


def autonomous_operator_autodeploy_enabled(db: Optional[Any],
                                           tenant_id: Optional[int] = None) -> bool:
    """True only when autodeploy AND propose are both ON for the tenant.

    design.md rule: autodeploy is only effective while propose is also
    true. The store may say autodeploy=True; with propose off this helper
    still answers False (fail closed). Both keys are read (so a broken
    store warns once for each); the effect of the rule is the AND.
    """
    propose = autonomous_operator_propose_enabled(db, tenant_id)
    value, err = _read(AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED_KEY, db, tenant_id)
    if err is not None:
        _log_degraded(AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED_KEY, (err,))
        return DEFAULT_AUTONOMOUS_OPERATOR_AUTODEPLOY_ENABLED
    return propose and bool(value)
