"""Module-level cache for the scheduler runtime config.

Singleton-shaped: reads from the `scheduler_runtime_config` table at
API startup (and on every successful POST to
`/platform/scheduler-runtime-config`). Falls back through env vars and
in-code defaults when the table is empty so a fresh deploy behaves
exactly like today.

Concurrency: an `RLock` guards the cache pointer swap during refresh.
Per-knob getters are non-blocking dict reads.
"""

from __future__ import annotations

from product_names import themed_logger
import os
import threading
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

import models

log = themed_logger(__name__, 'scheduler')


# Documented presets (kept in sync with proposal.md / design.md).
@dataclass(frozen=True)
class _Preset:
    name: str
    capacity_floor_pct: int
    packer_full_above_util_pct: int
    variance_penalty_s: int
    background_max_fleet_pct: int


PRESETS: dict[str, _Preset] = {
    "aggressive": _Preset("aggressive", capacity_floor_pct=10,
                          packer_full_above_util_pct=85, variance_penalty_s=5,
                          background_max_fleet_pct=60),
    "normal": _Preset("normal", capacity_floor_pct=30,
                      packer_full_above_util_pct=70, variance_penalty_s=15,
                      background_max_fleet_pct=40),
    "light": _Preset("light", capacity_floor_pct=50,
                     packer_full_above_util_pct=50, variance_penalty_s=30,
                     background_max_fleet_pct=20),
}

# In-code defaults — must match the Normal preset.
DEFAULT_CAPACITY_FLOOR_PCT = 30
DEFAULT_PACKER_FULL_ABOVE_UTIL_PCT = 70
DEFAULT_VARIANCE_PENALTY_S = 15
DEFAULT_BACKGROUND_MAX_FLEET_PCT = 40


@dataclass(frozen=True)
class SchedulerRuntimeConfigSnapshot:
    capacity_floor_pct: int
    packer_full_above_util_pct: int
    variance_penalty_s: int
    background_max_fleet_pct: int
    preset_name: str
    source: str  # 'db' | 'env' | 'default'


_lock = threading.RLock()
_cached: Optional[SchedulerRuntimeConfigSnapshot] = None


def detect_preset(capacity_floor_pct: int,
                  packer_full_above_util_pct: int,
                  variance_penalty_s: int,
                  background_max_fleet_pct: int = DEFAULT_BACKGROUND_MAX_FLEET_PCT) -> str:
    """Exact-match preset detection per design Decision 5."""
    for name, preset in PRESETS.items():
        if (capacity_floor_pct == preset.capacity_floor_pct
                and packer_full_above_util_pct == preset.packer_full_above_util_pct
                and variance_penalty_s == preset.variance_penalty_s
                and background_max_fleet_pct == preset.background_max_fleet_pct):
            return name
    return "custom"


def _resolve_from_env_or_default() -> SchedulerRuntimeConfigSnapshot:
    """Build a snapshot from env vars (then in-code defaults).

    Source is `env` if any of the three env vars is set, else `default`.
    """
    env_capacity = os.getenv("CALIBRATION_CAPACITY_FLOOR_PCT")
    env_packer = os.getenv("PACKER_2OPT_FULL_ABOVE_UTIL_PCT")
    env_variance = os.getenv("PACKER_VARIANCE_PENALTY_S")
    env_bg = os.getenv("BACKGROUND_MAX_FLEET_PCT")

    any_env_set = any(v is not None for v in (env_capacity, env_packer, env_variance, env_bg))

    capacity_floor_pct = _safe_int(env_capacity, DEFAULT_CAPACITY_FLOOR_PCT)
    packer_full_above_util_pct = _safe_int(env_packer, DEFAULT_PACKER_FULL_ABOVE_UTIL_PCT)
    variance_penalty_s = _safe_int(env_variance, DEFAULT_VARIANCE_PENALTY_S)
    background_max_fleet_pct = _safe_int(env_bg, DEFAULT_BACKGROUND_MAX_FLEET_PCT)

    return SchedulerRuntimeConfigSnapshot(
        capacity_floor_pct=capacity_floor_pct,
        packer_full_above_util_pct=packer_full_above_util_pct,
        variance_penalty_s=variance_penalty_s,
        background_max_fleet_pct=background_max_fleet_pct,
        preset_name=detect_preset(capacity_floor_pct, packer_full_above_util_pct,
                                  variance_penalty_s, background_max_fleet_pct),
        source="env" if any_env_set else "default",
    )


def _safe_int(raw: Optional[str], fallback: int) -> int:
    if raw is None or raw == "":
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("scheduler_runtime_cache: malformed env value %r — using fallback %d", raw, fallback)
        return fallback


def refresh(db: Session) -> SchedulerRuntimeConfigSnapshot:
    """Reload the cache from the DB row (if present) or env/default.

    Called once at API startup, and again by the POST endpoint after a
    successful write. Uses the documented fallback chain:
      DB row → env var → in-code default.
    """
    global _cached
    snapshot: SchedulerRuntimeConfigSnapshot

    try:
        row = (
            db.query(models.SchedulerRuntimeConfig)
            .filter(models.SchedulerRuntimeConfig.singleton_id == 1)
            .one_or_none()
        )
    except Exception as e:  # noqa: BLE001 — DB hiccup must not crash startup
        log.warning("scheduler_runtime_cache: DB read failed (%s) — falling back to env", e)
        row = None

    if row is None:
        snapshot = _resolve_from_env_or_default()
    else:
        snapshot = SchedulerRuntimeConfigSnapshot(
            capacity_floor_pct=int(row.capacity_floor_pct),
            packer_full_above_util_pct=int(row.packer_full_above_util_pct),
            variance_penalty_s=int(row.variance_penalty_s),
            background_max_fleet_pct=int(
                getattr(row, "background_max_fleet_pct", None)
                or DEFAULT_BACKGROUND_MAX_FLEET_PCT
            ),
            preset_name=row.preset_name,
            source="db",
        )

    with _lock:
        _cached = snapshot
    log.debug("scheduler_runtime_cache: refreshed %s", snapshot)
    return snapshot


def get_resolved() -> SchedulerRuntimeConfigSnapshot:
    """Return the current cached snapshot.

    If the cache hasn't been initialised yet (e.g. an early caller fires
    before startup finished), resolve from env/default on the fly without
    touching the DB.
    """
    with _lock:
        if _cached is not None:
            return _cached
    return _resolve_from_env_or_default()


def get_capacity_floor_pct() -> int:
    return get_resolved().capacity_floor_pct


def get_packer_full_above_util_pct() -> int:
    return get_resolved().packer_full_above_util_pct


def get_variance_penalty_s() -> int:
    return get_resolved().variance_penalty_s


def get_background_max_fleet_pct() -> int:
    return get_resolved().background_max_fleet_pct


def reset_for_tests() -> None:
    """Clear the cache pointer. Test-only helper."""
    global _cached
    with _lock:
        _cached = None
