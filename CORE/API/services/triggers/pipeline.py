"""add-workflow-triggers §1.3 — the one pipeline every trigger flows through.

    evaluate_and_dispatch(db, trigger, event_ctx) →
        conditions filter → dedup → rate-limit → dispatch_trigger_run → record + audit

Every trigger source (scheduler ticks, HTTP fire endpoints, event hooks) hands its
matched trigger + an event context here; this is the single place cross-cutting
policy lives, so adding a trigger TYPE is just "produce an event_ctx and call this".

event_ctx shape (all optional):
  {"event_key": <stable str for dedup>, "inputs": {<merged into run inputs>}, ...payload}

Per-trigger `configuration` keys this pipeline honors (all optional):
  condition:   {"path": "a.b", "op": "eq|ne|gt|lt|gte|lte|contains|regex|exists", "value": …,
                "time_window": {"start":"HH:MM","end":"HH:MM","tz":"UTC","days":[0-6]}}
  dedup_seconds: int   — suppress the same event_key within this window
  rate_limit:  {"max": int, "per_seconds": int}
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, time as _time_of_day, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

_expressiveness_metrics: Any = None
try:
    from services.expressiveness_metrics import (
        observe_event_trigger_to_dispatch_latency,
        record_event_trigger_dispatch,
    )
except (ImportError, ModuleNotFoundError):
    pass

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def event_key(trigger, event_ctx: Optional[Dict[str, Any]]) -> str:
    """Stable dedup key: an explicit `event_key`, else a hash of the context."""
    ctx = event_ctx or {}
    if ctx.get("event_key"):
        return str(ctx["event_key"])[:255]
    blob = json.dumps({k: v for k, v in ctx.items() if k != "inputs"},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:64]


# --- conditions ------------------------------------------------------------

def _get_path(obj: Any, path: Optional[str]) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _cmp(op: str, a: Any, b: Any) -> bool:
    try:
        if op == "exists":
            return a is not None
        if op == "eq":
            return a == b
        if op == "ne":
            return a != b
        if op == "gt":
            return a is not None and a > b
        if op == "lt":
            return a is not None and a < b
        if op == "gte":
            return a is not None and a >= b
        if op == "lte":
            return a is not None and a <= b
        if op == "contains":
            return b in (a or "")
        if op == "regex":
            return a is not None and re.search(str(b), str(a)) is not None
    except Exception:  # noqa: BLE001 — a bad comparison is a non-match, not a crash
        return False
    return False


def _in_time_window(win: Dict[str, Any]) -> bool:
    try:
        tz = ZoneInfo(win.get("tz", "UTC"))
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    now = _now().astimezone(tz)
    days = win.get("days")
    if days is not None and now.weekday() not in days:
        return False
    start, end = win.get("start"), win.get("end")
    if start and end:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        t = now.time()
        return _time_of_day(sh, sm) <= t <= _time_of_day(eh, em)
    return True


def conditions_pass(trigger, event_ctx: Optional[Dict[str, Any]]) -> bool:
    cond = (trigger.configuration or {}).get("condition")
    if not cond:
        return True
    win = cond.get("time_window")
    if win and not _in_time_window(win):
        return False
    op = cond.get("op")
    if not op:
        return True
    return _cmp(op, _get_path(event_ctx or {}, cond.get("path")), cond.get("value"))


# --- dedup / rate-limit ----------------------------------------------------

def _dedup_blocked(db, trigger, key: str, window_s: int) -> bool:
    from models import TriggerEventLog
    cutoff = _now() - timedelta(seconds=window_s)
    return db.query(TriggerEventLog.id).filter(
        TriggerEventLog.trigger_id == trigger.id,
        TriggerEventLog.event_key == key,
        TriggerEventLog.fired_at >= cutoff,
    ).first() is not None


def _rate_limited(db, trigger, max_n: int, window_s: int) -> bool:
    from models import TriggerEventLog
    cutoff = _now() - timedelta(seconds=window_s)
    n = db.query(TriggerEventLog.id).filter(
        TriggerEventLog.trigger_id == trigger.id,
        TriggerEventLog.fired_at >= cutoff,
    ).count()
    return n >= max_n


# --- 7.11 platform-default rate limit + overflow policy ---------------------
#
# A trigger's own `rate_limit` config block always wins when set. When it
# isn't, every trigger still gets a platform-wide cap (previously an
# unconfigured trigger was never rate-limited at all). `overflow_policy`
# controls what happens to a fire that exceeds the limit: "drop" (silently
# block it) or "defer" (park it in the existing trigger dead-letter sink
# instead of losing it, for later replay). The platform default is "defer":
# high-volume event-driven kinds (db_row_event, message_bus, file_event,
# node_completion, queue_empty) can legitimately burst past 1000/hr, and
# since the new platform cap applies to every trigger that doesn't set its
# own `rate_limit`, silently dropping those fires would be a behavior
# regression for them — deferring preserves the event for replay instead.
# Both knobs are platform-settings-backed (no new env vars) and
# per-trigger-overridable via `rate_limit.overflow_policy` in the trigger's
# own configuration (an operator who truly wants drop-on-overflow for one
# trigger can still set it there).

DEFAULT_RATE_LIMIT_PER_HOUR = 1000
DEFAULT_RATE_LIMIT_WINDOW_S = 3600
DEFAULT_OVERFLOW_POLICY = "defer"
_OVERFLOW_POLICIES = ("drop", "defer")

# 7.2 — at-least-once dispatch retry defaults
DEFAULT_RETRY_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_DELAY_S = 0.2
DEFAULT_RETRY_CAP_S = 10.0

# 432 — the fire endpoints run the pipeline in a threadpool offload, so the
# retry backoff must never hold a request hostage: total INLINE sleep per fire
# is capped, and once the budget is spent the remaining attempts are skipped
# (the fire dead-letters). Settings are additionally bounded (see validators).
MAX_INLINE_RETRY_SLEEP_S = 8.0
RETRY_MAX_ATTEMPTS_BOUNDS = (1, 10)
RETRY_BASE_DELAY_BOUNDS = (0.0, 2.0)
RETRY_CAP_BOUNDS = (0.0, 10.0)


def _validate_retry_attempts(value: int) -> None:
    lo, hi = RETRY_MAX_ATTEMPTS_BOUNDS
    v = int(value)
    if v < lo or v > hi:
        raise ValueError(f"triggers.retry_max_attempts must be within {lo}..{hi}, got {value!r}")


def _validate_retry_base_delay(value: float) -> None:
    lo, hi = RETRY_BASE_DELAY_BOUNDS
    v = float(value)
    if v < lo or v > hi:
        raise ValueError(f"triggers.retry_base_delay_s must be within {lo}..{hi}, got {value!r}")


def _validate_retry_cap(value: float) -> None:
    lo, hi = RETRY_CAP_BOUNDS
    v = float(value)
    if v < lo or v > hi:
        raise ValueError(f"triggers.retry_cap_s must be within {lo}..{hi}, got {value!r}")


RATE_LIMIT_PER_HOUR_BOUNDS = (0, 1_000_000)


def _validate_rate_limit_per_hour(value: int) -> None:
    """7.11 — bound for `triggers.default_rate_limit_per_hour`.

    0 means "no platform default" (unbounded unless the trigger sets its own);
    anything else must sit inside RATE_LIMIT_PER_HOUR_BOUNDS."""
    lo, hi = RATE_LIMIT_PER_HOUR_BOUNDS
    v = int(value)
    if v < lo or v > hi:
        raise ValueError(
            f"triggers.default_rate_limit_per_hour must be within {lo}..{hi}, got {value!r}"
        )


def _validate_overflow_policy(value: str) -> None:
    if str(value).strip().lower() not in _OVERFLOW_POLICIES:
        raise ValueError(f"overflow policy must be one of {_OVERFLOW_POLICIES}, got {value!r}")


def _ce_aware_default_rate_limit() -> int:
    """Community Edition has no quantity caps, so the
    platform-wide trigger fire cap defaults to 0 (unlimited) on CE; paid
    keeps DEFAULT_RATE_LIMIT_PER_HOUR (1000). ANY failure (import, edition
    probe, non-int) falls back to the PAID number, so a broken edition
    probe can never silently loosen a paid cap.

    Same shape as platform_settings.rate_limit_defaults() /
    platform_settings.feature_throttle() (the repo's existing CE-default
    pattern): lazy import of utils.paid_import.is_ce_edition inside a
    try/except, fail to paid.

    Evaluated when `_register_rate_limit_settings()` runs, which happens
    lazily on the read path (`_effective_rate_limit` at :277 and
    :477), not at module import; `is_ce_edition()` is sentinel-module
    presence (`utils/paid_import.py:73`: `not is_paid_tree()`), already
    decided by the time this module is importable, so the value is
    correct either way — see the result file for the proof.
    """
    try:
        from utils.paid_import import is_ce_edition
        value = 0 if is_ce_edition() else DEFAULT_RATE_LIMIT_PER_HOUR
    except Exception:  # noqa: BLE001 — fail to the PAID number
        return DEFAULT_RATE_LIMIT_PER_HOUR
    return int(value)


def _register_rate_limit_settings() -> None:
    from services import platform_settings as psr
    psr.register(
        "triggers.default_rate_limit_per_hour", int, _ce_aware_default_rate_limit(),
        validator=_validate_rate_limit_per_hour, group="triggers",
        description="7.11 — platform-wide fallback trigger fire cap per hour, "
                    "applied when a trigger's own `rate_limit` config doesn't "
                    "set one. CE default: 0 (unlimited; CE "
                    "has no quantity caps); 0 disables "
                    "the platform default; a trigger can still set its own.",
    )
    psr.register(
        "triggers.rate_limit_overflow_policy", str, DEFAULT_OVERFLOW_POLICY,
        validator=_validate_overflow_policy, group="triggers",
        description="7.11 — default action when a trigger exceeds its rate "
                    "limit: 'drop' (silently block the fire) or 'defer' (park "
                    "it in the trigger dead-letter sink for later replay "
                    "instead of losing it). A trigger's own "
                    "`rate_limit.overflow_policy` overrides this.",
    )
    psr.register(
        "triggers.retry_max_attempts", int, DEFAULT_RETRY_MAX_ATTEMPTS,
        validator=_validate_retry_attempts, group="triggers",
        description="7.2 — maximum dispatch retry attempts before dead-lettering "
                    "on a terminal dispatch failure. Default 5, bounded 1..10 "
                    "(432: the bound keeps a fire's inline backoff inside the "
                    "MAX_INLINE_RETRY_SLEEP_S budget).",
    )
    psr.register(
        "triggers.retry_base_delay_s", float, DEFAULT_RETRY_BASE_DELAY_S,
        validator=_validate_retry_base_delay, group="triggers",
        description="7.2 — base delay (seconds) for exponential backoff between "
                    "dispatch retries. Default 0.2, bounded 0..2.",
    )
    psr.register(
        "triggers.retry_cap_s", float, DEFAULT_RETRY_CAP_S,
        validator=_validate_retry_cap, group="triggers",
        description="7.2 — cap (seconds) on the backoff delay per retry attempt. "
                    "Default 10.0, bounded 0..10.",
    )


def _effective_rate_limit(trigger, cfg: Dict[str, Any], db) -> "tuple[int, int, str]":
    """Resolve (max_n, window_s, overflow_policy) for one trigger's fire.

    Returns max_n<=0 to mean "no limit" (platform default disabled and the
    trigger set none of its own)."""
    from services import platform_settings as psr
    _register_rate_limit_settings()
    rl = cfg.get("rate_limit") or {}
    # 7.11 — the platform fallback is per-tenant-overridable: resolve it with the
    # trigger's tenant_id so a tenant-scoped PlatformSetting row wins over the
    # platform row for that tenant's triggers (tenant row > platform row > default).
    tenant_id = getattr(trigger, "tenant_id", None)
    if rl.get("max"):
        max_n = int(rl.get("max", 0))
        window_s = int(rl.get("per_seconds", 60) or 60)
    else:
        max_n = int(psr.get_setting(
            "triggers.default_rate_limit_per_hour", db=db, tenant_id=tenant_id
        ) or 0)
        window_s = DEFAULT_RATE_LIMIT_WINDOW_S
    policy = str(
        rl.get("overflow_policy")
        or psr.get_setting(
            "triggers.rate_limit_overflow_policy", db=db, tenant_id=tenant_id
        )
        or DEFAULT_OVERFLOW_POLICY
    ).strip().lower()
    if policy not in _OVERFLOW_POLICIES:
        policy = DEFAULT_OVERFLOW_POLICY
    return max_n, window_s, policy


def _record_fire(db, trigger, key: str, dedup_window_s: int) -> None:
    from models import TriggerEventLog
    db.add(TriggerEventLog(
        trigger_id=trigger.id,
        tenant_id=getattr(trigger, "tenant_id", None),
        event_key=key,
        dedupe_until=(_now() + timedelta(seconds=dedup_window_s)) if dedup_window_s else None,
    ))
    trigger.last_triggered_at = _now()
    trigger.trigger_count = (trigger.trigger_count or 0) + 1


def _audit(db, trigger, event_name: str, extra: Dict[str, Any]) -> None:
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=getattr(trigger, "tenant_id", None),
            user_id=getattr(trigger, "created_by_user_id", None),
            event_name=event_name,
            details={"trigger_id": trigger.id,
                     "trigger_type": trigger.trigger_type.value if trigger.trigger_type else None,
                     "workflow_id": trigger.workflow_id, **extra},
        ))
    except Exception:  # noqa: BLE001 — audit must never block the fire
        logger.exception("trigger audit emit failed (%s)", event_name)


# --- 432 — BUG 2 guard: did the failed attempt already commit a run? -------


def _committed_trigger_run_id(db, trigger, baseline_id) -> Optional[int]:
    """Highest committed WorkflowRun for this trigger beyond `baseline_id`.

    `dispatch_trigger_run` commits the WorkflowRun before its post-commit work
    (refresh / thread spawn) can still raise. An exception there means the run
    DID land, so the retry loop must not dispatch a second one — this re-query
    after `db.rollback()` is what tells the two cases apart. Returns None when
    the probe finds nothing (or fails), i.e. "safe to retry".
    """
    from models import WorkflowRun
    try:
        q = db.query(WorkflowRun.id).filter(
            WorkflowRun.triggered_by_trigger_id == trigger.id,
            WorkflowRun.tenant_id == getattr(trigger, "tenant_id", None),
        )
        if baseline_id is not None:
            q = q.filter(WorkflowRun.id > baseline_id)
        row = q.order_by(WorkflowRun.id.desc()).first()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001 — a failed probe means "no run committed"
        return None


# --- 7.12 event_dispatch_dead_letter sink ----------------------------------

MAX_ERROR_CHARS = 4000


def _dead_letter(db, trigger, event_ctx: Dict[str, Any], error: str, attempts: int) -> None:
    """Park a terminally failed firing in `trigger_dead_letters`.

    Append-only (ISO 27001 A.18.1.3): the row is never deleted. The dedupe rule
    is (trigger_id, payload) among rows not yet replayed — the same payload
    failing again BEFORE a replay updates that row's attempts/last_failed_at/
    error rather than adding a second row. A counter update is allowed; a DELETE
    never is.

    Runs in a savepoint and never raises: a sink failure must not break the
    pipeline that is already handling a failure.
    """
    from models import TriggerDeadLetter
    err = (error or "")[:MAX_ERROR_CHARS]
    try:
        with db.begin_nested():
            row = db.query(TriggerDeadLetter).filter(
                TriggerDeadLetter.trigger_id == trigger.id,
                TriggerDeadLetter.replayed_at.is_(None),
                TriggerDeadLetter.payload == event_ctx,
            ).first()
            if row is None:
                row = TriggerDeadLetter(
                    tenant_id=getattr(trigger, "tenant_id", None),
                    trigger_id=trigger.id,
                    payload=event_ctx,
                    error=err,
                    attempts=attempts,
                )
                db.add(row)
            else:
                row.attempts = (row.attempts or 0) + attempts
                row.error = err
                row.last_failed_at = _now()
            db.flush()
        db.commit()
        logger.warning(
            "trigger %s dead-lettered (attempts=%s): %s", trigger.id, row.attempts, err
        )
    except Exception:  # noqa: BLE001 — the sink must never break the pipeline
        logger.exception("trigger dead-letter sink failed for trigger %s", trigger.id)


# --- the pipeline ----------------------------------------------------------

def dry_run(db, trigger, event_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate conditions/dedup/rate-limit WITHOUT dispatching — the test endpoint's
    'would this fire?' path. Returns {would_dispatch, reason, event_key}."""
    cfg = trigger.configuration or {}
    if not conditions_pass(trigger, event_ctx):
        return {"would_dispatch": False, "reason": "condition", "event_key": None}
    key = event_key(trigger, event_ctx)
    dwin = int(cfg.get("dedup_seconds", 0) or 0)
    if dwin and _dedup_blocked(db, trigger, key, dwin):
        return {"would_dispatch": False, "reason": "dedup", "event_key": key}
    max_n, window_s, _policy = _effective_rate_limit(trigger, cfg, db)
    if max_n > 0 and _rate_limited(db, trigger, max_n, window_s):
        return {"would_dispatch": False, "reason": "rate_limit", "event_key": key}
    return {"would_dispatch": True, "reason": None, "event_key": key}


def evaluate_and_dispatch(db, trigger, event_ctx: Optional[Dict[str, Any]] = None, *,
                          chain_depth: int = 0,
                          triggered_by_user_id: Optional[int] = None) -> Dict[str, Any]:
    """Run a matched trigger through conditions → dedup → rate-limit, then dispatch.
    Returns {"dispatched": bool, "reason": str|None, "run_id": int|None}."""
    _t0 = time.perf_counter()
    cfg = trigger.configuration or {}

    if not conditions_pass(trigger, event_ctx):
        _t = getattr(trigger, "trigger_type", None)
        try:
            record_event_trigger_dispatch(_t.value if _t else "unknown", "skipped")
        except Exception:  # noqa: BLE001 — metrics are best-effort
            pass
        return {"dispatched": False, "reason": "condition", "run_id": None}

    key = event_key(trigger, event_ctx)
    dwin = int(cfg.get("dedup_seconds", 0) or 0)
    if dwin and _dedup_blocked(db, trigger, key, dwin):
        _t = getattr(trigger, "trigger_type", None)
        try:
            record_event_trigger_dispatch(_t.value if _t else "unknown", "skipped")
        except Exception:  # noqa: BLE001 — metrics are best-effort
            pass
        return {"dispatched": False, "reason": "dedup", "run_id": None}

    max_n, window_s, overflow_policy = _effective_rate_limit(trigger, cfg, db)
    if max_n > 0 and _rate_limited(db, trigger, max_n, window_s):
        if overflow_policy == "defer":
            _dead_letter(db, trigger, event_ctx or {},
                         "rate_limit_exceeded: deferred for later replay", 0)
            _audit(db, trigger, "WORKFLOW_TRIGGER_BLOCKED",
                   {"reason": "rate_limit", "overflow_policy": "defer"})
            db.commit()
            _t = getattr(trigger, "trigger_type", None)
            try:
                record_event_trigger_dispatch(_t.value if _t else "unknown", "rate_limited")
            except Exception:  # noqa: BLE001 — metrics are best-effort
                pass
            return {"dispatched": False, "reason": "rate_limit_deferred", "run_id": None}
        _t = getattr(trigger, "trigger_type", None)
        try:
            record_event_trigger_dispatch(_t.value if _t else "unknown", "rate_limited")
        except Exception:  # noqa: BLE001 — metrics are best-effort
            pass
        _audit(db, trigger, "WORKFLOW_TRIGGER_BLOCKED",
               {"reason": "rate_limit", "overflow_policy": "drop"})
        db.commit()
        return {"dispatched": False, "reason": "rate_limit", "run_id": None}

    from services.workflow_orchestrator import dispatch_trigger_run
    workflow = trigger.workflow

    # 7.2 — at-least-once dispatch retry with bounded backoff
    from services import platform_settings as psr
    _register_rate_limit_settings()
    max_attempts = int(psr.get_setting("triggers.retry_max_attempts", db=db) or DEFAULT_RETRY_MAX_ATTEMPTS)
    base_delay = float(psr.get_setting("triggers.retry_base_delay_s", db=db) or DEFAULT_RETRY_BASE_DELAY_S)
    cap_s = float(psr.get_setting("triggers.retry_cap_s", db=db) or DEFAULT_RETRY_CAP_S)
    if max_attempts < 1:
        max_attempts = 1
    if max_attempts > RETRY_MAX_ATTEMPTS_BOUNDS[1]:
        max_attempts = RETRY_MAX_ATTEMPTS_BOUNDS[1]
    base_delay = min(max(base_delay, RETRY_BASE_DELAY_BOUNDS[0]), RETRY_BASE_DELAY_BOUNDS[1])
    cap_s = min(max(cap_s, RETRY_CAP_BOUNDS[0]), RETRY_CAP_BOUNDS[1])

    # BUG 2 (432) baseline: runs already recorded for this trigger before we
    # try. None-safe (no runs yet). Anything above this committed AFTER our
    # attempt is a real dispatch that merely reported an error.
    baseline_run_id = None
    try:
        from models import WorkflowRun as _WR
        _bq = db.query(_WR.id).filter(
            _WR.triggered_by_trigger_id == trigger.id,
            _WR.tenant_id == getattr(trigger, "tenant_id", None),
        ).order_by(_WR.id.desc()).first()
        baseline_run_id = int(_bq[0]) if _bq and _bq[0] is not None else None
    except Exception:  # noqa: BLE001 — None just means "any run counts as new"
        baseline_run_id = None

    _last_error: Optional[str] = None
    _attempts = 0
    run = None
    _slept_s = 0.0
    _budget_spent = False

    for attempt in range(1, max_attempts + 1):
        _attempts = attempt
        try:
            run = dispatch_trigger_run(
                db,
                workflow=workflow,
                triggered_by_user_id=triggered_by_user_id or getattr(trigger, "created_by_user_id", None),
                triggered_by_trigger_id=trigger.id,
                extra_inputs=(event_ctx or {}).get("inputs"),
                chain_depth=chain_depth,
            )
            break  # success — proceed below
        except Exception as exc:  # noqa: BLE001
            _last_error = str(exc)
            logger.warning("trigger %s dispatch attempt %d/%d failed: %s", trigger.id, attempt, max_attempts, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            # BUG 2 (432): the orchestrator commits the run BEFORE its post-commit
            # work can raise, so an exception here may still mean the run landed.
            # If a run for this trigger exists beyond the baseline, the attempt
            # SUCCEEDED — never retry (that would double-dispatch).
            _landed = _committed_trigger_run_id(db, trigger, baseline_run_id)
            if _landed is not None:
                logger.warning(
                    "trigger %s dispatch raised after committing run %s — treating as dispatched, no retry",
                    trigger.id, _landed,
                )
                _attempts = attempt
                try:
                    from models import WorkflowRun as _WR2
                    run = db.query(_WR2).filter(_WR2.id == _landed).first()
                except Exception:  # noqa: BLE001
                    run = None
                if run is None:
                    class _Landed:
                        id = _landed
                    run = _Landed()
                break
            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), cap_s)
                if _slept_s + delay > MAX_INLINE_RETRY_SLEEP_S:
                    delay = max(MAX_INLINE_RETRY_SLEEP_S - _slept_s, 0.0)
                if delay > 0:
                    _slept_s += delay
                    try:
                        time.sleep(delay)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    # 432: inline-sleep budget spent — skip the remaining attempts
                    _budget_spent = True
                    logger.warning(
                        "trigger %s retry sleep budget (%ss) spent after %d attempt(s); dead-lettering instead of looping inline",
                        trigger.id, MAX_INLINE_RETRY_SLEEP_S, attempt,
                    )
                    break

    if run is None:
        # All attempts exhausted → dead-letter
        logger.exception("trigger %s firing failed after %d attempts (dead-lettered): %s",
                         trigger.id, _attempts, _last_error)
        _t = getattr(trigger, "trigger_type", None)
        try:
            record_event_trigger_dispatch(_t.value if _t else "unknown", "error")
        except Exception:  # noqa: BLE001 — metrics are best-effort
            pass
        _dead_letter(db, trigger, event_ctx or {}, _last_error, _attempts)
        return {"dispatched": False, "reason": "dispatch_failed", "run_id": None}
    run_id = getattr(run, "id", None)
    _record_fire(db, trigger, key, dwin)
    _audit(db, trigger, "WORKFLOW_TRIGGER_FIRED", {"run_id": run_id, "event_key": key})
    db.commit()
    try:
        from services.mesh_metrics import record_trigger_fire
        record_trigger_fire(getattr(trigger, "trigger_type", "unknown"), "dispatched")
    except Exception:  # noqa: BLE001 — metrics are best-effort
        pass
    # §2.3 — advance any composite trigger this one is a constituent of (guarded
    # against nesting/loops inside notify_composites).
    try:
        from services.triggers.composite import notify_composites
        notify_composites(db, trigger)
    except Exception:  # noqa: BLE001
        logger.exception("composite notify failed after trigger %s fire", trigger.id)
    try:
        _t = getattr(trigger, "trigger_type", None)
        record_event_trigger_dispatch(_t.value if _t else "unknown", "dispatched")
    except Exception:  # noqa: BLE001 — metrics are best-effort
        pass
    try:
        observe_event_trigger_to_dispatch_latency(time.perf_counter() - _t0)
    except Exception:  # noqa: BLE001 — metrics are best-effort
        pass
    return {"dispatched": True, "reason": None, "run_id": run_id}
