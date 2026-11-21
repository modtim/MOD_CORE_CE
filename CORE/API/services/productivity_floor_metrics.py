"""sz-productivity-floor §10.1/§10.2 — Prometheus gauges + histograms for the
productivity floor (paused runs, external OAuth connections, output-delivery
health, CRDT concurrent editors) plus the three duration histograms the
productivity features need at /metrics.

Pattern note (the brief asks which pattern the existing modules use): the
sibling metrics modules (services/mesh_metrics.py, services/wizard_metrics.py)
do NOT compute at scrape time — they register lazily on the default REGISTRY
and expose a `refresh_from_db(db)` that recomputes the DB-derived gauges, driven
by a periodic loop. This module follows that pattern: `refresh_gauges(db)` is
the collector, called by the wiring task's scheduler, and the CRDT gauge has
only a setter because there is no source of truth yet.

  - paused_runs_count{tenant_id}                     (gauge, DB)
  - oauth_connections_count{provider,status}         (gauge, DB)
  - output_delivery_success_rate{output_kind}        (gauge, DB, last 24h)
  - crdt_concurrent_editors_count{workflow_id}       (gauge, setter only)
  - cost_forecaster_duration_seconds                 (histogram)
  - live_debugger_pause_duration_seconds             (histogram)
  - oauth_token_refresh_duration_seconds             (histogram)
  - cost_aware_router_savings_total{tenant_id}       (counter, call-site)
  - spot_preemption_count{tenant_id}                 (counter, call-site)

Design notes:

  - Registration is check-then-register (same as wizard/mesh/expressiveness):
    a duplicate name on reload / a second uvicorn worker is tolerated, so
    re-importing never raises "Duplicated timeseries".
  - prometheus_client is imported lazily; every helper no-ops when the dep is
    missing or registration failed, and NEVER raises out.
  - Label cardinality is bounded on purpose: tenant_id / workflow_id are
    emitted as plain str ints only (anything else is dropped), tenant_id rolls
    up to "other" past MAX_TENANT_LABEL_SERIES, and provider / status /
    output_kind are coerced to "other" when not in the model's enum sets.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

import models

from services import platform_settings as psr  # leaf module: no audit cycle

logger = logging.getLogger(__name__)

REFRESH_SETTING = "metrics.productivity_floor_refresh_s"
REFRESH_S_DEFAULT = 60
REFRESH_S_FLOOR = 15
REFRESH_S_CEIL = 3600


def _validate_refresh_seconds(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) \
            or not REFRESH_S_FLOOR <= value <= REFRESH_S_CEIL:
        raise ValueError(
            f"{REFRESH_SETTING} must be an int in {REFRESH_S_FLOOR}..{REFRESH_S_CEIL}"
        )


psr.register(
    REFRESH_SETTING,
    int,
    REFRESH_S_DEFAULT,
    group="metrics",
    validator=_validate_refresh_seconds,
    description="sz-10.1/10.2 — seconds between recomputations of the "
                "productivity-floor Prometheus gauges (paused_runs_count, "
                "oauth_connections_count, output_delivery_success_rate) by the "
                "per-worker refresh loop (services/productivity_floor_metrics."
                f"refresh_loop). {REFRESH_S_FLOOR}..{REFRESH_S_CEIL}, default "
                f"{REFRESH_S_DEFAULT}.",
)

# Bounded cardinality for the per-tenant / per-workflow label series: after
# this many distinct series, further ids roll up into the "other" label value.
MAX_TENANT_LABEL_SERIES = 200
MAX_WORKFLOW_LABEL_SERIES = 500

# Known label vocabularies — straight from the models.py CheckConstraints so a
# future value added to a table cannot silently mint a new Prometheus series.
OAUTH_PROVIDERS = frozenset(
    {"google", "github", "notion", "linear", "slack", "airtable", "microsoft365"}
)
OAUTH_STATUSES = frozenset({"active", "refresh_failed", "reconnect_required", "revoked"})
DELIVERY_CHANNELS = frozenset({"slack", "discord", "email", "sms", "push", "webhook", "file"})
DELIVERY_STATUSES = frozenset({"delivered", "failed", "retrying", "skipped"})

SUCCESS_STATUSES = frozenset({"delivered"})

_M_PAUSED_RUNS: Any = None        # Gauge[tenant_id]
_M_OAUTH_CONNECTIONS: Any = None  # Gauge[provider, status]
_M_DELIVERY_RATE: Any = None      # Gauge[output_kind]
_M_CRDT_EDITORS: Any = None       # Gauge[workflow_id]
_M_COST_FORECAST: Any = None      # Histogram
_M_DEBUGGER_PAUSE: Any = None     # Histogram
_M_TOKEN_REFRESH: Any = None      # Histogram
_M_ROUTER_SAVINGS: Any = None     # Counter[tenant_id]
_M_PREEMPTIONS: Any = None        # Counter[tenant_id]
_TENANT_SERIES: Dict[str, str] = {}
_WORKFLOW_SERIES: Dict[str, str] = {}
_SERIES_LOCK = threading.Lock()
_INITIALISED = False


def _init() -> None:
    """Lazy register. Idempotent: re-registering an existing metric name
    raises ValueError in prometheus_client; we catch + tolerate so tests,
    module reloads and a second uvicorn worker in the same process never
    blow up with "Duplicated timeseries in CollectorRegistry"."""
    global _INITIALISED, _M_PAUSED_RUNS, _M_OAUTH_CONNECTIONS, _M_DELIVERY_RATE
    global _M_CRDT_EDITORS, _M_COST_FORECAST, _M_DEBUGGER_PAUSE, _M_TOKEN_REFRESH
    global _M_ROUTER_SAVINGS, _M_PREEMPTIONS
    if _INITIALISED:
        return
    try:
        from prometheus_client import Counter, Gauge, Histogram
    except Exception:
        _INITIALISED = True
        return

    def _reg(builder, name, *args, **kw):
        """Check-then-register (same as mesh/wizard/expressiveness): a
        duplicate name — module reload, a second import, another uvicorn
        worker in the same process — raises ValueError in prometheus_client,
        which we tolerate so nothing blows up. The already-registered
        collector stays on REGISTRY and keeps serving /metrics; only the
        reloaded module's local handles go quiet."""
        try:
            return builder(name, *args, **kw)
        except Exception as e:  # noqa: BLE001 — duplicate registration on reload
            logger.debug("productivity_floor_metrics register failed for %s: %s", name, e)
            return None

    _M_PAUSED_RUNS = _reg(
        Gauge, "paused_runs_count",
        "Workflow runs currently paused (user-pressed pause) per tenant.",
        ["tenant_id"],
    )
    _M_OAUTH_CONNECTIONS = _reg(
        Gauge, "oauth_connections_count",
        "Live external-provider connections per tenant by provider and status.",
        ["provider", "status"],
    )
    _M_DELIVERY_RATE = _reg(
        Gauge, "output_delivery_success_rate",
        "Share of output-delivery attempts that landed, per output kind, over "
        "the last 24h. Absent (not 0) when the window has no attempts.",
        ["output_kind"],
    )
    _M_CRDT_EDITORS = _reg(
        Gauge, "crdt_concurrent_editors_count",
        "Concurrent editors on a workflow canvas (set by the WS layer; no "
        "source of truth in the DB yet).",
        ["workflow_id"],
    )
    _M_COST_FORECAST = _reg(
        Histogram, "cost_forecaster_duration_seconds",
        "Wall-clock seconds a cost-forecaster estimate took.",
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    )
    _M_DEBUGGER_PAUSE = _reg(
        Histogram, "live_debugger_pause_duration_seconds",
        "Wall-clock seconds a run stayed paused under the live debugger.",
        buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
    )
    _M_TOKEN_REFRESH = _reg(
        Histogram, "oauth_token_refresh_duration_seconds",
        "Wall-clock seconds an OAuth token refresh round-trip took.",
        buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
    )
    _M_ROUTER_SAVINGS = _reg(
        Counter, "cost_aware_router_savings_total",
        "Credits saved by the cost-aware router: (most-expensive candidate "
        "expected cost - chosen expected cost), accumulated per tenant, only "
        "when the saving is positive.",
        ["tenant_id"],
    )
    _M_PREEMPTIONS = _reg(
        Counter, "spot_preemption_count",
        "Preemptions of harvested/spot capacity (NOAH harvest-return is the "
        "real source today; the tracker spells it preemption), per tenant.",
        ["tenant_id"],
    )
    _INITIALISED = True


def register_productivity_floor_metrics() -> bool:
    """Register the §10.1/§10.2 metrics on the default REGISTRY. Idempotent."""
    _init()
    return any(h is not None for h in (
        _M_PAUSED_RUNS, _M_OAUTH_CONNECTIONS, _M_DELIVERY_RATE, _M_CRDT_EDITORS,
        _M_COST_FORECAST, _M_DEBUGGER_PAUSE, _M_TOKEN_REFRESH,
        _M_ROUTER_SAVINGS, _M_PREEMPTIONS,
    ))


# ---------------------------------------------------------------------
# Label guards — every emitted label value must come from a bounded set
# ---------------------------------------------------------------------

def _int_label(value: Any) -> Optional[str]:
    """tenant_id / workflow_id labels: plain ints as str, nothing else.

    A non-int (uuid, object id, attacker-supplied string) is dropped by the
    caller rather than minting an unbounded series."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return str(n)


def _bounded_label(series: Dict[str, str], key: str, cap: int) -> str:
    """Roll a label value into the bounded series map, past `cap` into "other"."""
    with _SERIES_LOCK:
        if key not in series:
            if len(series) >= cap:
                return "other"
            series[key] = key
    return key


def _known(value: Any, vocab: frozenset, *, fallback: str = "other") -> str:
    """provider / status / output_kind labels: known vocabulary or "other"."""
    s = str(value).strip().lower() if value is not None else ""
    return s if s in vocab else fallback


def _clear(gauge: Any) -> None:
    """Reset a gauge so a refresh cannot leave stale label series behind."""
    if gauge is None:
        return
    try:
        gauge.clear()
    except Exception:  # noqa: BLE001 — never raise out of a metrics helper
        pass


# ---------------------------------------------------------------------
# DB-derived gauges (the mesh_metrics.refresh_from_db pattern)
# ---------------------------------------------------------------------

def refresh_gauges(db: Session, *, window_hours: int = 24) -> dict:
    """Recompute the DB-backed productivity gauges from `db`.

    Returns a summary dict (the same shape mesh_metrics.refresh_from_db
    returns) so the wiring task's scheduler + the status endpoint can log
    what it collected. Every section is independently best-effort: a broken
    section logs and continues, it never raises out.
    """
    _init()
    summary: Dict[str, Any] = {
        "paused_runs": {}, "oauth": {}, "delivery": {}, "crdt": "setter-only",
    }

    try:
        counts: Dict[str, int] = {}
        rows = db.query(
            models.WorkflowRun.tenant_id, func.count(models.WorkflowRun.id),
        ).filter(
            models.WorkflowRun.deleted_at.is_(None),
            models.WorkflowRun.paused.is_(True),
        ).group_by(models.WorkflowRun.tenant_id).all()
        for tenant_id, n in rows:
            key = _int_label(tenant_id)
            if key is None:
                continue
            key = _bounded_label(_TENANT_SERIES, key, MAX_TENANT_LABEL_SERIES)
            counts[key] = counts.get(key, 0) + n
        _clear(_M_PAUSED_RUNS)
        for tenant_id, n in counts.items():
            try:
                _M_PAUSED_RUNS.labels(tenant_id=tenant_id).set(n)
            except Exception:  # noqa: BLE001
                pass
        summary["paused_runs"] = counts
    except Exception as e:  # noqa: BLE001
        logger.debug("productivity_floor_metrics paused-runs refresh failed: %s", e)

    try:
        ocounts: Dict[tuple, int] = {}
        rows = db.query(
            models.OAuthConnection.provider, models.OAuthConnection.status,
            func.count(models.OAuthConnection.id),
        ).filter(
            models.OAuthConnection.deleted_at.is_(None),
        ).group_by(
            models.OAuthConnection.provider, models.OAuthConnection.status,
        ).all()
        for provider, status, n in rows:
            key = (_known(provider, OAUTH_PROVIDERS), _known(status, OAUTH_STATUSES))
            ocounts[key] = ocounts.get(key, 0) + n
        _clear(_M_OAUTH_CONNECTIONS)
        for (provider, status), n in ocounts.items():
            try:
                _M_OAUTH_CONNECTIONS.labels(provider=provider, status=status).set(n)
            except Exception:  # noqa: BLE001
                pass
        summary["oauth"] = {f"{p}/{s}": n for (p, s), n in ocounts.items()}
    except Exception as e:  # noqa: BLE001
        logger.debug("productivity_floor_metrics oauth refresh failed: %s", e)

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(window_hours)))
        totals: Dict[str, list] = {}
        rows = db.query(
            models.OutputDeliveryLog.channel,
            models.OutputDeliveryLog.status,
            func.count(models.OutputDeliveryLog.id),
        ).filter(
            models.OutputDeliveryLog.created_at >= since,
        ).group_by(
            models.OutputDeliveryLog.channel, models.OutputDeliveryLog.status,
        ).all()
        for channel, status, n in rows:
            kind = _known(channel, DELIVERY_CHANNELS)
            bucket = totals.setdefault(kind, [0, 0])
            bucket[1] += n
            if _known(status, DELIVERY_STATUSES) in SUCCESS_STATUSES:
                bucket[0] += n
        _clear(_M_DELIVERY_RATE)
        rates: Dict[str, float] = {}
        for kind, (ok, total) in totals.items():
            if total <= 0:  # zero rows in the window -> do not emit at all
                continue
            rate = round(ok / total, 6)
            try:
                _M_DELIVERY_RATE.labels(output_kind=kind).set(rate)
            except Exception:  # noqa: BLE001
                pass
            rates[kind] = rate
        summary["delivery"] = rates
    except Exception as e:  # noqa: BLE001
        logger.debug("productivity_floor_metrics delivery refresh failed: %s", e)

    return summary


# ---------------------------------------------------------------------
# Setters / observers
# ---------------------------------------------------------------------

def set_crdt_editors(workflow_id: Any, editors: Any) -> None:
    """Set the concurrent-editor count for one workflow canvas.

    There is no source of truth for this yet (the WS layer of sz-3.2 owns it),
    so this is the only writer; refresh_gauges deliberately leaves it alone."""
    _init()
    if _M_CRDT_EDITORS is None:
        return
    try:
        key = _int_label(workflow_id)
        if key is None:
            return
        key = _bounded_label(_WORKFLOW_SERIES, key, MAX_WORKFLOW_LABEL_SERIES)
        n = max(0, int(editors)) if editors is not None else 0
        _M_CRDT_EDITORS.labels(workflow_id=key).set(n)
    except Exception:  # noqa: BLE001 — never raise out of a metrics helper
        pass


def observe_cost_forecaster_duration(seconds: Any) -> None:
    """Record how long a cost-forecaster estimate took."""
    _init()
    if _M_COST_FORECAST is None:
        return
    try:
        _M_COST_FORECAST.observe(max(0.0, float(seconds)))
    except Exception:  # noqa: BLE001
        pass


def observe_live_debugger_pause_duration(seconds: Any) -> None:
    """Record how long a run stayed paused under the live debugger."""
    _init()
    if _M_DEBUGGER_PAUSE is None:
        return
    try:
        _M_DEBUGGER_PAUSE.observe(max(0.0, float(seconds)))
    except Exception:  # noqa: BLE001
        pass


def observe_oauth_token_refresh_duration(seconds: Any) -> None:
    """Record how long an OAuth token refresh round-trip took."""
    _init()
    if _M_TOKEN_REFRESH is None:
        return
    try:
        _M_TOKEN_REFRESH.observe(max(0.0, float(seconds)))
    except Exception:  # noqa: BLE001
        pass


def refresh_seconds(db: Optional[Session] = None) -> int:
    """`metrics.productivity_floor_refresh_s`, clamped; a settings outage or
    an out-of-range stored value falls back to REFRESH_S_DEFAULT."""
    try:
        v = int(psr.get_setting(REFRESH_SETTING, db=db))
    except Exception:  # noqa: BLE001 — settings outage falls back to default
        return REFRESH_S_DEFAULT
    return v if REFRESH_S_FLOOR <= v <= REFRESH_S_CEIL else REFRESH_S_DEFAULT


async def refresh_loop(session_factory, *, refresh_s: Optional[int] = None) -> None:
    """Per-WORKER background loop that repopulates the DB-derived gauges
    (mesh_metrics.refresh_loop / egress_denial_metrics.refresh_loop shape):
    own session per tick, closed in `finally`, exceptions logged (never
    raised) so a hiccup doesn't kill the loop. `refresh_s` defaults to the
    platform setting `metrics.productivity_floor_refresh_s`, re-read every
    tick so an admin change takes effect without a restart."""
    while True:
        interval = refresh_s
        try:
            db = session_factory()
            try:
                if interval is None:
                    interval = refresh_seconds(db)
                refresh_gauges(db)
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — broken factory or refresh
            logger.warning("productivity_floor_metrics: refresh failed: %s", e)
        if interval is None:
            interval = REFRESH_S_DEFAULT
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("productivity_floor_metrics: cancelled, exiting")
            return


# ---------------------------------------------------------------------
# §10.1b — call-site counters (router savings + spot preemption)
# ---------------------------------------------------------------------

def savings_from_pick(pick_result: Any) -> float:
    """Credits the cost-aware router saved on one pick: max candidate
    expected_cost_credits minus the chosen variant's, 0.0 when not derivable.

    Pure — no metrics, no raise. Reads only the shape services/cost_aware_router
    .pick_variant returns (chosen_variant + candidates[]). A candidate with no
    expected_cost_credits (ineligible / unbenchmarked) cannot be the expensive
    end of the comparison, so it is skipped; a single candidate saves 0."""
    try:
        if not isinstance(pick_result, dict):
            return 0.0
        chosen = pick_result.get("chosen_variant")
        candidates = pick_result.get("candidates")
        if not isinstance(candidates, list):
            return 0.0
        chosen_cost = None
        costs: list = []
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            cost = entry.get("expected_cost_credits")
            if cost is None or isinstance(cost, bool):
                continue
            try:
                cost = float(cost)
            except (TypeError, ValueError):
                continue
            if cost != cost or cost in (float("inf"), float("-inf")):
                continue
            costs.append(cost)
            if entry.get("variant") == chosen:
                chosen_cost = cost
        if chosen_cost is None or not costs:
            return 0.0
        return max(0.0, max(costs) - chosen_cost)
    except Exception:  # noqa: BLE001 — pure helper, never raises
        return 0.0


def record_router_savings(tenant_id: Any, saved_credits: Any) -> None:
    """Accumulate credits saved by the cost-aware router for one tenant.

    Bad inputs are ignored: a non-tenant label, a non-number, NaN and any
    saving <= 0 mint no series. Never raises."""
    _init()
    if _M_ROUTER_SAVINGS is None:
        return
    try:
        key = _int_label(tenant_id)
        if key is None:
            return
        key = _bounded_label(_TENANT_SERIES, key, MAX_TENANT_LABEL_SERIES)
        value = float(saved_credits)
        if value != value or value in (float("inf"), float("-inf")) or value <= 0:
            return
        _M_ROUTER_SAVINGS.labels(tenant_id=key).inc(value)
    except Exception:  # noqa: BLE001 — never raise out of a metrics helper
        pass


def record_preemption(tenant_id: Any) -> None:
    """Count one preemption of harvested/spot capacity (NOAH harvest-return)."""
    _init()
    if _M_PREEMPTIONS is None:
        return
    try:
        key = _int_label(tenant_id)
        if key is None:
            return
        key = _bounded_label(_TENANT_SERIES, key, MAX_TENANT_LABEL_SERIES)
        _M_PREEMPTIONS.labels(tenant_id=key).inc()
    except Exception:  # noqa: BLE001 — never raise out of a metrics helper
        pass
