"""Prometheus gauge for the scheduler model's rolling MAE.

`scheduler_prediction_mae_24h{model_version}` exposes the mean
absolute error between `predicted_duration_s` and `actual_duration_s`
over `prediction_logs` rows in the past 24 hours, grouped by
`model_version`. Recomputed on a 5-minute background tick (not on
every Prometheus scrape) to avoid hammering the DB.

Wired into the API's lifespan startup as a single asyncio task.
"""

from __future__ import annotations

import asyncio
from product_names import themed_logger
import os
from typing import Optional

from sqlalchemy import text

logger = themed_logger(__name__, 'scheduler')

REFRESH_S_DEFAULT = 300
REFRESH_S_ENV = "SCHEDULER_MAE_GAUGE_REFRESH_S"

_GAUGE_NAME = "scheduler_prediction_mae_24h"

# Module-level Gauge — created lazily on first call so test imports
# don't double-register if the registry already holds it (which is the
# Prometheus client lib's default behaviour for clean re-registration).
_gauge = None


def _get_gauge():
    global _gauge
    if _gauge is None:
        from prometheus_client import Gauge, REGISTRY

        # Detect a pre-existing collector registered under the same
        # name — happens when the module is reloaded inside the same
        # process (test reruns, hot-reload). Re-use it instead of
        # crashing with `Duplicated timeseries`.
        existing = next(
            (c for c in REGISTRY._collector_to_names if getattr(c, "_name", None) == _GAUGE_NAME),
            None,
        )
        if existing is not None:
            _gauge = existing
        else:
            _gauge = Gauge(
                _GAUGE_NAME,
                "Rolling 24-hour mean absolute error between predicted "
                "and actual task duration, per model_version. Updated "
                "every 5 minutes from prediction_logs.",
                ["model_version"],
            )
    return _gauge


def reset_gauge_for_tests() -> None:
    """Clear the cached gauge handle so the next access re-discovers
    or re-creates it. Test-only helper."""
    global _gauge
    _gauge = None


_MAE_QUERY = text("""
    SELECT model_version,
           AVG(ABS(predicted_duration_s - actual_duration_s)) AS mae
    FROM prediction_logs
    WHERE completed_at > NOW() - INTERVAL '24 hours'
      AND actual_duration_s IS NOT NULL
      AND predicted_duration_s IS NOT NULL
    GROUP BY model_version
""")


def refresh_once(db) -> dict[int, float]:
    """Run the SQL once, set the gauge per model_version, prune retired
    series. Returns the {model_version → mae} map for caller assertions.
    """
    gauge = _get_gauge()
    rows = db.execute(_MAE_QUERY).fetchall()
    seen: dict[int, float] = {int(r.model_version): float(r.mae) for r in rows if r.mae is not None}

    # Set gauge per seen version.
    for model_version, mae in seen.items():
        gauge.labels(model_version=str(model_version)).set(mae)

    # Drop labels no longer present so retired models don't linger.
    try:
        # `_metrics` is a private mapping {label_tuple → child}. Walking
        # it is the documented way to detect retired label tuples; the
        # public API doesn't expose enumeration.
        existing_labels = list(gauge._metrics.keys())
        for label_tuple in existing_labels:
            label_value = label_tuple[0] if label_tuple else None
            if label_value is None:
                continue
            try:
                if int(label_value) not in seen:
                    gauge.remove(*label_tuple)
            except (TypeError, ValueError):
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug("scheduler_mae_gauge: prune step failed (non-fatal): %s", e)

    return seen


async def refresh_loop(session_factory, *, refresh_s: Optional[int] = None,
                       stop_event: Optional[asyncio.Event] = None) -> None:
    """Long-running asyncio task. Calls `refresh_once` every refresh_s
    seconds. Logs but never raises on DB / Prometheus errors so a
    transient hiccup doesn't kill the loop.
    """
    if refresh_s is None:
        try:
            refresh_s = int(os.getenv(REFRESH_S_ENV, REFRESH_S_DEFAULT))
        except ValueError:
            refresh_s = REFRESH_S_DEFAULT

    logger.info("scheduler_mae_gauge: refresh loop starting (interval=%ds)", refresh_s)

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("scheduler_mae_gauge: stop signalled, exiting")
            return
        try:
            db = session_factory()
            try:
                seen = refresh_once(db)
                if seen:
                    logger.debug("scheduler_mae_gauge: refreshed %d model_versions: %s",
                                 len(seen), seen)
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler_mae_gauge: refresh failed: %s", e)

        try:
            await asyncio.sleep(refresh_s)
        except asyncio.CancelledError:
            logger.info("scheduler_mae_gauge: cancelled, exiting")
            return
