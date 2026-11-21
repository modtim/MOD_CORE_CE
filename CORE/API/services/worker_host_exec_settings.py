"""services/worker_host_exec_settings.py — hx.2 (the internal design notes
tasks.md): platform settings for the host-exec capability.

Tunables are DB-backed platform settings registered through
`platform_settings.register(...)`, never new columns and never new env vars
(project rule). This module is only the registration + typed-reader half;
the dispatch/execution halves land in their own hx tasks.

Keys (all `group="worker_host_exec"`, all resolvable per tenant):
- `worker.host_exec_enabled` (bool, default False) — host-exec is opt-in;
  False keeps the feature fully inert everywhere it's gated.
- `worker.host_exec_max_output_bytes` (int, default 16777216 = 16 MiB) — cap
  on captured stdout/stderr per host-exec invocation.
- `worker.host_exec_default_timeout_s` (int, default 600, max 86400) — default
  wall-clock timeout for a host-exec invocation when the caller doesn't
  specify one.
- `worker.host_exec_workspace_root` (str, default "/var/lib/mod-worker/workspace")
  — the worker-host filesystem root host-exec commands run under.

Per the spec's D3 decision, there is deliberately NO allowlist setting here:
host-exec is gated solely by the enabled flag (plus whatever scoping its own
dispatch design applies), not by a command/path allowlist knob.

Resolution is the registry's own precedence: tenant row > platform row >
env fallback (none registered here) > registered default. A read failure
falls back to the registered default (the safe/inert value for each key)
and never raises — callers that gate on `host_exec_enabled()` must still get
an answer even when the settings layer is unhappy.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from services import platform_settings as psr  # leaf module: no audit cycle

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

GROUP = "worker_host_exec"

ENABLED_KEY = "worker.host_exec_enabled"
MAX_OUTPUT_BYTES_KEY = "worker.host_exec_max_output_bytes"
DEFAULT_TIMEOUT_S_KEY = "worker.host_exec_default_timeout_s"
WORKSPACE_ROOT_KEY = "worker.host_exec_workspace_root"
ROOT_REAUTH_MAX_AGE_S_KEY = "worker.host_exec_root_reauth_max_age_s"

_DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 600
_MAX_TIMEOUT_S = 86400
_DEFAULT_WORKSPACE_ROOT = "/var/lib/mod-worker/workspace"

_DEFAULT_ROOT_REAUTH_MAX_AGE_S = 300
_MIN_ROOT_REAUTH_MAX_AGE_S = 30
_MAX_ROOT_REAUTH_MAX_AGE_S = 3600


def _validate_root_reauth_max_age(value: int) -> None:
    if value < _MIN_ROOT_REAUTH_MAX_AGE_S or value > _MAX_ROOT_REAUTH_MAX_AGE_S:
        raise ValueError(
            "worker.host_exec_root_reauth_max_age_s must be in "
            f"[{_MIN_ROOT_REAUTH_MAX_AGE_S}, {_MAX_ROOT_REAUTH_MAX_AGE_S}]"
        )


def _validate_timeout(value: int) -> None:
    if value < 1 or value > _MAX_TIMEOUT_S:
        raise ValueError(
            f"worker.host_exec_default_timeout_s must be in [1, {_MAX_TIMEOUT_S}]"
        )


def host_exec_enabled(db: Session, tenant_id: Optional[int] = None) -> bool:
    """True when host-exec may run for `tenant_id`.

    Tenant override > platform row > registered default (False). A
    settings-read failure keeps host-exec OFF — fail closed, never open.
    """
    try:
        return bool(psr.get_setting(ENABLED_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — fail closed (off), never raise
        logger.warning(
            "worker_host_exec_settings: enabled-read failed; keeping "
            "host-exec OFF"
        )
        return False


def host_exec_max_output_bytes(db: Session, tenant_id: Optional[int] = None) -> int:
    """Max captured output bytes per host-exec invocation.

    A read failure (missing row, bad stored value) falls back to the
    registered default (16 MiB) rather than raising.
    """
    try:
        return int(psr.get_setting(MAX_OUTPUT_BYTES_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — safe default, never raise
        logger.warning(
            "worker_host_exec_settings: max-output-bytes read failed; "
            "using default %d", _DEFAULT_MAX_OUTPUT_BYTES,
        )
        return _DEFAULT_MAX_OUTPUT_BYTES


def host_exec_default_timeout_s(db: Session, tenant_id: Optional[int] = None) -> int:
    """Default wall-clock timeout (seconds) for a host-exec invocation.

    A read failure, or a stored value outside [1, 86400], falls back to the
    registered default (600s) rather than raising.
    """
    try:
        value = int(
            psr.get_setting(DEFAULT_TIMEOUT_S_KEY, db=db, tenant_id=tenant_id)
        )
    except Exception:  # noqa: BLE001 — safe default, never raise
        logger.warning(
            "worker_host_exec_settings: default-timeout read failed; "
            "using default %ds", _DEFAULT_TIMEOUT_S,
        )
        return _DEFAULT_TIMEOUT_S
    if value < 1 or value > _MAX_TIMEOUT_S:
        logger.warning(
            "worker_host_exec_settings: stored default-timeout %r out of "
            "range; using default %ds", value, _DEFAULT_TIMEOUT_S,
        )
        return _DEFAULT_TIMEOUT_S
    return value


def host_exec_workspace_root(db: Session, tenant_id: Optional[int] = None) -> str:
    """Worker-host filesystem root host-exec commands run under.

    A read failure, or an empty stored value, falls back to the registered
    default rather than raising.
    """
    try:
        value = psr.get_setting(WORKSPACE_ROOT_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — safe default, never raise
        logger.warning(
            "worker_host_exec_settings: workspace-root read failed; "
            "using default %r", _DEFAULT_WORKSPACE_ROOT,
        )
        return _DEFAULT_WORKSPACE_ROOT
    value = str(value or "").strip()
    return value or _DEFAULT_WORKSPACE_ROOT


def root_reauth_max_age_s(db: Session, tenant_id: Optional[int] = None) -> int:
    """Max age (s) of the MFA login (token auth_time) accepted for run_as=root.

    hx.16 — a read failure, or a stored value outside [30, 3600], falls back
    to the registered default (300s) rather than raising. Unlike the GY.C4b
    privileged-reauth knob there is NO off/0 value here: root host-exec is
    always step-up gated, so the window is always a positive number of
    seconds (fail closed).
    """
    try:
        value = int(
            psr.get_setting(
                ROOT_REAUTH_MAX_AGE_S_KEY, db=db, tenant_id=tenant_id
            )
        )
    except Exception:  # noqa: BLE001 — safe default, never raise
        logger.warning(
            "worker_host_exec_settings: root-reauth-max-age read failed; "
            "using default %ds", _DEFAULT_ROOT_REAUTH_MAX_AGE_S,
        )
        return _DEFAULT_ROOT_REAUTH_MAX_AGE_S
    if value < _MIN_ROOT_REAUTH_MAX_AGE_S or value > _MAX_ROOT_REAUTH_MAX_AGE_S:
        logger.warning(
            "worker_host_exec_settings: stored root-reauth-max-age %r out of "
            "range; using default %ds", value, _DEFAULT_ROOT_REAUTH_MAX_AGE_S,
        )
        return _DEFAULT_ROOT_REAUTH_MAX_AGE_S
    return value


def _register_settings() -> None:
    psr.register(
        ENABLED_KEY,
        bool,
        False,
        group=GROUP,
        description="hx.2 (worker-host-exec tasks.md) — host-exec is opt-in: "
                    "when True, the worker may run commands directly on the "
                    "host filesystem/process space instead of inside a task "
                    "container. Default False at first deploy, so an "
                    "untouched fleet never exposes host-exec. Per-tenant "
                    "overrides win over the platform row. D3: no allowlist "
                    "setting exists or is planned here.",
    )
    psr.register(
        MAX_OUTPUT_BYTES_KEY,
        int,
        _DEFAULT_MAX_OUTPUT_BYTES,
        group=GROUP,
        description="hx.2 — cap on captured stdout+stderr bytes per "
                    "host-exec invocation. Default 16 MiB.",
    )
    psr.register(
        DEFAULT_TIMEOUT_S_KEY,
        int,
        _DEFAULT_TIMEOUT_S,
        group=GROUP,
        validator=_validate_timeout,
        description="hx.2 — default wall-clock timeout (seconds) applied to "
                    "a host-exec invocation when the caller doesn't specify "
                    "one. Default 600s, max 86400s (24h).",
    )
    psr.register(
        WORKSPACE_ROOT_KEY,
        str,
        _DEFAULT_WORKSPACE_ROOT,
        group=GROUP,
        description="hx.2 — the worker-host filesystem root host-exec "
                    "commands run under. Default "
                    "/var/lib/mod-worker/workspace.",
    )
    psr.register(
        ROOT_REAUTH_MAX_AGE_S_KEY,
        int,
        _DEFAULT_ROOT_REAUTH_MAX_AGE_S,
        group=GROUP,
        validator=_validate_root_reauth_max_age,
        description="hx.16 — max age (s) of the MFA login (token auth_time) "
                    "accepted for run_as=root host exec. Default 300s; "
                    "range [30, 3600]. No off value: root exec is always "
                    "step-up gated.",
    )


_register_settings()
