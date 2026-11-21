"""FX.103 p3a (owner decision Q89=A) — the node RETIREMENT SCHEDULER: a
deprecated node stays RUNNABLE for at least `retirement.min_runnable_months`
(default 12 months) after `deprecated_at` before it may be flipped to
'retired', and only when no blocking condition holds.

Part 1 (services/node_deprecation.py) owns the lifecycle WRITES
(`apply_node_status` = the single write path + the NODE_RETIRED audit row)
and the write-time floors. Part 2 (services/node_retirement.py) owns the
notice emails and the dispatch run guard. THIS part owns the retirement
DECISION at run time:

* `run_retirement_schedule(db, now=...)` — the daily APScheduler tick
  (services/scheduler.py job `fx103p3a_node_retirement`, 04:15 UTC):
    (a) candidates = node_definitions in status 'deprecated' that are not
        soft-deleted (rows already 'retired' are skipped = idempotent);
    (b) the WINDOW: the node is due only once
        `now >= deprecated_at + effective_window_months(db)`. When the row
        carries a `retire_after`, that date must ALSO have passed (the
        admin's explicit retirement date can move the flip later, never
        earlier than the window — the window is the hard floor);
    (c) the BLOCKING CONDITIONS: a node still used by an ACTIVE schedule
        (a non-deleted workflow carrying an active SCHEDULE trigger) is
        HELD — the sweep reports it (`held` + per-node reason) and leaves
        the row untouched, because silently retiring a node the tenant has
        scheduled to fire on a cron would break a standing production
        pipeline with no way to see it;
    (d) the flip: status -> 'retired' (SOFT — nothing is deleted, the row
        and every existing run/workflow node stay intact) through the part-1
        write path, which writes the NODE_RETIRED audit row per transition.

* Retired = NOT OFFERED FOR NEW WORKFLOWS (the palette/lifecycle state the
  UI reads) while EXISTING runs keep working: existing Workflow /
  WorkflowNode / WorkflowRun / NodeRun rows are never touched here, and the
  part-2 dispatch guard (services/node_retirement.run_guard_violation) is
  what fails a NEW dispatch after the date.

* Report / dry-run mode — `retirement.dry_run` (bool, default False). When
  True the sweep changes NOTHING (no status flip, no audit row) and returns
  the same report with the flips counted as `would_retire`, so an admin can
  see exactly what the next tick does before it happens.

* The window itself is validated: `enforce_window_minimum` (called from
  the platform-settings set_setting write path, next to the fleet gates —
  a value-shape validator cannot see the session the write commits into)
  refuses a `retirement.min_runnable_months` below the 12-month minimum
  unless the platform override flag
  `retirement.allow_below_minimum_window` is turned ON deliberately — an
  install that wants a shorter runway has to say so explicitly, and that
  write is audited like every other setting change (ISO 27001).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import models
from sqlalchemy.orm import Session

try:
    from services import node_deprecation as _dep
except (ImportError, ModuleNotFoundError):
    _dep = None

logger = logging.getLogger(__name__)

MINIMUM_WINDOW_MONTHS = 12
WINDOW_SETTING_KEY = "retirement.min_runnable_months"
WINDOW_FALLBACK_KEYS = ("lifecycle.min_runnable_months",
                       "deprecation.min_runnable_months")
OVERRIDE_SETTING_KEY = "retirement.allow_below_minimum_window"
DRY_RUN_SETTING_KEY = "retirement.dry_run"
BLOCK_ACTIVE_SCHEDULES = "active_schedule"


class RetirementWindowError(ValueError):
    """The configured runnable window is below the 12-month minimum while
    the platform override flag is off. `code` is stable for the router:
      * 'window_below_minimum' — months < 12 and no override
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive legacy timestamps read as UTC; aware stay as-is."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _setting(key: str, db: Optional[Session], default):
    """Read one registry key, preferring the caller's session. Defensive:
    a broken registry must not take the sweep down (same spirit as
    services/node_deprecation._lifecycle_months)."""
    try:
        from services import platform_settings as psr
        from database import SessionLocal
        own = db is None
        if own:
            db = SessionLocal()
        try:
            return psr.get_setting(key, db=db)
        finally:
            if own:
                db.close()
    except Exception:
        return default


def _db_value(key: str, db: Optional[Session]):
    """(value, True) when the key has a LIVE DB row, (None, False) when it
    only resolves to its default/env fallback — the registry's `get_setting`
    happily returns the default, which is not the same as "this part set it",
    so the legacy-key fallback has to ask where the value came from."""
    try:
        from services import platform_settings as psr
        from database import SessionLocal
        own = db is None
        if own:
            db = SessionLocal()
        try:
            value, src = psr.resolve_source(key, db)
            return value, src == "db"
        finally:
            if own:
                db.close()
    except Exception:
        return None, False


def effective_window_months(db: Optional[Session] = None) -> int:
    """The runnable window in months: `retirement.min_runnable_months`
    (this part), falling back to the part-2 `lifecycle.*` then part-1
    `deprecation.*` keys so the three parts can never disagree. A value
    below MINIMUM_WINDOW_MONTHS is only honoured when the platform
    override flag is ON — otherwise it is raised to the minimum (the
    sweep must never run a shorter runway than the owner decision allows,
    even if a raw INSERT bypassed the settings validator)."""
    value = None
    for key in (WINDOW_SETTING_KEY,) + WINDOW_FALLBACK_KEYS:
        candidate, from_db = _db_value(key, db)
        if from_db:
            value = candidate
            break
    if value is None:
        value = MINIMUM_WINDOW_MONTHS
    try:
        months = max(1, int(value))
    except (TypeError, ValueError):
        months = MINIMUM_WINDOW_MONTHS
    if months < MINIMUM_WINDOW_MONTHS:
        if not bool(_setting(OVERRIDE_SETTING_KEY, db, False)):
            logger.warning(
                "FX.103 p3a: runnable window configured as %d months, below "
                "the %d-month minimum — raising to the minimum (turn "
                "%s on to allow it deliberately)",
                months, MINIMUM_WINDOW_MONTHS, OVERRIDE_SETTING_KEY)
            return MINIMUM_WINDOW_MONTHS
    return months


def enforce_window_minimum(value, db: Optional[Session] = None,
                           actor_user_id: Optional[int] = None) -> None:
    """The POLICY half of the window rule, called from the
    platform-settings set_setting write path (a value-shape validator
    cannot see the session the write commits into, so the gate lives here —
    same shape as the fleet gates in that module).

    Refuses a `retirement.min_runnable_months` below MINIMUM_WINDOW_MONTHS
    (12, owner decision Q89=A) unless the platform override flag
    `retirement.allow_below_minimum_window` is already ON. Raises
    RetirementWindowError (a ValueError, so the settings router maps it to
    400 with the reason as the message).

    The flag is read from the SAME session the settings write is
    committing into, so an admin cannot smuggle a short window by flipping
    the flag in a separate request that fails afterwards."""
    v = int(value)
    if v < 1 or v > 120:
        raise ValueError("retirement months must be 1..120 (got "
                         f"{v})")
    if v < MINIMUM_WINDOW_MONTHS:
        if not bool(_setting(OVERRIDE_SETTING_KEY, db, False)):
            raise RetirementWindowError(
                "window_below_minimum",
                f"a runnable window below {MINIMUM_WINDOW_MONTHS} months "
                "is refused (owner decision Q89=A: a deprecated node stays "
                "runnable for at least 12 months). Turn "
                f"{OVERRIDE_SETTING_KEY} on deliberately if this install "
                "really wants a shorter runway.",
            )


def blocking_conditions(db: Session, nd: "models.NodeDefinition") -> list:
    """The conditions that HOLD a due node back from retirement. Returns a
    list of {code, detail} dicts (empty = free to retire). Reported by the
    sweep so the admin sees WHY a node is stuck instead of silence.

    Only one blocking condition exists today:

    * `active_schedule` — the node is used by a workflow that carries an
      ACTIVE SCHEDULE trigger (a standing cron pipeline). Retiring it would
      silently break a pipeline that fires on its own, with nobody in front
      of a canvas to notice the failure, so the sweep holds the node and
      reports it until the tenant re-points or disables that schedule.
      Disabled/soft-deleted triggers and soft-deleted workflows do not
      count (a legacy row whose is_active is NULL counts as active — the
      column has no server_default).
    """
    out = []
    if nd.deprecated_at is None:
        # A deprecated row with no clock cannot be measured against the
        # window — hold rather than invent a date.
        out.append({"code": "missing_deprecated_at",
                    "detail": "no deprecated_at to measure the window from"})
        return out
    try:
        rows = (
            db.query(models.Workflow.id, models.WorkflowTrigger.id)
            .join(models.WorkflowNode,
                  models.WorkflowNode.workflow_id == models.Workflow.id)
            .join(models.WorkflowTrigger,
                  models.WorkflowTrigger.workflow_id == models.Workflow.id)
            .filter(models.WorkflowNode.node_definition_id == nd.id,
                    models.Workflow.deleted_at.is_(None),
                    models.WorkflowTrigger.deleted_at.is_(None),
                    models.WorkflowTrigger.trigger_type
                    == models.WorkflowTriggerType.SCHEDULE)
            .filter(models.WorkflowTrigger.is_active.is_(True)
                    | models.WorkflowTrigger.is_active.is_(None))
            .all()
        )
    except Exception as e:  # noqa: BLE001 — fail CLOSED: an unreadable
        # schedule scan must not retire a node in front of a possibly-live
        # pipeline.
        logger.warning("FX.103 p3a: schedule scan for node %s failed: %s",
                       nd.id, e)
        return [{"code": "schedule_scan_unavailable",
                "detail": f"schedule scan raised: {e}"}]
    if rows:
        out.append({
            "code": BLOCK_ACTIVE_SCHEDULES,
            "detail": (f"used by {len(rows)} active scheduled workflow(s) "
                       f"(workflow ids {sorted({r[0] for r in rows})})"),
        })
    return out


def run_retirement_schedule(db: Session,
                            now: Optional[datetime] = None,
                            *,
                            dry_run: Optional[bool] = None,
                            actor_user_id: Optional[int] = None) -> dict:
    """One retirement tick. Returns the report (never raises).

    Window: due only at `deprecated_at + effective_window_months` (default
    12 months); a row's own `retire_after`, when set, can only push the
    flip LATER (the window is the hard floor, the admin date is the
    appointment). Blocking conditions hold a due node and are reported.
    Flips go through services/node_deprecation.apply_node_status — the
    single write path — so every transition carries its NODE_RETIRED audit
    row, and nothing is deleted (soft state only).

    `dry_run` (or the `retirement.dry_run` setting) reports the flips it
    WOULD make without touching a single row. Idempotent: a node already
    'retired' is not a candidate, so a second tick retires nothing again.
    """
    now = _utc(now) or datetime.now(timezone.utc)
    months = effective_window_months(db)
    dry = (bool(_setting(DRY_RUN_SETTING_KEY, db, False))
           if dry_run is None else bool(dry_run))
    report = {"checked": 0, "not_due": 0, "due": 0, "retired": 0,
              "would_retire": 0, "held": 0, "refused": 0,
              "window_months": months, "dry_run": dry,
              "held_nodes": [], "retired_nodes": []}
    candidates = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.status
                == _dep.NODE_STATUS_DEPRECATED,
                models.NodeDefinition.deleted_at.is_(None))
        .all()
    )
    for nd in candidates:
        report["checked"] += 1
        dep_at = _utc(nd.deprecated_at)
        if dep_at is None:
            report["held"] += 1
            report["held_nodes"].append({
                "node_definition_id": nd.id, "name": nd.name,
                "blockers": blocking_conditions(db, nd)})
            continue
        window_end = _dep.months_after(dep_at, months)
        ra = _utc(nd.retire_after)
        due_on = max(window_end, ra) if ra else window_end
        if now < due_on:
            report["not_due"] += 1
            continue
        report["due"] += 1
        blockers = blocking_conditions(db, nd)
        if blockers:
            report["held"] += 1
            report["held_nodes"].append({
                "node_definition_id": nd.id, "name": nd.name,
                "window_end": window_end.isoformat(),
                "blockers": blockers})
            logger.warning(
                "FX.103 p3a: node %s (%s) is past its %d-month window "
                "(window_end=%s) but HELD — %s",
                nd.id, nd.name, months, window_end.isoformat(),
                "; ".join(f"{b['code']}: {b['detail']}" for b in blockers))
            continue
        entry = {"node_definition_id": nd.id, "name": nd.name,
                 "deprecated_at": dep_at.isoformat(),
                 "window_end": window_end.isoformat(),
                 "retire_after": (ra.isoformat() if ra else None)}
        if dry:
            report["would_retire"] += 1
            report["retired_nodes"].append(entry)
            continue
        # The row's own retire_after wins when it carries one (the admin's
        # appointment, already >= the window because the due date is the
        # later of the two); otherwise the window end is STAMPED as the
        # retirement date, so part 1's is_runnable and part 2's dispatch
        # run guard see a real expiry instead of NULL (NULL would keep a
        # 'retired' node runnable).
        if ra is None:
            nd.retire_after = due_on
        try:
            _dep.apply_node_status(db, nd, status=_dep.NODE_STATUS_RETIRED,
                                   deprecated_at=dep_at, retire_after=None,
                                   actor_user=None)
        except _dep.DeprecationError as e:
            db.rollback()
            report["refused"] += 1
            logger.warning(
                "FX.103 p3a: part-1 validator refused the flip for node %s "
                "(%s): %s", nd.id, nd.name, e)
            continue
        report["retired"] += 1
        report["retired_nodes"].append(entry)
        logger.info(
            "FX.103 p3a: retired node %s (%s) — deprecated_at=%s, window "
            "%d months elapsed on %s; no longer offered for new workflows, "
            "existing runs keep working",
            nd.id, nd.name, dep_at.isoformat(), months, now.isoformat())
    if not dry:
        db.commit()
    return report


def _tick() -> None:
    """APScheduler entry point (services/scheduler.py job
    `fx103p3a_node_retirement`, 04:15 UTC). Never raises into the
    scheduler."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        result = run_retirement_schedule(db)
        if result["retired"] or result["held"] or result["refused"] \
                or result["would_retire"]:
            logger.info("fx103p3a_node_retirement tick: %s", {
                k: v for k, v in result.items()
                if k not in ("held_nodes", "retired_nodes")})
    except Exception as e:  # noqa: BLE001 — a sweep must never be fatal
        db.rollback()
        logger.warning("fx103p3a_node_retirement tick raised: %s", e)
    finally:
        db.close()
